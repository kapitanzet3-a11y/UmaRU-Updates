# -*- coding: utf-8 -*-
"""Лаунчер Uma RU — окно и командная строка для игроков.

Окно почти без настроек и всё делает само:
  • находит игру в Steam (если нет — одна кнопка «указать папку»);
  • одной кнопкой ставит Hachimi и готовый перевод;
  • при каждом открытии сама проверяет, цел ли Hachimi и перевод, и после обновления игры чинит их без вопросов;
  • «Починить после обновления» — полная проверка: Hachimi ставится заново, файлы перевода сверяются по хэшам, кэш картинок очищается;
  • «Играть на русском» (и ярлык с --play) — проверка, починка при необходимости и запуск игры через Steam.

Командная строка (для проверки и для тех, кому окно не нужно; в окне-версии вывода нет, результат пишется в --json):
  --check | --install | --repair | --uninstall | --uninstall-all [--keep-program]   --deep   --json ФАЙЛ   --game-dir ПАПКА   --pack-dir ПАПКА
  --play (ярлык: починить при необходимости и запустить игру)   --auto (сам выполнить нужное и остаться в окне)
"""
import argparse
import ctypes
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import umaru_core as core  # noqa: E402
import umaru_updates as updates  # noqa: E402
import umaru_launcher as launcher  # noqa: E402
from _launcher_version import APP_VERSION  # noqa: E402

APP_TITLE = "Лаунчер Uma RU"          # одно имя везде: окно, меню «Пуск», установщик, README
SHORTCUT_NAME = "Играть на русском (Uma RU)"
INSTANCE_MUTEX = "UmaRUPlayerSingleInstance"     # это же имя проверяет деинсталлятор в setup/UmaRU.iss
SHOW_EVENT = "UmaRUPlayerShowWindow"             # вторая копия просит открытое окно показаться
PLAY_EVENT = "UmaRUPlayerPlay"                   # ... показаться и запустить игру (ярлык с --play)
LAUNCHER_DOWNLOADS = Path(tempfile.gettempdir()) / "UmaRU-launcher"
LAUNCHER_SNOOZE = 3 * 24 * 3600                  # «Позже» в предложении нового лаунчера: не спрашивать три дня
LAUNCHER_WAIT = 4.0                              # перед запуском игры ждём фоновую проверку версии не дольше, с
_ADMIN_HINT = ("\nЗапустите UmaRU.exe от имени администратора (правый щелчок по нему → «Запуск от имени администратора») "
               "и повторите удаление.")
log = logging.getLogger("umaru")


def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else HERE.parent


def pack_root():
    env = os.environ.get("UMA_RU_PACK")
    return Path(env) if env else app_dir() / "data"


def program_info():
    """Что считается «самой программой» при полном удалении. None — запуск из исходников: папку с исходниками не трогаем.
    Переменная UMA_RU_PROGRAM_DIR нужна только проверкам: показать на чужой папке, не собирая exe."""
    frozen = getattr(sys, "frozen", False)
    env = os.environ.get("UMA_RU_PROGRAM_DIR")
    if not frozen and not env:
        return None
    folder = Path(env) if env else app_dir()
    return {"dir": folder, "exe": Path(sys.executable) if frozen else folder / "UmaRU.exe",
            "shortcut": SHORTCUT_NAME, "pid": os.getpid(), "uninstaller": core.find_uninstaller(folder)}


def hachimi_installer():
    """Установщик Hachimi рядом с программой: им снимаем Hachimi, даже если пакета перевода в кэше уже нет."""
    return pack_root() / "hachimi_installer.exe"


def setup_logging():
    home = core.app_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(home / "umaru.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("umaru")
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


def short_path(path, limit=64):
    """Длинный путь для показа: диск, начало и последние две папки («C:\\Games\\…\\common\\UmamusumePrettyDerby_Jpn»)."""
    text = str(path)
    if len(text) <= limit:
        return text
    parts = Path(text).parts
    if len(parts) <= 4:
        return text
    return str(Path(*parts[:2])) + "\\…\\" + "\\".join(parts[-2:])


def pretty_date(version):
    """2026.10.29-1530 -> 29.10.2026."""
    try:
        y, m, d = version.split("-")[0].split(".")
        return f"{int(d):02d}.{int(m):02d}.{y}"
    except (ValueError, AttributeError):
        return version or "—"


def wanted_extras(choices):
    """Дополнительные пакеты, которые нужны для выбора игрока: английские комиксы качаем, только если их выбрали."""
    return ("comics_en",) if choices.get("comics") == "en" else ()


def cached_extras(data_dir, names):
    channel = updates.read_channel(data_dir)
    return {name: updates.cached_extra(data_dir, channel, name) if channel else None for name in names}


def check_channel(data_dir, game, *, download=True, progress=None, extras=()):
    channel = updates.read_channel(data_dir)
    if channel is None:
        return updates.Result(core.load_pack(data_dir), "error", "Не удалось прочитать адрес обновлений. Переустановите лаунчер.")
    if download:
        return updates.resolve(data_dir, channel, game_dir=game.path if game else None, progress=progress, extras=extras)
    return updates.Result(updates.cached_pack(data_dir, channel), "local", "", extras=cached_extras(data_dir, extras))


def launcher_snoozed(version, now=None):
    """Игрок нажал «Позже» для этой версии лаунчера меньше трёх дней назад."""
    later = core.load_settings().get("launcher_later")
    now = time.time() if now is None else now
    return (isinstance(later, dict) and later.get("version") == version
            and isinstance(later.get("until"), (int, float)) and now < later["until"])


def snooze_launcher(version, now=None):
    now = time.time() if now is None else now
    core.save_settings({"launcher_later": {"version": version, "until": now + LAUNCHER_SNOOZE}})


def choices_summary(choices):
    """«комиксы — английский, ролик сюжета — японский»; пусто, если всё на русском."""
    return ", ".join(f"{core.CONTENT_PARTS[part][0].lower()} — {core.LANGUAGE_NAMES[lang].lower()}"
                     for part, lang in choices.items() if lang != "ru")


# ---------------------------------------------------------------- командная строка (без окна)

def run_cli(args, pack):
    game = core.find_game(args.game_dir)
    data_dir = pack_root()
    online = (data_dir / "update-channel.json").is_file()
    result = {"app": APP_VERSION}
    code = 0
    removal = None
    choices = core.load_choices()
    extras = cached_extras(data_dir, wanted_extras(choices)) if online else {}
    try:
        if online:
            check = check_channel(data_dir, game, download=args.update_cache or bool(game and (args.install or args.repair)),
                                  extras=wanted_extras(choices))
            pack, extras = check.pack, check.extras
            result.update(update_state=check.state, update_message=check.message, release=check.tag)
        if args.update_cache and not online:
            raise core.UmaError("Для этого лаунчера не задан канал обновлений.")
        result['pack'] = pack.version if pack else None
        result['content'] = choices
        pack = core.compose_pack(pack, choices, comics_en=extras.get("comics_en"))
        ctx = core.Context(game, pack, online=online, installer=hachimi_installer())
        if args.update_cache:
            if result.get('update_state') not in ('updated', 'current'):
                raise core.UmaError(result.get('update_message') or 'Не удалось проверить обновления.')
            if args.json:
                Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            return 0
        report = core.diagnose(ctx, deep=args.deep)
        result.update(mode=report.mode, plan=report.plan, blocked=report.blocked,
                      checks=[c.__dict__ for c in report.checks])
        if args.install:
            if online and report.mode in ("no_game", "blocked", "need_channel"):
                raise core.UmaError(report.blocked or ("Игра не найдена." if game is None else "Повторите проверку канала обновлений."))
            plan = report.plan if online else report.plan or ["hachimi", "pack", "config"]
            result["done"] = core.apply_plan(ctx, plan, deep=args.deep) if plan else []
        elif args.repair:
            result["done"] = core.apply_plan(ctx, ["hachimi", "pack", "config", "caches"], deep=True)
        elif args.uninstall:
            result["done"] = core.uninstall(ctx)
        elif args.uninstall_all:
            result["done"], removal = core.uninstall_all(ctx, None if args.keep_program else program_info())
            result["program_removal"] = ("uninstaller" if removal and removal.get("uninstaller")
                                         else "scheduled" if removal else "none")
        if args.install or args.repair or args.uninstall or args.uninstall_all:
            after = core.diagnose(core.Context(core.find_game(args.game_dir), pack, online=online), deep=True)
            result["mode_after"] = after.mode
            result["plan_after"] = after.plan
    except core.UmaError as e:
        result["error"] = str(e)
        code = 1
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if removal:
        core.schedule_program_removal(removal)      # удалит программу, когда этот процесс завершится
    return code


# ---------------------------------------------------------------- окно

def relaunch_admin(extra=("--elevated",)):
    """Тот же запуск с правами администратора (запрос Windows). После вызова текущее окно надо закрыть."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, list(sys.argv[1:])
    else:
        exe, params = sys.executable, [str(Path(__file__).resolve())] + sys.argv[1:]
    params += [a for a in extra if a not in params]
    quoted = " ".join(f'"{p}"' if " " in p else p for p in params)
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, quoted, None, 1) > 32


def build_window(args, pack):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import ui_kit as K

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            K.apply_theme(self)
            self.title(APP_TITLE)
            self.withdraw()
            self.q = queue.Queue()
            self.report = None
            self.game = None
            self.busy = False
            self.auto_done = False
            self.banner_note = None
            self.last_summary = ("busy", "Проверяю…")   # главный статус, к которому окно вернётся после занятости
            self.raw_pack = pack           # пакет как скачан; self.pack_ — он же по выбору языков (compose_pack)
            self.extras = {}               # дополнительные пакеты (английские комиксы)
            self.offers = None             # что канал предлагает докачать; None — канал ещё не проверяли
            self.apply_requested = False   # игрок сменил языки: применить после проверки
            self.launcher_offer = None     # новая версия лаунчера, найденная проверкой при запуске
            self.launcher_checked = False  # фоновая проверка версии лаунчера закончилась
            self.launcher_started = time.monotonic()
            self.launcher_prompted = False # предложение уже показывали в этот запуск
            self.launcher_waiting = False  # запуск игры ждёт окончания проверки версии
            self.launching = False
            self.pack_ = core.compose_pack(pack, core.load_choices())
            self.online = (pack_root() / "update-channel.json").is_file()
            self.channel_checked = False
            self.channel_result = None
            self.reveal_job = None
            self.play_requested = False   # ярлык «Играть» нажали, пока окно уже было открыто
            self.width = K.px(560)
            K.center_window(self, 560, 360)
            self.minsize(K.px(480), K.px(200))
            self._build()
            self._fit_height()             # высота по содержимому ещё до показа: окно не прыгает при открытии
            K.dark_title_bar(self, K.THEME.dark)
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._poll_job = self.after(60, self._poll)
            hidden = args.play and not args.shot
            if not hidden:
                self.deiconify()
            self.after(120, self.refresh)
            if self.online and not args.shot:
                self._check_launcher_quietly()
            else:
                self.launcher_checked = True
            if args.shot and args.shot_after:   # снимок в заданный момент (например, посреди установки), а не после неё
                self._shot_scheduled = True
                self.after(int(args.shot_after * 1000), self._grab_and_quit)

        # ---------- каркас

        def _build(self):
            px = K.px
            outer = ttk.Frame(self, padding=(px(24), px(20), px(24), px(16)))
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(0, weight=1)
            self.outer = outer
            self.header = K.Header(outer, "Uma RU")
            self.header.grid(row=0, column=0, sticky="we")
            self.banner = K.Banner(outer)
            self.banner.grid(row=1, column=0, sticky="we", pady=(px(14), 0))
            self.banner.hide()
            # Главное одной строкой: готово ли к игре
            self.card = K.Card(outer)
            self.card.grid(row=2, column=0, sticky="we", pady=(px(14), 0))
            self.card.body.columnconfigure(0, weight=1)
            self.summary = K.StatusSummary(self.card.body, "Проверяю…")
            self.summary.grid(row=0, column=0, sticky="we")
            self.btn_languages = ttk.Button(self.card.body, text="Языки…", style="Secondary.TButton",
                                            command=self.choose_languages)
            self.btn_languages.grid(row=0, column=1, sticky="e", padx=(px(12), 0))
            self.btn_languages.state(["disabled"])
            self.primary = K.primary_button(outer, "Проверяю…", self._on_primary)
            self.primary.grid(row=3, column=0, sticky="we", pady=(px(18), 0))
            self.primary.state(["disabled"])
            # Место под полосу прогресса занято всегда: окно не меняет высоту, когда она появляется
            slot = ttk.Frame(outer, height=px(18))
            slot.grid(row=4, column=0, sticky="we", pady=(px(10), 0))
            slot.grid_propagate(False)
            slot.columnconfigure(0, weight=1)
            slot.rowconfigure(0, weight=1)
            self.progress = ttk.Progressbar(slot, maximum=1000)
            self.progress.grid(row=0, column=0, sticky="we")
            self.progress.grid_remove()
            row = ttk.Frame(outer)
            row.grid(row=5, column=0, sticky="we", pady=(px(6), 0))
            row.columnconfigure(2, weight=1)
            # «Починить» и «Новый лаунчер» видны, только когда они нужны
            self.btn_repair = ttk.Button(row, text="Починить", style="Secondary.TButton", command=self.repair_all)
            self.btn_repair.grid(row=0, column=0, sticky="w", padx=(0, px(8)))
            self.btn_repair.grid_remove()
            self.btn_launcher = ttk.Button(row, text="Новый лаунчер…", style="Secondary.Accent.TButton", command=self._show_launcher_offer)
            self.btn_launcher.grid(row=0, column=1, sticky="w")
            self.btn_launcher.grid_remove()
            self.btn_more = ttk.Button(row, text="Дополнительно ▾", style="Secondary.TButton", command=self._more_menu)
            self.btn_more.grid(row=0, column=3, sticky="e")
            self.menu = tk.Menu(self, tearoff=False)
            if self.online:
                self.menu.add_command(label="Проверить обновления", command=self.check_all_updates)
            for label, cmd in (("Проверить и починить", self.repair_all), ("Выбрать папку игры…", self.pick_game),
                               ("Скопировать отчёт для поддержки", self.copy_report)):
                self.menu.add_command(label=label, command=cmd)
            self.menu.add_separator()
            self.menu.add_command(label="Удалить Uma RU…", command=self.uninstall_everything)
            self.menu.add_command(label="О лаунчере", command=self.about)

        # ---------- служебное

        def ui(self, fn, *a):
            self.q.put((fn, a))

        def _poll(self):
            self._poll_job = self.after(80, self._poll)
            show, play = take_instance_requests()
            if show:
                self._on_second_launch(play)
            for _ in range(200):
                try:
                    fn, a = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*a)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка окна")

        def run_bg(self, work, done):
            def runner():
                result, error = None, None
                try:
                    result = work()
                except Exception as e:  # noqa: BLE001
                    error = e
                    if not isinstance(e, core.UmaError):
                        log.exception("Сбой")
                self.ui(done, result, error)
            threading.Thread(target=runner, daemon=True).start()

        def set_busy(self, busy, text=""):
            if self.reveal_job is not None:
                self.after_cancel(self.reveal_job)
                self.reveal_job = None
            self.busy = busy
            state = ["disabled"] if busy else ["!disabled"]
            for w in (self.primary, self.btn_repair, self.btn_more, self.btn_languages, self.btn_launcher):
                w.state(state)
            if busy:
                self.progress.configure(mode="determinate")
                self.progress["value"] = 0
                self.progress.grid()
                self.summary.set("busy", text or "Подождите…")
            else:
                self.progress.stop()
                self.progress.grid_remove()
                self.summary.set(*self.last_summary)

        def set_progress(self, frac, text):
            self.progress["value"] = int(frac * 1000)
            if text:
                self.summary.set(title=text)

        def _show_if_working(self):
            self.reveal_job = None
            if self.busy and args.play and not self.winfo_viewable():
                self.deiconify()

        def _bring_to_front(self):
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)    # Windows не всегда даёт фокус чужому окну: поднимаем ненадолго поверх всех
            self._topmost_job = self.after(300, self._drop_topmost)
            self.focus_force()

        def _drop_topmost(self):
            self._topmost_job = None
            self.attributes("-topmost", False)

        def destroy(self):
            for name in ("_topmost_job", "_poll_job"):    # отложенные вызовы закрытого окна иначе падают в Tcl
                if getattr(self, name, None):
                    self.after_cancel(getattr(self, name))
                    setattr(self, name, None)
            super().destroy()

        def _on_second_launch(self, play):
            """Программу запустили ещё раз (ярлык, меню «Пуск»), пока это окно открыто: показываемся, а для ярлыка
            «Играть» (--play) проверяем игру и запускаем её, как сделала бы новая копия."""
            self._bring_to_front()
            if not play or self.busy or self.report is None:
                return    # окно занято установкой или первой проверкой — человек видит, что происходит
            self.play_requested = True
            self.banner_note = None
            self.refresh()

        def _play_after_request(self):
            mode = self.report.mode
            if mode in ("ok", "installed_only"):
                self._launch()
            elif mode in ("repair", "update"):
                self.apply(self.report.plan, then_play=True)
            # остальное (нет игры, игра запущена, Steam обновляет…) решает человек: окно уже на виду

        # ---------- диагностика и показ

        def check_updates(self):
            if not self.busy:
                self.channel_checked = False
                self.auto_done = False
                self.banner_note = None
                self.refresh()

        def launcher_notice(self, kind, message):
            self.banner_note = (kind, message)
            self.banner.show(*self.banner_note)
            self.after_idle(self._fit_height)

        def check_all_updates(self):
            """«Проверить обновления»: сначала версия лаунчера (вышла новая — предложение), затем перевод."""
            if self.busy:
                return
            self.set_busy(True, "Проверяю обновления…")

            def checked(offer, error):
                self.set_busy(False)
                if offer is not None:
                    self._set_launcher_offer(offer)
                    self.launcher_prompted = True
                    self.offer_launcher(offer)
                    return
                if error:
                    self.launcher_notice("err", str(error))
                else:
                    self.launcher_notice("ok", f"Лаунчер {APP_VERSION} — последняя версия.")
                self.channel_checked = False
                self.auto_done = False      # найденное обновление перевода поставим сами, как при запуске
                self.refresh()
            self.run_bg(lambda: launcher.check(APP_VERSION), checked)

        def _check_launcher_quietly(self):
            """Как в других лаунчерах: при каждом запуске в фоне смотрим, не вышла ли новая версия. Окно не занято,
            ошибки сети только в журнал. Ставится новая версия, как и раньше, только по кнопке игрока."""
            self.launcher_started = time.monotonic()

            def work():
                try:
                    return launcher.check(APP_VERSION)
                except launcher.LauncherUpdateError as exc:
                    log.info("Проверка версии лаунчера: %s", exc)
                    return None

            def done(offer, _error):
                self.launcher_checked = True
                if offer is None:
                    return
                log.info("Вышел лаунчер %s", offer.version)
                self._set_launcher_offer(offer)
                if not args.play and self.auto_done and not self.busy:
                    self._prompt_launcher()
            self.run_bg(work, done)

        def _set_launcher_offer(self, offer):
            self.launcher_offer = offer
            self.btn_launcher.configure(text=f"Новый лаунчер {offer.version}…")
            self.btn_launcher.grid()
            self.after_idle(self._fit_height)

        def _show_launcher_offer(self):
            if self.launcher_offer is not None and not self.busy:
                self.launcher_prompted = True
                self.offer_launcher(self.launcher_offer)

        def _prompt_launcher(self, then_play=False):
            """Показать предложение нового лаунчера, если оно есть и игрок не откладывал его. True — показали."""
            offer = self.launcher_offer
            if offer is None or self.launcher_prompted or launcher_snoozed(offer.version):
                return False
            self.launcher_prompted = True
            self._bring_to_front()           # по ярлыку «Играть» окно скрыто, а предложение должно быть видно
            self.offer_launcher(offer, later=(lambda: self._launch(force=True)) if then_play else None)
            return True

        def _launcher_gate(self):
            """Перед запуском игры: если вышел новый лаунчер, сначала предложить его. True — запуск отложен."""
            if self.launcher_waiting:
                return True
            if not self.launcher_checked and time.monotonic() - self.launcher_started < LAUNCHER_WAIT:
                self.launcher_waiting = True

                def again():
                    self.launcher_waiting = False
                    self._launch()
                self.after(150, again)
                return True
            return self._prompt_launcher(then_play=True)

        def offer_launcher(self, offer, later=None):
            """Предложение нового лаунчера. «Позже» не спрашивает про эту версию три дня; later — что делать после
            «Позже» (например, всё-таки запустить игру)."""
            dialog = tk.Toplevel(self)
            dialog.withdraw()
            dialog.transient(self)
            dialog.title("Обновление лаунчера")
            body = ttk.Frame(dialog, padding=K.px(18))
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=f"Новый лаунчер {offer.version}", style="Header.TLabel").pack(anchor="w")
            size = round(offer.size / 1024**2, 1)
            ttk.Label(body, text=f"Сейчас у вас {APP_VERSION}   ·   Загрузка {size:g} МиБ").pack(anchor="w")
            if offer.notes:
                # Signed notes can be long. Keep them readable on small screens.
                notes = tk.Text(body, width=46, height=5, wrap="word", font="TkDefaultFont",
                                relief="flat", background=K.THEME.bg, foreground=K.THEME.fg)
                notes.insert("1.0", offer.notes)
                notes.configure(state="disabled")
                notes.pack(fill="both", expand=True, pady=K.px(10))
            buttons = ttk.Frame(body)
            buttons.pack(fill="x", pady=(K.px(8), 0))
            def postpone():
                snooze_launcher(offer.version)
                dialog.destroy()
                if later:
                    later()
            later_button = ttk.Button(buttons, text="Позже", command=postpone)
            later_button.pack(side="left")
            def accept():
                dialog.destroy()
                self.download_launcher(offer)
            ttk.Button(buttons, text="Скачать и установить", style="Secondary.Accent.TButton", command=accept).pack(side="right")
            dialog.bind("<Escape>", lambda _event: postpone())
            dialog.protocol("WM_DELETE_WINDOW", postpone)
            dialog.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
            dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
            dialog.deiconify()
            dialog.grab_set()
            later_button.focus_set()

        def download_launcher(self, offer):
            if self.busy:
                return
            if ctypes.windll.shell32.IsUserAnAdmin():
                self.launcher_notice("info", "Для установки обновления откройте лаунчер Uma RU без прав администратора.")
                return
            self.set_busy(True, f"Скачиваем лаунчер {offer.version}…")
            def progress(frac, _text):
                self.ui(self.set_progress, frac, f"Скачиваем лаунчер {offer.version}…")
            def downloaded(path, error):
                self.set_busy(False)
                if error:
                    self.launcher_notice("err", str(error))
                    return
                try:
                    params = [str(path), "/UMARUUPDATE"]
                    if getattr(sys, "frozen", False) and any(app_dir().glob("unins???.dat")):
                        params.append(f"/DIR={app_dir()}")
                    subprocess.Popen(params, cwd=str(path.parent))
                except OSError as exc:
                    self.launcher_notice("err", f"Не удалось открыть установщик. {exc}")
                    return
                self.destroy()
            self.run_bg(lambda: launcher.download(offer, LAUNCHER_DOWNLOADS, progress), downloaded)

        def refresh(self):
            if self.busy:
                return
            self.primary.configure(text="Проверяю…")
            self.set_busy(True, "Проверяем игру и перевод…")
            override = args.game_dir
            fetch = self.online and not self.channel_checked
            if fetch:
                self.reveal_job = self.after(1200, self._show_if_working)

            def work():
                game = core.find_game(override)
                raw, extras, result = self.raw_pack, dict(self.extras), None
                choices = core.load_choices()
                wanted = wanted_extras(choices)
                if fetch and game:
                    self.ui(self.set_progress, 0, "Проверяем доступные обновления…")
                    result = check_channel(pack_root(), game, progress=lambda f, s: self.ui(self.set_progress, f, s),
                                           extras=wanted)
                    raw, extras = result.pack, dict(result.extras)
                    core.fill_manifest(game)
                elif self.online:
                    for name, cached in cached_extras(pack_root(), [n for n in wanted if not extras.get(n)]).items():
                        extras[name] = cached
                pack = core.compose_pack(raw, choices, comics_en=extras.get("comics_en"))
                rep = core.diagnose(core.Context(game, pack, online=self.online))
                return game, rep, pack, result, raw, extras
            self.run_bg(work, self._after_refresh)

        def _after_refresh(self, result, error):
            self.set_busy(False)
            play_requested, self.play_requested = self.play_requested, False   # просьба действует на одну проверку
            apply_requested, self.apply_requested = self.apply_requested, False
            if error is not None:
                self.deiconify()
                self.last_summary = ("fail", "Не удалось проверить игру")
                self.summary.set(*self.last_summary)
                self.banner.show("err", str(error))
                self.primary.configure(text="Проверить снова")
                self.primary.state(["!disabled"])
                return
            self.game, self.report, self.pack_, checked, self.raw_pack, self.extras = result
            if checked:
                self.channel_checked = True
                self.channel_result = checked
                if checked.offers is not None:
                    self.offers = dict(checked.offers)
                if checked.state in ('offline', 'error'):
                    log.info("%s", checked.message)
            self.render()
            if not self.auto_done:
                self.auto_done = True
                self._auto()
            elif play_requested:
                self._play_after_request()
            elif apply_requested and self.report.mode in ("update", "repair"):
                self.apply(self.report.plan, ok_text="Языки перевода применены.")
            elif args.shot and not self.busy and not getattr(self, "_shot_scheduled", False):
                self._shot_scheduled = True   # после автоматической починки — снимок уже готового состояния
                self.after(1500, self._grab_and_quit)
            if not args.play and not self.busy and not self.launching:
                self._prompt_launcher()

        def render(self):
            rep = self.report
            labels = {"no_game": "Указать папку игры…", "blocked": "Проверить снова",
                      "not_installed": "Установить русификатор", "update": "Обновить перевод и играть",
                      "repair": "Починить и играть", "installed_only": "▶  Играть на русском",
                      "need_channel": "Повторить проверку", "ok": "▶  Играть на русском"}
            text = labels[rep.mode]
            if not self.online and self.pack_ is None and rep.mode not in ("no_game",):
                text = "Проверить снова"
            self.primary.configure(text=text)
            self.primary.state(["!disabled"] if not self.busy else ["disabled"])
            self.last_summary = self._summary(rep)
            if not self.busy:
                self.summary.set(*self.last_summary)
            if rep.mode == "repair" and self.pack_:
                self.btn_repair.grid()
            else:
                self.btn_repair.grid_remove()
            if self.banner_note and not self.busy:
                self.banner.show(*self.banner_note)   # итог последнего действия («Готово…») остаётся, пока человек не нажмёт что-то ещё
            elif not self.busy:
                self.banner.hide()                    # состояние игры показывает главный статус, плашка — только для итогов
            self.after(50, self._fit_height)

        def _summary(self, rep):
            """(состояние, заголовок) главного статуса: заголовок сам говорит, что делать."""
            mode = rep.mode
            if not self.online and self.pack_ is None and mode != "no_game":
                return "fail", "Нет файлов перевода рядом с лаунчером"
            if mode == "update":
                pack = rep.check("pack")
                if pack and pack.detail.startswith("Нужно применить"):
                    return "info", "Нужно применить языки"
                return "info", "Есть новая версия перевода"
            if mode == "blocked":
                return "warn", "Steam обновляет игру" if "Steam" in rep.blocked else "Закройте игру"
            return {"ok": ("ok", "Готово к игре"), "installed_only": ("ok", "Можно играть"),
                    "repair": ("warn", "Нужна починка"), "not_installed": ("info", "Русификатор не установлен"),
                    "need_channel": ("warn", "Нужен интернет"), "no_game": ("fail", "Игра не найдена")}.get(mode, ("off", ""))

        def _fit_height(self):
            """Высота окна по содержимому: без пустого поля снизу и без обрезания."""
            self.update_idletasks()
            width = self.winfo_width() if self.winfo_ismapped() else self.width   # пока окно скрыто, ширина ещё 1
            self.geometry(f"{max(width, K.px(480))}x{self.outer.winfo_reqheight()}")

        def _auto(self):
            """Что делаем сами при открытии: после обновления — чиним без вопросов, по ярлыку — ещё и запускаем игру."""
            mode = self.report.mode
            started = False
            if mode in ("ok", "installed_only"):
                if args.play:
                    self._launch()
                    started = True
            elif mode in ("repair", "update"):
                self.apply(self.report.plan, then_play=args.play)
                started = True
            elif mode == "not_installed" and args.auto:
                self.apply(self.report.plan)
                started = True
            elif args.play and not self.winfo_viewable():
                self.deiconify()   # ярлык, но нужно человеческое решение (нет игры, Steam обновляется…) — показываем окно
            if args.shot and not started:
                self._shot_scheduled = True
                self.after(1500, self._grab_and_quit)

        # ---------- действия

        def _on_primary(self):
            if self.busy or self.report is None:
                return
            self.banner_note = None
            mode = self.report.mode
            if mode == "installed_only":
                self._launch()
            elif mode == "need_channel":
                self.check_updates()
            elif self.pack_ is None and mode != "no_game":
                self.refresh()
            elif mode == "no_game":
                self.pick_game()
            elif mode == "blocked":
                self.refresh()
            elif mode == "not_installed":
                self.apply(self.report.plan)
            elif mode in ("repair", "update"):
                self.apply(self.report.plan, then_play=True)
            else:
                self._launch()

        def apply(self, plan, deep=False, then_play=False, ok_text="Готово."):
            if self.game is None:
                return
            ctx = core.Context(self.game, self.pack_, online=self.online)
            self.banner_note = None
            self.banner.hide()
            self.set_busy(True, "Начинаю…")
            self.reveal_job = self.after(1200, self._show_if_working)

            def work():
                return core.apply_plan(ctx, plan, progress=lambda f, t: self.ui(self.set_progress, f, t), deep=deep)

            def done(result, error):
                self.set_busy(False)
                if isinstance(error, core.NeedAdmin):
                    self._ask_admin()
                    return
                if error is not None:
                    self.deiconify()
                    self.banner_note = ("err", str(error), "Скопировать отчёт", self.copy_report)
                    self.banner.show(*self.banner_note)
                    log.error("%s", error)
                    self.refresh()
                    return
                for line in result:
                    log.info("%s", line)
                if "pack" in plan and not core.load_settings().get("shortcut_done"):
                    self._ensure_shortcut()
                self.banner_note = ("ok", ok_text + (" Запускаю игру…" if then_play else ""))
                self.banner.show(*self.banner_note)
                if then_play:
                    self._launch()
                else:
                    self.refresh()
            self.run_bg(work, done)

        def _english_comics(self):
            """Английские комиксы того же выпуска, что и пакет перевода (иначе они не применяются)."""
            return core.matching_extra(self.raw_pack, self.extras.get("comics_en"))

        def _english_available(self):
            """Английские комиксы можно выбрать: уже скачаны или канал их предлагает (пока не проверяли — тоже да)."""
            if self._english_comics():
                return True
            return self.online and (self.offers is None or "comics_en" in self.offers)

        def choose_languages(self):
            """Окно «Языки перевода»: для каждого раздела — русский, японский оригинал (и английский для комиксов).
            Без пояснений мелким шрифтом: недоступный вариант просто неактивен."""
            if self.busy:
                return
            current = core.load_choices()
            dialog = tk.Toplevel(self)
            dialog.withdraw()
            dialog.transient(self)
            dialog.title("Языки перевода")
            dialog.resizable(False, False)
            body = ttk.Frame(dialog, padding=K.px(20))
            body.pack(fill="both", expand=True)
            variables = {}
            for row, (part, (title, languages)) in enumerate(core.CONTENT_PARTS.items()):
                ttk.Label(body, text=title, style="Heading.TLabel").grid(row=row, column=0, sticky="w", pady=K.px(6),
                                                                        padx=(0, K.px(24)))
                variables[part] = tk.StringVar(master=dialog, value=current[part])
                radios = ttk.Frame(body)
                radios.grid(row=row, column=1, sticky="w", pady=K.px(6))
                for lang in languages:      # одинаковые переключатели-плашки для всех разделов
                    toggle = ttk.Radiobutton(radios, text=core.LANGUAGE_NAMES[lang], value=lang, variable=variables[part],
                                             style="Toggle.TButton")
                    toggle.pack(side="left", padx=(0, K.px(4)))
                    if part == "comics" and lang == "en" and not self._english_available():
                        toggle.state(["disabled"])
            buttons = ttk.Frame(body)
            buttons.grid(row=len(core.CONTENT_PARTS), column=0, columnspan=2, sticky="we", pady=(K.px(16), 0))
            ttk.Button(buttons, text="Отмена", style="Secondary.TButton", command=dialog.destroy).pack(side="left")

            def save():
                choices = {part: var.get() for part, var in variables.items()}
                dialog.destroy()
                self._apply_choices(choices)
            ttk.Button(buttons, text="Сохранить", style="Secondary.Accent.TButton", command=save).pack(side="right")
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            dialog.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
            dialog.geometry(f"{dialog.winfo_reqwidth()}x{dialog.winfo_reqheight()}+{max(0, x)}+{max(0, y)}")
            dialog.deiconify()
            dialog.grab_set()

        def _apply_choices(self, choices):
            before = core.load_choices()
            core.save_choices(choices)
            choices = core.load_choices()
            if choices == before:
                return
            log.info("Языки перевода: %s", choices_summary(choices) or "всё на русском")
            self.banner_note = None
            self.apply_requested = True
            if self.online and choices["comics"] == "en" and not self._english_comics():
                self.channel_checked = False      # английские комиксы надо скачать: проверим канал заново
            self.refresh()

        def repair_all(self):
            if self.busy or self.game is None:
                return
            if self.report and self.report.mode == "blocked":
                self.banner.show("warn", self.report.blocked)
                return
            self.apply(["hachimi", "pack", "config", "caches"], deep=True, ok_text="Всё проверено и починено.")

        def _launch(self, force=False):
            if self.launching or (not force and self._launcher_gate()):
                return
            self.launching = True
            try:
                core.launch_game()
            except OSError as e:
                self.launching = False
                self.banner.show("err", f"Не удалось запустить игру через Steam: {e}")
                return
            log.info("Игра запущена через Steam")
            self.banner.hide()
            self.set_busy(True, "Запускаю игру…")
            self.progress.configure(mode="indeterminate")
            self.progress.start(14)
            self.after(2600, self.destroy)

        def _ask_admin(self):
            if messagebox.askyesno(APP_TITLE, "Папка игры защищена от записи, нужны права администратора.\n\n"
                                              "Перезапустить лаунчер с правами администратора и продолжить?"):
                if relaunch_admin(("--elevated", "--auto")):   # новая копия сама продолжит с того же места
                    self.destroy()
                    return
                self.banner.show("err", "Не удалось запросить права администратора.")
            else:
                self.banner.show("warn", "Без прав администратора записать файлы в папку игры не получится.")
            self.refresh()

        def pick_game(self):
            path = filedialog.askdirectory(title="Папка игры (где лежит UmamusumePrettyDerby_Jpn.exe)")
            if not path:
                return
            if not core.is_game_dir(Path(path)):
                messagebox.showwarning(APP_TITLE, "В этой папке нет файла UmamusumePrettyDerby_Jpn.exe.\n"
                                                  "Выберите папку игры (обычно …\\steamapps\\common\\UmamusumePrettyDerby_Jpn).")
                return
            core.save_settings({"game_dir": path})
            args.game_dir = path
            self.auto_done = not self.online
            self.channel_checked = False
            self.refresh()

        def _ensure_shortcut(self):
            if not getattr(sys, "frozen", False):
                return
            try:
                core.create_shortcut(sys.executable, "--play", SHORTCUT_NAME)
                core.save_settings({"shortcut_done": True})
                log.info("Ярлык «%s» добавлен на рабочий стол", SHORTCUT_NAME)
            except Exception as e:  # noqa: BLE001
                log.warning("Ярлык не создан: %s", e)

        def copy_report(self):
            text = core.diagnostics_text(self.report, self.pack_) if self.report else "Проверка ещё не выполнялась."
            try:
                tail = (core.app_home() / "umaru.log").read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            except OSError:
                tail = []
            self.clipboard_clear()
            self.clipboard_append(text + "\n\n--- журнал ---\n" + "\n".join(tail))
            self.banner.show("ok", "Отчёт скопирован: вставьте его в сообщение поддержке.")

        def uninstall_everything(self):
            """Всё сразу: Hachimi и перевод из папки игры, ярлык, настройки и сама программа (её папку удалит помощник после выхода)."""
            if self.busy:
                return
            program = program_info()
            lines = []
            if self.game is not None:
                lines.append("• Hachimi и файлы перевода — из папки игры (игра станет такой, как была до русификатора);")
            installed = program is not None and program.get("uninstaller") is not None
            if program is not None:
                lines += [f"• ярлык «{SHORTCUT_NAME}» с рабочего стола;",
                          "• настройки, журнал и скачанный перевод;" if installed else "• настройки и журнал программы;",
                          f"• сам лаунчер — папка {program['dir']}."]
            else:
                lines.append("• ярлык и настройки лаунчера (сам лаунчер из исходников не трогаем).")
            again = ("заново установить лаунчер (UmaRU-Setup.exe)." if installed
                     else "заново распаковать архив с лаунчером.")
            if not messagebox.askyesno(APP_TITLE, "Удалить всё?\n\n" + "\n".join(lines) + "\n\nСаму игру и ваш прогресс в ней это не "
                                                  f"затронет. Чтобы вернуть русификатор, нужно будет {again}",
                                       icon="warning"):
                return
            ctx = core.Context(self.game, self.pack_, installer=hachimi_installer())
            self.set_busy(True, "Удаляю…")

            def done(result, error):
                self.set_busy(False)
                if error is not None:
                    self.banner.show("err", str(error) + _ADMIN_HINT if isinstance(error, core.NeedAdmin) else str(error))
                    self.refresh()
                    return
                done_lines, removal = result
                for line in done_lines:
                    log.info("%s", line)
                if removal is None:
                    self.banner.show("ok", "Готово: " + "; ".join(done_lines))
                    self.refresh()
                    return
                messagebox.showinfo(APP_TITLE, "Готово.\n\n" + "\n".join("• " + x for x in done_lines) + "\n\nОкно сейчас закроется.")
                core.schedule_program_removal(removal)   # помощник дождётся выхода и удалит программу
                self.destroy()
            self.run_bg(lambda: core.uninstall_all(ctx, program), done)

        def about(self):
            messagebox.showinfo(APP_TITLE, f"Лаунчер Uma RU {APP_VERSION}\n"
                                           f"Перевод: {pretty_date(self.pack_.version) if self.pack_ else 'нет'}\n"
                                           f"Hachimi Edge {self.pack_.manifest.get('hachimi', '') if self.pack_ else ''} — автор kairusds (GPL-3.0)\n"
                                           "Английские картинки интерфейса — перевод UmaTL.\n\n"
                                           f"Журнал и настройки: {core.app_home()}")

        def _more_menu(self):
            x = self.btn_more.winfo_rootx()
            y = self.btn_more.winfo_rooty() + self.btn_more.winfo_height()
            try:
                self.menu.tk_popup(x, y)
            finally:
                self.menu.grab_release()

        def _on_close(self):
            if self.busy and not messagebox.askyesno(APP_TITLE, "Идёт установка. Закрыть окно и прервать её?"):
                return
            self.destroy()

        def _grab_and_quit(self):
            from PIL import ImageGrab
            self.attributes("-topmost", True)
            self.update()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + self.winfo_width(), y + self.winfo_height()), all_screens=True).save(args.shot)
            self.destroy()

    K.enable_dpi_awareness()
    return App()


# ---------------------------------------------------------------- вход

def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
    k.CreateEventW.restype = ctypes.c_void_p
    k.OpenEventW.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p)
    k.OpenEventW.restype = ctypes.c_void_p
    k.SetEvent.argtypes = (ctypes.c_void_p,)
    k.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    k.WaitForSingleObject.restype = ctypes.c_ulong
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    return k


def single_instance():
    """Не запускаем вторую копию окна (двойной щелчок по ярлыку): True — мы первые. Первая копия заводит события,
    через которые следующие копии просят её показаться (notify_running_window)."""
    try:
        k = _kernel32()
        handle = k.CreateMutexW(None, False, INSTANCE_MUTEX)
        first = ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
        single_instance.handle = handle
        if first:
            single_instance.kernel = k
            single_instance.events = {name: k.CreateEventW(None, False, False, name) for name in (SHOW_EVENT, PLAY_EVENT)}
        return first
    except Exception:
        return True


def notify_running_window(play):
    """Вторая копия: попросить открытое окно выйти на передний план, а для ярлыка «Играть» (--play) — ещё и запустить игру."""
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY: иначе Windows не даст первой копии забрать фокус
        k = _kernel32()
        handle = k.OpenEventW(0x0002, False, PLAY_EVENT if play else SHOW_EVENT)   # EVENT_MODIFY_STATE
        if handle:
            k.SetEvent(handle)
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def take_instance_requests():
    """Первая копия: пришли ли просьбы от следующих копий -> (показать окно, запустить игру). События сбрасываются сами."""
    k, events = getattr(single_instance, "kernel", None), getattr(single_instance, "events", {})
    if k is None:
        return False, False
    try:
        fired = {name for name, handle in events.items() if handle and k.WaitForSingleObject(handle, 0) == 0}
    except Exception:  # noqa: BLE001
        return False, False
    return bool(fired), PLAY_EVENT in fired


def main(argv=None):
    ap = argparse.ArgumentParser(description="Лаунчер Uma RU")
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update-cache", action="store_true", help="получить и проверить онлайн-выпуск без установки в игру")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--uninstall-all", action="store_true", help="убрать Hachimi, перевод, ярлык, настройки и сам лаунчер")
    ap.add_argument("--keep-program", action="store_true", help="с --uninstall-all: сам лаунчер оставить")
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--game-dir", default="")
    ap.add_argument("--pack-dir", default="")
    ap.add_argument("--shot", default="", help="снимок окна в файл и выход (проверка вида)")
    ap.add_argument("--shot-after", type=float, default=0, help="с --shot: снять через столько секунд, а не после починки")
    ap.add_argument("--elevated", action="store_true")
    args = ap.parse_args(argv)
    setup_logging()
    if args.pack_dir:
        os.environ["UMA_RU_PACK"] = args.pack_dir
    pack = core.load_pack(pack_root())
    log.info("Лаунчер Uma RU %s, пакет %s, запуск: %s", APP_VERSION, pack.version if pack else "нет", " ".join(sys.argv[1:]))
    if args.check or args.update_cache or args.install or args.repair or args.uninstall or args.uninstall_all:
        return run_cli(args, pack)
    if not args.elevated and not args.shot and not single_instance():
        notify_running_window(args.play)
        return 0
    launcher.cleanup_downloads(LAUNCHER_DOWNLOADS)
    try:
        app = build_window(args, pack)
        app.mainloop()
    except Exception:
        log.exception("Окно не запустилось")
        try:
            ctypes.windll.user32.MessageBoxW(0, "Лаунчер не смог открыть окно. Подробности — в журнале:\n"
                                                f"{core.app_home() / 'umaru.log'}", APP_TITLE, 0x10)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Uma RU Player — окно и командная строка программы для игроков.

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

APP_TITLE = "Uma RU — русификатор Umamusume"
SHORTCUT_NAME = "Играть на русском (Uma RU)"
INSTANCE_MUTEX = "UmaRUPlayerSingleInstance"     # это же имя проверяет деинсталлятор в setup/UmaRU.iss
SHOW_EVENT = "UmaRUPlayerShowWindow"             # вторая копия просит открытое окно показаться
PLAY_EVENT = "UmaRUPlayerPlay"                   # ... показаться и запустить игру (ярлык с --play)
LAUNCHER_DOWNLOADS = Path(tempfile.gettempdir()) / "UmaRU-launcher"
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


def check_channel(data_dir, game, *, download=True, progress=None):
    channel = updates.read_channel(data_dir)
    if channel is None:
        return updates.Result(core.load_pack(data_dir), "error", "Не удалось прочитать адрес обновлений. Переустановите мастер.")
    if download:
        return updates.resolve(data_dir, channel, game_dir=game.path if game else None, progress=progress)
    return updates.Result(updates.cached_pack(data_dir, channel), "local", "")


# ---------------------------------------------------------------- командная строка (без окна)

def run_cli(args, pack):
    game = core.find_game(args.game_dir)
    data_dir = pack_root()
    online = (data_dir / "update-channel.json").is_file()
    result = {"app": APP_VERSION}
    code = 0
    removal = None
    try:
        if online:
            check = check_channel(data_dir, game, download=args.update_cache or bool(game and (args.install or args.repair)))
            pack = check.pack
            result.update(update_state=check.state, update_message=check.message, release=check.tag)
        if args.update_cache and not online:
            raise core.UmaError("Для этого мастера не задан канал обновлений.")
        result['pack'] = pack.version if pack else None
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
            self.log_visible = False
            self.banner_note = None
            self.pack_ = pack
            self.online = (pack_root() / "update-channel.json").is_file()
            self.channel_checked = False
            self.channel_result = None
            self.reveal_job = None
            self.play_requested = False   # ярлык «Играть» нажали, пока окно уже было открыто
            K.center_window(self, 560, 600)
            self.minsize(K.px(520), K.px(300))
            self._build()
            K.dark_title_bar(self, K.THEME.dark)
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(60, self._poll)
            hidden = args.play and not args.shot
            if not hidden:
                self.deiconify()
            self.after(120, self.refresh)
            if args.shot and args.shot_after:   # снимок в заданный момент (например, посреди установки), а не после неё
                self._shot_scheduled = True
                self.after(int(args.shot_after * 1000), self._grab_and_quit)

        # ---------- каркас

        def _build(self):
            px = K.px
            outer = ttk.Frame(self, padding=(px(24), px(20), px(24), px(14)))
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(0, weight=1)
            self.outer = outer
            sub = f"Перевод от {pretty_date(self.pack_.version)}" if self.pack_ else ("Автоматические обновления перевода" if self.online else "Нет файлов перевода рядом с программой")
            self.header = K.Header(outer, "Русификатор Umamusume", f"{sub} · Hachimi Edge {self.pack_.manifest.get('hachimi', '0.33.1') if self.pack_ else ''}".rstrip(" ·"))
            self.header.grid(row=0, column=0, sticky="we")
            self.banner = K.Banner(outer)
            self.banner.grid(row=1, column=0, sticky="we", pady=(px(14), 0))
            self.banner.hide()
            self.card = K.Card(outer, "Состояние")
            self.card.grid(row=2, column=0, sticky="we", pady=(px(14), 0))
            self.row_game = K.StatusRow(self.card.body, "Игра", "Проверяю…")
            self.row_hachimi = K.StatusRow(self.card.body, "Hachimi", "")
            self.row_pack = K.StatusRow(self.card.body, "Перевод", "")
            self.row_extra = K.StatusRow(self.card.body, "", "")
            for row in (self.row_game, self.row_hachimi, self.row_pack):
                row.pack(fill="x", pady=(0, px(10)))
            if self.online:
                self.row_online = K.StatusRow(self.card.body, "Обновления", "Проверю при запуске")
                self.row_online.pack(fill="x", pady=(0, px(10)))
            self.row_extra.pack(fill="x")
            self.row_extra.pack_forget()
            self.update_buttons = []
            if self.online:
                actions = ttk.Frame(self.card.body)
                actions.pack(fill="x")
                for label, command in (("Обновления перевода", self.check_updates),
                                       ("Обновления лаунчера…", self.check_launcher)):
                    button = ttk.Button(actions, text=label, style="Secondary.TButton", command=command)
                    button.pack(side="left", padx=(0, px(8)))
                    button.state(["disabled"])
                    self.update_buttons.append(button)
            self.primary = K.primary_button(outer, "Проверяю…", self._on_primary)
            self.primary.grid(row=3, column=0, sticky="we", pady=(px(18), 0))
            self.primary.state(["disabled"])
            self.hint = K.HintLabel(outer, "")
            self.hint.grid(row=4, column=0, sticky="we", pady=(px(6), 0))
            self.progress = ttk.Progressbar(outer, maximum=1000)
            self.progress.grid(row=5, column=0, sticky="we", pady=(px(10), 0))
            self.progress.grid_remove()
            self.progress_text = K.HintLabel(outer, "")
            self.progress_text.grid(row=6, column=0, sticky="we", pady=(px(4), 0))
            self.progress_text.grid_remove()
            row = ttk.Frame(outer)
            row.grid(row=7, column=0, sticky="we", pady=(px(12), 0))
            row.columnconfigure(1, weight=1)
            self.btn_repair = ttk.Button(row, text="Починить после обновления", style="Secondary.TButton", command=self.repair_all)
            self.btn_repair.grid(row=0, column=0, sticky="w")
            self.btn_more = ttk.Button(row, text="Дополнительно ▾", style="Secondary.TButton", command=self._more_menu)
            self.btn_more.grid(row=0, column=2, sticky="e")
            self.btn_log = ttk.Button(row, text="Подробности", style="Secondary.TButton", command=self.toggle_log)
            self.btn_log.grid(row=0, column=1, sticky="e", padx=(0, px(8)))
            self.log_box = K.LogBox(outer, height=7)
            self.log_box.grid(row=8, column=0, sticky="nsew", pady=(px(14), 0))
            self.log_box.grid_remove()
            outer.rowconfigure(8, weight=1)
            handler = _QueueHandler(self.q, self.log_box)
            handler.setLevel(logging.INFO)
            logging.getLogger("umaru").addHandler(handler)
            self.menu = tk.Menu(self, tearoff=False)
            if self.online:
                self.menu.add_command(label="Проверить обновления", command=self.check_updates)
                self.menu.add_separator()
            for label, cmd in (("Выбрать папку игры…", self.pick_game), ("Открыть папку игры", self.open_game_dir),
                               ("Создать ярлык на рабочем столе", self.make_shortcut), ("Открыть журнал", self.open_log),
                               ("Скопировать отчёт для того, кто помогает", self.copy_report)):
                self.menu.add_command(label=label, command=cmd)
            self.menu.add_separator()
            self.menu.add_command(label="Удалить русификатор и Hachimi…", command=self.uninstall)
            self.menu.add_command(label="Удалить всё, включая программу…", command=self.uninstall_everything)
            self.menu.add_command(label="О программе", command=self.about)

        # ---------- служебное

        def ui(self, fn, *a):
            self.q.put((fn, a))

        def _poll(self):
            self.after(80, self._poll)
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
            for w in (self.primary, self.btn_repair, self.btn_more, *self.update_buttons):
                w.state(state)
            if busy:
                self.progress.grid()
                self.progress_text.grid()
                self.progress["value"] = 0
                self.progress_text.configure(text=text)
            else:
                self.progress.grid_remove()
                self.progress_text.grid_remove()

        def set_progress(self, frac, text):
            self.progress["value"] = int(frac * 1000)
            if text:
                self.progress_text.configure(text=text)

        def _show_if_working(self):
            self.reveal_job = None
            if self.busy and args.play and not self.winfo_viewable():
                self.deiconify()

        def _bring_to_front(self):
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)    # Windows не всегда даёт фокус чужому окну: поднимаем ненадолго поверх всех
            self.after(300, lambda: self.attributes("-topmost", False))
            self.focus_force()

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

        def toggle_log(self, show=None):
            self.log_visible = (not self.log_visible) if show is None else show
            if self.log_visible:
                self.log_box.grid()
            else:
                self.log_box.grid_remove()
            self.after(30, self._fit_height)

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

        def check_launcher(self):
            if self.busy:
                return
            self.set_busy(True, "Проверяем версию лаунчера…")

            def checked(offer, error):
                self.set_busy(False)
                if error:
                    self.launcher_notice("err", str(error))
                elif offer is None:
                    self.launcher_notice("ok", f"У вас актуальная версия лаунчера {APP_VERSION}.")
                else:
                    self.offer_launcher(offer)
            self.run_bg(lambda: launcher.check(APP_VERSION), checked)

        def offer_launcher(self, offer):
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
            later = ttk.Button(buttons, text="Позже", command=dialog.destroy)
            later.pack(side="left")
            def accept():
                dialog.destroy()
                self.download_launcher(offer)
            ttk.Button(buttons, text="Скачать и установить", command=accept).pack(side="right")
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            dialog.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
            dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
            dialog.deiconify()
            dialog.grab_set()
            later.focus_set()

        def download_launcher(self, offer):
            if self.busy:
                return
            if ctypes.windll.shell32.IsUserAnAdmin():
                self.launcher_notice("info", "Для установки обновления откройте Uma RU Player без прав администратора.")
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
                pack = self.pack_
                result = None
                if fetch and game:
                    self.ui(self.set_progress, 0, "Проверяем доступные обновления…")
                    result = check_channel(pack_root(), game, progress=lambda f, s: self.ui(self.set_progress, f, s))
                    pack = result.pack
                    core.fill_manifest(game)
                rep = core.diagnose(core.Context(game, pack, online=self.online))
                return game, rep, pack, result
            self.run_bg(work, self._after_refresh)

        def _after_refresh(self, result, error):
            self.set_busy(False)
            play_requested, self.play_requested = self.play_requested, False   # просьба действует на одну проверку
            if error is not None:
                self.deiconify()
                self.banner.show("err", f"Не удалось проверить игру: {error}")
                self.primary.configure(text="Проверить снова")
                self.primary.state(["!disabled"])
                return
            self.game, self.report, self.pack_, checked = result
            if checked:
                self.channel_checked = True
                self.channel_result = checked
                release = f" · {checked.tag}" if checked.tag and checked.state in ('current', 'updated') else ""
                variant = updates.read_channel(pack_root())
                edition = 'Облегчённый перевод' if variant and variant.variant == 'lite' else 'Полный перевод'
                self.header.set_subtitle(f"{edition}{release}")
                if checked.state in ('offline', 'error'):
                    log.info("%s", checked.message)
            self.render()
            if not self.auto_done:
                self.auto_done = True
                self._auto()
            elif play_requested:
                self._play_after_request()
            elif args.shot and not self.busy and not getattr(self, "_shot_scheduled", False):
                self._shot_scheduled = True   # после автоматической починки — снимок уже готового состояния
                self.after(1500, self._grab_and_quit)

        def render(self):
            rep = self.report
            c = {x.key: x for x in rep.checks}
            for row, key in ((self.row_game, "game"), (self.row_hachimi, "hachimi"), (self.row_pack, "pack")):
                chk = c.get(key)
                if chk:
                    detail = chk.detail
                    if key == "game" and rep.game is not None:
                        detail = short_path(rep.game.path) + (f" · сборка {rep.game.buildid}" if rep.game.buildid else "")
                    row.set(chk.state, detail, chk.title)
                else:
                    row.set("off", "—")
            if self.online:
                online = self.channel_result
                if online is None:
                    self.row_online.set("info", "Ожидаю выбора папки игры")
                elif online.state in ("offline", "error"):
                    self.row_online.set("warn", online.message)
                elif online.state == "updated":
                    self.row_online.set("ok", f"Выпуск {online.tag} загружен")
                else:
                    self.row_online.set("ok", online.message)
            extra = next((c[k] for k in ("build", "config") if k in c), None)   # «Steam» и «Игра запущена» — в плашке сверху
            if extra:
                self.row_extra.set(extra.state, extra.detail, extra.title)
                self.row_extra.pack(fill="x")
            else:
                self.row_extra.pack_forget()
            labels = {"no_game": ("Указать папку игры…", "Программа не нашла игру в Steam. Укажите папку, где лежит UmamusumePrettyDerby_Jpn.exe."),
                      "blocked": ("Проверить снова", "Когда игра закроется (или Steam закончит обновление), нажмите «Проверить снова»."),
                      "not_installed": ("Установить русификатор", "Программа поставит Hachimi и перевод (около 400 МБ, примерно минута). Игру нужно закрыть."),
                      "update": ("Обновить перевод и играть", "Нашлась новая версия перевода: обновим только изменившиеся файлы."),
                      "repair": ("Починить и играть", "Игра обновилась или файлы повреждены: вернём Hachimi и перевод на место."),
                      "installed_only": ("▶  Играть на русском", "Можно играть с уже установленным переводом."),
                      "need_channel": ("Повторить проверку", "Подключитесь к интернету, чтобы загрузить подходящий выпуск перевода."),
                      "ok": ("▶  Играть на русском", "Всё на месте. Игру можно запускать и обычным способом из Steam — перевод включится сам.")}
            text, hint = labels[rep.mode]
            if not self.online and self.pack_ is None and rep.mode not in ("no_game",):
                text, hint = "Проверить снова", "Распакуйте архив с программой целиком (не запускайте из архива) и откройте UmaRU.exe из папки."
            self.primary.configure(text=text)
            self.primary.state(["!disabled"] if not self.busy else ["disabled"])
            self.hint.configure(text=hint)
            can_repair = self.pack_ and rep.mode not in ("no_game", "blocked", "installed_only", "need_channel")
            self.btn_repair.state(["!disabled"] if can_repair and not self.busy else ["disabled"])
            if rep.mode == "blocked":
                self.banner.show("warn", rep.blocked)
            elif rep.mode in ("repair", "update") and not self.busy:
                self.banner.show("info", {"repair": "Нужна починка: после обновления игры Hachimi мог слететь.",
                                          "update": "Есть новая версия перевода."}[rep.mode])
            elif self.banner_note and not self.busy:
                self.banner.show(*self.banner_note)   # итог последнего действия («Готово…») остаётся, пока человек не нажмёт что-то ещё
            elif not self.busy:
                self.banner.hide()
            self.after(50, self._fit_height)

        def _fit_height(self):
            """Высота окна по содержимому: без пустого поля снизу и без обрезания."""
            self.update_idletasks()
            width = max(self.winfo_width(), K.px(540))   # пока окно скрыто (--play), winfo_width() == 1
            self.geometry(f"{width}x{self.outer.winfo_reqheight()}")

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
            self.banner.show("info", "Работаю… Окно можно не закрывать, это займёт около минуты.")
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
                    self.banner_note = ("err", str(error), "Подробности", lambda: self.toggle_log(True))
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

        def repair_all(self):
            if self.busy or self.game is None:
                return
            if self.report and self.report.mode == "blocked":
                self.banner.show("warn", self.report.blocked)
                return
            self.apply(["hachimi", "pack", "config", "caches"], deep=True, ok_text="Всё проверено и починено.")

        def _launch(self):
            try:
                core.launch_game()
            except OSError as e:
                self.banner.show("err", f"Не удалось запустить игру через Steam: {e}")
                return
            log.info("Игра запущена через Steam")
            self.banner.show("ok", "Игра запускается…")
            self.set_busy(True, "Запускаю игру…")
            self.progress.configure(mode="indeterminate")
            self.progress.start(14)
            self.after(2600, self.destroy)

        def _ask_admin(self):
            if messagebox.askyesno(APP_TITLE, "Папка игры защищена от записи, нужны права администратора.\n\n"
                                              "Перезапустить программу с правами администратора и продолжить?"):
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

        def open_game_dir(self):
            if self.game:
                os.startfile(str(self.game.path))

        def open_log(self):
            p = core.app_home() / "umaru.log"
            if p.is_file():
                os.startfile(str(p))

        def make_shortcut(self):
            if not getattr(sys, "frozen", False):
                self.banner.show("warn", "Ярлык создаётся только из собранной программы (UmaRU.exe).")
                return
            try:
                lnk = core.create_shortcut(sys.executable, "--play", SHORTCUT_NAME)
            except core.UmaError as e:
                self.banner.show("err", str(e))
                return
            core.save_settings({"shortcut_done": True})
            self.banner.show("ok", f"Ярлык «{lnk.stem}» добавлен на рабочий стол.")

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
            self.banner.show("ok", "Отчёт скопирован в буфер обмена: вставьте его в сообщение тому, кто помогает.")

        def uninstall(self):
            if self.busy or self.game is None:
                return
            if not messagebox.askyesno(APP_TITLE, "Удалить русификатор и Hachimi?\n\nHachimi и файлы перевода будут убраны, игра вернётся "
                                                  "к исходному виду. Саму игру это не затронет, а программа останется: "
                                                  "русификатор можно будет поставить снова.", icon="warning"):
                return
            ctx = core.Context(self.game, self.pack_, installer=hachimi_installer())
            self.set_busy(True, "Удаляю…")

            def done(result, error):
                self.set_busy(False)
                if error is not None:
                    self.banner.show("err", str(error) + _ADMIN_HINT if isinstance(error, core.NeedAdmin) else str(error))
                else:
                    self.banner.show("ok", "Русификатор удалён: " + "; ".join(result))
                self.refresh()
            self.run_bg(lambda: core.uninstall(ctx), done)

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
                          f"• сама программа — папка {program['dir']}."]
            else:
                lines.append("• ярлык и настройки программы (саму программу из исходников не трогаем).")
            again = ("заново установить программу (UmaRU-Setup.exe)." if installed
                     else "заново распаковать архив с программой.")
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
            messagebox.showinfo(APP_TITLE, f"Uma RU Player {APP_VERSION}\n"
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

    class _QueueHandler(logging.Handler):
        def __init__(self, q, box):
            super().__init__()
            self.q, self.box = q, box

        def emit(self, record):
            try:
                msg, level, stamp = record.getMessage(), record.levelno, time.strftime("%H:%M:%S")
                self.q.put((self.box.append, (msg, level, stamp)))
            except Exception:  # noqa: BLE001
                pass

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
    ap = argparse.ArgumentParser(description="Uma RU Player")
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update-cache", action="store_true", help="получить и проверить онлайн-выпуск без установки в игру")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--uninstall-all", action="store_true", help="убрать Hachimi, перевод, ярлык, настройки и саму программу")
    ap.add_argument("--keep-program", action="store_true", help="с --uninstall-all: саму программу оставить")
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
    log.info("Uma RU Player %s, пакет %s, запуск: %s", APP_VERSION, pack.version if pack else "нет", " ".join(sys.argv[1:]))
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
            ctypes.windll.user32.MessageBoxW(0, "Программа не смогла открыть окно. Подробности — в журнале:\n"
                                                f"{core.app_home() / 'umaru.log'}", APP_TITLE, 0x10)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

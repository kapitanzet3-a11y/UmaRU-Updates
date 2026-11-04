# -*- coding: utf-8 -*-
"""Общее оформление окон Uma RU: тема, цвета, шрифты и готовые «кирпичики» — карточка, строка состояния, плашка-сообщение,
пояснение с переносом, журнал, шапка. Им пользуются окно владельца (app.pyw) и программа для игроков (player/).

Правила вида: отступы 16 / 12 / 8 px (умножаются на масштаб экрана через px()), у карточки один заголовок и серое пояснение,
главное действие экрана — одна крупная акцентная кнопка, остальные обычные; цвета только из THEME (светлая и тёмная
темы Windows подхватываются сами). Карточка — рамка в 1 px вокруг обычного фона, так что любые элементы внутри
(галочки, переключатели, подписи) не выбиваются из цвета.

Правила пользования: elements с растягиваемым текстом (HintLabel, StatusRow, StatusLine) кладите через fill="x" или
sticky="we", иначе перенос строк не сработает. Банер (Banner) кладётся только через grid().
Дополнения окна владельца: StatusLine — строка результата (точка + длинный текст с переносом), ScrollFrame — прокручиваемая
область (Canvas + полоса, колесо мыши, содержимое в .body; после построения вызвать bind_wheel())."""

import base64
import ctypes
import logging
import os
import tkinter as tk
from tkinter import ttk

try:
    from ui_icon import ICONS
except Exception:  # значок — украшение: без него всё работает
    ICONS = {}

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"

LIGHT = dict(bg="#fafafa", fg="#1c1c1c", muted="#6b6b6b", border="#d9d9d9", accent="#005fb8", ok="#0f7b0f",
             warn="#9d5d00", err="#c42b1c", brand="#534ab7", dot_off="#b3b3b3")
DARK = dict(bg="#1c1c1c", fg="#ffffff", muted="#9a9a9a", border="#3a3a3a", accent="#57c8ff", ok="#6ccb5f",
            warn="#fce100", err="#ff99a4", brand="#7a70e0", dot_off="#6a6a6a")

_SCALE = 1.0


class Theme:
    """Цвета текущей темы (заполняется apply_theme)."""
    dark = False
    bg = fg = muted = border = accent = ok = warn = err = brand = dot_off = "#000000"

    def color(self, state):
        """Цвет по состоянию: ok / warn / err / info / busy / off."""
        return {"ok": self.ok, "warn": self.warn, "err": self.err, "fail": self.err, "info": self.accent,
                "busy": self.accent, "off": self.dot_off}.get(state, self.dot_off)


THEME = Theme()


# ---------------------------------------------------------------- система

def enable_dpi_awareness():
    """Вызывать до создания окна Tk: иначе Windows растягивает окно и текст выходит мутным."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def windows_dark_mode():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def dark_title_bar(window, dark):
    """Тёмная строка заголовка окна (Windows 10 1809+ и 11)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (новые и старые сборки Windows 10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def px(n):
    """Размер в пикселях при 100 % масштабе -> пиксели на этом экране."""
    return int(round(n * _SCALE))


# ---------------------------------------------------------------- тема

def apply_theme(root, dark=None):
    """Включает тему Sun Valley (светлую или тёмную, как в Windows) и заводит стили. Возвращает THEME."""
    global _SCALE
    try:
        _SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        _SCALE = 1.0
    t = THEME
    forced = os.environ.get("UMA_RU_THEME", "").strip().lower()  # для проверки вида: UMA_RU_THEME=dark|light
    if dark is None and forced in ("dark", "light"):
        dark = forced == "dark"
    t.dark = windows_dark_mode() if dark is None else bool(dark)
    try:
        import sv_ttk
        sv_ttk.set_theme("dark" if t.dark else "light")
    except Exception:
        pass
    for key, value in (DARK if t.dark else LIGHT).items():
        setattr(t, key, value)
    style = ttk.Style(root)
    t.bg = style.lookup("TFrame", "background") or t.bg
    t.fg = style.lookup("TLabel", "foreground") or t.fg
    try:
        root.configure(background=t.bg)
    except tk.TclError:
        pass
    style.configure("Hint.TLabel", foreground=t.muted, font=(FONT, 9))
    style.configure("Body.TLabel", font=(FONT, 10))
    style.configure("Secondary.TButton", font=(FONT, 10), padding=(px(14), px(7)))
    style.configure("Heading.TLabel", font=(FONT_SEMI, 11))
    style.configure("Title.TLabel", font=(FONT_SEMI, 13))
    style.configure("Header.TLabel", font=(FONT_SEMI, 17))
    style.configure("Big.TLabel", font=(FONT_SEMI, 20))
    style.configure("Primary.Accent.TButton", font=(FONT_SEMI, 12), padding=(px(24), px(11)))
    # Без значка окна фон ttk на Windows выходит серым (#d9d9d9 вместо #fafafa), хотя root.cget("background") верный:
    # вызов iconphoto до построения окна это лечит (проверено побайтово снимком экрана), поэтому ставим значок здесь.
    set_window_icon(root)
    return t


def icon_image(root, size):
    """PhotoImage значка ближайшего размера (24/32/48/64/96) — без изменения размера на лету, чтобы не мылилось."""
    if not ICONS:
        return None
    best = min(ICONS, key=lambda s: abs(s - size))
    try:
        return tk.PhotoImage(master=root, data=base64.b64decode("".join(ICONS[best])))
    except Exception:
        return None


def set_window_icon(window):
    """Значок окна и панели задач. Ссылки на картинки хранятся в самом окне."""
    images = [icon_image(window, s) for s in (32, 64)]
    images = [i for i in images if i is not None]
    if not images:  # значка нет (не нашёлся ui_icon.py): нужен хотя бы пустой, иначе фон ttk выходит серым (см. apply_theme)
        try:
            images = [tk.PhotoImage(master=window, width=16, height=16)]
        except tk.TclError:
            return
    try:
        window.iconphoto(True, *images)
        window._icon_refs = images
    except tk.TclError:
        pass


def center_window(window, width, height):
    """Размер окна width x height (в px при 100 %) по центру экрана."""
    w, h = px(width), px(height)
    window.update_idletasks()
    x = max(0, (window.winfo_screenwidth() - w) // 2)
    y = max(0, (window.winfo_screenheight() - h) // 3)
    window.geometry(f"{w}x{h}+{x}+{y}")


# ---------------------------------------------------------------- кирпичики

class HintLabel(ttk.Label):
    """Серое пояснение, которое переносится по ширине, выданной ему родителем (кладите с fill='x' / sticky='we')."""

    def __init__(self, parent, text="", style="Hint.TLabel", wrap=420, **kw):
        super().__init__(parent, text=text, style=style, justify="left", wraplength=px(wrap), **kw)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        width = max(px(100), event.width - 2)
        try:
            current = int(float(str(self.cget("wraplength"))))
        except ValueError:
            current = 0
        if abs(width - current) > 2:
            self.configure(wraplength=width)


class Card(tk.Frame):
    """Рамка в 1 px с заголовком и пояснением. Содержимое — в .body (обычный ttk.Frame)."""

    def __init__(self, parent, title="", hint="", padding=16, action=None):
        super().__init__(parent, bg=THEME.bg, highlightthickness=1, highlightbackground=THEME.border,
                         highlightcolor=THEME.border, bd=0)
        self.inner = ttk.Frame(self, padding=px(padding))
        self.inner.pack(fill="both", expand=True)
        self.title_label = self.hint_label = self.action_button = None
        if title or action:
            head = ttk.Frame(self.inner)
            head.pack(fill="x")
            head.columnconfigure(0, weight=1)
            self.title_label = ttk.Label(head, text=title, style="Title.TLabel")
            self.title_label.grid(row=0, column=0, sticky="w")
            if action:
                self.action_button = ttk.Button(head, text=action[0], command=action[1])
                self.action_button.grid(row=0, column=1, sticky="e")
        if hint:
            self.hint_label = HintLabel(self.inner, hint)
            self.hint_label.pack(fill="x", pady=(px(2), 0))
        self.body = ttk.Frame(self.inner)
        self.body.pack(fill="both", expand=True, pady=(px(12) if (title or hint) else 0, 0))


class StatusRow(ttk.Frame):
    """Строка состояния: цветная точка, название, пояснение и (по желанию) кнопка справа."""

    def __init__(self, parent, title, detail="", state="off", action=None):
        super().__init__(parent)
        self.columnconfigure(1, weight=1)
        size = px(14)
        self.dot = tk.Canvas(self, width=size, height=size, highlightthickness=0, bg=THEME.bg)
        self.dot.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, px(10)), pady=(px(3), 0))
        self.dot.create_oval(px(2), px(2), size - px(2), size - px(2), fill=THEME.dot_off, outline="", tags="dot")
        self.title_label = ttk.Label(self, text=title, style="Heading.TLabel")
        self.title_label.grid(row=0, column=1, sticky="w")
        self.detail_label = HintLabel(self, detail)
        self.detail_label.grid(row=1, column=1, sticky="we")
        self.button = None
        if action:
            self.button = ttk.Button(self, text=action[0], command=action[1])
            self.button.grid(row=0, column=2, rowspan=2, sticky="e", padx=(px(12), 0))
        self.state = state
        self.set(state)

    def set(self, state=None, detail=None, title=None):
        if state is not None:
            self.state = state
            self.dot.itemconfigure("dot", fill=THEME.color(state))
        if detail is not None:
            self.detail_label.configure(text=detail)
        if title is not None:
            self.title_label.configure(text=title)


class StatusSummary(ttk.Frame):
    """Главное состояние экрана одной строкой: крупный значок цветом состояния, заголовок и пояснение. Значок различается
    и формой (галочка, «!», «i», крестик, многоточие), а не только цветом. Подробности по частям — в StatusRow."""

    GLYPHS = {"ok": "check", "warn": "bang", "fail": "cross", "err": "cross", "info": "info", "busy": "dots", "off": "dots"}

    def __init__(self, parent, title="", detail="", state="busy"):
        super().__init__(parent)
        self.columnconfigure(1, weight=1)
        self.size = px(40)
        self.icon = tk.Canvas(self, width=self.size, height=self.size, highlightthickness=0, bg=THEME.bg)
        self.icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, px(14)), pady=(px(2), 0))
        self.title_label = ttk.Label(self, text=title, style="Title.TLabel")
        self.title_label.grid(row=0, column=1, sticky="w")
        self.detail_label = HintLabel(self, detail)
        self.detail_label.grid(row=1, column=1, sticky="we")
        self.status = state  # не state: так называется метод ttk-виджета
        self.set(state)

    def set(self, state=None, title=None, detail=None):
        if state is not None:
            self.status = state
            self._draw()
        if title is not None:
            self.title_label.configure(text=title)
        if detail is not None:
            self.detail_label.configure(text=detail)

    def _draw(self):
        c, s = self.icon, self.size
        c.delete("all")
        c.create_oval(1, 1, s - 1, s - 1, fill=THEME.color(self.status), outline="")
        ink, w = THEME.bg, max(2, px(4))   # знак цветом фона: читается и в светлой, и в тёмной теме

        def at(x, y):
            return x * s, y * s

        def dot(x, y, r):
            cx, cy = at(x, y)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=ink, outline="")

        glyph = self.GLYPHS.get(self.status, "dots")
        if glyph == "check":
            c.create_line(*at(.28, .52), *at(.44, .67), *at(.72, .36), fill=ink, width=w, capstyle="round",
                          joinstyle="round")
        elif glyph == "cross":
            for a, b in (((.33, .33), (.67, .67)), ((.67, .33), (.33, .67))):
                c.create_line(*at(*a), *at(*b), fill=ink, width=w, capstyle="round")
        elif glyph == "bang":
            c.create_line(*at(.5, .25), *at(.5, .56), fill=ink, width=w, capstyle="round")
            dot(.5, .73, w * .7)
        elif glyph == "info":
            dot(.5, .28, w * .7)
            c.create_line(*at(.5, .45), *at(.5, .74), fill=ink, width=w, capstyle="round")
        else:
            for x in (.3, .5, .7):
                dot(x, .5, w * .6)


class Banner(tk.Frame):
    """Плашка-сообщение: цветная полоса слева, текст, необязательная кнопка действия. Класть только через grid()."""

    def __init__(self, parent):
        super().__init__(parent, bg=THEME.bg, highlightthickness=1, highlightbackground=THEME.border,
                         highlightcolor=THEME.border, bd=0)
        self.bar = tk.Frame(self, width=px(5), bg=THEME.accent)
        self.bar.pack(side="left", fill="y")
        inner = ttk.Frame(self, padding=(px(14), px(10)))
        inner.pack(side="left", fill="both", expand=True)
        inner.columnconfigure(0, weight=1)
        self.label = HintLabel(inner, "", style="Body.TLabel")
        self.label.grid(row=0, column=0, sticky="we")
        self.button = ttk.Button(inner, text="", style="Secondary.TButton")
        self.button.grid(row=0, column=1, sticky="e", padx=(px(12), 0))
        self.button.grid_remove()

    def show(self, kind, text, action_text=None, action=None):
        self.bar.configure(bg=THEME.color(kind))
        self.label.configure(text=text)
        if action_text and action:
            self.button.configure(text=action_text, command=action)
            self.button.grid()
        else:
            self.button.grid_remove()
        self.grid()

    def hide(self):
        self.grid_remove()


class Header(ttk.Frame):
    """Шапка окна: значок, название, строка под названием."""

    def __init__(self, parent, title, subtitle=""):
        super().__init__(parent)
        self.columnconfigure(1, weight=1)
        self.icon = icon_image(self, px(44))
        if self.icon is not None:
            ttk.Label(self, image=self.icon).grid(row=0, column=0, rowspan=2, padx=(0, px(14)))
        ttk.Label(self, text=title, style="Header.TLabel").grid(row=0, column=1, sticky="w")
        self.subtitle = HintLabel(self, subtitle)
        self.subtitle.grid(row=1, column=1, sticky="we")

    def set_subtitle(self, text):
        self.subtitle.configure(text=text)


class LogBox(ttk.Frame):
    """Журнал: только чтение, время серым, предупреждения и ошибки цветом, не больше max_lines строк."""

    def __init__(self, parent, height=8, max_lines=600):
        super().__init__(parent)
        self.max_lines = max_lines
        self.text = tk.Text(self, wrap="word", height=height, relief="flat", state="disabled", padx=px(10), pady=px(8),
                            bg=THEME.bg, fg=THEME.fg, insertbackground=THEME.fg, font=(FONT, 10),
                            highlightthickness=1, highlightbackground=THEME.border, highlightcolor=THEME.border)
        scroll = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("time", foreground=THEME.muted)
        self.text.tag_configure("warn", foreground=THEME.warn)
        self.text.tag_configure("error", foreground=THEME.err)
        self.text.tag_configure("ok", foreground=THEME.ok)

    def append(self, message, level=logging.INFO, stamp=""):
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else None
        self.text.configure(state="normal")
        if stamp:
            self.text.insert("end", stamp + "  ", "time")
        self.text.insert("end", message + "\n", tag or ())
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > self.max_lines:
            self.text.delete("1.0", f"{lines - int(self.max_lines * 0.8)}.0")
        self.text.configure(state="disabled")
        self.text.see("end")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


def primary_button(parent, text, command):
    """Крупная акцентная кнопка — одна на экран."""
    return ttk.Button(parent, text=text, command=command, style="Primary.Accent.TButton")


def section_title(parent, text):
    return ttk.Label(parent, text=text, style="Title.TLabel")


# ---------------------------------------------------------------- дополнения (окно владельца: результаты, длинные формы)

class StatusLine(ttk.Frame):
    """Строка результата: цветная точка и текст с переносом — для длинных предложений (находки проверки, шаги ремонта).
    В отличие от StatusRow — без заголовка и кнопки. Предупреждения и ошибки подкрашиваются и в тексте. Кладите с
    fill="x" / sticky="we"."""

    def __init__(self, parent, text="", state="off", bold=False):
        super().__init__(parent)
        self.columnconfigure(1, weight=1)
        size = px(14)
        self.dot = tk.Canvas(self, width=size, height=size, highlightthickness=0, bg=THEME.bg)
        self.dot.grid(row=0, column=0, sticky="n", padx=(0, px(10)), pady=(px(3), 0))
        self.dot.create_oval(px(2), px(2), size - px(2), size - px(2), fill=THEME.dot_off, outline="", tags="dot")
        self.label = HintLabel(self, text, style="Heading.TLabel" if bold else "Body.TLabel")
        self.label.grid(row=0, column=1, sticky="we")
        self.status = state  # не state: так называется метод ttk-виджета
        self.set(state)

    def set(self, state=None, text=None):
        if state is not None:
            self.status = state
            self.dot.itemconfigure("dot", fill=THEME.color(state))
            tint = {"warn": THEME.warn, "err": THEME.err, "fail": THEME.err}.get(state)
            self.label.configure(foreground=tint or THEME.fg)
        if text is not None:
            self.label.configure(text=text)


class ScrollFrame(ttk.Frame):
    """Прокручиваемая область (Canvas + полоса прокрутки): содержимое кладите в .body. Полоса видна, только когда оно не
    помещается; место под неё занято всегда, поэтому ширина содержимого не прыгает. Колесо мыши листает область над любым
    её элементом, в том числе над списками выбора и счётчиками (они иначе меняли бы значение). Когда содержимое построено
    или в нём появились новые элементы, вызовите bind_wheel()."""

    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(1, minsize=px(16))
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=THEME.bg, yscrollincrement=px(8))
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=(0, 0, e.width, e.height)))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self._wheel = 0.0
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    def _on_scroll(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()

    def bind_wheel(self, widget=None):
        """Подключить колесо мыши ко всем элементам области (повторный вызов ничего не дублирует)."""
        widget = widget or self.body
        widget.bind("<MouseWheel>", self._on_wheel)
        for child in widget.winfo_children():
            self.bind_wheel(child)

    def _on_wheel(self, event):
        first, last = self.canvas.yview()
        if first > 0.0 or last < 1.0:
            self._wheel += -event.delta / 120 * 9  # 9 шагов по 8 px (в масштабе экрана): около трёх строк на щелчок
            steps = int(self._wheel)
            self._wheel -= steps
            if steps:
                self.canvas.yview_scroll(steps, "units")
        return "break"

    def scroll_to_top(self):
        self.canvas.yview_moveto(0)

    def scroll_to_end(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1)


# ---------------------------------------------------------------- проверка вида: python ui_kit.py [--shot файл.png]

def _demo(shot=None):
    import sys
    enable_dpi_awareness()
    root = tk.Tk()
    apply_theme(root)
    set_window_icon(root)
    root.title("Лаунчер Uma RU — проверка оформления")
    center_window(root, 560, 760)
    outer = ttk.Frame(root, padding=px(20))
    outer.pack(fill="both", expand=True)
    Header(outer, "Uma RU", "Русский перевод Umamusume · полный вариант").pack(fill="x")
    card = Card(outer, "Состояние", "Всё проверяется автоматически при запуске")
    card.pack(fill="x", pady=(px(16), 0))
    for title, detail, state in (("Игра", "Найдена: C:\\Program Files (x86)\\Steam\\steamapps\\common\\UmamusumePrettyDerby_Jpn", "ok"),
                                 ("Hachimi", "Слетел после обновления игры — нажмите «Починить»", "warn"),
                                 ("Русский перевод", "Не хватает 3 файлов", "err"),
                                 ("Steam", "Обновление не идёт", "off")):
        StatusRow(card.body, title, detail, state).pack(fill="x", pady=(0, px(8)))
    banner = Banner(outer)
    banner.grid_forget()
    holder = ttk.Frame(outer)
    holder.pack(fill="x", pady=(px(14), 0))
    holder.columnconfigure(0, weight=1)
    b = Banner(holder)
    b.grid(row=0, column=0, sticky="we")
    b.show("warn", "Игра обновилась: Hachimi мог слететь. Починка занимает несколько секунд.", "Починить", lambda: None)
    primary_button(outer, "▶  Играть на русском", lambda: None).pack(fill="x", pady=(px(16), 0))
    row = ttk.Frame(outer)
    row.pack(fill="x", pady=(px(10), 0))
    ttk.Button(row, text="Починить после обновления", style="Secondary.TButton").pack(side="left")
    ttk.Button(row, text="Дополнительно ▾", style="Secondary.TButton").pack(side="right")
    log = LogBox(outer, height=6)
    log.pack(fill="both", expand=True, pady=(px(16), 0))
    log.append("Игра найдена", logging.INFO, "14:02:11")
    log.append("Hachimi: файл запуска заменён Steam", logging.WARNING, "14:02:11")
    log.append("Не удалось записать файл", logging.ERROR, "14:02:12")
    root.update()
    if shot:
        from PIL import ImageGrab
        root.attributes("-topmost", True)
        root.update()

        def grab():
            root.update()
            print("экран:", root.winfo_screenwidth(), "x", root.winfo_screenheight(), "| окно:", root.winfo_width(), "x",
                  root.winfo_height(), "+", root.winfo_rootx(), "+", root.winfo_rooty(), "| масштаб:", _SCALE)
            img = ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx() + root.winfo_width(),
                                       root.winfo_rooty() + root.winfo_height()), all_screens=True)
            img.save(shot)
            root.destroy()
        root.after(1500, grab)
    root.mainloop()


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I не добавляет папку скрипта: нужен ui_icon.py
    from ui_kit import _demo as run_demo  # модуль под своим именем, а не __main__: тема и значки общие
    run_demo(sys.argv[sys.argv.index("--shot") + 1] if "--shot" in sys.argv else None)

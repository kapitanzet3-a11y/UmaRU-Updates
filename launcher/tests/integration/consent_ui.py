"""Exercise actual Tk buttons and callbacks; network, game and setup are isolated."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import sys
import tempfile
import time
import tkinter as tk

HERE = Path(__file__).resolve().parent
PLAYER = HERE.parents[1] / "player"
sys.path.insert(0, str(PLAYER))
import umaru_app as player


def wait_until(app, predicate):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        app.update()
        if predicate():
            return
        time.sleep(.025)
    raise AssertionError("UI callback timed out")


def descendants(window):
    for child in window.winfo_children():
        yield child
        yield from descendants(child)


def action(window, text):
    return next(child for child in descendants(window)
                if child.winfo_class() == "TButton" and child.cget("text") == text)


with tempfile.TemporaryDirectory(dir=HERE) as work, ExitStack() as mocks:
    root = Path(work)
    (root / "update-channel.json").write_text("{}", encoding="utf-8")
    mocks.enter_context(patch.dict(os.environ, {"UMA_RU_HOME": work, "UMA_RU_PACK": work, "UMA_STEAM_ROOT": ""}))
    mocks.enter_context(patch.object(player.core, "find_game", return_value=None))
    check = mocks.enter_context(patch.object(player.launcher, "check"))
    download = mocks.enter_context(patch.object(player.launcher, "download", return_value=root / "fixture.exe"))
    grant = mocks.enter_context(patch.object(player.ctypes.windll.shell32, "IsUserAnAdmin", return_value=0))
    args = SimpleNamespace(shot="", play=False, auto=False, shot_after=0, game_dir="")
    app = player.build_window(args, None)
    wait_until(app, lambda: app.auto_done)
    proc = mocks.enter_context(patch.object(player.subprocess, "Popen"))
    assert check.call_count == download.call_count == proc.call_count == 0
    tl, launcher = app.update_buttons
    assert tl.winfo_y() == launcher.winfo_y() and launcher.winfo_x() >= tl.winfo_width()
    assert launcher.winfo_x() + launcher.winfo_width() <= launcher.master.winfo_width()
    tl.invoke()
    wait_until(app, lambda: not app.busy)
    assert check.call_count == download.call_count == proc.call_count == 0
    check.return_value = None
    launcher.invoke()
    wait_until(app, lambda: not app.busy)
    assert player.APP_VERSION in app.banner_note[1]
    assert proc.call_count == download.call_count == 0
    offer = SimpleNamespace(version="1.3", size=24_000_000, notes="Тест сезонного оформления.")
    check.return_value = offer
    def open_offer():
        launcher.invoke()
        wait_until(app, lambda: app.grab_current() is not None)
        return app.grab_current()
    action(open_offer(), "Позже").invoke()
    assert app.winfo_exists() and not app.busy and proc.call_count == download.call_count == 0
    check.side_effect = player.launcher.LauncherUpdateError("Подпись не прошла проверку")
    launcher.invoke()
    wait_until(app, lambda: not app.busy)
    assert app.banner_note[0] == "err" and proc.call_count == download.call_count == 0
    check.side_effect = None
    download.side_effect = player.launcher.LauncherUpdateError("Файл повреждён")
    action(open_offer(), "Скачать и установить").invoke()
    wait_until(app, lambda: not app.busy)
    assert app.winfo_exists() and app.banner_note[0] == "err" and proc.call_count == 0
    download.side_effect = None
    final_offer = open_offer()
    original_after = app.after
    def stop_poll_after_handoff(delay, callback=None, *callback_args):
        if callback == app._poll:
            return None
        return original_after(delay, callback, *callback_args)
    with patch.object(app, "after", side_effect=stop_poll_after_handoff):
        action(final_offer, "Скачать и установить").invoke()
        wait_until(app, lambda: proc.called)
    assert proc.call_count == 1 and proc.call_args.args[0][1:] == ["/UMARUUPDATE"]
    assert download.call_args.args[0] is offer
    print("PASS: no startup/translation-triggered check, adjacent visible buttons, current, later, errors and explicit handoff")

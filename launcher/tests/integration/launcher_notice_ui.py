"""New launcher notice at startup and before the game starts; only an explicit click downloads it."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import sys
import tempfile
import time

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


def scenario(play, snoozed=False, accept=False):
    with tempfile.TemporaryDirectory(dir=HERE) as work, ExitStack() as mocks:
        root = Path(work)
        (root / "update-channel.json").write_text("{}", encoding="utf-8")
        mocks.enter_context(patch.dict(os.environ, {"UMA_RU_HOME": work, "UMA_RU_PACK": work, "UMA_STEAM_ROOT": ""}))
        mocks.enter_context(patch.object(player.core, "find_game", return_value=None))
        offer = SimpleNamespace(version="9.9", size=24_000_000, notes="Что нового.")
        mocks.enter_context(patch.object(player.launcher, "check", return_value=offer))
        download = mocks.enter_context(patch.object(player.launcher, "download",
                                                    side_effect=player.launcher.LauncherUpdateError("стоп")))
        mocks.enter_context(patch.object(player.ctypes.windll.shell32, "IsUserAnAdmin", return_value=0))
        game = mocks.enter_context(patch.object(player.core, "launch_game"))
        if snoozed:
            player.snooze_launcher("9.9")
        app = player.build_window(SimpleNamespace(shot="", play=play, auto=False, shot_after=0, game_dir=""), None)
        wait_until(app, lambda: app.auto_done and app.launcher_checked)
        assert app.btn_launcher.cget("text") == "Новый лаунчер 9.9…"
        if play:
            app._launch()                       # то, что делает ярлык «Играть», когда игра готова
        dialog = app.grab_current()
        if snoozed:
            wait_until(app, lambda: game.called)
            assert dialog is None and download.call_count == 0
        elif accept:
            assert dialog is not None and game.call_count == 0
            action(dialog, "Скачать и установить").invoke()
            wait_until(app, lambda: download.called and not app.busy)
            assert game.call_count == 0          # игру не запускаем: игрок выбрал обновление
        else:
            assert dialog is not None and game.call_count == 0
            action(dialog, "Позже").invoke()
            if play:
                wait_until(app, lambda: game.called)
            assert download.call_count == 0 and player.launcher_snoozed("9.9")
        if app.winfo_exists():
            app.destroy()


SCENARIOS = {"window": dict(play=False), "play-later": dict(play=True),
             "play-download": dict(play=True, accept=True), "play-snoozed": dict(play=True, snoozed=True)}

if len(sys.argv) > 1:
    scenario(**SCENARIOS[sys.argv[1]])          # один Tk на процесс, как у настоящей программы
else:
    import subprocess
    for name in SCENARIOS:
        subprocess.run([sys.executable, __file__, name], check=True, timeout=120)
    print("PASS: startup notice, notice before the game, later starts the game, snoozed version is quiet, explicit download")

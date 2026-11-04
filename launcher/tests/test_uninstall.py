"""Uninstall hand-off to the Inno uninstaller, Hachimi removal without a cached pack, download cleanup."""
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent / "player"))
sys.path.insert(0, str(here.parent))
import umaru_core as core
import umaru_launcher as launcher


def patched_game_exe(path):
    """Smallest PE that exe_kind() reads as the unpacked game (no Steam .bind section)."""
    head = bytearray(b"MZ" + bytes(62))
    struct.pack_into("<I", head, 60, 64)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0, 0)
    section = b".text\0\0\0" + bytes(32)
    path.write_bytes(bytes(head) + coff + section)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"UMA_RU_HOME": str(self.root / "home"),
                                           "UMA_RU_DESKTOP": str(self.root / "desktop"),
                                           "UMA_RU_FAKE_RUNNING": "0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class HachimiRemoval(Isolated):
    def game_with_hachimi(self):
        game = core.Game(self.root / "game")
        game.path.mkdir()
        patched_game_exe(game.path / core.GAME_EXE)
        with open(game.path / core.PROXY_DLL, "wb") as dll:
            dll.truncate(core.HACHIMI_MIN_DLL_BYTES + 1)
        self.assertEqual(core.hachimi_state(game.path).kind, "ok")
        return game

    def test_without_pack_uses_installer_next_to_program(self):
        game = self.game_with_hachimi()
        installer = self.root / "hachimi_installer.exe"
        installer.write_bytes(b"stub")
        with patch.object(core, "run_installer", return_value=(0, "")) as run:
            done = core.uninstall(core.Context(game, None, online=True, installer=installer))
        run.assert_called_once_with(installer, game.path, "uninstall")
        self.assertIn("Hachimi удалён", done)

    def test_missing_installer_is_reported(self):
        game = self.game_with_hachimi()
        with patch.object(core, "run_installer") as run:
            done = core.uninstall(core.Context(game, None, online=True))
        run.assert_not_called()
        self.assertTrue(any(line.startswith("Hachimi остался") for line in done), done)


class ProgramRemoval(Isolated):
    def program(self, uninstaller=None):
        folder = self.root / "program"
        folder.mkdir(exist_ok=True)
        (folder / "UmaRU.exe").write_bytes(b"exe")
        return {"dir": folder, "exe": folder / "UmaRU.exe", "shortcut": "", "pid": 4242, "uninstaller": uninstaller}

    def test_find_uninstaller_needs_its_log(self):
        folder = self.root / "program"
        folder.mkdir()
        self.assertIsNone(core.find_uninstaller(folder))
        (folder / "unins000.exe").write_bytes(b"exe")
        self.assertIsNone(core.find_uninstaller(folder))
        (folder / "unins000.dat").write_bytes(b"log")
        self.assertEqual(core.find_uninstaller(folder), folder / "unins000.exe")

    def test_installed_program_is_handed_to_uninstaller(self):
        uninstaller = self.root / "program" / "unins000.exe"
        done, plan = core.uninstall_all(core.Context(None, None), self.program(uninstaller))
        self.assertEqual(plan, {"uninstaller": uninstaller, "pid": 4242})
        self.assertTrue(any("деинсталлятор" in line for line in done), done)
        with patch.object(core, "schedule_uninstaller") as run, patch.object(core, "schedule_removal") as remove:
            core.schedule_program_removal(plan)
        run.assert_called_once_with(uninstaller, 4242)
        remove.assert_not_called()

    def test_portable_program_removes_its_files(self):
        program = self.program()
        done, plan = core.uninstall_all(core.Context(None, None), program)
        self.assertNotIn("uninstaller", plan)
        self.assertIn(program["exe"], plan["items"])
        self.assertIn(core.app_home(), plan["items"])
        with patch.object(core, "schedule_uninstaller") as run, patch.object(core, "schedule_removal") as remove:
            core.schedule_program_removal(plan)
        run.assert_not_called()
        remove.assert_called_once_with(plan["items"], plan["dirs"], 4242)

    def test_keep_program_returns_no_plan(self):
        done, plan = core.uninstall_all(core.Context(None, None), None)
        self.assertIsNone(plan)

    def test_program_info_reports_inno_uninstaller(self):
        import umaru_app
        folder = self.root / "program"
        folder.mkdir()
        (folder / "unins000.exe").write_bytes(b"exe")
        (folder / "unins000.dat").write_bytes(b"log")
        with patch.dict(os.environ, {"UMA_RU_PROGRAM_DIR": str(folder)}):
            self.assertEqual(umaru_app.program_info()["uninstaller"], folder / "unins000.exe")


class LauncherDownloads(Isolated):
    def test_cleanup_removes_only_old_download_folders(self):
        parent = self.root / "UmaRU-launcher"
        old = parent / ("umaru-launcher-" + "a" * 32)
        fresh = parent / ("umaru-launcher-" + "b" * 32)
        foreign = parent / "keep-me"
        for folder in (old, fresh, foreign):
            folder.mkdir(parents=True)
            (folder / "UmaRU-Setup.exe").write_bytes(b"setup")
        stamp = time.time() - 2 * 3600
        for folder in (old, foreign):
            os.utime(folder, (stamp, stamp))
        self.assertEqual(launcher.cleanup_downloads(parent), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(foreign.exists())

    def test_cleanup_without_folder_is_harmless(self):
        self.assertEqual(launcher.cleanup_downloads(self.root / "missing"), 0)


if __name__ == "__main__":
    unittest.main()

"""Windows regression checks against isolated, synthetic game directories."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent / "player"))
import umaru_core as core


class SafeApply(unittest.TestCase):
    def test_second_writer_is_blocked(self):
        with tempfile.TemporaryDirectory(dir=here) as root, core.game_write_lock(Path(root)):
            proc = subprocess.run([sys.executable, "-B", "-I", __file__, "contend", root], timeout=20)
            self.assertEqual(proc.returncode, 0)

    def test_junction_to_sibling(self):
        with tempfile.TemporaryDirectory(dir=here) as root:
            base = Path(root) / "hachimi"
            sibling = Path(root) / "other"
            base.mkdir()
            sibling.mkdir()
            link = base / "localized_data_ru"
            made = subprocess.run(["powershell", "-NoProfile", "-Command",
                                   "New-Item -ItemType Junction -Path $env:UMA_TEST_LINK -Target $env:UMA_TEST_TARGET | Out-Null"],
                                  env={**os.environ, "UMA_TEST_LINK": str(link), "UMA_TEST_TARGET": str(sibling)},
                                  capture_output=True, text=True)
            if made.returncode:
                if "Access is denied" in made.stderr or "Отказано в доступе" in made.stderr:
                    self.skipTest("Windows sandbox denies junction creation")
                self.fail(made.stderr)
            try:
                with self.assertRaises(core.UmaError):
                    core.safe_join(base, "localized_data_ru/dict.json", for_write=True)
            finally:
                link.rmdir()

    def test_retry_obsolete_after_sharing_violation(self):
        with tempfile.TemporaryDirectory(dir=here) as root:
            game = core.Game(Path(root))
            old = "localized_data_ru/old.json"
            path = game.hachimi / old
            path.parent.mkdir(parents=True)
            path.write_bytes(b"old")
            prev = {"pack_version": "2026.11.02-233201", "entries": {old: core.sha256_file(path)}}
            core.save_state(game.path, prev)
            pack = core.Pack(game.path, {"version": "2026.11.03-124603", "entries": {}}, game.path, game.path)
            ctx = core.Context(game, pack, online=True)
            os.environ["UMA_RU_FAKE_RUNNING"] = "0"
            with path.open("rb"), self.assertRaises(core.UmaError):
                core.apply_plan(ctx, ["pack"])
            self.assertEqual(core.load_state(game.path), prev)
            core.apply_plan(ctx, ["pack"])
            self.assertFalse(path.exists())
            self.assertEqual(core.load_state(game.path)["pack_version"], pack.version)


if sys.argv[1:2] == ["contend"]:
    try:
        with core.game_write_lock(Path(sys.argv[2])):
            raise SystemExit(2)
    except core.UmaError:
        raise SystemExit(0)
elif __name__ == "__main__":
    unittest.main()

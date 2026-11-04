"""«Позже» in the new launcher offer: quiet for three days and only for that version."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent / "player"))
sys.path.insert(0, str(here.parent))
import umaru_app as app


class Snooze(unittest.TestCase):
    def test_later_is_per_version_and_expires(self):
        with tempfile.TemporaryDirectory(dir=here) as home, patch.dict(os.environ, {"UMA_RU_HOME": home}):
            self.assertFalse(app.launcher_snoozed("1.4", now=1000))
            app.snooze_launcher("1.4", now=1000)
            self.assertTrue(app.launcher_snoozed("1.4", now=1000 + app.LAUNCHER_SNOOZE - 1))
            self.assertFalse(app.launcher_snoozed("1.4", now=1000 + app.LAUNCHER_SNOOZE))
            self.assertFalse(app.launcher_snoozed("1.5", now=1001))


class HachimiVersion(unittest.TestCase):
    def test_version_from_installer_provenance(self):
        with tempfile.TemporaryDirectory(dir=here) as data, patch.dict(os.environ, {"UMA_RU_PACK": data}):
            self.assertEqual(app.hachimi_version(), "")
            Path(data, "hachimi_installer.txt").write_text(
                "Установщик Hachimi Edge (v0.33.1), как он выложен автором.\n", encoding="utf-8")
            self.assertEqual(app.hachimi_version(), "0.33.1")


if __name__ == "__main__":
    unittest.main()

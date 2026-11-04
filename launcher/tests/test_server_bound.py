"""Server-bound localize defaults (errors 205 and 395) stay native in a release."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
import server_bound


class ServerBound(unittest.TestCase):
    def check(self, actual):
        with tempfile.TemporaryDirectory(dir=here) as raw:
            root = Path(raw)
            native = {key: "ネイティブ" for key in server_bound.NATIVE_FALLBACK}
            native["Character701028"] = "プラン{0}"
            english = {key: f"EN {key}" for key in server_bound.UMATL_DEFAULTS}
            (root / "native.json").write_text(json.dumps(native, ensure_ascii=False), encoding="utf-8")
            (root / "en.json").write_text(json.dumps(english), encoding="utf-8")
            target = root / "localize_dict.json"
            target.write_text(json.dumps({**english, **actual}, ensure_ascii=False), encoding="utf-8")
            return server_bound.check(target, root / "en.json", root / "native.json")

    def test_clean_dictionary_passes(self):
        self.assertEqual(self.check({"Character701028": "Plan {0}", "Home0001": "Главная"}), [])

    def test_translated_autoplay_strategy_name_fails(self):
        errors = self.check({"SingleModeAutoPlay425023": "Новый план автоподготовки",
                             "SingleModeAutoPlay425024": "Свободно настраиваемый план"})
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("native fallback" in e for e in errors))

    def test_translated_practice_list_name_fails(self):
        self.assertEqual(len(self.check({"PracticeRace400201": "Копирование списка"})), 1)

    def test_cyrillic_plan_template_fails(self):
        self.assertEqual(len(self.check({"Character701028": "План {0}"})), 1)


if __name__ == "__main__":
    unittest.main()

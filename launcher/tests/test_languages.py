"""Per-part language choice: composing the install set, applying it to a synthetic game, English comics releases."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent / "player"))
sys.path.insert(0, str(here.parent))
import umaru_core as core
import publish_online as pub

LD = "localized_data_ru/"
COMIC_RU = LD + "assets/textures/outgame/comic/tex_comic_1001_01.diff.png"    # переведённая страница
COMIC_EN = LD + "assets/textures/outgame/comic/tex_comic_1002_01.diff.png"    # страница UmaTL без перевода
FILES = {
    LD + "config.json": json.dumps({"assets_dir": "assets", "use_text_wrapper": True,
                                    "replacement_font_name": "assets/font.otf", "remove_ruby": True}).encode(),
    LD + "info.json": b'{"name": "UmaTL"}',
    LD + "text_data_dict.json": b'{"1": "Russian"}',
    LD + "includes_win": b"font bundle",
    LD + "assets/story/data/01/0001/storytimeline_010010001.json": b'{"text": "ru"}',
    LD + "assets/textures/race/racetitle/title_00.diff.png": b"\x89PNG race ru",
    LD + "assets/atlas/race/race_tex.json": b'{"atlas": 1}',
    COMIC_RU: b"\x89PNG comic ru",
    COMIC_EN: b"\x89PNG comic en",
    LD + "assets/movies/mainstory/contents/mainstory_contents_2001.usm": b"movie ru",
}
ENGLISH = {COMIC_RU: b"\x89PNG comic en for 1001"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_pack(root, files, name="pack.zip"):
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / name, "w") as z:
        for rel, data in files.items():
            z.writestr(rel, data)
    entries = {rel: [len(data), digest(data)] for rel, data in files.items()}
    manifest = {"format": 1, "version": "2026.11.03-1200", "zip": "pack.zip", "files": len(entries),
                "bytes": sum(v[0] for v in entries.values()), "entries": entries}
    return core.Pack(root, manifest, root / name, root / "hachimi_installer.exe")


class Compose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.root = Path(self.tmp.name)
        self.pack = make_pack(self.root / "pack", FILES)
        self.english = make_pack(self.root / "english", ENGLISH)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parts(self):
        parts = {rel: core.content_part(rel) for rel in FILES}
        self.assertIsNone(parts[LD + "config.json"])
        self.assertIsNone(parts[LD + "info.json"])
        self.assertEqual(parts[LD + "includes_win"], "texts")
        self.assertEqual(parts[LD + "assets/story/data/01/0001/storytimeline_010010001.json"], "texts")
        self.assertEqual(parts[LD + "assets/atlas/race/race_tex.json"], "images")
        self.assertEqual(parts[COMIC_RU], "comics")
        self.assertEqual(parts[LD + "assets/movies/mainstory/contents/mainstory_contents_2001.usm"], "movie")

    def test_choices_are_normalized(self):
        self.assertEqual(core.normalize_choices({"comics": "en", "texts": "fr", "x": "ja"}),
                         {"texts": "ru", "images": "ru", "comics": "en", "movie": "ru"})

    def test_all_russian_keeps_pack(self):
        self.assertIs(core.compose_pack(self.pack, {}), self.pack)

    def test_japanese_parts_are_left_out(self):
        pack = core.compose_pack(self.pack, {"images": "ja", "comics": "ja", "movie": "ja"})
        parts = {core.content_part(rel) for rel in pack.entries}
        self.assertEqual(parts, {None, "texts"})
        self.assertEqual(pack.manifest["files"], len(pack.entries))
        self.assertEqual(pack.version, self.pack.version)

    def test_japanese_texts_get_a_plain_config(self):
        pack = core.compose_pack(self.pack, {"texts": "ja"})
        self.assertNotIn(LD + "includes_win", pack.entries)
        self.assertNotIn(LD + "text_data_dict.json", pack.entries)
        config = json.loads(pack.sources[LD + "config.json"])
        self.assertEqual(config, {"assets_dir": "assets"})
        size, sha = pack.entries[LD + "config.json"]
        self.assertEqual(sha, digest(pack.sources[LD + "config.json"]))
        self.assertIn(LD + "assets/textures/race/racetitle/title_00.diff.png", pack.entries)

    def test_english_comics_replace_russian_pages(self):
        pack = core.compose_pack(self.pack, {"comics": "en"}, comics_en=self.english)
        self.assertEqual(pack.entries[COMIC_RU], self.english.entries[COMIC_RU])
        self.assertEqual(pack.sources[COMIC_RU], self.english.zip_path)
        self.assertEqual(pack.entries[COMIC_EN], self.pack.entries[COMIC_EN])
        self.assertNotIn(COMIC_EN, pack.sources)

    def test_english_from_another_release_is_ignored(self):
        self.english.manifest["version"] = "2026.11.02-1200"
        pack = core.compose_pack(self.pack, {"comics": "en"}, comics_en=self.english)
        self.assertEqual(pack.entries[COMIC_RU], self.pack.entries[COMIC_RU])
        self.assertNotIn(COMIC_RU, pack.sources)

    def test_english_without_extra_keeps_pack_comics(self):
        pack = core.compose_pack(self.pack, {"comics": "en"})
        self.assertEqual(pack.entries[COMIC_RU], self.pack.entries[COMIC_RU])


class ApplyChoices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"UMA_RU_HOME": str(self.root / "home"), "UMA_RU_FAKE_RUNNING": "0"})
        self.env.start()
        self.pack = make_pack(self.root / "pack", FILES)
        self.english = make_pack(self.root / "english", ENGLISH)
        self.game = core.Game(self.root / "game")
        self.game.path.mkdir()
        (self.game.path / core.GAME_EXE).write_bytes(b"MZ")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def install(self, choices, comics_en=None):
        pack = core.compose_pack(self.pack, choices, comics_en=comics_en)
        ctx = core.Context(self.game, pack, online=True)
        report = core.diagnose(ctx)
        if report.plan:
            core.apply_plan(ctx, report.plan)    # как окно: всё, что нашла проверка (в первый раз — и настройки Hachimi)
        return pack, report

    def file(self, rel):
        return self.game.hachimi / rel

    def test_switching_languages_updates_the_game(self):
        self.install({})
        self.assertEqual(self.file(COMIC_RU).read_bytes(), FILES[COMIC_RU])
        # Hachimi уже собрал из русской страницы готовую картинку
        built = self.file(COMIC_RU[:-len(".diff.png")] + ".png")
        built.write_bytes(b"built from russian")
        built.with_name(built.name + ".srchash").write_bytes(b"hash")

        _pack, report = self.install({"comics": "en"}, comics_en=self.english)
        self.assertEqual(report.mode, "update")
        self.assertIn("языки", report.check("pack").detail)
        self.assertEqual(self.file(COMIC_RU).read_bytes(), ENGLISH[COMIC_RU])
        self.assertFalse(built.exists())
        self.assertFalse(built.with_name(built.name + ".srchash").exists())

        built.write_bytes(b"built from english")
        self.install({"comics": "ja", "texts": "ja", "movie": "ja"})
        self.assertFalse(self.file(COMIC_RU).exists())
        self.assertFalse(self.file(COMIC_EN).exists())
        self.assertFalse(built.exists())
        self.assertFalse(self.file(LD + "includes_win").exists())
        self.assertFalse(self.file(LD + "assets/movies/mainstory/contents/mainstory_contents_2001.usm").exists())
        self.assertEqual(json.loads(self.file(LD + "config.json").read_text()), {"assets_dir": "assets"})
        self.assertTrue(self.file(LD + "assets/textures/race/racetitle/title_00.diff.png").exists())

        _pack, report = self.install({})
        self.assertEqual(core.diagnose(core.Context(self.game, self.pack, online=True)).mode, "ok")
        for rel, data in FILES.items():
            self.assertEqual(self.file(rel).read_bytes(), data, rel)

    def test_pack_png_next_to_diff_is_not_a_cache(self):
        files = dict(FILES)
        files[COMIC_RU[:-len(".diff.png")] + ".png"] = b"\x89PNG shipped"
        self.pack = make_pack(self.root / "pack2", files)
        self.install({})
        self.install({"comics": "en"}, comics_en=self.english)
        self.assertEqual(self.file(COMIC_RU[:-len(".diff.png")] + ".png").read_bytes(), b"\x89PNG shipped")


class EnglishComicsRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=here)
        self.root = Path(self.tmp.name)
        full_files = {COMIC_RU: b"\x89PNG\r\n\x1a\nru", COMIC_EN: b"\x89PNG\r\n\x1a\nen 1002",
                      LD + "text_data_dict.json": b"{}"}
        lite_files = {k: v for k, v in full_files.items() if k != COMIC_EN}
        self.full, self.full_pack = self.manifest(full_files)
        self.lite, _ = self.manifest(lite_files)
        self.en_dir = self.root / "umatl" / "comic"
        self.en_dir.mkdir(parents=True)
        (self.en_dir / "tex_comic_1001_01.diff.png").write_bytes(b"\x89PNG\r\n\x1a\nen 1001")

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for rel, data in files.items():
                z.writestr(rel, data)
        entries = {rel: [len(data), digest(data)] for rel, data in files.items()}
        return ({"format": 1, "version": "2026.11.03-124603", "created": "2026-11-03 12:46:03", "hachimi": "0.33.1",
                 "zip": "pack.zip", "files": len(entries), "bytes": sum(v[0] for v in entries.values()),
                 "zip_bytes": len(buffer.getvalue()), "entries": entries}, buffer.getvalue())

    def test_full_gets_translated_pages_lite_gets_all(self):
        extras = pub.english_comics(self.full, self.full_pack, self.lite, self.en_dir)
        full_manifest, full_zip = extras["full"]
        lite_manifest, lite_zip = extras["lite"]
        self.assertEqual(set(full_manifest["entries"]), {COMIC_RU})
        self.assertEqual(set(lite_manifest["entries"]), {COMIC_RU, COMIC_EN})
        self.assertEqual(lite_manifest["entries"][COMIC_EN], self.full["entries"][COMIC_EN])
        with zipfile.ZipFile(io.BytesIO(full_zip)) as z:
            self.assertEqual(z.read(COMIC_RU), b"\x89PNG\r\n\x1a\nen 1001")
        self.assertEqual(full_manifest["zip_bytes"], len(full_zip))
        self.assertEqual(lite_manifest["version"], self.full["version"])

    def test_missing_page_fails(self):
        (self.en_dir / "tex_comic_1001_01.diff.png").unlink()
        with self.assertRaises(pub.ReleaseError):
            pub.english_comics(self.full, self.full_pack, self.lite, self.en_dir)

    def test_russian_page_given_as_english_fails(self):
        (self.en_dir / "tex_comic_1001_01.diff.png").write_bytes(b"\x89PNG\r\n\x1a\nru")
        with self.assertRaises(pub.ReleaseError):
            pub.english_comics(self.full, self.full_pack, self.lite, self.en_dir)


if __name__ == "__main__":
    unittest.main()

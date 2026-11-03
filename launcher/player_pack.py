# -*- coding: utf-8 -*-
"""Упаковка перевода «для друга»: собирает из ваших файлов готовый пакет (pack.zip + pack.json) и выпуск —
один zip, который можно отправить человеку: программа Uma RU + перевод + установщик Hachimi. API-ключ не нужен ни при
сборке, ни у друга.

Командная строка (её же вызывает кнопка «Пакет для друга» в окне программы владельца):
  python -I player_pack.py info                         что попадёт в пакет и всё ли готово
  python -I player_pack.py build [--out ПАПКА] [--no-release] [--lite]     (--lite: без комиксов и ролика, на ~140 МБ меньше)
печатает строки  PROGRESS <0-100> <текст>  и в конце  DONE <путь к zip> <размер в МБ>  (или  ERROR <текст>, код выхода 1).

Что берётся: всё из hachimi\\localized_data_ru (словари меню, тексты игры, описания навыков, реплики, комментарии, переведённые
главы, картинки, шрифт) КРОМЕ кэшей картинок (X.png рядом с X.diff.png и X.png.srchash: Hachimi соберёт их из игры друга),
файлов для Android и временного. Настройки Hachimi для друга — ваш config.json с выключенным режимом переводчика. Ключ API,
база переводов, копии и программа перевода в пакет НЕ попадают; перед упаковкой все текстовые файлы проверяются на «sk-ant-».
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server_bound import check as check_server_bound
LD_NAME = "localized_data_ru"
SKIP_DIRS = ("includes_android",)
SKIP_SUFFIXES = (".tmp", ".part", ".bak", ".umaru-part", ".log", ".pyc")
SKIP_NAMES = ("thumbs.db", "desktop.ini", ".ds_store")
TEXT_SUFFIXES = (".json", ".txt", ".csv", ".tsv")
DEFLATE_SUFFIXES = (".json", ".txt", ".csv", ".tsv", ".xml", ".html", ".md")
SECRET_RE = re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}")
KEEP_RELEASES = 3
README_PLAYER = "ПРОЧТИ МЕНЯ (для игрока).txt"

# Что меняем в вашем config.json для друга: без режима переводчика (у него нет сервера перевода), без автообновления
# английского перевода (оно перезаписало бы русский), без отладки и Discord, FPS по умолчанию; остальное — как у вас.
FRIEND_CONFIG = {
    "localized_data_dir": LD_NAME, "translator_mode": False, "auto_translate_stories": False,
    "auto_translate_localize": False, "disable_translations": False, "tl_auto_updater_mode": "Disabled",
    "translation_repo_index": None, "selected_tl_repo_id": None, "skip_first_time_setup": True,
    "debug_mode": False, "discord_rpc": False, "target_fps": 60, "shortcuts": [], "live_vocals_swap_presets": {},
    "custom_font_file": None, "custom_title_name": None,
}


class PackError(Exception):
    pass


def require_server_safe(target):
    """Check the actual payload, including packs inherited from older releases."""
    errors = check_server_bound(Path(target))
    if errors:
        raise PackError("Небезопасные служебные переводы (ошибка 205): " + "; ".join(errors))


def emit(kind, *parts):
    print(kind, *parts, flush=True)


def care_dir():
    """Папка обслуживания (рядом: release\\, hachimi_installer\\): UMA_CARE_HOME, затем Рабочий стол\\Программы\\UmaRU."""
    env = os.environ.get("UMA_CARE_HOME")
    candidates = [Path(env)] if env else []
    home = Path.home()
    candidates += [home / "Desktop" / "Программы" / "UmaRU", home / "OneDrive" / "Desktop" / "Программы" / "UmaRU",
                   home / "Рабочий стол" / "Программы" / "UmaRU"]
    for c in candidates:
        if (c / "hachimi_installer").is_dir() or (c / "release").is_dir() or (c / "tools").is_dir():
            return c
    return candidates[0] if candidates else None


def is_cache_png(path, names):
    """X.png — кэш Hachimi (оригинал игры + наша разница), если рядом X.diff.png или X.png.srchash."""
    name = path.name
    low = name.lower()
    if not low.endswith(".png") or low.endswith(".diff.png"):
        return False
    return f"{name[:-4]}.diff.png" in names or f"{name}.srchash" in names


def is_lite_skip(rel):
    """Облегчённый вариант: без английских комиксов (tex_comic*, ~100 МБ) и ролика (.usm, ~40 МБ)."""
    low = rel.lower()
    return "tex_comic" in low or low.endswith(".usm")


def collect(hachimi_dir, lite=False):
    """-> (файлы [(относительный путь с /, полный путь, размер)], пропущено {причина: (число, байт)})."""
    ld = Path(hachimi_dir) / LD_NAME
    if not ld.is_dir():
        raise PackError(f"Нет папки перевода {ld}.")
    files, skipped = [], {}
    comic_registry = Path(hachimi_dir) / 'ru_translator/data/ru_comics.json'
    russian_comics = json.loads(comic_registry.read_text(encoding='utf-8'))['files'] if comic_registry.is_file() else {}

    def skip(reason, size):
        n, b = skipped.get(reason, (0, 0))
        skipped[reason] = (n + 1, b + size)

    for dirpath, dirs, names in os.walk(ld):
        rel_dir = Path(dirpath).relative_to(ld).as_posix()
        dirs[:] = [d for d in dirs if not (rel_dir == "." and d in SKIP_DIRS)]
        nameset = set(names)
        for name in names:
            full = Path(dirpath) / name
            try:
                size = full.stat().st_size
            except OSError:
                continue
            low = name.lower()
            if rel_dir == "." and name in SKIP_DIRS:   # includes_android — файл шрифтов только для Android
                skip("файлы для Android", size)
            elif low in SKIP_NAMES or low.endswith(SKIP_SUFFIXES):
                skip("временные файлы и журналы", size)
            elif low.endswith(".srchash"):
                skip("кэш картинок (.srchash)", size)
            elif is_cache_png(full, nameset):
                skip("кэш картинок (X.png)", size)
            else:
                rel = (Path(LD_NAME) / Path(dirpath).relative_to(ld) / name).as_posix()
                comic_proof = russian_comics.get(full.relative_to(ld).as_posix())
                if comic_proof:
                    with full.open('rb') as stream:
                        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if actual != comic_proof['sha256']:
                        raise PackError('Русский комикс изменён или заменён старым пакетом: ' + rel)
                if lite and is_lite_skip(rel) and not comic_proof:
                    skip("комиксы и ролик (облегчённый вариант)", size)
                    continue
                files.append((rel, full, size))
    files.sort()
    return files, skipped


def friend_config(hachimi_dir):
    try:
        base = json.loads((Path(hachimi_dir) / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        base = {}
    if not isinstance(base, dict):
        base = {}
    base.update(FRIEND_CONFIG)
    return base


def scan_secrets(files):
    """Ключ Anthropic в пакете недопустим. Проверяем текстовые файлы; возвращает список найденного (пустой — чисто)."""
    bad = []
    for rel, full, size in files:
        low = rel.lower()
        if low.endswith(TEXT_SUFFIXES) or size < 1_000_000 and low.endswith((".md", ".html")):
            try:
                data = Path(full).read_bytes()
            except OSError:
                continue
            if SECRET_RE.search(data):
                bad.append(rel)
        if Path(rel).name.lower() == "settings.json":
            bad.append(rel + " (файл настроек программы перевода)")
    return bad


def human_mb(n):
    return f"{n / 1048576:.0f} МБ"


def installer_path():
    c = care_dir()
    p = c / "hachimi_installer" / "hachimi_installer.exe" if c else None
    return p if p and p.is_file() else None


def info(hachimi_dir):
    files, skipped = collect(hachimi_dir)
    total = sum(s for _r, _f, s in files)
    c = care_dir()
    app = (c / "release" / "dist" / "UmaRU") if c else None
    return {
        "files": len(files), "size_mb": round(total / 1048576, 1),
        "skipped": "; ".join(f"{k}: {n} ({human_mb(b)})" for k, (n, b) in skipped.items()) or "нет",
        "version": time.strftime("%Y.%m.%d-%H%M"),
        "app_ready": "да" if app and (app / "UmaRU.exe").is_file() else "нет (запустите tools\\build_player.cmd)",
        "installer_ready": "да" if installer_path() else "нет",
        "out_dir": str(c / "release" / "out") if c else "?",
    }


def build_pack(hachimi_dir, work_dir, progress=None, lite=False):
    """pack.zip + pack.json в work_dir. -> (путь к pack.json, манифест)."""
    require_server_safe(Path(hachimi_dir) / LD_NAME)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    emit_p = progress or (lambda f, t: None)
    files, skipped = collect(hachimi_dir, lite)
    if not files:
        raise PackError("В папке перевода нет файлов.")
    emit_p(0.01, f"Проверяю {len(files)} файлов на ключи и лишнее…")
    bad = scan_secrets(files)
    if bad:
        raise PackError("В пакете нашёлся ключ API или файл настроек, упаковка остановлена: " + ", ".join(bad[:5]))
    total = sum(s for _r, _f, s in files)
    entries, done = {}, 0
    zip_path = work_dir / "pack.zip"
    tmp = work_dir / "pack.zip.part"
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
        for n, (rel, full, size) in enumerate(files, 1):
            h = hashlib.sha256()
            ctype = zipfile.ZIP_DEFLATED if rel.lower().endswith(DEFLATE_SUFFIXES) else zipfile.ZIP_STORED
            zi = zipfile.ZipInfo(rel, date_time=time.localtime(Path(full).stat().st_mtime)[:6])
            zi.compress_type = ctype
            if ctype == zipfile.ZIP_DEFLATED:
                zi._compresslevel = 6
            with open(full, "rb") as src, z.open(zi, "w", force_zip64=size > 2**31) as dst:
                while chunk := src.read(1 << 20):
                    h.update(chunk)
                    dst.write(chunk)
            entries[rel] = [size, h.hexdigest()]
            done += size
            if n % 25 == 0 or n == len(files):
                emit_p(0.02 + 0.88 * done / max(1, total), f"Упаковываю перевод: {n} из {len(files)} файлов")
    os.replace(tmp, zip_path)
    emit_p(0.91, "Проверяю архив…")
    with zipfile.ZipFile(zip_path) as z:
        bad_member = z.testzip()
        names = set(z.namelist())
    if bad_member or names != set(entries):
        raise PackError("Архив pack.zip не прошёл проверку (повреждён или неполный).")
    require_server_safe(zip_path)
    manifest = {
        "format": 1, "version": time.strftime("%Y.%m.%d-%H%M"), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hachimi": "0.33.1", "zip": "pack.zip", "files": len(entries), "bytes": total,
        "zip_bytes": zip_path.stat().st_size, "entries": entries,
        "config_template": friend_config(hachimi_dir),
        "skipped": {k: list(v) for k, v in skipped.items()},
    }
    (work_dir / "pack.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return work_dir / "pack.json", manifest


def write_installer_note(dest, installer):
    h = hashlib.sha256(installer.read_bytes()).hexdigest()
    dest.write_text(
        "Установщик Hachimi Edge (v0.33.1), как он выложен автором.\n"
        "Источник: https://github.com/kairusds/Hachimi-Edge/releases/tag/v0.33.1 (файл hachimi_installer.exe)\n"
        f"SHA-256: {h}\n"
        "Hachimi Edge — открытая программа (GPL-3.0); русификатор только раскладывает рядом с ним готовый перевод.\n"
        "Английские картинки интерфейса — из перевода UmaTL (https://github.com/UmaTL); русские надписи на кнопках нарисованы поверх.\n",
        encoding="utf-8")


def assemble_release(manifest, pack_dir, out_dir, progress=None, lite=False):
    """Один zip для отправки: папка UmaRU\\ с программой, data\\ (пакет и установщик) и инструкцией."""
    emit_p = progress or (lambda f, t: None)
    c = care_dir()
    app = (c / "release" / "dist" / "UmaRU") if c else None
    if not app or not (app / "UmaRU.exe").is_file():
        raise PackError("Нет собранной программы для друга. Запустите tools\\build_player.cmd (один раз), потом повторите.")
    installer = installer_path()
    if not installer:
        raise PackError("Нет установщика Hachimi (папка hachimi_installer рядом с папкой обслуживания).")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"UmaRU_Player_{manifest['version'][:10].replace('.', '-')}" + ("_lite" if lite else "")
    final = out_dir / f"{stem}.zip"
    n = 2
    while final.exists():
        final = out_dir / f"{stem}_{n}.zip"
        n += 1
    part = final.with_name(final.name + ".part")
    note = Path(pack_dir) / "hachimi_installer.txt"
    write_installer_note(note, installer)
    readme = HERE / README_PLAYER
    extra = []   # (путь на диске, путь в архиве)
    for p in sorted(app.rglob("*")):
        if p.is_file():
            extra.append((p, "UmaRU/" + p.relative_to(app).as_posix()))
    extra += [(Path(pack_dir) / "pack.zip", "UmaRU/data/pack.zip"), (Path(pack_dir) / "pack.json", "UmaRU/data/pack.json"),
              (installer, "UmaRU/data/hachimi_installer.exe"), (note, "UmaRU/data/hachimi_installer.txt")]
    if readme.is_file():
        extra.append((readme, "UmaRU/ПРОЧТИ МЕНЯ.txt"))
    total = sum(p.stat().st_size for p, _ in extra)
    done = 0
    with zipfile.ZipFile(part, "w", allowZip64=True) as z:
        for i, (src, arc) in enumerate(extra, 1):
            stored = arc.endswith(("pack.zip",)) or src.suffix.lower() in (".pyd", ".dll") and src.stat().st_size > 5_000_000
            zi = zipfile.ZipInfo(arc, date_time=time.localtime(src.stat().st_mtime)[:6])
            zi.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            with open(src, "rb") as f, z.open(zi, "w", force_zip64=src.stat().st_size > 2**31) as dst:
                while chunk := f.read(1 << 20):
                    dst.write(chunk)
                    done += len(chunk)
            if i % 10 == 0 or i == len(extra):
                emit_p(0.92 + 0.07 * done / max(1, total), f"Собираю выпуск: {i} из {len(extra)} файлов")
    os.replace(part, final)
    h = hashlib.sha256()   # контрольная сумма рядом: другу проще убедиться, что большой файл докачался целиком
    with open(final, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    final.with_name(final.name + ".sha256.txt").write_text(f"{h.hexdigest()} *{final.name}\n", encoding="utf-8")
    releases = sorted(out_dir.glob("UmaRU_Player_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in releases[KEEP_RELEASES:]:
        old.unlink(missing_ok=True)
        old.with_name(old.name + ".sha256.txt").unlink(missing_ok=True)
    return final


def build_release(hachimi_dir=None, out_dir=None, release=True, progress=None, lite=False):
    """Весь путь: пакет -> (по желанию) zip для отправки. -> путь к zip (или к папке с пакетом без --release)."""
    hachimi_dir = Path(hachimi_dir) if hachimi_dir else Path(os.environ.get("UMA_RU_GAME_DIR") or ".") / "hachimi"
    c = care_dir()
    out_dir = Path(out_dir or ((c / "release" / "out") if c else HERE / "player_out"))
    work = out_dir / "_pack"
    manifest_path, manifest = build_pack(hachimi_dir, work, progress, lite)
    if not release:
        return work
    final = assemble_release(manifest, work, out_dir, progress, lite)
    shutil.rmtree(work, ignore_errors=True)
    if progress:
        progress(1.0, "Готово")
    return final


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info")
    p_info.add_argument("--hachimi-dir", default=str(Path(os.environ.get("UMA_RU_GAME_DIR") or ".") / "hachimi"))
    p_build = sub.add_parser("build")
    p_build.add_argument("--hachimi-dir", default=str(Path(os.environ.get("UMA_RU_GAME_DIR") or ".") / "hachimi"))
    p_build.add_argument("--out", default="")
    p_build.add_argument("--no-release", action="store_true", help="только pack.zip и pack.json, без zip для отправки")
    p_build.add_argument("--lite", action="store_true", help="облегчённый вариант: без английских комиксов и ролика (~140 МБ меньше)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "info":
            for k, v in info(args.hachimi_dir).items():
                print(f"{k}: {v}")
            return 0
        res = build_release(args.hachimi_dir, args.out or None, release=not args.no_release, lite=args.lite,
                            progress=lambda f, t: emit("PROGRESS", int(f * 100), t))
        size = (res.stat().st_size / 1048576) if res.is_file() else sum(p.stat().st_size for p in res.rglob("*") if p.is_file()) / 1048576
        emit("DONE", res, f"{size:.0f}")
        return 0
    except PackError as e:
        emit("ERROR", e)
        return 1
    except Exception as e:  # noqa: BLE001 — пишем причину строкой, окно владельца её покажет
        emit("ERROR", f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

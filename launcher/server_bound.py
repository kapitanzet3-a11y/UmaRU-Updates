"""Offline regression check for server-bound localize overrides (error 205)."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path
from zipfile import ZipFile, BadZipFile


GAME = Path(os.environ.get("UMA_RU_GAME_DIR") or ".")
EN_BASE = GAME / "hachimi/ru_translator/data/en_dicts/localize_dict.json"
NATIVE_DUMP = GAME / "hachimi/localize_dump.json"
NATIVE_FALLBACK = ("SingleMode418033", "Common0134")
UMATL_DEFAULTS = ("Character701018", "RoomMatch0023", "RoomMatch0024")
DEFAULT_NAME_RE = re.compile(r"(?:プラン|マイローテ|セット|編成)\{\d\}")
CYRILLIC_RE = re.compile(r"[\u0400-\u052f]")


class GuardError(Exception):
    pass


def read_json(data: bytes, description: str) -> dict:
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise GuardError(f"invalid JSON in {description}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardError(f"expected JSON object in {description}")
    return value


def select_path(names: list[str]) -> str:
    matches = [name for name in names if name.replace("\\", "/").split("/")[-1] == "localize_dict.json"]
    preferred = [name for name in matches if "/".join(name.replace("\\", "/").split("/")[-2:]) == "localized_data_ru/localize_dict.json"]
    if preferred:
        matches = preferred
    if len(matches) != 1:
        raise GuardError(f"expected one localize_dict.json, found {len(matches)}")
    return matches[0]


def load_target(target: Path) -> dict:
    if target.is_dir():
        # A direct candidate folder or a packaged tree are both supported.
        paths = [p for p in target.rglob("localize_dict.json") if p.is_file()]
        selected = select_path([str(p.relative_to(target)).replace("\\", "/") for p in paths])
        return read_json((target / selected).read_bytes(), selected)
    if target.is_file() and target.name.lower().endswith((".zip", ".zip.part")):
        try:
            with ZipFile(target) as archive:
                if "UmaRU/data/pack.zip" in archive.namelist():
                    if archive.namelist().count("UmaRU/data/pack.zip") != 1:
                        raise GuardError("duplicate nested pack.zip")
                    with ZipFile(io.BytesIO(archive.read("UmaRU/data/pack.zip"))) as inner:
                        selected = select_path(inner.namelist())
                        return read_json(inner.read(selected), selected)
                selected = select_path(archive.namelist())
                return read_json(archive.read(selected), selected)
        except BadZipFile as exc:
            raise GuardError(f"invalid zip: {exc}") from exc
    if target.is_file() and target.name == "localize_dict.json":
        return read_json(target.read_bytes(), str(target))
    raise GuardError("target must be a folder, pack.zip, or localize_dict.json")


def check(target: Path, en_base: Path = EN_BASE, native_dump: Path = NATIVE_DUMP) -> list[str]:
    actual = load_target(target)
    english = read_json(en_base.read_bytes(), str(en_base))
    native = read_json(native_dump.read_bytes(), str(native_dump))
    errors: list[str] = []
    for key in NATIVE_FALLBACK:
        if key in actual:
            errors.append(f"{key}: override present; native fallback requires absent key")
        if key not in native:
            errors.append(f"{key}: missing from native dump")
    for key in UMATL_DEFAULTS:
        if not isinstance(english.get(key), str):
            errors.append(f"{key}: missing UmaTL base value")
        elif actual.get(key) != english[key]:
            errors.append(f"{key}: differs from UmaTL base value")
    for key, jp in native.items():
        if isinstance(jp, str) and DEFAULT_NAME_RE.fullmatch(jp.strip()):
            value = actual.get(key)
            if isinstance(value, str) and CYRILLIC_RE.search(value):
                errors.append(f"{key}: Cyrillic in native template default")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="dictionary folder, pack.zip, or localize_dict.json")
    args = parser.parse_args(argv)
    try:
        errors = check(args.target)
    except (GuardError, OSError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors[:20]:
            print(f"FAIL: {error}", file=sys.stderr)
        if len(errors) > 20:
            print(f"FAIL: {len(errors) - 20} more violations", file=sys.stderr)
        return 1
    print("PASS: server-bound localize values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

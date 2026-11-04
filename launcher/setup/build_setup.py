"""Build the Uma RU launcher from public source with externally supplied Hachimi files.

Run on Windows with PyInstaller, sv-ttk and Inno Setup 6 already installed.
All generated files go to --build-dir, outside the source checkout.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent
CHECKOUT = LAUNCHER.parent
PLAYER = LAUNCHER / "player"
HACHIMI_SHA256 = "26f744a31cdad41e671205aaccfc65bb05d732dbad68fb8f3b671982b92a0628"


def read_version() -> str:
    source = (PLAYER / "_launcher_version.py").read_text(encoding="utf-8")
    assignments = [n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                   and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                   and n.targets[0].id == "APP_VERSION"]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant):
        raise RuntimeError("_launcher_version.py must define APP_VERSION as major.minor")
    version = assignments[0].value.value
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise RuntimeError("APP_VERSION must be major.minor")
    return version


def make_icon(path: Path) -> None:
    # ICO container with PNG images; Windows, PyInstaller and Inno accept it.
    sys.path.insert(0, str(LAUNCHER))
    from ui_icon import ICONS
    images = [base64.b64decode(ICONS[size]) for size in sorted(ICONS) if size <= 256]
    if not images:
        raise RuntimeError("ui_icon.ICONS has no images")
    import struct
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = []
    for size, image in zip(sorted(size for size in ICONS if size <= 256), images):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    path.write_bytes(header + b"".join(entries) + b"".join(images))


def write_version(path: Path, version: str) -> None:
    major, minor = map(int, version.split("."))
    path.write_text(f"""VSVersionInfo(
 ffi=FixedFileInfo(filevers=({major}, {minor}, 0, 0), prodvers=({major}, {minor}, 0, 0), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
 kids=[StringFileInfo([StringTable('041904B0', [
  StringStruct('CompanyName', 'Uma RU'),
  StringStruct('FileDescription', 'Лаунчер Uma RU — установка и запуск русского перевода Umamusume'),
  StringStruct('FileVersion', '{version}.0.0'),
  StringStruct('InternalName', 'UmaRU'),
  StringStruct('OriginalFilename', 'UmaRU.exe'),
  StringStruct('ProductName', 'Лаунчер Uma RU'),
  StringStruct('ProductVersion', '{version}.0.0')])]), VarFileInfo([VarStruct('Translation', [1049, 1200])])])
""", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--hachimi-installer", type=Path, required=True)
    parser.add_argument("--hachimi-provenance", type=Path, required=True)
    parser.add_argument("--iscc", type=Path, required=True)
    parser.add_argument("--icon", type=Path, help="Optional .ico; otherwise generated from ui_icon.ICONS")
    args = parser.parse_args()
    build = args.build_dir.resolve()
    if build == CHECKOUT.resolve() or CHECKOUT.resolve() in build.parents:
        parser.error("--build-dir must be outside the source checkout")
    for path in [args.hachimi_installer, args.hachimi_provenance, args.iscc,
                 PLAYER / "umaru_app.py", PLAYER / "umaru_core.py", PLAYER / "umaru_updates.py",
                 PLAYER / "umaru_launcher.py", PLAYER / "_launcher_version.py", LAUNCHER / "ui_icon.py"]:
        if not path.is_file():
            raise FileNotFoundError(path)
    hachimi = args.hachimi_installer.read_bytes()
    if hashlib.sha256(hachimi).hexdigest() != HACHIMI_SHA256:
        raise RuntimeError("Hachimi installer SHA-256 differs from pinned trusted release")
    provenance = args.hachimi_provenance.read_text(encoding="utf-8-sig")
    if HACHIMI_SHA256 not in provenance.lower():
        raise RuntimeError("Hachimi provenance must document the pinned SHA-256")
    if args.icon and (not args.icon.is_file() or args.icon.suffix.lower() != ".ico"):
        parser.error("--icon must point to an existing .ico")
    build.mkdir(parents=True, exist_ok=True)
    icon = build / "UmaRU.ico"
    if args.icon:
        shutil.copyfile(args.icon, icon)
    else:
        make_icon(icon)
    version = read_version()
    version_file = build / "version_info.txt"
    write_version(version_file, version)
    env = os.environ.copy()
    env.update(UMARU_SOURCE_ROOT=str(LAUNCHER), UMARU_VERSION_FILE=str(version_file),
               UMARU_ICON_FILE=str(icon), UMARU_APP_VERSION=version,
               UMARU_SETUP_ROOT=str(build))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(build / "stage"), "--workpath", str(build / "pyi-work"),
                    str(HERE / "pyi-spec" / "UmaRU.spec")], env=env, check=True)
    data = build / "stage" / "UmaRU" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "hachimi_installer.exe").write_bytes(hachimi)
    shutil.copyfile(args.hachimi_provenance, data / "hachimi_installer.txt")
    if any((data / name).exists() for name in ("pack.json", "pack.zip")):
        raise RuntimeError("A translation pack leaked into the thin installer")
    subprocess.run([str(args.iscc), str(HERE / "UmaRU.iss")], env=env, check=True)
    result = build / "output" / "UmaRU-Setup.exe"
    print(json.dumps({"installer": str(result), "bytes": result.stat().st_size,
                      "sha256": hashlib.sha256(result.read_bytes()).hexdigest()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

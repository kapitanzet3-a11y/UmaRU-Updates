"""Isolated 1.1-to-current installer smoke; never touches the real installation."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import build_setup


HERE = Path(__file__).resolve().parent
TEST_ID = "{{1537653A-FE7F-4986-A01E-2A9E7A4271D0}"


def prepare_script(smoke: Path) -> Path:
    source = (HERE / "UmaRU.iss").read_text(encoding="utf-8-sig")
    source = source.replace("AppId={{A54CA8D0-92CF-491B-BDB5-280DBA0E91D4}", f"AppId={TEST_ID}")
    source = source.replace("PrivilegesRequired=lowest", "PrivilegesRequired=lowest\nCreateUninstallRegKey=no\nUninstallable=no")
    source = source.replace("UsePreviousAppDir=yes", "UsePreviousAppDir=no")
    source = source.replace("OutputDir={#SetupRoot}\\output", "OutputDir={#SetupRoot}\\smoke\\output")
    source = re.sub(r"\n\[Icons\]\n.*?(?=\n\[Tasks\])", "\n", source, flags=re.S)
    source = re.sub(r"\n\[Run\]\n.*?(?=\n\[UninstallDelete\])", "\n", source, flags=re.S)
    if "AppId=" + TEST_ID not in source or "[Icons]" in source or "[Run]" in source:
        raise RuntimeError("Unsafe smoke script transformation")
    path = smoke / "UmaRU-upgrade-test.iss"
    path.write_text(source, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-stage", type=Path, required=True, help="External frozen 1.1 stage")
    parser.add_argument("--build-dir", type=Path, required=True, help="Output from build_setup.py")
    parser.add_argument("--iscc", type=Path, required=True)
    args = parser.parse_args()
    old_stage = args.old_stage.resolve()
    build = args.build_dir.resolve()
    if build == build_setup.CHECKOUT.resolve() or build_setup.CHECKOUT.resolve() in build.parents:
        parser.error("--build-dir must be outside the source checkout")
    smoke = build / "smoke"
    smoke.mkdir(parents=True, exist_ok=True)
    target = smoke / "installed"
    if not old_stage.is_dir() or not (build / "stage" / "UmaRU" / "UmaRU.exe").is_file():
        raise RuntimeError("Both frozen 1.1 and newly built stage folders are required")
    if target.exists():
        resolved = target.resolve()
        if smoke.resolve() not in resolved.parents:
            raise RuntimeError("Refusing to clear a target outside isolated smoke")
        shutil.rmtree(resolved)
    shutil.copytree(old_stage, target)
    data = target / "data"
    data.mkdir(exist_ok=True)
    channel = data / "update-channel.json"
    channel.write_text('{"schema":1,"variant":"lite","url":"https://example.invalid/channel.json"}\n', encoding="utf-8")
    cache = data / "cache" / "sentinel.txt"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("preserve me\n", encoding="utf-8")
    baseline = {"channel": hashlib.sha256(channel.read_bytes()).hexdigest(),
                "cache": hashlib.sha256(cache.read_bytes()).hexdigest(),
                "old_exe": hashlib.sha256((target / "UmaRU.exe").read_bytes()).hexdigest()}
    script = prepare_script(smoke)
    env = os.environ.copy()
    env["UMARU_SETUP_ROOT"] = str(build)
    env["UMARU_APP_VERSION"] = build_setup.read_version()
    env["UMARU_ICON_FILE"] = str(build / "UmaRU.ico")
    with (smoke / "inno-compile.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(args.iscc), str(script)], env=env, check=True, stdout=log)
    installer = smoke / "output" / "UmaRU-Setup.exe"
    subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UMARUUPDATE",
                    "/NOICONS", f"/DIR={target}", f"/LOG={smoke / 'inno-install.log'}"],
                   check=True, timeout=120)
    after = {"channel": hashlib.sha256(channel.read_bytes()).hexdigest(),
             "cache": hashlib.sha256(cache.read_bytes()).hexdigest(),
             "new_exe": hashlib.sha256((target / "UmaRU.exe").read_bytes()).hexdigest()}
    assert after["channel"] == baseline["channel"]
    assert after["cache"] == baseline["cache"]
    assert after["new_exe"] != baseline["old_exe"]
    assert after["new_exe"] == hashlib.sha256((build / "stage" / "UmaRU" / "UmaRU.exe").read_bytes()).hexdigest()
    (smoke / "result.json").write_text(json.dumps({"version": env["UMARU_APP_VERSION"],
        "baseline": baseline, "after": after, "installer": str(installer), "target": str(target)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("Isolated upgrade smoke passed:", smoke / "result.json")


if __name__ == "__main__":
    main()

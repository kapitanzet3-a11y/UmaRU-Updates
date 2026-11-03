"""Opt-in read-only check of the signed public 1.2 launcher release.

Downloads a verified installer into disposable scratch space; never runs it.
The expected signed manifest and optional translation channel are explicit inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

PLAYER = Path(__file__).resolve().parents[2] / "player"
sys.path.insert(0, str(PLAYER))
import umaru_launcher as launcher
from umaru_updates import _download


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="Approved signed launcher.json from this release")
    parser.add_argument("--scratch", type=Path, required=True,
                        help="Existing writable directory for disposable verified download")
    parser.add_argument("--expected-version", default="1.2")
    parser.add_argument("--previous-version", default="1.1")
    parser.add_argument("--channel", type=Path,
                        help="Approved channel.json to compare with the public latest asset")
    args = parser.parse_args()
    if not args.manifest.is_file() or not args.scratch.is_dir():
        parser.error("--manifest must be a file and --scratch an existing directory")
    if args.channel is not None and not args.channel.is_file():
        parser.error("--channel must be a file")

    expected = launcher.parse_manifest(args.manifest.read_bytes())
    assert expected.version == args.expected_version, expected.version
    offered = launcher.check(args.previous_version)
    assert offered == expected, "Public signed manifest differs from approved manifest"
    assert launcher.check(args.expected_version) is None, "Current version still offered as newer"
    with tempfile.TemporaryDirectory(prefix="umaru-public-", dir=args.scratch) as raw:
        fetched = launcher.download(offered, raw)
        assert launcher.verify_installer(offered, fetched) == fetched
        assert fetched.stat().st_size == expected.size
        assert sha(fetched) == expected.sha256

    result = {"pass": True, "version": expected.version, "setup_sha256": expected.sha256,
              "bytes": expected.size, "previous_version_offered": True,
              "current_version_offered": False}
    if args.channel is not None:
        url = f"https://github.com/{launcher.REPO}/releases/latest/download/channel.json"
        public = _download(url, url, 16 * 1024)
        assert public == args.channel.read_bytes(), "Public channel changed from approved input"
        result["channel_sha256"] = hashlib.sha256(public).hexdigest()
        result["translation_tag"] = json.loads(public)["tag"]
    print(json.dumps(result))


if __name__ == "__main__":
    main()

"""Opt-in packaged-player upgrade check using a real previous pack and candidate channel.

All writes go to a fresh child of --scratch. The supplied EXE is run only with
an isolated synthetic game; this script never launches the game itself.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import struct
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def inside(base: Path, relative: str) -> Path:
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ValueError(f"Archive path escapes the isolated game: {relative}")
    return target


def old_pack(archive: Path, game: Path, variant: str) -> dict:
    base = game / "hachimi"
    base.mkdir(parents=True)
    with zipfile.ZipFile(archive) as outer:
        names = [n for n in outer.namelist() if n.endswith("/data/pack.json")]
        if len(names) != 1:
            raise ValueError("Expected one data/pack.json in old archive")
        prefix = names[0].removesuffix("pack.json")
        manifest = json.loads(outer.read(names[0]))
        with tempfile.TemporaryFile(dir=game.parent) as nested:
            with outer.open(prefix + "pack.zip") as src:
                shutil.copyfileobj(src, nested)
            nested.seek(0)
            with zipfile.ZipFile(nested) as pack:
                if set(pack.namelist()) != set(manifest["entries"]):
                    raise ValueError("Old archive members do not match manifest")
                for rel, (size, digest) in manifest["entries"].items():
                    target = inside(base, rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with pack.open(rel) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if target.stat().st_size != size or sha(target) != digest:
                        raise ValueError(f"Old archive hash mismatch: {rel}")
    (base / "uma_ru_player.json").write_text(json.dumps({
        "pack_version": manifest["version"],
        "entries": {rel: spec[1] for rel, spec in manifest["entries"].items()},
        "config_ours": True,
    }), encoding="utf-8")
    (base / "config.json").write_text(json.dumps({
        "localized_data_dir": "localized_data_ru", "translator_mode": False,
        "auto_translate_stories": False, "auto_translate_localize": False,
        "disable_translations": False, "tl_auto_updater_mode": "Disabled",
        "skip_first_time_setup": True, "user_preserved": "fixture",
    }), encoding="utf-8")
    return manifest


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        name = self.path.removeprefix("/")
        if name == "channel.json":
            body = self.server.descriptor
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if name not in self.server.allowed:
            self.send_error(404)
            return
        path = self.server.channel / name
        self.send_response(200)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as src:
            shutil.copyfileobj(src, self.wfile)

    def log_message(self, *_):
        pass


def run(exe: Path, label: str, game: Path, data: Path, home: Path, *args: str) -> dict:
    output = home.parent / f"{label}.json"
    env = dict(os.environ, UMA_RU_HOME=str(home), UMA_STEAM_ROOT=str(game.parents[2]),
               UMA_RU_FAKE_RUNNING="0", UMA_RU_NO_LAUNCH="1")
    proc = subprocess.run([str(exe), *args, "--game-dir", str(game), "--pack-dir", str(data),
                           "--json", str(output)], env=env, capture_output=True, timeout=600)
    return {"exit": proc.returncode,
            "result": json.loads(output.read_text(encoding="utf-8")) if output.exists() else None,
            "stderr": proc.stderr.decode("utf-8", errors="replace")[-1000:]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-archive", type=Path, required=True)
    parser.add_argument("--exe", type=Path, required=True, help="Candidate packaged UmaRU.exe")
    parser.add_argument("--channel-dir", type=Path, required=True,
                        help="Approved channel.json, manifests and pack/delta ZIPs")
    parser.add_argument("--scratch", type=Path, required=True,
                        help="Existing writable directory for disposable isolated data")
    parser.add_argument("--variant", choices=("full", "lite"), default="full")
    parser.add_argument("--require-delta", action="store_true")
    args = parser.parse_args()
    for path in (args.old_archive, args.exe, args.channel_dir / "channel.json"):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    if not args.scratch.is_dir():
        parser.error("--scratch must be an existing directory")
    descriptor = json.loads((args.channel_dir / "channel.json").read_text(encoding="utf-8"))
    variant = descriptor["variants"][args.variant]
    assets = [variant["manifest"], variant["pack"], *variant["deltas"]]
    allowed = set()
    for asset in assets:
        name = Path(urlsplit(asset["url"]).path).name
        if not name or name in allowed or not (args.channel_dir / name).is_file():
            parser.error(f"Missing or duplicate channel asset: {name}")
        if sha(args.channel_dir / name) != asset["sha256"]:
            parser.error(f"Channel asset hash mismatch: {name}")
        allowed.add(name)
    manifest_name = Path(urlsplit(variant["manifest"]["url"]).path).name
    new = json.loads((args.channel_dir / manifest_name).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="umaru-acceptance-", dir=args.scratch) as raw:
        root = Path(raw)
        game = root / "fake-steam/steamapps/common/UmamusumePrettyDerby_Jpn"
        game.mkdir(parents=True)
        home, data = root / "home", root / "data"
        home.mkdir()
        data.mkdir()
        pe = bytearray(512)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 0x3C, 128)
        pe[128:132] = b"PE\0\0"
        struct.pack_into("<H", pe, 134, 1)
        pe[152:160] = b".text\0\0\0"
        (game / "UmamusumePrettyDerby_Jpn.exe").write_bytes(pe)
        with (game / "cri_mana_vpx.dll").open("wb") as dll:
            dll.write(b"hachimi::core")
            dll.truncate(8_000_001)
        old = old_pack(args.old_archive, game, args.variant)
        server = Server(("127.0.0.1", 0), Handler)
        server.requests, server.allowed = [], allowed
        server.channel = args.channel_dir
        base_url = f"http://127.0.0.1:{server.server_port}"
        local = json.loads(json.dumps(descriptor))
        for item in [local["variants"][args.variant]["manifest"],
                     local["variants"][args.variant]["pack"],
                     *local["variants"][args.variant]["deltas"]]:
            item["url"] = base_url + "/" + Path(urlsplit(item["url"]).path).name
        server.descriptor = json.dumps(local).encode("utf-8")
        (data / "update-channel.json").write_text(json.dumps({
            "schema": 1, "url": base_url + "/channel.json", "variant": args.variant,
        }), encoding="utf-8")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            first = run(args.exe, "install", game, data, home, "--install")
            assert first["exit"] == 0 and first["result"]["mode_after"] == "ok", first
            delta = next((d for d in variant["deltas"] if d["from_version"] == old["version"]), None)
            if args.require_delta:
                assert delta is not None, "No delta from previous archive version"
                delta_name = Path(urlsplit(delta["url"]).path).name
                assert "/" + delta_name in server.requests, server.requests
                assert "/" + Path(urlsplit(variant["pack"]["url"]).path).name not in server.requests
            state = json.loads((game / "hachimi/uma_ru_player.json").read_text(encoding="utf-8"))
            assert state["pack_version"] == new["version"]
            assert state["entries"] == {rel: spec[1] for rel, spec in new["entries"].items()}
            for rel, (size, digest) in new["entries"].items():
                target = inside(game / "hachimi", rel)
                assert target.stat().st_size == size and sha(target) == digest, rel
            cfg = json.loads((game / "hachimi/config.json").read_text(encoding="utf-8"))
            assert cfg["user_preserved"] == "fixture"
            server.requests.clear()
            current = run(args.exe, "current", game, data, home, "--update-cache")
            assert current["exit"] == 0 and current["result"]["update_state"] == "current", current
            assert server.requests == ["/channel.json"], server.requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        offline = run(args.exe, "offline", game, data, home, "--update-cache")
        assert offline["result"]["update_state"] == "offline", offline
        assert offline["result"]["pack"] == new["version"], offline
        print(json.dumps({"pass": True, "previous": old["version"], "installed": new["version"],
                          "verified_files": len(new["entries"]), "delta_required": args.require_delta,
                          "current": True, "offline_cache": True}))


if __name__ == "__main__":
    main()

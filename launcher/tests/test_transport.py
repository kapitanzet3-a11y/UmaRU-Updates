"""Loopback integration checks for the standalone player updater."""
import hashlib
import http.server
import http.client
import io
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "player"))
import umaru_core as core
import umaru_updates as updates


def digest(data):
    return hashlib.sha256(data).hexdigest()


def zip_bytes(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def manifest(version, files):
    entries = {n: [len(b), digest(b)] for n, b in files.items()}
    return {"format": 1, "version": version, "zip": "pack.zip", "files": len(entries),
            "bytes": sum(len(b) for b in files.values()), "entries": entries}


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(self.path)
        body = self.server.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class TransportTests(unittest.TestCase):
    def setUp(self):
        # Some sandboxes allow bind() but deny even a local TCP connect().
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            try:
                with socket.create_connection(listener.getsockname(), timeout=1):
                    pass
            except OSError as exc:
                if getattr(exc, "winerror", None) == 10013:
                    self.skipTest("Windows sandbox denies loopback TCP (WinError 10013)")
                raise
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.old_home = os.environ.get("UMA_RU_HOME")
        os.environ["UMA_RU_HOME"] = str(self.root / "home")
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.files = {}
        self.server.hits = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        (self.data / "update-channel.json").write_text(json.dumps({"schema": 1, "url": self.url + "/channel.json",
                                                                   "variant": "full"}), encoding="utf-8")
        self.channel = updates.read_channel(self.data)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.old_home is None:
            os.environ.pop("UMA_RU_HOME", None)
        else:
            os.environ["UMA_RU_HOME"] = self.old_home
        self.tmp.cleanup()

    def publish(self, version, files, *, delta=None, corrupt=False):
        m = manifest(version, files)
        mb = json.dumps(m).encode()
        zb = zip_bytes(files)
        self.server.files["/manifest.json"] = mb
        self.server.files["/pack.zip"] = zb
        variant = {"manifest": self.asset("/manifest.json", mb),
                   "pack": self.asset("/pack.zip", zb), "deltas": []}
        if delta:
            base_version, base_files = delta
            patch = {k: v for k, v in files.items() if base_files.get(k) != v}
            pb = zip_bytes(patch)
            self.server.files["/delta.zip"] = pb
            d = self.asset("/delta.zip", pb)
            d.update(from_version=base_version,
                     from_entries_sha256=updates._entries_digest(manifest(base_version, base_files)["entries"]))
            variant["deltas"] = [d]
        if corrupt:
            variant["pack"]["sha256"] = "0" * 64
        self.server.files["/channel.json"] = json.dumps({"schema": 1, "tag": "r5.5", "version": version,
                                                          "variants": {"full": variant}}).encode()
        return m

    def asset(self, path, body):
        return {"url": self.url + path, "size": len(body), "sha256": digest(body)}

    def test_full_then_current_then_incremental_with_removal(self):
        a = "localized_data_ru/a.json"
        b = "localized_data_ru/b.png"
        c = "localized_data_ru/c.usm"
        old = {a: b"old", b: b"same"}
        new = {a: b"new", b: b"same", c: b"video"}
        self.publish("2026.11.03-1200", old)
        first = updates.resolve(self.data, self.channel)
        self.assertEqual(first.state, "updated")
        self.assertEqual(updates.resolve(self.data, self.channel).state, "current")
        self.publish("2026.11.03-1201", new, delta=("2026.11.03-1200", old))
        self.server.hits.clear()
        result = updates.resolve(self.data, self.channel)
        self.assertEqual(result.state, "updated")
        self.assertIn("/delta.zip", self.server.hits)
        self.assertNotIn("/pack.zip", self.server.hits)
        self.assertEqual(result.pack.entries, manifest("2026.11.03-1201", new)["entries"])
        self.publish("2026.11.03-1202", {c: b"video"}, delta=("2026.11.03-1201", new))
        self.assertEqual(set(updates.resolve(self.data, self.channel).pack.entries), {c})

    def test_corrupt_remote_retains_last_good_and_offline(self):
        old = {"localized_data_ru/a.json": b"old"}
        self.publish("2026.11.03-1200", old)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        self.publish("2026.11.03-1201", {"localized_data_ru/a.json": b"new"}, corrupt=True)
        failed = updates.resolve(self.data, self.channel)
        self.assertEqual(failed.state, "error")
        self.assertEqual(failed.pack.version, "2026.11.03-1200")
        self.server.shutdown()
        offline = updates.resolve(self.data, self.channel)
        self.assertEqual(offline.state, "offline")
        self.assertEqual(offline.pack.version, "2026.11.03-1200")

    def test_unsafe_remote_manifest_fails_closed(self):
        self.publish("2026.11.03-1200", {"localized_data_ru/../evil.json": b"bad"})
        self.assertEqual(updates.resolve(self.data, self.channel).state, "error")
        self.assertIsNone(updates.cached_pack(self.data, self.channel))

    def test_bad_delta_falls_back_to_full(self):
        old = {"localized_data_ru/a.json": b"old", "localized_data_ru/b.usm": b"x" * 1000}
        new = {"localized_data_ru/a.json": b"new", "localized_data_ru/b.usm": b"x" * 1000}
        self.publish("2026.11.03-1200", old)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        self.publish("2026.11.03-1201", new, delta=("2026.11.03-1200", old))
        self.server.files["/delta.zip"] = b"x"
        self.server.hits.clear()
        result = updates.resolve(self.data, self.channel)
        self.assertEqual(result.state, "updated")
        self.assertIn("/delta.zip", self.server.hits)
        self.assertIn("/pack.zip", self.server.hits)

    def test_thin_install_bootstraps_from_verified_receipt(self):
        files = {"localized_data_ru/a.json": b"hello"}
        m = self.publish("2026.11.03-1200", files)
        game = self.root / "game"
        target = game / "hachimi" / "localized_data_ru" / "a.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"hello")
        core.save_state(game, {"pack_version": m["version"], "entries": {k: digest(v) for k, v in files.items()}})
        self.server.hits.clear()
        result = updates.resolve(self.data, self.channel, game_dir=game)
        self.assertEqual(result.state, "updated")
        self.assertNotIn("/pack.zip", self.server.hits)
        self.assertEqual(result.pack.entries, m["entries"])

    def test_current_reads_descriptor_only_and_does_not_rehash_zip(self):
        files = {"localized_data_ru/a.json": b"stable"}
        self.publish("2026.11.03-1200", files)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        self.server.hits.clear()
        with patch.object(updates, "_zip_members", side_effect=AssertionError("full rehash")):
            result = updates.resolve(self.data, self.channel)
        self.assertEqual(result.state, "current")
        self.assertEqual(self.server.hits, ["/channel.json"])

    def test_damaged_same_version_cache_reacquired(self):
        files = {"localized_data_ru/a.json": b"repair me"}
        self.publish("2026.11.03-1200", files)
        first = updates.resolve(self.data, self.channel)
        self.assertEqual(first.state, "updated")
        first.pack.zip_path.write_bytes(b"broken")
        self.server.hits.clear()
        fixed = updates.resolve(self.data, self.channel)
        self.assertEqual(fixed.state, "updated")
        self.assertIn("/pack.zip", self.server.hits)
        self.assertEqual(updates.cached_pack(self.data, self.channel).version, "2026.11.03-1200")

    def test_same_entries_newer_version_reuses_cache(self):
        files = {"localized_data_ru/a.json": b"same"}
        self.publish("2026.11.03-1200", files)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        self.publish("2026.11.03-1201", files)
        self.server.hits.clear()
        newer = updates.resolve(self.data, self.channel)
        self.assertEqual(newer.state, "updated")
        self.assertEqual(newer.pack.version, "2026.11.03-1201")
        self.assertNotIn("/pack.zip", self.server.hits)

    def test_malformed_variants_preserves_previous_pack(self):
        files = {"localized_data_ru/a.json": b"old"}
        self.publish("2026.11.03-1200", files)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        self.server.files["/channel.json"] = json.dumps({"schema": 1, "tag": "bad",
                                                          "version": "2026.11.03-1201", "variants": []}).encode()
        result = updates.resolve(self.data, self.channel)
        self.assertEqual(result.state, "error")
        self.assertEqual(result.pack.version, "2026.11.03-1200")

    def test_http_interruption_preserves_previous_pack(self):
        old = {"localized_data_ru/a.json": b"old", "localized_data_ru/b.usm": b"x" * 1000}
        new = {"localized_data_ru/a.json": b"new", "localized_data_ru/b.usm": b"x" * 1000}
        self.publish("2026.11.03-1200", old)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        self.publish("2026.11.03-1201", new, delta=("2026.11.03-1200", old), corrupt=True)
        original = updates._download
        def interrupted(url, *args, **kwargs):
            if url.endswith("/delta.zip"):
                raise http.client.IncompleteRead(b"partial", 100)
            return original(url, *args, **kwargs)
        with patch.object(updates, "_download", side_effect=interrupted):
            result = updates.resolve(self.data, self.channel)
        self.assertEqual(result.state, "error")
        self.assertEqual(result.pack.version, "2026.11.03-1200")

    def test_newer_game_receipt_skips_remote_payload(self):
        files = {"localized_data_ru/a.json": b"old"}
        self.publish("2026.11.03-1200", files)
        self.assertEqual(updates.resolve(self.data, self.channel).state, "updated")
        game = self.root / "game"
        core.save_state(game, {"pack_version": "2026.11.03-1202", "entries": {}})
        self.server.hits.clear()
        result = updates.resolve(self.data, self.channel, game_dir=game)
        self.assertEqual(result.state, "current")
        self.assertEqual(self.server.hits, ["/channel.json"])


if __name__ == "__main__":
    unittest.main()

"""Offline publication checks; no GitHub authentication or network required."""
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import publish_online as pub


class Draft:
    def __init__(self):
        self.release = None
        self.calls = []
        self.fail_upload = False

    def remote(self, _repo, _tag):
        return self.release

    def gh(self, *args):
        self.calls.append(args)
        if args[:2] == ("release", "create"):
            self.release = {"id": 42, "draft": True, "body": args[-1], "assets": []}
        elif args[:2] == ("release", "upload"):
            if self.fail_upload:
                self.fail_upload = False
                raise pub.ReleaseError("simulated interrupted upload")
            path = pathlib.Path(args[3])
            self.release["assets"].append({"name": path.name, "size": path.stat().st_size,
                                           "digest": "sha256:" + pub.sha_file(path)})
        elif args[:2] == ("release", "edit"):
            self.release["draft"] = False
        else:
            raise AssertionError(args)
        return ""


def snapshot(channel=b'{"schema":1,"tag":"r5.5"}', launcher=None, setup=b"old setup"):
    return {"id": 12, "tag": "r5.5", "channel": channel, "channel_tag": "r5.5",
            "channel_sha256": pub.sha_bytes(channel), "launcher": launcher,
            "launcher_sha256": pub.sha_bytes(launcher) if launcher else "none",
            "setup_sha256": pub.sha_bytes(setup),
            "offer": SimpleNamespace(version="1.1", size=len(setup), sha256=pub.sha_bytes(setup)) if launcher else None}


class PublishTest(unittest.TestCase):
    def test_default_launcher_stages_are_versioned(self):
        one = pub.default_launcher_stage("1.2")
        two = pub.default_launcher_stage("1.3")
        self.assertEqual(one.name, "launcher-v1.2")
        self.assertEqual(two.name, "launcher-v1.3")
        self.assertEqual(one.parent, two.parent)
        self.assertNotEqual(one, two)

    def test_public_snapshot_checks_exact_assets_and_signed_setup(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as raw:
            files = {"channel.json": b'{"tag":"r5.5","schema":1}',
                     "launcher.json": b"signed", "UmaRU-Setup.exe": b"setup"}
            release = {"id": 12, "tag_name": "launcher-v1.1", "draft": False,
                       "assets": [{"name": n, "size": len(data), "digest": "sha256:" + pub.sha_bytes(data)}
                                  for n, data in files.items()]}
            def gh(*args):
                self.assertEqual(args, ("api", "repos/owner/repo/releases/latest"))
                return json.dumps(release)
            def open_(request, **_kwargs):
                return io.BytesIO(files[request.full_url.rsplit("/", 1)[-1]])
            offer = SimpleNamespace(version="1.1", size=5, sha256=pub.sha_bytes(b"setup"))
            target = pathlib.Path(raw) / "UmaRU-Setup.exe"
            with patch.object(pub, "gh", side_effect=gh), patch.object(pub, "urlopen", side_effect=open_), \
                 patch.object(pub, "signed_launcher", return_value=offer):
                result = pub.latest_snapshot("owner/repo", require_launcher=True, installer_target=target)
                self.assertEqual(result["channel"], files["channel.json"])
                self.assertEqual(target.read_bytes(), b"setup")
                pub.same_latest("owner/repo", result)
                release["id"] = 13
                with self.assertRaises(pub.ReleaseError):
                    pub.same_latest("owner/repo", result)

    def test_owner_command_uses_channel_tag_when_latest_is_launcher(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as raw:
            work = pathlib.Path(raw)
            receipt = work / "new-job" / "release-r56" / "published.json"
            receipt.parent.mkdir(parents=True)
            pub.write_json(receipt, {"tag": "r5.6", "pass": True, "source_checks_passed": True})
            previous = work / "old-job" / "release-r55" / "published.json"
            previous.parent.mkdir(parents=True)
            pub.write_json(previous, {"tag": "r5.5", "pass": True, "source_checks_passed": True})
            with patch.dict(os.environ, {"UMA_RU_RELEASE_WORK": str(work)}), \
                 patch.object(pub, "latest_snapshot", return_value=snapshot(launcher=b"signed")), \
                 patch.object(pub, "prepare") as prep, patch.object(pub, "publish") as push:
                pub.release(argparse.Namespace(published=receipt))
            self.assertEqual(prep.call_args.args[0].previous, previous)
            self.assertEqual(push.call_args.args[0].out.name, "online-r5.6")

    def test_launcher_only_release_resumes_three_assets_and_keeps_channel_bytes(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as raw:
            root = pathlib.Path(raw)
            installer, manifest = root / "UmaRU-Setup.exe", root / "launcher.json"
            installer.write_bytes(b"new setup")
            manifest.write_bytes(b"signed launcher envelope")
            offer = SimpleNamespace(version="1.2", size=installer.stat().st_size,
                                    sha256=pub.sha_file(installer), notes="notes")
            old = snapshot()
            final = dict(old, launcher_sha256=pub.sha_file(manifest), setup_sha256=pub.sha_file(installer))
            draft = Draft()
            draft.fail_upload = True
            args = argparse.Namespace(repo="owner/repo", installer=installer, manifest=manifest, out=root / "stage")
            with patch.object(pub, "signed_launcher", return_value=offer), \
                 patch.object(pub, "latest_snapshot", side_effect=[old, old, final]), \
                 patch.object(pub, "remote_release", side_effect=draft.remote), \
                 patch.object(pub, "gh", side_effect=draft.gh), \
                 patch.object(pub, "same_latest") as stable, \
                 patch.object(pub, "verify_public_latest"):
                with self.assertRaises(pub.ReleaseError):
                    pub.publish_launcher(args)
                self.assertTrue(draft.release["draft"])
                pub.publish_launcher(args)
            self.assertEqual((root / "stage/channel.json").read_bytes(), old["channel"])
            self.assertEqual({a["name"] for a in draft.release["assets"]},
                             {"channel.json", "launcher.json", "UmaRU-Setup.exe"})
            self.assertEqual(sum(c[:2] == ("release", "create") for c in draft.calls), 1)
            self.assertEqual(stable.call_count, 1)

    def test_stale_latest_blocks_promotion(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as raw:
            root = pathlib.Path(raw)
            installer, manifest = root / "UmaRU-Setup.exe", root / "launcher.json"
            installer.write_bytes(b"new setup")
            manifest.write_bytes(b"signed")
            offer = SimpleNamespace(version="1.2", size=installer.stat().st_size,
                                    sha256=pub.sha_file(installer), notes="")
            draft = Draft()
            with patch.object(pub, "signed_launcher", return_value=offer), \
                 patch.object(pub, "latest_snapshot", return_value=snapshot()), \
                 patch.object(pub, "remote_release", side_effect=draft.remote), \
                 patch.object(pub, "gh", side_effect=draft.gh), \
                 patch.object(pub, "same_latest", side_effect=pub.ReleaseError("stale")):
                with self.assertRaisesRegex(pub.ReleaseError, "stale"):
                    pub.publish_launcher(argparse.Namespace(repo="owner/repo", installer=installer,
                                                            manifest=manifest, out=root / "stage"))
            self.assertTrue(draft.release["draft"])
            self.assertFalse(any(c[:2] == ("release", "edit") for c in draft.calls))

    def test_unknown_draft_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as raw:
            root = pathlib.Path(raw)
            installer, manifest = root / "UmaRU-Setup.exe", root / "launcher.json"
            installer.write_bytes(b"new setup")
            manifest.write_bytes(b"signed")
            offer = SimpleNamespace(version="1.2", size=installer.stat().st_size,
                                    sha256=pub.sha_file(installer), notes="")
            draft = Draft()
            draft.release = {"id": 42, "draft": True, "body": "someone else's draft", "assets": []}
            with patch.object(pub, "signed_launcher", return_value=offer), \
                 patch.object(pub, "latest_snapshot", return_value=snapshot()), \
                 patch.object(pub, "remote_release", side_effect=draft.remote), \
                 patch.object(pub, "gh", side_effect=draft.gh):
                with self.assertRaises(pub.ReleaseError):
                    pub.publish_launcher(argparse.Namespace(repo="owner/repo", installer=installer,
                                                            manifest=manifest, out=root / "stage"))
            self.assertEqual(draft.calls, [])

    def test_translation_release_preserves_signed_pair(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as raw:
            out = pathlib.Path(raw)
            channel = b'{"tag":"r5.6"}'
            (out / "channel.json").write_bytes(channel)
            pub.write_json(out / "upload.json", {"repo": "owner/repo", "tag": "r5.6",
                                                "channel_sha256": pub.sha_bytes(channel),
                                                "assets": [{"name": "channel.json", "size": len(channel),
                                                            "sha256": pub.sha_bytes(channel)}]})
            prior = out / "published.json"
            pub.write_json(prior, {"tag": "r5.5"})
            pub.write_json(out / "report.local.json", {"previous_receipt": str(prior)})
            old = snapshot(launcher=b"signed", setup=b"old setup")
            final = dict(old, launcher_sha256=old["launcher_sha256"], setup_sha256=old["setup_sha256"])
            draft = Draft()
            def latest(_repo, *, require_launcher, installer_target=None):
                if installer_target:
                    installer_target.write_bytes(b"old setup")
                    return old
                return final
            with patch.object(pub, "latest_snapshot", side_effect=latest), \
                 patch.object(pub, "remote_release", side_effect=draft.remote), \
                 patch.object(pub, "gh", side_effect=draft.gh), \
                 patch.object(pub, "same_latest"), \
                 patch.object(pub, "verify_public_latest"):
                pub.publish(argparse.Namespace(out=out))
            self.assertEqual((out / "launcher.json").read_bytes(), b"signed")
            self.assertEqual((out / "UmaRU-Setup.exe").read_bytes(), b"old setup")
            self.assertEqual({a["name"] for a in draft.release["assets"]},
                             {"channel.json", "launcher.json", "UmaRU-Setup.exe"})


if __name__ == "__main__":
    unittest.main()

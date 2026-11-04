"""Prepare a public UmaRU translation channel; publish only an approved draft.

See README.md for release commands and external inputs.
This script never reads local translator settings or builds translation content.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


INNER_MANIFEST = "UmaRU/data/pack.json"
INNER_PACK = "UmaRU/data/pack.zip"
PUBLIC_FIELDS = ("format", "version", "created", "hachimi", "zip", "files", "bytes", "zip_bytes", "entries")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
TAG_RE = re.compile(r"r[0-9]+(?:\.[0-9]+)*\Z")
LAUNCHER_TAG_RE = re.compile(r"launcher-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMIC_DIR = "localized_data_ru/assets/textures/outgame/comic/"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PRIVATE_RE = re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}|(?:ghp_|github_pat_)[A-Za-z0-9_]{10,}|[A-Za-z]:\\\\Users\\\\|[A-Za-z]:/Users/")
CHUNK = 1024 * 1024


class ReleaseError(Exception):
    pass


def fail(message: str) -> None:
    raise ReleaseError(message)


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(CHUNK):
            h.update(data)
    return h.hexdigest()


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        fail(f"Expected JSON object: {path}")
    return data


def member_names(z: zipfile.ZipFile, expected: set[str] | None = None) -> set[str]:
    names = z.namelist()
    if len(names) != len(set(names)):
        fail("Duplicate ZIP member")
    if expected is not None and set(names) != expected:
        fail("ZIP members differ from manifest")
    return set(names)


def safe_entry(name: str) -> bool:
    parts = name.split("/")
    if not parts or parts[0] != "localized_data_ru" or any(p in ("", ".", "..") for p in parts):
        return False
    if "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        return False
    if name == "localized_data_ru/includes_win":
        return True
    if len(parts) > 1 and parts[1].startswith("includes"):
        return False
    return name.lower().endswith((".json", ".png", ".usm"))


def validate_manifest(data: dict, version: str, label: str) -> dict:
    if data.get("format") != 1 or data.get("version") != version or data.get("zip") != "pack.zip":
        fail(f"{label}: format, version or zip mismatch")
    if not all(k in data for k in PUBLIC_FIELDS):
        fail(f"{label}: missing public fields")
    entries = data["entries"]
    if not isinstance(entries, dict) or not entries:
        fail(f"{label}: empty/invalid entries")
    if data["files"] != len(entries) or data["bytes"] != sum(v[0] for v in entries.values()):
        fail(f"{label}: aggregate mismatch")
    for name, value in entries.items():
        if not isinstance(name, str) or not safe_entry(name):
            fail(f"{label}: forbidden path {name!r}")
        if not (isinstance(value, list) and len(value) == 2 and type(value[0]) is int
                and value[0] >= 0 and isinstance(value[1], str) and SHA_RE.fullmatch(value[1])):
            fail(f"{label}: invalid entry {name!r}")
    if type(data["zip_bytes"]) is not int or data["zip_bytes"] <= 0:
        fail(f"{label}: bad ZIP size")
    return {key: data[key] for key in PUBLIC_FIELDS}


def receipt_archive(receipt: dict, variant: str) -> Path:
    candidates = []
    for item in receipt.get("files", []):
        source = item.get("source", "")
        name = Path(source).name
        if not name.startswith("UmaRU_Player_") or not name.endswith(".zip"):
            continue
        if ("_lite_" in name) == (variant == "lite"):
            candidates.append(item)
    if len(candidates) != 1:
        fail(f"Receipt has {len(candidates)} PC {variant} archives")
    item = candidates[0]
    path = Path(item["source"])
    if not path.is_file() or sha_file(path) != item.get("sha256"):
        fail(f"Receipt archive missing or SHA mismatch: {path}")
    return path


def read_pack(archive: Path, version: str, label: str, keep_pack: bool = True) -> tuple[dict, bytes | None]:
    with zipfile.ZipFile(archive) as outer:
        names = member_names(outer)
        if INNER_MANIFEST not in names or INNER_PACK not in names:
            fail(f"{label}: no nested translation pack")
        data = json.loads(outer.read(INNER_MANIFEST))
        public = validate_manifest(data, version, label)
        pack = outer.read(INNER_PACK)
    if len(pack) != public["zip_bytes"]:
        fail(f"{label}: pack size mismatch")
    with zipfile.ZipFile(io.BytesIO(pack)) as inner:
        member_names(inner, set(public["entries"]))
        for info in inner.infolist():
            expected_size, expected_sha = public["entries"][info.filename]
            if info.is_dir() or info.file_size != expected_size:
                fail(f"{label}: member size mismatch: {info.filename}")
            h = hashlib.sha256()
            tail = b""
            with inner.open(info) as stream:
                while chunk := stream.read(CHUNK):
                    h.update(chunk)
                    if info.filename.lower().endswith(".json") and PRIVATE_RE.search(tail + chunk):
                        fail(f"{label}: private value in {info.filename}")
                    tail = chunk[-100:]
            if h.hexdigest() != expected_sha:
                fail(f"{label}: member SHA mismatch: {info.filename}")
    # Preserve the established error-205 guard for the shipped localize dictionary.
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("server_bound", Path(__file__).with_name("server_bound.py"))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        check = guard.check
        errors = check(archive)
    except Exception as exc:
        fail(f"{label}: server-bound check unavailable: {exc}")
    if errors:
        fail(f"{label}: server-bound values: {'; '.join(errors[:3])}")
    return public, pack if keep_pack else None


def validate_family(full: dict, lite: dict, label: str) -> None:
    if full["version"] != lite["version"]:
        fail(f"{label}: full/lite versions differ")
    if not set(lite["entries"]).issubset(full["entries"]):
        fail(f"{label}: lite entries not a subset of full")
    for key, value in lite["entries"].items():
        if full["entries"][key] != value:
            fail(f"{label}: full/lite member mismatch: {key}")


def is_comic(name: str) -> bool:
    return name.startswith(COMIC_DIR) and name[len(COMIC_DIR):].startswith("tex_comic") and "/" not in name[len(COMIC_DIR):]


def english_comics(full: dict, full_pack: bytes, lite: dict, en_dir: Path) -> dict[str, tuple[dict, bytes]]:
    """Optional English comics for each variant -> {variant: (manifest, pack.zip bytes)}.

    The lite pack carries only Russian comic pages, so its comics are the pages
    that have a Russian translation. The other comics of the full pack are the
    English UmaTL pages. en_dir supplies English UmaTL pages for the Russian
    ones under the same file names (tex_comic_*.diff.png). Full gets those
    pages; lite gets every English page, since it ships none.
    """
    ru_pages = sorted(n for n in lite["entries"] if is_comic(n))
    en_pages = sorted(n for n in full["entries"] if is_comic(n) and n not in lite["entries"])
    if not ru_pages:
        fail("English comics: the lite pack has no Russian comic pages")
    supplied: dict[str, Path] = {}
    for path in sorted(Path(en_dir).rglob("*")):
        if path.is_file() and path.name.lower().endswith(".png"):
            if path.name in supplied:
                fail(f"English comics: duplicate file name {path.name}")
            supplied[path.name] = path
    pages, missing = {}, []
    for name in ru_pages:
        source = supplied.get(name.rsplit("/", 1)[-1])
        if source is None:
            missing.append(name.rsplit("/", 1)[-1])
            continue
        data = source.read_bytes()
        if not data.startswith(PNG_SIGNATURE):
            fail(f"English comics: not a PNG: {source.name}")
        if sha_bytes(data) == full["entries"][name][1]:
            fail(f"English comics: {source.name} is the Russian page, not the English one")
        pages[name] = data
    if missing:
        fail(f"English comics: missing {len(missing)} pages, e.g. {', '.join(missing[:3])}")
    with zipfile.ZipFile(io.BytesIO(full_pack)) as inner:
        english_in_full = {name: inner.read(name) for name in en_pages}
    result = {}
    for variant, files in (("full", pages), ("lite", {**pages, **english_in_full})):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", allowZip64=True) as archive:
            for name in sorted(files):
                archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), files[name],
                                 compress_type=zipfile.ZIP_STORED)
        pack = buffer.getvalue()
        entries = {name: [len(files[name]), sha_bytes(files[name])] for name in sorted(files)}
        manifest = {"format": 1, "version": full["version"], "created": full["created"], "hachimi": full["hachimi"],
                    "zip": "pack.zip", "files": len(entries), "bytes": sum(v[0] for v in entries.values()),
                    "zip_bytes": len(pack), "entries": entries}
        result[variant] = (validate_manifest(manifest, full["version"], f"English comics {variant}"), pack)
    return result


def descriptor(path: Path, repo: str, tag: str) -> dict:
    return {"url": f"https://github.com/{repo}/releases/download/{quote(tag)}/{quote(path.name)}",
            "size": path.stat().st_size, "sha256": sha_file(path)}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(args: argparse.Namespace) -> None:
    if not TAG_RE.fullmatch(args.tag) or not REPO_RE.fullmatch(args.repo):
        fail("Invalid repo or tag")
    current = json_file(args.published)
    if not current.get("pass") or not current.get("source_checks_passed") or current.get("tag") != args.tag:
        fail("Current receipt is not an approved published release for this tag")
    previous = json_file(args.previous) if args.previous else None
    if previous and (not previous.get("pass") or not previous.get("source_checks_passed")
                     or previous.get("tag") == args.tag or previous.get("version") == current.get("version")):
        fail("Previous receipt is not a distinct approved release")
    out = args.out.resolve()
    if out.exists():
        fail("Output directory already exists; prepare needs a new empty target")
    archives = {v: receipt_archive(current, v) for v in ("full", "lite")}
    old_archives = {v: receipt_archive(previous, v) for v in ("full", "lite")} if previous else {}
    current_packs = {v: read_pack(archives[v], current["version"], f"current {v}") for v in archives}
    old_packs = {v: read_pack(old_archives[v], previous["version"], f"previous {v}", False) for v in old_archives}
    validate_family(current_packs["full"][0], current_packs["lite"][0], "current")
    if previous:
        validate_family(old_packs["full"][0], old_packs["lite"][0], "previous")
    comics_en = getattr(args, "comics_en", None)
    extras = (english_comics(current_packs["full"][0], current_packs["full"][1], current_packs["lite"][0], comics_en)
              if comics_en else {})
    out.mkdir(parents=True)
    try:
        variants = {}
        upload = []
        metrics = {}
        for variant in ("full", "lite"):
            manifest, pack = current_packs[variant]
            manifest_file = out / f"manifest-{variant}.json"
            pack_file = out / f"pack-{variant}.zip"
            write_json(manifest_file, manifest)
            pack_file.write_bytes(pack)
            upload.extend([manifest_file.name, pack_file.name])
            deltas = []
            if previous:
                old_manifest = old_packs[variant][0]
                changed = [name for name, value in manifest["entries"].items()
                           if old_manifest["entries"].get(name) != value]
                if changed:
                    delta_file = out / f"delta-{variant}-{previous['tag']}-to-{args.tag}.zip"
                    with zipfile.ZipFile(io.BytesIO(pack)) as inner, zipfile.ZipFile(delta_file, "w", allowZip64=True) as delta:
                        for name in sorted(changed):
                            delta.writestr(inner.getinfo(name), inner.read(name))
                    with zipfile.ZipFile(delta_file) as delta:
                        member_names(delta, set(changed))
                    upload.append(delta_file.name)
                    deltas.append({"from_version": old_manifest["version"],
                                   "from_entries_sha256": sha_bytes(canonical(old_manifest["entries"])),
                                   **descriptor(delta_file, args.repo, args.tag)})
                metrics[variant] = {"changed": len(changed), "removed": len(set(old_manifest["entries"]) - set(manifest["entries"]))}
            variants[variant] = {"manifest": descriptor(manifest_file, args.repo, args.tag),
                                 "pack": descriptor(pack_file, args.repo, args.tag), "deltas": deltas}
            if variant in extras:
                # Optional: only launchers 1.3+ read "extras", and only for players who chose English comics.
                extra_manifest, extra_pack = extras[variant]
                extra_manifest_file = out / f"manifest-comics-en-{variant}.json"
                extra_pack_file = out / f"comics-en-{variant}.zip"
                write_json(extra_manifest_file, extra_manifest)
                extra_pack_file.write_bytes(extra_pack)
                upload.extend([extra_manifest_file.name, extra_pack_file.name])
                variants[variant]["extras"] = {"comics_en": {"manifest": descriptor(extra_manifest_file, args.repo, args.tag),
                                                             "pack": descriptor(extra_pack_file, args.repo, args.tag)}}
                metrics[f"comics_en_{variant}"] = {"pages": extra_manifest["files"], "bytes": len(extra_pack)}
        channel = {"schema": 1, "tag": args.tag, "version": current["version"], "variants": variants}
        write_json(out / "channel.json", channel)
        upload.append("channel.json")
        write_json(out / "upload.json", {"repo": args.repo, "tag": args.tag, "channel_sha256": sha_file(out / "channel.json"),
                                        "assets": [{"name": name, "size": (out / name).stat().st_size,
                                                    "sha256": sha_file(out / name)} for name in upload]})
        write_json(out / "report.local.json", {"current_receipt": str(args.published.resolve()),
                                               "comics_en": str(Path(comics_en).resolve()) if comics_en else None,
                                               "previous_receipt": str(args.previous.resolve()) if previous else None,
                                               "archives": {v: str(p) for v, p in archives.items()},
                                               "previous_archives": {v: str(p) for v, p in old_archives.items()},
                                               "metrics": metrics,
                                               "upload_allowlist": upload,
                                               "latest_url": f"https://github.com/{args.repo}/releases/latest/download/channel.json"})
    except Exception:
        shutil.rmtree(out)
        raise
    print(f"Prepared {args.tag} in {out}; upload only assets listed in upload.json")


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, encoding="utf-8")
    if result.returncode:
        fail(f"gh command failed (exit {result.returncode}): {' '.join(args[:3])}: {result.stderr.strip()[:300]}")
    return result.stdout


def remote_release(repo: str, tag: str) -> dict | None:
    # REST releases/tags/{tag} may miss a draft whose tag is still pending.
    result = subprocess.run(["gh", "release", "view", tag, "--repo", repo,
                             "--json", "databaseId,isDraft,body,tagName"],
                            text=True, capture_output=True, encoding="utf-8")
    if result.returncode:
        if re.search(r"release not found|could not find release|no release found", result.stderr, re.I):
            return None
        fail(f"Could not inspect release: {result.stderr.strip()[:300]}")
    view = json.loads(result.stdout)
    release_id = view.get("databaseId")
    if view.get("tagName") != tag or type(release_id) is not int or release_id <= 0:
        fail("Release view returned inconsistent tag or ID")
    release = json.loads(gh("api", f"repos/{repo}/releases/{release_id}"))
    if (release.get("id") != release_id or release.get("tag_name") != tag
            or release.get("draft") != view.get("isDraft")):
        fail("Release view and release-by-ID disagree")
    return release


def signed_launcher(raw: bytes):
    """Use the same pinned public key and parser as the installed launcher."""
    path = Path(__file__).with_name("player") / "umaru_launcher.py"
    spec = importlib.util.spec_from_file_location("umaru_launcher", path)
    if spec is None or spec.loader is None:
        fail("Launcher signature verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    try:
        return module.parse_manifest(raw)
    except Exception as exc:
        fail(f"Invalid signed launcher manifest: {exc}")


def latest_metadata(repo: str) -> dict:
    release = json.loads(gh("api", f"repos/{repo}/releases/latest"))
    if (not isinstance(release, dict) or type(release.get("id")) is not int
            or release.get("draft") or not isinstance(release.get("assets"), list)):
        fail("Cannot identify the current public Latest release")
    names = [a.get("name") for a in release["assets"]]
    if len(names) != len(set(names)):
        fail("Latest has duplicate asset names")
    return release


def latest_asset(repo: str, release: dict, name: str, target: Path | None = None) -> bytes | None:
    assets = {a["name"]: a for a in release["assets"]}
    if name not in assets:
        fail(f"Latest release is missing {name}")
    asset = assets[name]
    url = f"https://github.com/{repo}/releases/latest/download/{name}"
    h = hashlib.sha256()
    size = 0
    chunks = [] if target is None else None
    tmp = target.with_name(target.name + ".download") if target else None
    try:
        with urlopen(Request(url, headers={"User-Agent": "UmaRU-publisher"}), timeout=90) as response:
            with tmp.open("wb") if tmp else io.BytesIO() as stream:
                while chunk := response.read(CHUNK):
                    size += len(chunk)
                    h.update(chunk)
                    stream.write(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
        if size != asset.get("size") or (asset.get("digest") and asset["digest"] != "sha256:" + h.hexdigest()):
            fail(f"Public Latest {name} differs from release metadata")
        if target:
            tmp.replace(target)
        return b"".join(chunks) if chunks is not None else None
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def latest_snapshot(repo: str, *, require_launcher: bool, installer_target: Path | None = None) -> dict:
    release = latest_metadata(repo)
    channel = latest_asset(repo, release, "channel.json")
    try:
        channel_obj = json.loads(channel)
    except (ValueError, TypeError) as exc:
        fail(f"Invalid public Latest channel: {exc}")
    old_tag = channel_obj.get("tag") if isinstance(channel_obj, dict) else None
    if not isinstance(old_tag, str) or not TAG_RE.fullmatch(old_tag):
        fail("Public Latest channel has no translation tag")
    names = {a["name"] for a in release["assets"]}
    launcher = None
    offer = None
    if "launcher.json" in names:
        launcher = latest_asset(repo, release, "launcher.json")
        offer = signed_launcher(launcher)
    elif require_launcher:
        fail("Latest has no signed launcher.json; publish the launcher migration first")
    setup = None
    if installer_target is not None:
        setup = latest_asset(repo, release, "UmaRU-Setup.exe", installer_target)
        if offer and (installer_target.stat().st_size != offer.size or sha_file(installer_target) != offer.sha256):
            fail("Latest installer does not match signed launcher manifest")
    elif "UmaRU-Setup.exe" not in names:
        fail("Latest release has no installer")
    # Reject a Latest change while downloading the assets.
    if latest_metadata(repo)["id"] != release["id"]:
        fail("Latest changed during preparation; retry from the new Latest")
    return {"id": release["id"], "tag": release["tag_name"], "channel": channel,
            "channel_tag": old_tag, "launcher": launcher, "offer": offer,
            "channel_sha256": sha_bytes(channel),
            "launcher_sha256": sha_bytes(launcher) if launcher is not None else "none",
            "setup_sha256": sha_file(installer_target) if installer_target else
                next((a.get("digest", "").removeprefix("sha256:") for a in release["assets"]
                      if a["name"] == "UmaRU-Setup.exe"), "")}


def snapshot_marker(snapshot: dict) -> str:
    return (f"UmaRU predecessor: {snapshot['id']}/{snapshot['channel_sha256']}/"
            f"{snapshot['launcher_sha256']}/{snapshot['setup_sha256']}")


def same_latest(repo: str, snapshot: dict) -> None:
    release = latest_metadata(repo)
    if release["id"] != snapshot["id"]:
        fail("Latest changed since this draft was prepared; leave draft unpublished and start from current Latest")
    assets = {a["name"]: a for a in release["assets"]}
    for name, key in (("channel.json", "channel_sha256"), ("launcher.json", "launcher_sha256"),
                      ("UmaRU-Setup.exe", "setup_sha256")):
        if snapshot[key] == "none":
            if name in assets:
                fail("Latest asset set changed since draft preparation")
        elif name not in assets or assets[name].get("digest") != "sha256:" + snapshot[key]:
            fail(f"Latest {name} changed since draft preparation")
    if sha_bytes(latest_asset(repo, release, "channel.json")) != snapshot["channel_sha256"]:
        fail("Public channel changed since draft preparation")


def verify_remote_assets(repo: str, tag: str, release: dict, expected: dict[str, dict]) -> None:
    remote_assets = {a["name"]: a for a in release.get("assets", [])}
    if len(remote_assets) != len(release.get("assets", [])) or set(remote_assets) != set(expected):
        fail("Remote asset set mismatch")
    for name, local in expected.items():
        remote = remote_assets[name]
        if remote.get("size") != local["size"]:
            fail(f"Remote size mismatch: {name}")
        digest = remote.get("digest")
        if digest:
            if digest != "sha256:" + local["sha256"]:
                fail(f"Remote digest mismatch: {name}")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                gh("release", "download", tag, "--repo", repo, "--pattern", name, "--dir", tmp)
                if sha_file(Path(tmp) / name) != local["sha256"]:
                    fail(f"Downloaded remote SHA mismatch: {name}")


def verify_public_latest(repo: str, tag: str, release_id: int, channel_sha: str) -> None:
    # Deliberately unauthenticated. This also checks the stable player endpoint.
    headers = {"User-Agent": "UmaRU-publisher", "Accept": "application/vnd.github+json"}
    with urlopen(Request(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers), timeout=30) as response:
        latest = json.load(response)
    if latest.get("id") != release_id or latest.get("tag_name") != tag or latest.get("draft"):
        fail("Published release is not the anonymous public Latest release")
    with urlopen(Request(f"https://github.com/{repo}/releases/latest/download/channel.json",
                         headers={"User-Agent": "UmaRU-publisher"}), timeout=30) as response:
        if sha_bytes(response.read()) != channel_sha:
            fail("Anonymous latest channel download SHA mismatch")


def publish(args: argparse.Namespace) -> None:
    out = args.out.resolve()
    upload = json_file(out / "upload.json")
    repo, tag = upload["repo"], upload["tag"]
    if not REPO_RE.fullmatch(repo) or not TAG_RE.fullmatch(tag):
        fail("Invalid upload identity")
    channel = out / "channel.json"
    if sha_file(channel) != upload["channel_sha256"] or json_file(channel)["tag"] != tag:
        fail("Channel identity mismatch")
    assets = upload["assets"]
    names = [a["name"] for a in assets]
    public_asset = re.compile(r"(?:channel\.json|manifest-(?:comics-en-)?(?:full|lite)\.json|pack-(?:full|lite)\.zip"
                              r"|comics-en-(?:full|lite)\.zip|delta-(?:full|lite)-r[0-9.]+-to-r[0-9.]+\.zip)\Z")
    if (len(names) != len(set(names)) or "channel.json" not in names
            or any(not public_asset.fullmatch(n) for n in names)):
        fail("Invalid upload allowlist")
    for a in assets:
        name = a["name"]
        if Path(name).name != name or (out / name).stat().st_size != a["size"] or sha_file(out / name) != a["sha256"]:
            fail(f"Local upload asset mismatch: {name}")
    old_setup = out / "UmaRU-Setup.exe"
    previous = latest_snapshot(repo, require_launcher=True, installer_target=old_setup)
    if tuple(map(int, tag[1:].split("."))) <= tuple(map(int, previous["channel_tag"][1:].split("."))):
        fail(f"Translation channel is already {previous['channel_tag']}; choose a newer approved release")
    report = json_file(out / "report.local.json")
    prior_receipt = report.get("previous_receipt")
    if prior_receipt and json_file(Path(prior_receipt)).get("tag") != previous["channel_tag"]:
        fail("Prepared delta predecessor differs from public channel; prepare again")
    if not prior_receipt:
        fail("Translation release needs a verified public predecessor")
    launcher_file = out / "launcher.json"
    if launcher_file.exists() and launcher_file.read_bytes() != previous["launcher"]:
        fail("Staged launcher manifest differs from current Latest")
    launcher_file.write_bytes(previous["launcher"])
    if previous["offer"].size != old_setup.stat().st_size or previous["offer"].sha256 != sha_file(old_setup):
        fail("Staged installer does not match signed launcher manifest")
    expected = {a["name"]: a for a in assets}
    for path in (launcher_file, old_setup):
        expected[path.name] = {"name": path.name, "size": path.stat().st_size, "sha256": sha_file(path)}
    marker = f"UmaRU channel SHA256: {upload['channel_sha256']}; {snapshot_marker(previous)}"
    release = remote_release(repo, tag)
    if release is None:
        notes = (f"[Скачать установщик для Windows](https://github.com/{repo}/releases/latest/download/UmaRU-Setup.exe)\n\n"
                 "Если лаунчер Uma RU уже установлен, откройте ярлык "
                 "«Играть на русском (Uma RU)». Лаунчер сам проверит и установит перевод.\n\n"
                 f"<!-- {marker} -->")
        gh("release", "create", tag, "--repo", repo, "--draft", "--title", f"Перевод Uma RU {tag}", "--notes", notes)
        release = remote_release(repo, tag)
    if not release or not release.get("draft") or marker not in release.get("body", ""):
        fail("Release is published or draft identity does not match; refusing to overwrite")
    remote_assets = {a["name"]: a for a in release.get("assets", [])}
    if len(remote_assets) != len(release.get("assets", [])) or set(remote_assets) - set(expected):
        fail("Draft contains unexpected/duplicate assets")
    for name, local in expected.items():
        if name not in remote_assets:
            path = out / name
            gh("release", "upload", tag, str(path), "--repo", repo)
    release = remote_release(repo, tag)
    verify_remote_assets(repo, tag, release, expected)
    same_latest(repo, previous)
    gh("release", "edit", tag, "--repo", repo, "--draft=false", "--latest")
    verify_public_latest(repo, tag, release["id"], upload["channel_sha256"])
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        final = latest_snapshot(repo, require_launcher=True,
                                installer_target=Path(tmp) / "UmaRU-Setup.exe")
    if (final["launcher_sha256"] != previous["launcher_sha256"]
            or final["setup_sha256"] != previous["setup_sha256"]):
        fail("Published translation release changed launcher assets")
    print(f"Published and verified UmaRU {tag}: {len(expected)} assets")


def publish_launcher(args: argparse.Namespace) -> None:
    repo = args.repo
    if not REPO_RE.fullmatch(repo):
        fail("Invalid repository")
    installer = args.installer.resolve()
    manifest = args.manifest.resolve()
    if not installer.is_file() or not manifest.is_file() or installer.name != "UmaRU-Setup.exe" or manifest.name != "launcher.json":
        fail("Provide finalized UmaRU-Setup.exe and signed launcher.json")
    raw = manifest.read_bytes()
    offer = signed_launcher(raw)
    tag = f"launcher-v{offer.version}"
    if not LAUNCHER_TAG_RE.fullmatch(tag) or sha_file(installer) != offer.sha256 or installer.stat().st_size != offer.size:
        fail("Signed launcher manifest does not match the installer")
    out = args.out.resolve() if args.out else default_launcher_stage(offer.version)
    out.mkdir(parents=True, exist_ok=True)
    previous = latest_snapshot(repo, require_launcher=False)
    if previous["offer"]:
        old_version = tuple(map(int, previous["offer"].version.split(".")))
        if tuple(map(int, offer.version.split("."))) <= old_version:
            fail("New launcher version must exceed the current signed launcher version")
    channel_file = out / "channel.json"
    manifest_file = out / "launcher.json"
    for path, content in ((channel_file, previous["channel"]), (manifest_file, raw)):
        if path.exists() and path.read_bytes() != content:
            fail(f"Existing staged {path.name} differs from requested release")
        path.write_bytes(content)
    expected = {p.name: {"name": p.name, "size": p.stat().st_size, "sha256": sha_file(p)}
                for p in (channel_file, manifest_file, installer)}
    marker = (f"UmaRU launcher SHA256: {sha_bytes(raw)}; channel SHA256: {previous['channel_sha256']}; "
              f"{snapshot_marker(previous)}")
    release = remote_release(repo, tag)
    if release is None:
        notes = (f"[Скачать установщик для Windows](https://github.com/{repo}/releases/latest/download/UmaRU-Setup.exe)\n\n"
                 f"Версия лаунчера: {offer.version}. {offer.notes}\n\n<!-- {marker} -->")
        gh("release", "create", tag, "--repo", repo, "--draft", "--title", f"Лаунчер Uma RU {offer.version}", "--notes", notes)
        release = remote_release(repo, tag)
    if not release or not release.get("draft") or marker not in release.get("body", ""):
        fail("Launcher release is published or draft identity does not match")
    remote_assets = {a["name"]: a for a in release.get("assets", [])}
    if len(remote_assets) != len(release.get("assets", [])) or set(remote_assets) - set(expected):
        fail("Launcher draft contains unexpected or duplicate assets")
    for name in expected:
        if name not in remote_assets:
            gh("release", "upload", tag, str(installer if name == installer.name else out / name), "--repo", repo)
    release = remote_release(repo, tag)
    verify_remote_assets(repo, tag, release, expected)
    same_latest(repo, previous)
    gh("release", "edit", tag, "--repo", repo, "--draft=false", "--latest")
    verify_public_latest(repo, tag, release["id"], previous["channel_sha256"])
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        final = latest_snapshot(repo, require_launcher=True,
                                installer_target=Path(tmp) / "UmaRU-Setup.exe")
    if (final["launcher_sha256"] != expected["launcher.json"]["sha256"]
            or final["setup_sha256"] != expected["UmaRU-Setup.exe"]["sha256"]):
        fail("Public launcher assets differ from the signed release")
    print(f"Published and verified UmaRU launcher {offer.version}: 3 assets")


def default_launcher_stage(version: str) -> Path:
    return Path(__file__).resolve().parent / "publisher" / "stages" / f"launcher-v{version}"


def release(args: argparse.Namespace) -> None:
    selected = args.published
    if not selected:
        from tkinter import Tk, filedialog
        window = Tk()
        window.withdraw()
        try:
            selected = filedialog.askopenfilename(parent=window, title="Выберите проверенный выпуск UmaRU",
                                                  filetypes=[("Проверенный выпуск", "published.json")])
        finally:
            window.destroy()
        if not selected:
            return
    receipt = Path(selected).resolve()
    current = json_file(receipt)
    tag = current.get("tag", "")
    if not (current.get("pass") and current.get("source_checks_passed") and TAG_RE.fullmatch(tag)):
        fail("Select an approved published.json")
    repo = "kapitanzet3-a11y/UmaRU-Updates"
    last = latest_snapshot(repo, require_launcher=True)["channel_tag"]
    if not TAG_RE.fullmatch(last) or tuple(map(int, tag[1:].split("."))) <= tuple(map(int, last[1:].split("."))):
        fail(f"Latest is already {last}. Choose a newer approved release.")
    work = Path(os.environ.get("UMA_RU_RELEASE_WORK") or receipt.parent.parent.parent)
    previous = [f for f in work.glob("*/release-*/published.json") if json_file(f).get("tag") == last]
    if len(previous) != 1:
        fail(f"Could not identify a unique approved receipt for {last}. Use prepare with --previous.")
    out = receipt.parent / ("online-" + tag)
    if not out.exists():
        prepare(argparse.Namespace(published=receipt, previous=previous[0], tag=tag, repo=repo, out=out,
                                   comics_en=getattr(args, "comics_en", None)))
    else:
        report = json_file(out / "report.local.json")
        identity = json_file(out / "upload.json")
        if (report.get("current_receipt") != str(receipt) or identity.get("repo") != repo
                or identity.get("tag") != tag or report.get("previous_receipt") != str(previous[0].resolve())):
            fail("Prepared directory does not belong to this release")
    publish(argparse.Namespace(out=out))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--published", required=True, type=Path)
    p.add_argument("--previous", type=Path)
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--comics-en", type=Path, help="Folder with English UmaTL comic pages (tex_comic_*.diff.png)")
    q = sub.add_parser("publish")
    q.add_argument("--out", required=True, type=Path)
    r = sub.add_parser("release", help="Publish the next approved player release to the live UmaRU channel")
    r.add_argument("--published", type=Path)
    r.add_argument("--comics-en", type=Path, help="Folder with English UmaTL comic pages (tex_comic_*.diff.png)")
    s = sub.add_parser("publish-launcher", help="Publish a signed, versioned Windows launcher release")
    s.add_argument("--repo", default="kapitanzet3-a11y/UmaRU-Updates")
    s.add_argument("--installer", required=True, type=Path)
    s.add_argument("--manifest", required=True, type=Path)
    s.add_argument("--out", type=Path, help="Local staging directory; defaults to publisher/stages/launcher-vX.Y")
    args = parser.parse_args(argv)
    try:
        {"prepare": prepare, "publish": publish, "release": release,
         "publish-launcher": publish_launcher}[args.action](args)
    except (ReleaseError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""Verified, incremental translation updates for the standalone PC player.

Only the bundled update-channel.json may select a channel. The channel never
supplies executables, Hachimi settings, or paths outside localized_data_ru.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import umaru_core as core

VERSION = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d{2})(\d{2})(\d{2})?$")
SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST = 32 * 1024 * 1024
MAX_CHANNEL = 1024 * 1024
MAX_ASSET = 12 * 1024**3
MAX_EXPANDED = 24 * 1024**3
MAX_MEMBER = 8 * 1024**3
MAX_FILES = 100_000
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)
_GITHUB_REDIRECTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
# Optional packs a channel variant may offer under "extras"; downloaded only on request.
EXTRAS = {"comics_en": "comics"}     # name -> the only content part its files may belong to


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Channel:
    url: str
    variant: str


@dataclass(frozen=True)
class Result:
    pack: core.Pack | None
    state: str
    message: str
    tag: str | None = None
    extras: dict = field(default_factory=dict)     # requested optional packs: name -> core.Pack or None
    offers: dict | None = None                      # optional packs on offer: name -> size; None — channel not read


def _version(value):
    if not isinstance(value, str):
        raise UpdateError("Неверная версия перевода")
    m = VERSION.fullmatch(value)
    if not m:
        raise UpdateError("Неверная версия перевода")
    from datetime import datetime
    try:
        datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0))
    except ValueError as exc:
        raise UpdateError("Неверная версия перевода") from exc
    return tuple(int(x or 0) for x in m.groups())


def _json(raw, limit):
    if len(raw) > limit:
        raise UpdateError("Слишком большой файл обновления")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("Повреждён JSON обновления") from exc


def _origin(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
        port = p.port
    except (ValueError, TypeError) as exc:
        raise UpdateError("Неверный адрес обновления") from exc
    if not host or p.username or p.password or p.fragment or p.scheme not in ("https", "http"):
        raise UpdateError("Неверный адрес обновления")
    if p.scheme == "http" and not (host in ("127.0.0.1", "::1") and port is not None):
        raise UpdateError("Обновления требуют HTTPS")
    return p.scheme, host.lower(), port or (443 if p.scheme == "https" else 80)


def _allowed_url(url, channel_url):
    source = _origin(channel_url)
    dest = _origin(url)
    if source[0] == "http":
        if dest != source:
            raise UpdateError("Файл обновления находится на другом сервере")
    elif dest != source:
        raise UpdateError("Файл обновления находится на другом сервере")


def read_channel(data_dir: Path) -> Channel | None:
    try:
        path = Path(data_dir) / "update-channel.json"
        if path.stat().st_size > MAX_CHANNEL:
            return None
        obj = _json(path.read_bytes(), MAX_CHANNEL)
        if not isinstance(obj, dict) or obj.get("schema") != 1 or obj.get("variant") not in ("full", "lite"):
            return None
        _origin(obj.get("url"))
        return Channel(obj["url"], obj["variant"])
    except (OSError, UpdateError):
        return None


def _cache_root(channel):
    key = hashlib.sha256(channel.url.encode("utf-8")).hexdigest()[:24]
    return core.app_home() / "updates" / key / channel.variant


def _safe_name(name):
    if not isinstance(name, str) or len(name) > 500 or not name.startswith("localized_data_ru/"):
        raise UpdateError("Недопустимый путь в пакете")
    parts = name.split("/")
    if len(parts) < 2 or any(not p or p in (".", "..") or p[-1] in (" ", ".") for p in parts):
        raise UpdateError("Недопустимый путь в пакете")
    for part in parts:
        if unicodedata.normalize("NFC", part) != part or _RESERVED.fullmatch(part):
            raise UpdateError("Недопустимое имя в пакете")
        if any(ord(c) < 32 or c in '\\:<>"|?*~' for c in part):
            raise UpdateError("Недопустимое имя в пакете")
    if not (name == "localized_data_ru/includes_win" or name.endswith((".json", ".png", ".usm"))):
        raise UpdateError("Недопустимый тип файла в пакете")
    return name.casefold()


def _entries_digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _manifest(obj, version=None, *, trusted=False):
    if not isinstance(obj, dict) or obj.get("format") != 1 or obj.get("zip") != "pack.zip":
        raise UpdateError("Неверный манифест перевода")
    _version(obj.get("version"))
    if version is not None and obj["version"] != version:
        raise UpdateError("Версия манифеста не совпадает с каналом")
    entries = obj.get("entries")
    if not isinstance(entries, dict) or not entries or len(entries) > MAX_FILES:
        raise UpdateError("Неверный список файлов перевода")
    aliases = set()
    total = 0
    for rel, spec in entries.items():
        alias = _safe_name(rel)
        if alias in aliases:
            raise UpdateError("Повторяющееся имя файла в пакете")
        aliases.add(alias)
        if (not isinstance(spec, list) or len(spec) != 2 or type(spec[0]) is not int
                or not 0 <= spec[0] <= MAX_MEMBER or not isinstance(spec[1], str)
                or not SHA.fullmatch(spec[1])):
            raise UpdateError("Неверные размер или хэш файла")
        total += spec[0]
        if total > MAX_EXPANDED:
            raise UpdateError("Пакет слишком большой")
    if any("/".join(alias.split("/")[:n]) in aliases
           for alias in aliases for n in range(1, len(alias.split("/")))):
        raise UpdateError("Файл совпадает с родительским каталогом")
    if type(obj.get("files")) is not int or obj["files"] != len(entries):
        raise UpdateError("Число файлов не совпадает")
    if type(obj.get("bytes")) is not int or obj["bytes"] != total:
        raise UpdateError("Размер перевода не совпадает")
    clean = dict(obj)
    if not trusted:
        clean.pop("config_template", None)
    return clean


def _asset(obj, channel_url):
    if not isinstance(obj, dict) or type(obj.get("size")) is not int or not 0 < obj["size"] <= MAX_ASSET:
        raise UpdateError("Неверный размер файла обновления")
    if not isinstance(obj.get("sha256"), str) or not SHA.fullmatch(obj["sha256"]):
        raise UpdateError("Неверный хэш файла обновления")
    _allowed_url(obj.get("url"), channel_url)
    return obj


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = _origin(req.full_url)
        new = _origin(newurl)
        source = _origin(self.source)
        if source[0] == "http":
            good = new == source
        else:
            good = new == source or (old[0] == new[0] == "https" and
                                     old[1] in _GITHUB_REDIRECTS and new[1] in _GITHUB_REDIRECTS)
        if not good:
            raise UpdateError("Небезопасное перенаправление обновления")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, channel_url, limit, dest=None, expected=None, progress=None):
    _allowed_url(url, channel_url)
    # Keep Windows/system HTTPS proxy settings; loopback tests must never use one.
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirects(channel_url))
              if _origin(channel_url)[0] == "http" else urllib.request.build_opener(_Redirects(channel_url)))
    request = urllib.request.Request(url, headers={"User-Agent": "UmaRU-updater/1", "Accept-Encoding": "identity"})
    size = 0
    deadline = time.monotonic() + (1800 if expected else 10)
    digest = hashlib.sha256()
    chunks = [] if dest is None else None
    with opener.open(request, timeout=5) as src:
        if src.status != 200:
            raise UpdateError("Сервер не отдал файл обновления")
        with open(dest, "wb") if dest is not None else _NullWriter() as out:
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("Превышено время загрузки обновления")
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit or (expected and size > expected["size"]):
                    raise UpdateError("Файл обновления больше ожидаемого")
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
                else:
                    out.write(chunk)
                if progress and expected:
                    progress(min(.88, .1 + .7 * size / expected["size"]),
                             f"Скачиваем перевод: {min(100, size * 100 // expected['size'])}%…")
    if expected and (size != expected["size"] or digest.hexdigest() != expected["sha256"]):
        raise UpdateError("Хэш или размер скачанного файла не совпадает")
    return b"".join(chunks) if chunks is not None else None


class _NullWriter:
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False


def _zip_members(path, expected):
    names = set()
    expanded = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            alias = _safe_name(info.filename)
            if alias in names or info.filename not in expected:
                raise UpdateError("Архив содержит лишние или повторные файлы")
            names.add(alias)
            mode = info.external_attr >> 16
            if info.is_dir() or info.flag_bits & 1 or (mode & 0o170000) not in (0, 0o100000):
                raise UpdateError("Архив содержит ссылку или каталог")
            if info.file_size != expected[info.filename][0] or info.file_size > MAX_MEMBER:
                raise UpdateError("Размер файла в архиве не совпадает")
            expanded += info.file_size
            if expanded > MAX_EXPANDED:
                raise UpdateError("Архив слишком большой")
            h = hashlib.sha256()
            count = 0
            with zf.open(info) as f:
                while chunk := f.read(1 << 20):
                    count += len(chunk)
                    if count > info.file_size:
                        raise UpdateError("Файл в архиве слишком большой")
                    h.update(chunk)
            if count != info.file_size or h.hexdigest() != expected[info.filename][1]:
                raise UpdateError("Хэш файла в архиве не совпадает")
        if names != {name.casefold() for name in expected}:
            raise UpdateError("В архиве нет части файлов")


def _write_receipt(root, source_sha):
    manifest_path, zip_path = root / "pack.json", root / "pack.zip"
    z = zip_path.stat()
    core.write_json(root / "verified.json", {
        "schema": 1, "source_manifest_sha256": source_sha,
        "local_manifest_sha256": core.sha256_file(manifest_path),
        "zip_size": z.st_size, "zip_mtime_ns": z.st_mtime_ns,
    })


def _pack(root, installer, *, trusted=False, staged=False):
    root = Path(root)
    path = root / "pack.json"
    try:
        if path.stat().st_size > MAX_MANIFEST:
            return None
        raw = path.read_bytes()
        manifest = _manifest(_json(raw, MAX_MANIFEST), trusted=trusted)
        zip_path = root / "pack.zip"
        if not zip_path.is_file() or zip_path.stat().st_size > MAX_ASSET:
            return None
        if not trusted and not staged:
            receipt = core.read_json(root / "verified.json")
            if (not isinstance(receipt, dict) or receipt.get("schema") != 1
                    or not isinstance(receipt.get("source_manifest_sha256"), str)
                    or not SHA.fullmatch(receipt["source_manifest_sha256"])
                    or digest_bytes(raw) != receipt.get("local_manifest_sha256")):
                return None
            stat = zip_path.stat()
            if stat.st_size != receipt.get("zip_size") or stat.st_mtime_ns != receipt.get("zip_mtime_ns"):
                _zip_members(zip_path, manifest["entries"])
                _write_receipt(root, receipt["source_manifest_sha256"])
        else:
            _zip_members(zip_path, manifest["entries"])
        return core.Pack(root, manifest, zip_path, installer)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, UpdateError):
        return None


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _pointer_pack(root, installer):
    obj = core.read_json(root / "current.json")
    if not isinstance(obj, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(obj.get("id", ""))):
        return None
    candidate = root / obj["id"]
    if candidate.is_symlink() or candidate.resolve().parent != root.resolve():
        return None
    return _pack(candidate, installer)


def _remote_receipt_matches(pack, manifest_sha):
    if not pack or not pack.root.name or not re.fullmatch(r"[0-9a-f]{64}", pack.root.name):
        return False
    receipt = core.read_json(pack.root / "verified.json")
    return isinstance(receipt, dict) and receipt.get("source_manifest_sha256") == manifest_sha


def cached_pack(data_dir, channel):
    """Best fully verified local pack, respecting the selected variant."""
    installer = Path(data_dir) / "hachimi_installer.exe"
    local = _pointer_pack(_cache_root(channel), installer)
    bundled = None
    bundled_path = Path(data_dir) / "pack.json"
    # Reading the small bundled manifest is enough to skip an older ZIP.
    if local:
        try:
            small = _manifest(_json(bundled_path.read_bytes(), MAX_MANIFEST), trusted=True)
            if small.get("variant", "full") == channel.variant and _version(small["version"]) > _version(local.version):
                bundled = _pack(data_dir, installer, trusted=True)
        except (OSError, UpdateError):
            pass
    else:
        bundled = _pack(data_dir, installer, trusted=True)
        if bundled and bundled.manifest.get("variant", "full") != channel.variant:
            bundled = None
    if local and bundled:
        return local if _version(local.version) >= _version(bundled.version) else bundled
    return local or bundled


def _receipt_source(game_dir, manifest, target=False, from_version=None, digest=None):
    if not game_dir:
        return None
    state = core.load_state(game_dir)
    hashes = state.get("entries")
    if not isinstance(hashes, dict):
        return None
    if target:
        if state.get("pack_version") != manifest["version"] or hashes != {r: s[1] for r, s in manifest["entries"].items()}:
            return None
    elif state.get("pack_version") != from_version:
        return None
    base = Path(game_dir) / "hachimi"
    resolved_base = base.resolve()
    if base.is_symlink() or not resolved_base.is_relative_to(Path(game_dir).resolve()):
        return None
    entries = {}
    aliases = set()
    try:
        for rel, sha in hashes.items():
            alias = _safe_name(rel)
            if alias in aliases:
                return None
            aliases.add(alias)
            if not isinstance(sha, str) or not SHA.fullmatch(sha):
                return None
            p = core.safe_join(base, rel)
            parts = rel.split("/")
            if (not p.resolve().is_relative_to(resolved_base)
                    or any(base.joinpath(*parts[:n]).is_symlink() for n in range(1, len(parts) + 1))
                    or not p.is_file()):
                return None
            size = p.stat().st_size
            if size > MAX_MEMBER or core.sha256_file(p) != sha:
                return None
            entries[rel] = [size, sha]
    except (OSError, core.UmaError):
        return None
    if any("/".join(alias.split("/")[:n]) in aliases
           for alias in aliases for n in range(1, len(alias.split("/")))):
        return None
    if target:
        return base if entries == manifest["entries"] else None
    return base if _entries_digest(entries) == digest else None


def _write_rebuilt(dest, manifest, base_zip=None, game_base=None, patch=None):
    entries = manifest["entries"]
    changed = set(entries)
    old = base_zip.manifest["entries"] if base_zip else {}
    if base_zip:
        changed = {r for r, spec in entries.items() if old.get(r) != spec}
    elif game_base:
        changed = {r for r, spec in entries.items() if not core.safe_join(game_base, r).is_file()
                   or core.sha256_file(core.safe_join(game_base, r)) != spec[1]}
    if patch:
        _zip_members(patch, {r: entries[r] for r in changed})
    elif changed:
        raise UpdateError("Не хватает файлов для сборки перевода")
    with zipfile.ZipFile(dest, "w", allowZip64=True) as out:
        with zipfile.ZipFile(base_zip.zip_path) if base_zip else _NullWriter() as old_zip:
            with zipfile.ZipFile(patch) if patch else _NullWriter() as patch_zip:
                for rel, (size, sha) in entries.items():
                    if rel in changed:
                        src = patch_zip.open(rel)
                    elif base_zip:
                        src = old_zip.open(rel)
                    else:
                        src = open(core.safe_join(game_base, rel), "rb")
                    with src, out.open(rel, "w", force_zip64=size > 2**31) as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
    _zip_members(dest, entries)


def _safe_remove(root, path):
    """Remove only an owned direct cache child, never a junction out of cache."""
    root = Path(root).resolve()
    path = Path(path)
    name = path.name
    if not (re.fullmatch(r"[0-9a-f]{64}", name) or
            re.fullmatch(r"\.stage-[0-9a-f]{32}", name)):
        raise UpdateError("Недопустимый путь очистки кеша")
    if path.parent.resolve() != root or path.is_symlink() or path.resolve() == root or path.resolve().parent != root:
        raise UpdateError("Путь очистки выходит за пределы кеша")
    if path.is_dir():
        shutil.rmtree(path)


def _promote(root, stage, manifest, installer):
    identity = digest_bytes((manifest["version"] + ":" + _entries_digest(manifest["entries"])).encode())
    final = root / identity
    if final.exists():
        existing = _pack(final, installer)
        if existing and existing.version == manifest["version"] and existing.entries == manifest["entries"]:
            _safe_remove(root, stage)
        else:
            _safe_remove(root, final)
            os.replace(stage, final)
    else:
        os.replace(stage, final)
    core.write_json(root / "current.json", {"id": identity, "version": manifest["version"]})
    _prune(root, final)
    return core.Pack(final, manifest, final / "pack.zip", installer)


def _prune(root, keep):
    """Remove every cached pack except keep. Each full pack is hundreds of MB.

    Older packs are never read again: a delta starts from the current pack or
    from files already installed in the game. Pruning is best effort, so a pack
    that another process still has open stays until the next update instead of
    failing an update that was already promoted.
    """
    try:
        old = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name) and p != keep]
    except OSError:
        return
    for p in old:
        try:
            _safe_remove(root, p)
        except (OSError, UpdateError):
            pass


@contextmanager
def _lock(root):
    """Kernel lock: released even when a player process crashes."""
    path = root / ".lock"
    with open(path, "a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        deadline = time.monotonic() + 2
        while True:
            try:
                file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() > deadline:
                    raise UpdateError("Обновление уже выполняется") from exc
                time.sleep(.1)
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _extra_root(channel, name):
    return _cache_root(channel).with_name(f"{channel.variant}.{name}")


def _check_extra(name, manifest):
    part = EXTRAS[name]
    if any(core.content_part(rel) != part for rel in manifest["entries"]):
        raise UpdateError("Дополнительный пакет перевода содержит чужие файлы")


def cached_extra(data_dir, channel, name):
    """Last verified optional pack (e.g. English comics) or None."""
    if name not in EXTRAS:
        return None
    pack = _pointer_pack(_extra_root(channel, name), Path(data_dir) / "hachimi_installer.exe")
    try:
        if pack:
            _check_extra(name, pack.manifest)
    except UpdateError:
        return None
    return pack


def _offers(variant, channel_url):
    extras = variant.get("extras", {})
    if not isinstance(extras, dict):
        raise UpdateError("Неверный список дополнительных пакетов")
    offers = {}
    for name, spec in extras.items():
        if name in EXTRAS and isinstance(spec, dict):
            offers[name] = {"manifest": _asset(spec.get("manifest"), channel_url),
                            "pack": _asset(spec.get("pack"), channel_url)}
    return offers


def _resolve_extra(data_dir, channel, name, spec, version, progress=None):
    """Fetch one optional pack. Same checks as the main pack; reuse the cached ZIP when its files are unchanged."""
    installer = Path(data_dir) / "hachimi_installer.exe"
    root = _extra_root(channel, name)
    local = cached_extra(data_dir, channel, name)
    if local and local.version == version and _remote_receipt_matches(local, spec["manifest"]["sha256"]):
        return local
    raw_manifest = _download(spec["manifest"]["url"], channel.url, MAX_MANIFEST, expected=spec["manifest"])
    manifest = _manifest(_json(raw_manifest, MAX_MANIFEST), version)
    _check_extra(name, manifest)
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root):
        stage = root / (".stage-" + uuid.uuid4().hex)
        stage.mkdir()
        try:
            (stage / "pack.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            if local and local.entries == manifest["entries"]:
                _write_rebuilt(stage / "pack.zip", manifest, base_zip=local)
            else:
                label = "Скачиваем английские комиксы…"
                if progress:
                    progress(.1, label)
                _download(spec["pack"]["url"], channel.url, MAX_ASSET, stage / "pack.zip", spec["pack"],
                          (lambda frac, _message: progress(frac, label)) if progress else None)
            if not _pack(stage, installer, staged=True):
                raise UpdateError("Скачанный пакет не прошёл проверку")
            _write_receipt(stage, digest_bytes(raw_manifest))
            return _promote(root, stage, manifest, installer)
        finally:
            if stage.exists():
                _safe_remove(root, stage)


def resolve(data_dir, channel, *, game_dir=None, progress=None, extras=()):
    """Check channel, verify assets, promote a complete pack, or keep last good pack.

    extras names optional packs to fetch as well (EXTRAS). A failed optional
    pack never fails the main result: the last verified copy, if any, is used.
    """
    seen = {}
    result = _resolve_pack(data_dir, channel, game_dir=game_dir, progress=progress, seen=seen)
    offers = None
    if "variant" in seen:
        try:
            offers = _offers(seen["variant"], channel.url)
        except UpdateError:
            offers = {}
    got = {}
    for name in extras:
        if name not in EXTRAS:
            continue
        pack = None
        if offers and name in offers:
            try:
                pack = _resolve_extra(data_dir, channel, name, offers[name], seen["version"], progress)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, http.client.HTTPException,
                    urllib.error.URLError, socket.timeout, UpdateError):
                pack = None
        got[name] = pack or cached_extra(data_dir, channel, name)
    return replace(result, extras=got,
                   offers=None if offers is None else {name: spec["pack"]["size"] for name, spec in offers.items()})


def _resolve_pack(data_dir, channel, *, game_dir=None, progress=None, seen=None):
    """Main pack: check channel, verify assets, promote a complete pack, or keep last good pack."""
    seen = {} if seen is None else seen
    data_dir = Path(data_dir)
    local = cached_pack(data_dir, channel)
    root = _cache_root(channel)
    last_progress = [0.0]
    def report(frac, message):
        if progress and (frac >= 1 or time.monotonic() - last_progress[0] >= .15):
            last_progress[0] = time.monotonic()
            progress(frac, message)
    try:
        raw = _download(channel.url, channel.url, MAX_CHANNEL)
        index = _json(raw, MAX_CHANNEL)
        if not isinstance(index, dict) or index.get("schema") != 1 or not isinstance(index.get("tag"), str):
            raise UpdateError("Неверный канал обновлений")
        version = index.get("version")
        _version(version)
        # The installed receipt is a version floor even when its files still
        # need caller-side inspection. Do not fetch an older remote payload.
        if game_dir:
            installed = core.load_state(game_dir).get("pack_version")
            try:
                if _version(installed) > _version(version):
                    return Result(local, "current", "В игре уже установлена более новая версия перевода.", index["tag"])
            except UpdateError:
                pass
        variants = index.get("variants")
        if not isinstance(variants, dict):
            raise UpdateError("Неверный список вариантов перевода")
        variant = variants.get(channel.variant)
        if not isinstance(variant, dict):
            raise UpdateError("Нет выбранного варианта перевода")
        seen.update(variant=variant, version=version)
        manifest_asset = _asset(variant.get("manifest"), channel.url)
        pack_asset = _asset(variant.get("pack"), channel.url)
        deltas = variant.get("deltas", [])
        if not isinstance(deltas, list) or len(deltas) > 100:
            raise UpdateError("Неверный список дельт")
        for delta in deltas:
            if not isinstance(delta, dict):
                raise UpdateError("Неверная дельта")
            _version(delta.get("from_version"))
            if not isinstance(delta.get("from_entries_sha256"), str) or not SHA.fullmatch(delta["from_entries_sha256"]):
                raise UpdateError("Неверная дельта")
            _asset(delta, channel.url)
        tag = index["tag"]
        if local and _version(local.version) > _version(version):
            return Result(local, "current", "Установлен более новый перевод.", tag)
        if local and local.version == version and _remote_receipt_matches(local, manifest_asset["sha256"]):
            return Result(local, "current", "Перевод актуален.", tag)
        raw_manifest = _download(manifest_asset["url"], channel.url, MAX_MANIFEST, expected=manifest_asset)
        manifest = _manifest(_json(raw_manifest, MAX_MANIFEST), version)
        if local and local.version == version:
            if _entries_digest(local.entries) != _entries_digest(manifest["entries"]):
                raise UpdateError("Канал изменил содержимое той же версии")
            return Result(local, "current", "Перевод актуален.", tag)
        root.mkdir(parents=True, exist_ok=True)
        with _lock(root):
            for stale in root.glob(".stage-*"):
                if stale.is_dir() and time.time() - stale.stat().st_mtime > 3600:
                    _safe_remove(root, stale)
            # Another process may have promoted the pack while we waited.
            local = cached_pack(data_dir, channel)
            if local and _version(local.version) >= _version(version):
                if local.version == version and _entries_digest(local.entries) != _entries_digest(manifest["entries"]):
                    raise UpdateError("Канал изменил содержимое той же версии")
                return Result(local, "current", "Перевод актуален.", tag)
            stage = root / (".stage-" + uuid.uuid4().hex)
            stage.mkdir()
            try:
                (stage / "pack.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                base = local if local and _version(local.version) < _version(version) else None
                installed_base = None
                delta = next((d for d in deltas if base and d["from_version"] == base.version
                              and d["from_entries_sha256"] == _entries_digest(base.entries)), None)
                if not delta and game_dir:
                    for d in deltas:
                        installed_base = _receipt_source(game_dir, manifest, from_version=d["from_version"],
                                                         digest=d["from_entries_sha256"])
                        if installed_base:
                            delta = d
                            base = None
                            break
                target_files = _receipt_source(game_dir, manifest, target=True)
                if target_files:
                    _write_rebuilt(stage / "pack.zip", manifest, game_base=target_files)
                elif base and base.entries == manifest["entries"]:
                    _write_rebuilt(stage / "pack.zip", manifest, base_zip=base)
                else:
                    delta_ok = False
                    if delta and delta["size"] < pack_asset["size"]:
                        report(.1, "Скачиваем изменённые файлы…")
                        patch = stage / "patch.zip"
                        try:
                            _download(delta["url"], channel.url, MAX_ASSET, patch, delta, report)
                            _write_rebuilt(stage / "pack.zip", manifest, base_zip=base,
                                           game_base=installed_base, patch=patch)
                            delta_ok = True
                        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, http.client.HTTPException,
                                urllib.error.URLError, socket.timeout, UpdateError):
                            (stage / "pack.zip").unlink(missing_ok=True)
                        finally:
                            patch.unlink(missing_ok=True)
                    if not delta_ok:
                        report(.1, "Скачиваем полный перевод…")
                        _download(pack_asset["url"], channel.url, MAX_ASSET, stage / "pack.zip", pack_asset, report)
                if not _pack(stage, data_dir / "hachimi_installer.exe", staged=True):
                    raise UpdateError("Скачанный пакет не прошёл проверку")
                _write_receipt(stage, digest_bytes(raw_manifest))
                promoted = _promote(root, stage, manifest, data_dir / "hachimi_installer.exe")
                report(1, "Перевод обновлён.")
                return Result(promoted, "updated", "Перевод обновлён.", tag)
            finally:
                if stage.exists():
                    _safe_remove(root, stage)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, http.client.HTTPException,
            urllib.error.URLError, socket.timeout, UpdateError) as exc:
        state = ("offline" if isinstance(exc, (urllib.error.URLError, socket.timeout))
                 and not isinstance(exc, urllib.error.HTTPError) else "error")
        if local:
            return Result(local, state,
                          "Не удалось обновить перевод. Доступна сохранённая версия.")
        return Result(None, state,
                      "Не удалось загрузить перевод. Проверьте подключение и повторите.")

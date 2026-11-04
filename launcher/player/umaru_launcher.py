# -*- coding: utf-8 -*-
"""Signed launcher update discovery and verified installer download.

The window checks for a new version on every start and offers it; only the
player's click downloads and starts the installer. The manifest cannot choose
a URL or a local path. A verified version selects one immutable GitHub release
asset; this module never starts the installer.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from umaru_updates import _download, UpdateError

REPO = "kapitanzet3-a11y/UmaRU-Updates"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/launcher.json"
MAX_MANIFEST = 16 * 1024
MAX_SETUP = 1024**3
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA = re.compile(r"^[0-9a-f]{64}$")
DOWNLOAD_DIR = re.compile(r"^umaru-launcher-[0-9a-f]{32}$")

# Set to the public material printed by sign-launcher.ps1 -Init. An unset key
# fails closed; it must never be fetched from the update server.
PUBLIC_MODULUS_B64 = (
    "p3LhUMGVmPNR2UMLklXxp0GVgM7OwPF0Rawise8k2D765bLoFRGKJb2xlL97WfgvbeCkFqjhoAasLOBC7RcfxCmqim2kd0bCIQYrA42nOsqKzQ/t56jmykFUURLIP7qfUMaFgCnZ7EAu1IX509+dGaPcWLkjhJJae/u8Oi5AvQBdiIhUAISCR7CdGhmTVRvja3p43Bn6tzByD9mmOtyuxq2VmjsCP/cP3rg2kJs/0P+MCOn2ASxaUNE1u49VMkmCHOMQj4B2FZzndM0FjAI5DDHeeLF9jBR63bilwJcT2B+5EjG3Asi2oTQGSCSPxD7Cwsrzj3sXlOSn7/czsbEsOzo268jS+FGFJ6lnrd+pKuctN3P6O7o/7wc9GWRkx3Nn7OQJbLpkHQjsWMu8Lhny//2U03mzoFd6aZGhs+D1pBCMoIg8VU51NPpfbLxiTg40dNGY6kcwex/gUgztrrkDDLix52TEJK9vainlseUJcckCqT5ImpFz8zIVv2MgGQIJ"
)
PUBLIC_EXPONENT_B64 = "AQAB"


class LauncherUpdateError(Exception):
    pass


@dataclass(frozen=True)
class Offer:
    version: str
    notes: str
    size: int
    sha256: str
    _payload: bytes = field(repr=False, compare=False)
    _signature: bytes = field(repr=False, compare=False)


def _version(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or len(value) > 24 or not VERSION.fullmatch(value):
        raise LauncherUpdateError("Неверная версия лаунчера")
    parts = tuple(map(int, value.split(".")))
    if any(part > 2**31 - 1 for part in parts):
        raise LauncherUpdateError("Неверная версия лаунчера")
    return parts


def _unique_json(raw: bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise LauncherUpdateError("Повторяющееся поле в манифесте лаунчера")
            out[key] = value
        return out
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError) as exc:
        raise LauncherUpdateError("Повреждён манифест лаунчера") from exc


def _b64(value: str, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > limit:
        raise LauncherUpdateError("Неверная подпись обновления")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LauncherUpdateError("Неверная подпись обновления") from exc


def _verify(payload: bytes, signature: bytes, modulus_b64=None, exponent_b64=None):
    """RSA PKCS#1 v1.5 SHA-256 verification by Windows CNG (no private key)."""
    modulus = _b64(PUBLIC_MODULUS_B64 if modulus_b64 is None else modulus_b64, 1024)
    exponent = _b64(PUBLIC_EXPONENT_B64 if exponent_b64 is None else exponent_b64, 32)
    if (len(modulus) < 256 or len(modulus) > 512 or not modulus[0] & 0x80 or
            not 1 <= len(exponent) <= 8 or int.from_bytes(exponent, "big") < 3 or
            int.from_bytes(exponent, "big") % 2 == 0 or len(signature) != len(modulus)):
        raise LauncherUpdateError("Недопустимый открытый ключ или подпись лаунчера")
    if os.name != "nt":
        raise LauncherUpdateError("Проверка подписи лаунчера доступна только в Windows")
    rsa_blob = struct.pack("<6I", 0x31415352, len(modulus) * 8,
                           len(exponent), len(modulus), 0, 0) + exponent + modulus
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    ptr = ctypes.c_void_p
    ulong = ctypes.c_ulong
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(ptr), ctypes.c_wchar_p, ctypes.c_wchar_p, ulong]
    bcrypt.BCryptImportKeyPair.argtypes = [ptr, ptr, ctypes.c_wchar_p, ctypes.POINTER(ptr), ptr, ulong, ulong]
    bcrypt.BCryptVerifySignature.argtypes = [ptr, ptr, ptr, ulong, ptr, ulong, ulong]
    bcrypt.BCryptDestroyKey.argtypes = [ptr]
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ptr, ulong]
    alg = ptr()
    key = ptr()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "RSA", None, 0):
        raise LauncherUpdateError("Не удалось открыть системную проверку подписи")
    try:
        blob = ctypes.create_string_buffer(rsa_blob)
        if bcrypt.BCryptImportKeyPair(alg, None, "RSAPUBLICBLOB", ctypes.byref(key), blob,
                                      len(rsa_blob), 0):
            raise LauncherUpdateError("Не удалось загрузить открытый ключ лаунчера")
        try:
            class PaddingInfo(ctypes.Structure):
                _fields_ = [("pszAlgId", ctypes.c_wchar_p)]
            padding = PaddingInfo("SHA256")
            digest = ctypes.create_string_buffer(hashlib.sha256(payload).digest())
            sig = ctypes.create_string_buffer(signature)
            if bcrypt.BCryptVerifySignature(key, ctypes.byref(padding), digest, 32,
                                             sig, len(signature), 0x2):
                raise LauncherUpdateError("Подпись обновления лаунчера не совпадает")
        finally:
            bcrypt.BCryptDestroyKey(key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def _parse_manifest(raw: bytes, *, modulus_b64=None, exponent_b64=None) -> Offer:
    if len(raw) > MAX_MANIFEST:
        raise LauncherUpdateError("Манифест лаунчера слишком большой")
    outer = _unique_json(raw)
    if not isinstance(outer, dict) or set(outer) != {"payload", "signature"}:
        raise LauncherUpdateError("Неверный формат манифеста лаунчера")
    payload = _b64(outer["payload"], MAX_MANIFEST)
    signature = _b64(outer["signature"], 1024)
    _verify(payload, signature, modulus_b64, exponent_b64)
    obj = _unique_json(payload)
    if not isinstance(obj, dict) or set(obj) != {"schema", "version", "size", "sha256", "notes"}:
        raise LauncherUpdateError("Неверный формат данных лаунчера")
    if type(obj["schema"]) is not int or obj["schema"] != 1:
        raise LauncherUpdateError("Неизвестный формат обновления лаунчера")
    _version(obj["version"])
    if type(obj["size"]) is not int or not 0 < obj["size"] <= MAX_SETUP:
        raise LauncherUpdateError("Неверный размер установщика")
    if not isinstance(obj["sha256"], str) or not SHA.fullmatch(obj["sha256"]):
        raise LauncherUpdateError("Неверный SHA-256 установщика")
    if not isinstance(obj["notes"], str) or len(obj["notes"]) > 2000 or any(
            ord(ch) < 32 and ch not in "\n\r\t" for ch in obj["notes"]):
        raise LauncherUpdateError("Неверные заметки к выпуску")
    return Offer(obj["version"], obj["notes"], obj["size"], obj["sha256"], payload, signature)


def parse_manifest(raw: bytes) -> Offer:
    """Verify a downloaded launcher.json against the bundled public key."""
    return _parse_manifest(raw)


def check(current_version: str) -> Offer | None:
    """Fetch latest signed offer; return it only when newer than current_version."""
    current = _version(current_version)
    try:
        raw = _download(MANIFEST_URL, MANIFEST_URL, MAX_MANIFEST)
        offer = _parse_manifest(raw)
    except (UpdateError, OSError, urllib.error.URLError) as exc:
        raise LauncherUpdateError("Не удалось проверить обновление лаунчера") from exc
    return offer if _version(offer.version) > current else None


def _reverify_offer(offer: Offer) -> Offer:
    if not isinstance(offer, Offer):
        raise LauncherUpdateError("Неверное предложение обновления")
    verified = _parse_manifest(json.dumps({
        "payload": base64.b64encode(offer._payload).decode("ascii"),
        "signature": base64.b64encode(offer._signature).decode("ascii"),
    }).encode("ascii"))
    if (verified.version, verified.notes, verified.size, verified.sha256) != (
            offer.version, offer.notes, offer.size, offer.sha256):
        raise LauncherUpdateError("Предложение обновления было изменено")
    return verified


def verify_installer(offer: Offer, path: str | Path) -> Path:
    """Recheck a previously downloaded EXE immediately before manual launch."""
    verified = _reverify_offer(offer)
    target = Path(path)
    try:
        if target.name != "UmaRU-Setup.exe" or not stat.S_ISREG(target.lstat().st_mode):
            raise LauncherUpdateError("Неверный файл установщика")
        digest = hashlib.sha256()
        total = 0
        with target.open("rb") as source:
            while chunk := source.read(1 << 20):
                total += len(chunk)
                if total > verified.size:
                    raise LauncherUpdateError("Размер установщика не совпадает")
                digest.update(chunk)
        if total != verified.size or digest.hexdigest() != verified.sha256:
            raise LauncherUpdateError("SHA-256 установщика не совпадает")
    except OSError as exc:
        raise LauncherUpdateError("Не удалось проверить установщик") from exc
    return target


def download(offer: Offer, directory: str | Path, progress=None) -> Path:
    """Download to a unique child directory; return only a fully verified EXE."""
    verified = _reverify_offer(offer)
    version = verified.version
    url = (f"https://github.com/{REPO}/releases/download/"
           f"launcher-v{version}/UmaRU-Setup.exe")
    parent = Path(directory)
    parent.mkdir(parents=True, exist_ok=True)
    request_dir = parent / f"umaru-launcher-{uuid.uuid4().hex}"
    request_dir.mkdir(mode=0o700)
    partial = request_dir / "UmaRU-Setup.exe.part"
    final = request_dir / "UmaRU-Setup.exe"
    try:
        expected = {"size": verified.size, "sha256": verified.sha256}
        _download(url, MANIFEST_URL, verified.size, dest=partial,
                  expected=expected,
                  progress=(lambda fraction, _message: progress(fraction, "Скачиваем установщик…"))
                  if progress else None)
        os.replace(partial, final)
        return final
    except (UpdateError, OSError, urllib.error.URLError) as exc:
        raise LauncherUpdateError("Не удалось скачать и проверить установщик") from exc
    finally:
        partial.unlink(missing_ok=True)


def cleanup_downloads(directory: str | Path, max_age: float = 3600) -> int:
    """Remove installer folders left by earlier downloads; return how many were removed.

    The installer cannot delete itself, so each update leaves one folder. Only
    folders created by download() and older than max_age are touched: a recent
    one may still belong to a running setup.
    """
    try:
        children = list(Path(directory).iterdir())
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for child in children:
        try:
            if (not DOWNLOAD_DIR.fullmatch(child.name) or child.is_symlink() or not child.is_dir()
                    or now - child.stat().st_mtime < max_age):
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed

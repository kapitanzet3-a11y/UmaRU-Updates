# -*- coding: utf-8 -*-
"""Лаунчер Uma RU — ядро для игроков: найти игру, поставить/починить Hachimi, разложить готовый перевод.

Только стандартная библиотека (программа собирается в один exe). Само ядро в сеть не ходит: оно раскладывает готовый
«пакет» (pack.json + pack.zip). В установленной программе пакет скачивает и проверяет umaru_updates.py в кэш
%LOCALAPPDATA%\\UmaRU\\updates, в портативной сборке он лежит рядом с программой в data\\. Hachimi ставит его собственный
установщик (data\\hachimi_installer.exe).

Как устроена установка Hachimi Edge для Steam-версии (проверено на копии игры, результат совпал с настоящей
установкой байт в байт): `hachimi_installer.exe install --install-dir <игра> --explicit-target cri_mana_vpx.dll`
  UmamusumePrettyDerby_Jpn.exe      -> UmamusumePrettyDerby_Jpn.old.exe  (оригинальная «заглушка» Steam, в ней секция .bind)
  новый UmamusumePrettyDerby_Jpn.exe  (распакованный плеер Unity; установщик делает его из файла .exe._ этой сборки)
  cri_mana_vpx.dll                  (сам Hachimi, ~29 МБ, в корне игры; оригинал остаётся в Plugins\\x86_64)
  steam_appid.txt                   (3564400)
Установщик молчит и отвечает только кодом выхода (0 — готово, 128 — не вышло), права администратора не нужны.
После обновления игры Steam возвращает заглушку на место exe — ровно так Hachimi «слетает», а починка — повторный
запуск того же установщика (он сам сохраняет новую заглушку и пересобирает exe).
"""
import ctypes
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

APP_ID = "3564400"
GAME_EXE = "UmamusumePrettyDerby_Jpn.exe"
OLD_EXE = "UmamusumePrettyDerby_Jpn.old.exe"
PROXY_DLL = "cri_mana_vpx.dll"
APPID_FILE = "steam_appid.txt"
HACHIMI_DIR = "hachimi"
LD_DIR = "localized_data_ru"
STATE_NAME = "uma_ru_player.json"
CONFIG_NAME = "config.json"
CONFIG_BACKUP = "config.json.umaru-backup"
STEAM_URL = f"steam://rungameid/{APP_ID}"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HACHIMI_MIN_DLL_BYTES = 8_000_000          # настоящий cri_mana_vpx.dll игры — 2,2 МБ, Hachimi — около 29 МБ
HACHIMI_MARKER = b"hachimi::core"          # строка из самой библиотеки Hachimi
STEAM_UPDATING_FLAGS = 2 | 256 | 1024 | 2048   # StateFlags манифеста: нужно обновление / идёт обновление
FREE_SPACE_MARGIN = 300 * 1024 * 1024

# Что должно стоять в config.json Hachimi, чтобы он показывал наш перевод и ничего не качал и не подменял.
REQUIRED_CONFIG = {
    "localized_data_dir": LD_DIR,
    "translator_mode": False,
    "auto_translate_stories": False,
    "auto_translate_localize": False,
    "disable_translations": False,
    "tl_auto_updater_mode": "Disabled",
    "translation_repo_index": None,
    "selected_tl_repo_id": None,
    "skip_first_time_setup": True,
}

log = logging.getLogger("umaru")


class UmaError(Exception):
    """Ошибка, текст которой можно показывать человеку как есть."""


class NeedAdmin(UmaError):
    """В папку игры нельзя писать без прав администратора."""


# ---------------------------------------------------------------- настройки программы

def app_home():
    """Папка настроек и журнала программы (рядом с игрой ничего лишнего не кладём)."""
    env = os.environ.get("UMA_RU_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "UmaRU"


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data


def write_json(path, data, indent=1):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".umaru-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=indent)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def load_settings():
    data = read_json(app_home() / "settings.json")
    return data if isinstance(data, dict) else {}


def save_settings(updates):
    data = load_settings()
    data.update(updates)
    write_json(app_home() / "settings.json", data)


# ---------------------------------------------------------------- игра

@dataclass
class Game:
    path: Path
    buildid: str = ""
    state_flags: int = 0
    source: str = ""

    @property
    def steam_updating(self):
        return bool(self.state_flags & STEAM_UPDATING_FLAGS)

    @property
    def hachimi(self):
        return self.path / HACHIMI_DIR


def steam_roots():
    roots = []
    env = os.environ.get("UMA_STEAM_ROOT")
    if env is not None:   # для проверок: свой «Steam», настоящий (реестр, C:\Program Files) не смотрим
        return [Path(env)] if env else []
    try:
        import winreg
        for hive, sub, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    roots.append(Path(winreg.QueryValueEx(key, name)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    roots.append(Path(r"C:\Program Files (x86)\Steam"))
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def steam_libraries():
    libs, seen = [], set()

    def add(p):
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            libs.append(p)

    for root in steam_roots():
        add(root)
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            add(Path(m.group(1).replace("\\\\", "\\")))
    return libs


def parse_acf(text):
    info = {}
    for key, value in re.findall(r'"(\w+)"\s+"([^"]*)"', text):
        info.setdefault(key, value)
    return info


def is_game_dir(path):
    return path is not None and (Path(path) / GAME_EXE).is_file()


def find_game(override=None):
    """Папка игры: переопределение (аргумент, UMA_RU_GAME_DIR, настройки) -> библиотеки Steam -> типичные места на всех дисках."""
    candidates = [("аргумент", override), ("UMA_RU_GAME_DIR", os.environ.get("UMA_RU_GAME_DIR")),
                  ("настройки", load_settings().get("game_dir"))]
    for source, value in candidates:
        if value and is_game_dir(Path(value)):
            game = Game(Path(value), source=source)
            fill_manifest(game)
            return game
    for lib in steam_libraries():
        manifest = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
        try:
            info = parse_acf(manifest.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        installdir = info.get("installdir")
        if installdir and is_game_dir(lib / "steamapps" / "common" / installdir):
            return Game(lib / "steamapps" / "common" / installdir, info.get("buildid", ""),
                        int(info.get("StateFlags") or 0), "Steam")
    if os.environ.get("UMA_STEAM_ROOT") is not None:
        return None
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for sub in (r"SteamLibrary", r"Steam", r"Games\Steam", r"Program Files (x86)\Steam", r"Games\SteamLibrary"):
            p = Path(f"{letter}:\\") / sub / "steamapps" / "common" / "UmamusumePrettyDerby_Jpn"
            if is_game_dir(p):
                game = Game(p, source="поиск по дискам")
                fill_manifest(game)
                return game
    return None


def fill_manifest(game):
    """Номер сборки и состояние обновления из манифеста Steam, если игра лежит в обычной библиотеке."""
    try:
        steamapps = game.path.parent.parent
        info = parse_acf((steamapps / f"appmanifest_{APP_ID}.acf").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return
    game.buildid = info.get("buildid", "")
    game.state_flags = int(info.get("StateFlags") or 0)


def process_running(exe=GAME_EXE):
    fake = os.environ.get("UMA_RU_FAKE_RUNNING")   # только для проверок: 1 — «игра запущена», 0 — «не запущена»
    if fake in ("0", "1"):
        return fake == "1"
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH", "/FO", "CSV"], capture_output=True,
                             timeout=15, creationflags=NO_WINDOW).stdout.decode("utf-8", errors="ignore")
    except Exception:
        return False
    return f'"{exe.lower()}"' in out.lower()


def launch_game():
    if os.environ.get("UMA_RU_NO_LAUNCH"):   # проверки программы не должны запускать настоящую игру
        log.info("Запуск игры пропущен (UMA_RU_NO_LAUNCH)")
        return
    os.startfile(STEAM_URL)


# ---------------------------------------------------------------- exe и Hachimi

def pe_sections(path):
    """Имена секций PE-файла или None, если это не PE."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            if head[:2] != b"MZ":
                return None
            lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            if lfanew + 24 > len(head):
                f.seek(0)
                head = f.read(lfanew + 4096)
            if head[lfanew:lfanew + 4] != b"PE\0\0":
                return None
            nsec, = struct.unpack_from("<H", head, lfanew + 6)
            optsize, = struct.unpack_from("<H", head, lfanew + 20)
            off = lfanew + 24 + optsize
            if off + 40 * nsec > len(head):
                f.seek(0)
                head = f.read(off + 40 * nsec)
            return [head[off + 40 * i: off + 40 * i + 8].rstrip(b"\0").decode("latin1") for i in range(nsec)]
    except (OSError, struct.error):
        return None


def exe_kind(path):
    """missing | stub (оригинал Steam, секция .bind) | patched (плеер Unity после Hachimi) | unknown."""
    path = Path(path)
    if not path.is_file():
        return "missing"
    sections = pe_sections(path)
    if sections is None:
        return "unknown"
    return "stub" if ".bind" in sections else "patched"


def has_marker(path, marker=HACHIMI_MARKER):
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(4 << 20)
                if not chunk:
                    return False
                if marker in tail + chunk:
                    return True
                tail = chunk[-len(marker):]
    except OSError:
        return False


@dataclass
class HachimiState:
    kind: str       # ok | absent | stub_restored | dll_missing | foreign | no_game | unknown
    detail: str = ""


def hachimi_state(game_dir, deep=False):
    game_dir = Path(game_dir)
    kind = exe_kind(game_dir / GAME_EXE)
    dll = game_dir / PROXY_DLL
    dll_present = dll.is_file()
    dll_hachimi = dll_present and dll.stat().st_size > HACHIMI_MIN_DLL_BYTES
    if dll_hachimi and deep:
        dll_hachimi = has_marker(dll)
    if kind == "missing":
        return HachimiState("no_game", "нет файла игры")
    if kind == "stub":
        if dll_present:
            return HachimiState("stub_restored", "Steam вернул оригинальный файл запуска (так бывает после обновления игры)")
        return HachimiState("absent", "Hachimi не установлен")
    if kind == "patched":
        if dll_hachimi:
            return HachimiState("ok", "Hachimi на месте")
        if dll_present and deep:
            return HachimiState("foreign", f"{PROXY_DLL} в папке игры — не Hachimi")
        return HachimiState("dll_missing", f"нет файла {PROXY_DLL}")
    return HachimiState("unknown", "файл запуска не похож на exe")


def check_writable(folder):
    probe = Path(folder) / ".umaru-write-test"
    try:
        probe.write_bytes(b"x")
        probe.unlink()
    except PermissionError as e:
        raise NeedAdmin("В папку игры нельзя записывать без прав администратора.") from e
    except OSError as e:
        raise UmaError(f"Не удаётся записать в папку игры ({e}).") from e


def run_installer(installer, game_dir, action="install", timeout=180):
    """Официальный установщик Hachimi, без окон. -> (код выхода, вывод). 0 — готово; он молчит, о причине сбоя — только код."""
    installer = Path(installer)
    if not installer.is_file():
        raise UmaError("Рядом с лаунчером нет установщика Hachimi (data\\hachimi_installer.exe).")
    args = [str(installer), action, "--install-dir", str(game_dir), "--explicit-target", PROXY_DLL]
    log.info("Запуск установщика Hachimi: %s", " ".join(args[1:]))
    try:   # без cwd: CreateProcess не принимает рабочую папку длиннее MAX_PATH, а установщику она не нужна
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise UmaError("Установщик Hachimi завис и был остановлен.") from e
    except OSError as e:
        raise UmaError(f"Не удалось запустить установщик Hachimi ({e}). Возможно, его заблокировал антивирус.") from e
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    log.info("Установщик Hachimi: код %s %s", r.returncode, out)
    return r.returncode, out


# ---------------------------------------------------------------- пакет с переводом

@dataclass
class Pack:
    root: Path
    manifest: dict
    zip_path: Path
    installer: Path
    sources: dict = field(default_factory=dict)   # файл -> другой zip (Path) или готовые байты; остальное из zip_path

    @property
    def version(self):
        return str(self.manifest.get("version", ""))

    @property
    def entries(self):
        return self.manifest.get("entries", {})

    @property
    def total_bytes(self):
        return int(self.manifest.get("bytes", 0))

    @property
    def config_template(self):
        t = self.manifest.get("config_template")
        return t if isinstance(t, dict) else None


def load_pack(root):
    """Пакет из папки data рядом с программой; None, если его нет или он повреждён."""
    root = Path(root)
    manifest = read_json(root / "pack.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), dict):
        return None
    zip_path = root / manifest.get("zip", "pack.zip")
    if not zip_path.is_file():
        return None
    return Pack(root, manifest, zip_path, root / "hachimi_installer.exe")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def reject_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise UmaError(f"Файл или папка перевода является ссылкой: {path.name}.")


def safe_join(base, rel, *, for_write=False):
    parts = rel.split("/")
    if any(p in ("", ".", "..") or ":" in p or "\\" in p for p in parts):
        raise UmaError(f"Подозрительный путь в пакете: {rel}")
    path = Path(base).joinpath(*parts)
    if for_write:
        current = Path(base)
        reject_link(current)
        for part in parts:
            current = current / part
            reject_link(current)
        if not path.resolve().is_relative_to(Path(base).resolve()):
            raise UmaError("Путь в пакете выходит за пределы папки перевода.")
    return path


def state_path(game_dir):
    return Path(game_dir) / HACHIMI_DIR / STATE_NAME


def load_state(game_dir):
    data = read_json(state_path(game_dir))
    return data if isinstance(data, dict) else {}


def save_state(game_dir, state):
    write_json(state_path(game_dir), state, indent=1)


@dataclass
class PackStatus:
    installed: bool = False
    installed_version: str = ""
    missing: list = field(default_factory=list)
    changed: list = field(default_factory=list)   # размер или хэш не тот
    stale: list = field(default_factory=list)     # стоит другая версия пакета, файл отличается от нового
    obsolete: list = field(default_factory=list)

    @property
    def damaged(self):
        return bool(self.missing or self.changed)

    @property
    def todo(self):
        return sorted(set(self.missing) | set(self.changed) | set(self.stale))


def pack_status(game_dir, pack, deep=False, progress=None):
    """Что в папке игры расходится с пакетом. Быстро — по размерам файлов; deep — ещё и хэши (несколько секунд)."""
    base = Path(game_dir) / HACHIMI_DIR
    state = load_state(game_dir)
    st = PackStatus(installed=bool(state.get("pack_version")), installed_version=str(state.get("pack_version", "")))
    done_hashes = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    total = len(pack.entries)
    st.obsolete = sorted(done_hashes.keys() - pack.entries.keys()) if st.installed else []
    for n, (rel, (size, sha)) in enumerate(pack.entries.items(), 1):
        if st.installed and done_hashes.get(rel) != sha:
            st.stale.append(rel)   # в этой версии пакета файл новый или изменён: его надо (пере)записать, это не поломка
            continue
        try:
            p = safe_join(base, rel)
            real = p.stat().st_size
        except (OSError, UmaError):
            st.missing.append(rel)
            continue
        if real != size or (deep and sha256_file(p) != sha):
            st.changed.append(rel)
        if progress and deep and n % 100 == 0:
            progress(n / total, f"Проверка файлов перевода: {n} из {total}")
    return st


def drop_texture_cache(target, rel="", entries=()):
    """Hachimi собирает X.png из картинки игры и X.diff.png и помнит это в X.png.srchash. Если X.diff.png сменился
    (другой язык, новая версия) или убран, старую сборку надо выбросить, иначе игра покажет прежнюю картинку.
    X.png, который сам входит в пакет (entries), — не сборка, его не трогаем."""
    name = target.name
    if not name.lower().endswith(".diff.png"):
        return
    if rel and rel[:-len(".diff.png")] + ".png" in entries:
        return
    built = target.with_name(name[:-len(".diff.png")] + ".png")
    for path in (built, built.with_name(built.name + ".srchash")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def install_pack(game_dir, pack, rels, progress=None):
    """Разложить из пакета только перечисленные файлы (пишет рядом .part и заменяет, проверяя хэш). Файлы берутся из
    pack.zip или, если так указано в pack.sources, из другого архива (английские комиксы) или готовых байтов."""
    base = Path(game_dir) / HACHIMI_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise NeedAdmin("В папку игры нельзя записывать без прав администратора.") from e
    check_writable(base)
    need = sum(pack.entries[r][0] for r in rels)
    free = shutil.disk_usage(base).free
    if free < need + FREE_SPACE_MARGIN:
        raise UmaError(f"На диске мало места: нужно около {(need + FREE_SPACE_MARGIN) // 2**20} МБ, свободно {free // 2**20} МБ.")
    done = 0
    try:
        with ExitStack() as stack:
            archives = {}

            def open_source(rel):
                source = pack.sources.get(rel)
                if isinstance(source, (bytes, bytearray)):
                    return io.BytesIO(source)
                path = Path(source) if source else Path(pack.zip_path)
                if path not in archives:
                    zf = stack.enter_context(zipfile.ZipFile(path))
                    archives[path] = (zf, set(zf.namelist()))
                zf, names = archives[path]
                if rel not in names:
                    raise UmaError(f"В пакете перевода нет файла {rel}. Проверьте обновления перевода "
                                   "или переустановите лаунчер.")
                return zf.open(rel)

            for n, rel in enumerate(rels, 1):
                size, sha = pack.entries[rel]
                target = safe_join(base, rel, for_write=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                fd, temporary = tempfile.mkstemp(prefix=".umaru-", suffix=".part", dir=target.parent)
                part = Path(temporary)
                try:
                    with os.fdopen(fd, "wb") as dst, open_source(rel) as src:
                        while chunk := src.read(1 << 20):
                            h.update(chunk)
                            dst.write(chunk)
                    if h.hexdigest() != sha:
                        raise UmaError(f"Повреждён файл перевода {rel}. Повторите проверку обновлений.")
                    os.replace(part, target)
                finally:
                    part.unlink(missing_ok=True)
                drop_texture_cache(target, rel, pack.entries)
                done += size
                if progress and (n % 20 == 0 or n == len(rels)):
                    progress(done / max(1, need), f"Перевод: {n} из {len(rels)} файлов")
    except PermissionError as e:
        raise NeedAdmin("В папку игры нельзя записывать без прав администратора.") from e
    except zipfile.BadZipFile as e:
        raise UmaError("Архив перевода pack.zip повреждён. Проверьте обновления перевода "
                       "или переустановите лаунчер.") from e


# ---------------------------------------------------------------- выбор языков по разделам

CONTENT_PARTS = {           # раздел -> (название, языки по порядку показа; первый — по умолчанию)
    "texts": ("Тексты", ("ru", "ja")),
    "images": ("Надписи на картинках", ("ru", "ja")),
    "comics": ("Комиксы", ("ru", "en", "ja")),
    "movie": ("Ролик сюжета", ("ru", "ja")),
}
LANGUAGE_NAMES = {"ru": "Русский", "en": "Английский", "ja": "Японский"}
_ALWAYS_FILES = ("config.json", "info.json")     # описание перевода для Hachimi нужно при любом выборе
_IMAGE_DIRS = ("assets/textures/", "assets/atlas/", "assets/an_texture_sets/")
_TEXT_CONFIG_REL = LD_DIR + "/config.json"


def content_part(rel):
    """Раздел перевода, к которому относится файл пакета: texts / images / comics / movie;
    None — служебный файл, который ставится всегда."""
    low = rel.lower()
    prefix = LD_DIR + "/"
    if low.startswith(prefix):
        low = low[len(prefix):]
    if low in _ALWAYS_FILES:
        return None
    if low.endswith(".usm"):
        return "movie"
    if low.startswith("assets/textures/outgame/comic/") and low.rsplit("/", 1)[-1].startswith("tex_comic"):
        return "comics"
    if low.startswith(_IMAGE_DIRS):
        return "images"
    return "texts"      # словари, сюжет, реплики, песни, шрифт includes_win


def normalize_choices(choices):
    """Выбор игрока из настроек -> {раздел: язык}; неизвестное и пропущенное — язык по умолчанию (русский)."""
    choices = choices if isinstance(choices, dict) else {}
    out = {}
    for part, (_title, languages) in CONTENT_PARTS.items():
        value = choices.get(part)
        out[part] = value if value in languages else languages[0]
    return out


def load_choices():
    return normalize_choices(load_settings().get("content"))


def save_choices(choices):
    save_settings({"content": normalize_choices(choices)})


def _japanese_text_config(pack):
    """config.json перевода для японского текста. Настройки текста из пакета (перенос строк под латиницу, шрифт,
    удаление фуриганы, словари) испортили бы японский текст, поэтому остаётся только папка с картинками."""
    assets_dir = "assets"
    try:
        with zipfile.ZipFile(pack.zip_path) as zf:
            original = json.loads(zf.read(_TEXT_CONFIG_REL).decode("utf-8-sig"))
        if isinstance(original, dict) and isinstance(original.get("assets_dir"), str):
            assets_dir = original["assets_dir"]
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        pass
    return (json.dumps({"assets_dir": assets_dir}, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


def matching_extra(pack, extra):
    """Дополнительный пакет годится только к выпуску той же версии: страницы старого выпуска подменили бы новые
    и вернули бы убранные (repro_stale_comics.log)."""
    if pack is None or extra is None or extra.version != pack.version:
        return None
    return extra


def compose_pack(pack, choices=None, comics_en=None):
    """Пакет к установке по выбору игрока. Японский раздел — его файлов нет в составе (игра покажет оригинал, а уже
    поставленные файлы apply_plan уберёт). Английские комиксы: страницы из comics_en заменяют русские, остальные
    страницы пакета и так английские. Без comics_en (или если он от другого выпуска) английский выбор не применяется:
    комиксы остаются как в пакете.
    Сам пакет (pack.zip в кэше) не меняется."""
    if pack is None:
        return None
    choices = normalize_choices(choices)
    comics_en = matching_extra(pack, comics_en)
    english = choices["comics"] == "en" and comics_en is not None
    if all(lang == "ru" for lang in choices.values()):
        return pack
    entries, sources = {}, dict(pack.sources)
    for rel, spec in pack.entries.items():
        part = content_part(rel)
        lang = choices.get(part, "ru") if part else "ru"
        if part == "comics" and lang == "en":
            if english and rel in comics_en.entries:
                continue                # русская страница: вместо неё английская ниже
            lang = "ru"                 # страница и так английская (или английских нет вовсе)
        if lang == "ja":
            continue
        entries[rel] = spec
    if english:
        for rel, spec in comics_en.entries.items():
            if content_part(rel) == "comics":
                entries[rel] = spec
                sources[rel] = comics_en.zip_path
    if choices["texts"] == "ja" and _TEXT_CONFIG_REL in entries:
        data = _japanese_text_config(pack)
        entries[_TEXT_CONFIG_REL] = [len(data), hashlib.sha256(data).hexdigest()]
        sources[_TEXT_CONFIG_REL] = data
    manifest = dict(pack.manifest, entries=entries, files=len(entries), bytes=sum(v[0] for v in entries.values()),
                    content=choices)
    return Pack(pack.root, manifest, pack.zip_path, pack.installer, sources)


# ---------------------------------------------------------------- config.json Hachimi

def config_wrong_keys(game_dir):
    """Какие обязательные поля config.json не такие, как надо. None — файла нет или он повреждён."""
    cfg = read_json(Path(game_dir) / HACHIMI_DIR / CONFIG_NAME)
    if not isinstance(cfg, dict):
        return None
    return [k for k, v in REQUIRED_CONFIG.items() if cfg.get(k) != v]


def apply_config(game_dir, template=None):
    """Поставить обязательные поля, остальные настройки игрока не трогать. Первый раз — копия прежнего файла."""
    path = Path(game_dir) / HACHIMI_DIR / CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = read_json(path)
    created = False
    if not isinstance(cfg, dict):
        cfg = json.loads(json.dumps(template)) if template else {}
        created = True
    elif not (path.parent / CONFIG_BACKUP).exists() and not load_state(game_dir).get("config_ours"):
        shutil.copy2(path, path.parent / CONFIG_BACKUP)
    before = json.dumps(cfg, sort_keys=True)
    cfg.update(REQUIRED_CONFIG)
    changed = created or before != json.dumps(cfg, sort_keys=True)
    if changed:
        write_json(path, cfg, indent=2)
    return changed


# ---------------------------------------------------------------- кэши картинок

def clear_texture_caches(game_dir):
    """Удаляет кэши, которые Hachimi сам собирает из оригинала игры и нашей разницы (X.png рядом с X.diff.png и X.png.srchash).
    После обновления игры оригинальные текстуры могли измениться — кэши соберутся заново при первом показе."""
    root = Path(game_dir) / HACHIMI_DIR / LD_DIR / "assets"
    removed = 0
    if not root.is_dir():
        return 0
    for dirpath, _dirs, files in os.walk(root):
        names = set(files)
        for name in files:
            low = name.lower()
            if low.endswith(".png.srchash"):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
            elif low.endswith(".png") and not low.endswith(".diff.png"):
                stem = name[:-4]
                if f"{stem}.diff.png" in names or f"{name}.srchash" in names:
                    try:
                        os.remove(os.path.join(dirpath, name))
                        removed += 1
                    except OSError:
                        pass
    return removed


# ---------------------------------------------------------------- диагностика и починка

@dataclass
class Check:
    key: str
    state: str          # ok | warn | fail | info | off
    title: str
    detail: str = ""


@dataclass
class Context:
    game: object = None
    pack: object = None
    online: bool = False
    installer: object = None    # установщик Hachimi на случай, если пакета нет (кэш обновлений удалён или не скачан)


def keep_installed_translation(state, pack):
    """An unavailable or stale channel must never roll back a newer installation."""
    installed = str(state.get("pack_version") or "")
    if not installed:
        return False
    if pack is None:
        return True
    pattern = r"(\d{4}\.\d{2}\.\d{2}-\d{4})(\d{2})?"
    old, available = re.fullmatch(pattern, installed), re.fullmatch(pattern, pack.version)
    return bool(old and available and old[1] + (old[2] or "00") > available[1] + (available[2] or "00"))


@dataclass
class Report:
    game: object
    checks: list
    plan: list          # что надо сделать: hachimi / pack / config / caches
    mode: str           # no_game | blocked | not_installed | update | repair | ok
    blocked: str = ""   # пояснение, почему нельзя менять файлы прямо сейчас
    pack_status: object = None
    hachimi: object = None

    def check(self, key):
        return next((c for c in self.checks if c.key == key), None)


def diagnose(ctx, deep=False, progress=None):
    checks, plan = [], []
    game, pack = ctx.game, ctx.pack
    if game is None:
        checks.append(Check("game", "fail", "Игра", "Не найдена. Укажите папку, где лежит UmamusumePrettyDerby_Jpn.exe."))
        return Report(None, checks, plan, "no_game")
    state = load_state(game.path)
    first_time = not state.get("pack_version") and not state.get("installed_at")
    checks.append(Check("game", "ok", "Игра", f"{game.path}" + (f" · сборка {game.buildid}" if game.buildid else "")))

    blocked = ""
    running = process_running()
    if game.steam_updating:
        blocked = "Steam сейчас обновляет игру. Дождитесь конца обновления и откройте лаунчер снова."
        checks.append(Check("steam", "warn", "Steam", "Идёт обновление игры — подождите его конца."))
    elif running:
        blocked = "Игра запущена. Закройте её, чтобы изменить файлы."
        checks.append(Check("running", "info", "Игра запущена", "Закройте игру перед установкой и починкой."))
    prev_build = str(state.get("buildid", ""))
    build_changed = bool(prev_build and game.buildid and prev_build != game.buildid)
    if build_changed:
        checks.append(Check("build", "warn", "Игра обновилась", f"Сборка {prev_build} → {game.buildid}. Нужна проверка."))

    hs = hachimi_state(game.path, deep=deep)
    labels = {"ok": ("ok", "Hachimi", "Установлен и на месте"),
              "absent": ("fail" if not first_time else "off", "Hachimi", "Ещё не установлен"),
              "stub_restored": ("fail", "Hachimi", "Слетел после обновления игры — нужна починка"),
              "dll_missing": ("fail", "Hachimi", "Не хватает файла Hachimi — нужна починка"),
              "foreign": ("fail", "Hachimi", f"{PROXY_DLL} в папке игры — не Hachimi; его нельзя заменить автоматически"),
              "unknown": ("fail", "Hachimi", "Файл запуска игры не распознан"),
              "no_game": ("fail", "Hachimi", "Нет файла игры")}
    st, title, detail = labels[hs.kind]
    checks.append(Check("hachimi", st, title, detail))
    if hs.kind in ("absent", "stub_restored", "dll_missing"):
        plan.append("hachimi")

    ps = None
    retained = ctx.online and keep_installed_translation(state, pack)
    if retained:
        checks.append(Check("pack", "info", "Перевод", f"Установлен · версия {state['pack_version']}"))
    elif pack is None:
        message = ("Ожидаю загрузки выпуска из канала обновлений." if ctx.online else
                   "Рядом с лаунчером нет папки data. Распакуйте архив целиком и запустите UmaRU.exe из этой папки.")
        checks.append(Check("pack", "warn" if ctx.online else "fail", "Перевод", message))
    else:
        ps = pack_status(game.path, pack, deep=deep, progress=progress)
        if first_time or not ps.installed:
            checks.append(Check("pack", "off", "Перевод", f"Ещё не установлен · версия {pack.version}"))
            plan.append("pack")
        elif ps.damaged:
            what = []
            if ps.missing:
                what.append(f"не хватает {len(ps.missing)}")
            if ps.changed:
                what.append(f"изменено {len(ps.changed)}")
            checks.append(Check("pack", "fail", "Перевод", "Файлы повреждены или удалены: " + ", ".join(what) + " — починим."))
            plan.append("pack")
        elif ps.installed_version != pack.version:
            checks.append(Check("pack", "info", "Перевод", f"Есть новая версия: {ps.installed_version} → {pack.version}"))
            plan.append("pack")
        elif ps.stale or ps.obsolete:
            count = len(ps.stale) + len(ps.obsolete)
            checks.append(Check("pack", "info", "Перевод", f"Нужно применить языки ({count} файлов)"))
            plan.append("pack")
        else:
            checks.append(Check("pack", "ok", "Перевод", f"Установлен · версия {pack.version}"))
    wrong = config_wrong_keys(game.path)
    if wrong is None:
        checks.append(Check("config", "off" if first_time else "warn", "Настройки Hachimi", "Файла настроек ещё нет — создадим."))
        plan.append("config")
    elif wrong:
        checks.append(Check("config", "warn", "Настройки Hachimi", "Сбиты настройки перевода — вернём: " + ", ".join(wrong)))
        plan.append("config")
    if build_changed or deep:
        plan.append("caches")

    core = [p for p in plan if p != "caches"]
    if not is_game_dir(game.path):
        mode = "no_game"
    elif game.steam_updating:
        mode = "blocked"
    elif running and (core or first_time or build_changed):
        mode = "blocked"
    elif retained:
        usable = hs.kind == "ok" and wrong == [] and (game.hachimi / LD_DIR / "config.json").is_file()
        mode = "installed_only" if usable else "need_channel"
        plan = []
    elif ctx.online and pack is None:
        mode = "need_channel"
        plan = []
    elif first_time:
        mode = "not_installed"
    elif core:
        mode = "update" if (core == ["pack"] and ps is not None and not ps.damaged and ps.installed) else "repair"
    elif build_changed:
        mode = "repair"
    else:
        mode = "ok"
    return Report(game, checks, plan, mode, blocked, ps, hs)


@contextmanager
def game_write_lock(game_dir):
    """Serialize writers for this game, including the elevated master and CLI."""
    key = hashlib.sha256(os.path.normcase(str(Path(game_dir).resolve())).encode('utf-8')).hexdigest()
    busy = "Другая копия лаунчера уже меняет файлы этой игры. Дождитесь завершения и повторите."
    if os.name != 'nt':
        import fcntl
        with open(Path(tempfile.gettempdir()) / ('umaru-' + key + '.lock'), 'a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UmaError(busy) from exc
            yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, 'Global\\UmaRU.Player.Write.' + key)
    if not handle:
        raise UmaError(busy)
    try:
        if kernel.WaitForSingleObject(handle, 1500) not in (0, 0x80):
            raise UmaError(busy)
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
    finally:
        kernel.CloseHandle(handle)


def apply_plan(ctx, plan, progress=None, deep=False):
    """Выполняет план диагностики. Возвращает список строк «что сделано». «Отказано в доступе» из любого шага — NeedAdmin."""
    try:
        if ctx.game is None:
            raise UmaError("Игра не найдена.")
        with game_write_lock(ctx.game.path):
            reject_link(ctx.game.hachimi)
            return _apply_plan(ctx, plan, progress, deep)
    except PermissionError as e:
        raise NeedAdmin("В папку игры нельзя записывать без прав администратора.") from e


def _apply_plan(ctx, plan, progress=None, deep=False):
    game, pack = ctx.game, ctx.pack
    if game is None:
        raise UmaError("Игра не найдена.")
    fill_manifest(game)
    if game.steam_updating:
        raise UmaError("Steam обновляет игру. Повторите после завершения загрузки в Steam.")
    if process_running():
        raise UmaError("Игра запущена. Закройте её и повторите.")
    if ctx.online and (pack is None or keep_installed_translation(load_state(game.path), pack)):
        raise UmaError("Пока не удалось получить подходящий выпуск. Повторите проверку обновлений.")
    done = []

    def report(frac, text):
        if progress:
            progress(max(0.0, min(1.0, frac)), text)

    weights = {"hachimi": 0.12, "pack": 0.72, "config": 0.04, "caches": 0.12}
    spans, start = {}, 0.0
    steps = [s for s in ("hachimi", "pack", "config", "caches") if s in plan]
    total_w = sum(weights[s] for s in steps) or 1.0
    for s in steps:
        spans[s] = (start / total_w, (start + weights[s]) / total_w)
        start += weights[s]
    state = load_state(game.path)

    if "hachimi" in steps:
        a, b = spans["hachimi"]
        report(a, "Устанавливаем Hachimi…")
        if pack is None or not pack.installer.is_file():
            raise UmaError("Нет установщика Hachimi рядом с лаунчером (data\\hachimi_installer.exe). "
                           "Переустановите лаунчер.")
        check_writable(game.path)
        exe, old = game.path / GAME_EXE, game.path / OLD_EXE
        stub_copy = None
        if exe_kind(exe) == "stub":   # новая заглушка Steam: установщик оставляет прежний .old.exe, а удаление вернёт именно его
            stub_copy = game.path / (GAME_EXE + ".umaru-stub")
            shutil.copy2(exe, stub_copy)
        try:
            code, out = run_installer(pack.installer, game.path, "install")
            hs = hachimi_state(game.path, deep=True)
            if code != 0 or hs.kind != "ok":
                raise UmaError(f"Установщик Hachimi не справился (код {code}, состояние: {hs.kind}). "
                               "Проверьте, что игра закрыта, а антивирус не блокирует файлы в папке игры.")
            if stub_copy is not None and (not old.is_file() or sha256_file(old) != sha256_file(stub_copy)):
                shutil.copy2(stub_copy, old)
        finally:
            if stub_copy is not None:
                stub_copy.unlink(missing_ok=True)
        done.append("Hachimi установлен заново")
        report(b, "Hachimi готов")
    if "pack" in steps and pack is not None:
        a, b = spans["pack"]
        ps = pack_status(game.path, pack, deep=deep or not state.get("pack_version"))
        rels = ps.todo
        if not state.get("pack_version"):
            rels = sorted(pack.entries)   # первая установка: ставим всё, даже если часть уже лежит (старые файлы чужие)
        report(a, f"Раскладываем перевод: {len(rels)} файлов…")
        if rels:
            install_pack(game.path, pack, rels, lambda f, t: report(a + (b - a) * f, t))
        gone = [rel for rel in (state.get("entries") or {}) if rel not in pack.entries]
        for rel in gone:   # файлы, которых в новой версии пакета больше нет (ставили мы же)
            try:
                target = safe_join(game.hachimi, rel, for_write=True)
                if ctx.online and (not rel.startswith(LD_DIR + "/") or sha256_file(target) != state['entries'][rel]):
                    continue
                target.unlink()
                drop_texture_cache(target, rel, pack.entries)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise UmaError(f"Не удалось убрать прежний файл {rel}. Закройте игру и повторите обновление.") from exc
        state["entries"] = {rel: sha for rel, (_size, sha) in pack.entries.items()}
        state["pack_version"] = pack.version
        done.append(f"Перевод: записано {len(rels)} файлов (версия {pack.version})")
        report(b, "Перевод готов")
    if "config" in steps:
        a, b = spans["config"]
        report(a, "Настройки Hachimi…")
        if apply_config(game.path, pack.config_template if pack else None):
            done.append("Настройки Hachimi возвращены")
        state["config_ours"] = True
        report(b, "")
    if "caches" in steps:
        a, b = spans["caches"]
        report(a, "Очищаем кэш картинок…")
        n = clear_texture_caches(game.path)
        if n:
            done.append(f"Кэш картинок очищен ({n} файлов): игра соберёт его заново")
        report(b, "")
    state["buildid"] = game.buildid
    state["installed_at"] = state.get("installed_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_repair"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(game.path, state)
    report(1.0, "Готово")
    return done


def uninstall(ctx, remove_translation=True, progress=None):
    try:
        if ctx.game is None:
            raise UmaError("Игра не найдена.")
        with game_write_lock(ctx.game.path):
            reject_link(ctx.game.hachimi)
            return _uninstall(ctx, remove_translation, progress)
    except PermissionError as e:
        raise NeedAdmin("В папку игры нельзя записывать без прав администратора.") from e


def _uninstall(ctx, remove_translation=True, progress=None):
    game, pack = ctx.game, ctx.pack
    if game is None:
        raise UmaError("Игра не найдена.")
    if process_running():
        raise UmaError("Игра запущена. Закройте её и повторите.")
    done = []
    installer = pack.installer if pack else ctx.installer
    hs = hachimi_state(game.path)
    exe, old, dll = game.path / GAME_EXE, game.path / OLD_EXE, game.path / PROXY_DLL
    if hs.kind == "ok":
        if installer is not None and Path(installer).is_file():
            code, _out = run_installer(installer, game.path, "uninstall")   # вернёт заглушку Steam на место exe
            done.append("Hachimi удалён" if code == 0 else f"Установщик Hachimi вернул код {code}")
        else:
            done.append("Hachimi остался в папке игры: рядом с лаунчером нет установщика Hachimi")
    elif hs.kind in ("stub_restored", "dll_missing"):
        # exe уже оригинальный (после обновления игры) или Hachimi без библиотеки: убираем остатки сами. Старый .old.exe при
        # обновлении устарел — выбросить, а не возвращать поверх нового exe.
        if hs.kind == "dll_missing" and old.is_file():
            os.replace(old, exe)
        else:
            old.unlink(missing_ok=True)
        if dll.is_file() and dll.stat().st_size > HACHIMI_MIN_DLL_BYTES:
            dll.unlink(missing_ok=True)
        done.append("Остатки Hachimi убраны")
    appid = game.path / APPID_FILE
    try:
        if appid.is_file() and appid.read_text(encoding="utf-8", errors="ignore").strip() == APP_ID:
            appid.unlink()
    except OSError:
        pass
    base = game.hachimi
    if remove_translation:
        shutil.rmtree(base / LD_DIR, ignore_errors=True)
        done.append("Перевод удалён")
        backup = base / CONFIG_BACKUP
        cfg = base / CONFIG_NAME
        if backup.is_file():
            shutil.copy2(backup, cfg)
            backup.unlink()
            done.append("Прежние настройки Hachimi возвращены")
        elif cfg.is_file():
            try:
                cfg.unlink()
            except OSError:
                pass
        state_path(game.path).unlink(missing_ok=True)
        (game.path / "hachimi.log").unlink(missing_ok=True)   # журнал Hachimi, если его включали
        try:
            base.rmdir()      # папка hachimi опустела — убираем; если в ней есть чужое, rmdir откажет и всё остаётся
        except OSError:
            pass
    return done


# ---------------------------------------------------------------- ярлык и отчёт

def desktop_dir():
    env = os.environ.get("UMA_RU_DESKTOP")
    if env:
        return Path(env)
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:  # CSIDL_DESKTOPDIRECTORY
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Desktop"


def create_shortcut(target, arguments, name, icon=None):
    """Ярлык на рабочем столе (через PowerShell, без окна). -> путь к .lnk."""
    lnk = desktop_dir() / f"{name}.lnk"
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:UMARU_LNK);"
              "$s.TargetPath=$env:UMARU_TARGET;$s.Arguments=$env:UMARU_ARGS;"
              "$s.WorkingDirectory=(Split-Path $env:UMARU_TARGET);$s.IconLocation=$env:UMARU_ICON;"
              "$s.Description='Uma RU: русификатор Umamusume';$s.Save()")
    env = dict(os.environ, UMARU_LNK=str(lnk), UMARU_TARGET=str(target), UMARU_ARGS=arguments,
               UMARU_ICON=str(icon or target))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                       env=env, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    if r.returncode != 0 or not lnk.is_file():
        raise UmaError("Не удалось создать ярлык: " + r.stderr.decode("utf-8", errors="replace").strip()[:200])
    return lnk


# ---------------------------------------------------------------- полное удаление (вместе с самой программой)

def remove_shortcut(name, exe_name=None):
    """Убирает с рабочего стола ярлык «name.lnk». Если задано exe_name, ярлык должен вести на программу с таким именем
    файла: чужой ярлык с тем же названием не трогаем. -> True, если ярлык был и удалён."""
    lnk = desktop_dir() / f"{name}.lnk"
    try:
        if not lnk.is_file():
            return False
        if exe_name:
            data = lnk.read_bytes().lower()
            needle = exe_name.lower()
            if needle.encode("utf-16-le") not in data and needle.encode("utf-8") not in data:
                return False
        lnk.unlink()
    except OSError:
        return False
    return True


def program_items(program_dir, exe_path, pack=None):
    """Что в папке программы принадлежит ей: (файлы и папки на удаление, папки, которые убрать, если они опустеют,
    чужие файлы, которые остаются). Удаляем только известное по именам, а не всю папку целиком: если рядом с программой
    лежат файлы самого игрока (или программу случайно распаковали в папку с другими файлами), они останутся нетронутыми."""
    root = Path(program_dir)
    items, dirs = [], []
    exe = Path(exe_path)
    if exe.parent == root and exe.is_file():
        items.append(exe)
    internal = root / "_internal"
    if (internal / "base_library.zip").is_file() or (internal / "python3.dll").is_file():   # папка PyInstaller
        items.append(internal)
    data_dir = pack.root if pack is not None else root / "data"
    if pack is not None:
        items += [p for p in (pack.zip_path, data_dir / "pack.json", pack.installer) if p.is_file()]
    else:
        items += [p for p in (data_dir / "pack.zip", data_dir / "pack.json", data_dir / "hachimi_installer.exe") if p.is_file()]
    readme = root / "ПРОЧТИ МЕНЯ.txt"
    if readme.is_file():
        items.append(readme)
    dirs += [data_dir, root]
    mine = {os.path.normcase(str(p)) for p in items + dirs}
    left = []
    for folder in (data_dir, root):
        try:
            for p in folder.iterdir():
                if os.path.normcase(str(p)) not in mine:
                    left.append(p)
        except OSError:
            pass
    return items, dirs, left


_WAIT_FOR_PID = ("$ErrorActionPreference='SilentlyContinue';"
                 "$p=[int]$env:UMARU_WAIT_PID;"
                 "if($p -gt 0){for($i=0;$i -lt 1500;$i++){if(-not (Get-Process -Id $p)){break};Start-Sleep -Milliseconds 200}};"
                 "Start-Sleep -Milliseconds 500;")


def _spawn_hidden_powershell(script, env):
    """Скрытый PowerShell, который переживёт закрытие программы. Пути передаются через переменные окружения env."""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-Command", script]
    base = NO_WINDOW | 0x200                    # CREATE_NEW_PROCESS_GROUP
    kwargs = dict(env=env, cwd=os.environ.get("TEMP") or None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    try:
        subprocess.Popen(cmd, creationflags=base | 0x01000000, **kwargs)    # CREATE_BREAKAWAY_FROM_JOB: пережить закрытие запустившего
    except OSError:
        subprocess.Popen(cmd, creationflags=base, **kwargs)                 # задание не разрешает отделяться — без этого флага


def find_uninstaller(program_dir):
    """Деинсталлятор Inno Setup (unins000.exe рядом с unins000.dat), если программа установлена установщиком, иначе None."""
    try:
        for exe in sorted(Path(program_dir).glob("unins???.exe")):
            if exe.is_file() and exe.with_suffix(".dat").is_file():
                return exe
    except OSError:
        pass
    return None


def schedule_uninstaller(uninstaller, pid=None):
    """Отдельный скрытый процесс: ждёт, пока завершится процесс pid, и запускает деинсталлятор Inno Setup без повторного
    вопроса (/SILENT: окно с ходом удаления видно). Он сам уберёт программу, ярлыки, запись в «Установке и удалении
    программ» и %LOCALAPPDATA%\\UmaRU. Возвращает сразу."""
    script = _WAIT_FOR_PID + "Start-Process -FilePath $env:UMARU_UNINSTALLER -ArgumentList '/SILENT'"
    _spawn_hidden_powershell(script, dict(os.environ, UMARU_WAIT_PID=str(pid or 0), UMARU_UNINSTALLER=str(uninstaller)))


def schedule_program_removal(plan):
    """Выполнить план удаления программы из uninstall_all(): через деинсталлятор или удалением файлов по списку."""
    if plan.get("uninstaller"):
        schedule_uninstaller(plan["uninstaller"], plan.get("pid"))
    else:
        schedule_removal(plan["items"], plan.get("dirs", ()), plan.get("pid"))


def schedule_removal(items, dirs=(), pid=None):
    """Отдельный скрытый процесс: ждёт, пока завершится процесс pid (сама программа — пока она работает, Windows не даёт
    удалить её exe), удаляет items, затем пустые папки из dirs. Пути передаются через переменные окружения, а не в тексте
    команды. Возвращает сразу."""
    script = (_WAIT_FOR_PID +
              "$items=@($env:UMARU_ITEMS -split '\\|' | Where-Object {$_});"
              "$dirs=@($env:UMARU_DIRS -split '\\|' | Where-Object {$_});"
              "for($t=0;$t -lt 40;$t++){$left=0;"
              "foreach($x in $items){if(Test-Path -LiteralPath $x){Remove-Item -LiteralPath $x -Recurse -Force;"
              "if(Test-Path -LiteralPath $x){$left++}}};"
              "if($left -eq 0){break};Start-Sleep -Milliseconds 750};"
              "foreach($d in $dirs){if((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force | "
              "Select-Object -First 1)){Remove-Item -LiteralPath $d -Force}}")
    env = dict(os.environ, UMARU_WAIT_PID=str(pid or 0), UMARU_ITEMS="|".join(str(p) for p in items),
               UMARU_DIRS="|".join(str(p) for p in dirs))
    _spawn_hidden_powershell(script, env)


def uninstall_all(ctx, program=None, progress=None):
    """Полное удаление: Hachimi и перевод из папки игры (как uninstall), ярлык на рабочем столе и, если задан program,
    сама программа с её настройками и журналом. Саму программу удаляет не эта функция, а отдельный процесс после выхода:
    функция только готовит план и возвращает его вторым значением, а вызывающий запускает
    schedule_program_removal(план), когда окно уже можно закрывать.

    program = {"dir": папка программы, "exe": путь к её exe, "shortcut": имя ярлыка, "pid": номер процесса программы,
    "uninstaller": unins000.exe или None} или None (программу оставить: убрать только то, что в игре и на рабочем столе).
    Если программа поставлена установщиком, её удаляет его деинсталлятор: иначе в Windows остались бы запись
    в «Установке и удалении программ», ярлык в «Пуске» и unins000.exe.
    -> (список «что сделано», план удаления программы или None)"""
    done = []
    if ctx.game is not None:
        done += uninstall(ctx, True, progress)      # раньше всего: если игра запущена или нужны права, ничего не тронуто
    else:
        done.append("Игра не найдена, в её папке ничего не менялось")
    if program is None:
        return done, None
    if remove_shortcut(program.get("shortcut") or "", Path(program["exe"]).name):
        done.append("Ярлык убран с рабочего стола")
    for h in list(logging.getLogger("umaru").handlers):
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    pid = program.get("pid") or os.getpid()
    if program.get("uninstaller"):
        done.append("Лаунчер, его настройки и кэш перевода удалит деинсталлятор Windows сразу после закрытия окна")
        return done, {"uninstaller": Path(program["uninstaller"]), "pid": pid}
    items, dirs, left = program_items(program["dir"], program["exe"], ctx.pack)
    items.append(app_home())                         # настройки и журнал: журнал открыт у нас, поэтому удалит помощник
    done.append("Лаунчер и его настройки будут удалены сразу после закрытия окна")
    if left:
        done.append("В папке лаунчера лежат чужие файлы, они остаются: " + ", ".join(sorted(p.name for p in left)[:5]))
    return done, {"items": items, "dirs": dirs, "pid": pid}


def diagnostics_text(report, pack=None):
    lines = [f"Лаунчер Uma RU · {time.strftime('%Y-%m-%d %H:%M:%S')}", f"Windows: {sys.getwindowsversion().major}."
             f"{sys.getwindowsversion().minor}.{sys.getwindowsversion().build}" if hasattr(sys, "getwindowsversion") else ""]
    if pack:
        lines.append(f"Пакет перевода: {pack.version}")
    try:   # часовой пояс и время ПК: часть ошибок связи игры зависит от расхождения с японским временем
        off = time.localtime().tm_gmtoff or 0
        lines.append(f"Часовой пояс ПК: UTC{'+' if off >= 0 else '-'}{abs(off) // 3600:02d}:{abs(off) % 3600 // 60:02d} ({time.tzname[0]})")
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"Режим: {report.mode}; план: {', '.join(report.plan) or '—'}")
    for c in report.checks:
        lines.append(f"[{c.state}] {c.title}: {c.detail}")
    if report.game is not None:
        g = report.game
        lines.append(f"Игра: {g.path} (сборка {g.buildid}, флаги {g.state_flags}, найдена: {g.source})")
        for name in (GAME_EXE, OLD_EXE, PROXY_DLL, APPID_FILE):
            p = g.path / name
            lines.append(f"  {name}: " + (f"{p.stat().st_size} байт" if p.is_file() else "нет"))
    return "\n".join(x for x in lines if x)

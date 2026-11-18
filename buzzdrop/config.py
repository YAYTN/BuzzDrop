"""Per-OS paths, settings and transfer history."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import APP_NAME
from .util import IS_MAC, IS_WIN, FileLock

log = logging.getLogger("buzz")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """Where settings, history and logs live.

    ``BUZZ_DATA_DIR`` overrides it; a ``portable`` file next to a packaged
    executable makes the app keep everything in ``./data`` (USB-stick mode).
    """
    override = os.environ.get("BUZZ_DATA_DIR")
    if override:
        return Path(override)
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable").exists() or (exe_dir / "portable.txt").exists():
            return exe_dir / "data"
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "buzzdrop"


def _win_known_downloads() -> Optional[Path]:
    """The real Downloads folder, even if the user moved it (not always %USERPROFILE%\\Downloads)."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
        guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        buf = ctypes.c_void_p()
        if fn(ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0 and buf.value:
            try:
                return Path(ctypes.wstring_at(buf.value))
            finally:
                ole32.CoTaskMemFree(buf)
    except Exception:
        pass
    return None


def _xdg_downloads() -> Optional[Path]:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                if value and value.rstrip("/") != str(Path.home()):
                    return Path(value)
    except OSError:
        pass
    return None


def default_downloads_dir() -> Path:
    found = _win_known_downloads() if IS_WIN else (None if IS_MAC else _xdg_downloads())
    return found or Path.home() / "Downloads"


# --------------------------------------------------------------------------
# Atomic JSON
# --------------------------------------------------------------------------
def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:  # -sig: Notepad adds a BOM when you edit
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return default


def write_json(path: Path, data: Any) -> bool:
    """Write via temp file + ``os.replace`` with retries (Windows AV/indexers lock files briefly)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    text = json.dumps(data, indent=2, ensure_ascii=False)
    for attempt in range(6):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            if attempt == 5:
                log.error("Could not save %s: %s", path, exc)
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    # Receiving
    "download_folder": "",               # "" → the OS Downloads folder
    "auto_extract_zip": True,
    "delete_zip_after_extract": True,    # fewer files lying around; the ZIP is kept if extracting fails
    "launch_folder_after_download": True,
    "watch_clipboard": True,             # offer to download links you copy
    "open_blocked_in_browser": False,    # on a BuzzHeavier browser check: open the page automatically
    # Sending
    "copy_link": True,
    "link_style": "bz",                  # "bz" → BZ://code, "https" → https://buzzheavier.com/code
    "bundle_multiple": False,            # several files → one ZIP / one link
    "split_mb": 0,                       # optional: >0 → send files bigger than this in parts of this size
    # General
    "history_keep_days": 0,              # privacy, opt-in: 0 keep forever · -1 forget on quit · N>0 forget entries older than N days
    "theme": "system",                   # system | dark | light
    "accent": "teal",                    # teal | blue | violet | rose | amber | green
    "palette": "default",                # default | ocean | neutral | warm | pure (background colours)
    "clear_clipboard_s": 0,              # >0: wipe the link BuzzDrop copied after this many seconds
    "notifications": True,
    "check_for_updates": True,
    "auto_update": False,                # download updates in the background; they install when you next quit
    "skipped_version": "",               # a version the user chose not to be reminded about
    "max_parallel": 2,
    # Windows extras
    "enable_global_hotkey": True,
    "global_hotkey": "Ctrl+Shift+F1",
    "run_in_background": False,          # tray icon + start at login
    # Internal
    "integration_setup_done": False,
    "window": None,                      # [w, h, x, y]
    "last_update_check": 0,
}


def settings_path() -> Path:
    return data_dir() / "config.json"


def load_settings() -> Dict[str, Any]:
    raw = read_json(settings_path(), {})
    if not isinstance(raw, dict):
        raw = {}
    merged = dict(DEFAULTS)
    merged.update(raw)
    if merged.get("theme") not in ("system", "dark", "light"):
        merged["theme"] = "system"
    if merged.get("link_style") not in ("bz", "https"):
        merged["link_style"] = "bz"
    try:
        merged["max_parallel"] = max(1, min(6, int(merged.get("max_parallel") or 2)))
    except (TypeError, ValueError):
        merged["max_parallel"] = 2
    return merged


def save_settings(settings: Dict[str, Any]) -> bool:
    return write_json(settings_path(), settings)


def download_dir(settings: Dict[str, Any]) -> Path:
    custom = (settings.get("download_folder") or "").strip()
    return Path(os.path.expanduser(custom)) if custom else default_downloads_dir()


# --------------------------------------------------------------------------
# History (shared by GUI and CLI, merged under a file lock)
# --------------------------------------------------------------------------
MAX_HISTORY = 500


class History:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or data_dir() / "history.json"
        self.items: List[Dict[str, Any]] = self._read()

    def _read(self) -> List[Dict[str, Any]]:
        data = read_json(self.path, [])
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def reload(self) -> None:
        self.items = self._read()

    def add(self, kind: str, name: str, code: str, size: int = 0, path: str = "") -> Dict[str, Any]:
        entry = {"kind": kind, "name": name, "code": code, "size": int(size or 0),
                 "time": time.time(), "path": path}
        with FileLock(self.path.with_suffix(".lock"), timeout=3):
            items = self._read()
            items.insert(0, entry)
            self.items = items[:MAX_HISTORY]
            write_json(self.path, self.items)
        return entry

    def remove(self, entry: Dict[str, Any]) -> None:
        with FileLock(self.path.with_suffix(".lock"), timeout=3):
            items = [i for i in self._read()
                     if not (i.get("time") == entry.get("time") and i.get("code") == entry.get("code"))]
            self.items = items
            write_json(self.path, items)

    def clear(self) -> None:
        with FileLock(self.path.with_suffix(".lock"), timeout=3):
            self.items = []
            write_json(self.path, [])

    def older_than(self, days: float, now: Optional[float] = None) -> int:
        """How many entries are older than *days* days (entries without a valid time are never counted)."""
        cutoff = (time.time() if now is None else now) - days * 86400
        return sum(1 for i in self._read() if isinstance(i.get("time"), (int, float)) and i["time"] < cutoff)

    def prune(self, days: float, now: Optional[float] = None) -> int:
        """Forget entries older than *days* days. Returns how many were removed.

        Only this list changes: downloaded files and uploaded links are not touched.
        """
        cutoff = (time.time() if now is None else now) - days * 86400
        with FileLock(self.path.with_suffix(".lock"), timeout=3):
            items = self._read()
            kept = [i for i in items if not isinstance(i.get("time"), (int, float)) or i["time"] >= cutoff]
            removed = len(items) - len(kept)
            if removed:
                write_json(self.path, kept)
            self.items = kept
        return removed


def apply_history_policy(history: History, settings: Dict[str, Any], closing: bool = False) -> int:
    """Apply the opt-in "forget my history automatically" setting. Returns how many entries were forgotten.

    ``history_keep_days``: 0 = keep forever (default); N > 0 = forget entries older than N days;
    -1 = forget everything when BuzzDrop quits (only acts when *closing* is true).
    """
    try:
        keep = int(settings.get("history_keep_days") or 0)
    except (TypeError, ValueError):
        return 0
    if keep == 0:
        return 0
    if keep < 0:
        if not closing:
            return 0
        history.reload()
        count = len(history.items)
        if count:
            history.clear()
        return count
    return history.prune(keep)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> Path:
    log_dir = data_dir() / "logs"
    root = logging.getLogger("buzz")
    if root.handlers:
        return log_dir
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "buzzdrop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(process)d] %(message)s"))
        root.addHandler(handler)
    except OSError:
        root.addHandler(logging.NullHandler())
    return log_dir

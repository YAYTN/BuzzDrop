"""Dependency-free helpers shared by the GUI, the CLI and the transfer engine.

* link parsing (``BZ://code``, buzzheavier.com URLs, bare codes)
* human-friendly sizes / durations
* cross-platform safe file names and collision-free paths
* streaming ZIP packing and zip-slip-proof extraction
* opening files / folders / URLs with the OS
* a tiny cross-process file lock
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
import zipfile
from email.message import Message
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

ProgressFn = Callable[[int, int], None]

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = not (IS_WIN or IS_MAC)

COPY_CHUNK = 1 << 20  # 1 MiB


class Cancelled(Exception):
    """Raised inside long operations when the user cancels."""


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


class StopFlag(threading.Event):
    """A cancel flag that can also be paused. Everything lives in memory; nothing is written anywhere.

    Long loops already ask ``cancel.is_set()``; the ones that can pause (the upload body, the download
    read loop, the gap between split parts) also call :func:`checkpoint`, which waits here while paused.
    ``pauses`` counts how often it was paused, so a retry caused by a long pause (the server hung up on an
    idle connection) can be recognised and not counted as a real failure.
    """

    def __init__(self) -> None:
        super().__init__()
        self._running = threading.Event()
        self._running.set()
        self.pauses = 0

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        if self._running.is_set():
            self.pauses += 1
            self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def set(self) -> None:          # cancelling must also release a paused transfer
        super().set()
        self._running.set()

    def wait_if_paused(self) -> bool:
        """Block while paused (or until cancelled). Returns True if it had to wait."""
        waited = False
        while not self._running.is_set() and not super().is_set():
            waited = True
            self._running.wait(0.1)
        return waited


def checkpoint(flag) -> bool:
    """Wait here while *flag* is paused. Plain ``threading.Event`` flags (CLI, tests) never pause."""
    wait = getattr(flag, "wait_if_paused", None)
    return bool(wait()) if wait else False


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------
_CODE = r"[a-z0-9]{8,32}"
# Paths on buzzheavier.com that look like codes but aren't.
_NOT_CODES = {
    "developers", "settings", "register", "dashboard", "password", "download",
    "downloads", "uploads", "account", "privacy", "contact", "pricing", "premium",
    "loginpage", "signup00",
}
_BZ_RE = re.compile(r"(?i)\bbz:/{0,2}[ \t]*(" + _CODE + r")(?![a-z0-9])")
_URL_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:www\.)?(?:buzzheavier\.com|bzzhr\.co)/(" + _CODE + r")(?![a-z0-9])"
)
_BARE_RE = re.compile(r"(?i)^" + _CODE + r"$")


def find_codes(text: str) -> List[str]:
    """Every BuzzHeavier code linked in *text* (``BZ://…`` or a URL), in order, de-duplicated.

    Bare words are ignored here so that random text never triggers a download.
    """
    if not text:
        return []
    hits: List[Tuple[int, str]] = []
    for rx in (_BZ_RE, _URL_RE):
        hits.extend((m.start(), m.group(1).lower()) for m in rx.finditer(text))
    hits.sort()
    out: List[str] = []
    for _, code in hits:
        if code not in out and code not in _NOT_CODES:
            out.append(code)
    return out


def parse_codes(text: str) -> List[str]:
    """Like :func:`find_codes`, but also accepts input that is *only* bare codes.

    Used for things the user typed or pasted on purpose (the receive box, CLI).
    """
    codes = find_codes(text)
    if codes:
        return codes
    tokens = [t for t in re.split(r"[\s,;]+", (text or "").strip()) if t]
    if tokens and all(_BARE_RE.match(t) for t in tokens):
        return [c for c in dict.fromkeys(t.lower() for t in tokens) if c not in _NOT_CODES]
    return []


def bz_link(code: str) -> str:
    return "BZ://" + code


def web_link(code: str, base: str = "https://buzzheavier.com") -> str:
    return base.rstrip("/") + "/" + code


def share_link(code: str, style: str = "bz") -> str:
    """The link users share: ``BZ://code`` (opens the app) or an https URL (clickable everywhere)."""
    return web_link(code) if style == "https" else bz_link(code)


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def format_size(n: float) -> str:
    n = float(max(n or 0, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            return ("%.1f %s" if n < 100 else "%.0f %s") % (n, unit)
        n /= 1024.0
    return "%.0f TB" % n  # pragma: no cover


def format_rate(bps: float) -> str:
    return format_size(bps) + "/s"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return ""
    s = int(round(seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, (s % 3600) // 60)


def format_ago(ts: float, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    d = max(0, now - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return "%d min ago" % (d // 60)
    if d < 86400:
        h = int(d // 3600)
        return "%d hour%s ago" % (h, "" if h == 1 else "s")
    if d < 2 * 86400:
        return "yesterday"
    if d < 7 * 86400:
        return "%d days ago" % (d // 86400)
    return time.strftime("%d %b %Y", time.localtime(ts))


# --------------------------------------------------------------------------
# File names & paths
# --------------------------------------------------------------------------
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(10)} | {"LPT%d" % i for i in range(10)}
_MAX_NAME_BYTES = 240


def safe_filename(name: Optional[str], fallback: str = "download") -> str:
    """Make *name* a valid file name on every OS while keeping it readable.

    Only characters that are actually illegal are replaced (``[ ] & ' + , ~`` and
    the like are kept). Reserved Windows names, trailing dots and
    over-long names are handled; the extension is preserved when truncating.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # never allow a path component
    name = _ILLEGAL.sub("_", name).strip().rstrip(". ")
    if not name or set(name) <= {"."}:
        name = fallback
    if name.split(".", 1)[0].upper() in _WIN_RESERVED:
        name = "_" + name
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        root, ext = os.path.splitext(name)
        ext = ext if len(ext.encode("utf-8")) <= 20 else ""
        budget = _MAX_NAME_BYTES - len(ext.encode("utf-8"))
        root = root.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(". ")
        name = (root or fallback) + ext
    return name


def unique_path(path: Path) -> Path:
    """Return *path*, or ``name (1).ext``, ``name (2).ext`` … if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    if path.is_dir():
        stem, suffix = path.name, ""
    elif stem.lower().endswith(".tar"):  # keep .tar.gz together
        stem, suffix = stem[:-4], ".tar" + suffix
    for i in range(1, 100000):
        candidate = path.with_name("%s (%d)%s" % (stem, i, suffix))
        if not candidate.exists():
            return candidate
    raise FileExistsError(str(path))  # pragma: no cover


def filename_from_disposition(value: Optional[str]) -> Optional[str]:
    """Extract the file name from a Content-Disposition header (RFC 6266 / 5987)."""
    if not value:
        return None
    name = None
    # RFC 6266: the encoded filename* wins over a plain filename= fallback.
    star = re.search(r"filename\*\s*=\s*\"?([^\"';]*)'[^']*'([^\";]+)", value, re.I)
    if star:
        try:
            name = unquote(star.group(2).strip(), encoding=star.group(1) or "utf-8", errors="strict")
        except (LookupError, UnicodeDecodeError):
            name = None
    if not name:
        try:
            msg = Message()
            msg["content-disposition"] = value
            name = msg.get_filename()
        except Exception:
            name = None
    if not name:
        m = re.search(r"""filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))""", value, re.I)
        if m:
            name = (m.group(1) or m.group(2) or "").strip()
    if not name:
        return None
    # Servers often send raw UTF-8 that http.client decoded as Latin-1.
    try:
        fixed = name.encode("latin-1").decode("utf-8")
        if fixed != name:
            name = fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name.strip() or None


def disk_free(path: Path) -> Optional[int]:
    try:
        p = Path(path)
        while not p.exists() and p.parent != p:
            p = p.parent
        return shutil.disk_usage(str(p)).free
    except OSError:
        return None


# --------------------------------------------------------------------------
# ZIP packing (folders / multiple files) and safe extraction
# --------------------------------------------------------------------------
# Already-compressed formats are stored, not deflated: same size, far faster.
_STORE_EXT = {
    ".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".lz4", ".cab",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif", ".jxl",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mp3", ".aac", ".ogg",
    ".opus", ".flac", ".m4a", ".pdf", ".docx", ".xlsx", ".pptx", ".odt",
    ".apk", ".jar", ".ipa", ".dmg", ".iso", ".msi", ".appimage", ".epub",
}
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def _collect(sources: Sequence[Path]) -> List[Tuple[Optional[Path], str]]:
    entries: List[Tuple[Optional[Path], str]] = []
    used = set()

    def unique_arc(arc: str) -> str:
        base, n = arc, 1
        while arc.lower() in used:
            root, ext = os.path.splitext(base)
            arc = "%s (%d)%s" % (root, n, ext)
            n += 1
        used.add(arc.lower())
        return arc

    for src in sources:
        src = Path(src)
        if src.is_dir():
            top = unique_arc(src.name or "folder")
            for root, dirs, files in os.walk(src):
                dirs.sort()
                rel = Path(root).relative_to(src).as_posix()
                prefix = top if rel == "." else top + "/" + rel
                kept = [f for f in sorted(files) if f.lower() not in _JUNK]
                if not kept and not dirs:
                    entries.append((None, prefix + "/"))
                for f in kept:
                    p = Path(root) / f
                    if p.is_file():
                        entries.append((p, prefix + "/" + f))
        elif src.is_file():
            entries.append((src, unique_arc(src.name)))
        else:
            raise FileNotFoundError(str(src))
    return entries


def zip_paths(sources: Sequence[Path], dest: Path, progress: Optional[ProgressFn] = None,
              cancel=None) -> Path:
    """Pack files/folders into *dest* (a .zip), streaming in 1 MiB chunks."""
    entries = _collect(sources)
    total = sum(p.stat().st_size for p, _ in entries if p is not None)
    done = 0
    try:
        with zipfile.ZipFile(dest, "w", allowZip64=True) as zf:
            for path, arc in entries:
                _check(cancel)
                if path is None:
                    zf.writestr(zipfile.ZipInfo(arc), b"")
                    continue
                info = zipfile.ZipInfo.from_file(path, arc, strict_timestamps=False)
                info.compress_type = (zipfile.ZIP_STORED if path.suffix.lower() in _STORE_EXT
                                      else zipfile.ZIP_DEFLATED)
                with open(path, "rb") as src, zf.open(info, "w") as out:
                    while True:
                        _check(cancel)
                        chunk = src.read(COPY_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return dest


def is_zip_name(path: Path) -> bool:
    """Only real ``.zip`` files are auto-extracted.

    ``zipfile.is_zipfile`` is also true for .docx/.xlsx/.jar/.apk/.epub, which must
    stay in one piece, so the extension decides.
    """
    return Path(path).suffix.lower() == ".zip"


def _member_parts(name: str) -> List[str]:
    parts = []
    for p in name.replace("\\", "/").split("/"):
        if p in ("", ".", ".."):
            continue
        parts.append(safe_filename(p, "_"))
    return parts


def extract_zip(zip_path: Path, dest_parent: Path, progress: Optional[ProgressFn] = None,
                cancel=None) -> Path:
    """Safely extract *zip_path* next to it and return the folder that was created.

    * If the archive has exactly one top-level folder, that folder is used
      directly (no ``foo/foo/…`` double nesting); otherwise a folder named
      after the archive is created.
    * Nothing is ever overwritten — the target gets ``(1)``, ``(2)`` … suffixes.
    * Zip-slip (``../``, absolute paths, drive letters) is blocked.
    * Members are streamed, never read fully into RAM.
    * Extraction happens in a hidden temp folder that is renamed at the end,
      so a failure never leaves a half-extracted folder behind.
    """
    zip_path, dest_parent = Path(zip_path), Path(dest_parent)
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if _member_parts(i.filename)]
        total = sum(i.file_size for i in infos)
        free = disk_free(dest_parent)
        if free is not None and total > free:
            raise OSError("Not enough disk space to extract (%s needed, %s free)"
                          % (format_size(total), format_size(free)))
        tops = {_member_parts(i.filename)[0] for i in infos}
        single_root = (
            len(tops) == 1
            and all(len(_member_parts(i.filename)) > 1 or i.is_dir() for i in infos)
        )
        strip = 1 if single_root else 0
        folder_name = next(iter(tops)) if single_root else safe_filename(zip_path.stem, "archive")
        final = unique_path(dest_parent / folder_name)
        tmp = dest_parent / (".%s.extracting" % final.name)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        root = os.path.realpath(str(tmp))
        done = 0
        try:
            for info in infos:
                _check(cancel)
                parts = _member_parts(info.filename)[strip:]
                if not parts:
                    continue
                target = os.path.realpath(os.path.join(root, *parts))
                if os.path.commonpath([root, target]) != root:
                    continue  # zip-slip attempt
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        _check(cancel)
                        chunk = src.read(COPY_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                try:
                    ts = time.mktime(tuple(info.date_time) + (0, 0, -1))
                    os.utime(target, (ts, ts))
                except (OverflowError, ValueError, OSError):
                    pass
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return final


# --------------------------------------------------------------------------
# Opening things with the OS
# --------------------------------------------------------------------------
def _spawn(args: List[str]) -> None:
    kwargs = {}
    if IS_WIN:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kwargs)


def open_path(path: Path) -> None:
    """Open a file or folder with its default application."""
    path = str(path)
    if IS_WIN:
        os.startfile(path)  # type: ignore[attr-defined]
    elif IS_MAC:
        _spawn(["open", path])
    else:
        _spawn(["xdg-open", path])


def reveal(path: Path) -> None:
    """Show *path* selected in the file manager (or open it if it's a folder)."""
    path = Path(path)
    if not path.exists():
        path = path.parent
    if path.is_dir():
        open_path(path)
    elif IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif IS_MAC:
        _spawn(["open", "-R", str(path)])
    else:
        if shutil.which("dbus-send"):
            try:
                subprocess.run(
                    ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                     "--type=method_call", "/org/freedesktop/FileManager1",
                     "org.freedesktop.FileManager1.ShowItems",
                     "array:string:" + path.resolve().as_uri(), "string:"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=True)
                return
            except (OSError, subprocess.SubprocessError):
                pass
        open_path(path.parent)


def open_url(url: str) -> None:
    webbrowser.open(url, new=2)


# --------------------------------------------------------------------------
# Cross-process file lock
# --------------------------------------------------------------------------
class FileLock:
    """Exclusive lock on a lock file (``msvcrt`` on Windows, ``fcntl`` elsewhere)."""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if IS_WIN:
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    return self  # proceed unlocked rather than hang forever
                time.sleep(0.03)

    def __exit__(self, *exc) -> None:
        try:
            if IS_WIN:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()

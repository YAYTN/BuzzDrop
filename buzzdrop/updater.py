"""Self-update for the packaged Windows app (stdlib only).

The flow is deliberately boring and safe:

1. ``check_for_update`` (desktop.py) finds a newer GitHub release and its files.
2. :func:`download_update` fetches the new ``.exe`` next to the running one and checks its
   SHA-256 against ``checksums.txt`` from the same release. A mismatch deletes it.
3. :func:`preflight` starts the new exe once (``--version``) to prove it runs.
4. :func:`swap_in` renames the running exe to ``.old`` and moves the new one into its
   place (Windows lets a running exe be renamed, not overwritten). If anything fails the
   old exe is put back, so a failed update never leaves you without the app.
5. :func:`relaunch` starts the new exe, which waits for the old process to exit first.

Nothing here runs unless the user asks (or opted into background downloads), and it never
restarts the app by itself. macOS and Linux apps are not signed, so they only get a
"new version available" notice.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import REPO, __version__
from .util import IS_WIN

log = logging.getLogger("buzz")

ASSET_NAME = "BuzzDrop-windows-x64.exe"
CHECKSUMS_NAME = "checksums.txt"
MAX_APP_BYTES = 200 << 20
MAX_TEXT_BYTES = 1 << 20
_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class UpdateError(Exception):
    """A problem worded for the person using the app."""


def _strict() -> bool:
    """Production mode: only files from this repo's GitHub releases over HTTPS are accepted.

    ``BUZZ_GITHUB_API`` (tests) points the app at a local stand-in and relaxes that check.
    """
    return "BUZZ_GITHUB_API" not in os.environ


def api_base() -> str:
    return os.environ.get("BUZZ_GITHUB_API", "https://api.github.com").rstrip("/")


def _check_url(url: str) -> None:
    if _strict() and not url.startswith("https://github.com/%s/releases/download/" % REPO):
        raise UpdateError("The update isn't hosted where BuzzDrop expects it, so it was not downloaded.")


# --------------------------------------------------------------------------
# What the release offers
# --------------------------------------------------------------------------
def release_assets(data: dict) -> Dict[str, str]:
    """``{asset_name: download_url}`` for the files we care about in a GitHub release JSON."""
    wanted = {ASSET_NAME, CHECKSUMS_NAME}
    found = {}
    for a in data.get("assets") or []:
        name, url = a.get("name"), a.get("browser_download_url")
        if name in wanted and isinstance(url, str):
            found[name] = url
    return found


def current_exe() -> Optional[Path]:
    """The running packaged exe, or None when running from source / on other systems."""
    if IS_WIN and getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def can_self_update(target: Optional[Path] = None) -> bool:
    """True for the packaged Windows exe in a folder we may write to (not e.g. Program Files)."""
    target = target or current_exe()
    if not IS_WIN or target is None or target.suffix.lower() != ".exe":
        return False
    probe = target.with_name(target.name + ".probe")
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def offers_download(info: Optional[dict]) -> bool:
    return bool(info and info.get("asset_url") and info.get("checksums_url"))


# --------------------------------------------------------------------------
# Download + verify
# --------------------------------------------------------------------------
def parse_checksums(text: str) -> Dict[str, str]:
    """Parse ``sha256sum`` output: ``<64 hex>  <name>`` (a ``*`` before the name is allowed)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[os.path.basename(m.group(2).replace("\\", "/"))] = m.group(1).lower()
    return out


def _open(url: str, timeout: float):
    from .api import USER_AGENT, ssl_context
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def _fetch_text(url: str, timeout: float) -> str:
    try:
        with _open(url, timeout) as resp:
            return resp.read(MAX_TEXT_BYTES).decode("utf-8", "replace")
    except Exception as exc:
        raise UpdateError("Couldn't read the update's checksum file (%s)." % exc)


def download_update(info: dict, dest_dir: Path, progress: Optional[Callable[[int, int], None]] = None,
                    cancel=None, timeout: float = 30.0) -> Path:
    """Download the new exe into *dest_dir* and verify it. Returns the verified file."""
    if not offers_download(info):
        raise UpdateError("This release has no verified download for your system.")
    _check_url(info["asset_url"])
    _check_url(info["checksums_url"])
    expected = parse_checksums(_fetch_text(info["checksums_url"], timeout)).get(ASSET_NAME)
    if not expected:
        raise UpdateError("The release doesn't list a checksum for %s, so it won't be installed." % ASSET_NAME)

    dest_dir = Path(dest_dir)
    final = dest_dir / "BuzzDrop.exe.new"
    part = dest_dir / "BuzzDrop.exe.new.part"
    digest = hashlib.sha256()
    done = 0
    try:
        with _open(info["asset_url"], timeout) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            if total > MAX_APP_BYTES:
                raise UpdateError("The update is unexpectedly large, so it was not downloaded.")
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("Update cancelled.")
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_APP_BYTES:
                    raise UpdateError("The update is unexpectedly large, so it was discarded.")
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(done, total)
        if digest.hexdigest() != expected:
            raise UpdateError("The download didn't match its checksum, so it was discarded.")
        os.replace(part, final)
        return final
    except UpdateError:
        _silent_unlink(part)
        raise
    except OSError as exc:
        _silent_unlink(part)
        raise UpdateError("Couldn't download the update (%s)." % (exc.strerror or exc))
    except Exception as exc:  # network errors from urllib
        _silent_unlink(part)
        raise UpdateError("Couldn't download the update (%s)." % exc)


def preflight(new_path: Path, expected_version: str, command: Optional[List[str]] = None,
              timeout: float = 60.0) -> None:
    """Start the new exe once to be sure it runs and reports the version we expect."""
    cmd = command or [str(new_path), "--version"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW if IS_WIN else 0,
                           env=dict(os.environ, BUZZ_NO_INTEGRATION="1"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("The new version didn't start (%s), so it was not installed." % exc)
    if r.returncode != 0 or expected_version not in (r.stdout or ""):
        raise UpdateError("The new version didn't report the expected version, so it was not installed.")


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------
def _silent_unlink(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def swap_in(current: Path, new: Path) -> Path:
    """``current`` → ``current.old`` and ``new`` → ``current``; the old one is restored on failure."""
    current, new = Path(current), Path(new)
    old = current.with_name(current.name + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            raise UpdateError("An earlier update's backup is still in use. Close other BuzzDrop windows and try again.")
    try:
        os.replace(current, old)
    except OSError as exc:
        raise UpdateError("Couldn't install the update (%s)." % (exc.strerror or exc))
    try:
        os.replace(new, current)
    except OSError as exc:
        try:
            os.replace(old, current)  # put the working app back
        except OSError:
            log.error("Could not restore %s after a failed update", current)
        raise UpdateError("Couldn't install the update (%s). Your current version was kept." % (exc.strerror or exc))
    return old


def relaunch(exe: Path, old_pid: int) -> None:
    """Start the updated app; it waits for *old_pid* to exit first (single-instance safe)."""
    subprocess.Popen([str(exe), "--after-update", str(old_pid)], close_fds=True, cwd=str(Path(exe).parent),
                     creationflags=_DETACHED if IS_WIN else 0)


def wait_for_exit(pid: int, timeout: float = 20.0) -> bool:
    """Block until process *pid* has exited (True) or *timeout* passes (False)."""
    if not IS_WIN:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)
        return False
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE
    if not handle:
        return True  # already gone
    try:
        return k32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        k32.CloseHandle(handle)


def cleanup_leftovers(current: Path) -> None:
    """Remove the previous version's backup and any unfinished download (call once the app runs fine)."""
    current = Path(current)
    for suffix in (".old", ".new", ".new.part"):
        _silent_unlink(current.with_name(current.name + suffix) if suffix == ".old" else
                       current.with_name("BuzzDrop.exe" + suffix))


def newer(version: str) -> bool:
    from .desktop import _ver
    return _ver(version) > _ver(__version__)

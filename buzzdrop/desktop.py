"""Small desktop helpers that don't need a GUI toolkit: system theme, notifications,
clipboard for the CLI, and the update check."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request
from typing import Optional, Tuple

from . import REPO, __version__
from .util import IS_MAC, IS_WIN

log = logging.getLogger("buzz")


def system_prefers_dark() -> bool:
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        if IS_MAC:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2).stdout
            return "dark" in out.lower()
        if shutil.which("gsettings"):
            out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                                 capture_output=True, text=True, timeout=2).stdout
            if "dark" in out:
                return True
            if "light" in out:
                return False
            out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                                 capture_output=True, text=True, timeout=2).stdout
            return "dark" in out.lower()
    except Exception:
        pass
    return True


def notify(title: str, message: str) -> None:
    """Best-effort OS notification on macOS / Linux (Windows uses the tray balloon)."""
    try:
        if IS_MAC:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            subprocess.Popen(["osascript", "-e", 'display notification "%s" with title "%s"'
                              % (esc(message), esc(title))],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not IS_WIN and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "BuzzDrop", "-i", "buzzdrop", title, message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def copy_text(text: str) -> bool:
    """Clipboard copy without a GUI toolkit (used by the CLI)."""
    try:
        if IS_WIN:
            from .winsys import set_clipboard_text
            return set_clipboard_text(text)
        candidates = [["pbcopy"]] if IS_MAC else [["wl-copy"], ["xclip", "-selection", "clipboard"],
                                                     ["xsel", "--clipboard", "--input"]]
        for cmd in candidates:
            if shutil.which(cmd[0]):
                # xclip/xsel fork a helper that keeps serving the selection — it must not
                # inherit our stdout, or `buzz send f | jq` would hang.
                subprocess.run(cmd, input=text.encode("utf-8"), timeout=5, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
    except Exception as exc:
        log.debug("clipboard copy failed: %s", exc)
    return False


def _ver(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def check_for_update(timeout: float = 8.0) -> Optional[dict]:
    """``{"version", "url"}`` if a newer GitHub release exists, else None."""
    from .api import USER_AGENT, ssl_context
    from .updater import ASSET_NAME, CHECKSUMS_NAME, api_base, release_assets
    req = urllib.request.Request(
        "%s/repos/%s/releases/latest" % (api_base(), REPO),
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            data = json.loads(resp.read(2_000_000).decode("utf-8"))
    except Exception as exc:
        log.info("update check failed: %s", exc)
        return None
    tag = str(data.get("tag_name") or "")
    if tag and _ver(tag) > _ver(__version__):
        assets = release_assets(data)
        return {"version": tag.lstrip("vV"),
                "url": data.get("html_url") or "https://github.com/%s/releases" % REPO,
                "asset_url": assets.get(ASSET_NAME), "checksums_url": assets.get(CHECKSUMS_NAME)}
    return None

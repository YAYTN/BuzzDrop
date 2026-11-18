"""Operating-system integration — all per-user, no admin rights needed.

==================  =======================  ==========================  ===================
Feature             Windows                  Linux                       macOS
==================  =======================  ==========================  ===================
BZ:// links         HKCU\\Software\\Classes   .desktop + xdg-mime         Info.plist (bundle)
Right-click "Send"  Explorer context menu    Nautilus / Nemo / Dolphin   drop on Dock icon
App shortcut        Start-menu .lnk          app-menu .desktop           /Applications
``buzz`` command    buzz.cmd + user PATH     ~/.local/bin/buzz           ~/.local/bin/buzz
Run at login        HKCU Run (tray mode)     –                           –
==================  =======================  ==========================  ===================

Commands always point at the *current* executable, and :func:`heal` rewrites
them if the app was moved. When run from source they include the launcher script,
never a bare ``python.exe "%1"`` that could not work.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from . import APP_NAME
from .config import data_dir, is_frozen
from .util import IS_LINUX, IS_MAC, IS_WIN

log = logging.getLogger("buzz")

FEATURES = ("protocol", "context_menu", "shortcut", "cli", "autostart")


class IntegrationError(Exception):
    pass


# --------------------------------------------------------------------------
# How to launch ourselves
# --------------------------------------------------------------------------
def _assets_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    cand = base / "assets"
    return cand if cand.exists() else Path(__file__).resolve().parent / "assets"


def icon_path(ext: str = "ico") -> Path:
    return _assets_dir() / ("icon." + ext)


def _gui_python() -> str:
    exe = sys.executable
    if IS_WIN:
        w = Path(exe).with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return exe


def _console_python() -> str:
    exe = sys.executable
    if IS_WIN and Path(exe).name.lower() == "pythonw.exe":
        c = Path(exe).with_name("python.exe")
        if c.exists():
            return str(c)
    return exe


def _source_launcher() -> List[str]:
    """``[script]`` when running from a source checkout, ``["-m", "buzzdrop"]`` when pip-installed."""
    pkg = Path(__file__).resolve().parent
    launcher = pkg.parent / "BuzzDrop.pyw"
    if launcher.exists():
        return [str(launcher)]
    return ["-m", "buzzdrop"]


def launch_command(console: bool = False) -> List[str]:
    if is_frozen():
        exe = Path(sys.executable).resolve()
        if IS_MAC and ".app/Contents/MacOS" in str(exe):
            return [str(exe)]
        return [str(exe)]
    return [_console_python() if console else _gui_python()] + _source_launcher()


def _cmdline(args: List[str]) -> str:
    return subprocess.list2cmdline(args)


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
if IS_WIN:
    import winreg

    _CLASSES = r"Software\Classes"
    _PROTO = _CLASSES + r"\bz"
    _MENU_KEYS = (_CLASSES + r"\*\shell\BuzzDrop.Send", _CLASSES + r"\Directory\shell\BuzzDrop.Send")
    _RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def _set(path: str, name: str, value: str, kind: int = winreg.REG_SZ) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, kind, value)

    def _get(path: str, name: str = ""):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def _delete_tree(path: str) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as k:
                while True:
                    try:
                        child = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    _delete_tree(path + "\\" + child)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass

    def _delete_value(path: str, name: str) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    def _notify_shell() -> None:
        try:
            import ctypes
            ctypes.WinDLL("shell32").SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
        except Exception:
            pass

    def _broadcast_env() -> None:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32")
            user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                                   wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT,
                                                   ctypes.POINTER(ctypes.c_size_t)]
            res = ctypes.c_size_t()
            user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(res))
        except Exception:
            pass

    def _icon_ref() -> str:
        return (str(Path(sys.executable).resolve()) + ",0") if is_frozen() else str(icon_path("ico"))

    def _protocol_cmd() -> str:
        # Everything after --protocol is treated as link text only — a crafted link can't
        # smuggle in `--send C:\secret.txt` even if the launching app mangles the quoting.
        return _cmdline(launch_command()) + ' --protocol "%1"'

    def _shim_text() -> bytes:
        """buzz.cmd contents, encoded the way cmd.exe reads batch files (OEM code page)."""
        import ctypes
        cmd = launch_command(console=True)
        enc = "cp%d" % ctypes.WinDLL("kernel32").GetOEMCP()
        text = "@echo off\r\n%s %%*\r\n" % _cmdline(cmd)
        try:
            return text.encode(enc)
        except (UnicodeEncodeError, LookupError):
            # e.g. C:\Users\Jürgen\… on a code page without ü → use 8.3 short paths
            buf = ctypes.create_unicode_buffer(32768)
            short = []
            for part in cmd:
                n = ctypes.WinDLL("kernel32").GetShortPathNameW(part, buf, 32768) if os.path.exists(part) else 0
                short.append(buf.value if n else part)
            return ("@echo off\r\n%s %%*\r\n" % _cmdline(short)).encode(enc, "replace")

    def _menu_cmd() -> str:
        return _cmdline(launch_command()) + ' --send "%1"'

    def _start_menu_lnk() -> Path:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / (APP_NAME + ".lnk")

    def _bin_dir() -> Path:
        return data_dir() / "bin"

    def _user_path_entries() -> List[str]:
        value = _get("Environment", "Path") or ""
        return [p for p in value.split(";") if p]

    def _w_status() -> Dict[str, bool]:
        return {
            "protocol": (_get(_PROTO + r"\shell\open\command") or "") == _protocol_cmd(),
            "context_menu": (_get(_MENU_KEYS[0] + r"\command") or "") == _menu_cmd(),
            "shortcut": _start_menu_lnk().exists(),
            "cli": (_bin_dir() / "buzz.cmd").exists() and any(
                os.path.normcase(os.path.expandvars(p).rstrip("\\")) == os.path.normcase(str(_bin_dir()))
                for p in _user_path_entries()),
            "autostart": bool(_get(_RUN, APP_NAME)),
        }

    def _w_set(feature: str, on: bool) -> None:
        if feature == "protocol":
            if on:
                _set(_PROTO, "", "URL:BuzzHeavier link")
                _set(_PROTO, "URL Protocol", "")
                _set(_PROTO + r"\DefaultIcon", "", _icon_ref())
                _set(_PROTO + r"\shell\open\command", "", _protocol_cmd())
            else:
                _delete_tree(_PROTO)
            _notify_shell()
        elif feature == "context_menu":
            for key in _MENU_KEYS:
                if on:
                    _set(key, "", "Send with " + APP_NAME)
                    _set(key, "Icon", _icon_ref())
                    _set(key, "MultiSelectModel", "Player")  # keep the entry for >15 selected files
                    _set(key + r"\command", "", _menu_cmd())
                else:
                    _delete_tree(key)
        elif feature == "shortcut":
            lnk = _start_menu_lnk()
            if on:
                cmd = launch_command()
                q = lambda s: s.replace("'", "''")  # noqa: E731 - PowerShell single-quote escaping
                script = (
                    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                    "$s.TargetPath='%s';$s.Arguments='%s';$s.IconLocation='%s';"
                    "$s.WorkingDirectory='%s';$s.Description='Send and receive files with BuzzHeavier';$s.Save()"
                ) % (q(str(lnk)), q(cmd[0]), q(_cmdline(cmd[1:])), q(_icon_ref()), q(str(Path.home())))
                lnk.parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                   capture_output=True, text=True, timeout=30, creationflags=0x08000000)
                if r.returncode != 0 or not lnk.exists():
                    raise IntegrationError("Couldn't create the Start menu shortcut: %s" % (r.stderr.strip() or r.returncode))
            else:
                try:
                    lnk.unlink()
                except FileNotFoundError:
                    pass
        elif feature == "cli":
            bindir = _bin_dir()
            raw = _get("Environment", "Path") or ""
            entries = [p for p in raw.split(";") if p]
            norm = os.path.normcase(str(bindir))
            entries = [p for p in entries if os.path.normcase(os.path.expandvars(p).rstrip("\\")) != norm]
            shims = ("buzz.cmd", "bz.cmd")  # `bz` is a shorter alias
            if on:
                bindir.mkdir(parents=True, exist_ok=True)
                for shim in shims:
                    (bindir / shim).write_bytes(_shim_text())
                entries.append(str(bindir))
            else:
                for shim in shims:
                    try:
                        (bindir / shim).unlink()
                    except FileNotFoundError:
                        pass
            kind = winreg.REG_EXPAND_SZ
            _set("Environment", "Path", ";".join(entries), kind)
            _broadcast_env()
        elif feature == "autostart":
            if on:
                _set(_RUN, APP_NAME, _cmdline(launch_command()) + " --background")
            else:
                _delete_value(_RUN, APP_NAME)


# --------------------------------------------------------------------------
# Linux (freedesktop)
# --------------------------------------------------------------------------
def _xdg_data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _desktop_exec(args: List[str]) -> str:
    out = []
    for a in args:
        if any(c in a for c in ' \t"\'\\$`<>~|&;*?#()'):
            a = '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
        out.append(a.replace("%", "%%"))
    return " ".join(out)


def _sh_quote(args: List[str]) -> str:
    import shlex
    return " ".join(shlex.quote(a) for a in args)


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _linux_icon() -> str:
    """Copy the icon somewhere stable (the PyInstaller temp dir disappears)."""
    src = icon_path("png")
    dest = _xdg_data() / "icons" / "hicolor" / "256x256" / "apps" / "buzzdrop.png"
    try:
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            return "buzzdrop"
    except OSError:
        pass
    return str(src)


_DESKTOP_ID = "buzzdrop.desktop"


def _l_paths() -> Dict[str, Path]:
    d = _xdg_data()
    return {
        "desktop": d / "applications" / _DESKTOP_ID,
        "nautilus": d / "nautilus" / "scripts" / ("Send with " + APP_NAME),
        "nemo": d / "nemo" / "actions" / "buzzdrop-send.nemo_action",
        "dolphin": d / "kio" / "servicemenus" / "buzzdrop-send.desktop",
        "cli": Path.home() / ".local" / "bin" / "buzz",
    }


def _l_desktop_entry() -> str:
    return (
        "[Desktop Entry]\nType=Application\nName=%s\nGenericName=File transfer\n"
        "Comment=Send and receive files with BuzzHeavier\nExec=%s %%U\nIcon=%s\nTerminal=false\n"
        "Categories=Network;FileTransfer;\nMimeType=x-scheme-handler/bz;\nStartupWMClass=BuzzDrop\n"
        "Keywords=buzzheavier;upload;download;share;send;\n"
    ) % (APP_NAME, _desktop_exec(launch_command()), _linux_icon())


def _run_quiet(args: List[str]) -> None:
    if shutil.which(args[0]):
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass


def _l_status() -> Dict[str, bool]:
    p = _l_paths()
    return {
        "protocol": p["desktop"].exists() and "x-scheme-handler/bz" in p["desktop"].read_text("utf-8", "ignore"),
        "context_menu": p["nautilus"].exists() or p["dolphin"].exists() or p["nemo"].exists(),
        "shortcut": p["desktop"].exists(),
        "cli": p["cli"].exists(),
        "autostart": False,
    }


def _l_set(feature: str, on: bool) -> None:
    p = _l_paths()
    if feature in ("protocol", "shortcut"):
        if on:
            p["desktop"].parent.mkdir(parents=True, exist_ok=True)
            p["desktop"].write_text(_l_desktop_entry(), encoding="utf-8")
            _run_quiet(["xdg-mime", "default", _DESKTOP_ID, "x-scheme-handler/bz"])
        else:
            for f in (p["desktop"],):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
        _run_quiet(["update-desktop-database", str(p["desktop"].parent)])
    elif feature == "context_menu":
        cmd = launch_command()
        if on:
            _write_exec(p["nautilus"], "#!/bin/sh\nexec %s --send \"$@\"\n" % _sh_quote(cmd))
            p["nemo"].parent.mkdir(parents=True, exist_ok=True)
            p["nemo"].write_text(
                "[Nemo Action]\nName=Send with %s\nComment=Upload to BuzzHeavier\nExec=%s --send %%F\n"
                "Icon-Name=%s\nSelection=notnone\nExtensions=any;\n" % (APP_NAME, _desktop_exec(cmd), _linux_icon()),
                encoding="utf-8")
            _write_exec(p["dolphin"],
                        "[Desktop Entry]\nType=Service\nMimeType=all/all;inode/directory;\nActions=send\n"
                        "X-KDE-ServiceTypes=KonqPopupMenu/Plugin\n\n[Desktop Action send]\n"
                        "Name=Send with %s\nIcon=%s\nExec=%s --send %%F\n"
                        % (APP_NAME, _linux_icon(), _desktop_exec(cmd)))
        else:
            for key in ("nautilus", "nemo", "dolphin"):
                try:
                    p[key].unlink()
                except FileNotFoundError:
                    pass
    elif feature == "cli":
        if on:
            _write_exec(p["cli"], "#!/bin/sh\nexec %s \"$@\"\n" % _sh_quote(launch_command(console=True)))
        else:
            try:
                p["cli"].unlink()
            except FileNotFoundError:
                pass
    elif feature == "autostart":
        raise IntegrationError("Run-at-login is only available on Windows.")


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------
def _m_bundle() -> Path:
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return Path()


def _m_status() -> Dict[str, bool]:
    cli = Path.home() / ".local" / "bin" / "buzz"
    return {"protocol": bool(_m_bundle().name), "context_menu": False,
            "shortcut": str(_m_bundle()).startswith("/Applications/"), "cli": cli.exists(), "autostart": False}


def _m_set(feature: str, on: bool) -> None:
    if feature == "protocol":
        bundle = _m_bundle()
        if not bundle.name:
            raise IntegrationError("BZ:// links work with the packaged BuzzDrop.app.")
        if on:
            _run_quiet(["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                        "LaunchServices.framework/Support/lsregister", "-f", str(bundle)])
    elif feature == "cli":
        _l_set("cli", on)
    else:
        raise IntegrationError("Not available on macOS.")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def supported() -> Dict[str, bool]:
    if IS_WIN:
        return {f: True for f in FEATURES}
    if IS_MAC:
        return {"protocol": bool(_m_bundle().name), "context_menu": False, "shortcut": False,
                "cli": True, "autostart": False}
    return {"protocol": True, "context_menu": True, "shortcut": False, "cli": True, "autostart": False}


def status() -> Dict[str, bool]:
    try:
        if IS_WIN:
            return _w_status()
        if IS_MAC:
            return _m_status()
        return _l_status()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("integration status failed: %s", exc)
        return {f: False for f in FEATURES}


def set_feature(feature: str, on: bool) -> None:
    log.info("integration: %s → %s", feature, on)
    try:
        if IS_WIN:
            _w_set(feature, on)
        elif IS_MAC:
            _m_set(feature, on)
        else:
            _l_set(feature, on)
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(str(exc)) from exc


def setup_defaults() -> List[str]:
    """First-run setup: make BZ:// links and the right-click entry work out of the box."""
    done = []
    want = ["protocol", "context_menu"] + (["shortcut"] if is_frozen() else [])
    sup = supported()
    for feature in want:
        if sup.get(feature):
            try:
                set_feature(feature, True)
                done.append(feature)
            except IntegrationError as exc:
                log.warning("first-run %s failed: %s", feature, exc)
    return done


def heal() -> None:
    """If the app moved, re-point anything that still references the old location."""
    if not IS_WIN:
        return
    for feature, key in (("protocol", _PROTO + r"\shell\open\command"), ("context_menu", _MENU_KEYS[0] + r"\command")):
        current = _get(key)
        if current and current != (_protocol_cmd() if feature == "protocol" else _menu_cmd()):
            try:
                set_feature(feature, True)
            except IntegrationError:
                pass
    run = _get(_RUN, APP_NAME)
    if run and run != _cmdline(launch_command()) + " --background":
        try:
            set_feature("autostart", True)
        except IntegrationError:
            pass
    shim = _bin_dir() / "buzz.cmd"
    try:
        if shim.exists() and shim.read_bytes() != _shim_text():
            set_feature("cli", True)
    except (OSError, IntegrationError):
        pass


def remove_all() -> None:
    for feature, ok in supported().items():
        if ok:
            try:
                set_feature(feature, False)
            except IntegrationError:
                pass

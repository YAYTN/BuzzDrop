"""Command line + launcher.

    buzz                          open the app
    buzz send FILE... [options]   upload from the terminal, print the link(s)
    buzz get LINK...  [options]   download from the terminal
    buzz setup [--remove]         set up / remove system integration
    buzz BZ://code | FILE ...     hand a link / files to the app (what the OS calls)

The terminal commands never import tkinter, so they work over SSH and on
headless servers. Everything else goes through the single running instance.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from . import APP_NAME, __version__

HELP = """\
%(app)s %(ver)s - send & receive files with BuzzHeavier

Usage:
  buzz                          Open the app
  buzz send FILE... [options]   Upload files/folders and print the link(s)
      --note TEXT                 Note shown on the download page
      --zip                       Bundle everything into one ZIP (one link)
      --https                     Print https://buzzheavier.com/... links instead of BZ://
      --no-copy                   Don't copy the link(s) to the clipboard
      --split MB                  Optional: send files bigger than MB in separate parts, each
                                  retried alone (also prints a plain link for every part)
      --json                      Machine-readable output
  buzz get LINK... [options]    Download BZ:// links, buzzheavier.com URLs or codes
      -o, --output DIR            Save into DIR (default: your download folder)
      --no-extract                Keep ZIP files zipped
      --json                      Machine-readable output
  buzz setup [--remove]         Register (or remove) BZ:// links, right-click menu, `buzz` command
  buzz update [--check]         Look for a new version and (packaged Windows app) install it
  buzz --version

Examples:
  buzz send holiday-photos/
  buzz get BZ://h0l6lgmru052 -o ~/Desktop
"""


# --------------------------------------------------------------------------
# Console plumbing
# --------------------------------------------------------------------------
def _attach_console() -> None:
    """A windowed .exe / pythonw has no stdout; borrow the parent terminal's console."""
    def usable(stream) -> bool:
        try:
            return stream is not None and stream.fileno() >= 0
        except (OSError, ValueError, AttributeError):
            return False

    if not usable(sys.stdout) and sys.platform == "win32":
        try:
            import ctypes
            import msvcrt
            k32 = ctypes.WinDLL("kernel32")
            k32.GetStdHandle.restype = ctypes.c_void_p
            # 1) output redirected to a pipe/file (`buzz get … > log.txt`): use those handles
            for num, name in ((-11, "stdout"), (-12, "stderr")):
                h = k32.GetStdHandle(num)
                if h and h != ctypes.c_void_p(-1).value:
                    try:
                        fd = msvcrt.open_osfhandle(h, os.O_WRONLY)
                        setattr(sys, name, open(fd, "w", encoding="utf-8", errors="replace", closefd=False))
                    except OSError:
                        pass
            # 2) launched from a terminal: write to the parent console
            if not usable(sys.stdout) and k32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # ✓/✗ must never crash a cp1252 pipe
        except (AttributeError, ValueError):
            pass


class Printer:
    """Single-line live progress for terminals; quiet when piped."""

    def __init__(self, quiet: bool = False) -> None:
        self.out = sys.stderr
        self.tty = bool(getattr(self.out, "isatty", lambda: False)()) and not quiet
        self.quiet = quiet
        self.last = 0.0
        self.width = 0
        enc = getattr(self.out, "encoding", None) or "ascii"
        try:
            "█░·".encode(enc)
            self.full, self.empty = "█", "░"
        except (UnicodeEncodeError, LookupError):
            self.full, self.empty = "#", "-"

    def update(self, job) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if now - self.last < 0.1:
            return
        self.last = now
        from .transfers import describe
        cols = shutil.get_terminal_size((80, 20)).columns
        frac = job.fraction
        bar_w = 20
        bar = (self.full * int((frac or 0) * bar_w)).ljust(bar_w, self.empty) if frac is not None else ""
        text = "%s %s  %s" % (bar, describe(job), job.label) if bar else "%s  %s" % (describe(job), job.label)
        text = text[: max(10, cols - 1)]
        self.out.write("\r" + text.ljust(self.width))
        self.out.flush()
        self.width = len(text)

    def clear(self) -> None:
        if self.tty and self.width:
            self.out.write("\r" + " " * self.width + "\r")
            self.out.flush()
            self.width = 0

    def say(self, msg: str) -> None:
        self.clear()
        if not self.quiet:
            print(msg, file=self.out)


# --------------------------------------------------------------------------
# Terminal commands
# --------------------------------------------------------------------------
def cmd_send(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="buzz send", add_help=True)
    p.add_argument("paths", nargs="+")
    p.add_argument("--note")
    p.add_argument("--zip", action="store_true")
    p.add_argument("--https", action="store_true")
    p.add_argument("--no-copy", action="store_true")
    p.add_argument("--split", type=float, metavar="MB")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    from .api import Client
    from .config import History, load_settings, setup_logging
    from .desktop import copy_text
    from .transfers import DONE, SEND, Job, describe, execute
    from .util import share_link

    setup_logging()
    settings = load_settings()
    if a.split is not None:
        settings["split_mb"] = max(0.0, a.split)
    paths = [os.path.abspath(x) for x in a.paths]
    missing = [x for x in paths if not os.path.exists(x)]
    if missing:
        print("Not found: %s" % missing[0], file=sys.stderr)
        return 2
    groups = [paths] if a.zip and len(paths) > 1 else [[x] for x in paths]
    style = "https" if a.https else settings.get("link_style", "bz")
    printer = Printer(quiet=a.json)
    client, history = Client(), History()
    results: List[Dict[str, Any]] = []
    links: List[str] = []
    try:
        for group in groups:
            job = Job(SEND, Path(group[0]).name if len(group) == 1 else "%d items" % len(group),
                      paths=group, note=a.note)
            job.on_change = printer.update
            execute(job, client, settings)
            if job.state == DONE:
                code = job.result["code"]
                link = share_link(code, style)
                links.append(link)
                history.add("send", job.result["name"], code, job.result["size"], group[0])
                printer.clear()
                parts = job.result.get("parts") or []
                entry = {"ok": True, "file": group[0], "name": job.result["name"], "code": code,
                         "link": link, "url": share_link(code, "https"), "size": job.result["size"]}
                if parts:  # plain links to every part, for people who don't use BuzzDrop
                    entry["parts"] = [share_link(c, "https") for c in parts]
                results.append(entry)
                if not a.json:
                    print("%s  %s" % (link, job.result["name"]))
                    for i, c in enumerate(parts, 1):
                        print("  part %d: %s" % (i, share_link(c, "https")))
            else:
                printer.say("✗ %s: %s" % (job.label, describe(job)))
                results.append({"ok": False, "file": group[0], "error": job.error or job.state})
    except KeyboardInterrupt:
        printer.say("Cancelled.")
        return 130
    if links and not a.no_copy and settings.get("copy_link", True):
        if copy_text("\n".join(links)) and not a.json:
            printer.say("Copied to clipboard.")
    if a.json:
        print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_get(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="buzz get", add_help=True)
    p.add_argument("links", nargs="+")
    p.add_argument("-o", "--output")
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    from .api import Client
    from .config import History, load_settings, setup_logging
    from .transfers import DONE, RECEIVE, Job, describe, execute
    from .util import parse_codes, web_link

    setup_logging()
    codes = parse_codes(" ".join(a.links))
    if not codes:
        print("No BuzzHeavier link or code found in: %s" % " ".join(a.links), file=sys.stderr)
        return 2
    settings = load_settings()
    if a.output:
        settings["download_folder"] = os.path.abspath(os.path.expanduser(a.output))
    if a.no_extract:
        settings["auto_extract_zip"] = False
    printer = Printer(quiet=a.json)
    client, history = Client(), History()
    results: List[Dict[str, Any]] = []
    try:
        for code in codes:
            job = Job(RECEIVE, "BZ://" + code, code=code)
            job.on_change = printer.update
            execute(job, client, settings)
            if job.state == DONE:
                path = job.result.get("extracted") or job.result["path"]
                history.add("receive", job.result["name"], code, job.result["size"], path)
                printer.clear()
                results.append({"ok": True, "code": code, "path": path, "size": job.result["size"]})
                if not a.json:
                    print("✓ %s → %s" % (job.result["name"], path))
            else:
                printer.say("✗ BZ://%s: %s" % (code, describe(job)))
                if job.blocked:
                    printer.say("  Open it in your browser instead: %s" % web_link(code))
                results.append({"ok": False, "code": code, "error": job.error or job.state})
    except KeyboardInterrupt:
        printer.say("Cancelled.")
        return 130
    if a.json:
        print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_setup(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="buzz setup")
    p.add_argument("--remove", action="store_true")
    a = p.parse_args(argv)
    from . import integration
    from .config import setup_logging
    setup_logging()
    if a.remove:
        integration.remove_all()
        print("Removed %s integration." % APP_NAME)
        return 0
    sup = integration.supported()
    labels = {"protocol": "BZ:// links open %s" % APP_NAME, "context_menu": "Right-click → Send with %s" % APP_NAME,
              "shortcut": "Start menu shortcut", "cli": "`buzz` terminal command"}
    rc = 0
    for feature, label in labels.items():
        if not sup.get(feature):
            continue
        try:
            integration.set_feature(feature, True)
            print("✓ " + label)
        except integration.IntegrationError as exc:
            print("✗ %s: %s" % (label, exc))
            rc = 1
    if sup.get("cli"):
        print("Open a new terminal to use `buzz`.")
    return rc


# --------------------------------------------------------------------------
# App launch (single instance)
# --------------------------------------------------------------------------
def _gui_payload(argv: List[str]) -> Dict[str, Any]:
    from .util import find_codes, parse_codes
    payload: Dict[str, Any] = {"cmd": "open", "paths": [], "codes": [], "background": False}
    i = 0
    send_mode = False
    while i < len(argv):
        arg = argv[i]
        i += 1
        if arg.startswith("-psn_"):  # old macOS Finder launch argument
            continue
        if arg == "--send":
            send_mode = True
            continue
        if arg == "--background":
            payload["background"] = True
            continue
        if arg in ("--settings", "--tray"):
            payload["cmd"] = "settings" if arg == "--settings" else payload["cmd"]
            payload["background"] = payload["background"] or arg == "--tray"
            continue
        if arg == "--protocol":
            # Link activation: the rest is untrusted link text. Only codes are taken from it —
            # never flags or file paths (a crafted link can't trigger `--send C:\secret.txt`).
            rest = " ".join(argv[i:])
            payload["codes"].extend(c for c in find_codes(rest) or parse_codes(rest) if c not in payload["codes"])
            break
        if arg.lower().startswith("file://"):
            arg = unquote(urlparse(arg).path)
            if sys.platform == "win32" and arg.startswith("/") and len(arg) > 2 and arg[2] == ":":
                arg = arg[1:]
        if send_mode or (os.path.exists(arg) and not arg.lower().startswith("bz:")):
            if os.path.exists(arg):
                payload["paths"].append(os.path.abspath(arg))
            continue
        payload["codes"].extend(c for c in parse_codes(arg) if c not in payload["codes"])
    if payload["paths"]:
        payload["cmd"] = "send"
    elif payload["codes"]:
        payload["cmd"] = "receive"
    return payload


def _no_gui(reason: str) -> int:
    msg = ("%s\n\nYou can still use the terminal commands:\n  buzz send FILE...\n  buzz get LINK...\n"
           % reason)
    _attach_console()
    print(msg, file=sys.stderr)
    return 1


def launch_gui(argv: List[str]) -> int:
    from .config import setup_logging
    from .instance import acquire_or_forward

    payload = _gui_payload(argv)
    setup_logging()
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return _no_gui("No graphical display found.")
    server = acquire_or_forward(payload)
    if server is None:
        return 0  # the running instance took over
    try:
        try:
            from .gui.app import run
        except ImportError as exc:
            if "tkinter" in str(exc) or "_tkinter" in str(exc):
                return _no_gui("The app window needs Tk. Install it with e.g.\n"
                               "  sudo apt install python3-tk   (Debian/Ubuntu)\n"
                               "  sudo dnf install python3-tkinter   (Fedora)")
            raise
        return run(payload, server)
    finally:
        server.close()


def cmd_update(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="buzz update", add_help=True)
    p.add_argument("--check", action="store_true", help="only look for a new version")
    a = p.parse_args(argv)

    from . import updater
    from .desktop import check_for_update
    info = check_for_update()
    if not info:
        print("BuzzDrop %s is up to date." % __version__)
        return 0
    print("BuzzDrop %s is available (you have %s)." % (info["version"], __version__))
    exe = updater.current_exe()
    if a.check:
        print(info["url"])
        return 0
    if not (exe and updater.offers_download(info) and updater.can_self_update(exe)):
        print("This copy can't update itself. Download the new version from:\n  %s" % info["url"])
        return 1
    tty = bool(getattr(sys.stderr, "isatty", lambda: False)())

    def progress(done: int, total: int) -> None:
        if tty and total:
            sys.stderr.write("\r  downloading %d%%" % (100 * done // total))
            sys.stderr.flush()
    new = exe.parent / "BuzzDrop.exe.new"
    try:
        new = updater.download_update(info, exe.parent, progress)
        if tty:
            sys.stderr.write("\r" + " " * 24 + "\r")
        updater.preflight(new, info["version"])
        updater.swap_in(exe, new)
    except updater.UpdateError as exc:
        updater._silent_unlink(new)
        print("✗ %s" % exc, file=sys.stderr)
        return 1
    print("✓ Updated to %s. Start BuzzDrop again to use it." % info["version"])
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "--after-update":
        # Started by the app itself right after an update: wait for the old process to exit first.
        try:
            pid = int(argv[1])
        except ValueError:
            pid = 0
        argv = argv[2:]
        if pid:
            from .updater import wait_for_exit
            wait_for_exit(pid)
    head = argv[0] if argv else ""
    commands = {"send": cmd_send, "get": cmd_get, "setup": cmd_setup, "update": cmd_update}
    if head in commands or head in ("-h", "--help", "help", "-V", "--version", "version",
                                    "--install", "--uninstall"):
        _attach_console()
        if head in commands:
            return commands[head](argv[1:])
        if head in ("-V", "--version", "version"):
            print("%s %s" % (APP_NAME, __version__))
            return 0
        if head == "--install":
            return cmd_setup([])
        if head == "--uninstall":
            return cmd_setup(["--remove"])
        print(HELP % {"app": APP_NAME, "ver": __version__})
        return 0
    return launch_gui(argv)


def main_gui() -> int:  # entry point for the windowed launcher
    return main()


if __name__ == "__main__":
    sys.exit(main())

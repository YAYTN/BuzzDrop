"""A complete, checkable uninstall (stdlib only).

Removing BuzzDrop means undoing *everything* it set up, and then proving it:

1. ask a running BuzzDrop (e.g. the tray icon) to quit,
2. remove the integrations: BZ:// links, right-click menu, Start menu shortcut, start-at-login,
   the ``buzz`` command and PATH entry, and the entry in Settings → Apps,
3. clear Windows' own bookkeeping about the program and any unfinished update files,
4. delete the settings/history folder (optional, so a reinstall can keep them),
5. delete the program file itself once it has exited,
6. scan every place BuzzDrop uses and report anything that is still there.

It only ever deletes things BuzzDrop itself created: the data folder is cleaned by *file name*, never
with a blanket "delete this folder", and a BZ:// handler that now belongs to another program is left alone.
Files you downloaded or sent, and links you shared, are never touched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import APP_NAME, integration, updater
from .config import data_dir, is_frozen, read_json
from .util import IS_MAC, IS_WIN

log = logging.getLogger("buzz")

# The only things BuzzDrop writes into its data folder; nothing else in there is ever deleted.
_DATA_FILES = ("config.json", "history.json", "instance.json", "instance.lock", "history.lock", "config.lock")
_DATA_GLOBS = ("*.lock", "*.tmp", "config.json.*", "history.json.*")
_DATA_DIRS = {"logs": None, "bin": ("buzz.cmd", "bz.cmd", "buzz", "bz")}   # folder → allowed file names (None = any)


@dataclass
class Report:
    done: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    left: List[str] = field(default_factory=list)
    program: str = ""            # "scheduled" | "removed" | "manual" | "kept" | ""

    @property
    def clean(self) -> bool:
        return not self.problems and not self.left


def this_program() -> Optional[Path]:
    """The packaged app's own file, or None when running from source."""
    return Path(sys.executable).resolve() if is_frozen() else None


def describe(keep_data: bool = False, remove_program: bool = True) -> List[str]:
    """What an uninstall removes, in plain words (shown before anything happens)."""
    items = ["BZ:// links and the \"Send with %s\" right-click menu" % APP_NAME]
    if IS_WIN:
        items.append("the Start menu shortcut, start-at-login, the `buzz` command and the entry in Settings > Apps")
    else:
        items.append("the app-menu entry and the `buzz` command")
    items.append("your settings and history" if not keep_data else "(your settings and history are kept)")
    if remove_program and this_program() is not None:
        items.append("the %s program file itself" % APP_NAME)
    return items


NOT_TOUCHED = "Files you downloaded or sent, and links you shared, are not touched."


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
def stop_running_instance(timeout: float = 20.0) -> bool:
    """Ask a running BuzzDrop (tray icon, open window) to quit and wait for it. True if none is left."""
    info = read_json(data_dir() / "instance.json", None)
    if not isinstance(info, dict) or not info.get("pid"):
        return True
    try:
        pid = int(info["pid"])
    except (TypeError, ValueError):
        return True
    if pid == os.getpid():
        return True                       # we ARE the running app (uninstalling from its own Settings)
    from . import instance
    if not instance.forward({"cmd": "quit"}):
        return updater.wait_for_exit(pid, 0.5)       # nobody answered: a stale file, or it is already gone
    return updater.wait_for_exit(pid, timeout)


def remove_integrations(exe: Optional[Path] = None) -> Tuple[List[str], List[str]]:
    """Undo every integration and clear leftovers next to the program. Returns (done, problems)."""
    done, problems = [], []
    try:
        integration.remove_all()
        done.append("removed links, right-click menu, shortcut, start-at-login, `buzz` command and Apps entry")
    except Exception as exc:  # keep going: report it, then clean up the rest
        problems.append("could not remove some integrations: %s" % exc)
    if exe is not None:
        integration.forget_traces(exe)
        updater.cleanup_leftovers(exe)
    return done, problems


def _close_logging() -> None:
    """The log file must be closed before its folder can be deleted (Windows keeps open files locked)."""
    lg = logging.getLogger("buzz")
    for handler in list(lg.handlers):
        try:
            handler.close()
        except Exception:
            pass
        lg.removeHandler(handler)


def remove_data(directory: Optional[Path] = None) -> Tuple[List[str], List[str]]:
    """Delete BuzzDrop's own files from its settings folder, then the folder if nothing else is in it.

    Returns (removed names, names left because BuzzDrop did not create them).
    """
    d = Path(directory) if directory is not None else data_dir()
    _close_logging()
    if not d.is_dir():
        return [], []
    removed: List[str] = []

    def drop(p: Path) -> None:
        try:
            p.unlink()
            removed.append(p.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete %s: %s", p, exc)

    for name in _DATA_FILES:
        if (d / name).is_file():
            drop(d / name)
    for pattern in _DATA_GLOBS:
        for p in d.glob(pattern):
            if p.is_file():
                drop(p)
    for sub, allowed in _DATA_DIRS.items():
        folder = d / sub
        if folder.is_dir():
            for child in list(folder.iterdir()):
                if child.is_file() and (allowed is None or child.name in allowed):
                    drop(child)
            try:
                folder.rmdir()
            except OSError:
                pass
    try:
        d.rmdir()
        removed.append(d.name + "/")
        return removed, []
    except OSError:
        return removed, sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def schedule_program_removal(exe: Optional[Path] = None) -> str:
    """Delete the program file once this process has exited. Returns a status word for the report."""
    exe = exe if exe is not None else this_program()
    if exe is None:
        return "kept"                     # running from source: there is no program file to remove
    if IS_MAC:
        return "manual"                   # an .app bundle is removed by dragging it to the Trash
    if not IS_WIN:
        try:
            exe.unlink()                  # Linux lets a running program delete its own file
            return "removed"
        except OSError:
            return "manual"
    if exe.suffix.lower() != ".exe":
        return "kept"
    # Windows cannot delete a running exe, so a tiny script waits for us to exit, deletes it, tidies
    # the folder if it is now empty, and finally deletes itself.
    esc = lambda s: str(s).replace("%", "%%")
    script = Path(tempfile.gettempdir()) / ("buzzdrop-remove-%s.cmd" % uuid.uuid4().hex[:8])
    script.write_text(
        "@echo off\r\n"
        "for /l %%i in (1,1,900) do (\r\n"
        "  del /f /q \"{exe}\" >nul 2>&1\r\n"
        "  if not exist \"{exe}\" goto gone\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        ")\r\n"
        ":gone\r\n"
        "rmdir \"{dir}\" >nul 2>&1\r\n"
        "(goto) 2>nul & del \"%~f0\"\r\n".format(exe=esc(exe), dir=esc(exe.parent)),
        encoding="mbcs", errors="replace")
    try:
        subprocess.Popen(["cmd.exe", "/c", str(script)], close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000)     # CREATE_NO_WINDOW only: adding DETACHED_PROCESS makes
                                                       # Windows open (and close) a visible console instead
        return "scheduled"
    except OSError as exc:
        log.warning("could not schedule removal of the program: %s", exc)
        return "manual"


def leftovers(keep_data: bool = False, exe: Optional[Path] = None, directory: Optional[Path] = None) -> List[str]:
    """Everything BuzzDrop set up that is still in place (the running program file itself is not counted)."""
    left = integration.leftovers()
    if not keep_data:
        d = Path(directory) if directory is not None else data_dir()
        for name in _DATA_FILES:
            if (d / name).exists():
                left.append("settings file " + name)
        if (d / "logs").exists():
            left.append("log folder")
    if exe is not None:
        for suffix in (".old", ".new", ".new.part"):
            p = exe.with_name(exe.name + suffix) if suffix == ".old" else exe.with_name("BuzzDrop.exe" + suffix)
            if p.exists():
                left.append("update file " + p.name)
    return left


def finish(keep_data: bool = False, remove_program: bool = True, exe: Optional[Path] = None) -> Report:
    """The last part: delete the data folder and the program, then verify. Call when nothing else will write."""
    exe = exe if exe is not None else this_program()
    report = Report()
    if not keep_data:
        removed, left = remove_data()
        if removed:
            report.done.append("deleted your settings and history")
        if left:
            report.problems.append("left in the settings folder (not created by %s): %s" % (APP_NAME, ", ".join(left)))
    report.program = schedule_program_removal(exe) if remove_program else "kept"
    report.left = leftovers(keep_data, exe)
    return report


def run_all(keep_data: bool = False, remove_program: bool = True) -> Report:
    """The whole uninstall in one go (command line)."""
    exe = this_program()
    report = Report()
    if not stop_running_instance():
        report.problems.append("a running %s did not close; quit it from the tray icon and try again" % APP_NAME)
        return report
    done, problems = remove_integrations(exe)
    report.done += done
    report.problems += problems
    tail = finish(keep_data, remove_program, exe)
    report.done += tail.done
    report.problems += tail.problems
    report.left, report.program = tail.left, tail.program
    return report


def summary(report: Report, keep_data: bool = False) -> str:
    lines = ["%s has been removed." % APP_NAME if report.clean else "%s was mostly removed." % APP_NAME, ""]
    lines += ["• " + d for d in report.done]
    if report.program == "scheduled":
        lines.append("• the program file will disappear in a few seconds")
    elif report.program == "removed":
        lines.append("• deleted the program file")
    elif report.program == "manual":
        lines.append("• you can now delete the program yourself (drag it to the Trash / Recycle Bin)")
    elif report.program == "kept":
        lines.append("• the program file was kept")
    if keep_data:
        lines.append("• your settings and history were kept")
    if report.problems:
        lines += ["", "Problems:"] + ["• " + p for p in report.problems]
    if report.left:
        lines += ["", "Still there (could not be removed):"] + ["• " + x for x in report.left]
    else:
        lines += ["", "Checked: nothing of %s is left behind." % APP_NAME]
    lines += ["", NOT_TOUCHED]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Dialog for "Settings → Apps → Uninstall" (no console, no running window)
# --------------------------------------------------------------------------
def ask(parent=None) -> Optional[bool]:
    """One confirmation dialog. Returns None if cancelled (Cancel, Esc or the X), else True/False for
    "also delete my settings and history"."""
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(parent)
    win.title("Uninstall %s" % APP_NAME)
    win.resizable(False, False)
    result: List[Optional[bool]] = [None]
    delete = tk.BooleanVar(master=win, value=False)
    body = ttk.Frame(win, padding=18)
    body.pack(fill="both", expand=True)
    plan = "\n".join("•  " + i for i in describe())
    ttk.Label(body, text="This will remove:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Label(body, text=plan, justify="left", wraplength=420).pack(anchor="w", pady=(6, 10))
    ttk.Label(body, text=NOT_TOUCHED, justify="left", wraplength=420).pack(anchor="w", pady=(0, 10))
    ttk.Checkbutton(body, text="Also delete my settings and history", variable=delete).pack(anchor="w")
    ttk.Label(body, text="Leave this off to keep them, for example if you plan to install %s again." % APP_NAME,
              justify="left", wraplength=420, foreground="#666666").pack(anchor="w", padx=(22, 0), pady=(0, 14))
    buttons = ttk.Frame(body)
    buttons.pack(anchor="e")

    def go() -> None:
        result[0] = bool(delete.get())
        win.destroy()
    ok = ttk.Button(buttons, text="Uninstall", command=go)
    ok.pack(side="right")
    cancel = ttk.Button(buttons, text="Cancel", command=win.destroy)
    cancel.pack(side="right", padx=(0, 8))
    win.protocol("WM_DELETE_WINDOW", win.destroy)            # the X means Cancel
    win.bind("<Escape>", lambda _e: win.destroy())
    win.bind("<Return>", lambda _e: go())
    cancel.focus_set()                                       # the safe choice is the default one
    try:
        win.update_idletasks()
        x = win.winfo_screenwidth() // 2 - win.winfo_reqwidth() // 2
        y = win.winfo_screenheight() // 3 - win.winfo_reqheight() // 2
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        if parent is not None and parent.winfo_viewable():
            win.transient(parent)
        win.attributes("-topmost", True)
        win.grab_set()
        win.focus_force()
    except tk.TclError:
        pass
    win.wait_window()
    return result[0]


def show_result(parent, report: "Report", keep_data: bool) -> None:
    """The "all done" (or "mostly done") message, always on top so it can never hide behind other windows.

    The program stays running until it is dismissed (Windows cannot delete a running program), so a message
    that sat unseen behind another window would leave BuzzDrop in the Task Manager and its file on disk.
    It also closes by itself after a while, whatever happens.
    """
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(parent)
    win.title("Uninstall %s" % APP_NAME)
    win.resizable(False, False)
    body = ttk.Frame(win, padding=18)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=summary(report, keep_data), justify="left", wraplength=460).pack(anchor="w", pady=(0, 14))
    ok = ttk.Button(body, text="OK", command=win.destroy)
    ok.pack(anchor="e")
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.bind("<Return>", lambda _e: win.destroy())
    win.bind("<Escape>", lambda _e: win.destroy())
    ok.focus_set()
    try:
        win.update_idletasks()
        x = win.winfo_screenwidth() // 2 - win.winfo_reqwidth() // 2
        y = win.winfo_screenheight() // 3 - win.winfo_reqheight() // 2
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()
        win.after(60000, win.destroy)                 # never keep the program alive more than a minute
    except tk.TclError:
        pass
    try:
        win.wait_window()
    except tk.TclError:
        pass


def run_dialog() -> int:
    """``BuzzDrop --uninstall``: ask, uninstall, say what happened. Used by Windows' own Uninstall button."""
    try:
        import tkinter as tk
    except ImportError:
        print("No window system available. Use:  buzz uninstall --yes", file=sys.stderr)
        return 2
    root = tk.Tk()
    root.withdraw()
    try:
        delete_data = ask(root)
        if delete_data is None:
            return 1
        keep_data = not delete_data
        report = run_all(keep_data=keep_data)
        show_result(root, report, keep_data)
        return 0 if report.clean else 1
    finally:
        root.destroy()

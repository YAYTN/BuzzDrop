"""The BuzzDrop window.

One window, three tabs (Send · Receive · History) and a settings panel. All
network work happens on worker threads; this module only ever touches tkinter
from the main thread (a short poll loop drains the worker/IPC/hotkey queues).
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable, Dict, List, Optional

from .. import APP_ID, APP_NAME, REPO_URL, __version__, integration, uninstall, updater
from ..config import History, apply_history_policy, data_dir, download_dir, load_settings, save_settings
from ..desktop import check_for_update, notify as desktop_notify, system_prefers_dark
from ..transfers import CANCELLED, DONE, FAILED, QUEUED, RECEIVE, RUNNING, SEND, Job, TransferManager, describe
from ..util import (IS_MAC, IS_WIN, find_codes, format_ago, format_size, open_path, open_url, parse_codes,
                    reveal, share_link, web_link)
from .theme import ACCENTS, PALETTE_LABELS, UI, ellipsize
from .widgets import Button, IconButton, Input, RoundFrame, ScrollArea, Segmented, Toaster, Toggle, animate

log = logging.getLogger("buzz")

if IS_WIN:
    from .. import winsys
else:  # pragma: no cover
    winsys = None  # type: ignore

MOD = "Command" if IS_MAC else "Control"


# =============================================================================
# Hover tooltips for canvas items
# =============================================================================
class CanvasTip:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.win: Optional[tk.Toplevel] = None
        self.job = None

    def schedule(self, text: str, x: int, y: int) -> None:
        self.hide()
        self.job = self.app.root.after(550, lambda: self._show(text, x, y))

    def _show(self, text: str, x: int, y: int) -> None:
        ui = self.app.ui
        self.win = tk.Toplevel(self.app.root)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(self.win, text=text, bg=ui.c.toast, fg=ui.c.toast_text, font=ui.f.small,
                 padx=ui.px(8), pady=ui.px(4), bd=0).pack()
        self.win.update_idletasks()
        self.win.geometry("+%d+%d" % (x - self.win.winfo_width() // 2, y))

    def hide(self) -> None:
        if self.job:
            self.app.root.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


# =============================================================================
# Rows
# =============================================================================
class _Row(tk.Canvas):
    """Card-style list row drawn on a single canvas, with hoverable icon actions."""

    HEIGHT = 72

    def __init__(self, parent: tk.Misc, app: "App") -> None:
        self.app, self.ui = app, app.ui
        super().__init__(parent, height=self.ui.px(self.HEIGHT), bg=self.ui.c.bg, highlightthickness=0, bd=0)
        self.w = 0
        self.hover = False
        self._actions: List[tuple] = []
        self._action_hit = False
        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_click)

    # geometry
    @property
    def ch(self) -> int:  # card height
        return self.ui.px(self.HEIGHT) - self.ui.px(8)

    def _on_configure(self, e) -> None:
        if e.width != self.w and e.width > 60:
            self.w = e.width
            self.redraw()

    def _set_hover(self, on: bool) -> None:
        self.hover = on
        if self.w:
            c = self.ui.c
            self._bgimg = self.ui.rrect(self.w, self.ch, self.ui.px(12), c.hover if on else c.surface, c.border)
            self.itemconfigure("bg", image=self._bgimg)
            self.configure(cursor="hand2" if (on and self.clickable()) else "")

    def clickable(self) -> bool:
        return False

    def on_click(self) -> None:
        pass

    def _on_click(self, e) -> None:
        if self._action_hit:
            self._action_hit = False
            return
        if self.clickable() and 0 <= e.y <= self.ch:
            self.on_click()

    # drawing helpers
    def draw_card(self) -> None:
        c = self.ui.c
        self.delete("all")
        self._bgimg = self.ui.rrect(self.w, self.ch, self.ui.px(12), c.hover if self.hover else c.surface, c.border)
        self.create_image(0, 0, anchor="nw", image=self._bgimg, tags="bg")

    def draw_badge(self, text: Optional[str], icon: Optional[str], fill: str, fg: str) -> None:
        ui = self.ui
        s = ui.px(40)
        x, y = ui.px(14), (self.ch - s) / 2
        self._badge = ui.rrect(s, s, ui.px(10), fill)
        self.create_image(x, y, anchor="nw", image=self._badge)
        if icon:
            self._badge_icon = ui.icon(icon, 18, fg)
            self.create_image(x + s / 2, y + s / 2, image=self._badge_icon)
        elif text:
            self.create_text(x + s / 2, y + s / 2, text=text, font=ui.f.tiny_b, fill=fg)

    def draw_actions(self, actions: List[tuple]) -> int:
        """actions: (icon, tooltip, callback). Returns the x where actions start."""
        ui, c = self.ui, self.ui.c
        self.delete("action")
        size = ui.px(32)
        x = self.w - ui.px(12)
        cy = self.ch / 2
        self._action_imgs = []
        self._hover_bg = ui.rrect(size, size, size / 2, c.surface3)
        for i, (icon, tip, cb) in reversed(list(enumerate(actions))):
            x -= size
            tag = "action-%d" % i
            normal, hover = ui.icon(icon, 17, c.text2), ui.icon(icon, 17, c.text)
            self._action_imgs += [normal, hover]
            bg = self.create_image(x, cy - size / 2, anchor="nw", tags=("action", tag))
            ic = self.create_image(x + size / 2, cy, image=normal, tags=("action", tag))
            self.tag_bind(tag, "<Enter>", lambda e, b=bg, i_=ic, h=hover, t=tip: self._act_hover(b, i_, h, t, e))
            self.tag_bind(tag, "<Leave>", lambda e, b=bg, i_=ic, n=normal: self._act_leave(b, i_, n))
            self.tag_bind(tag, "<ButtonRelease-1>", lambda e, f=cb: self._act_click(f))
            x -= ui.px(2)
        return x

    def _act_hover(self, bg, ic, hover_img, tip, e) -> None:
        self.itemconfigure(bg, image=self._hover_bg)
        self.itemconfigure(ic, image=hover_img)
        self.configure(cursor="hand2")
        self.app.tip.schedule(tip, e.x_root, self.winfo_rooty() + self.ch)

    def _act_leave(self, bg, ic, normal_img) -> None:
        self.itemconfigure(bg, image="")
        self.itemconfigure(ic, image=normal_img)
        self.configure(cursor="hand2" if self.clickable() else "")
        self.app.tip.hide()

    def _act_click(self, cb: Callable[[], None]) -> None:
        self._action_hit = True
        self.app.tip.hide()
        cb()

    def redraw(self) -> None:  # pragma: no cover - overridden
        pass


def _ext_badge(name: str) -> Optional[str]:
    ext = os.path.splitext(name)[1].lstrip(".").upper()
    return ext[:4] if 0 < len(ext) <= 5 else None


class TransferRow(_Row):
    def __init__(self, parent: tk.Misc, app: "App", job: Job) -> None:
        self.job = job
        self._frac = 0.0
        self._key = None
        super().__init__(parent, app)

    def clickable(self) -> bool:
        return self.job.state == DONE

    def on_click(self) -> None:
        if self.job.kind == SEND:
            self.app.copy_link(self.job.result["code"])
        else:
            self.app.reveal(self.job.result.get("extracted") or self.job.result.get("path"))

    def _actions_for_state(self) -> List[tuple]:
        j, a = self.job, self.app
        if j.active:
            return [("x", "Cancel", lambda: a.manager.cancel(j))]
        remove = ("x", "Remove from list", lambda: a.remove_row(j))
        if j.state == DONE and j.kind == SEND:
            code = j.result["code"]
            return [("copy", "Copy BZ:// link (for people who use BuzzDrop)", lambda: a.copy_link(code, "bz")),
                    ("globe", "Copy web link (works for anyone)", lambda: a.copy_link(code, "https")),
                    ("external", "Open download page", lambda: open_url(web_link(code))), remove]
        if j.state == DONE:
            target = j.result.get("extracted") or j.result.get("path")
            return [("folder", "Show in folder", lambda: a.reveal(target)),
                    ("external", "Open", lambda: a.open_file(target)), remove]
        acts = []
        if j.blocked:
            acts.append(("external", "Open in browser", lambda: open_url(web_link(j.code))))
        acts.append(("retry", "Try again", lambda: a.retry(j)))
        return acts + [remove]

    def redraw(self) -> None:
        if not self.w:
            return
        self._key = None
        self.draw_card()
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        if not self.w:
            return
        j, ui, c = self.job, self.ui, self.ui.c
        key = (j.state, j.blocked, j.label, bool(j.result))
        if key != self._key or force:
            self._key = key
            self.delete("badge", "text", "action", "bar")
            self.draw_card()
            if j.kind == RECEIVE and not j.result and j.label.startswith("BZ://"):
                badge = (None, "download")
            elif j.kind == SEND and j.paths and len(j.paths) == 1 and os.path.isdir(j.paths[0]):
                badge = (None, "folder")
            else:
                ext = _ext_badge(j.label)
                badge = (ext, None if ext else "file")
            if j.state == FAILED:
                self.draw_badge(None, "alert", c.danger_soft, c.danger)
            elif j.state == DONE:
                self.draw_badge(badge[0], badge[1], c.accent_soft, c.accent_ink)
            else:
                self.draw_badge(badge[0], badge[1], c.surface2, c.text2)
            self.ax = self.draw_actions(self._actions_for_state())
            self.x0 = ui.px(14 + 40 + 13)
            self.title = self.create_text(self.x0, self.ch / 2 - ui.px(10), anchor="w", font=ui.f.body_b,
                                          fill=c.text, tags="text")
            self.status = self.create_text(self.x0, self.ch / 2 + ui.px(10), anchor="w", font=ui.f.small,
                                           fill=c.text2, tags="text")
            self.status2 = None
            if j.active:
                y = self.ch - ui.px(9)
                self.track = self.create_line(self.x0, y, self.ax - ui.px(6), y, width=ui.px(3),
                                              capstyle="round", fill=c.surface3, tags="bar")
                self.fill = self.create_line(self.x0, y, self.x0, y, width=ui.px(3), capstyle="round",
                                             fill=c.accent, tags="bar")
        avail = self.ax - self.x0 - ui.px(8)
        self.itemconfigure(self.title, text=ellipsize(ui.f.body_b, j.label, avail))
        if j.state == DONE and j.kind == SEND:
            link = share_link(j.result["code"], self.app.settings.get("link_style", "bz"))
            self.itemconfigure(self.status, text=link, fill=c.accent_ink, font=ui.f.mono)
            if self.status2 is None:
                lw = ui.f.mono.measure(link)
                self.status2 = self.create_text(self.x0 + lw, self.ch / 2 + ui.px(10), anchor="w",
                                                font=ui.f.small, fill=c.text3, tags="text",
                                                text="  ·  " + format_size(j.result.get("size", 0)))
        else:
            color = {FAILED: c.danger, CANCELLED: c.text3}.get(j.state, c.text2)
            self.itemconfigure(self.status, text=ellipsize(ui.f.small, describe(j), avail, middle=False), fill=color)
        if j.active:
            y = self.ch - ui.px(9)
            width = self.ax - ui.px(6) - self.x0
            frac = j.fraction
            if frac is None:
                if j.state == RUNNING:  # indeterminate shimmer
                    p = (time.monotonic() * 0.7) % 1.4 - 0.4
                    a, b = max(0.0, p), min(1.0, p + 0.4)
                    self.coords(self.fill, self.x0 + a * width, y, self.x0 + max(a, b) * width, y)
                else:
                    self.coords(self.fill, self.x0, y, self.x0, y)
            else:
                self._frac += (frac - self._frac) * 0.35 if frac >= self._frac else (frac - self._frac)
                self.coords(self.fill, self.x0, y, self.x0 + max(0.0, self._frac) * width, y)


class HistoryRow(_Row):
    HEIGHT = 64

    def __init__(self, parent: tk.Misc, app: "App", entry: Dict[str, Any]) -> None:
        self.entry = entry
        super().__init__(parent, app)

    def _path(self) -> Optional[str]:
        p = self.entry.get("path")
        return p if p and os.path.exists(p) else None

    def clickable(self) -> bool:
        return True

    def on_click(self) -> None:
        if self.entry.get("kind") == "send":
            self.app.copy_link(self.entry["code"])
        elif self._path():
            self.app.reveal(self._path())
        else:
            self.app.toast("That file isn't on this computer anymore.", "info")

    def redraw(self) -> None:
        e, ui, c = self.entry, self.ui, self.ui.c
        self.draw_card()
        sent = e.get("kind") == "send"
        self.draw_badge(None, "upload" if sent else "download", c.accent_soft if sent else c.info_soft,
                        c.accent_ink if sent else c.info)
        code = e.get("code", "")
        acts = [("copy", "Copy BZ:// link (for people who use BuzzDrop)", lambda: self.app.copy_link(code, "bz")),
                ("globe", "Copy web link (works for anyone)", lambda: self.app.copy_link(code, "https"))]
        if not sent and self._path():
            acts.append(("folder", "Show in folder", lambda: self.app.reveal(self._path())))
        elif not sent:
            acts.append(("download", "Download again", lambda: self.app.receive_codes([code])))
        acts.append(("x", "Remove from history", lambda: self.app.remove_history(e)))
        ax = self.draw_actions(acts)
        x0 = ui.px(14 + 40 + 13)
        avail = ax - x0 - ui.px(8)
        self.create_text(x0, self.ch / 2 - ui.px(9), anchor="w", font=ui.f.body_b, fill=c.text,
                         text=ellipsize(ui.f.body_b, e.get("name") or code, avail), tags="text")
        meta = "%s · %s · %s · BZ://%s" % ("Sent" if sent else "Received", format_ago(e.get("time", 0)),
                                           format_size(e.get("size", 0)), code)
        self.create_text(x0, self.ch / 2 + ui.px(10), anchor="w", font=ui.f.small, fill=c.text3,
                         text=ellipsize(ui.f.small, meta, avail, middle=False), tags="text")


# =============================================================================
# Drop zone
# =============================================================================
class DropZone(tk.Canvas):
    def __init__(self, parent: tk.Misc, app: "App") -> None:
        self.app, self.ui = app, app.ui
        super().__init__(parent, height=self.ui.px(176), bg=self.ui.c.bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.hover = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._folder_hit = False

    def _hov(self, on: bool) -> None:
        self.hover = on
        self.draw()

    def flash(self) -> None:
        self.hover = True
        self.draw()
        self.after(450, lambda: self._hov(False))

    def draw(self) -> None:
        ui, c = self.ui, self.ui.c
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        self.delete("all")
        self._bg = ui.rrect(w, h, ui.px(16), c.hover if self.hover else c.surface,
                            c.accent if self.hover else c.border2, bw=ui.px(1.5))
        self.create_image(0, 0, anchor="nw", image=self._bg)
        cx = w / 2
        self._circle = ui.circle(56, c.accent_soft)
        self.create_image(cx, ui.px(50), image=self._circle)
        self._icon = ui.icon("upload", 24, c.accent_ink)
        self.create_image(cx, ui.px(49), image=self._icon)
        self.create_text(cx, ui.px(98), text="Drop files here to send" if IS_WIN else "Choose files to send",
                         font=ui.f.h2, fill=c.text)
        sub = ("or click to browse · any size, any type" if IS_WIN else
               "click here · or drop files on the Dock icon" if IS_MAC else "click here to browse · any size, any type")
        self.create_text(cx, ui.px(122), text=sub, font=ui.f.small, fill=c.text3)
        t = self.create_text(cx, ui.px(150), text="Send a whole folder", font=ui.f.small_b,
                             fill=c.accent_ink, tags="folder")
        bbox = self.bbox(t)
        if bbox:
            self.create_line(bbox[0], bbox[3], bbox[2], bbox[3], fill=c.accent_soft, tags="folder")
        self.tag_bind("folder", "<ButtonRelease-1>", self._folder)

    def _folder(self, _e) -> None:
        self._folder_hit = True
        self.app.choose_folder()

    def _click(self, e) -> None:
        if self._folder_hit:
            self._folder_hit = False
            return
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.app.choose_files()


# =============================================================================
# App
# =============================================================================
class App:
    def __init__(self, root: tk.Tk, payload: Dict[str, Any], server) -> None:
        self.root = root
        self.server = server
        self.settings = load_settings()
        self.history = History()
        self.manager = TransferManager(lambda: self.settings)
        self.main_q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.rows: Dict[int, TransferRow] = {}
        self.page = "send"
        self.in_settings = False
        self.update_info: Optional[dict] = None
        self._update_state = "idle"      # idle → downloading → ready (nothing restarts until the user says so)
        self._update_pct = 0.0
        self._update_file = None
        self._update_applied = False
        self._update_silent = False
        self._can_update = None
        self._uninstalling: Optional[dict] = None   # set by "Uninstall…"; the files go at the very end of quit()
        self.suggested: set = set()
        self._last_clip = None
        self._clip_check = 0.0
        self._recent_links: List[tuple] = []
        self._hist_job = None
        self._hist_dirty = True
        self._closing = False
        self._pending_send: List[str] = []
        self._send_job = None
        self.shell = None
        self.drop = None
        self.background = bool(payload.get("background"))

        root.title(APP_NAME)
        self._build_ui()
        self._geometry()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._bind_keys()
        self._platform_hooks()

        tray = IS_WIN and self.settings.get("run_in_background")
        if self.background and tray:
            pass  # stay hidden in the tray
        else:
            self.show_window()
            if self.background:
                root.iconify()
        self.handle_payload(payload, initial=True)
        root.after(400, self._startup_tasks)
        root.after(60, self._tick)

    # ------------------------------------------------------------------ build
    def _theme_dark(self) -> bool:
        t = self.settings.get("theme", "system")
        return system_prefers_dark() if t == "system" else t == "dark"

    def _build_ui(self, offscreen: bool = False) -> None:
        root = self.root
        self.ui = UI(root, self._theme_dark(), self.settings.get("accent", "teal"),
                     self.settings.get("palette", "default"))
        ui, c = self.ui, self.ui.c
        root.configure(bg=c.bg)
        root.minsize(ui.px(420), ui.px(560))
        self.tip = CanvasTip(self)
        self.toaster = Toaster(root, ui)
        self._set_icon()

        # Everything lives in one host frame. A rebuild (new colours/theme) assembles the new
        # host out of sight and swaps it in at once, so you never watch it being built.
        self.host = tk.Frame(root, bg=c.bg)
        if offscreen:
            self.host.place(x=-30000, y=0, width=max(root.winfo_width(), ui.px(420)),
                            height=max(root.winfo_height(), ui.px(560)))
        else:
            self.host.pack(fill="both", expand=True)
        self.main = tk.Frame(self.host, bg=c.bg)
        self.main.pack(fill="both", expand=True)
        pad = ui.px(20)

        # header
        head = tk.Frame(self.main, bg=c.bg)
        head.pack(fill="x", padx=pad, pady=(ui.px(16), ui.px(12)))
        self._logo = ui.logo(30)
        tk.Label(head, image=self._logo, bg=c.bg, bd=0).pack(side="left")
        tk.Label(head, text=APP_NAME, font=ui.f.title, fg=c.text, bg=c.bg).pack(side="left", padx=(ui.px(10), 0))
        tk.Label(head, text="v" + __version__, font=ui.f.tiny, fg=c.text3, bg=c.bg).pack(
            side="left", padx=(ui.px(6), 0), pady=(ui.px(4), 0))
        IconButton(head, ui, "sliders", self.open_settings, tooltip="Settings (%s+,)" % ("⌘" if IS_MAC else "Ctrl")).pack(side="right")
        IconButton(head, ui, "sun" if ui.dark else "moon", self.toggle_theme,
                   tooltip="Light mode" if ui.dark else "Dark mode").pack(side="right", padx=(0, ui.px(2)))
        self.update_slot = tk.Frame(head, bg=c.bg)
        self.update_slot.pack(side="right", padx=(0, ui.px(6)))
        self._render_update_pill()

        # tabs
        self.tabs = Segmented(self.main, ui, [("send", "Send"), ("receive", "Receive"), ("history", "History")],
                              self.page, self.show_page)
        self.tabs.pack(fill="x", padx=pad)
        self.body = tk.Frame(self.main, bg=c.bg)
        self.body.pack(fill="both", expand=True, pady=(ui.px(14), 0))
        self.pages = {"send": self._build_send(), "receive": self._build_receive(), "history": self._build_history()}
        self.settings_frame: Optional[tk.Frame] = None
        for job in self.manager.jobs:
            self._add_row(job)
        self._update_empty()
        self.show_page(self.page, force=True)

    def _set_icon(self) -> None:
        try:
            ico = integration.icon_path("ico")
            if IS_WIN and ico.exists():
                self.root.iconbitmap(default=str(ico))
            else:
                self._icons = [self.ui.logo(256, scale=False), self.ui.logo(64, scale=False),
                               self.ui.logo(32, scale=False)]
                self.root.iconphoto(True, *self._icons)
        except tk.TclError:
            pass

    def _section_header(self, parent: tk.Misc, title: str, buttons: List[tuple]) -> tk.Frame:
        ui, c = self.ui, self.ui.c
        f = tk.Frame(parent, bg=c.bg)
        tk.Label(f, text=title, font=ui.f.small_b, fg=c.text2, bg=c.bg).pack(side="left")
        for text, cmd in reversed(buttons):
            Button(f, ui, text, kind="ghost", small=True, height=28, command=cmd).pack(side="right")
        return f

    def _build_send(self) -> tk.Frame:
        ui, c = self.ui, self.ui.c
        page = tk.Frame(self.body, bg=c.bg)
        pad = ui.px(20)
        self.dropzone = DropZone(page, self)
        self.dropzone.pack(fill="x", padx=pad)
        self.note = Input(page, ui, "Add a note for the download page (optional)", height=40, icon="file")
        self.note.pack(fill="x", padx=pad, pady=(ui.px(10), 0))
        hdr = self._section_header(page, "Sent", [("Copy all links", self.copy_all_links),
                                                   ("Clear", lambda: self.clear_finished(SEND))])
        hdr.pack(fill="x", padx=pad, pady=(ui.px(16), ui.px(6)))
        self.send_list = ScrollArea(page, ui)
        self.send_list.pack(fill="both", expand=True, padx=(pad, ui.px(10)), pady=(0, ui.px(8)))
        self.send_empty = tk.Label(self.send_list.inner, text="Your links will show up here.\n"
                                   "Tip: paste files with %s+V." % ("⌘" if IS_MAC else "Ctrl"),
                                   font=ui.f.small, fg=c.text3, bg=c.bg, justify="center")
        return page

    def _build_receive(self) -> tk.Frame:
        ui, c = self.ui, self.ui.c
        page = tk.Frame(self.body, bg=c.bg)
        pad = ui.px(20)
        row = tk.Frame(page, bg=c.bg)
        row.pack(fill="x", padx=pad)
        self.link_in = Input(row, ui, "Paste a BZ:// link, URL or code", height=44, icon="download",
                             on_submit=self.receive_from_input)
        Button(row, ui, "Download", kind="primary", height=44, command=self.receive_from_input).pack(
            side="right", padx=(ui.px(8), 0))
        self.link_in.pack(side="left", fill="x", expand=True)
        loc = tk.Frame(page, bg=c.bg)
        loc.pack(fill="x", padx=pad, pady=(ui.px(10), 0))
        self._folder_icon = ui.icon("folder", 15, c.text3)
        tk.Label(loc, image=self._folder_icon, bg=c.bg).pack(side="left")
        self.dest_label = tk.Label(loc, font=ui.f.small, fg=c.text2, bg=c.bg, anchor="w", cursor="hand2")
        self.dest_label.pack(side="left", padx=(ui.px(6), 0), fill="x", expand=True)
        self.dest_label.bind("<Button-1>", lambda e: self.reveal(str(download_dir(self.settings))))
        Button(loc, ui, "Change", kind="ghost", small=True, height=28, command=self.change_download_dir).pack(side="right")
        self._update_dest_label()
        hdr = self._section_header(page, "Received", [("Open folder", lambda: self.reveal(str(download_dir(self.settings)))),
                                                       ("Clear", lambda: self.clear_finished(RECEIVE))])
        hdr.pack(fill="x", padx=pad, pady=(ui.px(16), ui.px(6)))
        self.recv_list = ScrollArea(page, ui)
        self.recv_list.pack(fill="both", expand=True, padx=(pad, ui.px(10)), pady=(0, ui.px(8)))
        hint = ("Tip: select a BZ:// link anywhere and press %s." % self.settings.get("global_hotkey", "Ctrl+Shift+F1")
                if IS_WIN and self.settings.get("enable_global_hotkey") else
                "Tip: copy a BZ:// link and BuzzDrop will offer to download it.")
        self.recv_empty = tk.Label(self.recv_list.inner, text="Downloads will show up here.\n" + hint,
                                   font=ui.f.small, fg=c.text3, bg=c.bg, justify="center")
        return page

    def _build_history(self) -> tk.Frame:
        ui, c = self.ui, self.ui.c
        page = tk.Frame(self.body, bg=c.bg)
        pad = ui.px(20)
        self.search = Input(page, ui, "Search by name or code", height=40, icon="search",
                            on_change=lambda v: self._schedule_history())
        self.search.pack(fill="x", padx=pad)
        hdr = tk.Frame(page, bg=c.bg)
        hdr.pack(fill="x", padx=pad, pady=(ui.px(16), ui.px(6)))
        self.hist_count = tk.Label(hdr, font=ui.f.small_b, fg=c.text2, bg=c.bg)
        self.hist_count.pack(side="left")
        Button(hdr, ui, "Clear history", kind="ghost", small=True, height=28, command=self.clear_history).pack(side="right")
        self.hist_list = ScrollArea(page, ui)
        self.hist_list.pack(fill="both", expand=True, padx=(pad, ui.px(10)), pady=(0, ui.px(8)))
        return page

    # ------------------------------------------------------------ navigation
    def show_page(self, name: str, force: bool = False) -> None:
        if self.in_settings:
            self.close_settings()
        if name == self.page and not force:
            return
        self.page = name
        self.tabs.set(name)
        for key, frame in self.pages.items():
            if key == name:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        if name == "history":
            self._render_history()
        self.tabs.set_badge(name, False)
        if name == "receive":
            self.root.after_idle(self.link_in.focus)

    def toggle_theme(self) -> None:
        self.settings["theme"] = "light" if self.ui.dark else "dark"
        save_settings(self.settings)
        self.rebuild()

    def rebuild(self, settings_open: bool = False) -> None:
        if getattr(self, "_rebuilding", False):
            return  # a second click while the first rebuild is still running
        self._rebuilding = True
        try:
            note = self.note.get() if hasattr(self, "note") else ""
            scroll = 0.0  # how far down Settings was scrolled, so a colour change doesn't throw you back up
            if settings_open and self.in_settings and getattr(self, "_settings_area", None) is not None:
                try:
                    scroll = self._settings_area.canvas.yview()[0]
                except tk.TclError:
                    scroll = 0.0
            self.toaster.dismiss(now=True)
            self.tip.hide()
            old = list(self.root.winfo_children())
            self.rows.clear()
            self.in_settings = False
            self._dl_label = None
            if getattr(self, "_recording", False):
                self.root.unbind("<KeyPress>")
                self._recording = False
            self._hist_dirty = True
            self._build_ui(offscreen=True)       # built out of sight …
            self.note.set(note)
            if settings_open:
                self.open_settings()
            self.root.update()                   # … laid out and drawn while still hidden …
            if settings_open and scroll > 0:
                area = self._settings_area
                area.canvas.yview_moveto(scroll)  # back to where you were
                area._draw_bar()
            # … then swapped in at once. width/height must be reset: place() adds them to relwidth/relheight.
            self.host.place(x=0, y=0, relwidth=1, relheight=1, width=0, height=0)
            self.host.lift()
            self.root.update_idletasks()
            for w in old:
                w.destroy()
            self._style_titlebar()
        finally:
            self._rebuilding = False

    # ------------------------------------------------------------------ rows
    def _list_for(self, kind: str) -> ScrollArea:
        return self.send_list if kind == SEND else self.recv_list

    def _add_row(self, job: Job) -> TransferRow:
        area = self._list_for(job.kind)
        row = TransferRow(area.inner, self, job)
        kids = [w for w in area.inner.pack_slaves() if isinstance(w, TransferRow)]
        if kids:
            row.pack(fill="x", before=kids[0])
        else:
            row.pack(fill="x")
        self.rows[job.id] = row
        area.scroll_top()
        return row

    def _update_empty(self) -> None:
        for kind, label in ((SEND, self.send_empty), (RECEIVE, self.recv_empty)):
            if any(j.kind == kind for j in self.manager.jobs):
                label.pack_forget()
            else:
                label.pack(pady=self.ui.px(36))

    def remove_row(self, job: Job) -> None:
        self.manager.remove(job)
        row = self.rows.pop(job.id, None)
        if row:
            row.destroy()
        self._update_empty()

    def clear_finished(self, kind: str) -> None:
        for job in [j for j in self.manager.jobs if j.kind == kind and not j.active]:
            self.remove_row(job)

    def retry(self, job: Job) -> None:
        self.remove_row(job)
        new = self.manager.retry(job)
        if new:
            self._add_row(new)
            self._update_empty()

    # -------------------------------------------------------------- actions
    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(parent=self.root, title="Choose files to send")
        if paths:
            self.send_paths(list(paths))

    def choose_folder(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="Choose a folder to send", mustexist=True)
        if path:
            self.send_paths([path])

    def send_paths(self, paths: List[str]) -> None:
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            self.toast("Nothing to send — the file wasn't found.", "error")
            return
        note = self.note.get().strip() or None
        jobs = self.manager.add_send(paths, note=note)
        if note:
            self.note.set("")
        for job in jobs:
            self._add_row(job)
        self._update_empty()
        self.show_page("send")
        if self.page == "send":
            self.dropzone.flash()

    def receive_codes(self, codes: List[str]) -> None:
        if not codes:
            return
        jobs = self.manager.add_receive(codes)
        if not jobs:
            self.toast("Already downloading that link.", "info")
        for job in jobs:
            self._add_row(job)
            self.suggested.add(job.code)
        self._update_empty()
        self.show_page("receive")

    def receive_from_input(self) -> None:
        text = self.link_in.get()
        codes = parse_codes(text)
        if not codes:
            self.toast("That doesn't look like a BuzzHeavier link." if text.strip() else
                       "Paste a BZ:// link or a buzzheavier.com URL first.", "error")
            self.link_in.focus()
            return
        self.link_in.set("")
        self.receive_codes(codes)

    def copy(self, text: str) -> bool:
        self._last_clip = text
        ok = False
        if IS_WIN and winsys is not None and winsys.set_clipboard_text(text):
            ok = True
        else:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                ok = True
            except tk.TclError:
                ok = False
        if ok:
            self._schedule_clip_clear(text)
        return ok

    def _schedule_clip_clear(self, text: str) -> None:
        """Optionally wipe a link we copied after a delay — but never anything copied since."""
        if getattr(self, "_clip_after", None):
            try:
                self.root.after_cancel(self._clip_after)
            except (tk.TclError, ValueError):
                pass
            self._clip_after = None
        try:
            secs = int(self.settings.get("clear_clipboard_s") or 0)
        except (TypeError, ValueError):
            secs = 0
        if secs > 0:
            self._clip_after = self.root.after(secs * 1000, lambda: self._clear_clip_if_ours(text))

    def _clear_clip_if_ours(self, text: str) -> None:
        self._clip_after = None
        try:
            if IS_WIN and winsys is not None:
                winsys.clear_clipboard_if_text(text)
                return
            if self.root.clipboard_get().strip() == text.strip():
                self.root.clipboard_clear()
        except tk.TclError:
            pass  # empty, or not text any more: leave it alone

    # -------------------------------------------------------------- uninstall
    def uninstall_flow(self) -> None:
        """Settings → Uninstall…: explain, ask, undo the integrations now, delete the files as the app exits."""
        plan = "\n".join("• " + i for i in uninstall.describe())
        title = "Uninstall %s" % APP_NAME
        if not messagebox.askyesno(title, "This will remove:\n\n%s\n\n%s\n\nUninstall %s?"
                                   % (plan, uninstall.NOT_TOUCHED, APP_NAME), parent=self.root):
            return
        delete_data = messagebox.askyesno(
            title, "Also delete your settings and history?\n\nChoose No to keep them, for example if you plan to "
            "install %s again." % APP_NAME, parent=self.root)
        if self.manager.busy and not messagebox.askyesno(
                APP_NAME, "Transfers are still running.\nCancel them and uninstall?", icon="warning",
                parent=self.root):
            return
        done, problems = uninstall.remove_integrations(uninstall.this_program())
        self._uninstalling = {"keep_data": not delete_data, "done": done, "problems": problems}
        self.quit()

    def _finish_uninstall(self) -> None:
        """Runs at the very end of quit(), when nothing will write settings or logs any more."""
        u = self._uninstalling or {}
        report = uninstall.finish(keep_data=bool(u.get("keep_data")))
        report.done = list(u.get("done", [])) + report.done
        report.problems = list(u.get("problems", [])) + report.problems
        try:
            (messagebox.showinfo if report.clean else messagebox.showwarning)(
                "Uninstall %s" % APP_NAME, uninstall.summary(report, bool(u.get("keep_data"))))
        except tk.TclError:
            pass

    def _auto_open_budget(self) -> bool:
        """At most 3 browser tabs per minute, so a batch of blocked links can't flood the browser."""
        now = time.monotonic()
        recent = [t for t in getattr(self, "_auto_opens", []) if now - t < 60]
        if len(recent) >= 3:
            self._auto_opens = recent
            return False
        recent.append(now)
        self._auto_opens = recent
        return True

    def copy_link(self, code: str, style: Optional[str] = None) -> None:
        """Copy a link. *style* "bz" or "https" picks the kind; None follows the Link style setting."""
        link = share_link(code, style or self.settings.get("link_style", "bz"))
        if self.copy(link):
            self.toast("Copied %s" % link, "success")

    def copy_all_links(self) -> None:
        style = self.settings.get("link_style", "bz")
        links = [share_link(j.result["code"], style) for j in reversed(self.manager.jobs)
                 if j.kind == SEND and j.state == DONE]
        if not links:
            self.toast("No links yet — send something first.", "info")
        elif self.copy("\n".join(links)):
            self.toast("Copied %d link%s" % (len(links), "" if len(links) == 1 else "s"), "success")

    def reveal(self, path: Optional[str]) -> None:
        if not path:
            return
        try:
            reveal(Path(path))
        except Exception as exc:
            self.toast("Couldn't open the folder: %s" % exc, "error")

    def open_file(self, path: Optional[str]) -> None:
        if path and os.path.exists(path):
            try:
                open_path(Path(path))
            except Exception as exc:
                self.toast("Couldn't open it: %s" % exc, "error")
        else:
            self.toast("The file was moved or deleted.", "error")

    def change_download_dir(self) -> None:
        current = str(download_dir(self.settings))
        path = filedialog.askdirectory(parent=self.root, title="Save downloads to…", initialdir=current)
        if path:
            self.settings["download_folder"] = path
            save_settings(self.settings)
            self._update_dest_label()
            if getattr(self, "_dl_label", None):
                self._dl_label.configure(text=self._short_path(path))
            self.toast("Downloads will be saved to %s" % self._short_path(path), "success")

    @staticmethod
    def _short_path(path: str) -> str:
        home = str(Path.home())
        return "~" + path[len(home):] if path.startswith(home) else path

    def _update_dest_label(self) -> None:
        self.dest_label.configure(text="Saving to " + self._short_path(str(download_dir(self.settings))))

    def toast(self, text: str, kind: str = "info", action=None, duration: int = 3800) -> None:
        try:
            self.toaster.show(text, kind, action, duration)
        except tk.TclError:
            pass

    def notify(self, title: str, message: str) -> None:
        if not self.settings.get("notifications", True) or self.is_focused():
            return
        if IS_WIN and self.shell:
            self.shell.notify(title, message)
        else:
            desktop_notify(title, message)

    def is_focused(self) -> bool:
        try:
            return self.root.state() == "normal" and self.root.focus_displayof() is not None
        except (tk.TclError, KeyError):
            return False

    # ---------------------------------------------------------------- history
    def _schedule_history(self) -> None:
        if self._hist_job:
            self.root.after_cancel(self._hist_job)
        self._hist_job = self.root.after(120, self._render_history)

    def _render_history(self) -> None:
        self._hist_job = None
        self.history.reload()
        for w in self.hist_list.inner.winfo_children():
            w.destroy()
        q = self.search.get().strip().lower()
        items = [e for e in self.history.items
                 if not q or q in (e.get("name") or "").lower() or q in (e.get("code") or "").lower()]
        total = len(self.history.items)
        self.hist_count.configure(text=("%d transfer%s" % (total, "" if total == 1 else "s")) if not q
                                  else "%d of %d" % (len(items), total))
        ui, c = self.ui, self.ui.c
        if not items:
            tk.Label(self.hist_list.inner, text="Nothing here yet." if not q else "No matches.",
                     font=ui.f.small, fg=c.text3, bg=c.bg).pack(pady=ui.px(36))
        for e in items[:150]:
            HistoryRow(self.hist_list.inner, self, e).pack(fill="x")
        if len(items) > 150:
            tk.Label(self.hist_list.inner, text="Showing the latest 150 — search to find older ones.",
                     font=ui.f.small, fg=c.text3, bg=c.bg).pack(pady=ui.px(10))
        self.hist_list.scroll_top()
        self._hist_dirty = False

    def _apply_history_policy(self, closing: bool = False) -> None:
        """The opt-in "forget my history automatically" setting (off by default)."""
        try:
            removed = apply_history_policy(self.history, self.settings, closing)
        except Exception as exc:  # never let housekeeping disturb the app
            log.warning("history cleanup failed: %s", exc)
            return
        if removed and not closing:
            self._hist_dirty = True
            if self.page == "history" and not self.in_settings:
                self._render_history()

    def _history_timer(self) -> None:
        if self._closing:
            return
        self._apply_history_policy()
        self.root.after(30 * 60 * 1000, self._history_timer)   # left running for days? check every 30 minutes

    def remove_history(self, entry: Dict[str, Any]) -> None:
        self.history.remove(entry)
        self._render_history()

    def clear_history(self) -> None:
        if not self.history.items:
            return
        if messagebox.askyesno(APP_NAME, "Clear your whole transfer history?\n(Files are not deleted.)",
                               parent=self.root):
            self.history.clear()
            self._render_history()

    # --------------------------------------------------------------- settings
    def open_settings(self) -> None:
        if self.in_settings:
            return
        self.show_window()
        self.in_settings = True
        self.main.pack_forget()
        self.settings_frame = self._build_settings()
        self.settings_frame.pack(fill="both", expand=True)

    def close_settings(self) -> None:
        if not self.in_settings:
            return
        self.in_settings = False
        if getattr(self, "_recording", False):
            self.root.unbind("<KeyPress>")
        self._recording = False
        self._dl_label = None
        if self.settings_frame:
            self.settings_frame.destroy()
            self.settings_frame = None
        self.main.pack(fill="both", expand=True)
        self._update_dest_label()

    def _save(self, key: str, value: Any) -> None:
        self.settings[key] = value
        save_settings(self.settings)

    def _build_settings(self) -> tk.Frame:
        ui, c = self.ui, self.ui.c
        pad = ui.px(20)
        frame = tk.Frame(self.host, bg=c.bg)
        head = tk.Frame(frame, bg=c.bg)
        head.pack(fill="x", padx=ui.px(12), pady=(ui.px(14), ui.px(6)))
        IconButton(head, ui, "back", self.close_settings, tooltip="Back (Esc)").pack(side="left")
        tk.Label(head, text="Settings", font=ui.f.title, fg=c.text, bg=c.bg).pack(side="left", padx=(ui.px(6), 0))
        area = ScrollArea(frame, ui, settle_ms=110)  # ~25 rows of widgets: refit once resizing stops
        self._settings_area = area  # remembered so a rebuild can restore the scroll position
        area.pack(fill="both", expand=True, padx=(pad, ui.px(10)), pady=(0, ui.px(10)))
        body = area.inner
        status = integration.status()
        sup = integration.supported()

        def section(title: str) -> RoundFrame:
            tk.Label(body, text=title.upper(), font=ui.f.tiny_b, fg=c.text3, bg=c.bg).pack(
                anchor="w", pady=(ui.px(14), ui.px(6)), padx=ui.px(4))
            card = RoundFrame(body, ui, c.surface, c.border, radius=ui.px(14))
            card.pack(fill="x")
            card._rows = 0
            return card

        def row(card: RoundFrame, title: str, desc: str = "", control=None) -> tk.Frame:
            if card._rows:
                tk.Frame(card, bg=c.border, height=1).pack(fill="x", padx=ui.px(16))
            card._rows += 1
            r = tk.Frame(card, bg=c.surface)
            r.pack(fill="x", padx=ui.px(16), pady=ui.px(12))
            ctl = control(r) if control is not None else None
            stacked = ctl is not None and getattr(control, "stack", False)  # wide controls sit under the text
            txt = tk.Frame(r, bg=c.surface)
            if ctl is not None and not stacked:
                ctl.pack(side="right", padx=(ui.px(12), 0))
                txt.pack(side="left", fill="x", expand=True)
            else:
                txt.pack(fill="x")
                if stacked:
                    ctl.pack(anchor="w", pady=(ui.px(8), 0))
            labels = [tk.Label(txt, text=title, font=ui.f.body, fg=c.text, bg=c.surface, anchor="w",
                               justify="left", wraplength=ui.px(290))]
            labels[0].pack(fill="x")
            if desc:
                labels.append(tk.Label(txt, text=desc, font=ui.f.small, fg=c.text3, bg=c.surface, anchor="w",
                                       justify="left", wraplength=ui.px(290)))
                labels[1].pack(fill="x", pady=(ui.px(2), 0))

            last_w = [0]

            def rewrap(e, labels=labels, last_w=last_w) -> None:  # text wraps to the room it really has
                if abs(e.width - last_w[0]) < 2:  # height-only changes must not re-wrap again
                    return
                last_w[0] = e.width
                for lb in labels:
                    lb.configure(wraplength=max(ui.px(100), e.width - ui.px(4)))
            txt.bind("<Configure>", rewrap)
            return r

        def toggle(key: str, after: Optional[Callable[[bool], None]] = None):
            def make(parent):
                def changed(v: bool) -> None:
                    self._save(key, v)
                    if after:
                        after(v)
                return Toggle(parent, ui, bool(self.settings.get(key)), changed, bg=c.surface)
            return make

        def seg(key: str, options, after: Optional[Callable[[str], None]] = None, width: int = 210):
            def make(parent):
                def changed(v: str) -> None:
                    self._save(key, v if not isinstance(self.settings.get(key), int) else int(v))
                    if after:
                        after(v)
                return Segmented(parent, ui, options, str(self.settings.get(key)), changed, height=32,
                                 width=width, bg=c.surface, small=True)
            make.stack = width >= 170  # too wide to sit beside its description in a narrow window
            return make

        def integ(feature: str):
            def make(parent):
                t = Toggle(parent, ui, status.get(feature, False), lambda v: None, bg=c.surface)

                def changed(v: bool) -> None:
                    def work() -> None:
                        try:
                            integration.set_feature(feature, v)
                            if feature == "cli" and v:
                                self.main_q.put(lambda: self.toast("Open a new terminal and type: buzz --help", "success", duration=6000))
                        except integration.IntegrationError as exc:
                            msg = str(exc)  # `exc` is unbound once the except block ends
                            self.main_q.put(lambda: (t.set(not v), self.toast(msg, "error", duration=6000)))
                    threading.Thread(target=work, daemon=True).start()
                t.command = changed
                return t
            return make

        # -- Receiving
        card = section("Receiving")

        def folder_ctrl(parent):
            f = tk.Frame(parent, bg=c.surface)
            Button(f, ui, "Change", small=True, height=30, command=self.change_download_dir, bg=c.surface).pack(side="right")
            return f
        r = row(card, "Download folder", "", folder_ctrl)
        self._dl_label = tk.Label(r.winfo_children()[-1], text=self._short_path(str(download_dir(self.settings))),
                                  font=ui.f.small, fg=c.text3, bg=c.surface, anchor="w", cursor="hand2")
        self._dl_label.pack(fill="x", pady=(ui.px(2), 0))
        self._dl_label.bind("<Button-1>", lambda e: self.reveal(str(download_dir(self.settings))))
        row(card, "Extract ZIP files automatically", "Unpacks into a folder next to the download.",
            toggle("auto_extract_zip"))
        row(card, "Delete the ZIP after extracting", "Keeps Downloads tidy. If extracting fails, the ZIP is kept.",
            toggle("delete_zip_after_extract"))
        row(card, "Show in folder when done", "", toggle("launch_folder_after_download"))
        row(card, "Offer to download copied links", "When you copy a BZ:// link, BuzzDrop offers to fetch it.",
            toggle("watch_clipboard"))
        row(card, "Open blocked links in my browser", "If BuzzHeavier asks for a browser check, open the file's "
            "page for me. Off by default: you get an \"Open in browser\" button instead.",
            toggle("open_blocked_in_browser"))

        # -- Sending
        card = section("Sending")
        row(card, "Copy the link when an upload finishes", "", toggle("copy_link"))
        row(card, "Clear the copied link after a while", "Removes the link from your clipboard — only if you "
            "haven't copied anything else since. (Windows' own Win+V history keeps its own copy.)" if IS_WIN else
            "Removes the link from your clipboard — only if you haven't copied anything else since.",
            seg("clear_clipboard_s", [("0", "Off"), ("30", "30 s"), ("60", "1 min"), ("300", "5 min")], width=250))
        row(card, "Link style", "What BuzzDrop copies for you automatically. https:// links are clickable in every "
            "chat app; BZ:// links open BuzzDrop. Every finished upload also has a button for each kind.",
            seg("link_style", [("bz", "BZ://"), ("https", "https://")], after=lambda v: self._refresh_rows(), width=170))
        row(card, "Send multiple files as one ZIP", "One link for everything you drop at once.",
            toggle("bundle_multiple"))
        row(card, "Split very large uploads", "Sends big files in separate parts, so a dropped connection "
            "only repeats one part. Off by default. The person downloading needs BuzzDrop to get the file in one "
            "step; without it they must join the parts themselves.",
            seg("split_mb", [("0", "Off"), ("500", "500 MB"), ("1000", "1 GB"), ("2000", "2 GB")], width=270))
        row(card, "Transfers at the same time", "",
            seg("max_parallel", [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4")], width=170))

        # -- Appearance
        card = section("Appearance")
        row(card, "Theme", "", seg("theme", [("system", "System"), ("dark", "Dark"), ("light", "Light")],
                                   after=lambda v: self.root.after(180, lambda: self.rebuild(settings_open=True))))
        def swatches(parent):
            f = tk.Frame(parent, bg=c.surface)
            current = self.settings.get("accent", "teal")
            size = ui.px(28)
            for name, colour in ACCENTS.items():
                cv = tk.Canvas(f, width=size, height=size, bg=c.surface, highlightthickness=0, bd=0,
                               cursor="hand2")
                cv.pack(side="left", padx=(0, ui.px(8)))
                if name == current:
                    cv.create_oval(2, 2, size - 2, size - 2, outline=c.text, width=2)
                cv.create_oval(ui.px(6), ui.px(6), size - ui.px(6), size - ui.px(6), fill=colour, outline="")
                cv.bind("<Button-1>", lambda e, n=name: (self._save("accent", n),
                                                          self.root.after(60, lambda: self.rebuild(settings_open=True))))
            return f
        swatches.stack = True
        row(card, "Accent color", "", swatches)
        row(card, "Background style", "Changes the colors behind everything. Works with light and dark.",
            seg("palette", PALETTE_LABELS, width=340,
                after=lambda v: self.root.after(180, lambda: self.rebuild(settings_open=True))))
        row(card, "Notifications", "Tell me when a transfer finishes while I'm in another app.",
            toggle("notifications"))

        # -- Privacy
        card = section("Privacy")
        self._history_keep_prev = int(self.settings.get("history_keep_days") or 0)

        def history_changed(v: str) -> None:
            keep = int(v)
            self.history.reload()
            n = self.history.older_than(keep) if keep > 0 else 0
            if n and not messagebox.askyesno(
                    APP_NAME, "This forgets %d entr%s older than %d day%s from your history.\n"
                    "(Only the list: files and shared links are not touched.)\n\nContinue?"
                    % (n, "y" if n == 1 else "ies", keep, "" if keep == 1 else "s"), parent=self.root):
                self._save("history_keep_days", self._history_keep_prev)   # changed their mind
                self.root.after(60, lambda: self.rebuild(settings_open=True))
                return
            self._history_keep_prev = keep
            self._apply_history_policy()
        row(card, "Forget my history automatically", "Removes the list of what you sent and received, and "
            "nothing else: files you downloaded and links you shared are not touched. “On exit” means when you "
            "quit BuzzDrop (closing the window to the tray doesn't count). Off by default.",
            seg("history_keep_days", [("0", "Off"), ("-1", "On exit"), ("1", "1 day"), ("7", "7 days"),
                                      ("30", "30 days")], after=history_changed, width=340))

        # -- Integration
        card = section("System integration")
        labels = {
            "protocol": ("Open BZ:// links with %s" % APP_NAME, "Click a BZ:// link anywhere to start the download."
                         + (" Also adds BuzzDrop to your app menu." if not (IS_WIN or IS_MAC) else "")),
            "context_menu": ("“Send with %s” in the right-click menu" % APP_NAME,
                             "Explorer" if IS_WIN else "Files (Nautilus), Nemo and Dolphin"),
            "shortcut": ("Start menu shortcut", ""),
            "cli": ("`buzz` terminal command", "buzz send FILE · buzz get LINK"),
        }
        for feature, (title, desc) in labels.items():
            if sup.get(feature):
                row(card, title, desc, integ(feature))
        if IS_MAC and not sup.get("protocol"):
            row(card, "BZ:// links", "Work automatically with the packaged BuzzDrop.app.")
        if IS_WIN:
            def bg_changed(v: bool) -> None:
                if self.shell:
                    self.shell.set_tray(v)

                def work() -> None:
                    try:
                        integration.set_feature("autostart", v)
                    except integration.IntegrationError as exc:
                        msg = str(exc)
                        self.main_q.put(lambda: self.toast("Couldn't change start-at-login: " + msg, "error"))
                threading.Thread(target=work, daemon=True).start()
            row(card, "Run in the background", "Keeps a tray icon, starts at login, and the hotkey keeps "
                "working when the window is closed.", toggle("run_in_background", bg_changed))

            def hk_ctrl(parent):
                f = tk.Frame(parent, bg=c.surface)
                self._hk_btn = Button(f, ui, self.settings.get("global_hotkey", "Ctrl+Shift+F1"), icon="keyboard",
                                      small=True, height=30, command=self._record_hotkey, bg=c.surface,
                                      tooltip="Click, then press a new shortcut")
                self._hk_btn.pack(side="right", padx=(ui.px(8), 0))
                Toggle(f, ui, bool(self.settings.get("enable_global_hotkey")),
                       lambda v: (self._save("enable_global_hotkey", v), self._apply_hotkey()),
                       bg=c.surface).pack(side="right")
                return f
            row(card, "Global hotkey", "Select a BZ:// link in any app and press the hotkey to download it.", hk_ctrl)

        # -- About
        card = section("About")

        def about_ctrl(parent):
            f = tk.Frame(parent, bg=c.surface)
            Button(f, ui, "Check for updates", small=True, height=30, command=self.check_updates_now,
                   bg=c.surface).pack(side="right")
            return f
        row(card, "%s %s" % (APP_NAME, __version__), "Free & open source · MIT licence", about_ctrl)
        row(card, "Check for updates automatically", "", toggle("check_for_updates"))
        if self._self_update_possible():
            row(card, "Download updates in the background", "The update installs the next time you close "
                "BuzzDrop. Nothing restarts while you work.", toggle("auto_update"))
        if self.update_info:
            def skip_ctrl(parent):
                f = tk.Frame(parent, bg=c.surface)
                Button(f, ui, "Skip this version", small=True, kind="ghost", height=30, bg=c.surface,
                       command=self.skip_update).pack(side="right")
                return f
            row(card, "Version %s is available" % self.update_info["version"], "", skip_ctrl)

        def links_ctrl(parent):
            f = tk.Frame(parent, bg=c.surface)
            Button(f, ui, "Logs", small=True, kind="ghost", height=30, bg=c.surface,
                   command=lambda: self.reveal(str(data_dir() / "logs"))).pack(side="right")
            Button(f, ui, "GitHub", small=True, kind="ghost", height=30, bg=c.surface,
                   command=lambda: open_url(REPO_URL)).pack(side="right")
            return f
        row(card, "Project & support", "Report a bug or suggest a feature.", links_ctrl)

        def uninstall_ctrl(parent):
            f = tk.Frame(parent, bg=c.surface)
            Button(f, ui, "Uninstall…", small=True, kind="soft", height=30, bg=c.surface,
                   command=self.uninstall_flow).pack(side="right")
            return f
        row(card, "Uninstall %s" % APP_NAME, "Removes it completely: links, menus, settings and the program "
            "itself. Downloaded files and shared links are not touched.", uninstall_ctrl)
        tk.Frame(body, bg=c.bg, height=ui.px(16)).pack()
        return frame

    def _refresh_rows(self) -> None:
        for row in self.rows.values():
            row.redraw()

    def _record_hotkey(self) -> None:
        if getattr(self, "_recording", False):
            return
        self._recording = True
        self._hk_btn.set_text("Press keys…  (Esc cancels)")
        self.root.bind("<KeyPress>", self._hotkey_key)

    def _hotkey_key(self, e) -> str:
        names = {"Alt_L", "Alt_R", "Control_L", "Control_R", "Shift_L", "Shift_R", "Win_L", "Win_R", "Meta_L", "Meta_R"}
        if e.keysym in names:
            return "break"
        self.root.unbind("<KeyPress>")
        self._recording = False
        if e.keysym == "Escape":
            self._hk_btn.set_text(self.settings.get("global_hotkey", "Ctrl+Shift+F1"))
            return "break"
        mods = []
        if e.state & 0x4:
            mods.append("Ctrl")
        if e.state & 0x20000 or e.state & 0x8 and not IS_WIN:
            mods.append("Alt")
        if e.state & 0x1:
            mods.append("Shift")
        key = e.keysym.upper() if len(e.keysym) == 1 else e.keysym
        spec = "+".join(mods + [key])
        if winsys is None or winsys.parse_hotkey(spec) is None:
            self._hk_btn.set_text(self.settings.get("global_hotkey", "Ctrl+Shift+F1"))
            self.toast("Use a modifier (Ctrl/Alt/Shift) with a letter, number or F-key.", "error")
            return "break"
        self._save("global_hotkey", spec)
        self._hk_btn.set_text(spec)
        self._apply_hotkey()
        self.toast("Hotkey set to %s" % spec, "success")
        return "break"

    def _apply_hotkey(self) -> None:
        if self.shell:
            enabled = self.settings.get("enable_global_hotkey")
            self.shell.set_hotkey(self.settings.get("global_hotkey", "Ctrl+Shift+F1") if enabled else None)

    # ---------------------------------------------------------------- updates
    def _self_update_possible(self) -> bool:
        """The packaged Windows exe in a writable folder can update itself; everything else just links to the page."""
        if self._can_update is None:
            self._can_update = updater.can_self_update()
        return self._can_update

    def _render_update_pill(self) -> None:
        for w in self.update_slot.winfo_children():
            w.destroy()
        info = self.update_info
        if not info:
            return
        slot, ui = self.update_slot, self.ui
        if self._update_state == "downloading":
            Button(slot, ui, "Updating %d%%" % int(self._update_pct * 100), kind="soft", small=True, height=30,
                   command=lambda: None, tooltip="Downloading the update. Nothing restarts.").pack()
        elif self._update_state == "ready":
            Button(slot, ui, "Restart to finish", icon="retry", kind="soft", small=True, height=30,
                   command=self.restart_for_update,
                   tooltip="Installs %s. It also installs by itself the next time you close BuzzDrop."
                   % info["version"]).pack()
        elif updater.offers_download(info) and self._self_update_possible():
            Button(slot, ui, "Update to %s" % info["version"], icon="download", kind="soft", small=True, height=30,
                   command=self.start_update_download,
                   tooltip="Download it now. Nothing restarts until you choose.").pack()
        else:
            Button(slot, ui, "Update to %s" % info["version"], icon="download", kind="soft", small=True, height=30,
                   command=lambda: open_url(info["url"]), tooltip="A new version is available").pack()

    def _check_updates(self, manual: bool) -> None:
        def work() -> None:
            info = check_for_update()

            def done() -> None:
                self.settings["last_update_check"] = time.time()
                save_settings(self.settings)
                if not info:
                    if manual:
                        self.toast("You're on the latest version.", "success")
                    return
                if info["version"] == self.settings.get("skipped_version") and not manual:
                    return  # the user chose not to be reminded about this one
                same = bool(self.update_info) and self.update_info.get("version") == info["version"]
                if not same:
                    self.update_info = info
                    self._update_state, self._update_pct, self._update_file = "idle", 0.0, None
                self._render_update_pill()
                can = updater.offers_download(info) and self._self_update_possible()
                if same and self._update_state != "idle":
                    return  # already downloading / waiting for the restart
                if can and self.settings.get("auto_update") and not self.manager.busy:
                    self._update_silent = True      # opted in: fetch it quietly, install on the next quit
                    self.start_update_download()
                    return
                self.toast("BuzzDrop %s is available" % info["version"], "info",
                           action=("Update", self.start_update_download) if can
                           else ("Get it", lambda: open_url(info["url"])), duration=8000)
            self.main_q.put(done)
        threading.Thread(target=work, daemon=True).start()

    # -- one-click update (never restarts by itself)
    def start_update_download(self) -> None:
        info = self.update_info
        if not info or self._update_state != "idle":
            return
        exe = updater.current_exe()
        if exe is None or not updater.offers_download(info) or not self._self_update_possible():
            open_url(info["url"])
            return
        self._update_state, self._update_pct = "downloading", 0.0
        self._render_update_pill()
        last = [0.0]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if total and now - last[0] > 0.3:
                last[0] = now
                self.main_q.put(lambda p=done / total: self._set_update_progress(p))

        def work() -> None:
            try:
                new = updater.download_update(info, exe.parent, progress)
                try:
                    updater.preflight(new, info["version"])      # prove it starts before offering it
                except updater.UpdateError:
                    updater._silent_unlink(new)
                    raise
            except updater.UpdateError as exc:
                msg = str(exc)
                self.main_q.put(lambda: self._update_failed(msg))
                return
            self.main_q.put(lambda: self._update_ready(new))
        threading.Thread(target=work, daemon=True, name="update").start()

    def _set_update_progress(self, pct: float) -> None:
        self._update_pct = pct
        if self._update_state == "downloading":
            self._render_update_pill()

    def _update_failed(self, msg: str) -> None:
        log.warning("update failed: %s", msg)
        self._update_state = "idle"
        self._render_update_pill()
        if not self._update_silent and self.update_info:
            url = self.update_info["url"]
            self.toast(msg, "error", action=("Open page", lambda: open_url(url)), duration=9000)
        self._update_silent = False

    def _update_ready(self, new) -> None:
        self._update_state, self._update_file = "ready", new
        self._render_update_pill()
        if not self._update_silent:
            self.toast("BuzzDrop %s is ready. Restart whenever you like." % self.update_info["version"], "success",
                       action=("Restart", self.restart_for_update), duration=8000)
        self._update_silent = False

    def restart_for_update(self) -> None:
        if self._update_state != "ready" or self._update_applied or not self._update_file:
            return
        if self.manager.busy and not messagebox.askyesno(
                APP_NAME, "Transfers are still running.\nCancel them and restart to update?",
                icon="warning", parent=self.root):
            return
        exe = updater.current_exe()
        try:
            updater.swap_in(exe, self._update_file)
        except updater.UpdateError as exc:
            self.toast(str(exc), "error", duration=7000)
            return
        self._update_applied = True
        try:
            updater.relaunch(exe, os.getpid())
        except OSError as exc:
            log.warning("could not relaunch: %s", exc)
            self.toast("Updated. Close and reopen BuzzDrop to use the new version.", "success", duration=9000)
            return
        self.quit()

    def _apply_pending_update(self) -> None:
        """A downloaded update installs by itself when the app is closed for real (no restart)."""
        if self._update_state != "ready" or self._update_applied or not self._update_file:
            return
        exe = updater.current_exe()
        try:
            updater.swap_in(exe, self._update_file)
            self._update_applied = True
            log.info("installed update on quit")
        except Exception as exc:
            log.warning("could not install the update on quit: %s", exc)

    def skip_update(self) -> None:
        info = self.update_info
        if not info:
            return
        self._save("skipped_version", info["version"])
        if self._update_file:
            updater._silent_unlink(self._update_file)
        self.update_info, self._update_state, self._update_file = None, "idle", None
        self._render_update_pill()
        self.toast("OK, I won't remind you about %s." % info["version"], "info")
        if self.in_settings:
            self.rebuild(settings_open=True)

    def check_updates_now(self) -> None:
        self.toast("Checking for updates…", "info", duration=2000)
        self._check_updates(manual=True)

    # ------------------------------------------------------------- lifecycle
    def _geometry(self) -> None:
        ui = self.ui
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = ui.px(520), min(ui.px(740), sh - ui.px(80))
        saved = self.settings.get("window")
        if isinstance(saved, list) and len(saved) == 4:
            sw_, sh_, sx, sy = (int(v) for v in saved)
            if ui.px(380) <= sw_ <= sw and ui.px(480) <= sh_ <= sh and -50 < sx < sw - 100 and -10 < sy < sh - 100:
                self.root.geometry("%dx%d+%d+%d" % (sw_, sh_, sx, sy))
                return
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 2 - ui.px(20))))

    def _style_titlebar(self) -> None:
        if IS_WIN and winsys is not None:
            try:
                winsys.style_titlebar(self.root, self.ui.dark, caption=self.ui.c.bg, text=self.ui.c.text)
            except Exception:
                pass

    def show_window(self) -> None:
        r = self.root
        if IS_WIN and not getattr(self, "_shown_once", False):
            # The first time, keep the window see-through for a moment so its contents finish
            # drawing before they become visible (no half-built flash).
            self._shown_once = True
            try:
                r.attributes("-alpha", 0.0)
                r.after(140, lambda: r.attributes("-alpha", 1.0))
            except tk.TclError:
                pass
        r.deiconify()
        r.lift()
        if IS_WIN:
            try:
                r.attributes("-topmost", True)
                r.after(200, lambda: r.attributes("-topmost", False))
            except tk.TclError:
                pass
        r.focus_force()
        for delay in (10, 200, 700):  # the window may not be fully shown yet on the first try
            r.after(delay, self._style_titlebar)
        r.after(350, self._nudge_titlebar_once)

    def _nudge_titlebar_once(self) -> None:
        """Some Windows setups keep a white title bar until the window is resized; do that once, invisibly."""
        if not IS_WIN or winsys is None or getattr(self, "_frame_nudged", False):
            return
        try:
            self._frame_nudged = winsys.nudge_frame(self.root)
        except Exception:
            pass

    def _bind_keys(self) -> None:
        r = self.root
        # Restyle the title bar whenever the window is (re)mapped or gains focus.
        r.bind("<Map>", lambda e: e.widget is r and r.after(30, self._style_titlebar), add="+")
        r.bind("<FocusIn>", lambda e: e.widget is r and r.after(30, self._style_titlebar), add="+")
        r.bind_all("<%s-o>" % MOD, lambda e: self.choose_files())
        r.bind_all("<%s-O>" % MOD, lambda e: self.choose_folder())
        r.bind_all("<%s-v>" % MOD, self._smart_paste, add="+")
        r.bind_all("<%s-comma>" % MOD, lambda e: self.open_settings())
        for i, name in enumerate(("send", "receive", "history"), 1):
            r.bind_all("<%s-Key-%d>" % (MOD, i), lambda e, n=name: self.show_page(n))
        r.bind_all("<Escape>", self._escape)
        r.bind_all("<%s-w>" % MOD, lambda e: self.on_close())
        r.bind_all("<%s-q>" % MOD, lambda e: self.quit())
        r.bind("<FocusIn>", self._on_focus, add="+")

    def _escape(self, _e=None) -> None:
        if getattr(self, "_recording", False):
            return
        if self.in_settings:
            self.close_settings()
        else:
            self.toaster.dismiss()

    def _smart_paste(self, e) -> None:
        if isinstance(e.widget, tk.Entry):
            return
        files = winsys.clipboard_files() if IS_WIN and winsys is not None else []
        if files:
            self.send_paths(files)
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        codes = find_codes(text)
        if codes:
            self.receive_codes(codes)
        elif text.strip() and os.path.exists(text.strip().strip('"')):
            self.send_paths([text.strip().strip('"')])
        else:
            self.toast("Copy files or a BZ:// link first, then paste here.", "info")

    def _platform_hooks(self) -> None:
        if IS_WIN and winsys is not None:
            try:
                self.drop = winsys.FileDrop(self.root, lambda paths: self.main_q.put(lambda: self.send_paths(paths)))
            except Exception:
                log.exception("drag & drop unavailable")
            try:
                self.shell = winsys.ShellThread(str(integration.icon_path("ico")), find_codes)
                self.shell.set_tray(bool(self.settings.get("run_in_background")))
                self._apply_hotkey()
            except Exception:
                log.exception("tray/hotkey unavailable")
                self.shell = None
        if IS_MAC:
            r = self.root
            r.createcommand("::tk::mac::OpenDocument", lambda *p: self.handle_payload({"cmd": "send", "paths": list(p)}))
            r.createcommand("::tk::mac::LaunchURL", lambda url: self.handle_payload(
                {"cmd": "receive", "codes": parse_codes(url)}))
            r.createcommand("::tk::mac::ReopenApplication", self.show_window)
            r.createcommand("::tk::mac::ShowPreferences", self.open_settings)
            r.createcommand("::tk::mac::Quit", self.quit)
        self.root.report_callback_exception = self._on_error

    def _on_error(self, exc, val, tb) -> None:
        log.error("UI error", exc_info=(exc, val, tb))
        self.toast("Something went wrong — details are in the log.", "error")

    def _startup_tasks(self) -> None:
        s = self.settings
        if os.environ.get("BUZZ_NO_INTEGRATION"):
            pass  # development / tests: never touch the registry or desktop files
        elif not s.get("integration_setup_done"):
            def first_run() -> None:
                done = integration.setup_defaults()
                s["integration_setup_done"] = True
                self.main_q.put(lambda: (save_settings(s), done and self.toast(
                    "BZ:// links now open in BuzzDrop", "success",
                    action=("Settings", self.open_settings), duration=6000)))
            threading.Thread(target=first_run, daemon=True).start()
        else:
            threading.Thread(target=integration.heal, daemon=True).start()
        if s.get("check_for_updates", True) and time.time() - float(s.get("last_update_check") or 0) > 86400:
            self._check_updates(manual=False)
        self._apply_history_policy()
        self.root.after(30 * 60 * 1000, self._history_timer)
        exe = updater.current_exe()
        if exe is not None:  # once the app has run fine for a while, drop the previous version's backup
            def tidy() -> None:
                if self._update_state == "idle":
                    threading.Thread(target=updater.cleanup_leftovers, args=(exe,), daemon=True).start()
            self.root.after(30000, tidy)
        if not self.background:
            self._check_clipboard(force=True)

    def _flush_sends(self) -> None:
        self._send_job = None
        paths, self._pending_send = self._pending_send, []
        if paths:
            self.send_paths(paths)

    def handle_payload(self, msg: Dict[str, Any], initial: bool = False) -> None:
        cmd = msg.get("cmd", "open")
        if cmd == "quit":          # sent by the uninstaller (token-protected, same user only)
            self.quit()
            return
        if cmd == "send" and msg.get("paths"):
            # Explorer starts one process per selected file; gather them for a moment so
            # "send multiple files as one ZIP" and the single-window flow see them together.
            self.show_window()
            self._pending_send.extend(p for p in msg["paths"] if p not in self._pending_send)
            if self._send_job:
                self.root.after_cancel(self._send_job)
            self._send_job = self.root.after(700, self._flush_sends)
        elif cmd == "receive" and msg.get("codes"):
            self.show_window()
            self.receive_codes(msg["codes"])
        elif cmd == "settings":
            self.show_window()
            self.open_settings()
        elif not initial and not msg.get("background"):
            self.show_window()

    # clipboard watching (only when the window gains focus — no polling)
    def _on_focus(self, _e=None) -> None:
        self.root.after(150, self._check_clipboard)

    def _check_clipboard(self, force: bool = False) -> None:
        if not self.settings.get("watch_clipboard", True):
            return
        now = time.monotonic()
        if not force and now - self._clip_check < 1.0:
            return
        self._clip_check = now
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        if not text or text == self._last_clip or len(text) > 20000:
            return
        self._last_clip = text
        known = {e.get("code") for e in self.history.items} | {j.code for j in self.manager.jobs if j.code}
        known |= {j.result.get("code") for j in self.manager.jobs if j.result}
        codes = [c for c in find_codes(text) if c not in known and c not in self.suggested]
        if not codes:
            return
        self.suggested.update(codes)
        label = "BZ://%s" % codes[0] if len(codes) == 1 else "%d BuzzHeavier links" % len(codes)
        self.toast("Found %s in your clipboard" % label, "clipboard",
                   action=("Download", lambda: self.receive_codes(codes)), duration=9000)

    # job events --------------------------------------------------------------
    def _finished(self, job: Job) -> None:
        row = self.rows.get(job.id)
        if row:
            row.refresh(force=True)
        if job.state == DONE and job.kind == SEND:
            code, name = job.result["code"], job.result["name"]
            link = share_link(code, self.settings.get("link_style", "bz"))
            self.history.add("send", name, code, job.result.get("size", 0), job.paths[0] if job.paths else "")
            now = time.monotonic()
            self._recent_links = [(t, l) for t, l in self._recent_links if now - t < 6] + [(now, link)]
            if self.settings.get("copy_link", True):
                links = [l for _, l in self._recent_links]
                self.copy("\n".join(links))
                msg = ("Link copied · %s" % link) if len(links) == 1 else "%d links copied" % len(links)
            else:
                msg = "Uploaded · %s" % link
            self.toast(msg, "success", action=("Open", lambda: open_url(web_link(code))))
            self.notify("Upload complete", "%s\n%s" % (name, link))
        elif job.state == DONE:
            target = job.result.get("extracted") or job.result.get("path")
            self.history.add("receive", job.result.get("name", ""), job.code, job.result.get("size", 0), target or "")
            if self.settings.get("launch_folder_after_download", True):
                self.reveal(target)
            note = job.result.get("extract_note")
            self.toast(note or ("Saved %s" % os.path.basename(target or "")), "info" if note else "success",
                       action=("Show", lambda: self.reveal(target)))
            self.notify("Download complete", os.path.basename(target or ""))
        elif job.state == FAILED:
            if job.blocked and self.settings.get("open_blocked_in_browser") and self._auto_open_budget():
                # BuzzHeavier wants a real browser for this file: hand it over right away.
                open_url(web_link(job.code))
                self.toast("BuzzHeavier asked for a browser check — opened the link in your browser.", "info",
                           action=("Open again", lambda: open_url(web_link(job.code))), duration=7000)
            else:
                action = (("Open in browser", lambda: open_url(web_link(job.code))) if job.blocked
                          else ("Retry", lambda: self.retry(job)))
                self.toast("%s — %s" % (job.error_title or "Failed", job.error or ""), "error", action=action,
                           duration=7000)
                self.notify(job.error_title or "Transfer failed", job.error or job.label)
        self._hist_dirty = True
        if self.page == "history" and not self.in_settings:
            self._render_history()
        other = RECEIVE if job.kind == SEND else SEND
        if job.state == DONE and self.page == other:
            self.tabs.set_badge(job.kind, True)

    def _shell_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "codes":
            self.show_window()
            self.receive_codes(ev[1])
        elif kind == "nolink":
            self.toast("No BZ:// link in the selected text.", "info")
            self.notify(APP_NAME, "No BZ:// link found in the selected text.")
        elif kind == "hotkey_failed":
            self.toast("%s is already used by another app — choose another in Settings." % ev[1], "error",
                       action=("Settings", self.open_settings), duration=8000)
        elif kind == "open":
            self.show_window()
        elif kind == "receive_clipboard":
            try:
                codes = find_codes(self.root.clipboard_get())
            except tk.TclError:
                codes = []
            self.show_window()
            if codes:
                self.receive_codes(codes)
            else:
                self.toast("No BZ:// link in your clipboard.", "info")
        elif kind == "send":
            self.show_window()
            self.choose_files()
        elif kind == "quit":
            self.quit()

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            if self.server is not None:
                while not self.server.messages.empty():
                    self.handle_payload(self.server.messages.get_nowait())
            if self.shell is not None:
                while not self.shell.events.empty():
                    self._shell_event(self.shell.events.get_nowait())
            while not self.manager.events.empty():
                self._finished(self.manager.events.get_nowait())
            while not self.main_q.empty():
                self.main_q.get_nowait()()
            active = [j for j in self.manager.jobs if j.active]
            for job in active:
                row = self.rows.get(job.id)
                if row is None:
                    continue
                if row.winfo_exists():
                    row.refresh()
            self._title_progress(active)
        except Exception:
            log.exception("tick failed")
        self.root.after(70 if self.manager.busy else 250, self._tick)

    def _title_progress(self, active: List[Job]) -> None:
        known = [j for j in active if j.total > 0 and j.state == RUNNING]
        if known:
            pct = int(100 * sum(j.done for j in known) / max(1, sum(j.total for j in known)))
            title = "%d%% · %s" % (pct, APP_NAME)
        else:
            title = APP_NAME
        if self.root.title() != title:
            self.root.title(title)

    def on_close(self) -> None:
        if IS_WIN and self.shell and self.settings.get("run_in_background"):
            self._save_geometry()
            self.root.withdraw()
            if not self.settings.get("_tray_hint_shown"):
                self._save("_tray_hint_shown", True)
                self.shell.notify(APP_NAME, "Still running in the tray — your hotkey keeps working.")
            return
        if self.manager.busy:
            if not messagebox.askyesno(APP_NAME, "Transfers are still running.\nCancel them and quit?",
                                       icon="warning", parent=self.root):
                return
        self.quit()

    def _save_geometry(self) -> None:
        try:
            if self.root.state() == "normal":
                self.settings["window"] = [self.root.winfo_width(), self.root.winfo_height(),
                                           self.root.winfo_x(), self.root.winfo_y()]
                save_settings(self.settings)
        except tk.TclError:
            pass

    def quit(self) -> None:
        if self._closing:
            return
        self._save_geometry()
        self._closing = True
        self._apply_pending_update()
        if self.server is not None:
            self.server.close()  # new launches must not hand their link to a closing window
        self.manager.cancel_all()
        if self.shell:
            self.shell.stop()
        self.root.withdraw()
        deadline = time.monotonic() + 6.0

        def wait() -> None:  # let cancelled transfers delete their partial files
            if self.manager.busy and time.monotonic() < deadline:
                self.root.after(50, wait)
            else:
                self._apply_history_policy(closing=True)   # "forget on quit", after cancelled transfers settled
                if self._uninstalling is not None:
                    self._finish_uninstall()               # delete settings + program, now that nothing writes
                self.root.destroy()
        wait()


def run(payload: Dict[str, Any], server) -> int:
    if IS_WIN and winsys is not None:
        winsys.set_dpi_awareness()
        winsys.set_app_id(APP_ID)
    root = tk.Tk(className=APP_NAME)
    root.withdraw()
    if tk.TkVersion < 8.6:  # e.g. Apple's /usr/bin/python3 ships Tk 8.5, which can't load PNG images
        messagebox.showerror(APP_NAME, "BuzzDrop needs Tk 8.6 or newer (this Python has Tk %s).\n\n"
                             "Use the packaged app, or install Python from python.org." % tk.TkVersion)
        root.destroy()
        return 1
    App(root, payload, server)
    root.mainloop()
    return 0

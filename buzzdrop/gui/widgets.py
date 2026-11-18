"""A small widget kit on top of tkinter: rounded buttons, toggles, segmented
controls, inputs, cards, smooth scroll areas, toasts and tooltips."""

from __future__ import annotations

import sys
import time
import tkinter as tk
from typing import Callable, List, Optional, Sequence, Tuple

from .theme import UI, ellipsize

IS_MAC = sys.platform == "darwin"


def _ease(t: float) -> float:
    return 1 - (1 - t) ** 3


def animate(widget: tk.Misc, duration_ms: int, step: Callable[[float], None],
            done: Optional[Callable[[], None]] = None) -> None:
    frames = max(1, duration_ms // 15)
    state = {"i": 0}

    def tick() -> None:
        state["i"] += 1
        t = state["i"] / frames
        try:
            step(_ease(min(1.0, t)))
        except tk.TclError:
            return
        if t < 1.0:
            widget.after(15, tick)
        elif done:
            done()

    tick()


# --------------------------------------------------------------------------
# Tooltip
# --------------------------------------------------------------------------
class Tooltip:
    def __init__(self, widget: tk.Widget, ui: UI, text: str) -> None:
        self.widget, self.ui, self.text = widget, ui, text
        self.tip: Optional[tk.Toplevel] = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self.job = self.widget.after(550, self._show)

    def _cancel(self) -> None:
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self) -> None:
        if self.tip or not self.text:
            return
        c = self.ui.c
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        try:
            self.tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(self.tip, text=self.text, bg=c.toast, fg=c.toast_text, font=self.ui.f.small,
                 padx=self.ui.px(8), pady=self.ui.px(4), bd=0).pack()
        self.tip.update_idletasks()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2 - self.tip.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + self.ui.px(6)
        self.tip.geometry("+%d+%d" % (max(0, x), y))

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------
class Button(tk.Canvas):
    """Rounded button. kinds: primary · secondary · ghost · soft · danger."""

    def __init__(self, parent: tk.Misc, ui: UI, text: str = "", icon: Optional[str] = None,
                 kind: str = "secondary", command: Optional[Callable[[], None]] = None,
                 height: int = 36, width: Optional[int] = None, small: bool = False,
                 bg: Optional[str] = None, tooltip: Optional[str] = None) -> None:
        self.ui, self.kind, self.command = ui, kind, command
        self.text, self.icon_name, self.small = text, icon, small
        self.req_width, self.req_height = width, height
        self.enabled = True
        self._hover = self._press = False
        super().__init__(parent, bg=bg or parent.cget("bg"), highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self._draw()
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        if tooltip:
            Tooltip(self, ui, tooltip)

    def _colors(self) -> Tuple[Optional[str], Optional[str], Optional[str], str, str]:
        c = self.ui.c
        k = self.kind
        if not self.enabled:
            if k == "primary":
                return c.surface3, c.surface3, None, c.text3, c.text3
            return (c.surface2 if k != "ghost" else None), None, (c.border if k == "secondary" else None), c.text3, c.text3
        if k == "primary":
            return c.accent, c.accent2, None, c.accent_text, c.accent_text
        if k == "soft":
            return c.accent_soft, _mixed(c.accent_soft, c.accent, 0.12), None, c.text, c.text
        if k == "danger":
            return c.danger_soft, _mixed(c.danger_soft, c.danger, 0.12), None, c.danger, c.danger
        if k == "ghost":
            return None, c.surface2, None, c.text2, c.text
        return c.surface2, c.surface3, c.border, c.text, c.text

    def _draw(self) -> None:
        ui = self.ui
        self.delete("all")
        font = ui.f.small_b if self.small else ui.f.btn
        H = ui.px(self.req_height)
        isz = 14 if self.small else 16
        tw = font.measure(self.text) if self.text else 0
        iw = ui.px(isz) if self.icon_name else 0
        gap = ui.px(7) if self.text and self.icon_name else 0
        if self.text:
            padx = ui.px(11 if self.small else 15)
        else:
            padx = (H - iw) // 2
        W = ui.px(self.req_width) if self.req_width else tw + iw + gap + 2 * padx
        self.configure(width=W, height=H)
        fill, hover, border, fg, fg_hover = self._colors()
        r = min(H / 2.0, ui.px(9))
        self._imgs = {
            "normal": ui.rrect(W, H, r, fill, border) if fill else None,
            "hover": ui.rrect(W, H, r, hover or fill, border) if (hover or fill) else None,
        }
        self._bg = self.create_image(0, 0, anchor="nw")
        x = (W - (tw + iw + gap)) / 2.0
        self._fg = (fg, fg_hover)
        self._ic = None
        if self.icon_name:
            self._icons = (ui.icon(self.icon_name, isz, fg), ui.icon(self.icon_name, isz, fg_hover))
            self._ic = self.create_image(x, H / 2.0, anchor="w", image=self._icons[0])
            x += iw + gap
        self._tx = self.create_text(x, H / 2.0, anchor="w", text=self.text, font=font, fill=fg) if self.text else None
        self._refresh()

    def _refresh(self) -> None:
        hover = (self._hover or self._press) and self.enabled
        img = self._imgs["hover" if hover else "normal"]
        self.itemconfigure(self._bg, image=img or "")
        if self._ic:
            self.itemconfigure(self._ic, image=self._icons[1 if hover else 0])
        if self._tx:
            self.itemconfigure(self._tx, fill=self._fg[1 if hover else 0])
        self.configure(cursor="hand2" if self.enabled else "arrow")

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        if not on:
            self._press = False
        self._refresh()

    def _on_press(self, _e) -> None:
        self._press = True
        self._refresh()

    def _on_release(self, e) -> None:
        was = self._press
        self._press = False
        self._refresh()
        if was and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.invoke()

    def invoke(self) -> None:
        if self.enabled and self.command:
            self.command()

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self._draw()

    def set_enabled(self, on: bool) -> None:
        if on != self.enabled:
            self.enabled = on
            self._draw()


def _mixed(a: str, b: str, t: float) -> str:
    from .draw import mix
    return mix(a, b, t)


class IconButton(tk.Canvas):
    def __init__(self, parent: tk.Misc, ui: UI, icon: str, command: Optional[Callable[[], None]] = None,
                 size: int = 34, icon_size: int = 18, tooltip: Optional[str] = None, bg: Optional[str] = None,
                 color: Optional[str] = None, hover_bg: Optional[str] = None) -> None:
        bg = bg or parent.cget("bg")
        S = ui.px(size)
        super().__init__(parent, width=S, height=S, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        c = ui.c
        self.ui, self.command = ui, command
        self._imgs = (ui.icon(icon, icon_size, color or c.text2), ui.icon(icon, icon_size, c.text))
        self._hbg = ui.rrect(S, S, S / 2.0, hover_bg or c.surface3)
        self._bgi = self.create_image(0, 0, anchor="nw")
        self._ici = self.create_image(S / 2.0, S / 2.0, image=self._imgs[0])
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        if tooltip:
            Tooltip(self, ui, tooltip)

    def _hover(self, on: bool) -> None:
        self.itemconfigure(self._bgi, image=self._hbg if on else "")
        self.itemconfigure(self._ici, image=self._imgs[1 if on else 0])

    def _click(self, e) -> None:
        if self.command and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()


# --------------------------------------------------------------------------
# Toggle switch
# --------------------------------------------------------------------------
class Toggle(tk.Canvas):
    def __init__(self, parent: tk.Misc, ui: UI, value: bool, command: Callable[[bool], None],
                 bg: Optional[str] = None) -> None:
        W, H = ui.px(40), ui.px(23)
        super().__init__(parent, width=W, height=H, bg=bg or parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        c = ui.c
        self.ui, self.value, self.command = ui, bool(value), command
        self.W, self.H = W, H
        self.k = H - ui.px(6)
        self._on = ui.rrect(W, H, H / 2.0, c.accent)
        self._off = ui.rrect(W, H, H / 2.0, c.surface3, c.border2)
        self._knob = ui.circle(int(round(self.k / ui.s)), c.knob)
        self._track = self.create_image(0, 0, anchor="nw")
        self._k = self.create_image(0, H / 2.0, anchor="w", image=self._knob)
        self._render(1.0 if self.value else 0.0)
        self.bind("<ButtonRelease-1>", lambda e: self.toggle())

    def _render(self, pos: float) -> None:
        pad = self.ui.px(3)
        x = pad + pos * (self.W - 2 * pad - self.k)
        self.coords(self._k, x, self.H / 2.0)
        self.itemconfigure(self._track, image=self._on if pos >= 0.5 else self._off)

    def set(self, value: bool, animate_it: bool = True) -> None:
        value = bool(value)
        if value == self.value:
            return
        start = 1.0 if self.value else 0.0
        self.value = value
        end = 1.0 if value else 0.0
        if animate_it:
            animate(self, 140, lambda t: self._render(start + (end - start) * t))
        else:
            self._render(end)

    def toggle(self) -> None:
        self.set(not self.value)
        self.command(self.value)


# --------------------------------------------------------------------------
# Segmented control
# --------------------------------------------------------------------------
class Segmented(tk.Canvas):
    def __init__(self, parent: tk.Misc, ui: UI, options: Sequence[Tuple[str, str]], value: str,
                 command: Callable[[str], None], height: int = 38, width: Optional[int] = None,
                 bg: Optional[str] = None, small: bool = False) -> None:
        super().__init__(parent, height=ui.px(height), bg=bg or parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        if width:
            self.configure(width=ui.px(width))
        self.ui, self.options, self.value, self.command = ui, list(options), value, command
        self.small = small
        self._x = None
        self._badges = {}
        self.bind("<Configure>", lambda e: self._layout())
        self.bind("<ButtonRelease-1>", self._click)

    def _index(self, value: str) -> int:
        for i, (v, _) in enumerate(self.options):
            if v == value:
                return i
        return 0

    def _layout(self) -> None:
        ui, c = self.ui, self.ui.c
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20:
            return
        n = len(self.options)
        pad = ui.px(3)
        self.seg_w = (w - 2 * pad) / n
        r = min(h / 2.0, ui.px(10))
        self._track = ui.rrect(w, h, r, c.surface2, c.border)
        self._ind = ui.rrect(int(self.seg_w), h - 2 * pad, r - pad,
                             c.surface3 if ui.dark else c.surface, None if ui.dark else c.border)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._track)
        x = pad + self._index(self.value) * self.seg_w
        self._x = x
        self._ind_item = self.create_image(x, pad, anchor="nw", image=self._ind)
        font = ui.f.small_b if self.small else ui.f.body_b
        self._labels = []
        for i, (v, label) in enumerate(self.options):
            cx = pad + (i + 0.5) * self.seg_w
            t = self.create_text(cx, h / 2.0, text=label, font=font,
                                 fill=c.text if v == self.value else c.text2)
            self._labels.append(t)
            if self._badges.get(v):
                tw = font.measure(label)
                self.create_oval(cx + tw / 2 + ui.px(4), h / 2 - ui.px(6), cx + tw / 2 + ui.px(10), h / 2,
                                 fill=c.accent, outline="")

    def set_badge(self, value: str, on: bool) -> None:
        if self._badges.get(value) != on:
            self._badges[value] = on
            self._layout()

    def set(self, value: str, notify: bool = False) -> None:
        if value == self.value:
            return
        self.value = value
        if self._x is None:
            return
        c = self.ui.c
        for i, (v, _) in enumerate(self.options):
            self.itemconfigure(self._labels[i], fill=c.text if v == value else c.text2)
        start = self._x
        end = self.ui.px(3) + self._index(value) * self.seg_w
        self._x = end

        def step(t: float) -> None:
            self.coords(self._ind_item, start + (end - start) * t, self.ui.px(3))
        animate(self, 170, step)
        if notify:
            self.command(value)

    def _click(self, e) -> None:
        if self._x is None:
            return
        i = int((e.x - self.ui.px(3)) // self.seg_w)
        i = max(0, min(len(self.options) - 1, i))
        v = self.options[i][0]
        if v != self.value:
            self.set(v)
            self.command(v)


# --------------------------------------------------------------------------
# Rounded containers & inputs
# --------------------------------------------------------------------------
class RoundFrame(tk.Frame):
    """A frame with an anti-aliased rounded background that follows its size."""

    def __init__(self, parent: tk.Misc, ui: UI, fill: str, border: Optional[str] = None,
                 radius: Optional[int] = None, bg: Optional[str] = None, **kw) -> None:
        super().__init__(parent, bg=bg or parent.cget("bg"), **kw)
        self.ui, self.fill, self.border = ui, fill, border
        self.radius = ui.px(12) if radius is None else radius
        self._lbl = tk.Label(self, bd=0, bg=self.cget("bg"), highlightthickness=0, padx=0, pady=0)
        self._lbl.place(x=0, y=0, relwidth=1, relheight=1)
        self._lbl.lower()
        self._job = None
        self._size = (0, 0)
        self.bind("<Configure>", self._schedule, add="+")

    def _schedule(self, _e=None) -> None:
        if self._job is None:
            self._job = self.after_idle(self._redraw)

    def _redraw(self) -> None:
        self._job = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        self._size = (w, h)
        self._img = self.ui.rrect(w, h, self.radius, self.fill, self.border)
        self._lbl.configure(image=self._img)

    def set_colors(self, fill: Optional[str] = None, border: Optional[str] = None) -> None:
        self.fill = fill or self.fill
        self.border = border
        self._redraw()


class Input(RoundFrame):
    def __init__(self, parent: tk.Misc, ui: UI, placeholder: str = "", height: int = 40,
                 icon: Optional[str] = None, on_submit: Optional[Callable[[], None]] = None,
                 on_change: Optional[Callable[[str], None]] = None, font=None, fill: Optional[str] = None) -> None:
        c = ui.c
        fill = fill or c.surface2
        super().__init__(parent, ui, fill=fill, border=c.border, radius=ui.px(10), height=ui.px(height))
        self.pack_propagate(False)
        self.on_change = on_change
        if icon:
            self._icon_img = ui.icon(icon, 16, c.text3)  # keep a reference: Tk drops unreferenced images
            tk.Label(self, image=self._icon_img, bg=fill, bd=0).pack(side="left", padx=(ui.px(12), 0))
        self.var = tk.StringVar()
        self.entry = tk.Entry(self, textvariable=self.var, bd=0, relief="flat", highlightthickness=0,
                              bg=fill, fg=c.text, insertbackground=c.text, font=font or ui.f.body,
                              selectbackground=c.select, selectforeground=c.text,
                              insertwidth=max(1, ui.px(1.5)))
        self.entry.pack(side="left", fill="x", expand=True, padx=(ui.px(10 if icon else 13), ui.px(12)))
        self.ph = tk.Label(self, text=placeholder, bg=fill, fg=c.text3, font=font or ui.f.body,
                           bd=0, padx=0, pady=0, anchor="w", cursor="xterm")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.var.trace_add("write", lambda *a: self._changed())
        self.entry.bind("<FocusIn>", lambda e: self.set_colors(border=c.accent), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.set_colors(border=c.border), add="+")
        if on_submit:
            self.entry.bind("<Return>", lambda e: on_submit())
        self.after_idle(self._changed)

    def _changed(self) -> None:
        try:
            if self.var.get():
                self.ph.place_forget()
            else:  # leave the caret visible at the left edge
                self.ph.place(in_=self.entry, x=self.ui.px(3), rely=0.5, anchor="w")
        except tk.TclError:
            return
        if self.on_change:
            self.on_change(self.var.get())

    def get(self) -> str:
        return self.var.get()

    def set(self, text: str) -> None:
        self.var.set(text)
        self.entry.icursor("end")

    def focus(self) -> None:
        self.entry.focus_set()


# --------------------------------------------------------------------------
# Scroll area with a slim auto-hiding scrollbar and smooth wheel scrolling
# --------------------------------------------------------------------------
_wheel_roots = set()


def _install_wheel(root: tk.Misc) -> None:
    """One global wheel handler that scrolls whichever ScrollArea is under the pointer
    (per-widget <Enter>/<Leave> bindings break as soon as the pointer is over a child)."""
    key = str(root)
    if key in _wheel_roots:
        return
    _wheel_roots.add(key)

    def handler(e) -> None:
        try:
            w = root.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return
        while w is not None:
            area = getattr(w, "_scroll_area", None)
            if area is not None:
                area._wheel(e)
                return
            w = getattr(w, "master", None)

    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(seq, handler, add="+")
class ScrollArea(tk.Frame):
    def __init__(self, parent: tk.Misc, ui: UI, bg: Optional[str] = None, settle_ms: int = 0) -> None:
        bg = bg or parent.cget("bg")
        super().__init__(parent, bg=bg)
        self.ui = ui
        self.settle_ms = settle_ms  # >0: for heavy pages, refit the content only once resizing stops
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=1)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.bar = tk.Canvas(self, width=ui.px(10), bg=bg, highlightthickness=0, bd=0)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._thumb = None
        self._drag = None
        for w in (self, self.canvas, self.inner, self.bar):
            w._scroll_area = self  # found by the global wheel handler
        _install_wheel(self.winfo_toplevel())
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bar.bind("<ButtonPress-1>", self._bar_press)
        self.bar.bind("<B1-Motion>", self._bar_drag)

    def _on_canvas(self, e) -> None:
        # Re-laying out a page with hundreds of widgets for every pixel of a window drag makes
        # resizing choppy. Apply the new width at most every ~60 ms while dragging, and always
        # once more when the size settles, so the final layout is exact.
        self._w_pending = e.width
        now = time.monotonic()
        if getattr(self, "_w_last", 0.0) == 0.0:
            self._apply_width()  # first layout: always immediate
        elif self.settle_ms:
            # Heavy page (Settings): leave the content alone while the window edge is being
            # dragged, then fit it to the final width when no new size arrives for a moment.
            if getattr(self, "_w_job", None) is not None:
                self.after_cancel(self._w_job)
            self._w_job = self.after(self.settle_ms, self._apply_width)
        elif now - self._w_last >= 0.06:
            self._apply_width()
        elif getattr(self, "_w_job", None) is None:
            self._w_job = self.after(70, self._apply_width)

    def _apply_width(self) -> None:
        self._w_job = None
        self._w_last = time.monotonic()
        self.canvas.itemconfigure(self._win, width=self._w_pending)
        self._update()

    def _content_h(self) -> int:
        return max(self.inner.winfo_reqheight(), 1)

    def _update(self) -> None:
        h = self._content_h()
        view = self.canvas.winfo_height()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), max(h, view)))
        self._draw_bar()

    def _draw_bar(self) -> None:
        self.bar.delete("all")
        h, view = self._content_h(), self.canvas.winfo_height()
        if h <= view + 1 or view < 10:
            self._thumb = None
            return
        top, bottom = self.canvas.yview()
        pad = self.ui.px(4)
        H = self.bar.winfo_height() - 2 * pad
        y1, y2 = pad + top * H, pad + bottom * H
        x = self.ui.px(5)
        self._thumb = (y1, y2)
        self.bar.create_line(x, y1 + 2, x, y2 - 2, width=self.ui.px(4), capstyle="round",
                             fill=self.ui.c.border2)

    def scroll_px(self, dy: int) -> None:
        if self._content_h() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(int(dy), "units")
        self._draw_bar()

    def scroll_top(self) -> None:
        self.canvas.yview_moveto(0)
        self._draw_bar()

    def _wheel(self, e) -> None:
        if e.num == 4:
            delta = 1
        elif e.num == 5:
            delta = -1
        elif IS_MAC:
            delta = e.delta / 3.0
        else:
            delta = e.delta / 120.0
        self.scroll_px(-delta * self.ui.px(56))

    def _bar_press(self, e) -> None:
        self._drag = (e.y, self.canvas.yview()[0])

    def _bar_drag(self, e) -> None:
        if not self._drag:
            return
        y0, top0 = self._drag
        H = max(1, self.bar.winfo_height())
        self.canvas.yview_moveto(top0 + (e.y - y0) / H)
        self._draw_bar()


# --------------------------------------------------------------------------
# Toasts
# --------------------------------------------------------------------------
class Toaster:
    ICONS = {"success": ("check", "success"), "error": ("alert", "danger"),
             "info": ("zap", "accent"), "clipboard": ("clipboard", "accent")}

    def __init__(self, root: tk.Tk, ui: UI) -> None:
        self.root, self.ui = root, ui
        self.cv: Optional[tk.Canvas] = None
        self._hide_job = None
        self._hovered = False

    def show(self, text: str, kind: str = "info", action: Optional[Tuple[str, Callable[[], None]]] = None,
             duration: int = 3800) -> None:
        self.dismiss(now=True)
        ui, c = self.ui, self.ui.c
        f = ui.f.body
        icon, color = self.ICONS.get(kind, self.ICONS["info"])
        H = ui.px(46)
        max_w = max(ui.px(220), self.root.winfo_width() - ui.px(48))
        aw = ui.f.body_b.measure(action[0]) + ui.px(18) if action else 0
        text = ellipsize(f, text, max_w - aw - ui.px(60), middle=False)
        W = ui.px(16) + ui.px(18) + ui.px(10) + f.measure(text) + aw + ui.px(18)
        cv = tk.Canvas(self.root, width=W, height=H, bg=c.bg, highlightthickness=0, bd=0)
        self._img = ui.rrect(W, H, ui.px(12), c.toast)
        cv.create_image(0, 0, anchor="nw", image=self._img)
        self._ic = ui.icon(icon, 18, getattr(c, color))
        cv.create_image(ui.px(16), H / 2, anchor="w", image=self._ic)
        cv.create_text(ui.px(44), H / 2, anchor="w", text=text, font=f, fill=c.toast_text)
        if action:
            ax = W - ui.px(16) - ui.f.body_b.measure(action[0])
            t = cv.create_text(ax, H / 2, anchor="w", text=action[0], font=ui.f.body_b, fill=c.accent2)
            cv.tag_bind(t, "<Enter>", lambda e: cv.configure(cursor="hand2"))
            cv.tag_bind(t, "<Leave>", lambda e: cv.configure(cursor=""))
            cv.tag_bind(t, "<ButtonRelease-1>", lambda e: (self.dismiss(), action[1]()))
        cv.bind("<Enter>", lambda e: self._set_hover(True))
        cv.bind("<Leave>", lambda e: self._set_hover(False))
        self.cv = cv
        self._duration = duration
        start, end = H, -ui.px(18)

        def step(t: float) -> None:
            if self.cv is cv:
                cv.place(relx=0.5, rely=1.0, y=start + (end - start) * t, anchor="s")
                tk.Misc.tkraise(cv)  # Canvas.lift() raises canvas *items*, not the widget
        animate(cv, 180, step)
        self._hide_job = self.root.after(duration, self.dismiss)

    def _set_hover(self, on: bool) -> None:
        self._hovered = on
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        if not on:
            self._hide_job = self.root.after(1600, self.dismiss)

    def dismiss(self, now: bool = False) -> None:
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        cv, self.cv = self.cv, None
        if cv is None:
            return
        if now:
            cv.destroy()
            return
        H = cv.winfo_height()

        def step(t: float) -> None:
            cv.place(relx=0.5, rely=1.0, y=-self.ui.px(18) + (H + self.ui.px(18)) * t, anchor="s")
        animate(cv, 160, step, done=cv.destroy)

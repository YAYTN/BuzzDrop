"""Colours, fonts and the per-window UI context (scale + image cache)."""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict
from types import SimpleNamespace
from typing import Optional

from . import draw

ACCENT = "#62a79a"  # BuzzDrop teal

# accent_ink = the accent when used as *text/icons* (contrast ≥ 4.5:1 on that theme's surfaces).
# accent_text = text on accent-filled buttons (dark ink: white on #62a79a is only 2.8:1).
DARK = dict(
    bg="#0c1110", surface="#141a19", surface2="#1a2221", surface3="#232d2b", hover="#182120",
    border="#232d2b", border2="#31403d", text="#eef3f2", text2="#a2b1ae", text3="#6a7a77",
    accent=ACCENT, accent2="#78bbae", accent_ink="#7fc4b7", accent_text="#05201b",
    success="#5fd39a", danger="#f87171", info="#8aa8f8",
    toast="#25302e", toast_text="#eef3f2", knob="#ffffff", select="#1f3d37",
)
LIGHT = dict(
    bg="#f2f6f5", surface="#ffffff", surface2="#edf2f1", surface3="#e0e8e6", hover="#f7faf9",
    border="#dde5e3", border2="#c9d4d1", text="#0f1716", text2="#52605d", text3="#84928f",
    accent=ACCENT, accent2="#72b6a9", accent_ink="#2c7366", accent_text="#05201b",
    success="#15803d", danger="#dc2626", info="#2563eb",
    toast="#1a2422", toast_text="#f2f6f5", knob="#ffffff", select="#cfe6e1",
)

# Accent colours people can pick in Settings → Appearance. "teal" is the hand-tuned default.
ACCENTS = {
    "teal": ACCENT, "blue": "#5b8def", "violet": "#8b7cf6",
    "rose": "#e0698f", "amber": "#d9992f", "green": "#5db36a",
}


# Background styles people can pick in Settings → Appearance. Each overrides the neutral
# colours of the dark and light theme; "default" keeps the hand-tuned teal-tinted look.
# name -> (dark overrides, light overrides)
PALETTES = {
    "default": ({}, {}),
    "ocean": (  # blue-tinted
        dict(bg="#0b0f1a", surface="#121826", surface2="#182030", surface3="#222c42", hover="#151c2c",
             border="#222c42", border2="#313e5a", text="#edf1fa", text2="#a3afc8", text3="#7886a6"),
        dict(bg="#eef2f9", surface="#ffffff", surface2="#e8edf6", surface3="#d9e1ef", hover="#f5f8fd",
             border="#d9e1ef", border2="#c3cee3", text="#0f1522", text2="#4d5a73", text3="#6f7c97"),
    ),
    "neutral": (  # plain gray
        dict(bg="#101010", surface="#181818", surface2="#1f1f1f", surface3="#2a2a2a", hover="#1c1c1c",
             border="#2a2a2a", border2="#3a3a3a", text="#f0f0f0", text2="#a8a8a8", text3="#7c7c7c"),
        dict(bg="#f3f3f3", surface="#ffffff", surface2="#ececec", surface3="#dedede", hover="#f8f8f8",
             border="#e0e0e0", border2="#cccccc", text="#141414", text2="#565656", text3="#767676"),
    ),
    "warm": (  # brownish dark / paper-like light
        dict(bg="#14110e", surface="#1c1814", surface2="#24201b", surface3="#2f2923", hover="#1f1b17",
             border="#2f2923", border2="#43392f", text="#f4efe9", text2="#b5aa9c", text3="#8a7e70"),
        dict(bg="#f7f2ea", surface="#fffdf9", surface2="#f0e9dd", surface3="#e4dac9", hover="#fbf7f0",
             border="#e6dccb", border2="#d3c6b0", text="#1d1812", text2="#5f564a", text3="#7d7364"),
    ),
    "pure": (  # true black (saves power on OLED screens) / true white
        dict(bg="#000000", surface="#0a0a0a", surface2="#121212", surface3="#1c1c1c", hover="#0e0e0e",
             border="#1c1c1c", border2="#2c2c2c", text="#f5f5f5", text2="#a5a5a5", text3="#7c7c7c"),
        dict(bg="#ffffff", surface="#ffffff", surface2="#f4f4f4", surface3="#e8e8e8", hover="#fafafa",
             border="#ebebeb", border2="#d6d6d6", text="#000000", text2="#4a4a4a", text3="#767676"),
    ),
}
PALETTE_LABELS = [("default", "Default"), ("ocean", "Ocean"), ("neutral", "Neutral"),
                  ("warm", "Warm"), ("pure", "Pure")]


def _linear(v: float) -> float:
    v /= 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    r, g, b = draw.rgb(color)
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _accent_colors(base: str, palette: dict, dark: bool) -> dict:
    """Derive the accent variants for *base* so text/icons stay readable (≥ 4.5:1) on this theme."""
    toward = "#ffffff" if dark else "#000000"
    ink = base
    for step in range(0, 21):  # nudge towards white (dark theme) / black (light theme) until legible
        ink = draw.mix(base, toward, step / 20.0)
        if all(_contrast(ink, palette[k]) >= 4.5 for k in ("bg", "surface", "surface2")):
            break
    on_accent = "#0b0f0e" if _contrast("#0b0f0e", base) >= _contrast("#ffffff", base) else "#ffffff"
    return dict(accent=base, accent2=draw.mix(base, "#ffffff", 0.15), accent_ink=ink, accent_text=on_accent,
                select=_selection(base, palette, dark))


def _selection(accent: str, palette: dict, dark: bool) -> str:
    return draw.mix(accent, palette["bg"] if dark else "#ffffff", 0.72)


def build_palette(dark: bool, accent: str = "teal", style: str = "default") -> dict:
    """All colours for one combination of light/dark theme, accent colour and background style."""
    palette = dict(DARK if dark else LIGHT)
    variant = PALETTES.get(style, PALETTES["default"])[0 if dark else 1]
    palette.update(variant)
    if variant:  # tinted pieces that follow the background
        palette["toast"] = draw.mix(palette["text"], palette["bg"], 0.1) if not dark else \
            draw.mix(palette["surface3"], "#ffffff", 0.06)  # dark pop-up on light, lighter pop-up on dark
        palette["toast_text"] = palette["text"] if dark else palette["bg"]
    if accent in ACCENTS and accent != "teal":
        palette.update(_accent_colors(ACCENTS[accent], palette, dark))
    elif variant:
        palette["select"] = _selection(palette["accent"], palette, dark)
    return palette


_FAMILIES = ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter",
             "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans")
_SEMIBOLD = ("Segoe UI Variable Display Semib", "Segoe UI Semibold")
_MONO = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono")


class UI:
    """Everything widgets need: colours (``c``), scale (``px``), fonts (``f``) and cached images."""

    def __init__(self, root: tk.Tk, dark: bool, accent: str = "teal", style: str = "default") -> None:
        self.root = root
        self.dark = dark
        palette = build_palette(dark, accent, style)
        palette["accent_soft"] = draw.mix(palette["accent"], palette["surface"], 0.82 if dark else 0.84)
        palette["danger_soft"] = draw.mix(palette["danger"], palette["surface"], 0.86)
        palette["success_soft"] = draw.mix(palette["success"], palette["surface"], 0.86)
        palette["info_soft"] = draw.mix(palette["info"], palette["surface"], 0.86 if dark else 0.9)
        self.c = SimpleNamespace(**palette)
        if sys.platform == "darwin":
            self.s = 1.0
        else:
            self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.f = self._fonts()
        self._cache: "OrderedDict[tuple, tk.PhotoImage]" = OrderedDict()

    # -- metrics -------------------------------------------------------------
    def px(self, n: float) -> int:
        return int(round(n * self.s))

    def _fonts(self) -> SimpleNamespace:
        fams = set(tkfont.families(self.root))
        family = next((f for f in _FAMILIES if f in fams), tkfont.nametofont("TkDefaultFont").actual("family"))
        semi = next((f for f in _SEMIBOLD if f in fams), None)
        mono = next((f for f in _MONO if f in fams), "TkFixedFont")
        base = 13 if sys.platform == "darwin" else 10

        def font(size_delta: int = 0, bold: bool = False, fam: Optional[str] = None) -> tkfont.Font:
            if bold and semi and fam is None:
                return tkfont.Font(root=self.root, family=semi, size=base + size_delta)
            return tkfont.Font(root=self.root, family=fam or family, size=base + size_delta,
                               weight="bold" if bold else "normal")

        return SimpleNamespace(
            body=font(0), body_b=font(0, True), small=font(-1), small_b=font(-1, True),
            tiny=font(-2), tiny_b=font(-2, True), title=font(3, True), h2=font(1, True),
            mono=font(-1, fam=mono), btn=font(0, True), big=font(5, True),
        )

    # -- images --------------------------------------------------------------
    def _get(self, key: tuple, build) -> tk.PhotoImage:
        img = self._cache.get(key)
        if img is not None:
            self._cache.move_to_end(key)
            return img
        w, h, data = build()
        img = tk.PhotoImage(master=self.root, data=draw.png_b64(w, h, data), format="png")
        self._cache[key] = img
        # Only size-dependent rounded rects pile up (window resizing); icons are a small fixed
        # set and stay cached forever, so a label that only references an icon never goes blank.
        if len(self._cache) > 400:
            stale = next((k for k in self._cache if k[0] == "rr"), None)
            if stale is not None:
                del self._cache[stale]  # owners hold their own reference
        return img

    def rrect(self, w: int, h: int, r: float, fill: str, border: Optional[str] = None,
              bw: Optional[float] = None) -> tk.PhotoImage:
        w, h = max(2, int(w)), max(2, int(h))
        bw = self.s if bw is None else bw
        return self._get(("rr", w, h, r, fill, border, bw), lambda: (w, h, draw.rrect(w, h, r, fill, border, bw)))

    def icon(self, name: str, size: int, color: str) -> tk.PhotoImage:
        px = self.px(size)
        return self._get(("ic", name, px, color), lambda: (px, px, draw.icon_rgba(name, px, color)))

    def circle(self, size: int, color: str) -> tk.PhotoImage:
        px = self.px(size)
        return self._get(("ci", px, color), lambda: (px, px, draw.circle_rgba(px, color)))

    def logo(self, size: int, scale: bool = True) -> tk.PhotoImage:
        px = self.px(size) if scale else size
        return self._get(("logo", px), lambda: (px, px, draw.logo_rgba(px)))


def ellipsize(font: tkfont.Font, text: str, width: int, middle: bool = True) -> str:
    """Shorten *text* to fit *width* pixels; keeps the extension visible for file names."""
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    ell = "…"
    if middle and len(text) > 12:
        tail = text[-8:] if "." not in text[-10:] else text[text.rfind("."):][-10:]
        tail = text[-max(len(tail), 6):]
        head = text[: len(text) - len(tail)]
        lo, hi = 0, len(head)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(head[:mid] + ell + tail) <= width:
                lo = mid
            else:
                hi = mid - 1
        if lo > 0:
            return head[:lo].rstrip() + ell + tail
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + ell) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell

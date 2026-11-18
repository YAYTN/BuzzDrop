"""Anti-aliased shapes and icons, rendered in pure Python.

Tk's canvas doesn't anti-alias on Windows or X11, so rounded corners and icons
drawn with it look jagged. Everything round in the UI is rendered here instead:
signed-distance-field coverage → RGBA → PNG → ``PhotoImage``, cached by the caller.
Icons follow a 24-unit grid with 2-unit strokes (Lucide-style).
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

RGB = Tuple[int, int, int]


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------
def rgb(c: str) -> RGB:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hexc(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (int(round(r)), int(round(g)), int(round(b)))


def mix(a: str, b: str, t: float) -> str:
    """Blend colour *a* towards *b* by *t* (0 → a, 1 → b)."""
    ra, rb = rgb(a), rgb(b)
    return hexc(*(x + (y - x) * t for x, y in zip(ra, rb)))


# --------------------------------------------------------------------------
# PNG encoding
# --------------------------------------------------------------------------
def encode_png(w: int, h: int, rgba: bytes) -> bytes:
    stride = w * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def png_b64(w: int, h: int, rgba: bytes) -> str:
    return base64.b64encode(encode_png(w, h, rgba)).decode("ascii")


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


# --------------------------------------------------------------------------
# Rounded rectangle (optimised: only the corner bands are evaluated)
# --------------------------------------------------------------------------
def rrect(w: int, h: int, r: float, fill: str, border: str = None, bw: float = 1.0) -> bytes:
    w, h = max(1, int(w)), max(1, int(h))
    r = max(0.0, min(float(r), w / 2.0, h / 2.0))
    F = rgb(fill)
    B = rgb(border) if border else F
    has_border = bool(border) and bw > 0
    hw, hh = w / 2.0, h / 2.0
    bx = min(int(math.ceil(r + bw + 1)), (w + 1) // 2)
    by = min(int(math.ceil(r + bw + 1)), (h + 1) // 2)

    def pixel(x: int, y: int) -> bytes:
        qx = abs(x + 0.5 - hw) - (hw - r)
        qy = abs(y + 0.5 - hh) - (hh - r)
        d = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
        a = _clamp01(0.5 - d)
        if a <= 0.0:
            return b"\x00\x00\x00\x00"
        if has_border:
            k = _clamp01(0.5 - (d + bw)) / a
            c = (F[0] * k + B[0] * (1 - k), F[1] * k + B[1] * (1 - k), F[2] * k + B[2] * (1 - k))
        else:
            c = F
        return bytes((int(c[0] + 0.5), int(c[1] + 0.5), int(c[2] + 0.5), int(a * 255 + 0.5)))

    def row(y: int) -> bytes:
        left = [pixel(x, y) for x in range(bx)]
        mid_n = w - 2 * bx
        mid = pixel(bx, y) * mid_n if mid_n > 0 else b""
        right = [pixel(x, y) for x in range(max(bx, w - bx), w)]
        return b"".join(left) + mid + b"".join(right)

    top = [row(y) for y in range(by)]
    mid_rows = h - 2 * by
    middle = row(by) * mid_rows if mid_rows > 0 else b""
    bottom = [row(y) for y in range(max(by, h - by), h)]
    return b"".join(top) + middle + b"".join(bottom)


# --------------------------------------------------------------------------
# Generic SDF renderer (icons, logo, circles)
# --------------------------------------------------------------------------
Layer = Tuple[Callable[[float, float], float], object]  # (distance in units, colour or fn(y)->RGB)


def render(w: int, h: int, layers: Sequence[Layer], unit: float = 1.0) -> bytes:
    """Composite *layers* (back to front). Distances are in units; ``unit`` = pixels per unit."""
    out = bytearray(w * h * 4)
    prepared = [(fn, rgb(col) if isinstance(col, str) else col) for fn, col in layers]
    i = 0
    inv = 1.0 / unit
    for y in range(h):
        uy = (y + 0.5) * inv
        for x in range(w):
            ux = (x + 0.5) * inv
            r = g = b = a = 0.0
            for fn, col in prepared:
                cov = 0.5 - fn(ux, uy) * unit
                if cov <= 0.0:
                    continue
                if cov > 1.0:
                    cov = 1.0
                c = col(uy) if callable(col) else col
                r = c[0] * cov + r * (1 - cov)
                g = c[1] * cov + g * (1 - cov)
                b = c[2] * cov + b * (1 - cov)
                a = cov + a * (1 - cov)
            if a > 0.0:
                out[i] = int(r / a + 0.5)
                out[i + 1] = int(g / a + 0.5)
                out[i + 2] = int(b / a + 0.5)
                out[i + 3] = int(a * 255 + 0.5)
            i += 4
    return bytes(out)


# --------------------------------------------------------------------------
# Distance primitives (unit space)
# --------------------------------------------------------------------------
def d_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else _clamp01(((px - ax) * dx + (py - ay) * dy) / l2)
    return math.hypot(px - ax - dx * t, py - ay - dy * t)


def d_poly(px: float, py: float, pts: Sequence[Tuple[float, float]]) -> float:
    return min(d_seg(px, py, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


def d_box(px: float, py: float, cx: float, cy: float, hw: float, hh: float, r: float) -> float:
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def d_circle(px: float, py: float, cx: float, cy: float, r: float) -> float:
    return math.hypot(px - cx, py - cy) - r


def d_arc(px: float, py: float, cx: float, cy: float, r: float, a0: float, a1: float) -> float:
    """Distance to an arc from angle a0 to a1 (degrees, y-down, increasing = clockwise)."""
    ang = math.degrees(math.atan2(py - cy, px - cx))
    span = (a1 - a0) % 360 or 360
    if (ang - a0) % 360 <= span:
        return abs(math.hypot(px - cx, py - cy) - r)
    e0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
    e1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    return min(math.hypot(px - e0[0], py - e0[1]), math.hypot(px - e1[0], py - e1[1]))


def d_hex(px: float, py: float, cx: float, cy: float, r: float) -> float:
    """Pointy-top hexagon with inradius *r*."""
    x, y = abs(py - cy), abs(px - cx)  # swap axes → pointy top
    kx, ky, kz = -0.866025404, 0.5, 0.577350269
    dot = min(kx * x + ky * y, 0.0)
    x -= 2 * dot * kx
    y -= 2 * dot * ky
    x -= max(-kz * r, min(kz * r, x))
    y -= r
    return math.hypot(x, y) * (1 if y > 0 else -1)


# --------------------------------------------------------------------------
# Icons (24-unit grid, stroke 2)
# --------------------------------------------------------------------------
S = 1.0  # half stroke


def _p(*pts):
    return pts


ICONS: Dict[str, Callable[[float, float], float]] = {
    "upload": lambda x, y: min(d_poly(x, y, _p((12, 15), (12, 3.5))), d_poly(x, y, _p((7, 8.5), (12, 3.5), (17, 8.5))),
                               d_poly(x, y, _p((4, 15), (4, 19), (20, 19), (20, 15)))) - S,
    "download": lambda x, y: min(d_poly(x, y, _p((12, 3.5), (12, 15))), d_poly(x, y, _p((7, 10), (12, 15), (17, 10))),
                                 d_poly(x, y, _p((4, 15), (4, 19), (20, 19), (20, 15)))) - S,
    "x": lambda x, y: min(d_seg(x, y, 6, 6, 18, 18), d_seg(x, y, 18, 6, 6, 18)) - S,
    "check": lambda x, y: d_poly(x, y, _p((4.5, 12.5), (9.5, 17.5), (19.5, 6.5))) - S,
    "copy": lambda x, y: min(
        abs(d_box(x, y, 15, 15, 7, 7, 2)) - S,
        max(abs(d_box(x, y, 9, 9, 7, 7, 2)) - S, -d_box(x, y, 15, 15, 9, 9, 3))),
    "globe": lambda x, y: min(
        abs(d_circle(x, y, 12, 12, 9.5)), d_seg(x, y, 2.5, 12, 21.5, 12), d_seg(x, y, 12, 2.5, 12, 21.5),
        d_seg(x, y, 3.63, 7.5, 20.37, 7.5), d_seg(x, y, 3.63, 16.5, 20.37, 16.5)) - S,
    "folder": lambda x, y: d_poly(x, y, _p((4, 20), (20, 20), (22, 18), (22, 8), (20, 6), (12.1, 6), (10.4, 5.1),
                                           (9.6, 3.9), (7.9, 3), (4, 3), (2, 5), (2, 18), (4, 20))) - S,
    "external": lambda x, y: min(d_poly(x, y, _p((15, 3), (21, 3), (21, 9))), d_seg(x, y, 10, 14, 21, 3),
                                 d_poly(x, y, _p((18, 13), (18, 19), (16, 21), (5, 21), (3, 19), (3, 8), (5, 6), (11, 6)))) - S,
    "back": lambda x, y: d_poly(x, y, _p((15, 18), (9, 12), (15, 6))) - S,
    "sliders": lambda x, y: min(
        d_seg(x, y, 3, 6, 9.5, 6), d_seg(x, y, 14.5, 6, 21, 6), abs(d_circle(x, y, 12, 6, 2.2)),
        d_seg(x, y, 3, 12, 5.5, 12), d_seg(x, y, 10.5, 12, 21, 12), abs(d_circle(x, y, 8, 12, 2.2)),
        d_seg(x, y, 3, 18, 13.5, 18), d_seg(x, y, 18.5, 18, 21, 18), abs(d_circle(x, y, 16, 18, 2.2))) - S,
    "history": lambda x, y: min(
        d_arc(x, y, 12, 12, 9, -135, 180), d_seg(x, y, 5.64, 5.64, 3, 8),
        d_poly(x, y, _p((3, 3), (3, 8), (8, 8))), d_poly(x, y, _p((12, 7), (12, 12), (16, 14)))) - S,
    "retry": lambda x, y: min(
        d_arc(x, y, 12, 12, 9, 0, 315), d_seg(x, y, 18.36, 5.64, 21, 8),
        d_poly(x, y, _p((21, 3), (21, 8), (16, 8)))) - S,
    "trash": lambda x, y: min(
        d_seg(x, y, 3, 6, 21, 6), d_poly(x, y, _p((19, 6), (19, 20), (17, 22), (7, 22), (5, 20), (5, 6))),
        d_poly(x, y, _p((8, 6), (8, 4), (10, 2), (14, 2), (16, 4), (16, 6)))) - S,
    "clipboard": lambda x, y: min(
        abs(d_box(x, y, 12, 4, 4, 2, 1)),
        d_poly(x, y, _p((16, 4), (18, 4), (20, 6), (20, 20), (18, 22), (6, 22), (4, 20), (4, 6), (6, 4), (8, 4)))) - S,
    "file": lambda x, y: min(
        d_poly(x, y, _p((15, 2), (6, 2), (4, 4), (4, 20), (6, 22), (18, 22), (20, 20), (20, 7), (15, 2))),
        d_poly(x, y, _p((14, 2), (14, 6), (16, 8), (20, 8)))) - S,
    "plus": lambda x, y: min(d_seg(x, y, 12, 5, 12, 19), d_seg(x, y, 5, 12, 19, 12)) - S,
    "search": lambda x, y: min(abs(d_circle(x, y, 11, 11, 7)), d_seg(x, y, 21, 21, 16.65, 16.65)) - S,
    "alert": lambda x, y: min(abs(d_circle(x, y, 12, 12, 10)), d_seg(x, y, 12, 7.5, 12, 12.5),
                              d_seg(x, y, 12, 16.4, 12, 16.6)) - S,
    "sun": lambda x, y: min(
        abs(d_circle(x, y, 12, 12, 4)),
        *(d_seg(x, y, 12 + 7.2 * math.cos(math.radians(a)), 12 + 7.2 * math.sin(math.radians(a)),
                12 + 9.2 * math.cos(math.radians(a)), 12 + 9.2 * math.sin(math.radians(a)))
          for a in range(0, 360, 45))) - S,
    "moon": lambda x, y: abs(max(d_circle(x, y, 12, 12, 9), -d_circle(x, y, 17.5, 6.5, 6.6))) - S,
    "keyboard": lambda x, y: min(abs(d_box(x, y, 12, 12, 10, 7, 2)), d_seg(x, y, 8, 15.5, 16, 15.5),
                                 *(d_seg(x, y, kx, ky, kx + 0.01, ky) for kx in (6.5, 10, 13.5, 17.5) for ky in (8.5, 12))) - S,
    "zap": lambda x, y: d_poly(x, y, _p((13, 2), (4, 14), (12, 14), (11, 22), (20, 10), (12, 10), (13, 2))) - S,
}


def icon_rgba(name: str, size: int, color: str) -> bytes:
    fn = ICONS[name]
    return render(size, size, [(fn, color)], unit=size / 24.0)


def circle_rgba(size: int, color: str) -> bytes:
    r = size / 2.0
    return render(size, size, [(lambda x, y: d_circle(x, y, r, r, r - 0.5), color)], unit=1.0)


def d_polygon(px: float, py: float, pts: Sequence[Tuple[float, float]]) -> float:
    """Signed distance to a filled polygon (negative inside)."""
    d = (px - pts[0][0]) ** 2 + (py - pts[0][1]) ** 2
    s = 1.0
    j = len(pts) - 1
    for i in range(len(pts)):
        vix, viy = pts[i]
        ex, ey = pts[j][0] - vix, pts[j][1] - viy
        wx, wy = px - vix, py - viy
        t = _clamp01((wx * ex + wy * ey) / (ex * ex + ey * ey))
        bx, by = wx - ex * t, wy - ey * t
        d = min(d, bx * bx + by * by)
        c1, c2, c3 = py >= viy, py < pts[j][1], ex * wy > ey * wx
        if (c1 and c2 and c3) or (not c1 and not c2 and not c3):
            s = -s
        j = i
    return s * math.sqrt(d)


def d_drop(px: float, py: float, cx: float, cy: float, r1: float, r2: float, h: float) -> float:
    """Teardrop: circle of radius r1 at (cx, cy) tapering up to radius r2, h units higher."""
    x, y = abs(px - cx), cy - py
    b = (r1 - r2) / h
    a = math.sqrt(1 - b * b)
    k = -b * x + a * y
    if k < 0:
        return math.hypot(x, y) - r1
    if k > a * h:
        return math.hypot(x, y - h) - r2
    return x * a + y * b - r1


# --------------------------------------------------------------------------
# Logo — a droplet ("drop your files") with a bolt ("buzz", fast) on a teal tile
# --------------------------------------------------------------------------
_TEAL_TOP, _TEAL_BOTTOM = rgb("#80c6b8"), rgb("#4b8d81")
_DROP, _BOLT = rgb("#f6fbfa"), rgb("#3f8a7d")
_BOLT_PTS = ((13.2, 9.2), (9.2, 14.4), (11.9, 14.4), (11.0, 18.3), (14.9, 12.9), (12.3, 12.9))


def _teal(y: float) -> RGB:
    t = _clamp01((y - 1.0) / 22.0)
    return tuple(a + (b - a) * t for a, b in zip(_TEAL_TOP, _TEAL_BOTTOM))  # type: ignore[return-value]


def logo_rgba(size: int) -> bytes:
    tile = lambda x, y: d_box(x, y, 12, 12, 11, 11, 5.4)  # noqa: E731
    drop = lambda x, y: d_drop(x, y, 12, 14.2, 5.6, 0.75, 9.8)  # noqa: E731
    bolt = lambda x, y: d_polygon(x, y, _BOLT_PTS) - 0.15  # noqa: E731
    return render(size, size, [(tile, _teal), (drop, _DROP), (bolt, _BOLT)], unit=size / 24.0)


# --------------------------------------------------------------------------
# Icon files for packaging (.ico / .icns / .png)
# --------------------------------------------------------------------------
def build_ico(sizes: Iterable[int] = (16, 20, 24, 32, 40, 48, 64, 128, 256)) -> bytes:
    images = [(s, encode_png(s, s, logo_rgba(s))) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def build_icns(png_by_size: Dict[int, bytes]) -> bytes:
    types = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10"}
    body = b""
    for size, data in sorted(png_by_size.items()):
        if size in types:
            body += types[size] + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(body) + 8) + body

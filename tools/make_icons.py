"""Regenerate buzzdrop/assets/icon.{png,ico,icns} from the vector logo.

    python tools/make_icons.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buzzdrop.gui import draw  # noqa: E402


def main() -> None:
    out = ROOT / "buzzdrop" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    pngs = {}
    for size in (16, 32, 64, 128, 256, 512):
        pngs[size] = draw.encode_png(size, size, draw.logo_rgba(size))
        print("rendered", size)
    (out / "icon.png").write_bytes(pngs[256])
    (out / "icon.ico").write_bytes(draw.build_ico())
    (out / "icon.icns").write_bytes(draw.build_icns(pngs))
    print("wrote", ", ".join(p.name for p in sorted(out.iterdir())))


if __name__ == "__main__":
    main()

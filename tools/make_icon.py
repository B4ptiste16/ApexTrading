"""
APEX/BAPTOU · PNG → .ico converter  (v4.1.0)
─────────────────────────────────────────────────────────────────────
build.bat runs this BEFORE PyInstaller. If assets/icon.png (or
assets/baptou_logo.png as a fallback) exists, it generates an
assets/icon.ico containing the standard Windows icon sizes
(16/32/48/64/128/256) embedded as a single multi-resolution .ico.

Requires Pillow. If Pillow isn't installed OR no source PNG exists,
the script no-ops gracefully (keeps the existing icon.ico if any).
"""

import sys
from pathlib import Path

ROOT      = Path(__file__).parent.parent
ASSETS    = ROOT / "assets"
PRIMARY   = ASSETS / "icon.png"            # ideal: square, no text
FALLBACK  = ASSETS / "baptou_logo.png"     # full logo, will be auto-cropped
OUT       = ASSETS / "icon.ico"

def main():
    src = PRIMARY if PRIMARY.exists() else (FALLBACK if FALLBACK.exists() else None)
    if not src:
        print("[make_icon] no source PNG (assets/icon.png or assets/baptou_logo.png) — skipping")
        return
    try:
        from PIL import Image
    except ImportError:
        print("[make_icon] Pillow not installed — run 'pip install pillow' to enable icon conversion")
        return
    try:
        img = Image.open(src).convert("RGBA")
        # If source isn't square, crop to a centered square so the
        # icon doesn't end up letterboxed in Windows.
        w, h = img.size
        if w != h:
            side = min(w, h)
            left = (w - side) // 2
            top  = (h - side) // 2
            img  = img.crop((left, top, left + side, top + side))
            print(f"[make_icon] auto-cropped {src.name} {w}x{h} -> {side}x{side}")
        sizes = [(s, s) for s in (16, 32, 48, 64, 128, 256)]
        img.save(OUT, format="ICO", sizes=sizes)
        print(f"[make_icon] wrote {OUT} ({len(sizes)} sizes from {src.name})")
    except Exception as e:
        print(f"[make_icon] conversion failed: {e}")

if __name__ == "__main__":
    main()

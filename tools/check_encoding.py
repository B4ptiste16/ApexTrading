"""Report any lines still containing control characters or unmatched mojibake."""
from pathlib import Path

ROOT = Path(__file__).parent.parent

BAD_CODEPOINTS = {0x008F, 0x0081, 0x0090, 0x009D, 0x0097, 0x0096}


def check(rel_path):
    path = ROOT / rel_path
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    for i, line in enumerate(lines, 1):
        bad = [c for c in line if ord(c) in BAD_CODEPOINTS]
        if bad:
            safe = "".join(c if ord(c) < 0x80 else f"<U+{ord(c):04X}>" for c in line.strip()[:100])
            print(f"  Line {i}: {safe}")
            found = True
    if not found:
        print(f"  {rel_path}: clean")


for f in ["main.py", "ui/overview.py", "core/updater.py"]:
    print(f"--- {f} ---")
    check(f)

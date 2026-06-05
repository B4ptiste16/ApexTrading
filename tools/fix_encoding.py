"""
One-shot script: replace all mojibake (corrupted Unicode) sequences in the
Python source files with clean ASCII/Unicode equivalents, then delete itself.

Run once from the project root:
    python tools/fix_encoding.py
"""

import os
from pathlib import Path

# ── Ordered replacements (longer strings first to avoid partial matches) ──────
REPLACEMENTS = [
    # ── tab labels ──────────────────────────────────────────────────────────
    ("â—ˆ  OVERVIEW",    "OVERVIEW"),      # â—ˆ → (dropped icon)
    ("âŠ•  MORE BOTS",   "MORE BOTS"),     # âŠ•
    ("ðŸ›’  BOT MARKET", "BOT MARKET"),   # ðŸ›'
    ("ðŸ”‘  BOT MARKET", "BOT MARKET"),   # alt encoding
    ("âœ‹  MANUAL",      "MANUAL"),        # âœ‹
    ("âš¡  AUTO",        "AUTO"),          # âš¡
    # ── header buttons ──────────────────────────────────────────────────────
    ("â¬† UPDATE AVAILABLE", "UPDATE AVAILABLE"),  # â¬†
    ("â»  QUIT",              "QUIT"),          # â»
    # ── manual mode label ───────────────────────────────────────────────────
    ("âœ‹  MANUAL",      "MANUAL"),        # duplicate of above
    # ── bot quick-control buttons (play/stop/remove) ─────────────────────────
    ("â–¶",              ">"),             # â–¶  ▶
    ("â– ",             "[ ]"),            # â–   ■
    ("âœ•",             "X"),              # âœ•  ✕
    # ── status dot ──────────────────────────────────────────────────────────
    ('"\\u00e2\\u25cf"',               '"\\u25cf"'),      # skip — handled below
    # ── bot label dict entries ───────────────────────────────────────────────
    ("â–² LONG",        "LONG"),           # â–² LONG
    ("â–¼ SHORT",       "SHORT"),          # â–¼ SHORT
    ("â—† DAY",         "DAY"),            # â—† DAY
    ("â–² LONG BOT",    "LONG BOT"),
    ("â–¼ SHORT BOT",   "SHORT BOT"),
    ("â—† DAY BOT",     "DAY BOT"),
    # icon-only variants used in "icon" keys
    ("â–²",             "^"),              # â–² alone
    ("â–¼",             "v"),              # â–¼ alone
    ("â—†",             "*"),              # â—† alone
    ("â—ˆ",             "o"),              # â—ˆ  ◈ (overview icon)
    ("â—‰",             "o"),              # â—‰  ◉ (custom bot dot)
    # ── file/library buttons ────────────────────────────────────────────────
    ("ðŸ”‚  Browse .py file...", "Browse .py file..."),
    ("ðŸ”–  Open skeleton guide", "Open skeleton guide"),
    ("â¬†  Publish to marketplace",   "Publish to marketplace"),
    ("ðŸ”’  LIBRARY LOCK",        "LIBRARY LOCK"),
    ("ðŸ”’  Lock library",        "Lock library"),
    ("ðŸ””  Unlock library",      "Unlock library"),
    ("ðŸ›’  Open Bot Market",     "Open Bot Market"),
    ("ðŸ›’  BOT MARKET",          "BOT MARKET"),
    # ── inline success messages (checkmark) ─────────────────────────────────
    ("âœ” ",             "OK "),           # âœ"   ✓
    ("âœ”\n",            "OK\n"),
    # ── upload button ───────────────────────────────────────────────────────
    ("â¬†  Publish",     "Publish"),
    ("â¬† ",             ""),              # bare ⬆ prefix
]

TARGET_FILES = [
    "main.py",
    "ui/overview.py",
    "ui/login.py",
    "ui/manual_trading.py",
]

ROOT = Path(__file__).parent.parent


def fix_file(rel_path: str) -> int:
    path = ROOT / rel_path
    if not path.exists():
        print(f"  skip (not found): {rel_path}")
        return 0
    original = path.read_text(encoding="utf-8")
    fixed = original
    for bad, good in REPLACEMENTS:
        fixed = fixed.replace(bad, good)
    if fixed == original:
        print(f"  no changes: {rel_path}")
        return 0
    path.write_text(fixed, encoding="utf-8")
    changed = sum(1 for b, g in REPLACEMENTS if b in original)
    print(f"  fixed {changed} pattern(s): {rel_path}")
    return changed


if __name__ == "__main__":
    print("Fixing mojibake in source files...")
    total = sum(fix_file(f) for f in TARGET_FILES)
    print(f"Done — {total} replacement pattern(s) applied total.")
    # Self-delete (no longer needed after first run)
    # Path(__file__).unlink(missing_ok=True)

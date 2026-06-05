"""Fix remaining mojibake: status dot and emoji button labels."""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def fix_file(rel_path):
    path = ROOT / rel_path
    if not path.exists():
        print(f"  skip: {rel_path}")
        return
    text = path.read_text(encoding="utf-8")
    original = text

    # 1. Status dot: corrupted ● stored as â (U+00E2) + — (U+2014) + U+008F
    status_dot = "â—"
    text = text.replace(status_dot, "●")   # ● (BLACK CIRCLE, widely supported)

    # 2. Emoji button labels: strip any non-ASCII prefix before known label text.
    #    Pattern: opening-quote, some non-ASCII/space chars, then the actual text.
    def strip_emoji_prefix(t, label):
        # Match: open-quote + junk-chars + optional-spaces + label
        pattern = r'"[^\x00-\x7e"]{1,8}\s*(' + re.escape(label) + r')'
        return re.sub(pattern, r'"\1', t)

    for label in [
        "Browse .py file...",
        "Open skeleton guide",
        "Publish to marketplace",
        "LIBRARY LOCK",
        "Lock library",
        "Unlock library",
        "BOT MARKET",
        "Open Bot Market",
    ]:
        text = strip_emoji_prefix(text, label)

    # 3. Checkmark prefix (âœ" = U+00E2 U+0153 U+2039) -> "OK "
    checkmark = "âœ‹ "
    text = text.replace(checkmark, "OK ")

    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"  fixed: {rel_path}")
    else:
        print(f"  no change: {rel_path}")


for f in ["main.py", "ui/overview.py", "ui/login.py"]:
    fix_file(f)
print("Done.")

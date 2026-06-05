"""Fix the last 4 garbled lines in main.py."""
from pathlib import Path

ROOT = Path(__file__).parent.parent
path = ROOT / "main.py"
text = path.read_text(encoding="utf-8")
original = text


def fix_line(text, key_phrase, new_line):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if key_phrase in line:
            lines[i] = new_line
            print(f"  Fixed line {i+1}: {key_phrase}")
            return "\n".join(lines)
    print(f"  NOT FOUND: {key_phrase}")
    return text


text = fix_line(text,
    "quit_btn = QPushButton",
    '        self.quit_btn = QPushButton("QUIT")')

text = fix_line(text,
    "Switch back to Alpaca",
    '        back = QPushButton("Switch back to Alpaca")')

text = fix_line(text,
    "Sign out\", self",
    '        signout_act = QAction("Sign out", self)')

# Broker label line: return f"<garble>  {label}  <garble>"
lines = text.split("\n")
for i, line in enumerate(lines):
    if "return f" in line and "{label}" in line and any(ord(c) in (0x008F, 0x0081, 0x0090) for c in line):
        lines[i] = '        return f"{label}  v"'
        print(f"  Fixed line {i+1}: broker label")
        break
text = "\n".join(lines)

if text != original:
    path.write_text(text, encoding="utf-8")
    print("main.py saved.")
else:
    print("No changes.")

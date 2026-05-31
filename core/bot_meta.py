"""
APEX · Bot metadata + dependency parser  (v4.6.4)
─────────────────────────────────────────────────────────────────
Custom bots declare a metadata block as the first docstring of
their .py file. Both the desktop app (Make Bot tab, marketplace
card) and the Oracle server (preflight pip install before spawn)
parse the same block.

Expected format — a Python docstring that starts with the literal
line ``APEX-BOT-META`` followed by ``key: value`` lines:

    \"""
    APEX-BOT-META
    name:               CRYPTO momentum bot
    description:        AI-driven crypto fades on volume spikes
    method:             Linear regression on hourly bars + Groq calls
    ai_used:            groq
    compatible_models:  groq, anthropic, openai
    asset_type:         crypto
    universe:           crypto_universe.txt
    requirements:       scikit-learn, ccxt
    \"""

Schema
──────
name              Human-readable label.
description       One-line pitch shown on cards / tabs.
method            How the bot makes decisions, in plain English.
ai_used           Which AI provider was used to *build* this bot.
compatible_models Comma-separated list of providers that can RUN it.
asset_type        One of: stocks, crypto, etfs, futures, options.
universe          Filename of compatible universe (e.g. crypto_universe.txt).
requirements      Comma-separated pip-installable packages this bot needs
                  beyond APEX's bundled set. Server installs them on first
                  start; local frozen builds reject deps not already shipped.

The parser also discovers imports the AI may have forgotten to
declare — it scans top-level ``import X`` / ``from X import …``
lines and reports anything outside the standard library + APEX's
bundled set as a missing dependency. Server uses this as a
fall-back when META is absent or incomplete.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Optional


# ── Packages APEX ships with — bots can import these freely ─────
BUNDLED_PACKAGES = {
    "alpaca", "alpaca_py", "alpaca-py",
    "anthropic",
    "pandas", "numpy",
    "yfinance",
    "requests",
    "dotenv", "python-dotenv",
    "PIL", "Pillow",
    "matplotlib",
    "openai",
    "google", "google.generativeai",
}

# Mapping from importable module name → pip-installable name.
# Most are identical (e.g. `import requests` → pip install requests),
# but a few differ and need explicit mapping. Extend this list as
# new bots reveal new deltas.
IMPORT_TO_PIP = {
    "sklearn":   "scikit-learn",
    "PIL":       "Pillow",
    "dotenv":    "python-dotenv",
    "ccxt":      "ccxt",
    "ta":        "ta",
    "talib":     "TA-Lib",
    "alpaca":    "alpaca-py",
    "yaml":      "PyYAML",
    "cv2":       "opencv-python-headless",
    "google":    "google-generativeai",
}

# Modules in the Python stdlib that should NEVER trigger a pip install
STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(
    sys, "stdlib_module_names") else {
    # Fallback subset for Pythons that don't expose stdlib_module_names
    "os", "sys", "json", "time", "datetime", "pathlib", "re", "math",
    "collections", "itertools", "functools", "threading", "subprocess",
    "logging", "typing", "random", "hashlib", "secrets", "string",
    "io", "csv", "sqlite3", "urllib", "http", "argparse", "copy",
    "decimal", "fractions", "statistics", "warnings", "traceback",
    "tempfile", "shutil", "glob", "pickle", "queue", "socket", "ssl",
    "asyncio", "concurrent", "contextlib", "dataclasses", "enum",
    "inspect", "operator", "weakref", "uuid", "base64", "binascii",
    "codecs", "encodings", "platform",
}


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]


def parse_meta(source: str) -> dict:
    """Extract the APEX-BOT-META block. Returns a dict (empty if
    no block is found). Never raises — bots without a META block
    are handled gracefully by the import scanner."""
    out: dict = {}
    if not source:
        return out
    # Find any docstring that contains the marker. We scan all
    # triple-quoted strings in the first 4 KB of the file (META
    # should be the very first docstring, but tolerate leading
    # imports).
    head = source[:4096]
    m = re.search(
        r'(?:"""|\'\'\')\s*APEX-BOT-META\s*(.*?)(?:"""|\'\'\')',
        head, re.DOTALL,
    )
    if not m:
        return out
    body = m.group(1)
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower().replace("-", "_")
        v = v.strip()
        if k in ("requirements", "compatible_models",
                 "compatible", "tags", "brokers"):
            out[k] = _split_csv(v)
        else:
            out[k] = v
    # Normalize a few aliases
    if "compatible" in out and "compatible_models" not in out:
        out["compatible_models"] = out.pop("compatible")
    return out


def scan_imports(source: str) -> list[str]:
    """Walk the AST and return every TOP-LEVEL import name. Used as
    a fall-back when META.requirements is missing or incomplete."""
    try:
        tree = ast.parse(source)
    except Exception:
        return []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                names.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return sorted(names)


def required_pip_packages(source: str) -> list[str]:
    """Return the union of:
      - META.requirements (explicit declarations)
      - Imports the AST scanner found that are NOT stdlib and NOT
        already bundled in APEX
    Each name is normalized to its pip-installable form via
    IMPORT_TO_PIP, then deduplicated."""
    meta = parse_meta(source)
    explicit = list(meta.get("requirements") or [])
    out: dict[str, None] = {p.strip(): None for p in explicit if p.strip()}

    # Scan imports for anything missing
    bundled_lower = {p.lower() for p in BUNDLED_PACKAGES}
    for mod in scan_imports(source):
        m_lower = mod.lower()
        if mod in STDLIB_MODULES or m_lower in STDLIB_MODULES:
            continue
        if m_lower in bundled_lower or mod in BUNDLED_PACKAGES:
            continue
        pip_name = IMPORT_TO_PIP.get(mod, mod)
        out.setdefault(pip_name, None)
    return list(out.keys())


def short_summary(source: str) -> str:
    """One-line headline for log output / UI cards."""
    meta = parse_meta(source)
    name = meta.get("name") or meta.get("description") or "(unnamed bot)"
    method = meta.get("method", "")
    if method:
        return f"{name} — {method}"
    return name


# ── Server-side helper: actually run pip install ────────────────

def install_missing(bot_path: Path,
                    venv_python: Path,
                    marker_dir: Optional[Path] = None,
                    log_path: Optional[Path] = None) -> tuple[bool, str]:
    """Preflight: pip-install whatever the bot needs into the shared
    venv. Caches a marker file so a re-run skips the network call.
    Safe to call before every bot start.

    Args:
      bot_path:    /opt/apex_users/user_X/private_bots/foo.py
      venv_python: /opt/apex_venv/bin/python
      marker_dir:  where to write .deps_installed_<bot> (default: bot_path.parent)
      log_path:    optional file to append pip output to

    Returns (ok, message). ok=True means deps are now in place (either
    they were already there or pip succeeded). ok=False means pip
    failed — caller decides whether to spawn anyway (it'll just
    crash with ModuleNotFoundError, but at least we logged why).
    """
    import subprocess
    try:
        source = bot_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return (False, f"could not read {bot_path}: {e}")
    needed = required_pip_packages(source)
    if not needed:
        return (True, "no extra deps required")
    marker_dir = marker_dir or bot_path.parent
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f".deps_installed_{bot_path.stem}"
    if marker.exists():
        try:
            prev = marker.read_text(encoding="utf-8").strip()
            if prev == ",".join(sorted(needed)):
                return (True, f"deps already installed: {prev}")
        except Exception:
            pass
    # Run pip install — limit to 90s so a stuck mirror doesn't hang
    # the whole bot start indefinitely.
    cmd = [str(venv_python), "-m", "pip", "install", "--quiet",
           "--disable-pip-version-check", *needed]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return (False, "pip install timed out after 90s")
    except Exception as e:
        return (False, f"pip spawn failed: {e}")
    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== pip install {' '.join(needed)} ===\n")
                f.write(p.stdout)
                f.write(p.stderr)
        except Exception:
            pass
    if p.returncode != 0:
        return (False, f"pip exit {p.returncode}: {p.stderr.strip()[:300]}")
    try:
        marker.write_text(",".join(sorted(needed)), encoding="utf-8")
    except Exception:
        pass
    return (True, f"installed: {', '.join(needed)}")

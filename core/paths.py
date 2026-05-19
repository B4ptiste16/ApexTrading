"""
Central path resolution for APEX.

Source / dev mode:
    The app runs from the project folder and keeps its data files there
    (state, universe lists, charts, .env) — unchanged legacy behaviour.

Frozen / installed mode (PyInstaller build):
    The executable lives in a read-only / per-user install dir, so all
    writable data must live in a stable per-user folder instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def _source_root() -> Path:
    # core/paths.py -> core -> project root
    return Path(__file__).resolve().parent.parent


if is_frozen():
    _base = (os.environ.get("LOCALAPPDATA")
             or os.environ.get("APPDATA")
             or str(Path.home()))
    DATA_DIR = Path(_base) / "APEX Trading Platform"
else:
    DATA_DIR = _source_root()

DATA_DIR = Path(DATA_DIR)


def ensure_data_dir() -> Path:
    """
    Make sure the writable data dir exists (frozen mode only) and, if the
    user has not created their .env yet, drop a short instructions file
    next to where it must go.
    """
    if is_frozen():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not (DATA_DIR / ".env").exists():
            try:
                (DATA_DIR / "READ_ME_FIRST.txt").write_text(
                    "APEX Trading Platform - data folder\r\n"
                    "===================================\r\n\r\n"
                    "Create a file named exactly  .env  in this folder and put\r\n"
                    "your API keys in it. Required keys:\r\n\r\n"
                    "  ANTHROPIC_API_KEY=...\r\n"
                    "  ALPACA_API_KEY_LONG=...\r\n"
                    "  ALPACA_SECRET_KEY_LONG=...\r\n"
                    "  ALPACA_API_KEY_SHORT=...\r\n"
                    "  ALPACA_SECRET_KEY_SHORT=...\r\n"
                    "  ALPACA_API_KEY_DAY=...\r\n"
                    "  ALPACA_SECRET_KEY_DAY=...\r\n\r\n"
                    "The app and bots read this file on startup.\r\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
    return DATA_DIR

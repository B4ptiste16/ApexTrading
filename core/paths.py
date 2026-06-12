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


# V7.1.11: APEX_DATA_DIR env var takes precedence over everything else.
# Used by the APEX cloud bot runner (server/bot_runner.py) to isolate
# each user's state files / trade logs / charts under
# /opt/apex_users/<user_id>/ when a bot is run on the Oracle server.
_env_dir = os.environ.get("APEX_DATA_DIR")
if _env_dir:
    DATA_DIR = Path(_env_dir)
elif is_frozen():
    _base = (os.environ.get("LOCALAPPDATA")
             or os.environ.get("APPDATA")
             or str(Path.home()))
    DATA_DIR = Path(_base) / "APEX Trading Platform"
else:
    DATA_DIR = _source_root()

DATA_DIR = Path(DATA_DIR)


# ── V4.6.101 — PER-ACCOUNT DATA ISOLATION ────────────────────────────────
# DATA_DIR stays the SHARED root: it holds only the login layer
# (apex_auth.json / apex_accounts.json / apex_server.json), the app binary,
# and app-level logs. Everything account-scoped (settings, .env keys, the bot
# registry, ledgers, per-bot state, universes, local bot scripts) lives under
# ACCOUNT_DIR = DATA_DIR/accounts/<user_id>, so two signed-in accounts on the
# same machine never share bots, keys or settings. The active account is
# resolved ONCE at process start from apex_auth.json; login / switch / sign-out
# call updater.restart_app() so the new process re-resolves the right account.
def _read_active_uid() -> str | None:
    """Active account id (from the login file at the SHARED root), used to scope
    the per-account data dir. None when nobody is signed in yet."""
    try:
        import json
        f = DATA_DIR / "apex_auth.json"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            uid = (d.get("user") or {}).get("id")
            if uid is not None and str(uid).strip():
                return str(uid)
    except Exception:
        pass
    return None


# On the SERVER, APEX_DATA_DIR is already the per-user dir (/opt/apex_users/<id>/),
# so ACCOUNT_DIR == DATA_DIR — never add a second accounts/<uid> layer there or
# bots would lose their ledgers. On the desktop, scope under accounts/<uid>.
if _env_dir:
    ACCOUNT_DIR = DATA_DIR
else:
    _active_uid = _read_active_uid()
    ACCOUNT_DIR = DATA_DIR / "accounts" / (_active_uid or "_pending")

ACCOUNT_DIR = Path(ACCOUNT_DIR)


def active_account_id() -> str | None:
    """Public accessor for the active account id (None pre-login)."""
    return None if _env_dir else _read_active_uid()


def ensure_account_dir() -> Path:
    """Create the active account's data dir (desktop). No-op fields are safe."""
    try:
        ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return ACCOUNT_DIR


def ensure_data_dir() -> Path:
    """
    Make sure the writable data dir exists (frozen mode only) and, if the
    user has not created their .env yet, drop a short instructions file
    next to where it must go.
    """
    if is_frozen():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ensure_account_dir()   # V4.6.101 — account-scoped data lives here now
        if not (ACCOUNT_DIR / ".env").exists():
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

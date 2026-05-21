"""
APEX Auth Server — Cloud bot runner  (V7.1.11)
─────────────────────────────────────────────────────────────────────
Spawns and tracks per-user bot processes on the Oracle server. Each
running bot is a Python subprocess invoked with PYTHONPATH set to the
shared /opt/apex_bots folder; the user's encrypted broker credentials
are decrypted in-memory and exported to the bot's environment.

State / logs live under /opt/apex_users/<user_id>/ so users don't
share files. The runner remembers PIDs in `_RUNNING` so subsequent
status calls don't rely on persistent storage — if uvicorn restarts,
in-flight bots will keep running but we lose the tracking. That's
acceptable for the MVP (a restart of the auth service is rare and
won't kill the bot anyway because subprocess.Popen detaches the
child once it's spawned — Linux orphans it cleanly to PID 1).

Environment variables set per bot:
    ALPACA_API_KEY           ─┐ values pulled from the user's
    ALPACA_SECRET_KEY        ─┤ encrypted credentials (server/
    ALPACA_API_KEY_LONG      ─┤  credentials.py). The bot reads
    ALPACA_SECRET_KEY_LONG   ─┤  whichever pair its side uses.
    ALPACA_API_KEY_SHORT     ─┤
    ALPACA_SECRET_KEY_SHORT  ─┤
    ALPACA_API_KEY_DAY       ─┤
    ALPACA_SECRET_KEY_DAY    ─┘
    ANTHROPIC_API_KEY          for AI-driven decisions
    APEX_DATA_DIR              the user's isolated state folder
    APEX_BOT_SIDE              "LONG" / "SHORT" / "DAY" / custom slug
    PYTHONIOENCODING           always "utf-8" (Linux is, but be explicit)
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import credentials as creds


# ── Configuration  (env-overridable at deploy time) ─────────────────

BOTS_DIR    = Path(os.environ.get("APEX_BOTS_DIR",   "/opt/apex_bots"))
USERS_DIR   = Path(os.environ.get("APEX_USERS_DIR",  "/opt/apex_users"))
VENV_PYTHON = Path(os.environ.get("APEX_VENV_PYTHON", "/opt/apex_venv/bin/python"))

MAX_LOG_TAIL = 4000   # chars returned by /bots/{side}/logs


# Map APEX_BOT_SIDE → Python module name in BOTS_DIR
_BUILTIN_MODULES = {
    "LONG":  "longbot_v2",
    "SHORT": "shortbot_v2",
    "DAY":   "daybot",
}


# ── Runtime tracking ────────────────────────────────────────────────

@dataclass
class _RunningBot:
    user_id:    int
    side:       str
    pid:        int
    started_at: float = field(default_factory=time.time)
    log_path:   Optional[Path] = None
    proc:       Optional[subprocess.Popen] = None


# (user_id, side) → _RunningBot
_RUNNING: dict[tuple[int, str], _RunningBot] = {}
_LOCK    = threading.Lock()


# ── Helpers ─────────────────────────────────────────────────────────

def _user_data_dir(user_id: int) -> Path:
    p = USERS_DIR / f"user_{user_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _log_path(user_id: int, side: str) -> Path:
    d = _user_data_dir(user_id) / "logs"
    d.mkdir(exist_ok=True)
    return d / f"{side.lower()}.log"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. POSIX-only."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _bot_module(side: str) -> Optional[str]:
    """Resolve a side identifier to the Python module name to invoke."""
    return _BUILTIN_MODULES.get(side.upper())   # custom bots — to be added


def _build_env(user_id: int, side: str) -> dict[str, str]:
    """Return os.environ-style dict for the spawned bot."""
    env = os.environ.copy()
    env["APEX_DATA_DIR"]    = str(_user_data_dir(user_id))
    env["APEX_BOT_SIDE"]    = side.upper()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"]       = str(BOTS_DIR)

    # Inject the user's broker credentials. Each individual bot reads
    # ALPACA_API_KEY / ALPACA_SECRET_KEY — for the per-side keys we
    # also set the side-suffixed pair so existing read_env_keys()
    # callers still work.
    blob = creds.load_credentials(user_id) or {}
    s = side.upper()
    k = blob.get(f"ALPACA_API_KEY_{s}")    or blob.get("ALPACA_API_KEY")
    sk= blob.get(f"ALPACA_SECRET_KEY_{s}") or blob.get("ALPACA_SECRET_KEY")
    if k:  env["ALPACA_API_KEY"]    = k
    if sk: env["ALPACA_SECRET_KEY"] = sk
    # Also pass the full set so an Overview-style aggregator works too
    for label in ("LONG", "SHORT", "DAY"):
        kk = blob.get(f"ALPACA_API_KEY_{label}")
        ss = blob.get(f"ALPACA_SECRET_KEY_{label}")
        if kk: env[f"ALPACA_API_KEY_{label}"]    = kk
        if ss: env[f"ALPACA_SECRET_KEY_{label}"] = ss
    if blob.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = blob["ANTHROPIC_API_KEY"]
    return env


# ── Public API ──────────────────────────────────────────────────────

def is_running(user_id: int, side: str) -> bool:
    key = (user_id, side.upper())
    bot = _RUNNING.get(key)
    if bot is None:
        return False
    if _pid_alive(bot.pid):
        return True
    # Reap the dead entry
    with _LOCK:
        _RUNNING.pop(key, None)
    return False


def start_bot(user_id: int, side: str) -> dict:
    """Spawn the bot. Returns a small status dict for the API caller."""
    key = (user_id, side.upper())
    if is_running(user_id, side):
        bot = _RUNNING[key]
        return {"ok": True, "already_running": True, "pid": bot.pid}

    module = _bot_module(side)
    if module is None:
        return {"ok": False, "detail": f"Unknown bot side: {side}"}

    # Validate that we have keys for this bot
    blob = creds.load_credentials(user_id) or {}
    s = side.upper()
    if not (blob.get(f"ALPACA_API_KEY_{s}") and
            blob.get(f"ALPACA_SECRET_KEY_{s}")):
        return {"ok": False,
                "detail": f"No Alpaca keys synced for {side}. "
                          "Upload them from the desktop app's "
                          "Tools → ACCOUNT LINKING."}

    bot_file = BOTS_DIR / f"{module}.py"
    if not bot_file.exists():
        return {"ok": False,
                "detail": f"Bot script not deployed yet: {bot_file}"}
    if not VENV_PYTHON.exists():
        return {"ok": False,
                "detail": f"Python venv not found at {VENV_PYTHON}."}

    log_path = _log_path(user_id, side)
    env      = _build_env(user_id, side)

    # Invoke as:   /opt/apex_venv/bin/python -c "import M; M.main()"
    # so the bot's existing main() entry point is reused unmodified.
    cmd = [
        str(VENV_PYTHON), "-u", "-c",
        f"import {module} as bot; bot.main()",
    ]

    log_fh = open(log_path, "a", buffering=1, encoding="utf-8")
    log_fh.write(f"\n=== bot start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"user={user_id} side={side} ===\n")
    log_fh.flush()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BOTS_DIR),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,    # detach so a uvicorn reload
                                        # doesn't kill the bot
        )
    except Exception as e:
        log_fh.close()
        return {"ok": False, "detail": f"spawn failed: {e}"}

    with _LOCK:
        _RUNNING[key] = _RunningBot(
            user_id=user_id, side=side.upper(),
            pid=proc.pid, log_path=log_path, proc=proc,
        )
    return {"ok": True, "pid": proc.pid,
            "log": str(log_path)}


def stop_bot(user_id: int, side: str) -> dict:
    key = (user_id, side.upper())
    bot = _RUNNING.get(key)
    if bot is None or not _pid_alive(bot.pid):
        with _LOCK:
            _RUNNING.pop(key, None)
        return {"ok": True, "not_running": True}
    try:
        os.killpg(os.getpgid(bot.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as e:
        return {"ok": False, "detail": f"kill failed: {e}"}
    # Give the bot up to 5s to exit cleanly, then SIGKILL
    for _ in range(50):
        if not _pid_alive(bot.pid):
            break
        time.sleep(0.1)
    if _pid_alive(bot.pid):
        try:
            os.killpg(os.getpgid(bot.pid), signal.SIGKILL)
        except Exception:
            pass
    with _LOCK:
        _RUNNING.pop(key, None)
    return {"ok": True}


def status(user_id: int, side: str) -> dict:
    key = (user_id, side.upper())
    bot = _RUNNING.get(key)
    if bot is None or not _pid_alive(bot.pid):
        return {"running": False}
    return {"running": True, "pid": bot.pid,
            "uptime_s": round(time.time() - bot.started_at, 1)}


def list_running(user_id: int) -> list[dict]:
    out = []
    for (uid, side), bot in list(_RUNNING.items()):
        if uid != user_id:
            continue
        if not _pid_alive(bot.pid):
            continue
        out.append({"side": side, "pid": bot.pid,
                    "uptime_s": round(time.time() - bot.started_at, 1)})
    return out


def tail_log(user_id: int, side: str, n_chars: int = MAX_LOG_TAIL) -> str:
    p = _log_path(user_id, side)
    if not p.exists():
        return ""
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > n_chars:
                f.seek(size - n_chars)
            return f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"[log read error: {e}]"


def shutdown_all() -> None:
    """Stop every tracked bot. Called from the FastAPI lifespan cleanup
    so a graceful uvicorn shutdown doesn't strand orphan processes."""
    for (uid, side) in list(_RUNNING.keys()):
        try:
            stop_bot(uid, side)
        except Exception:
            pass

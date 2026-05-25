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


def _pid_file(user_id: int, side: str) -> Path:
    """v1.2.1 — cross-worker dedup. uvicorn runs with --workers 2, so each
    worker has its own in-memory _RUNNING dict. Without a shared
    coordination point, two consecutive /bots/X/start calls hit different
    workers and spawn duplicate bot processes. We persist the spawned PID
    to a file under /opt/apex_users/user_<id>/pids/ that all workers read."""
    d = _user_data_dir(user_id) / "pids"
    d.mkdir(exist_ok=True)
    return d / f"{side.lower()}.pid"


def _read_pid_file(user_id: int, side: str) -> int:
    p = _pid_file(user_id, side)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def _write_pid_file(user_id: int, side: str, pid: int) -> None:
    try:
        _pid_file(user_id, side).write_text(str(pid), encoding="utf-8")
    except Exception as e:
        print(f"[bot_runner] pid-file write failed: {e}", flush=True)


def _clear_pid_file(user_id: int, side: str) -> None:
    p = _pid_file(user_id, side)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


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
    """Resolve a side identifier to the Python module name to invoke.
    Only built-ins. Custom bots are resolved via _custom_bot_path()
    and run from an absolute path rather than `import <module>`."""
    return _BUILTIN_MODULES.get(side.upper())


def _custom_bot_path(user_id: int, side: str) -> Optional[Path]:
    """V4.0.2 — return the .py file path for a user's privately-uploaded
    custom bot (created in MAKE BOT, locally tested, then uploaded to
    Oracle for cloud-run without going through the public marketplace).
    Files live at /opt/apex_users/user_<id>/private_bots/<slug>.py."""
    private_dir = _user_data_dir(user_id) / "private_bots"
    cand = private_dir / f"{side.lower()}.py"
    return cand if cand.exists() else None


def private_bots_dir(user_id: int) -> Path:
    """Public accessor — used by the upload endpoint."""
    p = _user_data_dir(user_id) / "private_bots"
    p.mkdir(parents=True, exist_ok=True)
    return p


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
    # Pass all AI provider API keys that exist in the blob
    for ai_key in ("ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY",
                   "XAI_API_KEY", "GROQ_API_KEY"):
        if blob.get(ai_key):
            env[ai_key] = blob[ai_key]

    # Per-bot AI config: AI_PROVIDER_<SIDE>, AI_MODEL_<SIDE>, AI_MODE_<SIDE>
    # take precedence over the global AI_PROVIDER / AI_MODEL / AI_MODE.
    prov  = blob.get(f"AI_PROVIDER_{s}") or blob.get("AI_PROVIDER", "anthropic")
    model = blob.get(f"AI_MODEL_{s}")    or blob.get("AI_MODEL", "")
    mode  = blob.get(f"AI_MODE_{s}")     or blob.get("AI_MODE", "vision")
    env["AI_PROVIDER"] = prov
    if model:
        env["AI_MODEL"] = model
    env["AI_MODE"] = mode
    return env


# ── Public API ──────────────────────────────────────────────────────

def is_running(user_id: int, side: str) -> bool:
    """v1.2.1 — first consult the on-disk PID file so any uvicorn worker
    sees bots spawned by any other worker. Falls back to the in-memory
    registry for completeness."""
    s = side.upper()
    # 1. PID file — cross-worker source of truth
    pid = _read_pid_file(user_id, s)
    if pid and _pid_alive(pid):
        return True
    if pid and not _pid_alive(pid):
        _clear_pid_file(user_id, s)
    # 2. Worker-local registry (still useful for proc / log handles)
    key = (user_id, s)
    bot = _RUNNING.get(key)
    if bot is None:
        return False
    if _pid_alive(bot.pid):
        # Restore the missing PID file (e.g. crashed before write)
        _write_pid_file(user_id, s, bot.pid)
        return True
    with _LOCK:
        _RUNNING.pop(key, None)
    return False


def start_bot(user_id: int, side: str) -> dict:
    """Spawn the bot. Returns a small status dict for the API caller."""
    s = side.upper()
    key = (user_id, s)
    if is_running(user_id, side):
        existing_pid = (_RUNNING[key].pid if key in _RUNNING
                        else _read_pid_file(user_id, s))
        return {"ok": True, "already_running": True, "pid": existing_pid}

    # V4.0.2 — try built-in first; fall back to a privately-uploaded
    # custom bot's .py file. Custom bots are run from an absolute path
    # rather than `import M as bot` because their module name may
    # collide with stdlib / 3rd-party packages.
    module = _bot_module(side)
    custom_path: Optional[Path] = None if module else _custom_bot_path(user_id, side)
    if module is None and custom_path is None:
        return {"ok": False,
                "detail": f"Unknown bot side: {side}. "
                          f"For custom bots, upload the .py first via "
                          f"POST /bots/private/upload."}

    # Validate that we have keys for this bot
    blob = creds.load_credentials(user_id) or {}
    s = side.upper()
    if not (blob.get(f"ALPACA_API_KEY_{s}") and
            blob.get(f"ALPACA_SECRET_KEY_{s}")):
        return {"ok": False,
                "detail": f"MUST ASSIGN API KEY IN TOOLS. No Alpaca "
                          f"keys synced for {side}. Open Tools → "
                          f"ACCOUNT LINKING and Sync your slot keys to "
                          f"the APEX server."}

    if not VENV_PYTHON.exists():
        return {"ok": False,
                "detail": f"Python venv not found at {VENV_PYTHON}."}

    log_path = _log_path(user_id, side)
    env      = _build_env(user_id, side)

    if module is not None:
        bot_file = BOTS_DIR / f"{module}.py"
        if not bot_file.exists():
            return {"ok": False,
                    "detail": f"Bot script not deployed yet: {bot_file}"}
        # Invoke as:   /opt/apex_venv/bin/python -c "import M; M.main()"
        cmd = [str(VENV_PYTHON), "-u", "-c",
               f"import {module} as bot; bot.main()"]
    else:
        # Custom bot — run the .py directly. We DON'T `import` it
        # because the slug could collide with a real package name and
        # private_bots/ is not on sys.path globally.
        cmd = [str(VENV_PYTHON), "-u", str(custom_path)]

    # V4.6.5 — truncate the log on each bot start. Previously we
    # appended, so every restart left every prior crash in the log
    # forever and the user saw the same old tracebacks scroll past on
    # every new tail. Truncating means the log shows ONLY the current
    # bot run; prior history lives in *.bak (one rotation kept).
    try:
        if log_path.exists() and log_path.stat().st_size > 0:
            bak = log_path.with_suffix(log_path.suffix + ".bak")
            try:
                if bak.exists():
                    bak.unlink()
                log_path.rename(bak)
            except Exception:
                pass
    except Exception:
        pass
    log_fh = open(log_path, "w", buffering=1, encoding="utf-8")
    log_fh.write(f"=== bot start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"user={user_id} side={side} ===\n")
    log_fh.flush()

    # V4.6.4 — preflight pip install for custom bots. Parses the
    # APEX-BOT-META block + scans imports, installs anything that's
    # not already in the venv, then proceeds with spawn. A failure
    # here doesn't abort the launch (the bot would just crash with
    # ModuleNotFoundError as before) — we log the reason and continue
    # so behavior never regresses vs. pre-v4.6.4.
    if custom_path is not None:
        try:
            # Import lazily so a broken bot_meta import doesn't kill
            # the entire server. core.bot_meta lives in the desktop
            # tree but is deployed alongside server code by the build.
            from . import bot_meta as _BM  # type: ignore
        except Exception:
            try:
                import core.bot_meta as _BM  # type: ignore
            except Exception as _e:
                _BM = None
                log_fh.write(f"[preflight] bot_meta import failed: {_e}\n")
        if _BM is not None:
            try:
                ok, msg = _BM.install_missing(
                    custom_path, VENV_PYTHON,
                    marker_dir=custom_path.parent,
                    log_path=log_path)
                log_fh.write(f"[preflight] {msg}\n")
                if not ok:
                    log_fh.write(
                        f"[preflight] WARNING: pip install failed — bot "
                        f"may crash on import. Spawning anyway so the "
                        f"user sees the underlying error.\n")
            except Exception as _e:
                log_fh.write(f"[preflight] unexpected error: {_e}\n")
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
    _write_pid_file(user_id, side, proc.pid)
    return {"ok": True, "pid": proc.pid,
            "log": str(log_path)}


def stop_bot(user_id: int, side: str) -> dict:
    s = side.upper()
    key = (user_id, s)
    # PID file is the cross-worker source of truth; fall back to the
    # worker-local registry only when the file is gone.
    pid = _read_pid_file(user_id, s)
    if not pid:
        bot = _RUNNING.get(key)
        pid = bot.pid if bot else 0
    if not pid or not _pid_alive(pid):
        with _LOCK:
            _RUNNING.pop(key, None)
        _clear_pid_file(user_id, s)
        return {"ok": True, "not_running": True}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception as e:
        return {"ok": False, "detail": f"kill failed: {e}"}
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
    with _LOCK:
        _RUNNING.pop(key, None)
    _clear_pid_file(user_id, s)
    return {"ok": True}


def status(user_id: int, side: str) -> dict:
    s = side.upper()
    pid = _read_pid_file(user_id, s)
    if pid and _pid_alive(pid):
        bot = _RUNNING.get((user_id, s))
        return {"running": True, "pid": pid,
                "uptime_s": (round(time.time() - bot.started_at, 1)
                             if bot else None)}
    if pid:
        _clear_pid_file(user_id, s)
    bot = _RUNNING.get((user_id, s))
    if bot and _pid_alive(bot.pid):
        _write_pid_file(user_id, s, bot.pid)
        return {"running": True, "pid": bot.pid,
                "uptime_s": round(time.time() - bot.started_at, 1)}
    return {"running": False}


def list_running(user_id: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    # Iterate PID files first (cross-worker truth)
    try:
        pid_dir = _user_data_dir(user_id) / "pids"
        if pid_dir.exists():
            for pf in pid_dir.glob("*.pid"):
                side = pf.stem.upper()
                pid  = _read_pid_file(user_id, side)
                if pid and _pid_alive(pid):
                    bot = _RUNNING.get((user_id, side))
                    out.append({
                        "side": side, "pid": pid,
                        "uptime_s": (round(time.time() - bot.started_at, 1)
                                     if bot else None),
                    })
                    seen.add(side)
                elif pid:
                    _clear_pid_file(user_id, side)
    except Exception as e:
        print(f"[bot_runner] list_running pid scan: {e}", flush=True)
    # Fall through to in-memory entries this worker spawned
    for (uid, side), bot in list(_RUNNING.items()):
        if uid != user_id or side in seen:
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

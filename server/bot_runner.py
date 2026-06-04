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
import fcntl
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
    broker:     str = "alpaca"
    started_at: float = field(default_factory=time.time)
    log_path:   Optional[Path] = None
    proc:       Optional[subprocess.Popen] = None


# (user_id, side, broker) → _RunningBot
# V4.6.41 — the instance key includes the broker so the SAME bot can run on
# Alpaca AND IBKR at the same time (e.g. crypto on both). Alpaca keeps the
# legacy on-disk paths (no migration); other brokers get a nested namespace.
_RUNNING: dict[tuple[int, str, str], _RunningBot] = {}
_LOCK    = threading.Lock()


# ── Helpers ─────────────────────────────────────────────────────────

def _user_data_dir(user_id: int) -> Path:
    p = USERS_DIR / f"user_{user_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _instance_root(user_id: int, broker: str = "alpaca") -> Path:
    """V4.6.41 — per-broker root for a bot's logs / pids / state.

    Alpaca keeps the LEGACY layout (the user_<id> dir itself) so existing
    cloud bots and their state are untouched — zero migration. Any other
    broker (ibkr) gets its own nested sub-dir so the SAME side can run on
    both brokers concurrently without their pid/log/state files colliding."""
    base = _user_data_dir(user_id)
    if broker and broker.lower() != "alpaca":
        d = base / broker.lower()
        d.mkdir(parents=True, exist_ok=True)
        return d
    return base


def _log_path(user_id: int, side: str, broker: str = "alpaca") -> Path:
    d = _instance_root(user_id, broker) / "logs"
    d.mkdir(exist_ok=True)
    return d / f"{side.lower()}.log"


def _pid_file(user_id: int, side: str, broker: str = "alpaca") -> Path:
    """v1.2.1 — cross-worker dedup. uvicorn runs with --workers 2, so each
    worker has its own in-memory _RUNNING dict. Without a shared
    coordination point, two consecutive /bots/X/start calls hit different
    workers and spawn duplicate bot processes. We persist the spawned PID
    to a file under /opt/apex_users/user_<id>/[broker/]pids/ that all
    workers read."""
    d = _instance_root(user_id, broker) / "pids"
    d.mkdir(exist_ok=True)
    return d / f"{side.lower()}.pid"


def _read_pid_file(user_id: int, side: str, broker: str = "alpaca") -> int:
    p = _pid_file(user_id, side, broker)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def _write_pid_file(user_id: int, side: str, pid: int,
                    broker: str = "alpaca") -> None:
    try:
        _pid_file(user_id, side, broker).write_text(str(pid), encoding="utf-8")
    except Exception as e:
        print(f"[bot_runner] pid-file write failed: {e}", flush=True)


def _clear_pid_file(user_id: int, side: str, broker: str = "alpaca") -> None:
    p = _pid_file(user_id, side, broker)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


# ── Desired-bots registry (V4.6.63) ─────────────────────────────────────
# A persisted list of the (side, broker) a user wants running 24/7 on the
# cloud. start_bot() records into it; stop_bot() removes from it. The
# auto-scheduler watchdog ensures every desired bot is running every cycle —
# so bots survive crashes, the desktop being closed, AND server restarts,
# instead of only being started once at the market-open edge.

def _desired_path(user_id: int) -> Path:
    return _user_data_dir(user_id) / "desired_bots.json"


def list_desired(user_id: int) -> list[dict]:
    import json as _j
    p = _desired_path(user_id)
    try:
        return _j.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception:
        return []


def _mutate_desired(user_id: int, fn) -> None:
    """V4.6.65 — atomic read-modify-write of the desired registry under a file
    lock. uvicorn runs --workers 2 and each worker's watchdog touches this file;
    without a lock, concurrent stale-read writes clobbered entries (bots silently
    dropped from the always-on set). `fn(items) -> new_items`."""
    import json as _j
    p = _desired_path(user_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    lock = open(p.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            items = _j.loads(p.read_text(encoding="utf-8")) if p.exists() else []
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        new = fn(list(items))
        try:
            p.write_text(_j.dumps(new, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[desired] save failed: {e}", flush=True)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception:
            pass
        lock.close()


def add_desired(user_id: int, side: str, broker: str) -> None:
    s = side.upper(); b = (broker or "alpaca").lower()
    def _f(items):
        if not any(i.get("side") == s and i.get("broker") == b for i in items):
            items.append({"side": s, "broker": b})
        return items
    _mutate_desired(user_id, _f)


def remove_desired(user_id: int, side: str, broker: str) -> None:
    s = side.upper(); b = (broker or "alpaca").lower()
    def _f(items):
        return [i for i in items
                if not (i.get("side") == s and i.get("broker") == b)]
    _mutate_desired(user_id, _f)


def list_all_desired() -> list[tuple]:
    """(user_id, side, broker) for every desired bot across all users."""
    out: list[tuple] = []
    try:
        for d in USERS_DIR.glob("user_*"):
            try:
                uid = int(d.name.split("_")[1])
            except Exception:
                continue
            for it in list_desired(uid):
                if it.get("side"):
                    out.append((uid, it["side"], it.get("broker", "alpaca")))
    except Exception:
        pass
    return out


def _ibkr_ledger_dir(user_id: int) -> Path:
    """Where this user's IBKR sub-portfolio ledgers live on the server.
    Mirrors core.ledger.ledger_path(data_dir=<ibkr instance root>, broker=ibkr)."""
    return _instance_root(user_id, "ibkr") / "ibkr" / "ledgers"


def list_ibkr_ledgers(user_id: int) -> list[dict]:
    """V4.6.51 — return each IBKR bot's sub-portfolio snapshot (bot_id, cash,
    holdings, last_value) so the desktop can show a LIVE allocation %."""
    import json as _json
    out: list[dict] = []
    d = _ibkr_ledger_dir(user_id)
    if not d.exists():
        return out
    for f in d.glob("*.json"):
        if f.name.endswith(".rebalance.json"):
            continue
        try:
            j = _json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "bot_id":     j.get("bot_id", f.stem),
                "cash":       float(j.get("cash", 0.0)),
                "allocated":  float(j.get("allocated_cash", 0.0)),
                "value":      float(j.get("last_value", 0.0)),
                "holdings":   j.get("holdings", {}),
                "marks":      j.get("marks", {}),     # V4.6.61 — per-holding exact marks
                "file":       f.stem,
            })
        except Exception:
            continue
    return out


def list_ibkr_fills(user_id: int, side: str, mode: str = "paper",
                    limit: int = 500) -> list[dict]:
    """V4.6.63 — return a cloud IBKR bot's recorded fills (newest first) so the
    desktop can show Trade History / Recent Closed Trades / Trade Summary."""
    import json as _json
    f = _ibkr_ledger_dir(user_id) / f"{side.lower()}_{mode.lower()}.fills.jsonl"
    out: list[dict] = []
    if not f.exists():
        return out
    try:
        for line in f.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    out.reverse()
    return out


def request_ibkr_rebalance(user_id: int, side: str, target_pct: float,
                           mode: str = "paper") -> dict:
    """V4.6.51 — drop a rebalance request next to the bot's ledger. The
    running bot reads it next cycle and sells down to `target_pct` of the
    account, handing the freed cash back to the main account."""
    import json as _json
    d = _ibkr_ledger_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    req = d / f"{side.lower()}_{mode.lower()}.rebalance.json"
    try:
        req.write_text(_json.dumps({"target_pct": float(target_pct)}),
                       encoding="utf-8")
        return {"ok": True, "request": str(req)}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _resolve_broker(user_id: int, side: str,
                    broker: Optional[str] = None) -> str:
    """V4.6.41 — the broker an instance belongs to. An explicit value (from
    the desktop's ?broker= query param) wins; otherwise fall back to the
    per-user/per-side default in the credential blob so old clients that
    don't pass a broker keep their existing single-broker behavior."""
    if broker:
        return broker.lower()
    blob = creds.load_credentials(user_id) or {}
    return _cloud_broker(blob, side)


def _pid_alive(pid: int) -> bool:
    """True if a LIVE process with this PID exists. POSIX-only.

    V4.6.62 — treat ZOMBIE (defunct) processes as dead. A bot killed with -9
    whose parent (uvicorn worker) hasn't reaped it stays as a zombie; os.kill(0)
    succeeds for zombies, which made the runner think a crashed bot was still
    'already_running' and refuse to restart it. Reading /proc state fixes that."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    # Alive per kill(0) — but a zombie also passes that. Confirm it's not defunct.
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            state = f.read().split(") ", 1)[1].split(" ", 1)[0]
        if state == "Z":
            return False
    except Exception:
        pass
    return True


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


def _cloud_broker(blob: dict, side: str) -> str:
    """Which broker this user's cloud bot should trade on. Per-side override
    APEX_BROKER_<SIDE> wins over the per-user default APEX_CLOUD_BROKER;
    defaults to 'alpaca' so nothing regresses for existing users."""
    return str(blob.get(f"APEX_BROKER_{side.upper()}")
               or blob.get("APEX_CLOUD_BROKER") or "alpaca").lower()


def _ibkr_client_id(blob: dict, side: str) -> int:
    """Stable, collision-resistant clientId for this side on the user's
    shared gateway. Honors a desktop-synced IBKR_CLIENT_ID_<SIDE> when set,
    else derives a deterministic id from the side name (stable across
    processes, unlike hash())."""
    import zlib
    explicit = blob.get(f"IBKR_CLIENT_ID_{side.upper()}")
    if explicit:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    return 1 + (zlib.crc32(side.upper().encode()) % 990)


def _build_env(user_id: int, side: str,
               broker: str = "alpaca") -> dict[str, str]:
    """Return os.environ-style dict for the spawned bot."""
    env = os.environ.copy()
    # V4.6.41 — broker-scoped state dir so Alpaca and IBKR instances of the
    # same side keep separate positions / ledger / caches. Alpaca keeps the
    # legacy user_<id> dir (no migration for existing cloud bots).
    env["APEX_DATA_DIR"]    = str(_instance_root(user_id, broker))
    env["APEX_BOT_SIDE"]    = side.upper()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"]       = str(BOTS_DIR)
    # V4.6.8 — propagate the desktop's Alpaca paper/live toggle. The
    # value is stored alongside other creds in the encrypted blob the
    # client uploaded (key 'APEX_ALPACA_MODE'); fall back to 'paper'.
    # bot_framework + built-ins read this and construct TradingClient
    # with paper=False when mode='live'.
    try:
        _blob = creds.load_credentials(user_id) or {}
        env["APEX_ALPACA_MODE"] = str(
            _blob.get("APEX_ALPACA_MODE", "paper")).lower()
    except Exception:
        env["APEX_ALPACA_MODE"] = "paper"

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

    # ── Cloud-IBKR (v4.6.40) ────────────────────────────────────────
    # When this user's cloud bot is flagged to trade on IBKR, point the
    # bot at the per-user IB Gateway that start_bot() launched on this
    # same host (127.0.0.1:user_port). The broker abstraction in
    # core.broker_client reads APEX_BROKER=='ibkr' and builds the
    # ledger-backed IBKR shim instead of Alpaca. Each side connects with
    # its own clientId so they share one gateway without colliding.
    if (broker or "alpaca").lower() == "ibkr":
        from . import ibkr_gateway
        _ibkr_mode = str(blob.get("IBKR_TRADING_MODE", "paper")).lower()
        env["APEX_BROKER"]          = "ibkr"
        env["APEX_ALPACA_MODE"]     = str(
            blob.get("IBKR_TRADING_MODE", "paper")).lower()
        env["APEX_IBKR_HOST"]       = "127.0.0.1"
        # Use the port the gateway ACTUALLY opened (it may keep its built-in
        # default if it ignored OverrideTwsApiPort), not just the target.
        env["APEX_IBKR_PORT"]       = str(ibkr_gateway.active_port(user_id, _ibkr_mode))
        env["APEX_IBKR_CLIENT_ID"]  = str(_ibkr_client_id(blob, side))
        # V4.6.58 — request LIVE real-time data on the cloud too. This requires
        # the user's IBKR market-data subscription to be shared with the paper
        # account (Client Portal → Settings → Paper Trading → "Share market
        # data subscriptions with paper account"). If it isn't, pricing falls
        # back to delayed quotes and then yfinance, so orders still size.
        # Override with APEX_IBKR_DATA_TYPE_<SIDE> in the synced blob if needed.
        env.setdefault("APEX_IBKR_DATA_TYPE",
                       str(blob.get(f"APEX_IBKR_DATA_TYPE_{s}",
                                    blob.get("APEX_IBKR_DATA_TYPE", "1"))))
        # V4.6.50 — this bot's allocation % (synced from Tools → IBKR) so the
        # shim seeds its sub-portfolio slice instead of trading the whole acct.
        _alloc = blob.get(f"APEX_IBKR_ALLOC_{s}")
        if _alloc not in (None, ""):
            env["APEX_IBKR_ALLOC"] = str(_alloc)

    # Per-bot AI config: AI_PROVIDER_<SIDE>, AI_MODEL_<SIDE>, AI_MODE_<SIDE>
    # take precedence over the global AI_PROVIDER / AI_MODEL / AI_MODE.
    prov  = blob.get(f"AI_PROVIDER_{s}") or blob.get("AI_PROVIDER", "anthropic")
    model = blob.get(f"AI_MODEL_{s}")    or blob.get("AI_MODEL", "")
    mode  = blob.get(f"AI_MODE_{s}")     or blob.get("AI_MODE", "vision")
    env["AI_PROVIDER"] = prov
    if model:
        env["AI_MODEL"] = model
    env["AI_MODE"] = mode

    # V4.6.48 — per-bot minimum confidence (synced from the desktop slider) so
    # the framework's confidence gate honors the user's setting on the cloud.
    mc = blob.get(f"APEX_MIN_CONFIDENCE_{s}") or blob.get("APEX_MIN_CONFIDENCE")
    if mc not in (None, ""):
        env[f"APEX_MIN_CONFIDENCE_{s}"] = str(mc)
    return env


# ── Public API ──────────────────────────────────────────────────────

def is_running(user_id: int, side: str, broker: str = "alpaca") -> bool:
    """v1.2.1 — first consult the on-disk PID file so any uvicorn worker
    sees bots spawned by any other worker. Falls back to the in-memory
    registry for completeness.  V4.6.41 — scoped per broker."""
    s = side.upper()
    b = (broker or "alpaca").lower()
    # 1. PID file — cross-worker source of truth
    pid = _read_pid_file(user_id, s, b)
    if pid and _pid_alive(pid):
        return True
    if pid and not _pid_alive(pid):
        _clear_pid_file(user_id, s, b)
    # 2. Worker-local registry (still useful for proc / log handles)
    key = (user_id, s, b)
    bot = _RUNNING.get(key)
    if bot is None:
        return False
    if _pid_alive(bot.pid):
        # Restore the missing PID file (e.g. crashed before write)
        _write_pid_file(user_id, s, bot.pid, b)
        return True
    with _LOCK:
        _RUNNING.pop(key, None)
    return False


def start_bot(user_id: int, side: str, broker: Optional[str] = None) -> dict:
    """V4.6.62 — cross-worker SPAWN LOCK. uvicorn runs --workers 2, and the
    desktop can fire a start at the same moment as the auto-scheduler or a
    manual call. The pid-file dedup has a check-then-spawn race window, so two
    workers both passed is_running()==False and double-spawned the bot — two
    processes then fought over the SAME IBKR clientId (Error 326) and crash-
    looped. We serialise the whole check+spawn under an exclusive file lock so
    only one spawn can win per (user, side, broker)."""
    s = side.upper()
    b = _resolve_broker(user_id, s, broker)
    lock_path = _pid_file(user_id, s, b).with_suffix(".startlock")
    lf = open(lock_path, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        res = _start_bot_impl(user_id, side, b)
        # V4.6.63 — record into the desired-bots registry so the watchdog keeps
        # it running 24/7 (survives crash / desktop-close / server restart).
        if isinstance(res, dict) and res.get("ok"):
            try:
                add_desired(user_id, s, b)
            except Exception:
                pass
        return res
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        lf.close()


def _start_bot_impl(user_id: int, side: str, broker: Optional[str] = None) -> dict:
    """Spawn the bot. Returns a small status dict for the API caller.

    V4.6.41 — `broker` (from the desktop's ?broker= param) selects which
    broker this instance trades on, so the SAME side can run on Alpaca and
    IBKR concurrently. When omitted, falls back to the per-user default."""
    s = side.upper()
    b = _resolve_broker(user_id, s, broker)
    key = (user_id, s, b)
    if is_running(user_id, side, b):
        existing_pid = (_RUNNING[key].pid if key in _RUNNING
                        else _read_pid_file(user_id, s, b))
        return {"ok": True, "already_running": True, "pid": existing_pid,
                "broker": b}

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

    # Validate that we have credentials for this bot — broker-aware. The
    # explicit broker (not the per-user default) drives which creds we need.
    blob = creds.load_credentials(user_id) or {}
    s = side.upper()
    cloud_broker = b
    if cloud_broker == "ibkr":
        # V4.6.40 — IBKR cloud: need a synced paper login and a live
        # server-side IB Gateway BEFORE we spawn the bot, otherwise it
        # would just fail to connect on every tick.
        if not (blob.get("IBKR_USERNAME") and blob.get("IBKR_PASSWORD")):
            return {"ok": False,
                    "detail": "No IBKR paper login synced. Open Tools → "
                              "IBKR, enter your paper username/password and "
                              "enable 'Run IBKR bots on Oracle', then Sync."}
        try:
            from . import ibkr_gateway as _gw
            _gw.ensure_gateway(
                user_id, blob["IBKR_USERNAME"], blob["IBKR_PASSWORD"],
                str(blob.get("IBKR_TRADING_MODE", "paper")).lower())
        except Exception as e:
            return {"ok": False, "detail": f"IBKR gateway not ready: {e}"}
    else:
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

    log_path = _log_path(user_id, side, b)
    env      = _build_env(user_id, side, b)

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
            user_id=user_id, side=side.upper(), pid=proc.pid,
            broker=b, log_path=log_path, proc=proc,
        )
    _write_pid_file(user_id, side, proc.pid, b)
    return {"ok": True, "pid": proc.pid, "broker": b,
            "log": str(log_path)}


def stop_bot(user_id: int, side: str, broker: Optional[str] = None,
             user_initiated: bool = False) -> dict:
    s = side.upper()
    b = _resolve_broker(user_id, s, broker)
    key = (user_id, s, b)
    # V4.6.65 — only an EXPLICIT user stop removes it from the always-on
    # registry. Automated stops (graceful server shutdown via shutdown_all,
    # the market-close reconcile) must NOT wipe the user's 24/7 intent — that
    # was dropping cloud bots from the keep-alive set on every restart / close.
    if user_initiated:
        try:
            remove_desired(user_id, s, b)
        except Exception:
            pass
    # PID file is the cross-worker source of truth; fall back to the
    # worker-local registry only when the file is gone.
    pid = _read_pid_file(user_id, s, b)
    if not pid:
        bot = _RUNNING.get(key)
        pid = bot.pid if bot else 0
    if not pid or not _pid_alive(pid):
        with _LOCK:
            _RUNNING.pop(key, None)
        _clear_pid_file(user_id, s, b)
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
    _clear_pid_file(user_id, s, b)
    return {"ok": True, "broker": b}


def status(user_id: int, side: str, broker: Optional[str] = None) -> dict:
    s = side.upper()
    b = _resolve_broker(user_id, s, broker)
    pid = _read_pid_file(user_id, s, b)
    if pid and _pid_alive(pid):
        bot = _RUNNING.get((user_id, s, b))
        return {"running": True, "pid": pid, "broker": b,
                "uptime_s": (round(time.time() - bot.started_at, 1)
                             if bot else None)}
    if pid:
        _clear_pid_file(user_id, s, b)
    bot = _RUNNING.get((user_id, s, b))
    if bot and _pid_alive(bot.pid):
        _write_pid_file(user_id, s, bot.pid, b)
        return {"running": True, "pid": bot.pid, "broker": b,
                "uptime_s": round(time.time() - bot.started_at, 1)}
    return {"running": False, "broker": b}


def list_running(user_id: int) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    base = _user_data_dir(user_id)
    # V4.6.41 — scan the legacy Alpaca pid dir (user_<id>/pids) AND each
    # per-broker nested dir (user_<id>/<broker>/pids), so a side running on
    # both brokers shows up twice (once per broker).
    pid_dirs = [("alpaca", base / "pids")]
    try:
        for child in base.iterdir():
            sub = child / "pids"
            if child.is_dir() and sub.exists():
                pid_dirs.append((child.name.lower(), sub))
    except Exception:
        pass
    try:
        for brk, pid_dir in pid_dirs:
            if not pid_dir.exists():
                continue
            for pf in pid_dir.glob("*.pid"):
                side = pf.stem.upper()
                pid  = _read_pid_file(user_id, side, brk)
                if pid and _pid_alive(pid):
                    bot = _RUNNING.get((user_id, side, brk))
                    out.append({
                        "side": side, "broker": brk, "pid": pid,
                        "uptime_s": (round(time.time() - bot.started_at, 1)
                                     if bot else None),
                    })
                    seen.add((side, brk))
                elif pid:
                    _clear_pid_file(user_id, side, brk)
    except Exception as e:
        print(f"[bot_runner] list_running pid scan: {e}", flush=True)
    # Fall through to in-memory entries this worker spawned
    for (uid, side, brk), bot in list(_RUNNING.items()):
        if uid != user_id or (side, brk) in seen:
            continue
        if not _pid_alive(bot.pid):
            continue
        out.append({"side": side, "broker": brk, "pid": bot.pid,
                    "uptime_s": round(time.time() - bot.started_at, 1)})
    return out


def tail_log(user_id: int, side: str, n_chars: int = MAX_LOG_TAIL,
             broker: Optional[str] = None) -> str:
    b = _resolve_broker(user_id, side, broker)
    p = _log_path(user_id, side, b)
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

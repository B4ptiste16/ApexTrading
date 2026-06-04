"""
APEX Auth Server — Cloud-side market-schedule loop  (V7.1.12)
─────────────────────────────────────────────────────────────────────
Periodic background task that, every minute, checks the US market
clock and starts / stops each user's bots according to their stored
per-bot schedule preference.

Persistence: the schedule lives next to the encrypted credentials,
under a top-level `_schedule` key, e.g.

    {
      "ALPACA_API_KEY_LONG":  "...",
      "ALPACA_SECRET_KEY_LONG": "...",
      ...
      "_schedule": ["LONG", "DAY"]    # bots set to auto-run
    }

This means the desktop app's existing "Sync keys to APEX server"
button already round-trips the schedule once we add a field for it.
A dedicated /schedule endpoint is also exposed in app.py.

Market clock: cached for 30 s via Alpaca's get_clock() using any one
user's stored keys (the response is identical regardless of which
account). If no user has linked keys, we fall back to a US/Eastern
wall-clock check — accurate enough for the open/close window though
it can't see holidays.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import database, credentials as creds, bot_runner


# ── Market clock cache ──────────────────────────────────────────────

_CLOCK_CACHE: dict = {"ts": 0.0, "is_open": None}
_CACHE_TTL = 30.0     # seconds


def _et_now() -> datetime:
    """Approx US/Eastern wall clock (DST handled crudely). Used as a
    last-resort fallback when no user has Alpaca keys."""
    u = datetime.now(timezone.utc)
    off = -4 if 3 <= u.month <= 11 else -5
    return u + timedelta(hours=off)


def _wall_clock_open() -> bool:
    et = _et_now()
    if et.weekday() >= 5:            # Sat / Sun
        return False
    m = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= m < 16 * 60   # 09:30..16:00 ET


def _alpaca_clock_open() -> Optional[bool]:
    """Pick any user's stored Alpaca keys, ask Alpaca for the official
    clock. Returns None if we couldn't get a real answer."""
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        return None
    # Find the first user who has at least one Alpaca pair linked
    users = database.list_user_ids() if hasattr(database, "list_user_ids") else []
    for uid in users:
        blob = creds.load_credentials(uid) or {}
        for label in ("LONG", "SHORT", "DAY"):
            k = blob.get(f"ALPACA_API_KEY_{label}")
            s = blob.get(f"ALPACA_SECRET_KEY_{label}")
            if k and s:
                try:
                    c = TradingClient(k, s, paper=True)
                    return bool(c.get_clock().is_open)
                except Exception:
                    continue
    return None


def market_is_open() -> bool:
    now = time.time()
    if now - _CLOCK_CACHE["ts"] < _CACHE_TTL and _CLOCK_CACHE["is_open"] is not None:
        return _CLOCK_CACHE["is_open"]
    api = _alpaca_clock_open()
    result = api if api is not None else _wall_clock_open()
    _CLOCK_CACHE.update({"ts": now, "is_open": result})
    return result


# ── Per-user schedule storage  (lives inside the encrypted creds) ───

def get_schedule(user_id: int) -> list[str]:
    blob = creds.load_credentials(user_id) or {}
    sched = blob.get("_schedule")
    if isinstance(sched, list):
        return [str(s).upper() for s in sched]
    return []


def set_schedule(user_id: int, sides: list[str]) -> None:
    blob = creds.load_credentials(user_id) or {}
    blob["_schedule"] = sorted({str(s).upper() for s in sides})
    creds.save_credentials(user_id, blob)


# ── Background loop ─────────────────────────────────────────────────

_TASK:    Optional[asyncio.Task] = None
_LAST_OPEN: Optional[bool]       = None


async def _loop() -> None:
    """Runs forever inside the FastAPI event loop until cancelled.
    Polls the market clock once a minute and reconciles each user's
    scheduled bots against the actual running state."""
    global _LAST_OPEN
    while True:
        try:
            await asyncio.sleep(60)
            is_open = market_is_open()
            for uid in (database.list_user_ids()
                        if hasattr(database, "list_user_ids") else []):
                try:
                    _reconcile_user(uid, is_open)
                except Exception as e:
                    print(f"[sched] user {uid}: {e}")
            _LAST_OPEN = is_open
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[sched] loop: {e}")
            await asyncio.sleep(30)


def _reconcile_user(user_id: int, is_open: bool) -> None:
    """Start scheduled bots that should be running but aren't; stop
    scheduled bots that should be stopped but are still alive."""
    sched = get_schedule(user_id)
    if not sched:
        return
    for side in sched:
        running = bot_runner.is_running(user_id, side)
        if is_open and not running:
            bot_runner.start_bot(user_id, side)
            print(f"[sched] started {side} for user {user_id}")
        # V4.6.65 — do NOT auto-stop at market close. Cloud bots run 24/7
        # (crypto trades around the clock; stock bots sleep themselves when the
        # market is shut). The market-close stop was fighting the keep-alive
        # watchdog and dropping bots from the always-on registry.


# ── Public lifecycle hooks (called from app.py lifespan) ────────────

def start_loop() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_loop())


def stop_loop() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
    _TASK = None

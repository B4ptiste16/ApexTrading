"""
APEX  —  market schedule + update-window helpers.

Used by main.py to:
  * auto-start bots at the US open and auto-stop them at the close
  * only let the INSTALLED app self-update in the overnight window
    (roughly 1h after close .. 1h before open), never during the day.

Broker-agnostic: market state comes from the active broker's clock
(Alpaca get_clock / IBKR shim), so this file is identical in both builds.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _et_now() -> datetime:
    """Approx US/Eastern wall clock (no zoneinfo dep). DST ~ Mar–Nov."""
    u = datetime.now(timezone.utc)
    off = -4 if 3 <= u.month <= 11 else -5
    return u + timedelta(hours=off)


def market_is_open() -> bool | None:
    """True/False from the broker clock, or None if it can't be read."""
    try:
        from core.data import get_client
        c = get_client("LONG")
        if not c:
            return None
        return bool(c.get_clock().is_open)
    except Exception:
        return None


def update_allowed_now(is_open: bool | None) -> bool:
    """
    Overnight maintenance window for the installed app's auto-update.

    Allowed only when the market is CLOSED and the Eastern wall clock is
    between 17:00 (close 16:00 + 1h) and 08:30 (open 09:30 - 1h). This
    covers weeknights and weekends, and never the trading day.
    """
    if is_open:
        return False
    et = _et_now()
    mins = et.hour * 60 + et.minute
    after_close = mins >= 17 * 60          # >= 17:00 ET
    before_open = mins < 8 * 60 + 30       # <  08:30 ET
    return after_close or before_open

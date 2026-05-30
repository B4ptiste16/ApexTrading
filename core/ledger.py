"""
APEX · Per-bot sub-portfolio ledger  (v4.6.38)
──────────────────────────────────────────────────────────────────────
IBKR exposes ONE shared brokerage account.  To let several bots trade the
same IBKR account without stepping on each other, each bot is given a
*sub-portfolio*: a starting cash allocation (and optionally shares it
inherits from a bot it replaced).  A bot may only ever spend its own cash
and sell its own shares — it can never see or touch another bot's slice.

This module is the source of truth for what a bot "owns".  The IBKR broker
shim (core.broker_client._IBKRShim) consults the ledger so a bot's
get_account / get_all_positions reflect ONLY its slice, and every fill is
written back so the slice stays accurate.

Alpaca is unaffected: there, each Alpaca account already IS the bot's whole
world, so no ledger is used.  get_ledger() returns None whenever the broker
isn't IBKR or no ledger file has been created — the shim then falls back to
the historic whole-account behavior, so nothing regresses.

Ledger file (one per broker + mode + bot side):
    <data_dir>/<broker>/ledgers/<side>_<mode>.json
{
  "bot_id":         "LONG",
  "allocated_cash": 1000.0,    # initial grant — never changes after create
  "cash":           742.13,    # free cash remaining in this slice
  "holdings":       {"AAPL": 5.0, "BTC": 0.0125},  # qty per symbol (neg = short)
  "created":        "2026-05-29T23:40:00+00:00",
  "updated":        "2026-05-29T23:55:00+00:00"
}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_EPS = 1e-9


def normalize_symbol(symbol: str) -> str:
    """Collapse the various symbol spellings to the plain IBKR ticker the
    ledger stores.  yfinance 'BTC-USD' and Alpaca 'BTC/USD' both become
    'BTC'; equities pass through unchanged ('AAPL' → 'AAPL')."""
    s = str(symbol or "").upper().strip()
    if "/" in s:
        s = s.split("/", 1)[0]
    elif s.endswith("-USD"):
        s = s[:-4]
    return s


def ledger_path(side: str,
                broker: str,
                mode: str,
                data_dir: str | Path) -> Path:
    """Resolve the ledger file path for one (side, broker, mode)."""
    base = Path(data_dir) / broker / "ledgers"
    return base / f"{side.lower()}_{mode.lower()}.json"


class Ledger:
    """A bot's sub-portfolio: free cash + per-symbol share holdings.

    All mutating methods persist immediately so a bot crash can't lose the
    slice's accounting.  Holdings of (near) zero are pruned so an empty
    slice reads cleanly.
    """

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.bot_id = data.get("bot_id", "")
        self.allocated_cash = float(data.get("allocated_cash", 0.0))
        self.cash = float(data.get("cash", 0.0))
        self.holdings: dict[str, float] = {
            normalize_symbol(k): float(v)
            for k, v in (data.get("holdings") or {}).items()
        }
        self.created = data.get("created", "")
        self.updated = data.get("updated", "")

    # ── persistence ─────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> Optional["Ledger"]:
        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return cls(p, data)

    @classmethod
    def create(cls, path: Path, *,
               bot_id: str,
               allocated_cash: float,
               holdings: Optional[dict] = None) -> "Ledger":
        """Create (and persist) a fresh ledger.  A replacement bot passes
        the outgoing bot's `holdings` so it inherits the slot's shares; a
        brand-new bot passes none and starts cash-only."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        led = cls(path, {
            "bot_id": bot_id,
            "allocated_cash": float(allocated_cash),
            "cash": float(allocated_cash),
            "holdings": holdings or {},
            "created": now,
            "updated": now,
        })
        led.save()
        return led

    def save(self) -> None:
        self.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._prune()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "bot_id": self.bot_id,
                "allocated_cash": self.allocated_cash,
                "cash": self.cash,
                "holdings": self.holdings,
                "created": self.created,
                "updated": self.updated,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  [ledger] save failed: {e}", flush=True)

    def delete(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as e:
            print(f"  [ledger] delete failed: {e}", flush=True)

    # ── queries ─────────────────────────────────────────────────────

    def holding(self, symbol: str) -> float:
        return self.holdings.get(normalize_symbol(symbol), 0.0)

    def symbols(self) -> list[str]:
        return [s for s, q in self.holdings.items() if abs(q) > _EPS]

    def can_buy(self, cost: float) -> bool:
        return cost <= self.cash + _EPS

    def affordable_qty(self, price: float) -> float:
        """Largest qty this slice can afford at `price` (0 if price<=0)."""
        if price <= 0:
            return 0.0
        return max(0.0, self.cash / price)

    # ── mutations (each persists) ───────────────────────────────────

    def record_buy(self, symbol: str, qty: float, price: float) -> None:
        sym = normalize_symbol(symbol)
        self.cash -= qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) + qty
        self.save()

    def record_sell(self, symbol: str, qty: float, price: float) -> None:
        sym = normalize_symbol(symbol)
        self.cash += qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) - qty
        self.save()

    def record_short(self, symbol: str, qty: float, price: float) -> None:
        """Sell-to-open: proceeds add to cash, holdings go negative."""
        sym = normalize_symbol(symbol)
        self.cash += qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) - qty
        self.save()

    def record_cover(self, symbol: str, qty: float, price: float) -> None:
        """Buy-to-cover: pays cash, brings a negative holding back up."""
        sym = normalize_symbol(symbol)
        self.cash -= qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) + qty
        self.save()

    def _prune(self) -> None:
        self.holdings = {
            s: q for s, q in self.holdings.items() if abs(q) > _EPS
        }


# ── env-driven convenience (used by the bot subprocess) ─────────────

def get_ledger(*,
               side: Optional[str] = None,
               broker: Optional[str] = None,
               mode: Optional[str] = None,
               data_dir: Optional[str] = None) -> Optional[Ledger]:
    """Return this bot's Ledger, or None when ledger isolation doesn't
    apply (non-IBKR broker, or no ledger file created yet).

    Defaults are read from the env vars APEX set on the bot subprocess:
    APEX_BOT_SIDE, APEX_BROKER, APEX_ALPACA_MODE, APEX_DATA_DIR.  Returning
    None makes the IBKR shim fall back to whole-account behavior, so a bot
    launched before a ledger exists still trades (against the full account)
    rather than refusing — the Tools lifecycle creates the ledger on add."""
    broker = (broker or os.environ.get("APEX_BROKER") or "alpaca").lower()
    if broker != "ibkr":
        return None
    side = side or os.environ.get("APEX_BOT_SIDE", "")
    if not side:
        return None
    mode = (mode or os.environ.get("APEX_ALPACA_MODE") or "paper").lower()
    data_dir = data_dir or os.environ.get("APEX_DATA_DIR") or "."
    return Ledger.load(ledger_path(side, broker, mode, data_dir))

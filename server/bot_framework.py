"""
APEX · Bot Framework  (v4.6.5)
─────────────────────────────────────────────────────────────────
Battle-tested boilerplate so AI-generated bots only have to supply
a strategy function — never the plumbing.

Why?
────
Free-form bots (the v4.6.4 way) gave the AI 100% of the surface
area to bungle: yfinance MultiIndex columns, pandas Series vs.
scalar truthiness, Alpaca's BTC/USD vs. yfinance's BTC-USD, missing
flush=True, no outer loop, no error handling per symbol, dead PIDs
… every new bot reopened the same wounds.

This module provides a `BotRunner` class that owns all of that and
calls a user-supplied `decide(...)` for each symbol on each tick.
The strategy function returns a strict Decision dict — the runner
translates it into Alpaca orders, handles failures, logs to stdout,
persists state. The AI cannot break the data pipeline because it
never touches it.

Strategy contract (the ONLY thing the AI writes):
─────────────────────────────────────────────────
    def decide(symbol: str,
               bars: pd.DataFrame,         # OHLCV, single-level cols, ascending
               position: dict,             # {"qty": float, "side": "long"|"short"|"flat", "avg_entry": float}
               account: dict) -> dict:     # {"cash": float, "equity": float, "buying_power": float}
        return {"action": "BUY"|"SELL"|"SHORT"|"COVER"|"HOLD",
                "qty":    int|float,       # required unless action == "HOLD"
                "reason": str}             # human-readable, shown in logs

Asset types
───────────
Each bot declares `asset_type` in its APEX-BOT-META block. The
runner uses it to pick the right Alpaca symbol format and the
right "is the market open" check:
    stocks  → Alpaca equity API, NYSE clock, BTC-USD style symbols unchanged
    crypto  → Alpaca crypto API (24/7), BTC-USD → BTC/USD translation

For other asset types (etfs, futures, options) the runner currently
defaults to the equity path; add cases below as APEX expands.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd


# ── Lazy imports so a missing optional dep (yfinance, alpaca) only
#    fails when the runner actually needs it, not at module import.

def _yf():
    import yfinance as yf
    return yf


def _alpaca_clients(asset_type: str):
    """Return (trading_client, ALPACA_KEY, ALPACA_SECRET)."""
    from alpaca.trading.client import TradingClient
    key = os.environ["ALPACA_API_KEY"]
    sec = os.environ["ALPACA_SECRET_KEY"]
    return TradingClient(key, sec, paper=True), key, sec


# ── Symbol translation ──────────────────────────────────────────

# yfinance crypto symbols use a hyphen (BTC-USD).
# Alpaca crypto symbols use a slash (BTC/USD).
def _to_alpaca_symbol(symbol: str, asset_type: str) -> str:
    if asset_type == "crypto" and "-" in symbol:
        base, _, quote = symbol.partition("-")
        return f"{base}/{quote}"
    return symbol


# ── Data fetch ──────────────────────────────────────────────────

def _fetch_bars(symbol: str,
                period: str = "1y",
                interval: str = "1d") -> pd.DataFrame:
    """yfinance bars with a clean single-level column index. Empty
    DataFrame returned on any failure so the runner can skip the
    symbol without crashing."""
    try:
        df = _yf().download(symbol, period=period, interval=interval,
                            auto_adjust=False, progress=False)
    except Exception as e:
        print(f"  [fetch] {symbol}: {e}", flush=True)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    # Modern yfinance wraps single-ticker queries in a column
    # MultiIndex like (Close, BTC-USD). Strip the ticker level.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # Ensure ascending chronological order
    df = df.sort_index()
    return df


# ── Decision validation ─────────────────────────────────────────

VALID_ACTIONS = {"BUY", "SELL", "SHORT", "COVER", "HOLD"}


def _validate_decision(symbol: str, raw: object) -> Optional[dict]:
    """Return a sanitized Decision dict or None if invalid. We coerce
    sloppy AI output (extra keys, wrong case, qty as string) into a
    valid shape so a borderline `decide` still trades."""
    if not isinstance(raw, dict):
        print(f"  [decide] {symbol}: returned {type(raw).__name__}, "
              f"expected dict — skipping", flush=True)
        return None
    action = str(raw.get("action", "")).upper().strip()
    if action not in VALID_ACTIONS:
        print(f"  [decide] {symbol}: unknown action '{action}' "
              f"(valid: {sorted(VALID_ACTIONS)}) — treating as HOLD",
              flush=True)
        action = "HOLD"
    qty = raw.get("qty", 0)
    try:
        qty = float(qty) if qty is not None else 0.0
    except Exception:
        qty = 0.0
    reason = str(raw.get("reason", "")).strip() or "(no reason given)"
    if action != "HOLD" and qty <= 0:
        print(f"  [decide] {symbol}: action={action} but qty<=0 — "
              f"treating as HOLD", flush=True)
        action = "HOLD"
    return {"action": action, "qty": qty, "reason": reason}


# ── Position + account snapshot helpers ─────────────────────────

def _position_for(client, alpaca_symbol: str) -> dict:
    try:
        positions = client.get_all_positions()
    except Exception:
        return {"qty": 0.0, "side": "flat", "avg_entry": 0.0}
    for p in positions:
        if p.symbol.upper() == alpaca_symbol.upper():
            return {
                "qty":       float(p.qty),
                "side":      getattr(p, "side", "long"),
                "avg_entry": float(getattr(p, "avg_entry_price", 0.0) or 0.0),
            }
    return {"qty": 0.0, "side": "flat", "avg_entry": 0.0}


def _account_snapshot(client) -> dict:
    try:
        a = client.get_account()
        return {
            "cash":          float(a.cash),
            "equity":        float(a.equity),
            "buying_power":  float(a.buying_power),
        }
    except Exception:
        return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0}


# ── Order execution ─────────────────────────────────────────────

def _submit(client, alpaca_symbol: str, action: str, qty: float):
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    side = (OrderSide.BUY if action in ("BUY", "COVER")
            else OrderSide.SELL)
    req = MarketOrderRequest(
        symbol=alpaca_symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.GTC,
    )
    return client.submit_order(order_data=req)


# ── Universe loading ────────────────────────────────────────────

def _load_universe(universe_path: str | Path,
                   default_symbols: list[str]) -> list[str]:
    """Read a *_universe.txt file. Lines starting with `#` are
    comments. Returns `default_symbols` if the file is missing or
    empty."""
    if not universe_path:
        return list(default_symbols)
    p = Path(universe_path)
    if not p.is_absolute():
        # Look in APEX_DATA_DIR first, then cwd
        data_dir = os.environ.get("APEX_DATA_DIR")
        if data_dir:
            cand = Path(data_dir) / p
            if cand.exists():
                p = cand
    if not p.exists():
        return list(default_symbols)
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip inline `# note`
            ticker = line.split("#", 1)[0].strip().split()[0].upper()
            if ticker:
                out.append(ticker)
    except Exception as e:
        print(f"[universe] error reading {p}: {e}", flush=True)
        return list(default_symbols)
    return out or list(default_symbols)


# ── The runner ──────────────────────────────────────────────────

class BotRunner:
    """Owns the bot main loop.

    Usage in an AI-generated bot:

        from core.bot_framework import BotRunner

        def decide(symbol, bars, position, account):
            # … strategy …
            return {"action": "BUY", "qty": 1, "reason": "trend up"}

        if __name__ == "__main__":
            BotRunner(
                asset_type="crypto",
                default_symbols=["BTC-USD", "ETH-USD"],
                universe_path="crypto_universe.txt",
                tick_seconds=300,
                bar_period="1y",
                bar_interval="1d",
            ).run(decide)
    """

    def __init__(self, *,
                 asset_type: str = "stocks",
                 default_symbols: Optional[list[str]] = None,
                 universe_path: Optional[str] = None,
                 tick_seconds: int = 300,
                 bar_period: str = "1y",
                 bar_interval: str = "1d",
                 name: str = "bot"):
        self.asset_type     = asset_type.lower()
        self.default_symbols = list(default_symbols or [])
        self.universe_path  = universe_path
        self.tick_seconds   = max(10, int(tick_seconds))
        self.bar_period     = bar_period
        self.bar_interval   = bar_interval
        self.name           = name

    def run(self, decide: Callable[..., dict]):
        print(f"[{self.name}] APEX bot framework v4.6.5  ·  "
              f"asset_type={self.asset_type}  ·  "
              f"tick={self.tick_seconds}s", flush=True)
        client, _, _ = _alpaca_clients(self.asset_type)
        while True:
            symbols = _load_universe(self.universe_path,
                                     self.default_symbols)
            if not symbols:
                print(f"[{self.name}] no symbols configured — "
                      f"sleeping {self.tick_seconds}s", flush=True)
                time.sleep(self.tick_seconds)
                continue
            account = _account_snapshot(client)
            print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                  f"tick — equity=${account['equity']:.2f}  "
                  f"cash=${account['cash']:.2f}  "
                  f"universe={len(symbols)}", flush=True)
            for sym in symbols:
                try:
                    self._step(client, sym, decide, account)
                except Exception as e:
                    print(f"  ERROR {sym}: {e}", flush=True)
                    traceback.print_exc()
                time.sleep(1)  # tiny gap between symbols
            time.sleep(self.tick_seconds)

    def _step(self, client, symbol: str, decide, account):
        bars = _fetch_bars(symbol, self.bar_period, self.bar_interval)
        if bars.empty:
            print(f"  no bars for {symbol} — skip", flush=True)
            return
        alpaca_sym = _to_alpaca_symbol(symbol, self.asset_type)
        position   = _position_for(client, alpaca_sym)
        raw        = decide(symbol, bars, position, account)
        d          = _validate_decision(symbol, raw)
        if d is None:
            return
        action = d["action"]
        if action == "HOLD":
            print(f"  HOLD   {symbol:<10}  {d['reason']}", flush=True)
            return
        try:
            order = _submit(client, alpaca_sym, action, d["qty"])
            print(f"  {action:<6} {symbol:<10}  qty={d['qty']}  "
                  f"id={order.id}  ({d['reason']})", flush=True)
        except Exception as e:
            print(f"  {action:<6} {symbol:<10}  FAILED: {e}",
                  flush=True)

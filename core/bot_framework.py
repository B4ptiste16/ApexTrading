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
    """Return (trading_client, ALPACA_KEY, ALPACA_SECRET).
    V4.6.8 — honors APEX_ALPACA_MODE env var. 'live' creates a
    real-money TradingClient (paper=False); anything else falls back
    to paper trading. The desktop's header toggle writes this var."""
    from alpaca.trading.client import TradingClient
    key = os.environ["ALPACA_API_KEY"]
    sec = os.environ["ALPACA_SECRET_KEY"]
    mode = (os.environ.get("APEX_ALPACA_MODE") or "paper").lower()
    is_paper = (mode != "live")
    print(f"[framework] Alpaca client mode: "
          f"{'PAPER' if is_paper else 'LIVE'}", flush=True)
    return TradingClient(key, sec, paper=is_paper), key, sec


# V4.6.26 — Initial portfolio cleanup. Runs ONCE at bot startup.
# Liquidates any position whose Alpaca asset_class doesn't match
# this bot's declared asset_type. Crypto bot finding stock positions
# closes them deterministically (no AI needed); a stocks bot finding
# crypto positions does the same. The existing v4.6.2 transition
# cleanup handles the subtler long-vs-short case within the same
# asset class via AI.

_BOT_ASSET_TYPE_TO_ALPACA_CLASS = {
    "crypto":  "crypto",
    "stocks":  "us_equity",
    "etfs":    "us_equity",   # Alpaca treats ETFs as us_equity
    "options": "us_option",
}


def liquidate_off_strategy_positions(client, asset_type: str) -> list:
    """Close any open position whose Alpaca asset_class doesn't match
    this bot's declared asset_type. Returns list of (symbol, action)
    tuples for logging. Safe to call multiple times — if no positions
    mismatch, no orders are sent."""
    asset_type = (asset_type or "").lower()
    expected = _BOT_ASSET_TYPE_TO_ALPACA_CLASS.get(asset_type)
    if expected is None:
        print(f"[startup-cleanup] unknown asset_type='{asset_type}', "
              f"skipping cleanup", flush=True)
        return []
    try:
        positions = client.get_all_positions()
    except Exception as e:
        print(f"[startup-cleanup] get_all_positions failed: {e}",
              flush=True)
        return []
    if not positions:
        print(f"[startup-cleanup] account is flat — nothing to clean",
              flush=True)
        return []
    actions = []
    kept = []
    for p in positions:
        pos_class = getattr(p, "asset_class", "")
        # alpaca-py may expose as enum; coerce to string
        pos_class = str(pos_class).lower().split(".")[-1]
        sym = getattr(p, "symbol", "?")
        if pos_class == expected:
            kept.append(sym)
            continue
        # Liquidate — symbol's asset_class doesn't fit our strategy
        print(f"[startup-cleanup] {sym} (asset_class={pos_class}) "
              f"doesn't match bot asset_type={asset_type} — "
              f"liquidating", flush=True)
        try:
            client.close_position(sym)
            actions.append((sym, "liquidated"))
        except Exception as e:
            print(f"[startup-cleanup]   close_position({sym}) "
                  f"failed: {e}", flush=True)
            actions.append((sym, f"failed: {e}"))
    if kept:
        print(f"[startup-cleanup] kept (matching asset_type={asset_type}): "
              f"{kept}", flush=True)
    if actions:
        print(f"[startup-cleanup] {len(actions)} position(s) acted on: "
              f"{actions}", flush=True)
    else:
        print(f"[startup-cleanup] all {len(positions)} position(s) "
              f"already match asset_type={asset_type} — nothing to do",
              flush=True)
    return actions


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


def _write_trade_log(slug: str, symbol: str, action: str,
                     qty, price, reason: str = "") -> None:
    """V4.6.30 — append a trade-log entry so the bot's tab Closed
    Trades feed + equity history populate. Written to
    APEX_DATA_DIR/<side>_trade_log.jsonl in the same shape the
    built-in bots use (so core.data.load_jsonl + the feed parse it).

    Uses APEX_BOT_SIDE (the registry slug APEX launched us with) for
    the filename — NOT the BotRunner display name — so it matches
    what core.data.log_files_for(side) reads."""
    import json as _j
    from datetime import datetime as _dt, timezone as _tz
    data_dir = os.environ.get("APEX_DATA_DIR") or "."
    side = os.environ.get("APEX_BOT_SIDE", slug)
    # V4.6.32 — per-broker trade log so the same bot keeps separate history
    # on each broker. Alpaca writes the historic flat path; others nest under
    # <data_dir>/<broker>/ to match core.data.log_files_for().
    broker = (os.environ.get("APEX_BROKER") or "alpaca").lower()
    base = Path(data_dir)
    if broker != "alpaca":
        base = base / broker
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    path = base / f"{side.lower()}_trade_log.jsonl"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_j.dumps({
                "time":    _dt.now(_tz.utc).isoformat(timespec="seconds"),
                "symbol":  symbol,
                "action":  action,
                "qty":     qty,
                "price":   price,
                "reason":  reason,
            }) + "\n")
    except Exception as e:
        print(f"  [trade-log] write failed: {e}", flush=True)


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
        print(f"[{self.name}] APEX bot framework v4.6.27  ·  "
              f"asset_type={self.asset_type}  ·  "
              f"tick={self.tick_seconds}s", flush=True)
        client, _, _ = _alpaca_clients(self.asset_type)
        # V4.6.26 — Initial portfolio cleanup. Liquidate any positions
        # whose asset_class doesn't match this bot's strategy (crypto
        # bot finding stocks, stocks bot finding options, etc.). Runs
        # ONCE at startup. The v4.6.2 AI transition cleanup runs after
        # this to handle the subtler long-vs-short cases.
        try:
            liquidate_off_strategy_positions(client, self.asset_type)
        except Exception as e:
            print(f"[{self.name}] startup cleanup error: {e}", flush=True)
        # V4.6.27 — outer loop wrapped in try/except + heartbeat prints
        # during the inter-tick sleep so users can SEE the bot is alive.
        # Previously a transient error in _account_snapshot or
        # _load_universe could kill the whole bot silently, and the
        # 300s sleep looked indistinguishable from a crash.
        cycle = 0
        while True:
            cycle += 1
            try:
                symbols = _load_universe(self.universe_path,
                                         self.default_symbols)
                if not symbols:
                    print(f"[{self.name}] no symbols configured — "
                          f"sleeping {self.tick_seconds}s",
                          flush=True)
                    self._heartbeat_sleep(self.tick_seconds, cycle)
                    continue
                account = _account_snapshot(client)
                print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                      f"=== cycle {cycle} === equity=${account['equity']:.2f}  "
                      f"cash=${account['cash']:.2f}  "
                      f"universe={len(symbols)}", flush=True)
                for sym in symbols:
                    try:
                        self._step(client, sym, decide, account)
                    except Exception as e:
                        print(f"  ERROR {sym}: {e}", flush=True)
                        traceback.print_exc()
                    time.sleep(1)  # tiny gap between symbols
                print(f"[{self.name}] cycle {cycle} done — "
                      f"next tick in {self.tick_seconds}s", flush=True)
            except Exception as e:
                # Catch-all so a transient API or yfinance hiccup
                # doesn't take the whole bot down. Bot will retry
                # on the next tick.
                print(f"[{self.name}] cycle {cycle} crashed: {e} "
                      f"— continuing", flush=True)
                traceback.print_exc()
            self._heartbeat_sleep(self.tick_seconds, cycle)

    def _heartbeat_sleep(self, total_seconds: int, cycle: int):
        """V4.6.27 — sleep with periodic 'alive' prints. Without
        this, a 5-min sleep looks identical to a crashed bot from
        the log viewer's point of view. Prints once a minute with
        the remaining time."""
        import math
        elapsed = 0
        interval = 60   # heartbeat every minute
        while elapsed < total_seconds:
            chunk = min(interval, total_seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk
            remaining = total_seconds - elapsed
            if remaining > 0:
                print(f"[{self.name}] sleeping  ·  cycle {cycle}  ·  "
                      f"next tick in {remaining}s", flush=True)

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
            # V4.6.30 — record the trade so the bot tab's Closed Trades
            # feed + equity history populate (custom bots showed nothing
            # under the console before this).
            try:
                last_price = float(bars["Close"].squeeze().iloc[-1])
            except Exception:
                last_price = 0.0
            _write_trade_log(self.name, symbol, action, d["qty"],
                             last_price, d.get("reason", ""))
        except Exception as e:
            print(f"  {action:<6} {symbol:<10}  FAILED: {e}",
                  flush=True)

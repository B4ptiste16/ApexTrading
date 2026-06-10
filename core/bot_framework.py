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
    """Return (trading_client, KEY, SECRET).
    V4.6.34 — delegates to core.broker_client.get_broker_client so framework
    custom bots automatically use the IBKR shim when APEX_BROKER=ibkr.
    Alpaca behavior is byte-for-byte unchanged when APEX_BROKER is unset or
    'alpaca' (the only path before v4.6.34).  Name kept for back-compat with
    any imports."""
    from core.broker_client import get_broker_client
    return get_broker_client(asset_type)


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


def liquidate_off_strategy_positions(client, asset_type: str,
                                     universe: Optional[list] = None) -> list:
    """Close any open position that doesn't belong to this bot's strategy:
      • wrong Alpaca asset_class for the bot's asset_type, OR
      • (V4.6.88) when `universe` is provided and non-empty, any symbol NOT in
        that universe — so a bot taking over an account only keeps & trades the
        tickers IT knows, and sells off whatever else was sitting in the
        account.
    Returns a list of (symbol, action) tuples for logging. Safe to call
    repeatedly — if nothing is off-strategy, no orders are sent."""
    asset_type = (asset_type or "").lower()
    # Normalised set of symbols this bot is allowed to hold (empty = no
    # universe filter, keep the legacy asset-class-only behaviour).
    def _norm(x) -> str:
        # Compare on a broker-agnostic form: upper-case, strip a crypto
        # '-USD'/'/USD' suffix so 'BTC-USD' and 'BTC' match.
        t = str(x or "").upper().replace("/", "").strip()
        for suf in ("-USD", "USD"):
            if t.endswith(suf) and len(t) > len(suf):
                t = t[: -len(suf)]
                break
        return t

    uni_set = {_norm(s) for s in (universe or []) if s}
    expected = _BOT_ASSET_TYPE_TO_ALPACA_CLASS.get(asset_type)
    if expected is None:
        print(f"[startup-cleanup] unknown asset_type='{asset_type}', "
              f"skipping cleanup", flush=True)
        return []
    # V4.6.56 — SAFETY: never run account-wide cleanup on a shared IBKR
    # account that has no sub-portfolio ledger. Without a ledger,
    # get_all_positions() returns EVERY bot's positions, so we'd wrongly
    # liquidate other bots' holdings. Only clean when a ledger isolates this
    # bot's own slice. (Alpaca clients have no `ledger` attr → cleanup runs.)
    if getattr(client, "ledger", "n/a") is None:
        print("[startup-cleanup] no sub-portfolio ledger yet — skipping "
              "account-wide cleanup to avoid touching other bots' positions",
              flush=True)
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
        # V4.6.88 — keep only same-asset-class AND in-universe symbols.
        in_universe = (not uni_set) or (_norm(sym) in uni_set)
        if pos_class == expected and in_universe:
            kept.append(sym)
            continue
        # Liquidate — wrong asset class, or not a ticker this bot knows.
        why = (f"asset_class={pos_class} != {asset_type}"
               if pos_class != expected
               else "not in this bot's universe")
        print(f"[startup-cleanup] {sym} ({why}) — liquidating", flush=True)
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
    out = {"action": action, "qty": qty, "reason": reason}
    # V4.6.48 — preserve an optional confidence (0..1) so the framework can
    # gate trades against the user's Minimum-confidence setting.
    conf = raw.get("confidence")
    if conf is not None:
        try:
            out["confidence"] = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            pass
    return out


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
        snap = {
            "cash":          float(a.cash),
            "equity":        float(a.equity),
            "buying_power":  float(a.buying_power),
        }
        # V4.6.43 — Alpaca's non_marginable_buying_power is the free cash
        # actually spendable on crypto (it already deducts open-order
        # reservations + held positions), which is what 'available' in the
        # broker's reject refers to. The IBKR shim doesn't expose it →
        # fall back to cash (its ledger/TotalCash already means free funds).
        nm = getattr(a, "non_marginable_buying_power", None)
        try:
            snap["available_cash"] = float(nm) if nm is not None else snap["cash"]
        except (TypeError, ValueError):
            snap["available_cash"] = snap["cash"]
        return snap
    except Exception:
        return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0,
                "available_cash": 0.0}


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
        print(f"[{self.name}] APEX bot framework v4.6.38  ·  "
              f"asset_type={self.asset_type}  ·  "
              f"tick={self.tick_seconds}s", flush=True)
        # V4.6.38 — BULLETPROOF startup. The broker client (Alpaca keys or an
        # IBKR gateway) might not be ready when the bot launches. Instead of
        # crashing with a KeyError / connection error, keep retrying every
        # tick with a clear message so the user can fix it live and the bot
        # picks up on its own — there is no failure mode that kills the bot.
        client = None
        did_startup_cleanup = False
        cycle = 0
        while True:
            cycle += 1
            try:
                if client is None:
                    try:
                        client, _, _ = _alpaca_clients(self.asset_type)
                    except Exception as e:
                        print(f"[{self.name}] broker not ready: {e}", flush=True)
                        print(f"[{self.name}] will retry in {self.tick_seconds}s "
                              f"— fix the above and the bot resumes "
                              f"automatically (no restart needed).", flush=True)
                        self._heartbeat_sleep(self._call_delay(), cycle)
                        continue
                # Load the bot's universe up-front so startup cleanup can keep
                # ONLY the tickers this bot knows.
                symbols = _load_universe(self.universe_path,
                                         self.default_symbols)
                if not did_startup_cleanup:
                    # Initial portfolio cleanup runs ONCE, after the client is
                    # live: liquidate positions whose asset_class doesn't match
                    # this bot's strategy OR aren't in its universe. Failure
                    # here must never kill the bot.
                    try:
                        liquidate_off_strategy_positions(
                            client, self.asset_type, universe=symbols)
                    except Exception as e:
                        print(f"[{self.name}] startup cleanup error: {e}",
                              flush=True)
                    did_startup_cleanup = True
                # V4.6.48 — read the FULL universe but skip symbols the active
                # broker can't trade (e.g. IBKR lists only major cryptos). The
                # broker client exposes tradeable_symbols() when it filters;
                # Alpaca trades everything so it has no such method.
                if symbols and hasattr(client, "tradeable_symbols"):
                    try:
                        _full = list(symbols)
                        symbols = client.tradeable_symbols(_full)
                        _skipped = [s for s in _full if s not in symbols]
                        if _skipped and _skipped != getattr(self, "_last_skipped", None):
                            print(f"[{self.name}] universe: {len(symbols)} "
                                  f"tradeable on this broker, skipping "
                                  f"{len(_skipped)} not supported: "
                                  f"{', '.join(_skipped[:12])}"
                                  f"{'…' if len(_skipped) > 12 else ''}",
                                  flush=True)
                            self._last_skipped = _skipped
                    except Exception as e:
                        print(f"[{self.name}] universe filter skipped: {e}",
                              flush=True)
                if not symbols:
                    print(f"[{self.name}] no symbols configured — "
                          f"sleeping {self.tick_seconds}s",
                          flush=True)
                    self._heartbeat_sleep(self._call_delay(), cycle)
                    continue
                # V4.6.51 — honor a pending allocation cut: sell holdings to
                # hand cash back to the main account before this cycle trades.
                if hasattr(client, "maybe_rebalance"):
                    try:
                        client.maybe_rebalance()
                    except Exception as e:
                        print(f"[{self.name}] rebalance check: {e}", flush=True)
                account = _account_snapshot(client)
                print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                      f"=== cycle {cycle} === equity=${account['equity']:.2f}  "
                      f"cash=${account['cash']:.2f}  "
                      f"universe={len(symbols)}", flush=True)
                # V4.6.91 — collect held-back BUY candidates this cycle so the
                # min-positions floor can deploy into them WITHOUT re-running
                # decide() (no extra AI calls).
                self._cycle_candidates = []
                for sym in symbols:
                    try:
                        self._step(client, sym, decide, account)
                    except Exception as e:
                        print(f"  ERROR {sym}: {e}", flush=True)
                        traceback.print_exc()
                    time.sleep(1)  # tiny gap between symbols
                # V4.6.91 — minimum-positions floor: if we hold fewer than the
                # user's floor, deploy into the highest-confidence candidates
                # that the confidence gate held back, so the bot stays invested.
                try:
                    self._enforce_min_positions(client, account)
                except Exception as e:
                    print(f"[{self.name}] min-positions enforcement: {e}",
                          flush=True)
                print(f"[{self.name}] cycle {cycle} done — "
                      f"next tick in {self.tick_seconds}s", flush=True)
            except Exception as e:
                # Catch-all so a transient API or yfinance hiccup
                # doesn't take the whole bot down. Bot will retry
                # on the next tick.
                print(f"[{self.name}] cycle {cycle} crashed: {e} "
                      f"— continuing", flush=True)
                traceback.print_exc()
            self._heartbeat_sleep(self._call_delay(), cycle)

    def _record_floor_candidate(self, symbol, alpaca_sym, bars, conf,
                                buy_intent):
        """V4.6.91 — remember a flat symbol the strategy didn't open this cycle
        so the min-positions floor can deploy into it (no extra decide() call)."""
        try:
            c = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            c = 0.0
        if not hasattr(self, "_cycle_candidates"):
            self._cycle_candidates = []
        self._cycle_candidates.append({
            "symbol": symbol, "alpaca_sym": alpaca_sym, "bars": bars,
            "conf": c, "buy_intent": bool(buy_intent)})

    def _enforce_min_positions(self, client, account):
        """V4.6.91 — keep at least `min positions` open. After the normal pass,
        if we hold fewer than the user's per-bot/per-broker floor, deploy free
        cash into the best held-back candidates (those the confidence gate
        blocked first, then highest confidence) with equal sizing. Honors the
        SAME setting the bot tab edits, resolved per broker."""
        side = (os.environ.get("APEX_BOT_SIDE", "") or str(self.name)).upper()
        try:
            from core import data as _D
            floor_n = int(_D.get_bot_min_positions(side))
        except Exception:
            floor_n = 0
        if floor_n <= 0:
            return
        try:
            positions = client.get_all_positions()
            held = sum(1 for p in positions
                       if abs(float(getattr(p, "qty", 0) or 0)) > 0)
        except Exception:
            return
        need = floor_n - held
        if need <= 0:
            return
        cands = getattr(self, "_cycle_candidates", []) or []
        if not cands:
            print(f"[{self.name}] min-positions floor: want {floor_n}, hold "
                  f"{held}, but no candidates this cycle", flush=True)
            return
        cands = sorted(cands, key=lambda d: (d["buy_intent"], d["conf"]),
                       reverse=True)
        try:
            cash = float(account.get("cash", 0) or 0)
        except Exception:
            cash = 0.0
        if cash <= 1:
            print(f"[{self.name}] min-positions floor: need {need} more but "
                  f"no free cash", flush=True)
            return
        per = (cash / max(need, 1)) * 0.95   # small buffer for fees/slippage
        bought = 0
        for d in cands:
            if bought >= need:
                break
            sym, asym, bars = d["symbol"], d["alpaca_sym"], d["bars"]
            try:
                px = float(bars["Close"].squeeze().iloc[-1])
            except Exception:
                continue
            if px <= 0:
                continue
            raw = per / px
            qty = raw if self.asset_type == "crypto" else float(int(raw))
            if qty <= 0:
                continue
            try:
                order = _submit(client, asym, "BUY", qty)
                bought += 1
                print(f"  FLOOR  {sym:<10}  qty={qty}  id={order.id}  "
                      f"(min-positions floor → reach {floor_n})", flush=True)
            except Exception as e:
                print(f"  FLOOR-FAIL {sym}: {e}", flush=True)
        if bought:
            print(f"[{self.name}] min-positions floor: opened {bought} "
                  f"position(s) to reach {floor_n}", flush=True)

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

    def _call_delay(self) -> int:
        """V4.6.66 — user-adjustable seconds between AI calls / cycles. Read
        fresh each cycle (so changes apply without a restart), floored for
        safety. Prefers the env var the cloud runner injects, then local
        settings, then this bot's built-in tick_seconds."""
        import os
        env = (os.environ.get(f"APEX_CALL_DELAY_{self.name.upper()}")
               or os.environ.get("APEX_CALL_DELAY"))
        val = None
        if env:
            try:
                val = int(float(env))
            except ValueError:
                val = None
        if val is None:
            try:
                import core.data as _D
                # V4.6.68 — default cadence is 30 min (DEFAULT_CALL_DELAY)
                # unless the user set a value for this bot.
                _dflt = getattr(_D, "DEFAULT_CALL_DELAY", 1800)
                val = int(_D.get_bot_call_delay(self.name.upper(), _dflt))
            except Exception:
                val = self.tick_seconds
        try:
            from core.data import CALL_DELAY_FLOOR as _floor
        except Exception:
            _floor = 30
        return max(int(_floor), int(val))

    def _min_confidence(self) -> float:
        """V4.6.48 — the user's 'Minimum confidence to trade' for this bot.
        Prefers the env var the runner injects (works on the cloud, where the
        settings file isn't present), then the local settings, then 0."""
        import os
        env = (os.environ.get(f"APEX_MIN_CONFIDENCE_{self.name.upper()}")
               or os.environ.get("APEX_MIN_CONFIDENCE"))
        if env:
            try:
                return float(env)
            except ValueError:
                pass
        try:
            import core.data as _D
            return float(_D.get_bot_min_conf(self.name.upper()))
        except Exception:
            return 0.0

    def _cap_to_funds(self, client, symbol: str, action: str,
                      qty: float, bars):
        """V4.6.43 — clamp a BUY/COVER qty to the funds actually spendable
        right now, so an over-sized order shrinks instead of being rejected.

        Re-reads the account live (cash changes as the bot fills earlier
        symbols in the same cycle). For crypto we use free cash (no margin);
        for equities we allow the broker's buying power. Leaves a small
        headroom for fees/slippage and respects whole-share rounding."""
        try:
            qty = float(qty or 0)
        except (TypeError, ValueError):
            return 0.0
        if qty <= 0:
            return qty
        try:
            price = float(bars["Close"].squeeze().iloc[-1])
        except Exception:
            price = 0.0
        if price <= 0:
            return qty   # can't price → don't interfere
        live = _account_snapshot(client)
        if self.asset_type == "crypto":
            # free cash for crypto (no margin) — deducts open orders/positions
            avail = live.get("available_cash", live.get("cash", 0.0))
        else:
            avail = max(live.get("buying_power", 0.0), live.get("cash", 0.0))
        if avail <= 0:
            return 0.0
        max_qty = (avail * 0.97) / price          # 3% headroom for fees/slippage
        if self.asset_type != "crypto":
            max_qty = float(int(max_qty))          # whole shares
        if max_qty <= 0:
            return 0.0
        if qty > max_qty:
            print(f"  CAP    {symbol:<10}  {action} qty {qty:g}→{max_qty:g} "
                  f"(only ${avail:.2f} free @ ${price:.4f})", flush=True)
            return max_qty
        return qty

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
        _flat = str(position.get("side", "flat")).lower() == "flat" \
            or abs(float(position.get("qty", 0) or 0)) < 1e-9
        if action == "HOLD":
            print(f"  HOLD   {symbol:<10}  {d['reason']}", flush=True)
            if _flat:
                self._record_floor_candidate(
                    symbol, alpaca_sym, bars, d.get("confidence"), buy_intent=False)
            return
        # V4.6.48 — universal minimum-confidence gate. If the strategy reports
        # a confidence (0..1), skip the trade when it's below the user's
        # "Minimum confidence to trade" setting. Bots that don't report a
        # confidence are never gated (unchanged behavior).
        conf = d.get("confidence")
        if conf is not None and action in ("BUY", "SELL", "SHORT", "COVER"):
            min_conf = self._min_confidence()
            if conf < min_conf:
                print(f"  SKIP   {symbol:<10}  confidence {conf:.0%} < "
                      f"min {min_conf:.0%}", flush=True)
                if _flat and action == "BUY":
                    # Wanted to buy but the gate blocked it → prime candidate
                    # for the min-positions floor.
                    self._record_floor_candidate(
                        symbol, alpaca_sym, bars, conf, buy_intent=True)
                return
        # V4.6.43 — cash-safety clamp. A strategy may size a BUY off equity
        # (total account value) rather than free cash, so when most of the
        # account is already invested it requests more than is available and
        # the broker rejects it ("insufficient balance"). Cap the BUY to the
        # funds actually spendable right now so the order succeeds (smaller)
        # instead of failing outright. Applies to entries only — exits
        # (SELL / closing) never need cash and are left untouched.
        if action in ("BUY", "COVER"):
            d["qty"] = self._cap_to_funds(client, symbol, action, d["qty"], bars)
            if d["qty"] is None or float(d["qty"]) <= 0:
                print(f"  SKIP   {symbol:<10}  no free cash to {action} "
                      f"(already fully invested)", flush=True)
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
            # V4.6.46 — a symbol IBKR doesn't list (e.g. ONDO) is a clean SKIP,
            # not an order failure: log it once-style and move on quietly.
            if type(e).__name__ == "UnsupportedSymbol":
                print(f"  SKIP   {symbol:<10}  {e}", flush=True)
            else:
                print(f"  {action:<6} {symbol:<10}  FAILED: {e}",
                      flush=True)

"""
APEX Core Data Layer
Handles all data fetching: Alpaca accounts, logs, snapshots, API costs.
Used by all UI tabs — no Qt imports here, pure Python.
"""

import os
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── Optional imports (graceful fallback if not installed) ──
try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
    from alpaca.trading.enums import QueryOrderStatus
    HAS_ALPACA = True
except ImportError:
    HAS_ALPACA = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

from core.paths import ACCOUNT_DIR as ROOT  # V4.6.101 — per-account data dir
# (DATA_DIR/accounts/<uid> on desktop; == DATA_DIR on the server). All
# account-scoped data — apex_settings.json, .env, ledgers, snapshots, per-bot
# state, universes — now lives under the signed-in account's folder so two
# accounts on one machine never share anything.

# In a frozen build the working dir is not the data dir, so load the
# user's API keys explicitly from the data folder.
try:
    from dotenv import load_dotenv as _load_env
    _load_env(ROOT / ".env")
except Exception:
    pass

BOT_SCRIPTS = {
    "LONG":  ROOT / "longbot_v2.py",
    "SHORT": ROOT / "shortbot_v2.py",
    "DAY":   ROOT / "daybot.py",
}

LOG_FILES = {
    "LONG":  [ROOT/"trade_log.jsonl", ROOT/"longv2_trade_log.jsonl"],
    "SHORT": [ROOT/"short_trade_log.jsonl", ROOT/"shortv2_trade_log.jsonl"],
    "DAY":   [ROOT/"daybot_trade_log.jsonl"],
}

SNAPSHOT_FILES = {
    "LONG":  [ROOT/"portfolio_snapshots.jsonl"],
    "SHORT": [ROOT/"shortv2_snapshots.jsonl"],
    "DAY":   [ROOT/"daybot_snapshots.jsonl"],
}


import threading as _threading
import time as _time_mod

# V4.6.64 — thread-local broker override + a short display cache so a background
# thread can PRELOAD the *other* broker's account/positions without changing the
# global setting. This makes switching Alpaca<->IBKR instant. These caches are
# DISPLAY-ONLY (the trading bots are separate processes) so staleness never
# affects orders.
_broker_override = _threading.local()
_DISPLAY_TTL = 6.0
_DISPLAY_TTL_CHART = 30.0   # charts change slowly — longer cache so broker
                            # switching renders instantly from a warm cache
_acct_cache:   dict = {}   # (broker, side) -> (ts, dict)
_pos_cache:    dict = {}   # (broker, side) -> (ts, list)
_orders_cache: dict = {}   # (broker, side) -> (ts, DataFrame)
_hist_cache:   dict = {}   # (broker, side, period) -> (ts, DataFrame)
_cache_lock = _threading.Lock()


def current_broker() -> str:
    ov = getattr(_broker_override, "value", None)
    if ov:
        return ov
    return load_settings().get("broker_mode", "alpaca")


class broker_context:
    """`with broker_context('alpaca'):` — read a specific broker's data on THIS
    thread only (used by the background preloader)."""
    def __init__(self, broker: str):
        self.broker = broker
    def __enter__(self):
        _broker_override.value = self.broker
        return self
    def __exit__(self, *a):
        _broker_override.value = None


def prefetch_broker(broker: str, sides: list, charts: bool = True) -> None:
    """Warm the display cache for `broker` (call from a background thread).
    With charts=True also warms the equity history + orders so the bot tabs'
    graphs render instantly when the user switches to this broker."""
    with broker_context(broker):
        for side in sides:
            try:
                get_account(side)
                get_positions(side)
                if charts:
                    get_orders(side)
                    get_history(side, "1D")
            except Exception:
                pass


def prefetch_other_broker(sides: list | None = None) -> None:
    """Preload whichever broker the user is NOT currently viewing."""
    cur = load_settings().get("broker_mode", "alpaca")
    other = "ibkr" if cur == "alpaca" else "alpaca"
    if sides is None:
        sides = ["LONG", "SHORT", "DAY"]
        try:
            reg = load_bot_registry()
            sides += [str(c.get("id", "")).upper()
                      for c in reg.get("custom", []) if c.get("id")]
        except Exception:
            pass
    prefetch_broker(other, list(dict.fromkeys(sides)))


def broker_data_dir(broker: str | None = None) -> Path:
    """V4.6.32 — per-broker data subdir so the SAME bot can run independently
    on Alpaca and IBKR with separate P/L history, trade logs and state.
    Alpaca keeps the historic flat layout at ROOT (no migration, no history
    loss); every other broker gets its own ROOT/<broker>/ folder.
    Shared across brokers (NOT scoped): universes, .env, settings, credits."""
    if broker is None:
        broker = current_broker()
    if broker == "alpaca":
        return ROOT
    d = ROOT / broker
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# Built-in conventional file stems per side (used to find non-alpaca logs)
_BUILTIN_LOG_NAME = {
    "LONG":  "longv2_trade_log.jsonl",
    "SHORT": "shortv2_trade_log.jsonl",
    "DAY":   "daybot_trade_log.jsonl",
}


def log_files_for(side: str) -> list:
    """V4.6.30 — resolve trade-log paths for ANY bot side, built-in or custom.
    V4.6.32 — Alpaca uses the historic flat paths; other brokers read from
    their own ROOT/<broker>/ folder so each broker keeps separate trades."""
    s = side.upper()
    if current_broker() == "alpaca":
        if s in LOG_FILES:
            return LOG_FILES[s]
        return [ROOT / f"{side.lower()}_trade_log.jsonl"]
    bdir = broker_data_dir()
    paths = [bdir / f"{side.lower()}_trade_log.jsonl"]
    if s in _BUILTIN_LOG_NAME:
        paths.append(bdir / _BUILTIN_LOG_NAME[s])
    return paths


def snapshot_files_for(side: str) -> list:
    """V4.6.30 — resolve snapshot/equity-history paths for any side.
    V4.6.32 — per-broker (Alpaca = historic flat layout)."""
    s = side.upper()
    if current_broker() == "alpaca":
        if s in SNAPSHOT_FILES:
            return SNAPSHOT_FILES[s]
        return [ROOT / f"{side.lower()}_lifetime.jsonl"]
    return [broker_data_dir() / f"{side.lower()}_lifetime.jsonl"]

DAY_STATE = ROOT / "daybot_state.json"


def day_state_path() -> Path:
    """Per-broker daybot state file (Alpaca = historic flat path)."""
    if current_broker() == "alpaca":
        return DAY_STATE
    return broker_data_dir() / "daybot_state.json"

# Anthropic pricing (per million tokens) — update if pricing changes
HAIKU_INPUT_PER_M  = 0.80
HAIKU_OUTPUT_PER_M = 4.00
SONNET_INPUT_PER_M = 3.00
SONNET_OUTPUT_PER_M= 15.00

# Alpaca period map
# V4.6.107 — "ALL" was missing, so `.get(period, ("1D","5Min"))` collapsed the
# whole-history view to the last day. Map it to a year of daily bars (covers all
# of these bots' lifetimes) so the value curve spans the full timeline.
ALPACA_PERIOD = {
    "1D": ("1D","5Min"), "1W": ("1W","1H"),
    "1M": ("1M","1D"),   "3M": ("3M","1D"),
    "6M": ("6M","1D"),   "1Y": ("1A","1D"),
    "ALL": ("1A","1D"),
}


# ─────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────

_clients = {}

def get_client(side: str):
    if not HAS_ALPACA:
        return None
    s = load_settings()
    # In non-Alpaca broker modes (e.g. IBKR) we must NOT fall back to the
    # Alpaca account — that would surface the wrong broker's returns. Each
    # broker owns its own data path.
    if current_broker() != "alpaca":
        return None
    side = side.upper()
    mode = s.get("alpaca_mode", "paper")          # paper / live
    cache_key = f"{side}:{mode}"
    if cache_key in _clients:
        return _clients[cache_key]
    # Build env-var names dynamically so any custom bot slug (e.g. "CRYPTO")
    # resolves to ALPACA_API_KEY_CRYPTO / ALPACA_SECRET_KEY_CRYPTO.
    fallback_key    = os.getenv("ALPACA_API_KEY", "")
    fallback_secret = os.getenv("ALPACA_SECRET_KEY", "")
    api_key    = os.getenv(f"ALPACA_API_KEY_{side}",    fallback_key)
    api_secret = os.getenv(f"ALPACA_SECRET_KEY_{side}", fallback_secret)
    # V4.6.58 — in LIVE mode prefer the live-namespaced keys (separate Alpaca
    # account), falling back to the paper keys for single-account users.
    if mode == "live":
        api_key    = (os.getenv(f"ALPACA_API_KEY_LIVE_{side}")
                      or os.getenv("ALPACA_API_KEY_LIVE") or api_key)
        api_secret = (os.getenv(f"ALPACA_SECRET_KEY_LIVE_{side}")
                      or os.getenv("ALPACA_SECRET_KEY_LIVE") or api_secret)
    if not api_key or not api_secret:
        return None
    try:
        client = TradingClient(api_key, api_secret, paper=(mode != "live"))
        _clients[cache_key] = client
        return client
    except Exception as e:
        print(f"[client] {side}: {e}")
        return None


def reset_clients():
    """Call after .env changes to reconnect."""
    _clients.clear()


# ─────────────────────────────────────────
# SAFE WRAPPERS
# ─────────────────────────────────────────

def safe(fn, default, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[safe] {fn.__name__}: {e}")
        return default


def load_jsonl(paths) -> list:
    rows = []
    for p in ([paths] if not isinstance(paths, list) else paths):
        p = Path(p)
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


# ─────────────────────────────────────────
# ACCOUNT DATA
# ─────────────────────────────────────────

def get_account(side: str) -> dict:
    # V4.6.64 — display cache (6s) keyed by broker+side so switching brokers is
    # instant and the background preloader can warm the other broker.
    b = current_broker()
    key = (b, side.upper())
    now = _time_mod.time()
    with _cache_lock:
        hit = _acct_cache.get(key)
    if hit and (now - hit[0]) < _DISPLAY_TTL:
        return hit[1]
    val = _get_account_uncached(side, b)
    if val:
        with _cache_lock:
            _acct_cache[key] = (now, val)
    return val


def _get_account_uncached(side: str, broker: str | None = None) -> dict:
    if (broker or current_broker()) == "ibkr":
        from core import ibkr_data
        return ibkr_data.get_account(side)
    c = get_client(side)
    if not c:
        return {}
    try:
        a = c.get_account()
        return {
            "portfolio_value": float(a.portfolio_value),
            "equity":          float(a.equity),
            "cash":            float(a.cash),
            "buying_power":    float(a.buying_power),
            "last_equity":     float(getattr(a,"last_equity",a.equity) or a.equity),
            "connected":       True,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


_ATR_CACHE: dict = {}            # ticker -> (timestamp, atr_pct)
_ATR_CACHE_TTL = 3600            # one hour


def _ticker_atr_pct(symbol: str) -> float:
    """Fetch this ticker's 14-day ATR as a % of price.
    Cached for an hour so the chart doesn't hit yfinance every refresh."""
    import time as _time
    now = _time.time()
    cached = _ATR_CACHE.get(symbol)
    if cached and (now - cached[0]) < _ATR_CACHE_TTL:
        return cached[1]
    try:
        import yfinance as yf
        # Map crypto tickers to yfinance form: BTCUSD / BTC/USD -> BTC-USD.
        yf_sym = symbol
        s = symbol.replace("/", "").upper()
        if s.endswith("USD") and "-" not in symbol and len(s) > 3:
            yf_sym = f"{s[:-3]}-USD"
        hist = yf.Ticker(yf_sym).history(period="30d", auto_adjust=True)
        if hist.empty or len(hist) < 14:
            _ATR_CACHE[symbol] = (now, 0.025)
            return 0.025
        h = hist["High"].values.astype(float)
        l = hist["Low"].values.astype(float)
        c = hist["Close"].values.astype(float)
        tr = np.maximum(h[1:] - l[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]),
                                   np.abs(l[1:] - c[:-1])))
        atr = float(tr[-14:].mean())
        price = float(c[-1])
        atr_pct = atr / price if price > 0 else 0.025
        _ATR_CACHE[symbol] = (now, atr_pct)
        return atr_pct
    except Exception:
        _ATR_CACHE[symbol] = (now, 0.025)
        return 0.025


def position_meta(positions: list, side: str,
                  stop_mult: float = 2.5,
                  tp_mult:   float = 5.0) -> dict:
    """For each open position, return per-stock ATR-based stop/target
    levels so the position-gauge chart shows DIFFERENT objectives per
    ticker instead of a flat 2.5% fallback for all of them.

    Returns {symbol: {atr_pct, stop_pct, tp_pct, stop_price, tp_price}}.
    """
    meta: dict = {}
    # Pre-fetch every ticker's ATR concurrently. _ticker_atr_pct is cached for
    # an hour, but on a cold cache a bot with N positions used to make N
    # sequential yfinance calls (~1s each) — the main cause of slow chart
    # loading. Run them in a small thread pool so it's one round-trip instead.
    syms = [p.get("symbol") for p in positions
            if p.get("symbol") and float(p.get("avg_entry_price", 0)) > 0]
    atr_by_sym: dict = {}
    if syms:
        from concurrent.futures import ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(syms))) as ex:
                for s, a in zip(syms, ex.map(_ticker_atr_pct, syms)):
                    atr_by_sym[s] = a
        except Exception:
            atr_by_sym = {s: _ticker_atr_pct(s) for s in syms}
    for p in positions:
        sym = p.get("symbol")
        entry = float(p.get("avg_entry_price", 0))
        if not sym or entry <= 0:
            continue
        atr_pct = atr_by_sym.get(sym) or _ticker_atr_pct(sym)
        if side == "SHORT":
            stop_pct = +atr_pct * stop_mult
            tp_pct   = -atr_pct * tp_mult
        else:
            stop_pct = -atr_pct * stop_mult
            tp_pct   = +atr_pct * tp_mult
        meta[sym] = {
            "atr_pct":    atr_pct,
            "stop_pct":   stop_pct * 100,
            "tp_pct":     tp_pct   * 100,
            "stop_price": round(entry * (1 + stop_pct), 2),
            "tp_price":   round(entry * (1 + tp_pct),   2),
        }
    return meta


def get_positions(side: str) -> list:
    # V4.6.64 — display cache (6s) + broker-override aware (see get_account).
    b = current_broker()
    key = (b, side.upper())
    now = _time_mod.time()
    with _cache_lock:
        hit = _pos_cache.get(key)
    if hit and (now - hit[0]) < _DISPLAY_TTL:
        return hit[1]
    val = _get_positions_uncached(side, b)
    with _cache_lock:
        _pos_cache[key] = (now, val)
    return val


def _get_positions_uncached(side: str, broker: str | None = None) -> list:
    # V4.6.43 — route to the IBKR data path in IBKR mode (get_client() returns
    # None there by design, which previously left the IBKR page with no
    # positions at all).
    if (broker or current_broker()) == "ibkr":
        try:
            from core import ibkr_data
            return ibkr_data.get_positions(side)
        except Exception as e:
            print(f"[positions] IBKR {side}: {e}")
            return []
    c = get_client(side)
    if not c:
        return []
    try:
        positions = c.get_all_positions() or []
        return [{
            "symbol":          p.symbol,
            "qty":             float(p.qty),
            "market_value":    float(p.market_value),
            "avg_entry_price": float(p.avg_entry_price),
            "unrealized_pl":   float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
            "current_price":   float(p.current_price) if hasattr(p,"current_price") else 0,
        } for p in positions]
    except Exception as e:
        print(f"[positions] {side}: {e}")
        return []


def _ibkr_cloud_orders(side: str) -> pd.DataFrame:
    """V4.6.63 — build an orders/fills DataFrame for a cloud IBKR bot from the
    server's recorded fills (the desktop has no IBKR order API). Same columns
    as the Alpaca path so trade history / closed trades / summary work."""
    try:
        s = load_settings()
        mode = s.get("alpaca_mode", "paper")
        cfg = s.get(f"ibkr_{mode}", s.get("ibkr", {})) or {}
        if not cfg.get("run_on_oracle"):
            return pd.DataFrame()
        from core import ibkr_data as _ix
        tok, url = _ix._cloud_creds()
        if not tok:
            return pd.DataFrame()
        import requests
        r = requests.get(f"{url}/ibkr/{side}/fills", params={"mode": mode},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=8)
        fills = r.json().get("fills", []) if r.ok else []
    except Exception as e:
        print(f"[orders] IBKR cloud {side}: {e}")
        return pd.DataFrame()
    rows = []
    for f in fills:
        try:
            q = float(f.get("qty", 0)); p = float(f.get("price", 0))
        except (TypeError, ValueError):
            continue
        rows.append({
            "Ticker":    f.get("symbol", ""),
            "Side":      str(f.get("side", "")).upper(),
            "Qty":       round(q, 6),
            "Notional":  round(q * p, 2),
            "Status":    "filled",
            "Submitted": f.get("ts"),
            "Filled":    f.get("ts"),
            "Avg Fill":  p,
            "Type":      "market",
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["Submitted"] = pd.to_datetime(df["Submitted"], utc=True, errors="coerce")
        df["Filled"]    = pd.to_datetime(df["Filled"],    utc=True, errors="coerce")
        df = df.sort_values("Submitted", ascending=False)
    return df


def get_orders(side: str) -> pd.DataFrame:
    # V4.6.67 — cached per broker so switching brokers shows trades instantly
    # and the background preloader can warm the other broker.
    b = current_broker()
    key = (b, side.upper())
    now = _time_mod.time()
    with _cache_lock:
        hit = _orders_cache.get(key)
    if hit and (now - hit[0]) < _DISPLAY_TTL_CHART:
        return hit[1]
    df = _get_orders_uncached(side, b)
    with _cache_lock:
        _orders_cache[key] = (now, df)
    return df


def _get_orders_uncached(side: str, broker: str | None = None) -> pd.DataFrame:
    # V4.6.63 — cloud IBKR bots have no Alpaca order API; read recorded fills
    # from the server instead.
    if (broker or current_broker()) == "ibkr":
        return _ibkr_cloud_orders(side)
    c = get_client(side)
    if not c:
        return pd.DataFrame()
    try:
        orders = c.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500, nested=True)) or []
        rows = []
        for o in orders:
            fq = float(o.filled_qty or 0)
            fp = float(o.filled_avg_price or 0)
            rows.append({
                "Ticker":    o.symbol,
                "Side":      str(o.side).replace("OrderSide.","").upper(),
                "Qty":       round(fq, 6),
                "Notional":  round(fq*fp, 2),
                "Status":    str(o.status).replace("OrderStatus.",""),
                "Submitted": o.submitted_at,
                "Filled":    o.filled_at,
                "Avg Fill":  fp,
                "Type":      str(getattr(o,"order_class","simple")),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df["Submitted"] = pd.to_datetime(df["Submitted"], utc=True, errors="coerce")
            df["Filled"]    = pd.to_datetime(df["Filled"],    utc=True, errors="coerce")
            df = df.sort_values("Submitted", ascending=False)
        return df
    except Exception as e:
        print(f"[orders] {side}: {e}")
        return pd.DataFrame()


def get_history(side: str, period: str) -> pd.DataFrame:
    """V4.6.67 — cached per (broker, side, period) so the equity chart renders
    instantly on broker switch (and the preloader can warm both brokers)."""
    b = current_broker()
    key = (b, side.upper(), period)
    now = _time_mod.time()
    with _cache_lock:
        hit = _hist_cache.get(key)
    if hit and (now - hit[0]) < _DISPLAY_TTL_CHART:
        return hit[1]
    df = _get_history_uncached(side, period, b)
    with _cache_lock:
        _hist_cache[key] = (now, df)
    return df


def _get_history_uncached(side: str, period: str,
                          broker: str | None = None) -> pd.DataFrame:
    """
    Live portfolio history for one bot's Alpaca account.

    V7.1.14: chart no longer shrinks to 13:30-20:00 UTC (US market hours).
    """
    # V4.6.43 — IBKR mode has its own equity-history source.
    if (broker or current_broker()) == "ibkr":
        try:
            from core import ibkr_data
            return ibkr_data.get_history(side, period)
        except Exception as e:
            print(f"[history] IBKR {side}: {e}")
            return pd.DataFrame()
    c = get_client(side)
    if not c:
        return pd.DataFrame()
    try:
        ap, tf = ALPACA_PERIOD.get(period, ("1D","5Min"))
        req_kwargs = dict(period=ap, timeframe=tf)
        # Prefer the proper enum-based switch on modern alpaca-py;
        # fall back to the older boolean toggle.
        try:
            from alpaca.trading.enums import IntradayReporting  # type: ignore
            req_kwargs["intraday_reporting"] = IntradayReporting.CONTINUOUS
        except Exception:
            req_kwargs["extended_hours"] = True

        h = c.get_portfolio_history(GetPortfolioHistoryRequest(**req_kwargs))
        if not h or not h.timestamp or not h.equity:
            return pd.DataFrame()

        df = pd.DataFrame({
            "time":   pd.to_datetime(h.timestamp, unit="s", utc=True),
            "equity": [float(v) if v is not None else float("nan")
                       for v in h.equity],
            # v3.1.2 — Alpaca's profit_loss is the equity series MINUS
            # deposits / withdrawals at each tick. Lets the chart show a
            # "Trade Republic style" performance line where adding cash
            # doesn't fake a sudden jump.
            "profit_loss": [float(v) if v is not None else float("nan")
                            for v in (getattr(h, "profit_loss", None)
                                       or [None] * len(h.timestamp))],
        })
        df["equity"]      = df["equity"].ffill().bfill()
        df["profit_loss"] = df["profit_loss"].fillna(0.0)
        return df[df["equity"] > 0].reset_index(drop=True)
    except Exception as e:
        print(f"[history] {side} {period}: {e}")
        return pd.DataFrame()


def active_bot_sides() -> list[str]:
    """V4.6.94 — every active (non-silenced) bot side for the CURRENT
    broker/mode: built-ins + custom bots. Used so the combined portfolio chart
    reflects ALL bots (e.g. a custom 'energy' bot), not just LONG/SHORT/DAY.
    Falls back to the three built-ins if the registry can't be read."""
    try:
        reg = load_bot_registry()
        active   = list(reg.get("active", ["LONG", "SHORT", "DAY"]))
        silenced = set(reg.get("silenced", []))
        sides = [s for s in active if s not in silenced]
        return sides or ["LONG", "SHORT", "DAY"]
    except Exception:
        return ["LONG", "SHORT", "DAY"]


def get_combined_history(period: str) -> pd.DataFrame:
    """
    Total portfolio value over time = sum of EVERY active bot's equity,
    aligned on each bot's recorded timestamps. Works 24/7, independent of
    whether any bot is running.

    v3.1.2 — also sums profit_loss across all sides so the overview chart
    can show deposit-adjusted performance.
    V4.6.94 — now sums ALL active bots (incl. custom bots like 'energy'),
    not just the three built-ins, so the curve matches the live total.
    """
    frames = []
    for side in active_bot_sides():
        df = get_history(side, period)
        if df is not None and not df.empty:
            frames.append(df.rename(columns={
                "equity":      f"eq_{side}",
                "profit_loss": f"pl_{side}",
            }).set_index("time"))
    if not frames:
        return pd.DataFrame()
    # V4.6.94 — ffill then bfill (not fillna(0)): a bot that started mid-period
    # otherwise dragged the combined equity down to a bogus 0 at the start,
    # producing a flat/spiky curve. bfill carries its first known value back.
    merged = pd.concat(frames, axis=1).sort_index().ffill().bfill().fillna(0.0)
    eq_cols = [c for c in merged.columns if c.startswith("eq_")]
    eq_series = (merged[eq_cols].sum(axis=1)
                 if eq_cols else pd.Series(0.0, index=merged.index))
    # V4.6.103 — ALWAYS derive a CUMULATIVE P/L from equity (period start as the
    # baseline). We no longer sum the broker's per-day profit_loss column:
    # Alpaca resets profit_loss to ~0 at every market open, which made the
    # combined chart cliff straight down to zero at each day boundary. A
    # continuous equity-baseline curve reads as true performance over time and
    # matches IBKR/custom bots (which record equity only).
    base = float(eq_series.iloc[0]) if len(eq_series) else 0.0
    pl_series = eq_series - base
    return pd.DataFrame({
        "time":        merged.index,
        "equity":      eq_series.values,
        "profit_loss": pl_series.values,
    }).reset_index(drop=True)


def compute_trade_events(orders_df, side: str) -> list:
    """v3.1.2 — extract per-order events for the equity-chart vertical
    markers. Returns a list of (timestamp, kind, label) where:
      kind = "buy"  → orange vertical line  (opening a position)
      kind = "win"  → green                 (closing at profit)
      kind = "loss" → red                   (closing at loss)
    Uses a running average cost basis per ticker to classify each closing
    order. For SHORT bots the BUY/SELL semantics are swapped so the
    colours stay consistent (orange = enter, green/red = close)."""
    if orders_df is None or orders_df.empty:
        return []
    df = orders_df[orders_df["Filled"].notna()].copy()
    if df.empty:
        return []
    df = df.sort_values("Filled")
    events = []
    basis: dict = {}      # ticker → (qty, avg_price)

    for _, row in df.iterrows():
        t     = row["Ticker"]
        s     = str(row["Side"]).upper()
        price = float(row.get("Avg Fill") or 0)
        qty   = float(row.get("Qty") or 0)
        ts    = row["Filled"]
        if not ts or price <= 0 or qty <= 0:
            continue

        old_qty, old_avg = basis.get(t, (0.0, 0.0))

        if side == "SHORT":
            if s == "SELL":            # opening short
                kind = "buy"
                new_qty = old_qty + qty
                new_avg = ((old_qty * old_avg + qty * price) / new_qty
                           if new_qty else 0)
                basis[t] = (new_qty, new_avg)
                label = f"SHORT {t} @${price:.2f}"
            else:                       # BUY = cover
                kind = ("win" if (old_avg > 0 and price < old_avg)
                        else "loss" if old_avg > 0 else "buy")
                basis[t] = (max(0.0, old_qty - qty), old_avg)
                label = f"COVER {t} @${price:.2f}"
        else:                           # LONG / DAY
            if s == "BUY":
                kind = "buy"
                new_qty = old_qty + qty
                new_avg = ((old_qty * old_avg + qty * price) / new_qty
                           if new_qty else 0)
                basis[t] = (new_qty, new_avg)
                label = f"BUY {t} @${price:.2f}"
            else:                       # SELL = close
                kind = ("win" if (old_avg > 0 and price >= old_avg)
                        else "loss" if old_avg > 0 else "buy")
                basis[t] = (max(0.0, old_qty - qty), old_avg)
                label = f"SELL {t} @${price:.2f}"

        events.append((ts, kind, label))
    return events


# ─────────────────────────────────────────
# LIVE BOT SETTINGS  (editable from the app, read by the bots each cycle)
# ─────────────────────────────────────────

SETTINGS_FILE = ROOT / "apex_settings.json"

# Built-in defaults (must match each bot's MIN_CONFIDENCE constant)
BOT_DEFAULT_CONF = {"LONG": 0.60, "SHORT": 0.65, "DAY": 0.72}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def bot_registry_key(mode: str | None = None) -> str:
    """Return the per-user, per-broker, per-mode settings key for the bot
    registry.  Paper and live trading — and Alpaca vs IBKR — have completely
    independent bot lists.

    V4.6.116 — CRITICAL FIX: the uid was read from `auth['user_id']`, which is
    None (the id lives at `auth['user']['id']`), so the key collapsed to the
    plain non-broker-scoped "bot_registry". That made Alpaca and IBKR SHARE one
    registry — silencing a bot on Alpaca also silenced its IBKR twin. We now take
    the uid from active_account_id() (reliable) and ALWAYS include the broker, so
    the two brokers can never collide."""
    try:
        s = load_settings()
        broker = s.get("broker_mode", "alpaca")
        if mode is None:
            mode = s.get("alpaca_mode", "paper")
        uid = ""
        try:
            from core.paths import active_account_id
            uid = active_account_id() or ""
        except Exception:
            uid = ""
        if not uid:
            try:
                from ui.login import load_auth
                auth = load_auth() or {}
                u = auth.get("user") if isinstance(auth.get("user"), dict) else {}
                uid = str(auth.get("user_id") or auth.get("email")
                          or (u or {}).get("id") or (u or {}).get("email") or "")
            except Exception:
                uid = ""
        uid = uid or "default"
        return f"bot_registry_{uid}_{broker}_{mode}"
    except Exception:
        return "bot_registry"


def load_bot_registry() -> dict:
    """Load the per-user, per-broker, per-mode bot registry from settings.
    Migration chain (newest → oldest):
      bot_registry_{uid}_{broker}_{mode}   ← current
      bot_registry_{uid}_{broker}          ← pre-mode-split (< v4.6.31)
      bot_registry                         ← global legacy
    Always returns a dict with 'active', 'silenced', 'custom' keys."""
    s   = load_settings()
    key = bot_registry_key()
    reg = s.get(key)
    if reg is None:
        # Migrate from old per-broker key (no mode suffix)
        try:
            from ui.login import load_auth
            auth = load_auth() or {}
            uid = auth.get("user_id") or auth.get("email") or ""
            broker = s.get("broker_mode", "alpaca")
            if uid:
                reg = s.get(f"bot_registry_{uid}_{broker}")
        except Exception:
            pass
    reg = reg or s.get("bot_registry") or {}
    reg.setdefault("active",   [])
    reg.setdefault("silenced", [])
    reg.setdefault("custom",   [])
    return reg


def save_bot_registry(reg: dict) -> None:
    """Persist *reg* under the per-user, per-broker settings key."""
    s = load_settings()
    s[bot_registry_key()] = reg
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def load_all_custom_bots() -> list[dict]:
    """Return every custom bot the user has defined across ALL broker/mode
    registries, de-duplicated by id.  Bot DEFINITIONS are not mode-specific —
    a bot built under Alpaca should still be selectable under IBKR — so the
    add/replace pickers look here rather than at a single registry."""
    s = load_settings()
    seen: dict[str, dict] = {}
    for key, val in s.items():
        if not str(key).startswith("bot_registry") or not isinstance(val, dict):
            continue
        for c in val.get("custom", []):
            if isinstance(c, dict) and c.get("id"):
                seen.setdefault(c["id"], c)
    return list(seen.values())


def get_auto_schedule() -> bool:
    """Back-compat: True if any bot has auto-schedule enabled. Used
    elsewhere as a coarse 'is the schedule feature active' check."""
    return bool(get_auto_schedule_active_bots())


def set_auto_schedule(on: bool) -> None:
    """Legacy global toggle, kept for migration. New code uses
    set_auto_schedule_for(side, on)."""
    s = load_settings()
    s["auto_schedule"] = bool(on)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# ── V7.1.10: per-bot auto-schedule (v4.6.31: also per trading-mode) ──

def _auto_schedule_key() -> str:
    """Settings key for auto-schedule flags — scoped to the current mode
    (paper / live) so schedules are independent between the two."""
    mode = load_settings().get("alpaca_mode", "paper")
    return f"auto_schedule_bots_{mode}"


def get_auto_schedule_for(side: str) -> bool:
    """Per-bot, per-mode auto-schedule flag.
    Migration: falls back to the shared 'auto_schedule_bots' key (< v4.6.31)
    and then to the legacy global flag (< V7.1.10)."""
    s = load_settings()
    # Current per-mode key
    per_bot = s.get(_auto_schedule_key())
    if per_bot is None:
        # Pre-mode-split key
        per_bot = s.get("auto_schedule_bots")
    if per_bot is not None and side in per_bot:
        return bool(per_bot[side])
    return bool(s.get("auto_schedule", False))


def set_auto_schedule_for(side: str, on: bool) -> None:
    s = load_settings()
    key = _auto_schedule_key()
    per_bot = dict(s.get(key) or {})
    per_bot[side] = bool(on)
    s[key] = per_bot
    s.pop("auto_schedule", None)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def get_auto_schedule_active_bots() -> list[str]:
    """Bot sides flagged for auto-start at market open, current mode only."""
    s = load_settings()
    per_bot = s.get(_auto_schedule_key())
    if per_bot is None:
        per_bot = s.get("auto_schedule_bots")
    if per_bot is not None:
        return [side for side, on in per_bot.items() if on]
    if s.get("auto_schedule"):
        reg = load_bot_registry()
        return list(reg.get("active", ["LONG", "SHORT", "DAY"]))
    return []


# ── V7.1.13: per-bot cloud-execution toggle ─────────────────────────

def _cloud_bots_key() -> str:
    """V4.6.33 — cloud-execution flags are per-broker so a bot started on
    Alpaca's cloud doesn't show as running in the IBKR view (and vice-versa)."""
    return f"cloud_bots_{current_broker()}"


def get_cloud_bots() -> list[str]:
    """Sides set to run on the APEX Oracle server instead of locally,
    scoped to the current broker."""
    s = load_settings()
    key = _cloud_bots_key()
    if key in s:
        return list(s[key])
    # Migrate the legacy global list into Alpaca (the only pre-v4.6.33 broker).
    if current_broker() == "alpaca":
        return list(s.get("cloud_bots", []))
    return []


def set_cloud_bots(sides: list[str]) -> None:
    s = load_settings()
    s[_cloud_bots_key()] = sorted({str(x).upper() for x in sides})
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def is_cloud_bot(side: str) -> bool:
    return side.upper() in {s.upper() for s in get_cloud_bots()}


def add_cloud_bot(side: str) -> None:
    cur = set(get_cloud_bots())
    cur.add(side.upper())
    set_cloud_bots(list(cur))


def remove_cloud_bot(side: str) -> None:
    cur = set(get_cloud_bots())
    cur.discard(side.upper())
    set_cloud_bots(list(cur))


# ── V7.1+: force-update setting ─────────────────────────────────────

def day_pl_baseline(side: str, current_eq: float):
    """V4.6.119 — the bot's equity at the PREVIOUS market close, derived from its
    local snapshots, with a re-seed guard. Mirrors the Overview card logic so
    IBKR bots — whose broker reports last_equity == equity (giving a 0 day P/L) —
    get a real daily baseline. Returns None when there's no usable history."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    try:
        snaps = read_bot_snapshots(side)
    except Exception:
        snaps = []
    if not snaps:
        return None
    now = _dt.now(_tz.utc)
    off = -4 if 3 <= now.month <= 11 else -5
    et = now + _td(hours=off)
    op = et.replace(hour=9, minute=30, second=0, microsecond=0)
    if et < op:
        op -= _td(days=1)
    while op.weekday() >= 5:
        op -= _td(days=1)
    base_et = (op - _td(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
    while base_et.weekday() >= 5:
        base_et -= _td(days=1)
    last_close = (base_et - _td(hours=off)).replace(tzinfo=_tz.utc)
    # V4.6.120 — anchor the baseline AFTER the most recent capital event so a
    # deposit / withdrawal / re-allocation is never counted as P/L. We detect a
    # re-allocation precisely when the recorded `allocated` changed between two
    # snapshots; for older snapshots that predate the field, a >40% equity jump
    # between consecutive points is the fallback (a re-seed, not a trade).
    floor_ts = None
    pv = None
    pts = None
    pa = None
    for s in reversed(snaps):
        try:
            v = float(s.get("equity", 0) or 0)
            ts = _dt.fromisoformat(s["ts"])
            a = s.get("allocated")
            a = float(a) if a is not None else None
        except Exception:
            continue
        if pv is not None:
            if a is not None and pa is not None and abs(a - pa) > 1.0:
                floor_ts = pts
                break
            if v > 0 and pv > 0 and abs(pv - v) / max(pv, v) > 0.40:
                floor_ts = pts
                break
        pv, pts, pa = v, ts, a
    base = None
    for s in reversed(snaps):
        try:
            ts = _dt.fromisoformat(s["ts"])
            if floor_ts is not None and ts < floor_ts:
                break
            if ts <= last_close:
                base = float(s.get("equity", current_eq))
                break
        except Exception:
            continue
    if base is None:
        for s in snaps:
            try:
                ts = _dt.fromisoformat(s["ts"])
                if floor_ts is None or ts >= floor_ts:
                    base = float(s.get("equity", current_eq))
                    break
            except Exception:
                continue
    return base


def get_bot_metrics(side: str) -> dict:
    """V7.1.4 — one-stop summary of all the numbers the Overview sort
    dropdown might key on. Each value falls back to 0.0 when the
    underlying Alpaca call can't be made (no keys linked, etc.) so the
    sort still has a stable order even with missing data.

    Returns:
        {
          "portfolio":   total bot account value (float),
          "day_pl":      $ change today (float),
          "day_pct":     % change today (float),
          "positions":   # of open positions (int),
          "win_rate":    closed-trade win rate, 0..100 (float),
          "lifetime_pl": realised P/L across all closed trades ($, float),
        }
    """
    out = {"portfolio": 0.0, "day_pl": 0.0, "day_pct": 0.0,
           "positions": 0, "win_rate": 0.0, "lifetime_pl": 0.0}
    try:
        a = get_account(side) or {}
        out["portfolio"] = float(a.get("portfolio_value") or 0)
        eq  = float(a.get("equity") or 0)
        le  = float(a.get("last_equity") or eq)
        out["day_pl"]  = eq - le
        out["day_pct"] = ((eq - le) / le * 100) if le else 0.0
        # V4.6.119 — IBKR reports last_equity == equity (no intraday baseline),
        # so the above is always 0. Fall back to the snapshot-derived previous-
        # close baseline (same as the Overview cards) for a real daily figure.
        if abs(eq - le) < 1e-9 and eq > 0:
            b = day_pl_baseline(side, eq)
            if b and b > 0:
                out["day_pl"]  = eq - b
                out["day_pct"] = (eq - b) / b * 100
    except Exception:
        pass
    try:
        out["positions"] = len(get_positions(side) or [])
    except Exception:
        pass
    # Lifetime P/L and win-rate from orders. We pair each SELL with the
    # average BUY price seen so far for the same symbol (FIFO bucket).
    # Not a proper portfolio attribution but a fair best-effort that
    # matches the closed-trades feed on the bot tab.
    try:
        df = get_orders(side)
        if df is not None and not df.empty:
            df = df[df["Status"].str.lower() == "filled"]
            wins = 0
            total_closed = 0
            pl = 0.0
            avg_buy = {}   # ticker → (qty, total cost)
            # Process oldest → newest so buys precede their sells.
            for _, row in df.sort_values("Submitted").iterrows():
                t = row["Ticker"]
                q = float(row.get("Qty") or 0)
                p = float(row.get("Avg Fill") or 0)
                side_o = str(row.get("Side", "")).upper()
                if q <= 0 or p <= 0:
                    continue
                if side_o == "BUY":
                    qt, ct = avg_buy.get(t, (0.0, 0.0))
                    avg_buy[t] = (qt + q, ct + q * p)
                elif side_o == "SELL":
                    qt, ct = avg_buy.get(t, (0.0, 0.0))
                    if qt > 0:
                        avg_p = ct / qt
                        trade_pl = (p - avg_p) * q
                        pl += trade_pl
                        total_closed += 1
                        if trade_pl > 0:
                            wins += 1
                        # consume the sold shares from the bucket
                        new_qty = max(0.0, qt - q)
                        new_cost = avg_p * new_qty
                        avg_buy[t] = (new_qty, new_cost)
            out["lifetime_pl"] = round(pl, 2)
            out["win_rate"] = (wins / total_closed * 100) if total_closed else 0.0
    except Exception:
        pass
    return out


def get_force_update_now() -> bool:
    """If true, auto-update can apply at any time (not just overnight)."""
    return bool(load_settings().get("force_update_now", False))


def set_force_update_now(on: bool) -> None:
    s = load_settings()
    s["force_update_now"] = bool(on)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def get_bot_min_conf(side: str) -> float:
    """Effective min-confidence for a bot (saved override or default)."""
    default = BOT_DEFAULT_CONF.get(side, 0.65)
    try:
        v = float(load_settings().get(side, {}).get("min_confidence"))
        return v if 0.0 < v <= 1.0 else default
    except Exception:
        return default


def set_bot_min_conf(side: str, value: float) -> None:
    """Persist a new min-confidence for a bot. Picked up on its next cycle."""
    s = load_settings()
    s.setdefault(side, {})["min_confidence"] = round(float(value), 2)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# ── Call delay (V4.6.66) — how often a bot calls the AI / runs a cycle ──────
# Faster than this floor risks rate-limit / cost spikes / overlapping cycles.
CALL_DELAY_FLOOR = 30          # seconds — minimum allowed
DEFAULT_CALL_DELAY = 1800      # seconds — default cadence (30 min) when unset
_CALL_TOKENS = {
    "LONG":  {"input": 12000, "output": 600},
    "SHORT": {"input": 10000, "output": 600},
    "DAY":   {"input":  6000, "output": 400},
}


def get_bot_call_delay(side: str, default: int = DEFAULT_CALL_DELAY) -> int:
    """Effective call delay (seconds) for a bot — saved override or default."""
    try:
        v = int(load_settings().get(side, {}).get("call_delay") or 0)
        return v if v >= CALL_DELAY_FLOOR else int(default)
    except Exception:
        return int(default)


def set_bot_call_delay(side: str, seconds: int) -> None:
    """Persist a new call delay (seconds). Picked up on the bot's next cycle."""
    s = load_settings()
    s.setdefault(side, {})["call_delay"] = max(CALL_DELAY_FLOOR, int(seconds))
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def resolve_call_delay(side: str) -> int:
    """V4.6.68 — the effective seconds a bot should sleep between AI calls.
    Honors the cloud-synced env var, then the user's saved per-bot setting,
    then the 30-min default. Floored for safety. Built-in bots (LONG/SHORT/DAY)
    call this from their own loop so the call-delay control applies to them too."""
    import os
    env = (os.environ.get(f"APEX_CALL_DELAY_{side.upper()}")
           or os.environ.get("APEX_CALL_DELAY"))
    if env:
        try:
            return max(CALL_DELAY_FLOOR, int(float(env)))
        except (TypeError, ValueError):
            pass
    return max(CALL_DELAY_FLOOR, get_bot_call_delay(side, DEFAULT_CALL_DELAY))


def estimate_call_cost(side: str) -> float:
    """Approx $ per AI call for this bot (Haiku pricing, token estimate)."""
    est = _CALL_TOKENS.get(side.upper(), {"input": 8000, "output": 500})
    return (est["input"]  / 1_000_000 * HAIKU_INPUT_PER_M +
            est["output"] / 1_000_000 * HAIKU_OUTPUT_PER_M)


def estimate_daily_cost_at_delay(side: str, delay_seconds: int) -> dict:
    """Projected cost if the bot calls the AI every `delay_seconds`.
    Assumes ~continuous operation (crypto 24/7; stock bots only call during
    market hours, so this is a conservative upper bound)."""
    delay = max(1, int(delay_seconds))
    calls_per_day = 86400.0 / delay
    per_call = estimate_call_cost(side)
    return {
        "calls_per_day": calls_per_day,
        "per_call":      per_call,
        "per_day":       calls_per_day * per_call,
        "per_month":     calls_per_day * per_call * 30.0,
    }


# Min-positions floor (LONG bot) — deploy at least N names even if the AI
# is cautious. 0 = fully cautious (original behaviour).
BOT_DEFAULT_POS = {"LONG": 5}


def get_bot_min_positions(side: str = "LONG", broker: str | None = None) -> int:
    """V4.6.91 — PER-BROKER minimum-positions floor. Resolution order:
      1. APEX_MIN_POSITIONS_<SIDE> env — set per (side, broker) by the cloud
         runner, so a cloud bot reads ITS broker's value.
      2. settings[side]['min_positions_<broker>'] (broker defaults to the
         active broker) — the desktop/local path.
      3. legacy settings[side]['min_positions'] (pre-4.6.91, side-only).
      4. the built-in default.
    Previously this was side-only, so the Alpaca and IBKR copies of a bot
    shared one value."""
    default = BOT_DEFAULT_POS.get(side, 5)
    ev = os.environ.get(f"APEX_MIN_POSITIONS_{side.upper()}")
    if ev not in (None, ""):
        try:
            iv = int(ev)
            return iv if 0 <= iv <= 50 else default
        except (TypeError, ValueError):
            pass
    try:
        broker = (broker or current_broker() or "alpaca").lower()
        sd = load_settings().get(side, {})
        v = sd.get(f"min_positions_{broker}")
        if v is None:
            v = sd.get("min_positions")   # legacy side-only fallback
        if v is None:
            return default
        v = int(v)
        return v if 0 <= v <= 50 else default
    except Exception:
        return default


def set_bot_min_positions(side: str, value: int, broker: str | None = None) -> None:
    broker = (broker or current_broker() or "alpaca").lower()
    s = load_settings()
    s.setdefault(side, {})[f"min_positions_{broker}"] = int(value)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# Longbot: minimum local score before the (paid) Claude Vision call.
BOT_DEFAULT_SCORE = {"LONG": 25.0}


def get_bot_min_score(side: str = "LONG") -> float:
    default = BOT_DEFAULT_SCORE.get(side, 25.0)
    try:
        v = float(load_settings().get(side, {}).get("min_score"))
        return v if v >= 0 else default
    except Exception:
        return default


def set_bot_min_score(side: str, value: float) -> None:
    s = load_settings()
    s.setdefault(side, {})["min_score"] = round(float(value), 1)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# Max concurrent bracket positions (DAY bot). 0 = unlimited.
BOT_DEFAULT_BRACKETS = {"DAY": 0}


def get_bot_max_brackets(side: str = "DAY") -> int:
    default = BOT_DEFAULT_BRACKETS.get(side, 0)
    try:
        v = load_settings().get(side, {}).get("max_brackets")
        if v is None:
            return default
        v = int(v)
        return v if v >= 0 else default
    except Exception:
        return default


def set_bot_max_brackets(side: str, value: int) -> None:
    s = load_settings()
    s.setdefault(side, {})["max_brackets"] = max(0, int(value))
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# DAY bot ATR multiples for the bracket stop / take-profit.
# Defaults match daybot.py constants (small = tight intraday trades).
DAY_DEFAULT_STOP_MULT = 0.5
DAY_DEFAULT_TP_MULT   = 1.0


def get_day_atr_mults() -> tuple:
    """(stop_atr_mult, tp_atr_mult) — saved overrides or defaults."""
    s = load_settings().get("DAY", {})
    try:
        sm = float(s.get("stop_atr_mult"))
        sm = sm if 0.05 <= sm <= 10.0 else DAY_DEFAULT_STOP_MULT
    except Exception:
        sm = DAY_DEFAULT_STOP_MULT
    try:
        tm = float(s.get("tp_atr_mult"))
        tm = tm if 0.05 <= tm <= 20.0 else DAY_DEFAULT_TP_MULT
    except Exception:
        tm = DAY_DEFAULT_TP_MULT
    return sm, tm


def set_day_atr_mults(stop_mult: float, tp_mult: float) -> None:
    s = load_settings()
    d = s.setdefault("DAY", {})
    d["stop_atr_mult"] = round(float(stop_mult), 2)
    d["tp_atr_mult"]   = round(float(tp_mult), 2)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# ─────────────────────────────────────────
# API KEYS  (.env editable from the app)
# ─────────────────────────────────────────

ENV_FILE = ROOT / ".env"

ENV_KEYS = [
    # AI providers
    "ANTHROPIC_API_KEY",
    "GOOGLE_AI_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "AI_PROVIDER",
    "AI_MODEL",
    "AI_MODE",
    # Alpaca (built-in bots)
    "ALPACA_API_KEY_LONG",  "ALPACA_SECRET_KEY_LONG",
    "ALPACA_API_KEY_SHORT", "ALPACA_SECRET_KEY_SHORT",
    "ALPACA_API_KEY_DAY",   "ALPACA_SECRET_KEY_DAY",
]


def read_env_keys() -> dict:
    """Read all managed keys from the data-folder .env, including custom-bot
    slot keys (ALPACA_API_KEY_<SLUG> / ALPACA_SECRET_KEY_<SLUG>) and
    per-bot AI config (AI_PROVIDER_<SIDE>, AI_MODEL_<SIDE>, AI_MODE_<SIDE>)."""
    out = {k: "" for k in ENV_KEYS}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if (k in ENV_KEYS
                        or k.startswith("ALPACA_API_KEY_")
                        or k.startswith("ALPACA_SECRET_KEY_")
                        or k.startswith("AI_PROVIDER_")
                        or k.startswith("AI_MODEL_")
                        or k.startswith("AI_MODE_")
                        or k.startswith("APEX_ALPACA_ALLOC_")):
                    out[k] = v
    except Exception:
        pass
    return out


def get_bot_ai_config(side: str) -> dict:
    """Return the per-bot AI config for *side* (LONG/SHORT/DAY/custom).

    Keys: provider, model, mode.
    Falls back to the global AI_PROVIDER / AI_MODEL / AI_MODE .env vars,
    then to sensible defaults if nothing is set."""
    s = side.upper()
    env = read_env_keys()
    provider = env.get(f"AI_PROVIDER_{s}") or env.get("AI_PROVIDER", "anthropic")
    model    = env.get(f"AI_MODEL_{s}")    or env.get("AI_MODEL", "")
    mode     = env.get(f"AI_MODE_{s}")     or env.get("AI_MODE", "vision")
    return {"provider": provider, "model": model, "mode": mode}


def set_bot_ai_config(side: str, provider: str, model: str, mode: str) -> None:
    """Persist per-bot AI config as AI_PROVIDER_<SIDE> etc. in .env.

    These are picked up by read_env_keys() and included in the credentials
    sync so the Oracle bot runner receives them automatically."""
    s = side.upper()
    write_env_keys({
        f"AI_PROVIDER_{s}": provider,
        f"AI_MODEL_{s}":    model,
        f"AI_MODE_{s}":     mode,
    })


def delete_env_keys(names: list[str]) -> None:
    """V4.0.3 — remove the named env vars from .env outright. Used by
    the Tools slot UI when a user reassigns a slot to 'Unassigned' —
    write_env_keys can't do this since it intentionally ignores empty
    values."""
    if not names:
        return
    targets = set(n for n in names if n)
    try:
        existing = open(ENV_FILE, "r", encoding="utf-8").read().splitlines()
    except Exception:
        return
    out = []
    for line in existing:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in targets:
                continue  # drop this line
        out.append(line)
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out).strip() + "\n")
    try:
        from dotenv import load_dotenv as _ld
        _ld(dotenv_path=str(ENV_FILE), override=True)
    except Exception:
        pass


def write_env_keys(values: dict) -> None:
    """
    Merge the given keys into the data-folder .env (other lines preserved),
    then reload them and reconnect the Alpaca clients — no restart needed.
    Empty values are ignored so a blank field never wipes an existing key.
    """
    # V4.0.3 — allow custom-bot slot keys (ALPACA_API_KEY_<SLUG> /
    # ALPACA_SECRET_KEY_<SLUG>) in addition to the static ENV_KEYS
    # allowlist, otherwise saving a slot for a 'crypto' bot would
    # silently drop the new key.
    def _allow(k: str) -> bool:
        if k in ENV_KEYS:
            return True
        return (k.startswith("ALPACA_API_KEY_")
                or k.startswith("ALPACA_SECRET_KEY_")
                or k.startswith("AI_PROVIDER_")
                or k.startswith("AI_MODEL_")
                or k.startswith("AI_MODE_")
                or k.startswith("APEX_ALPACA_ALLOC_"))
    vals = {k: str(v).strip() for k, v in values.items()
            if _allow(k) and str(v).strip()}
    if not vals:
        return
    try:
        existing = open(ENV_FILE, "r", encoding="utf-8").read().splitlines()
    except Exception:
        existing = []
    out, seen = [], set()
    for line in existing:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in vals:
                out.append(f"{k}={vals[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in vals.items():
        if k not in seen:
            out.append(f"{k}={v}")
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out).strip() + "\n")
    try:
        from dotenv import load_dotenv as _ld
        _ld(ENV_FILE, override=True)
    except Exception:
        pass
    reset_clients()


# ─────────────────────────────────────────
# PER-BOT LIFETIME SNAPSHOTS  (v4.6.22)
# ─────────────────────────────────────────
# Account-level history shows the Alpaca account's full timeline,
# which is misleading when multiple bots have used the same account
# over time (e.g. shortbot before crypto). Each active bot also writes
# its own JSONL snapshot file so the Overview can scope P/L to ONLY
# the period that bot was in charge.
#
# File path:  DATA_DIR/<side>_lifetime.jsonl
# Append-only. Each line is a JSON object:
#   {"ts": ISO8601, "equity": float, "portfolio_value": float,
#    "positions_count": int}
# Throttled to one snapshot per ~5 minutes per bot to keep the file
# tiny (a year of 5-min ticks at 200 bytes = ~21 MB max).

def _bot_snapshot_path(side: str):
    # V4.6.32 — per-broker lifetime file. Alpaca keeps its historic flat
    # path (no migration); IBKR / others write to ROOT/<broker>/.
    if current_broker() == "alpaca":
        return ROOT / f"{side.lower()}_lifetime.jsonl"
    return broker_data_dir() / f"{side.lower()}_lifetime.jsonl"


def delete_bot_snapshots(side: str) -> None:
    """V4.6.90 — wipe a bot's lifetime snapshot log (both broker paths). Called
    when a bot is removed/re-allocated so its LIFETIME / period P&L baseline
    starts fresh at the new allocation — otherwise the equity jump from a
    re-allocation is mis-counted as 'profit' against the old, lower baseline."""
    paths = {ROOT / f"{side.lower()}_lifetime.jsonl"}
    try:
        paths.add(broker_data_dir() / f"{side.lower()}_lifetime.jsonl")
    except Exception:
        pass
    # Also cover the other broker's dir so a re-add on either broker is clean.
    try:
        for sub in ("ibkr", "alpaca"):
            paths.add(ROOT / sub / f"{side.lower()}_lifetime.jsonl")
    except Exception:
        pass
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def append_bot_snapshot(side: str, *, equity: float = 0.0,
                        portfolio_value: float = 0.0,
                        positions_count: int = 0,
                        allocated: float = 0.0) -> None:
    """Append one snapshot to the bot's lifetime log. Skips writing
    if the most recent entry is less than 5 minutes old (so callers
    can refresh aggressively without ballooning the file)."""
    import json as _j
    from datetime import datetime as _dt, timezone as _tz
    p = _bot_snapshot_path(side)
    now = _dt.now(_tz.utc)
    # Throttle
    try:
        if p.exists() and p.stat().st_size > 0:
            # Read last line cheaply
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 400))
                tail = f.read().decode("utf-8", errors="replace").splitlines()
            if tail:
                last = _j.loads(tail[-1])
                last_ts = _dt.fromisoformat(last["ts"])
                if (now - last_ts).total_seconds() < 300:
                    return  # < 5 min — skip
    except Exception:
        pass
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(_j.dumps({
                "ts":              now.isoformat(timespec="seconds"),
                "equity":          float(equity or 0),
                "portfolio_value": float(portfolio_value or 0),
                "positions_count": int(positions_count or 0),
                # V4.6.120 — capital allocated to the bot at this instant, so a
                # later re-allocation can be detected and excluded from P/L.
                "allocated":       float(allocated or 0),
            }) + "\n")
    except Exception as e:
        print(f"[snapshot] {side}: append failed: {e}")


def read_bot_snapshots(side: str, limit: int = 5000) -> list[dict]:
    """Read all snapshots for this bot. Returns oldest-first. Used by
    Overview to compute bot-lifetime P/L when account history is too
    coarse / pre-dates the bot."""
    import json as _j
    p = _bot_snapshot_path(side)
    if not p.exists():
        return []
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f.readlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(_j.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"[snapshot] {side}: read failed: {e}")
    return out


# ─────────────────────────────────────────
# UNIVERSE BREAKDOWN  (what the universe manager picked, per bot)
# ─────────────────────────────────────────

UNIVERSE_FILES = {
    "LONG":  ROOT / "longbot_universe.txt",
    "SHORT": ROOT / "shortbot_universe.txt",
    "DAY":   ROOT / "daybot_universe.txt",
}

# V4.6.2 — file-name convention for custom-bot universes. A bot with
# slug "crypto" gets its tickers from "crypto_universe.txt" (and the
# universe tab will display it automatically alongside the built-ins).
def universe_path_for(side: str) -> Path:
    """Canonical universe-file path for a built-in OR custom bot side."""
    s = side.upper()
    if s in UNIVERSE_FILES:
        return UNIVERSE_FILES[s]
    # Custom bot — lowercase slug + _universe.txt convention
    return ROOT / f"{side.lower()}_universe.txt"


def discover_universe_files() -> dict:
    """V4.6.2 — return {SIDE: Path} for every universe file we know
    about: the three built-ins + every registered custom bot + any
    *_universe.txt found in ROOT (covers manual additions). Used by
    the Universe tab so a new crypto bot appears immediately."""
    found: dict[str, Path] = dict(UNIVERSE_FILES)
    # Custom bots from the per-broker registry
    known_sides = {s.upper() for s in UNIVERSE_FILES}
    try:
        reg = load_bot_registry()
        for c in reg.get("custom", []):
            slug = str(c.get("id", "")).strip()
            if not slug:
                continue
            side = slug.upper()
            known_sides.add(side)
            if side not in found:
                found[side] = universe_path_for(side)
    except Exception:
        pass
    # Any free-floating *_universe.txt left on disk — but ONLY for sides that
    # belong to a real built-in or registered bot. V4.6.77: this stops stale
    # leftover files (e.g. crypto_universe.txt from a deleted crypto bot) from
    # surfacing a phantom universe card with no bot behind it.
    try:
        for p in ROOT.glob("*_universe.txt"):
            stem = p.stem
            if stem.endswith("_universe"):
                stem = stem[:-len("_universe")]
            # Built-in stems → canonical side
            stem_map = {"longbot": "LONG", "shortbot": "SHORT",
                        "daybot":  "DAY"}
            side = stem_map.get(stem, stem.upper())
            if side not in found and side in known_sides:
                found[side] = p
    except Exception:
        pass
    return found


UNIVERSE_SCRIPT = ROOT / "universe_manager.py"


def read_universe_breakdown() -> dict:
    """
    Parse every discovered universe file into:
      {"LONG":[{"Ticker","Note"}...], "SHORT":[...], "DAY":[...],
       "<CUSTOM>": [...], "counts":{"LONG":n,...}, "sides":["LONG",...]}
    Comment/header lines (starting with #) are skipped; an inline
    '# note' after a ticker is captured as the reason it was chosen.
    V4.6.2: now covers custom bot universes (e.g. crypto_universe.txt).
    """
    out: dict = {"counts": {}, "sides": []}
    files = discover_universe_files()
    # Stable ordering: built-ins first (LONG, SHORT, DAY), then custom alpha.
    builtin_order = ["LONG", "SHORT", "DAY"]
    ordered_sides = [s for s in builtin_order if s in files] + sorted(
        [s for s in files if s not in builtin_order])
    for side in ordered_sides:
        path = files[side]
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "#" in line:
                        tk, note = line.split("#", 1)
                        ticker = tk.strip().split()[0].upper()
                        note   = note.strip()
                    else:
                        ticker = line.split()[0].upper()
                        note   = ""
                    rows.append({"Bot": side, "Ticker": ticker, "Note": note})
        except Exception:
            pass
        out[side] = rows
        out["counts"][side] = len(rows)
        out["sides"].append(side)
    return out


def close_position(side: str, ticker: str) -> str:
    c = get_client(side)
    if not c:
        return f"No client for {side}"
    try:
        c.close_position(ticker)
        return f"✓ Closed {ticker}"
    except Exception as e:
        return f"✗ {e}"


# ─────────────────────────────────────────
# LOCAL FILE DATA
# ─────────────────────────────────────────

def load_snapshots(side: str) -> pd.DataFrame:
    """Time series of portfolio_value samples used by the RISK METRICS
    & STATS panel on each bot tab.

    V7.1.12 — derive from the bot's trade log when no dedicated
    snapshots file exists. The bots write portfolio_before / _after
    into every trade-log entry, so we already have a sample every
    time the bot ran. The old behaviour of reading a separate
    *_snapshots.jsonl file is kept as the preferred source so a
    future bot version that writes snapshots explicitly still works.
    """
    # Primary source: dedicated snapshots file (if it exists)
    rows = load_jsonl(snapshot_files_for(side))
    if rows:
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        return (df.dropna(subset=["time"])
                  .sort_values("time")
                  .reset_index(drop=True))

    # Fallback: synthesize from the trade log. Each entry carries the
    # portfolio_value before AND after the run; we pick "after" since
    # that's the latest state the bot saw.
    log_rows = load_jsonl(log_files_for(side))
    if not log_rows:
        return pd.DataFrame()

    samples = []
    for r in log_rows:
        t = r.get("time")
        if not t:
            continue
        # LONG / SHORT use portfolio_before / portfolio_after.portfolio_value;
        # DAY uses portfolio.value (different schema, single dict).
        pa = r.get("portfolio_after")  or {}
        pb = r.get("portfolio_before") or {}
        pday = r.get("portfolio")      or {}
        pv = (pa.get("portfolio_value")
              or pb.get("portfolio_value")
              or pday.get("value")
              or pday.get("portfolio_value"))
        if pv is None:
            continue
        try:
            samples.append({"time": t, "portfolio_value": float(pv)})
        except (TypeError, ValueError):
            continue
    if not samples:
        return pd.DataFrame()

    df = pd.DataFrame(samples)
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    # Deduplicate runs at the same timestamp (bots sometimes log twice
    # in the same second). Keep the last.
    df = df.drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
    return df


def _ibkr_cloud_log(side: str) -> pd.DataFrame:
    """V4.6.63 — for a cloud IBKR bot, fetch its server log and extract recent
    AI calls (decision / confidence / analysis / action) so the LAST AI SIGNAL
    card populates. Best-effort text parse; returns empty on any failure."""
    try:
        s = load_settings()
        mode = s.get("alpaca_mode", "paper")
        cfg = s.get(f"ibkr_{mode}", s.get("ibkr", {})) or {}
        if not cfg.get("run_on_oracle"):
            return pd.DataFrame()
        from core import ibkr_data as _ix
        tok, url = _ix._cloud_creds()
        if not tok:
            return pd.DataFrame()
        import requests
        r = requests.get(f"{url}/bots/{side}/logs",
                         params={"tail": 4000, "broker": "ibkr"},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=8)
        text = r.json().get("log", "") if r.ok else ""
        if not text:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()
    import re as _re
    from datetime import datetime as _dt, timezone as _tz
    rows = []
    cur = {"decision": "", "confidence": None, "analysis": "", "action": ""}
    for line in text.splitlines():
        try:
            m = _re.search(r'"confidence"\s*:\s*([0-9.]+)', line)
            if m:
                cur["confidence"] = float(m.group(1))
            m = _re.search(r'"(?:reason|short_analysis)"\s*:\s*"([^"]+)"', line)
            if m:
                cur["analysis"] = m.group(1)
            m = _re.search(r'Decision:\s*([A-Za-z ]+)', line)
            if m:
                cur["decision"] = m.group(1).strip()
            mc = _re.search(r'Confidence:\s*([0-9.]+)%', line)
            if mc:
                cur["confidence"] = float(mc.group(1)) / 100.0
            if "ACTION:" in line or line.strip().startswith("ACTION"):
                cur["action"] = line.split("ACTION:", 1)[-1].strip()[:160]
                rows.append(dict(cur))
        except Exception:
            continue
    if not rows and (cur["decision"] or cur["analysis"] or cur["confidence"]):
        rows.append(dict(cur))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = _dt.now(_tz.utc)
    return df


def load_bot_log(side: str) -> pd.DataFrame:
    if load_settings().get("broker_mode", "alpaca") == "ibkr":
        return _ibkr_cloud_log(side)
    rows = load_jsonl(log_files_for(side))
    if not rows:
        return pd.DataFrame()
    parsed = []
    for r in rows:
        sig = r.get("signal", {})
        if not isinstance(sig, dict):
            try: sig = json.loads(sig)
            except: sig = {}
        pb = r.get("portfolio_before", {})
        pa = r.get("portfolio_after",  {})
        parsed.append({
            "time":       r.get("time",""),
            "decision":   sig.get("decision",""),
            "confidence": sig.get("confidence"),
            "analysis":   sig.get("short_analysis", r.get("reason","")),
            "action":     str(r.get("action","")),
            "pv_before":  pb.get("portfolio_value") if isinstance(pb,dict) else None,
            "pv_after":   pa.get("portfolio_value") if isinstance(pa,dict) else
                          (r.get("portfolio",{}) or {}).get("value"),
            "change":     r.get("change"),
        })
    df = pd.DataFrame(parsed)
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    return df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def load_day_state() -> dict:
    p = day_state_path()
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except:
        return {}


# ─────────────────────────────────────────
# API COST TRACKING
# ─────────────────────────────────────────

def estimate_api_costs(side: str) -> dict:
    """
    Estimate Claude API costs from bot log.
    Each run that called Claude = one API call.
    We know roughly how many tokens each bot uses per call.
    """
    log = load_bot_log(side)
    if log.empty:
        return {"total": 0.0, "calls": 0, "per_day": 0.0, "model": "haiku"}

    # Count actual Claude calls. LONG/SHORT signals carry a "decision";
    # DAY's signal has no decision (ticker/confidence/reason) so also
    # count rows that have a confidence => Claude was actually called.
    has_decision  = log["decision"].notna() & (log["decision"] != "")
    has_conf      = log["confidence"].notna() if "confidence" in log else False
    calls = log[has_decision | has_conf].shape[0]

    # Approximate tokens per call based on bot type
    token_estimates = {
        "LONG":  {"input": 12000, "output": 600},   # 8 charts + table
        "SHORT": {"input": 10000, "output": 600},
        "DAY":   {"input":  6000, "output": 400},   # 3 charts + compact
    }
    est = token_estimates.get(side, {"input": 8000, "output": 500})

    input_cost  = calls * est["input"]  / 1_000_000 * HAIKU_INPUT_PER_M
    output_cost = calls * est["output"] / 1_000_000 * HAIKU_OUTPUT_PER_M
    total       = round(input_cost + output_cost, 4)

    # Per day estimate
    if len(log) > 0 and log["time"].notna().any():
        days = max(1, (log["time"].max() - log["time"].min()).days + 1)
        per_day = round(total / days, 4)
    else:
        per_day = 0.0

    return {
        "total":   total,
        "calls":   calls,
        "per_day": per_day,
        "model":   "haiku",
    }


def estimate_total_costs() -> dict:
    """Total API costs across all bots."""
    costs = {s: estimate_api_costs(s) for s in ["LONG","SHORT","DAY"]}
    total = round(sum(c["total"] for c in costs.values()), 4)
    per_day = round(sum(c["per_day"] for c in costs.values()), 4)
    per_month = round(per_day * 30, 2)
    per_year  = round(per_day * 365, 2)

    # Universe manager (once/week, 3 calls @ $0.003 each)
    universe_weekly = 0.009
    universe_yearly = round(universe_weekly * 52, 2)

    return {
        "by_bot":        costs,
        "total":         total,
        "per_day":       per_day,
        "per_month":     per_month,
        "per_year":      per_year,
        "universe_year": universe_yearly,
        "grand_year":    round(per_year + universe_yearly, 2),
    }


# ─────────────────────────────────────────
# RISK METRICS
# ─────────────────────────────────────────

def risk_metrics(equity_series: pd.Series) -> dict:
    v = equity_series.dropna().values
    if len(v) < 2:
        return {}
    r   = np.diff(v) / v[:-1]
    tot = (v[-1]/v[0]-1)*100 if v[0]>0 else 0
    sh  = (r.mean()/r.std()*math.sqrt(252)) if r.std()>0 else 0
    pk  = np.maximum.accumulate(v)
    dd  = float(((v-pk)/pk*100).min())
    wr  = float((r>0).mean()*100)
    vol = float(r.std()*math.sqrt(252)*100)
    return {
        "total_return": round(tot,2),
        "sharpe":       round(sh,3),
        "max_dd":       round(dd,2),
        "win_rate":     round(wr,1),
        "volatility":   round(vol,2),
    }


def realized_pl(orders_df: pd.DataFrame, ticker: str) -> float:
    if orders_df.empty:
        return 0.0
    t = orders_df[(orders_df["Ticker"]==ticker)
                  &orders_df["Filled"].notna()].sort_values("Filled")
    real, avg, held = 0.0, 0.0, 0.0
    for _, o in t.iterrows():
        q, p = float(o["Qty"]), float(o["Avg Fill"])
        if q<=0 or p<=0: continue
        if o["Side"]=="BUY":
            avg = (avg*held+p*q)/(held+q); held+=q
        elif o["Side"]=="SELL":
            real += (p-avg)*q; held=max(0,held-q)
    return real


# ─────────────────────────────────────────
# BROKER EXPORT
# ─────────────────────────────────────────

def export_ibkr_csv(side: str) -> str:
    positions = get_positions(side)
    if not positions:
        return ""
    rows = ["Symbol,Action,Quantity,OrderType,TimeInForce"]
    for p in positions:
        qty    = abs(float(p["qty"]))
        action = "SELL" if float(p["qty"]) < 0 else "BUY"
        rows.append(f"{p['symbol']},{action},{qty:.6f},MKT,DAY")
    return "\n".join(rows)


def export_ibkr_script(side: str) -> str:
    positions = get_positions(side)
    if not positions:
        return ""
    lines = [
        "# IBKR position replication — generated by APEX",
        "from ib_insync import IB, Stock, MarketOrder",
        "ib = IB()",
        "ib.connect('127.0.0.1', 7497, clientId=1)",
        "",
    ]
    for p in positions:
        qty    = abs(float(p["qty"]))
        action = "SELL" if float(p["qty"]) < 0 else "BUY"
        lines += [
            f"# {p['symbol']} — ${float(p['market_value']):.2f}",
            f"ib.placeOrder(Stock('{p['symbol']}','SMART','USD'), MarketOrder('{action}',{qty:.6f}))",
            "",
        ]
    lines.append("ib.disconnect()")
    return "\n".join(lines)

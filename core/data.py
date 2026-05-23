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

from core.paths import DATA_DIR as ROOT  # project root (source) or %LocalAppData% (frozen)

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

DAY_STATE = ROOT / "daybot_state.json"

# Anthropic pricing (per million tokens) — update if pricing changes
HAIKU_INPUT_PER_M  = 0.80
HAIKU_OUTPUT_PER_M = 4.00
SONNET_INPUT_PER_M = 3.00
SONNET_OUTPUT_PER_M= 15.00

# Alpaca period map
ALPACA_PERIOD = {
    "1D": ("1D","5Min"), "1W": ("1W","1H"),
    "1M": ("1M","1D"),   "3M": ("3M","1D"),
    "6M": ("6M","1D"),   "1Y": ("1A","1D"),
}


# ─────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────

_clients = {}

def get_client(side: str):
    if not HAS_ALPACA:
        return None
    if side in _clients:
        return _clients[side]
    key_map = {
        "LONG":  ("ALPACA_API_KEY_LONG",  "ALPACA_SECRET_KEY_LONG"),
        "SHORT": ("ALPACA_API_KEY_SHORT", "ALPACA_SECRET_KEY_SHORT"),
        "DAY":   ("ALPACA_API_KEY_DAY",   "ALPACA_SECRET_KEY_DAY"),
    }
    fallback_key    = os.getenv("ALPACA_API_KEY", "")
    fallback_secret = os.getenv("ALPACA_SECRET_KEY", "")
    k, s = key_map.get(side, ("",""))
    api_key    = os.getenv(k, fallback_key)
    api_secret = os.getenv(s, fallback_secret)
    if not api_key or not api_secret:
        return None
    try:
        client = TradingClient(api_key, api_secret, paper=True)
        _clients[side] = client
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
        hist = yf.Ticker(symbol).history(period="30d", auto_adjust=True)
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
    for p in positions:
        sym = p.get("symbol")
        entry = float(p.get("avg_entry_price", 0))
        if not sym or entry <= 0:
            continue
        atr_pct = _ticker_atr_pct(sym)
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


def get_orders(side: str) -> pd.DataFrame:
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
    """
    Live portfolio history for one bot's Alpaca account.

    V7.1.14: chart no longer shrinks to 13:30-20:00 UTC (US market
    hours). Two changes:
      1. Use intraday_reporting=CONTINUOUS exclusively when available
         — it asks Alpaca for 24/7 equity samples. extended_hours
         is only used as a fallback for older SDKs that don't have
         the enum.
      2. NaN equity samples outside market hours (when Alpaca has
         no fresh tick because no trades happened) are now
         forward-filled with the last known equity instead of being
         dropped. The plot now shows a flat line overnight rather
         than auto-shrinking the X-axis to the market session.
    """
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


def get_combined_history(period: str) -> pd.DataFrame:
    """
    Total portfolio value over time = LONG + SHORT + DAY equity, aligned on
    Alpaca's portfolio-history timestamps. Works 24/7 (Alpaca returns the
    equity series whether or not the market is open), independent of whether
    any bot is running.

    v3.1.2 — also sums profit_loss across all sides so the overview chart
    can show deposit-adjusted performance.
    """
    frames = []
    for side in ("LONG", "SHORT", "DAY"):
        df = get_history(side, period)
        if df is not None and not df.empty:
            frames.append(df.rename(columns={
                "equity":      f"eq_{side}",
                "profit_loss": f"pl_{side}",
            }).set_index("time"))
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, axis=1).sort_index().ffill().fillna(0.0)
    eq_cols = [c for c in merged.columns if c.startswith("eq_")]
    pl_cols = [c for c in merged.columns if c.startswith("pl_")]
    return pd.DataFrame({
        "time":        merged.index,
        "equity":      merged[eq_cols].sum(axis=1).values if eq_cols else 0,
        "profit_loss": merged[pl_cols].sum(axis=1).values if pl_cols else 0,
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


# ── V7.1.10: per-bot auto-schedule ──────────────────────────────────

def get_auto_schedule_for(side: str) -> bool:
    """Per-bot auto-schedule. Falls back to the legacy global flag for
    users upgrading from before V7.1.10."""
    s = load_settings()
    per_bot = s.get("auto_schedule_bots", None)
    if per_bot is not None and side in per_bot:
        return bool(per_bot[side])
    # No per-bot entry yet → use the legacy global as a default so
    # existing users don't suddenly lose their schedule.
    return bool(s.get("auto_schedule", False))


def set_auto_schedule_for(side: str, on: bool) -> None:
    s = load_settings()
    per_bot = dict(s.get("auto_schedule_bots") or {})
    per_bot[side] = bool(on)
    s["auto_schedule_bots"] = per_bot
    # Drop the legacy global once any per-bot entry exists, otherwise
    # newly-added bots would silently inherit the old global.
    s.pop("auto_schedule", None)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def get_auto_schedule_active_bots() -> list[str]:
    """Set of bot sides currently flagged for auto-start at open."""
    s = load_settings()
    per_bot = s.get("auto_schedule_bots", None)
    if per_bot is not None:
        return [side for side, on in per_bot.items() if on]
    # Legacy migration — if the global flag was on, treat every active
    # bot from the registry as scheduled.
    if s.get("auto_schedule"):
        reg = s.get("bot_registry", {})
        return list(reg.get("active", ["LONG", "SHORT", "DAY"]))
    return []


# ── V7.1.13: per-bot cloud-execution toggle ─────────────────────────

def get_cloud_bots() -> list[str]:
    """Sides set to run on the APEX Oracle server instead of locally."""
    return list(load_settings().get("cloud_bots", []))


def set_cloud_bots(sides: list[str]) -> None:
    s = load_settings()
    s["cloud_bots"] = sorted({str(x).upper() for x in sides})
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


# Min-positions floor (LONG bot) — deploy at least N names even if the AI
# is cautious. 0 = fully cautious (original behaviour).
BOT_DEFAULT_POS = {"LONG": 5}


def get_bot_min_positions(side: str = "LONG") -> int:
    default = BOT_DEFAULT_POS.get(side, 5)
    try:
        v = load_settings().get(side, {}).get("min_positions")
        if v is None:
            return default
        v = int(v)
        return v if 0 <= v <= 50 else default
    except Exception:
        return default


def set_bot_min_positions(side: str, value: int) -> None:
    s = load_settings()
    s.setdefault(side, {})["min_positions"] = int(value)
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
    "ANTHROPIC_API_KEY",
    "ALPACA_API_KEY_LONG",  "ALPACA_SECRET_KEY_LONG",
    "ALPACA_API_KEY_SHORT", "ALPACA_SECRET_KEY_SHORT",
    "ALPACA_API_KEY_DAY",   "ALPACA_SECRET_KEY_DAY",
]


def read_env_keys() -> dict:
    """Current values of the managed keys from the data-folder .env."""
    out = {k: "" for k in ENV_KEYS}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in out:
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


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
                or k.startswith("ALPACA_SECRET_KEY_"))
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
# UNIVERSE BREAKDOWN  (what the universe manager picked, per bot)
# ─────────────────────────────────────────

UNIVERSE_FILES = {
    "LONG":  ROOT / "longbot_universe.txt",
    "SHORT": ROOT / "shortbot_universe.txt",
    "DAY":   ROOT / "daybot_universe.txt",
}

UNIVERSE_SCRIPT = ROOT / "universe_manager.py"


def read_universe_breakdown() -> dict:
    """
    Parse the three universe files into:
      {"LONG":[{"Ticker","Note"}...], "SHORT":[...], "DAY":[...],
       "counts":{"LONG":n,...}}
    Comment/header lines (starting with #) are skipped; an inline
    '# note' after a ticker is captured as the reason it was chosen.
    """
    out = {"counts": {}}
    for side, path in UNIVERSE_FILES.items():
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
    rows = load_jsonl(SNAPSHOT_FILES.get(side, []))
    if rows:
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        return (df.dropna(subset=["time"])
                  .sort_values("time")
                  .reset_index(drop=True))

    # Fallback: synthesize from the trade log. Each entry carries the
    # portfolio_value before AND after the run; we pick "after" since
    # that's the latest state the bot saw.
    log_rows = load_jsonl(LOG_FILES.get(side, []))
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


def load_bot_log(side: str) -> pd.DataFrame:
    rows = load_jsonl(LOG_FILES.get(side, []))
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
    if not DAY_STATE.exists():
        return {}
    try:
        with open(DAY_STATE) as f:
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

# -*- coding: utf-8 -*-
"""
APEX DAY BOT  -  Bracket Order Strategy
-----------------------------------------------------------------
Philosophy:
  Find ONE high-conviction setup per run. Place a bracket order
  (entry + stop loss + take profit) and let Alpaca handle the exit
  completely automatically. The bot never manually closes positions.

  This is the purest form of rule-based trading:
    * Enter only when multiple signals align
    * Risk is fixed the moment you enter
    * Emotion is zero  -  the bracket executes itself
    * Bot can be offline after placing the order

Architecture:
  Layer 1  -  Scan & Score  (free, yfinance + numpy)
    * Same indicators as longbot_v2 (RSI, MACD, ATR, patterns)
    * Scores every ticker in universe
    * Hard filters: no earnings within 3 days, min ATR, min volume
    * Pre-filters to top 5 candidates

  Layer 2  -  Claude Vision  (Haiku, 1 call per run MAX)
    * Only called when score > MIN_SCORE_FOR_CLAUDE
    * Sends top 3 charts + compact table
    * Returns single best ticker + entry rationale
    * If confidence < threshold -> no trade this run

  Layer 3  -  Bracket Execution  (Alpaca)
    * Market entry order
    * Stop loss  = entry - ATR x STOP_ATR_MULT   (risk)
    * Take profit = entry + ATR x TP_ATR_MULT    (reward)
    * Default ratio: 1:2 risk/reward
    * Max 1 open bracket trade at a time
    * Position size = fixed % of portfolio per trade

Cost per run: ~$0.001-0.008
  -> Only calls Claude when a strong setup exists
  -> Most runs cost $0 (scan only, no API call)

Files:
  daybot_universe.txt    <- editable ticker list
  daybot_state.json
  daybot_trade_log.jsonl
  daybot_analysis.txt
  daybot_charts/
-----------------------------------------------------------------
"""

print("Booting APEX DAY bot - loading libraries, first start can take 1-2 min...", flush=True)

import os
import io
import json
import math
import time
import base64
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from dotenv import load_dotenv
from core.ai_client import call_ai_vision, call_ai_text, load_ai_config

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, GetOrdersRequest,
    TakeProfitRequest, StopLossRequest
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderClass, QueryOrderStatus
)

warnings.filterwarnings("ignore")
_apex_data_dir = os.environ.get("APEX_DATA_DIR") or str(
    __import__("pathlib").Path(
        os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or
        str(__import__("pathlib").Path.home())
    ) / "APEX Trading Platform"
)
load_dotenv(__import__("pathlib").Path(_apex_data_dir) / ".env")
load_dotenv()


# =========================================================
# CONFIG
# =========================================================

# -- Universe file -----------------------------------------
# Edit daybot_universe.txt to add/remove tickers at any time.
# One ticker per line. Lines starting with # are ignored.
UNIVERSE_FILE = os.environ.get(
    "APEX_BOT_UNIVERSE", "daybot_universe.txt")
# V4.6.5 — APEX_BOT_UNIVERSE env var lets the bot tab override the path.

DEFAULT_UNIVERSE = """# APEX DAY BOT  -  Universe
# Edit freely. One ticker per line. # = comment.
# Tip: keep this to 30-60 tickers for best performance.

# High-vol tech (best for intraday brackets)
NVDA
AMD
TSLA
META
MSFT
GOOGL
AAPL
AMZN

# Semiconductors
QCOM
MU
INTC
ARM
MRVL
AVGO

# AI / Cloud
PLTR
CRWD
DDOG
PANW
NET
SNOW
CRM

# High-beta growth
MSTR
COIN
HOOD
SOFI
RKLB
ASTS
IONQ

# Finance
GS
JPM
MS
BLK

# ETFs (good for regime plays)
QQQ
SPY
SMH
ARKK
XBI
"""

# -- Risk / reward -----------------------------------------
# Stop/Take-profit are ATR multiples. daybot targets high-ATR names
# (>=1.5%/day), so big multiples => double-digit % targets that won't
# fill intraday (the old 1.5/3.0 gave ~5%/11% on volatile names).
# Small multiples => tight, fast-filling day trades. App-adjustable.
STOP_ATR_MULT    = 0.5   # default stop = entry - ATR * 0.5
TP_ATR_MULT      = 1.0   # default tgt  = entry + ATR * 1.0  (1:2 R/R)


def live_stop_atr_mult() -> float:
    """App-adjustable stop multiple (apex_settings.json, read each cycle)."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = float(json.load(_f).get("DAY", {}).get("stop_atr_mult"))
        if 0.05 <= _v <= 10.0:
            return _v
    except Exception:
        pass
    return STOP_ATR_MULT


def live_tp_atr_mult() -> float:
    """App-adjustable take-profit multiple (apex_settings.json)."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = float(json.load(_f).get("DAY", {}).get("tp_atr_mult"))
        if 0.05 <= _v <= 20.0:
            return _v
    except Exception:
        pass
    return TP_ATR_MULT
# Math: you only need to win 34% of trades to be profitable

# -- Position sizing ---------------------------------------
# Risk per trade as % of portfolio  -  THIS is the key lever.
# 1% risk means: if stop loss hits, you lose 1% of portfolio.
# Position size is calculated automatically from this.
RISK_PER_TRADE_PCT = 0.01      # risk 1% of portfolio per trade
MAX_POSITION_PCT   = 0.15      # never put more than 15% in one trade
MIN_POSITION_VALUE = 50.0      # minimum trade size in dollars

# -- Entry filters -----------------------------------------
MIN_SCORE_FOR_CLAUDE  = 40.0   # don't call Claude if best score < this
MIN_CONFIDENCE        = 0.72   # Claude must be this confident to trade


def live_min_confidence() -> float:
    """App-adjustable confidence threshold. Read from apex_settings.json
    every decision cycle (no restart). Falls back to MIN_CONFIDENCE."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = float(json.load(_f).get("DAY", {}).get("min_confidence"))
        if 0.0 < _v <= 1.0:
            return _v
    except Exception:
        pass
    return MIN_CONFIDENCE
MIN_ATR_PCT           = 0.015  # stock must move at least 1.5%/day (ATR/price)
MIN_VOLUME            = 500_000 # minimum avg daily volume
MAX_OPEN_BRACKETS     = 1      # legacy default (see live_max_brackets)


def live_max_brackets() -> int:
    """App-adjustable cap on concurrent bracket positions. 0 = unlimited
    (each bracket has its own stop-loss + take-profit, so the bot can run
    many at once). Read from apex_settings.json every cycle (no restart)."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = json.load(_f).get("DAY", {}).get("max_brackets")
        if _v is not None:
            _v = int(_v)
            if _v >= 0:
                return _v
    except Exception:
        pass
    return 0   # default: unlimited
EARNINGS_BLACKOUT     = 3      # skip if earnings within 3 days

# -- Timing -----------------------------------------------
# Run every 20 minutes during market hours.
# Most runs will cost $0 (scan only, no Claude call).
SLEEP_SECONDS          = 1200   # 20 min between runs
OPENING_SKIP_MINUTES   = 15     # skip first 15 min (spreads are wide at open)
CLOSING_SKIP_MINUTES   = 15     # skip last 15 min before close
OPENING_BURST_SECONDS  = 300    # 5 min between runs during burst window
OPENING_BURST_DURATION = 30 * 60

# -- AI --------------------------------------------------
TOP_N_SCAN    = 5      # score all, send top 5 to scan filter
TOP_N_CLAUDE  = 3      # send top 3 charts to AI
# AI_PROVIDER / AI_MODEL loaded from .env via load_ai_config()
MAX_TOKENS    = 500    # small  -  just needs one ticker + confidence

# -- Files ------------------------------------------------
STATE_FILE    = "daybot_state.json"
LOG_FILE      = "daybot_trade_log.jsonl"
ANALYSIS_FILE = "daybot_analysis.txt"
CHART_DIR     = "daybot_charts"


# =========================================================
# SETUP
# =========================================================

os.makedirs(CHART_DIR, exist_ok=True)

_AI_PROVIDER, _AI_MODEL, _AI_KEY, _AI_MODE = load_ai_config()
print(f"[ai] provider={_AI_PROVIDER}  model={_AI_MODEL}  mode={_AI_MODE}")

_ALPACA_IS_PAPER = (os.environ.get("APEX_ALPACA_MODE", "paper").lower() != "live")
trading_client   = TradingClient(
    os.getenv("ALPACA_API_KEY_DAY",   os.getenv("ALPACA_API_KEY")),
    os.getenv("ALPACA_SECRET_KEY_DAY", os.getenv("ALPACA_SECRET_KEY")),
    paper=_ALPACA_IS_PAPER
)


# =========================================================
# UNIVERSE  -  txt file based
# =========================================================

def ensure_universe_file():
    """Create default universe file if it doesn't exist."""
    if not os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_UNIVERSE)
        print(f"Created {UNIVERSE_FILE} with default universe.")


def load_universe() -> list:
    ensure_universe_file()
    tickers = []
    with open(UNIVERSE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ticker = line.upper().split()[0]  # handle trailing comments
            tickers.append(ticker)
    return list(dict.fromkeys(tickers))  # deduplicate, preserve order


def save_universe(tickers: list, notes: dict = None):
    """
    Write tickers back to universe file.
    notes = {ticker: "reason added/kept"} for documentation.
    """
    lines = ["# APEX DAY BOT  -  Universe\n",
             f"# Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
             "# One ticker per line. # = comment.\n\n"]
    for t in tickers:
        note = notes.get(t, "") if notes else ""
        lines.append(f"{t:<8}  # {note}\n" if note else f"{t}\n")
    with open(UNIVERSE_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


# =========================================================
# STATE
# =========================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "open_brackets":    {},   # ticker -> {entry, stop, tp, qty, time}
            "trades_today":     0,
            "last_trade_date":  None,
            "wins":             0,
            "losses":           0,
            "total_pnl":        0.0,
            "last_run_skipped": False,
        }
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def reset_daily(state: dict) -> dict:
    today = datetime.now().date().isoformat()
    if state.get("last_trade_date") != today:
        state["last_trade_date"] = today
        state["trades_today"]    = 0
    return state


# =========================================================
# MARKET HOURS
# =========================================================

def get_clock():
    return trading_client.get_clock()


def is_good_trading_time() -> tuple:
    """
    Returns (can_trade: bool, reason: str).
    Avoids open/close when spreads are wide.
    """
    clock = trading_client.get_clock()
    if not clock.is_open:
        return False, "market closed"

    now       = clock.timestamp
    mkt_open  = clock.next_close - timedelta(hours=6, minutes=30)
    mkt_close = clock.next_close
    mins_since_open  = (now - mkt_open).total_seconds()  / 60
    mins_until_close = (mkt_close - now).total_seconds() / 60

    if mins_since_open < OPENING_SKIP_MINUTES:
        return False, f"too early  -  {OPENING_SKIP_MINUTES - mins_since_open:.0f}min until trading window"
    if mins_until_close < CLOSING_SKIP_MINUTES:
        return False, f"too close to close  -  {mins_until_close:.0f}min remaining"

    return True, "ok"


def get_sleep(clock) -> int:
    if not clock.is_open:
        return 60
    mkt_open = clock.next_close - timedelta(hours=6, minutes=30)
    elapsed  = (clock.timestamp - mkt_open).total_seconds()
    if elapsed < OPENING_BURST_DURATION:
        return OPENING_BURST_SECONDS
    return SLEEP_SECONDS


# =========================================================
# PORTFOLIO
# =========================================================

def get_portfolio() -> dict:
    a   = trading_client.get_account()
    pos = trading_client.get_all_positions()
    pv  = float(a.portfolio_value)
    return {
        "portfolio_value": pv,
        "cash":            float(a.cash),
        "buying_power":    float(a.buying_power),
        "positions":       {p.symbol: {
            "qty":           float(p.qty),
            "market_value":  float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "avg_entry":     float(p.avg_entry_price),
            "current_price": float(p.current_price) if hasattr(p,"current_price") else 0,
        } for p in pos}
    }


def count_open_brackets(state: dict) -> int:
    """Count bracket orders that are still active."""
    return len(state.get("open_brackets", {}))


def sync_brackets_with_alpaca(state: dict) -> dict:
    """
    Check which bracket orders have been filled/closed by Alpaca.
    Update win/loss/pnl stats. Remove closed brackets from state.
    """
    if not state.get("open_brackets"):
        return state

    try:
        orders = trading_client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=100,
                nested=True
            )
        )
        closed_symbols = {str(o.symbol) for o in orders
                          if hasattr(o, "symbol") and o.symbol}
    except Exception:
        return state

    to_remove = []
    for ticker, bracket in state["open_brackets"].items():
        if ticker in closed_symbols:
            # Try to determine if it was a win or loss
            try:
                pos_list = trading_client.get_all_positions()
                still_open = any(p.symbol == ticker for p in pos_list)
                if not still_open:
                    # Position closed  -  figure out P/L from recent orders
                    recent = [o for o in orders if str(o.symbol) == ticker]
                    if recent:
                        sell_order = sorted(recent,
                            key=lambda o: o.filled_at or datetime.min.replace(tzinfo=timezone.utc),
                            reverse=True)[0]
                        fill_price = float(sell_order.filled_avg_price or 0)
                        entry      = float(bracket.get("entry", fill_price))
                        qty        = float(bracket.get("qty", 0))
                        pnl        = (fill_price - entry) * qty

                        state["total_pnl"] = round(
                            state.get("total_pnl", 0) + pnl, 4)
                        if pnl >= 0:
                            state["wins"]   = state.get("wins", 0) + 1
                        else:
                            state["losses"] = state.get("losses", 0) + 1

                        result = "WIN" if pnl >= 0 else "LOSS"
                        print(f"  [bracket closed] {ticker}: {result} "
                              f"${pnl:+.2f}")
                    to_remove.append(ticker)
            except Exception:
                to_remove.append(ticker)

    for t in to_remove:
        state["open_brackets"].pop(t, None)

    return state


def rearm_orphaned_positions(state: dict) -> dict:
    """
    V7.1.10 safety net.

    If any open position no longer has live take-profit / stop-loss
    orders (which would happen on a v1.1.9-or-earlier bot whose
    DAY-TIF bracket children expired at 16:00 ET), re-place the legs
    so the position is protected again. Uses the original entry +
    stored stop / tp levels from state["open_brackets"] if available;
    otherwise computes a fresh ATR-based stop and a +2x ATR target
    from the current price.

    No-op for positions that already have an open SELL order alive.
    """
    try:
        positions = trading_client.get_all_positions()
    except Exception:
        return state
    if not positions:
        return state

    # Get all currently-open orders so we can tell which positions
    # already have a live exit order.
    try:
        live = trading_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN,
                                    limit=200, nested=True)
        ) or []
    except Exception:
        live = []
    have_sell_for = {str(o.symbol) for o in live
                     if hasattr(o, "side") and "SELL" in str(o.side).upper()}

    for p in positions:
        sym = str(p.symbol)
        if sym in have_sell_for:
            continue                       # already protected
        try:
            qty   = int(float(p.qty))
            if qty <= 0:
                continue
            entry = float(p.avg_entry_price)
            # Prefer the levels we recorded when we placed the bracket
            saved = (state.get("open_brackets") or {}).get(sym, {})
            stop  = float(saved.get("stop") or 0)
            tp    = float(saved.get("tp")   or 0)
            # Fallback: re-derive from a fresh ATR if we don't have them
            if not (stop and tp):
                try:
                    data = analyse_stock(sym)
                    atr  = data.get("atr") if data else None
                    if atr:
                        stop = round(entry - atr * live_stop_atr_mult(), 2)
                        tp   = round(entry + atr * live_tp_atr_mult(),   2)
                except Exception:
                    pass
            if not (stop and tp):
                # Last resort: 1 % / 2 %
                stop = round(entry * 0.99, 2)
                tp   = round(entry * 1.02, 2)

            # Place a STAND-ALONE OCO pair (one-cancels-other) so we
            # don't have to re-buy. Alpaca exposes this via OrderClass.OCO
            # with a take_profit + stop_loss leg on a SELL.
            order = MarketOrderRequest(
                symbol=sym,
                qty=str(qty),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.OCO,
                stop_loss=StopLossRequest(stop_price=stop),
                take_profit=TakeProfitRequest(limit_price=tp),
            )
            trading_client.submit_order(order)
            print(f"  [re-arm] {sym} qty={qty} stop=${stop} tp=${tp}",
                  flush=True)
            # Track in state so future syncs can update wins/losses
            state.setdefault("open_brackets", {})[sym] = {
                "qty":   qty, "entry": entry,
                "stop":  stop, "tp":    tp,
                "rearmed": True,
            }
        except Exception as e:
            print(f"  [re-arm failed] {sym}: {e}", flush=True)

    return state


def force_close_stale_brackets(state: dict) -> dict:
    """
    Safety net: if a position's current price has already moved past the
    take-profit or stop-loss level but Alpaca's bracket order didn't
    auto-fill (paper-trading gap fills, data delays, etc.), send a manual
    market close. Called every run cycle after sync_brackets_with_alpaca.
    """
    if not state.get("open_brackets"):
        return state

    try:
        positions = {str(p.symbol): p for p in trading_client.get_all_positions()}
    except Exception:
        return state

    to_remove = []
    for ticker, bracket in list(state["open_brackets"].items()):
        pos = positions.get(ticker)
        if pos is None:
            continue

        try:
            current = float(pos.current_price or 0)
        except Exception:
            continue

        tp  = float(bracket.get("tp",   0))
        sl  = float(bracket.get("stop", 0))
        qty = float(bracket.get("qty",  0))

        hit_tp = tp > 0 and current >= tp
        hit_sl = sl > 0 and current <= sl
        if not (hit_tp or hit_sl):
            continue

        reason = (f"price ${current:.2f} >= TP ${tp:.2f}"
                  if hit_tp else
                  f"price ${current:.2f} <= SL ${sl:.2f}")
        print(f"  [force-close] {ticker}: {reason} — bracket "
              f"didn't auto-fill, sending manual market SELL", flush=True)

        # Cancel any open exit orders first so the manual sell isn't blocked
        try:
            live = trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN,
                                       limit=50, nested=False)
            ) or []
            for o in live:
                if str(getattr(o, "symbol", "")) == ticker:
                    try:
                        trading_client.cancel_order_by_id(str(o.id))
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            sell = MarketOrderRequest(
                symbol=ticker,
                qty=str(int(qty)) if qty == int(qty) else str(qty),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            trading_client.submit_order(sell)
            entry = float(bracket.get("entry", current))
            pnl   = (current - entry) * qty
            state["total_pnl"] = round(state.get("total_pnl", 0) + pnl, 4)
            if pnl >= 0:
                state["wins"]   = state.get("wins",   0) + 1
            else:
                state["losses"] = state.get("losses", 0) + 1
            print(f"  [force-close] {ticker}: market sell submitted "
                  f"qty={int(qty)}, P/L≈${pnl:+.2f}", flush=True)
            to_remove.append(ticker)
        except Exception as e:
            print(f"  [force-close failed] {ticker}: {e}", flush=True)

    for t in to_remove:
        state["open_brackets"].pop(t, None)
    return state


# =========================================================
# LAYER 1  -  INDICATORS  (identical to longbot_v2)
# =========================================================

def _ema(s: np.ndarray, p: int) -> np.ndarray:
    out = np.zeros_like(s, dtype=float)
    k   = 2.0 / (p + 1)
    out[0] = s[0]
    for i in range(1, len(s)):
        out[i] = s[i] * k + out[i-1] * (1-k)
    return out


def calc_rsi(c: np.ndarray, p=14) -> float:
    if len(c) < p+1: return 50.0
    d  = np.diff(c)
    ag = np.where(d>0,d,0)[-p:].mean()
    al = np.where(d<0,-d,0)[-p:].mean()
    if al == 0: return 100.0
    return round(100 - 100/(1+ag/al), 2)


def calc_macd(c: np.ndarray) -> tuple:
    if len(c) < 26: return 0.,0.,0.
    m = _ema(c,12) - _ema(c,26)
    s = _ema(m,9)
    return round(float(m[-1]),4), round(float(s[-1]),4), round(float((m-s)[-1]),4)


def calc_bollinger(c: np.ndarray, p=20) -> tuple:
    if len(c) < p: v=c[-1]; return v,v,v,0.5
    w=c[-p:]; mid=w.mean(); std=w.std()
    up=mid+2*std; lo=mid-2*std
    pb=(c[-1]-lo)/(up-lo) if (up-lo)>0 else 0.5
    return round(up,4),round(mid,4),round(lo,4),round(pb,4)


def calc_atr(h,l,c,p=14) -> float:
    if len(h)<p+1: return float(h[-1]-l[-1])
    tr=np.maximum(h[1:]-l[1:],np.maximum(
        np.abs(h[1:]-c[:-1]),np.abs(l[1:]-c[:-1])))
    return round(float(tr[-p:].mean()),4)


def calc_vwap(h,l,c,v) -> float:
    tp=(h+l+c)/3; tv=v.sum()
    return round(float((tp*v).sum()/tv),4) if tv>0 else float(c[-1])


def calc_zscore(c: np.ndarray, w=20) -> float:
    if len(c)<w: return 0.
    seg=c[-w:]; std=seg.std()
    return round(float((c[-1]-seg.mean())/std),3) if std>0 else 0.


def detect_golden_cross(c):
    if len(c)<55: return False
    for i in range(-5,0):
        if c[i-20:i].mean()>c[i-50:i].mean() and c[i-21:i-1].mean()<=c[i-51:i-1].mean():
            return True
    return False


def detect_bullish_divergence(c,p=14):
    if len(c)<40: return False
    return (calc_rsi(c[-20:],p) > calc_rsi(c[-40:-20],p)) and (c[-1]<c[-20])


def detect_double_bottom(c,tol=0.03):
    if len(c)<30: return False
    w=c[-30:]
    tr=[w[i] for i in range(1,len(w)-1) if w[i]<w[i-1] and w[i]<w[i+1]]
    if len(tr)<2: return False
    return abs(tr[-2]-tr[-1])/max(tr[-2],tr[-1])<tol


def detect_volume_confirmation(v,c):
    if len(v)<10: return False
    return v[-5:].mean()>v[-10:-5].mean()*1.2 and c[-1]>c[-5]


# =========================================================
# LAYER 1  -  STOCK ANALYSIS  (day-trading focused)
# =========================================================

def analyse_stock(ticker: str) -> dict:
    """
    Fetch 80 days of OHLCV + fundamentals.
    Compute bullish score optimised for intraday bracket entries:
    - Requires meaningful ATR (stock must actually move)
    - Requires sufficient volume (can enter/exit cleanly)
    - Rewards momentum + oversold conditions + patterns
    Returns None if filtered out.
    """
    try:
        hist = yf.Ticker(ticker).history(period="80d", auto_adjust=True)
        if hist.empty or len(hist) < 30:
            return None

        c = hist["Close"].values.astype(float)
        h = hist["High"].values.astype(float)
        l = hist["Low"].values.astype(float)
        v = hist["Volume"].values.astype(float)

        price = c[-1]
        atr   = calc_atr(h, l, c)
        atr_pct = atr / price if price > 0 else 0

        # -- Hard filters ----------------------------------
        # Must have enough movement to make bracket worthwhile
        if atr_pct < MIN_ATR_PCT:
            return None
        # Must have enough volume to enter/exit without slippage
        avg_vol = v[-20:].mean()
        if avg_vol < MIN_VOLUME:
            return None

        # -- Indicators ------------------------------------
        rsi                  = calc_rsi(c)
        _, _, macd_hist      = calc_macd(c)
        bb_up, bb_mid, bb_lo, pct_b = calc_bollinger(c)
        vwap                 = calc_vwap(h, l, c, v)
        zscore               = calc_zscore(c)

        ma20 = c[-20:].mean() if len(c)>=20 else price
        ma50 = c[-50:].mean() if len(c)>=50 else price

        w_ret = round((price/c[-5]-1)*100,2)  if len(c)>=5  else 0
        m_ret = round((price/c[-20]-1)*100,2) if len(c)>=20 else 0
        vol   = round(float(np.diff(np.log(c[-20:])).std()*100),3)

        # -- Patterns --------------------------------------
        patterns = {
            "golden_cross":        detect_golden_cross(c),
            "bullish_divergence":  detect_bullish_divergence(c),
            "double_bottom":       detect_double_bottom(c),
            "volume_confirmation": detect_volume_confirmation(v, c),
        }
        n_patterns = sum(patterns.values())

        # -- Bullish score (same as longbot_v2) ------------
        score = 0.0
        score += max(0, m_ret)    * 0.30
        score += max(0, w_ret)    * 0.50
        score += max(0, 40-rsi)   * 0.40   # oversold = room to run
        score += max(0, 0.3-pct_b)* 30     # near lower BB = bounce setup
        score += max(0, -zscore)  * 6      # underextended
        score += max(0, macd_hist*100)*0.50
        if price < vwap:
            score += 8
        score += n_patterns * 14
        score *= (1 + vol * 0.06)           # vol multiplier
        score  = round(float(score), 3)

        # -- Fundamentals (quick check, no filter) ---------
        try:
            info = yf.Ticker(ticker).info
            earn_ts = info.get("earningsTimestamp","")
            if earn_ts and str(earn_ts) not in ("None",""):
                dt = datetime.fromtimestamp(float(earn_ts), tz=timezone.utc)
                days = (dt - datetime.now(timezone.utc)).days
                if 0 <= days <= EARNINGS_BLACKOUT:
                    return None   # blackout
        except Exception:
            pass

        return {
            "ticker":   ticker,
            "price":    round(price, 2),
            "atr":      atr,
            "atr_pct":  round(atr_pct*100, 2),
            "avg_vol":  int(avg_vol),
            "score":    score,
            "indicators": {
                "rsi":       rsi,
                "macd_hist": macd_hist,
                "pct_b":     round(pct_b,3),
                "zscore":    zscore,
                "w_ret":     w_ret,
                "m_ret":     m_ret,
                "vol":       vol,
                "vs_vwap":   round(price-vwap,3),
                "vs_ma20":   round((price/ma20-1)*100,2),
            },
            "patterns":  patterns,
            "n_patterns":n_patterns,
            # -- bracket levels pre-computed (live ATR multiples) ----
            "stop_loss":   round(price - atr * live_stop_atr_mult(), 2),
            "take_profit": round(price + atr * live_tp_atr_mult(),   2),
            "risk_pct":    round(atr * live_stop_atr_mult() / price * 100, 2),
            "reward_pct":  round(atr * live_tp_atr_mult()  / price * 100, 2),
            # full OHLCV for chart
            "_ohlcv": {
                "c": c.tolist(), "h": h.tolist(),
                "l": l.tolist(), "v": v.tolist(),
                "dates": [str(d)[:10] for d in hist.index],
                "ma20": [round(float(c[max(0,i-19):i+1].mean()),2) for i in range(len(c))],
                "ma50": [round(float(c[max(0,i-49):i+1].mean()),2) for i in range(len(c))],
                "bb_up": bb_up, "bb_lo": bb_lo,
            }
        }

    except Exception as e:
        print(f"  [scan] {ticker}: {e}")
        return None


def scan_universe() -> list:
    tickers = load_universe()
    print(f"  Scanning {len(tickers)} tickers from {UNIVERSE_FILE}...")
    results = [d for t in tickers if (d := analyse_stock(t)) is not None]
    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:TOP_N_SCAN]
    if top:
        best = top[0]
        print(f"  -> {len(results)} valid | best: {best['ticker']} "
              f"(score {best['score']:.1f}, "
              f"ATR {best['atr_pct']:.1f}%, "
              f"{best['n_patterns']} patterns)")
    return top


# =========================================================
# LAYER 2A  -  CHART  (same dark style as other bots)
# =========================================================

CS = {
    "bg":"#060a0e","panel":"#0b0f16","border":"#18212e",
    "text":"#d0d8e4","muted":"#4a5568",
    "green":"#00f5c3","red":"#ff4d6d",
    "yellow":"#ffe353","purple":"#7b8cff",
}


def generate_chart(data: dict) -> bytes:
    """
    4-panel chart identical to longbot_v2.
    Extra: stop loss and take profit levels drawn as horizontal lines.
    """
    o   = data["_ohlcv"]
    n   = min(60, len(o["c"]))
    c   = np.array(o["c"][-n:])
    h   = np.array(o["h"][-n:])
    l   = np.array(o["l"][-n:])
    v   = np.array(o["v"][-n:])
    ma20= np.array(o["ma20"][-n:])
    ma50= np.array(o["ma50"][-n:])
    dt  = o["dates"][-n:]
    x   = np.arange(n)

    full = np.array(o["c"])
    rsi_s = np.array([calc_rsi(full[:i+1]) for i in range(len(full))])[-n:]
    ema12 = _ema(full,12); ema26 = _ema(full,26)
    macd_l= (ema12-ema26)[-n:]; sig_l=_ema(ema12-ema26,9)[-n:]
    macd_h= macd_l - sig_l

    fig = plt.figure(figsize=(14,10), facecolor=CS["bg"])
    gs  = gridspec.GridSpec(4,1,height_ratios=[5,1.5,1.2,1.2],
                            hspace=0.04,left=0.06,right=0.97,
                            top=0.93,bottom=0.06)
    ax_p=fig.add_subplot(gs[0]); ax_v=fig.add_subplot(gs[1],sharex=ax_p)
    ax_r=fig.add_subplot(gs[2],sharex=ax_p); ax_m=fig.add_subplot(gs[3],sharex=ax_p)

    for ax in [ax_p,ax_v,ax_r,ax_m]:
        ax.set_facecolor(CS["panel"])
        ax.tick_params(colors=CS["muted"],labelsize=7)
        ax.spines[:].set_color(CS["border"])
        ax.grid(color=CS["border"],linewidth=0.4,alpha=0.7)
        plt.setp(ax.get_xticklabels(),visible=False)
    plt.setp(ax_m.get_xticklabels(),visible=True,rotation=30,ha="right")

    # Candles
    for i in range(n):
        o2=c[i-1] if i>0 else c[i]; cl=c[i]; hi=h[i]; lo=l[i]
        col=CS["green"] if cl>=o2 else CS["red"]
        ax_p.plot([i,i],[lo,hi],color=col,linewidth=0.8,alpha=0.8)
        bh=abs(cl-o2) if abs(cl-o2)>0 else (hi-lo)*0.1
        ax_p.bar(i,bh,bottom=min(cl,o2),width=0.6,color=col,alpha=0.9,linewidth=0)

    ax_p.plot(x,ma20,color=CS["yellow"],linewidth=1.2,label="MA20",alpha=0.9)
    ax_p.plot(x,ma50,color=CS["purple"],linewidth=1.2,label="MA50",alpha=0.9)
    bb_up=np.full(n,o["bb_up"]); bb_lo=np.full(n,o["bb_lo"])
    ax_p.plot(x,bb_up,color=CS["muted"],linewidth=0.8,linestyle="--",alpha=0.6,label="BB")
    ax_p.plot(x,bb_lo,color=CS["muted"],linewidth=0.8,linestyle="--",alpha=0.6)
    ax_p.fill_between(x,bb_up,bb_lo,color=CS["muted"],alpha=0.04)

    # -- KEY ADDITION: draw stop loss and take profit levels --
    sl = data["stop_loss"]
    tp = data["take_profit"]
    ax_p.axhline(sl, color=CS["red"],   linewidth=1.5, linestyle="--", alpha=0.9,
                 label=f"SL ${sl:.2f}")
    ax_p.axhline(tp, color=CS["green"], linewidth=1.5, linestyle="--", alpha=0.9,
                 label=f"TP ${tp:.2f}")
    ax_p.axhline(data["price"], color="white", linewidth=0.8,
                 linestyle=":", alpha=0.5, label=f"Entry ${data['price']:.2f}")

    # Pattern labels
    pats = [k for k,v in data["patterns"].items() if v]
    if pats:
        ax_p.annotate("  ".join(pats),
            xy=(n*0.02, c.min()*0.99), fontsize=7.5,
            color=CS["green"], fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3",fc=CS["panel"],
                      ec=CS["green"],alpha=0.85))

    ax_p.set_ylabel("Price $",color=CS["muted"],fontsize=8)
    ax_p.tick_params(axis="y",colors=CS["muted"])
    ax_p.legend(loc="upper left",fontsize=7,framealpha=0.3,
                labelcolor=CS["text"],facecolor=CS["panel"])

    ind = data["indicators"]
    fig.suptitle(
        f"{data['ticker']}  ${data['price']:.2f}  "
        f"Score:{data['score']:.0f}  RSI:{ind['rsi']:.0f}  "
        f"ATR:{data['atr_pct']:.1f}%  "
        f"R/R: {data['risk_pct']:.1f}% / {data['reward_pct']:.1f}%",
        color=CS["text"],fontsize=11,fontweight="bold",y=0.975,
        bbox=dict(boxstyle="round",fc=CS["panel"],ec=CS["border"],alpha=0.9)
    )

    vcols=[CS["green"] if c[i]>=(c[i-1] if i>0 else c[i]) else CS["red"] for i in range(n)]
    ax_v.bar(x,v,color=vcols,alpha=0.55,width=0.8)
    ax_v.plot(x,[v[max(0,i-9):i+1].mean() for i in range(n)],
              color=CS["yellow"],linewidth=0.9,alpha=0.8)
    ax_v.set_ylabel("Vol",color=CS["muted"],fontsize=7)
    ax_v.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda val,_: f"{val/1e6:.1f}M" if val>=1e6 else f"{val/1e3:.0f}K"))

    ax_r.plot(x,rsi_s,color=CS["purple"],linewidth=1.2)
    ax_r.axhline(70,color=CS["red"],  linewidth=0.7,linestyle="--",alpha=0.7)
    ax_r.axhline(30,color=CS["green"],linewidth=0.7,linestyle="--",alpha=0.7)
    ax_r.fill_between(x,rsi_s,30,where=(rsi_s<=30),color=CS["green"],alpha=0.15)
    ax_r.set_ylim(0,100); ax_r.set_ylabel("RSI",color=CS["muted"],fontsize=7)
    ax_r.yaxis.set_ticks([30,50,70])

    ax_m.bar(x,macd_h,color=[CS["green"] if v>=0 else CS["red"] for v in macd_h],
             alpha=0.7,width=0.8)
    ax_m.plot(x,macd_l,color=CS["yellow"],linewidth=1.0,label="MACD")
    ax_m.plot(x,sig_l, color=CS["purple"],linewidth=1.0,label="Signal")
    ax_m.axhline(0,color=CS["muted"],linewidth=0.5)
    ax_m.set_ylabel("MACD",color=CS["muted"],fontsize=7)
    ax_m.legend(loc="upper left",fontsize=6,framealpha=0.2,
                labelcolor=CS["text"],facecolor=CS["panel"])

    step=max(1,n//8)
    ax_m.set_xticks(range(0,n,step))
    ax_m.set_xticklabels([dt[i] for i in range(0,n,step)],
                          color=CS["muted"],fontsize=7)

    buf=io.BytesIO()
    plt.savefig(buf,format="png",dpi=110,facecolor=CS["bg"],bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================================================
# LAYER 2B  -  CLAUDE VISION  (called only when setup is strong)
# =========================================================

def ask_claude(candidates: list, charts: dict) -> dict:
    """
    Send top 3 charts to Claude.
    Ask for ONE best ticker + confidence.
    Returns {"ticker": "NVDA", "confidence": 0.85, "reason": "..."}
    or {"ticker": None, "confidence": 0.0, "reason": "no setup"}
    """
    content = []
    content.append({"type":"text","text":(
        "You are a day trader analysing bracket order setups.\n"
        "Each chart shows: candlestick + MA20/MA50 + Bollinger Bands + "
        "stop loss (red dashed) + take profit (green dashed) + RSI + MACD.\n"
        "The stop loss and take profit are pre-set using ATR.\n\n"
        "Return ONLY valid JSON  -  no markdown, no text outside JSON.\n\n"
        "Format:\n"
        '{"ticker":"NVDA","confidence":0.85,"reason":"one sentence"}\n\n'
        "Rules:\n"
        "- Pick the SINGLE best bracket setup from the charts\n"
        "- Only pick if you see a clear bullish pattern with high probability\n"
        "- The entry must be clean  -  not in the middle of a range\n"
        "- RSI should be coming from oversold OR showing bullish momentum\n"
        "- MACD histogram should be green or turning green\n"
        "- Volume confirmation makes the setup much stronger\n"
        "- If NO setup is convincing enough, return ticker: null\n"
        "- confidence between 0 and 1\n\n"
        "Candidates:"
    )})

    for c in candidates[:TOP_N_CLAUDE]:
        t = c["ticker"]
        content.append({"type":"text","text":(
            f"\n--- {t} | Score:{c['score']:.0f} | "
            f"RSI:{c['indicators']['rsi']:.0f} | "
            f"ATR:{c['atr_pct']:.1f}% | "
            f"SL:${c['stop_loss']} | TP:${c['take_profit']} | "
            f"Patterns:{[k for k,v in c['patterns'].items() if v] or 'none'} ---"
        )})
        if t in charts:
            content.append({
                "type":"image",
                "source":{"type":"base64","media_type":"image/png","data":charts[t]}
            })

    raw = call_ai_vision(content, _AI_PROVIDER, _AI_MODEL, _AI_KEY, MAX_TOKENS)
    raw = raw.replace("```json","").replace("```","").strip()
    print(f"  {_AI_PROVIDER.capitalize()}: {raw}")
    try:
        return json.loads(raw)
    except Exception:
        return {"ticker": None, "confidence": 0.0, "reason": "JSON parse error"}


def ask_ai_text(candidates: list) -> dict:
    """Text-only AI path for daybot — returns ONE ticker to trade today.
    Works with Groq and all providers (no charts needed)."""
    lines = []
    for c in candidates[:TOP_N_CLAUDE]:
        t = c["ticker"]
        ind = c.get("indicators", {})
        pats = [k for k, v in c.get("patterns", {}).items() if v]
        lines.append(
            f"  {t}: RSI={ind.get('rsi', 0):.0f} "
            f"ATR={c.get('atr_pct', 0):.1f}% "
            f"SL=${c.get('stop_loss', 0)} TP=${c.get('take_profit', 0)} "
            f"patterns={pats or 'none'}"
        )

    prompt = (
        f"You are a day-trading assistant. Pick ONE stock from the list below "
        f"for a bracket order trade today (one entry, one stop-loss, one take-profit).\n\n"
        f"CANDIDATES:\n" + "\n".join(lines) + "\n\n"
        f"Return ONLY valid JSON. No markdown.\n"
        f'{{"ticker":"AAPL","confidence":0.85,"reason":"one sentence"}}\n\n'
        f"Rules:\n"
        f"- Pick exactly ONE ticker, or null if nothing is attractive\n"
        f"- confidence between 0 and 1 (only pick if > 0.6)\n"
        f"- Prefer: clear pattern + ATR stop not too wide + good R:R\n"
        f"- If unsure: return {{\"ticker\": null, \"confidence\": 0, \"reason\": \"no clear setup\"}}"
    )

    raw = call_ai_text(prompt, _AI_PROVIDER, _AI_MODEL, _AI_KEY, MAX_TOKENS)
    raw = raw.replace("```json", "").replace("```", "").strip()
    print(f"  {_AI_PROVIDER.capitalize()} (text): {raw}")
    try:
        return json.loads(raw)
    except Exception:
        return {"ticker": None, "confidence": 0.0, "reason": "JSON parse error"}


# =========================================================
# LAYER 3  -  BRACKET EXECUTION
# =========================================================

def calculate_position_size(portfolio: dict, entry: float, stop: float) -> float:
    """
    Risk-based position sizing:
    Risk $ = portfolio_value x RISK_PER_TRADE_PCT
    Qty    = Risk $ / (entry - stop)

    This means no matter what stock you're trading,
    you always risk the same dollar amount per trade.
    """
    pv          = portfolio["portfolio_value"]
    risk_dollars = pv * RISK_PER_TRADE_PCT
    risk_per_share = entry - stop

    if risk_per_share <= 0:
        return 0.0

    qty           = risk_dollars / risk_per_share
    notional      = qty * entry
    max_notional  = pv * MAX_POSITION_PCT

    # Cap at max position size
    if notional > max_notional:
        qty = max_notional / entry

    # Alpaca BRACKET orders do NOT support fractional shares -> whole shares
    qty = math.floor(qty)
    if qty < 1:
        return 0.0

    # Floor at minimum trade value
    if qty * entry < MIN_POSITION_VALUE:
        return 0.0

    return float(qty)


def place_bracket(ticker: str, entry_price: float, stop: float,
                  tp: float, qty: float) -> bool:
    """
    Place a bracket order on Alpaca.
    Returns True if successful.

    V7.1.10 — time_in_force changed from DAY to GTC. Alpaca's bracket
    children (the take-profit limit order and the stop-loss stop order)
    inherit the parent's TIF. With DAY they were cancelled at 16:00 ET,
    so any position not closed same-day became an unprotected open
    position — next day the price could pass the take-profit threshold
    and nothing would sell (the TP order no longer existed). GTC keeps
    the TP and SL alive across sessions until either fills or the user
    explicitly cancels.

    Market entry still fills instantly (TIF on a market order doesn't
    delay the fill), so the only behavioural change is that exit
    orders now persist.
    """
    try:
        order = MarketOrderRequest(
            symbol=ticker,
            qty=str(int(qty)),          # whole shares (bracket requirement)
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(
                stop_price=round(stop, 2),
            ),
            take_profit=TakeProfitRequest(
                limit_price=round(tp, 2),
            ),
        )
        trading_client.submit_order(order)
        return True
    except Exception as e:
        print(f"  [bracket] {ticker} failed: {e}")
        return False


# =========================================================
# LOGGING
# =========================================================

def log_run(signal: dict, action: str, portfolio: dict,
            candidate: dict, skipped: bool = False):
    entry = {
        "time":      datetime.now(timezone.utc).isoformat(),
        "skipped":   skipped,
        "signal":    signal,
        "action":    action,
        "portfolio": {
            "value": portfolio["portfolio_value"],
            "cash":  portfolio["cash"],
        },
    }
    if candidate:
        entry["candidate"] = {
            "ticker":      candidate["ticker"],
            "score":       candidate["score"],
            "stop_loss":   candidate["stop_loss"],
            "take_profit": candidate["take_profit"],
            "risk_pct":    candidate["risk_pct"],
            "reward_pct":  candidate["reward_pct"],
        }

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    text = (
        f"\nTIME: {datetime.now()}\n"
        f"ACTION: {action}\n"
    )
    if candidate and not skipped:
        text += (
            f"TICKER: {candidate['ticker']}\n"
            f"ENTRY:  ${candidate['price']:.2f}\n"
            f"STOP:   ${candidate['stop_loss']:.2f}  "
            f"(-{candidate['risk_pct']:.1f}%)\n"
            f"TARGET: ${candidate['take_profit']:.2f}  "
            f"(+{candidate['reward_pct']:.1f}%)\n"
            f"R/R:    1:{live_tp_atr_mult()/max(live_stop_atr_mult(),1e-9):.1f}\n"
        )
    if signal:
        text += f"CONFIDENCE: {signal.get('confidence',0):.0%}\n"
        text += f"REASON: {signal.get('reason','')}\n"

    with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


def print_stats(state: dict):
    wins   = state.get("wins", 0)
    losses = state.get("losses", 0)
    total  = wins + losses
    wr     = wins/total*100 if total > 0 else 0
    pnl    = state.get("total_pnl", 0)
    print(f"  Stats: {wins}W / {losses}L  ({wr:.0f}% win rate)  "
          f"Total P/L: ${pnl:+.2f}")


# =========================================================
# MAIN LOOP
# =========================================================

def run_once():
    state     = load_state()
    state     = reset_daily(state)
    state     = sync_brackets_with_alpaca(state)
    state     = force_close_stale_brackets(state)
    # V7.1.10 — protect any position that lost its TP/SL legs (e.g.
    # legacy DAY-TIF brackets that expired overnight)
    state     = rearm_orphaned_positions(state)
    portfolio = get_portfolio()
    pv        = portfolio["portfolio_value"]

    print(f"\n  Portfolio: ${pv:,.2f} | "
          f"Cash: ${portfolio['cash']:,.2f} | "
          f"Open brackets: {count_open_brackets(state)}")
    print_stats(state)

    # -- Skip only if at the (optional) concurrent-position cap ----
    mb = live_max_brackets()
    n_open = count_open_brackets(state)
    if mb > 0 and n_open >= mb:
        print(f"  At max concurrent positions ({n_open}/{mb})  -  "
              f"skipping new entry this run")
        log_run({}, f"Skipped  -  at max positions ({n_open}/{mb})",
                portfolio, None, skipped=True)
        save_state(state)
        return

    # -- Scan universe -------------------------------------
    candidates = scan_universe()

    if not candidates:
        print("  No valid candidates.")
        log_run({}, "No valid candidates", portfolio, None, skipped=True)
        save_state(state)
        return

    best = candidates[0]

    # -- Skip if best score is below threshold -------------
    # This saves the Claude API call for weak market days
    if best["score"] < MIN_SCORE_FOR_CLAUDE:
        print(f"  Best score {best['score']:.1f} < {MIN_SCORE_FOR_CLAUDE} threshold  -  no Claude call")
        log_run({}, f"Score too low ({best['score']:.1f})", portfolio, best, skipped=True)
        save_state(state)
        return

    # -- Ask AI (vision or text mode) ----------------------
    if _AI_MODE == "text":
        print(f"  Asking {_AI_PROVIDER} (text mode — no charts)...")
        signal = ask_ai_text(candidates)
    else:
        # Vision mode: generate charts then ask AI
        print(f"  Generating charts for top {min(TOP_N_CLAUDE, len(candidates))}...")
        charts = {}
        for c in candidates[:TOP_N_CLAUDE]:
            try:
                png = generate_chart(c)
                charts[c["ticker"]] = base64.standard_b64encode(png).decode()
                path = os.path.join(CHART_DIR, f"{c['ticker']}.png")
                with open(path, "wb") as f:
                    f.write(png)
            except Exception as e:
                print(f"  [chart] {c['ticker']}: {e}")

        print(f"  Asking {_AI_PROVIDER} (vision)...")
        signal = ask_claude(candidates, charts)

    chosen_ticker = signal.get("ticker")
    confidence    = float(signal.get("confidence", 0))

    # -- No trade if Claude not confident ------------------
    if not chosen_ticker or confidence < live_min_confidence():
        reason = signal.get("reason", "low confidence")
        print(f"  No trade: {reason} (confidence {confidence:.0%})")
        log_run(signal, f"No trade  -  {reason}", portfolio, best, skipped=True)
        save_state(state)
        return

    # -- Find the chosen candidate's data ------------------
    chosen = next((c for c in candidates if c["ticker"] == chosen_ticker), None)
    if not chosen:
        print(f"  Claude chose {chosen_ticker} but not in candidates  -  skip")
        log_run(signal, "Chosen ticker not in candidates", portfolio, best, skipped=True)
        save_state(state)
        return

    # Don't stack a second bracket on a symbol we already hold
    if chosen_ticker in state.get("open_brackets", {}):
        print(f"  {chosen_ticker} already has an open bracket  -  skip")
        log_run(signal, f"Skipped  -  {chosen_ticker} already open",
                portfolio, chosen, skipped=True)
        save_state(state)
        return

    # -- Calculate position size ---------------------------
    qty = calculate_position_size(
        portfolio, chosen["price"], chosen["stop_loss"]
    )

    if qty <= 0:
        print(f"  Position size too small  -  skip")
        log_run(signal, "Position size too small", portfolio, chosen, skipped=True)
        save_state(state)
        return

    notional = round(qty * chosen["price"], 2)
    if notional > portfolio["buying_power"]:
        print(f"  Insufficient buying power "
              f"(need ${notional:.2f}, have ${portfolio['buying_power']:.2f})")
        log_run(signal, "Insufficient buying power", portfolio, chosen, skipped=True)
        save_state(state)
        return

    # -- Place bracket -------------------------------------
    print(f"  Placing bracket: {chosen_ticker} | "
          f"qty={int(qty)} | entry~${chosen['price']:.2f} | "
          f"SL=${chosen['stop_loss']:.2f} | "
          f"TP=${chosen['take_profit']:.2f}")

    success = place_bracket(
        chosen_ticker,
        chosen["price"],
        chosen["stop_loss"],
        chosen["take_profit"],
        qty
    )

    if success:
        state["open_brackets"][chosen_ticker] = {
            "entry": chosen["price"],
            "stop":  chosen["stop_loss"],
            "tp":    chosen["take_profit"],
            "qty":   qty,
            "time":  datetime.now(timezone.utc).isoformat(),
        }
        state["trades_today"] = state.get("trades_today", 0) + 1
        action = (
            f"BRACKET PLACED: {chosen_ticker} | "
            f"qty={qty:.4f} | ~${notional:.0f} | "
            f"SL=${chosen['stop_loss']:.2f} (-{chosen['risk_pct']:.1f}%) | "
            f"TP=${chosen['take_profit']:.2f} (+{chosen['reward_pct']:.1f}%) | "
            f"R/R=1:{live_tp_atr_mult()/max(live_stop_atr_mult(),1e-9):.1f}"
        )
    else:
        action = f"BRACKET FAILED: {chosen_ticker}"

    log_run(signal, action, portfolio, chosen)
    save_state(state)


_DAY_PHILOSOPHY = (
    "I open intraday LONG bracket trades only — entry, take-profit, "
    "and hard stop all submitted as a single bracket order. I never "
    "short. I never hold overnight. I exit any open position close to "
    "the close. I want clean breakout-of-resistance setups on liquid "
    "names with favorable risk-reward. I will close any short position "
    "(I cannot manage it), any position without a paired bracket "
    "exit, and any position that has been held longer than one session.")


def _day_ai_caller(prompt: str):
    try:
        return call_ai_text(prompt, _AI_PROVIDER, _AI_MODEL, _AI_KEY)
    except Exception as e:
        print(f"[transition] AI call failed: {e}", flush=True)
        return None


def main():
    print("=" * 65)
    print("APEX DAY BOT  -  Bracket Order Strategy")
    print(f"Universe: {UNIVERSE_FILE}  |  Model: {_AI_MODEL}")
    print(f"R/R: 1:{live_tp_atr_mult()/max(live_stop_atr_mult(),1e-9):.1f}  |  "
          f"Risk/trade: {RISK_PER_TRADE_PCT:.0%}  |  "
          f"Max brackets: {live_max_brackets() or 'unlimited'}")
    print(f"Min score for Claude call: {MIN_SCORE_FOR_CLAUDE}  |  "
          f"Min confidence: {MIN_CONFIDENCE:.0%}")
    print("=" * 65)

    # Print current universe on startup
    tickers = load_universe()
    print(f"Universe ({len(tickers)} tickers): {', '.join(tickers[:10])}"
          + (f"... +{len(tickers)-10} more" if len(tickers) > 10 else ""))
    print()

    # V4.6.2 — transition cleanup
    try:
        from core import transition as _T
        _acct = trading_client.get_account()
        _T.record_account_or_flag_transition("DAY", _acct.id)
    except Exception as _e:
        print(f"[transition] DAY init failed: {_e}", flush=True)

    # V4.6.26 — Initial deterministic cleanup: liquidate non-equity
    # positions a previous bot may have left behind.
    try:
        from core.bot_framework import liquidate_off_strategy_positions
        liquidate_off_strategy_positions(trading_client, "stocks")
    except Exception as _e:
        print(f"[startup-cleanup] DAY failed: {_e}", flush=True)

    while True:
        try:
            clock = trading_client.get_clock()
            try:
                from core import transition as _T
                _T.maybe_run_cleanup("DAY", trading_client,
                                     _day_ai_caller, _DAY_PHILOSOPHY)
            except Exception as _e:
                print(f"[transition] DAY cleanup error: {_e}", flush=True)

            if clock.is_open:
                can_trade, reason = is_good_trading_time()
                ts = datetime.now().strftime("%H:%M:%S")

                if can_trade:
                    print(f"[{ts}] Market OPEN  -  running")
                    run_once()
                else:
                    print(f"[{ts}] Waiting: {reason}")

                s = get_sleep(clock)
                print(f"  Next run in {s}s")
                time.sleep(s)

            else:
                # V4.6.6 — display next_open in Eastern Time (NYSE).
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    nxt = clock.next_open.astimezone(
                        _ZI("America/New_York")).strftime(
                        "%a %b %d %H:%M ET")
                except Exception:
                    nxt = clock.next_open.astimezone().strftime(
                        "%a %b %d %H:%M")
                print(f"\r[{datetime.now().strftime('%H:%M:%S')}] "
                      f"CLOSED  -  next open: {nxt}  ", end="")
                time.sleep(60)

        except KeyboardInterrupt:
            print("\nStopped.")
            break

        except Exception as e:
            print(f"\nERROR: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""
APEX LONG BOT v2
-----------------------------------------------------------------
Architecture mirrors shortbot_v2 exactly:

  Layer 1  -  Data & Indicators   (free, yfinance + numpy)
    * RSI, MACD, Bollinger Bands, ATR, VWAP, Stochastic
    * Bullish pattern detection: golden cross, bullish divergence,
      double bottom, falling wedge, volume confirmation
    * Fundamentals: P/E, revenue growth, debt/equity, short float
    * Earnings blackout filter (avoid binary events)
    * Market regime: SPY vs 50-day MA
      -> BULL regime: up to MAX_POSITIONS allowed
      -> BEAR regime: defensive, cut to fewer positions
    * Composite bullish score -> pre-filter to top 8 for Claude

  Layer 2  -  Claude Vision + JSON  (Haiku, cheap)
    * Chart images: candlestick + MA + BB + volume + RSI + MACD
    * Bullish patterns annotated directly on chart
    * Claude sees images + compact numeric table
    * Returns target portfolio weights + pattern notes

  Layer 3  -  Execution  (Alpaca paper)
    * ATR-based trailing stop-loss on each position
    * Time stop: sell if no meaningful gain after N days
    * Sell before buy, dust protection, cooldowns
    * close_position() for full exits (no leftover dust shares)

Files (separate from short bot  -  no conflicts):
    longv2_state.json
    longv2_trade_log.jsonl
    longv2_analysis.txt
    longv2_charts/

Cost per run: ~$0.005-0.015  (Haiku + 8 chart images)
-----------------------------------------------------------------
"""

print("Booting APEX LONG bot - loading libraries, first start can take 1-2 min...", flush=True)

import os
import io
import json
import math
import time
import base64
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from dotenv import load_dotenv
from anthropic import Anthropic

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

warnings.filterwarnings("ignore")


# =========================================================
# CONFIG
# =========================================================

UNIVERSE = [
    # Mega-cap / core
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "AVGO", "ORCL", "IBM", "CRM", "ADBE", "NFLX", "NOW", "INTU",
    # Semiconductors
    "AMD", "QCOM", "INTC", "MU", "TXN", "ASML", "ARM", "TSM",
    "AMAT", "LRCX", "KLAC", "MRVL", "SMCI", "DELL",
    # AI / software / cyber
    "PLTR", "CRWD", "PANW", "NET", "DDOG", "SNOW", "ZS", "MDB",
    "OKTA", "SHOP", "SPOT", "UBER", "ABNB", "APP",
    # Finance
    "JPM", "GS", "MS", "BAC", "BLK", "BX", "KKR",
    "V", "MA", "AXP", "PYPL", "HOOD", "COIN",
    # Healthcare
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ISRG",
    "VRTX", "REGN", "AMGN", "GILD", "NVO",
    # Consumer / defensive
    "PG", "KO", "PEP", "MCD", "WMT", "COST", "HD",
    "NKE", "SBUX", "DIS", "CMG", "PM",
    # Industrials / defense
    "CAT", "DE", "GE", "HON", "RTX", "LMT", "NOC", "GD", "BA",
    "VRT", "AVAV", "URI",
    # Energy / nuclear
    "XOM", "CVX", "COP", "OXY", "VST", "CEG",
    "NEE", "CCJ", "SMR", "OKLO",
    # High-vol growth
    "MSTR", "SOFI", "ROKU", "UPST", "AFRM",
    "RKLB", "ASTS", "IONQ",
    # China / international
    "BABA", "PDD", "TSM", "TM", "INFY",
    # Crypto-linked
    "MARA", "RIOT", "HUT",
]

ETF_UNIVERSE = {
    "SPY", "VOO", "VTI", "QQQ", "DIA", "IWM",
    "XLK", "XLF", "XLV", "XLI", "XLY", "XLP", "XLE",
    "SMH", "SOXX", "ARKK", "BOTZ", "AIQ", "HACK", "CIBR",
    "URA", "LIT", "XBI",
}

ALL_UNIVERSE = list(set(UNIVERSE) | ETF_UNIVERSE)

# -- Portfolio rules --------------------------------------
MIN_CONFIDENCE         = 0.60


def live_min_confidence() -> float:
    """App-adjustable confidence threshold. Read from apex_settings.json
    every decision cycle (no restart). Falls back to MIN_CONFIDENCE."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = float(json.load(_f).get("LONG", {}).get("min_confidence"))
        if 0.0 < _v <= 1.0:
            return _v
    except Exception:
        pass
    return MIN_CONFIDENCE
MIN_POSITIONS          = 5


def live_min_positions() -> int:
    """App-adjustable floor on number of positions. Read from
    apex_settings.json every cycle (no restart). When > 0 the bot deploys
    at least this many top-ranked names instead of sitting in cash, even
    if the AI says HOLD. 0 = fully cautious (original behaviour).
    Falls back to MIN_POSITIONS."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = json.load(_f).get("LONG", {}).get("min_positions")
        if _v is not None:
            _v = int(_v)
            if 0 <= _v <= MAX_POSITIONS:
                return _v
    except Exception:
        pass
    return MIN_POSITIONS


MAX_POSITIONS          = 20
MAX_SINGLE_WEIGHT      = 0.22
MAX_ETF_WEIGHT         = 0.35
MIN_CASH_WEIGHT        = 0.03

# -- Stops ------------------------------------------------
ATR_STOP_MULTIPLIER    = 2.5    # sell if price drops 2.5xATR below entry
TIME_STOP_DAYS         = 10     # sell if no meaningful gain after 10 days
MIN_PROFIT_TO_TRAIL    = 0.04   # start trailing after 4% profit

# -- Rebalancing ------------------------------------------
MIN_REBALANCE_DELTA    = 0.02
MIN_ORDER_PCT          = 0.001
MAX_TRADES_PER_RUN     = 40

# -- Dust protection --------------------------------------
DUST_POSITION_PCT      = 0.0005
MIN_REMAINING_PCT      = 0.001

# -- Anti-overtrading -------------------------------------
TRADE_COOLDOWN_MINUTES = 5
MIN_HOLD_HOURS         = 1

# -- Timing -----------------------------------------------
SLEEP_SECONDS          = 1050
OPENING_BURST_SECONDS  = 60
OPENING_BURST_DURATION = 5 * 60

# -- Fundamental filters ----------------------------------
EARNINGS_BLACKOUT_DAYS = 3      # skip if earnings within 3 days

# -- Market regime ----------------------------------------
BULL_REGIME_THRESHOLD  = 0.02   # SPY > 2% above 50MA = bull
BEAR_REGIME_MAX_POS    = 8      # defensive in bear market
BULL_REGIME_MAX_POS    = MAX_POSITIONS

# -- Claude -----------------------------------------------
TOP_N_FOR_CLAUDE       = 5      # was 8 — fewer chart images = cheaper call
CHART_LOOKBACK_DAYS    = 60

# Skip the (expensive) Claude Vision call when the best local score is
# below this — most weak days then cost $0. App-adjustable.
MIN_SCORE_FOR_CLAUDE   = 25.0


def live_min_score() -> float:
    """App-adjustable: skip Claude if best local bullish score < this.
    Read from apex_settings.json each cycle. Falls back to the constant."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = float(json.load(_f).get("LONG", {}).get("min_score"))
        if _v >= 0:
            return _v
    except Exception:
        pass
    return MIN_SCORE_FOR_CLAUDE
CLAUDE_MODEL           = "claude-haiku-4-5-20251001"
MAX_TOKENS             = 800

# -- Files ------------------------------------------------
STATE_FILE    = "longv2_state.json"
LOG_FILE      = "longv2_trade_log.jsonl"
ANALYSIS_FILE = "longv2_analysis.txt"
CHART_DIR     = "longv2_charts"
UNIVERSE_FILE = "longbot_universe.txt"   # managed by universe_manager.py


# =========================================================
# SETUP
# =========================================================

load_dotenv()
os.makedirs(CHART_DIR, exist_ok=True)

anthropic_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
trading_client   = TradingClient(
    os.getenv("ALPACA_API_KEY_LONG", os.getenv("ALPACA_API_KEY")),
    os.getenv("ALPACA_SECRET_KEY_LONG", os.getenv("ALPACA_SECRET_KEY")),
    paper=True
)


# =========================================================
# STATE
# =========================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "last_trade_times":  {},
            "known_buy_times":   {},
            "entry_prices":      {},
            "trailing_stops":    {},   # ticker -> current stop price
            "peak_profits":      {},   # ticker -> best profit % seen
            "trades_today":      0,
            "last_trade_date":   None,
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


def record_trade(state, ticker):
    state["last_trade_times"][ticker] = datetime.now(timezone.utc).isoformat()
    state["trades_today"] += 1


def record_buy(state, ticker, entry_price, atr):
    now = datetime.now(timezone.utc).isoformat()
    state["known_buy_times"][ticker] = now
    state["entry_prices"][ticker]    = entry_price
    # Initial stop = entry - ATR * multiplier (below entry = loss for long)
    state["trailing_stops"][ticker]  = round(entry_price - atr * ATR_STOP_MULTIPLIER, 4)
    state["peak_profits"][ticker]    = 0.0
    record_trade(state, ticker)


def record_sell(state, ticker):
    for d in ["known_buy_times", "entry_prices", "trailing_stops", "peak_profits"]:
        state[d].pop(ticker, None)
    record_trade(state, ticker)


def in_cooldown(state, ticker) -> bool:
    last = state["last_trade_times"].get(ticker)
    if not last:
        return False
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)
            < timedelta(minutes=TRADE_COOLDOWN_MINUTES))


def held_too_recently(state, ticker) -> bool:
    t = state["known_buy_times"].get(ticker)
    if not t:
        return False
    return (datetime.now(timezone.utc) - datetime.fromisoformat(t)
            < timedelta(hours=MIN_HOLD_HOURS))


# =========================================================
# MARKET HOURS
# =========================================================

def is_market_open() -> bool:
    return trading_client.get_clock().is_open


def seconds_since_open():
    clock = trading_client.get_clock()
    if not clock.is_open:
        return None
    market_open = clock.next_close - timedelta(hours=6, minutes=30)
    return (clock.timestamp - market_open).total_seconds()


def get_sleep_seconds() -> int:
    elapsed = seconds_since_open()
    if elapsed is not None and elapsed < OPENING_BURST_DURATION:
        return OPENING_BURST_SECONDS
    return SLEEP_SECONDS


# =========================================================
# PORTFOLIO
# =========================================================

def get_portfolio() -> dict:
    account   = trading_client.get_account()
    positions = trading_client.get_all_positions()
    pv        = float(account.portfolio_value)

    pos_dict = {}
    for p in positions:
        mv = float(p.market_value)
        pos_dict[p.symbol] = {
            "qty":             float(p.qty),
            "market_value":    mv,
            "weight":          mv / pv if pv > 0 else 0,
            "avg_entry_price": float(p.avg_entry_price),
            "current_price":   float(p.current_price) if hasattr(p, "current_price") else 0,
            "unrealized_pl":   float(p.unrealized_pl),
        }

    return {
        "cash":            float(account.cash),
        "buying_power":    float(account.buying_power),
        "portfolio_value": pv,
        "positions":       pos_dict,
    }


# =========================================================
# LAYER 1  -  INDICATORS  (pure numpy, zero API cost)
# =========================================================

def _ema(series: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros_like(series, dtype=float)
    k   = 2.0 / (period + 1)
    out[0] = series[0]
    for i in range(1, len(series)):
        out[i] = series[i] * k + out[i - 1] * (1 - k)
    return out


def calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    d = np.diff(closes)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag, al = g[-period:].mean(), l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def calc_macd(closes: np.ndarray) -> tuple:
    if len(closes) < 26:
        return 0.0, 0.0, 0.0
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd  = ema12 - ema26
    sig   = _ema(macd, 9)
    hist  = macd - sig
    return round(float(macd[-1]), 4), round(float(sig[-1]), 4), round(float(hist[-1]), 4)


def calc_bollinger(closes: np.ndarray, period: int = 20) -> tuple:
    if len(closes) < period:
        c = closes[-1]
        return c, c, c, 0.5
    w   = closes[-period:]
    mid = w.mean()
    std = w.std()
    up  = mid + 2 * std
    lo  = mid - 2 * std
    pb  = (closes[-1] - lo) / (up - lo) if (up - lo) > 0 else 0.5
    return round(up, 4), round(mid, 4), round(lo, 4), round(pb, 4)


def calc_atr(highs, lows, closes, period: int = 14) -> float:
    if len(highs) < period + 1:
        return float(highs[-1] - lows[-1])
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]),
                   np.abs(lows[1:]  - closes[:-1]))
    )
    return round(float(tr[-period:].mean()), 4)


def calc_vwap(highs, lows, closes, volumes) -> float:
    tp  = (highs + lows + closes) / 3
    tv  = volumes.sum()
    if tv == 0:
        return float(closes[-1])
    return round(float((tp * volumes).sum() / tv), 4)


def calc_stochastic(highs, lows, closes, period=14) -> tuple:
    if len(closes) < period:
        return 50.0, 50.0
    lo  = lows[-period:].min()
    hi  = highs[-period:].max()
    rng = hi - lo
    k   = (closes[-1] - lo) / rng * 100 if rng > 0 else 50.0
    return round(k, 2), round(k, 2)


def calc_zscore(closes: np.ndarray, window=20) -> float:
    if len(closes) < window:
        return 0.0
    w   = closes[-window:]
    std = w.std()
    if std == 0:
        return 0.0
    return round(float((closes[-1] - w.mean()) / std), 3)


# -- Bullish pattern detectors -----------------------------

def detect_golden_cross(closes: np.ndarray) -> bool:
    """MA20 crossed above MA50 in last 5 bars  -  bullish."""
    if len(closes) < 55:
        return False
    for i in range(-5, 0):
        ma20_now  = closes[i - 20:i].mean()
        ma50_now  = closes[i - 50:i].mean()
        ma20_prev = closes[i - 21:i - 1].mean()
        ma50_prev = closes[i - 51:i - 1].mean()
        if ma20_prev <= ma50_prev and ma20_now > ma50_now:
            return True
    return False


def detect_bullish_divergence(closes: np.ndarray, period=14) -> bool:
    """Price making lower lows but RSI making higher lows -> hidden strength."""
    if len(closes) < 40:
        return False
    rsi_recent = calc_rsi(closes[-20:], period)
    rsi_older  = calc_rsi(closes[-40:-20], period)
    price_lower = closes[-1] < closes[-20]
    rsi_higher  = rsi_recent > rsi_older
    return price_lower and rsi_higher


def detect_double_bottom(closes: np.ndarray, tolerance=0.03) -> bool:
    """Two troughs within tolerance separated by a peak  -  bullish reversal."""
    if len(closes) < 30:
        return False
    window = closes[-30:]
    troughs = []
    for i in range(1, len(window) - 1):
        if window[i] < window[i - 1] and window[i] < window[i + 1]:
            troughs.append((i, window[i]))
    if len(troughs) < 2:
        return False
    t1, t2 = troughs[-2][1], troughs[-1][1]
    return abs(t1 - t2) / max(t1, t2) < tolerance


def detect_falling_wedge(closes: np.ndarray) -> bool:
    """Lower lows but rate of decline slowing -> typically breaks upward."""
    if len(closes) < 20:
        return False
    loss_first  = (closes[-20] - closes[-10]) / closes[-20] if closes[-20] > 0 else 0
    loss_second = (closes[-10] - closes[-1])  / closes[-10] if closes[-10] > 0 else 0
    price_down  = closes[-1] < closes[-20]
    slowing     = loss_second < loss_first * 0.5
    return price_down and slowing and loss_first > 0


def detect_volume_confirmation(volumes: np.ndarray, closes: np.ndarray) -> bool:
    """Price rising on increasing volume -> strong bullish signal."""
    if len(volumes) < 10:
        return False
    recent_vol = volumes[-5:].mean()
    older_vol  = volumes[-10:-5].mean()
    price_up   = closes[-1] > closes[-5]
    vol_up     = recent_vol > older_vol * 1.2
    return price_up and vol_up


def detect_oversold_bounce(closes, rsi) -> bool:
    """RSI was oversold (<30) and is now recovering  -  mean-reversion long."""
    if len(closes) < 20:
        return False
    recent_low_rsi = calc_rsi(closes[-20:-5]) < 35
    recovering     = rsi > 40
    return recent_low_rsi and recovering


# =========================================================
# LAYER 1  -  FUNDAMENTALS
# =========================================================

def get_fundamentals(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
        return {
            "pe_ratio":        info.get("trailingPE"),
            "forward_pe":      info.get("forwardPE"),
            "revenue_growth":  info.get("revenueGrowth"),
            "debt_to_equity":  info.get("debtToEquity"),
            "short_float_pct": info.get("shortPercentOfFloat"),
            "profit_margins":  info.get("profitMargins"),
            "earnings_date":   str(info.get("earningsTimestamp", "")),
            "price_to_book":   info.get("priceToBook"),
            "sector":          info.get("sector", "Unknown"),
            "analyst_target":  info.get("targetMeanPrice"),
        }
    except Exception:
        return {}


def has_earnings_soon(fundamentals: dict) -> bool:
    ts = fundamentals.get("earnings_date", "")
    if not ts or ts in ("None", ""):
        return False
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return 0 <= (dt - datetime.now(timezone.utc)).days <= EARNINGS_BLACKOUT_DAYS
    except Exception:
        return False


def analyst_upside(fundamentals: dict, current_price: float) -> float:
    """% upside to analyst consensus target. Positive = buy signal."""
    target = fundamentals.get("analyst_target")
    if not target or current_price <= 0:
        return 0.0
    return round((float(target) / current_price - 1) * 100, 2)


# =========================================================
# LAYER 1  -  MARKET REGIME
# =========================================================

def get_market_regime() -> str:
    """
    BULL  = SPY > 50MA by threshold -> full position size allowed
    BEAR  = SPY < 50MA by threshold -> defensive, fewer positions
    NEUTRAL = in between
    """
    try:
        spy    = yf.Ticker("SPY").history(period="3mo", auto_adjust=True)
        if spy.empty or len(spy) < 50:
            return "NEUTRAL"
        closes = spy["Close"].values
        ma50   = closes[-50:].mean()
        last   = closes[-1]
        pct    = (last - ma50) / ma50
        if pct > BULL_REGIME_THRESHOLD:
            return "BULL"
        elif pct < -BULL_REGIME_THRESHOLD:
            return "BEAR"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


_spy_cache = {"t": 0.0, "ret": 0.0}


def _spy_month_ret() -> float:
    """SPY ~1-month % return, cached ~15 min (one yfinance call per run,
    not per ticker). Used for relative-strength scoring."""
    now = time.time()
    if now - _spy_cache["t"] < 900:
        return _spy_cache["ret"]
    try:
        spy = yf.Ticker("SPY").history(period="2mo", auto_adjust=True)
        c = spy["Close"].values
        r = float((c[-1] / c[-21] - 1) * 100) if len(c) >= 21 else 0.0
    except Exception:
        r = 0.0
    _spy_cache.update(t=now, ret=r)
    return r


# =========================================================
# LAYER 1  -  FULL STOCK ANALYSIS
# =========================================================

def analyse_stock(ticker: str) -> dict:
    """
    Fetch OHLCV + fundamentals, compute all indicators,
    assign a composite BULLISH score.
    Returns None if data insufficient or stock filtered.
    """
    try:
        hist = yf.Ticker(ticker).history(
            period=f"{CHART_LOOKBACK_DAYS + 20}d",
            auto_adjust=True
        )
        if hist.empty or len(hist) < 30:
            return None

        closes  = hist["Close"].values.astype(float)
        highs   = hist["High"].values.astype(float)
        lows    = hist["Low"].values.astype(float)
        volumes = hist["Volume"].values.astype(float)

        last_price = closes[-1]

        # -- Indicators --
        rsi                      = calc_rsi(closes)
        macd, sig, hist_val      = calc_macd(closes)
        bb_upper, bb_mid, bb_lower, pct_b = calc_bollinger(closes)
        atr                      = calc_atr(highs, lows, closes)
        vwap                     = calc_vwap(highs, lows, closes, volumes)
        stoch_k, stoch_d         = calc_stochastic(highs, lows, closes)
        zscore                   = calc_zscore(closes)

        ma20 = closes[-20:].mean() if len(closes) >= 20 else last_price
        ma50 = closes[-50:].mean() if len(closes) >= 50 else last_price
        pct_vs_ma20 = round((last_price / ma20 - 1) * 100, 2) if ma20 > 0 else 0
        pct_vs_ma50 = round((last_price / ma50 - 1) * 100, 2) if ma50 > 0 else 0

        weekly_ret  = round((last_price / closes[-5]  - 1) * 100, 2) if len(closes) >= 5  else 0
        monthly_ret = round((last_price / closes[-20] - 1) * 100, 2) if len(closes) >= 20 else 0
        volatility  = round(float(np.diff(np.log(closes[-20:])).std() * 100), 3)

        # -- Pattern flags --
        patterns = {
            "golden_cross":        detect_golden_cross(closes),
            "bullish_divergence":  detect_bullish_divergence(closes),
            "double_bottom":       detect_double_bottom(closes),
            "falling_wedge":       detect_falling_wedge(closes),
            "volume_confirmation": detect_volume_confirmation(volumes, closes),
            "oversold_bounce":     detect_oversold_bounce(closes, rsi),
        }
        pattern_count = sum(patterns.values())

        # -- Fundamentals --
        fundamentals = get_fundamentals(ticker)

        # -- Filter: skip earnings blackout --
        if has_earnings_soon(fundamentals):
            print(f"  [filter] {ticker}: earnings blackout")
            return None

        upside = analyst_upside(fundamentals, last_price)

        # -- Composite bullish score ----------------------
        # Higher = better long candidate
        score = 0.0

        # Positive momentum
        score += max(0, monthly_ret) * 0.30
        score += max(0, weekly_ret)  * 0.50

        # Oversold / undervalued (mean-reversion long)
        score += max(0, 40 - rsi)    * 0.40   # lower RSI = more oversold = more room to run
        score += max(0, 30 - stoch_k)* 0.20

        # Near lower Bollinger Band (potential bounce)
        score += max(0, 0.3 - pct_b) * 30

        # Below VWAP (buying opportunity)
        if last_price < vwap:
            score += 8

        # Negative z-score (underextended, room to grow)
        score += max(0, -zscore) * 6

        # MACD bullish (histogram positive and rising)
        score += max(0, hist_val * 100) * 0.50

        # Analyst upside
        score += max(0, upside) * 0.15

        # Pattern bonus
        score += pattern_count * 14

        # Fundamental bonus: profitable, growing revenue
        rv = fundamentals.get("revenue_growth")
        if rv and float(rv) > 0.10:
            score += 10    # >10% revenue growth
        pm = fundamentals.get("profit_margins")
        if pm and float(pm) > 0.15:
            score += 8     # >15% profit margin

        # -- Relative strength vs SPY (momentum quality overlay) ----
        # Reward names outperforming the market over the last ~month.
        rs = monthly_ret - _spy_month_ret()
        score += max(0, rs) * 0.45
        score += min(0, rs) * 0.15            # mild penalty for laggards

        # Clean-uptrend bonus: price above both MAs, MA20 above MA50.
        if last_price > ma20 > ma50:
            score += 12

        # Volatility multiplier  -  higher vol = bigger potential moves
        vol_mult = 1 + volatility * 0.06
        score    = round(float(score * vol_mult), 3)

        return {
            "ticker":        ticker,
            "price":         round(last_price, 2),
            "atr":           atr,
            "bullish_score": score,
            "indicators": {
                "rsi":           rsi,
                "macd_hist":     hist_val,
                "pct_b":         round(pct_b, 3),
                "stoch_k":       stoch_k,
                "zscore":        zscore,
                "pct_vs_ma20":   pct_vs_ma20,
                "pct_vs_ma50":   pct_vs_ma50,
                "weekly_ret":    weekly_ret,
                "monthly_ret":   monthly_ret,
                "volatility":    volatility,
                "vwap_vs_price": round(last_price - vwap, 3),
                "analyst_upside": upside,
            },
            "patterns":      patterns,
            "pattern_count": pattern_count,
            "fundamentals": {
                "pe_ratio":       fundamentals.get("pe_ratio"),
                "forward_pe":     fundamentals.get("forward_pe"),
                "revenue_growth": fundamentals.get("revenue_growth"),
                "profit_margins": fundamentals.get("profit_margins"),
                "short_float":    fundamentals.get("short_float_pct"),
                "debt_equity":    fundamentals.get("debt_to_equity"),
                "sector":         fundamentals.get("sector", "Unknown"),
                "analyst_upside": upside,
            },
            # Full OHLCV for chart generation (not sent to Claude as text)
            "_ohlcv": {
                "closes":   closes.tolist(),
                "highs":    highs.tolist(),
                "lows":     lows.tolist(),
                "volumes":  volumes.tolist(),
                "dates":    [str(d)[:10] for d in hist.index],
                "ma20":     [round(float(closes[max(0, i-19):i+1].mean()), 2)
                             for i in range(len(closes))],
                "ma50":     [round(float(closes[max(0, i-49):i+1].mean()), 2)
                             for i in range(len(closes))],
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
            }
        }

    except Exception as e:
        print(f"  [analyse] {ticker}: {e}")
        return None


def load_universe() -> list:
    """
    Load tickers from longbot_universe.txt (managed by universe_manager.py).
    Falls back to hardcoded ALL_UNIVERSE if file does not exist yet.
    """
    if not os.path.exists(UNIVERSE_FILE):
        print(f"  [universe] {UNIVERSE_FILE} not found  -  using built-in list ({len(ALL_UNIVERSE)} tickers)")
        return ALL_UNIVERSE
    tickers = []
    with open(UNIVERSE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tickers.append(line.upper().split()[0])
    tickers = list(dict.fromkeys(tickers))
    print(f"  [universe] Loaded {len(tickers)} tickers from {UNIVERSE_FILE}")
    return tickers


def scan_universe(regime: str) -> list:
    """Score all tickers from universe file, filter, sort by bullish score, return top N."""
    universe = load_universe()
    print(f"Scanning {len(universe)} tickers (regime: {regime})...")
    results = []
    for ticker in universe:
        d = analyse_stock(ticker)
        if d:
            results.append(d)

    results.sort(key=lambda x: x["bullish_score"], reverse=True)
    top = results[:TOP_N_FOR_CLAUDE]

    # Always include currently held positions so Claude can decide on them
    portfolio = get_portfolio()
    held = set(portfolio["positions"].keys())
    top_tickers = {c["ticker"] for c in top}
    for r in results:
        if r["ticker"] in held and r["ticker"] not in top_tickers:
            top.append(r)

    print(f"  -> {len(results)} valid | {len(top)} sent to Claude")
    if top:
        print(f"  -> #1: {top[0]['ticker']} "
              f"(score {top[0]['bullish_score']:.1f}, "
              f"{top[0]['pattern_count']} patterns)")
    return top


# =========================================================
# LAYER 2A  -  CHART GENERATION
# =========================================================

CS = {
    "bg":          "#060a0e",
    "panel":       "#0b0f16",
    "border":      "#18212e",
    "text":        "#d0d8e4",
    "muted":       "#4a5568",
    "green":       "#00f5c3",
    "red":         "#ff4d6d",
    "yellow":      "#ffe353",
    "purple":      "#7b8cff",
    "candle_up":   "#00f5c3",
    "candle_down": "#ff4d6d",
}


def generate_chart(data: dict) -> bytes:
    """
    4-panel dark chart:
      [0] Candlestick + MA20 + MA50 + Bollinger Bands + pattern labels
      [1] Volume + 10-bar MA
      [2] RSI with zones
      [3] MACD histogram + lines
    Returns PNG bytes.
    """
    ohlcv  = data["_ohlcv"]
    ticker = data["ticker"]
    n      = min(CHART_LOOKBACK_DAYS, len(ohlcv["closes"]))

    closes  = np.array(ohlcv["closes"][-n:])
    highs   = np.array(ohlcv["highs"][-n:])
    lows    = np.array(ohlcv["lows"][-n:])
    volumes = np.array(ohlcv["volumes"][-n:])
    ma20    = np.array(ohlcv["ma20"][-n:])
    ma50    = np.array(ohlcv["ma50"][-n:])
    dates   = ohlcv["dates"][-n:]
    bb_upper = ohlcv["bb_upper"]
    bb_lower = ohlcv["bb_lower"]
    x       = np.arange(n)

    # Recompute indicators for full history for smooth chart lines
    full = np.array(ohlcv["closes"])
    rsi_series = np.array([calc_rsi(full[:i+1]) for i in range(len(full))])[-n:]
    ema12      = _ema(full, 12)
    ema26      = _ema(full, 26)
    macd_l     = (ema12 - ema26)[-n:]
    sig_l      = _ema(ema12 - ema26, 9)[-n:]
    macd_h     = macd_l - sig_l

    fig = plt.figure(figsize=(14, 10), facecolor=CS["bg"])
    gs  = gridspec.GridSpec(4, 1, height_ratios=[5, 1.5, 1.2, 1.2],
                            hspace=0.04, left=0.06, right=0.97,
                            top=0.93, bottom=0.06)

    ax_p = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_p)
    ax_r = fig.add_subplot(gs[2], sharex=ax_p)
    ax_m = fig.add_subplot(gs[3], sharex=ax_p)

    for ax in [ax_p, ax_v, ax_r, ax_m]:
        ax.set_facecolor(CS["panel"])
        ax.tick_params(colors=CS["muted"], labelsize=7)
        ax.spines[:].set_color(CS["border"])
        ax.grid(color=CS["border"], linewidth=0.4, alpha=0.7)
        plt.setp(ax.get_xticklabels(), visible=False)
    plt.setp(ax_m.get_xticklabels(), visible=True, rotation=30, ha="right")

    # -- Candlestick --
    for i in range(n):
        o = closes[i - 1] if i > 0 else closes[i]
        c, h, l = closes[i], highs[i], lows[i]
        color = CS["candle_up"] if c >= o else CS["candle_down"]
        ax_p.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.8)
        body  = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.1
        ax_p.bar(i, body, bottom=min(c, o), width=0.6,
                 color=color, alpha=0.9, linewidth=0)

    # -- MAs + Bollinger --
    ax_p.plot(x, ma20, color=CS["yellow"], linewidth=1.2, label="MA20", alpha=0.9)
    ax_p.plot(x, ma50, color=CS["purple"], linewidth=1.2, label="MA50", alpha=0.9)
    bb_up_a = np.full(n, bb_upper)
    bb_lo_a = np.full(n, bb_lower)
    ax_p.plot(x, bb_up_a, color=CS["muted"], linewidth=0.8, linestyle="--", alpha=0.6)
    ax_p.plot(x, bb_lo_a, color=CS["muted"], linewidth=0.8, linestyle="--",
              alpha=0.6, label="BB")
    ax_p.fill_between(x, bb_up_a, bb_lo_a, color=CS["muted"], alpha=0.04)

    # -- Trailing stop line if held --
    # (we don't have state here, but we can show entry price if available)

    # -- Pattern annotations --
    pats = data["patterns"]
    labels = []
    if pats.get("golden_cross"):        labels.append("[STAR] Golden Cross")
    if pats.get("bullish_divergence"):  labels.append(">/> Bull Div.")
    if pats.get("double_bottom"):       labels.append("W Double Bottom")
    if pats.get("falling_wedge"):       labels.append("[TRI] Falling Wedge")
    if pats.get("volume_confirmation"): labels.append("[UP] Vol Confirm")
    if pats.get("oversold_bounce"):     labels.append("[~] Oversold Bounce")
    if labels:
        ax_p.annotate(
            "  ".join(labels),
            xy=(n * 0.02, closes.min() * 0.99),
            fontsize=7.5, color=CS["green"], fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc=CS["panel"],
                      ec=CS["green"], alpha=0.85)
        )

    ax_p.set_ylabel("Price $", color=CS["muted"], fontsize=8)
    ax_p.tick_params(axis="y", colors=CS["muted"])
    ax_p.legend(loc="upper left", fontsize=7, framealpha=0.3,
                labelcolor=CS["text"], facecolor=CS["panel"])

    ind = data["indicators"]
    fig.suptitle(
        f"{ticker}   ${data['price']:.2f}   "
        f"Score: {data['bullish_score']:.1f}   "
        f"RSI: {ind['rsi']:.0f}   "
        f"ATR: {data['atr']:.2f}   "
        f"Analyst ^: {ind['analyst_upside']:+.1f}%",
        color=CS["text"], fontsize=11, fontweight="bold", y=0.975,
        bbox=dict(boxstyle="round", fc=CS["panel"], ec=CS["border"], alpha=0.9)
    )

    # -- Volume --
    vcols = [CS["candle_up"] if closes[i] >= (closes[i-1] if i > 0 else closes[i])
             else CS["candle_down"] for i in range(n)]
    ax_v.bar(x, volumes, color=vcols, alpha=0.55, width=0.8)
    vol_ma = np.array([volumes[max(0, i-9):i+1].mean() for i in range(n)])
    ax_v.plot(x, vol_ma, color=CS["yellow"], linewidth=0.9, alpha=0.8)
    ax_v.set_ylabel("Vol", color=CS["muted"], fontsize=7)
    ax_v.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K")
    )

    # -- RSI --
    ax_r.plot(x, rsi_series, color=CS["purple"], linewidth=1.2)
    ax_r.axhline(70, color=CS["red"],   linewidth=0.7, linestyle="--", alpha=0.7)
    ax_r.axhline(30, color=CS["green"], linewidth=0.7, linestyle="--", alpha=0.7)
    ax_r.axhline(50, color=CS["muted"], linewidth=0.5, linestyle=":", alpha=0.5)
    ax_r.fill_between(x, rsi_series, 30,
                      where=(rsi_series <= 30),
                      color=CS["green"], alpha=0.15)   # oversold zone highlighted green for longs
    ax_r.set_ylim(0, 100)
    ax_r.set_ylabel("RSI", color=CS["muted"], fontsize=7)
    ax_r.yaxis.set_ticks([30, 50, 70])

    # -- MACD --
    bar_cols = [CS["green"] if v >= 0 else CS["red"] for v in macd_h]
    ax_m.bar(x, macd_h, color=bar_cols, alpha=0.7, width=0.8)
    ax_m.plot(x, macd_l, color=CS["yellow"], linewidth=1.0, label="MACD")
    ax_m.plot(x, sig_l,  color=CS["purple"], linewidth=1.0, label="Signal")
    ax_m.axhline(0, color=CS["muted"], linewidth=0.5)
    ax_m.set_ylabel("MACD", color=CS["muted"], fontsize=7)
    ax_m.legend(loc="upper left", fontsize=6, framealpha=0.2,
                labelcolor=CS["text"], facecolor=CS["panel"])

    tick_step = max(1, n // 8)
    tick_pos  = list(range(0, n, tick_step))
    ax_m.set_xticks(tick_pos)
    ax_m.set_xticklabels([dates[i] for i in tick_pos],
                          color=CS["muted"], fontsize=7)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=85,
                facecolor=CS["bg"], bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def chart_to_b64(png: bytes) -> str:
    return base64.standard_b64encode(png).decode("utf-8")


# =========================================================
# LAYER 2B  -  CLAUDE VISION
# =========================================================

def build_compact_table(candidates: list) -> str:
    lines = ["TICKER | SCORE | RSI | MACD_H | %B | ZSCORE | W_RET | M_RET | ANA_UP | PATTERNS"]
    lines.append("-" * 90)
    for c in candidates:
        i   = c["indicators"]
        pat = [k for k, v in c["patterns"].items() if v]
        lines.append(
            f"{c['ticker']:6s} | {c['bullish_score']:6.1f} | "
            f"{i['rsi']:5.1f} | {i['macd_hist']:+6.4f} | "
            f"{i['pct_b']:4.2f} | {i['zscore']:+5.2f} | "
            f"{i['weekly_ret']:+5.1f}% | {i['monthly_ret']:+5.1f}% | "
            f"{i['analyst_upside']:+5.1f}% | "
            f"{', '.join(pat) if pat else ' - '}"
        )
    return "\n".join(lines)


def ask_claude_vision(candidates: list, portfolio: dict,
                      regime: str, charts: dict) -> dict:
    held = portfolio["positions"]
    max_pos = BEAR_REGIME_MAX_POS if regime == "BEAR" else BULL_REGIME_MAX_POS

    content = []

    content.append({
        "type": "text",
        "text": (
            f"You are a LONG-ONLY portfolio manager using technical + fundamental analysis.\n"
            f"Market regime: {regime}.\n"
            f"Analyse the charts below for bullish setups, then return a JSON allocation.\n\n"
            f"CURRENT HOLDINGS:\n"
            f"{json.dumps({k: {'weight': round(v['weight'], 3), 'pl': v['unrealized_pl'], 'entry': v['avg_entry_price']} for k, v in held.items()}, indent=2)}\n\n"
            f"NUMERIC SUMMARY:\n{build_compact_table(candidates)}\n\n"
            f"CHARTS:"
        )
    })

    for c in candidates:
        t = c["ticker"]
        if t in charts:
            content.append({"type": "text", "text": f"\n--- {t} ---"})
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": "image/png",
                    "data":       charts[t],
                }
            })

    content.append({
        "type": "text",
        "text": (
            f"\nReturn ONLY valid JSON. No markdown.\n\n"
            f'{{"decision":"ALLOCATE",'
            f'"target_portfolio":{{"TICKER":0.15,"CASH":0.03}},'
            f'"confidence":0.0,'
            f'"short_analysis":"one sentence",'
            f'"pattern_notes":{{"TICKER":"pattern seen"}}}}\n\n'
            f"Rules:\n"
            f"- decision: ALLOCATE or HOLD\n"
            f"- Only long tickers from the candidate list\n"
            f"- Max {max_pos} positions (regime is {regime})\n"
            f"- Max single stock: {MAX_SINGLE_WEIGHT}, Max ETF: {MAX_ETF_WEIGHT}\n"
            f"- Min CASH: {MIN_CASH_WEIGHT}\n"
            f"- Weights sum to ~1.0\n"
            f"- Prefer: RSI oversold + bullish pattern + positive revenue growth\n"
            f"- Prefer adding to existing winners, avoid unnecessary turnover\n"
            f"- In BEAR regime: be defensive, prefer ETFs or cash-heavy\n"
            f"- In BULL regime: be aggressive, tilt toward momentum\n"
            f"- confidence between 0 and 1\n"
            f"- If no strong setup: HOLD"
        )
    })

    response = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": content}]
    )

    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    print("RAW CLAUDE:", raw[:500])

    try:
        return json.loads(raw)
    except Exception as e:
        print(f"JSON parse error: {e}")
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": 0.0,
                "short_analysis": "JSON error.", "pattern_notes": {}}


# =========================================================
# SIGNAL VALIDATION
# =========================================================

def validate_signal(signal: dict, candidates: list, regime: str) -> dict:
    confidence = float(signal.get("confidence", 0))
    mc = live_min_confidence()
    mp = live_min_positions()

    weak = (signal.get("decision") != "ALLOCATE") or (confidence < mc)
    floor_on = mp > 0 and len(candidates) > 0

    # Fully cautious only when no floor is set: respect the AI's HOLD.
    if weak and not floor_on:
        why = ("Non-ALLOCATE." if signal.get("decision") != "ALLOCATE"
               else f"Confidence {confidence:.2f} < {mc:.2f}.")
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": confidence,
                "short_analysis": why, "pattern_notes": {}}

    floor_n = mp if mp > 0 else MIN_POSITIONS

    valid  = {c["ticker"] for c in candidates}
    # Weak signal + floor set -> ignore the AI's (empty) target and let the
    # padding below deploy the top-ranked candidates instead of holding cash.
    target = {} if weak else signal.get("target_portfolio", {})
    cleaned = {}

    for ticker, weight in target.items():
        try:
            weight = float(weight)
        except Exception:
            continue
        if weight <= 0:
            continue
        ticker = ticker.upper()
        if ticker == "CASH":
            cleaned["CASH"] = max(weight, MIN_CASH_WEIGHT)
            continue
        if ticker not in valid:
            continue
        cap = MAX_ETF_WEIGHT if ticker in ETF_UNIVERSE else MAX_SINGLE_WEIGHT
        cleaned[ticker] = min(weight, cap)

    cleaned["CASH"] = max(cleaned.get("CASH", MIN_CASH_WEIGHT), MIN_CASH_WEIGHT)

    max_pos  = BEAR_REGIME_MAX_POS if regime == "BEAR" else BULL_REGIME_MAX_POS
    non_cash = sorted(
        [(k, v) for k, v in cleaned.items() if k != "CASH"],
        key=lambda x: x[1], reverse=True
    )[:max_pos]

    if len(non_cash) < floor_n:
        existing = {k for k, _ in non_cash}
        for c in candidates:
            if c["ticker"] not in existing:
                non_cash.append((c["ticker"], 1.0 / floor_n))
                existing.add(c["ticker"])
                if len(non_cash) >= floor_n:
                    break

    cleaned = dict(non_cash)
    cleaned["CASH"] = max(cleaned.get("CASH", MIN_CASH_WEIGHT), MIN_CASH_WEIGHT)
    total   = sum(cleaned.values())

    if total <= 0:
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": confidence,
                "short_analysis": "Empty after validation.", "pattern_notes": {}}

    cleaned = {k: round(v / total, 4) for k, v in cleaned.items()}
    n_pos = len([k for k in cleaned if k != "CASH"])
    signal["decision"]         = "ALLOCATE"
    signal["target_portfolio"] = cleaned
    signal["confidence"]       = confidence
    if weak:
        signal["short_analysis"] = (
            f"AI cautious (conf {confidence:.2f} < {mc:.2f}); deploying "
            f"baseline of top {n_pos} ranked names (min-positions floor={mp}).")
    signal.setdefault("pattern_notes", {})
    return signal


# =========================================================
# LAYER 3  -  TRAILING STOP ENGINE
# =========================================================

def update_trailing_stops(portfolio: dict, state: dict,
                          candidate_data: dict) -> list:
    """
    For each long position:
    1. Update peak profit
    2. Once profitable by MIN_PROFIT_TO_TRAIL, trail the stop upward
    3. If current price <= trailing stop -> sell immediately
    4. Time stop: sell if held TIME_STOP_DAYS with < 1% gain
    """
    sell_actions = []

    for ticker, pos in list(portfolio["positions"].items()):
        entry   = float(state["entry_prices"].get(ticker, pos["avg_entry_price"]))
        current = float(pos["current_price"]) if pos["current_price"] > 0 else entry
        atr     = float(candidate_data.get(ticker, {}).get("atr", entry * 0.02))

        profit_pct = (current - entry) / entry if entry > 0 else 0

        # Update peak
        peak = float(state["peak_profits"].get(ticker, 0))
        if profit_pct > peak:
            state["peak_profits"][ticker] = profit_pct
            peak = profit_pct

        # Tighten trailing stop once profitable
        if peak >= MIN_PROFIT_TO_TRAIL:
            new_stop = current - atr * ATR_STOP_MULTIPLIER
            old_stop = float(state["trailing_stops"].get(
                ticker, entry - atr * ATR_STOP_MULTIPLIER
            ))
            # For longs: only move stop UP (lock in more profit)
            if new_stop > old_stop:
                state["trailing_stops"][ticker] = round(new_stop, 4)

        stop = float(state["trailing_stops"].get(
            ticker, entry - atr * ATR_STOP_MULTIPLIER
        ))

        # -- Check stop triggered --
        if current <= stop:
            try:
                print(f"  [stop] Selling {ticker}: "
                      f"price ${current:.2f} <= stop ${stop:.2f}")
                trading_client.close_position(ticker)
                sell_actions.append(
                    f"TRAILING STOP SELL {ticker} "
                    f"(price ${current:.2f} <= stop ${stop:.2f}, "
                    f"peak profit {peak:.1%})"
                )
                record_sell(state, ticker)
                time.sleep(1)
            except Exception as e:
                sell_actions.append(f"FAILED STOP SELL {ticker}: {e}")
            continue

        # -- Time stop --
        buy_time = state["known_buy_times"].get(ticker)
        if buy_time:
            days_held = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(buy_time)).days
            if days_held >= TIME_STOP_DAYS and profit_pct < 0.01:
                try:
                    print(f"  [time stop] Selling {ticker}: "
                          f"{days_held}d, only {profit_pct:.1%} gain")
                    trading_client.close_position(ticker)
                    sell_actions.append(
                        f"TIME STOP SELL {ticker} "
                        f"({days_held}d held, {profit_pct:.1%} gain)"
                    )
                    record_sell(state, ticker)
                    time.sleep(1)
                except Exception as e:
                    sell_actions.append(f"FAILED TIME STOP {ticker}: {e}")

    return sell_actions


# =========================================================
# LAYER 3  -  EXECUTION
# =========================================================

def execute_allocation(signal: dict, portfolio: dict,
                       state: dict, candidate_data: dict) -> str:
    if signal.get("decision") != "ALLOCATE":
        return "HOLD  -  no allocation."

    target    = signal.get("target_portfolio", {})
    pv        = portfolio["portfolio_value"]
    positions = portfolio["positions"]
    actions   = []
    trades    = 0

    min_order  = pv * MIN_ORDER_PCT
    dust       = pv * DUST_POSITION_PCT
    min_remain = pv * MIN_REMAINING_PCT

    cur_weights = {t: p["weight"] for t, p in positions.items()}
    cur_weights["CASH"] = portfolio["cash"] / pv if pv > 0 else 0
    all_tickers = set(cur_weights) | set(target)

    # -- 1. SELL positions being reduced -------------------
    for ticker in all_tickers:
        if ticker == "CASH" or trades >= MAX_TRADES_PER_RUN:
            break

        cur_w = cur_weights.get(ticker, 0)
        tgt_w = target.get(ticker, 0)

        if tgt_w - cur_w >= -MIN_REBALANCE_DELTA:
            continue
        if ticker not in positions:
            continue
        if in_cooldown(state, ticker) or held_too_recently(state, ticker):
            continue

        try:
            position = trading_client.get_open_position(ticker)
            qty      = float(position.qty)
            mv       = float(positions[ticker]["market_value"])

            if qty <= 0 or mv <= 0:
                continue

            tgt_val   = pv * tgt_w
            to_sell   = mv - tgt_val

            if to_sell < min_order:
                continue

            if tgt_val < min_remain or (mv - to_sell) < dust:
                # Full exit using close_position (no dust shares)
                trading_client.close_position(ticker)
                actions.append(f"SELL ALL {ticker} -> target {tgt_w:.1%}")
                record_sell(state, ticker)
            else:
                frac         = min(to_sell / mv, 1.0)
                qty_to_sell  = math.floor(qty * frac * 1_000_000) / 1_000_000
                if qty_to_sell <= 0:
                    continue
                order = MarketOrderRequest(
                    symbol=ticker, qty=str(qty_to_sell),
                    side=OrderSide.SELL, time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order)
                actions.append(f"SELL {qty_to_sell:.4f} {ticker} -> {tgt_w:.1%}")
                record_trade(state, ticker)

            trades += 1
            time.sleep(1)

        except Exception as e:
            actions.append(f"FAILED SELL {ticker}: {e}")

    time.sleep(5)

    # Refresh after sells
    updated  = get_portfolio()
    exposure = updated["portfolio_value"] - updated["cash"]
    max_exp  = updated["portfolio_value"] * (1 - MIN_CASH_WEIGHT)
    eff_bp   = min(updated["buying_power"], max(0, max_exp - exposure))

    # -- 2. BUY new / increased positions ------------------
    for ticker in all_tickers:
        if ticker == "CASH" or trades >= MAX_TRADES_PER_RUN:
            break

        cur_w = cur_weights.get(ticker, 0)
        tgt_w = target.get(ticker, 0)

        if tgt_w - cur_w <= MIN_REBALANCE_DELTA:
            continue
        if in_cooldown(state, ticker):
            continue

        dollars = min(pv * (tgt_w - cur_w), eff_bp)
        dollars = round(dollars, 2)
        if dollars < min_order:
            continue

        try:
            # Use notional for buys (fractional shares, no price lookup needed)
            order = MarketOrderRequest(
                symbol=ticker, notional=dollars,
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY
            )
            trading_client.submit_order(order)

            # Fetch price for stop calculation
            atr   = float(candidate_data.get(ticker, {}).get("atr", 0))
            price = float(candidate_data.get(ticker, {}).get("price", 0))
            if price <= 0:
                try:
                    price = float(yf.Ticker(ticker).fast_info.last_price)
                except Exception:
                    price = 0
            if atr <= 0 and price > 0:
                atr = price * 0.02

            actions.append(
                f"BUY ${dollars:.0f} of {ticker} -> {tgt_w:.1%}"
                + (f" | ATR stop ${price - atr * ATR_STOP_MULTIPLIER:.2f}"
                   if price > 0 else "")
            )
            if price > 0 and atr > 0:
                record_buy(state, ticker, price, atr)
            else:
                record_trade(state, ticker)
                state["known_buy_times"][ticker] = datetime.now(timezone.utc).isoformat()

            eff_bp -= dollars
            trades += 1
            time.sleep(1)

        except Exception as e:
            actions.append(f"FAILED BUY {ticker}: {e}")

    return " | ".join(actions) if actions else "No trades needed."


# =========================================================
# LOGGING
# =========================================================

def write_outputs(signal, action, before, after, state, regime, charts_saved):
    pv_b   = before.get("portfolio_value", 0)
    pv_a   = after.get("portfolio_value", 0)
    change = round(pv_a - pv_b, 4)

    log = {
        "time":             datetime.now(timezone.utc).isoformat(),
        "regime":           regime,
        "signal":           signal,
        "action":           action,
        "portfolio_before": before,
        "portfolio_after":  after,
        "state":            state,
        "change":           change,
        "charts_generated": charts_saved,
    }

    text = (
        f"\nTIME: {datetime.now()}\n"
        f"REGIME: {regime}\n\n"
        f"SIGNAL:\n{json.dumps(signal, indent=2)}\n\n"
        f"ACTION:\n{action}\n\n"
        f"CHARTS: {charts_saved}\n\n"
        f"PORTFOLIO: ${pv_b:,.2f} -> ${pv_a:,.2f}  ({change:+,.2f})\n"
    )

    with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log) + "\n")

    print(text)


# =========================================================
# MAIN
# =========================================================

def run_once():
    state  = load_state()
    state  = reset_daily(state)
    before = get_portfolio()
    pv     = before["portfolio_value"]

    print(f"\n  Portfolio: ${pv:,.2f} | Cash: ${before['cash']:,.2f} | "
          f"Positions: {len(before['positions'])}")

    regime = get_market_regime()
    print(f"  Market regime: {regime}")

    candidates     = scan_universe(regime)
    candidate_data = {c["ticker"]: c for c in candidates}

    if not candidates:
        print("  No valid candidates  -  skipping.")
        return

    # -- Trailing stops first --
    print("  Checking trailing stops...")
    stop_actions = update_trailing_stops(before, state, candidate_data)
    if stop_actions:
        print(f"  Stop actions: {stop_actions}")

    # -- Cost gate: only call Claude Vision when a setup is strong --
    best_score = max((c["bullish_score"] for c in candidates), default=0.0)
    _mins = live_min_score()
    if best_score < _mins:
        print(f"  Best score {best_score:.1f} < {_mins:.1f}  -  "
              f"skipping Claude Vision (saved API cost)")
        signal = validate_signal({"decision": "HOLD", "confidence": 0.0},
                                  candidates, regime)
    else:
        print(f"  Generating {len(candidates)} charts...")
        charts = {}
        saved  = []
        for c in candidates:
            try:
                png = generate_chart(c)
                charts[c["ticker"]] = chart_to_b64(png)
                path = os.path.join(CHART_DIR, f"{c['ticker']}.png")
                with open(path, "wb") as f:
                    f.write(png)
                saved.append(c["ticker"])
            except Exception as e:
                print(f"  [chart] {c['ticker']}: {e}")
        print(f"  Charts ready: {saved}")
        print("  Calling Claude Vision...")
        raw_signal = ask_claude_vision(candidates, before, regime, charts)
        signal     = validate_signal(raw_signal, candidates, regime)

    print(f"  Decision: {signal['decision']} | "
          f"Confidence: {signal.get('confidence', 0):.0%}")
    for t, note in (signal.get("pattern_notes") or {}).items():
        print(f"    {t}: {note}")

    action = execute_allocation(signal, before, state, candidate_data)
    if stop_actions:
        action = " | ".join(stop_actions + [action])

    after = get_portfolio()
    save_state(state)
    write_outputs(signal, action, before, after, state, regime, saved)


def main():
    print("=" * 65)
    print("APEX LONG BOT v2   -  Vision + Technical Analysis")
    print(f"Model:    {CLAUDE_MODEL}")
    print(f"Universe: {UNIVERSE_FILE} -> top {TOP_N_FOR_CLAUDE} to Claude")
    print(f"Stops:    ATRx{ATR_STOP_MULTIPLIER} trailing | {TIME_STOP_DAYS}d time stop")
    print(f"Regime:   BULL={BULL_REGIME_MAX_POS} pos | BEAR={BEAR_REGIME_MAX_POS} pos")
    print("=" * 65)

    while True:
        try:
            clock = trading_client.get_clock()

            if clock.is_open:
                elapsed = seconds_since_open()
                mode    = ("BURST" if elapsed is not None
                           and elapsed < OPENING_BURST_DURATION else "NORMAL")
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                      f"Market OPEN  -  {mode}")
                run_once()
                s = get_sleep_seconds()
                print(f"Sleeping {s}s...")
                time.sleep(s)

            else:
                nxt = clock.next_open.astimezone().strftime("%a %b %d %H:%M")
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
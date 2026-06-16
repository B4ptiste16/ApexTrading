# -*- coding: utf-8 -*-
"""
APEX SHORT BOT v2
-----------------------------------------------------------------
Architecture:
  Layer 1  -  Data & Indicators   (free, yfinance + numpy)
    * OHLCV fetch for all candidates
    * RSI, MACD, Bollinger Bands, ATR, VWAP, Stochastic
    * Pattern detection: death cross, bearish divergence,
      double top, rising wedge, volume confirmation
    * Fundamental filter: P/E, revenue growth, short float,
      debt/equity, earnings proximity blackout
    * Market regime: SPY vs 50-day MA (no shorts in strong bull)
    * Z-score extension from mean
    * Pre-filter to top 8 candidates -> tiny Claude prompt

  Layer 2  -  Claude Vision + JSON (Haiku, minimal tokens)
    * Receives chart IMAGES + compact numeric table
    * Identifies patterns visually, assigns weights + thesis
    * Returns structured JSON allocation

  Layer 3  -  Execution (Alpaca paper)
    * ATR-based trailing stops (not fixed %)
    * Time-based stop (cover if no movement after N days)
    * Cover before short, dust protection, cooldowns

Cost per run: ~$0.005-0.015  (Haiku + small images)
-----------------------------------------------------------------
"""

print("Booting APEX SHORT bot - loading libraries, first start can take 1-2 min...", flush=True)

import os
import io
import json
import math
import time
import base64
import warnings
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")   # non-interactive backend  -  no display needed
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch
import matplotlib.dates as mdates

from dotenv import load_dotenv
from core.ai_client import call_ai_vision, call_ai_text, load_ai_config

from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from core.broker_client import get_broker_client

warnings.filterwarnings("ignore")


# =========================================================
# CONFIG
# =========================================================

SHORT_UNIVERSE = [
    # High-beta tech prone to sharp reversals
    "NVDA", "AMD", "TSLA", "META", "NFLX", "SNOW", "SHOP",
    "PLTR", "COIN", "HOOD", "MSTR", "ROKU", "SPOT",
    # Speculative
    "RIVN", "LCID", "SOFI", "UPST", "AFRM", "SOUN", "BBAI",
    "IONQ", "RGTI", "QBTS", "RKLB", "ASTS",
    # Crypto-linked
    "MARA", "RIOT", "CLSK", "HUT", "BITF",
    # Overextended consumer
    "ABNB", "UBER", "LYFT", "DASH", "W", "ETSY",
    # Biotech
    "SRPT", "ACAD", "EXAS", "FATE", "BEAM",
    # Solar / clean energy
    "ENPH", "SEDG", "FSLR", "RUN", "PLUG", "BE",
    # China ADRs
    "BABA", "PDD", "JD", "BIDU", "NIO", "XPEV", "LI",
]

SHORT_ETF_UNIVERSE = [
    "ARKK", "KWEB", "XBI", "ICLN", "SMH",
]

ALL_SHORTABLE = SHORT_UNIVERSE + SHORT_ETF_UNIVERSE

# -- Portfolio rules --------------------------------------
MIN_CONFIDENCE         = 0.65


def live_min_confidence() -> float:
    """App-adjustable confidence threshold. Read from apex_settings.json
    every decision cycle (no restart). Falls back to MIN_CONFIDENCE."""
    try:
        with open("apex_settings.json", "r", encoding="utf-8") as _f:
            _v = float(json.load(_f).get("SHORT", {}).get("min_confidence"))
        if 0.0 < _v <= 1.0:
            return _v
    except Exception:
        pass
    return MIN_CONFIDENCE
MIN_POSITIONS          = 3
MAX_POSITIONS          = 10
MAX_SINGLE_WEIGHT      = 0.18
MAX_ETF_WEIGHT         = 0.25
MIN_CASH_WEIGHT        = 0.10

# -- Stop-loss: ATR-based trailing ------------------------
ATR_STOP_MULTIPLIER    = 2.5    # cover if price moves 2.5x ATR against us
TIME_STOP_DAYS         = 7      # cover if position hasn't moved in our favour for 7 days
MIN_PROFIT_TO_TRAIL    = 0.03   # only start trailing after 3% profit

# -- Rebalancing ------------------------------------------
MIN_REBALANCE_DELTA    = 0.02
MIN_ORDER_PCT          = 0.005
MAX_TRADES_PER_RUN     = 18

# -- Dust -------------------------------------------------
DUST_POSITION_PCT      = 0.001
MIN_REMAINING_PCT      = 0.002

# -- Anti-overtrading -------------------------------------
TRADE_COOLDOWN_MINUTES = 12
MIN_HOLD_HOURS         = 2

# -- Timing -----------------------------------------------
SLEEP_SECONDS          = 1800
OPENING_BURST_SECONDS  = 120
OPENING_BURST_DURATION = 5 * 60

# -- Fundamental filters ----------------------------------
MAX_SHORT_FLOAT_PCT    = 0.30   # skip if >30% already shorted (squeeze risk)
EARNINGS_BLACKOUT_DAYS = 5      # skip stocks with earnings within 5 days

# -- Market regime ----------------------------------------
# If SPY > 50-day MA by this much, market is too bullish  -  be very selective
BULL_REGIME_THRESHOLD  = 0.03   # SPY > 3% above 50MA = bull regime
BEAR_REGIME_MAX_POS    = MAX_POSITIONS
BULL_REGIME_MAX_POS    = 4      # only 4 positions max in bull market

# -- Top candidates sent to Claude ------------------------
TOP_N_FOR_CLAUDE       = 8      # only 8 stocks -> tiny prompt, cheap
CHART_LOOKBACK_DAYS    = 60     # days of OHLCV shown in chart

# AI provider / model loaded from .env (AI_PROVIDER, AI_MODEL …)
MAX_TOKENS             = 800

# -- Files ------------------------------------------------
STATE_FILE    = "shortv2_state.json"
LOG_FILE      = "shortv2_trade_log.jsonl"
ANALYSIS_FILE = "shortv2_analysis.txt"
CHART_DIR     = "shortv2_charts"
UNIVERSE_FILE = os.environ.get(
    "APEX_BOT_UNIVERSE", "shortbot_universe.txt")  # managed by universe_manager.py
# V4.6.5 — APEX_BOT_UNIVERSE env var lets the bot tab override the path.


# =========================================================
# SETUP
# =========================================================

_apex_data_dir = os.environ.get("APEX_DATA_DIR") or str(
    __import__("pathlib").Path(
        os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or
        str(__import__("pathlib").Path.home())
    ) / "APEX Trading Platform"
)
load_dotenv(__import__("pathlib").Path(_apex_data_dir) / ".env", override=True)
load_dotenv(override=True)

# V4.6.109 — scope state/log/analysis/charts to THIS bot's per-broker instance
# dir (mirrors core.data.broker_data_dir) so the same user running SHORT on both
# Alpaca and IBKR — or two different users — never share one state file. The old
# RELATIVE paths resolved to the launch cwd (/opt/apex_bots on the server), so
# every instance wrote the SAME file and one bot's reconcile (seeing only its own
# broker's positions) corrupted another's open positions and win/loss tally.
_apex_broker = (os.environ.get("APEX_BROKER") or "alpaca").lower()
_data_root = __import__("pathlib").Path(_apex_data_dir)
if _apex_broker != "alpaca":
    _data_root = _data_root / _apex_broker
try:
    _data_root.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
STATE_FILE    = str(_data_root / "shortv2_state.json")
LOG_FILE      = str(_data_root / "shortv2_trade_log.jsonl")
ANALYSIS_FILE = str(_data_root / "shortv2_analysis.txt")
CHART_DIR     = str(_data_root / "shortv2_charts")
os.makedirs(CHART_DIR, exist_ok=True)

_AI_PROVIDER, _AI_MODEL, _AI_KEY, _AI_MODE = load_ai_config()
print(f"[ai] provider={_AI_PROVIDER}  model={_AI_MODEL}  mode={_AI_MODE}")

_ALPACA_IS_PAPER = (os.environ.get("APEX_ALPACA_MODE", "paper").lower() != "live")
# V4.6.38 — broker-aware: get_broker_client returns the Alpaca TradingClient on
# Alpaca, or the IBKR shim (ledger-backed) on IBKR. The shim mimics the alpaca-py
# surface (get_clock/get_account/get_all_positions/get_open_position/
# close_position/submit_order) and supports SELL-to-open / BUY-to-cover shorts via
# the per-bot ledger, so the SAME strategy code runs on both brokers.
trading_client, _, _ = get_broker_client("stocks")


# =========================================================
# STATE
# =========================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "last_trade_times":  {},
            "known_short_times": {},
            "short_entry_prices":{},
            "trailing_stops":    {},   # ticker -> current stop price
            "peak_profits":      {},   # ticker -> best unrealized profit seen
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


def record_short(state, ticker, entry_price, atr):
    now = datetime.now(timezone.utc).isoformat()
    state["known_short_times"][ticker]  = now
    state["short_entry_prices"][ticker] = entry_price
    # Initial trailing stop = entry + ATR * multiplier (above entry = loss for short)
    state["trailing_stops"][ticker]     = round(entry_price + atr * ATR_STOP_MULTIPLIER, 4)
    state["peak_profits"][ticker]       = 0.0
    record_trade(state, ticker)


def record_cover(state, ticker):
    for d in ["known_short_times", "short_entry_prices",
              "trailing_stops", "peak_profits"]:
        state[d].pop(ticker, None)
    record_trade(state, ticker)


def in_cooldown(state, ticker) -> bool:
    last = state["last_trade_times"].get(ticker)
    if not last:
        return False
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)
            < timedelta(minutes=TRADE_COOLDOWN_MINUTES))


def held_too_recently(state, ticker) -> bool:
    t = state["known_short_times"].get(ticker)
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
    # V4.6.68 — honor the user's adjustable call delay (default 30 min).
    try:
        import core.data as _D
        return _D.resolve_call_delay("SHORT")
    except Exception:
        pass
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
        qty = float(p.qty)
        mv  = float(p.market_value)
        pos_dict[p.symbol] = {
            "qty":             qty,
            "market_value":    mv,
            "abs_exposure":    abs(mv),
            "weight":          abs(mv) / pv if pv > 0 else 0,
            "avg_entry_price": float(p.avg_entry_price),
            "current_price":   float(p.current_price) if hasattr(p, "current_price") else 0,
            "unrealized_pl":   float(p.unrealized_pl),
            "side":            "SHORT" if qty < 0 else "LONG",
        }

    return {
        "cash":            float(account.cash),
        "buying_power":    float(account.buying_power),
        "portfolio_value": pv,
        "positions":       pos_dict,
    }


# =========================================================
# LAYER 1  -  INDICATORS  (all computed locally, zero API cost)
# =========================================================

def _ema(series: np.ndarray, period: int) -> np.ndarray:
    result = np.zeros_like(series)
    k      = 2 / (period + 1)
    result[0] = series[0]
    for i in range(1, len(series)):
        result[i] = series[i] * k + result[i - 1] * (1 - k)
    return result


def calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[-period:].mean()
    avg_l  = losses[-period:].mean()
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)


def calc_macd(closes: np.ndarray) -> tuple:
    """Returns (macd_line, signal_line, histogram)  -  last values."""
    if len(closes) < 26:
        return 0.0, 0.0, 0.0
    ema12    = _ema(closes, 12)
    ema26    = _ema(closes, 26)
    macd     = ema12 - ema26
    signal   = _ema(macd, 9)
    hist     = macd - signal
    return round(float(macd[-1]), 4), round(float(signal[-1]), 4), round(float(hist[-1]), 4)


def calc_bollinger(closes: np.ndarray, period: int = 20) -> tuple:
    """Returns (upper, mid, lower, %B)  -  last values."""
    if len(closes) < period:
        c = closes[-1]
        return c, c, c, 0.5
    window = closes[-period:]
    mid    = window.mean()
    std    = window.std()
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    pct_b  = (closes[-1] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    return round(upper, 4), round(mid, 4), round(lower, 4), round(pct_b, 4)


def calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
             period: int = 14) -> float:
    """Average True Range  -  key for position sizing and stop placement."""
    if len(highs) < period + 1:
        return float(highs[-1] - lows[-1])
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1])
        )
    )
    return round(float(tr[-period:].mean()), 4)


def calc_vwap(highs, lows, closes, volumes) -> float:
    """VWAP for the available window."""
    typical = (highs + lows + closes) / 3
    total_v = volumes.sum()
    if total_v == 0:
        return float(closes[-1])
    return round(float((typical * volumes).sum() / total_v), 4)


def calc_stochastic(highs, lows, closes, k_period=14) -> tuple:
    """Returns (%K, %D)."""
    if len(closes) < k_period:
        return 50.0, 50.0
    low_min  = lows[-k_period:].min()
    high_max = highs[-k_period:].max()
    rng      = high_max - low_min
    pct_k    = (closes[-1] - low_min) / rng * 100 if rng > 0 else 50.0
    pct_d    = pct_k  # simplified; full version needs 3-bar smoothing
    return round(pct_k, 2), round(pct_d, 2)


def detect_death_cross(closes: np.ndarray) -> bool:
    """MA20 crossed below MA50 in last 5 bars."""
    if len(closes) < 55:
        return False
    for i in range(-5, 0):
        ma20_now  = closes[i - 20:i].mean()
        ma50_now  = closes[i - 50:i].mean()
        ma20_prev = closes[i - 21:i - 1].mean()
        ma50_prev = closes[i - 51:i - 1].mean()
        if ma20_prev >= ma50_prev and ma20_now < ma50_now:
            return True
    return False


def detect_bearish_divergence(closes: np.ndarray, rsi_period=14) -> bool:
    """
    Price making higher highs but RSI making lower highs
    over the last 20 bars -> hidden weakness.
    """
    if len(closes) < 40:
        return False
    mid   = len(closes) - 20
    # Recent vs older RSI
    rsi_recent = calc_rsi(closes[-20:], rsi_period)
    rsi_older  = calc_rsi(closes[-40:-20], rsi_period)
    price_higher = closes[-1] > closes[-20]
    rsi_lower    = rsi_recent < rsi_older
    return price_higher and rsi_lower


def detect_double_top(closes: np.ndarray, tolerance=0.03) -> bool:
    """
    Two peaks within tolerance of each other separated by a trough.
    Simple implementation over last 30 bars.
    """
    if len(closes) < 30:
        return False
    window = closes[-30:]
    # Find local maxima
    peaks = []
    for i in range(1, len(window) - 1):
        if window[i] > window[i - 1] and window[i] > window[i + 1]:
            peaks.append((i, window[i]))
    if len(peaks) < 2:
        return False
    # Check if last two peaks are within tolerance
    p1, p2 = peaks[-2][1], peaks[-1][1]
    return abs(p1 - p2) / max(p1, p2) < tolerance


def detect_rising_wedge(closes: np.ndarray, highs: np.ndarray) -> bool:
    """
    Higher highs but decreasing momentum  -  wedge typically breaks down.
    Approximated by: price up but rate of gain slowing.
    """
    if len(closes) < 20:
        return False
    gains_first  = (closes[-10] - closes[-20]) / closes[-20] if closes[-20] > 0 else 0
    gains_second = (closes[-1]  - closes[-10]) / closes[-10] if closes[-10] > 0 else 0
    price_up   = closes[-1] > closes[-20]
    slowing    = gains_second < gains_first * 0.5
    return price_up and slowing and gains_first > 0


def detect_volume_weakness(volumes: np.ndarray, closes: np.ndarray) -> bool:
    """Price rising on declining volume -> weak rally."""
    if len(volumes) < 10:
        return False
    recent_vol = volumes[-5:].mean()
    older_vol  = volumes[-10:-5].mean()
    price_up   = closes[-1] > closes[-5]
    vol_down   = recent_vol < older_vol * 0.8
    return price_up and vol_down


def calc_zscore(closes: np.ndarray, window=20) -> float:
    """How many std devs above 20-day mean. >2 = very extended."""
    if len(closes) < window:
        return 0.0
    w    = closes[-window:]
    mean = w.mean()
    std  = w.std()
    if std == 0:
        return 0.0
    return round(float((closes[-1] - mean) / std), 3)


# =========================================================
# LAYER 1  -  FUNDAMENTALS  (yfinance .info, free)
# =========================================================

def get_fundamentals(ticker: str) -> dict:
    """
    Fetch fundamental data. Returns empty dict on failure.
    Cached in the data dict to avoid double-fetching.
    """
    try:
        info = yf.Ticker(ticker).info
        return {
            "pe_ratio":           info.get("trailingPE", None),
            "revenue_growth":     info.get("revenueGrowth", None),
            "debt_to_equity":     info.get("debtToEquity", None),
            "short_float_pct":    info.get("shortPercentOfFloat", None),
            "earnings_date":      str(info.get("earningsTimestamp", "")),
            "forward_pe":         info.get("forwardPE", None),
            "price_to_book":      info.get("priceToBook", None),
            "sector":             info.get("sector", "Unknown"),
        }
    except Exception:
        return {}


def has_earnings_soon(fundamentals: dict) -> bool:
    """True if earnings within EARNINGS_BLACKOUT_DAYS."""
    ts = fundamentals.get("earnings_date", "")
    if not ts or ts == "None" or ts == "":
        return False
    try:
        ts_val = float(ts)
        earnings_dt = datetime.fromtimestamp(ts_val, tz=timezone.utc)
        days_away   = (earnings_dt - datetime.now(timezone.utc)).days
        return 0 <= days_away <= EARNINGS_BLACKOUT_DAYS
    except Exception:
        return False


def short_squeeze_risk(fundamentals: dict) -> bool:
    """True if short float % is already very high -> squeeze risk."""
    sf = fundamentals.get("short_float_pct")
    if sf is None:
        return False
    return float(sf) > MAX_SHORT_FLOAT_PCT


# =========================================================
# LAYER 1  -  MARKET REGIME  (SPY vs 50-day MA)
# =========================================================

def get_market_regime() -> str:
    """
    Returns 'BULL', 'BEAR', or 'NEUTRAL'.
    BULL = SPY > 50MA by BULL_REGIME_THRESHOLD -> be very selective shorting.
    BEAR = SPY < 50MA -> can be more aggressive.
    """
    try:
        spy   = yf.Ticker("SPY").history(period="3mo", auto_adjust=True)
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


# =========================================================
# LAYER 1  -  FULL STOCK ANALYSIS
# =========================================================

def analyse_stock(ticker: str) -> dict | None:
    """
    Fetch OHLCV + fundamentals, compute all indicators,
    assign a composite bearish score. Returns None if data
    is insufficient or stock should be filtered out.
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

        # -- Technical indicators --
        rsi            = calc_rsi(closes)
        macd, sig, hist_val = calc_macd(closes)
        bb_upper, bb_mid, bb_lower, pct_b = calc_bollinger(closes)
        atr            = calc_atr(highs, lows, closes)
        vwap           = calc_vwap(highs, lows, closes, volumes)
        stoch_k, stoch_d = calc_stochastic(highs, lows, closes)
        zscore         = calc_zscore(closes)

        ma20 = closes[-20:].mean() if len(closes) >= 20 else last_price
        ma50 = closes[-50:].mean() if len(closes) >= 50 else last_price
        pct_vs_ma20 = round((last_price / ma20 - 1) * 100, 2) if ma20 > 0 else 0
        pct_vs_ma50 = round((last_price / ma50 - 1) * 100, 2) if ma50 > 0 else 0

        weekly_ret  = round((last_price / closes[-5]  - 1) * 100, 2) if len(closes) >= 5  else 0
        monthly_ret = round((last_price / closes[-20] - 1) * 100, 2) if len(closes) >= 20 else 0

        volatility = round(float(np.diff(np.log(closes[-20:])).std() * 100), 3)

        # -- Pattern flags --
        patterns = {
            "death_cross":        detect_death_cross(closes),
            "bearish_divergence": detect_bearish_divergence(closes),
            "double_top":         detect_double_top(closes),
            "rising_wedge":       detect_rising_wedge(closes, highs),
            "volume_weakness":    detect_volume_weakness(volumes, closes),
        }
        pattern_count = sum(patterns.values())

        # -- Fundamentals --
        fundamentals = get_fundamentals(ticker)

        # -- Filter: skip if earnings soon or squeeze risk --
        if has_earnings_soon(fundamentals):
            print(f"  [filter] {ticker}: earnings blackout")
            return None
        if short_squeeze_risk(fundamentals):
            print(f"  [filter] {ticker}: high short float (squeeze risk)")
            return None

        # -- Composite bearish score ----------------------
        # Higher = better short candidate
        score = 0.0

        # Momentum (negative monthly return = bearish = good for short)
        score += max(0, -monthly_ret) * 0.25
        score += max(0, -weekly_ret)  * 0.40

        # Overbought indicators (still overbought = topping = good entry)
        score += max(0, rsi - 60)     * 0.50
        score += max(0, stoch_k - 70) * 0.20
        score += max(0, pct_b - 0.8)  * 30   # very near upper BB

        # Extension from mean (mean-reversion short)
        score += max(0, zscore)        * 8
        score += max(0, pct_vs_ma20)   * 0.30
        score += max(0, pct_vs_ma50)   * 0.20

        # MACD bearish signal (histogram going negative)
        score += max(0, -hist_val * 100) * 0.50

        # Price below VWAP (selling pressure)
        if last_price < vwap:
            score += 5

        # Pattern bonus
        score += pattern_count * 12

        # Volatility multiplier  -  volatile stocks = bigger moves
        vol_mult = 1 + volatility * 0.08
        score    = round(float(score * vol_mult), 3)

        return {
            "ticker":        ticker,
            "price":         round(last_price, 2),
            "atr":           atr,
            "bearish_score": score,
            # Compact summary for Claude prompt
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
                "atr":           atr,
            },
            "patterns":      patterns,
            "pattern_count": pattern_count,
            "fundamentals": {
                "pe_ratio":       fundamentals.get("pe_ratio"),
                "revenue_growth": fundamentals.get("revenue_growth"),
                "short_float":    fundamentals.get("short_float_pct"),
                "debt_equity":    fundamentals.get("debt_to_equity"),
                "sector":         fundamentals.get("sector", "Unknown"),
            },
            # Full OHLCV stored for charting (not sent to Claude as text)
            "_ohlcv": {
                "closes":  closes.tolist(),
                "highs":   highs.tolist(),
                "lows":    lows.tolist(),
                "volumes": volumes.tolist(),
                "dates":   [str(d)[:10] for d in hist.index],
                "ma20":    [round(float(closes[max(0,i-19):i+1].mean()), 2)
                            for i in range(len(closes))],
                "ma50":    [round(float(closes[max(0,i-49):i+1].mean()), 2)
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
    Load tickers from shortbot_universe.txt (managed by universe_manager.py).
    Falls back to hardcoded ALL_SHORTABLE if file does not exist yet.
    """
    if not os.path.exists(UNIVERSE_FILE):
        print(f"  [universe] {UNIVERSE_FILE} not found  -  using built-in list ({len(ALL_SHORTABLE)} tickers)")
        return ALL_SHORTABLE
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
    """
    Load universe from file, analyse all tickers, filter bad ones,
    sort by bearish score. Returns top candidates ready for charting + Claude.
    """
    universe = load_universe()
    max_pos  = BULL_REGIME_MAX_POS if regime == "BULL" else BEAR_REGIME_MAX_POS
    results  = []

    print(f"Scanning {len(universe)} tickers (regime: {regime})...")

    for ticker in universe:
        d = analyse_stock(ticker)
        if d:
            results.append(d)

    results.sort(key=lambda x: x["bearish_score"], reverse=True)

    top = results[:TOP_N_FOR_CLAUDE]
    print(f"  -> {len(results)} valid | top {len(top)} sent to Claude")
    if top:
        print(f"  -> #1 candidate: {top[0]['ticker']} "
              f"(score {top[0]['bearish_score']:.1f}, "
              f"{top[0]['pattern_count']} patterns)")
    return top


# =========================================================
# LAYER 2A  -  CHART GENERATION
# =========================================================

# Dark chart style matching APEX dashboard
CHART_STYLE = {
    "bg":       "#060a0e",
    "panel":    "#0b0f16",
    "border":   "#18212e",
    "text":     "#d0d8e4",
    "muted":    "#4a5568",
    "green":    "#00f5c3",
    "red":      "#ff4d6d",
    "yellow":   "#ffe353",
    "purple":   "#7b8cff",
    "candle_up":   "#00f5c3",
    "candle_down": "#ff4d6d",
    "volume_up":   "rgba(0,245,195,0.4)",
    "volume_down": "rgba(255,77,109,0.4)",
}

CS = CHART_STYLE


def generate_chart(data: dict) -> bytes:
    """
    Generate a comprehensive technical analysis chart.
    Returns PNG bytes.

    Layout:
      Row 0 (60%): Candlestick + MA20 + MA50 + Bollinger Bands
      Row 1 (20%): Volume bars
      Row 2 (10%): RSI with overbought/oversold lines
      Row 3 (10%): MACD histogram + signal
    """
    ohlcv   = data["_ohlcv"]
    ticker  = data["ticker"]
    n       = min(CHART_LOOKBACK_DAYS, len(ohlcv["closes"]))

    closes  = np.array(ohlcv["closes"][-n:])
    highs   = np.array(ohlcv["highs"][-n:])
    lows    = np.array(ohlcv["lows"][-n:])
    volumes = np.array(ohlcv["volumes"][-n:])
    ma20    = np.array(ohlcv["ma20"][-n:])
    ma50    = np.array(ohlcv["ma50"][-n:])
    dates   = ohlcv["dates"][-n:]
    x       = np.arange(n)

    bb_upper = ohlcv["bb_upper"]
    bb_lower = ohlcv["bb_lower"]

    # Recompute MACD and RSI for full series for charting
    full_closes = np.array(ohlcv["closes"])
    rsi_series  = np.array([
        calc_rsi(full_closes[:i+1]) for i in range(len(full_closes))
    ])[-n:]

    ema12     = _ema(full_closes, 12)
    ema26     = _ema(full_closes, 26)
    macd_line = ema12 - ema26
    sig_line  = _ema(macd_line, 9)
    macd_hist = macd_line - sig_line
    macd_line = macd_line[-n:]
    sig_line  = sig_line[-n:]
    macd_hist = macd_hist[-n:]

    # -- Figure setup --
    fig = plt.figure(figsize=(14, 10), facecolor=CS["bg"])
    gs  = gridspec.GridSpec(
        4, 1, height_ratios=[5, 1.5, 1.2, 1.2],
        hspace=0.04, left=0.06, right=0.97, top=0.93, bottom=0.06
    )

    ax_price  = fig.add_subplot(gs[0])
    ax_vol    = fig.add_subplot(gs[1], sharex=ax_price)
    ax_rsi    = fig.add_subplot(gs[2], sharex=ax_price)
    ax_macd   = fig.add_subplot(gs[3], sharex=ax_price)

    for ax in [ax_price, ax_vol, ax_rsi, ax_macd]:
        ax.set_facecolor(CS["panel"])
        ax.tick_params(colors=CS["muted"], labelsize=7)
        ax.spines[:].set_color(CS["border"])
        ax.grid(color=CS["border"], linewidth=0.4, alpha=0.7)
        plt.setp(ax.get_xticklabels(), visible=False)

    plt.setp(ax_macd.get_xticklabels(), visible=True, rotation=30, ha="right")

    # -- Candlestick --
    candle_w = 0.6
    for i in range(n):
        o = closes[i - 1] if i > 0 else closes[i]  # approx open as prev close
        c = closes[i]
        h, l = highs[i], lows[i]
        color = CS["candle_up"] if c >= o else CS["candle_down"]
        # Wick
        ax_price.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.8)
        # Body
        body_h = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.1
        body_y = min(c, o)
        ax_price.bar(i, body_h, bottom=body_y, width=candle_w,
                     color=color, alpha=0.9, linewidth=0)

    # -- Moving averages --
    ax_price.plot(x, ma20, color=CS["yellow"],  linewidth=1.2,
                  label="MA20", alpha=0.9)
    ax_price.plot(x, ma50, color=CS["purple"],  linewidth=1.2,
                  label="MA50", alpha=0.9)

    # -- Bollinger Bands --
    bb_up_arr  = np.full(n, bb_upper)
    bb_lo_arr  = np.full(n, bb_lower)
    ax_price.plot(x, bb_up_arr, color=CS["muted"], linewidth=0.8,
                  linestyle="--", label="BB", alpha=0.6)
    ax_price.plot(x, bb_lo_arr, color=CS["muted"], linewidth=0.8,
                  linestyle="--", alpha=0.6)
    ax_price.fill_between(x, bb_up_arr, bb_lo_arr,
                          color=CS["muted"], alpha=0.04)

    # -- Patterns annotated --
    patterns = data["patterns"]
    ann_y    = closes.max() * 1.02
    ann_x    = n - 1
    pat_labels = []
    if patterns.get("death_cross"):        pat_labels.append("[SKULL] Death Cross")
    if patterns.get("bearish_divergence"): pat_labels.append("/>> Bear Div.")
    if patterns.get("double_top"):         pat_labels.append("[MTN][MTN] Dbl Top")
    if patterns.get("rising_wedge"):       pat_labels.append("[TRI] Rising Wedge")
    if patterns.get("volume_weakness"):    pat_labels.append("[DOWN] Vol Weak")

    if pat_labels:
        ax_price.annotate(
            "  ".join(pat_labels),
            xy=(ann_x * 0.02, ann_y),
            fontsize=7.5,
            color=CS["red"],
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", fc=CS["panel"],
                      ec=CS["red"], alpha=0.85)
        )

    # -- Price axis labels --
    ax_price.set_ylabel("Price $", color=CS["muted"], fontsize=8)
    ax_price.tick_params(axis="y", colors=CS["muted"])
    ax_price.legend(loc="upper left", fontsize=7, framealpha=0.3,
                    labelcolor=CS["text"], facecolor=CS["panel"])

    # -- Score badge --
    score_text = (
        f"{ticker}   ${data['price']:.2f}   "
        f"Score: {data['bearish_score']:.1f}   "
        f"RSI: {data['indicators']['rsi']:.0f}   "
        f"ATR: {data['atr']:.2f}"
    )
    fig.suptitle(score_text, color=CS["text"], fontsize=11,
                 fontweight="bold", y=0.975,
                 bbox=dict(boxstyle="round", fc=CS["panel"],
                           ec=CS["border"], alpha=0.9))

    # -- Volume --
    vol_colors = [
        CS["candle_up"] if closes[i] >= (closes[i-1] if i > 0 else closes[i])
        else CS["candle_down"]
        for i in range(n)
    ]
    ax_vol.bar(x, volumes, color=vol_colors, alpha=0.55, width=0.8)
    vol_ma = np.array([volumes[max(0,i-9):i+1].mean() for i in range(n)])
    ax_vol.plot(x, vol_ma, color=CS["yellow"], linewidth=0.9, alpha=0.8)
    ax_vol.set_ylabel("Vol", color=CS["muted"], fontsize=7)
    ax_vol.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K")
    )

    # -- RSI --
    ax_rsi.plot(x, rsi_series, color=CS["purple"], linewidth=1.2)
    ax_rsi.axhline(70, color=CS["red"],   linewidth=0.7, linestyle="--", alpha=0.7)
    ax_rsi.axhline(30, color=CS["green"], linewidth=0.7, linestyle="--", alpha=0.7)
    ax_rsi.axhline(50, color=CS["muted"], linewidth=0.5, linestyle=":", alpha=0.5)
    ax_rsi.fill_between(x, rsi_series, 70,
                        where=(rsi_series >= 70),
                        color=CS["red"], alpha=0.15)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", color=CS["muted"], fontsize=7)
    ax_rsi.yaxis.set_ticks([30, 50, 70])

    # -- MACD --
    bar_colors = [CS["green"] if v >= 0 else CS["red"] for v in macd_hist]
    ax_macd.bar(x, macd_hist, color=bar_colors, alpha=0.7, width=0.8)
    ax_macd.plot(x, macd_line, color=CS["yellow"], linewidth=1.0, label="MACD")
    ax_macd.plot(x, sig_line,  color=CS["purple"], linewidth=1.0, label="Signal")
    ax_macd.axhline(0, color=CS["muted"], linewidth=0.5)
    ax_macd.set_ylabel("MACD", color=CS["muted"], fontsize=7)
    ax_macd.legend(loc="upper left", fontsize=6, framealpha=0.2,
                   labelcolor=CS["text"], facecolor=CS["panel"])

    # -- X axis ticks (show every ~10 bars) --
    tick_step = max(1, n // 8)
    tick_pos  = list(range(0, n, tick_step))
    tick_lbl  = [dates[i] for i in tick_pos]
    ax_macd.set_xticks(tick_pos)
    ax_macd.set_xticklabels(tick_lbl, color=CS["muted"], fontsize=7)

    # -- Export to bytes --
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110,
                facecolor=CS["bg"], bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def chart_to_b64(png_bytes: bytes) -> str:
    return base64.standard_b64encode(png_bytes).decode("utf-8")


# =========================================================
# LAYER 2B  -  CLAUDE VISION + JSON
# =========================================================

def build_compact_table(candidates: list) -> str:
    """
    Build a compact text table of key numbers for Claude.
    Keeps the text portion of the prompt small.
    """
    lines = ["TICKER | SCORE | RSI | MACD_H | %B | ZSCORE | W_RET | M_RET | PATTERNS"]
    lines.append("-" * 80)
    for c in candidates:
        ind = c["indicators"]
        pat = [k for k, v in c["patterns"].items() if v]
        lines.append(
            f"{c['ticker']:6s} | {c['bearish_score']:6.1f} | "
            f"{ind['rsi']:5.1f} | {ind['macd_hist']:+6.4f} | "
            f"{ind['pct_b']:4.2f} | {ind['zscore']:+5.2f} | "
            f"{ind['weekly_ret']:+5.1f}% | {ind['monthly_ret']:+5.1f}% | "
            f"{', '.join(pat) if pat else ' - '}"
        )
    return "\n".join(lines)


def ask_claude_vision(candidates: list, portfolio: dict,
                      regime: str, charts: dict) -> dict:
    """
    Send chart images + compact numeric table to Claude Haiku.
    Charts is a dict: ticker -> base64 PNG string.
    """
    held = portfolio["positions"]

    # Build message content: alternating image + label blocks
    content = []

    # Intro text block
    content.append({
        "type": "text",
        "text": (
            f"You are a SHORT-ONLY portfolio manager. "
            f"Market regime: {regime}. "
            f"Below are technical charts and data for short candidates. "
            f"Analyse each chart for bearish patterns, then return a JSON allocation.\n\n"
            f"CURRENT SHORTS HELD:\n"
            f"{json.dumps({k: {'weight': round(v['weight'],3), 'pl': v['unrealized_pl'], 'entry': v['avg_entry_price']} for k, v in held.items()}, indent=2)}\n\n"
            f"NUMERIC SUMMARY:\n{build_compact_table(candidates)}\n\n"
            f"CHARTS (one per ticker, in order):"
        )
    })

    # Add each chart image
    for c in candidates:
        ticker = c["ticker"]
        if ticker in charts:
            content.append({"type": "text", "text": f"\n--- {ticker} ---"})
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": "image/png",
                    "data":       charts[ticker],
                }
            })

    # Final instruction
    max_pos = BULL_REGIME_MAX_POS if regime == "BULL" else MAX_POSITIONS
    content.append({
        "type": "text",
        "text": (
            f"\nReturn ONLY valid JSON. No markdown. No text outside JSON.\n\n"
            f"Format:\n"
            f'{{"decision":"ALLOCATE",'
            f'"target_portfolio":{{"TICKER":0.15,"CASH":0.10}},'
            f'"confidence":0.0,'
            f'"short_analysis":"one sentence",'
            f'"pattern_notes":{{"TICKER":"pattern identified"}}}}\n\n'
            f"Rules:\n"
            f"- decision: ALLOCATE or HOLD\n"
            f"- Only short tickers from the candidate list\n"
            f"- Max {max_pos} positions (regime is {regime})\n"
            f"- Max single weight: {MAX_SINGLE_WEIGHT}\n"
            f"- Minimum CASH: {MIN_CASH_WEIGHT}\n"
            f"- Weights sum to ~1.0\n"
            f"- Prioritise stocks with multiple bearish patterns AND overbought RSI\n"
            f"- If regime is BULL, only take highest-conviction shorts\n"
            f"- If no strong thesis: HOLD\n"
            f"- confidence between 0 and 1"
        )
    })

    raw = call_ai_vision(content, _AI_PROVIDER, _AI_MODEL, _AI_KEY, MAX_TOKENS)
    raw = raw.replace("```json", "").replace("```", "").strip()
    print(f"RAW {_AI_PROVIDER.upper()}:", raw[:500])

    try:
        return json.loads(raw)
    except Exception as e:
        print(f"JSON parse error: {e}")
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": 0.0,
                "short_analysis": "JSON error  -  holding.",
                "pattern_notes": {}}


def ask_ai_text(candidates: list, portfolio: dict, regime: str) -> dict:
    """Text-only AI path — no charts. Works with Groq and all providers."""
    held = portfolio["positions"]
    max_pos = MAX_POSITIONS

    prompt = (
        f"You are a SHORT-ONLY portfolio manager (you short stocks expecting them to fall).\n"
        f"Market regime: {regime}.\n"
        f"Analyse the following data for bearish setups and return a JSON allocation.\n\n"
        f"CURRENT SHORT HOLDINGS:\n"
        f"{json.dumps({k: {'weight': round(v['weight'], 3), 'pl': v['unrealized_pl'], 'entry': v['avg_entry_price']} for k, v in held.items()}, indent=2)}\n\n"
        f"CANDIDATE STOCKS (numeric data — no charts available):\n"
        f"{build_compact_table(candidates)}\n\n"
        f"Return ONLY valid JSON. No markdown.\n\n"
        f'{{"decision":"ALLOCATE",'
        f'"target_portfolio":{{"TICKER":0.15,"CASH":0.10}},'
        f'"confidence":0.0,'
        f'"short_analysis":"one sentence",'
        f'"pattern_notes":{{"TICKER":"indicator-based note"}}}}\n\n'
        f"Rules:\n"
        f"- decision: ALLOCATE or HOLD\n"
        f"- Only short tickers from the candidate list\n"
        f"- Max {max_pos} positions\n"
        f"- Max single weight: {MAX_SINGLE_WEIGHT}\n"
        f"- Min CASH: {MIN_CASH_WEIGHT}\n"
        f"- Weights sum to ~1.0\n"
        f"- Prefer: RSI overbought + negative momentum + weak fundamentals\n"
        f"- In BULL regime: only take highest-conviction shorts\n"
        f"- confidence between 0 and 1\n"
        f"- If no strong bearish setup: HOLD"
    )

    raw = call_ai_text(prompt, _AI_PROVIDER, _AI_MODEL, _AI_KEY, MAX_TOKENS)
    raw = raw.replace("```json", "").replace("```", "").strip()
    print(f"RAW {_AI_PROVIDER.upper()} (text):", raw[:500])

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
    if signal.get("decision") != "ALLOCATE":
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": 0.0,
                "short_analysis": "Non-ALLOCATE  -  holding.", "pattern_notes": {}}

    confidence = float(signal.get("confidence", 0))
    mc = live_min_confidence()
    if confidence < mc:
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": confidence,
                "short_analysis": f"Confidence {confidence:.2f} below {mc:.2f}.",
                "pattern_notes": {}}

    valid  = {c["ticker"] for c in candidates}
    target = signal.get("target_portfolio", {})
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
        cap = MAX_ETF_WEIGHT if ticker in SHORT_ETF_UNIVERSE else MAX_SINGLE_WEIGHT
        cleaned[ticker] = min(weight, cap)

    cleaned["CASH"] = max(cleaned.get("CASH", MIN_CASH_WEIGHT), MIN_CASH_WEIGHT)

    max_pos  = BULL_REGIME_MAX_POS if regime == "BULL" else MAX_POSITIONS
    non_cash = sorted(
        [(k, v) for k, v in cleaned.items() if k != "CASH"],
        key=lambda x: x[1], reverse=True
    )[:max_pos]

    if len(non_cash) < MIN_POSITIONS:
        existing = {k for k, _ in non_cash}
        for c in candidates:
            if c["ticker"] not in existing and c["ticker"] in valid:
                non_cash.append((c["ticker"], 1.0 / MIN_POSITIONS))
                existing.add(c["ticker"])
                if len(non_cash) >= MIN_POSITIONS:
                    break

    cleaned      = dict(non_cash)
    cleaned["CASH"] = max(cleaned.get("CASH", MIN_CASH_WEIGHT), MIN_CASH_WEIGHT)
    total        = sum(cleaned.values())

    if total <= 0:
        return {"decision": "HOLD", "target_portfolio": {}, "confidence": confidence,
                "short_analysis": "Empty after validation.", "pattern_notes": {}}

    cleaned      = {k: round(v / total, 4) for k, v in cleaned.items()}
    signal["target_portfolio"] = cleaned
    signal["confidence"]       = confidence
    return signal


# =========================================================
# LAYER 3  -  TRAILING STOP ENGINE
# =========================================================

def update_trailing_stops(portfolio: dict, state: dict,
                          candidate_data: dict) -> list:
    """
    For each open short:
    1. If profit >= MIN_PROFIT_TO_TRAIL, tighten the trailing stop
       so it trails the lowest price seen (best for short = lowest price).
    2. If current price >= trailing stop -> cover immediately.
    3. If TIME_STOP_DAYS elapsed with no meaningful profit -> cover.

    candidate_data: dict of ticker -> analyse_stock result (for ATR).
    Returns list of action strings for tickers that were covered.
    """
    covered_actions = []

    for ticker, pos in list(portfolio["positions"].items()):
        if pos["side"] != "SHORT":
            continue

        entry   = float(state["short_entry_prices"].get(ticker, pos["avg_entry_price"]))
        current = float(pos["current_price"]) if pos["current_price"] > 0 else entry
        atr     = float(candidate_data.get(ticker, {}).get("atr", entry * 0.02))

        # -- Profit so far (positive = good for short) --
        profit_pct = (entry - current) / entry if entry > 0 else 0

        # -- Update peak profit --
        peak = float(state["peak_profits"].get(ticker, 0))
        if profit_pct > peak:
            state["peak_profits"][ticker] = profit_pct
            peak = profit_pct

        # -- Tighten trailing stop once profitable --
        if peak >= MIN_PROFIT_TO_TRAIL:
            # New stop = current_price + ATR*multiplier
            # (if price drops further our stop drops too)
            new_stop = current + atr * ATR_STOP_MULTIPLIER
            old_stop = float(state["trailing_stops"].get(ticker, entry + atr * ATR_STOP_MULTIPLIER))
            # For shorts: lower stop is BETTER (allows more room)
            # We only tighten (lower) the stop, never loosen it
            if new_stop < old_stop:
                state["trailing_stops"][ticker] = round(new_stop, 4)

        # -- Check if stop triggered --
        stop_price = float(state["trailing_stops"].get(
            ticker, entry + atr * ATR_STOP_MULTIPLIER
        ))

        if current >= stop_price:
            try:
                print(f"  [stop] Covering {ticker}: price ${current:.2f} >= stop ${stop_price:.2f}")
                trading_client.close_position(ticker)
                covered_actions.append(
                    f"TRAILING STOP COVER {ticker} "
                    f"(price ${current:.2f} >= stop ${stop_price:.2f}, "
                    f"peak profit {peak:.1%})"
                )
                record_cover(state, ticker)
                time.sleep(1)
            except Exception as e:
                covered_actions.append(f"FAILED STOP COVER {ticker}: {e}")
            continue

        # -- Time stop: cover if held too long without profit --
        short_time = state["known_short_times"].get(ticker)
        if short_time:
            days_held = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(short_time)).days
            if days_held >= TIME_STOP_DAYS and profit_pct < 0.01:
                try:
                    print(f"  [time stop] Covering {ticker}: "
                          f"{days_held}d held, only {profit_pct:.1%} profit")
                    trading_client.close_position(ticker)
                    covered_actions.append(
                        f"TIME STOP COVER {ticker} "
                        f"({days_held}d held, {profit_pct:.1%} profit)"
                    )
                    record_cover(state, ticker)
                    time.sleep(1)
                except Exception as e:
                    covered_actions.append(f"FAILED TIME STOP {ticker}: {e}")

    return covered_actions


# =========================================================
# LAYER 3  -  EXECUTION
# =========================================================

def execute_allocation(signal: dict, portfolio: dict,
                       state: dict, candidate_data: dict) -> str:
    """
    candidate_data: dict of ticker -> full analyse_stock result (for ATR).
    """
    if signal.get("decision") != "ALLOCATE":
        return "HOLD  -  no allocation."

    target     = signal.get("target_portfolio", {})
    confidence = float(signal.get("confidence", 0))
    pv         = portfolio["portfolio_value"]
    positions  = portfolio["positions"]
    actions    = []
    trades     = 0

    min_order  = pv * MIN_ORDER_PCT
    dust       = pv * DUST_POSITION_PCT
    min_remain = pv * MIN_REMAINING_PCT

    cur_weights = {t: p["weight"] for t, p in positions.items()}
    cur_weights["CASH"] = portfolio["cash"] / pv if pv > 0 else 0
    all_tickers = set(cur_weights) | set(target)

    # -- 1. COVER positions being reduced ------------------
    for ticker in all_tickers:
        if ticker == "CASH" or trades >= MAX_TRADES_PER_RUN:
            break

        cur_w = cur_weights.get(ticker, 0)
        tgt_w = target.get(ticker, 0)

        if tgt_w - cur_w >= -MIN_REBALANCE_DELTA:
            continue

        pos = positions.get(ticker)
        if not pos or pos["side"] != "SHORT":
            continue

        if in_cooldown(state, ticker) or held_too_recently(state, ticker):
            continue

        try:
            position    = trading_client.get_open_position(ticker)
            short_qty   = abs(float(position.qty))
            abs_exp     = abs(float(position.market_value))

            if short_qty <= 0 or abs_exp <= 0:
                continue

            tgt_exp   = pv * tgt_w
            to_cover  = abs_exp - tgt_exp

            if to_cover < min_order:
                continue

            if tgt_exp < min_remain or (abs_exp - to_cover) < dust:
                trading_client.close_position(ticker)
                actions.append(f"COVER ALL {ticker} -> target {tgt_w:.1%}")
                record_cover(state, ticker)
            else:
                frac         = min(to_cover / abs_exp, 1.0)
                qty_to_cover = math.floor(short_qty * frac)
                if qty_to_cover < 1:
                    continue
                order = MarketOrderRequest(
                    symbol=ticker, qty=str(int(qty_to_cover)),
                    side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order)
                actions.append(f"COVER {int(qty_to_cover)} {ticker} "
                               f"(-> {tgt_w:.1%})")
                record_trade(state, ticker)

            trades += 1
            time.sleep(1)

        except Exception as e:
            actions.append(f"FAILED COVER {ticker}: {e}")

    time.sleep(5)

    # Refresh portfolio after covers
    updated  = get_portfolio()
    short_exp = sum(p["abs_exposure"] for p in updated["positions"].values()
                    if p["side"] == "SHORT")
    max_exp   = pv * (1 - MIN_CASH_WEIGHT)
    eff_bp    = min(updated["buying_power"], max(0, max_exp - short_exp))

    # -- 2. OPEN new short positions -----------------------
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
            # Use fast_info for current price (1 yf call, very cheap)
            fi    = yf.Ticker(ticker).fast_info
            price = float(fi.last_price)
            if price <= 0:
                continue

            # Alpaca cannot short fractional shares -> whole shares only
            qty = math.floor(dollars / price)
            if qty < 1:
                continue

            # Get ATR for stop placement
            atr = float(candidate_data.get(ticker, {}).get("atr", price * 0.02))

            order = MarketOrderRequest(
                symbol=ticker, qty=str(int(qty)),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            trading_client.submit_order(order)
            stop = round(price + atr * ATR_STOP_MULTIPLIER, 4)
            actions.append(
                f"SHORT {int(qty)} {ticker} @ ~${price:.2f} "
                f"(~${dollars:.0f}) | ATR stop @ ${stop:.2f}"
            )
            record_short(state, ticker, price, atr)
            eff_bp -= dollars
            trades += 1
            time.sleep(1)

        except Exception as e:
            actions.append(f"FAILED SHORT {ticker}: {e}")

    return " | ".join(actions) if actions else "No trades needed."


# =========================================================
# LOGGING
# =========================================================

def write_outputs(signal, action, before, after, state, regime, charts_saved):
    pv_before = before.get("portfolio_value", 0)
    pv_after  = after.get("portfolio_value", 0)
    change    = round(pv_after - pv_before, 4)

    log_entry = {
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

    text = f"""
TIME: {datetime.now()}
REGIME: {regime}

SIGNAL:
{json.dumps(signal, indent=2)}

ACTION:
{action}

CHARTS GENERATED: {charts_saved}

PORTFOLIO BEFORE: ${pv_before:,.2f}
PORTFOLIO AFTER:  ${pv_after:,.2f}
CHANGE:           ${change:+,.2f}
"""

    with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
        f.write(text)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    print(text)


# =========================================================
# MAIN LOOP
# =========================================================

def run_once():
    state = load_state()
    state = reset_daily(state)

    # -- Portfolio snapshot --
    before = get_portfolio()
    pv     = before["portfolio_value"]
    print(f"\n  Portfolio: ${pv:,.2f} | Cash: ${before['cash']:,.2f} | "
          f"Shorts: {len(before['positions'])}")

    # -- Market regime check --
    regime = get_market_regime()
    print(f"  Market regime: {regime}")

    # -- Layer 1: scan + score all candidates --
    candidates = scan_universe(regime)
    if not candidates:
        print("  No valid candidates  -  skipping.")
        return

    # Build quick lookup by ticker for ATR access during execution
    candidate_data = {c["ticker"]: c for c in candidates}

    # -- Trailing stop check (runs before new decisions) --
    print("  Checking trailing stops...")
    stop_actions = update_trailing_stops(before, state, candidate_data)
    if stop_actions:
        print(f"  Stop actions: {stop_actions}")

    # -- AI Signal (vision or text mode) --
    if _AI_MODE == "text":
        print(f"  Calling {_AI_PROVIDER} (text mode — no charts)...")
        raw_signal = ask_ai_text(candidates, before, regime)
        signal     = validate_signal(raw_signal, candidates, regime)
    else:
        # Vision mode: generate charts then send to AI
        print(f"  Generating {len(candidates)} charts...")
        charts = {}
        saved  = []
        for c in candidates:
            try:
                png   = generate_chart(c)
                b64   = chart_to_b64(png)
                charts[c["ticker"]] = b64
                path = os.path.join(CHART_DIR, f"{c['ticker']}.png")
                with open(path, "wb") as f:
                    f.write(png)
                saved.append(c["ticker"])
            except Exception as e:
                print(f"  [chart] {c['ticker']}: {e}")

        print(f"  Charts ready: {saved}")
        print(f"  Calling {_AI_PROVIDER} Vision...")
        raw_signal = ask_claude_vision(candidates, before, regime, charts)
        signal     = validate_signal(raw_signal, candidates, regime)

    print(f"  Decision: {signal['decision']} | "
          f"Confidence: {signal.get('confidence', 0):.0%}")
    if signal.get("pattern_notes"):
        for t, note in signal["pattern_notes"].items():
            print(f"    {t}: {note}")

    # -- Layer 3: Execute --
    action = execute_allocation(signal, before, state, candidate_data)
    if stop_actions:
        action = " | ".join(stop_actions + [action])

    after = get_portfolio()
    save_state(state)
    write_outputs(signal, action, before, after, state, regime, saved)


_SHORT_PHILOSOPHY = (
    "I open SHORT positions only, sized by ATR risk, targeting "
    "mean-reversion fades on overextended US large-caps. I never go long. "
    "I trail stops using ATR multiples and exit on time decay after "
    f"{TIME_STOP_DAYS} days. I want short-side fade candidates — I will "
    "close any long position outright (I cannot manage it), any short "
    "position whose unrealized loss already exceeds my trailing stop "
    "budget, and any position with a thesis I cannot articulate as "
    "mean-reversion / overextension.")


def _short_ai_caller(prompt: str):
    """Plain-text AI call used by transition cleanup (no charts)."""
    try:
        return call_ai_text(prompt, _AI_PROVIDER, _AI_MODEL, _AI_KEY)
    except Exception as e:
        print(f"[transition] AI call failed: {e}", flush=True)
        return None


def main():
    print("=" * 65)
    print("APEX SHORT BOT v2   -  Vision + Technical Analysis")
    print(f"Model:    {_AI_MODEL}")
    print(f"Universe: {UNIVERSE_FILE} -> top {TOP_N_FOR_CLAUDE} to Claude")
    print(f"Stops:    ATRx{ATR_STOP_MULTIPLIER} trailing | {TIME_STOP_DAYS}d time stop")
    print(f"Regime:   BULL={BULL_REGIME_MAX_POS} pos max | BEAR={BEAR_REGIME_MAX_POS} pos max")
    print("=" * 65)

    # V4.6.2 — transition cleanup
    try:
        from core import transition as _T
        _acct = trading_client.get_account()
        _T.record_account_or_flag_transition("SHORT", _acct.id)
    except Exception as _e:
        print(f"[transition] SHORT init failed: {_e}", flush=True)

    # V4.6.26 — Initial deterministic cleanup: liquidate non-equity
    # positions a previous bot may have left behind.
    try:
        from core.bot_framework import liquidate_off_strategy_positions
        liquidate_off_strategy_positions(trading_client, "stocks")
    except Exception as _e:
        print(f"[startup-cleanup] SHORT failed: {_e}", flush=True)

    while True:
        try:
            clock = trading_client.get_clock()
            try:
                from core import transition as _T
                _T.maybe_run_cleanup("SHORT", trading_client,
                                     _short_ai_caller, _SHORT_PHILOSOPHY)
            except Exception as _e:
                print(f"[transition] SHORT cleanup error: {_e}", flush=True)

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
"""
APEX — Stock Research (Find Stocks tab backend)
─────────────────────────────────────────────────────────────────────
Powers the manual-mode "Find Stocks" tab:

  • list_tradable_assets() / search_assets()  — the symbols available on the
    user's broker (the dedicated MANUAL Alpaca account), cached to disk.
  • get_snapshot(symbol)                      — price + fundamentals + recent
    performance, like a broker app's quote page (yfinance, free).
  • get_history(symbol, period)               — OHLCV for the price chart.
  • weekly_evaluation(symbol)                 — an AI rating + write-up, cached
    per ISO week (refreshed every Monday).

Everything here is data/AI only — no order placement. Orders go through the
manual tab on the dedicated MANUAL account.
"""

from __future__ import annotations

import json
import time
import datetime

from core.paths import ACCOUNT_DIR

# ── Cache files (per-account) ──────────────────────────────────────────────
_ASSETS_CACHE = ACCOUNT_DIR / "stock_assets_cache.json"
_EVALS_CACHE  = ACCOUNT_DIR / "stock_ai_evals.json"

_ASSETS_TTL = 24 * 3600            # refresh the broker asset list once a day

# yfinance period selector → (yfinance period, interval)
PERIODS: dict[str, tuple[str, str]] = {
    "1D": ("1d",  "5m"),
    "1W": ("5d",  "30m"),
    "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"),
    "6M": ("6mo", "1d"),
    "1Y": ("1y",  "1d"),
    "5Y": ("5y",  "1wk"),
}


# ══════════════════════════════════════════════════════════════════════════
#  Broker client (dedicated MANUAL Alpaca account)
# ══════════════════════════════════════════════════════════════════════════

def manual_client():
    """Build the dedicated MANUAL Alpaca client from its own keys, exactly like
    the manual trading tab — independent of the app's active broker so search
    keeps working even while the rest of the app is on IBKR."""
    try:
        from core import data as D
        keys = D.read_env_keys()
        key = keys.get("ALPACA_API_KEY_MANUAL", "").strip()
        sec = keys.get("ALPACA_SECRET_KEY_MANUAL", "").strip()
        if not key or not sec:
            return None
        from alpaca.trading.client import TradingClient
        return TradingClient(key, sec, paper=True)
    except Exception as e:
        print(f"[stock_research] manual client build failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
#  Asset list + search
# ══════════════════════════════════════════════════════════════════════════

def _load_assets_cache() -> list[dict]:
    try:
        d = json.loads(_ASSETS_CACHE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("assets"):
            return d["assets"]
    except Exception:
        pass
    return []


def _save_assets_cache(assets: list[dict]) -> None:
    try:
        ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
        _ASSETS_CACHE.write_text(
            json.dumps({"ts": time.time(), "assets": assets}),
            encoding="utf-8")
    except Exception as e:
        print(f"[stock_research] save assets cache failed: {e}")


def _cache_fresh() -> bool:
    try:
        d = json.loads(_ASSETS_CACHE.read_text(encoding="utf-8"))
        return (time.time() - float(d.get("ts", 0))) < _ASSETS_TTL
    except Exception:
        return False


def list_tradable_assets(force: bool = False) -> list[dict]:
    """All tradable US equities on the broker, as [{symbol, name, exchange}].
    Cached to disk for a day; refreshed in the background when stale."""
    if not force and _cache_fresh():
        return _load_assets_cache()

    client = manual_client()
    if client is None:
        return _load_assets_cache()   # keep whatever we last fetched

    try:
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus
        req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY,
                               status=AssetStatus.ACTIVE)
        raw = client.get_all_assets(req) or []
        assets = []
        for a in raw:
            if not getattr(a, "tradable", False):
                continue
            assets.append({
                "symbol":   str(getattr(a, "symbol", "")).upper(),
                "name":     str(getattr(a, "name", "") or ""),
                "exchange": str(getattr(a, "exchange", "") or ""),
            })
        assets.sort(key=lambda x: x["symbol"])
        if assets:
            _save_assets_cache(assets)
        return assets
    except Exception as e:
        print(f"[stock_research] fetch assets failed: {e}")
        return _load_assets_cache()


def search_assets(query: str, limit: int = 25) -> list[dict]:
    """Return broker assets matching *query* by symbol or company name.
    Symbol-prefix matches rank first, then symbol-contains, then name matches."""
    q = (query or "").strip().upper()
    if not q:
        return []
    assets = list_tradable_assets()

    if not assets:
        # No broker list yet (e.g. keys not set) — still let the user pull up a
        # ticker they type exactly, validated later by the data fetch.
        if q.replace(".", "").replace("-", "").isalnum() and len(q) <= 6:
            return [{"symbol": q, "name": "", "exchange": ""}]
        return []

    sym_prefix, sym_contains, name_match = [], [], []
    for a in assets:
        sym = a["symbol"]
        nm  = a["name"].upper()
        if sym == q or sym.startswith(q):
            sym_prefix.append(a)
        elif q in sym:
            sym_contains.append(a)
        elif q in nm:
            name_match.append(a)
        if len(sym_prefix) >= limit:
            break
    out = sym_prefix + sym_contains + name_match
    return out[:limit]


# ══════════════════════════════════════════════════════════════════════════
#  Snapshot (price + fundamentals + recent performance)
# ══════════════════════════════════════════════════════════════════════════

def _f(v):
    """Coerce to float or None."""
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:        # NaN
            return None
        return f
    except Exception:
        return None


def get_snapshot(symbol: str) -> dict:
    """A broker-app-style quote: price, day change, OHLC, fundamentals and a
    few trailing returns. Resilient to yfinance gaps — any missing field is
    simply None."""
    sym = (symbol or "").strip().upper()
    out: dict = {"symbol": sym, "ok": False}
    if not sym:
        out["error"] = "No symbol"
        return out
    try:
        import yfinance as yf
        import numpy as np
        t = yf.Ticker(sym)

        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}

        fi = {}
        try:
            fi = dict(t.fast_info or {})
        except Exception:
            fi = {}

        def pick(*keys):
            for src in (fi, info):
                for k in keys:
                    if k in src and src[k] is not None:
                        v = _f(src[k])
                        if v is not None:
                            return v
            return None

        price     = pick("last_price", "lastPrice", "currentPrice",
                         "regularMarketPrice")
        prev_close = pick("previous_close", "previousClose",
                          "regularMarketPreviousClose")

        # Daily history powers the trailing-return + moving-average stats and is
        # the cheapest reliable source for price when fast_info is empty.
        hist = None
        try:
            hist = t.history(period="1y", interval="1d", auto_adjust=True)
        except Exception:
            hist = None

        closes = None
        if hist is not None and not hist.empty:
            closes = hist["Close"].dropna()
            if price is None and len(closes):
                price = _f(closes.iloc[-1])
            if prev_close is None and len(closes) >= 2:
                prev_close = _f(closes.iloc[-2])

        change = chg_pct = None
        if price is not None and prev_close:
            change  = price - prev_close
            chg_pct = (change / prev_close * 100.0) if prev_close else None

        def ret_over(days: int):
            if closes is None or len(closes) <= days:
                return None
            past = _f(closes.iloc[-days - 1])
            now  = _f(closes.iloc[-1])
            if past and now:
                return (now - past) / past * 100.0
            return None

        ma50 = ma200 = None
        if closes is not None:
            if len(closes) >= 50:
                ma50 = _f(closes.iloc[-50:].mean())
            if len(closes) >= 200:
                ma200 = _f(closes.iloc[-200:].mean())

        out.update({
            "ok":        price is not None,
            "name":      str(info.get("longName") or info.get("shortName") or ""),
            "price":     price,
            "prev_close": prev_close,
            "change":    change,
            "change_pct": chg_pct,
            "currency":  str(info.get("currency") or fi.get("currency") or "USD"),
            "open":      pick("open", "regularMarketOpen", "regularMarketOpen"),
            "day_high":  pick("day_high", "dayHigh", "regularMarketDayHigh"),
            "day_low":   pick("day_low", "dayLow", "regularMarketDayLow"),
            "volume":    pick("last_volume", "regularMarketVolume", "volume"),
            "avg_volume": pick("averageVolume", "average_volume",
                              "averageDailyVolume10Day"),
            "market_cap": pick("market_cap", "marketCap"),
            "pe":        pick("trailingPE"),
            "forward_pe": pick("forwardPE"),
            "eps":       pick("trailingEps"),
            "div_yield": pick("dividendYield"),
            "beta":      pick("beta"),
            "wk_high":   pick("year_high", "fiftyTwoWeekHigh"),
            "wk_low":    pick("year_low",  "fiftyTwoWeekLow"),
            "ma50":      ma50,
            "ma200":     ma200,
            "ret_1m":    ret_over(21),
            "ret_3m":    ret_over(63),
            "ret_6m":    ret_over(126),
            "ret_1y":    ret_over(251),
            "sector":    str(info.get("sector") or ""),
            "industry":  str(info.get("industry") or ""),
            "exchange":  str(info.get("exchange") or fi.get("exchange") or ""),
            "summary":   str(info.get("longBusinessSummary") or ""),
        })
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def get_history(symbol: str, period: str):
    """OHLCV DataFrame for the chart. Returns an empty df on failure."""
    import pandas as pd
    sym = (symbol or "").strip().upper()
    yf_period, interval = PERIODS.get(period, ("6mo", "1d"))
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period=yf_period, interval=interval,
                                    auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.dropna(subset=["Close"])
        return df
    except Exception as e:
        print(f"[stock_research] history {sym} {period} failed: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════
#  Weekly AI evaluation (cached per ISO week — refreshed every Monday)
# ══════════════════════════════════════════════════════════════════════════

_RATINGS = ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL", "NEUTRAL"]


def current_week_monday() -> str:
    today = datetime.date.today()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


def _load_evals() -> dict:
    try:
        return json.loads(_EVALS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_evals(d: dict) -> None:
    try:
        ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
        _EVALS_CACHE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[stock_research] save evals failed: {e}")


def _fmt(v, kind="num"):
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v:+.1f}%"
    if kind == "price":
        return f"${v:,.2f}"
    if kind == "cap":
        for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
            if abs(v) >= div:
                return f"${v / div:.2f}{unit}"
        return f"${v:,.0f}"
    return f"{v:,.2f}"


def _build_eval_prompt(snap: dict) -> str:
    sym = snap.get("symbol", "?")
    name = snap.get("name") or sym
    lines = [
        f"Stock: {sym} ({name})",
        f"Sector: {snap.get('sector') or 'n/a'} / {snap.get('industry') or 'n/a'}",
        f"Price: {_fmt(snap.get('price'), 'price')} "
        f"(today {_fmt(snap.get('change_pct'), 'pct')})",
        f"52-week range: {_fmt(snap.get('wk_low'), 'price')} – "
        f"{_fmt(snap.get('wk_high'), 'price')}",
        f"Market cap: {_fmt(snap.get('market_cap'), 'cap')}   "
        f"P/E: {_fmt(snap.get('pe'))}   EPS: {_fmt(snap.get('eps'))}   "
        f"Beta: {_fmt(snap.get('beta'))}",
        f"Trailing returns — 1M {_fmt(snap.get('ret_1m'), 'pct')}, "
        f"3M {_fmt(snap.get('ret_3m'), 'pct')}, "
        f"6M {_fmt(snap.get('ret_6m'), 'pct')}, "
        f"1Y {_fmt(snap.get('ret_1y'), 'pct')}",
        f"50-day MA: {_fmt(snap.get('ma50'), 'price')}   "
        f"200-day MA: {_fmt(snap.get('ma200'), 'price')}",
    ]
    data_block = "\n".join(lines)
    return (
        "You are a concise equity analyst writing a weekly note for a retail "
        "investor who trades manually. Using ONLY the data below, evaluate this "
        "stock.\n\n"
        "Respond in this exact format:\n"
        "RATING: <one of STRONG BUY, BUY, HOLD, SELL, STRONG SELL>\n"
        "Then 3-5 sentences covering price momentum (vs its 50/200-day MAs and "
        "trailing returns), valuation (P/E vs the move), and the single biggest "
        "risk. Finish with: 'Not financial advice.'\n\n"
        f"DATA:\n{data_block}\n"
    )


def _parse_rating(text: str) -> str:
    head = (text or "").upper()
    # Look at the first ~120 chars so we catch "RATING: BUY" / "BUY -" etc.
    head = head[:120]
    for r in _RATINGS:                       # longest phrases first
        if r in head:
            return r.title()
    return "Review"


def weekly_evaluation(symbol: str, snap: dict | None = None,
                      force: bool = False) -> dict:
    """AI rating + write-up for *symbol*, cached for the current ISO week.

    The first time a stock is opened in a new week (i.e. on/after Monday) a
    fresh evaluation is generated; the rest of the week returns the cache.
    Returns {symbol, week, rating, text, generated_at, cached, error?}."""
    sym = (symbol or "").strip().upper()
    week = current_week_monday()
    cache = _load_evals()
    have = cache.get(sym)
    if (not force and have and have.get("week") == week
            and (have.get("text") or "").strip()):
        return {**have, "cached": True}

    if snap is None:
        snap = get_snapshot(sym)

    # Need an AI provider key to generate. Be explicit when it's missing rather
    # than failing silently — the UI surfaces this as a hint.
    try:
        from core.ai_client import load_ai_config, call_ai_text
        provider, model, api_key, _mode = load_ai_config()
    except Exception as e:
        return {"symbol": sym, "week": week, "rating": "—",
                "text": "", "error": f"AI client unavailable: {e}",
                "generated_at": "", "cached": False}

    if not api_key:
        return {"symbol": sym, "week": week, "rating": "—", "text": "",
                "error": "No AI provider key configured. Add one (e.g. "
                         "ANTHROPIC_API_KEY) in your .env to enable weekly "
                         "stock evaluations.",
                "generated_at": "", "cached": False}

    try:
        prompt = _build_eval_prompt(snap)
        text = call_ai_text(prompt, provider, model, api_key, max_tokens=400)
        rating = _parse_rating(text)
        entry = {
            "symbol":       sym,
            "week":         week,
            "rating":       rating,
            "text":         text.strip(),
            "provider":     provider,
            "model":        model,
            "generated_at": datetime.datetime.now().isoformat(timespec="minutes"),
        }
        cache[sym] = entry
        _save_evals(cache)
        return {**entry, "cached": False}
    except Exception as e:
        return {"symbol": sym, "week": week, "rating": "—", "text": "",
                "error": f"Evaluation failed: {e}",
                "generated_at": "", "cached": False}

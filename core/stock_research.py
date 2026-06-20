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
# Weekly AI evaluations are now generated + cached on the SERVER (shared across
# all users), so there is no local eval cache — see weekly_evaluation().
_ASSETS_CACHE = ACCOUNT_DIR / "stock_assets_cache.json"

_ASSETS_TTL = 24 * 3600            # refresh the broker asset list once a day

# yfinance period selector → (yfinance period, interval). Intraday intervals are
# kept fine-grained so 1D/1W have plenty of candles (≈195 / ≈130 bars).
PERIODS: dict[str, tuple[str, str]] = {
    "1D": ("1d",  "2m"),
    "1W": ("5d",  "15m"),
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


def history_since(symbol: str, start_date):
    """Daily OHLC from *start_date* (a date / 'YYYY-MM-DD' / None) to now. Used
    by the portfolio position-growth chart. Falls back to 1y if no start."""
    import pandas as pd
    sym = (symbol or "").strip().upper()
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        if start_date:
            df = t.history(start=str(start_date)[:10], interval="1d",
                           auto_adjust=True)
        else:
            df = t.history(period="1y", interval="1d", auto_adjust=True)
        if df is None or df.empty:
            df = t.history(period="1y", interval="1d", auto_adjust=True)
        return df.dropna(subset=["Close"]) if df is not None else pd.DataFrame()
    except Exception as e:
        print(f"[stock_research] history_since {sym} failed: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════
#  Portfolio — per-position trade history (markers on the growth curve)
# ══════════════════════════════════════════════════════════════════════════

def position_trades(symbol: str) -> list[dict]:
    """Filled trades for *symbol* on the MANUAL account, oldest first, each
    classified by what it did to the running position:
        buy        — first opening buy
        add        — a buy that increased an existing position
        sell_some  — a sell that left some position
        sell_all   — a sell that closed the position
    Returns [{time, price, kind, qty}]. Empty if no client / no fills."""
    sym = (symbol or "").strip().upper()
    client = manual_client()
    if client is None:
        return []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                               symbols=[sym], limit=500)
        orders = client.get_orders(filter=req) or []
    except Exception as e:
        print(f"[stock_research] get_orders {sym} failed: {e}")
        return []

    fills = []
    for o in orders:
        try:
            fq = float(getattr(o, "filled_qty", 0) or 0)
            fa = getattr(o, "filled_at", None)
            if fq <= 0 or fa is None:
                continue
            px = float(getattr(o, "filled_avg_price", 0) or 0)
            side = getattr(o, "side", None)
            is_buy = (side == OrderSide.BUY) or (str(side).upper().endswith("BUY"))
            fills.append({"time": fa, "price": px, "qty": fq, "is_buy": is_buy})
        except Exception:
            continue

    fills.sort(key=lambda f: str(f["time"]))
    events, running = [], 0.0
    for f in fills:
        if f["is_buy"]:
            kind = "buy" if running <= 1e-9 else "add"
            running += f["qty"]
        else:
            after = running - f["qty"]
            kind = "sell_all" if after <= 1e-9 else "sell_some"
            running = max(0.0, after)
        events.append({"time": f["time"], "price": f["price"],
                       "kind": kind, "qty": f["qty"]})
    return events


# ══════════════════════════════════════════════════════════════════════════
#  This week's suggested stocks (deterministic per ISO week — no AI/API cost)
# ══════════════════════════════════════════════════════════════════════════

# A pool of liquid, widely-followed US names. Each week a deterministic subset
# is featured — identical for every user (seeded by the week), and picking the
# list costs nothing (no AI, no network): evaluations are only generated when a
# user actually opens one of them.
_SUGGESTION_POOL = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD", "NFLX",
    "AVGO", "JPM", "V", "MA", "WMT", "COST", "HD", "DIS", "KO", "PEP", "MCD",
    "NKE", "CRM", "ADBE", "ORCL", "INTC", "QCOM", "TXN", "CSCO", "BA", "CAT",
    "GE", "XOM", "CVX", "UNH", "JNJ", "LLY", "ABBV", "PG", "BAC", "GS",
    "COIN", "PLTR", "UBER", "SHOP", "PYPL", "SBUX",
]


def suggested_symbols(n: int = 8) -> list[dict]:
    """This week's featured stocks as [{symbol, name}], deterministic per ISO
    week so everyone sees the same set. Names are filled from the cached broker
    asset list when available."""
    import random
    week = current_week_monday()
    try:
        seed = int(week.replace("-", ""))
    except Exception:
        seed = 0
    pool = list(_SUGGESTION_POOL)
    random.Random(seed).shuffle(pool)
    picks = pool[:max(1, n)]

    names = {}
    for a in _load_assets_cache():
        names[a.get("symbol", "")] = a.get("name", "")
    return [{"symbol": s, "name": names.get(s, "")} for s in picks]


# ══════════════════════════════════════════════════════════════════════════
#  Weekly AI evaluation (cached per ISO week — refreshed every Monday)
# ══════════════════════════════════════════════════════════════════════════

def current_week_monday() -> str:
    today = datetime.date.today()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


def _cloud_creds() -> tuple:
    """(token, server_url) from the desktop auth files, without importing ui."""
    from core.paths import DATA_DIR
    tok, url = None, "http://localhost:8000"
    try:
        tok = json.loads((DATA_DIR / "apex_auth.json").read_text(
            encoding="utf-8")).get("token")
    except Exception:
        pass
    try:
        url = json.loads((DATA_DIR / "apex_server.json").read_text(
            encoding="utf-8")).get("url", url).rstrip("/")
    except Exception:
        pass
    return tok, url


def weekly_evaluation(symbol: str, snap: dict | None = None,
                      force: bool = False) -> dict:
    """Fetch the SHARED weekly evaluation for *symbol* from the server.

    The server generates one review per AI provider it has a key for (using the
    main account's keys) and caches it per ISO week, so every user sees the same
    multi-model result. The client just sends its market snapshot to seed the
    first generation. Returns the server's structured dict:
        {symbol, week, consensus:{rating,score,n}, models:[...], error?}
    plus a 'cached' flag for the UI."""
    import requests
    sym = (symbol or "").strip().upper()
    week = current_week_monday()
    empty = {"symbol": sym, "week": week,
             "consensus": {"rating": "—", "score": 0.0, "n": 0},
             "models": [], "cached": False}

    tok, url = _cloud_creds()
    if not tok:
        return {**empty, "error": "Sign in to load AI evaluations."}

    if snap is None:
        snap = get_snapshot(sym)

    try:
        r = requests.post(
            f"{url}/stocks/{sym}/evaluation",
            headers={"Authorization": f"Bearer {tok}"},
            json={"snapshot": snap, "force": bool(force)},
            timeout=90,
        )
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            return {**empty, "error": f"Server: {detail}"}
        data = r.json()
        data.setdefault("cached", not force)
        return data
    except Exception as e:
        return {**empty, "error": f"Couldn't reach evaluation server: {e}"}

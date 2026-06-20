"""
IBKR data adapter (ib_async).

Connects to a running TWS / IB Gateway socket and returns account / position
data in the SAME dict shapes as the Alpaca helpers in core.data, so the
overview and bot tabs work unchanged when broker_mode == "ibkr".

Design notes
------------
• IBKR shares ONE account across every bot (unlike Alpaca where each bot has
  its own account). We therefore connect ONCE per mode with a dedicated
  read-only client id, snapshot the whole account, and then scope each bot's
  numbers in Python:
    – equity / cash / buying-power  →  scaled by the bot's allocation %
    – positions                     →  attributed by the bot's universe symbols
• Everything degrades gracefully: if ib_async isn't installed or the gateway
  is unreachable, every function returns an empty / "not connected" result and
  NEVER raises, so the app keeps running when IBKR is configured but offline.

This module talks to a live socket and cannot be exercised without a running
gateway — keep the failure paths defensive.
"""

from __future__ import annotations

import threading
import time

import pandas as pd

import core.data as D

try:
    from ib_async import IB
    HAS_IB = True
except Exception:
    HAS_IB = False


# A dedicated, high client id for read-only data pulls so it never collides
# with a bot that is actively trading on its own client id. Overridable per
# mode via settings["ibkr_<mode>"]["reader_client_id"].
_DEFAULT_READER_CLIENT_ID = 9001

_SNAP_TTL = 5.0                       # seconds — overview hits 3 bots in a row
_snap_cache: dict[str, tuple] = {}    # mode -> (timestamp, snapshot dict)
_snap_lock = threading.Lock()


# ── helpers ─────────────────────────────────────────────────────────────

def _mode() -> str:
    return D.load_settings().get("alpaca_mode", "paper")


def _cfg(mode: str) -> dict:
    s = D.load_settings()
    return s.get(f"ibkr_{mode}", s.get("ibkr", {})) or {}


def _ibkr_bots(mode: str) -> list:
    return [b for b in _cfg(mode).get("bots", []) if isinstance(b, dict)]


def _alloc_fraction(mode: str, side: str) -> float:
    """Fraction (0..1) of the shared account allocated to *side*.
    Falls back to an equal split when no allocations are set, so the overview
    isn't all-zero before the user configures percentages."""
    bots = _ibkr_bots(mode)
    total = 0.0
    mine = None
    for b in bots:
        try:
            pct = float(str(b.get("allocation", "")).rstrip("%") or 0)
        except ValueError:
            pct = 0.0
        total += pct
        if b.get("id") == side:
            mine = pct
    if mine is None:
        return 0.0
    if total > 0:
        return mine / 100.0
    # No explicit allocations → equal split among configured bots
    return (1.0 / len(bots)) if bots else 0.0


def _bot_universe(side: str) -> set:
    """Set of ticker symbols this bot trades (for position attribution).
    Empty set when no universe file exists."""
    try:
        path = D.universe_path_for(side)
        if not path.exists():
            return set()
        out = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            t = line.strip().upper()
            if t and not t.startswith("#"):
                out.add(t.split()[0])           # first token = ticker
        return out
    except Exception:
        return set()


def _pick(values, tag: str, default: float = 0.0) -> float:
    """Pull a numeric account value by tag, preferring the USD row."""
    fallback = None
    for av in values:
        if av.tag != tag:
            continue
        if av.currency == "USD":
            try:
                return float(av.value)
            except (TypeError, ValueError):
                pass
        if fallback is None:
            fallback = av.value
    try:
        return float(fallback) if fallback is not None else default
    except (TypeError, ValueError):
        return default


def _connect(host: str, port, client_id: int):
    """Open a read-only IB connection on this thread. Returns IB or None."""
    if not HAS_IB:
        return None
    import asyncio
    # QThreads start without an event loop; ib_async needs one.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    ib = IB()
    try:
        ib.connect(host or "127.0.0.1", int(port), clientId=int(client_id),
                   timeout=6, readonly=True)
        return ib
    except Exception as e:
        print(f"[ibkr] connect {host}:{port} cid={client_id}: {e}")
        try:
            ib.disconnect()
        except Exception:
            pass
        return None


def _snapshot(mode: str) -> dict:
    """Connect once and snapshot the whole IBKR account. Cached for _SNAP_TTL."""
    with _snap_lock:
        cached = _snap_cache.get(mode)
        if cached and (time.time() - cached[0]) < _SNAP_TTL:
            return cached[1]

    cfg = _cfg(mode)
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", "7497" if mode == "paper" else "7496")
    reader_cid = int(cfg.get("reader_client_id", _DEFAULT_READER_CLIENT_ID))

    ib = _connect(host, port, reader_cid)
    if ib is None:
        snap = {"connected": False, "positions": []}
    else:
        try:
            vals = ib.accountValues()
            net_liq = _pick(vals, "NetLiquidation")
            cash    = _pick(vals, "TotalCashValue")
            bp      = _pick(vals, "BuyingPower")
            # No clean intraday baseline from IBKR — the per-bot snapshot files
            # provide the historical curve; today's equity == last_equity here.
            positions = []
            for it in ib.portfolio():
                mv = float(it.marketValue or 0)
                positions.append({
                    "symbol":          it.contract.symbol,
                    "qty":             float(it.position),
                    "market_value":    mv,
                    "avg_entry_price": float(it.averageCost or 0),
                    "unrealized_pl":   float(it.unrealizedPNL or 0),
                    "unrealized_plpc": (float(it.unrealizedPNL or 0) / abs(mv)) if mv else 0.0,
                    "current_price":   float(it.marketPrice or 0),
                })
            snap = {
                "connected": True,
                "net_liq":   net_liq,
                "cash":      cash,
                "buying_power": bp,
                "positions": positions,
            }
        except Exception as e:
            print(f"[ibkr] snapshot: {e}")
            snap = {"connected": False, "positions": []}
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    with _snap_lock:
        _snap_cache[mode] = (time.time(), snap)
    return snap


def reset() -> None:
    """Drop the cached snapshot — call after IBKR settings change."""
    with _snap_lock:
        _snap_cache.clear()
        _cloud_cache.clear()


# ── cloud path (v4.6.59) ─────────────────────────────────────────────────
# When the user's IBKR bots run 24/7 on the Oracle cloud (run_on_oracle), the
# desktop has NO local gateway to read, so the overview used to show
# "not connected". Instead we pull each bot's sub-portfolio ledger (cash +
# holdings + server-computed slice value) from the APEX server and price the
# holdings locally. The fetch happens on a BACKGROUND thread (the overview
# refresh runs on the UI thread) and is cached, so the UI never blocks — the
# data simply appears on the next refresh tick.

_CLOUD_TTL = 12.0
_cloud_cache: dict[str, tuple] = {}     # mode -> (ts, {SIDE: {account, positions}})
_cloud_inflight: set = set()
_whole_account: dict[str, dict] = {}    # mode -> {net_liq, cash, updated} (V4.6.111)
_bots_pl: dict[str, tuple] = {}         # mode -> (pl_sum, allocated_sum) (V4.6.111)


def bots_total_pl(mode: str | None = None):
    """V4.6.111 — true lifetime P/L across all IBKR bots as (pl, base), where
    pl = Σ(value − allocated) and base = Σ allocated. Deposit-proof, unlike the
    combined-equity baseline. (0, 0) when no ledgers have been read yet."""
    m = (mode or _mode())
    with _snap_lock:
        return _bots_pl.get(m, (0.0, 0.0))


def whole_account_value(mode: str | None = None) -> float:
    """V4.6.111 — the FULL IBKR account NetLiquidation (every bot's slice plus
    any unallocated cash), as last snapshotted by a running bot. 0.0 when no
    snapshot exists yet (gateway never came up this session)."""
    m = (mode or _mode())
    with _snap_lock:
        return float((_whole_account.get(m) or {}).get("net_liq", 0.0) or 0.0)

_PRICE_TTL = 60.0
_price_cache: dict[str, tuple] = {}     # SYM -> (ts, price)


def _cloud_enabled(mode: str) -> bool:
    return bool(_cfg(mode).get("run_on_oracle"))


def _cloud_creds() -> tuple:
    """(token, server_url) from the desktop auth files, without importing ui."""
    import json
    from core.paths import DATA_DIR
    tok = None
    url = "http://localhost:8000"
    try:
        with open(DATA_DIR / "apex_auth.json", encoding="utf-8") as f:
            tok = json.load(f).get("token")
    except Exception:
        pass
    try:
        with open(DATA_DIR / "apex_server.json", encoding="utf-8") as f:
            url = json.load(f).get("url", url).rstrip("/")
    except Exception:
        pass
    return tok, url


def _quick_price(sym: str) -> float:
    """Best-effort latest price for a holding symbol (cached 60s). Maps crypto
    tickers to yfinance form (BTC -> BTC-USD). Returns 0.0 on failure."""
    key = str(sym).upper()
    now = time.time()
    c = _price_cache.get(key)
    if c and (now - c[0]) < _PRICE_TTL:
        return c[1]
    px = 0.0
    try:
        import yfinance as yf
        s = key.replace("/", "")
        # crypto: BTC / BTCUSD -> BTC-USD ; leave normal tickers alone
        cryptoish = s.endswith("USD") and len(s) > 3
        yf_sym = f"{s[:-3]}-USD" if cryptoish else key
        h = yf.Ticker(yf_sym).history(period="1d")
        if not h.empty:
            px = float(h["Close"].iloc[-1])
    except Exception:
        px = 0.0
    _price_cache[key] = (now, px)
    return px


def _build_cloud_data(mode: str) -> dict:
    """Fetch /ibkr/ledgers and build {SIDE: {account, positions}} by pricing
    each bot's holdings locally. Runs on a background thread."""
    tok, url = _cloud_creds()
    if not tok:
        return {}
    try:
        import requests
        rr = requests.get(f"{url}/ibkr/ledgers",
                          headers={"Authorization": f"Bearer {tok}"},
                          timeout=10)
        if not rr.ok:
            return {}
        _body  = rr.json()
        ledgers = _body.get("ledgers", []) or []
        # V4.6.111 — whole-account NetLiquidation snapshot (bots + unallocated
        # cash) so the desktop can show the FULL account, not just bot slices.
        acct = _body.get("account") or {}
        if float(acct.get("net_liq", 0) or 0) > 0:
            with _snap_lock:
                _whole_account[mode] = acct
    except Exception as e:
        print(f"[ibkr] cloud ledgers fetch: {e}")
        return {}

    # V4.6.111 — true lifetime P/L across the bots = Σ(current value − capital
    # allocated at creation). The combined-equity baseline counts capital ADDED
    # when later bots were created as "profit" (hence the bogus +40%); comparing
    # each slice to its own allocation is deposit-proof.
    _pl_sum = 0.0
    _alloc_sum = 0.0
    for led in ledgers:
        v = float(led.get("value", 0) or 0)
        a = float(led.get("allocated", 0) or 0)
        if a > 0:
            _pl_sum    += (v - a)
            _alloc_sum += a
    if _alloc_sum > 0:
        with _snap_lock:
            _bots_pl[mode] = (_pl_sum, _alloc_sum)

    out: dict = {}
    for led in ledgers:
        bid = str(led.get("bot_id") or "").upper()
        if not bid:
            # derive from file stem like "day_paper" -> "DAY"
            stem = str(led.get("file", ""))
            bid = stem.split("_")[0].upper() if stem else ""
        if not bid:
            continue
        cash = float(led.get("cash", 0.0) or 0.0)
        holdings = led.get("holdings", {}) or {}
        marks = led.get("marks", {}) or {}
        basis = led.get("cost_basis", {}) or {}
        positions = []
        held_val = 0.0
        for sym, qty in holdings.items():
            try:
                q = float(qty)
            except (TypeError, ValueError):
                continue
            if abs(q) <= 1e-9:
                continue
            # V4.6.61/108 — entry price precedence:
            #   1) the slice's own recorded cost basis (per-bot correct),
            #   2) the bot's captured mark avg_entry (only if it differs from
            #      price — i.e. a REAL cost, not the break-even fallback),
            #   3) a live yfinance price (break-even) as the last resort.
            # Always price the current value with a LIVE quote so the gauge's
            # current→entry distance (the % P/L) reflects today's market.
            mk = marks.get(sym) or marks.get(str(sym).upper()) or {}
            px = (float(mk.get("price", 0) or 0)) or _quick_price(sym)
            cb = float(basis.get(sym, 0) or basis.get(str(sym).upper(), 0) or 0)
            mk_entry = float(mk.get("avg_entry", 0) or 0)
            mk_price = float(mk.get("price", 0) or 0)
            entry = cb
            if entry <= 0:
                # only trust mark avg_entry if it isn't the break-even fallback
                entry = mk_entry if (mk_entry > 0 and abs(mk_entry - mk_price) > 1e-6) else 0.0
            if entry <= 0:
                entry = px
            mv    = q * px
            upl   = (px - entry) * q
            plpc  = (upl / (entry * abs(q))) if (entry and q) else 0.0
            held_val += mv
            positions.append({
                "symbol":          sym,
                "qty":             q,
                "market_value":    mv,
                "avg_entry_price": entry,
                "unrealized_pl":   upl,
                "unrealized_plpc": plpc,
                "current_price":   px,
            })
        # Headline equity: trust the server-computed slice value (the bot
        # priced it with its live feed). Fall back to locally-priced holdings
        # only if the server hasn't snapshotted a value yet.
        equity = float(led.get("value", 0.0) or 0.0)
        if equity <= 0:
            equity = cash + held_val
        out[bid] = {
            "account": {
                "portfolio_value": equity,
                "equity":          equity,
                "cash":            cash,
                "buying_power":    cash,
                "last_equity":     equity,
                # V4.6.120 — capital allocated to this slice. The desktop records
                # it in snapshots so a re-allocation (deposit/withdrawal of the
                # slice's capital) can be excluded from P/L.
                "allocated":       float(led.get("allocated", 0.0) or 0.0),
                "connected":       True,
            },
            "positions": positions,
        }
    return out


def _cloud_data(mode: str):
    """Non-blocking accessor: return the cached cloud per-side data and kick
    off a background refresh when it's stale. None when cloud mode is off."""
    if not _cloud_enabled(mode):
        return None
    now = time.time()
    with _snap_lock:
        c = _cloud_cache.get(mode)
        fresh = c and (now - c[0]) < _CLOUD_TTL
        if not fresh and mode not in _cloud_inflight:
            _cloud_inflight.add(mode)

            def _refresh():
                try:
                    data = _build_cloud_data(mode)
                    with _snap_lock:
                        _cloud_cache[mode] = (time.time(), data)
                finally:
                    with _snap_lock:
                        _cloud_inflight.discard(mode)
            threading.Thread(target=_refresh, daemon=True).start()
        return c[1] if c else None


def available_cash(mode: str | None = None) -> float:
    """Whole-account free cash (TotalCashValue), used by the Tools tab to
    size new sub-portfolio allocations.  0.0 when the gateway is offline."""
    snap = _snapshot(mode or _mode())
    return float(snap.get("cash", 0.0)) if snap.get("connected") else 0.0


# ── ledger-aware scoping (v4.6.38) ──────────────────────────────────────

def _ledger_for(mode: str, side: str):
    """This bot's sub-portfolio ledger, or None when isolation isn't in use."""
    try:
        from core.ledger import ledger_path, Ledger
        from core.paths import ACCOUNT_DIR as DATA_DIR   # V4.6.101 account-scoped
        return Ledger.load(ledger_path(side, "ibkr", mode, DATA_DIR))
    except Exception:
        return None


def _price_map(snap: dict) -> dict:
    out = {}
    for p in snap.get("positions", []):
        try:
            out[str(p["symbol"]).upper()] = float(p.get("current_price", 0) or 0)
        except Exception:
            continue
    return out


# ── public API (mirrors core.data) ──────────────────────────────────────

def get_account(side: str) -> dict:
    mode = _mode()
    # Cloud bots (run on Oracle): read the per-bot ledger from the server
    # instead of a local gateway that doesn't exist on this machine.
    if _cloud_enabled(mode):
        cd = _cloud_data(mode)
        if cd and side.upper() in cd:
            return cd[side.upper()]["account"]
        return {"connected": False}
    snap = _snapshot(mode)
    if not snap.get("connected"):
        return {"connected": False}
    # V4.6.38 — prefer the bot's ledger so the overview shows its ACTUAL
    # sub-portfolio (free cash + held shares), not an allocation-% estimate.
    led = _ledger_for(mode, side)
    if led is not None:
        pm = _price_map(snap)
        holdings_val = sum(
            qty * pm.get(sym.upper(), 0.0)
            for sym, qty in led.holdings.items())
        equity = led.cash + holdings_val
        return {
            "portfolio_value": equity,
            "equity":          equity,
            "cash":            led.cash,
            "buying_power":    led.cash,
            "last_equity":     equity,
            "connected":       True,
        }
    frac = _alloc_fraction(mode, side)
    equity = snap["net_liq"] * frac
    return {
        "portfolio_value": equity,
        "equity":          equity,
        "cash":            snap["cash"] * frac,
        "buying_power":    snap["buying_power"] * frac,
        "last_equity":     equity,
        "connected":       True,
    }


def get_positions(side: str) -> list:
    mode = _mode()
    if _cloud_enabled(mode):
        cd = _cloud_data(mode)
        if cd and side.upper() in cd:
            return cd[side.upper()]["positions"]
        return []
    snap = _snapshot(mode)
    if not snap.get("connected"):
        return []
    all_pos = snap.get("positions", [])
    # V4.6.38 — ledger-scoped positions: show exactly the shares in this
    # bot's slice, priced from the live account snapshot.
    led = _ledger_for(mode, side)
    if led is not None:
        pm = _price_map(snap)
        marks = getattr(led, "marks", {}) or {}
        out = []
        for sym, qty in led.holdings.items():
            if abs(qty) <= 1e-9:
                continue
            # V4.6.61 — use the bot's exact captured marks (real average cost +
            # unrealized P/L) when present; else fall back to the snapshot price.
            mk = marks.get(str(sym).upper()) or {}
            px    = float(mk.get("price", 0) or 0) or pm.get(str(sym).upper(), 0.0)
            entry = float(mk.get("avg_entry", 0) or 0) or px
            mv    = float(mk.get("mv", 0) or 0) or (qty * px)
            upl   = float(mk.get("upl", 0) or 0)
            plpc  = (upl / (entry * abs(qty))) if (entry and qty) else 0.0
            out.append({
                "symbol":          sym,
                "qty":             qty,
                "market_value":    mv,
                "avg_entry_price": entry,
                "unrealized_pl":   upl,
                "unrealized_plpc": plpc,
                "current_price":   px,
            })
        return out
    universe = _bot_universe(side)
    if universe:
        return [p for p in all_pos if p["symbol"] in universe]
    # No universe to attribute by: if this is the only IBKR bot, it owns the
    # whole account; otherwise we can't safely attribute, so show nothing.
    if len(_ibkr_bots(mode)) <= 1:
        return all_pos
    return []


def get_history(side: str, period: str) -> pd.DataFrame:
    """IBKR has no portfolio-equity-curve endpoint, so build the curve from
    this bot's locally-recorded equity snapshots (the same file the overview
    appends to). Filtered to the requested period. Empty until at least one
    snapshot exists. V4.6.60."""
    try:
        snaps = D.read_bot_snapshots(side)
    except Exception:
        snaps = []
    if not snaps:
        return pd.DataFrame()
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    # V4.6.107 — "ALL" (and any unknown period) means the WHOLE history, not a
    # 1-day fallback. The old dict had no "ALL" key, so the default `.get(...,1)`
    # collapsed the chart to the last day (the overview defaults to ALL), which
    # is why the equity/value curve "only showed the last day".
    days = {"1D": 1, "1W": 7, "1M": 30, "3M": 90,
            "6M": 180, "1Y": 365}.get(period)
    cutoff = None if days is None else (_dt.now(_tz.utc) - _td(days=days))
    rows = []
    for s in snaps:
        try:
            ts = _dt.fromisoformat(s["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            if cutoff is None or ts >= cutoff:
                rows.append((ts, float(s.get("equity", 0) or 0)))
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "equity"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df

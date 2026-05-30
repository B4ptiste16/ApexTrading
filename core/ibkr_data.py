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
        from core.paths import DATA_DIR
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
    snap = _snapshot(mode)
    if not snap.get("connected"):
        return []
    all_pos = snap.get("positions", [])
    # V4.6.38 — ledger-scoped positions: show exactly the shares in this
    # bot's slice, priced from the live account snapshot.
    led = _ledger_for(mode, side)
    if led is not None:
        pm = _price_map(snap)
        out = []
        for sym, qty in led.holdings.items():
            if abs(qty) <= 1e-9:
                continue
            px = pm.get(sym.upper(), 0.0)
            mv = qty * px
            out.append({
                "symbol":          sym,
                "qty":             qty,
                "market_value":    mv,
                "avg_entry_price": 0.0,
                "unrealized_pl":   0.0,
                "unrealized_plpc": 0.0,
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
    """IBKR has no simple portfolio-equity-curve endpoint. Return empty so the
    chart falls back to the per-bot lifetime snapshot files."""
    return pd.DataFrame()

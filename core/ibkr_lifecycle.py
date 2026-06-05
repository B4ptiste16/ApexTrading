"""
APEX · IBKR per-bot ledger lifecycle  (v4.6.38)
──────────────────────────────────────────────────────────────────────
The Tools → IBKR table is where a bot's sub-portfolio is born and dies:

  • ADD a bot      → seed a ledger with (allocation% × available account cash).
  • REMOVE a bot   → market-liquidate its entire sub-portfolio, then delete the
                     ledger so the freed cash is available for manual
                     redistribution.
  • REPLACE a bot  → hand the slot's ledger (cash + shares) to the new bot, which
                     decides on its first cycle what to keep or sell.

All ledger files live at <root>/ibkr/ledgers/<side>_<mode>.json — the SAME path
the bot subprocess reads via core.ledger.get_ledger(), because APEX_DATA_DIR is
the root and the ledger path appends /ibkr/ledgers itself.

The liquidation path opens a *writable* IB connection and cannot be tested
without a running gateway, so every failure is caught and surfaced to the
caller rather than raised — and on failure the ledger is KEPT so positions are
never silently orphaned.
"""

from __future__ import annotations

import time

import core.data as D
from core.paths import DATA_DIR
from core.ledger import Ledger, ledger_path, normalize_symbol

_EPS = 1e-9
_BROKER = "ibkr"


# ── settings helpers ────────────────────────────────────────────────────

def _mode() -> str:
    return D.load_settings().get("alpaca_mode", "paper")


def _cfg(mode: str) -> dict:
    s = D.load_settings()
    return s.get(f"ibkr_{mode}", s.get("ibkr", {})) or {}


def _client_id_for(mode: str, side: str, default: int = 11) -> int:
    for b in _cfg(mode).get("bots", []) or []:
        if isinstance(b, dict) and str(b.get("id", "")).upper() == side.upper():
            try:
                return int(b.get("client_id", default))
            except (TypeError, ValueError):
                return default
    return default


def _path(side: str, mode: str | None = None):
    mode = mode or _mode()
    return ledger_path(side, _BROKER, mode, DATA_DIR)


# ── add: seed a ledger ───────────────────────────────────────────────────

def seed_ledger(side: str, allocation_pct, available_cash: float,
                mode: str | None = None) -> Ledger | None:
    """Create the bot's ledger if it doesn't already exist, granting it
    allocation% of the available account cash.  Existing ledgers are left
    untouched so a running balance is never reset.  Returns the ledger (new
    or existing), or None if the allocation is zero/blank."""
    mode = mode or _mode()
    p = _path(side, mode)
    existing = Ledger.load(p)
    if existing is not None:
        return existing
    try:
        pct = float(str(allocation_pct).rstrip("%") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        return None
    grant = max(0.0, pct / 100.0 * float(available_cash or 0))
    return Ledger.create(p, bot_id=side, allocated_cash=grant)


def seed_all(rows: list, available_cash: float, mode: str | None = None) -> int:
    """Seed ledgers for every (side, allocation%) row that lacks one.
    `rows` is a list of {"id":side, "allocation":pct}.  Returns how many
    new ledgers were created."""
    mode = mode or _mode()
    created = 0
    for r in rows:
        side = str(r.get("id", "")).strip()
        if not side:
            continue
        p = _path(side, mode)
        if p.exists():
            continue
        led = seed_ledger(side, r.get("allocation", ""), available_cash, mode)
        if led is not None:
            created += 1
    return created


# ── replace: transfer the slot's ledger to the new bot ───────────────────

def transfer_ledger(old_side: str, new_side: str,
                    mode: str | None = None) -> Ledger | None:
    """Move the slot's ledger from old_side → new_side, preserving cash and
    shares so the incoming bot inherits the sub-portfolio.  Returns the new
    ledger, or None if the old bot had no ledger."""
    mode = mode or _mode()
    op = _path(old_side, mode)
    led = Ledger.load(op)
    if led is None:
        return None
    np = _path(new_side, mode)
    led.bot_id = new_side
    led.path = np
    led.save()
    if op != np:
        try:
            op.unlink()
        except Exception:
            pass
    return led


# ── remove: liquidate the whole sub-portfolio then delete the ledger ─────

def liquidate_and_remove(side: str, mode: str | None = None) -> tuple[bool, str]:
    """Market-sell (longs) / buy-to-cover (shorts) every share in the bot's
    sub-portfolio, then delete the ledger.  Returns (ok, message).

    On any failure the ledger is KEPT and ok=False so the caller can abort the
    row removal — never orphan real positions."""
    mode = mode or _mode()
    p = _path(side, mode)
    led = Ledger.load(p)
    if led is None:
        return True, "No ledger for this bot — nothing to liquidate."

    holdings = {s: q for s, q in led.holdings.items() if abs(q) > _EPS}
    if not holdings:
        led.delete()
        return True, "Ledger had no open positions — cash freed."

    try:
        from ib_async import IB, Stock, Crypto, MarketOrder
    except Exception as e:
        return False, (f"ib_async unavailable ({e}); ledger kept so "
                       f"positions aren't orphaned.")

    cfg = _cfg(mode)
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", "7497" if mode == "paper" else "7496"))
    cid = _client_id_for(mode, side)

    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    ib = IB()
    try:
        ib.connect(host, port, clientId=cid, timeout=15, readonly=False)
    except Exception as e:
        return False, (f"Could not reach IB Gateway at {host}:{port} ({e}). "
                       f"Start the gateway and remove again — ledger kept.")

    # crypto vs equity contract per the bot's declared asset type
    is_crypto = _is_crypto_bot(side)
    failures = []
    try:
        for sym, qty in holdings.items():
            action = "SELL" if qty > 0 else "BUY"
            contract = (Crypto(sym, "PAXOS", "USD") if is_crypto
                        else Stock(sym, "SMART", "USD"))
            try:
                ib.qualifyContracts(contract)
                ib.placeOrder(contract, MarketOrder(action, abs(qty)))
                ib.sleep(1)
            except Exception as e:
                failures.append(f"{sym}: {e}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    if failures:
        return False, ("Some positions could not be liquidated "
                       f"({'; '.join(failures)}). Ledger kept — retry.")
    led.delete()
    return True, (f"Liquidated {len(holdings)} position(s); "
                  f"cash freed for redistribution.")


def free_ibkr_allocation(side: str) -> tuple[bool, str]:
    """V4.6.70 — called when a bot is DELETED. Sells the bot's IBKR holdings,
    removes its sub-portfolio ledger, and drops it from the Tools allocation
    table (settings[ibkr_<mode>]['bots']) so its funds are freed. Handles both
    a locally-run gateway and a cloud (Oracle) bot. Best-effort + never raises."""
    import json as _json
    import core.data as _D
    side_u = side.upper()
    mode = _mode()
    s = _D.load_settings()
    cfg = s.get(f"ibkr_{mode}", {}) or {}
    is_cloud = bool(cfg.get("run_on_oracle"))

    msg = ""
    try:
        if is_cloud:
            # Liquidate + delete the ledger on the server (where the gateway is)
            from core.paths import DATA_DIR
            import urllib.request
            tok = url = None
            try:
                with open(DATA_DIR / "apex_auth.json", encoding="utf-8") as f:
                    tok = _json.load(f).get("token")
                with open(DATA_DIR / "apex_server.json", encoding="utf-8") as f:
                    url = _json.load(f).get("url", "").rstrip("/")
            except Exception:
                pass
            if tok and url:
                try:
                    import requests
                    r = requests.post(f"{url}/ibkr/{side_u}/liquidate",
                                      params={"mode": mode},
                                      headers={"Authorization": f"Bearer {tok}"},
                                      timeout=45)
                    j = r.json() if r.content else {}
                    msg = j.get("detail", "")
                except Exception as e:
                    msg = f"cloud liquidation error: {e}"
        else:
            ok, info = liquidate_and_remove(side_u, mode)
            msg = info
    except Exception as e:
        msg = f"liquidation error: {e}"

    # Drop the bot from the allocation table regardless, so a deleted bot never
    # keeps showing allocated funds.
    try:
        bots = cfg.get("bots") or []
        new = [b for b in bots
               if str(b.get("id", "")).upper() != side_u]
        if len(new) != len(bots):
            cfg["bots"] = new
            s[f"ibkr_{mode}"] = cfg
            with open(_D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _json.dump(s, f, indent=2)
    except Exception as e:
        msg = (msg + f"  (allocation cleanup error: {e})").strip()
    return True, (msg or f"{side_u} removed from IBKR allocation.")


def _is_crypto_bot(side: str) -> bool:
    """Best-effort asset-type sniff so liquidation builds the right contract.
    Built-ins are equities; custom bots declare asset_type in their registry
    entry."""
    s = side.upper()
    if s in ("LONG", "SHORT", "DAY"):
        return False
    try:
        for c in D.load_all_custom_bots():
            if isinstance(c, dict) and str(c.get("id", "")).upper() == s:
                return str(c.get("asset_type", "")).lower() == "crypto"
    except Exception:
        pass
    return False

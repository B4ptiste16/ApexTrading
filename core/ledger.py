"""
APEX · Per-bot sub-portfolio ledger  (v4.6.38)
──────────────────────────────────────────────────────────────────────
IBKR exposes ONE shared brokerage account.  To let several bots trade the
same IBKR account without stepping on each other, each bot is given a
*sub-portfolio*: a starting cash allocation (and optionally shares it
inherits from a bot it replaced).  A bot may only ever spend its own cash
and sell its own shares — it can never see or touch another bot's slice.

This module is the source of truth for what a bot "owns".  The IBKR broker
shim (core.broker_client._IBKRShim) consults the ledger so a bot's
get_account / get_all_positions reflect ONLY its slice, and every fill is
written back so the slice stays accurate.

Alpaca is unaffected: there, each Alpaca account already IS the bot's whole
world, so no ledger is used.  get_ledger() returns None whenever the broker
isn't IBKR or no ledger file has been created — the shim then falls back to
the historic whole-account behavior, so nothing regresses.

Ledger file (one per broker + mode + bot side):
    <data_dir>/<broker>/ledgers/<side>_<mode>.json
{
  "bot_id":         "LONG",
  "allocated_cash": 1000.0,    # initial grant — never changes after create
  "cash":           742.13,    # free cash remaining in this slice
  "holdings":       {"AAPL": 5.0, "BTC": 0.0125},  # qty per symbol (neg = short)
  "created":        "2026-05-29T23:40:00+00:00",
  "updated":        "2026-05-29T23:55:00+00:00"
}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_EPS = 1e-9


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def normalize_symbol(symbol: str) -> str:
    """Collapse the various symbol spellings to the plain IBKR ticker the
    ledger stores.  yfinance 'BTC-USD' and Alpaca 'BTC/USD' both become
    'BTC'; equities pass through unchanged ('AAPL' → 'AAPL')."""
    s = str(symbol or "").upper().strip()
    if "/" in s:
        s = s.split("/", 1)[0]
    elif s.endswith("-USD"):
        s = s[:-4]
    return s


def ledger_path(side: str,
                broker: str,
                mode: str,
                data_dir: str | Path) -> Path:
    """Resolve the ledger file path for one (side, broker, mode)."""
    base = Path(data_dir) / broker / "ledgers"
    return base / f"{side.lower()}_{mode.lower()}.json"


class Ledger:
    """A bot's sub-portfolio: free cash + per-symbol share holdings.

    All mutating methods persist immediately so a bot crash can't lose the
    slice's accounting.  Holdings of (near) zero are pruned so an empty
    slice reads cleanly.
    """

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.bot_id = data.get("bot_id", "")
        self.allocated_cash = float(data.get("allocated_cash", 0.0))
        self.cash = float(data.get("cash", 0.0))
        self.holdings: dict[str, float] = {
            normalize_symbol(k): float(v)
            for k, v in (data.get("holdings") or {}).items()
        }
        self.created = data.get("created", "")
        self.updated = data.get("updated", "")
        # V4.6.51 — last priced total value of the slice (cash + holdings MV),
        # written by the bot each cycle so the desktop can show a LIVE
        # allocation % that grows/shrinks with the bot's performance.
        self.last_value = float(data.get("last_value", 0.0))
        # V4.6.61 — per-holding live marks captured from the IBKR account each
        # cycle: {SYM: {"avg_entry": x, "price": y, "mv": z, "upl": p}}. Lets the
        # desktop render EXACT entry price + unrealized P/L for cloud bots (the
        # holdings dict alone has only quantity, no cost basis).
        self.marks: dict[str, dict] = {
            normalize_symbol(k): dict(v)
            for k, v in (data.get("marks") or {}).items()
            if isinstance(v, dict)
        }
        # V4.6.108 — per-slice average ENTRY price, maintained from THIS bot's
        # own fills (weighted avg on adds, preserved on partial exits, dropped on
        # flat). This is the source of truth for a bot's entry — unlike IBKR's
        # account-level `averageCost`, which is BLENDED across every bot that
        # holds the same symbol (and was coming back 0/empty for cloud bots,
        # leaving the position gauge stuck at 0%).
        self.cost_basis: dict[str, float] = {
            normalize_symbol(k): float(v)
            for k, v in (data.get("cost_basis") or {}).items()
            if _is_num(v)
        }
        # V4.6.128 — daily P/L baseline: the slice's value as of the previous
        # trading day's close, captured at the first pricing of each new day.
        # Display-only (never touches cash/holdings) — lets the desktop show a
        # real DAY P/L for cloud bots, which otherwise reported last_equity ==
        # equity (so DAY P/L was always 0).
        self.day_baseline = float(data.get("day_baseline", 0.0) or 0.0)
        self.day_baseline_date = str(data.get("day_baseline_date", "") or "")

    def roll_day_baseline(self, current_value: float,
                          today: Optional[str] = None) -> float:
        """On the first call of a new (US/Eastern) trading day, snapshot the
        slice's PRIOR value (yesterday's last_value) as the day's baseline.
        Returns the current baseline. Persists only the two baseline fields."""
        try:
            if today is None:
                from datetime import timedelta
                now = datetime.now(timezone.utc)
                off = -4 if 3 <= now.month <= 11 else -5
                today = (now + timedelta(hours=off)).date().isoformat()
            if self.day_baseline_date != today:
                prior = float(self.last_value or 0.0)
                self.day_baseline = prior if prior > 0 else float(current_value or 0.0)
                self.day_baseline_date = today
                self.save()
        except Exception as e:
            print(f"  [ledger] roll_day_baseline failed: {e}", flush=True)
        return self.day_baseline

    # ── persistence ─────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> Optional["Ledger"]:
        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return cls(p, data)

    @classmethod
    def create(cls, path: Path, *,
               bot_id: str,
               allocated_cash: float,
               holdings: Optional[dict] = None) -> "Ledger":
        """Create (and persist) a fresh ledger.  A replacement bot passes
        the outgoing bot's `holdings` so it inherits the slot's shares; a
        brand-new bot passes none and starts cash-only."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        led = cls(path, {
            "bot_id": bot_id,
            "allocated_cash": float(allocated_cash),
            "cash": float(allocated_cash),
            "holdings": holdings or {},
            "created": now,
            "updated": now,
        })
        led.save()
        return led

    def save(self) -> None:
        self.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._prune()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "bot_id": self.bot_id,
                "allocated_cash": self.allocated_cash,
                "cash": self.cash,
                "holdings": self.holdings,
                "created": self.created,
                "updated": self.updated,
                "last_value": self.last_value,
                "marks": self.marks,
                "cost_basis": self.cost_basis,
                "day_baseline": self.day_baseline,
                "day_baseline_date": self.day_baseline_date,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  [ledger] save failed: {e}", flush=True)

    def delete(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as e:
            print(f"  [ledger] delete failed: {e}", flush=True)

    # ── queries ─────────────────────────────────────────────────────

    def holding(self, symbol: str) -> float:
        return self.holdings.get(normalize_symbol(symbol), 0.0)

    def symbols(self) -> list[str]:
        return [s for s, q in self.holdings.items() if abs(q) > _EPS]

    def can_buy(self, cost: float) -> bool:
        return cost <= self.cash + _EPS

    def affordable_qty(self, price: float) -> float:
        """Largest qty this slice can afford at `price` (0 if price<=0)."""
        if price <= 0:
            return 0.0
        return max(0.0, self.cash / price)

    # ── mutations (each persists) ───────────────────────────────────

    def _update_basis(self, sym: str, delta: float, price: float) -> None:
        """V4.6.108 — maintain this slice's average entry for `sym` given a
        SIGNED quantity change `delta` (+buy/cover, −sell/short) at `price`.
        Must be called BEFORE `holdings` is mutated (it reads the old qty).

        Adds in the same direction → weighted average; opening or flipping
        direction → fresh price; partial exit → keep the existing average;
        flat → drop. Works identically for longs (positive) and shorts
        (negative), so the entry is always the price the slice paid/received."""
        price = abs(float(price or 0.0))
        old = self.holdings.get(sym, 0.0)
        new = old + delta
        if abs(new) <= _EPS:
            self.cost_basis.pop(sym, None)
            return
        if price <= 0:
            return
        old_avg = self.cost_basis.get(sym, 0.0)
        opening = abs(old) <= _EPS or (old > 0) != (new > 0)
        if opening:
            self.cost_basis[sym] = price
        elif abs(new) > abs(old) + _EPS:        # adding in same direction
            if old_avg > 0:
                self.cost_basis[sym] = (
                    abs(old) * old_avg + abs(delta) * price) / abs(new)
            else:                               # no prior basis — best estimate
                self.cost_basis[sym] = price
        # else: partial exit → keep old_avg unchanged

    def record_buy(self, symbol: str, qty: float, price: float) -> None:
        sym = normalize_symbol(symbol)
        self._update_basis(sym, qty, price)
        self.cash -= qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) + qty
        self.save()

    def record_sell(self, symbol: str, qty: float, price: float) -> None:
        sym = normalize_symbol(symbol)
        self._update_basis(sym, -qty, price)
        self.cash += qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) - qty
        self.save()

    def record_short(self, symbol: str, qty: float, price: float) -> None:
        """Sell-to-open: proceeds add to cash, holdings go negative."""
        sym = normalize_symbol(symbol)
        self._update_basis(sym, -qty, price)
        self.cash += qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) - qty
        self.save()

    def record_cover(self, symbol: str, qty: float, price: float) -> None:
        """Buy-to-cover: pays cash, brings a negative holding back up."""
        sym = normalize_symbol(symbol)
        self._update_basis(sym, qty, price)
        self.cash -= qty * price
        self.holdings[sym] = self.holdings.get(sym, 0.0) + qty
        self.save()

    # ── cost-basis backfill (V4.6.108) ──────────────────────────────────

    def backfill_basis_from_fills(self, fills_path: Optional[Path] = None) -> bool:
        """Reconstruct missing average-entry prices for currently-held symbols
        by replaying this slice's fills log. Used to heal positions that were
        opened before per-slice cost tracking existed. Returns True if anything
        was filled in. Only fills GAPS — never overwrites a live basis."""
        missing = {s: q for s, q in self.holdings.items()
                   if abs(q) > _EPS and self.cost_basis.get(s, 0.0) <= 0}
        if not missing:
            return False
        if fills_path is None:
            fills_path = self.path.with_name(self.path.stem + ".fills.jsonl")
        replayed = replay_fills_basis(fills_path)
        changed = False
        for s, hq in missing.items():
            info = replayed.get(s)
            if not info:
                continue
            avg = float(info.get("avg", 0) or 0)
            rq = float(info.get("qty", 0) or 0)
            # only trust a reconstructed entry when the fills log reproduces this
            # holding (the log began at v4.6.63, so older opens may be missing —
            # a partial log yields a phantom-short basis we must not record)
            if avg > 0 and (hq > 0) == (rq > 0) and abs(abs(rq) - abs(hq)) <= max(
                    0.01 * abs(hq), 1e-6):
                self.cost_basis[s] = avg
                changed = True
        if changed:
            self.save()
        return changed

    # ── valuation + rebalancing (V4.6.51) ───────────────────────────

    def value(self, price_of) -> float:
        """Total live value of this slice = free cash + market value of the
        shares it holds. `price_of(symbol)` returns the current price."""
        v = self.cash
        for sym, qty in self.holdings.items():
            if abs(qty) > _EPS:
                try:
                    v += qty * float(price_of(sym) or 0.0)
                except Exception:
                    pass
        return v

    def withdraw(self, amount: float, price_of, sell) -> float:
        """Free up to `amount` of cash from this slice and hand it back to the
        main (unallocated) account. Spends free cash first, then SELLS holdings
        — largest market value first ("decides what to sell") — calling
        sell(symbol, qty) to place each real order. The sale proceeds are
        withdrawn (not re-added to the slice), so the slice's total value drops
        by ~`amount`. Returns the amount actually freed."""
        amount = max(0.0, float(amount or 0.0))
        if amount <= _EPS:
            return 0.0
        freed = 0.0
        # 1) hand back free cash first
        take = min(max(0.0, self.cash), amount)
        if take > _EPS:
            self.cash -= take
            freed += take
        remaining = amount - freed
        if remaining <= _EPS:
            self.save()
            return freed
        # 2) sell holdings, largest market value first, until raised
        longs = []
        for s, q in self.holdings.items():
            if q > _EPS:
                px = float(price_of(s) or 0.0)
                if px > 0:
                    longs.append((s, q, px, q * px))
        longs.sort(key=lambda t: t[3], reverse=True)
        for sym, qty, px, mv in longs:
            if remaining <= _EPS:
                break
            sell_qty = min(qty, remaining / px)
            if sell_qty <= _EPS:
                continue
            try:
                sell(sym, sell_qty)          # place the real market sell
            except Exception as e:
                print(f"[ledger] withdraw sell {sym} failed: {e}", flush=True)
                continue
            # holdings drop; proceeds are withdrawn (NOT added to slice cash)
            self.holdings[sym] = self.holdings.get(sym, 0.0) - sell_qty
            proceeds = sell_qty * px
            freed += proceeds
            remaining -= proceeds
        self._prune()
        self.save()
        return freed

    def _prune(self) -> None:
        self.holdings = {
            s: q for s, q in self.holdings.items() if abs(q) > _EPS
        }


def replay_fills_basis(fills_path: str | Path) -> dict[str, dict]:
    """Replay a `.fills.jsonl` log to reconstruct the average ENTRY price AND net
    quantity of each symbol's CURRENT open position. Same weighted-average rules
    the live ledger uses (adds average in, partial exits keep the average,
    flat/flip resets). Returns {SYM: {"avg": entry, "qty": net}} for symbols
    still open at the end. Callers verify `qty` against the live holding before
    trusting `avg` (the log may predate a position's open → bogus basis)."""
    p = Path(fills_path)
    if not p.exists():
        return {}
    pos: dict[str, float] = {}      # signed qty
    avg: dict[str, float] = {}      # avg entry
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            sym = normalize_symbol(r.get("symbol", ""))
            side = str(r.get("side", "")).upper()
            qty = abs(float(r.get("qty", 0) or 0))
            price = abs(float(r.get("price", 0) or 0))
        except Exception:
            continue
        if not sym or qty <= _EPS or price <= 0:
            continue
        delta = qty if side in ("BUY", "COVER") else -qty
        old = pos.get(sym, 0.0)
        new = old + delta
        if abs(new) <= _EPS:
            pos[sym] = 0.0
            avg.pop(sym, None)
            continue
        if abs(old) <= _EPS or (old > 0) != (new > 0):
            avg[sym] = price                      # opening / flip
        elif abs(new) > abs(old) + _EPS:          # adding
            a0 = avg.get(sym, 0.0)
            avg[sym] = ((abs(old) * a0 + abs(delta) * price) / abs(new)
                        if a0 > 0 else price)
        # partial exit → keep avg
        pos[sym] = new
    return {s: {"avg": a, "qty": pos.get(s, 0.0)}
            for s, a in avg.items() if abs(pos.get(s, 0.0)) > _EPS and a > 0}


# ── env-driven convenience (used by the bot subprocess) ─────────────

def get_ledger(*,
               side: Optional[str] = None,
               broker: Optional[str] = None,
               mode: Optional[str] = None,
               data_dir: Optional[str] = None) -> Optional[Ledger]:
    """Return this bot's Ledger, or None when ledger isolation doesn't
    apply (non-IBKR broker, or no ledger file created yet).

    Defaults are read from the env vars APEX set on the bot subprocess:
    APEX_BOT_SIDE, APEX_BROKER, APEX_ALPACA_MODE, APEX_DATA_DIR.  Returning
    None makes the IBKR shim fall back to whole-account behavior, so a bot
    launched before a ledger exists still trades (against the full account)
    rather than refusing — the Tools lifecycle creates the ledger on add."""
    broker = (broker or os.environ.get("APEX_BROKER") or "alpaca").lower()
    if broker != "ibkr":
        return None
    side = side or os.environ.get("APEX_BOT_SIDE", "")
    if not side:
        return None
    mode = (mode or os.environ.get("APEX_ALPACA_MODE") or "paper").lower()
    data_dir = data_dir or os.environ.get("APEX_DATA_DIR") or "."
    return Ledger.load(ledger_path(side, broker, mode, data_dir))

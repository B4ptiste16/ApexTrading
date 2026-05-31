"""
Unified broker client for the APEX bot framework.

Returns either Alpaca's TradingClient (byte-for-byte unchanged so existing
Alpaca bots can't regress) OR an IBKR shim that mimics the same surface
(`get_account`, `get_all_positions`, `close_position`, `submit_order(...)`)
so the framework + custom bots don't care which broker they're on.

Broker selection comes from the APEX_BROKER env var that the desktop sets
when launching the bot subprocess.  IBKR connection details (host / port /
client id) come from APEX_IBKR_HOST / APEX_IBKR_PORT / APEX_IBKR_CLIENT_ID.

The IBKR path needs a running IB Gateway / TWS and ib_async installed; it
cannot be unit-tested here without a live gateway.  Built-in bots
(longbot_v2 / shortbot_v2 / daybot) still call alpaca-py directly — they
will be refactored to use this abstraction in a follow-up release.  For now
the desktop pre-flight blocks built-ins in IBKR mode with a clear message.
"""

from __future__ import annotations

import math
import os
import time
from types import SimpleNamespace

from core.ledger import get_ledger, normalize_symbol

_EPS = 1e-9


# ── PUBLIC API ─────────────────────────────────────────────────────────

def get_broker_client(asset_type: str = "stocks"):
    """Return (client, key, sec) for the broker currently selected via the
    APEX_BROKER env var.  Falls back to Alpaca for any unknown broker so
    nothing regresses.  key/sec are empty strings for non-Alpaca brokers.

    Raises a clear RuntimeError (never a bare KeyError) when credentials or a
    gateway are missing, so the bot framework can surface a readable message
    and retry rather than dying."""
    broker = (os.environ.get("APEX_BROKER") or "alpaca").lower()
    if broker == "ibkr":
        return _make_ibkr_client(asset_type), "", ""
    key, sec = _resolve_alpaca_keys()
    return _make_alpaca_client(key, sec), key, sec


def disconnect_broker_client(client) -> None:
    """Cleanly disconnect IBKR clients on bot exit; no-op for Alpaca."""
    if isinstance(client, _IBKRShim):
        try:
            client.ib.disconnect()
        except Exception:
            pass


# ── ALPACA (unchanged behavior) ────────────────────────────────────────

def _resolve_alpaca_keys():
    """Find this bot's Alpaca key + secret without ever raising KeyError.

    Looks (in order) at the side-specific slot env vars APEX sets
    (ALPACA_API_KEY_<SIDE>), the generic ALPACA_API_KEY, then re-reads the
    data-dir .env in case it was populated after launch.  Returns (key, sec)
    — either may be '' when truly unset, which the caller turns into a clear
    actionable error."""
    side = (os.environ.get("APEX_BOT_SIDE") or "").upper()
    key = sec = ""
    candidates_key = [f"ALPACA_API_KEY_{side}" if side else "",
                      "ALPACA_API_KEY"]
    candidates_sec = [f"ALPACA_SECRET_KEY_{side}" if side else "",
                      "ALPACA_SECRET_KEY"]
    for k in candidates_key:
        if k and os.environ.get(k):
            key = os.environ[k]
            break
    for k in candidates_sec:
        if k and os.environ.get(k):
            sec = os.environ[k]
            break
    if not key or not sec:
        # Last resort: the user may have added keys to the data-dir .env after
        # the process started — reload it and retry the generic names.
        try:
            from dotenv import load_dotenv
            data_dir = os.environ.get("APEX_DATA_DIR")
            load_dotenv(os.path.join(data_dir, ".env") if data_dir else None,
                        override=False)
        except Exception:
            pass
        key = key or os.environ.get("ALPACA_API_KEY", "")
        sec = sec or os.environ.get("ALPACA_SECRET_KEY", "")
    return key, sec


def _make_alpaca_client(key: str = "", sec: str = ""):
    from alpaca.trading.client import TradingClient
    if not key or not sec:
        key, sec = _resolve_alpaca_keys()
    if not key or not sec:
        raise RuntimeError(
            "Alpaca API keys not found. Open APEX → settings and paste your "
            "key + secret into a slot assigned to this bot (or add "
            "ALPACA_API_KEY / ALPACA_SECRET_KEY to the .env in your data "
            "folder), then start the bot again.")
    mode = (os.environ.get("APEX_ALPACA_MODE") or "paper").lower()
    is_paper = (mode != "live")
    print(f"[broker] Alpaca {'PAPER' if is_paper else 'LIVE'}", flush=True)
    return TradingClient(key, sec, paper=is_paper)


# ── IBKR (ib_async) ────────────────────────────────────────────────────

def _make_ibkr_client(asset_type: str):
    """Open a persistent IB Gateway/TWS connection for this bot's lifetime.
    Retries a few times so a slow gateway start doesn't kill the bot."""
    try:
        from ib_async import IB
    except Exception as e:
        raise RuntimeError(
            f"ib_async not installed: {e}.  IBKR execution requires "
            f"ib_async to be bundled into the build.")
    host = os.environ.get("APEX_IBKR_HOST") or "127.0.0.1"
    port = int(os.environ.get("APEX_IBKR_PORT") or "7497")
    cid  = int(os.environ.get("APEX_IBKR_CLIENT_ID") or "1")

    # Each bot subprocess is its own event loop owner.  Make sure one
    # exists before ib_async tries to use it.
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    ib = IB()
    last_err = None
    for attempt in range(3):
        try:
            print(f"[broker] IBKR connect {host}:{port} cid={cid} "
                  f"(attempt {attempt + 1}/3)", flush=True)
            ib.connect(host, port, clientId=cid, timeout=20, readonly=False)
            break
        except Exception as e:
            last_err = e
            print(f"[broker] IBKR connect failed: {e}", flush=True)
            time.sleep(5)
    if not ib.isConnected():
        raise RuntimeError(
            f"Could not reach IB Gateway/TWS at {host}:{port} after 3 attempts "
            f"({last_err}).  Make sure the gateway is running and API access "
            f"is enabled.")
    print(f"[broker] IBKR connected — managed accounts: {ib.managedAccounts()}",
          flush=True)
    return _IBKRShim(ib, asset_type)


class UnsupportedSymbol(Exception):
    """Raised when IBKR has no tradeable contract for a symbol (e.g. a crypto
    coin IBKR doesn't list). The bot framework treats this as a clean SKIP,
    not an order failure."""
    def __init__(self, symbol: str):
        self.symbol = symbol
        super().__init__(
            f"{symbol} isn't tradeable on IBKR — IBKR lists only a limited set "
            f"of crypto (e.g. BTC/ETH/LTC/BCH). Run this crypto bot on Alpaca "
            f"for full coin coverage.")


class _IBKRShim:
    """alpaca-py TradingClient look-alike backed by ib_async.

    Implements only the methods the APEX framework uses:
      • get_account()                 → SimpleNamespace
      • get_all_positions()           → list[SimpleNamespace]
      • close_position(symbol)        → submits the opposite market order
      • submit_order(MarketOrderRequest) → SimpleNamespace(id=...)

    Limit / bracket orders and order-cancellation are NOT implemented yet
    — those are mostly needed by the built-in bots which still trade via
    alpaca-py until the follow-up release refactors them.
    """

    def __init__(self, ib, asset_type: str):
        self.ib = ib
        self.asset_type = (asset_type or "stocks").lower()
        try:
            accts = ib.managedAccounts()
            self.account = accts[0] if accts else ""
        except Exception:
            self.account = ""
        # V4.6.38 — per-bot sub-portfolio ledger.  When present, this bot
        # sees and trades ONLY its allocated cash + shares (the shared IBKR
        # account is partitioned in software).  None ⇒ whole-account
        # behavior (no ledger created yet, or non-IBKR), so nothing changes.
        self.ledger = get_ledger()
        if self.ledger is not None:
            print(f"[broker] IBKR sub-portfolio ledger active for "
                  f"'{self.ledger.bot_id}' — cash=${self.ledger.cash:.2f}  "
                  f"holdings={self.ledger.holdings}", flush=True)
        # V4.6.46 — symbols IBKR has no contract for (e.g. crypto coins IBKR
        # doesn't list, like ONDO). Cached after the first rejection so we
        # stop re-qualifying them every tick (which spammed Error 200 logs)
        # and skip them cleanly instead of "FAILED" on every cycle.
        self._unsupported: set = set()

    # ── account ────────────────────────────────────────────────────────

    def get_account(self):
        # V4.6.38 — ledger-scoped account: the bot only ever sees its own
        # slice's free cash + the market value of the shares it holds.
        if self.ledger is not None:
            led = self.ledger
            holdings_val = 0.0
            for sym, qty in led.holdings.items():
                if abs(qty) <= _EPS:
                    continue
                holdings_val += qty * self._price(sym)
            eq = led.cash + holdings_val
            return SimpleNamespace(
                portfolio_value=eq,
                equity=eq,
                cash=led.cash,
                buying_power=led.cash,   # a slice can only spend its own cash
                last_equity=eq,
            )

        vals = self.ib.accountValues()

        def pick(tag: str, default: float = 0.0) -> float:
            fallback = None
            for av in vals:
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

        eq = pick("NetLiquidation")
        return SimpleNamespace(
            portfolio_value=eq,
            equity=eq,
            cash=pick("TotalCashValue"),
            buying_power=pick("BuyingPower"),
            last_equity=eq,
        )

    # ── positions ──────────────────────────────────────────────────────

    def get_all_positions(self):
        # V4.6.38 — ledger-scoped: report ONLY the shares in this bot's
        # slice (priced live), never the whole shared IBKR account.
        if self.ledger is not None:
            asset_class = "crypto" if self.asset_type == "crypto" else "us_equity"
            out = []
            for sym, qty in self.ledger.holdings.items():
                if abs(qty) <= _EPS:
                    continue
                px = self._price(sym)
                mv = qty * px
                out.append(SimpleNamespace(
                    symbol=sym,
                    qty=qty,
                    market_value=mv,
                    avg_entry_price=0.0,
                    unrealized_pl=0.0,
                    unrealized_plpc=0.0,
                    current_price=px,
                    side=("short" if qty < 0 else "long"),
                    asset_class=asset_class,
                ))
            return out

        out = []
        for it in self.ib.portfolio():
            qty = float(it.position)
            mv  = float(it.marketValue or 0)
            pl  = float(it.unrealizedPNL or 0)
            stype = (getattr(it.contract, "secType", "STK") or "STK").upper()
            asset_class = "crypto" if stype == "CRYPTO" else "us_equity"
            out.append(SimpleNamespace(
                symbol=it.contract.symbol,
                qty=qty,
                market_value=mv,
                avg_entry_price=float(it.averageCost or 0),
                unrealized_pl=pl,
                unrealized_plpc=(pl / abs(mv)) if mv else 0.0,
                current_price=float(it.marketPrice or 0),
                asset_class=asset_class,
            ))
        return out

    # ── single-symbol position lookup (alpaca-py compat) ──────────────

    def get_open_position(self, symbol: str):
        """Mirrors alpaca-py: returns the position object or raises if the
        symbol has no open position (alpaca-py raises APIError; we raise
        ValueError, which the built-in bots already catch as Exception)."""
        for p in self.get_all_positions():
            if p.symbol.upper() == str(symbol).upper():
                return p
        raise ValueError(f"position does not exist for {symbol}")

    # ── market clock (alpaca-py compat) ────────────────────────────────

    def get_clock(self):
        """Returns an object with .is_open / .timestamp matching alpaca-py's
        Clock surface, derived from US equity hours (9:30–16:00 ET, M–F).
        Holidays aren't honored — the bot's AI tends to HOLD on quiet days
        and IBKR itself rejects orders on a closed exchange.  For crypto
        bots this is irrelevant (asset_type='crypto' → 24/7 path)."""
        from datetime import datetime, timezone, timedelta
        from types import SimpleNamespace
        # Cheap ET offset: -4 for EDT roughly Mar–Nov, -5 for EST otherwise.
        # Good enough for is_open; precise scheduling lives in the AI loop.
        now = datetime.now(timezone.utc)
        et = now - timedelta(hours=5 if now.month in (12, 1, 2) else 4)
        weekday_open = et.weekday() < 5
        minutes = et.hour * 60 + et.minute
        is_open = weekday_open and (9 * 60 + 30) <= minutes < (16 * 60)
        return SimpleNamespace(is_open=is_open, timestamp=now)

    # ── close one symbol ───────────────────────────────────────────────

    def close_position(self, symbol: str):
        sym = normalize_symbol(symbol)
        # V4.6.38 — ledger-scoped: close exactly this slice's holding,
        # never the whole shared account's position in the symbol.
        if self.ledger is not None:
            held = self.ledger.holding(sym)
            if abs(held) <= _EPS:
                return None
            action = "SELL" if held > 0 else "BUY"
            trade = self._market_order(sym, action, abs(held))
            self._settle(trade, sym, action, abs(held), self._price(sym))
            return self._order_result(trade)
        for p in self.get_all_positions():
            if p.symbol != symbol:
                continue
            qty = abs(p.qty)
            if qty == 0:
                return None
            action = "SELL" if p.qty > 0 else "BUY"
            return self._order_result(self._market_order(symbol, action, qty))

    # ── submit (market orders) ─────────────────────────────────────────

    def submit_order(self, order_data=None, req=None):
        """Accepts alpaca-py MarketOrderRequest (qty- or notional-sized).
        Anything else raises so the bug surfaces loudly rather than silently
        mis-trading.  When a ledger is active the order is constrained to —
        and recorded against — this bot's sub-portfolio.

        V4.6.45 — alpaca-py's TradingClient.submit_order is called as
        `submit_order(order_data=req)` by the framework and built-in bots, so
        the shim must accept that exact keyword (it previously used a bare
        `req` positional and crashed with 'unexpected keyword argument
        order_data'). Accepts positional, order_data=, or req= for safety."""
        req = order_data if order_data is not None else req
        if req is None:
            raise ValueError("submit_order: no order request provided")
        side_str = str(getattr(req, "side", "")).split(".")[-1].upper()
        if side_str not in ("BUY", "SELL"):
            raise ValueError(f"Unsupported order side for IBKR shim: {req.side}")
        order_type = type(req).__name__
        if "Market" not in order_type:
            raise NotImplementedError(
                f"IBKR shim only handles MarketOrderRequest right now, "
                f"got {order_type}.")
        # V4.6.42 — bracket / OCO support so the DAY bot (and any bracket
        # strategy) runs on IBKR with native protective legs. Alpaca-py sends
        # MarketOrderRequest(order_class=BRACKET|OCO, take_profit=..., stop_loss=...).
        order_class = str(getattr(req, "order_class", "") or "").split(".")[-1].lower()
        tp_req = getattr(req, "take_profit", None)
        sl_req = getattr(req, "stop_loss", None)
        tp_price = float(getattr(tp_req, "limit_price", 0) or 0) if tp_req else 0.0
        sl_price = float(getattr(sl_req, "stop_price", 0) or 0) if sl_req else 0.0
        sym = normalize_symbol(req.symbol)
        # Bail out cleanly + early if IBKR has no contract for this symbol
        # (raises UnsupportedSymbol, which the framework turns into a SKIP).
        self._require_contract(sym)
        qty = float(getattr(req, "qty", 0) or 0)
        notional = float(getattr(req, "notional", 0) or 0)

        # Resolve a price up-front for notional sizing and/or ledger checks.
        price = 0.0
        if notional > 0 and qty <= 0:
            price = self._price(sym)
            if price <= 0:
                raise RuntimeError(f"could not price {sym} for notional order")
            qty = notional / price
        if qty <= 0:
            raise ValueError(f"submit_order: non-positive qty {qty}")

        if self.ledger is not None:
            if price <= 0:
                price = self._price(sym)
            if price <= 0:
                raise RuntimeError(f"could not price {sym} to size against ledger")
            qty = self._enforce(side_str, sym, qty, price)
            qty = self._round_qty(qty)
            if qty <= _EPS:
                raise RuntimeError(
                    f"sub-portfolio can't {side_str} {sym}: "
                    f"cash=${self.ledger.cash:.2f}  held={self.ledger.holding(sym)}")
        else:
            qty = self._round_qty(qty)
            if qty <= _EPS:
                raise ValueError(f"submit_order: qty rounds to 0 for {sym}")

        # ── bracket entry (market entry + OCA take-profit / stop-loss) ──────
        if order_class == "bracket" and tp_price > 0 and sl_price > 0:
            trade = self._market_bracket(sym, side_str, qty, tp_price, sl_price)
            # The market entry fills now; settle it into the ledger. The TP/SL
            # legs rest on IBKR (they execute even with the user's PC off) and
            # are reconciled on the next position sync.
            self._settle(trade, sym, side_str, qty, price)
            return self._order_result(trade)

        # ── OCO exit re-arm (resting take-profit / stop-loss, no entry) ─────
        if order_class == "oco" and tp_price > 0 and sl_price > 0:
            trade = self._oco_exit(sym, side_str, qty, tp_price, sl_price)
            # Resting protective orders — no immediate fill, so no ledger write
            # here; the eventual TP/SL fill is picked up on position sync.
            return self._order_result(trade)

        trade = self._market_order(sym, side_str, qty)
        self._settle(trade, sym, side_str, qty, price)
        return self._order_result(trade)

    # ── ledger enforcement ──────────────────────────────────────────────

    def _enforce(self, side: str, sym: str, qty: float, price: float) -> float:
        """Cap `qty` so a buy never spends more than the slice's cash and a
        short never exceeds the slice's cash as crude margin.  Reducing
        existing exposure (selling a long, covering a short) is never
        capped — a bot must always be able to exit."""
        led = self.ledger
        held = led.holding(sym)
        if side == "BUY":
            if held < -_EPS:
                # Covering a short is always allowed; only the part that
                # would flip into a new long needs cash.
                cover = min(qty, -held)
                long_extra = qty - cover
                if long_extra > _EPS and not led.can_buy(long_extra * price):
                    long_extra = min(long_extra, led.affordable_qty(price))
                return cover + long_extra
            # Pure long add — must fit the slice's cash.
            if not led.can_buy(qty * price):
                return min(qty, led.affordable_qty(price))
            return qty
        # SELL
        long_part = min(qty, max(held, 0.0))      # closing a long: free
        short_part = qty - long_part               # opening/adding a short
        if short_part > _EPS:
            already_short = max(0.0, -held)
            room = max(0.0, led.affordable_qty(price) - already_short)
            short_part = min(short_part, room)
        return long_part + short_part

    def _settle(self, trade, sym: str, side: str,
                req_qty: float, est_price: float) -> None:
        """Read the fill and write it back to the ledger.  No-op without a
        ledger.  Falls back to the requested qty / estimated price if the
        gateway hasn't reported a fill yet (e.g. market closed)."""
        if self.ledger is None:
            return
        filled, avg = self._fill_info(trade)
        if filled <= _EPS:
            filled = req_qty            # assume it will fill (market order)
        px = avg if avg > 0 else est_price
        if px <= 0:
            px = self._price(sym)
        if side == "BUY":
            self.ledger.record_buy(sym, filled, px)
        else:
            self.ledger.record_sell(sym, filled, px)
        print(f"  [ledger] {side} {sym} {filled:g} @ ${px:.4f}  "
              f"→ cash=${self.ledger.cash:.2f}  "
              f"held={self.ledger.holding(sym):g}", flush=True)

    # ── pricing / contracts / fills ─────────────────────────────────────

    def _contract(self, symbol: str):
        from ib_async import Stock, Crypto
        if self.asset_type == "crypto":
            return Crypto(symbol, "PAXOS", "USD")
        return Stock(symbol, "SMART", "USD")

    def tradeable_symbols(self, symbols):
        """V4.6.48 — given the bot's full universe, return only the symbols
        IBKR can actually trade. Lets a bot READ its whole universe (incl.
        coins IBKR doesn't list, like ONDO) but trade just the supported
        subset. Each symbol is qualified once and cached, so this is cheap
        after the first cycle. On a transient error we KEEP the symbol (don't
        drop it on a hiccup)."""
        if not hasattr(self, "_supported_cache"):
            self._supported_cache = set()
        out = []
        for s in symbols:
            sym = normalize_symbol(s)
            if sym in self._unsupported:
                continue
            if sym in self._supported_cache:
                out.append(s)
                continue
            try:
                self._require_contract(sym)
                self._supported_cache.add(sym)
                out.append(s)
            except UnsupportedSymbol:
                continue
            except Exception:
                out.append(s)   # transient — don't drop the symbol
        return out

    def _require_contract(self, symbol: str):
        """Qualify the contract; raise UnsupportedSymbol (cached) if IBKR has
        no security definition for it, so the bot skips it cleanly instead of
        spamming 'no security definition' / price-failure every tick."""
        sym = normalize_symbol(symbol)
        if sym in self._unsupported:
            raise UnsupportedSymbol(sym)
        c = self._contract(sym)
        try:
            self.ib.qualifyContracts(c)
        except Exception:
            c = None
        if not c or not getattr(c, "conId", 0):
            self._unsupported.add(sym)
            raise UnsupportedSymbol(sym)
        return c

    def _round_qty(self, qty: float) -> float:
        """Equities trade in whole shares; crypto is fractional."""
        if self.asset_type == "crypto":
            return max(0.0, float(qty))
        return float(math.floor(max(0.0, qty)))

    def _price(self, symbol: str) -> float:
        sym = normalize_symbol(symbol)
        # 1) reuse the live portfolio mark if we already hold it
        try:
            for it in self.ib.portfolio():
                if (it.contract.symbol or "").upper() == sym:
                    mp = float(it.marketPrice or 0)
                    if mp > 0:
                        return mp
        except Exception:
            pass
        # 2) request a fresh snapshot
        try:
            c = self._contract(sym)
            self.ib.qualifyContracts(c)
            tks = self.ib.reqTickers(c)
            if tks:
                t = tks[0]
                for cand in (t.marketPrice(), t.last, t.close, t.bid, t.ask):
                    try:
                        v = float(cand)
                        if v == v and v > 0:    # not NaN, positive
                            return v
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            print(f"  [ibkr] price lookup {sym} failed: {e}", flush=True)
        return 0.0

    def _fill_info(self, trade) -> tuple:
        """(filled_qty, avg_fill_price) from an ib_async Trade, best-effort."""
        try:
            st = trade.orderStatus
            return float(st.filled or 0), float(st.avgFillPrice or 0)
        except Exception:
            return 0.0, 0.0

    def _order_result(self, trade):
        try:
            return SimpleNamespace(id=str(trade.order.orderId))
        except Exception:
            return SimpleNamespace(id="")

    # ── internal: market order placement ───────────────────────────────

    def _market_order(self, symbol: str, action: str, qty: float):
        from ib_async import MarketOrder
        contract = self._contract(normalize_symbol(symbol))
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            raise RuntimeError(f"IBKR could not qualify {symbol}: {e}")
        order = MarketOrder(action, qty)
        trade = self.ib.placeOrder(contract, order)
        # Give the gateway a moment to assign an orderId / report a fill.
        self.ib.sleep(1)
        return trade

    # ── internal: bracket / OCO placement (V4.6.42) ─────────────────────

    def _exit_action(self, entry_action: str) -> str:
        return "SELL" if entry_action == "BUY" else "BUY"

    def _round_px(self, px: float) -> float:
        """Equity prices are penny-ticked; keep crypto at full precision."""
        return float(px) if self.asset_type == "crypto" else round(float(px), 2)

    def _market_bracket(self, symbol: str, action: str, qty: float,
                        tp_price: float, sl_price: float):
        """Market entry with attached take-profit (limit) + stop-loss (stop)
        legs in a one-cancels-all group. The protective legs live on IBKR's
        servers, so they execute even when the user's computer is off."""
        from ib_async import MarketOrder, LimitOrder, StopOrder
        contract = self._contract(normalize_symbol(symbol))
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            raise RuntimeError(f"IBKR could not qualify {symbol}: {e}")
        exit_action = self._exit_action(action)
        oca = f"apex_{symbol}_{int(time.time()*1000)}"

        parent = MarketOrder(action, qty)
        parent.orderId  = self.ib.client.getReqId()
        parent.transmit = False

        tp = LimitOrder(exit_action, qty, self._round_px(tp_price))
        tp.orderId  = self.ib.client.getReqId()
        tp.parentId = parent.orderId
        tp.ocaGroup = oca; tp.ocaType = 1
        tp.transmit = False

        sl = StopOrder(exit_action, qty, self._round_px(sl_price))
        sl.orderId  = self.ib.client.getReqId()
        sl.parentId = parent.orderId
        sl.ocaGroup = oca; sl.ocaType = 1
        sl.transmit = True          # transmits the whole chain

        parent_trade = self.ib.placeOrder(contract, parent)
        self.ib.placeOrder(contract, tp)
        self.ib.placeOrder(contract, sl)
        self.ib.sleep(1)
        print(f"  [ibkr] bracket {action} {qty:g} {symbol} "
              f"tp=${tp_price:g} sl=${sl_price:g} oca={oca}", flush=True)
        return parent_trade

    def _oco_exit(self, symbol: str, action: str, qty: float,
                  tp_price: float, sl_price: float):
        """Resting take-profit + stop-loss on an EXISTING position (no entry),
        one-cancels-all. `action` is the exit side (SELL to protect a long)."""
        from ib_async import LimitOrder, StopOrder
        contract = self._contract(normalize_symbol(symbol))
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            raise RuntimeError(f"IBKR could not qualify {symbol}: {e}")
        oca = f"apex_{symbol}_{int(time.time()*1000)}"

        tp = LimitOrder(action, qty, self._round_px(tp_price))
        tp.ocaGroup = oca; tp.ocaType = 1; tp.transmit = False
        sl = StopOrder(action, qty, self._round_px(sl_price))
        sl.ocaGroup = oca; sl.ocaType = 1; sl.transmit = True

        self.ib.placeOrder(contract, tp)
        sl_trade = self.ib.placeOrder(contract, sl)
        self.ib.sleep(1)
        print(f"  [ibkr] OCO exit {action} {qty:g} {symbol} "
              f"tp=${tp_price:g} sl=${sl_price:g} oca={oca}", flush=True)
        return sl_trade

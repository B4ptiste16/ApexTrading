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

import os
import time
from types import SimpleNamespace


# ── PUBLIC API ─────────────────────────────────────────────────────────

def get_broker_client(asset_type: str = "stocks"):
    """Return (client, key, sec) for the broker currently selected via the
    APEX_BROKER env var.  Falls back to Alpaca for any unknown broker so
    nothing regresses.  key/sec are empty strings for non-Alpaca brokers."""
    broker = (os.environ.get("APEX_BROKER") or "alpaca").lower()
    if broker == "ibkr":
        return _make_ibkr_client(asset_type), "", ""
    return _make_alpaca_client(), os.environ.get("ALPACA_API_KEY", ""), \
        os.environ.get("ALPACA_SECRET_KEY", "")


def disconnect_broker_client(client) -> None:
    """Cleanly disconnect IBKR clients on bot exit; no-op for Alpaca."""
    if isinstance(client, _IBKRShim):
        try:
            client.ib.disconnect()
        except Exception:
            pass


# ── ALPACA (unchanged behavior) ────────────────────────────────────────

def _make_alpaca_client():
    from alpaca.trading.client import TradingClient
    key = os.environ["ALPACA_API_KEY"]
    sec = os.environ["ALPACA_SECRET_KEY"]
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

    # ── account ────────────────────────────────────────────────────────

    def get_account(self):
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

    # ── close one symbol ───────────────────────────────────────────────

    def close_position(self, symbol: str):
        for p in self.get_all_positions():
            if p.symbol != symbol:
                continue
            qty = abs(p.qty)
            if qty == 0:
                return None
            action = "SELL" if p.qty > 0 else "BUY"
            return self._market_order(symbol, action, qty)
        return None

    # ── submit (market orders) ─────────────────────────────────────────

    def submit_order(self, req):
        """Accepts alpaca-py MarketOrderRequest.  Anything else raises so
        the bug surfaces loudly rather than silently mis-trading."""
        side_str = str(getattr(req, "side", "")).split(".")[-1].upper()
        if side_str not in ("BUY", "SELL"):
            raise ValueError(f"Unsupported order side for IBKR shim: {req.side}")
        sym = str(req.symbol)
        # Alpaca crypto pairs come through as BTC/USD — translate to plain BTC.
        if "/" in sym:
            sym = sym.split("/", 1)[0]
        qty = float(getattr(req, "qty", 0) or 0)
        if qty <= 0:
            raise ValueError(f"submit_order: non-positive qty {qty}")
        # Only market orders are wired today — limit / bracket / stop come
        # with the built-in refactor.
        order_type = type(req).__name__
        if "Market" not in order_type:
            raise NotImplementedError(
                f"IBKR shim only handles MarketOrderRequest right now, "
                f"got {order_type}.  Built-in bot refactor will add the rest.")
        return self._market_order(sym, side_str, qty)

    # ── internal: market order placement ───────────────────────────────

    def _market_order(self, symbol: str, action: str, qty: float):
        from ib_async import Stock, Crypto, MarketOrder
        if self.asset_type == "crypto":
            contract = Crypto(symbol, "PAXOS", "USD")
        else:
            contract = Stock(symbol, "SMART", "USD")
        try:
            self.ib.qualifyContracts(contract)
        except Exception as e:
            raise RuntimeError(f"IBKR could not qualify {symbol}: {e}")
        order = MarketOrder(action, qty)
        trade = self.ib.placeOrder(contract, order)
        # Give the gateway a moment to assign an orderId so the caller gets
        # a meaningful return value for logging.
        self.ib.sleep(1)
        return SimpleNamespace(id=str(trade.order.orderId))

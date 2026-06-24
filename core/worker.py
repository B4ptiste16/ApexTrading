"""
APEX Background Worker
Runs all data fetching off the main thread so the UI never freezes.
"""

from PyQt6.QtCore import QThread, pyqtSignal


class DataWorker(QThread):
    """
    Generic worker — runs any callable in a background thread.
    Emits result_ready(data) when done, error(msg) on failure.
    """
    result_ready = pyqtSignal(object)
    error        = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn     = fn
        self._args   = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.result_ready.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class RefreshWorker(QThread):
    """
    Fetches all data needed for one bot tab in one background thread.

    V4.6.43 — two-phase + per-field isolation:
      * Phase 1 (FAST, local files): trade log, snapshots, orders, costs,
        day_state. Emitted immediately via `partial` so the tab paints its
        history right away — even for a bot that ISN'T running and even
        when the broker is slow/unreachable.
      * Phase 2 (SLOW, live broker): account, positions, equity history,
        per-stock meta. Merged in and emitted via `done`.
    Every single fetch is isolated so one failure (slow IBKR gateway, a
    crypto symbol yfinance can't price, …) can never blank the whole tab.
    """
    done    = pyqtSignal(dict)
    partial = pyqtSignal(dict)
    error   = pyqtSignal(str)

    def __init__(self, side: str, period: str = "1D"):
        super().__init__()
        self.side   = side
        self.period = period

    @staticmethod
    def _safe(fn, default, label):
        try:
            return fn()
        except Exception as e:
            print(f"[refresh] {label} failed: {e}", flush=True)
            return default

    def run(self):
        import core.data as D
        import pandas as pd
        S = self._safe

        # ── Phase 1: fast, local, never needs the broker ──────────────────
        orders = S(lambda: D.get_orders(self.side), pd.DataFrame(), "orders")
        data = {
            "orders":       orders,
            "trade_events": S(lambda: D.compute_trade_events(orders, self.side),
                              [], "trade_events"),
            "snapshots":    S(lambda: D.load_snapshots(self.side),
                              pd.DataFrame(), "snapshots"),
            "log":          S(lambda: D.load_bot_log(self.side), "", "log"),
            "costs":        S(lambda: D.estimate_api_costs(self.side), {}, "costs"),
            "day_state":    S(lambda: D.load_day_state(), {}, "day_state")
                            if self.side == "DAY" else {},
            # placeholders so _apply_* never KeyErrors before phase 2 lands
            "account": {}, "positions": [], "position_meta": {},
            "history": pd.DataFrame(),
            # V4.6.132 — flag the fast phase so the tab does NOT overwrite the
            # live widgets (positions, gauge, account, P/L) with these empty
            # placeholders. Without this, re-entering a tab flashed the gauge to
            # 0/fewer positions until phase 2 landed.
            "_partial": True,
        }
        try:
            self.partial.emit(dict(data))
        except Exception:
            pass
        data["_partial"] = False

        # ── Phase 2: live broker data (may be slow / unreachable) ─────────
        positions = S(lambda: D.get_positions(self.side), [], "positions")
        if not positions:
            meta = {}
        elif self.side == "DAY":
            sm, tm = S(lambda: D.get_day_atr_mults(), (2.5, 5.0), "atr_mults")
            meta = S(lambda: D.position_meta(positions, "DAY",
                                             stop_mult=sm, tp_mult=tm),
                     {}, "position_meta")
        else:
            meta = S(lambda: D.position_meta(positions, self.side),
                     {}, "position_meta")
        acct = S(lambda: D.get_account(self.side), {}, "account")
        # V4.6.60 — record an equity snapshot from the detail page too (throttled
        # to 1/5min in core.data). Previously only the Overview tab appended
        # snapshots, so an IBKR bot's equity / lifetime curves stayed empty
        # unless the user happened to visit Overview. This builds the curve
        # from whichever tab is open.
        try:
            if acct and acct.get("connected"):
                D.append_bot_snapshot(self.side,
                                      equity=acct.get("equity", 0),
                                      portfolio_value=acct.get("portfolio_value", 0),
                                      positions_count=len(positions))
        except Exception:
            pass
        data.update({
            "account":       acct,
            "positions":     positions,
            "position_meta": meta,
            "history":       S(lambda: D.get_history(self.side, self.period),
                              pd.DataFrame(), "history"),
        })
        self.done.emit(data)


class OverviewWorker(QThread):
    """Fetches data for all three bots for the overview tab."""
    done  = pyqtSignal(dict)
    error = pyqtSignal(str)

    def run(self):
        try:
            import core.data as D
            result = {}
            for side in ["LONG", "SHORT", "DAY"]:
                result[side] = {
                    "account":   D.get_account(side),
                    "positions": D.get_positions(side),
                    "snapshots": D.load_snapshots(side),
                }
            result["costs"] = D.estimate_total_costs()
            self.done.emit(result)
        except Exception as e:
            self.error.emit(str(e))

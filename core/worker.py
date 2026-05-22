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
    Emits a single dict with everything when done.
    """
    done  = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, side: str, period: str = "1D"):
        super().__init__()
        self.side   = side
        self.period = period

    def run(self):
        try:
            import core.data as D
            positions = D.get_positions(self.side)
            # Per-stock ATR/TP for all sides:
            #   LONG/SHORT — feeds the position-gauge chart.
            #   DAY        — feeds the bracket-gauge fallback for
            #                positions present on Alpaca but missing
            #                from daybot_state.json's open_brackets.
            if not positions:
                meta = {}
            elif self.side == "DAY":
                sm, tm = D.get_day_atr_mults()
                meta = D.position_meta(positions, "DAY",
                                        stop_mult=sm, tp_mult=tm)
            else:
                meta = D.position_meta(positions, self.side)
            orders = D.get_orders(self.side)
            result = {
                "account":       D.get_account(self.side),
                "positions":     positions,
                "position_meta": meta,
                "history":       D.get_history(self.side, self.period),
                "orders":        orders,
                "trade_events":  D.compute_trade_events(orders, self.side),
                "snapshots":     D.load_snapshots(self.side),
                "log":           D.load_bot_log(self.side),
                "costs":         D.estimate_api_costs(self.side),
                "day_state":     D.load_day_state() if self.side == "DAY" else {},
            }
            self.done.emit(result)
        except Exception as e:
            self.error.emit(str(e))


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

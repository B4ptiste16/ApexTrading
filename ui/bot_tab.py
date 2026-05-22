"""
APEX Bot Tab — threaded refresh so UI never freezes.
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QComboBox, QSizePolicy, QDoubleSpinBox, QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer

from ui.styles  import COLORS, BOT_COLOR
from ui.widgets import (
    ChartView, MetricCard, SectionHeader,
    BotProcessWidget, ScrollContent, DataTable, ClosedTradesFeed,
)
from core.worker import RefreshWorker
import core.data   as D
import core.charts as CH

C = COLORS


# Tooltip text for each risk-metric card. Hovering on the ⓘ icon
# explains how the number is computed + the time-frame used.
_METRIC_TOOLTIPS = {
    "TOTAL RETURN": "Cumulative % change from the first portfolio snapshot to the latest.\n"
                    "Time frame: full snapshot history (since bot first ran).",
    "SHARPE":       "Annualised risk-adjusted return: mean(daily return) / std(daily return) × √252.\n"
                    "Above 1 is good, above 2 excellent.\n"
                    "Time frame: full daily-snapshot history.",
    "MAX DD":       "Largest peak-to-trough decline in portfolio value, as a percentage.\n"
                    "More negative = worse drawdown.\n"
                    "Time frame: full snapshot history.",
    "WIN RATE":     "Percentage of trading days where the portfolio finished higher than the day before.\n"
                    "Time frame: full daily-snapshot history.",
    "VOLATILITY":   "Annualised standard deviation of daily returns × 100.\n"
                    "Higher = more variable performance.\n"
                    "Time frame: full daily-snapshot history.",
    "AVG DAILY":    "Total return divided by the number of calendar days covered.\n"
                    "Time frame: first snapshot → latest snapshot.",
    "PORTFOLIO":    "Equity reported by Alpaca (cash + market value of all open positions).",
    "DAY P/L":      "Equity today minus equity at yesterday's close, also shown as %.\n"
                    "Source: Alpaca account today_equity vs last_equity.",
    "PERIOD P/L":   "Equity change over the period selected by the dropdown above.\n"
                    "Computed from the equity-history time series.",
    "CASH":         "Buying power available on the Alpaca paper account.",
    "INVESTED":     "Portfolio value minus cash — % of equity actually deployed.",
    "POSITIONS":    "Number of open positions currently held.",
    "W/L":          "Wins / Losses across all closed bracket trades (DAY bot).\n"
                    "Tracked in daybot_state.json.",
    "WIN RATE_DAY": "Win rate across all closed bracket trades (DAY bot).",
    "BRACKET P/L":  "Cumulative realised profit / loss across closed bracket trades.",
    "BRACKETS":     "Number of bracket orders currently active.",
}


class BotTab(QWidget):

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side    = side
        self.color   = BOT_COLOR[side]
        self.script  = D.BOT_SCRIPTS[side]
        self._worker = None          # keep reference so GC doesn't kill it
        self._cached = {}            # last fetched data

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build_ui()

    # ── BUILD ────────────────────────────────────────────────

    def _build_ui(self):
        s = self.scroll

        # 1. ACCOUNT CARDS
        s.add(SectionHeader("ACCOUNT SUMMARY", self.color))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.card_portfolio  = MetricCard("PORTFOLIO",  "—", self.color,
                                          tooltip=_METRIC_TOOLTIPS["PORTFOLIO"])
        self.card_day_pl     = MetricCard("DAY P/L",    "—",
                                          tooltip=_METRIC_TOOLTIPS["DAY P/L"])
        self.card_period_pl  = MetricCard("PERIOD P/L", "—",
                                          tooltip=_METRIC_TOOLTIPS["PERIOD P/L"])
        self.card_cash       = MetricCard("CASH",       "—",
                                          tooltip=_METRIC_TOOLTIPS["CASH"])
        self.card_invested   = MetricCard("INVESTED",   "—",
                                          tooltip=_METRIC_TOOLTIPS["INVESTED"])
        self.card_positions  = MetricCard("POSITIONS",  "—",
                                          tooltip=_METRIC_TOOLTIPS["POSITIONS"])
        for c in [self.card_portfolio, self.card_day_pl, self.card_period_pl,
                  self.card_cash, self.card_invested, self.card_positions]:
            row.addWidget(c)
        if self.side == "DAY":
            self.card_wl       = MetricCard("W/L",         "—",
                                            tooltip=_METRIC_TOOLTIPS["W/L"])
            self.card_wr       = MetricCard("WIN RATE",    "—",
                                            tooltip=_METRIC_TOOLTIPS["WIN RATE_DAY"])
            self.card_bpl      = MetricCard("BRACKET P/L", "—",
                                            tooltip=_METRIC_TOOLTIPS["BRACKET P/L"])
            self.card_brackets = MetricCard("BRACKETS",    "—",
                                            tooltip=_METRIC_TOOLTIPS["BRACKETS"])
            for c in [self.card_wl, self.card_wr, self.card_bpl, self.card_brackets]:
                row.addWidget(c)
        row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        s.add(rw)

        # 2. RECENT CLOSED TRADES
        s.add(SectionHeader("RECENT CLOSED TRADES", self.color))
        self.closed_trades_feed = ClosedTradesFeed()
        s.add(self.closed_trades_feed)

        # 3. SIGNAL
        s.add(SectionHeader("LAST AI SIGNAL", self.color))
        s.add(self._build_signal_panel())

        # 4. BOT CONTROLS
        s.add(SectionHeader("BOT CONTROLS", self.color))
        self.bot_ctrl = BotProcessWidget(self.side, self.script)
        s.add(self.bot_ctrl)

        # Adjustable AI confidence threshold (live, no restart)
        conf_row = QHBoxLayout()
        conf_lbl = QLabel("Min AI confidence to trade:")
        conf_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        self.conf_spin = QDoubleSpinBox()
        self.conf_spin.setRange(0.30, 0.95)
        self.conf_spin.setSingleStep(0.01)
        self.conf_spin.setDecimals(2)
        self.conf_spin.setFixedWidth(80)
        self.conf_spin.setValue(D.get_bot_min_conf(self.side))
        self.conf_spin.valueChanged.connect(self._on_conf_changed)
        self.conf_saved = QLabel("")
        self.conf_saved.setStyleSheet(f"color:{C['green']};font-size:10px;")
        # Recommended values match each bot's built-in MIN_CONFIDENCE constant
        _rec_conf = D.BOT_DEFAULT_CONF.get(self.side, 0.65)
        conf_hint = QLabel(
            f"lower = trades more often · applied on the bot's next "
            f"cycle · Recommended: {_rec_conf:.2f}")
        conf_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        conf_row.addWidget(conf_lbl)
        conf_row.addWidget(self.conf_spin)
        conf_row.addWidget(self.conf_saved)
        conf_row.addWidget(conf_hint)
        conf_row.addStretch()
        crw = QWidget(); crw.setLayout(conf_row)
        s.add(crw)

        # Minimum positions floor (LONG only): deploy at least N names
        # even when the AI is cautious. 0 = fully cautious.
        if self.side == "LONG":
            mp_row = QHBoxLayout()
            mp_lbl = QLabel("Min positions to hold:")
            mp_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
            self.minpos_spin = QSpinBox()
            self.minpos_spin.setRange(0, 20)
            self.minpos_spin.setFixedWidth(70)
            self.minpos_spin.setValue(D.get_bot_min_positions("LONG"))
            self.minpos_spin.valueChanged.connect(self._on_minpos_changed)
            self.minpos_saved = QLabel("")
            self.minpos_saved.setStyleSheet(f"color:{C['green']};font-size:10px;")
            mp_hint = QLabel(
                "0 = only buy when AI is confident · 5+ = always stay invested "
                "in the top-ranked names · Recommended: 5")
            mp_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            mp_row.addWidget(mp_lbl)
            mp_row.addWidget(self.minpos_spin)
            mp_row.addWidget(self.minpos_saved)
            mp_row.addWidget(mp_hint)
            mp_row.addStretch()
            mpw = QWidget(); mpw.setLayout(mp_row)
            s.add(mpw)

            # Min local score before the paid Claude call (cost control)
            ms_row = QHBoxLayout()
            ms_lbl = QLabel("Min score to call AI:")
            ms_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
            self.minscore_spin = QDoubleSpinBox()
            self.minscore_spin.setRange(0.0, 200.0)
            self.minscore_spin.setSingleStep(5.0)
            self.minscore_spin.setDecimals(1)
            self.minscore_spin.setFixedWidth(80)
            self.minscore_spin.setValue(D.get_bot_min_score("LONG"))
            self.minscore_spin.valueChanged.connect(self._on_minscore_changed)
            self.minscore_saved = QLabel("")
            self.minscore_saved.setStyleSheet(
                f"color:{C['green']};font-size:10px;")
            ms_hint = QLabel(
                "higher = call Claude (costs $) less often · 0 = always "
                "call · Recommended: 30")
            ms_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            ms_row.addWidget(ms_lbl)
            ms_row.addWidget(self.minscore_spin)
            ms_row.addWidget(self.minscore_saved)
            ms_row.addWidget(ms_hint)
            ms_row.addStretch()
            msw = QWidget(); msw.setLayout(ms_row)
            s.add(msw)

        # Max concurrent bracket positions (DAY only). 0 = unlimited.
        if self.side == "DAY":
            mb_row = QHBoxLayout()
            mb_lbl = QLabel("Max concurrent positions:")
            mb_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
            self.maxbr_spin = QSpinBox()
            self.maxbr_spin.setRange(0, 50)
            self.maxbr_spin.setFixedWidth(90)
            self.maxbr_spin.setSpecialValueText("unlimited")  # shown at 0
            self.maxbr_spin.setValue(D.get_bot_max_brackets("DAY"))
            self.maxbr_spin.valueChanged.connect(self._on_maxbr_changed)
            self.maxbr_saved = QLabel("")
            self.maxbr_saved.setStyleSheet(f"color:{C['green']};font-size:10px;")
            mb_hint = QLabel(
                "0 = unlimited · each position has its own stop-loss & "
                "take-profit · capped by buying power · Recommended: 0")
            mb_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            mb_row.addWidget(mb_lbl)
            mb_row.addWidget(self.maxbr_spin)
            mb_row.addWidget(self.maxbr_saved)
            mb_row.addWidget(mb_hint)
            mb_row.addStretch()
            mbw = QWidget(); mbw.setLayout(mb_row)
            s.add(mbw)

            # Bracket size: stop / take-profit as ATR multiples.
            sm0, tm0 = D.get_day_atr_mults()
            atr_row = QHBoxLayout()
            sl_lbl = QLabel("Stop = ATR ×")
            sl_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
            self.slmult_spin = QDoubleSpinBox()
            self.slmult_spin.setRange(0.10, 10.0)
            self.slmult_spin.setSingleStep(0.05)
            self.slmult_spin.setDecimals(2)
            self.slmult_spin.setFixedWidth(70)
            self.slmult_spin.setValue(sm0)
            tp_lbl = QLabel("   Take-profit = ATR ×")
            tp_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
            self.tpmult_spin = QDoubleSpinBox()
            self.tpmult_spin.setRange(0.10, 20.0)
            self.tpmult_spin.setSingleStep(0.05)
            self.tpmult_spin.setDecimals(2)
            self.tpmult_spin.setFixedWidth(70)
            self.tpmult_spin.setValue(tm0)
            self.slmult_spin.valueChanged.connect(self._on_atrmult_changed)
            self.tpmult_spin.valueChanged.connect(self._on_atrmult_changed)
            self.atrmult_saved = QLabel("")
            self.atrmult_saved.setStyleSheet(f"color:{C['green']};font-size:10px;")
            atr_hint = QLabel(
                "smaller = tighter, faster intraday trades · % ≈ ATR× × the "
                "stock's daily ATR% · Recommended: stop ×0.50 / TP ×1.00")
            atr_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            atr_row.addWidget(sl_lbl)
            atr_row.addWidget(self.slmult_spin)
            atr_row.addWidget(tp_lbl)
            atr_row.addWidget(self.tpmult_spin)
            atr_row.addWidget(self.atrmult_saved)
            atr_row.addWidget(atr_hint)
            atr_row.addStretch()
            arw = QWidget(); arw.setLayout(atr_row)
            s.add(arw)

        # 5. POSITION GAUGE
        title = ("OPEN BRACKETS — LIVE" if self.side=="DAY"
                 else "POSITION GAUGE — vs ATR LEVELS")
        s.add(SectionHeader(title, self.color))
        self.gauge_chart = ChartView(height=260)
        s.add(self.gauge_chart)

        # 6. EQUITY CURVES
        s.add(SectionHeader("EQUITY CURVES", self.color,
                            controls=self._period_combo()))
        cr = QHBoxLayout()
        self.equity_chart   = ChartView(height=260)
        self.lifetime_chart = ChartView(height=260)
        cr.addWidget(self.equity_chart)
        cr.addWidget(self.lifetime_chart)
        cw = QWidget(); cw.setLayout(cr)
        s.add(cw)

        # 7. TRADE HISTORY
        s.add(SectionHeader("TRADE HISTORY", self.color))
        self.timeline_chart = ChartView(height=260)
        s.add(self.timeline_chart)
        s.add(QLabel("Trade Summary",
                      styleSheet=f"color:{C['muted']};font-size:10px;margin-top:6px;"))
        self.trade_table = DataTable()
        self.trade_table.setFixedHeight(200)
        s.add(self.trade_table)

        # 8. P/L & ALLOCATION
        s.add(SectionHeader("P/L & ALLOCATION", self.color))
        pr = QHBoxLayout()
        self.pl_chart  = ChartView(height=220)
        self.pie_chart = ChartView(height=220)
        pr.addWidget(self.pl_chart)
        pr.addWidget(self.pie_chart)
        pw = QWidget(); pw.setLayout(pr)
        s.add(pw)

        # 9. RISK METRICS & STATS
        s.add(SectionHeader("RISK METRICS & STATS", self.color))
        rr = QHBoxLayout()
        rr.setSpacing(14)
        self.dd_chart = ChartView(height=180)
        rr.addWidget(self.dd_chart)
        self.risk_cards_frame = QFrame()
        rcl = QGridLayout(self.risk_cards_frame)
        rcl.setSpacing(8)
        self.risk_cards = {}
        for i, m in enumerate(["TOTAL RETURN","SHARPE","MAX DD",
                                "WIN RATE","VOLATILITY","AVG DAILY"]):
            card = MetricCard(m, "—", tooltip=_METRIC_TOOLTIPS.get(m, ""))
            rcl.addWidget(card, i//2, i%2)
            self.risk_cards[m] = card
        rr.addWidget(self.risk_cards_frame)
        rrw = QWidget(); rrw.setLayout(rr)
        s.add(rrw)

        # 10. API COST
        s.add(SectionHeader("API COST", self.color))
        cr2 = QHBoxLayout()
        self.cost_total   = MetricCard("TOTAL SPENT",  "—", C["yellow"])
        self.cost_per_day = MetricCard("PER DAY",      "—", C["muted"])
        self.cost_calls   = MetricCard("CLAUDE CALLS", "—", C["muted"])
        for c in [self.cost_total, self.cost_per_day, self.cost_calls]:
            cr2.addWidget(c)
        cr2.addStretch()
        cw2 = QWidget(); cw2.setLayout(cr2)
        s.add(cw2)

        # 11. POSITION MANAGEMENT
        s.add(SectionHeader("POSITION MANAGEMENT", self.color))
        self.pos_table = DataTable()
        self.pos_table.setFixedHeight(180)
        s.add(self.pos_table)
        mr = QHBoxLayout()
        self.pos_combo = QComboBox()
        self.pos_combo.setPlaceholderText("Select ticker...")
        self.liq_btn   = QPushButton("⚠  LIQUIDATE")
        self.liq_btn.setObjectName("dangerBtn")
        self.liq_btn.clicked.connect(self._liquidate)
        self.liq_msg   = QLabel("")
        self.liq_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        mr.addWidget(self.pos_combo)
        mr.addWidget(self.liq_btn)
        mr.addWidget(self.liq_msg)
        mr.addStretch()
        mw = QWidget(); mw.setLayout(mr)
        s.add(mw)
        s.add_stretch()

    def _build_signal_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;border-top:2px solid {self.color};")
        layout = QGridLayout(frame)
        layout.setContentsMargins(14,10,14,10)
        layout.setSpacing(14)

        def pair(label, size=20, color=None):
            box = QVBoxLayout()
            lb  = QLabel(label)
            lb.setStyleSheet(f"font-size:8px;color:{C['muted']};letter-spacing:3px;")
            vl  = QLabel("—")
            vl.setStyleSheet(
                f"font-family:'Syne',sans-serif;font-size:{size}px;"
                f"font-weight:800;color:{color or C['text']};")
            vl.setWordWrap(True)
            box.addWidget(lb); box.addWidget(vl)
            w = QWidget(); w.setLayout(box)
            return w, vl

        self.sig_decision, self.sig_dec_val  = pair("DECISION",   22, self.color)
        self.sig_conf,     self.sig_conf_val = pair("CONFIDENCE", 22, self.color)
        self.sig_analysis, self.sig_ana_val  = pair("ANALYSIS",   11)
        self.sig_action,   self.sig_act_val  = pair("ACTION",     11, C["muted"])
        self.sig_time_lbl = QLabel("—")
        self.sig_time_lbl.setStyleSheet(f"font-size:9px;color:{C['muted']};")

        layout.addWidget(self.sig_decision, 0, 0)
        layout.addWidget(self.sig_conf,     0, 1)
        layout.addWidget(self.sig_analysis, 0, 2)
        layout.addWidget(self.sig_action,   0, 3)
        layout.addWidget(self.sig_time_lbl, 1, 0, 1, 4)
        return frame

    def _period_combo(self) -> QComboBox:
        self.period_combo = QComboBox()
        self.period_combo.addItems(["1D","1W","1M","3M","6M","1Y"])
        self.period_combo.setFixedWidth(80)
        self.period_combo.currentTextChanged.connect(
            lambda _: self._apply_equity(self._cached))
        return self.period_combo

    def _on_conf_changed(self, val: float):
        D.set_bot_min_conf(self.side, val)
        self.conf_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.conf_saved.setText(""))

    def _on_minscore_changed(self, val: float):
        D.set_bot_min_score("LONG", val)
        self.minscore_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.minscore_saved.setText(""))

    def _on_minpos_changed(self, val: int):
        D.set_bot_min_positions("LONG", val)
        self.minpos_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.minpos_saved.setText(""))

    def _on_maxbr_changed(self, val: int):
        D.set_bot_max_brackets("DAY", val)
        self.maxbr_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.maxbr_saved.setText(""))

    def _on_atrmult_changed(self, _val: float):
        D.set_day_atr_mults(self.slmult_spin.value(),
                            self.tpmult_spin.value())
        self.atrmult_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.atrmult_saved.setText(""))

    # ── REFRESH (non-blocking) ───────────────────────────────

    def refresh(self):
        """Start background fetch — returns immediately, UI stays responsive."""
        if self._worker and self._worker.isRunning():
            return   # already fetching
        period = getattr(self, "period_combo",
                         type("",(),{"currentText":lambda s:"1D"})()).currentText()
        self._worker = RefreshWorker(self.side, period)
        self._worker.done.connect(self._on_data)
        self._worker.error.connect(
            lambda e: print(f"[{self.side}] refresh error: {e}"))
        self._worker.start()

    def _on_data(self, data: dict):
        """Called on main thread when the background fetch completes.

        V7.1.11 fixes:
          1. Wrap the whole sweep in setUpdatesEnabled(False/True) so
             Qt only repaints once — kills the wobble that the v1.1.1
             fix didn't catch (that fix wrapped the kickoff only;
             this is where the actual card mutations happen).
          2. Each _apply_* is now isolated in its own try/except so a
             single failure (e.g., empty snapshots blowing up
             _apply_risk) can't strand everything after it (API COST
             row + POSITION MANAGEMENT table used to never load).
        """
        self._cached = data
        self.setUpdatesEnabled(False)
        try:
            for name, fn in (
                ("account",       self._apply_account),
                ("closed_trades", self._apply_closed_trades),
                ("signal",        self._apply_signal),
                ("gauge",         self._apply_gauge),
                ("equity",        self._apply_equity),
                ("trades",        self._apply_trades),
                ("pl",            self._apply_pl),
                ("risk",          self._apply_risk),
                ("costs",         self._apply_costs),
                ("positions",     self._apply_positions),
            ):
                try:
                    fn(data)
                except Exception as e:
                    print(f"[{self.side}] _apply_{name} failed: {e}")
        finally:
            self.setUpdatesEnabled(True)

    # ── APPLY ────────────────────────────────────────────────

    def _apply_account(self, data):
        import pandas as pd
        a    = data.get("account", {})
        pos  = data.get("positions", [])
        hist = data.get("history", pd.DataFrame())
        pv   = a.get("portfolio_value", 0)
        eq   = a.get("equity", 0)
        ca   = a.get("cash", 0)
        le   = a.get("last_equity", eq)
        dp   = eq - le
        dpc  = dp/le*100 if le else 0
        inv  = pv - ca
        inv_pct = inv/pv*100 if pv else 0
        arrow = "▲" if dp>=0 else "▼"
        dc    = C["green"] if dp>=0 else C["red"]

        # Period P/L from equity history (replaces raw unrealized)
        period = getattr(self, "period_combo",
                         type("",(),{"currentText": lambda s: "1D"})()).currentText()
        if hist is not None and not hist.empty and len(hist) >= 2:
            p_pl  = hist["equity"].iloc[-1] - hist["equity"].iloc[0]
            p_pct = p_pl / hist["equity"].iloc[0] * 100 if hist["equity"].iloc[0] else 0
            pc    = C["green"] if p_pl >= 0 else C["red"]
            pa    = "▲" if p_pl >= 0 else "▼"
            period_txt = f"{pa} ${abs(p_pl):,.2f}"
            period_sub = f"{p_pct:+.2f}%  ({period})"
        else:
            period_txt = "—"
            period_sub = period
            pc = C["muted"]

        self.card_portfolio.update_value(f"${pv:,.2f}", self.color)
        self.card_day_pl.update_value(
            f"{arrow} ${abs(dp):,.2f}", dc, sub=f"{dpc:+.2f}%")
        self.card_period_pl.update_value(period_txt, pc, sub=period_sub)
        self.card_cash.update_value(
            f"${ca:,.2f}", sub=f"{ca/pv*100:.1f}%" if pv else "")
        self.card_invested.update_value(
            f"${inv:,.2f}", sub=f"{inv_pct:.1f}%")
        self.card_positions.update_value(str(len(pos)))

        if self.side == "DAY":
            st = data.get("day_state", {})
            w,l = st.get("wins",0), st.get("losses",0)
            tot = w+l; wr = w/tot*100 if tot else 0
            pnl = st.get("total_pnl",0)
            self.card_wl.update_value(
                f"{w}W/{l}L", C["green"] if w>=l else C["red"])
            self.card_wr.update_value(
                f"{wr:.0f}%", C["green"] if wr>=50 else C["red"])
            self.card_bpl.update_value(
                f"${pnl:+,.2f}", C["green"] if pnl>=0 else C["red"])
            self.card_brackets.update_value(
                str(len(st.get("open_brackets",{}))))

    def _apply_closed_trades(self, data):
        import pandas as pd
        odf = data.get("orders", pd.DataFrame())
        if odf.empty:
            self.closed_trades_feed.load([])
            return
        filled = odf[odf["Filled"].notna()].copy()
        sells  = filled[filled["Side"] == "SELL"].sort_values(
            "Filled", ascending=False)
        if sells.empty:
            self.closed_trades_feed.load([])
            return

        now   = pd.Timestamp.now(tz="UTC")
        items = []
        for _, row in sells.head(25).iterrows():
            t      = row["Ticker"]
            qty    = float(row["Qty"])
            price  = float(row["Avg Fill"])
            filled_at = row["Filled"]

            # Compute per-ticker round-trip P/L
            t_ord  = filled[filled["Ticker"] == t]
            buys   = t_ord[t_ord["Side"] == "BUY"]
            t_sells= t_ord[t_ord["Side"] == "SELL"]
            avg_b  = ((buys["Qty"] * buys["Avg Fill"]).sum() /
                      buys["Qty"].sum()) if (not buys.empty and buys["Qty"].sum() > 0) else 0
            avg_s  = ((t_sells["Qty"] * t_sells["Avg Fill"]).sum() /
                      t_sells["Qty"].sum()) if (not t_sells.empty and t_sells["Qty"].sum() > 0) else price
            pl     = (avg_s - avg_b) * t_sells["Qty"].sum() if avg_b > 0 else 0
            pl_pct = (avg_s / avg_b - 1) * 100 if avg_b > 0 else 0

            # Human-readable "when"
            delta  = now - filled_at
            secs   = int(delta.total_seconds())
            if secs < 3600:
                when = f"{secs//60}m ago"
            elif secs < 86400:
                when = f"{secs//3600}h ago"
            else:
                when = f"{secs//86400}d ago"

            items.append({
                "ticker":   t,
                "qty":      qty,
                "avg_sell": avg_s,
                "pl":       pl,
                "pl_pct":   pl_pct,
                "when":     when,
            })

        self.closed_trades_feed.load(items)

    def _apply_signal(self, data):
        log = data.get("log")
        if log is None or (hasattr(log,"empty") and log.empty): return
        last = log.iloc[-1]
        dec  = str(last.get("decision","—"))
        conf = last.get("confidence")
        ana  = str(last.get("analysis","—"))
        act  = str(last.get("action","—"))
        t    = last.get("time")
        ts   = t.strftime("%b %d %Y  %H:%M UTC") if hasattr(t,"strftime") else "—"
        dc   = {"BUY":C["green"],"SELL":C["red"],"HOLD":C["muted"],
                "ALLOCATE":C["purple"],"BRACKET PLACED":C["orange"]}.get(dec,C["text"])
        self.sig_dec_val.setText(dec)
        self.sig_dec_val.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:{dc};")
        self.sig_conf_val.setText(f"{conf:.0%}" if conf else "—")
        self.sig_conf_val.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:{dc};")
        self.sig_ana_val.setText(ana[:120]+("..." if len(ana)>120 else ""))
        self.sig_act_val.setText(act[:120]+("..." if len(act)>120 else ""))
        self.sig_act_val.setStyleSheet(
            f"font-size:11px;color:{C['muted']};font-style:italic;")
        self.sig_time_lbl.setText(ts)

    def _apply_gauge(self, data):
        pos  = data.get("positions", [])
        meta = data.get("position_meta", {})
        if self.side == "DAY":
            st   = data.get("day_state", {})
            html = CH.bracket_gauge(st.get("open_brackets",{}), pos, meta=meta)
        else:
            html = CH.position_gauge(pos, self.side, meta=meta)
        self.gauge_chart.load_chart(html)

    def _apply_equity(self, data):
        if not data: return
        period = self.period_combo.currentText()
        eq     = data.get("history")
        snaps  = data.get("snapshots")
        events = data.get("trade_events") or []
        import pandas as pd
        if eq is None:    eq    = pd.DataFrame()
        if snaps is None: snaps = pd.DataFrame()
        self.equity_chart.load_chart(
            CH.equity_curve(eq, self.side, period, events=events))
        self.lifetime_chart.load_chart(CH.lifetime_chart(snaps, self.side))

    def _apply_trades(self, data):
        import pandas as pd
        odf = data.get("orders", pd.DataFrame())
        self.timeline_chart.load_chart(CH.trade_timeline_chart(odf, self.side))
        if odf.empty: return
        filled = odf[odf["Filled"].notna()].copy()
        now    = pd.Timestamp.now(tz="UTC")
        rows   = []
        for t in sorted(filled["Ticker"].unique()):
            t_ord = filled[filled["Ticker"]==t].sort_values("Filled")
            buys  = t_ord[t_ord["Side"]=="BUY"]
            sells = t_ord[t_ord["Side"]=="SELL"]
            if buys.empty: continue
            avg_b = ((buys["Qty"]*buys["Avg Fill"]).sum()/buys["Qty"].sum()
                     if buys["Qty"].sum()>0 else 0)
            avg_s = ((sells["Qty"]*sells["Avg Fill"]).sum()/sells["Qty"].sum()
                     if (not sells.empty and sells["Qty"].sum()>0) else 0)
            pl    = (avg_s-avg_b)*sells["Qty"].sum() if not sells.empty else 0
            pl_p  = (avg_s/avg_b-1)*100 if (avg_b>0 and not sells.empty) else 0
            opened= buys["Filled"].iloc[0]
            closed= sells["Filled"].iloc[-1] if not sells.empty else None
            dur   = ((closed or now)-opened).total_seconds()/3600
            rows.append({
                "Ticker": t,
                "Status": "OPEN" if sells.empty else "CLOSED",
                "Opened": opened.strftime("%b %d %H:%M") if pd.notna(opened) else "—",
                "Closed": (closed.strftime("%b %d %H:%M")
                           if (closed and pd.notna(closed)) else "OPEN"),
                "Hold(h)":  round(dur,1),
                "Notional": round(buys["Notional"].sum(),2),
                "P/L ($)":  round(pl,2),
                "P/L (%)":  round(pl_p,2),
            })

        def pl_color(v):
            try: return C["green"] if float(v)>=0 else C["red"]
            except: return None

        self.trade_table.load(rows,
            ["Ticker","Status","Opened","Closed","Hold(h)","Notional","P/L ($)","P/L (%)"],
            color_rules={"P/L ($)":pl_color,"P/L (%)":pl_color})

    def _apply_pl(self, data):
        import pandas as pd, json as _json
        pos = data.get("positions", [])
        odf = data.get("orders", pd.DataFrame())
        self.pl_chart.load_chart(CH.pl_bar_chart(pos, odf, self.side))
        if not pos: return
        labels = [p["symbol"] for p in pos]
        values = [abs(float(p["market_value"])) for p in pos]
        pie_data = [{
            "type":"pie","labels":labels,"values":values,"hole":0.52,
            "textinfo":"label+percent","textfont":{"size":10},
            "marker":{"line":{"color":"#060a0e","width":2}},
            "sort":True,
            "hovertemplate":"%{label}<br><b>$%{value:,.2f}</b><br>%{percent}<extra></extra>",
        }]
        pie_layout = {
            "paper_bgcolor":"rgba(0,0,0,0)","plot_bgcolor":"rgba(0,0,0,0)",
            "font":{"family":"'JetBrains Mono',monospace","size":10,"color":C["text"]},
            "margin":{"l":8,"r":8,"t":30,"b":8},"height":220,
            "showlegend":False,
            "title":{"text":"Allocation","font":{"size":11,"color":self.color},"x":0},
            "template":"plotly_dark",
        }
        from core.charts import _make_html
        self.pie_chart.load_chart(_make_html(pie_data, pie_layout))

    def _apply_risk(self, data):
        import pandas as pd
        snaps = data.get("snapshots", pd.DataFrame())
        if snaps.empty or "portfolio_value" not in snaps.columns: return
        m = D.risk_metrics(snaps["portfolio_value"])
        if not m: return
        self.dd_chart.load_chart(CH.drawdown_chart(snaps))
        days = max(1, (snaps["time"].max() - snaps["time"].min()).days + 1)
        avg_daily = m["total_return"] / days if days > 0 else 0
        updates = {
            "TOTAL RETURN":(f"{m['total_return']:+.2f}%",
                            C["green"] if m["total_return"]>=0 else C["red"]),
            "SHARPE":      (f"{m['sharpe']:.3f}",
                            C["green"] if m["sharpe"]>=1 else
                            (C["yellow"] if m["sharpe"]>=0 else C["red"])),
            "MAX DD":      (f"{m['max_dd']:.2f}%",
                            C["red"] if m["max_dd"]<-10 else
                            (C["yellow"] if m["max_dd"]<-5 else C["green"])),
            "WIN RATE":    (f"{m['win_rate']:.1f}%",
                            C["green"] if m["win_rate"]>=50 else C["red"]),
            "VOLATILITY":  (f"{m['volatility']:.2f}%/yr", C["muted"]),
            "AVG DAILY":   (f"{avg_daily:+.3f}%/day",
                            C["green"] if avg_daily>=0 else C["red"]),
        }
        for key,(val,color) in updates.items():
            if key in self.risk_cards:
                self.risk_cards[key].update_value(val, color)

    def _apply_costs(self, data):
        costs = data.get("costs", {})
        self.cost_total.update_value(
            f"${costs.get('total',0):.4f}", C["yellow"])
        self.cost_per_day.update_value(
            f"${costs.get('per_day',0):.4f}/day", C["muted"])
        self.cost_calls.update_value(
            str(costs.get("calls",0)), C["muted"])

    def _apply_positions(self, data):
        pos = data.get("positions", [])
        a   = data.get("account", {})
        pv  = a.get("portfolio_value", 1)
        rows = []
        tickers = []
        for p in sorted(pos, key=lambda x: abs(x.get("market_value",0)), reverse=True):
            mv  = float(p["market_value"])
            unr = float(p["unrealized_pl"])
            rows.append({
                "Ticker":     p["symbol"],
                "Qty":        round(float(p["qty"]),6),
                "Value ($)":  round(mv,2),
                "Weight %":   round(abs(mv)/pv*100,2) if pv else 0,
                "Avg Entry":  round(float(p["avg_entry_price"]),3),
                "Unreal P/L": round(unr,2),
                "Dust?":      "⚠" if abs(mv)<1.0 else "",
            })
            tickers.append(p["symbol"])

        def unr_color(v):
            try: return C["green"] if float(v)>=0 else C["red"]
            except: return None

        self.pos_table.load(rows,
            ["Ticker","Qty","Value ($)","Weight %","Avg Entry","Unreal P/L","Dust?"],
            color_rules={"Unreal P/L":unr_color})

        current = self.pos_combo.currentText()
        self.pos_combo.clear()
        self.pos_combo.addItems(tickers)
        if current in tickers:
            self.pos_combo.setCurrentText(current)

    def _liquidate(self):
        ticker = self.pos_combo.currentText()
        if not ticker:
            self.liq_msg.setText("Select a position first")
            return
        msg = D.close_position(self.side, ticker)
        self.liq_msg.setText(msg)
        QTimer.singleShot(3000, lambda: self.liq_msg.setText(""))

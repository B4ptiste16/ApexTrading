"""
APEX Bot Tab — threaded refresh so UI never freezes.
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QSizePolicy, QDoubleSpinBox, QSpinBox,
    QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer

from ui.styles  import COLORS, BOT_COLOR
from ui.widgets import (
    ChartView, LazyChartView, MetricCard, SectionHeader,
    BotProcessWidget, ScrollContent, DataTable, ClosedTradesFeed,
    NoScrollComboBox,
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
        # v3.1.9 — custom bots don't have a BOT_COLOR entry. Fall back to
        # the colour the registry recorded for them (set when the user
        # imported / published the bot), else a neutral purple.
        self.color = BOT_COLOR.get(side) or self._resolve_custom_color(side)
        # Same defensive lookup for the script path
        self.script = D.BOT_SCRIPTS.get(side) or self._resolve_custom_script(side)
        self._worker = None          # keep reference so GC doesn't kill it
        self._cached = {}            # last fetched data

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build_ui()

    @staticmethod
    def _resolve_custom_color(side: str) -> str:
        """Look up the colour the user picked when they imported a
        custom bot. Returns C['purple'] if the entry can't be found."""
        try:
            s   = D.load_settings()
            key = D.bot_registry_key()
            reg = s.get(key) or s.get("bot_registry", {})
            for c in reg.get("custom", []):
                if str(c.get("id", "")).upper() == side.upper():
                    return c.get("color") or COLORS["purple"]
        except Exception:
            pass
        return COLORS["purple"]

    @staticmethod
    def _resolve_custom_script(side: str):
        """Where the bot's .py / .apex lives on disk."""
        from pathlib import Path as _P
        try:
            s   = D.load_settings()
            key = D.bot_registry_key()
            reg = s.get(key) or s.get("bot_registry", {})
            for c in reg.get("custom", []):
                if str(c.get("id", "")).upper() == side.upper():
                    p = _P(c.get("script", ""))
                    if p.exists():
                        return p
                    # Maybe the user toggled the library lock — look for the sibling
                    for ext in (".apex", ".py"):
                        alt = p.with_suffix(ext)
                        if alt.exists():
                            return alt
                    # V4.6.101 — registry paths can go stale (the per-account
                    # migration moved bots/ into accounts/<id>/). Resolve by
                    # BASENAME inside this account's bots dir so old absolute
                    # paths keep working, and heal the registry entry.
                    from core.paths import ACCOUNT_DIR
                    for ext in (p.suffix or ".py", ".py", ".apex"):
                        cand = ACCOUNT_DIR / "bots" / (p.stem + ext)
                        if cand.exists():
                            try:
                                c["script"] = str(cand)
                                s[key] = reg
                                import json as _json
                                with open(D.SETTINGS_FILE, "w",
                                          encoding="utf-8") as f:
                                    _json.dump(s, f, indent=2)
                            except Exception:
                                pass
                            return cand
        except Exception:
            pass
        return None

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

        # V4.6.69 — RECENT CLOSED TRADES moved to the bottom, side-by-side with
        # the positions table (see POSITION MANAGEMENT section).

        # 3. SIGNAL
        s.add(SectionHeader("LAST AI SIGNAL", self.color))
        s.add(self._build_signal_panel())
        # V4.6.130 — only AI bots get the AI MODEL selector. Pure algorithmic /
        # ML bots don't call an LLM, so a model picker is misleading for them.
        if self._bot_uses_ai():
            s.add(self._build_ai_config_panel())

        # 4. BOT CONTROLS
        s.add(SectionHeader("BOT CONTROLS", self.color))
        self.bot_ctrl = BotProcessWidget(self.side, self.script)
        s.add(self.bot_ctrl)

        # V4.6.73 — the per-bot universe picker was removed. A bot's
        # ticker universe is now assigned ONCE, at creation (Make Bot's
        # "Ticker universe" dropdown, fed by the server's themed public
        # universes), and baked into the bot's code. There's no longer a
        # per-tab dropdown to reassign it. (_build_universe_picker and the
        # APEX_BOT_UNIVERSE resolution remain for any legacy custom
        # universe scripts a user may still have, but aren't shown here.)

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

        # V4.6.66 — adjustable AI call delay (seconds between cycles) with a
        # LIVE cost preview and a confirm step before applying. Floored at 30s.
        delay_row = QHBoxLayout()
        delay_lbl = QLabel("AI call delay (sec):")
        delay_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(D.CALL_DELAY_FLOOR, 3600)
        self.delay_spin.setSingleStep(15)
        self.delay_spin.setFixedWidth(90)
        self.delay_spin.setValue(D.get_bot_call_delay(self.side))
        self.delay_spin.valueChanged.connect(self._update_delay_cost)
        self.delay_apply = QPushButton("Apply")
        self.delay_apply.setObjectName("toolBtn")
        self.delay_apply.setMinimumWidth(78)
        self.delay_apply.setStyleSheet(
            f"QPushButton{{background:rgba(138,147,201,0.18);color:{C['purple']};"
            f"border:none;border-radius:4px;padding:4px 14px;font-size:10px;"
            f"font-weight:700;}}QPushButton:hover{{background:rgba(138,147,201,0.32);}}")
        self.delay_apply.clicked.connect(self._on_delay_apply)
        self.delay_saved = QLabel("")
        self.delay_saved.setStyleSheet(f"color:{C['green']};font-size:10px;")
        self.delay_cost = QLabel("")
        self.delay_cost.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        delay_row.addWidget(delay_lbl)
        delay_row.addWidget(self.delay_spin)
        delay_row.addWidget(self.delay_apply)
        delay_row.addWidget(self.delay_saved)
        delay_row.addWidget(self.delay_cost)
        delay_row.addStretch()
        drw = QWidget(); drw.setLayout(delay_row)
        s.add(drw)
        self._update_delay_cost()

        # V4.6.89 — Minimum positions floor for EVERY bot (was LONG-only):
        # deploy at least N names even when the AI is cautious. 0 = fully
        # cautious; per-bot setting keyed by this bot's side.
        if True:
            mp_row = QHBoxLayout()
            mp_lbl = QLabel("Min positions to hold:")
            mp_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
            self.minpos_spin = QSpinBox()
            self.minpos_spin.setRange(0, 20)
            self.minpos_spin.setFixedWidth(70)
            self.minpos_spin.setValue(D.get_bot_min_positions(self.side))
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
        self.gauge_chart = LazyChartView(height=260)
        s.add(self.gauge_chart)

        # 6. EQUITY CURVES
        s.add(SectionHeader("EQUITY CURVES", self.color,
                            controls=self._period_combo()))
        cr = QHBoxLayout()
        self.equity_chart   = LazyChartView(height=260)
        self.lifetime_chart = LazyChartView(height=260)
        cr.addWidget(self.equity_chart)
        cr.addWidget(self.lifetime_chart)
        cw = QWidget(); cw.setLayout(cr)
        s.add(cw)

        # 7. TRADE HISTORY
        s.add(SectionHeader("TRADE HISTORY", self.color))
        self.timeline_chart = LazyChartView(height=260)
        s.add(self.timeline_chart)
        s.add(QLabel("Trade Summary — open trades (closed trades are in the feed below)",
                      styleSheet=f"color:{C['muted']};font-size:10px;margin-top:6px;"))
        self.trade_table = DataTable()
        self.trade_table.setFixedHeight(200)
        s.add(self.trade_table)

        # 8. P/L & ALLOCATION
        s.add(SectionHeader("P/L & ALLOCATION", self.color))
        pr = QHBoxLayout()
        self.pl_chart  = LazyChartView(height=220)
        self.pie_chart = LazyChartView(height=220)
        pr.addWidget(self.pl_chart)
        pr.addWidget(self.pie_chart)
        pw = QWidget(); pw.setLayout(pr)
        s.add(pw)

        # 9. RISK METRICS & STATS
        s.add(SectionHeader("RISK METRICS & STATS", self.color))
        rr = QHBoxLayout()
        rr.setSpacing(14)
        self.dd_chart = LazyChartView(height=180)
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
        self.cost_calls   = MetricCard("AI CALLS",     "—", C["muted"])
        for c in [self.cost_total, self.cost_per_day, self.cost_calls]:
            cr2.addWidget(c)
        cr2.addStretch()
        cw2 = QWidget(); cw2.setLayout(cr2)
        s.add(cw2)

        # 11. POSITIONS + RECENT CLOSED TRADES (half/half, V4.6.69)
        # V4.6.110 — header carries a "Show P/L" toggle that hides the realised
        # (closed trades) AND unrealised (open positions) gain figures on demand.
        _ph = QHBoxLayout(); _ph.setContentsMargins(0, 0, 0, 0)
        _ph.addWidget(SectionHeader("POSITIONS & CLOSED TRADES", self.color), 1)
        self.pl_toggle = QCheckBox("Show P/L")
        self.pl_toggle.setChecked(True)
        self.pl_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pl_toggle.setStyleSheet(
            f"QCheckBox{{color:{C['muted']};font-size:10px;"
            f"letter-spacing:1px;spacing:6px;}}"
            f"QCheckBox::indicator{{width:13px;height:13px;border-radius:3px;"
            f"border:1px solid {C['muted']};}}"
            f"QCheckBox::indicator:checked{{background:{self.color};"
            f"border:1px solid {self.color};}}")
        self.pl_toggle.toggled.connect(self._on_toggle_pl)
        _ph.addWidget(self.pl_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        _phw = QWidget(); _phw.setLayout(_ph)
        s.add(_phw)
        self._show_pl = True
        self.pos_table = DataTable()
        self.pos_table.setFixedHeight(190)
        self.closed_trades_feed = ClosedTradesFeed()
        split = QHBoxLayout()
        split.setSpacing(12)
        _left = QVBoxLayout(); _left.setSpacing(4)
        _lt = QLabel("Open positions")
        _lt.setStyleSheet(f"color:{C['muted']};font-size:10px;letter-spacing:1px;")
        _left.addWidget(_lt); _left.addWidget(self.pos_table)
        _lw = QWidget(); _lw.setLayout(_left)
        _right = QVBoxLayout(); _right.setSpacing(4)
        _rt = QLabel("Recent closed trades")
        _rt.setStyleSheet(f"color:{C['muted']};font-size:10px;letter-spacing:1px;")
        _right.addWidget(_rt); _right.addWidget(self.closed_trades_feed)
        _rw = QWidget(); _rw.setLayout(_right)
        split.addWidget(_lw, 1)
        split.addWidget(_rw, 1)
        spw = QWidget(); spw.setLayout(split)
        s.add(spw)
        mr = QHBoxLayout()
        self.pos_combo = NoScrollComboBox()
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
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
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

    def _bot_uses_ai(self) -> bool:
        """True if this bot actually calls an LLM at runtime (so the AI MODEL
        selector is relevant). Detected from the bot's source — built-ins import
        core.ai_client; pure algorithmic / ML custom bots don't. Cached per tab."""
        cached = getattr(self, "_uses_ai_cache", None)
        if cached is not None:
            return cached
        uses = True
        try:
            from pathlib import Path as _P
            if self.script and _P(self.script).exists():
                src = _P(self.script).read_text(encoding="utf-8", errors="replace")
                uses = ("ai_client" in src or "call_ai" in src
                        or "load_ai_config" in src or "AI_PROVIDER" in src)
        except Exception:
            uses = True            # unknown → keep the selector (safe default)
        self._uses_ai_cache = uses
        return uses

    def _build_ai_config_panel(self) -> QFrame:
        """Compact AI provider + model + mode selector shown beneath
        LAST AI SIGNAL.  Settings are saved per-bot as AI_PROVIDER_<SIDE>
        etc. in .env and synced to Oracle with the regular key sync."""
        from core.ai_client import (PROVIDER_LABELS, PROVIDER_MODELS,
                                    provider_supports_vision)

        cfg = D.get_bot_ai_config(self.side)

        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        fl = QGridLayout(frame)
        fl.setContentsMargins(14, 10, 14, 10)
        fl.setSpacing(8)
        fl.setColumnStretch(1, 1)

        title = QLabel("AI MODEL")
        title.setStyleSheet(
            f"font-size:8px;color:{C['muted']};letter-spacing:3px;"
            f"font-weight:700;font-family:'JetBrains Mono';")
        fl.addWidget(title, 0, 0, 1, 4)

        # Provider
        prov_lbl = QLabel("Provider")
        prov_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        self._bot_prov_combo = NoScrollComboBox()
        self._bot_prov_combo.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:4px;padding:4px;")
        saved_prov = cfg.get("provider", "anthropic")
        for key, label in PROVIDER_LABELS.items():
            self._bot_prov_combo.addItem(label, key)
            if key == saved_prov:
                self._bot_prov_combo.setCurrentIndex(
                    self._bot_prov_combo.count() - 1)
        fl.addWidget(prov_lbl,             1, 0)
        fl.addWidget(self._bot_prov_combo, 1, 1)

        # Model
        model_lbl = QLabel("Model")
        model_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        self._bot_model_combo = NoScrollComboBox()
        self._bot_model_combo.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:4px;padding:4px;")
        fl.addWidget(model_lbl,              1, 2)
        fl.addWidget(self._bot_model_combo,  1, 3)

        # Mode
        mode_lbl = QLabel("Mode")
        mode_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        self._bot_mode_combo = NoScrollComboBox()
        self._bot_mode_combo.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:4px;padding:4px;")
        self._bot_mode_combo.addItem("Vision  (charts)", "vision")
        self._bot_mode_combo.addItem("Text-only  (no charts — works with Groq)", "text")
        saved_mode = cfg.get("mode", "vision")
        mode_idx = self._bot_mode_combo.findData(saved_mode)
        if mode_idx >= 0:
            self._bot_mode_combo.setCurrentIndex(mode_idx)
        fl.addWidget(mode_lbl,             2, 0)
        fl.addWidget(self._bot_mode_combo, 2, 1, 1, 2)

        # Save button + status
        save_btn = QPushButton("Save")
        save_btn.setObjectName("toolBtn")
        save_btn.clicked.connect(self._save_ai_config)
        self._ai_cfg_msg = QLabel("")
        self._ai_cfg_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        fl.addWidget(save_btn,          2, 3)
        fl.addWidget(self._ai_cfg_msg,  3, 0, 1, 4)

        # Wire provider → update model list and enforce text mode for Groq
        def _on_prov(_idx):
            prov = self._bot_prov_combo.currentData()
            models = PROVIDER_MODELS.get(prov, [])
            saved_model = cfg.get("model", "")
            self._bot_model_combo.clear()
            for m in models:
                self._bot_model_combo.addItem(m)
            idx = self._bot_model_combo.findText(saved_model)
            if idx >= 0:
                self._bot_model_combo.setCurrentIndex(idx)
            if not provider_supports_vision(prov):
                self._bot_mode_combo.setCurrentIndex(
                    self._bot_mode_combo.findData("text"))
                self._bot_mode_combo.setEnabled(False)
            else:
                self._bot_mode_combo.setEnabled(True)

        self._bot_prov_combo.currentIndexChanged.connect(_on_prov)
        _on_prov(0)   # populate model list immediately

        # Live cost indicator: refresh whenever the selected provider/model
        # changes (the delay_cost label is built later, hence the hasattr guard).
        def _cost_refresh(*_a):
            if hasattr(self, "delay_cost"):
                self._update_delay_cost()
        self._bot_prov_combo.currentIndexChanged.connect(_cost_refresh)
        self._bot_model_combo.currentIndexChanged.connect(_cost_refresh)
        return frame

    def _save_ai_config(self):
        prov  = self._bot_prov_combo.currentData()  or "anthropic"
        model = self._bot_model_combo.currentText().strip()
        mode  = self._bot_mode_combo.currentData()  or "vision"
        D.set_bot_ai_config(self.side, prov, model, mode)
        self._ai_cfg_msg.setText("saving + syncing…")
        self._ai_cfg_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")

        # V4.6.130 — auto-push to the server so CLOUD bots actually use the new
        # provider/model (previously this only wrote .env locally, so cloud bots
        # kept the default — Claude). The running bot still needs a restart to
        # pick it up, so tell the user clearly.
        from PyQt6.QtCore import QThread, pyqtSignal as _Sig

        class _PushWorker(QThread):
            done = _Sig(bool)

            def run(self):
                try:
                    from core import account_store as _AS
                    self.done.emit(bool(_AS.push_keys_to_server()))
                except Exception as e:
                    print(f"[ai-config] push failed: {e}")
                    self.done.emit(False)

        def _on_done(ok):
            if ok:
                self._ai_cfg_msg.setText(
                    f"✓ saved + synced ({prov}/{model or 'default'}). "
                    f"RESTART this bot (⏹ then ▶) to trade on the new model.")
                self._ai_cfg_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            else:
                self._ai_cfg_msg.setText(
                    "saved locally — sign in / sync failed, so cloud bots won't "
                    "use it yet. Use Tools → Sync keys.")
                self._ai_cfg_msg.setStyleSheet(f"color:{C['orange']};font-size:10px;")
            QTimer.singleShot(12000, lambda: self._ai_cfg_msg.setText(""))

        w = _PushWorker()
        w.done.connect(_on_done)
        w.finished.connect(
            lambda _w=w: self._ai_workers.remove(_w)
            if _w in getattr(self, "_ai_workers", []) else None)
        if not hasattr(self, "_ai_workers"):
            self._ai_workers = []
        self._ai_workers.append(w)
        w.start()

    def _period_combo(self) -> NoScrollComboBox:
        self.period_combo = NoScrollComboBox()
        self.period_combo.addItems(["1D","1W","1M","3M","6M","1Y"])
        self.period_combo.setFixedWidth(80)
        # V4.6.111 — re-FETCH on period change (the history is fetched per
        # period). Re-rendering the cached 1D data could never show a week/month,
        # which is why switching to 1W/1M "did nothing". Repaint the cached view
        # instantly for feedback, then pull the wider history in the background.
        self.period_combo.currentTextChanged.connect(self._on_period_changed)
        return self.period_combo

    def _on_period_changed(self, _period: str):
        if getattr(self, "_cached", None):
            try:
                self._apply_equity(self._cached)
            except Exception:
                pass
        self.refresh()

    def _on_conf_changed(self, val: float):
        D.set_bot_min_conf(self.side, val)
        self.conf_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.conf_saved.setText(""))

    def _update_delay_cost(self):
        """Live cost indicator for the SELECTED model + call delay. Reads the
        provider/model from the dropdowns (even before Save) so you can compare
        what each model would cost to run."""
        try:
            v = self.delay_spin.value()
            prov = (self._bot_prov_combo.currentData()
                    if hasattr(self, "_bot_prov_combo") else None)
            model = (self._bot_model_combo.currentText().strip()
                     if hasattr(self, "_bot_model_combo") else None)
            est = D.estimate_daily_cost_at_delay(self.side, v,
                                                 provider=prov, model=model)
            warn = "  ⚠ very frequent" if v < 60 else ""
            label = (model or prov or "AI")
            free = est["per_day"] < 1e-9
            cost = ("FREE" if free else
                    f"${est['per_call']:.4f}/call · ${est['per_day']:.2f}/day · "
                    f"${est['per_month']:.2f}/mo")
            self.delay_cost.setText(
                f"{label} · ≈{est['calls_per_day']:.0f} calls/day · {cost}{warn}")
            self.delay_cost.setStyleSheet(
                f"color:{C['red'] if v < 60 else C['muted']};font-size:10px;")
        except Exception:
            pass

    def _on_delay_apply(self):
        """Confirm the new call delay (showing the projected cost) before saving.
        Faster than ~60s is flagged; 30s is the hard floor."""
        from PyQt6.QtWidgets import QMessageBox
        v = self.delay_spin.value()
        est = D.estimate_daily_cost_at_delay(self.side, v)
        msg = (f"Set {self.side} to call the AI every {v} seconds?\n\n"
               f"Estimated ~{est['calls_per_day']:.0f} AI calls/day\n"
               f"≈ ${est['per_day']:.2f}/day  ·  ${est['per_month']:.2f}/month\n\n")
        if v < 60:
            msg += ("⚠ Calling more often than ~60s can hit API rate limits, "
                    "spike your cost, and overlap cycles. 30s is the hard "
                    "minimum.\n\n")
        msg += "Apply? (takes effect on the bot's next cycle — no restart needed)"
        box = QMessageBox(self)
        box.setWindowTitle("Change AI call delay")
        box.setIcon(QMessageBox.Icon.Warning if v < 60
                    else QMessageBox.Icon.Question)
        box.setText(msg)
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self.delay_spin.setValue(D.get_bot_call_delay(self.side))
            return
        D.set_bot_call_delay(self.side, v)
        self.delay_saved.setText("saved ✓")
        QTimer.singleShot(2500, lambda: self.delay_saved.setText(""))

    # ── V4.6.5 — per-bot universe picker ─────────────────────────

    def _build_universe_picker(self, scroll):
        """V4.6.13 — dropdown that lists every UNIVERSE BOT (script)
        the user has registered, filtered by asset_type compatibility.
        When the user picks a universe bot, this trading bot reads
        from whatever .txt file that script writes (the script's
        META.universe field). Decoupled from the Universe tab's RUN
        dropdown — that one picks which script to EXECUTE, this one
        picks which script's OUTPUT this trading bot consumes.

        Stored as 'bot_universe_script_<SIDE>' in settings (id of the
        chosen universe bot, or '' for default). The resolved target
        file path is exported to the bot subprocess via APEX_BOT_UNIVERSE."""
        from PyQt6.QtWidgets import QHBoxLayout as _H, QLabel as _L, QWidget as _W
        try:
            from ui.widgets import NoScrollComboBox as _Combo
        except Exception:
            from PyQt6.QtWidgets import QComboBox as _Combo  # type: ignore

        my_type = self._bot_asset_type()
        # Each entry = (script_id, label, target_file_path, ok_compat)
        compat  = self._compatible_universe_scripts(my_type)
        cur_sel = D.load_settings().get(
            f"bot_universe_script_{self.side.upper()}", "")

        row = _H()
        lbl = _L("Universe bot:")
        lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        self._universe_combo = _Combo()
        # Always offer a "(default)" entry — bot uses its hardcoded
        # universe file (longbot_universe.txt, daybot_universe.txt, …).
        self._universe_combo.addItem(
            "(use this bot's own default universe)", "")
        selected_idx = 0
        for i, (script_id, label, target, ok) in enumerate(compat, start=1):
            self._universe_combo.addItem(label, script_id)
            if script_id == cur_sel:
                selected_idx = i
        self._universe_combo.setCurrentIndex(selected_idx)
        self._universe_combo.currentIndexChanged.connect(
            self._on_universe_changed)
        self._universe_saved = _L("")
        self._universe_saved.setStyleSheet(
            f"color:{C['green']};font-size:10px;")

        hint = _L(f"asset_type={my_type or 'unknown'}  ·  "
                  f"this bot reads from whichever universe bot you "
                  f"assign here · run/update the universe in the "
                  f"Universe tab")
        hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        hint.setWordWrap(True)

        row.addWidget(lbl)
        row.addWidget(self._universe_combo)
        row.addWidget(self._universe_saved)
        row.addStretch()
        w = _W(); w.setLayout(row)
        scroll.add(w)
        scroll.add(hint)

    def _bot_asset_type(self) -> str:
        """Read this bot's asset_type. Built-ins default to stocks;
        custom bots read it from their META block via script path."""
        # Built-in mapping
        builtin = {"LONG": "stocks", "SHORT": "stocks", "DAY": "stocks"}
        if self.side.upper() in builtin:
            return builtin[self.side.upper()]
        # Custom bot — parse META block
        if not self.script:
            return ""
        try:
            from core.bot_meta import parse_meta
            src = open(self.script, "r", encoding="utf-8").read()
            return (parse_meta(src) or {}).get("asset_type", "")
        except Exception:
            return ""

    def _compatible_universe_scripts(self, my_type: str) -> list:
        """V4.6.13 — return list of (script_id, label, target_file, ok).
        script_id  : settings key — '' for default, 'built-in' for
                     universe_manager.py, slug for custom universe bots
        label      : what shows in the dropdown
        target_file: the .txt file this script writes to (the bot will
                     read from this path)
        ok         : True if asset_type compatible with this trading bot
        Incompatible entries are filtered out when my_type is known."""
        out = []
        # Built-in universe_manager rewrites THREE files (LONG/SHORT/DAY).
        # The trading bot would use its own default file if it picks the
        # built-in option, so we represent it as a single 'use built-in
        # universe_manager scan' choice with the bot's default target.
        builtin_target = {
            "LONG":  "longbot_universe.txt",
            "SHORT": "shortbot_universe.txt",
            "DAY":   "daybot_universe.txt",
        }.get(self.side.upper(), "")
        out.append((
            "built-in",
            f"Built-in universe_manager  →  {builtin_target or '(no default for this bot)'}",
            builtin_target,
            True,  # built-in works for any built-in side
        ))
        # Custom universe bots registered via Make Bot
        try:
            s = D.load_settings()
            scripts = s.get("universe_scripts", []) or []
        except Exception:
            scripts = []
        for entry in scripts:
            if not isinstance(entry, dict):
                continue
            slug   = entry.get("id", "")
            if not slug:
                continue
            label_name = entry.get("label", slug)
            target = entry.get("target", "") or f"{slug}_universe.txt"
            u_type = (entry.get("asset_type", "") or "").lower()
            ok = (not my_type) or (not u_type) or (u_type == my_type)
            label = f"{label_name}  →  rewrites {target}"
            if u_type:
                label += f"  (asset_type={u_type})"
            if not ok:
                label += "  · incompatible"
            out.append((slug, label, target, ok))
        # Compatible first, then alphabetical; drop incompatible
        # entirely if we know our asset_type.
        out.sort(key=lambda t: (not t[3], t[1]))
        if my_type:
            out = [t for t in out if t[3]]
        return out

    def _compatible_universes(self, my_type: str) -> list:
        """v4.6.13 — kept for back-compat with any caller still using
        the file-list shape. Delegates to the new script-based picker
        and re-flattens its target files."""
        return [(t[2], t[1], t[3])
                for t in self._compatible_universe_scripts(my_type)
                if t[2]]

    @staticmethod
    def _universe_asset_type(path) -> str:
        """A universe .txt may declare its asset_type via a leading
        comment line like:    # asset_type: crypto
        Returns "" if unknown."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("#"):
                        # We hit the first ticker — no header found
                        return ""
                    low = line.lstrip("#").strip().lower()
                    if low.startswith("asset_type"):
                        _, _, v = low.partition(":")
                        return v.strip()
        except Exception:
            pass
        return ""

    def _on_universe_changed(self, idx: int):
        """V4.6.13 — stores the chosen universe BOT id (not file path).
        widgets.py BotProcessWidget.start_bot() resolves the id to the
        actual target file at launch time, so even if the user later
        changes the universe bot's META.universe field, the trading
        bot tracks it automatically."""
        script_id = self._universe_combo.currentData() or ""
        try:
            s = D.load_settings()
            s[f"bot_universe_script_{self.side.upper()}"] = script_id
            import json as _j
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _j.dump(s, f, indent=2)
            label = self._universe_combo.currentText()
            self._universe_saved.setText(
                f"saved ✓ (next start: {label[:30]}…)")
            QTimer.singleShot(4000,
                              lambda: self._universe_saved.setText(""))
        except Exception as e:
            self._universe_saved.setText(f"save failed: {e}")
            self._universe_saved.setStyleSheet(
                f"color:{C['red']};font-size:10px;")

    def _on_minscore_changed(self, val: float):
        D.set_bot_min_score("LONG", val)
        self.minscore_saved.setText("saved ✓")
        QTimer.singleShot(2000, lambda: self.minscore_saved.setText(""))

    def _on_minpos_changed(self, val: int):
        D.set_bot_min_positions(self.side, val)
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
        # V4.6.43 — paint fast local history the instant it's ready (phase 1),
        # then refresh with live broker data (phase 2). Both go through the
        # same applier, so a slow/unreachable broker never delays the
        # historical charts and a stopped bot still shows all its data.
        self._worker.partial.connect(self._on_data)
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
        # Cache only the full (done) snapshot so cached re-renders (e.g. the
        # show-P/L toggle) never replay the partial phase's empty positions.
        if not data.get("_partial"):
            self._cached = data
        # V4.6.132 — the fast (partial) phase carries empty live-data
        # placeholders; skip the appliers that render LIVE broker data so the
        # gauge / positions / account / P/L never flash to 0 / fewer positions
        # when you re-enter a tab. They update on the 'done' phase.
        partial = bool(data.get("_partial"))
        _live_only = {"account", "gauge", "positions", "pl"}
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
                if partial and name in _live_only:
                    continue
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
        # V4.6.110 — show the FULL closed-trade history (the feed scrolls); cap
        # high only to bound widget count on a very long-lived bot.
        for _, row in sells.head(500).iterrows():
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
        conf = last.get("confidence")
        ana  = str(last.get("analysis","—"))
        act  = str(last.get("action","—"))
        # V4.6.141 — the parser often leaves decision="" (the model wrote an
        # analysis + action but no explicit verb). Derive it: use the action's
        # verb if present, else — when an evaluation clearly ran (analysis or a
        # confidence number) — treat it as HOLD rather than showing a blank "—".
        dec = (str(last.get("decision") or "")).strip()
        if not dec:
            _a = act.strip().upper()
            if _a.startswith("BUY"):
                dec = "BUY"
            elif _a.startswith(("SELL", "COVER")):
                dec = "SELL"
            elif _a.startswith("HOLD"):
                dec = "HOLD"
            elif (ana and ana != "—") or (conf not in (None, "", "—")):
                dec = "HOLD"
            else:
                dec = "—"
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
        # V4.6.137 — the Trade Summary lists ONLY OPEN trades; closed trades have
        # their own "Recent closed trades" feed. "Open" is taken from the LIVE
        # positions, so this table matches the position gauge exactly, and each
        # open row shows its live UNREALISED P/L instead of a misleading $0.
        positions = data.get("positions")
        has_pos   = isinstance(positions, list)
        upl: dict = {}
        if has_pos:
            for p in positions:
                try:
                    upl[str(p.get("symbol", "")).upper()] = (
                        float(p.get("unrealized_pl", 0) or 0),
                        float(p.get("unrealized_plpc", 0) or 0) * 100.0)
                except Exception:
                    pass
        filled = odf[odf["Filled"].notna()].copy()
        now    = pd.Timestamp.now(tz="UTC")
        rows   = []
        for t in sorted(filled["Ticker"].unique()):
            t_ord = filled[filled["Ticker"]==t].sort_values("Filled")
            buys  = t_ord[t_ord["Side"]=="BUY"]
            sells = t_ord[t_ord["Side"]=="SELL"]
            if buys.empty: continue
            # Open = currently held (matches the gauge). Without live positions,
            # fall back to "no exit recorded yet".
            is_open = (str(t).upper() in upl) if has_pos else sells.empty
            if not is_open:
                continue
            pl, pl_p = upl.get(str(t).upper(), (0.0, 0.0))
            opened   = buys["Filled"].iloc[0]
            dur      = (now - opened).total_seconds() / 3600
            rows.append({
                "Ticker":   t,
                "Status":   "OPEN",
                "Opened":   opened.strftime("%b %d %H:%M") if pd.notna(opened) else "—",
                "Closed":   "OPEN",
                "Hold(h)":  round(dur, 1),
                "Notional": round(buys["Notional"].sum(), 2),
                "P/L ($)":  round(pl, 2),
                "P/L (%)":  round(pl_p, 2),
            })

        def pl_color(v):
            try: return C["green"] if float(v)>=0 else C["red"]
            except: return None

        self.trade_table.load(rows,
            ["Ticker","Opened","Hold(h)","Notional","P/L ($)","P/L (%)"],
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

    def _on_toggle_pl(self, checked: bool):
        """V4.6.110 — show/hide realised + unrealised gains across the closed
        trades feed and the open-positions table. Re-renders from the cached
        data so the change is instant (no broker round-trip)."""
        self._show_pl = checked
        try:
            self.closed_trades_feed.set_show_pl(checked)
        except Exception:
            pass
        if getattr(self, "_cached", None):
            try:
                self._apply_positions(self._cached)
            except Exception:
                pass

    def _apply_positions(self, data):
        pos = data.get("positions", [])
        a   = data.get("account", {})
        pv  = a.get("portfolio_value", 1)
        show_pl = getattr(self, "_show_pl", True)
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
                "Unreal P/L": round(unr,2) if show_pl else "•••",
                "Dust?":      "⚠" if abs(mv)<1.0 else "",
            })
            tickers.append(p["symbol"])

        def unr_color(v):
            if not show_pl:
                return None
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

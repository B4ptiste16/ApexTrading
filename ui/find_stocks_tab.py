"""
BAPTOU Trading Platform — Find Stocks Tab  (manual mode)
─────────────────────────────────────────────────────────────────────
Search any stock available on the broker, see a broker-app-style quote
(price, day change, charts/curves, fundamentals) and a weekly AI
evaluation (refreshed every Monday). "Trade this" hands the symbol to
the Manual Trading tab's order form.

Data: yfinance (charts + fundamentals, free) and the dedicated MANUAL
Alpaca account (the broker's tradable-asset list). AI evaluation routes
through the app's configured AI provider and is cached per ISO week.
No orders are placed here.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QGridLayout, QLineEdit, QListWidget, QListWidgetItem, QSizePolicy,
)

from ui.styles  import COLORS
from ui.widgets import ScrollContent, ChartView

C = COLORS


# ── Workers ───────────────────────────────────────────────────────────────

class _SearchWorker(QThread):
    done = pyqtSignal(str, list)        # (query, results)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        try:
            from core import stock_research as SR
            res = SR.search_assets(self.query)
        except Exception as e:
            print(f"[find] search failed: {e}")
            res = []
        self.done.emit(self.query, res)


class _SnapshotWorker(QThread):
    done = pyqtSignal(str, dict)        # (symbol, snapshot)

    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol

    def run(self):
        try:
            from core import stock_research as SR
            snap = SR.get_snapshot(self.symbol)
        except Exception as e:
            snap = {"symbol": self.symbol, "ok": False, "error": str(e)}
        self.done.emit(self.symbol, snap)


class _HistoryWorker(QThread):
    done = pyqtSignal(str, str, object)   # (symbol, period, DataFrame)

    def __init__(self, symbol: str, period: str):
        super().__init__()
        self.symbol, self.period = symbol, period

    def run(self):
        try:
            from core import stock_research as SR
            df = SR.get_history(self.symbol, self.period)
        except Exception as e:
            print(f"[find] history failed: {e}")
            df = None
        self.done.emit(self.symbol, self.period, df)


class _WarmWorker(QThread):
    """Pre-fetch the broker asset list off the GUI thread so the first search
    returns instantly (and never blocks the UI)."""
    done = pyqtSignal()

    def run(self):
        try:
            from core import stock_research as SR
            SR.list_tradable_assets()
        except Exception as e:
            print(f"[find] warm failed: {e}")
        self.done.emit()


class _EvalWorker(QThread):
    done = pyqtSignal(str, dict)        # (symbol, eval)

    def __init__(self, symbol: str, snap: dict, force: bool = False):
        super().__init__()
        self.symbol, self.snap, self.force = symbol, snap, force

    def run(self):
        try:
            from core import stock_research as SR
            ev = SR.weekly_evaluation(self.symbol, self.snap, force=self.force)
        except Exception as e:
            ev = {"symbol": self.symbol, "rating": "—", "text": "",
                  "error": str(e)}
        self.done.emit(self.symbol, ev)


# ── Tab ───────────────────────────────────────────────────────────────────

PERIODS = ["1D", "1W", "1M", "3M", "6M", "1Y", "5Y"]


class FindStocksTab(QWidget):
    """Search + research any broker-tradable stock (manual mode)."""

    trade_requested = pyqtSignal(str)   # symbol → main switches to MANUAL tab

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers: list = []
        self._cur_symbol = ""
        self._cur_period = "6M"
        self._cur_type   = "candle"
        self._hist_cache: dict = {}     # (symbol, period) -> DataFrame
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build()

    def on_shown(self):
        """Called by the main window when this tab is activated — warm the
        broker asset list (off-thread) so the first search is instant."""
        if getattr(self, "_warmed", False):
            return
        try:
            from core import stock_research as SR
            if SR._cache_fresh():
                self._warmed = True
                return
            w = _WarmWorker()
            w.done.connect(lambda: setattr(self, "_warmed", True))
            w.finished.connect(lambda _w=w: self._drop_worker(_w))
            self._workers.append(w)
            w.start()
        except Exception as e:
            print(f"[find] warm start failed: {e}")

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        # Hero
        hero = QFrame()
        hero.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:12px;"
            f"border-left:4px solid {C['purple']};")
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(26, 20, 26, 18)
        hv.setSpacing(6)
        title = QLabel("🔎  FIND STOCKS")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;"
            f"color:{C['purple']};letter-spacing:3px;")
        hv.addWidget(title)
        note = QLabel(
            "Search any stock available on your broker, view live charts and key "
            "stats, and read a fresh AI evaluation refreshed every Monday.")
        note.setStyleSheet(f"color:{C['muted']};font-size:11px;letter-spacing:0.5px;")
        note.setWordWrap(True)
        hv.addWidget(note)
        s.add(hero)

        # Search box
        search_frame = QFrame()
        search_frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        sv = QVBoxLayout(search_frame)
        sv.setContentsMargins(16, 14, 16, 14)
        sv.setSpacing(8)

        row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search symbol or company — e.g. AAPL, Tesla, NVDA")
        self._search_edit.setStyleSheet(
            f"QLineEdit{{background:{C['bg']};color:{C['text']};border:1px solid "
            f"{C['border']};border-radius:6px;padding:9px 12px;font-size:13px;}}")
        self._search_edit.textChanged.connect(self._on_query_changed)
        self._search_edit.returnPressed.connect(self._on_search_enter)
        row.addWidget(self._search_edit)
        rw = QWidget(); rw.setLayout(row)
        sv.addWidget(rw)

        self._results = QListWidget()
        self._results.setStyleSheet(
            f"QListWidget{{background:{C['bg']};color:{C['text']};border:1px "
            f"solid {C['border']};border-radius:6px;font-size:12px;}}"
            f"QListWidget::item{{padding:6px 10px;}}"
            f"QListWidget::item:selected{{background:{C['panel2']};"
            f"color:{C['text']};}}")
        self._results.setMaximumHeight(190)
        self._results.itemActivated.connect(self._on_result_chosen)
        self._results.itemClicked.connect(self._on_result_chosen)
        self._results.setVisible(False)
        sv.addWidget(self._results)
        s.add(search_frame)

        # Empty-state hint (before any stock is picked)
        self._hint = QLabel(
            "Type a ticker or company name above to pull up its chart, "
            "fundamentals and weekly AI rating.")
        self._hint.setStyleSheet(
            f"color:{C['muted']};font-size:12px;padding:30px 8px;")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        s.add(self._hint)

        # Detail area (hidden until a stock is chosen)
        self._detail = QWidget()
        dl = QVBoxLayout(self._detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)
        self._build_detail(dl)
        self._detail.setVisible(False)
        s.add(self._detail)

        s.add_stretch()

    def _build_detail(self, layout: QVBoxLayout):
        # Price header
        head = QFrame()
        head.setStyleSheet(f"background:{C['panel']};border:none;border-radius:10px;")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(20, 16, 20, 16)
        hv.setSpacing(2)
        top = QHBoxLayout()
        self._h_symbol = QLabel("—")
        self._h_symbol.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:24px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        top.addWidget(self._h_symbol)
        top.addSpacing(12)
        self._h_price = QLabel("")
        self._h_price.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:24px;font-weight:700;"
            f"color:{C['text']};")
        top.addWidget(self._h_price)
        top.addSpacing(10)
        self._h_change = QLabel("")
        self._h_change.setStyleSheet(f"font-size:14px;font-weight:700;")
        top.addWidget(self._h_change)
        top.addStretch()
        self._trade_btn = QPushButton("⬆  Trade this stock")
        self._trade_btn.setObjectName("addBotBtn")
        self._trade_btn.clicked.connect(
            lambda: self.trade_requested.emit(self._cur_symbol))
        top.addWidget(self._trade_btn)
        tw = QWidget(); tw.setLayout(top)
        hv.addWidget(tw)
        self._h_name = QLabel("")
        self._h_name.setStyleSheet(f"color:{C['muted']};font-size:12px;")
        self._h_name.setWordWrap(True)
        hv.addWidget(self._h_name)
        layout.addWidget(head)

        layout.addSpacing(12)

        # Chart controls
        ctl = QHBoxLayout()
        ctl.setSpacing(6)
        self._period_btns: dict[str, QPushButton] = {}
        for p in PERIODS:
            b = QPushButton(p)
            b.setObjectName("toolBtn")
            b.setCheckable(True)
            b.setFixedWidth(46)
            b.clicked.connect(lambda _=False, pp=p: self._set_period(pp))
            self._period_btns[p] = b
            ctl.addWidget(b)
        ctl.addStretch()
        self._candle_btn = QPushButton("Candles")
        self._line_btn   = QPushButton("Line")
        for b, t in ((self._candle_btn, "candle"), (self._line_btn, "line")):
            b.setObjectName("toolBtn")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, tt=t: self._set_type(tt))
            ctl.addWidget(b)
        cw = QWidget(); cw.setLayout(ctl)
        layout.addWidget(cw)
        self._period_btns[self._cur_period].setChecked(True)
        self._candle_btn.setChecked(True)

        layout.addSpacing(6)
        self._chart = ChartView(height=360)
        layout.addWidget(self._chart)

        layout.addSpacing(12)

        # Key stats grid
        stats_frame = QFrame()
        stats_frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        sg = QVBoxLayout(stats_frame)
        sg.setContentsMargins(18, 14, 18, 14)
        sg.setSpacing(8)
        st = QLabel("KEY STATS")
        st.setStyleSheet(
            f"color:{C['purple']};font-size:10px;letter-spacing:3px;font-weight:800;")
        sg.addWidget(st)
        self._stats_grid = QGridLayout()
        self._stats_grid.setHorizontalSpacing(22)
        self._stats_grid.setVerticalSpacing(12)
        gw = QWidget(); gw.setLayout(self._stats_grid)
        sg.addWidget(gw)
        self._stat_cells: dict[str, QLabel] = {}
        self._stat_order = [
            ("Open", "open"), ("Day High", "day_high"), ("Day Low", "day_low"),
            ("Prev Close", "prev_close"),
            ("Volume", "volume"), ("Avg Volume", "avg_volume"),
            ("Market Cap", "market_cap"), ("P/E", "pe"),
            ("Fwd P/E", "forward_pe"), ("EPS", "eps"),
            ("Div Yield", "div_yield"), ("Beta", "beta"),
            ("52W High", "wk_high"), ("52W Low", "wk_low"),
            ("50D MA", "ma50"), ("200D MA", "ma200"),
        ]
        cols = 4
        for i, (label, key) in enumerate(self._stat_order):
            cell = self._make_stat_cell(label)
            self._stat_cells[key] = cell.findChild(QLabel, "statval")
            self._stats_grid.addWidget(cell, i // cols, i % cols)
        layout.addWidget(stats_frame)

        layout.addSpacing(12)

        # AI evaluation
        ai = QFrame()
        ai.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;"
            f"border-left:3px solid {C['orange']};")
        av = QVBoxLayout(ai)
        av.setContentsMargins(20, 16, 20, 16)
        av.setSpacing(10)
        ai_top = QHBoxLayout()
        ai_title = QLabel("🤖  AI EVALUATION")
        ai_title.setStyleSheet(
            f"color:{C['orange']};font-size:10px;letter-spacing:3px;font-weight:800;")
        ai_top.addWidget(ai_title)
        ai_top.addSpacing(10)
        self._ai_badge = QLabel("")
        self._ai_badge.setStyleSheet("font-size:12px;font-weight:800;")
        ai_top.addWidget(self._ai_badge)
        ai_top.addStretch()
        self._ai_meta = QLabel("")
        self._ai_meta.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        ai_top.addWidget(self._ai_meta)
        self._ai_refresh = QPushButton("↺ Refresh")
        self._ai_refresh.setObjectName("silenceBtn")
        self._ai_refresh.clicked.connect(self._refresh_eval)
        ai_top.addWidget(self._ai_refresh)
        atw = QWidget(); atw.setLayout(ai_top)
        av.addWidget(atw)
        self._ai_text = QLabel("")
        self._ai_text.setStyleSheet(f"color:{C['text']};font-size:12px;line-height:1.7;")
        self._ai_text.setWordWrap(True)
        self._ai_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        av.addWidget(self._ai_text)
        ai_note = QLabel("Evaluations are AI-generated from market data and refresh "
                         "weekly (every Monday). Informational only — not financial advice.")
        ai_note.setStyleSheet(f"color:{C['muted']};font-size:9px;line-height:1.5;")
        ai_note.setWordWrap(True)
        av.addWidget(ai_note)
        layout.addWidget(ai)

        # Business summary
        self._summary = QLabel("")
        self._summary.setStyleSheet(
            f"color:{C['muted']};font-size:11px;line-height:1.7;padding:10px 4px;")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

    def _make_stat_cell(self, label: str) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        lab = QLabel(label.upper())
        lab.setStyleSheet(
            f"color:{C['muted']};font-size:8px;letter-spacing:1.5px;"
            f"font-family:'JetBrains Mono';")
        val = QLabel("—")
        val.setObjectName("statval")
        val.setStyleSheet(
            f"color:{C['text']};font-size:13px;font-weight:700;"
            f"font-family:'Syne',sans-serif;")
        v.addWidget(lab)
        v.addWidget(val)
        return w

    # ── Search ──────────────────────────────────────────────────────────────

    def _on_query_changed(self, text: str):
        if not hasattr(self, "_search_timer"):
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._run_search)
        if len((text or "").strip()) < 1:
            self._results.setVisible(False)
            return
        self._search_timer.start(220)

    def _on_search_enter(self):
        self._search_timer_stop()
        q = self._search_edit.text().strip()
        # A plain ticker → load it straight away. A company name (has spaces or
        # is long) → fall back to the search list.
        compact = q.replace(".", "").replace("-", "")
        if q and " " not in q and compact.isalnum() and len(q) <= 6:
            self._results.setVisible(False)
            self.load_symbol(q)
        else:
            self._run_search()

    def _search_timer_stop(self):
        if hasattr(self, "_search_timer"):
            self._search_timer.stop()

    def _run_search(self):
        q = self._search_edit.text().strip()
        if not q:
            self._results.setVisible(False)
            return
        w = _SearchWorker(q)
        w.done.connect(self._on_search_done)
        w.finished.connect(lambda _w=w: self._drop_worker(_w))
        self._workers.append(w)
        w.start()

    def _on_search_done(self, query: str, results: list):
        # Ignore stale results if the box has moved on.
        if query != self._search_edit.text().strip():
            return
        self._results.clear()
        if not results:
            self._results.setVisible(False)
            return
        for a in results:
            sym = a.get("symbol", "")
            nm  = a.get("name", "")
            label = f"{sym}   {nm}" if nm else sym
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, sym)
            self._results.addItem(it)
        self._results.setVisible(True)

    def _on_result_chosen(self, item: QListWidgetItem):
        sym = item.data(Qt.ItemDataRole.UserRole) or item.text().split()[0]
        self._results.setVisible(False)
        self.load_symbol(sym)

    # ── Load a symbol ────────────────────────────────────────────────────────

    def load_symbol(self, symbol: str):
        sym = (symbol or "").strip().upper()
        if not sym:
            return
        self._cur_symbol = sym
        self._search_edit.setText(sym)
        self._hint.setVisible(False)
        self._detail.setVisible(True)

        # Reset header to a loading state
        self._h_symbol.setText(sym)
        self._h_price.setText("…")
        self._h_change.setText("")
        self._h_name.setText("Loading…")
        for v in self._stat_cells.values():
            v.setText("—")
        self._ai_badge.setText("")
        self._ai_text.setText("Generating evaluation…")
        self._ai_meta.setText("")
        self._summary.setText("")

        # Snapshot
        sw = _SnapshotWorker(sym)
        sw.done.connect(self._on_snapshot)
        sw.finished.connect(lambda _w=sw: self._drop_worker(_w))
        self._workers.append(sw)
        sw.start()

        # Chart
        self._load_history(sym, self._cur_period)

    def _load_history(self, symbol: str, period: str):
        key = (symbol, period)
        if key in self._hist_cache:
            self._render_chart(self._hist_cache[key])
            return
        self._chart.load_chart(_loading_html())
        hw = _HistoryWorker(symbol, period)
        hw.done.connect(self._on_history)
        hw.finished.connect(lambda _w=hw: self._drop_worker(_w))
        self._workers.append(hw)
        hw.start()

    def _on_history(self, symbol: str, period: str, df):
        if symbol != self._cur_symbol or period != self._cur_period:
            return
        self._hist_cache[(symbol, period)] = df
        self._render_chart(df)

    def _render_chart(self, df):
        try:
            from core import charts
            html = charts.price_history_chart(
                df, self._cur_symbol, self._cur_period, self._cur_type)
            self._chart.load_chart(html)
        except Exception as e:
            print(f"[find] render chart failed: {e}")

    def _on_snapshot(self, symbol: str, snap: dict):
        if symbol != self._cur_symbol:
            return
        if not snap.get("ok"):
            self._h_name.setText(
                f"Couldn't load data for {symbol}. "
                f"{snap.get('error', '') or 'It may not be available.'}")
            self._h_price.setText("—")
            self._ai_text.setText(
                "No market data available for this symbol, so it can't be "
                "evaluated.")
            self._ai_text.setStyleSheet(
                f"color:{C['muted']};font-size:12px;line-height:1.7;")
            self._ai_badge.setText("")
            return
        self._render_snapshot(snap)

        # Kick off the weekly AI evaluation once we have data to feed it.
        ew = _EvalWorker(symbol, snap, force=False)
        ew.done.connect(self._on_eval)
        ew.finished.connect(lambda _w=ew: self._drop_worker(_w))
        self._workers.append(ew)
        ew.start()

    def _render_snapshot(self, snap: dict):
        price = snap.get("price")
        self._h_price.setText(_money(price))
        self._h_name.setText(
            " · ".join(x for x in [snap.get("name", ""),
                                   snap.get("sector", ""),
                                   snap.get("exchange", "")] if x))
        chg = snap.get("change"); pct = snap.get("change_pct")
        if chg is not None and pct is not None:
            up = chg >= 0
            self._h_change.setText(f"{'+' if up else ''}{chg:,.2f}  ({pct:+.2f}%)")
            self._h_change.setStyleSheet(
                f"font-size:14px;font-weight:700;"
                f"color:{C['green'] if up else C['red']};")
            self._h_price.setStyleSheet(
                f"font-family:'Syne',sans-serif;font-size:24px;font-weight:700;"
                f"color:{C['green'] if up else C['red']};")
        else:
            self._h_change.setText("")

        fmt = {
            "open": _money, "day_high": _money, "day_low": _money,
            "prev_close": _money, "volume": _big, "avg_volume": _big,
            "market_cap": _cap, "pe": _num, "forward_pe": _num, "eps": _money,
            "div_yield": _pct_yield, "beta": _num, "wk_high": _money,
            "wk_low": _money, "ma50": _money, "ma200": _money,
        }
        for key, lbl in self._stat_cells.items():
            lbl.setText(fmt.get(key, _num)(snap.get(key)))

        summ = snap.get("summary", "")
        if summ:
            self._summary.setText(summ[:700] + ("…" if len(summ) > 700 else ""))
        else:
            self._summary.setText("")

    def _on_eval(self, symbol: str, ev: dict):
        if symbol != self._cur_symbol:
            return
        err = ev.get("error")
        if err and not (ev.get("text") or "").strip():
            self._ai_badge.setText("")
            self._ai_text.setText(err)
            self._ai_text.setStyleSheet(
                f"color:{C['muted']};font-size:12px;line-height:1.7;")
            self._ai_meta.setText("")
            return
        rating = ev.get("rating", "—")
        self._ai_badge.setText(rating.upper())
        self._ai_badge.setStyleSheet(
            f"font-size:12px;font-weight:800;color:{_rating_color(rating)};")
        # Strip a leading "RATING: x" line — it's already shown as the badge.
        text = (ev.get("text") or "").strip()
        lines = [ln for ln in text.splitlines()]
        if lines and lines[0].strip().upper().startswith("RATING"):
            lines = lines[1:]
        self._ai_text.setText("\n".join(lines).strip())
        self._ai_text.setStyleSheet(
            f"color:{C['text']};font-size:12px;line-height:1.7;")
        wk = ev.get("week", "")
        cached = "cached" if ev.get("cached") else "new"
        prov = ev.get("provider", "")
        meta = f"week of {wk}"
        if prov:
            meta += f"  ·  {prov}"
        meta += f"  ·  {cached}"
        self._ai_meta.setText(meta)

    def _refresh_eval(self):
        if not self._cur_symbol:
            return
        self._ai_text.setText("Re-evaluating…")
        self._ai_badge.setText("")
        # snap=None → weekly_evaluation re-pulls a fresh snapshot to evaluate.
        ew = _EvalWorker(self._cur_symbol, None, force=True)
        ew.done.connect(self._on_eval)
        ew.finished.connect(lambda _w=ew: self._drop_worker(_w))
        self._workers.append(ew)
        ew.start()

    # ── Controls ─────────────────────────────────────────────────────────────

    def _set_period(self, period: str):
        for p, b in self._period_btns.items():
            b.setChecked(p == period)
        self._cur_period = period
        if self._cur_symbol:
            self._load_history(self._cur_symbol, period)

    def _set_type(self, ctype: str):
        self._candle_btn.setChecked(ctype == "candle")
        self._line_btn.setChecked(ctype == "line")
        self._cur_type = ctype
        key = (self._cur_symbol, self._cur_period)
        if key in self._hist_cache:
            self._render_chart(self._hist_cache[key])

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _drop_worker(self, w):
        if w in self._workers:
            self._workers.remove(w)


# ── Formatting helpers ──────────────────────────────────────────────────────

def _money(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def _num(v):
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "—"


def _big(v):
    if not isinstance(v, (int, float)):
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.0f}"


def _cap(v):
    if not isinstance(v, (int, float)):
        return "—"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.0f}"


def _pct_yield(v):
    if not isinstance(v, (int, float)):
        return "—"
    # yfinance gives dividendYield as a fraction (0.005) on .info but sometimes
    # already as a percent on fast paths — normalise the common fraction case.
    pct = v * 100.0 if v < 1 else v
    return f"{pct:.2f}%"


def _rating_color(rating: str) -> str:
    r = (rating or "").upper()
    if "BUY" in r:
        return C["green"]
    if "SELL" in r:
        return C["red"]
    if "HOLD" in r or "NEUTRAL" in r:
        return C["orange"]
    return C["muted"]


def _loading_html() -> str:
    return (f"<html><body style='background:{C['bg']};margin:0;display:flex;"
            f"align-items:center;justify-content:center;height:100vh;'>"
            f"<span style=\"color:{C['muted']};font-family:'JetBrains Mono';"
            f"font-size:12px;\">Loading chart…</span></body></html>")

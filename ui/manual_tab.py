"""
BAPTOU Trading Platform — Manual Portfolio Tab
─────────────────────────────────────────────────────────────────────
A read-only PORTFOLIO OVERVIEW for the dedicated MANUAL Alpaca account
(keys ALPACA_API_KEY_MANUAL / ALPACA_SECRET_KEY_MANUAL — zero overlap
with any bot account). It shows the account's holdings; click a holding
to see that position's growth (% from cost basis) with markers on the
curve where you bought / bought more / sold some / sold.

Order entry was removed in v4.6.123 — this tab no longer places trades.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
)
from PyQt6.QtGui import QColor

from ui.styles  import COLORS
from ui.widgets import ScrollContent, MetricCard, ChartView

C = COLORS

MANUAL_SIDE = "MANUAL"   # the env-var suffix used for the dedicated manual account


class _FetchWorker(QThread):
    done = pyqtSignal(bool, dict)

    def run(self):
        try:
            client = _manual_client()
            if client is None:
                self.done.emit(False, {"error": "no manual key"})
                return
            a = client.get_account()
            eq = float(getattr(a, "equity", 0) or 0)
            account = {
                "portfolio_value": float(getattr(a, "portfolio_value", eq) or eq),
                "equity":          eq,
                "last_equity":     float(getattr(a, "last_equity", eq) or eq),
                "buying_power":    float(getattr(a, "buying_power", 0) or 0),
                "cash":            float(getattr(a, "cash", 0) or 0),
            }
            positions = client.get_all_positions() or []
            self.done.emit(True, {"positions": positions, "account": account})
        except Exception as e:
            self.done.emit(False, {"error": str(e)})


class _PositionDetailWorker(QThread):
    """Build the per-position growth chart data off the GUI thread: the symbol's
    trade history (markers) + the price history covering the holding period."""
    done = pyqtSignal(str, object, list, float)   # symbol, df, events, ref_price

    def __init__(self, symbol: str, ref_price: float):
        super().__init__()
        self.symbol, self.ref_price = symbol, ref_price

    def run(self):
        df, events = None, []
        try:
            from core import stock_research as SR
            events = SR.position_trades(self.symbol)
            start = events[0]["time"] if events else None
            df = SR.history_since(self.symbol, start)
        except Exception as e:
            print(f"[manual] position detail failed: {e}")
        self.done.emit(self.symbol, df, events, self.ref_price)


def _manual_mode() -> str:
    from core import stock_research as SR
    return SR.manual_mode()


def _has_manual_key() -> bool:
    """True if the MANUAL account has keys for the CURRENT (paper/live) mode."""
    from core import stock_research as SR
    return SR.has_manual_key()


def _manual_client():
    """Mode-aware MANUAL Alpaca client (paper or live) — see
    stock_research.manual_client()."""
    from core import stock_research as SR
    return SR.manual_client()


class ManualTradingTab(QWidget):
    """Read-only portfolio overview for the dedicated MANUAL account."""

    switch_off_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._workers: list = []
        self._positions: list = []
        self._sel_symbol: str = ""
        self._build()

    def on_shown(self):
        self._refresh()

    # ── Build ──────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        # ─── HERO ─────────────────────────────────────────────
        hero = QFrame()
        hero.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:12px;")
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(26, 20, 26, 20)
        hv.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("📁  MY PORTFOLIO")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;"
            f"color:{C['orange']};letter-spacing:3px;")
        top.addWidget(title)
        top.addSpacing(12)
        # LIVE / paper indicator (follows the header Paper/Live toggle)
        self._mode_chip = QLabel("")
        self._mode_chip.setStyleSheet("font-size:11px;font-weight:800;")
        top.addWidget(self._mode_chip)
        top.addStretch()
        off_btn = QPushButton("⚡  Switch to Auto")
        off_btn.setObjectName("toolBtn")
        off_btn.clicked.connect(self.switch_off_requested)
        top.addWidget(off_btn)
        tw = QWidget(); tw.setLayout(top)
        hv.addWidget(tw)

        sep_note = QLabel(
            "An overview of your MANUAL account — completely separate from all AI "
            "bot accounts. Click any holding to see its growth and your trades.")
        sep_note.setStyleSheet(f"color:{C['muted']};font-size:11px;letter-spacing:0.5px;")
        sep_note.setWordWrap(True)
        hv.addWidget(sep_note)
        s.add(hero)

        # ─── NO-KEY WARNING ──────────────────────────────────────
        self._no_key_frame = self._build_no_key_frame()
        s.add(self._no_key_frame)

        # ─── MAIN CONTENT ────────────────────────────────────────
        self._main_frame = QWidget()
        main_layout = QVBoxLayout(self._main_frame)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self._build_main_content(main_layout)
        s.add(self._main_frame)

        s.add_stretch()
        self._refresh_key_state()

    def _build_no_key_frame(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        v = QVBoxLayout(frame)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(10)

        self._nokey_title = QLabel("⚠  No manual account configured")
        self._nokey_title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:14px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        v.addWidget(self._nokey_title)

        self._nokey_desc = QLabel("")
        self._nokey_desc.setStyleSheet(
            f"color:{C['muted']};font-size:11px;line-height:1.7;")
        self._nokey_desc.setWordWrap(True)
        v.addWidget(self._nokey_desc)

        self._mk_key_edit = QLineEdit()
        self._mk_key_edit.setPlaceholderText("Alpaca API Key ID")
        self._mk_sec_edit = QLineEdit()
        self._mk_sec_edit.setPlaceholderText("Alpaca Secret Key")
        self._mk_sec_edit.setEchoMode(QLineEdit.EchoMode.Password)
        for e in (self._mk_key_edit, self._mk_sec_edit):
            e.setStyleSheet(
                f"QLineEdit{{background:{C['bg']};color:{C['text']};border:1px "
                f"solid {C['border']};border-radius:6px;padding:7px 10px;"
                f"font-size:11px;}}")
            v.addWidget(e)
        save_row = QHBoxLayout()
        save_btn = QPushButton("Save manual keys")
        save_btn.setObjectName("addBotBtn")
        save_btn.clicked.connect(self._save_manual_keys_inline)
        save_row.addWidget(save_btn)
        self._mk_msg = QLabel("")
        self._mk_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        save_row.addWidget(self._mk_msg)
        save_row.addStretch()
        srw = QWidget(); srw.setLayout(save_row)
        v.addWidget(srw)

        try:
            from ui.onboarding import InstructionsPanel
            v.addWidget(InstructionsPanel("alpaca"))
        except Exception as _e:
            print(f"[manual] instructions: {_e}")

        return frame

    def _save_manual_keys_inline(self):
        try:
            from core import data as D
            from core import stock_research as SR
            key = self._mk_key_edit.text().strip()
            sec = self._mk_sec_edit.text().strip()
            if not key or not sec:
                self._mk_msg.setText("Enter both key and secret.")
                self._mk_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
                return
            # Save under the CURRENT mode's keyset (live keys are separate from
            # paper keys — different Alpaca accounts).
            kn, sn = SR.manual_key_names()
            D.write_env_keys({kn: key, sn: sec})
            mode = SR.manual_mode()
            self._mk_msg.setText(f"✓ Saved {mode} keys")
            self._mk_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            self._mk_key_edit.clear()
            self._mk_sec_edit.clear()
            self._refresh_key_state()
        except Exception as e:
            self._mk_msg.setText(f"Save failed: {e}")
            self._mk_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")

    def _build_main_content(self, layout: QVBoxLayout):
        # Metrics row
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(10)
        self._m_equity    = MetricCard("PORTFOLIO VALUE", "—", C["text"])
        self._m_daypl     = MetricCard("DAY P/L",         "—", C["green"])
        self._m_positions = MetricCard("OPEN POSITIONS",  "—", C["orange"])
        self._m_bp        = MetricCard("CASH",            "—", C["purple"])
        for card in (self._m_equity, self._m_daypl, self._m_positions, self._m_bp):
            metrics_row.addWidget(card)
        mw = QWidget(); mw.setLayout(metrics_row)
        layout.addWidget(mw)

        layout.addSpacing(16)
        layout.addWidget(self._build_positions_panel())

        layout.addSpacing(16)
        self._detail_frame = self._build_position_detail()
        self._detail_frame.setVisible(False)
        layout.addWidget(self._detail_frame)

    def _build_positions_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        v = QVBoxLayout(frame)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("HOLDINGS")
        title.setStyleSheet(
            f"color:{C['orange']};font-size:10px;letter-spacing:3px;font-weight:800;")
        title_row.addWidget(title)
        hint = QLabel("— click a holding to see its growth")
        hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        title_row.addWidget(hint)
        title_row.addStretch()
        refresh = QPushButton("↺")
        refresh.setObjectName("silenceBtn")
        refresh.setFixedSize(28, 28)
        refresh.clicked.connect(self._refresh)
        title_row.addWidget(refresh)
        trw = QWidget(); trw.setLayout(title_row)
        v.addWidget(trw)

        self._pos_table = QTableWidget(0, 7)
        self._pos_table.setHorizontalHeaderLabels(
            ["SYMBOL", "QTY", "SIDE", "ENTRY", "CURRENT", "P/L", "P/L %"])
        self._pos_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._pos_table.verticalHeader().setVisible(False)
        self._pos_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._pos_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._pos_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self._pos_table.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos_table.setMinimumHeight(240)
        self._pos_table.cellClicked.connect(self._on_position_clicked)
        v.addWidget(self._pos_table)
        return frame

    def _build_position_detail(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        v = QVBoxLayout(frame)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        head = QHBoxLayout()
        self._d_title = QLabel("")
        self._d_title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:16px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        head.addWidget(self._d_title)
        head.addStretch()
        self._d_pl = QLabel("")
        self._d_pl.setStyleSheet("font-size:14px;font-weight:800;")
        head.addWidget(self._d_pl)
        hw = QWidget(); hw.setLayout(head)
        v.addWidget(hw)

        # Marker legend
        legend = QLabel(
            f"<span style='color:{C['green']}'>▲ Bought</span> &nbsp;&nbsp; "
            f"<span style='color:{C['green']}'>◆ Bought more</span> &nbsp;&nbsp; "
            f"<span style='color:{C['orange']}'>▽ Sold some</span> &nbsp;&nbsp; "
            f"<span style='color:{C['red']}'>✕ Sold</span>")
        legend.setStyleSheet("font-size:10px;")
        v.addWidget(legend)

        self._d_chart = ChartView(height=340)
        v.addWidget(self._d_chart)

        self._d_note = QLabel("")
        self._d_note.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        self._d_note.setWordWrap(True)
        v.addWidget(self._d_note)
        return frame

    # ── Key state ──────────────────────────────────────────────────

    def refresh_mode(self):
        """Called when the header Paper/Live toggle flips — re-evaluate which
        account (paper vs live) the portfolio should show."""
        self._sel_symbol = ""
        if hasattr(self, "_detail_frame"):
            self._detail_frame.setVisible(False)
        self._refresh_key_state()

    def _update_mode_ui(self):
        live = _manual_mode() == "live"
        # Hero chip
        if live:
            self._mode_chip.setText("◉ LIVE · REAL MONEY")
            self._mode_chip.setStyleSheet(
                f"font-size:11px;font-weight:800;color:{C['red']};")
        else:
            self._mode_chip.setText("○ paper")
            self._mode_chip.setStyleSheet(
                f"font-size:11px;font-weight:800;color:{C['muted']};")
        # No-key entry copy
        if live:
            self._nokey_title.setText("⚠  No LIVE account configured")
            self._nokey_desc.setText(
                "You're in LIVE mode. Paste your REAL-money Alpaca live API key + "
                "secret below and Save. These are different from your paper keys "
                "and trade real money. Stored only on this machine.")
        else:
            self._nokey_title.setText("⚠  No manual account configured")
            self._nokey_desc.setText(
                "Your portfolio uses its own dedicated Alpaca paper account, kept "
                "separate from your bots. Paste its API key + secret below and "
                "Save to see your holdings.")

    def _refresh_key_state(self):
        self._update_mode_ui()
        has_key = _has_manual_key()
        self._no_key_frame.setVisible(not has_key)
        self._main_frame.setVisible(has_key)
        if has_key:
            self._refresh()

    # ── Data ───────────────────────────────────────────────────────

    def _refresh(self):
        if not _has_manual_key():
            self._refresh_key_state()
            return
        w = _FetchWorker()
        w.done.connect(self._on_data)
        w.finished.connect(
            lambda _w=w: self._workers.remove(_w) if _w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_data(self, ok: bool, data: dict):
        if not ok:
            return
        acct = data.get("account", {}) or {}
        try:
            pv = float(acct.get("portfolio_value", acct.get("equity", 0)) or 0)
            self._m_equity.update_value(f"${pv:,.2f}")
        except Exception:
            pass
        try:
            eq = float(acct.get("equity", 0) or 0)
            le = float(acct.get("last_equity", eq) or eq)
            daypl = eq - le
            dpct  = (daypl / le * 100) if le else 0.0
            self._m_daypl.update_value(
                f"{'+'if daypl>=0 else ''}${daypl:,.2f} ({dpct:+.1f}%)",
                C["green"] if daypl >= 0 else C["red"])
        except Exception:
            pass
        try:
            self._m_bp.update_value(
                f"${float(acct.get('cash', acct.get('buying_power', 0)) or 0):,.2f}")
        except Exception:
            pass

        positions = data.get("positions", [])
        self._positions = positions
        self._m_positions.update_value(str(len(positions)))
        self._pos_table.setRowCount(len(positions))
        for r, pos in enumerate(positions):
            try:
                sym   = str(getattr(pos, "symbol", "?"))
                qty   = str(getattr(pos, "qty", "?"))
                side  = str(getattr(pos, "side", "?")).upper().replace(
                    "POSITIONSIDE.", "")
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
                curr  = float(getattr(pos, "current_price", 0) or 0)
                pl    = float(getattr(pos, "unrealized_pl", 0) or 0)
                plpct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100.0
                cells = [sym, qty, side, f"${entry:,.2f}", f"${curr:,.2f}",
                         f"{'+'if pl>=0 else ''}{pl:,.2f}",
                         f"{plpct:+.2f}%"]
            except Exception:
                cells, pl = ["?"]*7, 0
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col in (5, 6):
                    item.setForeground(QColor(C["green"] if pl >= 0 else C["red"]))
                self._pos_table.setItem(r, col, item)

        # If a position detail is open, keep it in sync with the latest data.
        if self._sel_symbol:
            for r, pos in enumerate(positions):
                if str(getattr(pos, "symbol", "")) == self._sel_symbol:
                    return
            # Position no longer held — hide the stale detail.
            self._detail_frame.setVisible(False)
            self._sel_symbol = ""

    # ── Position detail ─────────────────────────────────────────────

    def _on_position_clicked(self, row: int, _col: int):
        if row < 0 or row >= len(self._positions):
            return
        pos = self._positions[row]
        sym = str(getattr(pos, "symbol", "")).upper()
        if not sym:
            return
        self._sel_symbol = sym
        ref = float(getattr(pos, "avg_entry_price", 0) or 0)
        qty = getattr(pos, "qty", "")
        side = str(getattr(pos, "side", "")).upper().replace("POSITIONSIDE.", "")
        plpct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100.0

        self._detail_frame.setVisible(True)
        self._d_title.setText(f"{sym}   ·   {qty} sh   ·   {side}")
        self._d_pl.setText(f"{plpct:+.2f}%")
        self._d_pl.setStyleSheet(
            f"font-size:14px;font-weight:800;"
            f"color:{C['green'] if plpct >= 0 else C['red']};")
        self._d_note.setText("Loading trade history…")
        self._d_chart.load_chart(_loading_html())

        w = _PositionDetailWorker(sym, ref)
        w.done.connect(self._on_position_detail)
        w.finished.connect(
            lambda _w=w: self._workers.remove(_w) if _w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_position_detail(self, symbol: str, df, events: list, ref_price: float):
        if symbol != self._sel_symbol:
            return
        try:
            from core import charts
            html = charts.position_growth_chart(df, events, ref_price, symbol)
            self._d_chart.load_chart(html)
        except Exception as e:
            print(f"[manual] render position chart failed: {e}")
        if events:
            n_buy = sum(1 for e in events if e["kind"] in ("buy", "add"))
            n_sell = sum(1 for e in events if e["kind"] in ("sell_some", "sell_all"))
            self._d_note.setText(
                f"{len(events)} trades on this position "
                f"({n_buy} buys, {n_sell} sells). Growth is measured from your "
                f"average cost basis.")
        else:
            self._d_note.setText(
                "No recorded trades on the manual account for this symbol — the "
                "curve shows the stock's price growth from your cost basis.")


def _loading_html() -> str:
    return (f"<html><body style='background:{C['bg']};margin:0;display:flex;"
            f"align-items:center;justify-content:center;height:100vh;'>"
            f"<span style=\"color:{C['muted']};font-family:'JetBrains Mono';"
            f"font-size:12px;\">Loading…</span></body></html>")

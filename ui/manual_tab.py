"""
BAPTOU Trading Platform — Manual Trading Tab
─────────────────────────────────────────────────────────────────────
Fully independent trading interface — uses a DEDICATED Alpaca account
whose keys are stored under ALPACA_API_KEY_MANUAL / ALPACA_SECRET_KEY_MANUAL.
This account has ZERO overlap with any bot account (LONG/SHORT/DAY).
The toggle in the header activates this tab; bots keep running untouched.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QGridLayout, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QSizePolicy,
)

from ui.styles  import COLORS
from ui.widgets import ScrollContent, SectionHeader, MetricCard, NoScrollComboBox

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


class _OrderWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, symbol: str, qty: float,
                 direction: str, order_type: str, limit_price: float | None):
        super().__init__()
        self.symbol, self.qty          = symbol, qty
        self.direction, self.order_type = direction, order_type
        self.limit_price               = limit_price

    def run(self):
        try:
            client = _manual_client()
            if client is None:
                self.done.emit(False, "No manual account configured.")
                return
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
            from alpaca.trading.enums    import OrderSide, TimeInForce
            side_enum = OrderSide.BUY if self.direction == "BUY" else OrderSide.SELL
            if self.order_type == "MARKET":
                req = MarketOrderRequest(
                    symbol=self.symbol.upper(),
                    qty=self.qty,
                    side=side_enum,
                    time_in_force=TimeInForce.DAY,
                )
            else:
                req = LimitOrderRequest(
                    symbol=self.symbol.upper(),
                    qty=self.qty,
                    side=side_enum,
                    time_in_force=TimeInForce.GTC,
                    limit_price=self.limit_price,
                )
            order = client.submit_order(req)
            self.done.emit(True, f"Order placed: {order.id}")
        except Exception as e:
            self.done.emit(False, str(e))


def _has_manual_key() -> bool:
    try:
        from core import data as D
        keys = D.read_env_keys()
        return bool(keys.get("ALPACA_API_KEY_MANUAL", "").strip()
                    and keys.get("ALPACA_SECRET_KEY_MANUAL", "").strip())
    except Exception:
        return False


def _manual_client():
    """V4.6.94 — build the dedicated MANUAL Alpaca client DIRECTLY from its
    keys, independent of the app's active broker. Manual trading always runs
    on its own Alpaca paper account, so it must keep working even while the
    rest of the app is set to IBKR (where D.get_client returns None)."""
    try:
        from core import data as D
        keys = D.read_env_keys()
        key = keys.get("ALPACA_API_KEY_MANUAL", "").strip()
        sec = keys.get("ALPACA_SECRET_KEY_MANUAL", "").strip()
        if not key or not sec:
            return None
        from alpaca.trading.client import TradingClient
        return TradingClient(key, sec, paper=True)
    except Exception as e:
        print(f"[manual] client build failed: {e}")
        return None


class ManualTradingTab(QWidget):
    """Manual trading on a DEDICATED account — never touches bot accounts."""

    switch_off_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._workers: list = []
        self._build()

    def on_shown(self):
        """Called by main window every time this tab is activated."""
        self._refresh()

    # ── Build ──────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        # ─── HERO ─────────────────────────────────────────────
        hero = QFrame()
        hero.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:12px;border-left:4px solid {C['orange']};")
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(26, 20, 26, 20)
        hv.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("✋  MANUAL TRADING")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;"
            f"color:{C['orange']};letter-spacing:3px;")
        top.addWidget(title)
        top.addStretch()
        off_btn = QPushButton("⚡  Switch to Auto")
        off_btn.setObjectName("toolBtn")
        off_btn.clicked.connect(self.switch_off_requested)
        top.addWidget(off_btn)
        tw = QWidget(); tw.setLayout(top)
        hv.addWidget(tw)

        sep_note = QLabel(
            "Uses your MANUAL account only — completely separate from all AI bot accounts.\n"
            "Bots keep running normally on their own accounts while you trade here.")
        sep_note.setStyleSheet(
            f"color:{C['muted']};font-size:11px;letter-spacing:0.5px;")
        sep_note.setWordWrap(True)
        hv.addWidget(sep_note)
        s.add(hero)

        # ─── NO-KEY WARNING (shown when MANUAL key not configured) ──
        self._no_key_frame = self._build_no_key_frame()
        s.add(self._no_key_frame)

        # ─── MAIN CONTENT (hidden until key is configured) ──────
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
            f"background:{C['panel']};border:none;border-radius:10px;"
            f"border-left:3px solid {C['red']};")
        v = QVBoxLayout(frame)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(10)

        title = QLabel("⚠  No manual trading account configured")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:14px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        v.addWidget(title)

        desc = QLabel(
            "Manual trading uses its own dedicated Alpaca paper account, kept "
            "completely separate from your bots. Create a separate Alpaca "
            "account, then paste its API key + secret below and Save.")
        desc.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.7;")
        desc.setWordWrap(True)
        v.addWidget(desc)

        # V4.6.94 — inline key entry so manual mode is self-contained (the Tools
        # tab is hidden in manual mode).
        from ui.widgets import NoScrollComboBox  # noqa: F401 (kept for parity)
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

        # Collapsible "how to create & link an Alpaca account" guide.
        try:
            from ui.onboarding import InstructionsPanel
            v.addWidget(InstructionsPanel("alpaca"))
        except Exception as _e:
            print(f"[manual] instructions: {_e}")

        why = QLabel(
            "Why separate?  Bot accounts have active orders managed by algorithms. "
            "If you traded on the same account, your manual order could fill "
            "before a bot's stop-loss — or the bot could sell a position you just "
            "bought.  Keep them apart to avoid conflicts.")
        why.setStyleSheet(f"color:{C['muted']};font-size:10px;line-height:1.6;")
        why.setWordWrap(True)
        v.addWidget(why)

        return frame

    def _save_manual_keys_inline(self):
        try:
            from core import data as D
            key = self._mk_key_edit.text().strip()
            sec = self._mk_sec_edit.text().strip()
            if not key or not sec:
                self._mk_msg.setText("Enter both key and secret.")
                self._mk_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
                return
            D.write_env_keys({
                "ALPACA_API_KEY_MANUAL":    key,
                "ALPACA_SECRET_KEY_MANUAL": sec,
            })
            self._mk_msg.setText("✓ Saved")
            self._mk_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
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
        self._m_bp        = MetricCard("BUYING POWER",    "—", C["purple"])
        for card in (self._m_equity, self._m_daypl, self._m_positions, self._m_bp):
            metrics_row.addWidget(card)
        mw = QWidget(); mw.setLayout(metrics_row)
        layout.addWidget(mw)

        # Divider spacing
        layout.addSpacing(16)

        # Order entry + positions side by side
        split = QHBoxLayout()
        split.setSpacing(16)
        split.addWidget(self._build_order_form(), 1)
        split.addWidget(self._build_positions_panel(), 2)
        sw = QWidget(); sw.setLayout(split)
        layout.addWidget(sw)

        # Friend challenge
        layout.addSpacing(16)
        layout.addWidget(self._build_challenge_frame())

    def _build_order_form(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        g = QGridLayout(frame)
        g.setContentsMargins(18, 16, 18, 16)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        def lbl(text: str) -> QLabel:
            w = QLabel(text)
            w.setStyleSheet(
                f"color:{C['muted']};font-size:9px;letter-spacing:2px;font-weight:700;")
            return w

        title = QLabel("ORDER ENTRY")
        title.setStyleSheet(
            f"color:{C['orange']};font-size:10px;letter-spacing:3px;font-weight:800;")
        g.addWidget(title, 0, 0, 1, 2)

        g.addWidget(lbl("SYMBOL"), 1, 0)
        self._sym_edit = QLineEdit()
        self._sym_edit.setPlaceholderText("e.g. AAPL")
        g.addWidget(self._sym_edit, 1, 1)

        g.addWidget(lbl("SIDE"), 2, 0)
        self._dir_combo = NoScrollComboBox()
        self._dir_combo.addItems(["BUY", "SELL"])
        g.addWidget(self._dir_combo, 2, 1)

        g.addWidget(lbl("QUANTITY"), 3, 0)
        self._qty_edit = QLineEdit()
        self._qty_edit.setPlaceholderText("shares")
        g.addWidget(self._qty_edit, 3, 1)

        g.addWidget(lbl("ORDER TYPE"), 4, 0)
        self._type_combo = NoScrollComboBox()
        self._type_combo.addItems(["MARKET", "LIMIT"])
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        g.addWidget(self._type_combo, 4, 1)

        self._lp_lbl = lbl("LIMIT PRICE")
        g.addWidget(self._lp_lbl, 5, 0)
        self._lp_edit = QLineEdit()
        self._lp_edit.setPlaceholderText("$ per share")
        self._lp_edit.setEnabled(False)
        g.addWidget(self._lp_edit, 5, 1)

        submit = QPushButton("⬆  Submit Order")
        submit.setObjectName("addBotBtn")
        submit.clicked.connect(self._submit_order)
        g.addWidget(submit, 6, 0, 1, 2)

        self._order_msg = QLabel("")
        self._order_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        self._order_msg.setWordWrap(True)
        g.addWidget(self._order_msg, 7, 0, 1, 2)

        return frame

    def _build_positions_panel(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;")
        v = QVBoxLayout(frame)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("OPEN POSITIONS")
        title.setStyleSheet(
            f"color:{C['orange']};font-size:10px;letter-spacing:3px;font-weight:800;")
        title_row.addWidget(title)
        title_row.addStretch()
        refresh = QPushButton("↺")
        refresh.setObjectName("silenceBtn")
        refresh.setFixedSize(28, 28)
        refresh.clicked.connect(self._refresh)
        title_row.addWidget(refresh)
        trw = QWidget(); trw.setLayout(title_row)
        v.addWidget(trw)

        self._pos_table = QTableWidget(0, 6)
        self._pos_table.setHorizontalHeaderLabels(
            ["SYMBOL", "QTY", "SIDE", "ENTRY", "CURRENT", "P/L"])
        self._pos_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._pos_table.verticalHeader().setVisible(False)
        self._pos_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._pos_table.setMinimumHeight(220)
        v.addWidget(self._pos_table)
        return frame

    def _build_challenge_frame(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;"
            f"border-left:3px solid {C['purple']};")
        v = QVBoxLayout(frame)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        title = QLabel("⚔  CHALLENGE FRIENDS")
        title.setStyleSheet(
            f"color:{C['purple']};font-size:10px;letter-spacing:3px;font-weight:800;")
        v.addWidget(title)

        info = QLabel(
            "Challenge a friend to a trading duel — both of you trade manually on "
            "your separate MANUAL accounts and the one with the best return over the "
            "chosen period wins. No bots allowed in a duel. Rankings will show in "
            "the Friends tab.")
        info.setStyleSheet(f"color:{C['text']};font-size:11px;line-height:1.6;")
        info.setWordWrap(True)
        v.addWidget(info)

        row = QHBoxLayout()
        btn = QPushButton("⚔  Challenge a Friend")
        btn.setObjectName("toolBtn")
        btn.clicked.connect(self._challenge_friend)
        row.addWidget(btn)
        row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        v.addWidget(rw)
        return frame

    # ── Key state ──────────────────────────────────────────────────

    def _refresh_key_state(self):
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
            # V4.6.94 — the account dict has equity + last_equity, not Alpaca's
            # raw unrealized_intraday_pl; derive the day P/L from them.
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
                f"${float(acct.get('buying_power', acct.get('cash', 0)) or 0):,.2f}")
        except Exception:
            pass

        positions = data.get("positions", [])
        self._m_positions.update_value(str(len(positions)))
        self._pos_table.setRowCount(len(positions))
        for r, pos in enumerate(positions):
            try:
                sym   = str(getattr(pos, "symbol", "?"))
                qty   = str(getattr(pos, "qty", "?"))
                side  = str(getattr(pos, "side", "?")).upper()
                entry = f"${float(getattr(pos, 'avg_entry_price', 0) or 0):,.2f}"
                curr  = f"${float(getattr(pos, 'current_price', 0) or 0):,.2f}"
                pl    = float(getattr(pos, "unrealized_pl", 0) or 0)
                pl_s  = f"{'+'if pl>=0 else ''}{pl:,.2f}"
            except Exception:
                sym, qty, side, entry, curr, pl_s, pl = "?","?","?","?","?","?",0
            for col, val in enumerate([sym, qty, side, entry, curr, pl_s]):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 5:
                    from PyQt6.QtGui import QColor
                    item.setForeground(
                        QColor(C["green"] if pl >= 0 else C["red"]))
                self._pos_table.setItem(r, col, item)

    # ── Order ──────────────────────────────────────────────────────

    def _on_type_changed(self, otype: str):
        is_limit = otype == "LIMIT"
        self._lp_edit.setEnabled(is_limit)
        self._lp_lbl.setStyleSheet(
            f"color:{C['text'] if is_limit else C['muted']};"
            f"font-size:9px;letter-spacing:2px;font-weight:700;")

    def _submit_order(self):
        sym = self._sym_edit.text().strip().upper()
        if not sym:
            self._show_order_msg("Enter a symbol.", ok=False)
            return
        try:
            qty = float(self._qty_edit.text().strip())
            assert qty > 0
        except Exception:
            self._show_order_msg("Enter a valid quantity (> 0).", ok=False)
            return
        otype       = self._type_combo.currentText()
        limit_price = None
        if otype == "LIMIT":
            try:
                limit_price = float(self._lp_edit.text().strip())
                assert limit_price > 0
            except Exception:
                self._show_order_msg("Enter a valid limit price.", ok=False)
                return
        direction = self._dir_combo.currentText()
        self._show_order_msg("Submitting…", ok=None)
        w = _OrderWorker(sym, qty, direction, otype, limit_price)
        w.done.connect(self._on_order_done)
        w.finished.connect(
            lambda _w=w: self._workers.remove(_w) if _w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_order_done(self, ok: bool, msg: str):
        self._show_order_msg(f"{'✓' if ok else '✗'}  {msg}", ok=ok)
        if ok:
            self._sym_edit.clear()
            self._qty_edit.clear()
            self._lp_edit.clear()
            QTimer.singleShot(1500, self._refresh)
        QTimer.singleShot(7000, lambda: self._show_order_msg(""))

    def _show_order_msg(self, text: str, ok=True):
        color = C["green"] if ok is True else C["red"] if ok is False else C["muted"]
        self._order_msg.setText(text)
        self._order_msg.setStyleSheet(f"color:{color};font-size:10px;")

    def _challenge_friend(self):
        QMessageBox.information(
            self, "Challenge Friends",
            "Friend challenges are coming soon!\n\n"
            "You'll be able to challenge any friend on your friends list to a "
            "manual trading duel — same starting capital, no bots, best return "
            "over the agreed period wins. Rankings will appear in FRIENDS tab.")

"""
APEX Overview Tab — all three bots at a glance
APEX Tools Tab — broker conversion, cloud access, costs
"""

import os
import socket
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QFileDialog, QSizePolicy, QTextEdit, QComboBox,
    QLineEdit, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer

from ui.styles  import COLORS, BOT_COLOR
from ui.widgets import (
    ChartView, MetricCard, SectionHeader, ScrollContent, DataTable,
)
from core import data as D
from core import charts as CH
from core.worker import DataWorker

C = COLORS


# ─────────────────────────────────────────
# OVERVIEW TAB
# ─────────────────────────────────────────

class OverviewTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build()

    def _build(self):
        s = self.scroll

        # ── Three account blocks side by side ──
        s.add(SectionHeader("ALL ACCOUNTS", C["text"]))
        row = QHBoxLayout()
        self.blocks = {}
        for side in ["LONG","SHORT","DAY"]:
            block = self._account_block(side)
            self.blocks[side] = block
            row.addWidget(block)
        rw = QWidget()
        rw.setLayout(row)
        s.add(rw)

        # ── Live total-portfolio chart (works 24/7) ──
        s.add(SectionHeader("PORTFOLIO VALUE  —  LIVE (updates even when market closed)",
                            C["text"], controls=self._period_combo()))
        self.combined_chart = ChartView(height=300)
        self.combined_chart.load_chart(CH.empty_chart("Loading…", height=300))
        s.add(self.combined_chart)

        # ── Total API cost summary ──
        s.add(SectionHeader("TOTAL RUNNING COSTS", C["yellow"]))
        cost_grid = QGridLayout()
        cost_grid.setSpacing(8)
        self.cost_cards = {}
        cost_metrics = [
            ("PER DAY",    C["muted"]),
            ("PER MONTH",  C["yellow"]),
            ("PER YEAR",   C["orange"]),
            ("GRAND TOTAL",C["text"]),
        ]
        for i, (label, color) in enumerate(cost_metrics):
            card = MetricCard(label, "—", color)
            cost_grid.addWidget(card, 0, i)
            self.cost_cards[label] = card

        # Per-bot cost row
        for j, side in enumerate(["LONG","SHORT","DAY"]):
            label = f"{side} TOTAL"
            card  = MetricCard(label, "—", BOT_COLOR[side])
            cost_grid.addWidget(card, 1, j)
            self.cost_cards[label] = card

        cw = QWidget()
        cw.setLayout(cost_grid)
        s.add(cw)

        s.add_stretch()

    def _account_block(self, side: str) -> QFrame:
        color = BOT_COLOR[side]
        block = QFrame()
        block.setStyleSheet(
            f"background:{C['panel']};border:1px solid {color}40;"
            f"border-radius:10px;border-left:3px solid {color};"
        )
        layout = QVBoxLayout(block)
        layout.setContentsMargins(14,12,14,12)
        layout.setSpacing(6)

        labels = {
            "LONG":  "▲ LONG BOT",
            "SHORT": "▼ SHORT BOT",
            "DAY":   "◆ DAY BOT",
        }
        title = QLabel(labels[side])
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:11px;font-weight:800;"
            f"letter-spacing:3px;color:{color};"
        )
        layout.addWidget(title)

        cards_layout = QGridLayout()
        cards_layout.setSpacing(6)

        card_defs = [
            ("PORTFOLIO",  "—", color),
            ("DAY P/L",    "—", C["text"]),
            ("UNREALIZED", "—", C["text"]),
            ("POSITIONS",  "—", C["text"]),
        ]
        block._cards = {}
        for i, (lbl, val, c) in enumerate(card_defs):
            card = MetricCard(lbl, val, c)
            card.setFixedHeight(70)
            cards_layout.addWidget(card, i//2, i%2)
            block._cards[lbl] = card

        layout.addLayout(cards_layout)
        block.side = side
        return block

    def _period_combo(self) -> QComboBox:
        self.period_combo = QComboBox()
        self.period_combo.addItems(["1D","1W","1M","3M","6M","1Y"])
        self.period_combo.setFixedWidth(80)
        self.period_combo.currentTextChanged.connect(
            lambda _: self._reload_combined())
        return self.period_combo

    def _reload_combined(self):
        """Fetch the live total-portfolio history off the UI thread."""
        if getattr(self, "_hist_worker", None) and self._hist_worker.isRunning():
            return
        period = self.period_combo.currentText()
        self._hist_worker = DataWorker(D.get_combined_history, period)
        self._hist_worker.result_ready.connect(self._on_combined)
        self._hist_worker.error.connect(
            lambda e: print(f"[overview history] {e}"))
        self._hist_worker.start()

    def _on_combined(self, df):
        period = self.period_combo.currentText()
        self.combined_chart.load_chart(CH.combined_history_chart(df, period))

    def refresh(self):
        for side, block in self.blocks.items():
            self._refresh_block(side, block)

        # Live total-portfolio chart (background fetch, 24/7)
        self._reload_combined()

        # Costs
        costs = D.estimate_total_costs()
        self.cost_cards["PER DAY"].update_value(
            f"${costs['per_day']:.4f}", C["muted"])
        self.cost_cards["PER MONTH"].update_value(
            f"${costs['per_month']:.2f}", C["yellow"])
        self.cost_cards["PER YEAR"].update_value(
            f"${costs['per_year']:.2f}", C["orange"])
        self.cost_cards["GRAND TOTAL"].update_value(
            f"${costs['grand_year']:.2f}/yr", C["text"])
        for side in ["LONG","SHORT","DAY"]:
            bc = costs["by_bot"].get(side,{})
            self.cost_cards[f"{side} TOTAL"].update_value(
                f"${bc.get('total',0):.4f}", BOT_COLOR[side])

    def _refresh_block(self, side, block):
        a   = D.get_account(side)
        pos = D.get_positions(side)
        pv  = a.get("portfolio_value",0)
        eq  = a.get("equity",0)
        le  = a.get("last_equity",eq)
        dp  = eq-le
        unr = sum(float(p.get("unrealized_pl",0)) for p in pos)
        arrow = "▲" if dp>=0 else "▼"
        dc    = C["green"] if dp>=0 else C["red"]
        uc    = C["green"] if unr>=0 else C["red"]
        block._cards["PORTFOLIO"].update_value(f"${pv:,.2f}", BOT_COLOR[side])
        block._cards["DAY P/L"].update_value(
            f"{arrow} ${abs(dp):,.2f}", dc)
        block._cards["UNREALIZED"].update_value(f"${unr:+,.2f}", uc)
        block._cards["POSITIONS"].update_value(str(len(pos)))


# ─────────────────────────────────────────
# TOOLS TAB
# ─────────────────────────────────────────

class ToolsTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build()

    def _build(self):
        s = self.scroll

        # ── API KEYS ─────────────────────────────────────
        s.add(SectionHeader("API KEYS", C["green"]))
        keys_info = QLabel(
            "Enter your Alpaca paper keys (one API/secret pair per bot) and "
            "your Anthropic (Claude) key. Stored only on this PC's data "
            "folder; applied immediately — no restart needed.")
        keys_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        keys_info.setWordWrap(True)
        s.add(keys_info)

        cur = D.read_env_keys()
        self._key_edits = {}
        form = QFrame()
        form.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        fl = QGridLayout(form)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.setSpacing(8)
        fields = [
            ("ANTHROPIC_API_KEY",      "Claude (Anthropic) API key"),
            ("ALPACA_API_KEY_LONG",    "Alpaca API key  —  LONG"),
            ("ALPACA_SECRET_KEY_LONG", "Alpaca secret    —  LONG"),
            ("ALPACA_API_KEY_SHORT",   "Alpaca API key  —  SHORT"),
            ("ALPACA_SECRET_KEY_SHORT","Alpaca secret    —  SHORT"),
            ("ALPACA_API_KEY_DAY",     "Alpaca API key  —  DAY"),
            ("ALPACA_SECRET_KEY_DAY",  "Alpaca secret    —  DAY"),
        ]
        for i, (k, label) in enumerate(fields):
            lb = QLabel(label)
            lb.setStyleSheet(f"color:{C['text']};font-size:11px;")
            ed = QLineEdit(cur.get(k, ""))
            ed.setEchoMode(QLineEdit.EchoMode.Password)
            ed.setStyleSheet(
                f"background:{C['panel2']};color:{C['text']};"
                f"border:1px solid {C['border']};border-radius:4px;"
                f"padding:5px;font-family:'JetBrains Mono';font-size:11px;")
            fl.addWidget(lb, i, 0)
            fl.addWidget(ed, i, 1)
            self._key_edits[k] = ed

        show = QCheckBox("Show keys")
        show.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        show.toggled.connect(lambda on: [
            e.setEchoMode(QLineEdit.EchoMode.Normal if on
                          else QLineEdit.EchoMode.Password)
            for e in self._key_edits.values()])
        save_btn = QPushButton("Save keys")
        save_btn.setObjectName("toolBtn")
        save_btn.clicked.connect(self._save_keys)
        self.keys_msg = QLabel("")
        self.keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        fl.addWidget(show, len(fields), 0)
        brow = QHBoxLayout()
        brow.addWidget(save_btn)
        brow.addWidget(self.keys_msg)
        brow.addStretch()
        bwrap = QWidget()
        bwrap.setLayout(brow)
        fl.addWidget(bwrap, len(fields) + 1, 0, 1, 2)
        s.add(form)

        # ── AUTOMATION ───────────────────────────────────
        s.add(SectionHeader("AUTOMATION", C["green"]))
        self.auto_chk = QCheckBox(
            "Auto-trade on US market schedule  (start all bots at the "
            "open, stop them at the close)")
        self.auto_chk.setStyleSheet(f"color:{C['text']};font-size:11px;")
        try:
            self.auto_chk.setChecked(D.get_auto_schedule())
        except Exception:
            pass
        self.auto_chk.toggled.connect(self._toggle_auto)
        s.add(self.auto_chk)
        auto_note = QLabel(
            "When on, bots launch automatically at the US open and stop at "
            "the close — you don't need to press RUN. The installed app also "
            "self-updates only overnight (after close, before open); running "
            "from run.bat never auto-updates.")
        auto_note.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        auto_note.setWordWrap(True)
        s.add(auto_note)

        # ── BROKER CONVERSION ────────────────────────────
        s.add(SectionHeader("BROKER CONVERSION — ALPACA → IBKR", C["purple"]))

        conv_info = QLabel(
            "Export your current positions as IBKR-compatible files.\n"
            "CSV: import via IBKR's Order Import tool.\n"
            "Python script: run with ib_insync after connecting TWS or IB Gateway."
        )
        conv_info.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.8;")
        s.add(conv_info)

        for side in ["LONG","SHORT","DAY"]:
            color = BOT_COLOR[side]
            row   = QHBoxLayout()
            lbl   = QLabel(f"{side} BOT →")
            lbl.setStyleSheet(
                f"color:{color};font-weight:600;font-size:11px;"
                f"letter-spacing:1px;min-width:90px;"
            )
            csv_btn = QPushButton("⬇  Export CSV")
            csv_btn.setObjectName("toolBtn")
            csv_btn.clicked.connect(lambda _, s2=side: self._export_csv(s2))
            py_btn  = QPushButton("⬇  Export Python (.py)")
            py_btn.setObjectName("toolBtn")
            py_btn.clicked.connect(lambda _, s2=side: self._export_py(s2))

            self_lbl = QLabel("")
            self_lbl.setStyleSheet(f"color:{C['green']};font-size:10px;")
            setattr(self, f"{side.lower()}_export_msg", self_lbl)

            row.addWidget(lbl)
            row.addWidget(csv_btn)
            row.addWidget(py_btn)
            row.addWidget(self_lbl)
            row.addStretch()
            rw = QWidget()
            rw.setLayout(row)
            s.add(rw)

        # ── CLOUD ACCESS ─────────────────────────────────
        s.add(SectionHeader("CLOUD ACCESS", C["muted"]))

        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "localhost"

        cloud_frame = QFrame()
        cloud_frame.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;"
        )
        cloud_layout = QGridLayout(cloud_frame)
        cloud_layout.setContentsMargins(16,14,16,14)
        cloud_layout.setSpacing(16)

        # Local network
        local_title = QLabel("LOCAL NETWORK")
        local_title.setStyleSheet(f"font-size:8px;color:{C['muted']};letter-spacing:3px;")
        local_url = QLabel(f"http://{ip}:8050")
        local_url.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:13px;"
            f"color:{C['green']};font-weight:600;"
        )
        local_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        local_desc = QLabel("Open this URL on any device connected to your WiFi.")
        local_desc.setStyleSheet(f"font-size:10px;color:{C['muted']};")

        # ngrok
        ngrok_title = QLabel("INTERNET ACCESS (ANYWHERE)")
        ngrok_title.setStyleSheet(f"font-size:8px;color:{C['muted']};letter-spacing:3px;")
        ngrok_steps = QLabel(
            "1.  pip install pyngrok\n"
            "2.  Open a new terminal\n"
            "3.  ngrok http 8050\n"
            "4.  Use the URL ngrok gives you from any device, anywhere"
        )
        ngrok_steps.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:11px;"
            f"color:{C['text']};line-height:2;"
        )

        cloud_layout.addWidget(local_title, 0, 0)
        cloud_layout.addWidget(local_url,   1, 0)
        cloud_layout.addWidget(local_desc,  2, 0)
        cloud_layout.addWidget(ngrok_title, 0, 1)
        cloud_layout.addWidget(ngrok_steps, 1, 1, 2, 1)
        s.add(cloud_frame)

        # ── APP UPDATES ──────────────────────────────────
        s.add(SectionHeader("APP UPDATES", C["purple"]))

        update_frame = QFrame()
        update_frame.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;"
        )
        update_layout = QVBoxLayout(update_frame)
        update_layout.setContentsMargins(16,14,16,14)
        update_layout.setSpacing(8)

        update_info = QLabel(
            "APEX checks for updates automatically on startup.\n"
            "To set up auto-updates from your own GitHub repository:\n\n"
            "  1. Create a GitHub repo with your project files\n"
            "  2. Edit core/updater.py → set GITHUB_REPO = 'yourname/your-repo'\n"
            "  3. Add version.json to your repo: {\"version\": \"1.0.0\", \"notes\": \"...\"}\n"
            "  4. Push updates to GitHub → app will detect them on next launch\n\n"
            "Your .env, state files, and universe.txt files are NEVER overwritten by updates."
        )
        update_info.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:11px;"
            f"color:{C['text']};line-height:1.8;"
        )
        update_info.setWordWrap(True)

        check_btn = QPushButton("⟳  Check for Updates Now")
        check_btn.setObjectName("toolBtn")
        check_btn.clicked.connect(self._check_update_now)
        self.update_msg = QLabel("")
        self.update_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")

        update_layout.addWidget(update_info)
        btn_row = QHBoxLayout()
        btn_row.addWidget(check_btn)
        btn_row.addWidget(self.update_msg)
        btn_row.addStretch()
        update_layout.addLayout(btn_row)
        s.add(update_frame)

        # ── BUILD .EXE ───────────────────────────────────
        s.add(SectionHeader("PACKAGE AS .EXE", C["muted"]))
        exe_info = QLabel(
            "To distribute APEX as a standalone Windows application:\n\n"
            "  pip install pyinstaller\n"
            "  cd apex_app\n"
            "  pyinstaller --onefile --windowed --name APEX main.py\n\n"
            "The .exe will be in the dist/ folder. Double-click to run — no Python needed."
        )
        exe_info.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:11px;"
            f"color:{C['muted']};line-height:1.8;"
        )
        exe_info.setWordWrap(True)
        s.add(exe_info)

        s.add_stretch()

    def refresh(self):
        pass  # Tools tab is mostly static

    def _toggle_auto(self, on: bool):
        try:
            D.set_auto_schedule(bool(on))
        except Exception:
            pass

    def _save_keys(self):
        vals = {k: e.text().strip() for k, e in self._key_edits.items()}
        try:
            D.write_env_keys(vals)
            self.keys_msg.setText("Saved ✓  keys applied & reconnected")
            self.keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        except Exception as e:
            self.keys_msg.setText(f"Error: {e}")
            self.keys_msg.setStyleSheet(f"color:{C['red']};font-size:11px;")
        QTimer.singleShot(5000, lambda: self.keys_msg.setText(""))

    def _export_csv(self, side: str):
        content = D.export_ibkr_csv(side)
        if not content:
            getattr(self, f"{side.lower()}_export_msg").setText("No positions to export")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save {side} IBKR CSV", f"{side.lower()}_to_ibkr.csv",
            "CSV Files (*.csv)")
        if path:
            with open(path, "w") as f:
                f.write(content)
            getattr(self, f"{side.lower()}_export_msg").setText(f"✓ Saved")
            QTimer.singleShot(3000,
                lambda: getattr(self, f"{side.lower()}_export_msg").setText(""))

    def _export_py(self, side: str):
        content = D.export_ibkr_script(side)
        if not content:
            getattr(self, f"{side.lower()}_export_msg").setText("No positions to export")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save {side} IBKR Script", f"{side.lower()}_to_ibkr.py",
            "Python Files (*.py)")
        if path:
            with open(path, "w") as f:
                f.write(content)
            getattr(self, f"{side.lower()}_export_msg").setText(f"✓ Saved")
            QTimer.singleShot(3000,
                lambda: getattr(self, f"{side.lower()}_export_msg").setText(""))

    def _check_update_now(self):
        from core.updater import check_for_update, get_current_version
        self.update_msg.setText("Checking...")
        info = check_for_update()
        if info:
            self.update_msg.setText(
                f"v{info['latest']} available! "
                f"(current: v{info['current']})"
            )
            self.update_msg.setStyleSheet(f"color:{C['purple']};font-size:11px;")
        else:
            self.update_msg.setText(
                f"Up to date — v{get_current_version()}")
            self.update_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        QTimer.singleShot(8000, lambda: self.update_msg.setText(""))

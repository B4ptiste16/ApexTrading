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

    # V7.1.4: how the Overview blocks can be ordered.
    # (key, human label). The key is what we persist in settings.
    # "default" → registry order (tab-bar order); everything else is a
    # descending sort on the named metric.
    _SORT_OPTIONS = [
        ("default",     "Order: default"),
        ("portfolio",   "Order: portfolio $"),
        ("lifetime_pl", "Order: total profit $"),
        ("day_pl",      "Order: day P/L $"),
        ("day_pct",     "Order: day P/L %"),
        ("win_rate",    "Order: win rate %"),
        ("positions",   "Order: # positions"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build()

    def _build(self):
        s = self.scroll

        # ── Account blocks for ONLY active (non-silenced) bots ──
        # V7.1.4: Sort dropdown drives the block order in the row.
        # V7.1.6: Period dropdown drives the timeframe used by every
        # block's PERIOD P/L card. Both choices persist.
        self._sort_combo = QComboBox()
        for key, label in self._SORT_OPTIONS:
            self._sort_combo.addItem(label, key)
        try:
            saved = D.load_settings().get("overview_sort", "default")
            idx = next((i for i, (k, _) in enumerate(self._SORT_OPTIONS)
                        if k == saved), 0)
            self._sort_combo.setCurrentIndex(idx)
        except Exception:
            pass
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        self._period_combo = QComboBox()
        self._period_combo.addItems(["1D", "1W", "1M", "3M", "6M", "1Y"])
        self._period_combo.setFixedWidth(56)
        try:
            saved_period = D.load_settings().get("overview_period", "1D")
            self._period_combo.setCurrentText(saved_period)
        except Exception:
            pass
        self._period_combo.currentTextChanged.connect(self._on_period_changed)

        # Wrap both controls in a small row so the section header can
        # show them side-by-side.
        controls_w = QWidget()
        cl = QHBoxLayout(controls_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        period_lbl = QLabel("Period:")
        period_lbl.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        cl.addWidget(period_lbl)
        cl.addWidget(self._period_combo)
        cl.addSpacing(16)
        cl.addWidget(self._sort_combo)
        s.add(SectionHeader("ALL ACCOUNTS", C["text"], controls=controls_w))

        self._blocks_row = QHBoxLayout()
        self._blocks_row.setSpacing(10)
        self.blocks = {}
        # Cache of latest metrics per side — populated by refresh() and
        # used by the sort dropdown to reorder without re-fetching.
        self._last_metrics: dict[str, dict] = {}
        self._blocks_container = QWidget()
        self._blocks_container.setLayout(self._blocks_row)
        s.add(self._blocks_container)
        # Populate based on current bot registry (skips silenced/removed)
        self._rebuild_account_blocks()

        # ── Live total-portfolio chart (works 24/7) ──
        s.add(SectionHeader("PORTFOLIO VALUE  —  LIVE (updates even when market closed)",
                            C["text"], controls=self._period_combo()))
        self.combined_chart = ChartView(height=300)
        self.combined_chart.load_chart(CH.empty_chart("Loading…", height=300))
        s.add(self.combined_chart)

        # ── Total API cost summary ──
        s.add(SectionHeader("AI API KEY COSTS", C["yellow"]))
        cost_grid = QGridLayout()
        cost_grid.setSpacing(8)
        self.cost_cards = {}
        # V7.1.2: TOTAL SPENT (lifetime) replaces GRAND TOTAL (per-year
        # estimate) as the prominent figure — what the user has actually
        # paid Anthropic so far, summed across all bot logs.
        cost_metrics = [
            ("TOTAL SPENT", C["green"]),
            ("PER DAY",     C["muted"]),
            ("PER MONTH",   C["yellow"]),
            ("PER YEAR",    C["orange"]),
        ]
        for i, (label, color) in enumerate(cost_metrics):
            card = MetricCard(label, "—", color)
            cost_grid.addWidget(card, 0, i)
            self.cost_cards[label] = card

        # Per-bot cost row — only built-in bots have a token-estimate
        # model. Custom bots show "—" until they ship their own estimate.
        for j, side in enumerate(["LONG", "SHORT", "DAY"]):
            label = f"{side} TOTAL"
            card  = MetricCard(label, "—", BOT_COLOR[side])
            cost_grid.addWidget(card, 1, j)
            self.cost_cards[label] = card

        cw = QWidget()
        cw.setLayout(cost_grid)
        s.add(cw)

        s.add_stretch()

    # ── Active-bot block management (V7.1.2) ────────────────

    def _displayable_bots(self) -> list[dict]:
        """Return [{side, label, color}, ...] for bots that should
        appear in the Overview: active and NOT silenced. Reads the
        registry fresh each call so changes propagate after add/remove.

        V7.1.4: When the sort dropdown is set to anything other than
        "default", the list is reordered (descending) by the cached
        metric for that key. Falls back to registry order whenever
        metrics haven't been fetched yet (first render before
        refresh() has run)."""
        try:
            reg = D.load_settings().get("bot_registry", {})
        except Exception:
            reg = {}
        active   = reg.get("active",   ["LONG", "SHORT", "DAY"])
        silenced = set(reg.get("silenced", []))
        customs  = {c["id"]: c for c in reg.get("custom", []) if isinstance(c, dict)}

        builtin_meta = {
            "LONG":  ("▲ LONG BOT",  BOT_COLOR["LONG"]),
            "SHORT": ("▼ SHORT BOT", BOT_COLOR["SHORT"]),
            "DAY":   ("◆ DAY BOT",   BOT_COLOR["DAY"]),
        }
        out = []
        for sid in active:
            if sid in silenced:
                continue
            if sid in builtin_meta:
                label, color = builtin_meta[sid]
            elif sid in customs:
                label = customs[sid].get("label", sid).upper()
                color = customs[sid].get("color", C["purple"])
            else:
                label, color = sid.upper(), C["purple"]
            out.append({"side": sid, "label": label, "color": color})

        # Apply sort
        sort_key = self._current_sort_key()
        if sort_key != "default" and self._last_metrics:
            out.sort(
                key=lambda b: self._last_metrics.get(b["side"], {})
                                                .get(sort_key, 0),
                reverse=True,
            )
        return out

    def _current_sort_key(self) -> str:
        try:
            return self._sort_combo.currentData() or "default"
        except Exception:
            return "default"

    def _on_sort_changed(self, _idx: int):
        """Persist + rebuild the row. No metric re-fetch — we already
        have cached numbers from the most recent refresh()."""
        try:
            s = D.load_settings()
            s["overview_sort"] = self._current_sort_key()
            from json import dump
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                dump(s, f, indent=2)
        except Exception:
            pass
        self._rebuild_account_blocks()

    def _on_period_changed(self, period: str):
        """V7.1.6: persist + recompute the PERIOD P/L card on every
        bot block for the newly-selected timeframe. We re-run
        _refresh_block for each existing block (cheap — only touches
        the one card) instead of a full refresh()."""
        try:
            s = D.load_settings()
            s["overview_period"] = period
            from json import dump
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                dump(s, f, indent=2)
        except Exception:
            pass
        # Recompute PERIOD P/L for every visible block
        for side, block in self.blocks.items():
            try:
                self._refresh_block(side, block)
            except Exception:
                pass

    def _current_period(self) -> str:
        try:
            return self._period_combo.currentText() or "1D"
        except Exception:
            return "1D"

    def _rebuild_account_blocks(self):
        """Tear down and rebuild the row of bot blocks based on the
        current registry. Called by main.py whenever the registry
        changes (bot added / removed / silenced / unsilenced) so the
        Overview stays in sync without an app restart."""
        # Clear any existing blocks
        while self._blocks_row.count():
            item = self._blocks_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.blocks = {}

        bots = self._displayable_bots()
        if not bots:
            # No active bots — show a friendly empty state instead of a
            # blank row so the user knows to add a bot from MORE BOTS.
            empty = QLabel(
                "  No active bots.  Open the MORE BOTS tab to add one.")
            empty.setStyleSheet(
                f"color:{C['muted']};font-size:11px;padding:14px;"
                f"background:{C['panel']};border:1px dashed {C['border']};"
                f"border-radius:8px;")
            self._blocks_row.addWidget(empty)
            return

        for meta in bots:
            block = self._account_block(meta["side"],
                                        label_text=meta["label"],
                                        color=meta["color"])
            self.blocks[meta["side"]] = block
            self._blocks_row.addWidget(block)

    # Public alias for main.py to call after registry mutations.
    def refresh_active_bots(self):
        self._rebuild_account_blocks()
        # Re-trigger a data refresh so the new blocks show real numbers.
        try:
            self.refresh()
        except Exception:
            pass

    def reorder_active_bots(self):
        """V7.1.6: lightweight path called by main.py after a bot-tab
        drag. Just shuffles the existing block widgets within the row
        — no widget creation, no Alpaca calls, no flicker. The block
        contents stay populated from the most recent refresh tick."""
        try:
            ordered_sides = [b["side"] for b in self._displayable_bots()]
            # Remove all blocks (widgets persist), then re-add in order.
            for side, w in list(self.blocks.items()):
                self._blocks_row.removeWidget(w)
            for side in ordered_sides:
                w = self.blocks.get(side)
                if w is not None:
                    self._blocks_row.addWidget(w)
        except Exception as e:
            print(f"[overview reorder] {e}")

    def _account_block(self, side: str,
                       label_text: str | None = None,
                       color: str | None = None) -> QFrame:
        # V7.1.2: label + color are passed in by the registry-aware
        # builder so custom bots get a sensible card. Defaults keep
        # the old behaviour for any direct callers.
        if color is None:
            color = BOT_COLOR.get(side, C["purple"])
        if label_text is None:
            label_text = {"LONG": "▲ LONG BOT", "SHORT": "▼ SHORT BOT",
                          "DAY":  "◆ DAY BOT"}.get(side, side.upper())
        block = QFrame()
        block.setStyleSheet(
            f"background:{C['panel']};border:1px solid {color}30;"
            f"border-radius:10px;border-left:3px solid {color};"
        )
        layout = QVBoxLayout(block)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Title row with status dot
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        status_dot = QLabel("●")
        status_dot.setStyleSheet(
            f"color:{C['muted']};font-size:9px;background:transparent;border:none;"
        )
        status_dot.setFixedSize(14, 16)
        block._status_dot = status_dot

        title = QLabel(label_text)
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:11px;font-weight:800;"
            f"letter-spacing:3px;color:{color};"
        )
        title_row.addWidget(status_dot)
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        cards_layout = QGridLayout()
        cards_layout.setSpacing(8)

        card_defs = [
            ("PORTFOLIO", "—", color),
            ("DAY P/L",   "—", C["text"]),
            ("PERIOD P/L","—", C["text"]),
            ("POSITIONS", "—", C["text"]),
        ]
        block._cards = {}
        for i, (lbl, val, c) in enumerate(card_defs):
            card = MetricCard(lbl, val, c)
            card.setFixedHeight(72)
            cards_layout.addWidget(card, i//2, i%2)
            block._cards[lbl] = card

        layout.addLayout(cards_layout)
        block.side = side
        return block

    def update_bot_status(self, side: str, state: str):
        """Called by main window to update the status dot for a bot block."""
        block = self.blocks.get(side)
        if not block:
            return
        dot = block._status_dot
        dot_colors = {
            "running":         C["green"],
            "sleeping":        C["orange"],
            "scheduled":       C["red"],
            "stopped":         C["muted"],
            "silenced":        C["border"],
        }
        c = dot_colors.get(state, C["muted"])
        dot.setStyleSheet(
            f"color:{c};font-size:9px;background:transparent;border:none;"
        )

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
        """V7.1.1: freeze repaints around the whole refresh sweep so Qt
        only paints once at the end — kills the brief wobble/flash that
        used to happen as each block recalculated its layout.
        V7.1.4: cache fresh metrics for every displayed bot so the sort
        dropdown can reorder without a re-fetch, and rebuild the row in
        the active sort order."""
        self.setUpdatesEnabled(False)
        try:
            # Cache metrics so the sort dropdown has fresh numbers
            for meta in self._displayable_bots():
                try:
                    self._last_metrics[meta["side"]] = D.get_bot_metrics(meta["side"])
                except Exception:
                    pass
            # If a non-default sort is active, the block order in the
            # row may need to change; rebuilding is cheap so just do it.
            if self._current_sort_key() != "default":
                self._rebuild_account_blocks()

            for side, block in self.blocks.items():
                self._refresh_block(side, block)
        finally:
            self.setUpdatesEnabled(True)

        # Live total-portfolio chart (background fetch, 24/7)
        self._reload_combined()

        # Costs — V7.1.2: surface lifetime spend as TOTAL SPENT,
        # demote the per-year projection.
        costs = D.estimate_total_costs()
        total_spent = round(sum(c.get("total", 0.0)
                                for c in costs.get("by_bot", {}).values()), 4)
        if "TOTAL SPENT" in self.cost_cards:
            self.cost_cards["TOTAL SPENT"].update_value(
                f"${total_spent:.4f}", C["green"])
        self.cost_cards["PER DAY"].update_value(
            f"${costs['per_day']:.4f}", C["muted"])
        self.cost_cards["PER MONTH"].update_value(
            f"${costs['per_month']:.2f}", C["yellow"])
        self.cost_cards["PER YEAR"].update_value(
            f"${costs['per_year']:.2f}", C["orange"])
        # Per-bot row — only update cards for currently-displayed
        # built-in bots; silenced/removed bots leave their card blank.
        active_sides = {b["side"] for b in self._displayable_bots()}
        for side in ["LONG", "SHORT", "DAY"]:
            card = self.cost_cards.get(f"{side} TOTAL")
            if card is None:
                continue
            if side in active_sides:
                bc = costs["by_bot"].get(side, {})
                card.update_value(f"${bc.get('total',0):.4f}", BOT_COLOR[side])
            else:
                card.update_value("—", C["muted"])

    def _refresh_block(self, side, block):
        a    = D.get_account(side)
        pos  = D.get_positions(side)
        pv   = a.get("portfolio_value", 0)
        eq   = a.get("equity", 0)
        le   = a.get("last_equity", eq)
        dp   = eq - le
        arrow = "▲" if dp >= 0 else "▼"
        dc    = C["green"] if dp >= 0 else C["red"]

        # Period P/L: history over the period the user selected in the
        # ALL ACCOUNTS header (V7.1.6). Falls back to 1D if the combo
        # hasn't been built yet (very early refresh during _build).
        try:
            hist = D.get_history(side, self._current_period())
            if hist is not None and not hist.empty and len(hist) >= 2:
                p_pl  = hist["equity"].iloc[-1] - hist["equity"].iloc[0]
                p_pct = p_pl / hist["equity"].iloc[0] * 100 if hist["equity"].iloc[0] else 0
                pc    = C["green"] if p_pl >= 0 else C["red"]
                pa    = "▲" if p_pl >= 0 else "▼"
                period_txt = f"{pa} ${abs(p_pl):,.2f} ({p_pct:+.1f}%)"
            else:
                period_txt, pc = "—", C["muted"]
        except Exception:
            period_txt, pc = "—", C["muted"]

        block._cards["PORTFOLIO"].update_value(f"${pv:,.2f}", BOT_COLOR[side])
        block._cards["DAY P/L"].update_value(f"{arrow} ${abs(dp):,.2f}", dc)
        block._cards["PERIOD P/L"].update_value(period_txt, pc)
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

        # ── UPDATES  (V7.1.3) ────────────────────────────
        # The old "allow updates during the trading day" toggle is gone:
        # updates are now notify-only, so no time gating applies. APEX
        # checks on startup and every hour; when a new version exists,
        # an UPDATE AVAILABLE button appears in the top-right header.
        # Nothing installs without the user clicking that banner.
        s.add(SectionHeader("UPDATES", C["purple"]))
        upd_info = QLabel(
            "APEX checks GitHub for a new version on startup and once "
            "an hour. When one is available, an <b>UPDATE AVAILABLE</b> "
            "button appears in the top-right header — click it to see "
            "release notes and confirm the install. The installer's "
            "normal UI is shown (no silent install), so SmartScreen / "
            "Defender prompts are handled correctly and you always know "
            "exactly what's happening.")
        upd_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        upd_info.setWordWrap(True)
        upd_info.setTextFormat(Qt.TextFormat.RichText)
        s.add(upd_info)

        upd_row = QHBoxLayout()
        self._check_now_btn = QPushButton("⟳  Check for updates now")
        self._check_now_btn.setObjectName("toolBtn")
        self._check_now_btn.clicked.connect(self._check_updates_now)
        self._check_now_msg = QLabel("")
        self._check_now_msg.setStyleSheet(
            f"color:{C['muted']};font-size:10px;")
        upd_row.addWidget(self._check_now_btn)
        upd_row.addWidget(self._check_now_msg)
        upd_row.addStretch()
        urw = QWidget()
        urw.setLayout(upd_row)
        s.add(urw)

        # ── ACCOUNT LINKING (V7.1+) ─────────────────────────
        s.add(SectionHeader("ACCOUNT LINKING", C["purple"]))
        link_info = QLabel(
            "Sync your broker credentials to the APEX server so the cloud "
            "bots (running 24/7 on Oracle) can trade on your behalf even "
            "with your laptop off. Credentials are encrypted at rest with "
            "Fernet (AES-128) and only decrypted in-memory on the server.\n\n"
            "Direct Alpaca / IBKR OAuth linking is coming in a future "
            "release — for now, the keys above are pushed to the server "
            "when you click Sync."
        )
        link_info.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.6;")
        link_info.setWordWrap(True)
        s.add(link_info)

        link_row = QHBoxLayout()
        self._link_alpaca_btn = QPushButton("◯  Link Alpaca account…")
        self._link_alpaca_btn.setObjectName("toolBtn")
        self._link_alpaca_btn.setEnabled(False)
        self._link_alpaca_btn.setToolTip(
            "OAuth account linking — coming soon. Use the manual sync "
            "below for now.")
        self._link_ibkr_btn = QPushButton("◯  Link IBKR account…")
        self._link_ibkr_btn.setObjectName("toolBtn")
        self._link_ibkr_btn.setEnabled(False)
        self._link_ibkr_btn.setToolTip("Coming soon.")
        link_row.addWidget(self._link_alpaca_btn)
        link_row.addWidget(self._link_ibkr_btn)
        link_row.addStretch()
        lrw = QWidget()
        lrw.setLayout(link_row)
        s.add(lrw)

        sync_row = QHBoxLayout()
        self._sync_btn = QPushButton("⬆  Sync keys to APEX server")
        self._sync_btn.setObjectName("toolBtn")
        self._sync_btn.clicked.connect(self._sync_keys_to_server)
        self._sync_msg = QLabel("")
        self._sync_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        sync_row.addWidget(self._sync_btn)
        sync_row.addWidget(self._sync_msg)
        sync_row.addStretch()
        srw = QWidget()
        srw.setLayout(sync_row)
        s.add(srw)

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

    # ── V7.1+ handlers ──────────────────────────────────────

    def _toggle_force_update(self, on: bool):
        # Kept as a no-op for back-compat with any older signals; the
        # force_update flag itself is unused since V7.1.3 went to a
        # notify-only update model.
        try:
            D.set_force_update_now(bool(on))
        except Exception:
            pass

    def _check_updates_now(self):
        """V7.1.3: walk up the parent chain to find the ApexWindow and
        invoke its update checker. If nothing's available, briefly
        show 'You're on the latest version.' next to the button."""
        from PyQt6.QtWidgets import QApplication
        # Find the top-level ApexWindow (we don't import it directly to
        # avoid a circular import — main.py already imports ToolsTab).
        win = self.window()
        if not win or not hasattr(win, "_check_updates"):
            self._check_now_msg.setText("Update checker unavailable.")
            self._check_now_msg.setStyleSheet(
                f"color:{C['red']};font-size:10px;")
            return
        self._check_now_msg.setText("Checking…")
        self._check_now_msg.setStyleSheet(
            f"color:{C['muted']};font-size:10px;")

        # Wrap the existing _check_updates so we can show a status line
        # whether an update was found or not. The original signal flow:
        #   UpdateChecker.update_available  ->  _on_update_found
        # If no update is found, the signal never fires. We listen to
        # UpdateChecker.finished as a fallback to report "all good".
        from core.updater import check_for_update

        from PyQt6.QtCore import QThread, pyqtSignal as _Sig

        class _OneShotChecker(QThread):
            done = _Sig(object)  # update info dict or None

            def run(self):
                self.done.emit(check_for_update())

        def _on_done(info):
            if info:
                self._check_now_msg.setText(
                    f"v{info.get('latest','?')} available — see header.")
                self._check_now_msg.setStyleSheet(
                    f"color:{C['green']};font-size:10px;")
                # Trigger the main-window banner via its public hook
                try:
                    win._on_update_found(info)
                except Exception:
                    pass
            else:
                self._check_now_msg.setText("You're on the latest version. ✓")
                self._check_now_msg.setStyleSheet(
                    f"color:{C['green']};font-size:10px;")
            QTimer.singleShot(
                6000, lambda: self._check_now_msg.setText(""))

        self._one_shot = _OneShotChecker()
        self._one_shot.done.connect(_on_done)
        self._one_shot.start()

    def _sync_keys_to_server(self):
        """Push the current set of Alpaca/Anthropic keys to the APEX
        auth server, where they're stored encrypted (Fernet) per user.
        Requires a stored auth token (login or signup must have run on
        this machine before)."""
        from PyQt6.QtCore import QThread, pyqtSignal
        import json as _json
        try:
            from ui.login import load_auth, load_server_url
        except Exception:
            self._sync_msg.setText("Server module unavailable.")
            self._sync_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return

        stored = load_auth() or {}
        token = stored.get("token")
        if not token:
            self._sync_msg.setText(
                "Not signed in — sign in first (close the app and "
                "log in on the start screen).")
            self._sync_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return

        # Build the payload from the in-form edits (no need to round-trip
        # through .env — keys may have been edited but not saved yet).
        payload = {
            k: e.text().strip()
            for k, e in self._key_edits.items()
            if e.text().strip()
        }
        if not payload:
            self._sync_msg.setText("No keys entered.")
            self._sync_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            return

        server_url = load_server_url()
        self._sync_msg.setText("Uploading…")
        self._sync_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")

        class _UploadWorker(QThread):
            done = pyqtSignal(bool, str)

            def __init__(self, url, tok, body):
                super().__init__()
                self.url, self.tok, self.body = url, tok, body

            def run(self):
                import requests
                try:
                    r = requests.put(
                        f"{self.url}/credentials",
                        headers={"Authorization": f"Bearer {self.tok}"},
                        json=self.body, timeout=12,
                    )
                    if r.ok:
                        n = len(r.json().get("fields", []))
                        self.done.emit(True, f"Synced ✓  {n} key(s) encrypted on server.")
                    else:
                        self.done.emit(False, f"Server error ({r.status_code}).")
                except Exception as ex:
                    self.done.emit(False, str(ex))

        self._upload_worker = _UploadWorker(server_url, token, payload)
        self._upload_worker.done.connect(self._on_sync_result)
        self._upload_worker.start()

    def _on_sync_result(self, ok: bool, msg: str):
        self._sync_msg.setText(msg)
        color = C["green"] if ok else C["red"]
        self._sync_msg.setStyleSheet(f"color:{color};font-size:10px;")
        QTimer.singleShot(5000, lambda: self._sync_msg.setText(""))

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

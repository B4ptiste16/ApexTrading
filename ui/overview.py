"""
APEX Overview Tab — all three bots at a glance
APEX Tools Tab — broker conversion, cloud access, costs
"""

import os
import socket
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QFileDialog, QSizePolicy, QTextEdit,
    QLineEdit, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer

from ui.styles  import COLORS, BOT_COLOR
from ui.widgets import (
    ChartView, MetricCard, SectionHeader, ScrollContent, DataTable,
    NoScrollComboBox,
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
        self._sort_combo = NoScrollComboBox()
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

        # V7.1.10: renamed from _period_combo to _pl_period_combo so it
        # doesn't shadow the existing _period_combo() method that builds
        # the PORTFOLIO VALUE chart's period selector below.
        self._pl_period_combo = NoScrollComboBox()
        self._pl_period_combo.addItems(["1D", "1W", "1M", "3M", "6M", "1Y"])
        self._pl_period_combo.setFixedWidth(56)
        try:
            saved_period = D.load_settings().get("overview_period", "1D")
            self._pl_period_combo.setCurrentText(saved_period)
        except Exception:
            pass
        self._pl_period_combo.currentTextChanged.connect(self._on_period_changed)

        # Wrap both controls in a small row so the section header can
        # show them side-by-side.
        controls_w = QWidget()
        cl = QHBoxLayout(controls_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        period_lbl = QLabel("Period:")
        period_lbl.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        cl.addWidget(period_lbl)
        cl.addWidget(self._pl_period_combo)
        cl.addSpacing(16)
        cl.addWidget(self._sort_combo)
        s.add(SectionHeader("ALL ACCOUNTS", C["text"], controls=controls_w))

        # V4.0.0 — switched from a single horizontal row to a wrapping
        # grid (max 3 columns) so users with 4+ bots see them all.
        self._blocks_row = QGridLayout()
        self._blocks_row.setSpacing(10)
        for col in range(3):
            self._blocks_row.setColumnStretch(col, 1)
        self.blocks = {}
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
            reg = D.load_bot_registry()
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
            return self._pl_period_combo.currentText() or "1D"
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
            empty = QLabel(
                "  No active bots.  Open the MORE BOTS tab to add one.")
            empty.setStyleSheet(
                f"color:{C['muted']};font-size:11px;padding:14px;"
                f"background:{C['panel']};border:1px dashed {C['border']};"
                f"border-radius:8px;")
            self._blocks_row.addWidget(empty, 0, 0, 1, 2)
            return

        # V4.6.30 — 2-wide grid: row = idx // 2, col = idx % 2.
        # Each block has 4-6 metric cards; 3-per-row was too cramped to
        # read the numbers. Two per row gives each block room to breathe
        # and stacks additional bots underneath.
        self._blocks_row.setColumnStretch(0, 1)
        self._blocks_row.setColumnStretch(1, 1)
        for idx, meta in enumerate(bots):
            block = self._account_block(meta["side"],
                                        label_text=meta["label"],
                                        color=meta["color"])
            self.blocks[meta["side"]] = block
            self._blocks_row.addWidget(block, idx // 2, idx % 2)

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
            for side, w in list(self.blocks.items()):
                self._blocks_row.removeWidget(w)
            for idx, side in enumerate(ordered_sides):
                w = self.blocks.get(side)
                if w is not None:
                    self._blocks_row.addWidget(w, idx // 3, idx % 3)
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
            f"background:{C['panel']};border:none;"
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
        self.period_combo = NoScrollComboBox()
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
        the active sort order.
        V7.1.11: each per-block call is now wrapped in try/except so a
        single broken bot can't strand the rest of the row half-blank."""
        self.setUpdatesEnabled(False)
        try:
            # Cache metrics so the sort dropdown has fresh numbers
            for meta in self._displayable_bots():
                try:
                    self._last_metrics[meta["side"]] = D.get_bot_metrics(meta["side"])
                except Exception as e:
                    print(f"[overview] metrics {meta['side']}: {e}")
            # If a non-default sort is active, the block order in the
            # row may need to change; rebuilding is cheap so just do it.
            if self._current_sort_key() != "default":
                try:
                    self._rebuild_account_blocks()
                except Exception as e:
                    print(f"[overview] reorder: {e}")

            for side, block in self.blocks.items():
                try:
                    self._refresh_block(side, block)
                except Exception as e:
                    print(f"[overview] block {side}: {e}")
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
        # V4.6.28 — ALL P/L metrics scoped to the BOT'S lifetime, never
        # the Alpaca account's. A brand-new bot on a previously-used
        # account shows 0% / $0 until its first tick establishes a
        # baseline. Switching SHORT bot in for DAY bot doesn't carry
        # SHORT's history into DAY's display.
        block_color = BOT_COLOR.get(side, C["purple"])

        try:
            a    = D.get_account(side)
            pos  = D.get_positions(side)
        except Exception as e:
            print(f"[overview] get_account/{side} failed: {e}", flush=True)
            block._cards["PORTFOLIO"].update_value("—", C["muted"])
            block._cards["DAY P/L"].update_value("—", C["muted"])
            block._cards["PERIOD P/L"].update_value("—", C["muted"])
            block._cards["POSITIONS"].update_value("—", C["muted"])
            return

        # V4.6.32 — if the broker isn't connected (e.g. IBKR with no gateway
        # reachable, or Alpaca with no keys), show a neutral "not connected"
        # state. Critically, do NOT fall through with equity=0 — that would
        # both render a bogus -100% loss and append a 0-equity snapshot that
        # corrupts the bot's lifetime baseline.
        if not a or not a.get("connected"):
            block._cards["PORTFOLIO"].update_value("—", C["muted"])
            block._cards["DAY P/L"].update_value("—", C["muted"])
            block._cards["PERIOD P/L"].update_value("not connected", C["muted"])
            block._cards["POSITIONS"].update_value("—", C["muted"])
            return

        pv = a.get("portfolio_value", 0)
        eq = a.get("equity", 0)

        # Append the current snapshot to this bot's JSONL log (throttled
        # to 1 entry per 5 min by core.data). First call establishes the
        # baseline; subsequent calls extend the timeline.
        try:
            D.append_bot_snapshot(side, equity=eq,
                                   portfolio_value=pv,
                                   positions_count=len(pos))
        except Exception as _e:
            print(f"[overview] snapshot append for {side} failed: {_e}",
                  flush=True)

        bot_hist = []
        try:
            bot_hist = D.read_bot_snapshots(side)
        except Exception:
            pass

        # ── DAY P/L: scoped to BOT lifetime ─────────────────────
        # Find the earliest snapshot from today (UTC midnight).
        # Bot started today AFTER market open → DAY P/L = since first
        # tick today. Bot started yesterday → DAY P/L = since last tick
        # before today's UTC midnight.
        from datetime import datetime as _dt, timezone as _tz
        now_utc = _dt.now(_tz.utc)
        midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        today_baseline_eq = None
        for s in bot_hist:
            try:
                ts = _dt.fromisoformat(s["ts"])
                if ts >= midnight:
                    today_baseline_eq = float(s.get("equity", eq))
                    break
            except Exception:
                continue
        if today_baseline_eq is None and bot_hist:
            # No tick yet today — use the most recent pre-midnight snapshot
            for s in reversed(bot_hist):
                try:
                    ts = _dt.fromisoformat(s["ts"])
                    if ts < midnight:
                        today_baseline_eq = float(s.get("equity", eq))
                        break
                except Exception:
                    continue
        if today_baseline_eq is None:
            # Brand-new bot, no snapshots yet — 0 P/L (just appended above)
            today_baseline_eq = eq
        day_pl = eq - today_baseline_eq
        day_pct = (day_pl / today_baseline_eq * 100
                   if today_baseline_eq else 0)
        d_arrow = "▲" if day_pl >= 0 else "▼"
        d_color = C["green"] if day_pl >= 0 else C["red"]

        # ── PERIOD P/L: scoped to BOT lifetime, filtered by user's
        #    selected period (1D / 1W / 1M / 3M / 6M / 1Y) ──────
        period = self._current_period()
        period_days = {"1D": 1, "1W": 7, "1M": 30, "3M": 90,
                       "6M": 180, "1Y": 365}.get(period, 1)
        from datetime import timedelta as _td
        period_start = now_utc - _td(days=period_days)
        period_baseline_eq = None
        for s in bot_hist:
            try:
                ts = _dt.fromisoformat(s["ts"])
                if ts >= period_start:
                    period_baseline_eq = float(s.get("equity", eq))
                    break
            except Exception:
                continue
        if period_baseline_eq is None:
            # Bot's whole lifetime is shorter than the period — use the
            # earliest snapshot ever (= bot's birth equity).
            if bot_hist:
                period_baseline_eq = float(bot_hist[0].get("equity", eq))
            else:
                period_baseline_eq = eq
        p_pl  = eq - period_baseline_eq
        p_pct = (p_pl / period_baseline_eq * 100
                 if period_baseline_eq else 0)
        if len(bot_hist) < 2:
            # First-ever tick — show 0 / 0% (no historical comparison
            # makes sense yet)
            period_txt = "$0.00 (0.0%) · new bot"
            p_color    = C["muted"]
        else:
            p_arrow = "▲" if p_pl >= 0 else "▼"
            p_color = C["green"] if p_pl >= 0 else C["red"]
            period_txt = (f"{p_arrow} ${abs(p_pl):,.2f} ({p_pct:+.1f}%) "
                          f"· bot lifetime")

        block._cards["PORTFOLIO"].update_value(f"${pv:,.2f}", block_color)
        if len(bot_hist) < 2:
            block._cards["DAY P/L"].update_value("$0.00 (0.0%)",
                                                  C["muted"])
        else:
            block._cards["DAY P/L"].update_value(
                f"{d_arrow} ${abs(day_pl):,.2f} ({day_pct:+.1f}%)",
                d_color)
        block._cards["PERIOD P/L"].update_value(period_txt, p_color)
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

    def rebuild_for_mode(self, mode: str):
        """V3.2.0 — re-render the API-key section when broker mode changes."""
        # Clear EVERYTHING and rebuild from scratch — easiest way to
        # swap the entire mode-specific content.
        layout = self.layout()
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget() if it else None
            if w: w.deleteLater()
        self.scroll = ScrollContent()
        layout.addWidget(self.scroll)
        self._build()

    def _build(self):
        s = self.scroll
        mode = D.load_settings().get("broker_mode", "alpaca")

        # ── BROKER MODE BANNER ──────────────────────────────
        s.add(SectionHeader(
            f"BROKER MODE  ·  {mode.upper()}", C["purple"]))
        mode_info = QLabel(
            "Switch the active broker via the chip in the top-right of "
            "the app header. Each mode has its own Tools layout — "
            "non-Alpaca modes are still being wired up.")
        mode_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        mode_info.setWordWrap(True)
        s.add(mode_info)

        if mode == "alpaca":
            self._build_alpaca_section(s)
        else:
            self._build_coming_soon_section(s, mode)

        # ── AUTOMATION (always visible, all broker modes) ───
        self._build_automation_section(s)

        # ── AI PROVIDER KEYS (common to all modes) ──────────
        self._build_ai_key_section(s)

    def _build_coming_soon_section(self, s, mode: str):
        """IBKR shows a real connection-setup section. Other future brokers
        (TradingView) keep the coming-soon placeholder."""
        if mode == "ibkr":
            self._build_ibkr_section(s)
        else:
            label = mode.upper() if mode != "tradingview" else "TRADINGVIEW"
            s.add(SectionHeader(f"{label}  —  COMING VERY SOON", C["orange"]))
            wrap = QFrame()
            wrap.setStyleSheet(
                f"background:{C['panel']};border:none;"
                f"border-radius:10px;border-left:3px solid {C['orange']};")
            v = QVBoxLayout(wrap)
            v.setContentsMargins(24, 22, 24, 22)
            v.setSpacing(8)
            title = QLabel(f"🚧  {label} integration is on the way")
            title.setStyleSheet(
                f"font-family:'Syne',sans-serif;font-size:16px;"
                f"font-weight:800;color:{C['text']};letter-spacing:2px;")
            v.addWidget(title)
            desc = QLabel(
                f"APEX bots currently route orders through Alpaca's paper "
                f"trading API. {label} support — including connection setup, "
                f"key management, and per-bot account assignment — will land "
                f"in a future release.\n\n"
                f"Switch back to Alpaca via the broker-mode chip in the "
                f"header to manage your live keys.")
            desc.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.6;")
            desc.setWordWrap(True)
            v.addWidget(desc)
            s.add(wrap)

    def _build_ibkr_section(self, s):
        """Full IBKR connection setup — gateway + per-bot client IDs + % allocation.
        Configuration is stored per trading mode (paper / live) so the two
        environments are completely independent."""
        settings = D.load_settings()
        ibkr_mode = settings.get("alpaca_mode", "paper")
        self._ibkr_mode_key = f"ibkr_{ibkr_mode}"

        # ── Mode banner ──────────────────────────────────────────────────
        mode_color = C["orange"] if ibkr_mode == "paper" else C["red"]
        mode_label = "PAPER" if ibkr_mode == "paper" else "LIVE"
        banner = QFrame()
        banner.setStyleSheet(
            f"background:rgba(255,165,0,0.07);border:none;border-radius:8px;"
            f"border-left:3px solid {mode_color};")
        banner_row = QHBoxLayout(banner)
        banner_row.setContentsMargins(14, 10, 14, 10)
        banner_lbl = QLabel(
            f"{'⚙' if ibkr_mode == 'paper' else '⚠'}  Configuring IBKR  "
            f"<b>{mode_label} mode</b> — "
            f"{'simulated paper trading account.' if ibkr_mode == 'paper' else 'REAL money live account.'}"
            f"  Switch modes via the Paper / Live toggle in the app header.")
        banner_lbl.setStyleSheet(f"color:{mode_color};font-size:11px;")
        banner_lbl.setWordWrap(True)
        banner_row.addWidget(banner_lbl)
        s.add(banner)

        # ── Gateway connection ────────────────────────────────────────────
        s.add(SectionHeader("IBKR  ·  GATEWAY CONNECTION", C["green"]))
        conn_info = QLabel(
            "Interactive Brokers uses the IB Gateway or TWS desktop app as a local "
            "API bridge. Enter your connection details below. All BAPTOU bots share "
            "a single TWS/Gateway connection — each bot gets a unique Client ID so "
            "orders don't collide.")
        conn_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        conn_info.setWordWrap(True)
        s.add(conn_info)

        form = QFrame()
        form.setStyleSheet(f"background:{C['panel']};border:none;border-radius:8px;")
        fl = QGridLayout(form)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.setHorizontalSpacing(12)
        fl.setVerticalSpacing(10)

        # Load config for THIS mode; fall back to legacy "ibkr" key on first run
        cur = settings.get(self._ibkr_mode_key, settings.get("ibkr", {}))

        def lbl(text):
            w = QLabel(text)
            w.setStyleSheet(f"color:{C['text']};font-size:11px;")
            return w

        def inp(placeholder, key, default=""):
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            e.setText(str(cur.get(key, default)))
            return e

        default_port = "7497" if ibkr_mode == "paper" else "7496"
        fl.addWidget(lbl("TWS / Gateway Host"), 0, 0)
        self._ibkr_host = inp("127.0.0.1", "host", "127.0.0.1")
        fl.addWidget(self._ibkr_host, 0, 1)

        fl.addWidget(lbl(f"Port  (paper: 7497 · live: 7496)"), 1, 0)
        self._ibkr_port = inp(default_port, "port", default_port)
        fl.addWidget(self._ibkr_port, 1, 1)

        fl.addWidget(lbl("Master Account (optional)"), 2, 0)
        self._ibkr_account = inp("e.g. DU123456", "account", "")
        fl.addWidget(self._ibkr_account, 2, 1)

        s.add(form)

        # ── Cloud 24/7 on Oracle (paper login) ────────────────────────────
        # V4.6.40 — let IBKR bots run 24/7 on the APEX server WITHOUT the
        # user keeping TWS/Gateway open locally. APEX runs a per-user IB
        # Gateway on Oracle, logged into the user's PAPER account. Live is
        # not offered here because it needs IBKR Mobile 2FA, which can't be
        # approved head-less.
        s.add(SectionHeader("CLOUD 24/7  ·  RUN ON ORACLE (PAPER)", C["green"]))
        cloud_info = QLabel(
            "Run your IBKR bots 24/7 on the APEX cloud server — no need to keep "
            "TWS / IB Gateway open on this computer. APEX launches a private "
            "IB Gateway on the server, logged into your <b>paper</b> account. "
            "Your login is stored <b>encrypted</b> on the server and used only to "
            "start your gateway.<br><br>"
            "⚠ <b>Paper only.</b> Live accounts require IBKR Mobile 2FA approval "
            "on every login, which can't be done on a head-less server.")
        cloud_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        cloud_info.setWordWrap(True)
        s.add(cloud_info)

        cloud_form = QFrame()
        cloud_form.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:8px;")
        cfl = QGridLayout(cloud_form)
        cfl.setContentsMargins(16, 14, 16, 14)
        cfl.setHorizontalSpacing(12)
        cfl.setVerticalSpacing(10)

        cfl.addWidget(lbl("Paper username"), 0, 0)
        self._ibkr_cloud_user = QLineEdit()
        self._ibkr_cloud_user.setPlaceholderText("IBKR paper username")
        self._ibkr_cloud_user.setText(str(cur.get("cloud_username", "")))
        cfl.addWidget(self._ibkr_cloud_user, 0, 1)

        cfl.addWidget(lbl("Paper password"), 1, 0)
        self._ibkr_cloud_pw = QLineEdit()
        self._ibkr_cloud_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._ibkr_cloud_pw.setPlaceholderText("IBKR paper password")
        self._ibkr_cloud_pw.setText(str(cur.get("cloud_password", "")))
        cfl.addWidget(self._ibkr_cloud_pw, 1, 1)

        self._ibkr_run_on_oracle = QCheckBox(
            "Run IBKR bots on Oracle (24/7, this computer can be closed)")
        self._ibkr_run_on_oracle.setChecked(bool(cur.get("run_on_oracle", False)))
        self._ibkr_run_on_oracle.setStyleSheet(f"color:{C['text']};font-size:11px;")
        cfl.addWidget(self._ibkr_run_on_oracle, 2, 0, 1, 2)

        cloud_save_row = QHBoxLayout()
        cloud_save_btn = QPushButton("☁  Save & sync cloud login")
        cloud_save_btn.setObjectName("addBotBtn")
        cloud_save_btn.clicked.connect(self._save_ibkr_cloud_login)
        self._ibkr_cloud_msg = QLabel("")
        self._ibkr_cloud_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        cloud_save_row.addWidget(cloud_save_btn)
        cloud_save_row.addWidget(self._ibkr_cloud_msg)
        cloud_save_row.addStretch()
        cloud_save_w = QWidget(); cloud_save_w.setLayout(cloud_save_row)
        cfl.addWidget(cloud_save_w, 3, 0, 1, 2)

        s.add(cloud_form)

        # ── Bot client IDs & % allocation (combined table) ────────────────
        s.add(SectionHeader("BOT  ·  CLIENT IDs & FUND ALLOCATION (%)", C["purple"]))
        bot_info = QLabel(
            "Each bot uses a unique Client ID on the shared TWS connection. "
            "Allocate a <b>percentage of available account cash</b> per bot — "
            "e.g. 30% means the bot may use up to 30% of the IBKR account's "
            "buying power. Percentages should sum to ≤ 100%. "
            "Built-in bots (LONG / SHORT / DAY) are fully IBKR-compatible. "
            "When a bot is replaced, the incoming bot inherits the slot's positions; "
            "positions incompatible with its asset class are auto-liquidated on first run.")
        bot_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        bot_info.setWordWrap(True)
        s.add(bot_info)

        # Live allocation indicator (% remaining)
        alloc_bar = QFrame()
        alloc_bar.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:8px;")
        alloc_row = QHBoxLayout(alloc_bar)
        alloc_row.setContentsMargins(16, 12, 16, 12)
        self._ibkr_remaining_lbl = QLabel("Allocated: 0%  ·  Remaining: 100%")
        self._ibkr_remaining_lbl.setStyleSheet(
            f"color:{C['green']};font-size:11px;font-weight:700;")
        alloc_row.addWidget(self._ibkr_remaining_lbl)
        alloc_row.addStretch()
        s.add(alloc_bar)

        # Dynamic bot rows container
        self._ibkr_rows_frame = QFrame()
        self._ibkr_rows_frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:8px;")
        self._ibkr_rows_layout = QVBoxLayout(self._ibkr_rows_frame)
        self._ibkr_rows_layout.setContentsMargins(16, 14, 16, 14)
        self._ibkr_rows_layout.setSpacing(8)
        self._ibkr_bot_rows: list[dict] = []

        # Column header
        hdr_w = QWidget()
        hdr_l = QHBoxLayout(hdr_w)
        hdr_l.setContentsMargins(0, 0, 0, 4)
        hdr_l.setSpacing(8)
        for txt, w in [("Bot", 155), ("Client ID", 80), ("Alloc %", 70)]:
            h = QLabel(txt)
            h.setStyleSheet(
                f"color:{C['muted']};font-size:10px;font-weight:700;")
            h.setFixedWidth(w)
            hdr_l.addWidget(h)
        hdr_l.addStretch()
        self._ibkr_rows_layout.addWidget(hdr_w)

        for entry in self._ibkr_load_saved_bots(cur):
            self._ibkr_add_bot_row(
                entry["id"],
                entry.get("client_id", ""),
                entry.get("allocation", ""))

        s.add(self._ibkr_rows_frame)

        # Add bot row
        add_frame = QFrame()
        add_frame.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:8px;")
        add_row = QHBoxLayout(add_frame)
        add_row.setContentsMargins(16, 10, 16, 10)
        self._ibkr_add_combo = NoScrollComboBox()
        self._ibkr_add_combo.setMinimumWidth(180)
        self._ibkr_refresh_add_combo()
        add_btn_w = QPushButton("+ Add bot")
        add_btn_w.setObjectName("addBotBtn")
        add_btn_w.clicked.connect(self._ibkr_add_from_combo)
        add_row.addWidget(self._ibkr_add_combo)
        add_row.addWidget(add_btn_w)
        add_row.addStretch()
        s.add(add_frame)

        # Save + test row
        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾  Save IBKR settings")
        save_btn.setObjectName("addBotBtn")
        save_btn.clicked.connect(self._save_ibkr_settings)
        test_btn = QPushButton("⚡  Test connection")
        test_btn.setObjectName("toolBtn")
        test_btn.clicked.connect(self._test_ibkr_connection)
        self._ibkr_msg = QLabel("")
        self._ibkr_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        btn_row.addWidget(save_btn)
        btn_row.addWidget(test_btn)
        btn_row.addWidget(self._ibkr_msg)
        btn_row.addStretch()
        bw = QWidget(); bw.setLayout(btn_row)
        s.add(bw)

        s.add(SectionHeader("NOTES", C["muted"]))
        notes = QLabel(
            "• Short bots hold negative positions (borrowed shares). "
            "IBKR requires a margin account for shorting — paper accounts support this.\n"
            "• Paper IBKR account IDs start with 'DU'; live IDs start with 'U'.\n"
            "• Bot tab settings (LONG / SHORT / DAY) are independent per mode — "
            "switch modes in the header to configure each independently.\n"
            "• When replacing a bot, the new bot inherits existing positions. "
            "Positions outside its asset class are auto-liquidated on its first cycle.")
        notes.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:11px;"
            f"color:{C['text']};line-height:1.8;")
        notes.setWordWrap(True)
        s.add(notes)

        self._update_ibkr_remaining()

    # ── IBKR helpers ────────────────────────────────────────────────────

    def _ibkr_load_saved_bots(self, cur: dict) -> list[dict]:
        """Return bot list from settings; migrates legacy separate dicts format."""
        bots = cur.get("bots", None)
        if bots is not None:
            return bots
        # Migrate legacy format: separate client_ids + allocations dicts
        old_cids   = cur.get("client_ids",  {})
        old_allocs = cur.get("allocations", {})
        _DEFAULTS = ["LONG", "SHORT", "DAY"]
        try:
            reg = D.load_bot_registry()
            active = reg.get("active", _DEFAULTS)
        except Exception:
            active = _DEFAULTS
        result = []
        for i, bid in enumerate(active):
            result.append({
                "id":         bid,
                "client_id":  old_cids.get(bid, str(i + 1)),
                "allocation": old_allocs.get(bid, ""),
            })
        if not result:
            for i, bid in enumerate(_DEFAULTS):
                result.append({"id": bid, "client_id": str(i + 1), "allocation": ""})
        return result

    def _ibkr_bot_meta(self, bot_id: str) -> tuple:
        """Return (display_label, color_hex) for a bot ID."""
        _BUILTIN = {
            "LONG":  ("▲  LONG",  BOT_COLOR.get("LONG",  C["green"])),
            "SHORT": ("▼  SHORT", BOT_COLOR.get("SHORT", C["red"])),
            "DAY":   ("◆  DAY",   BOT_COLOR.get("DAY",   C["orange"])),
        }
        if bot_id in _BUILTIN:
            return _BUILTIN[bot_id]
        try:
            for c in D.load_all_custom_bots():
                if isinstance(c, dict) and c.get("id") == bot_id:
                    return f"●  {c.get('label', bot_id)}", c.get("color", C["purple"])
        except Exception:
            pass
        return f"●  {bot_id}", C["muted"]

    def _ibkr_add_bot_row(self, bot_id: str, client_id: str = "", allocation: str = ""):
        """Append one bot row to the dynamic IBKR bot table."""
        label, color = self._ibkr_bot_meta(bot_id)

        row_w = QWidget()
        row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(6)

        lbl_w = QLabel(label)
        lbl_w.setStyleSheet(
            f"color:{color};font-size:11px;font-weight:700;")
        lbl_w.setFixedWidth(155)

        auto_cid = client_id if client_id else str(len(self._ibkr_bot_rows) + 1)
        cid_edit = QLineEdit(auto_cid)
        cid_edit.setPlaceholderText("ID")
        cid_edit.setFixedWidth(80)
        cid_edit.textChanged.connect(self._update_ibkr_remaining)

        alloc_edit = QLineEdit(str(allocation))
        alloc_edit.setPlaceholderText("e.g. 30")
        alloc_edit.setFixedWidth(70)
        alloc_edit.textChanged.connect(self._update_ibkr_remaining)

        # Replace button — opens picker to swap this slot to another bot
        repl_btn = QPushButton("↔ Replace")
        repl_btn.setFixedWidth(72)
        repl_btn.setFixedHeight(26)
        repl_btn.setStyleSheet(
            f"QPushButton{{background:rgba(138,147,201,0.18);color:{C['purple']};"
            f"border:none;border-radius:4px;font-size:10px;font-weight:600;}}"
            f"QPushButton:hover{{background:rgba(138,147,201,0.30);}}")

        rm_btn = QPushButton("✕")
        rm_btn.setFixedWidth(26)
        rm_btn.setFixedHeight(26)
        rm_btn.setStyleSheet(
            f"QPushButton{{background:rgba(231,76,60,0.15);color:{C['red']};"
            f"border:none;border-radius:4px;font-size:11px;font-weight:700;}}"
            f"QPushButton:hover{{background:rgba(231,76,60,0.30);}}")

        row_l.addWidget(lbl_w)
        row_l.addWidget(cid_edit)
        row_l.addWidget(alloc_edit)
        row_l.addWidget(repl_btn)
        row_l.addWidget(rm_btn)
        row_l.addStretch()

        entry = {
            "id": bot_id, "label": label, "color": color,
            "cid_edit": cid_edit, "alloc_edit": alloc_edit,
            "lbl_widget": lbl_w, "row_widget": row_w,
        }
        self._ibkr_bot_rows.append(entry)
        self._ibkr_rows_layout.addWidget(row_w)

        def _remove(_e=entry):
            from PyQt6.QtWidgets import QMessageBox, QApplication
            from PyQt6.QtCore import Qt
            side = _e["id"]
            # V4.6.38 — removing a bot liquidates its entire sub-portfolio so
            # the cash is freed for manual redistribution. Confirm first since
            # this places real market orders.
            try:
                from core import ibkr_lifecycle
                from core.ledger import ledger_path, Ledger
                from core.paths import DATA_DIR
                mode = D.load_settings().get("alpaca_mode", "paper")
                led = Ledger.load(ledger_path(side, "ibkr", mode, DATA_DIR))
            except Exception:
                led, ibkr_lifecycle = None, None
            holdings = (led.symbols() if led is not None else [])
            if holdings:
                resp = QMessageBox.question(
                    self, "Remove bot & liquidate",
                    f"Removing <b>{_e['label'].strip()}</b> will MARKET-SELL its "
                    f"entire sub-portfolio ({len(holdings)} position(s): "
                    f"{', '.join(holdings)}) and free the cash for manual "
                    f"redistribution.<br><br>Proceed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if resp != QMessageBox.StandardButton.Yes:
                    return
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    ok, info = ibkr_lifecycle.liquidate_and_remove(side, mode)
                finally:
                    QApplication.restoreOverrideCursor()
                if not ok:
                    QMessageBox.warning(self, "Liquidation incomplete", info)
                    return   # keep the row so positions aren't orphaned
                if hasattr(self, "_ibkr_msg"):
                    self._ibkr_msg.setText(f"✓ {info}")
                    self._ibkr_msg.setStyleSheet(
                        f"color:{C['green']};font-size:10px;")
            elif led is not None:
                led.delete()   # empty ledger — just drop it
            if _e in self._ibkr_bot_rows:
                self._ibkr_bot_rows.remove(_e)
            _e["row_widget"].deleteLater()
            self._save_ibkr_settings()
            self._ibkr_refresh_add_combo()
            self._update_ibkr_remaining()

        rm_btn.clicked.connect(_remove)
        repl_btn.clicked.connect(lambda checked=False, _e=entry: self._ibkr_start_replace(_e))
        self._ibkr_refresh_add_combo()
        self._update_ibkr_remaining()

    def _ibkr_start_replace(self, entry: dict):
        """Open a picker to choose the replacement bot for this slot."""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout as _VL
        already = {r["id"] for r in self._ibkr_bot_rows if r is not entry}
        options: list[tuple] = []
        for bid in ("LONG", "SHORT", "DAY"):
            if bid not in already:
                lbl, _ = self._ibkr_bot_meta(bid)
                options.append((bid, lbl.strip()))
        try:
            for c in D.load_all_custom_bots():
                if not isinstance(c, dict):
                    continue
                bid = c.get("id", "")
                if bid and bid not in already and bid != entry["id"]:
                    options.append((bid, c.get("label", bid)))
        except Exception:
            pass
        if not options:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "No bots available",
                "All available bots are already in the table.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Replace bot")
        dlg.setMinimumWidth(380)
        dlg.setStyleSheet(self.window().styleSheet() if self.window() else "")
        vl = _VL(dlg)
        vl.setSpacing(10)
        vl.setContentsMargins(20, 16, 20, 16)

        info = QLabel(
            f"Replacing  <b>{entry['label'].strip()}</b>  (Client ID {entry['cid_edit'].text()}).<br>"
            f"The replacement bot inherits this slot's Client ID and allocation %.<br>"
            f"<span style='color:{C['muted']};font-size:10px;'>"
            f"Existing positions in this IBKR slot are inherited by the new bot. "
            f"Positions incompatible with the new bot's asset class will be "
            f"auto-liquidated on its first cycle.</span>")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{C['text']};font-size:11px;")
        vl.addWidget(info)

        pick_lbl = QLabel("Replace with:")
        pick_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;font-weight:700;")
        vl.addWidget(pick_lbl)

        pick_combo = NoScrollComboBox()
        for bid, lbl_txt in options:
            pick_combo.addItem(lbl_txt, bid)
        vl.addWidget(pick_combo)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        btns.setStyleSheet(
            f"QPushButton{{background:{C['panel']};color:{C['text']};"
            f"border:none;border-radius:6px;padding:6px 18px;font-weight:700;}}"
            f"QPushButton:hover{{background:{C['purple']};color:#fff;}}")
        vl.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_id = pick_combo.currentData()
        if not new_id or new_id == entry["id"]:
            return
        self._ibkr_do_replace(entry, new_id)

    def _ibkr_do_replace(self, old_entry: dict, new_id: str):
        """Swap the bot in a table row, stop the old bot subprocess, save."""
        old_id = old_entry["id"]
        new_label, new_color = self._ibkr_bot_meta(new_id)

        # V4.6.38 — hand the slot's sub-portfolio (cash + shares) to the new
        # bot so it inherits the positions and decides what to keep/sell on
        # its first cycle (off-asset-class holdings auto-liquidate there).
        try:
            from core import ibkr_lifecycle
            mode = D.load_settings().get("alpaca_mode", "paper")
            ibkr_lifecycle.transfer_ledger(old_id, new_id, mode)
        except Exception as e:
            print(f"[ibkr-replace] ledger transfer: {e}")

        # Update the row's display
        old_entry["id"] = new_id
        old_entry["label"] = new_label
        old_entry["color"] = new_color
        old_entry["lbl_widget"].setText(new_label)
        old_entry["lbl_widget"].setStyleSheet(
            f"color:{new_color};font-size:11px;font-weight:700;")

        # Save settings immediately
        self._save_ibkr_settings()

        # Stop the old bot subprocess if it's running locally
        try:
            main_win = self.window()
            bot_tabs = getattr(main_win, "_bot_tabs", {})
            old_tab = bot_tabs.get(old_id)
            if old_tab:
                ctrl = getattr(old_tab, "bot_ctrl", None)
                if ctrl and ctrl.is_running():
                    ctrl.stop_bot()
        except Exception as e:
            print(f"[ibkr-replace] stop old bot: {e}")

        self._ibkr_refresh_add_combo()
        self._update_ibkr_remaining()

        # Confirm message
        if hasattr(self, "_ibkr_msg"):
            old_lbl = old_id  # old_entry["label"] is now updated
            self._ibkr_msg.setText(
                f"✓ Slot replaced → {new_label.strip()}. "
                f"Start it from its tab.")
            self._ibkr_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            QTimer.singleShot(6000, lambda: self._ibkr_msg.setText(""))

    def _ibkr_refresh_add_combo(self):
        """Refresh the 'add bot' combo: only bots not yet in the table."""
        if not hasattr(self, "_ibkr_add_combo"):
            return
        already = {r["id"] for r in getattr(self, "_ibkr_bot_rows", [])}
        options: list[tuple] = []
        for bid in ("LONG", "SHORT", "DAY"):
            if bid not in already:
                label, _ = self._ibkr_bot_meta(bid)
                options.append((bid, label.strip()))
        try:
            for c in D.load_all_custom_bots():
                if not isinstance(c, dict):
                    continue
                bid = c.get("id", "")
                if bid and bid not in already:
                    options.append((bid, c.get("label", bid)))
        except Exception:
            pass
        self._ibkr_add_combo.blockSignals(True)
        self._ibkr_add_combo.clear()
        for bot_id, label in options:
            self._ibkr_add_combo.addItem(label, bot_id)
        if not options:
            self._ibkr_add_combo.addItem("— all bots added —", None)
        self._ibkr_add_combo.blockSignals(False)

    def _ibkr_add_from_combo(self):
        bot_id = self._ibkr_add_combo.currentData()
        if not bot_id:
            return
        used_ids: set[int] = set()
        for r in self._ibkr_bot_rows:
            try:
                used_ids.add(int(r["cid_edit"].text()))
            except ValueError:
                pass
        next_cid = 1
        while next_cid in used_ids:
            next_cid += 1
        self._ibkr_add_bot_row(bot_id, str(next_cid), "")

    def _update_ibkr_remaining(self):
        """Recompute and display allocated / remaining % across all IBKR bot rows."""
        if not hasattr(self, "_ibkr_remaining_lbl"):
            return
        allocated = 0.0
        for r in getattr(self, "_ibkr_bot_rows", []):
            txt = r["alloc_edit"].text().strip().rstrip("%")
            if txt:
                try:
                    allocated += float(txt)
                except ValueError:
                    pass
        remaining = 100.0 - allocated
        color = C["green"] if remaining >= 0 else C["red"]
        self._ibkr_remaining_lbl.setText(
            f"Allocated: {allocated:.1f}%  ·  Remaining: {remaining:.1f}%")
        self._ibkr_remaining_lbl.setStyleSheet(
            f"color:{color};font-size:11px;font-weight:700;")

    def _save_ibkr_settings(self):
        try:
            import json
            s = D.load_settings()
            # Save under the per-mode key (ibkr_paper / ibkr_live)
            key = getattr(self, "_ibkr_mode_key", "ibkr")
            s[key] = {
                "host":    self._ibkr_host.text().strip() or "127.0.0.1",
                "port":    self._ibkr_port.text().strip() or "7497",
                "account": self._ibkr_account.text().strip(),
                "bots": [
                    {
                        "id":         r["id"],
                        "client_id":  r["cid_edit"].text().strip() or str(i + 1),
                        "allocation": r["alloc_edit"].text().strip().rstrip("%"),
                    }
                    for i, r in enumerate(self._ibkr_bot_rows)
                ],
            }
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
            # Drop the cached IBKR snapshot so new host/port/allocations apply
            try:
                from core import ibkr_data
                ibkr_data.reset()
            except Exception:
                pass
            # V4.6.38 — seed a sub-portfolio ledger for any newly-added bot,
            # granting it allocation% of the account's available cash. Existing
            # ledgers keep their running balance. Needs a live gateway to read
            # cash; silently skips (bot falls back to whole-account) if offline.
            seeded = 0
            try:
                from core import ibkr_data, ibkr_lifecycle
                cash = ibkr_data.available_cash()
                if cash > 0:
                    seeded = ibkr_lifecycle.seed_all(
                        [{"id": r["id"],
                          "allocation": r["alloc_edit"].text()}
                         for r in self._ibkr_bot_rows],
                        cash)
            except Exception as e:
                print(f"[ibkr] ledger seed skipped: {e}")
            msg = "✓ Saved"
            if seeded:
                msg = f"✓ Saved · seeded {seeded} sub-portfolio(s)"
            self._ibkr_msg.setText(msg)
            self._ibkr_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        except Exception as e:
            self._ibkr_msg.setText(f"Save failed: {e}")
            self._ibkr_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
        QTimer.singleShot(3000, lambda: self._ibkr_msg.setText(""))

    def _ibkr_cloud_credentials(self, settings: dict | None = None) -> dict:
        """V4.6.40 — return the IBKR cloud-login fields to include in any
        /credentials sync, read from local settings (ibkr_<mode>). Empty
        dict when no paper login is stored. APEX_CLOUD_BROKER is only set to
        'ibkr' when the user enabled 'Run IBKR bots on Oracle'."""
        try:
            s = settings if settings is not None else D.load_settings()
            mode = s.get("alpaca_mode", "paper")
            cur  = s.get(f"ibkr_{mode}", {}) or {}
            user = str(cur.get("cloud_username", "")).strip()
            pw   = str(cur.get("cloud_password", ""))
            if not (user and pw):
                return {}
            run_oracle = bool(cur.get("run_on_oracle", False))
            return {
                "IBKR_USERNAME":     user,
                "IBKR_PASSWORD":     pw,
                "IBKR_TRADING_MODE": mode,
                "APEX_CLOUD_BROKER": "ibkr" if run_oracle else "alpaca",
            }
        except Exception:
            return {}

    def _save_ibkr_cloud_login(self):
        """V4.6.40 — persist the IBKR paper login + 'run on Oracle' toggle
        locally, then push them (encrypted server-side) to /credentials so
        the cloud bot_runner can launch this user's IB Gateway 24/7."""
        try:
            import json
            s = D.load_settings()
            key = getattr(self, "_ibkr_mode_key", "ibkr")
            cur = dict(s.get(key, {}))
            cur["cloud_username"] = self._ibkr_cloud_user.text().strip()
            cur["cloud_password"] = self._ibkr_cloud_pw.text()
            cur["run_on_oracle"]  = bool(self._ibkr_run_on_oracle.isChecked())
            s[key] = cur
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            self._ibkr_cloud_msg.setText(f"Save failed: {e}")
            self._ibkr_cloud_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            QTimer.singleShot(4000, lambda: self._ibkr_cloud_msg.setText(""))
            return

        run_oracle = bool(self._ibkr_run_on_oracle.isChecked())
        user = self._ibkr_cloud_user.text().strip()
        pw   = self._ibkr_cloud_pw.text()
        if run_oracle and not (user and pw):
            self._ibkr_cloud_msg.setText(
                "✗ Enter your paper username & password to run on Oracle")
            self._ibkr_cloud_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            QTimer.singleShot(6000, lambda: self._ibkr_cloud_msg.setText(""))
            return

        # Push to the APEX server credential blob (encrypted at rest).
        try:
            from ui.login import load_auth, load_server_url
        except Exception:
            self._ibkr_cloud_msg.setText("✓ Saved locally (login module unavailable)")
            self._ibkr_cloud_msg.setStyleSheet(f"color:{C['orange']};font-size:10px;")
            QTimer.singleShot(6000, lambda: self._ibkr_cloud_msg.setText(""))
            return

        stored = load_auth() or {}
        token  = stored.get("token")
        if not token:
            self._ibkr_cloud_msg.setText(
                "✓ Saved locally — sign in, then save again to sync to cloud")
            self._ibkr_cloud_msg.setStyleSheet(f"color:{C['orange']};font-size:10px;")
            QTimer.singleShot(7000, lambda: self._ibkr_cloud_msg.setText(""))
            return

        ibkr_mode = D.load_settings().get("alpaca_mode", "paper")
        fields = {
            "IBKR_USERNAME":     user,
            "IBKR_PASSWORD":     pw,
            "IBKR_TRADING_MODE": ibkr_mode,
            # Tells the cloud bot_runner to trade this user's bots on IBKR
            # (vs. Alpaca) and launch their gateway. 'alpaca' = run on Alpaca.
            "APEX_CLOUD_BROKER": "ibkr" if run_oracle else "alpaca",
        }
        server_url = load_server_url()

        from PyQt6.QtCore import QThread, pyqtSignal

        class _CloudLoginWorker(QThread):
            done = pyqtSignal(bool, str)

            def __init__(self, url, tok, new_fields):
                super().__init__()
                self.url, self.tok, self.new_fields = url, tok, new_fields

            def run(self):
                import requests
                hdr = {"Authorization": f"Bearer {self.tok}"}
                try:
                    # PUT /credentials REPLACES the whole blob, so merge the
                    # new IBKR fields into the user's existing credentials
                    # (Alpaca keys, AI keys, mode, schedule flags) first.
                    blob = {}
                    try:
                        g = requests.get(f"{self.url}/credentials",
                                         headers=hdr, timeout=20)
                        if g.ok and isinstance(g.json(), dict):
                            blob = g.json()
                    except Exception:
                        blob = {}
                    blob.update(self.new_fields)
                    r = requests.put(f"{self.url}/credentials",
                                     json=blob, headers=hdr, timeout=20)
                    if r.ok:
                        self.done.emit(True, "")
                    else:
                        self.done.emit(False, f"HTTP {r.status_code}: {r.text[:120]}")
                except Exception as ex:
                    self.done.emit(False, str(ex))

        def _on_done(ok, err):
            if ok:
                msg = ("☁ Cloud login synced — IBKR bots will run on Oracle 24/7"
                       if run_oracle else
                       "✓ Synced — IBKR bots will run locally (Oracle run off)")
                self._ibkr_cloud_msg.setText(msg)
                self._ibkr_cloud_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            else:
                self._ibkr_cloud_msg.setText(f"✗ Cloud sync failed: {err}")
                self._ibkr_cloud_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            QTimer.singleShot(9000, lambda: self._ibkr_cloud_msg.setText(""))

        self._ibkr_cloud_msg.setText("☁ Syncing cloud login…")
        self._ibkr_cloud_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        self._cloud_login_worker = _CloudLoginWorker(server_url, token, fields)
        self._cloud_login_worker.done.connect(_on_done)
        self._cloud_login_worker.start()

    def _test_ibkr_connection(self):
        self._ibkr_msg.setText("Testing…")
        self._ibkr_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        try:
            host = self._ibkr_host.text().strip() or "127.0.0.1"
            port = int(self._ibkr_port.text().strip() or "7497")
            import socket
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            self._ibkr_msg.setText(f"✓ Port {port} reachable — IB Gateway appears to be running")
            self._ibkr_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        except Exception as e:
            self._ibkr_msg.setText(
                f"✗ Could not reach {self._ibkr_host.text().strip()}:{self._ibkr_port.text().strip()} "
                f"— make sure IB Gateway / TWS is open and API is enabled")
            self._ibkr_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
        QTimer.singleShot(8000, lambda: self._ibkr_msg.setText(""))

    def refresh_alpaca_slot_assignments(self):
        """V4.6.24 — repopulate each slot's Assigned dropdown to pick
        up newly-created custom bots without forcing an app restart.
        Called by ApexWindow when bot_added / bot_removed fires.
        Preserves the currently-selected value per slot when possible."""
        if not hasattr(self, "_alpaca_slot_edits"):
            return  # section not built yet (broker != alpaca)
        # Rebuild SIDE_OPTIONS from the current bot registry
        SIDE_OPTIONS = [
            ("LONG",       "▲ LONG bot"),
            ("SHORT",      "▼ SHORT bot"),
            ("DAY",        "◆ DAY bot"),
            ("UNASSIGNED", "(unassigned)"),
        ]
        try:
            reg = D.load_bot_registry()
            for c in reg.get("custom", []):
                slug = str(c.get("id", "")).upper()
                if slug and slug not in {sv for sv, _ in SIDE_OPTIONS}:
                    SIDE_OPTIONS.append(
                        (slug, c.get("label", slug) + " bot"))
        except Exception:
            pass
        # Update each existing combo: keep current selection if its
        # value still exists, else fall back to UNASSIGNED.
        for slot in self._alpaca_slot_edits:
            combo = slot.get("assign")
            if combo is None:
                continue
            current = combo.currentData() or "UNASSIGNED"
            combo.blockSignals(True)
            combo.clear()
            for val, lbl in SIDE_OPTIONS:
                combo.addItem(lbl, val)
            idx = next((j for j, (v, _) in enumerate(SIDE_OPTIONS)
                        if v == current), 0)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    def _build_alpaca_section(self, s):
        """V3.2.0 — slot-based key layout. The user enters up to 3 API
        key/secret pairs and assigns each to LONG / SHORT / DAY via a
        dropdown. On save we translate the slot→bot mapping into the
        underlying ALPACA_API_KEY_{LONG/SHORT/DAY} env vars so the
        existing bot code keeps working unmodified."""
        # V4.6.24 — section header with a refresh button so the user
        # can force the Assigned dropdowns to pick up new bots without
        # restarting the app.
        from PyQt6.QtWidgets import QPushButton as _PB
        _refresh = _PB("↻ Reload bots")
        _refresh.setObjectName("toolBtn")
        _refresh.setStyleSheet(
            f"QPushButton#toolBtn{{background:rgba(138,147,201,0.15);"
            f"color:{C['purple']};border:none;border-radius:4px;"
            f"padding:4px 10px;font-size:10px;font-weight:600;}}")
        _refresh.setToolTip(
            "Re-scan the bot registry so newly-created bots show up "
            "in each slot's Assigned dropdown.")
        _refresh.clicked.connect(self.refresh_alpaca_slot_assignments)
        s.add(SectionHeader("ALPACA  ·  API KEYS", C["green"],
                            controls=_refresh))
        keys_info = QLabel(
            "Enter up to 3 Alpaca paper API key / secret pairs and "
            "assign each to a built-in bot (LONG / SHORT / DAY). Each "
            "bot needs its own Alpaca paper account because Alpaca only "
            "allows one set of open positions per account.")
        keys_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        keys_info.setWordWrap(True)
        s.add(keys_info)

        cur = D.read_env_keys()
        existing_pairs = []
        # Use saved slot order so custom-bot assignments survive restarts.
        _saved_order = D.load_settings().get("alpaca_slot_order", [])
        if _saved_order:
            for side in _saved_order:
                k  = cur.get(f"ALPACA_API_KEY_{side}", "")
                sk = cur.get(f"ALPACA_SECRET_KEY_{side}", "")
                existing_pairs.append((side, k, sk))
        else:
            for side in ("LONG", "SHORT", "DAY"):
                k  = cur.get(f"ALPACA_API_KEY_{side}", "")
                sk = cur.get(f"ALPACA_SECRET_KEY_{side}", "")
                if k or sk:
                    existing_pairs.append((side, k, sk))
        # Pad with empty slots so the user always sees 3
        while len(existing_pairs) < 3:
            existing_pairs.append(("__none__", "", ""))

        self._alpaca_slot_edits: list[dict] = []
        form = QFrame()
        form.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        fl = QGridLayout(form)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.setHorizontalSpacing(10)
        fl.setVerticalSpacing(8)

        # V4.0.2 — slot dropdown includes the user's custom bots so an
        # Alpaca key can be assigned to e.g. a 'crypto' bot, not just
        # the three built-ins.
        SIDE_OPTIONS = [("__none__", "Unassigned"),
                        ("LONG",    "LONG bot"),
                        ("SHORT",   "SHORT bot"),
                        ("DAY",     "DAY bot")]
        try:
            reg = D.load_bot_registry()
            for c in reg.get("custom", []):
                slug = str(c.get("id", "")).upper()
                if slug and slug not in {s for s, _ in SIDE_OPTIONS}:
                    SIDE_OPTIONS.append(
                        (slug, c.get("label", slug) + " bot"))
        except Exception:
            pass

        for i, (assigned, k_val, s_val) in enumerate(existing_pairs):
            slot_lbl = QLabel(f"API SLOT {i+1}")
            slot_lbl.setStyleSheet(
                f"color:{C['muted']};font-size:9px;letter-spacing:2px;"
                f"font-weight:700;")
            fl.addWidget(slot_lbl, i*3 + 0, 0, 1, 4)

            key_ed = QLineEdit(k_val)
            key_ed.setEchoMode(QLineEdit.EchoMode.Password)
            key_ed.setPlaceholderText("API key")
            key_ed.setStyleSheet(
                f"background:{C['panel2']};color:{C['text']};"
                f"border:none;border-radius:4px;"
                f"padding:5px;font-family:'JetBrains Mono';font-size:11px;")
            fl.addWidget(QLabel("Key"), i*3 + 1, 0)
            fl.addWidget(key_ed,        i*3 + 1, 1, 1, 2)

            sec_ed = QLineEdit(s_val)
            sec_ed.setEchoMode(QLineEdit.EchoMode.Password)
            sec_ed.setPlaceholderText("Secret")
            sec_ed.setStyleSheet(key_ed.styleSheet())
            fl.addWidget(QLabel("Secret"), i*3 + 2, 0)
            fl.addWidget(sec_ed,            i*3 + 2, 1, 1, 2)

            assign = NoScrollComboBox()
            for val, lbl in SIDE_OPTIONS:
                assign.addItem(lbl, val)
            idx = next((j for j, (v, _) in enumerate(SIDE_OPTIONS)
                        if v == assigned), 0)
            assign.setCurrentIndex(idx)
            fl.addWidget(QLabel("Assigned"), i*3 + 1, 3)
            fl.addWidget(assign,              i*3 + 2, 3)

            self._alpaca_slot_edits.append(
                {"key": key_ed, "secret": sec_ed, "assign": assign})

        # V4.6.24 — remember the side-options template + each combo
        # so refresh_alpaca_slot_assignments() can rebuild contents
        # when a new bot gets added without forcing an app restart.
        self._alpaca_side_options_default = list(SIDE_OPTIONS)

        last_row = len(existing_pairs) * 3
        show = QCheckBox("Show keys")
        show.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        def _on_show(on):
            for slot in self._alpaca_slot_edits:
                mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
                slot["key"].setEchoMode(mode)
                slot["secret"].setEchoMode(mode)
        show.toggled.connect(_on_show)
        fl.addWidget(show, last_row, 0)

        save_btn = QPushButton("Save slots")
        save_btn.setObjectName("toolBtn")
        save_btn.clicked.connect(self._save_alpaca_slots)
        self.keys_msg = QLabel("")
        self.keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        brow = QHBoxLayout()
        brow.addWidget(save_btn)
        brow.addWidget(self.keys_msg)
        brow.addStretch()
        bwrap = QWidget(); bwrap.setLayout(brow)
        fl.addWidget(bwrap, last_row + 1, 0, 1, 4)
        s.add(form)

        # ── MANUAL TRADING ACCOUNT ───────────────────────────
        s.add(SectionHeader("MANUAL TRADING ACCOUNT", C["orange"]))
        manual_info = QLabel(
            "A separate Alpaca paper account dedicated to manual trading. "
            "This account is completely isolated from the LONG / SHORT / DAY "
            "bot accounts — manual trades never interfere with bot positions.\n\n"
            "Once saved, activate manual mode via the ✋  MANUAL button "
            "in the app header.")
        manual_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        manual_info.setWordWrap(True)
        s.add(manual_info)

        cur_m = D.read_env_keys()
        manual_frame = QFrame()
        manual_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;border-left:3px solid {C['orange']};")
        mfl = QGridLayout(manual_frame)
        mfl.setContentsMargins(16, 14, 16, 14)
        mfl.setHorizontalSpacing(10)
        mfl.setVerticalSpacing(8)

        _key_style = (
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:4px;"
            f"padding:5px;font-family:'JetBrains Mono';font-size:11px;")

        self._manual_key_edit = QLineEdit(cur_m.get("ALPACA_API_KEY_MANUAL", ""))
        self._manual_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._manual_key_edit.setPlaceholderText("Alpaca API key  (manual account)")
        self._manual_key_edit.setStyleSheet(_key_style)

        self._manual_secret_edit = QLineEdit(cur_m.get("ALPACA_SECRET_KEY_MANUAL", ""))
        self._manual_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._manual_secret_edit.setPlaceholderText("Alpaca secret  (manual account)")
        self._manual_secret_edit.setStyleSheet(_key_style)

        mfl.addWidget(QLabel("API key"), 0, 0)
        mfl.addWidget(self._manual_key_edit, 0, 1)
        mfl.addWidget(QLabel("Secret"), 1, 0)
        mfl.addWidget(self._manual_secret_edit, 1, 1)

        m_show = QCheckBox("Show")
        m_show.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        def _toggle_manual_show(on):
            _m = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            self._manual_key_edit.setEchoMode(_m)
            self._manual_secret_edit.setEchoMode(_m)
        m_show.toggled.connect(_toggle_manual_show)

        m_save = QPushButton("Save")
        m_save.setObjectName("toolBtn")
        m_save.clicked.connect(self._save_manual_keys)
        self._manual_keys_msg = QLabel("")
        self._manual_keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        m_brow = QHBoxLayout()
        m_brow.addWidget(m_show)
        m_brow.addSpacing(8)
        m_brow.addWidget(m_save)
        m_brow.addWidget(self._manual_keys_msg)
        m_brow.addStretch()
        m_bwrap = QWidget()
        m_bwrap.setLayout(m_brow)
        mfl.addWidget(m_bwrap, 2, 0, 1, 2)
        s.add(manual_frame)

    def _build_automation_section(self, s):
        """AUTOMATION — auto-start bots at US market open.
        Lives in its own method so it always appears regardless of
        which broker section is above it, and can't be skipped by an
        exception in the AI key section."""
        s.add(SectionHeader("AUTOMATION", C["green"]))

        auto_intro = QLabel(
            "Tick each bot you want APEX to auto-start at the US "
            "market open (and auto-stop at the close). Bots not "
            "ticked stay manual — press ▶ on their tab.")
        auto_intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        auto_intro.setWordWrap(True)
        s.add(auto_intro)

        # Container rebuilt by _rebuild_auto_schedule_row whenever
        # the active-bot registry changes.
        self._auto_sched_holder = QFrame()
        self._auto_sched_holder.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        self._auto_sched_layout = QHBoxLayout(self._auto_sched_holder)
        self._auto_sched_layout.setContentsMargins(14, 10, 14, 10)
        self._auto_sched_layout.setSpacing(18)
        s.add(self._auto_sched_holder)
        self._auto_sched_checks: dict[str, QCheckBox] = {}
        self._rebuild_auto_schedule_row()

        # Cloud-run row — immediately below so the two concepts live together
        cloud_intro = QLabel(
            "Run on Oracle 24/7  (laptop optional — APEX-server "
            "manages the process, with cloud-side market-clock scheduling):")
        cloud_intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        cloud_intro.setWordWrap(True)
        s.add(cloud_intro)

        self._cloud_holder = QFrame()
        self._cloud_holder.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        self._cloud_layout = QHBoxLayout(self._cloud_holder)
        self._cloud_layout.setContentsMargins(14, 10, 14, 10)
        self._cloud_layout.setSpacing(18)
        s.add(self._cloud_holder)
        self._cloud_checks: dict[str, QCheckBox] = {}
        self._rebuild_cloud_row()

        auto_note = QLabel(
            "Local bots run on this computer and stop if you quit "
            "APEX. Cloud bots run on the Oracle server — they keep "
            "trading with your laptop closed. The cloud's own "
            "scheduler starts/stops them based on the US market "
            "clock when their auto-schedule box (above) is also ticked.")
        auto_note.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        auto_note.setWordWrap(True)
        s.add(auto_note)

    def _build_ai_key_section(self, s):
        """AI provider API key entry.
        Only stores API keys here — provider/model/mode are chosen
        per-bot under each bot's LAST AI SIGNAL section."""
        from core.ai_client import PROVIDER_LABELS, PROVIDER_ENV_KEY

        s.add(SectionHeader("AI PROVIDER KEYS", C["yellow"]))

        # Hint text
        hint = QLabel(
            "Enter the API key for each AI provider you want to use. "
            "Groq (Llama) and Google Gemini are completely FREE. "
            "Choose which provider and model each bot uses from the bot's own tab.")
        hint.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        hint.setWordWrap(True)
        s.add(hint)

        cur = D.read_env_keys()
        saved_provider = cur.get("AI_PROVIDER", "anthropic").lower()
        if saved_provider not in PROVIDER_LABELS:
            saved_provider = "anthropic"

        ai_frame = QFrame()
        ai_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        afl = QGridLayout(ai_frame)
        afl.setContentsMargins(16, 14, 16, 14)
        afl.setSpacing(8)
        afl.setColumnStretch(1, 1)

        # Row 0: Provider selector (chooses which key to display/edit)
        prov_lbl = QLabel("Provider")
        prov_lbl.setStyleSheet(f"color:{C['text']};font-size:12px;")
        self._ai_provider_combo = NoScrollComboBox()
        self._ai_provider_combo.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:4px;padding:4px;")
        for key, label in PROVIDER_LABELS.items():
            self._ai_provider_combo.addItem(label, key)
            if key == saved_provider:
                self._ai_provider_combo.setCurrentIndex(
                    self._ai_provider_combo.count() - 1)
        afl.addWidget(prov_lbl, 0, 0)
        afl.addWidget(self._ai_provider_combo, 0, 1)

        # Row 1: API key field
        key_lbl = QLabel("API key")
        key_lbl.setStyleSheet(f"color:{C['text']};font-size:12px;")
        self._ai_key_edit = QLineEdit()
        self._ai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._ai_key_edit.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:4px;"
            f"padding:5px;font-family:'JetBrains Mono';font-size:11px;")
        afl.addWidget(key_lbl, 1, 0)
        afl.addWidget(self._ai_key_edit, 1, 1)

        # Row 2: show key checkbox + save button
        ai_show = QCheckBox("Show key")
        ai_show.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        ai_show.toggled.connect(lambda on:
            self._ai_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        ai_save = QPushButton("Save")
        ai_save.setObjectName("toolBtn")
        ai_save.clicked.connect(self._save_ai_key)
        afl.addWidget(ai_show, 2, 0)
        afl.addWidget(ai_save, 2, 1)

        # Row 3: status message
        self._ai_save_msg = QLabel("")
        self._ai_save_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        afl.addWidget(self._ai_save_msg, 3, 0, 1, 2)

        # Row 4: where-to-get-key link per provider
        self._ai_key_hint = QLabel("")
        self._ai_key_hint.setStyleSheet(
            f"color:{C['muted']};font-size:10px;")
        self._ai_key_hint.setOpenExternalLinks(True)
        afl.addWidget(self._ai_key_hint, 4, 0, 1, 2)

        s.add(ai_frame)

        # Wire up provider change → fill key field + hint
        def _on_provider_changed(_idx):
            prov = self._ai_provider_combo.currentData()
            env_var = PROVIDER_ENV_KEY.get(prov, "ANTHROPIC_API_KEY")
            self._ai_key_edit.setText(cur.get(env_var, ""))
            _HINTS = {
                "anthropic": "Get key: <a href='https://console.anthropic.com'>console.anthropic.com</a> (paid)",
                "google":    "Get FREE key: <a href='https://aistudio.google.com'>aistudio.google.com</a> — 1 500 req/day, resets daily",
                "xai":       "Get key: <a href='https://console.x.ai'>console.x.ai</a> ($25 free credits/mo for new accounts)",
                "groq":      "Get FREE key: <a href='https://console.groq.com'>console.groq.com</a> — 14 400 req/day, resets daily",
            }
            self._ai_key_hint.setText(_HINTS.get(prov, ""))

        self._ai_provider_combo.currentIndexChanged.connect(_on_provider_changed)
        # Trigger once to fill key from saved state
        _on_provider_changed(0)

    def _save_alpaca_slots(self):
        """Translate slot dropdown assignments into ALPACA_* env vars.
        Steps: (1) collect new mappings, (2) wipe all existing Alpaca
        keys, (3) write the new ones, (4) persist slot order.
        Full error handling so failures surface to the user instead of
        silently succeeding with an empty .env."""
        from PyQt6.QtCore import QTimer as _QT
        try:
            new_writes: dict[str, str] = {}
            seen_sides: set[str] = set()
            missing: list[str] = []

            for slot in self._alpaca_slot_edits:
                side = slot["assign"].currentData()
                if not side or side == "__none__":
                    continue
                if side in seen_sides:
                    self.keys_msg.setText(
                        f"⚠  Multiple slots assigned to {side} — "
                        f"only the first kept.")
                    self.keys_msg.setStyleSheet(
                        f"color:{C['orange']};font-size:11px;")
                    continue
                seen_sides.add(side)
                key_val    = slot["key"].text().strip()
                secret_val = slot["secret"].text().strip()
                if not key_val or not secret_val:
                    missing.append(side)
                    continue
                new_writes[f"ALPACA_API_KEY_{side}"]    = key_val
                new_writes[f"ALPACA_SECRET_KEY_{side}"] = secret_val

            if missing:
                self.keys_msg.setText(
                    f"⚠  Slot(s) for {', '.join(missing)} have an empty "
                    f"key or secret — fill both fields before saving.")
                self.keys_msg.setStyleSheet(
                    f"color:{C['orange']};font-size:11px;")
                # Still continue to save any complete slots

            # 1. Wipe ALL existing Alpaca slot entries for any known side
            # so a slot re-pointed to 'Unassigned' doesn't leave a stale key.
            candidate_sides = ["LONG", "SHORT", "DAY"]
            try:
                reg = D.load_bot_registry()
                candidate_sides += [str(c.get("id", "")).upper()
                                     for c in reg.get("custom", [])]
            except Exception:
                pass
            to_delete = [f"{p}{s}"
                         for s in candidate_sides
                         for p in ("ALPACA_API_KEY_", "ALPACA_SECRET_KEY_")]
            D.delete_env_keys(to_delete)

            # 2. Write the new assignments (skips empty — already filtered above)
            if new_writes:
                D.write_env_keys(new_writes)

            # 3. Persist slot order so assignments survive restarts
            import json as _json
            _order = [slot["assign"].currentData() for slot in self._alpaca_slot_edits]
            _s = D.load_settings()
            _s["alpaca_slot_order"] = _order
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as _f:
                _json.dump(_s, _f, indent=2)

            # 4. Verify write succeeded by reading back
            saved = D.read_env_keys()
            saved_sides = [s for s in candidate_sides
                           if saved.get(f"ALPACA_API_KEY_{s}", "").strip()
                           and saved.get(f"ALPACA_SECRET_KEY_{s}", "").strip()]

            if saved_sides and not missing:
                self.keys_msg.setText(
                    f"✓ Keys saved for: {', '.join(saved_sides)}  "
                    f"— syncing to APEX server…")
                self.keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
            elif saved_sides:
                self.keys_msg.setText(
                    f"✓ Saved {', '.join(saved_sides)}  "
                    f"(⚠ incomplete slots above were skipped) — syncing…")
                self.keys_msg.setStyleSheet(f"color:{C['orange']};font-size:11px;")
            elif not new_writes:
                self.keys_msg.setText(
                    "⚠  Nothing saved — assign a bot and fill key + secret first.")
                self.keys_msg.setStyleSheet(f"color:{C['orange']};font-size:11px;")
                _QT.singleShot(6000, lambda: self.keys_msg.setText(""))
                return
            else:
                self.keys_msg.setText(
                    f"⚠  Save may have failed — keys not found in .env  "
                    f"({D.ENV_FILE})")
                self.keys_msg.setStyleSheet(f"color:{C['red']};font-size:11px;")
                _QT.singleShot(6000, lambda: self.keys_msg.setText(""))
                return

            # V4.6.1 — auto-sync to APEX server so cloud bots immediately
            # see the new keys (previously the user had to click "Sync keys
            # to APEX server" separately, which was easy to miss and caused
            # the "MUST ASSIGN API KEY IN TOOLS" error from cloud bot_runner).
            self._auto_sync_after_slot_save(saved_sides)

        except Exception as e:
            self.keys_msg.setText(f"✗ Save failed: {e}")
            self.keys_msg.setStyleSheet(f"color:{C['red']};font-size:11px;")
            import traceback
            print(f"[save-alpaca-slots] {traceback.format_exc()}")

    def _auto_sync_after_slot_save(self, saved_sides: list[str]):
        """V4.6.1 — push the freshly-saved keys to the APEX server in a
        background thread so cloud bots get them without a manual sync
        click. Silent no-op if the user isn't signed in (the local save
        already succeeded, that's the important part)."""
        from PyQt6.QtCore import QThread, pyqtSignal, QTimer as _QT
        try:
            from ui.login import load_auth, load_server_url
        except Exception:
            self.keys_msg.setText(
                f"✓ Saved locally for: {', '.join(saved_sides)} "
                f"(server sync skipped — login module unavailable)")
            self.keys_msg.setStyleSheet(f"color:{C['orange']};font-size:11px;")
            _QT.singleShot(8000, lambda: self.keys_msg.setText(""))
            return

        stored = load_auth() or {}
        token  = stored.get("token")
        if not token:
            self.keys_msg.setText(
                f"✓ Saved locally for: {', '.join(saved_sides)}  "
                f"(not signed in — cloud bots will get keys after you sign in & sync)")
            self.keys_msg.setStyleSheet(f"color:{C['orange']};font-size:11px;")
            _QT.singleShot(9000, lambda: self.keys_msg.setText(""))
            return

        all_keys = D.read_env_keys()
        payload  = {k: v for k, v in all_keys.items() if v}
        # V4.6.8 — include the Alpaca paper/live mode so the cloud
        # bot_runner can construct TradingClient with the right paper
        # flag. Default 'paper' for safety.
        try:
            _s = D.load_settings()
            payload["APEX_ALPACA_MODE"] = _s.get("alpaca_mode", "paper")
            # V4.6.27 — sync per-bot auto-schedule flags so the cloud
            # scheduler can auto-start bots at market open even when
            # APEX is closed. Reads the v7.1.10 'auto_schedule_<SIDE>'
            # keys and exports as APEX_AUTO_SCHEDULE_<SIDE>=1/0.
            for k in list(_s.keys()):
                if k.startswith("auto_schedule_"):
                    side = k[len("auto_schedule_"):].upper()
                    payload[f"APEX_AUTO_SCHEDULE_{side}"] = (
                        "1" if _s.get(k) else "0")
            # V4.6.48 — sync each bot's 'Minimum confidence to trade' so the
            # cloud framework's confidence gate honors the slider. Stored as
            # settings[<SIDE>]["min_confidence"]; exported APEX_MIN_CONFIDENCE_<SIDE>.
            for k, v in list(_s.items()):
                if isinstance(v, dict) and "min_confidence" in v:
                    try:
                        payload[f"APEX_MIN_CONFIDENCE_{k.upper()}"] = str(float(v["min_confidence"]))
                    except (TypeError, ValueError):
                        pass
            # V4.6.40 — PUT /credentials REPLACES the whole server blob, so
            # carry the IBKR cloud-login fields (stored under ibkr_<mode>)
            # along with every Alpaca sync or they'd be wiped server-side.
            payload.update(self._ibkr_cloud_credentials(_s))
        except Exception:
            payload["APEX_ALPACA_MODE"] = "paper"
        if not payload:
            return  # nothing to send (shouldn't happen — we just verified saves)

        server_url = load_server_url()

        class _AutoSyncWorker(QThread):
            done = pyqtSignal(bool, str, list)

            def __init__(self, url, tok, body, sides):
                super().__init__()
                self.url, self.tok, self.body, self.sides = url, tok, body, sides

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
                        self.done.emit(True,
                            f"{n} key(s) encrypted on server", self.sides)
                    else:
                        self.done.emit(False,
                            f"server error {r.status_code}", self.sides)
                except Exception as ex:
                    self.done.emit(False, str(ex), self.sides)

        # Keep a ref on self so the QThread isn't garbage-collected mid-flight
        self._slot_sync_worker = _AutoSyncWorker(
            server_url, token, payload, saved_sides)
        self._slot_sync_worker.done.connect(self._on_auto_sync_done)
        self._slot_sync_worker.start()

    def _on_auto_sync_done(self, ok: bool, msg: str, saved_sides: list):
        from PyQt6.QtCore import QTimer as _QT
        sides_str = ", ".join(saved_sides) if saved_sides else "keys"
        if ok:
            self.keys_msg.setText(
                f"✓ {sides_str} saved locally AND synced to APEX server "
                f"({msg}) — cloud bots ready")
            self.keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
            _QT.singleShot(8000, lambda: self.keys_msg.setText(""))
        else:
            self.keys_msg.setText(
                f"✓ {sides_str} saved locally  ⚠ server sync failed: {msg}. "
                f"Click 'Sync keys to APEX server' to retry.")
            self.keys_msg.setStyleSheet(f"color:{C['orange']};font-size:11px;")
            _QT.singleShot(12000, lambda: self.keys_msg.setText(""))

    def _save_manual_keys(self):
        from PyQt6.QtCore import QTimer as _QT
        D.write_env_keys({
            "ALPACA_API_KEY_MANUAL":    self._manual_key_edit.text().strip(),
            "ALPACA_SECRET_KEY_MANUAL": self._manual_secret_edit.text().strip(),
        })
        self._manual_keys_msg.setText("✓ Manual keys saved")
        self._manual_keys_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        _QT.singleShot(4000, lambda: self._manual_keys_msg.setText(""))

    def _save_ai_key(self):
        from PyQt6.QtCore import QTimer as _QT
        from core.ai_client import PROVIDER_ENV_KEY
        prov      = self._ai_provider_combo.currentData() or "anthropic"
        key_value = self._ai_key_edit.text().strip()
        env_var   = PROVIDER_ENV_KEY.get(prov, "ANTHROPIC_API_KEY")
        if key_value:
            D.write_env_keys({env_var: key_value})
        prov_label = self._ai_provider_combo.currentText()
        self._ai_save_msg.setText(
            f"✓ {prov_label} key saved — sync to Oracle to use it on cloud bots")
        self._ai_save_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        _QT.singleShot(5000, lambda: self._ai_save_msg.setText(""))

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
            f"background:{C['panel']};border:none;"
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
            f"background:{C['panel']};border:none;"
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

        # ── V4.0.1 — STAY UPDATED  (bottom of Tools) ───────────
        # User-requested prominent CTA at the bottom of Tools because
        # the auto-update banner sometimes doesn't fire (transient
        # network blip, GitHub API rate-limit, etc.).
        s.add(SectionHeader("STAY UPDATED", C["green"]))
        stay_frame = QFrame()
        stay_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:10px;border-left:3px solid {C['green']};")
        sv = QVBoxLayout(stay_frame)
        sv.setContentsMargins(20, 16, 20, 16)
        sv.setSpacing(8)
        stay_lbl = QLabel(
            "BAPTOU checks for new releases on startup and once an "
            "hour, but if the auto-banner hasn't fired and you want "
            "to confirm you're on the latest, force a check here.")
        stay_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.6;")
        stay_lbl.setWordWrap(True)
        sv.addWidget(stay_lbl)
        big_check = QPushButton("⟳   CHECK FOR UPDATES NOW")
        big_check.setObjectName("addBotBtn")
        big_check.setFixedHeight(46)
        big_check.setMinimumWidth(280)
        big_check.clicked.connect(self._check_update_now)
        self._stay_msg = QLabel("")
        self._stay_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        row = QHBoxLayout()
        row.addWidget(big_check)
        row.addWidget(self._stay_msg)
        row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        sv.addWidget(rw)
        s.add(stay_frame)

        s.add_stretch()

    def refresh(self):
        # V7.1.10 — keep the per-bot auto-schedule row in sync with the
        # active bot registry. Cheap (just QCheckBox creation).
        # V7.1.13 — same for the cloud-execution row.
        try:
            self._rebuild_auto_schedule_row()
        except Exception:
            pass
        try:
            self._rebuild_cloud_row()
        except Exception:
            pass

    def _toggle_auto(self, on: bool):
        # Legacy global toggle, kept so any older signal connection
        # still works. V7.1.10 routes new clicks to set_auto_schedule_for.
        try:
            D.set_auto_schedule(bool(on))
        except Exception:
            pass

    # ── V7.1.10: per-bot auto-schedule row ─────────────────

    def _rebuild_auto_schedule_row(self):
        """Tear down and rebuild the AUTOMATION checkbox row to mirror
        the current active-bot registry. Silenced bots are skipped (a
        silenced bot stays manual until the user un-silences it)."""
        # Clear current checkboxes
        layout = self._auto_sched_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._auto_sched_checks.clear()

        try:
            reg = D.load_bot_registry()
        except Exception:
            reg = {}
        active   = reg.get("active",   ["LONG", "SHORT", "DAY"])
        silenced = set(reg.get("silenced", []))
        customs  = {c["id"]: c for c in reg.get("custom", [])
                    if isinstance(c, dict)}

        builtin_labels = {"LONG":  "▲ LONG",
                          "SHORT": "▼ SHORT",
                          "DAY":   "◆ DAY"}
        # V4.6.18 — built-ins are all US-equity, market-hours bound.
        # Custom bots declare asset_type in their META block; we read
        # it here so 24/7 markets (crypto) skip the auto-start toggle.
        builtin_asset_type = {"LONG":  "stocks", "SHORT": "stocks",
                              "DAY":   "stocks"}
        always_on_asset_types = {"crypto"}  # 24/7 markets — no schedule
        any_added = False
        always_on_labels = []  # collected to render a footnote
        for sid in active:
            if sid in silenced:
                continue
            label = builtin_labels.get(sid)
            if label is None:
                label = customs.get(sid, {}).get("label", sid).upper()
            # Determine asset_type for this bot
            atype = builtin_asset_type.get(sid)
            if atype is None:
                # Custom bot — parse META.asset_type from its .py
                script_path = customs.get(sid, {}).get("script", "")
                if script_path:
                    try:
                        from core.bot_meta import parse_meta
                        from pathlib import Path as _P
                        if _P(script_path).exists():
                            src = open(script_path, "r",
                                       encoding="utf-8").read()
                            atype = (parse_meta(src) or {}).get(
                                "asset_type", "").lower()
                    except Exception:
                        atype = ""
                atype = atype or "stocks"  # safe default
            # 24/7 asset → no auto-start needed
            if atype in always_on_asset_types:
                always_on_labels.append(f"{label} (24/7 {atype})")
                continue
            cb = QCheckBox(label)
            cb.setStyleSheet(
                f"color:{C['text']};font-size:11px;letter-spacing:1px;")
            cb.setToolTip(
                f"Auto-start {label} at the next US market open "
                f"(asset_type={atype}).")
            try:
                cb.setChecked(D.get_auto_schedule_for(sid))
            except Exception:
                pass
            cb.toggled.connect(
                lambda on, s=sid: self._on_per_bot_schedule_toggled(s, on))
            layout.addWidget(cb)
            self._auto_sched_checks[sid] = cb
            any_added = True

        if not any_added and not always_on_labels:
            empty = QLabel("(no active bots — add some from MORE BOTS)")
            empty.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            layout.addWidget(empty)
        layout.addStretch()
        # V4.6.18 — show a footnote for 24/7 bots that intentionally
        # don't get the schedule checkbox.
        if always_on_labels:
            note = QLabel("  ·  always-on (no schedule needed):  "
                          + ", ".join(always_on_labels))
            note.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            note.setWordWrap(True)
            layout.addWidget(note)

    def _on_per_bot_schedule_toggled(self, side: str, on: bool):
        try:
            D.set_auto_schedule_for(side, bool(on))
        except Exception as e:
            print(f"[schedule] toggle {side}: {e}")
        # V7.1.13: schedule changes affect the cloud schedule too —
        # only the intersection cloud ∩ scheduled gets pushed to server.
        self._push_schedule_to_server()
        # V4.6.27 — also re-sync the credentials blob so the new flag
        # propagates to the server-side auto_scheduler which actually
        # fires the bot start at market open (even with APEX closed).
        try:
            self._sync_keys_to_server()
        except Exception as e:
            print(f"[schedule] credentials re-sync after toggle "
                  f"{side}: {e}")

    # ── V7.1.13: cloud toggles ─────────────────────────────────

    def _rebuild_cloud_row(self):
        """Tear down + rebuild the Run-on-Oracle checkbox row to mirror
        the current active-bot set. Silenced bots are excluded."""
        layout = self._cloud_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cloud_checks.clear()

        try:
            reg = D.load_bot_registry()
        except Exception:
            reg = {}
        active   = reg.get("active",   ["LONG", "SHORT", "DAY"])
        silenced = set(reg.get("silenced", []))
        customs  = {c["id"]: c for c in reg.get("custom", [])
                    if isinstance(c, dict)}

        builtin_labels = {"LONG":  "▲ LONG",
                          "SHORT": "▼ SHORT",
                          "DAY":   "◆ DAY"}
        any_added = False
        cloud_set = {s.upper() for s in D.get_cloud_bots()}
        for sid in active:
            if sid in silenced:
                continue
            label = builtin_labels.get(sid) or customs.get(sid, {}).get("label", sid).upper()
            cb = QCheckBox(label)
            cb.setStyleSheet(
                f"color:{C['text']};font-size:11px;letter-spacing:1px;")
            cb.setChecked(sid.upper() in cloud_set)
            cb.toggled.connect(
                lambda on, s=sid: self._on_cloud_toggled(s, on))
            layout.addWidget(cb)
            self._cloud_checks[sid] = cb
            any_added = True

        if not any_added:
            empty = QLabel("(no active bots)")
            empty.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            layout.addWidget(empty)
        layout.addStretch()

    def _on_cloud_toggled(self, side: str, on: bool):
        try:
            if on:
                D.add_cloud_bot(side)
            else:
                D.remove_cloud_bot(side)
        except Exception as e:
            print(f"[cloud] toggle {side}: {e}")
        self._push_schedule_to_server()

    def _push_schedule_to_server(self):
        """Push (cloud ∩ scheduled) to the server's /schedule. Bots
        that are cloud-only-no-schedule or scheduled-only-no-cloud
        are NOT in the cloud scheduler — they're handled either by
        manual ▶ (cloud only) or the desktop's _tick_schedule (local
        scheduled). The intersection is what the server should
        auto-manage."""
        try:
            cloud = set(D.get_cloud_bots())
            sched = set(D.get_auto_schedule_active_bots())
            payload = sorted(cloud & sched)
        except Exception as e:
            print(f"[cloud] push prep failed: {e}")
            return

        from PyQt6.QtCore import QThread, pyqtSignal as _Sig
        try:
            from ui.login import load_auth, load_server_url
        except Exception:
            return
        token = (load_auth() or {}).get("token")
        if not token:
            return                # no auth, server push impossible
        url = f"{load_server_url()}/schedule"

        class _W(QThread):
            done = _Sig(bool, str)
            def __init__(self, u, t, body):
                super().__init__()
                self.u, self.t, self.body = u, t, body
            def run(self):
                import requests
                try:
                    r = requests.put(
                        self.u, headers={"Authorization": f"Bearer {self.t}"},
                        json={"bots": self.body}, timeout=10)
                    self.done.emit(r.ok, r.text)
                except Exception as e:
                    self.done.emit(False, str(e))

        worker = _W(url, token, payload)
        worker.done.connect(
            lambda ok, t: print(f"[cloud] push -> {payload} : ok={ok}"))
        worker.start()
        # Hold ref so it isn't GC'd before run completes
        self._push_workers = getattr(self, "_push_workers", [])
        self._push_workers.append(worker)
        self._push_workers = [w for w in self._push_workers if w.isRunning()]

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

        # Build the payload from .env (includes all built-in slots AND custom
        # bot slots like ALPACA_API_KEY_CRYPTO that _key_edits never had).
        all_keys = D.read_env_keys()
        payload = {k: v for k, v in all_keys.items() if v}
        # V4.6.8 — include paper/live toggle alongside the keys
        try:
            _s = D.load_settings()
            payload["APEX_ALPACA_MODE"] = _s.get("alpaca_mode", "paper")
            # V4.6.27 — sync per-bot auto-schedule flags so the cloud
            # scheduler can auto-start bots at market open even when
            # APEX is closed. Reads the v7.1.10 'auto_schedule_<SIDE>'
            # keys and exports as APEX_AUTO_SCHEDULE_<SIDE>=1/0.
            for k in list(_s.keys()):
                if k.startswith("auto_schedule_"):
                    side = k[len("auto_schedule_"):].upper()
                    payload[f"APEX_AUTO_SCHEDULE_{side}"] = (
                        "1" if _s.get(k) else "0")
        except Exception:
            payload["APEX_ALPACA_MODE"] = "paper"
        if not payload:
            self._sync_msg.setText("No keys saved yet — save your keys first.")
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

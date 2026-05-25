"""
APEX Universe Manager Tab
Run universe_manager.py (like a bot) and see the per-bot ticker breakdown.

V4.6.2 — side-by-side layout. Instead of one long table that mixes every
bot's tickers, each universe gets its own card laid out in a 2-wide grid:
the first universe goes top-left, the second top-right, the third
bottom-left, and so on. With LONG + a custom CRYPTO bot you see stocks
on the left, crypto on the right — no scrolling between them.
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton,
)
from PyQt6.QtCore import Qt

from ui.styles  import COLORS
from ui.widgets import (
    SectionHeader, BotProcessWidget, ScrollContent, DataTable,
    NoScrollComboBox,
)
import core.data as D

C = COLORS


class UniverseCard(QFrame):
    """A single universe-file card: header (side name + count) + table."""

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        self.setStyleSheet(
            f"QFrame{{background:{C['panel']};border:none;"
            f"border-radius:8px;}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        head = QHBoxLayout()
        self.title = QLabel(f"{side}  UNIVERSE")
        self.title.setStyleSheet(
            f"color:{C['text']};font-weight:700;font-size:12px;"
            f"letter-spacing:1.5px;border:none;")
        self.count_lbl = QLabel("0 tickers")
        self.count_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:11px;border:none;")
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.count_lbl)
        head_w = QWidget()
        head_w.setLayout(head)
        v.addWidget(head_w)

        self.table = DataTable()
        self.table.setMinimumHeight(280)
        v.addWidget(self.table)

    def load(self, rows: list):
        """rows: list of {Ticker, Note} dicts."""
        self.count_lbl.setText(f"{len(rows)} tickers")
        # Strip the 'Bot' column from the per-card table — it's redundant
        # because the card header already names the bot.
        flat = [{"Ticker": r["Ticker"], "Note": r["Note"] or "—"}
                for r in rows]
        self.table.load(flat, ["Ticker", "Note"])


class UniverseTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._cards: dict[str, UniverseCard] = {}
        self._grid: QGridLayout | None = None
        self._build()

    def _build(self):
        s = self.scroll

        s.add(SectionHeader("UNIVERSE MANAGER", C["purple"]))
        info = QLabel(
            "Discovers market movers, scores them with Claude, and rewrites "
            "each bot's ticker universe. Click RUN to update now — output "
            "streams below just like a bot.")
        info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        info.setWordWrap(True)
        s.add(info)

        # V4.6.9 — Universe-script picker. The built-in universe_manager.py
        # is always available; every custom universe generator that the
        # user has created via Make Bot (kind=Universe) shows up too.
        # Selecting a different one swaps the script the BotProcessWidget
        # below runs.
        pick_row = QHBoxLayout()
        pick_lbl = QLabel("Run which universe script:")
        pick_lbl.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        # V4.6.11 — create _desc_lbl BEFORE populating the combo so
        # the chain _populate_script_combo -> _on_script_changed ->
        # self._desc_lbl.setText(...) doesn't AttributeError on first
        # paint.
        self._desc_lbl = QLabel("")
        self._desc_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:10px;")
        self._desc_lbl.setWordWrap(True)
        self._script_combo = NoScrollComboBox()
        self._script_combo.setMinimumWidth(280)
        self._populate_script_combo()
        self._script_combo.currentIndexChanged.connect(
            self._on_script_changed)
        # Refresh button to rescan the universe_scripts/ folder
        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("Rescan universe scripts")
        refresh_btn.setFixedWidth(28)
        refresh_btn.clicked.connect(self._populate_script_combo)
        pick_row.addWidget(pick_lbl)
        pick_row.addWidget(self._script_combo)
        pick_row.addWidget(refresh_btn)
        pick_row.addStretch()
        pw = QWidget()
        pw.setLayout(pick_row)
        s.add(pw)
        s.add(self._desc_lbl)

        s.add(SectionHeader("RUN", C["purple"]))
        # The runner script can be swapped at runtime. Default to the
        # built-in universe_manager.py path.
        self.runner = BotProcessWidget("UNIVERSE", D.UNIVERSE_SCRIPT)
        s.add(self.runner)

        s.add(SectionHeader("UNIVERSE BREAKDOWN", C["purple"]))
        layout_hint = QLabel(
            "One card per registered bot universe — your stocks universe "
            "sits on the left, crypto (or any custom bot's universe) on "
            "the right, additional universes stack underneath in the same "
            "2-wide grid.")
        layout_hint.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        layout_hint.setWordWrap(True)
        s.add(layout_hint)

        # ── 2-wide grid container ───────────────────────────────
        grid_wrap = QWidget()
        self._grid = QGridLayout(grid_wrap)
        self._grid.setContentsMargins(0, 8, 0, 0)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(12)
        # Make both columns share width 50/50
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        s.add(grid_wrap)
        s.add_stretch()

        self.refresh()

    # ── V4.6.9 universe-script picker ───────────────────────────

    def _populate_script_combo(self):
        """Build / rebuild the dropdown contents. Always include the
        built-in universe_manager.py at the top, then list every
        registered custom universe generator from settings."""
        self._script_combo.blockSignals(True)
        self._script_combo.clear()
        # Built-in
        self._script_combo.addItem(
            "Built-in: universe_manager.py  (default)",
            {"path": str(D.UNIVERSE_SCRIPT),
             "label": "Built-in universe_manager",
             "description": "Default APEX universe scanner — picks "
                            "movers across LONG / SHORT / DAY pools."})
        # Custom universe scripts the user has generated via Make Bot
        try:
            s = D.load_settings()
            scripts = s.get("universe_scripts", []) or []
        except Exception:
            scripts = []
        for entry in scripts:
            if not isinstance(entry, dict):
                continue
            path = entry.get("script", "")
            if not path or not Path(path).exists():
                continue
            label  = entry.get("label", entry.get("id", "(unnamed)"))
            target = entry.get("target", "")
            desc   = entry.get("description", "")
            ui_label = f"{label}  →  rewrites {target}" if target else label
            self._script_combo.addItem(ui_label, {
                "path":        path,
                "label":       label,
                "description": desc or "(no description in META)",
            })
        # Restore last-selected script if it still exists
        try:
            last = D.load_settings().get("universe_script_selection", "")
            if last:
                for i in range(self._script_combo.count()):
                    if (self._script_combo.itemData(i) or {}).get(
                            "path") == last:
                        self._script_combo.setCurrentIndex(i)
                        break
        except Exception:
            pass
        self._script_combo.blockSignals(False)
        self._on_script_changed()  # update runner + desc to match selection

    def _on_script_changed(self):
        data = self._script_combo.currentData() or {}
        path = data.get("path", str(D.UNIVERSE_SCRIPT))
        # Swap the runner's script pointer in-place. BotProcessWidget
        # stores it as `script_path`; also update the visible script
        # filename label inside the runner so the user can confirm at
        # a glance which script will execute on next RUN click.
        try:
            self.runner.script_path = Path(path)
        except Exception as e:
            print(f"[universe-tab] could not swap runner script_path: {e}")
        # Update the in-widget script filename label (if present)
        try:
            for child in self.runner.findChildren(QLabel):
                txt = child.text()
                # The label is set to the .name of the script (e.g.
                # "universe_manager.py"). Replace any label whose text
                # looks like a .py filename.
                if txt.endswith(".py"):
                    child.setText(Path(path).name)
                    break
        except Exception:
            pass
        # Persist selection so it sticks across app restarts
        try:
            s = D.load_settings()
            s["universe_script_selection"] = str(path)
            import json as _j
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _j.dump(s, f, indent=2)
        except Exception:
            pass
        # Update the description label (defensive: may not exist
        # yet on first call from inside _build before the label is
        # added to the layout — _build's order is fixed but a future
        # refactor could reintroduce the gap).
        try:
            self._desc_lbl.setText(
                f"▸ {data.get('description', '')}")
        except (AttributeError, RuntimeError):
            pass

    def refresh(self):
        # Refresh both the script combo (new universe bots may have
        # been created since last paint) and the breakdown grid.
        try:
            self._populate_script_combo()
        except Exception as e:
            print(f"[universe-tab] script combo refresh failed: {e}")
        bd = D.read_universe_breakdown()
        sides = bd.get("sides", []) or ["LONG", "SHORT", "DAY"]

        # ── Rebuild the grid if the set of sides changed (e.g. user
        # added a custom bot since last refresh) ───────────────────
        if set(sides) != set(self._cards.keys()):
            self._rebuild_grid(sides)

        for side in sides:
            card = self._cards.get(side)
            if card is None:
                continue
            card.load(bd.get(side, []))

    def _rebuild_grid(self, sides: list[str]):
        """Tear down + re-add UniverseCards into the 2-wide grid in
        the order returned by read_universe_breakdown() (built-ins
        first, custom bots alphabetical after)."""
        # Remove old cards from the layout & memory
        for card in list(self._cards.values()):
            self._grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        # Re-insert in row-major order, 2 per row
        for i, side in enumerate(sides):
            row, col = divmod(i, 2)
            card = UniverseCard(side)
            self._grid.addWidget(card, row, col)
            self._cards[side] = card

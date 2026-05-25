"""
APEX Universe Manager Tab
Run universe_manager.py (like a bot) and see the per-bot ticker breakdown.

V4.6.2 — side-by-side layout. Instead of one long table that mixes every
bot's tickers, each universe gets its own card laid out in a 2-wide grid:
the first universe goes top-left, the second top-right, the third
bottom-left, and so on. With LONG + a custom CRYPTO bot you see stocks
on the left, crypto on the right — no scrolling between them.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
)
from PyQt6.QtCore import Qt

from ui.styles  import COLORS
from ui.widgets import (
    SectionHeader, BotProcessWidget, ScrollContent, DataTable,
)
import core.data as D

C = COLORS


class UniverseCard(QFrame):
    """A single universe-file card: header (side name + count) + table."""

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        self.setStyleSheet(
            f"QFrame{{background:{C['panel']};border:1px solid {C['border']};"
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

        s.add(SectionHeader("RUN", C["purple"]))
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

    def refresh(self):
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

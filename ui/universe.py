"""
APEX Universe Manager Tab
Run universe_manager.py (like a bot) and see the per-bot ticker breakdown.
"""

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt

from ui.styles  import COLORS
from ui.widgets import (
    SectionHeader, BotProcessWidget, ScrollContent, DataTable, MetricCard,
)
import core.data as D

C = COLORS


class UniverseTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
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
        row = QHBoxLayout()
        self.count_cards = {}
        for side in ["LONG", "SHORT", "DAY"]:
            card = MetricCard(f"{side} TICKERS", "—")
            self.count_cards[side] = card
            row.addWidget(card)
        row.addStretch()
        rw = QWidget()
        rw.setLayout(row)
        s.add(rw)

        self.table = DataTable()
        self.table.setMinimumHeight(440)
        s.add(self.table)
        s.add_stretch()

        self.refresh()

    def refresh(self):
        bd = D.read_universe_breakdown()
        for side, card in self.count_cards.items():
            card.update_value(str(bd["counts"].get(side, 0)))
        rows = []
        for side in ["LONG", "SHORT", "DAY"]:
            for r in bd.get(side, []):
                rows.append({"Bot": r["Bot"],
                             "Ticker": r["Ticker"],
                             "Note": r["Note"] or "—"})
        self.table.load(rows, ["Bot", "Ticker", "Note"])

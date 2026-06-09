"""
APEX Universe Tab
View the ticker universes — both YOUR BOTS' assigned universes and the full
catalogue of curated PUBLIC universes the APEX server regenerates weekly.

V4.6.76 — removed the "run a universe script" control. Universes are no
longer something the user runs locally: the server's universe_factory
regenerates ~34 themed public universes every Monday, and each bot picks one
(or its own tickers) at creation. This tab is now read-only: it shows what
each of your bots trades, plus every public universe available to assign.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ui.styles  import COLORS
from ui.widgets import SectionHeader, ScrollContent, DataTable
import core.data as D

C = COLORS


def _fetch_universe_rows(name: str) -> list:
    """[(ticker, note)] for one public universe via GET /universes/{name}.
    The note carries the score + reason the factory wrote."""
    try:
        from ui.make_bot_tab import _server_creds
        import requests
        tok, url = _server_creds()
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = requests.get(f"{url}/universes/{name}", headers=headers, timeout=8)
        if not r.ok:
            return []
        txt = r.text
        try:
            j = r.json()
            if isinstance(j, dict):
                txt = j.get("content", j.get("text", txt))
        except Exception:
            pass
        rows = []
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "#" in ln:
                sym, note = ln.split("#", 1)
                sym = (sym.strip().split() or [""])[0]
                if sym:
                    rows.append((sym, note.strip()))
            else:
                rows.append(((ln.split() or [""])[0], ""))
        return rows
    except Exception as e:
        print(f"[universe-tab] rows fetch '{name}': {e}")
        return []


def _show_universe_detail(parent, title: str, rows: list, blurb: str = ""):
    """Modal dialog showing a universe's full content (Ticker + the score /
    reason the factory recorded). Opened by clicking any universe card."""
    from PyQt6.QtWidgets import QDialog, QVBoxLayout as _V, QLabel as _L
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(440, 480)
    dlg.setStyleSheet(f"background:{C.get('bg', '#0c0f16')};")
    v = _V(dlg)
    v.setContentsMargins(16, 16, 16, 16)
    v.setSpacing(8)
    h = _L(f"{title}   ·   {len(rows)} tickers")
    h.setStyleSheet(f"color:{C['text']};font-weight:800;font-size:14px;")
    v.addWidget(h)
    if blurb:
        b = _L(blurb)
        b.setWordWrap(True)
        b.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        v.addWidget(b)
    tbl = DataTable()
    tbl.load([{"Ticker": t, "Score / reason": (n or "—")} for t, n in rows],
             ["Ticker", "Score / reason"])
    v.addWidget(tbl)
    dlg.exec()


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
        flat = [{"Ticker": r["Ticker"], "Note": r["Note"] or "—"}
                for r in rows]
        self.table.load(flat, ["Ticker", "Note"])


class PublicUniverseCard(QFrame):
    """Compact card for one server-side public universe: name, count, blurb,
    and the ticker list (wrapped). Light-weight so all ~34 render fast."""

    def __init__(self, name: str, total: int, blurb: str, rows: list,
                 parent=None):
        super().__init__(parent)
        self._name = name
        self._rows = rows or []
        self._blurb = blurb
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QFrame{{background:{C['panel']};border:none;"
            f"border-radius:8px;}}"
            f"QFrame:hover{{background:{C['panel2']};}}")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(4)

        head = QHBoxLayout()
        title = QLabel(f"🌐  {name.upper()}")
        title.setStyleSheet(
            f"color:{C['purple']};font-weight:800;font-size:12px;"
            f"letter-spacing:1px;border:none;")
        count = QLabel(f"{total} tickers  ›")
        count.setStyleSheet(f"color:{C['muted']};font-size:11px;border:none;")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(count)
        hw = QWidget(); hw.setLayout(head)
        v.addWidget(hw)

        if blurb:
            b = QLabel(blurb)
            b.setStyleSheet(f"color:{C['muted']};font-size:10px;border:none;")
            b.setWordWrap(True)
            v.addWidget(b)

        tickers = [r[0] for r in self._rows]
        if tickers:
            t = QLabel(", ".join(tickers))
            t.setStyleSheet(
                f"color:{C['text']};font-family:'JetBrains Mono';"
                f"font-size:10px;line-height:1.5;border:none;")
            t.setWordWrap(True)
            v.addWidget(t)
        hint = QLabel("click for per-ticker detail")
        hint.setStyleSheet(f"color:{C['muted']};font-size:9px;border:none;")
        v.addWidget(hint)

    def mousePressEvent(self, _e):
        _show_universe_detail(self.window(), self._name.upper(),
                              self._rows, self._blurb)


class _PublicUniWorker(QThread):
    """Fetches the public-universe catalogue (list + each one's tickers) off
    the UI thread. Emits a list of {name,total,blurb,tickers}."""
    done = pyqtSignal(list)

    def run(self):
        out = []
        try:
            from ui.make_bot_tab import _fetch_public_universes
            for u in _fetch_public_universes():
                nm = u.get("name", "")
                if not nm:
                    continue
                try:
                    rows = _fetch_universe_rows(nm)
                except Exception:
                    rows = []
                out.append({
                    "name":  nm,
                    "total": u.get("total", len(rows)),
                    "blurb": (u.get("blurb", "") or "").strip(),
                    "rows":  rows,
                })
        except Exception as e:
            print(f"[universe-tab] public fetch failed: {e}")
        self.done.emit(out)


class UniverseTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._cards: dict[str, UniverseCard] = {}
        self._grid: QGridLayout | None = None
        self._pub_grid: QGridLayout | None = None
        self._pub_worker: _PublicUniWorker | None = None
        self._build()

    def _build(self):
        s = self.scroll

        s.add(SectionHeader("YOUR BOTS' UNIVERSES", C["purple"]))
        info = QLabel(
            "The ticker universe each of your bots currently trades. Universes "
            "are assigned when a bot is created (in Make Bot) — pick a curated "
            "public universe below, or let the bot choose its own tickers.")
        info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        info.setWordWrap(True)
        s.add(info)

        # ── 2-wide grid: one card per bot universe ──────────────
        grid_wrap = QWidget()
        self._grid = QGridLayout(grid_wrap)
        self._grid.setContentsMargins(0, 8, 0, 0)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(12)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        s.add(grid_wrap)

        # ── Public universe catalogue (server) ──────────────────
        s.add(SectionHeader("PUBLIC UNIVERSES  ·  REGENERATED WEEKLY", C["green"]))
        pub_info = QLabel(
            "Curated, pre-scored universes the APEX server rebuilds every "
            "Monday. Any bot can be assigned one of these at creation. "
            "Sector packs, factor packs and crypto — all shown below.")
        pub_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        pub_info.setWordWrap(True)
        s.add(pub_info)

        self._pub_status = QLabel("Loading public universes…")
        self._pub_status.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        s.add(self._pub_status)

        pub_wrap = QWidget()
        self._pub_grid = QGridLayout(pub_wrap)
        self._pub_grid.setContentsMargins(0, 8, 0, 0)
        self._pub_grid.setHorizontalSpacing(12)
        self._pub_grid.setVerticalSpacing(12)
        self._pub_grid.setColumnStretch(0, 1)
        self._pub_grid.setColumnStretch(1, 1)
        s.add(pub_wrap)
        s.add_stretch()

        self.refresh()
        self._load_public_universes()

    # ── public universe catalogue ───────────────────────────────

    def _load_public_universes(self):
        try:
            self._pub_worker = _PublicUniWorker()
            self._pub_worker.done.connect(self._on_public_loaded)
            self._pub_worker.start()
        except Exception as e:
            self._pub_status.setText(f"Could not load public universes: {e}")

    def _on_public_loaded(self, universes: list):
        # Clear any existing cards
        while self._pub_grid.count():
            item = self._pub_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not universes:
            self._pub_status.setText(
                "No public universes available (offline or not signed in). "
                "They appear once you're connected to the APEX server.")
            return
        self._pub_status.setText(f"{len(universes)} public universes available:")
        # Cache by name so bot cards can resolve their assigned universe.
        self._public_by_name = {u["name"]: u for u in universes}
        for i, u in enumerate(sorted(universes, key=lambda x: x["name"])):
            row, col = divmod(i, 2)
            card = PublicUniverseCard(
                u["name"], u.get("total", 0), u.get("blurb", ""),
                u.get("rows", []))
            self._pub_grid.addWidget(card, row, col)
        # Now that public data is in, refresh the bot cards so a bot that
        # uses a public universe shows that universe's tickers.
        try:
            self.refresh()
        except Exception as e:
            print(f"[universe-tab] bot refresh after public load: {e}")

    # ── your bots' universes ─────────────────────────────────────

    def refresh(self):
        bd = D.read_universe_breakdown()
        sides = bd.get("sides", []) or ["LONG", "SHORT", "DAY"]
        if set(sides) != set(self._cards.keys()):
            self._rebuild_grid(sides)
        for side in sides:
            card = self._cards.get(side)
            if card is None:
                continue
            rows = bd.get(side, [])
            # V4.6.81 — if the local universe file is empty (e.g. a custom bot
            # that trades a SERVER public universe or AI-picked tickers), resolve
            # what the bot actually trades so the card isn't blank.
            if not rows:
                resolved = self._resolve_bot_universe(side)
                if resolved:
                    rows = resolved
            card.load(rows)

    def _resolve_bot_universe(self, side: str) -> list:
        """Return [{Ticker, Note}] a bot trades when it has no local universe
        file: from its assigned PUBLIC universe (META.universe) or, failing
        that, the default_symbols baked into its code. [] if unknown."""
        try:
            reg = D.load_bot_registry()
            entry = next((c for c in reg.get("custom", [])
                          if str(c.get("id", "")).upper() == side.upper()), None)
            if not entry:
                return []
            script = entry.get("script", "")
            src = ""
            try:
                src = open(script, encoding="utf-8").read() if script else ""
            except Exception:
                src = ""
            uni_name = ""
            if src:
                try:
                    from core.bot_meta import parse_meta
                    uni_name = str((parse_meta(src) or {}).get("universe", "")).strip()
                except Exception:
                    uni_name = ""
            # 1) Assigned public universe → its scored rows (if loaded).
            pub = getattr(self, "_public_by_name", {}).get(uni_name)
            if pub and pub.get("rows"):
                return [{"Ticker": t, "Note": n} for t, n in pub["rows"]]
            # 2) Otherwise the tickers baked into the code (default_symbols).
            if src:
                import re
                m = re.search(r"default_symbols\s*=\s*\[([^\]]*)\]", src)
                if m:
                    syms = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
                    if syms:
                        return [{"Ticker": s, "Note": "AI-selected"} for s in syms]
        except Exception as e:
            print(f"[universe-tab] resolve {side}: {e}")
        return []

    def _rebuild_grid(self, sides: list[str]):
        for card in list(self._cards.values()):
            self._grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        for i, side in enumerate(sides):
            row, col = divmod(i, 2)
            card = UniverseCard(side)
            self._grid.addWidget(card, row, col)
            self._cards[side] = card

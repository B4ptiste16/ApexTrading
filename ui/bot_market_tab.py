"""
APEX · Bot Market tab  (V3.1.3)
─────────────────────────────────────────────────────────────────────
Full marketplace experience — only appears in the tab bar when the
user clicks "OPEN BOT MARKET" inside the MORE BOTS tab.

Layout:
  ┌──────────────────────────────────────────────────────────┐
  │  🛒 BOT MARKET                          [✕ Close market] │
  │  Browse, install and publish bots from the APEX network. │
  ├──────────────────────────────────────────────────────────┤
  │  🔍 [search...............]   [⬆  Publish a bot]         │
  │  Philosophy [▼]  Sort [▼]  Max ◊ [   ]  Min win % [   ]  │
  ├──────────────────────────────────────────────────────────┤
  │   ✨ Featured     🎯 For You     🔍 Browse     📊 Mine   │← sub-nav pills
  │   ──────────                                              │
  │                                                          │
  │   [bot card]     [bot card]     [bot card]               │← 2-col grid
  │   [bot card]     [bot card]     [bot card]               │
  │                                                          │
  └──────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QMessageBox,
    QStackedWidget, QInputDialog, QSizePolicy,
)

from ui.styles  import COLORS
from ui.widgets import ScrollContent, SectionHeader

C = COLORS


class _HttpWorker(QThread):
    done = pyqtSignal(bool, dict)

    def __init__(self, method: str, path: str,
                 payload: Optional[dict] = None,
                 params: Optional[dict] = None):
        super().__init__()
        self.method  = method
        self.path    = path
        self.payload = payload
        self.params  = params

    def run(self):
        import requests
        from ui.login import load_auth, load_server_url
        tok = (load_auth() or {}).get("token") or ""
        try:
            fn = {"GET":  requests.get,
                  "POST": requests.post,
                  "PUT":  requests.put}.get(self.method, requests.get)
            r = fn(f"{load_server_url()}{self.path}",
                   headers={"Authorization": f"Bearer {tok}"} if tok else None,
                   json=self.payload if self.method in ("POST", "PUT") else None,
                   params=self.params,
                   timeout=15)
            try:
                body = r.json()
            except Exception:
                body = {"text": r.text}
            self.done.emit(r.ok, body)
        except Exception as e:
            self.done.emit(False, {"detail": str(e)})


class BotMarketTab(QWidget):
    """The standalone marketplace. Opened via the MORE BOTS button."""

    closed = pyqtSignal()   # emitted when user clicks ✕

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._workers: list = []
        self._current_view = "featured"   # "featured" | "recommended" | "browse" | "mine"
        self._build()
        QTimer.singleShot(300, self.refresh)

    def refresh(self):
        self._refresh_current_view()

    # ── Layout ─────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        # ─── HERO ─────────────────────────────────────────────────
        hero = QFrame()
        hero.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:12px;border-top:3px solid {C['purple']};")
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(26, 22, 26, 22)
        hv.setSpacing(8)

        top_row = QHBoxLayout()
        title = QLabel("🛒  BOT MARKET")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:24px;font-weight:800;"
            f"color:{C['text']};letter-spacing:4px;")
        top_row.addWidget(title)
        top_row.addStretch()
        close_btn = QPushButton("✕  Close market")
        close_btn.setObjectName("toolBtn")
        close_btn.clicked.connect(self.closed.emit)
        top_row.addWidget(close_btn)
        tw = QWidget(); tw.setLayout(top_row)
        hv.addWidget(tw)

        sub = QLabel("Browse, install and publish bots from the APEX network.")
        sub.setStyleSheet(f"color:{C['muted']};font-size:11px;letter-spacing:2px;")
        hv.addWidget(sub)
        s.add(hero)

        # ─── SEARCH + PUBLISH ROW ─────────────────────────────────
        search_wrap = QFrame()
        search_wrap.setStyleSheet("background:transparent;border:none;")
        sw = QVBoxLayout(search_wrap)
        sw.setContentsMargins(4, 16, 4, 0)
        sw.setSpacing(10)

        search_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("🔍   Search bots by name or description…")
        self._search_edit.setFixedHeight(42)
        self._search_edit.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:8px;"
            f"padding:0 16px;font-size:13px;")
        self._search_edit.returnPressed.connect(self._refresh_current_view)
        search_row.addWidget(self._search_edit, 1)

        publish_btn = QPushButton("⬆  Publish a bot")
        publish_btn.setObjectName("addBotBtn")
        publish_btn.setFixedHeight(42)
        publish_btn.clicked.connect(self._publish_dialog)
        search_row.addWidget(publish_btn)
        srw = QWidget(); srw.setLayout(search_row)
        sw.addWidget(srw)

        # Filter row
        filt_row = QHBoxLayout()
        filt_row.setSpacing(14)

        filt_row.addWidget(self._lbl("PHILOSOPHY"))
        self._filt_philosophy = QComboBox()
        self._filt_philosophy.addItem("All", "")
        for opt in ("long", "short", "day", "options", "momentum",
                    "mean-reversion", "scalping", "swing"):
            self._filt_philosophy.addItem(opt.title(), opt)
        self._filt_philosophy.currentIndexChanged.connect(self._refresh_current_view)
        filt_row.addWidget(self._filt_philosophy)

        filt_row.addWidget(self._lbl("SORT"))
        self._filt_sort = QComboBox()
        for label, key in (("Most downloaded", "downloads"),
                           ("Top rated",       "rating"),
                           ("Highest win rate","win_rate"),
                           ("Newest",          "newest"),
                           ("Cheapest first",  "cheapest")):
            self._filt_sort.addItem(label, key)
        self._filt_sort.currentIndexChanged.connect(self._refresh_current_view)
        filt_row.addWidget(self._filt_sort)

        filt_row.addWidget(self._lbl("MAX ◊"))
        self._filt_max_price = QSpinBox()
        self._filt_max_price.setRange(0, 1_000_000)
        self._filt_max_price.setSingleStep(50)
        self._filt_max_price.setSpecialValueText("any")
        self._filt_max_price.setValue(0)
        self._filt_max_price.setFixedWidth(110)
        self._filt_max_price.valueChanged.connect(self._refresh_current_view)
        filt_row.addWidget(self._filt_max_price)

        filt_row.addWidget(self._lbl("MIN WIN %"))
        self._filt_min_wr = QDoubleSpinBox()
        self._filt_min_wr.setRange(0.0, 100.0)
        self._filt_min_wr.setSingleStep(5.0)
        self._filt_min_wr.setDecimals(1)
        self._filt_min_wr.setSpecialValueText("any")
        self._filt_min_wr.setValue(0.0)
        self._filt_min_wr.setFixedWidth(90)
        self._filt_min_wr.valueChanged.connect(self._refresh_current_view)
        filt_row.addWidget(self._filt_min_wr)

        filt_row.addStretch()
        fw = QWidget(); fw.setLayout(filt_row)
        sw.addWidget(fw)
        s.add(search_wrap)

        # ─── SUB-NAV PILLS ────────────────────────────────────────
        nav_wrap = QFrame()
        nav_wrap.setStyleSheet("background:transparent;border:none;")
        nl = QVBoxLayout(nav_wrap)
        nl.setContentsMargins(4, 22, 4, 8)
        nl.setSpacing(8)

        pills = QHBoxLayout()
        pills.setSpacing(4)
        self._pills: dict[str, QPushButton] = {}
        for key, label in (("featured",    "✨  Featured"),
                           ("recommended", "🎯  For You"),
                           ("browse",      "🔍  Browse all"),
                           ("mine",        "📊  My published")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet(self._pill_css())
            btn.clicked.connect(lambda _checked=False, k=key: self._set_view(k))
            pills.addWidget(btn)
            self._pills[key] = btn
        pills.addStretch()
        pw = QWidget(); pw.setLayout(pills)
        nl.addWidget(pw)

        # Active section title + status hint
        self._section_label = QLabel("")
        self._section_label.setStyleSheet(
            f"color:{C['muted']};font-size:10px;letter-spacing:3px;"
            f"font-weight:600;padding:0;")
        nl.addWidget(self._section_label)
        s.add(nav_wrap)

        # ─── RESULTS GRID (one column of cards) ───────────────────
        self._results_box = QWidget()
        self._results_layout = QGridLayout(self._results_box)
        self._results_layout.setContentsMargins(0, 6, 0, 6)
        self._results_layout.setHorizontalSpacing(14)
        self._results_layout.setVerticalSpacing(14)
        self._results_layout.setColumnStretch(0, 1)
        self._results_layout.setColumnStretch(1, 1)
        s.add(self._results_box)

        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:14px 0;")
        s.add(self._status)

        s.add_stretch()
        self._set_view("featured")

    # ── small helpers ──────────────────────────────────────────────

    def _lbl(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(
            f"color:{C['muted']};font-size:9px;letter-spacing:2px;"
            f"font-weight:700;")
        return l

    def _pill_css(self) -> str:
        return f"""
            QPushButton {{
                background    : transparent;
                color         : {C['muted']};
                border        : 1px solid {C['border']};
                border-radius : 18px;
                padding       : 8px 16px;
                font-family   : 'JetBrains Mono';
                font-size     : 10px;
                letter-spacing: 2px;
                font-weight   : 600;
            }}
            QPushButton:hover {{
                color         : {C['text']};
                border-color  : {C['muted']};
            }}
            QPushButton:checked {{
                color         : {C['text']};
                border-color  : {C['purple']};
                background    : rgba(138,147,201,0.10);
            }}
        """

    def _spawn(self, w: _HttpWorker, on_done):
        w.done.connect(on_done)
        w.finished.connect(
            lambda _w=w: self._workers.remove(_w)
                          if _w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _clear_results(self):
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    # ── view switching ─────────────────────────────────────────────

    def _set_view(self, view: str):
        self._current_view = view
        for k, btn in self._pills.items():
            btn.setChecked(k == view)
        labels = {
            "featured":    "BOTS HAND-PICKED BY APEX EDITORS",
            "recommended": "BOTS WE THINK YOU'LL LIKE",
            "browse":      "EVERY PUBLISHED BOT (filters apply)",
            "mine":        "YOUR PUBLISHED BOTS — PUBLISHER ANALYTICS",
        }
        self._section_label.setText(labels.get(view, ""))
        self._refresh_current_view()

    def _refresh_current_view(self):
        self._clear_results()
        self._status.setText("Loading…")
        self._status.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:14px 0;")
        if self._current_view == "mine":
            w = _HttpWorker("GET", "/bots/mine")
        else:
            params = {
                "q":          self._search_edit.text().strip(),
                "philosophy": self._filt_philosophy.currentData() or "",
                "sort":       self._filt_sort.currentData() or "downloads",
                "limit":      40,
            }
            if self._current_view == "featured":
                params["section"] = "featured"
            elif self._current_view == "recommended":
                params["section"] = "recommended"
            if self._filt_max_price.value() > 0:
                params["max_price"] = self._filt_max_price.value()
            if self._filt_min_wr.value() > 0:
                params["min_win_rate"] = self._filt_min_wr.value()
            w = _HttpWorker("GET", "/bots/v2", params=params)
        self._spawn(w, self._on_results_loaded)

    def _on_results_loaded(self, ok: bool, body: dict):
        if not ok:
            self._status.setText(body.get("detail", "Could not load."))
            self._status.setStyleSheet(
                f"color:{C['red']};font-size:11px;padding:14px 0;")
            return
        bots = body.get("bots", []) or []
        if not bots:
            empties = {
                "featured":    "No featured bots yet. Check back soon.",
                "recommended": "No personalised recommendations yet — install a few bots so we can learn what you like.",
                "browse":      "No bots match these filters.",
                "mine":        "You haven't published any bots yet. Click ⬆ Publish a bot above to share one with the APEX community.",
            }
            self._status.setText(empties.get(self._current_view,
                                              "Nothing here."))
            return
        self._status.setText(f"{len(bots)} bots")
        self._status.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:8px 0;")
        # Render in a 2-column grid
        for i, b in enumerate(bots):
            card = self._make_card(b, owned=(self._current_view == "mine"))
            row = i // 2
            col = i % 2
            self._results_layout.addWidget(card, row, col)

    # ── Card factory ───────────────────────────────────────────────

    def _make_card(self, b: dict, *, owned: bool) -> QWidget:
        accent = (C["orange"] if b.get("featured")
                  else C["purple"] if b.get("recommended")
                  else C["green"] if not b.get("price_credits")
                  else C["yellow"])
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background:{C['panel']}; "
            f"border:1px solid {C['border']}; "
            f"border-radius:12px; "
            f"border-top:3px solid {accent}; }}"
            f"QFrame:hover {{ border-color:{C['muted']}; }}")
        card.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        # Header — name + badges
        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(b.get("name", b.get("slug", "?")))
        name.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:14px;font-weight:800;"
            f"color:{C['text']};letter-spacing:0.5px;")
        name.setWordWrap(True)
        head.addWidget(name, 1)
        for cond, txt, col in (
            (b.get("featured"),     "⭐ FEATURED", C["orange"]),
            (b.get("recommended"),  "✦ RECO",      C["purple"]),
        ):
            if cond:
                badge = QLabel(txt)
                badge.setStyleSheet(
                    f"color:{col};font-size:8px;letter-spacing:2px;"
                    f"font-weight:700;padding:3px 7px;"
                    f"border:1px solid {col};border-radius:4px;")
                head.addWidget(badge)
        hw = QWidget(); hw.setLayout(head)
        v.addWidget(hw)

        # Description (or placeholder)
        desc = QLabel(b.get("description") or "No description provided.")
        desc.setStyleSheet(
            f"color:{C['text']};font-size:11px;line-height:1.5;")
        desc.setWordWrap(True)
        desc.setMinimumHeight(40)
        v.addWidget(desc)

        # Stat strip — 4 columns
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        for label, value, val_color in [
            ("PHILOSOPHY",
             (b.get("philosophy") or "general").upper(),
             C["text"]),
            ("WIN RATE",
             f"{b['win_rate_pct']:.0f}%" if b.get("win_rate_pct") else "—",
             C["green"] if (b.get("win_rate_pct") or 0) >= 50 else C["muted"]),
            ("DOWNLOADS",
             f"{b.get('downloads', 0):,}",
             C["muted"]),
            ("RATING",
             f"★ {b['rating']:.1f}" if b.get("rating") else "—",
             C["yellow"] if b.get("rating") else C["muted"]),
        ]:
            stat = QVBoxLayout()
            stat.setSpacing(2)
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color:{C['muted']};font-size:8px;letter-spacing:2px;")
            val = QLabel(value)
            val.setStyleSheet(
                f"color:{val_color};font-size:12px;font-weight:700;")
            stat.addWidget(lbl)
            stat.addWidget(val)
            sw = QWidget(); sw.setLayout(stat)
            stat_row.addWidget(sw)
        sr = QWidget(); sr.setLayout(stat_row)
        v.addWidget(sr)

        # Bottom — price + action
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        price = int(b.get("price_credits", 0))
        if price > 0:
            price_lbl = QLabel(f"◊  {price:,} credits")
            price_lbl.setStyleSheet(
                f"color:{C['yellow']};font-size:13px;font-weight:700;")
        else:
            price_lbl = QLabel("FREE")
            price_lbl.setStyleSheet(
                f"color:{C['green']};font-size:11px;letter-spacing:3px;"
                f"font-weight:800;")
        bottom.addWidget(price_lbl)
        bottom.addStretch()

        if owned:
            stat = QLabel(f"📊  {b.get('downloads', 0)} installs")
            stat.setStyleSheet(f"color:{C['green']};font-size:11px;")
            bottom.addWidget(stat)
        else:
            install = QPushButton("⬇  Install")
            install.setObjectName("addBotBtn")
            install.clicked.connect(
                lambda _, slug=b["slug"], name=b["name"]:
                    self._install(slug, name))
            bottom.addWidget(install)
        bw = QWidget(); bw.setLayout(bottom)
        v.addWidget(bw)
        return card

    # ── Install flow ───────────────────────────────────────────────

    def _install(self, slug: str, name: str):
        from PyQt6.QtCore import QThread as _QT, pyqtSignal as _Sig
        from ui.login   import load_server_url
        from core.paths import DATA_DIR

        url = load_server_url()

        class _DL(_QT):
            done = _Sig(bool, str, bytes)
            def run(self_):
                import requests
                try:
                    r = requests.get(f"{url}/bots/{slug}/download", timeout=20)
                    if r.ok:
                        self_.done.emit(True, "", r.content)
                    else:
                        self_.done.emit(False, f"HTTP {r.status_code}", b"")
                except Exception as e:
                    self_.done.emit(False, str(e), b"")

        def _on(ok, err, blob):
            if not ok:
                QMessageBox.warning(self, "Install failed", err)
                return
            bots_dir = DATA_DIR / "bots"
            bots_dir.mkdir(exist_ok=True)
            dest = bots_dir / f"{slug}.py"
            dest.write_bytes(blob)
            try:
                from core import data as _D
                import json as _json
                s = _D.load_settings()
                reg = s.get("bot_registry",
                            {"active": [], "silenced": [], "custom": []})
                existing = [c["id"] for c in reg.get("custom", [])]
                if slug not in existing:
                    reg.setdefault("custom", []).append({
                        "id":     slug, "label":  name,
                        "script": str(dest), "color":  C["purple"],
                    })
                    s["bot_registry"] = reg
                    with open(_D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                        _json.dump(s, f, indent=2)
            except Exception as e:
                print(f"[market-install] registry update: {e}")
            QMessageBox.information(
                self, "Bot installed",
                f"'{name}' is now in your library.\n\n"
                f"Open MORE BOTS → AVAILABLE TO ADD to activate it.")

        self._dl_worker = _DL()
        self._dl_worker.done.connect(_on)
        self._dl_worker.start()

    # ── Publish flow (delegates to MoreBotsTab so drag-drop stays one path) ──

    def _publish_dialog(self):
        """Open the file picker, then hand the chosen .py to MoreBotsTab's
        publish flow (which captures name + philosophy + price + uploads)."""
        from PyQt6.QtWidgets import QFileDialog
        from core.paths import DATA_DIR
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a bot to publish",
            str(DATA_DIR / "bots"), "Python Files (*.py)")
        if not path:
            return
        win = self.window()
        more = getattr(win, "more_bots_tab", None)
        if more and hasattr(more, "_publish_bot_with_path"):
            more._publish_bot_with_path(path)
            # Refresh the My-Published view shortly after the upload
            QTimer.singleShot(3000, self._refresh_current_view)
        else:
            QMessageBox.warning(self, "Publish",
                                "Could not access the publish flow.")

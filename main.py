"""
APEX Trading Platform — Desktop App  V7
"""

import sys
import os
import json
import shutil
from pathlib import Path

try:
    if sys.stdout is not None:
        print("Starting APEX Trading Platform...", flush=True)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QFrame, QScrollArea,
    QSizePolicy, QSystemTrayIcon, QMenu, QMessageBox, QProgressDialog,
    QFileDialog, QTabBar, QGridLayout,
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QSize, QPoint, QPropertyAnimation,
    QEasingCurve,
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap, QFontDatabase,
    QAction, QPainter, QLinearGradient,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from ui.overview   import OverviewTab
from ui.bot_tab    import BotTab
from ui.overview   import ToolsTab
from ui.universe   import UniverseTab
from ui.styles     import DARK_STYLESHEET, COLORS
from core.updater  import (check_for_update, download_and_apply,
                            get_current_version, restart_app,
                            launch_downloaded_installer)
from core.paths    import DATA_DIR, ensure_data_dir
import core.data   as D

C = COLORS


# ─────────────────────────────────────────
# SINGLE INSTANCE
# ─────────────────────────────────────────

def _check_single_instance() -> bool:
    """Return False if another APEX instance is already running."""
    try:
        import ctypes
        _mutex = ctypes.windll.kernel32.CreateMutexW(
            None, False, "APEX_Trading_Platform_SingleInstance_v7")
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:
        return True   # can't check — allow launch


# ─────────────────────────────────────────
# GRADIENT BACKGROUND
# ─────────────────────────────────────────

class GradientBackground(QWidget):
    """Central widget that paints a subtle diagonal gradient."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0.0, QColor(C["bg"]))
        gradient.setColorAt(1.0, QColor(C["bg2"]))
        painter.fillRect(self.rect(), gradient)


# ─────────────────────────────────────────
# STATUS DOT  (blinking indicator)
# ─────────────────────────────────────────

class StatusDot(QLabel):
    """Coloured blinking ● for bot status."""

    _DOT_COLORS = {
        "running":   C["green"],
        "sleeping":  C["orange"],
        "scheduled": C["red"],
        "stopped":   C["muted"],
        "silenced":  "#2a3347",
    }

    def __init__(self, parent=None):
        super().__init__("●", parent)
        self.setFixedSize(16, 16)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._state = "stopped"
        self._phase = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._refresh()

    def set_state(self, state: str):
        if state == self._state:
            return
        self._state = state
        if state in ("running", "sleeping", "scheduled"):
            if not self._timer.isActive():
                self._timer.start(700)
        else:
            self._timer.stop()
            self._phase = True
        self._refresh()

    def _tick(self):
        self._phase = not self._phase
        self._refresh()

    def _refresh(self):
        c = self._DOT_COLORS.get(self._state, C["muted"])
        if self._phase:
            col = c
        else:
            h = c.lstrip("#")
            r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            col = f"rgba({r},{g},{b},0.12)"
        self.setStyleSheet(
            f"color:{col};font-size:8px;background:transparent;border:none;"
        )


# ─────────────────────────────────────────
# TAB QUICK CONTROLS  (▶ ■ ✕)
# ─────────────────────────────────────────

class TabQuickControls(QWidget):
    play_clicked   = pyqtSignal()
    stop_clicked   = pyqtSignal()
    remove_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedHeight(20)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 3, 0)
        row.setSpacing(1)

        self._play = QPushButton("▶")
        self._stop = QPushButton("■")
        self._remove = QPushButton("✕")

        self._play.setObjectName("tabPlayBtn")
        self._stop.setObjectName("tabStopBtn")
        self._remove.setObjectName("tabRemoveBtn")

        for btn in (self._play, self._stop, self._remove):
            btn.setFixedSize(16, 16)
            row.addWidget(btn)

        self._stop.setVisible(False)
        self._play.clicked.connect(self.play_clicked)
        self._stop.clicked.connect(self.stop_clicked)
        self._remove.clicked.connect(self.remove_clicked)

    def set_running(self, running: bool):
        self._play.setVisible(not running)
        self._stop.setVisible(running)


# ─────────────────────────────────────────
# BOT REGISTRY
# ─────────────────────────────────────────

BUILTIN_BOTS = {
    "LONG": {
        "label":       "▲ LONG",
        "icon":        "▲",
        "color":       C["green"],
        "description": "Momentum + mean-reversion portfolio. "
                       "Uses Claude Vision on charts to rank candidates.",
        "cost":        "~$0.05–0.20 / day",
        "account":     "Alpaca — 1 dedicated API key pair",
    },
    "SHORT": {
        "label":       "▼ SHORT",
        "icon":        "▼",
        "color":       C["red"],
        "description": "Bear momentum, defensive in BULL regime. "
                       "Sells short on weakness, covers on strength.",
        "cost":        "~$0.03–0.12 / day",
        "account":     "Alpaca — 1 dedicated API key pair",
    },
    "DAY": {
        "label":       "◆ DAY",
        "icon":        "◆",
        "color":       C["orange"],
        "description": "Single high-conviction intraday bracket orders. "
                       "ATR-based stop-loss and take-profit.",
        "cost":        "~$0.02–0.08 / day",
        "account":     "Alpaca — 1 dedicated API key pair",
    },
}

MAX_ACTIVE_BOTS = 5


def _load_registry() -> dict:
    s = D.load_settings()
    default = {"active": ["LONG","SHORT","DAY"], "silenced": [], "custom": []}
    reg = s.get("bot_registry", default)
    # Ensure all keys exist
    for k in default:
        reg.setdefault(k, default[k])
    return reg


def _save_registry(reg: dict):
    s = D.load_settings()
    s["bot_registry"] = reg
    with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# ─────────────────────────────────────────
# MORE BOTS TAB
# ─────────────────────────────────────────

class MoreBotsTab(QWidget):
    """Manage active/silenced bots + upload custom bots."""

    bot_added     = pyqtSignal(str)
    bot_removed   = pyqtSignal(str)
    bot_silenced  = pyqtSignal(str)
    bot_unsilenced = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        from ui.widgets import ScrollContent
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._build()

    def _build(self):
        from ui.widgets import SectionHeader
        s = self.scroll

        # ── ACTIVE BOTS ──────────────────────────────────────
        s.add(SectionHeader("ACTIVE BOTS", C["green"]))

        info = QLabel(
            f"Active bots appear as tabs and can be started. "
            f"Max {MAX_ACTIVE_BOTS} bots (Alpaca: 1 API key pair per bot; "
            f"IBKR: multiple bots share one connection via different client IDs)."
        )
        info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        info.setWordWrap(True)
        s.add(info)

        self._active_grid = QWidget()
        self._active_layout = QGridLayout(self._active_grid)
        self._active_layout.setSpacing(10)
        self._active_layout.setContentsMargins(0, 4, 0, 4)
        s.add(self._active_grid)

        # ── AVAILABLE TO ADD ─────────────────────────────────
        s.add(SectionHeader("AVAILABLE TO ADD", C["purple"]))
        self._avail_grid = QWidget()
        self._avail_layout = QGridLayout(self._avail_grid)
        self._avail_layout.setSpacing(10)
        self._avail_layout.setContentsMargins(0, 4, 0, 4)
        s.add(self._avail_grid)

        self._none_lbl = QLabel("All built-in bots are already active.")
        self._none_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._avail_layout.addWidget(self._none_lbl, 0, 0)

        # ── SILENCED BOTS ────────────────────────────────────
        s.add(SectionHeader("SILENCED BOTS", C["muted"]))
        self._silenced_grid = QWidget()
        self._silenced_layout = QGridLayout(self._silenced_grid)
        self._silenced_layout.setSpacing(10)
        self._silenced_layout.setContentsMargins(0, 4, 0, 4)
        s.add(self._silenced_grid)

        self._none_sil = QLabel("No silenced bots.")
        self._none_sil.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._silenced_layout.addWidget(self._none_sil, 0, 0)

        # ── UPLOAD CUSTOM BOT ────────────────────────────────
        s.add(SectionHeader("UPLOAD CUSTOM BOT", C["yellow"]))
        custom_info = QLabel(
            "Upload a Python trading script (.py) to add it as a custom bot. "
            "The script must expose a main() function. "
            "Uploaded bots are stored in the data folder and can be added to tabs like built-in bots."
        )
        custom_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        custom_info.setWordWrap(True)
        s.add(custom_info)

        upload_row = QHBoxLayout()
        upload_btn = QPushButton("📂  Browse .py file...")
        upload_btn.setObjectName("toolBtn")
        upload_btn.clicked.connect(self._upload_bot)
        self._upload_msg = QLabel("")
        self._upload_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        upload_row.addWidget(upload_btn)
        upload_row.addWidget(self._upload_msg)
        upload_row.addStretch()
        uw = QWidget()
        uw.setLayout(upload_row)
        s.add(uw)

        # ── PUBLIC BOT LIBRARY  (V7.1+) ──────────────────────
        s.add(SectionHeader("BROWSE PUBLIC BOTS", C["purple"]))
        lib_info = QLabel(
            "Search bots shared by other APEX users. Click  ⬇  Install  "
            "to copy a bot into your local library — it'll then show up "
            "under  AVAILABLE TO ADD  above.")
        lib_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        lib_info.setWordWrap(True)
        s.add(lib_info)

        from PyQt6.QtWidgets import QLineEdit as _QLineEdit
        search_row = QHBoxLayout()
        self._lib_search = _QLineEdit()
        self._lib_search.setPlaceholderText("Search by name or description…")
        self._lib_search.setFixedHeight(32)
        self._lib_search.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:6px;"
            f"padding:0 12px;font-size:11px;")
        self._lib_search.returnPressed.connect(self._refresh_library)
        search_row.addWidget(self._lib_search)
        lib_btn = QPushButton("Search")
        lib_btn.setObjectName("toolBtn")
        lib_btn.clicked.connect(self._refresh_library)
        search_row.addWidget(lib_btn)
        upload_pub_btn = QPushButton("⬆  Publish own bot…")
        upload_pub_btn.setObjectName("addBotBtn")
        upload_pub_btn.clicked.connect(self._publish_bot)
        search_row.addWidget(upload_pub_btn)
        srw = QWidget()
        srw.setLayout(search_row)
        s.add(srw)

        self._lib_results = QWidget()
        self._lib_layout = QVBoxLayout(self._lib_results)
        self._lib_layout.setSpacing(6)
        self._lib_layout.setContentsMargins(0, 6, 0, 6)
        self._lib_status = QLabel("Click Search to load public bots.")
        self._lib_status.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._lib_layout.addWidget(self._lib_status)
        s.add(self._lib_results)

        # ── ACCOUNT LIMITS INFO ──────────────────────────────
        s.add(SectionHeader("ACCOUNT LIMITS", C["muted"]))
        limits = QLabel(
            "Alpaca paper trading:\n"
            "  • Each bot requires its own paper account and API key pair.\n"
            "  • You can create multiple free paper accounts in Alpaca.\n"
            "  • No hard limit, but 3–5 is practical.\n\n"
            "IBKR (Interactive Brokers):\n"
            "  • Multiple bots can share a single TWS/Gateway connection\n"
            "    by using different clientId values.\n"
            "  • APEX uses clientId 1, 2, 3… per bot automatically."
        )
        limits.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:11px;"
            f"color:{C['text']};line-height:1.8;")
        limits.setWordWrap(True)
        s.add(limits)
        s.add_stretch()

        self.refresh()

    def refresh(self):
        reg = _load_registry()
        active   = reg["active"]
        silenced = reg["silenced"]
        custom   = reg.get("custom", [])

        self._rebuild_grid(
            self._active_layout,
            [s for s in active if s not in silenced],
            mode="active",
        )
        self._rebuild_grid(
            self._silenced_layout,
            [s for s in active if s in silenced],
            mode="silenced",
        )
        # Available = built-ins not in active + custom not in active
        all_known = list(BUILTIN_BOTS.keys()) + [c["id"] for c in custom]
        available = [b for b in all_known if b not in active]
        if available:
            self._none_lbl.setVisible(False)
            self._rebuild_grid(self._avail_layout, available, mode="available")
        else:
            self._none_lbl.setVisible(True)
            self._clear_grid(self._avail_layout, keep_row0=True)

        # Silenced label visibility
        sil_list = [s for s in active if s in silenced]
        self._none_sil.setVisible(len(sil_list) == 0)

    def _clear_grid(self, layout, keep_row0=False):
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            if item and item.widget():
                if keep_row0:
                    pos = layout.getItemPosition(i)
                    if pos[0] == 0:
                        continue
                item.widget().deleteLater()

    def _rebuild_grid(self, layout, sides: list, mode: str):
        self._clear_grid(layout)
        for col, side in enumerate(sides):
            card = self._make_bot_card(side, mode)
            layout.addWidget(card, 0, col)

    def _make_bot_card(self, side: str, mode: str) -> QFrame:
        info = BUILTIN_BOTS.get(side, {
            "label": side, "icon": "◉", "color": C["purple"],
            "description": "Custom bot", "cost": "—", "account": "—",
        })
        color   = info["color"]
        opacity = "0.45" if mode == "silenced" else "1.0"

        card = QFrame()
        card.setStyleSheet(
            f"background:{C['panel2']};border:1px solid {color}30;"
            f"border-radius:10px;border-top:2px solid {color};"
        )
        card.setFixedWidth(220)
        vl = QVBoxLayout(card)
        vl.setContentsMargins(14, 12, 14, 12)
        vl.setSpacing(6)

        title = QLabel(info["label"])
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:12px;"
            f"font-weight:800;letter-spacing:2px;color:{color};"
            f"opacity:{opacity};"
        )
        vl.addWidget(title)

        desc = QLabel(info["description"])
        desc.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        desc.setWordWrap(True)
        vl.addWidget(desc)

        cost = QLabel(f"Cost: {info['cost']}")
        cost.setStyleSheet(f"color:{C['yellow']};font-size:9px;")
        vl.addWidget(cost)

        acct = QLabel(info["account"])
        acct.setStyleSheet(f"color:{C['muted']};font-size:9px;")
        acct.setWordWrap(True)
        vl.addWidget(acct)

        vl.addSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        if mode == "active":
            sil_btn = QPushButton("Silence")
            sil_btn.setObjectName("silenceBtn")
            sil_btn.clicked.connect(lambda _, s=side: self._silence(s))
            rem_btn = QPushButton("Remove")
            rem_btn.setObjectName("dangerBtn")
            rem_btn.clicked.connect(lambda _, s=side: self._remove(s))
            btn_row.addWidget(sil_btn)
            btn_row.addWidget(rem_btn)

        elif mode == "silenced":
            unsil_btn = QPushButton("Unsilence")
            unsil_btn.setObjectName("addBotBtn")
            unsil_btn.clicked.connect(lambda _, s=side: self._unsilence(s))
            btn_row.addWidget(unsil_btn)

        elif mode == "available":
            add_btn = QPushButton("+ Add Bot")
            add_btn.setObjectName("addBotBtn")
            add_btn.clicked.connect(lambda _, s=side: self._add(s))
            btn_row.addWidget(add_btn)

        btn_row.addStretch()
        bw = QWidget()
        bw.setLayout(btn_row)
        vl.addWidget(bw)
        return card

    def _add(self, side: str):
        reg = _load_registry()
        if side not in reg["active"]:
            if len(reg["active"]) >= MAX_ACTIVE_BOTS:
                QMessageBox.warning(
                    self, "Max bots reached",
                    f"You can have at most {MAX_ACTIVE_BOTS} active bots.\n"
                    "Remove one before adding another.")
                return
            reg["active"].append(side)
            _save_registry(reg)
            self.bot_added.emit(side)
        self.refresh()

    def _remove(self, side: str):
        reg = _load_registry()
        if side in reg["active"]:
            reg["active"].remove(side)
        if side in reg["silenced"]:
            reg["silenced"].remove(side)
        _save_registry(reg)
        self.bot_removed.emit(side)
        self.refresh()

    def _silence(self, side: str):
        reg = _load_registry()
        if side not in reg["silenced"]:
            reg["silenced"].append(side)
        _save_registry(reg)
        self.bot_silenced.emit(side)
        self.refresh()

    def _unsilence(self, side: str):
        reg = _load_registry()
        if side in reg["silenced"]:
            reg["silenced"].remove(side)
        _save_registry(reg)
        self.bot_unsilenced.emit(side)
        self.refresh()

    def _upload_bot(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Bot Script", "", "Python Files (*.py)")
        if not path:
            return
        bots_dir = DATA_DIR / "bots"
        bots_dir.mkdir(exist_ok=True)
        dest = bots_dir / Path(path).name
        shutil.copy2(path, dest)

        reg = _load_registry()
        bot_id = Path(path).stem
        existing_ids = [c["id"] for c in reg.get("custom", [])]
        if bot_id not in existing_ids:
            reg.setdefault("custom", []).append({
                "id": bot_id,
                "label": bot_id,
                "script": str(dest),
                "color": C["purple"],
            })
            _save_registry(reg)

        self._upload_msg.setText(f"✓ {Path(path).name} uploaded — appears in Available")
        QTimer.singleShot(4000, lambda: self._upload_msg.setText(""))
        self.refresh()

    # ── V7.1+ public bot library ────────────────────────────

    def _refresh_library(self):
        """Hit the server's GET /bots endpoint and render results."""
        from PyQt6.QtCore import QThread, pyqtSignal as _Sig
        from ui.login import load_server_url

        q = self._lib_search.text().strip()
        url = load_server_url()

        # Clear current rows except the status label
        for i in reversed(range(self._lib_layout.count())):
            w = self._lib_layout.itemAt(i).widget()
            if w and w is not self._lib_status:
                w.deleteLater()
        self._lib_status.setText("Loading…")
        self._lib_status.setVisible(True)

        class _ListWorker(QThread):
            done = _Sig(bool, list, str)

            def __init__(self, base, query):
                super().__init__()
                self.base, self.query = base, query

            def run(self):
                import requests
                try:
                    r = requests.get(
                        f"{self.base}/bots",
                        params={"q": self.query, "limit": 50},
                        timeout=8,
                    )
                    if r.ok:
                        self.done.emit(True, r.json().get("bots", []), "")
                    else:
                        self.done.emit(False, [], f"Server error ({r.status_code}).")
                except Exception as e:
                    self.done.emit(False, [], str(e))

        self._lib_worker = _ListWorker(url, q)
        self._lib_worker.done.connect(self._on_library_loaded)
        self._lib_worker.start()

    def _on_library_loaded(self, ok: bool, bots: list, err: str):
        if not ok:
            self._lib_status.setText(f"Could not load library: {err}")
            self._lib_status.setStyleSheet(
                f"color:{C['red']};font-size:11px;padding:4px 0;")
            return
        if not bots:
            self._lib_status.setText("No public bots match your search.")
            self._lib_status.setStyleSheet(
                f"color:{C['muted']};font-size:11px;padding:4px 0;")
            return
        self._lib_status.setVisible(False)
        for b in bots:
            self._lib_layout.addWidget(self._make_library_row(b))

    def _make_library_row(self, b: dict) -> QWidget:
        row = QFrame()
        row.setStyleSheet(
            f"background:{C['panel2']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 10, 14, 10)
        hl.setSpacing(10)

        meta = QVBoxLayout()
        title = QLabel(b.get("name", b.get("slug", "?")))
        title.setStyleSheet(
            f"color:{C['text']};font-weight:700;font-size:12px;")
        sub = QLabel(
            f"{b.get('description','—') or '—'}   ·   "
            f"{b.get('downloads',0)}↓   ·   "
            f"{b.get('size_bytes',0) // 1024} KB")
        sub.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        sub.setWordWrap(True)
        meta.addWidget(title)
        meta.addWidget(sub)
        mw = QWidget()
        mw.setLayout(meta)
        hl.addWidget(mw, 1)

        install_btn = QPushButton("⬇  Install")
        install_btn.setObjectName("addBotBtn")
        install_btn.clicked.connect(lambda _, slug=b["slug"], name=b["name"]:
                                    self._install_public_bot(slug, name))
        hl.addWidget(install_btn)
        return row

    def _install_public_bot(self, slug: str, name: str):
        """Download a public bot to DATA_DIR/bots and register it locally."""
        from PyQt6.QtCore import QThread, pyqtSignal as _Sig
        from ui.login import load_server_url

        url = load_server_url()

        class _DLWorker(QThread):
            done = _Sig(bool, str, bytes)

            def __init__(self, base, sl):
                super().__init__()
                self.base, self.sl = base, sl

            def run(self):
                import requests
                try:
                    r = requests.get(f"{self.base}/bots/{self.sl}/download",
                                     timeout=20)
                    if r.ok:
                        self.done.emit(True, "", r.content)
                    else:
                        self.done.emit(False, f"HTTP {r.status_code}", b"")
                except Exception as e:
                    self.done.emit(False, str(e), b"")

        def _on_dl(ok, err, blob):
            if not ok:
                QMessageBox.warning(self, "Install failed", err)
                return
            bots_dir = DATA_DIR / "bots"
            bots_dir.mkdir(exist_ok=True)
            dest = bots_dir / f"{slug}.py"
            dest.write_bytes(blob)
            reg = _load_registry()
            existing_ids = [c["id"] for c in reg.get("custom", [])]
            if slug not in existing_ids:
                reg.setdefault("custom", []).append({
                    "id": slug, "label": name, "script": str(dest),
                    "color": C["purple"],
                })
                _save_registry(reg)
            QMessageBox.information(
                self, "Installed",
                f"{name} is now in your local library and can be added "
                f"from AVAILABLE TO ADD.")
            self.refresh()

        self._dl_worker = _DLWorker(url, slug)
        self._dl_worker.done.connect(_on_dl)
        self._dl_worker.start()

    def _publish_bot(self):
        """Upload one of the local custom bots to the public marketplace."""
        from PyQt6.QtWidgets import QInputDialog
        from ui.login import load_auth, load_server_url

        stored = load_auth() or {}
        token = stored.get("token")
        if not token:
            QMessageBox.warning(
                self, "Sign in required",
                "Publishing a bot requires an APEX account. Sign in or "
                "create one from the start screen.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Select bot script to publish",
            str(DATA_DIR / "bots"), "Python Files (*.py)")
        if not path:
            return
        blob = Path(path).read_bytes()
        name, ok = QInputDialog.getText(
            self, "Bot name", "Display name:", text=Path(path).stem)
        if not ok or not name.strip():
            return
        desc, _ = QInputDialog.getText(
            self, "Description", "One-line description:")
        tags, _ = QInputDialog.getText(
            self, "Tags", "Comma-separated tags (optional):")

        from PyQt6.QtCore import QThread, pyqtSignal as _Sig

        class _UpWorker(QThread):
            done = _Sig(bool, str)

            def __init__(self, base, tok, n, d, t, b):
                super().__init__()
                self.base, self.tok, self.n, self.d, self.t, self.b = \
                    base, tok, n, d, t, b

            def run(self):
                import requests
                try:
                    r = requests.post(
                        f"{self.base}/bots",
                        headers={"Authorization": f"Bearer {self.tok}"},
                        data={"name": self.n, "description": self.d,
                              "tags": self.t},
                        files={"file": ("bot.py", self.b, "text/x-python")},
                        timeout=20,
                    )
                    if r.ok:
                        self.done.emit(
                            True, f"Published as {r.json().get('slug','?')}")
                    else:
                        msg = r.json().get("detail", f"HTTP {r.status_code}") \
                              if r.headers.get("content-type","").startswith("application/json") \
                              else f"HTTP {r.status_code}"
                        self.done.emit(False, msg)
                except Exception as e:
                    self.done.emit(False, str(e))

        self._pub_worker = _UpWorker(
            load_server_url(), token, name.strip(), desc.strip(),
            tags.strip(), blob)
        def _on_pub(ok, msg):
            box = QMessageBox.information if ok else QMessageBox.warning
            box(self, "Publish bot", msg)
            if ok:
                self._refresh_library()
        self._pub_worker.done.connect(_on_pub)
        self._pub_worker.start()


# ─────────────────────────────────────────
# UPDATE WORKERS
# ─────────────────────────────────────────

class UpdateWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, update_info):
        super().__init__()
        self.update_info = update_info

    def run(self):
        ok, msg = download_and_apply(
            self.update_info,
            progress_callback=lambda p, m: self.progress.emit(p, m))
        self.finished.emit(ok, msg)


class UpdateChecker(QThread):
    update_available = pyqtSignal(dict)

    def run(self):
        info = check_for_update()
        if info:
            self.update_available.emit(info)


# ─────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────

class ApexWindow(QMainWindow):

    def __init__(self, user: dict = None):
        super().__init__()
        self._user = user or {}
        self.setWindowTitle(f"APEX Trading Platform  v{get_current_version()}")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)
        self.setStyleSheet(DARK_STYLESHEET)

        # State
        self._bot_tabs:   dict[str, BotTab]            = {}
        self._bot_dots:   dict[str, StatusDot]          = {}
        self._bot_ctrls:  dict[str, TabQuickControls]   = {}
        self._mkt_open_prev = None
        self._tab_indices: dict[str, int]               = {}

        # Central gradient widget
        central = GradientBackground()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setSpacing(0)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Header
        root_layout.addWidget(self._build_header())

        # ── TAB WIDGET ──────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName("mainTabs")
        root_layout.addWidget(self.tabs)

        # ── STATIC TABS ─────────────────────────────────────
        self.overview_tab  = OverviewTab()
        self.more_bots_tab = MoreBotsTab()
        self.universe_tab  = UniverseTab()
        self.tools_tab     = ToolsTab()

        self._overview_idx  = self.tabs.addTab(self.overview_tab,  "◈  OVERVIEW")
        self._morebots_idx  = self.tabs.addTab(self.more_bots_tab, "⊕  MORE BOTS")

        # Universe + Tools added as real tabs but hidden — corner buttons drive them
        self._universe_idx  = self.tabs.addTab(self.universe_tab, "")
        self._tools_idx     = self.tabs.addTab(self.tools_tab,    "")
        self.tabs.tabBar().setTabVisible(self._universe_idx, False)
        self.tabs.tabBar().setTabVisible(self._tools_idx, False)

        # ── CORNER WIDGET (Universe / Tools) ────────────────
        self.tabs.setCornerWidget(self._build_corner(), Qt.Corner.TopRightCorner)

        # ── DYNAMIC BOT TABS ─────────────────────────────────
        self._insert_bot_tabs()

        # Connect more-bots signals
        self.more_bots_tab.bot_added.connect(self._on_bot_added)
        self.more_bots_tab.bot_removed.connect(self._on_bot_removed)
        self.more_bots_tab.bot_silenced.connect(self._on_bot_silenced)
        self.more_bots_tab.bot_unsilenced.connect(self._on_bot_unsilenced)

        # Status bar
        self.statusBar().setStyleSheet(
            f"background:{C['panel']};color:{C['muted']};"
            f"font-family:'JetBrains Mono';font-size:11px;padding:2px 8px;"
        )
        self.statusBar().showMessage("APEX ready")

        # Timers
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(1000)

        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._refresh_all)
        self.refresh_timer.start(45_000)

        self._blink_timer = QTimer()
        self._blink_timer.start(500)   # dot blink ticks handled per-dot

        self._mkt_open_prev = None
        self.sched_timer = QTimer()
        self.sched_timer.timeout.connect(self._tick_schedule)
        self.sched_timer.start(60_000)
        QTimer.singleShot(8_000, self._tick_schedule)

        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._maybe_auto_update)
        self.update_timer.start(60 * 60 * 1000)
        QTimer.singleShot(15_000, self._maybe_auto_update)

        self._setup_tray()
        QTimer.singleShot(600, self._refresh_all)

    # ── HEADER ──────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("appHeader")
        header.setFixedHeight(54)
        header.setStyleSheet(
            f"QFrame#appHeader {{"
            f"  background:{C['panel']};"
            f"  border-bottom:1px solid {C['border']};"
            f"}}"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 0, 20, 0)

        logo = QLabel("APEX")
        logo.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;"
            f"color:{C['green']};letter-spacing:4px;"
        )
        sub = QLabel("TRADING PLATFORM")
        sub.setStyleSheet(
            f"font-size:9px;color:{C['muted']};letter-spacing:3px;"
            f"margin-top:2px;"
        )
        brand_v = QVBoxLayout()
        brand_v.setSpacing(0)
        brand_v.addWidget(logo)
        brand_v.addWidget(sub)
        layout.addLayout(brand_v)
        layout.addStretch()

        self.mkt_label = QLabel("● CHECKING...")
        self.mkt_label.setStyleSheet(
            f"font-size:10px;font-weight:600;letter-spacing:2px;"
            f"color:{C['muted']};padding:3px 12px;"
            f"border:1px solid {C['border']};border-radius:12px;"
        )
        layout.addWidget(self.mkt_label)

        self.clock_label = QLabel()
        self.clock_label.setStyleSheet(
            f"font-size:11px;color:{C['muted']};margin-left:18px;"
        )
        layout.addWidget(self.clock_label)

        self.update_btn = QPushButton("⬆ UPDATE AVAILABLE")
        self.update_btn.setVisible(False)
        self.update_btn.setObjectName("updateBtn")
        self.update_btn.clicked.connect(self._show_update_dialog)
        layout.addWidget(self.update_btn)

        # Logged-in user chip
        display = self._user.get("display_name") or self._user.get("username", "")
        if display:
            user_lbl = QLabel(f"▸  {display}")
            user_lbl.setStyleSheet(
                f"font-size:10px;color:{C['muted']};margin-left:12px;"
                f"letter-spacing:0.5px;"
            )
            layout.addWidget(user_lbl)

        return header

    # ── CORNER WIDGET ────────────────────────────────────────

    def _build_corner(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 8, 0)
        row.setSpacing(0)

        self._corner_universe = QPushButton("✦  UNIVERSE")
        self._corner_tools    = QPushButton("⚙  TOOLS")

        for btn in (self._corner_universe, self._corner_tools):
            btn.setObjectName("cornerBtn")
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(btn)

        self._corner_universe.clicked.connect(
            lambda: self._switch_corner(self._universe_idx, self._corner_universe))
        self._corner_tools.clicked.connect(
            lambda: self._switch_corner(self._tools_idx, self._corner_tools))
        self.tabs.currentChanged.connect(self._on_tab_changed)
        return w

    def _switch_corner(self, idx: int, btn: QPushButton):
        self.tabs.setCurrentIndex(idx)
        self._corner_universe.setProperty("active", str(btn is self._corner_universe).lower())
        self._corner_tools.setProperty("active", str(btn is self._corner_tools).lower())
        for b in (self._corner_universe, self._corner_tools):
            b.style().unpolish(b)
            b.style().polish(b)

    def _on_tab_changed(self, idx: int):
        # Deactivate corner buttons if we switched away from their tabs
        if idx not in (self._universe_idx, self._tools_idx):
            for b in (self._corner_universe, self._corner_tools):
                b.setProperty("active", "false")
                b.setChecked(False)
                b.style().unpolish(b)
                b.style().polish(b)

    # ── BOT TAB MANAGEMENT ──────────────────────────────────

    def _insert_bot_tabs(self):
        """Insert bot tabs based on registry (between Overview and More Bots)."""
        reg = _load_registry()
        for side in reg["active"]:
            self._add_bot_tab(side, silenced=side in reg["silenced"])

    def _add_bot_tab(self, side: str, silenced: bool = False):
        if side in self._bot_tabs:
            return

        info = BUILTIN_BOTS.get(side)
        if info:
            script = D.BOT_SCRIPTS.get(side, Path())
            color  = info["color"]
            label  = info["label"]
        else:
            # Custom bot
            reg = _load_registry()
            custom_info = next(
                (c for c in reg.get("custom", []) if c["id"] == side), {})
            script = Path(custom_info.get("script", ""))
            color  = custom_info.get("color", C["purple"])
            label  = custom_info.get("label", side)

        tab = BotTab(side) if side in BUILTIN_BOTS else BotTab.__new__(BotTab)
        if side not in BUILTIN_BOTS:
            # Minimal init for custom bot — treat like a generic BotTab
            BotTab.__init__(tab, side)

        self._bot_tabs[side] = tab

        # Insert BEFORE the More Bots tab
        insert_at = self._morebots_idx
        self.tabs.insertTab(insert_at, tab, label)

        # Shift static indices
        self._morebots_idx += 1
        self._universe_idx += 1
        self._tools_idx    += 1

        # Update tab button visibility
        self.tabs.tabBar().setTabVisible(self._universe_idx, False)
        self.tabs.tabBar().setTabVisible(self._tools_idx, False)

        actual_idx = self.tabs.indexOf(tab)
        self._tab_indices[side] = actual_idx

        # Status dot (left side)
        dot = StatusDot()
        self._bot_dots[side] = dot
        self.tabs.tabBar().setTabButton(
            actual_idx, QTabBar.ButtonPosition.LeftSide, dot)

        # Quick controls (right side)
        ctrl = TabQuickControls()
        self._bot_ctrls[side] = ctrl
        self.tabs.tabBar().setTabButton(
            actual_idx, QTabBar.ButtonPosition.RightSide, ctrl)

        # Wire quick controls
        ctrl.play_clicked.connect(lambda s=side: self._quick_play(s))
        ctrl.stop_clicked.connect(lambda s=side: self._quick_stop(s))
        ctrl.remove_clicked.connect(lambda s=side: self._on_bot_removed(s))

        # Wire bot status signal
        bot_ctrl = getattr(tab, "bot_ctrl", None)
        if bot_ctrl:
            bot_ctrl.status_changed.connect(self._on_bot_status_changed)

        # Apply silenced state
        if silenced:
            self._grey_tab(side, True)

    def _remove_bot_tab(self, side: str):
        tab = self._bot_tabs.pop(side, None)
        if tab is None:
            return
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.removeTab(idx)
            # Shift static indices back
            if idx < self._morebots_idx:
                self._morebots_idx -= 1
            if idx < self._universe_idx:
                self._universe_idx -= 1
            if idx < self._tools_idx:
                self._tools_idx -= 1
            self.tabs.tabBar().setTabVisible(self._universe_idx, False)
            self.tabs.tabBar().setTabVisible(self._tools_idx, False)

        self._bot_dots.pop(side, None)
        self._bot_ctrls.pop(side, None)
        self._tab_indices.pop(side, None)
        tab.deleteLater()

    def _grey_tab(self, side: str, silenced: bool):
        tab = self._bot_tabs.get(side)
        if not tab:
            return
        idx = self.tabs.indexOf(tab)
        if idx < 0:
            return
        color = C["muted"] if silenced else BUILTIN_BOTS.get(side, {}).get("color", C["text"])
        self.tabs.tabBar().setTabTextColor(idx, QColor(color if not silenced else C["border"]))

        # Disable bot controls when silenced
        bot_ctrl = getattr(tab, "bot_ctrl", None)
        if bot_ctrl:
            bot_ctrl.run_btn.setEnabled(not silenced)

        dot = self._bot_dots.get(side)
        if dot:
            dot.set_state("silenced" if silenced else "stopped")

    # ── REGISTRY SIGNALS ─────────────────────────────────────

    def _on_bot_added(self, side: str):
        self._add_bot_tab(side)

    def _on_bot_removed(self, side: str):
        # Stop bot if running
        tab = self._bot_tabs.get(side)
        if tab:
            bot_ctrl = getattr(tab, "bot_ctrl", None)
            if bot_ctrl and bot_ctrl.is_running():
                bot_ctrl.stop_bot()
        reg = _load_registry()
        if side in reg["active"]:
            reg["active"].remove(side)
        if side in reg["silenced"]:
            reg["silenced"].remove(side)
        _save_registry(reg)
        self._remove_bot_tab(side)
        self.more_bots_tab.refresh()

    def _on_bot_silenced(self, side: str):
        tab = self._bot_tabs.get(side)
        if tab:
            bot_ctrl = getattr(tab, "bot_ctrl", None)
            if bot_ctrl and bot_ctrl.is_running():
                bot_ctrl.stop_bot()
        self._grey_tab(side, True)
        self._update_overview_dot(side, "silenced")

    def _on_bot_unsilenced(self, side: str):
        self._grey_tab(side, False)
        self._update_overview_dot(side, "stopped")

    # ── QUICK CONTROLS ───────────────────────────────────────

    def _quick_play(self, side: str):
        tab = self._bot_tabs.get(side)
        if not tab:
            return
        reg = _load_registry()
        if side in reg.get("silenced", []):
            return
        bot_ctrl = getattr(tab, "bot_ctrl", None)
        if bot_ctrl and not bot_ctrl.is_running():
            bot_ctrl.start_bot()

    def _quick_stop(self, side: str):
        tab = self._bot_tabs.get(side)
        if not tab:
            return
        bot_ctrl = getattr(tab, "bot_ctrl", None)
        if bot_ctrl and bot_ctrl.is_running():
            bot_ctrl.stop_bot()

    # ── BOT STATUS SIGNAL ────────────────────────────────────

    def _on_bot_status_changed(self, side: str, is_running: bool):
        mkt_open = self._mkt_open_prev

        if is_running:
            state = "running" if mkt_open else "sleeping"
        else:
            auto   = D.get_auto_schedule()
            state  = "scheduled" if (auto and not mkt_open) else "stopped"

        dot = self._bot_dots.get(side)
        if dot:
            dot.set_state(state)

        ctrl = self._bot_ctrls.get(side)
        if ctrl:
            ctrl.set_running(is_running)

        self._update_overview_dot(side, state)

    def _update_overview_dot(self, side: str, state: str):
        if hasattr(self, "overview_tab"):
            self.overview_tab.update_bot_status(side, state)

    # ── TRAY ─────────────────────────────────────────────────

    def _setup_tray(self):
        try:
            self.tray = QSystemTrayIcon(self)
            pm = QPixmap(16, 16)
            pm.fill(QColor(C["green"]))
            self.tray.setIcon(QIcon(pm))
            menu = QMenu()
            menu.addAction(QAction("Show APEX", self, triggered=self.show))
            menu.addSeparator()
            menu.addAction(QAction("Sign Out", self, triggered=self._sign_out))
            menu.addAction(QAction("Quit", self, triggered=QApplication.quit))
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
        except Exception as e:
            print(f"[tray] {e}")

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()

    def _sign_out(self):
        from ui.login import clear_auth, LoginWindow
        clear_auth()
        self.hide()
        login = LoginWindow()
        login.auth_success.connect(
            lambda tok, usr: self._on_relogin(login, tok, usr))
        login.offline_mode.connect(lambda: login.close())
        login.show()
        QApplication.instance()._relogin_win = login

    def _on_relogin(self, login_win, token: str, user: dict):
        from ui.login import save_auth
        save_auth(token, user)
        login_win.close()
        self._user = user
        # Update header user label if present
        display = user.get("display_name") or user.get("username", "")
        for child in self.centralWidget().findChildren(QLabel):
            if child.text().startswith("▸  "):
                child.setText(f"▸  {display}")
                break
        self.show()
        self.raise_()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "APEX", "Running in background.",
                QSystemTrayIcon.MessageIcon.Information, 2000)

    # ── CLOCK ────────────────────────────────────────────────

    def _tick_clock(self):
        from datetime import datetime
        self.clock_label.setText(
            datetime.now().strftime("%a %d %b %Y  —  %H:%M:%S"))

    # ── REFRESH ──────────────────────────────────────────────

    def _refresh_all(self):
        self.statusBar().showMessage("Refreshing data...")
        try:
            idx = self.tabs.currentIndex()
            tab = self.tabs.widget(idx)
            if tab and hasattr(tab, "refresh"):
                tab.refresh()
            self._refresh_market_status()
            self.statusBar().showMessage(
                f"Last updated: "
                f"{__import__('datetime').datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self.statusBar().showMessage(f"Refresh error: {e}")

    def _refresh_market_status(self):
        try:
            c = D.get_client("LONG")
            if not c:
                return
            ck = c.get_clock()
            if ck.is_open:
                self._mkt_open_prev = True
                self.mkt_label.setText("● MARKET OPEN")
                self.mkt_label.setStyleSheet(
                    f"font-size:10px;font-weight:600;letter-spacing:2px;"
                    f"color:{C['green']};padding:3px 12px;"
                    f"border:1px solid {C['green']};border-radius:12px;"
                    f"background:rgba(63,184,154,0.08);"
                )
            else:
                self._mkt_open_prev = False
                nxt = ck.next_open.astimezone().strftime("%b %d %H:%M")
                self.mkt_label.setText(f"● CLOSED  {nxt}")
                self.mkt_label.setStyleSheet(
                    f"font-size:10px;font-weight:600;letter-spacing:2px;"
                    f"color:{C['muted']};padding:3px 12px;"
                    f"border:1px solid {C['border']};border-radius:12px;"
                )
            # Re-sync all running dots now that we know market state
            for side, tab in self._bot_tabs.items():
                bot_ctrl = getattr(tab, "bot_ctrl", None)
                if bot_ctrl:
                    self._on_bot_status_changed(side, bot_ctrl.is_running())
        except Exception:
            pass

    # ── AUTO-TRADE SCHEDULE ──────────────────────────────────

    def _tick_schedule(self):
        try:
            if not D.get_auto_schedule():
                return
            from core.schedule import market_is_open
            is_open = market_is_open()
            if is_open is None:
                return
            bot_tabs = list(self._bot_tabs.values())
            if is_open and self._mkt_open_prev in (None, False):
                for t in bot_tabs:
                    bc = getattr(t, "bot_ctrl", None)
                    if bc and not bc.is_running():
                        bc.start_bot()
                self.statusBar().showMessage(
                    "Auto-schedule: market OPEN — bots started")
            elif (not is_open) and self._mkt_open_prev in (None, True):
                for t in bot_tabs:
                    bc = getattr(t, "bot_ctrl", None)
                    if bc and bc.is_running():
                        bc.stop_bot()
                self.statusBar().showMessage(
                    "Auto-schedule: market CLOSED — bots stopped")
            self._mkt_open_prev = is_open
        except Exception:
            pass

    # ── AUTO-UPDATE ──────────────────────────────────────────

    def _maybe_auto_update(self):
        if not getattr(sys, "frozen", False):
            return
        try:
            from core.schedule import market_is_open, update_allowed_now
            if not update_allowed_now(market_is_open()):
                return
        except Exception:
            return
        self._auto_checker = UpdateChecker()
        self._auto_checker.update_available.connect(self._auto_apply_update)
        self._auto_checker.start()

    def _auto_apply_update(self, info: dict):
        try:
            from core.schedule import market_is_open, update_allowed_now
            if not update_allowed_now(market_is_open()):
                self._on_update_found(info)
                return
        except Exception:
            return
        self._do_update(info)

    def _check_updates(self):
        self._update_checker = UpdateChecker()
        self._update_checker.update_available.connect(self._on_update_found)
        self._update_checker.start()

    def _on_update_found(self, info: dict):
        self._pending_update = info
        self.update_btn.setVisible(True)
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "APEX Update Available",
                f"v{info['latest']} is ready to install.",
                QSystemTrayIcon.MessageIcon.Information, 5000)

    def _show_update_dialog(self):
        info = getattr(self, "_pending_update", {})
        msg  = QMessageBox(self)
        msg.setWindowTitle("Update Available")
        msg.setText(
            f"<b>APEX v{info.get('latest','?')} is available</b><br><br>"
            f"Current: {info.get('current','?')}<br><br>"
            f"<i>{info.get('notes','')}</i><br><br>"
            f"Update now? The app will restart automatically."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setStyleSheet(self.styleSheet())
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self._do_update(info)

    def _do_update(self, info: dict):
        progress = QProgressDialog("Downloading update...", None, 0, 100, self)
        progress.setWindowTitle("Updating APEX")
        progress.setModal(True)
        progress.show()
        self._update_worker = UpdateWorker(info)
        self._update_worker.progress.connect(
            lambda p, m: (progress.setValue(p), progress.setLabelText(m)))
        self._update_worker.finished.connect(
            lambda ok, m: self._on_update_done(ok, m, progress))
        self._update_worker.start()

    def _on_update_done(self, ok: bool, msg: str, progress):
        progress.close()
        if not ok:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Update Failed")
            dlg.setText(msg)
            dlg.setStyleSheet(self.styleSheet())
            dlg.exec()
            return
        local = (getattr(self, "_pending_update", {}) or {}).get("_local_path")
        if local and getattr(sys, "frozen", False):
            box = QMessageBox(self)
            box.setWindowTitle("Update ready")
            box.setText(
                f"<b>{msg}</b><br><br>"
                "Click <b>Install now</b> to apply. APEX will close while "
                "the installer runs (~1 min) then reopen automatically."
            )
            box.setStyleSheet(self.styleSheet())
            install_btn = box.addButton("Install now",
                                        QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is install_btn:
                launch_downloaded_installer(local)
            return
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Update Complete")
        dlg.setText(msg)
        dlg.setStyleSheet(self.styleSheet())
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()
        restart_app()


# ─────────────────────────────────────────
# BOT ENTRY POINT (frozen build)
# ─────────────────────────────────────────

def _run_bot(side: str) -> int:
    import io
    for _fd, _name in ((1, "stdout"), (2, "stderr")):
        _s = getattr(sys, _name, None)
        try:
            if _s is None:
                setattr(sys, _name, io.TextIOWrapper(
                    open(_fd, "wb", buffering=0),
                    encoding="utf-8", line_buffering=True))
            else:
                _s.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:
            pass
    ensure_data_dir()
    try:
        os.chdir(DATA_DIR)
    except Exception:
        pass
    import importlib
    side_u = side.upper()
    if side_u == "UNIVERSE":
        um = importlib.import_module("universe_manager")
        um.run_manager(["DAY", "LONG", "SHORT"])
        return 0
    mod_name = {"LONG": "longbot_v2",
                "SHORT": "shortbot_v2",
                "DAY": "daybot"}.get(side_u)
    if not mod_name:
        print(f"Unknown bot: {side}", flush=True)
        return 2
    bot = importlib.import_module(mod_name)
    bot.main()
    return 0


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-bot":
        sys.exit(_run_bot(sys.argv[2]))

    if not _check_single_instance():
        sys.exit(0)

    ensure_data_dir()
    app = QApplication(sys.argv)
    app.setApplicationName("APEX Trading Platform")
    app.setApplicationVersion(get_current_version())
    app.setQuitOnLastWindowClosed(False)

    from ui.widgets import WheelGuard
    app._wheel_guard = WheelGuard()
    app.installEventFilter(app._wheel_guard)

    try:
        QFontDatabase.addApplicationFont("assets/JetBrainsMono-Regular.ttf")
    except Exception:
        pass

    from ui.login import (
        LoginWindow, TokenVerifyWorker,
        load_auth, save_auth, clear_auth, load_server_url,
    )

    def _launch(user: dict):
        w = ApexWindow(user=user)
        app._main_window = w
        w.show()

    def _on_login_success(login_win, token: str, user: dict):
        login_win.close()
        _launch(user)

    def _on_offline(login_win):
        login_win.close()
        _launch({"display_name": "Offline"})

    stored = load_auth()

    if stored and stored.get("token"):
        # ── Have a stored token: launch immediately, verify in background ──
        _launch(stored.get("user", {}))

        # Background token verification — if expired show re-login dialog
        def _on_token_invalid():
            clear_auth()
            mw = getattr(app, "_main_window", None)
            if mw:
                from PyQt6.QtWidgets import QMessageBox
                box = QMessageBox(mw)
                box.setWindowTitle("Session Expired")
                box.setText(
                    "Your session has expired. Please log in again.\n\n"
                    "(Your trading bots and settings are unaffected.)"
                )
                box.setStyleSheet(mw.styleSheet())
                box.setStandardButtons(QMessageBox.StandardButton.Ok)
                box.exec()
                mw._sign_out()

        srv_url = load_server_url()
        verify = TokenVerifyWorker(stored["token"], srv_url)
        verify.valid.connect(lambda u: save_auth(stored["token"], u))   # refresh user info
        verify.invalid.connect(_on_token_invalid)
        # offline → do nothing (keep user logged in)
        app._token_verify = verify
        QTimer.singleShot(3_000, verify.start)   # defer 3s so app opens first

    else:
        # ── No token: show login window ──────────────────────────────────────
        login = LoginWindow()
        login.auth_success.connect(
            lambda tok, usr: _on_login_success(login, tok, usr))
        login.offline_mode.connect(
            lambda: _on_offline(login))
        app._login = login
        login.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

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

from ui.overview     import OverviewTab
from ui.bot_tab      import BotTab
from ui.overview     import ToolsTab
from ui.universe     import UniverseTab
from ui.make_bot_tab import MakeBotTab    # V7.1.9
from ui.friends_tab  import FriendsTab    # V3.0.0
from ui.account_tab  import AccountTab    # V3 wave 4
from ui.admin_tab    import AdminTab      # V3 wave 5
from ui.bot_market_tab import BotMarketTab  # V3.1.3
from ui.styles     import DARK_STYLESHEET, COLORS
from core.updater  import (check_for_update, download_and_apply,
                            get_current_version, restart_app,
                            launch_downloaded_installer)
from core.paths    import DATA_DIR, ensure_data_dir
import core.data   as D

C = COLORS


# ─────────────────────────────────────────
# WINDOW ICON
# ─────────────────────────────────────────

def _app_icon() -> QIcon:
    """v3.1.6 — return the APEX icon for the title bar.
    Tries the frozen-bundle location first (sys._MEIPASS/assets), falls
    back to the source-tree location for dev runs. If neither exists,
    returns a blank QIcon so setWindowIcon doesn't crash."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "icon.ico")
    exe_dir = Path(sys.executable).parent
    candidates.append(exe_dir / "_internal" / "assets" / "icon.ico")
    candidates.append(Path(__file__).parent / "assets" / "icon.ico")
    for p in candidates:
        try:
            if p.exists():
                return QIcon(str(p))
        except Exception:
            continue
    return QIcon()


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
        self.setFixedSize(20, 20)
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
            f"color:{col};font-size:12px;background:transparent;border:none;"
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
        row.setContentsMargins(0, 0, 4, 0)
        row.setSpacing(2)

        self._play = QPushButton("▶")
        self._stop = QPushButton("■")
        self._remove = QPushButton("✕")

        self._play.setObjectName("tabPlayBtn")
        self._stop.setObjectName("tabStopBtn")
        self._remove.setObjectName("tabRemoveBtn")

        for btn in (self._play, self._stop, self._remove):
            btn.setFixedSize(16, 16)

        # Pack: play / stop (mutually exclusive), then breathing room,
        # then the remove ✕. V7.1.1 — the ✕ used to sit flush against
        # the play button which made mis-clicks easy.
        row.addWidget(self._play)
        row.addWidget(self._stop)
        row.addSpacing(8)
        row.addWidget(self._remove)

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
            "Drag-and-drop a .py file anywhere on the APEX window, or "
            "click Browse below. The script must expose a main() "
            "function. Read the skeleton guide first if you're new to "
            "writing bots — it explains every constraint and includes "
            "a minimal working example.")
        custom_info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        custom_info.setWordWrap(True)
        s.add(custom_info)

        upload_row = QHBoxLayout()
        upload_btn = QPushButton("📂  Browse .py file...")
        upload_btn.setObjectName("toolBtn")
        upload_btn.clicked.connect(self._upload_bot)
        skel_btn = QPushButton("📖  Open skeleton guide")
        skel_btn.setObjectName("toolBtn")
        skel_btn.clicked.connect(self._open_skeleton_guide)
        self._upload_msg = QLabel("")
        self._upload_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        upload_row.addWidget(upload_btn)
        upload_row.addWidget(skel_btn)
        upload_row.addWidget(self._upload_msg)
        upload_row.addStretch()
        uw = QWidget()
        uw.setLayout(upload_row)
        s.add(uw)

        # ─────────────────────────────────────────────────────
        # V3.1.5 — Bot library lock
        # ─────────────────────────────────────────────────────
        lock_wrap = QFrame()
        lock_wrap.setStyleSheet(
            f"background:{C['panel2']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        lv = QVBoxLayout(lock_wrap)
        lv.setContentsMargins(16, 12, 16, 12)
        lv.setSpacing(6)

        lock_head = QHBoxLayout()
        lock_title = QLabel("🔒  LIBRARY LOCK")
        lock_title.setStyleSheet(
            f"color:{C['muted']};font-size:9px;letter-spacing:3px;"
            f"font-weight:700;")
        lock_head.addWidget(lock_title)
        lock_head.addStretch()
        self._lock_status = QLabel("")
        self._lock_status.setStyleSheet(f"color:{C['green']};font-size:10px;")
        lock_head.addWidget(self._lock_status)
        lhw = QWidget(); lhw.setLayout(lock_head)
        lv.addWidget(lhw)

        lock_desc = QLabel(
            "Encrypt every custom bot's .py file → .apex so the bot "
            "library can't be opened by a text editor or sync utility. "
            "<b>Casual protection only</b> — anyone with APEX.exe can "
            "extract the key. For real isolation, run bots in cloud "
            "mode (Tools → AUTOMATION).")
        lock_desc.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.5;")
        lock_desc.setWordWrap(True)
        lv.addWidget(lock_desc)

        lock_row = QHBoxLayout()
        self._lock_btn = QPushButton("🔒  Lock library")
        self._lock_btn.setObjectName("toolBtn")
        self._lock_btn.clicked.connect(self._toggle_lock)
        lock_row.addWidget(self._lock_btn)
        lock_row.addStretch()
        lrw = QWidget(); lrw.setLayout(lock_row)
        lv.addWidget(lrw)
        s.add(lock_wrap)
        self._refresh_lock_state()

        # ─────────────────────────────────────────────────────
        # V3.1.3 — BOT MARKET launcher
        # The full marketplace lives in its own tab (ui/bot_market_tab.py).
        # It only appears in the tab bar once the user opens it from here,
        # keeping the MORE BOTS view focused on local bot management.
        # ─────────────────────────────────────────────────────
        market_wrap = QFrame()
        market_wrap.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:12px;border-top:3px solid {C['purple']};")
        mv = QVBoxLayout(market_wrap)
        mv.setContentsMargins(24, 20, 24, 20)
        mv.setSpacing(8)
        title = QLabel("🛒  BOT MARKET")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:18px;font-weight:800;"
            f"color:{C['text']};letter-spacing:3px;")
        mv.addWidget(title)
        sub = QLabel("Browse, install and publish bots from the APEX network. "
                      "Featured picks, personalised recommendations, full search, "
                      "publisher analytics — open it in its own tab.")
        sub.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.6;")
        sub.setWordWrap(True)
        mv.addWidget(sub)

        open_row = QHBoxLayout()
        open_row.setContentsMargins(0, 6, 0, 0)
        open_btn = QPushButton("🛒  Open Bot Market")
        open_btn.setObjectName("addBotBtn")
        open_btn.setMinimumHeight(40)
        open_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background:rgba(138,147,201,0.12);"
            f"  color:{C['purple']};"
            f"  border:1px solid {C['purple']};"
            f"  border-radius:8px;"
            f"  font-family:'JetBrains Mono';font-size:11px;"
            f"  letter-spacing:3px;font-weight:700;"
            f"  padding:10px 26px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background:rgba(138,147,201,0.22);"
            f"}}"
        )
        open_btn.clicked.connect(self._open_bot_market)
        open_row.addWidget(open_btn)
        open_row.addStretch()
        ow = QWidget(); ow.setLayout(open_row)
        mv.addWidget(ow)
        s.add(market_wrap)

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

    def _open_bot_market(self):
        """V3.1.3 — tell ApexWindow to reveal + focus the BOT MARKET tab."""
        win = self.window()
        if hasattr(win, "_open_bot_market"):
            win._open_bot_market()

    # ── V3.1.5 — Library lock / unlock ──────────────────────

    def _refresh_lock_state(self):
        from core.paths  import DATA_DIR
        from core        import secure
        bots_dir = DATA_DIR / "bots"
        locked   = secure.is_locked(bots_dir)
        if locked:
            self._lock_btn.setText("🔓  Unlock library")
            self._lock_status.setText("LOCKED")
            self._lock_status.setStyleSheet(
                f"color:{C['orange']};font-size:10px;font-weight:700;"
                f"letter-spacing:2px;")
        else:
            self._lock_btn.setText("🔒  Lock library")
            self._lock_status.setText("UNLOCKED")
            self._lock_status.setStyleSheet(
                f"color:{C['muted']};font-size:10px;letter-spacing:2px;")

    def _toggle_lock(self):
        from core.paths import DATA_DIR
        from core       import secure
        bots_dir = DATA_DIR / "bots"
        if not secure.HAS_CRYPTO:
            QMessageBox.warning(
                self, "Missing dependency",
                "Locking requires the 'cryptography' Python package. "
                "Install it: pip install cryptography")
            return
        if secure.is_locked(bots_dir):
            unlocked = secure.unlock_bot_library(bots_dir)
            self._sync_registry_after_lock(unlocked, locked=False)
            self._upload_msg.setText(
                f"✓ Unlocked {len(unlocked)} bot{'s' if len(unlocked)!=1 else ''}")
        else:
            if QMessageBox.question(
                    self, "Lock library",
                    "Encrypt every custom bot in your library?\n\n"
                    "After locking, the .py files become .apex and can "
                    "no longer be opened by a text editor. You can "
                    "unlock the library any time from this same button."
            ) != QMessageBox.StandardButton.Yes:
                return
            locked = secure.lock_bot_library(bots_dir)
            self._sync_registry_after_lock(locked, locked=True)
            self._upload_msg.setText(
                f"✓ Locked {len(locked)} bot{'s' if len(locked)!=1 else ''}")
        QTimer.singleShot(4000, lambda: self._upload_msg.setText(""))
        self._refresh_lock_state()

    def _sync_registry_after_lock(self, slugs: list, *, locked: bool):
        """Rewrite custom-bot registry entries so their `script` field
        points to whichever file extension currently exists on disk."""
        from core.paths import DATA_DIR
        s = D.load_settings()
        reg = s.get("bot_registry",
                    {"active": [], "silenced": [], "custom": []})
        new_ext = ".apex" if locked else ".py"
        for c in reg.get("custom", []):
            if c.get("id") in slugs:
                old = Path(c.get("script", ""))
                c["script"] = str(old.with_suffix(new_ext))
        s["bot_registry"] = reg
        with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)

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

    def _open_skeleton_guide(self):
        """V7.1.1: open BOT_SKELETON.md so the user can read or paste
        it into an AI chat for help crafting a bot. The file ships
        inside the PyInstaller bundle, so we look there first; falls
        back to the project root for dev runs."""
        import subprocess
        from pathlib import Path as _P
        candidates = []
        # PyInstaller --onedir build: data files land in sys._MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(_P(meipass) / "BOT_SKELETON.md")
        # source / dev run
        candidates.append(_P(__file__).parent / "BOT_SKELETON.md")
        for p in candidates:
            if p.exists():
                try:
                    os.startfile(str(p))
                except AttributeError:
                    # non-Windows fallback
                    subprocess.Popen(["xdg-open", str(p)])
                return
        QMessageBox.warning(
            self, "Skeleton guide missing",
            "BOT_SKELETON.md was not bundled in this build. You can "
            "read it on GitHub: "
            "https://github.com/B4ptiste16/ApexTrading/blob/main/BOT_SKELETON.md")

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
    # NOTE: the actual marketplace UI moved to ui/bot_market_tab.py in V3.1.3.
    # _install_public_bot stays here so the old drag-and-drop install
    # paths in ApexWindow still work.

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
        """Pick a .py via file dialog, then publish it."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select bot script to publish",
            str(DATA_DIR / "bots"), "Python Files (*.py)")
        if path:
            self._publish_bot_with_path(path)

    def _publish_bot_with_path(self, path: str):
        """Upload a known .py file to the public marketplace. Called
        directly by the drag-and-drop handler in ApexWindow."""
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

        blob = Path(path).read_bytes()
        name, ok = QInputDialog.getText(
            self, "Bot name", "Display name:", text=Path(path).stem)
        if not ok or not name.strip():
            return
        desc, _ = QInputDialog.getText(
            self, "Description", "One-line description:")
        tags, _ = QInputDialog.getText(
            self, "Tags", "Comma-separated tags (optional):")
        # V3.1.1 — capture philosophy + price so the listing surfaces in
        # the new filters and the publisher can monetise.
        philos_choices = ["long", "short", "day", "options", "momentum",
                          "mean-reversion", "scalping", "swing", "other"]
        philos, ok_p = QInputDialog.getItem(
            self, "Philosophy",
            "Trading philosophy (helps users find your bot):",
            philos_choices, 0, False)
        if not ok_p:
            philos = ""
        price, _ = QInputDialog.getInt(
            self, "Price",
            "Price in APEX credits  (0 = free):", 0, 0, 1_000_000, 10)

        from PyQt6.QtCore import QThread, pyqtSignal as _Sig

        class _UpWorker(QThread):
            done = _Sig(bool, str)

            def __init__(self, base, tok, n, d, t, b, ph, pr):
                super().__init__()
                self.base, self.tok, self.n, self.d, self.t, self.b = \
                    base, tok, n, d, t, b
                self.ph, self.pr = ph, pr

            def run(self):
                import requests
                try:
                    r = requests.post(
                        f"{self.base}/bots",
                        headers={"Authorization": f"Bearer {self.tok}"},
                        data={"name": self.n, "description": self.d,
                              "tags": self.t,
                              "philosophy": self.ph,
                              "price_credits": self.pr},
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
            tags.strip(), blob,
            philos if philos != "other" else "",
            int(price))
        def _on_pub(ok, msg):
            box = QMessageBox.information if ok else QMessageBox.warning
            box(self, "Publish bot", msg)
            if ok:
                self._refresh_library()
        self._pub_worker.done.connect(_on_pub)
        self._pub_worker.start()


# ─────────────────────────────────────────
# SWEEP LABEL  (shimmer animation for MARKET CLOSED)
# ─────────────────────────────────────────

class SweepLabel(QLabel):
    """QLabel that paints a right-to-left shimmer when sweep is active."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._sweep_x = 1.2
        self._sweeping = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)

    def start_sweep(self):
        if self._sweeping:
            return
        self._sweeping = True
        self._sweep_x = 1.2
        self._timer.start(16)

    def stop_sweep(self):
        self._sweeping = False
        self._timer.stop()
        self.update()

    def _step(self):
        self._sweep_x -= 0.014
        if self._sweep_x < -0.4:
            self._sweep_x = 1.2
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._sweeping:
            return
        painter = QPainter(self)
        w, h = self.width(), self.height()
        cx = self._sweep_x * w
        sw = w * 0.40
        grad = QLinearGradient(cx - sw, 0, cx + sw, 0)
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(255, 255, 255, 26))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(0, 0, w, h, grad)
        painter.end()


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
        self.setWindowIcon(_app_icon())   # v3.1.6 — fixes the exe-style window icon

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
        # V7.1.4: Chrome-style tab reordering. We enable movable on the
        # whole bar, then in _on_tab_moved we revert any move that
        # touches OVERVIEW / MORE BOTS / UNIVERSE / TOOLS — only
        # bot-tab ↔ bot-tab moves are accepted, and the new order is
        # persisted to the bot registry.
        self.tabs.setMovable(True)
        self.tabs.tabBar().tabMoved.connect(self._on_tab_moved)
        self._suppress_tab_move = False
        root_layout.addWidget(self.tabs)

        # ── STATIC TABS ─────────────────────────────────────
        self.overview_tab  = OverviewTab()
        self.more_bots_tab = MoreBotsTab()
        self.universe_tab  = UniverseTab()
        self.make_bot_tab  = MakeBotTab()        # V7.1.9
        self.friends_tab   = FriendsTab()        # V3.0.0
        self.account_tab   = AccountTab()        # V3 wave 4
        self.admin_tab     = AdminTab()          # V3 wave 5
        self.bot_market_tab = BotMarketTab()     # V3.1.3
        self.bot_market_tab.closed.connect(self._close_bot_market)
        self.tools_tab     = ToolsTab()

        self._overview_idx  = self.tabs.addTab(self.overview_tab,  "◈  OVERVIEW")
        self._morebots_idx  = self.tabs.addTab(self.more_bots_tab, "⊕  MORE BOTS")

        # V3.1.3 — BOT MARKET is a "summon-able" tab. Hidden by default;
        # MORE BOTS → 🛒 Open Bot Market makes it visible and switches to
        # it. The market tab itself has a ✕ Close button that hides it
        # again. Placed right after MORE BOTS so it appears in the
        # natural reading order when revealed.
        self._botmarket_idx = self.tabs.addTab(self.bot_market_tab,
                                                "🛒  BOT MARKET")

        # Corner row reads:
        #   UNIVERSE · MAKE BOT · FRIENDS · ACCOUNT · ADMIN · TOOLS
        # (ADMIN is hidden for non-admin users — see _refresh_admin_visibility.)
        self._universe_idx  = self.tabs.addTab(self.universe_tab, "")
        self._makebot_idx   = self.tabs.addTab(self.make_bot_tab, "")
        self._friends_idx   = self.tabs.addTab(self.friends_tab,  "")
        self._account_idx   = self.tabs.addTab(self.account_tab,  "")
        self._admin_idx     = self.tabs.addTab(self.admin_tab,    "")
        self._tools_idx     = self.tabs.addTab(self.tools_tab,    "")
        for idx in (self._botmarket_idx,
                    self._universe_idx, self._makebot_idx,
                    self._friends_idx, self._account_idx,
                    self._admin_idx, self._tools_idx):
            self.tabs.tabBar().setTabVisible(idx, False)

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
        # v1.2.0 — restore cloud-bot UI state on startup. If any cloud
        # bots are still running on Oracle (the server keeps them alive
        # across desktop restarts), light up their tabs without making
        # the user click ▶ again.
        QTimer.singleShot(3000, self._resume_cloud_bots)
        # V3 wave 5 — credits + admin visibility refresh, fires shortly
        # after launch and then every 60 s.
        QTimer.singleShot(1500, self._refresh_user_meta)
        self._meta_timer = QTimer()
        self._meta_timer.timeout.connect(self._refresh_user_meta)
        self._meta_timer.start(60_000)

        # V7.1.1: accept .py drops anywhere in the window so a user can
        # drag a bot script in and we'll offer to install it locally or
        # publish it to the public library.
        self.setAcceptDrops(True)

    # ── DRAG & DROP  (V7.1.1) ────────────────────────────────

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            for u in md.urls():
                p = u.toLocalFile()
                if p.lower().endswith(".py"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.toLocalFile().lower().endswith(".py")]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # Defer so the drag operation in the OS finishes cleanly before
        # we pop a modal dialog (avoids losing focus / orphaned drag).
        QTimer.singleShot(0, lambda ps=paths: self._handle_dropped_files(ps))

    def _handle_dropped_files(self, paths: list):
        for path in paths:
            self._prompt_dropped_bot(path)

    def _prompt_dropped_bot(self, path: str):
        """Ask: install locally, publish to library, or cancel."""
        name = Path(path).name
        box = QMessageBox(self)
        box.setWindowTitle("Import bot")
        box.setText(
            f"<b>{name}</b><br><br>"
            f"What would you like to do with this Python script?")
        box.setStyleSheet(self.styleSheet())
        local_btn = box.addButton(
            "Add to my library",   QMessageBox.ButtonRole.AcceptRole)
        pub_btn   = box.addButton(
            "Publish publicly…",   QMessageBox.ButtonRole.ActionRole)
        box.addButton("Cancel",    QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is local_btn:
            self._import_bot_locally(path)
        elif clicked is pub_btn:
            self._import_then_publish(path)

    def _import_bot_locally(self, path: str):
        bots_dir = DATA_DIR / "bots"
        bots_dir.mkdir(exist_ok=True)
        dest = bots_dir / Path(path).name
        shutil.copy2(path, dest)
        reg = _load_registry()
        bot_id = Path(path).stem
        existing_ids = [c["id"] for c in reg.get("custom", [])]
        if bot_id not in existing_ids:
            reg.setdefault("custom", []).append({
                "id":     bot_id,
                "label":  bot_id,
                "script": str(dest),
                "color":  C["purple"],
            })
            _save_registry(reg)
        self.more_bots_tab.refresh()
        QMessageBox.information(
            self, "Bot imported",
            f"{Path(path).name} was added to your library. Open the "
            f"MORE BOTS tab to activate it.")

    def _import_then_publish(self, path: str):
        """Reuse the existing MoreBotsTab publish flow but with the
        dropped file pre-selected."""
        self._dropped_path_for_publish = path
        # MoreBotsTab._publish_bot prompts for the file with a dialog;
        # we shortcut by writing a tiny shim that uses our pre-set path.
        self.more_bots_tab._publish_bot_with_path(path)

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

        self.mkt_label = SweepLabel("● CHECKING...")
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

        # V7.1.7: explicit Quit button so the user doesn't have to
        # dig into the system-tray menu to actually exit. The window
        # X button still hides-to-tray (bots keep running) — this
        # button does a real cleanup + QApplication.quit().
        self.quit_btn = QPushButton("⏻  QUIT")
        self.quit_btn.setObjectName("quitBtn")
        self.quit_btn.setToolTip(
            "Fully quit APEX (stops all running bots).\n"
            "The window X button minimises to the tray instead — "
            "your bots keep running headless.")
        self.quit_btn.clicked.connect(self._quit_app)
        layout.addWidget(self.quit_btn)

        # V3 wave 5 — credit balance chip (visible to everyone)
        self.credits_chip = QLabel("◊  — credits")
        self.credits_chip.setObjectName("creditsChip")
        self.credits_chip.setStyleSheet(
            f"color:{C['yellow']};font-size:10px;font-weight:600;"
            f"letter-spacing:1px;padding:6px 12px;margin-left:6px;"
            f"border:1px solid {C['border']};border-radius:5px;"
            f"background:rgba(214,201,94,0.06);"
        )
        self.credits_chip.setToolTip("Your APEX credit balance — refreshes every 60 s")
        layout.addWidget(self.credits_chip)

        # Logged-in user chip — V3.0.1: clickable, opens an account menu
        # with Switch account / Sign out so the user doesn't have to dig
        # into the system-tray menu.
        display = self._user.get("display_name") or self._user.get("username", "")
        if display:
            self.user_chip_btn = QPushButton(f"▸  {display}  ▾")
            self.user_chip_btn.setObjectName("userChipBtn")
            self.user_chip_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.user_chip_btn.setToolTip("Click to switch account or sign out")
            self.user_chip_btn.clicked.connect(self._show_account_menu)
            layout.addWidget(self.user_chip_btn)

        return header

    def _show_account_menu(self):
        """V3 wave 5 — drop-down anchored under the user chip. Lists any
        OTHER saved accounts for one-click switch (no re-login required),
        plus 'Add account' which takes you to the login window."""
        from ui.login import list_saved_accounts, activate_saved_account
        menu = QMenu(self)
        menu.setStyleSheet(self.styleSheet())

        current_id = (self._user or {}).get("id")
        others = [a for a in list_saved_accounts()
                  if int(a.get("user", {}).get("id", 0)) != int(current_id or 0)]

        if others:
            hdr = QAction("◍  Switch to saved account", self)
            hdr.setEnabled(False)
            menu.addAction(hdr)
            for acc in others:
                u = acc.get("user", {})
                label = f"   {u.get('display_name') or u.get('username','?')}"
                act = QAction(label, self)
                act.setToolTip(f"@{u.get('username','?')}")
                act.triggered.connect(
                    lambda _checked=False, uid=u.get("id"):
                        self._switch_to_saved_account(uid))
                menu.addAction(act)
            menu.addSeparator()

        add_act = QAction("➕  Add / sign in to another account", self)
        add_act.triggered.connect(self._sign_out)
        menu.addAction(add_act)

        signout_act = QAction("⏻  Sign out", self)
        signout_act.triggered.connect(self._sign_out)
        menu.addAction(signout_act)

        pos = self.user_chip_btn.mapToGlobal(
            QPoint(0, self.user_chip_btn.height()))
        menu.exec(pos)

    def _switch_to_saved_account(self, user_id: int):
        """One-click switch to a previously-saved account — no login
        prompt. The saved token is reused; if it has expired the next
        API call will 401 and TokenVerifyWorker boots us to the login
        screen as usual."""
        from ui.login import activate_saved_account
        acc = activate_saved_account(int(user_id))
        if not acc:
            self.statusBar().showMessage(
                "Could not switch to that account.")
            return
        self._user = acc["user"]
        display = self._user.get("display_name") or self._user.get("username", "")
        if hasattr(self, "user_chip_btn"):
            self.user_chip_btn.setText(f"▸  {display}  ▾")
        self.statusBar().showMessage(f"Switched to {display}")
        # Refresh account-scoped tabs against the new user
        try:
            if hasattr(self, "friends_tab"):
                self.friends_tab.refresh()
            if hasattr(self, "account_tab"):
                self.account_tab.refresh()
        except Exception as e:
            print(f"[switch] refresh: {e}")
        QTimer.singleShot(500, self._refresh_all)
        QTimer.singleShot(2500, self._resume_cloud_bots)

    # ── CORNER WIDGET ────────────────────────────────────────

    def _build_corner(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 8, 0)
        row.setSpacing(0)

        self._corner_universe = QPushButton("✦  UNIVERSE")
        self._corner_makebot  = QPushButton("⚒  MAKE BOT")
        self._corner_friends  = QPushButton("☺  FRIENDS")
        self._corner_account  = QPushButton("⚇  ACCOUNT")
        self._corner_admin    = QPushButton("♛  ADMIN")      # V3 wave 5
        self._corner_tools    = QPushButton("⚙  TOOLS")

        for btn in (self._corner_universe, self._corner_makebot,
                    self._corner_friends, self._corner_account,
                    self._corner_admin, self._corner_tools):
            btn.setObjectName("cornerBtn")
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(btn)

        # ADMIN corner button is hidden by default — _refresh_admin_visibility
        # un-hides it once /auth/me returns a non-USER role.
        self._corner_admin.setVisible(False)

        self._corner_universe.clicked.connect(
            lambda: self._switch_corner(self._universe_idx, self._corner_universe))
        self._corner_makebot.clicked.connect(
            lambda: self._switch_corner(self._makebot_idx,  self._corner_makebot))
        self._corner_friends.clicked.connect(
            lambda: self._switch_corner(self._friends_idx,  self._corner_friends))
        self._corner_account.clicked.connect(
            lambda: self._switch_corner(self._account_idx,  self._corner_account))
        self._corner_admin.clicked.connect(
            lambda: self._switch_corner(self._admin_idx,    self._corner_admin))
        self._corner_tools.clicked.connect(
            lambda: self._switch_corner(self._tools_idx, self._corner_tools))
        self.tabs.currentChanged.connect(self._on_tab_changed)
        return w

    def _switch_corner(self, idx: int, btn: QPushButton):
        self.tabs.setCurrentIndex(idx)
        all_corners = (self._corner_universe, self._corner_makebot,
                       self._corner_friends, self._corner_account,
                       self._corner_admin, self._corner_tools)
        for b in all_corners:
            b.setProperty("active", str(b is btn).lower())
            b.style().unpolish(b)
            b.style().polish(b)

    def _on_tab_changed(self, idx: int):
        # Deactivate corner buttons if we switched away from their tabs
        if idx not in (self._universe_idx, self._makebot_idx,
                       self._friends_idx, self._account_idx,
                       self._admin_idx, self._tools_idx):
            for b in (self._corner_universe, self._corner_makebot,
                      self._corner_friends, self._corner_account,
                      self._corner_admin, self._corner_tools):
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

        # Shift static indices  (V3.1.3 added _botmarket_idx)
        self._morebots_idx  += 1
        self._botmarket_idx += 1
        self._universe_idx  += 1
        self._makebot_idx   += 1
        self._friends_idx   += 1
        self._account_idx   += 1
        self._admin_idx     += 1
        self._tools_idx     += 1

        for hidden in (self._botmarket_idx,
                       self._universe_idx, self._makebot_idx,
                       self._friends_idx, self._account_idx,
                       self._admin_idx, self._tools_idx):
            # The BOT MARKET tab keeps whatever visibility the user
            # set last; the others stay hidden behind the corner buttons.
            if hidden == self._botmarket_idx:
                # leave alone — visibility is toggled by _open_bot_market
                pass
            else:
                self.tabs.tabBar().setTabVisible(hidden, False)

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
            # Shift static indices back  (V3.1.3 added _botmarket_idx)
            if idx < self._morebots_idx:  self._morebots_idx  -= 1
            if idx < self._botmarket_idx: self._botmarket_idx -= 1
            if idx < self._universe_idx:  self._universe_idx  -= 1
            if idx < self._makebot_idx:   self._makebot_idx   -= 1
            if idx < self._friends_idx:   self._friends_idx   -= 1
            if idx < self._account_idx:   self._account_idx   -= 1
            if idx < self._admin_idx:     self._admin_idx     -= 1
            if idx < self._tools_idx:     self._tools_idx     -= 1
            for hidden in (self._universe_idx, self._makebot_idx,
                           self._friends_idx, self._account_idx,
                           self._admin_idx, self._tools_idx):
                self.tabs.tabBar().setTabVisible(hidden, False)

        self._bot_dots.pop(side, None)
        self._bot_ctrls.pop(side, None)
        self._tab_indices.pop(side, None)
        tab.deleteLater()

    # ── V7.1.4: drag-reorder bot tabs only ──────────────────

    def _on_tab_moved(self, from_idx: int, to_idx: int):
        """Called by QTabBar after a user drags a tab to a new slot.
        We only allow moves whose source AND destination are bot tabs;
        anything else gets reverted. On accepted moves, persist the new
        ordering to the bot registry so it survives restarts."""
        if self._suppress_tab_move:
            return

        # The valid bot-tab range is everything strictly between the
        # Overview tab and the MORE BOTS tab. (Universe + Tools live
        # past MORE BOTS but are hidden, so we don't need to guard
        # against landing on them — drag won't visit invisible tabs.)
        # After the move, _morebots_idx may have shifted by ±1 if the
        # move crossed boundaries (it shouldn't, but be defensive).
        bot_lo = self._overview_idx + 1
        bot_hi = self._morebots_idx        # exclusive
        in_range = lambda i: bot_lo <= i < bot_hi

        if not (in_range(from_idx) and in_range(to_idx)):
            # Move would have shuffled a static tab — undo it.
            self._suppress_tab_move = True
            try:
                self.tabs.tabBar().moveTab(to_idx, from_idx)
            finally:
                self._suppress_tab_move = False
            return

        # Accepted — rebuild the registry's active-bot order from the
        # current tab sequence.
        reg = _load_registry()
        old_order = list(reg.get("active", []))
        new_order = []
        for i in range(bot_lo, bot_hi):
            w = self.tabs.widget(i)
            for side, tab in self._bot_tabs.items():
                if tab is w:
                    new_order.append(side)
                    break
        # Preserve any active bots that aren't currently tabbed (e.g.
        # the user silenced one — it's in `active` but not in the bar).
        tabless = [s for s in old_order if s not in new_order]
        reg["active"] = new_order + tabless
        _save_registry(reg)

        # V7.1.6: keep the Overview block order in sync, but defer it
        # via QTimer.singleShot(0) so the drag animation completes
        # first (the user felt a brief freeze otherwise) AND use the
        # lightweight reorder path that just shuffles the existing
        # block widgets in their layout — no Alpaca refetch.
        ov = getattr(self, "overview_tab", None)
        if ov and hasattr(ov, "reorder_active_bots"):
            QTimer.singleShot(0, ov.reorder_active_bots)

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
        # V7.1.1: deferred so the click that triggered the add can fully
        # complete before we mutate the tab bar (sibling buttons get
        # repositioned during insertTab, which could destabilise the
        # widget that fired the signal).
        QTimer.singleShot(0, lambda s=side: (self._add_bot_tab(s),
                                              self._sync_overview_blocks()))

    def _on_bot_removed(self, side: str):
        """V7.1.1: deferred so the originating click signal finishes
        before the source widget is destroyed. Removing the QPushButton
        that fired the click from inside the click handler used to
        occasionally tear down the whole window."""
        QTimer.singleShot(0, lambda s=side: self._do_remove_bot(s))

    def _do_remove_bot(self, side: str):
        # Stop bot if running
        tab = self._bot_tabs.get(side)
        if tab:
            bot_ctrl = getattr(tab, "bot_ctrl", None)
            if bot_ctrl and bot_ctrl.is_running():
                try:
                    bot_ctrl.stop_bot()
                except Exception:
                    pass
        reg = _load_registry()
        if side in reg["active"]:
            reg["active"].remove(side)
        if side in reg["silenced"]:
            reg["silenced"].remove(side)
        _save_registry(reg)
        # Move focus back to Overview before destroying the bot tab so
        # QTabWidget doesn't briefly try to render a now-dead page.
        try:
            self.tabs.setCurrentIndex(self._overview_idx)
        except Exception:
            pass
        self._remove_bot_tab(side)
        self.more_bots_tab.refresh()
        # V7.1.2: Overview now mirrors active-bot list
        self._sync_overview_blocks()

    def _on_bot_silenced(self, side: str):
        tab = self._bot_tabs.get(side)
        if tab:
            bot_ctrl = getattr(tab, "bot_ctrl", None)
            if bot_ctrl and bot_ctrl.is_running():
                bot_ctrl.stop_bot()
        self._grey_tab(side, True)
        self._update_overview_dot(side, "silenced")
        # V7.1.2: silenced bots disappear from the Overview blocks row.
        self._sync_overview_blocks()

    def _on_bot_unsilenced(self, side: str):
        self._grey_tab(side, False)
        self._update_overview_dot(side, "stopped")
        # V7.1.2: unsilenced bots reappear in the Overview blocks row.
        self._sync_overview_blocks()

    def _sync_overview_blocks(self):
        """Tell the Overview to rebuild its account-block row based on
        the current bot registry. No-op if Overview hasn't built yet."""
        try:
            ov = getattr(self, "overview_tab", None)
            if ov and hasattr(ov, "refresh_active_bots"):
                ov.refresh_active_bots()
        except Exception as e:
            print(f"[overview sync] {e}")

    # ── BOT MARKET show / hide ───────────────────────────────

    def _open_bot_market(self):
        """V3.1.3 — invoked from MoreBotsTab's '🛒 Open Bot Market'
        button. Reveal the tab in the bar, switch focus to it, and ask
        the market to refresh (so it shows fresh server data even if
        the user opened it a long time after launch)."""
        if hasattr(self, "_botmarket_idx"):
            self.tabs.tabBar().setTabVisible(self._botmarket_idx, True)
            self.tabs.setCurrentIndex(self._botmarket_idx)
            try:
                self.bot_market_tab.refresh()
            except Exception as e:
                print(f"[market] refresh on open: {e}")

    def _close_bot_market(self):
        """Called when the market tab's ✕ button is clicked."""
        if hasattr(self, "_botmarket_idx"):
            self.tabs.tabBar().setTabVisible(self._botmarket_idx, False)
            # Send the user back to MORE BOTS, since that's where they
            # entered from.
            self.tabs.setCurrentIndex(self._morebots_idx)

    # ── CLOUD RESUME ─────────────────────────────────────────

    def _resume_cloud_bots(self):
        """Ask each cloud-flagged bot's controller to query Oracle for
        its current running state, and restore the UI accordingly."""
        for side, tab in self._bot_tabs.items():
            bc = getattr(tab, "bot_ctrl", None)
            if bc and hasattr(bc, "cloud_resume_if_running"):
                try:
                    bc.cloud_resume_if_running()
                except Exception as e:
                    print(f"[cloud-resume] {side}: {e}")

    # ── V3 wave 5 — credits chip + admin tab visibility ─────────────

    def _refresh_user_meta(self):
        """Hit /auth/me + /credits/me to update the credit chip and the
        ADMIN corner button visibility. Bundled so we only hit the
        server twice instead of once per consumer."""
        class _W(QThread):
            done = pyqtSignal(dict, int)
            def run(self_):
                import requests
                from ui.login import load_auth, load_server_url
                tok = (load_auth() or {}).get("token") or ""
                if not tok:
                    self_.done.emit({}, 0)
                    return
                base = load_server_url()
                hdr  = {"Authorization": f"Bearer {tok}"}
                me   = {}
                bal  = 0
                try:
                    r = requests.get(f"{base}/auth/me", headers=hdr, timeout=8)
                    if r.ok:
                        me = r.json()
                except Exception:
                    pass
                try:
                    r = requests.get(f"{base}/credits/me", headers=hdr, timeout=8)
                    if r.ok:
                        bal = int(r.json().get("balance", 0))
                except Exception:
                    pass
                self_.done.emit(me, bal)

        w = _W()
        w.done.connect(self._on_user_meta_loaded)
        w.finished.connect(
            lambda _w=w: self._meta_workers.remove(_w)
                          if _w in getattr(self, "_meta_workers", []) else None)
        self._meta_workers = getattr(self, "_meta_workers", [])
        self._meta_workers.append(w)
        w.start()

    def _on_user_meta_loaded(self, me: dict, balance: int):
        # Credits chip — always update
        if hasattr(self, "credits_chip"):
            self.credits_chip.setText(f"◊  {balance:,} credits")
        # Admin corner button — show only for admin roles
        role = (me or {}).get("role", "USER")
        is_admin = role in ("ADMIN", "SUB_BOSS_ADMIN", "BOSS_ADMIN")
        if hasattr(self, "_corner_admin"):
            self._corner_admin.setVisible(is_admin)
            if is_admin:
                self._corner_admin.setToolTip(f"Admin dashboard · role: {role}")

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
        display = user.get("display_name") or user.get("username", "")
        # Update the clickable chip in the header (v3.0.1)
        if hasattr(self, "user_chip_btn"):
            self.user_chip_btn.setText(f"▸  {display}  ▾")
        # Legacy QLabel fallback (in case it survives from older builds)
        for child in self.centralWidget().findChildren(QLabel):
            if child.text().startswith("▸  "):
                child.setText(f"▸  {display}")
                break
        self.show()
        self.raise_()
        # Refresh anything that's account-scoped so the new user doesn't
        # see the previous account's data. Friends in particular needs
        # to repopulate the requests / friends lists.
        try:
            if hasattr(self, "friends_tab"):
                self.friends_tab.refresh()
            if hasattr(self, "account_tab"):
                self.account_tab.refresh()
        except Exception as e:
            print(f"[relogin] tabs refresh: {e}")
        QTimer.singleShot(500, self._refresh_all)
        # Re-attempt cloud-bot resume against the new user's bot list
        QTimer.singleShot(2500, self._resume_cloud_bots)

    def closeEvent(self, event):
        # V7.1.7: distinguish between the X button (minimise-to-tray
        # so bots keep running) and an explicit Quit (full cleanup).
        if getattr(self, "_user_requested_quit", False):
            try:
                self._teardown_for_quit()
            except Exception:
                pass
            event.accept()
            QApplication.quit()
            return
        event.ignore()
        self.hide()
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "APEX",
                "Running in background — your bots keep trading.\n"
                "Use the QUIT button in the header to fully exit.",
                QSystemTrayIcon.MessageIcon.Information, 2500)

    def _quit_app(self):
        """v1.2.3 — explicit quit. Cloud bots live on Oracle and keep
        trading independently of the desktop; only LOCAL bots die when
        APEX quits. Confirmation dialog now distinguishes the two so the
        user knows what will and won't happen."""
        local_running:  list[str] = []
        cloud_running:  list[str] = []
        for side, tab in self._bot_tabs.items():
            bc = getattr(tab, "bot_ctrl", None)
            if not bc:
                continue
            if getattr(bc, "_cloud_running", False):
                cloud_running.append(side)
            elif bc.is_running():
                local_running.append(side)

        if local_running or cloud_running:
            msg = QMessageBox(self)
            msg.setWindowTitle("Quit APEX?")
            parts = []
            if local_running:
                parts.append(
                    f"<b>{len(local_running)} local bot"
                    f"{'s' if len(local_running) > 1 else ''} "
                    f"will stop:</b> {', '.join(local_running)}<br>"
                    f"<span style='color:#7a8597;font-size:11px;'>"
                    f"Running on this laptop — they die when APEX "
                    f"quits.</span>")
            if cloud_running:
                parts.append(
                    f"<b>{len(cloud_running)} cloud bot"
                    f"{'s' if len(cloud_running) > 1 else ''} "
                    f"will KEEP RUNNING on Oracle:</b> "
                    f"{', '.join(cloud_running)}<br>"
                    f"<span style='color:#7a8597;font-size:11px;'>"
                    f"They trade 24/7 regardless of whether the "
                    f"desktop is open.</span>")
            msg.setText("<br><br>".join(parts) + "<br><br>Quit APEX?")
            msg.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            msg.setDefaultButton(QMessageBox.StandardButton.No)
            msg.setStyleSheet(self.styleSheet())
            if msg.exec() != QMessageBox.StandardButton.Yes:
                return

        self._user_requested_quit = True
        self.close()

    def _teardown_for_quit(self):
        """Stop LOCAL bots only, hide the tray icon, save state.
        Cloud bots live on Oracle and must survive desktop quit — the
        whole point of cloud mode is 24/7 trading independent of this
        machine. Called from closeEvent when _user_requested_quit
        is True."""
        for side, tab in self._bot_tabs.items():
            bc = getattr(tab, "bot_ctrl", None)
            if not bc:
                continue
            # Skip anything that's running on Oracle.
            if getattr(bc, "_cloud_running", False):
                continue
            if bc.is_running():
                try:
                    bc.stop_bot()
                except Exception:
                    pass
        try:
            if hasattr(self, "tray"):
                self.tray.hide()
        except Exception:
            pass

    # ── CLOCK ────────────────────────────────────────────────

    def _tick_clock(self):
        from datetime import datetime
        self.clock_label.setText(
            datetime.now().strftime("%a %d %b %Y  —  %H:%M:%S"))

    # ── REFRESH ──────────────────────────────────────────────

    def _refresh_all(self):
        """V7.1.11: bot-tab refresh is async (returns immediately, the
        worker emits done() later). The previous setUpdatesEnabled
        wrap around tab.refresh() was a no-op for visual wobble
        because the actual card mutations happen in _on_data, which
        runs after this method has already re-enabled updates. The
        wobble fix now lives in BotTab._on_data — here we just kick
        off the fetch and refresh the market status."""
        self.statusBar().showMessage("Refreshing data...")
        try:
            idx = self.tabs.currentIndex()
            tab = self.tabs.widget(idx)
            if tab and hasattr(tab, "refresh"):
                try:
                    tab.refresh()
                except Exception as e:
                    print(f"[refresh] tab error: {e}")
            try:
                self._refresh_market_status()
            except Exception as e:
                print(f"[refresh] market status: {e}")
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
                self.mkt_label.stop_sweep()
                self.mkt_label.setText("● MARKET OPEN")
                self.mkt_label.setStyleSheet(
                    f"font-size:10px;font-weight:600;letter-spacing:2px;"
                    f"color:{C['green']};padding:3px 12px;"
                    f"border:1px solid {C['green']};border-radius:12px;"
                    f"background:rgba(122,181,162,0.08);"
                )
            else:
                self._mkt_open_prev = False
                nxt = ck.next_open.astimezone().strftime("%b %d %H:%M")
                self.mkt_label.setText(f"● CLOSED  {nxt}")
                self.mkt_label.setStyleSheet(
                    f"font-size:10px;font-weight:600;letter-spacing:2px;"
                    f"color:{C['red']};padding:3px 12px;"
                    f"border:1px solid rgba(194,142,151,0.5);border-radius:12px;"
                    f"background:rgba(194,142,151,0.06);"
                )
                self.mkt_label.start_sweep()
            # Re-sync all running dots now that we know market state
            for side, tab in self._bot_tabs.items():
                bot_ctrl = getattr(tab, "bot_ctrl", None)
                if bot_ctrl:
                    self._on_bot_status_changed(side, bot_ctrl.is_running())
        except Exception:
            pass

    # ── AUTO-TRADE SCHEDULE ──────────────────────────────────

    def _tick_schedule(self):
        """V7.1.10: per-bot auto-schedule. Each bot has its own
        checkbox in Tools → AUTOMATION; only the checked ones get
        started/stopped on the market-open / market-close edge."""
        try:
            scheduled = set(D.get_auto_schedule_active_bots())
            if not scheduled:
                return
            from core.schedule import market_is_open
            is_open = market_is_open()
            if is_open is None:
                return
            if is_open and self._mkt_open_prev in (None, False):
                started = []
                for side in scheduled:
                    tab = self._bot_tabs.get(side)
                    if not tab:
                        continue
                    bc = getattr(tab, "bot_ctrl", None)
                    if bc and not bc.is_running():
                        try:
                            bc.start_bot()
                            started.append(side)
                        except Exception as e:
                            print(f"[schedule] start {side}: {e}")
                if started:
                    self.statusBar().showMessage(
                        f"Auto-schedule: market OPEN — started "
                        f"{', '.join(started)}")
            elif (not is_open) and self._mkt_open_prev in (None, True):
                stopped = []
                for side in scheduled:
                    tab = self._bot_tabs.get(side)
                    if not tab:
                        continue
                    bc = getattr(tab, "bot_ctrl", None)
                    if bc and bc.is_running():
                        try:
                            bc.stop_bot()
                            stopped.append(side)
                        except Exception as e:
                            print(f"[schedule] stop {side}: {e}")
                if stopped:
                    self.statusBar().showMessage(
                        f"Auto-schedule: market CLOSED — stopped "
                        f"{', '.join(stopped)}")
            self._mkt_open_prev = is_open
        except Exception as e:
            print(f"[schedule] tick error: {e}")

    # ── AUTO-UPDATE ──────────────────────────────────────────

    def _maybe_auto_update(self):
        """V7.1.3: notification-only. The check runs in the background
        and surfaces an "UPDATE AVAILABLE" banner in the header — the
        actual download and install only happens when the user clicks
        that banner. No more silent-install races, no more loops, no
        more market-hours gating: the user is in control.

        The dev (non-frozen) build still skips the check entirely so
        run.bat sessions never get the banner."""
        if not getattr(sys, "frozen", False):
            return
        self._auto_checker = UpdateChecker()
        self._auto_checker.update_available.connect(self._on_update_found)
        self._auto_checker.start()

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
    app.setWindowIcon(_app_icon())          # v3.1.6 — default for every window
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

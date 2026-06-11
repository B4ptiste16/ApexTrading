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
        print("Starting BAPTOU Trading Platform...", flush=True)
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
from ui.manual_tab     import ManualTradingTab  # V4.1.0
from ui.credit_shop    import CreditShopDialog  # V4.2.0
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
# TAB QUICK CONTROLS  (> [ ] X)
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

        self._play = QPushButton(">")
        self._stop = QPushButton("[ ]")
        self._remove = QPushButton("X")

        self._play.setObjectName("tabPlayBtn")
        self._stop.setObjectName("tabStopBtn")
        self._remove.setObjectName("tabRemoveBtn")

        for btn in (self._play, self._stop, self._remove):
            btn.setFixedSize(16, 16)

        # Pack: play / stop (mutually exclusive), then breathing room,
        # then the remove X. V7.1.1 — the X used to sit flush against
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
        "label":       "LONG",
        "icon":        "^",
        "color":       C["green"],
        "description": "Momentum + mean-reversion portfolio. "
                       "Uses Claude Vision on charts to rank candidates.",
        "cost":        "~$0.05–0.20 / day",
        "account":     "1 dedicated API key pair (Alpaca) or shared TWS connection (IBKR)",
        "brokers":     ["alpaca", "ibkr"],
    },
    "SHORT": {
        "label":       "SHORT",
        "icon":        "v",
        "color":       C["red"],
        "description": "Bear momentum, defensive in BULL regime. "
                       "Sells short on weakness, covers on strength.",
        "cost":        "~$0.03–0.12 / day",
        "account":     "1 dedicated API key pair (Alpaca) or shared TWS connection (IBKR)",
        "brokers":     ["alpaca", "ibkr"],
    },
    "DAY": {
        "label":       "DAY",
        "icon":        "*",
        "color":       C["orange"],
        "description": "Single high-conviction intraday bracket orders. "
                       "ATR-based stop-loss and take-profit.",
        "cost":        "~$0.02–0.08 / day",
        "account":     "1 dedicated API key pair (Alpaca) or shared TWS connection (IBKR)",
        "brokers":     ["alpaca", "ibkr"],
    },
}

MAX_ACTIVE_BOTS = 5


def _registry_key() -> str:
    """Return a per-user, per-broker settings key so each account+broker
    has its own independent bot list and stats.  Delegates to the
    core.data helper so the logic lives in one place."""
    return D.bot_registry_key()


def _load_registry() -> dict:
    s = D.load_settings()
    broker = s.get("broker_mode", "alpaca")
    # Default: all three built-ins active for any broker (LONG/SHORT/DAY now
    # support both Alpaca and IBKR).
    default = {"active": ["LONG","SHORT","DAY"], "silenced": [], "custom": []}
    key = _registry_key()
    reg = s.get(key)
    if reg is None:
        # Migration path 1: old per-broker key without mode suffix (< v4.6.31)
        try:
            from ui.login import load_auth
            auth = load_auth() or {}
            uid = auth.get("user_id") or auth.get("email") or ""
            if uid:
                reg = s.get(f"bot_registry_{uid}_{broker}")
        except Exception:
            pass
    if reg is None and broker == "alpaca":
        # Migration path 2: per-user key without broker suffix (pre-v3)
        try:
            from ui.login import load_auth
            auth = load_auth() or {}
            uid = auth.get("user_id") or auth.get("email") or ""
            if uid:
                reg = s.get(f"bot_registry_{uid}")
        except Exception:
            pass
    if reg is None:
        # Migration path 3: legacy global key
        reg = dict(s.get("bot_registry", default))
    for k in default:
        reg.setdefault(k, default[k])
    return reg


def _save_registry(reg: dict):
    s = D.load_settings()
    s[_registry_key()] = reg
    with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def bot_broker_compatible(side: str, broker: str, custom_list=None) -> bool:
    """V4.6.79 — single source of truth for "can this bot run on `broker`?".
    Used by BOTH the MORE BOTS list and the bot-TAB creation so a bot is
    shown/runnable in exactly the brokers it supports.

    Built-ins use BUILTIN_BOTS.brokers. Custom bots prefer the registry
    `brokers` list; when that's missing (older entries stored a buggy
    singular `broker='alpaca'`), the bot file's META.brokers is RE-PARSED so
    the user's real broker choice wins. A genuinely untagged bot shows
    everywhere."""
    info = BUILTIN_BOTS.get(side)
    if info:
        return broker in info.get("brokers", ["alpaca"])
    if custom_list is None:
        custom_list = _load_registry().get("custom", [])
    for c in custom_list:
        if not isinstance(c, dict) or c.get("id") != side:
            continue
        brokers = c.get("brokers")
        if not brokers:
            script = c.get("script", "")
            if script:
                try:
                    from core.bot_meta import parse_meta
                    src = open(script, encoding="utf-8").read()
                    brokers = (parse_meta(src) or {}).get("brokers")
                except Exception:
                    brokers = None
        if not brokers:
            b = str(c.get("broker", "")).strip().lower()
            brokers = [b] if b else []
        brokers = [str(x).strip().lower() for x in (brokers or [])]
        if not brokers:
            return True
        return any(broker == x or x == "both" for x in brokers)
    return True


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
        upload_btn = QPushButton("Browse .py file...")
        upload_btn.setObjectName("toolBtn")
        upload_btn.clicked.connect(self._upload_bot)
        skel_btn = QPushButton("Open skeleton guide")
        skel_btn.setObjectName("toolBtn")
        skel_btn.clicked.connect(self._open_skeleton_guide)
        # V4.0.0 — explicit Publish button so the user can test a bot
        # locally first, then publish without going through BOT MARKET.
        publish_btn = QPushButton("Publish to marketplace")
        publish_btn.setObjectName("addBotBtn")
        publish_btn.clicked.connect(self._publish_bot)
        self._upload_msg = QLabel("")
        self._upload_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        upload_row.addWidget(upload_btn)
        upload_row.addWidget(skel_btn)
        upload_row.addWidget(publish_btn)
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
            f"background:{C['panel2']};border:none;"
            f"border-radius:8px;")
        lv = QVBoxLayout(lock_wrap)
        lv.setContentsMargins(16, 12, 16, 12)
        lv.setSpacing(6)

        lock_head = QHBoxLayout()
        lock_title = QLabel("LIBRARY LOCK")
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
        self._lock_btn = QPushButton("Lock library")
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
            f"background:{C['panel']};border:none;"
            f"border-radius:12px;")
        mv = QVBoxLayout(market_wrap)
        mv.setContentsMargins(24, 20, 24, 20)
        mv.setSpacing(8)
        title = QLabel("BOT MARKET")
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
        open_btn = QPushButton("Open Bot Market")
        open_btn.setObjectName("addBotBtn")
        open_btn.setMinimumHeight(40)
        open_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background:rgba(138,147,201,0.12);"
            f"  color:{C['purple']};"
            f"  border:none;"
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
        broker   = D.load_settings().get("broker_mode", "alpaca")

        def _broker_ok(side: str) -> bool:
            """V4.6.79 — delegate to the shared, robust compatibility check."""
            return bot_broker_compatible(side, broker, custom)

        # ACTIVE section — only broker-compatible, non-silenced bots
        self._rebuild_grid(
            self._active_layout,
            [s for s in active if s not in silenced and _broker_ok(s)],
            mode="active",
        )
        # SILENCED section — all silenced bots (including incompatible ones so the
        # user can see they exist but are unavailable on this broker)
        self._rebuild_grid(
            self._silenced_layout,
            [s for s in active if s in silenced],
            mode="silenced",
        )
        # AVAILABLE section — built-ins + custom compatible with current broker, not already active
        compatible_builtins = [
            k for k, v in BUILTIN_BOTS.items()
            if broker in v.get("brokers", ["alpaca"])
        ]
        # V4.6.77 — custom bots must ALSO pass the broker filter here; the
        # old code added every custom bot to AVAILABLE regardless of broker,
        # so an IBKR-only bot showed up under Alpaca too (and vice-versa).
        all_known = compatible_builtins + [
            c["id"] for c in custom if _broker_ok(c["id"])]
        available = [b for b in all_known if b not in active]
        # V4.6.7 — wrap every placeholder-label access in a guard that
        # transparently re-creates the QLabel if _rebuild_grid wiped
        # it. Previously a sequence of (silence bot → refresh → silence
        # again → refresh) would crash on the second setVisible because
        # the first refresh's _rebuild_grid had already destroyed the
        # underlying C++ QLabel.
        if available:
            self._safe_placeholder_visible("_none_lbl", False)
            self._rebuild_grid(self._avail_layout, available, mode="available")
        else:
            self._safe_placeholder_visible("_none_lbl", True)
            self._clear_grid(self._avail_layout, keep_row0=True)

        # Silenced label visibility
        sil_list = [s for s in active if s in silenced]
        self._safe_placeholder_visible("_none_sil", len(sil_list) == 0)

    # V4.6.7 — placeholder label safety helpers
    _PLACEHOLDER_META = {
        "_none_lbl": ("All built-in bots are already active.", "avail"),
        "_none_sil": ("No silenced bots.",                     "silenced"),
    }

    def _safe_placeholder_visible(self, attr: str, visible: bool):
        """Set visibility on _none_lbl / _none_sil, transparently
        rebuilding the QLabel if a previous _rebuild_grid() destroyed
        the underlying C++ object."""
        lbl = getattr(self, attr, None)
        if lbl is not None:
            try:
                lbl.setVisible(visible)
                return
            except RuntimeError:
                pass  # widget deleted, fall through to recreate
        # Recreate the placeholder
        text, target = self._PLACEHOLDER_META.get(attr, (attr, "avail"))
        layout = (self._avail_layout if target == "avail"
                  else self._silenced_layout)
        new_lbl = QLabel(text)
        new_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        new_lbl.setVisible(visible)
        layout.addWidget(new_lbl, 0, 0)
        setattr(self, attr, new_lbl)

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
            self._lock_btn.setText("Unlock library")
            self._lock_status.setText("LOCKED")
            self._lock_status.setStyleSheet(
                f"color:{C['orange']};font-size:10px;font-weight:700;"
                f"letter-spacing:2px;")
        else:
            self._lock_btn.setText("Lock library")
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
        key = _registry_key()
        reg = s.get(key, {"active": [], "silenced": [], "custom": []})
        new_ext = ".apex" if locked else ".py"
        for c in reg.get("custom", []):
            if c.get("id") in slugs:
                old = Path(c.get("script", ""))
                c["script"] = str(old.with_suffix(new_ext))
        s[key] = reg
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
            "label": side, "icon": "o", "color": C["purple"],
            "description": "Custom bot", "cost": "—", "account": "—",
        })
        color   = info["color"]
        opacity = "0.45" if mode == "silenced" else "1.0"

        card = QFrame()
        card.setStyleSheet(
            f"background:{C['panel2']};border:none;"
            f"border-radius:10px;"
        )
        # V4.6.9 — wider card so all three buttons (Silence + Remove +
        # Delete) fit without clipping. Previously 220px forced "Remove"
        # to render as "emov" on custom bots.
        card.setFixedWidth(260)
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

        # V4.6.68 — enriched meta: AI-app logos (built-with + runs-on), broker
        # chips, and a Details button → telemetry dialog.
        try:
            from ui.bot_card import add_card_meta
            add_card_meta(vl, side, info, side not in BUILTIN_BOTS, self)
        except Exception as _e:
            print(f"[card-meta] {side}: {_e}")

        vl.addSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        # V4.6.8 — figure out if this is a custom (deletable) bot.
        # Built-ins (LONG/SHORT/DAY) can be silenced/removed but never
        # deleted from disk. Custom bots get an extra DELETE button.
        is_custom = side not in BUILTIN_BOTS

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

        # V4.6.8 — DELETE button for custom bots only. Wipes the .py
        # file from disk + every settings entry referencing this bot
        # + the registry entry + the tab. Shown in all modes (active /
        # silenced / available) so the user can always reach it.
        # V4.6.9 — switched from emoji-only "🗑" (which was clipping
        # and unclear) to "Delete" text on a slightly wider button.
        if is_custom:
            del_btn = QPushButton("Delete")
            del_btn.setObjectName("dangerBtn")
            del_btn.setToolTip(f"Delete {side} permanently — removes the .py "
                              f"file from your bot library and clears the "
                              f"registry entry. Cannot be undone.")
            del_btn.setStyleSheet(
                f"QPushButton#dangerBtn{{"
                f"background:{C['red']}20;color:{C['red']};"
                f"border:none;border-radius:4px;"
                f"padding:4px 8px;font-size:10px;font-weight:600;}}"
                f"QPushButton#dangerBtn:hover{{background:{C['red']}50;}}")
            del_btn.clicked.connect(lambda _, s=side: self._delete_bot(s))
            btn_row.addWidget(del_btn)

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
        # Defer refresh so the clicked button is not destroyed mid-click-event
        QTimer.singleShot(0, self.refresh)

    def _delete_bot(self, side: str):
        """V4.6.9 — hard delete a custom bot. Order matters here:
          1. Confirm with the user
          2. Wipe disk + registry + settings + transition state
             ALL BEFORE emitting any signal, so the ApexWindow's
             _on_bot_removed handler (which re-loads + re-writes the
             registry) sees the cleaned state and can't put the bot
             back. Previous v4.6.8 ordering raced with that handler.
          3. Stop the bot tab via emit (queued; handler reads fresh registry)
          4. Refresh UI

        Verbose logging at every step so if anything fails we can
        actually diagnose from the console / apex_crash.log."""
        from PyQt6.QtWidgets import QMessageBox as _QMB
        from pathlib import Path as _P
        import json as _j

        # Look up the bot's display label + script path
        reg = _load_registry()
        entry = next((c for c in reg.get("custom", [])
                      if str(c.get("id", "")).upper() == side.upper()), None)
        label  = entry.get("label", side) if entry else side
        script = entry.get("script", "")   if entry else ""
        print(f"[delete-bot] === BEGIN delete '{side}' "
              f"(label='{label}', script='{script}') ===", flush=True)

        confirm = _QMB.question(
            self.window(),
            f"Delete {label}?",
            f"This will permanently delete <b>{label}</b> from your "
            f"library.<br><br>"
            f"<b>What gets removed:</b><br>"
            f"• The .py file at <code>{script or '(unknown path)'}</code><br>"
            f"• The entry in your bot registry<br>"
            f"• Per-bot universe / confidence settings<br>"
            f"• The tab in this window (if shown)<br><br>"
            f"<b>This cannot be undone.</b> Continue?",
            _QMB.StandardButton.Yes | _QMB.StandardButton.No,
            _QMB.StandardButton.No,
        )
        if confirm != _QMB.StandardButton.Yes:
            print(f"[delete-bot] user cancelled", flush=True)
            return

        # ── Step 0a: UNPUBLISH from the bot market (V4.6.79) — deleting a
        # personally-made bot removes it from the marketplace too. Best-effort:
        # owner-only DELETE on the server (matches the local slug + any
        # collision-suffixed copies). Never blocks the local delete.
        try:
            from ui.login import load_auth, load_server_url
            import requests as _rq
            _tok = (load_auth() or {}).get("token")
            if _tok:
                _base = load_server_url()
                _hdr = {"Authorization": f"Bearer {_tok}"}
                _targets = {side, side.lower()}
                try:
                    r = _rq.get(f"{_base}/bots/v2", params={"view": "mine"},
                                headers=_hdr, timeout=6)
                    if r.ok:
                        body = r.json()
                        mine = body.get("bots", body) if isinstance(body, dict) else body
                        for pb in (mine or []):
                            sl = str(pb.get("slug", ""))
                            if sl == side or sl.startswith(side.lower() + "-"):
                                _targets.add(sl)
                except Exception:
                    pass
                for sl in _targets:
                    try:
                        dr = _rq.delete(f"{_base}/bots/{sl}", headers=_hdr,
                                        timeout=6)
                        if dr.ok:
                            print(f"[delete-bot] unpublished '{sl}' from market",
                                  flush=True)
                    except Exception:
                        pass
        except Exception as _e:
            print(f"[delete-bot] market unpublish skipped: {_e}", flush=True)

        # ── Step 0: free any IBKR allocation — sell the bot's sub-portfolio
        # and drop it from the Tools allocation table so deleted bots never
        # keep funds allocated (V4.6.70). Best-effort; never blocks deletion.
        try:
            from core import ibkr_lifecycle as _ibl
            ok, info = _ibl.free_ibkr_allocation(side)
            print(f"[delete-bot] IBKR allocation: {info}", flush=True)
        except Exception as _e:
            print(f"[delete-bot] IBKR allocation cleanup failed: {_e}", flush=True)

        # ── Step 1: delete the .py / .apex file ─────────────────
        deleted_files = []
        if script:
            for path in [_P(script),
                         _P(script).with_suffix(".py"),
                         _P(script).with_suffix(".apex")]:
                try:
                    if path.exists():
                        path.unlink()
                        deleted_files.append(str(path))
                except Exception as e:
                    print(f"[delete-bot] FAILED to remove {path}: {e}",
                          flush=True)
        # Also scan DATA_DIR/bots/ and DATA_DIR/universe_scripts/ for
        # a file matching the slug in case the registry script path
        # was wrong / stale.
        try:
            from core.paths import DATA_DIR
            for base in (DATA_DIR / "bots",
                         DATA_DIR / "universe_scripts"):
                if not base.exists():
                    continue
                for cand in base.glob(f"{side}.*"):
                    try:
                        cand.unlink()
                        deleted_files.append(str(cand))
                    except Exception as e:
                        print(f"[delete-bot] FAILED to remove {cand}: {e}",
                              flush=True)
                # Case-insensitive fallback
                for cand in base.iterdir():
                    if cand.is_file() and cand.stem.lower() == side.lower():
                        try:
                            cand.unlink()
                            deleted_files.append(str(cand))
                        except Exception as e:
                            print(f"[delete-bot] FAILED rm {cand}: {e}",
                                  flush=True)
        except Exception as e:
            print(f"[delete-bot] dir scan failed: {e}", flush=True)
        print(f"[delete-bot] removed {len(deleted_files)} file(s): "
              f"{deleted_files}", flush=True)

        # ── Step 2: strip from registry FIRST (before signal) ────
        before_custom = len(reg.get("custom", []))
        reg["active"]   = [b for b in reg.get("active",   []) if b != side]
        reg["silenced"] = [b for b in reg.get("silenced", []) if b != side]
        reg["custom"]   = [c for c in reg.get("custom",   [])
                           if str(c.get("id", "")).upper() != side.upper()]
        _save_registry(reg)
        print(f"[delete-bot] registry: custom went from "
              f"{before_custom} -> {len(reg['custom'])}", flush=True)

        # ── Step 3: clean settings (per-bot universe / conf / etc.)
        try:
            s = D.load_settings()
            wiped_keys = []
            for k in (f"bot_universe_{side.upper()}",
                      f"bot_min_conf_{side.upper()}",
                      f"bot_min_score_{side.upper()}",
                      f"bot_min_positions_{side.upper()}",
                      f"bot_max_brackets_{side.upper()}"):
                if k in s:
                    s.pop(k, None)
                    wiped_keys.append(k)
            us = s.get("universe_scripts", [])
            if isinstance(us, list):
                before = len(us)
                s["universe_scripts"] = [
                    u for u in us
                    if str(u.get("id", "")).lower() != side.lower()
                ]
                if before != len(s["universe_scripts"]):
                    wiped_keys.append("universe_scripts entry")
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _j.dump(s, f, indent=2)
            print(f"[delete-bot] settings wiped: {wiped_keys}", flush=True)
        except Exception as e:
            print(f"[delete-bot] settings cleanup failed: {e}", flush=True)

        # ── Step 4: transition state ─────────────────────────────
        try:
            from core.paths import DATA_DIR
            ts_path = DATA_DIR / "apex_transition_state.json"
            if ts_path.exists():
                ts = _j.loads(ts_path.read_text(encoding="utf-8"))
                if side.upper() in ts:
                    ts.pop(side.upper(), None)
                    ts_path.write_text(_j.dumps(ts, indent=2),
                                       encoding="utf-8")
                    print(f"[delete-bot] transition state cleared",
                          flush=True)
        except Exception as e:
            print(f"[delete-bot] transition state cleanup failed: {e}",
                  flush=True)

        # ── Step 5: NOW signal the window to tear down the tab.
        # By the time _on_bot_removed -> _do_remove_bot runs, the
        # registry is already clean, so _do_remove_bot's reload-modify-
        # save is a no-op and can't undo our work.
        try:
            self.bot_removed.emit(side)
            print(f"[delete-bot] bot_removed signal emitted", flush=True)
        except Exception as e:
            print(f"[delete-bot] signal emit failed: {e}", flush=True)

        # ── Step 6: hard-refresh this tab so the card disappears now
        QTimer.singleShot(0, self.refresh)
        print(f"[delete-bot] === END delete '{side}' OK ===", flush=True)

    def _silence(self, side: str):
        reg = _load_registry()
        if side not in reg["silenced"]:
            reg["silenced"].append(side)
        _save_registry(reg)
        self.bot_silenced.emit(side)
        QTimer.singleShot(0, self.refresh)

    def _unsilence(self, side: str):
        reg = _load_registry()
        if side in reg["silenced"]:
            reg["silenced"].remove(side)
        _save_registry(reg)
        self.bot_unsilenced.emit(side)
        QTimer.singleShot(0, self.refresh)

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

    def _publish_bot_with_path(self, path: str, *, prefill: dict | None = None):
        """Upload a known .py file to the public marketplace. Called
        directly by the drag-and-drop handler in ApexWindow.
        Optional `prefill` dict can supply default values for the dialogs
        (keys: name, description, tags, philosophy, creator_ai, runner_ai)."""
        from PyQt6.QtWidgets import QInputDialog
        from ui.login import load_auth, load_server_url

        stored = load_auth() or {}
        token = stored.get("token")
        if not token:
            QMessageBox.warning(
                self, "Sign in required",
                "Publishing a bot requires a BAPTOU account. Sign in or "
                "create one from the start screen.")
            return

        pf = prefill or {}
        blob = Path(path).read_bytes()
        # V4.6.75 (#8) — read the bot's universe from its META so the
        # marketplace can badge it (public universe vs own AI picks).
        bot_universe = ""
        try:
            from core.bot_meta import parse_meta
            bot_universe = (parse_meta(blob.decode("utf-8", "replace"))
                            or {}).get("universe", "").strip()
        except Exception:
            bot_universe = ""
        # V3.3.0 — similarity gate. Run a pre-flight check against
        # every existing published bot. Block ≥85%, warn ≥60%.
        try:
            import requests as _rq
            r = _rq.post(
                f"{load_server_url()}/bots/check-similarity",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("bot.py", blob, "text/x-python")},
                timeout=20)
            if r.ok:
                matches = r.json().get("matches", [])
                top = matches[0] if matches else None
                if top and top["score"] >= 0.85:
                    QMessageBox.critical(
                        self, "Too similar — cannot publish",
                        f"Your bot is <b>{int(top['score']*100)}% similar</b> "
                        f"to <code>{top['slug']}</code>. BAPTOU rejects "
                        f"near-duplicates to keep the marketplace useful.<br>"
                        f"<br>Consider building on top of "
                        f"<code>{top['slug']}</code> rather than re-publishing.")
                    return
                if top and top["score"] >= 0.60:
                    msg = ("Your bot is <b>{:.0%}</b> similar to "
                           "<code>{}</code>.<br>That's high enough to "
                           "publish but worth a heads-up — buyers may "
                           "complain it's a re-skin.<br><br>"
                           "Publish anyway?").format(top["score"], top["slug"])
                    if QMessageBox.question(
                            self, "Similarity warning", msg,
                            QMessageBox.StandardButton.Yes
                            | QMessageBox.StandardButton.No
                    ) != QMessageBox.StandardButton.Yes:
                        return
        except Exception as e:
            # Network/server problems shouldn't block publishing — just log.
            print(f"[publish] similarity check failed: {e}")
        # V4.6.78 — ZERO manual entry. Everything the marketplace needs is
        # auto-derived from the bot's APEX-BOT-META (written by the AI at
        # creation) + any prefill from Make Bot. We show ONE confirmation so
        # the user can review/cancel, but they never have to type anything.
        from core.bot_meta import parse_meta
        try:
            meta = parse_meta(blob.decode("utf-8", "replace")) or {}
        except Exception:
            meta = {}

        name = (pf.get("name") or meta.get("name") or Path(path).stem).strip()
        desc = (pf.get("description") or meta.get("description")
                or meta.get("method") or "").strip()
        # Tags: prefill, else asset_type + brokers (keeps listings searchable).
        if pf.get("tags"):
            tags = pf["tags"].strip()
        else:
            _tag_bits = [meta.get("asset_type", "")] + list(meta.get("brokers") or [])
            tags = ", ".join(t for t in _tag_bits if t)
        philos = (pf.get("philosophy") or meta.get("philosophy") or "").strip().lower()
        if philos not in ("long", "short", "day", "options", "momentum",
                          "mean-reversion", "scalping", "swing"):
            philos = ""    # server accepts empty
        try:
            price = int(pf.get("price", 0) or 0)
        except (TypeError, ValueError):
            price = 0
        creator_ai = (pf.get("creator_ai") or meta.get("ai_used") or "").strip()
        _cm = meta.get("compatible_models")
        runner_ai = (pf.get("runner_ai")
                     or (", ".join(_cm) if isinstance(_cm, list)
                         else (_cm or ""))).strip()
        _bl = meta.get("brokers") or []
        if isinstance(_bl, str):
            _bl = [_bl]
        _bl = [str(x).strip().lower() for x in _bl]
        if "alpaca" in _bl and "ibkr" in _bl:
            broker = "Alpaca + IBKR"
        elif "ibkr" in _bl:
            broker = "IBKR (Interactive Brokers)"
        elif "alpaca" in _bl:
            broker = "Alpaca"
        else:
            broker = "Alpaca + IBKR"

        uni_txt = bot_universe or "(AI-selected tickers)"
        summary = (
            f"<b>{name}</b><br><br>"
            f"{desc or 'No description.'}<br><br>"
            f"<b>AI:</b> {creator_ai or '—'}<br>"
            f"<b>Runs on:</b> {broker}<br>"
            f"<b>Universe:</b> {uni_txt}<br>"
            f"<b>Tags:</b> {tags or '—'}<br>"
            f"<b>Price:</b> {'FREE' if not price else f'{price} credits'}<br><br>"
            f"All of this was filled in automatically from the bot. "
            f"Publish to the BAPTOU bot market?")
        if QMessageBox.question(
                self, "Publish bot", summary,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return

        from PyQt6.QtCore import QThread, pyqtSignal as _Sig

        class _UpWorker(QThread):
            done = _Sig(bool, str)

            def __init__(self, base, tok, n, d, t, b, ph, pr, cai, rai, brk,
                         uni=""):
                super().__init__()
                self.base, self.tok, self.n, self.d, self.t, self.b = \
                    base, tok, n, d, t, b
                self.ph, self.pr = ph, pr
                self.cai, self.rai, self.brk = cai, rai, brk
                self.uni = uni

            def run(self):
                import requests
                try:
                    r = requests.post(
                        f"{self.base}/bots",
                        headers={"Authorization": f"Bearer {self.tok}"},
                        data={"name": self.n, "description": self.d,
                              "tags": self.t,
                              "philosophy": self.ph,
                              "price_credits": self.pr,
                              "creator_ai": self.cai,
                              "runner_ai":  self.rai,
                              "broker":     self.brk,
                              "universe":   self.uni},
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
            int(price),
            creator_ai, runner_ai, broker, bot_universe)
        def _on_pub(ok, msg):
            box = QMessageBox.information if ok else QMessageBox.warning
            box(self, "Publish bot", msg)
            if ok:
                self.refresh()
                # Also refresh the Bot Market tab so the listing shows up immediately
                try:
                    win = self.window()
                    bmt = getattr(win, "bot_market_tab", None)
                    if bmt:
                        QTimer.singleShot(800, lambda: (
                            bmt.refresh(),
                            bmt._set_view("mine"),
                        ))
                except Exception:
                    pass
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
        self.setWindowTitle(f"BAPTOU Trading Platform  v{get_current_version()}")
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

        # V4.0.0 — content body is a QStackedWidget so we can swap the
        # entire window between the Alpaca tabbed UI and the
        # "coming-very-soon" placeholder for not-yet-supported brokers
        # (IBKR / TradingView). The header stays visible in both modes
        # so the broker-mode chip is always reachable.
        from PyQt6.QtWidgets import QStackedWidget as _QStack
        self._content_stack = _QStack()
        root_layout.addWidget(self._content_stack, 1)

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
        # V4.0.0 — page 0 of the stack is the full Alpaca tabbed UI
        self._content_stack.addWidget(self.tabs)
        # Page 1 — the "coming soon" placeholder used by IBKR / TradingView
        self._coming_soon_page = self._build_coming_soon_page()
        self._content_stack.addWidget(self._coming_soon_page)

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

        # V4.1.0 — Manual trading tab (hidden until toggle is on)
        self.manual_tab = ManualTradingTab()
        self.manual_tab.switch_off_requested.connect(
            lambda: self._toggle_manual_mode(force_off=True))

        self._overview_idx  = self.tabs.addTab(self.overview_tab,  "OVERVIEW")
        self._morebots_idx  = self.tabs.addTab(self.more_bots_tab, "MORE BOTS")

        # V3.1.3 — BOT MARKET is a "summon-able" tab.
        self._botmarket_idx = self.tabs.addTab(self.bot_market_tab,
                                                "BOT MARKET")

        # V4.1.0 — MANUAL TRADING is a "summon-able" tab (shown when toggle is ON)
        self._manual_idx = self.tabs.addTab(self.manual_tab, "MANUAL")

        # Corner row reads:
        #   UNIVERSE · MAKE BOT · FRIENDS · ACCOUNT · ADMIN · TOOLS
        # (ADMIN is hidden for non-admin users — see _refresh_admin_visibility.)
        self._universe_idx  = self.tabs.addTab(self.universe_tab, "")
        self._makebot_idx   = self.tabs.addTab(self.make_bot_tab, "")
        self._friends_idx   = self.tabs.addTab(self.friends_tab,  "")
        self._account_idx   = self.tabs.addTab(self.account_tab,  "")
        self._admin_idx     = self.tabs.addTab(self.admin_tab,    "")
        self._tools_idx     = self.tabs.addTab(self.tools_tab,    "")
        for idx in (self._botmarket_idx, self._manual_idx,
                    self._universe_idx, self._makebot_idx,
                    self._friends_idx, self._account_idx,
                    self._admin_idx, self._tools_idx):
            self.tabs.tabBar().setTabVisible(idx, False)
        # Startup state applied after corner is built (corner buttons need to exist)
        # — deferred to _apply_manual_mode_ui() called at end of _setup_ui

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
        self.statusBar().showMessage("BAPTOU ready")

        # Timers
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(1000)

        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._refresh_all)
        self.refresh_timer.start(45_000)
        # V4.6.65 — eager-load every tab right after launch so the app opens
        # already populated (no load-on-click wait on first visit).
        QTimer.singleShot(600, self._refresh_all)

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
        # the user click > again.
        QTimer.singleShot(3000, self._resume_cloud_bots)
        # V3 wave 5 — credits + admin visibility refresh, fires shortly
        # after launch and then every 60 s.
        QTimer.singleShot(1500, self._refresh_user_meta)
        self._meta_timer = QTimer()
        self._meta_timer.timeout.connect(self._refresh_user_meta)
        self._meta_timer.start(60_000)
        # V3.3.0 — pull any bots removed by moderation that we used to
        # have installed, and delete them locally.
        QTimer.singleShot(4500, self._sync_revocations)
        # V4.0.1 — show the T&C acceptance modal if the user hasn't
        # ticked it yet. Defer so the main window has time to paint.
        QTimer.singleShot(2000, self._check_tos_acceptance)
        # V4.6.94 — first-run welcome wizard (after T&C so it doesn't stack).
        QTimer.singleShot(2600, self._maybe_show_onboarding)
        # V4.0.0 — apply the saved broker mode AFTER the tabs are wired
        # so the stack is fully populated before we switch pages.
        QTimer.singleShot(0, lambda: self._apply_broker_mode(
            self._current_broker_mode()))
        # V4.3.0 — restore manual mode UI state after all tabs exist
        QTimer.singleShot(0, lambda: self._apply_manual_mode_ui(
            self._is_manual_mode()))

        # V7.1.1: accept .py drops anywhere in the window so a user can
        # drag a bot script in and we'll offer to install it locally or
        # publish it to the public library.
        self.setAcceptDrops(True)
        # Align corner widget height to the tab bar once the window has painted.
        # Run at 200ms AND 800ms — the first fires before most paints, the
        # second catches DPI-aware size hints that settle after the first frame.
        QTimer.singleShot(200, self._sync_corner_height)
        QTimer.singleShot(800, self._sync_corner_height)

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

        logo = QLabel("BAPTOU")
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
            f"border:none;border-radius:12px;"
        )
        layout.addWidget(self.mkt_label)

        self.clock_label = QLabel()
        self.clock_label.setStyleSheet(
            f"font-size:11px;color:{C['muted']};margin-left:18px;"
        )
        layout.addWidget(self.clock_label)

        self.update_btn = QPushButton("UPDATE AVAILABLE")
        self.update_btn.setVisible(False)
        self.update_btn.setObjectName("updateBtn")
        self.update_btn.clicked.connect(self._show_update_dialog)
        layout.addWidget(self.update_btn)

        # V7.1.7: explicit Quit button so the user doesn't have to
        # dig into the system-tray menu to actually exit. The window
        # X button still hides-to-tray (bots keep running) — this
        # button does a real cleanup + QApplication.quit().
        self.quit_btn = QPushButton("QUIT")
        self.quit_btn.setObjectName("quitBtn")
        self.quit_btn.setToolTip(
            "Fully quit BAPTOU (stops all running bots).\n"
            "The window X button minimises to the tray instead — "
            "your bots keep running headless.")
        self.quit_btn.clicked.connect(self._quit_app)
        layout.addWidget(self.quit_btn)

        # V4.1.0 — manual trading mode toggle (iPhone-style ON/OFF)
        self._manual_mode_btn = QPushButton(self._manual_mode_label())
        self._manual_mode_btn.setObjectName("manualModeBtn")
        self._manual_mode_btn.setCheckable(True)
        self._manual_mode_btn.setChecked(self._is_manual_mode())
        self._manual_mode_btn.setProperty("active",
            "true" if self._is_manual_mode() else "false")
        self._manual_mode_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._manual_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_mode_btn.setToolTip(
            "Toggle between AI Auto-trading and Manual trading mode.\n"
            "In Manual mode you place orders yourself — great for trading vs friends.")
        self._manual_mode_btn.clicked.connect(self._toggle_manual_mode)
        layout.addWidget(self._manual_mode_btn)

        # V3.2.0 — broker-mode selector.
        self._broker_mode_btn = QPushButton(self._current_broker_label())
        self._broker_mode_btn.setObjectName("brokerModeBtn")
        self._broker_mode_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._broker_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._broker_mode_btn.setToolTip(
            "Switch the app between broker modes")
        self._broker_mode_btn.clicked.connect(self._show_broker_menu)
        layout.addWidget(self._broker_mode_btn)

        # V4.6.8 — Paper/Live Alpaca toggle. Pill-style button that
        # flips between paper trading (Alpaca paper API) and live
        # trading (Alpaca live API). The selected mode is exported to
        # every bot subprocess via APEX_ALPACA_MODE; the bot framework
        # + the built-in scripts honor it.
        # Live mode auto-silences any bot whose META declares
        # `alpaca_mode: paper` (paper-only); paper mode auto-silences
        # any bot whose META declares `alpaca_mode: live` (live-only).
        # Default: both modes acceptable — bot runs in either.
        self._alpaca_mode_btn = QPushButton(self._alpaca_mode_label())
        self._alpaca_mode_btn.setObjectName("alpacaModeBtn")
        self._alpaca_mode_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._alpaca_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._alpaca_mode_btn.setToolTip(
            "Click to toggle between Alpaca PAPER trading and LIVE "
            "trading.\n\nPaper: simulated, no real money.\nLive: "
            "real orders on real money. Use carefully.")
        self._alpaca_mode_btn.clicked.connect(self._toggle_alpaca_mode)
        self._restyle_alpaca_mode_btn()
        layout.addWidget(self._alpaca_mode_btn)

        # V3 wave 5 — credit balance chip (clickable → Credit Shop)
        self.credits_chip = QPushButton("— credits")
        self.credits_chip.setObjectName("creditsChip")
        self.credits_chip.setStyleSheet(
            f"QPushButton#creditsChip{{"
            f"color:{C['yellow']};font-size:10px;font-weight:600;"
            f"letter-spacing:1px;padding:6px 12px;margin-left:6px;"
            f"border:none;border-radius:5px;"
            f"background:rgba(214,201,94,0.06);cursor:pointer;}}"
            f"QPushButton#creditsChip:hover{{"
            f"background:rgba(214,201,94,0.14);"
            f"border:none;}}"
        )
        self.credits_chip.setToolTip("Click to buy more credits")
        self.credits_chip.clicked.connect(self._open_credit_shop)
        layout.addWidget(self.credits_chip)

        # Logged-in user chip — V3.0.1: clickable, opens an account menu
        # with Switch account / Sign out so the user doesn't have to dig
        # into the system-tray menu.
        display = self._user.get("display_name") or self._user.get("username", "")
        if display:
            self.user_chip_btn = QPushButton(f"{display}  v")
            self.user_chip_btn.setObjectName("userChipBtn")
            self.user_chip_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.user_chip_btn.setToolTip("Click to switch account or sign out")
            self.user_chip_btn.clicked.connect(self._show_account_menu)
            layout.addWidget(self.user_chip_btn)

        return header

    # ── V4.6.8 — Alpaca paper/live mode toggle ──────────────

    def _current_alpaca_mode(self) -> str:
        """'paper' (default) or 'live'."""
        try:
            return D.load_settings().get("alpaca_mode", "paper")
        except Exception:
            return "paper"

    def _alpaca_mode_label(self) -> str:
        return ("◉ LIVE" if self._current_alpaca_mode() == "live"
                else "○ paper")

    def _restyle_alpaca_mode_btn(self):
        mode = self._current_alpaca_mode()
        if mode == "live":
            # Red-tinted urgency for live trading
            self._alpaca_mode_btn.setStyleSheet(
                f"QPushButton#alpacaModeBtn{{"
                f"color:{C['red']};font-size:10px;font-weight:700;"
                f"letter-spacing:1.5px;padding:6px 12px;margin-left:6px;"
                f"border:none;border-radius:12px;"
                f"background:rgba(194,142,151,0.08);}}"
                f"QPushButton#alpacaModeBtn:hover{{"
                f"background:rgba(194,142,151,0.16);}}")
        else:
            # Muted/calm for paper
            self._alpaca_mode_btn.setStyleSheet(
                f"QPushButton#alpacaModeBtn{{"
                f"color:{C['muted']};font-size:10px;font-weight:600;"
                f"letter-spacing:1.5px;padding:6px 12px;margin-left:6px;"
                f"border:none;border-radius:12px;"
                f"background:rgba(106,120,148,0.06);}}"
                f"QPushButton#alpacaModeBtn:hover{{"
                f"background:rgba(106,120,148,0.14);}}")
        self._alpaca_mode_btn.setText(self._alpaca_mode_label())

    def _toggle_alpaca_mode(self):
        """Flip paper↔live with a confirm dialog when switching TO live
        (real money, deserves a checkbox click)."""
        from PyQt6.QtWidgets import QMessageBox
        cur = self._current_alpaca_mode()
        target = "live" if cur == "paper" else "paper"

        if target == "live":
            # Hard confirm for live trading
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Switch to LIVE trading?")
            box.setText(
                "<b>LIVE mode places real orders with real money.</b><br><br>"
                "All running bots will be migrated to the live Alpaca API on "
                "their next cycle. Make sure your live API keys are configured "
                "in Tools → ALPACA · API KEYS before switching.<br><br>"
                "Continue?")
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return

        # Persist
        try:
            s = D.load_settings()
            s["alpaca_mode"] = target
            import json as _j
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _j.dump(s, f, indent=2)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return

        self._restyle_alpaca_mode_btn()
        # Refresh tabs so each mode's independent bot list is shown, and
        # rebuild the Tools tab so IBKR settings switch to the new mode.
        try:
            self._rebuild_broker_bot_tabs()
        except Exception:
            pass
        try:
            broker = D.load_settings().get("broker_mode", "alpaca")
            if hasattr(self, "tools_tab") and hasattr(self.tools_tab, "rebuild_for_mode"):
                self.tools_tab.rebuild_for_mode(broker)
        except Exception as e:
            print(f"[mode-toggle] tools rebuild: {e}")
        # Tell the user the switch landed and that running bots will
        # pick it up on their next cycle (start_bot reads the env var).
        from PyQt6.QtWidgets import QMessageBox as _Q
        _Q.information(
            self, "Alpaca mode switched",
            f"Now in {target.upper()} mode. "
            f"{'Bots already running will pick up the new mode on their next cycle. ' if target == 'paper' else 'Live trading active — bots will trade real money on next start.'}"
            "You can switch back at any time.")

    # ── V4.1.0 — manual trading mode toggle ─────────────────

    def _is_manual_mode(self) -> bool:
        try:
            return bool(D.load_settings().get("manual_mode", False))
        except Exception:
            return False

    def _manual_mode_label(self) -> str:
        return "MANUAL" if self._is_manual_mode() else "AUTO"

    def _apply_manual_mode_ui(self, on: bool):
        """Show/hide tabs and corner buttons to reflect manual-vs-auto mode.
        Safe to call at any time after _setup_tabs + _insert_bot_tabs."""
        tb = self.tabs.tabBar()

        # ── Bot tabs (OVERVIEW, dynamic bots, MORE BOTS, BOT MARKET) ──
        tb.setTabVisible(self._overview_idx,  not on)
        tb.setTabVisible(self._morebots_idx,  not on)
        tb.setTabVisible(self._botmarket_idx, False)   # always hidden from bar
        for idx in self._tab_indices.values():
            tb.setTabVisible(idx, not on)

        # ── MANUAL tab ─────────────────────────────────────────────────
        tb.setTabVisible(self._manual_idx, on)

        # ── Corner buttons ─────────────────────────────────────────────
        # V4.6.94 — MANUAL mode is a fully separate workspace: hide EVERY
        # auto-mode nav button so the only thing shared with auto is the broker
        # chip. The manual tab is self-contained (connect + trade + positions).
        for attr in ("_corner_universe", "_corner_makebot", "_corner_friends",
                     "_corner_account", "_corner_tools"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setVisible(not on)
        # Admin button: only ever visible to admins — hide in manual, and on the
        # way back restore it to the admin-status flag (default hidden).
        if hasattr(self, "_corner_admin"):
            self._corner_admin.setVisible(
                (not on) and bool(getattr(self, "_is_admin_user", False)))

        # ── Navigate to the right tab ──────────────────────────────────
        if on:
            self.tabs.setCurrentIndex(self._manual_idx)
            if hasattr(self, "manual_tab"):
                self.manual_tab._refresh_key_state()
        else:
            self.tabs.setCurrentIndex(self._overview_idx)

        # ── Friends tab: switch display mode ───────────────────────────
        if hasattr(self, "friends_tab") and hasattr(self.friends_tab,
                                                    "set_manual_mode"):
            self.friends_tab.set_manual_mode(on)

    def _toggle_manual_mode(self, *, force_off: bool = False):
        new_state = False if force_off else not self._is_manual_mode()
        try:
            s = D.load_settings()
            s["manual_mode"] = new_state
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[manual-mode] save failed: {e}")
            return
        if hasattr(self, "_manual_mode_btn"):
            self._manual_mode_btn.setText(self._manual_mode_label())
            self._manual_mode_btn.setChecked(new_state)
            self._manual_mode_btn.setProperty("active", "true" if new_state else "false")
            self._manual_mode_btn.setStyleSheet(
                self._manual_mode_btn.styleSheet())  # force repaint
        if hasattr(self, "tabs") and hasattr(self, "_manual_idx"):
            self._apply_manual_mode_ui(new_state)

    # ── V3.2.0 — broker-mode selector ───────────────────────

    BROKER_MODES = {
        # value → (display label, status)
        "alpaca":     ("Alpaca",      "active"),
        "ibkr":       ("IBKR",        "coming"),
        "tradingview":("TradingView", "coming"),
    }

    def _current_broker_mode(self) -> str:
        try:
            return D.load_settings().get("broker_mode", "alpaca")
        except Exception:
            return "alpaca"

    def _current_broker_label(self) -> str:
        mode = self._current_broker_mode()
        label, _status = self.BROKER_MODES.get(mode, ("Alpaca", "active"))
        return f"{label}  v"

    def _set_broker_mode(self, mode: str):
        try:
            s = D.load_settings()
            s["broker_mode"] = mode
            import json as _json
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[broker-mode] save failed: {e}")
            return
        if hasattr(self, "_broker_mode_btn"):
            self._broker_mode_btn.setText(self._current_broker_label())
        # Re-render the Tools tab so the new mode's content shows
        try:
            if hasattr(self, "tools_tab") and hasattr(self.tools_tab, "rebuild_for_mode"):
                self.tools_tab.rebuild_for_mode(mode)
        except Exception as e:
            print(f"[broker-mode] tools rebuild: {e}")
        # V4.0.0 — flip the whole content stack so non-Alpaca modes get
        # the coming-soon placeholder instead of stale Alpaca data.
        self._apply_broker_mode(mode)
        # Rebuild bot tabs so the new broker's registry is reflected:
        # • Alpaca → LONG/SHORT/DAY tabs restored from per-broker registry
        # • IBKR / others → no Alpaca-only tabs, built-ins appear silenced
        QTimer.singleShot(0, self._rebuild_broker_bot_tabs)

    def _build_coming_soon_page(self) -> QWidget:
        """The full-window placeholder shown when broker_mode is anything
        other than 'alpaca'. Stays minimal — title, mode, a description,
        and a 'switch back to Alpaca' hint."""
        wrap = QFrame()
        wrap.setStyleSheet(f"background:transparent;border:none;")
        v = QVBoxLayout(wrap)
        v.setContentsMargins(60, 80, 60, 80)
        v.setSpacing(18)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel("🚧")
        icon.setStyleSheet("font-size:72px;background:transparent;")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(icon)

        self._coming_soon_title = QLabel("COMING VERY SOON")
        self._coming_soon_title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:34px;font-weight:800;"
            f"color:{COLORS['text']};letter-spacing:6px;background:transparent;")
        self._coming_soon_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._coming_soon_title)

        self._coming_soon_sub = QLabel("")
        self._coming_soon_sub.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:13px;"
            f"color:{COLORS['muted']};letter-spacing:2px;"
            f"background:transparent;")
        self._coming_soon_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._coming_soon_sub)

        v.addSpacing(20)
        desc = QLabel(
            "BAPTOU bots currently route orders through Alpaca's paper-"
            "trading API. The account model, key structure, and order "
            "flow are completely different per broker, so each integration "
            "needs its own end-to-end wiring.<br><br>"
            "Switch back to <b>Alpaca</b> via the mode chip in the header "
            "to manage your bots, keys, and live trading data.")
        desc.setStyleSheet(
            f"font-family:'JetBrains Mono';font-size:12px;line-height:1.7;"
            f"color:{COLORS['muted']};background:transparent;")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        desc.setMaximumWidth(640)
        v.addWidget(desc, alignment=Qt.AlignmentFlag.AlignCenter)

        # "Switch back" button — one click to return to Alpaca
        back = QPushButton("Switch back to Alpaca")
        back.setObjectName("addBotBtn")
        back.setFixedHeight(44)
        back.setMinimumWidth(260)
        back.clicked.connect(lambda: self._set_broker_mode("alpaca"))
        v.addWidget(back, alignment=Qt.AlignmentFlag.AlignCenter)
        return wrap

    def _apply_broker_mode(self, mode: str):
        """Swap the central stack page based on broker mode. Page 0 is
        the main tabbed UI (Alpaca and IBKR both use it); page 1 is the
        placeholder for not-yet-wired brokers like TradingView."""
        if not hasattr(self, "_content_stack"):
            return
        if mode in ("alpaca", "ibkr"):
            self._content_stack.setCurrentIndex(0)
        else:
            label = self.BROKER_MODES.get(mode, (mode.title(), "coming"))[0]
            self._coming_soon_title.setText(
                f"{label.upper()}  —  COMING VERY SOON")
            self._coming_soon_sub.setText(
                f"Mode: {label}    ·    Not yet wired end-to-end")
            self._content_stack.setCurrentIndex(1)

    def _rebuild_broker_bot_tabs(self):
        """Tear down all existing bot tabs and re-insert from the current
        broker's registry. Called whenever the user switches broker mode so:
        • Alpaca  → LONG/SHORT/DAY tabs come back (from per-broker registry)
        • IBKR    → no Alpaca-only tabs; built-ins silenced in More Bots
        • Others  → no compatible built-in tabs (coming-soon page shown)"""
        # Stop only LOCAL running bots. CLOUD bots keep trading on Oracle —
        # a broker / paper-live VIEW switch must never halt a 24/7 cloud bot
        # (e.g. switching the desktop to IBKR must not stop your Alpaca cloud
        # bots). Their tabs are rebuilt below and resume status via Oracle.
        for side, tab in list(self._bot_tabs.items()):
            bot_ctrl = getattr(tab, "bot_ctrl", None)
            if not bot_ctrl:
                continue
            try:
                is_cloud = (bot_ctrl._is_cloud_mode()
                            or getattr(bot_ctrl, "_cloud_running", False))
                if is_cloud:
                    continue  # leave it running on the server
                if bot_ctrl.is_running():
                    bot_ctrl.stop_bot()
            except Exception:
                pass

        # Remove all existing bot tabs
        for side in list(self._bot_tabs.keys()):
            try:
                self._remove_bot_tab(side)
            except Exception as e:
                print(f"[broker-switch] remove {side}: {e}")

        # Re-insert tabs for the new broker's registry
        try:
            self._insert_bot_tabs()
        except Exception as e:
            print(f"[broker-switch] insert: {e}")

        # Reconnect any still-running cloud bots to their freshly-built tabs
        # so the UI reflects that they never stopped.
        try:
            self._resume_cloud_bots()
        except Exception as e:
            print(f"[broker-switch] cloud-resume: {e}")

        # Refresh More Bots panel so section counts update
        if hasattr(self, "more_bots_tab"):
            try:
                self.more_bots_tab.refresh()
            except Exception as e:
                print(f"[broker-switch] more-bots refresh: {e}")

        # Refresh Overview after a small delay (data workers need to spin up)
        if hasattr(self, "overview_tab"):
            QTimer.singleShot(300, lambda: (
                getattr(self.overview_tab, "refresh", lambda: None)()
            ))

    def _show_broker_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self.styleSheet())
        current = self._current_broker_mode()
        for key, (label, status) in self.BROKER_MODES.items():
            text = f"{'> ' if key == current else '  '}{label}"
            if status == "coming":
                text += "    (coming very soon)"
            act = QAction(text, self)
            act.triggered.connect(
                lambda _checked=False, m=key: self._set_broker_mode(m))
            menu.addAction(act)
        pos = self._broker_mode_btn.mapToGlobal(
            QPoint(0, self._broker_mode_btn.height()))
        menu.exec(pos)

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
            hdr = QAction("Switch account", self)
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

        add_act = QAction("+ Add / sign in to another account", self)
        add_act.triggered.connect(self._sign_out)
        menu.addAction(add_act)

        signout_act = QAction("Sign out", self)
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
            self.user_chip_btn.setText(f"{display}  v")
        self.statusBar().showMessage(f"Switched to {display}")
        # V4.6.79 — a real account switch re-fetches EVERY account-scoped view
        # with the new token (these tabs read the token fresh per request, so
        # refreshing them is enough). Previously only friends + account were
        # refreshed, so the marketplace ("my bots"), credits and admin views
        # kept showing the PREVIOUS account — making the switch look cosmetic.
        for attr, how in (("friends_tab", "refresh"),
                          ("account_tab", "refresh"),
                          ("admin_tab", "refresh"),
                          ("more_bots_tab", "refresh"),
                          ("bot_market_tab", "_refresh_current_view"),
                          ("tools_tab", "refresh")):
            tab = getattr(self, attr, None)
            fn = getattr(tab, how, None) if tab is not None else None
            if callable(fn):
                try:
                    fn()
                except Exception as e:
                    print(f"[switch] {attr}.{how}: {e}")
        # Refresh the user chip's credit balance / identity if shown.
        try:
            if hasattr(self, "_refresh_user_chip"):
                self._refresh_user_chip()
        except Exception:
            pass
        QTimer.singleShot(500, self._refresh_all)
        QTimer.singleShot(2500, self._resume_cloud_bots)

    # ── CORNER WIDGET ────────────────────────────────────────

    def _build_corner(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 8, 0)
        row.setSpacing(0)
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._corner_universe = QPushButton("UNIVERSE")
        self._corner_makebot  = QPushButton("MAKE BOT")
        self._corner_friends  = QPushButton("FRIENDS")
        self._corner_account  = QPushButton("ACCOUNT")
        self._corner_admin    = QPushButton("ADMIN")           # V3 wave 5
        self._corner_tools    = QPushButton("TOOLS")

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

    def _sync_corner_height(self):
        """Pin the corner widget height to the actual tab bar height so
        the corner buttons sit perfectly flush with the left-side tabs."""
        try:
            from PyQt6.QtCore import Qt as _Qt
            h = self.tabs.tabBar().height()
            corner = self.tabs.cornerWidget(_Qt.Corner.TopRightCorner)
            if corner and h > 0:
                corner.setFixedHeight(h)
                # Also enforce the same height on each button so they don't
                # overflow or under-fill the bar
                for btn in corner.findChildren(QPushButton):
                    btn.setFixedHeight(h)
        except Exception as e:
            print(f"[corner-height] {e}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-align the corner buttons on every resize in case the tab bar
        # changed height (e.g. font/DPI change after first paint).
        QTimer.singleShot(0, self._sync_corner_height)

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
        """Insert bot tabs based on registry (between Overview and More Bots).
        Bots that are not compatible with the current broker are skipped — they
        still live in the registry (so they appear in the More Bots silenced list)
        but no tab is created for them."""
        reg = _load_registry()
        broker = D.load_settings().get("broker_mode", "alpaca")
        custom = reg.get("custom", [])
        for side in reg["active"]:
            # V4.6.79 — use the shared broker-compatibility check (re-parses
            # META.brokers for legacy custom entries) so a bot the user marked
            # for one broker gets a runnable TAB only on that broker — and is
            # never silently tab-less in the broker it DOES support.
            if not bot_broker_compatible(side, broker, custom):
                continue
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

        # Shift static indices  (V3.1.3 added _botmarket_idx, V4.3.0 added _manual_idx)
        self._morebots_idx  += 1
        self._botmarket_idx += 1
        self._manual_idx    += 1
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
            # Shift static indices back  (V3.1.3 added _botmarket_idx, V4.3.0 _manual_idx)
            if idx < self._morebots_idx:  self._morebots_idx  -= 1
            if idx < self._botmarket_idx: self._botmarket_idx -= 1
            if idx < self._manual_idx:    self._manual_idx    -= 1
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
        # V4.6.24: also refresh the Tools tab's Alpaca-slot Assigned
        # dropdowns so the newly-added bot appears as an option without
        # restarting the app.
        QTimer.singleShot(0, lambda s=side: (
            self._add_bot_tab(s),
            self._sync_overview_blocks(),
            self._refresh_tools_slot_assigns(),
        ))

    def _on_bot_removed(self, side: str):
        """V7.1.1: deferred so the originating click signal finishes
        before the source widget is destroyed. Removing the QPushButton
        that fired the click from inside the click handler used to
        occasionally tear down the whole window."""
        QTimer.singleShot(0, lambda s=side: (
            self._do_remove_bot(s),
            self._refresh_tools_slot_assigns(),
        ))

    def _refresh_tools_slot_assigns(self):
        """V4.6.24 — repopulate the Tools tab's Alpaca-slot Assigned
        dropdowns so newly-added / removed bots appear correctly.
        Silently no-ops if Tools tab not built yet or method missing."""
        try:
            tools = getattr(self, "overview_tab", None)
            if tools and hasattr(tools, "refresh_alpaca_slot_assignments"):
                tools.refresh_alpaca_slot_assignments()
        except Exception as e:
            print(f"[overview] slot-assign refresh failed: {e}")

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
        """Called when the market tab's X button is clicked."""
        if hasattr(self, "_botmarket_idx"):
            self.tabs.tabBar().setTabVisible(self._botmarket_idx, False)
            # Send the user back to MORE BOTS, since that's where they
            # entered from.
            self.tabs.setCurrentIndex(self._morebots_idx)

    # ── CLOUD RESUME ─────────────────────────────────────────

    def _resume_cloud_bots(self):
        """Ask each cloud-flagged bot's controller to query Oracle for
        its current running state, and restore the UI accordingly.
        V4.6.33 — only resume bots flagged cloud in the CURRENT broker, so a
        cloud bot started under Alpaca doesn't show as running in the IBKR
        view.
        V4.6.41 — cloud instances are keyed per broker on the server now, so
        we just resume the current broker's cloud-flagged bots; each tab's
        cloud_resume_if_running() attaches to that broker's instance. (A bot
        also running on the other broker is independent and resumes when the
        user switches to that broker.)"""
        try:
            cloud_here = {s.upper() for s in D.get_cloud_bots()}
        except Exception:
            cloud_here = set()
        for side, tab in self._bot_tabs.items():
            if str(side).upper() not in cloud_here:
                continue
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

    # ── V4.0.1 — Terms of Service acceptance ─────────────────

    def _maybe_show_onboarding(self):
        """V4.6.94 — first time on a fresh account, show the welcome wizard
        (intro → pick broker → connect steps). Marks itself done so it only
        ever appears once. If the user picked a broker, reflect it in the UI."""
        try:
            from ui.onboarding import needs_onboarding, WelcomeWizard
            if not needs_onboarding():
                return
            dlg = WelcomeWizard(self)
            dlg.exec()
            # The wizard may have changed broker_mode — re-sync the header/tabs.
            try:
                self._set_broker_mode(self._current_broker_mode())
            except Exception:
                pass
        except Exception as e:
            print(f"[onboarding] wizard failed: {e}")

    def _check_tos_acceptance(self):
        """Hit /auth/tos and pop a modal if the user hasn't accepted yet.
        The modal blocks (modal exec) until the user explicitly clicks
        I Accept — Decline closes the app via _sign_out."""
        class _W(QThread):
            done = pyqtSignal(dict)
            def run(self_):
                import requests
                from ui.login import load_auth, load_server_url
                tok = (load_auth() or {}).get("token") or ""
                if not tok:
                    self_.done.emit({})
                    return
                try:
                    r = requests.get(f"{load_server_url()}/auth/tos",
                        headers={"Authorization": f"Bearer {tok}"},
                        timeout=10)
                    self_.done.emit(r.json() if r.ok else {})
                except Exception:
                    self_.done.emit({})

        w = _W()
        w.done.connect(self._on_tos_loaded)
        self._tos_workers = getattr(self, "_tos_workers", [])
        self._tos_workers.append(w)
        w.finished.connect(
            lambda _w=w: self._tos_workers.remove(_w)
                          if _w in self._tos_workers else None)
        w.start()

    def _on_tos_loaded(self, body: dict):
        if not body or not body.get("needs_accept"):
            return
        from PyQt6.QtWidgets import (
            QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout,
        )
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Terms & Conditions  v{body.get('version','?')}")
        dlg.setModal(True)
        dlg.setMinimumSize(640, 540)
        lv = QVBoxLayout(dlg)
        intro = QLabel(
            "<b>Please read and accept BAPTOU's Terms & Conditions "
            "before continuing.</b>")
        intro.setStyleSheet(f"color:{COLORS['text']};font-size:12px;padding:6px;")
        intro.setWordWrap(True)
        lv.addWidget(intro)
        body_box = QPlainTextEdit()
        body_box.setReadOnly(True)
        body_box.setPlainText(body.get("text", "(T&C text missing.)"))
        body_box.setStyleSheet(
            f"background:{COLORS['panel2']};color:{COLORS['text']};"
            f"border:none;border-radius:6px;"
            f"padding:10px;font-family:'JetBrains Mono';font-size:11px;")
        lv.addWidget(body_box, 1)
        btns = QDialogButtonBox()
        accept = btns.addButton("I Accept", QDialogButtonBox.ButtonRole.AcceptRole)
        decline = btns.addButton("Decline & sign out",
                                  QDialogButtonBox.ButtonRole.RejectRole)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lv.addWidget(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._send_tos_acceptance()
        else:
            # User declined → sign out so they can re-think
            self.statusBar().showMessage("T&C declined. Signing out.")
            QTimer.singleShot(800, self._sign_out)

    def _send_tos_acceptance(self):
        class _W(QThread):
            done = pyqtSignal(bool)
            def run(self_):
                import requests
                from ui.login import load_auth, load_server_url
                tok = (load_auth() or {}).get("token") or ""
                try:
                    r = requests.post(f"{load_server_url()}/auth/tos/accept",
                        headers={"Authorization": f"Bearer {tok}"},
                        timeout=10)
                    self_.done.emit(r.ok)
                except Exception:
                    self_.done.emit(False)

        w = _W()
        w.done.connect(lambda ok: print(f"[tos] accept ok={ok}"))
        self._tos_workers = getattr(self, "_tos_workers", [])
        self._tos_workers.append(w)
        w.finished.connect(
            lambda _w=w: self._tos_workers.remove(_w)
                          if _w in self._tos_workers else None)
        w.start()

    def _sync_revocations(self):
        """V3.3.0 — ask the server which of our installed bots have been
        removed by moderation, delete them locally."""
        class _W(QThread):
            done = pyqtSignal(list)
            def run(self_):
                import requests
                from ui.login import load_auth, load_server_url
                tok = (load_auth() or {}).get("token") or ""
                if not tok:
                    self_.done.emit([])
                    return
                try:
                    r = requests.get(
                        f"{load_server_url()}/bots/mine/revocations",
                        headers={"Authorization": f"Bearer {tok}"},
                        timeout=8)
                    if r.ok:
                        self_.done.emit(r.json().get("revoked_slugs", []) or [])
                    else:
                        self_.done.emit([])
                except Exception:
                    self_.done.emit([])

        w = _W()
        w.done.connect(self._apply_revocations)
        self._rev_workers = getattr(self, "_rev_workers", [])
        self._rev_workers.append(w)
        w.finished.connect(
            lambda _w=w: self._rev_workers.remove(_w)
                          if _w in self._rev_workers else None)
        w.start()

    def _apply_revocations(self, slugs: list):
        if not slugs:
            return
        from core.paths import DATA_DIR
        from pathlib import Path as _P
        # Remove from filesystem
        for slug in slugs:
            for ext in (".py", ".apex"):
                p = DATA_DIR / "bots" / f"{slug}{ext}"
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        # Remove from bot_registry (per-user-broker key)
        try:
            s = D.load_settings()
            rk = _registry_key()
            reg = s.get(rk, {"active": [], "silenced": [], "custom": []})
            reg["custom"] = [c for c in reg.get("custom", [])
                              if c.get("id") not in slugs]
            reg["active"] = [a for a in reg.get("active", [])
                              if a not in slugs]
            reg["silenced"] = [a for a in reg.get("silenced", [])
                                if a not in slugs]
            s[rk] = reg
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[revocations] registry update: {e}")
        # Toast
        QMessageBox.information(
            self, "Bots removed by moderation",
            f"{len(slugs)} bot{'s' if len(slugs)!=1 else ''} you had "
            f"installed were removed by BAPTOU moderators and have been "
            f"deleted from your library:\n\n  · "
            + "\n  · ".join(slugs) +
            "\n\nIf they were paid bots, the credits have been refunded "
            "to your BAPTOU balance.")

    def _open_credit_shop(self):
        """Open the Credit Shop dialog. Balance passed in so we skip
        an extra round-trip — it'll be refreshed inside the dialog."""
        try:
            bal = int(self.credits_chip.text().split()[1].replace(",", ""))
        except Exception:
            bal = 0
        dlg = CreditShopDialog(parent=self, current_balance=bal)
        dlg.balance_refreshed.connect(
            lambda b: self.credits_chip.setText(f"{b:,} credits"))
        dlg.exec()

    def _on_user_meta_loaded(self, me: dict, balance: int):
        # Credits chip — always update
        if hasattr(self, "credits_chip"):
            self.credits_chip.setText(f"{balance:,} credits")
        # Admin corner button — show only for admin roles
        role = (me or {}).get("role", "USER")
        is_admin = role in ("ADMIN", "SUB_BOSS_ADMIN", "BOSS_ADMIN")
        self._is_admin_user = is_admin   # V4.6.94 — manual-mode restore needs it
        if hasattr(self, "_corner_admin"):
            self._corner_admin.setVisible(is_admin and not self._is_manual_mode())
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
            menu.addAction(QAction("Show BAPTOU", self, triggered=self.show))
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
            self.user_chip_btn.setText(f"{display}  v")
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
                "BAPTOU",
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
            msg.setWindowTitle("Quit BAPTOU?")
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
            msg.setText("<br><br>".join(parts) + "<br><br>Quit BAPTOU?")
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
            # V4.6.65 — eagerly refresh ALL data tabs (Overview + every bot
            # tab), not just the visible one, so every tab is preloaded and
            # switching is instant instead of loading-on-click. Bot-tab refresh
            # is async (spawns a worker), so this never blocks the UI.
            targets = []
            ov = getattr(self, "overview_tab", None)
            if ov is not None:
                targets.append(ov)
            try:
                targets += list(self._bot_tabs.values())
            except Exception:
                pass
            cur = self.tabs.widget(self.tabs.currentIndex())
            if cur is not None and cur not in targets:
                targets.append(cur)
            for tab in targets:
                if hasattr(tab, "refresh"):
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
                    f"border:none;border-radius:12px;"
                    f"background:rgba(122,181,162,0.08);"
                )
            else:
                self._mkt_open_prev = False
                nxt = ck.next_open.astimezone().strftime("%b %d %H:%M")
                self.mkt_label.setText(f"● CLOSED  {nxt}")
                self.mkt_label.setStyleSheet(
                    f"font-size:10px;font-weight:600;letter-spacing:2px;"
                    f"color:{C['red']};padding:3px 12px;"
                    f"border:none;border-radius:12px;"
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
        started/stopped on the market-open / market-close edge.

        V4.6.21 — emits [schedule] diagnostic lines on every tick so
        users can see in the apex_crash.log / console exactly why
        bots aren't auto-starting. Common causes: no checkbox ticked,
        broker mode not Alpaca, market clock unreachable, edge
        already passed (APEX started after market open)."""
        try:
            scheduled = set(D.get_auto_schedule_active_bots())
            if not scheduled:
                # V4.6.21 — log this rarely (once per ~5 ticks ≈ 5 min)
                # so the user notices when nothing is scheduled.
                if getattr(self, "_sched_log_counter", 0) % 5 == 0:
                    print("[schedule] no bots have auto-start enabled — "
                          "check the AUTOMATION row in Overview", flush=True)
                self._sched_log_counter = \
                    getattr(self, "_sched_log_counter", 0) + 1
                return
            from core.schedule import market_is_open
            is_open = market_is_open()
            if is_open is None:
                print("[schedule] market clock unreachable — broker not "
                      "Alpaca, or get_clock() raised. Scheduled bots: "
                      f"{sorted(scheduled)}", flush=True)
                return
            print(f"[schedule] tick: market_is_open={is_open}  "
                  f"prev={self._mkt_open_prev}  "
                  f"scheduled={sorted(scheduled)}", flush=True)
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
                "BAPTOU Update Available",
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

def _run_bot(side: str, script_path: str = "") -> int:
    """V4.6.15 — accepts an optional script_path for UNIVERSE side.
    When the user picks a custom universe generator from the Universe
    tab dropdown, the BotProcessWidget passes that script's path as
    a 3rd CLI arg. We then exec the script directly instead of
    running the built-in universe_manager (which would silently
    overwrite the user's selection)."""
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
        # If a custom script was passed, execute IT instead of the
        # built-in universe_manager. Use runpy so the script's
        # __name__ == "__main__" block fires.
        if script_path:
            from pathlib import Path as _P
            sp = _P(script_path)
            if not sp.exists():
                print(f"[universe] script not found: {sp}", flush=True)
                return 2
            print(f"[universe] running custom script {sp.name}",
                  flush=True)
            try:
                import runpy
                runpy.run_path(str(sp), run_name="__main__")
                return 0
            except SystemExit as e:
                return int(getattr(e, "code", 0) or 0)
            except Exception as e:
                import traceback
                print(f"[universe] script crashed: {e}", flush=True)
                traceback.print_exc()
                return 1
        # No script_path → built-in universe_manager (legacy behavior)
        um = importlib.import_module("universe_manager")
        um.run_manager(["DAY", "LONG", "SHORT"])
        return 0
    mod_name = {"LONG": "longbot_v2",
                "SHORT": "shortbot_v2",
                "DAY": "daybot"}.get(side_u)
    if mod_name:
        bot = importlib.import_module(mod_name)
        bot.main()
        return 0

    # V4.6.25 — custom bot path. Built-in lookup failed, so this is
    # a custom bot. Find its .py in DATA_DIR/bots/ (or universe_scripts/)
    # and exec via runpy. Without this, every locally-run custom bot
    # died with 'Unknown bot' because _run_bot only knew about LONG /
    # SHORT / DAY / UNIVERSE.
    from pathlib import Path as _P
    candidates = []
    if script_path:
        candidates.append(_P(script_path))
    # Slug as stored in the registry — lowercase
    slug = side.lower()
    candidates.extend([
        DATA_DIR / "bots" / f"{slug}.py",
        DATA_DIR / "bots" / f"{slug}.apex",
        DATA_DIR / "universe_scripts" / f"{slug}.py",
    ])
    # Case-insensitive scan as a last resort
    for base in (DATA_DIR / "bots", DATA_DIR / "universe_scripts"):
        if base.exists():
            for p in base.iterdir():
                if p.is_file() and p.stem.lower() == slug:
                    candidates.append(p)
    for sp in candidates:
        if sp and sp.exists():
            print(f"[bot] running custom script {sp.name} "
                  f"(side={side})", flush=True)
            # Decrypt .apex on the fly if library is locked
            run_path_ = sp
            if sp.suffix == ".apex":
                try:
                    from core import secure as _sec
                    run_path_ = _sec.decrypted_temp_file(sp)
                except Exception as e:
                    print(f"[bot] could not decrypt {sp.name}: {e}",
                          flush=True)
                    return 1
            try:
                import runpy
                runpy.run_path(str(run_path_), run_name="__main__")
                return 0
            except SystemExit as e:
                return int(getattr(e, "code", 0) or 0)
            except Exception as e:
                import traceback
                print(f"[bot] script crashed: {e}", flush=True)
                traceback.print_exc()
                return 1
    print(f"Unknown bot: {side}  (not in built-ins, no .py found in "
          f"{DATA_DIR / 'bots'} or universe_scripts/)", flush=True)
    return 2


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

def _install_exception_handler():
    """v3.1.9 — replace Python's default 'print to stderr and die'
    behaviour with a friendly QMessageBox + writing the traceback to
    DATA_DIR/apex_crash.log. The app keeps running afterwards."""
    import traceback as _tb
    def _handler(etype, value, tb):
        try:
            from core.paths import DATA_DIR
            import datetime
            text = "".join(_tb.format_exception(etype, value, tb))
            log = DATA_DIR / "apex_crash.log"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.datetime.now().isoformat()} ===\n")
                f.write(text)
            # Try to show a dialog. If QApplication isn't up yet we
            # silently fall through to the default print behaviour.
            app = QApplication.instance()
            if app:
                short = f"{etype.__name__}: {value}"
                box = QMessageBox()
                box.setWindowTitle("BAPTOU hit an unexpected error")
                box.setIcon(QMessageBox.Icon.Warning)
                box.setText(f"<b>{short}</b><br><br>"
                            "BAPTOU caught this exception so it doesn't crash. "
                            "Details (and a full traceback) were saved to:"
                            f"<br><code>{log}</code><br><br>"
                            "If this is reproducible, send me that file.")
                box.setDetailedText(text)
                box.setStandardButtons(QMessageBox.StandardButton.Ok)
                box.exec()
        except Exception:
            _tb.print_exception(etype, value, tb)
    sys.excepthook = _handler


class _LoadingSplash(QWidget):
    """V4.6.67 — startup splash with a progress bar shown while BOTH brokers'
    data is preloaded, so the app opens fully populated."""
    def __init__(self):
        from PyQt6.QtWidgets import QProgressBar
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.SplashScreen)
        self.setFixedSize(460, 220)
        self.setStyleSheet(
            "background:#0e1016;border:1px solid #2a2f3e;border-radius:14px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(34, 30, 34, 30)
        lay.setSpacing(14)
        title = QLabel("BAPTOU")
        title.setStyleSheet("color:#7aa2ff;font-size:30px;font-weight:800;"
                            "letter-spacing:3px;background:transparent;border:none;")
        sub = QLabel("TRADING PLATFORM")
        sub.setStyleSheet("color:#8a93c9;font-size:11px;letter-spacing:4px;"
                          "background:transparent;border:none;")
        lay.addStretch()
        lay.addWidget(title)
        lay.addWidget(sub)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet(
            "QProgressBar{background:#1b1f2b;border:none;border-radius:4px;}"
            "QProgressBar::chunk{background:#5b6cf0;border-radius:4px;}")
        lay.addWidget(self.bar)
        self.status = QLabel("Starting…")
        self.status.setStyleSheet("color:#6b7390;font-size:10px;"
                                  "background:transparent;border:none;")
        lay.addWidget(self.status)
        lay.addStretch()
        # Center on the primary screen
        try:
            scr = QApplication.primaryScreen().geometry()
            self.move(scr.center().x() - 230, scr.center().y() - 110)
        except Exception:
            pass

    def set_progress(self, pct: int, msg: str = ""):
        try:
            self.bar.setValue(max(0, min(100, int(pct))))
            if msg:
                self.status.setText(msg)
        except Exception:
            pass


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-bot":
        # V4.6.15 — 4th arg is the optional explicit script path for
        # UNIVERSE side (lets the user's Universe-tab dropdown
        # selection actually win over the built-in default).
        _sp = sys.argv[3] if len(sys.argv) >= 4 else ""
        sys.exit(_run_bot(sys.argv[2], _sp))

    if not _check_single_instance():
        sys.exit(0)

    ensure_data_dir()
    app = QApplication(sys.argv)
    app.setApplicationName("BAPTOU Trading Platform")
    app.setApplicationVersion(get_current_version())
    app.setWindowIcon(_app_icon())          # v3.1.6 — default for every window
    app.setQuitOnLastWindowClosed(False)
    _install_exception_handler()

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
        from PyQt6.QtCore import QThread, pyqtSignal, QTimer as _QT
        # Show the splash FIRST so it appears instantly, then build the window
        # (heavy) and preload both brokers behind it.
        splash = _LoadingSplash()
        try:
            splash.show(); app.processEvents()
        except Exception:
            splash = None

        w = ApexWindow(user=user)
        app._main_window = w

        try:
            sides = list(w._bot_tabs.keys()) or ["LONG", "SHORT", "DAY"]
        except Exception:
            sides = ["LONG", "SHORT", "DAY"]

        class _Preload(QThread):
            progress = pyqtSignal(int, str)
            finished_ = pyqtSignal()
            def run(self):
                try:
                    import core.data as _D
                    cur = _D.load_settings().get("broker_mode", "alpaca")
                    order = [cur, "ibkr" if cur == "alpaca" else "alpaca"]
                    total = max(1, len(order) * len(sides))
                    i = 0
                    for brk in order:
                        for s in sides:
                            try:
                                _D.prefetch_broker(brk, [s])
                            except Exception:
                                pass
                            i += 1
                            self.progress.emit(int(i * 100 / total),
                                               f"Loading {brk.upper()} · {s}…")
                except Exception:
                    pass
                self.finished_.emit()

        _shown = {"v": False}
        def _reveal():
            if _shown["v"]:
                return
            _shown["v"] = True
            try:
                if splash is not None:
                    splash.close()
            except Exception:
                pass
            w.show()
            try:
                w._refresh_all()
            except Exception:
                pass

        try:
            pre = _Preload()
            app._preload_thread = pre   # keep a ref so it isn't GC'd
            if splash is not None:
                pre.progress.connect(splash.set_progress)
            pre.finished_.connect(_reveal)
            pre.start()
        except Exception:
            _reveal()
        # Hard timeout — the app ALWAYS opens even if preload stalls.
        _QT.singleShot(15000, _reveal)

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


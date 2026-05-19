"""
APEX Trading Platform — Desktop App
PyQt6-based native Windows application.

Run:  python main.py
Build: pyinstaller --onefile --windowed --icon=assets/icon.ico main.py
"""

import sys
import os
from pathlib import Path

# Immediate feedback before the (heavy/slow) imports below, so the
# launcher window is never a silent blank box. Guarded: a frozen
# --windowed build has no console (sys.stdout is None).
try:
    if sys.stdout is not None:
        print("Starting APEX Trading Platform - loading libraries, "
              "the window can take 1-2 min on first launch...", flush=True)
except Exception:
    pass

# Add app directory to path so core/ imports work
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QFrame, QScrollArea,
    QSplitter, QSystemTrayIcon, QMenu, QMessageBox, QProgressDialog,
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QSize, QPoint
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap, QFontDatabase,
    QAction,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from ui.overview   import OverviewTab
from ui.bot_tab    import BotTab
from ui.overview  import ToolsTab
from ui.universe   import UniverseTab
from ui.styles     import DARK_STYLESHEET, COLORS
from core.updater  import (check_for_update, download_and_apply,
                            get_current_version, restart_app,
                            launch_downloaded_installer)
from core.paths     import DATA_DIR, ensure_data_dir


# ─────────────────────────────────────────
# UPDATE WORKER THREAD
# ─────────────────────────────────────────

class UpdateWorker(QThread):
    progress   = pyqtSignal(int, str)
    finished   = pyqtSignal(bool, str)

    def __init__(self, update_info):
        super().__init__()
        self.update_info = update_info

    def run(self):
        def cb(pct, msg):
            self.progress.emit(pct, msg)
        ok, msg = download_and_apply(self.update_info, progress_callback=cb)
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

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"APEX Trading Platform  v{get_current_version()}")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)
        self.setStyleSheet(DARK_STYLESHEET)

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setSpacing(0)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Header
        header = self._build_header()
        root_layout.addWidget(header)

        # Tab bar
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName("mainTabs")
        root_layout.addWidget(self.tabs)

        # Tabs
        self.overview_tab = OverviewTab()
        self.long_tab     = BotTab("LONG")
        self.short_tab    = BotTab("SHORT")
        self.day_tab      = BotTab("DAY")
        self.universe_tab = UniverseTab()
        self.tools_tab    = ToolsTab()

        self.tabs.addTab(self.overview_tab, "◈  OVERVIEW")
        self.tabs.addTab(self.long_tab,     "▲  LONG")
        self.tabs.addTab(self.short_tab,    "▼  SHORT")
        self.tabs.addTab(self.day_tab,      "◆  DAY")
        self.tabs.addTab(self.universe_tab, "✦  UNIVERSE")
        self.tabs.addTab(self.tools_tab,    "⚙  TOOLS")

        # Status bar
        self.statusBar().setStyleSheet(
            f"background:{COLORS['panel']};color:{COLORS['muted']};"
            f"font-family:'JetBrains Mono';font-size:11px;padding:2px 8px;"
        )
        self.statusBar().showMessage("APEX ready")

        # Clock timer
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(1000)

        # Data refresh timer (20 seconds)
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._refresh_all)
        self.refresh_timer.start(45_000)   # 45s: live but far less blinking

        # System tray
        self._setup_tray()

        # Auto-trade scheduler (start at US open / stop at close) — checks
        # every 60s. Off unless enabled in TOOLS.
        self._mkt_open_prev = None
        self.sched_timer = QTimer()
        self.sched_timer.timeout.connect(self._tick_schedule)
        self.sched_timer.start(60_000)
        QTimer.singleShot(8000, self._tick_schedule)

        # Auto-update: installed build only, and only in the overnight
        # window. Never from source (run.bat) so daytime testing is safe.
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._maybe_auto_update)
        self.update_timer.start(60 * 60 * 1000)          # hourly
        QTimer.singleShot(15000, self._maybe_auto_update)

        # Initial data load
        QTimer.singleShot(500, self._refresh_all)

    # ── HEADER ──────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("appHeader")
        header.setFixedHeight(54)
        header.setStyleSheet(
            f"QFrame#appHeader {{"
            f"  background:{COLORS['panel']};"
            f"  border-bottom:1px solid {COLORS['border']};"
            f"}}"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 0, 18, 0)

        # Logo
        logo = QLabel("APEX")
        logo.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;"
            f"color:{COLORS['green']};letter-spacing:4px;"
        )
        sub = QLabel("TRADING PLATFORM")
        sub.setStyleSheet(
            f"font-size:9px;color:{COLORS['muted']};letter-spacing:3px;"
            f"margin-top:2px;"
        )
        brand_v = QVBoxLayout()
        brand_v.setSpacing(0)
        brand_v.addWidget(logo)
        brand_v.addWidget(sub)

        layout.addLayout(brand_v)
        layout.addStretch()

        # Market status
        self.mkt_label = QLabel("● CHECKING...")
        self.mkt_label.setStyleSheet(
            f"font-size:10px;font-weight:600;letter-spacing:2px;"
            f"color:{COLORS['muted']};padding:3px 10px;"
            f"border:1px solid {COLORS['border']};border-radius:12px;"
        )
        layout.addWidget(self.mkt_label)

        # Clock
        self.clock_label = QLabel()
        self.clock_label.setStyleSheet(
            f"font-size:11px;color:{COLORS['muted']};margin-left:16px;"
        )
        layout.addWidget(self.clock_label)

        # Update button (hidden until update found)
        self.update_btn = QPushButton("⬆ UPDATE AVAILABLE")
        self.update_btn.setVisible(False)
        self.update_btn.setObjectName("updateBtn")
        self.update_btn.clicked.connect(self._show_update_dialog)
        layout.addWidget(self.update_btn)

        return header

    # ── SYSTEM TRAY ─────────────────────────────────────────

    def _setup_tray(self):
        try:
            self.tray = QSystemTrayIcon(self)
            # Use a simple coloured pixmap as icon
            pm = QPixmap(16, 16)
            pm.fill(QColor(COLORS["green"]))
            self.tray.setIcon(QIcon(pm))
            tray_menu = QMenu()
            show_act  = QAction("Show APEX", self)
            show_act.triggered.connect(self.show)
            quit_act  = QAction("Quit", self)
            quit_act.triggered.connect(QApplication.quit)
            tray_menu.addAction(show_act)
            tray_menu.addSeparator()
            tray_menu.addAction(quit_act)
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
        except Exception as e:
            print(f"[tray] {e}")

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        if hasattr(self,"tray"):
            self.tray.showMessage(
                "APEX", "Running in background",
                QSystemTrayIcon.MessageIcon.Information, 2000
            )

    # ── CLOCK ───────────────────────────────────────────────

    def _tick_clock(self):
        from datetime import datetime
        now = datetime.now().strftime("%a %d %b %Y  —  %H:%M:%S")
        self.clock_label.setText(now)

    # ── REFRESH ─────────────────────────────────────────────

    def _refresh_all(self):
        self.statusBar().showMessage("Refreshing data...")
        try:
            # Only refresh the active tab to save API calls
            idx = self.tabs.currentIndex()
            tab_map = {
                0: self.overview_tab,
                1: self.long_tab,
                2: self.short_tab,
                3: self.day_tab,
                4: self.universe_tab,
                5: self.tools_tab,
            }
            tab = tab_map.get(idx)
            if tab and hasattr(tab, "refresh"):
                tab.refresh()

            # Always refresh market status
            self._refresh_market_status()
            self.statusBar().showMessage(
                f"Last updated: {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
            )
        except Exception as e:
            self.statusBar().showMessage(f"Refresh error: {e}")

    def _refresh_market_status(self):
        try:
            from core.data import get_client
            c = get_client("LONG")
            if c:
                ck = c.get_clock()
                if ck.is_open:
                    self.mkt_label.setText("● MARKET OPEN")
                    self.mkt_label.setStyleSheet(
                        f"font-size:10px;font-weight:600;letter-spacing:2px;"
                        f"color:{COLORS['green']};padding:3px 10px;"
                        f"border:1px solid {COLORS['green']};border-radius:12px;"
                        f"background:rgba(0,245,195,0.08);"
                    )
                else:
                    nxt = ck.next_open.astimezone().strftime("%b %d %H:%M")
                    self.mkt_label.setText(f"● CLOSED  {nxt}")
                    self.mkt_label.setStyleSheet(
                        f"font-size:10px;font-weight:600;letter-spacing:2px;"
                        f"color:{COLORS['muted']};padding:3px 10px;"
                        f"border:1px solid {COLORS['border']};border-radius:12px;"
                    )
        except Exception:
            pass

    # ── UPDATES ─────────────────────────────────────────────

    # ── AUTO-TRADE SCHEDULE ─────────────────────────────────

    def _tick_schedule(self):
        try:
            from core.data import get_auto_schedule
            if not get_auto_schedule():
                return
            from core.schedule import market_is_open
            is_open = market_is_open()
            if is_open is None:
                return
            tabs = [self.long_tab, self.short_tab, self.day_tab]
            if is_open and self._mkt_open_prev in (None, False):
                for t in tabs:
                    bc = getattr(t, "bot_ctrl", None)
                    if bc and not bc.is_running():
                        bc.start_bot()
                self.statusBar().showMessage(
                    "Auto-schedule: US market OPEN — bots started")
            elif (not is_open) and self._mkt_open_prev in (None, True):
                for t in tabs:
                    bc = getattr(t, "bot_ctrl", None)
                    if bc and bc.is_running():
                        bc.stop_bot()
                self.statusBar().showMessage(
                    "Auto-schedule: US market CLOSED — bots stopped")
            self._mkt_open_prev = is_open
        except Exception:
            pass

    # ── AUTO-UPDATE (installed build, overnight window only) ─

    def _maybe_auto_update(self):
        if not getattr(sys, "frozen", False):
            return  # running from source (run.bat) never auto-updates
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
                self._on_update_found(info)   # window closed: just flag it
                return
        except Exception:
            return
        self._pending_update = info
        self._do_update(info)                 # download, apply, restart

    def _check_updates(self):
        self._update_checker = UpdateChecker()
        self._update_checker.update_available.connect(self._on_update_found)
        self._update_checker.start()

    def _on_update_found(self, info: dict):
        self._pending_update = info
        self.update_btn.setVisible(True)
        self.update_btn.setStyleSheet(
            f"background:rgba(123,140,255,0.15);border:1px solid {COLORS['purple']};"
            f"color:{COLORS['purple']};font-family:'JetBrains Mono';font-size:10px;"
            f"font-weight:600;padding:5px 12px;border-radius:6px;letter-spacing:1px;"
        )
        if hasattr(self,"tray"):
            self.tray.showMessage(
                "APEX Update Available",
                f"v{info['latest']} is ready to install.",
                QSystemTrayIcon.MessageIcon.Information, 5000
            )

    def _show_update_dialog(self):
        info = getattr(self,"_pending_update",{})
        msg  = QMessageBox(self)
        msg.setWindowTitle("Update Available")
        msg.setText(
            f"<b>APEX v{info.get('latest','?')} is available</b><br><br>"
            f"Current version: {info.get('current','?')}<br><br>"
            f"<i>{info.get('notes','')}</i><br><br>"
            f"Update now? The app will restart automatically."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
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
            lambda pct, msg: (progress.setValue(pct), progress.setLabelText(msg))
        )
        self._update_worker.finished.connect(
            lambda ok, msg: self._on_update_done(ok, msg, progress)
        )
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

        # Frozen install path: downloader saved a local installer and
        # stripped Mark-of-the-Web. Ask the user before we close the
        # app to install — they get to see the download finish and
        # decide when to apply it.
        local = (getattr(self, "_pending_update", {}) or {}).get("_local_path")
        if local and getattr(sys, "frozen", False):
            box = QMessageBox(self)
            box.setWindowTitle("Update ready")
            box.setText(
                f"<b>{msg}</b><br><br>"
                f"Click <b>Install now</b> to apply it. APEX will close "
                f"while the installer runs (~1 minute) and then reopen "
                f"automatically. You can also choose <b>Later</b> and "
                f"install on next launch."
            )
            box.setStyleSheet(self.styleSheet())
            install_btn = box.addButton("Install now",
                                        QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is install_btn:
                launch_downloaded_installer(local)   # exits the process
            return

        # Source-mode (file-replacement) path: keep the original restart.
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Update Complete")
        dlg.setText(msg)
        dlg.setStyleSheet(self.styleSheet())
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()
        restart_app()


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

def _run_bot(side: str) -> int:
    """
    Frozen entry point. In a PyInstaller build sys.executable is APEX.exe
    (not python), so the GUI launches `APEX.exe --run-bot SIDE` and we run
    the bot in-process here instead of starting the GUI.
    """
    # A --windowed PyInstaller build has no console, so Python sets
    # sys.stdout/stderr to None. QProcess still assigns its capture pipes
    # as this child's OS handles (fd 1/2), so rebuild text streams on them
    # — otherwise the bot's print() output never reaches the in-app log.
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


def main():
    # Frozen multi-process dispatch: APEX.exe --run-bot LONG|SHORT|DAY
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-bot":
        sys.exit(_run_bot(sys.argv[2]))

    ensure_data_dir()
    app = QApplication(sys.argv)
    app.setApplicationName("APEX Trading Platform")
    app.setApplicationVersion(get_current_version())
    app.setQuitOnLastWindowClosed(False)

    # Stop charts/tables/console from hijacking the page scroll.
    from ui.widgets import WheelGuard
    app._wheel_guard = WheelGuard()          # keep ref so it isn't GC'd
    app.installEventFilter(app._wheel_guard)

    # Load JetBrains Mono if available
    try:
        QFontDatabase.addApplicationFont("assets/JetBrainsMono-Regular.ttf")
    except Exception:
        pass

    window = ApexWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

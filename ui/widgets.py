"""
APEX Reusable Widgets
Common Qt components used across all tabs.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QTableWidget, QTableWidgetItem,
    QSizePolicy, QTextEdit, QComboBox, QFileDialog,
    QHeaderView, QAbstractItemView, QGridLayout,
)
from PyQt6.QtCore import (
    Qt, QUrl, QTimer, QProcess, QProcessEnvironment, pyqtSignal, QSize,
    QObject, QEvent,
)
from PyQt6.QtGui import QColor, QFont, QTextCursor
from PyQt6.QtWebEngineWidgets import QWebEngineView

from ui.styles import COLORS, BOT_COLOR
from core.paths import DATA_DIR

C = COLORS


# ─────────────────────────────────────────
# CHART VIEW (embeds Plotly HTML)
# ─────────────────────────────────────────

class ChartView(QWebEngineView):
    """Embeds a Plotly chart HTML string."""

    def __init__(self, height=280, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.page().setBackgroundColor(QColor(C["bg"]))
        self._last_html = None
        self._loading   = False
        self._pending   = None
        self.loadFinished.connect(self._on_loaded)
        self._show_placeholder()

    def _on_loaded(self, ok):
        self._loading = False
        if self._pending is not None:
            nxt, self._pending = self._pending, None
            self._do_load(nxt)

    def _do_load(self, html: str):
        self._last_html = html
        self._loading   = True
        self.setHtml(html, QUrl("about:blank"))

    def _show_placeholder(self):
        self.setHtml(f"""
        <html><body style="background:{C['bg']};margin:0;
        display:flex;align-items:center;justify-content:center;height:100vh;">
        <span style="color:{C['muted']};font-family:'JetBrains Mono';font-size:12px;">
        Loading...</span></body></html>""")

    def load_chart(self, html: str):
        # 1) Skip if nothing changed -> no blink when data is unchanged.
        if html == self._last_html:
            return
        # 2) If a previous chart is still rendering, don't interrupt it
        #    (that's the "reloads before it even loads" flicker). Remember
        #    the latest and apply it once the current render finishes.
        if self._loading:
            self._pending = html
            return
        self._do_load(html)


# ─────────────────────────────────────────
# METRIC CARD
# ─────────────────────────────────────────

class MetricCard(QFrame):
    """
    A single metric card with label, value, and optional sub-label.
    Like the cards in the web version.
    """

    def __init__(self, label: str, value: str = "—",
                 color: str = C["text"], sub: str = "",
                 tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedHeight(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._color = color

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(2)

        label_text = f"{label.upper()}  ⓘ" if tooltip else label.upper()
        self._label = QLabel(label_text)
        self._label.setStyleSheet(
            f"font-size:8px;color:{C['muted']};letter-spacing:3px;"
            f"font-family:'JetBrains Mono';"
        )
        if tooltip:
            self._label.setToolTip(tooltip)
            self.setToolTip(tooltip)

        self._value = QLabel(value)
        self._value.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:17px;"
            f"font-weight:700;color:{color};"
        )
        self._value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        layout.addWidget(self._label)
        layout.addWidget(self._value)

        if sub:
            self._sub = QLabel(sub)
            self._sub.setStyleSheet(
                f"font-size:9px;color:{C['muted']};"
            )
            layout.addWidget(self._sub)
        else:
            self._sub = None

        # Top accent line via stylesheet
        self.setStyleSheet(
            f"QFrame#card {{"
            f"  background:{C['panel']};"
            f"  border:1px solid {C['border']};"
            f"  border-radius:8px;"
            f"  border-top:2px solid {color};"
            f"}}"
        )

    def update_value(self, value: str, color: str = None,
                     sub: str = None):
        self._value.setText(value)
        c = color or self._color
        self._value.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:17px;"
            f"font-weight:700;color:{c};"
        )
        self.setStyleSheet(
            f"QFrame#card {{"
            f"  background:{C['panel']};"
            f"  border:1px solid {C['border']};"
            f"  border-radius:8px;"
            f"  border-top:2px solid {c};"
            f"}}"
        )
        if sub is not None and self._sub:
            self._sub.setText(sub)


# ─────────────────────────────────────────
# SECTION HEADER
# ─────────────────────────────────────────

class SectionHeader(QWidget):
    def __init__(self, title: str, color: str = C["green"],
                 controls: QWidget = None, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 18, 0, 10)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:10px;font-weight:600;"
            f"letter-spacing:2px;color:{C['muted']};"
            f"text-transform:uppercase;"
        )
        row.addWidget(lbl)
        row.addStretch()
        if controls:
            row.addWidget(controls)
        outer.addLayout(row)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        rule.setStyleSheet(
            f"background:{C['border']};border:none;max-height:1px;"
        )
        outer.addWidget(rule)


# ─────────────────────────────────────────
# BOT PROCESS CONTROL
# ─────────────────────────────────────────

class BotProcessWidget(QWidget):
    """
    Run/Stop buttons + live log output for a bot script.
    Uses QProcess to run the bot as a subprocess.
    """

    status_changed = pyqtSignal(str, bool)  # (side, is_running)

    def __init__(self, side: str, script_path: Path, parent=None):
        super().__init__(parent)
        self.side        = side
        self.script_path = script_path
        self.process     = None
        color            = BOT_COLOR.get(side, C["green"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Control row ──────────────────────────────────────
        ctrl = QHBoxLayout()

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color:{C['muted']};font-size:14px;")

        self.status_lbl = QLabel("STOPPED")
        self.status_lbl.setStyleSheet(
            f"font-size:10px;color:{C['muted']};letter-spacing:2px;"
            f"font-weight:600;"
        )

        self.run_btn = QPushButton("▶  RUN BOT")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.setFixedHeight(32)
        self.run_btn.clicked.connect(self.start_bot)

        self.stop_btn = QPushButton("■  STOP")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setFixedHeight(32)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_bot)

        self.restart_btn = QPushButton("↺  RESTART")
        self.restart_btn.setFixedHeight(32)
        self.restart_btn.setEnabled(False)
        self.restart_btn.clicked.connect(self.restart_bot)

        # Script path display
        script_lbl = QLabel(str(self.script_path.name))
        script_lbl.setStyleSheet(f"color:{C['muted']};font-size:10px;")

        ctrl.addWidget(self.status_dot)
        ctrl.addWidget(self.status_lbl)
        ctrl.addSpacing(12)
        ctrl.addWidget(script_lbl)
        ctrl.addStretch()
        ctrl.addWidget(self.run_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addWidget(self.restart_btn)

        layout.addLayout(ctrl)

        # ── Log output ───────────────────────────────────────
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(160)
        self.log.setPlaceholderText("Bot output will appear here...")
        self.log.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:5px;"
            f"font-size:10px;font-family:'JetBrains Mono';padding:6px;"
        )
        layout.addWidget(self.log)

        # ── Upload row ───────────────────────────────────────
        upload_row = QHBoxLayout()
        self.upload_btn = QPushButton("📂  Replace Bot Script")
        self.upload_btn.clicked.connect(self.upload_script)
        self.upload_lbl = QLabel("")
        self.upload_lbl.setStyleSheet(f"color:{C['green']};font-size:10px;")
        upload_row.addWidget(self.upload_btn)
        upload_row.addWidget(self.upload_lbl)
        upload_row.addStretch()
        layout.addLayout(upload_row)

    def _log(self, msg: str, color: str = None):
        from datetime import datetime
        ts   = datetime.now().strftime("%H:%M:%S")
        c    = color or C["text"]
        html = f'<span style="color:{C["muted"]}">[{ts}]</span> <span style="color:{c}">{msg}</span>'
        self.log.append(html)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def _set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.restart_btn.setEnabled(running)
        cloud = self._is_cloud_mode()
        dot_color = (C["purple"] if running and cloud
                     else C["green"] if running
                     else C["muted"])
        self.status_dot.setStyleSheet(f"color:{dot_color};font-size:14px;")
        if running:
            label = "RUNNING ☁ ORACLE" if cloud else "RUNNING"
        else:
            label = "STOPPED  (cloud)" if cloud else "STOPPED"
        self.status_lbl.setText(label)
        self.status_lbl.setStyleSheet(
            f"font-size:10px;color:{dot_color};letter-spacing:2px;font-weight:600;"
        )
        self.status_changed.emit(self.side, running)

    def is_running(self) -> bool:
        # V7.1.13: a bot running on the Oracle server has no local
        # QProcess. Cloud running-state is tracked via _cloud_running
        # (set by the success of /bots/{side}/start and cleared by
        # /bots/{side}/stop or the 30s status poll).
        if getattr(self, "_cloud_running", False):
            return True
        return (self.process is not None
                and self.process.state() == QProcess.ProcessState.Running)

    def _is_cloud_mode(self) -> bool:
        """True if the user has flagged this bot for cloud execution."""
        try:
            from core import data as _D
            return _D.is_cloud_bot(self.side)
        except Exception:
            return False

    def start_bot(self):
        # V7.1.13: route to the cloud path when this bot is in
        # cloud_bots. Local QProcess code only runs for laptop bots.
        if self._is_cloud_mode():
            return self._cloud_start()
        frozen = getattr(sys, "frozen", False)
        # In a frozen build the bot code is bundled inside APEX.exe and
        # launched via `APEX.exe --run-bot SIDE`, so there is no .py on
        # disk to check. Only validate the path when running from source.
        if not frozen and not self.script_path.exists():
            self._log(f"Script not found: {self.script_path}", C["red"])
            return
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(env)

        if frozen:
            # Installed build: sys.executable is APEX.exe, not python, and
            # the bot .py files live inside the exe. Launch the dedicated
            # --run-bot entry point with data living in DATA_DIR.
            self.process.setWorkingDirectory(str(DATA_DIR))
            self.process.start(sys.executable, ["--run-bot", self.side])
        else:
            self.process.start(sys.executable, ["-u", str(self.script_path)])
        if self.process.waitForStarted(3000):
            self._set_running(True)
            self._log(f"Started {self.script_path.name}", C["green"])
            self._log("Loading libraries… the bot stays silent while it "
                      "imports (longbot ~1 min, daybot up to ~2 min). "
                      "This is normal — wait for its header.", C["muted"])
        else:
            self._log("Failed to start process", C["red"])

    def stop_bot(self):
        # V7.1.13: cloud bots get a /bots/{side}/stop call instead of
        # a local QProcess.terminate.
        if self._is_cloud_mode() or getattr(self, "_cloud_running", False):
            return self._cloud_stop()
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.process.terminate()
            if not self.process.waitForFinished(5000):
                self.process.kill()
            self._log("Bot stopped", C["yellow"])
        self._set_running(False)

    # ── V7.1.13: cloud-execution path ──────────────────────────────

    def _cloud_call(self, method: str, path: str,
                    on_done, payload: dict | None = None):
        """Spawn a QThread that calls the APEX server with the
        signed-in user's bearer token. on_done(ok: bool, data: dict)
        runs on the main thread when finished."""
        from PyQt6.QtCore import QThread, pyqtSignal as _Sig
        from ui.login import load_auth, load_server_url
        token = (load_auth() or {}).get("token")
        if not token:
            self._log("Cloud mode needs a signed-in APEX account. "
                      "Open APEX and sign in.", C["red"])
            on_done(False, {"detail": "not signed in"})
            return
        url = f"{load_server_url()}{path}"

        class _W(QThread):
            done = _Sig(bool, dict)
            def __init__(self, m, u, t, p):
                super().__init__()
                self.m, self.u, self.t, self.p = m, u, t, p
            def run(self):
                import requests
                try:
                    fn = {"GET": requests.get, "POST": requests.post,
                          "PUT": requests.put}.get(self.m, requests.get)
                    r = fn(self.u, headers={"Authorization": f"Bearer {self.t}"},
                           json=self.p, timeout=15)
                    try:
                        body = r.json()
                    except Exception:
                        body = {"text": r.text}
                    self.done.emit(r.ok, body)
                except Exception as e:
                    self.done.emit(False, {"detail": str(e)})

        worker = _W(method, url, token, payload)
        worker.done.connect(on_done)
        worker.start()
        # Keep a reference so the worker isn't garbage-collected
        self._http_workers = getattr(self, "_http_workers", [])
        self._http_workers.append(worker)
        # Trim finished workers
        self._http_workers = [w for w in self._http_workers if w.isRunning()]

    def cloud_resume_if_running(self):
        """v1.2.0 — called on app startup. If the Oracle server reports
        this bot is already running (i.e. you closed the desktop app
        while a cloud bot kept trading), restore the running UI state
        and resume log-tail polling instead of asking the user to start
        it again."""
        if not self._is_cloud_mode():
            return
        def _on(ok, body):
            if ok and body.get("running", False):
                self._cloud_running = True
                self._set_running(True)
                self._log(
                    f"☁  Already running on Oracle (resumed) · pid "
                    f"{body.get('pid','?')}",
                    C["green"])
                self._start_cloud_polling()
        self._cloud_call("GET", f"/bots/{self.side}/status", _on)

    def _cloud_start(self):
        self._log("☁  Asking Oracle to start bot…", C["muted"])
        def _on(ok, body):
            if ok:
                self._cloud_running = True
                self._log(f"☁  Running on Oracle  ·  pid {body.get('pid','?')}",
                          C["green"])
                self._set_running(True)
                self._start_cloud_polling()
            else:
                detail = body.get("detail") or body.get("text") or "start failed"
                self._log(f"Cloud start failed: {detail}", C["red"])
                self._set_running(False)
        self._cloud_call("POST", f"/bots/{self.side}/start", _on)

    def _cloud_stop(self):
        self._log("☁  Asking Oracle to stop bot…", C["muted"])
        def _on(ok, body):
            self._cloud_running = False
            self._set_running(False)
            self._stop_cloud_polling()
            if ok:
                self._log("☁  Stopped", C["yellow"])
            else:
                detail = body.get("detail") or body.get("text") or "stop failed"
                self._log(f"Cloud stop failed: {detail}", C["red"])
        self._cloud_call("POST", f"/bots/{self.side}/stop", _on)

    def _start_cloud_polling(self):
        """Every 30 s, ask the server for live status + tail the log
        so the user sees real-time output in the bot tab even though
        the bot is on Oracle."""
        if getattr(self, "_cloud_poll_timer", None):
            return
        self._cloud_poll_timer = QTimer(self)
        self._cloud_poll_timer.timeout.connect(self._cloud_poll_tick)
        self._cloud_poll_timer.start(30_000)
        self._cloud_last_log_len = 0
        # Immediate first tick so the log starts populating fast
        QTimer.singleShot(2000, self._cloud_poll_tick)

    def _stop_cloud_polling(self):
        t = getattr(self, "_cloud_poll_timer", None)
        if t:
            t.stop()
        self._cloud_poll_timer = None

    def _cloud_poll_tick(self):
        # Status — keeps the dot/badge in sync with reality
        def _on_status(ok, body):
            if ok and not body.get("running", False):
                # Server says the bot exited on its own (crash, scheduled
                # stop, etc.). Reflect that in the UI.
                if self._cloud_running:
                    self._cloud_running = False
                    self._set_running(False)
                    self._stop_cloud_polling()
                    self._log("☁  Bot exited on Oracle", C["yellow"])
        self._cloud_call("GET", f"/bots/{self.side}/status", _on_status)

        # Log tail — append the part we haven't seen yet
        def _on_logs(ok, body):
            if not ok: return
            text = body.get("log", "") or ""
            prev = getattr(self, "_cloud_last_log_len", 0)
            if len(text) > prev:
                new = text[prev:]
                for line in new.splitlines():
                    if line.strip():
                        self._log(line)
                self._cloud_last_log_len = len(text)
        self._cloud_call("GET", f"/bots/{self.side}/logs?tail=8000", _on_logs)

    def restart_bot(self):
        self.stop_bot()
        QTimer.singleShot(1000, self.start_bot)

    def _on_stdout(self):
        data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        for line in data.strip().split("\n"):
            if line.strip():
                self._log(line)

    def _on_stderr(self):
        data = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in data.strip().split("\n"):
            if line.strip():
                self._log(line, C["red"])

    def _on_finished(self, exit_code, exit_status):
        self._set_running(False)
        self._log(f"Process ended (exit code {exit_code})",
                  C["yellow"] if exit_code == 0 else C["red"])

    def upload_script(self):
        """Replace bot script via file dialog."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Bot Script", "", "Python Files (*.py)")
        if not path:
            return
        import shutil
        from datetime import datetime
        # Backup old
        if self.script_path.exists():
            bk = self.script_path.with_suffix(
                f"_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py")
            shutil.copy2(self.script_path, bk)
        shutil.copy2(path, self.script_path)
        self.upload_lbl.setText(f"✓ Replaced with {Path(path).name}")
        self._log(f"Script replaced: {Path(path).name}", C["green"])


# ─────────────────────────────────────────
# SCROLLABLE CONTENT AREA
# ─────────────────────────────────────────

class ScrollContent(QScrollArea):
    """Wraps content in a scrollable area."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._inner = QWidget()
        self._inner.setStyleSheet("background:transparent;")
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(20, 16, 20, 32)
        self._layout.setSpacing(16)
        self.setWidget(self._inner)

    def add(self, widget: QWidget):
        self._layout.addWidget(widget)

    def add_stretch(self):
        self._layout.addStretch()


# ─────────────────────────────────────────
# WHEEL GUARD  (stop inner widgets stealing scroll)
# ─────────────────────────────────────────

class WheelGuard(QObject):
    """
    Application-wide event filter so the mouse wheel scrolls the PAGE even
    when the cursor is over a chart, table or the bot console.

    - Charts never scroll internally -> wheel always moves the page.
    - Tables / console only scroll themselves once you click into them
      (i.e. they have keyboard focus); otherwise the wheel moves the page.

    Only acts on Wheel events over those widgets; everything else is
    untouched.
    """

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.Wheel:
            return False

        # The wheel target is usually an inner viewport — climb to the
        # widget we care about.
        w = obj
        guarded = None
        focusable = False
        while w is not None:
            if isinstance(w, ChartView):
                guarded, focusable = w, False
                break
            if isinstance(w, (QTextEdit, QAbstractItemView)):
                guarded, focusable = w, True
                break
            w = w.parentWidget() if isinstance(w, QWidget) else None

        if guarded is None:
            return False

        # Clicked into a table/console -> let it scroll itself.
        if focusable and guarded.hasFocus():
            return False

        # Find the enclosing page scroll area and move it instead.
        sa = guarded.parentWidget()
        while sa is not None and not isinstance(sa, QScrollArea):
            sa = sa.parentWidget()
        if sa is None:
            return False

        d = ev.angleDelta().y() or ev.pixelDelta().y()
        if d:
            bar = sa.verticalScrollBar()
            bar.setValue(bar.value() - d)
        return True


# ─────────────────────────────────────────
# CLOSED TRADES FEED
# ─────────────────────────────────────────

class ClosedTradesFeed(QWidget):
    """
    Compact scrollable list of closed sell trades.
    Shows: SOLD AAPL ×12  @$195.40  +$234.50 (+1.8%)  · 3h ago
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFixedHeight(180)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._container.setStyleSheet("background:transparent;")
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(3)
        self._vbox.addStretch()
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll)

        self._empty = QLabel("No closed trades yet")
        self._empty.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:8px 0;")
        self._vbox.insertWidget(0, self._empty)

    def load(self, trades: list):
        """trades = list of dicts: ticker, qty, avg_sell, pl, pl_pct, closed_at"""
        while self._vbox.count() > 0:
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not trades:
            empty = QLabel("No closed trades yet")
            empty.setStyleSheet(
                f"color:{C['muted']};font-size:11px;padding:8px 0;")
            self._vbox.addWidget(empty)
            self._vbox.addStretch()
            return

        for t in trades:
            row = self._make_row(t)
            self._vbox.addWidget(row)
        self._vbox.addStretch()

    def _make_row(self, t: dict) -> QWidget:
        ticker  = t.get("ticker", "")
        qty     = t.get("qty", 0)
        price   = t.get("avg_sell", 0)
        pl      = t.get("pl", 0)
        pl_pct  = t.get("pl_pct", 0)
        when    = t.get("when", "")
        pl_c    = C["green"] if pl >= 0 else C["red"]
        sign    = "+" if pl >= 0 else ""

        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:6px;")
        row = QHBoxLayout(frame)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(8)

        action = QLabel("SOLD")
        action.setStyleSheet(
            f"color:{C['red']};font-size:9px;font-weight:700;"
            f"letter-spacing:2px;min-width:32px;")

        sym = QLabel(ticker)
        sym.setStyleSheet(
            f"color:{C['text']};font-size:11px;font-weight:600;"
            f"min-width:54px;")

        qty_lbl = QLabel(f"×{abs(qty):.0f}" if qty == int(qty)
                         else f"×{abs(qty):.4g}")
        qty_lbl.setStyleSheet(f"color:{C['muted']};font-size:10px;")

        price_lbl = QLabel(f"@ ${price:,.2f}")
        price_lbl.setStyleSheet(f"color:{C['muted']};font-size:10px;")

        pl_lbl = QLabel(f"{sign}${abs(pl):,.2f}  ({sign}{pl_pct:.1f}%)")
        pl_lbl.setStyleSheet(
            f"color:{pl_c};font-size:11px;font-weight:600;")

        when_lbl = QLabel(when)
        when_lbl.setStyleSheet(f"color:{C['muted']};font-size:9px;")

        row.addWidget(action)
        row.addWidget(sym)
        row.addWidget(qty_lbl)
        row.addWidget(price_lbl)
        row.addWidget(pl_lbl)
        row.addStretch()
        row.addWidget(when_lbl)
        return frame


# ─────────────────────────────────────────
# DATA TABLE
# ─────────────────────────────────────────

class DataTable(QTableWidget):
    """Pre-styled table widget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.verticalHeader().setVisible(False)
        self.setSortingEnabled(True)
        self.setShowGrid(True)

    def load(self, rows: list, columns: list,
             color_rules: dict = None):
        """
        Load data. color_rules = {column_name: callable(value) -> color_str}
        """
        self.setRowCount(len(rows))
        self.setColumnCount(len(columns))
        self.setHorizontalHeaderLabels(columns)

        for r, row in enumerate(rows):
            for c, col in enumerate(columns):
                val = row.get(col, "")
                item = QTableWidgetItem(str(val))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

                if color_rules and col in color_rules:
                    try:
                        color = color_rules[col](val)
                        if color:
                            item.setForeground(QColor(color))
                    except Exception:
                        pass

                self.setItem(r, c, item)

        self.resizeColumnsToContents()

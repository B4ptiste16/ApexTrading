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
# NO-SCROLL COMBO BOX
# ─────────────────────────────────────────

class NoScrollComboBox(QComboBox):
    """QComboBox that ignores scroll-wheel events.

    Prevents accidentally changing a setting while the user is simply
    scrolling the page past a dropdown — the event is passed up to the
    scroll area instead.
    """
    def wheelEvent(self, event):
        event.ignore()   # let the parent scroll area handle it


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

        # V4.6.71 — minimalist: flat card, no coloured top-accent line.
        self.setStyleSheet(
            f"QFrame#card {{"
            f"  background:{C['panel']};"
            f"  border:none;"
            f"  border-radius:8px;"
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
            f"  border:none;"
            f"  border-radius:8px;"
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

        # V4.6.14 — removed the horizontal rule under the section
        # header. Site-wide minimalism pass: separation comes from
        # background tint + letter-spacing on the label, no rule.


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

        # V4.0.0 — bigger always-visible play/stop/restart buttons.
        # Roughly double their previous size + bumped font-weight so
        # they're impossible to miss without hovering.
        self.run_btn = QPushButton("▶  RUN BOT")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.setFixedHeight(54)
        self.run_btn.setMinimumWidth(140)
        self.run_btn.clicked.connect(self.start_bot)

        self.stop_btn = QPushButton("■  STOP")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setFixedHeight(54)
        self.stop_btn.setMinimumWidth(120)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_bot)

        self.restart_btn = QPushButton("↺  RESTART")
        self.restart_btn.setFixedHeight(54)
        self.restart_btn.setMinimumWidth(140)
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
        # V4.6.90 — cap the log to the last 800 lines. Previously it grew
        # unbounded; after a few hours of streaming a cloud bot's output the
        # QTextEdit held tens of thousands of lines and made the whole app
        # lag/freeze. setMaximumBlockCount auto-drops the oldest lines.
        self.log.document().setMaximumBlockCount(800)
        self.log.setPlaceholderText("Bot output will appear here...")
        # V4.6.5 — pin alignment to left + disable line wrapping so a
        # long ASCII header (e.g. daybot's `=` * 65 banner) does NOT
        # make the rest of the lines visually center themselves. Also
        # set the document default alignment so HTML insertions inherit it.
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtGui  import QTextOption
        self.log.setAlignment(_Qt.AlignmentFlag.AlignLeft)
        self.log.document().setDefaultTextOption(
            QTextOption(_Qt.AlignmentFlag.AlignLeft))
        self.log.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.log.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:5px;"
            f"font-size:10px;font-family:'JetBrains Mono';padding:6px;"
            f"text-align:left;"
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
        # V4.6.7 — guard against the QTextEdit being destroyed before
        # an async cloud callback fires (same class of crash as
        # _set_running). Bail silently if the underlying widget is gone.
        try:
            self.log.append(html)
            self.log.moveCursor(QTextCursor.MoveOperation.End)
        except RuntimeError:
            return

    def _set_running(self, running: bool):
        # V4.6.7 — guard every child-widget access against
        # "wrapped C/C++ object has been deleted" crashes. A
        # cloud-mode callback can fire after the user has navigated
        # away and the BotProcessWidget was destroyed; touching a
        # deleted button raises RuntimeError. We just bail silently.
        try:
            self.run_btn.setEnabled(not running)
            self.stop_btn.setEnabled(running)
            self.restart_btn.setEnabled(running)
        except RuntimeError:
            return
        cloud = self._is_cloud_mode()
        dot_color = (C["purple"] if running and cloud
                     else C["green"] if running
                     else C["muted"])
        try:
            self.status_dot.setStyleSheet(f"color:{dot_color};font-size:14px;")
        except RuntimeError:
            return
        if running:
            label = "RUNNING ☁ ORACLE" if cloud else "RUNNING"
        else:
            label = "STOPPED  (cloud)" if cloud else "STOPPED"
        try:
            self.status_lbl.setText(label)
        except RuntimeError:
            return
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

    def _broker_mode(self) -> str:
        """Current global broker ('alpaca' or 'ibkr'). Never raises."""
        try:
            from core import data as _D
            return str(_D.load_settings().get("broker_mode", "alpaca")).lower()
        except Exception:
            return "alpaca"

    def _ibkr_cloud_enabled(self) -> bool:
        """V4.6.40 — True when the user enabled 'Run IBKR bots on Oracle' for
        the current paper/live mode AND a paper login is stored. APEX runs a
        per-user IB Gateway on the server, so IBKR bots CAN run on the cloud."""
        try:
            from core import data as _D
            s    = _D.load_settings()
            mode = s.get("alpaca_mode", "paper")
            cur  = s.get(f"ibkr_{mode}", {}) or {}
            return (bool(cur.get("run_on_oracle", False))
                    and bool(str(cur.get("cloud_username", "")).strip())
                    and bool(str(cur.get("cloud_password", ""))))
        except Exception:
            return False

    def _is_cloud_mode(self) -> bool:
        """True if the user has flagged this bot for cloud execution.

        V4.6.39 — IBKR bots used to NEVER run on Oracle (the cloud runner was
        Alpaca-only and had no route to the user's local TWS/Gateway).

        V4.6.40 — IBKR bots CAN run on Oracle when the user enabled 'Run IBKR
        bots on Oracle' (Tools → IBKR): APEX launches a per-user IB Gateway on
        the server logged into their paper account, and the cloud runner sets
        APEX_BROKER=ibkr. Without that toggle, IBKR bots stay LOCAL so a bot
        the user believes is on IBKR can't silently trade Alpaca on the cloud."""
        try:
            from core import data as _D
            if self._broker_mode() == "ibkr":
                # V4.6.82 — IBKR runs on the APEX cloud (a per-user server-side
                # gateway). When the user has enabled 'Run IBKR bots on Oracle'
                # + saved their login, EVERY IBKR bot routes to the cloud — no
                # local TWS/Gateway needed. (Previously this also required a
                # per-bot cloud flag, so a freshly-added IBKR bot fell through to
                # the local path and failed with 'IB Gateway not reachable'.)
                return self._ibkr_cloud_enabled()
            return _D.is_cloud_bot(self.side)
        except Exception:
            return False

    def _has_alpaca_key_for_side(self) -> bool:
        """V4.0.2 — return True iff an Alpaca API+secret pair is currently
        wired to this bot's side via the Tools tab slot dropdowns. Looks
        up ALPACA_API_KEY_{SIDE} + ALPACA_SECRET_KEY_{SIDE} in .env."""
        try:
            from core import data as _D
            env = _D.read_env_keys()
            side_up = (self.side or "").upper()
            return bool(env.get(f"ALPACA_API_KEY_{side_up}", "").strip()
                        and env.get(f"ALPACA_SECRET_KEY_{side_up}", "").strip())
        except Exception:
            return True  # don't false-positive — let the underlying error surface

    def start_bot(self):
        # V4.6.47 — CLOUD bots run on the Oracle server and connect to the
        # SERVER-side gateway, so they must NOT be gated by any local-machine
        # readiness check (local TWS socket probe, local Alpaca-key check,
        # bundled-package check — all of which inspect THIS computer). Route
        # straight to the cloud path so the bot starts with TWS closed / the
        # laptop off. The server validates its own creds + gateway and returns
        # a clear error if something's missing.
        if self._is_cloud_mode():
            return self._cloud_start()

        # V4.0.2 — pre-flight: refuse to start without an Alpaca key
        # assigned to this bot. Surfaces 'MUST ASSIGN API KEY IN TOOLS'
        # instead of letting the bot die opaquely on its first Alpaca
        # call. Applies BEFORE the cloud-mode branch so the warning is
        # the same regardless of where the bot will run.
        #
        # V4.6.6 — skip the Alpaca-key precheck for non-trading scripts
        # (UNIVERSE manager + asset_type='universe' custom bots). These
        # scripts only read market data + rewrite a *_universe.txt
        # file — they NEVER submit orders, so Alpaca credentials are
        # genuinely not needed. Previously hitting RUN on the Universe
        # Manager surfaced a misleading "MUST ASSIGN API KEY" dialog.
        # V4.6.34 — broker-aware pre-flight.
        # • Alpaca: same Alpaca-key check as before (unchanged).
        # • IBKR built-ins (LONG/SHORT/DAY): still BLOCKED — those bots call
        #   alpaca-py directly; refactoring them to the broker abstraction is
        #   the next release.  Starting them in IBKR mode would silently
        #   trade the Alpaca account.
        # • IBKR custom/framework bots: ALLOWED if (a) the bot has a Client
        #   ID in Tools → IBKR and (b) the gateway port is reachable.
        try:
            from core import data as _D
            _settings = _D.load_settings()
            _broker = _settings.get("broker_mode", "alpaca")
        except Exception:
            _settings = {}
            _broker = "alpaca"

        if _broker == "ibkr" and not self._is_non_trading_script():
            from PyQt6.QtWidgets import QMessageBox as _QMB
            # V4.6.42 — LONG, SHORT and DAY are ALL broker-aware now. DAY's
            # bracket orders (entry + take-profit + stop-loss) are translated
            # by the IBKR shim into native OCA bracket legs that rest on IBKR's
            # servers, so its protective legs survive even with the PC off.
            # Nothing is blocked on IBKR anymore.
            # Custom/framework bot — verify IBKR readiness before launch.
            _amode = _settings.get("alpaca_mode", "paper")
            _ibkr_cfg = _settings.get(f"ibkr_{_amode}", _settings.get("ibkr", {})) or {}
            _has_cid = any(
                isinstance(b, dict)
                and str(b.get("id", "")).upper() == str(self.side).upper()
                for b in (_ibkr_cfg.get("bots") or [])
            )
            if not _has_cid:
                _QMB.warning(
                    self.window(),
                    "Bot not in IBKR tools",
                    f"<b>{self.side}</b> isn't in the IBKR bot table. "
                    f"Open <b>Tools → IBKR</b>, click <b>+ Add bot</b> and "
                    f"give it a Client ID + allocation, then save.")
                return
            _host = str(_ibkr_cfg.get("host", "127.0.0.1"))
            try:
                _port = int(str(_ibkr_cfg.get("port",
                    "7497" if _amode == "paper" else "7496")))
            except ValueError:
                _port = 7497
            import socket as _sock
            try:
                _s_sock = _sock.create_connection((_host, _port), timeout=3)
                _s_sock.close()
            except Exception as _e:
                _QMB.warning(
                    self.window(),
                    "Run this bot on the cloud",
                    f"This IBKR bot isn't set to run on the APEX cloud, and no "
                    f"local IB Gateway is reachable at {_host}:{_port}.<br><br>"
                    f"<b>Recommended:</b> open <b>Tools → IBKR</b>, tick "
                    f"<b>“Run IBKR bots on Oracle”</b> and make sure your IBKR "
                    f"login is saved — then APEX runs it on the server (no local "
                    f"gateway needed).<br><br>"
                    f"(Only start a local IB Gateway / TWS if you specifically "
                    f"want to run on this computer.)")
                return

        # V4.6.35 — verify META.requirements are importable in the frozen
        # build BEFORE launching, so a bot whose model picked an unbundled
        # package (e.g. scikit-learn) gets a helpful message rather than
        # exit-1 with "No module named X" five lines into its log.
        try:
            _missing = self._missing_requirements()
        except Exception:
            _missing = []
        if _missing:
            from PyQt6.QtWidgets import QMessageBox as _QMB
            _lines = "<br>".join(
                f"&nbsp;&nbsp;• <b>{pip}</b>" for pip, _imp in _missing)
            _QMB.warning(
                self.window(),
                "Bot needs packages not in this build",
                f"<b>{self.side}</b> declares pip requirements that aren't "
                f"bundled into APEX:<br><br>{_lines}<br><br>"
                f"Two ways forward:<br>"
                f"&nbsp;1. <b>Run on Oracle</b> — the cloud server auto-installs "
                f"requirements (Tools → Automation → Run on Oracle).<br>"
                f"&nbsp;2. Add the package(s) to <code>build.bat</code> "
                f"(<code>--collect-all &lt;name&gt;</code>) and rebuild APEX.")
            return

        if _broker == "alpaca" and not self._is_non_trading_script() \
                and not self._has_alpaca_key_for_side():
            from PyQt6.QtWidgets import QMessageBox as _QMB
            _QMB.warning(
                self.window(),
                "MUST ASSIGN API KEY IN TOOLS",
                f"No Alpaca API key is assigned to the <b>{self.side}</b> "
                f"bot.<br><br>Open <b>Tools → ALPACA · API KEYS</b>, paste "
                f"a key + secret into one of the slots, set its 'Assigned' "
                f"dropdown to <b>{self.side}</b>, click <b>Save slots</b>, "
                f"and try again.")
            return

        # V4.6.11 — CRITICAL FIX: the launch logic below used to live
        # at module level after _is_non_trading_script's body and was
        # therefore DEAD CODE since v4.6.6. That broke RUN BOT for the
        # Universe tab and any local (non-cloud) bot. Now it's back
        # inside start_bot where it belongs.
        # V7.1.13: route to the cloud path when this bot is in
        # cloud_bots. Local QProcess code only runs for laptop bots.
        if self._is_cloud_mode():
            return self._cloud_start()
        frozen = getattr(sys, "frozen", False)

        # V3.1.5 — if the user has locked their bot library, the file
        # on disk is .apex (encrypted). Find whatever's actually there,
        # decrypt to a short-lived temp .py, then run THAT.
        run_path = self._broker_script_path()
        self._tmp_script: Optional[Path] = None
        if not frozen:
            if not run_path.exists() and self.script_path.suffix in (".py", ".apex"):
                alt = self.script_path.with_suffix(
                    ".apex" if self.script_path.suffix == ".py" else ".py")
                if alt.exists():
                    run_path = alt
            if run_path.suffix == ".apex":
                try:
                    from core import secure
                    self._tmp_script = secure.decrypted_temp_file(run_path)
                    run_path = self._tmp_script
                    self._log(f"🔓  Decrypted {self.script_path.name} → "
                              f"{run_path.name}", C["muted"])
                except Exception as e:
                    self._log(f"Could not decrypt {self.script_path.name}: {e}",
                              C["red"])
                    return
            if not run_path.exists():
                self._log(f"Script not found: {run_path}", C["red"])
                return
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        # Pass DATA_DIR explicitly so bot scripts' load_dotenv() always
        # finds the right .env regardless of working directory.
        env.insert("APEX_DATA_DIR", str(DATA_DIR))
        # V4.6.38 — the bot's registry slug. Bots key their per-broker trade
        # log AND (on IBKR) their sub-portfolio ledger off this, so it must be
        # set for local launches too (the cloud runner already sets it).
        env.insert("APEX_BOT_SIDE", str(self.side).upper())
        # V4.6.5 — per-bot universe override.
        # V4.6.13 — resolve the chosen UNIVERSE BOT (script_id) to its
        # target .txt file at launch time. The bot reads that file as
        # its universe. Falls back to the legacy 'bot_universe_<SIDE>'
        # key (direct file path) for back-compat with older settings.
        try:
            import core.data as _D
            _s = _D.load_settings()
            _u = ""
            script_id = _s.get(
                f"bot_universe_script_{self.side.upper()}", "")
            if script_id:
                # 'built-in' = use this bot's default universe file
                if script_id == "built-in":
                    _u = {
                        "LONG":  "longbot_universe.txt",
                        "SHORT": "shortbot_universe.txt",
                        "DAY":   "daybot_universe.txt",
                    }.get(self.side.upper(), "")
                else:
                    # Look up the universe bot's target from its
                    # registry entry
                    for entry in _s.get("universe_scripts", []) or []:
                        if str(entry.get("id", "")) == script_id:
                            _u = entry.get("target",
                                           f"{script_id}_universe.txt")
                            break
            # Legacy fallback: direct file path stored under the old key
            if not _u:
                _u = _s.get(
                    f"bot_universe_{self.side.upper()}", "")
            if _u:
                env.insert("APEX_BOT_UNIVERSE", str(_u))
            # V4.6.8 — Alpaca paper/live mode propagation.
            _amode = _s.get("alpaca_mode", "paper")
            env.insert("APEX_ALPACA_MODE", str(_amode))
            # V4.6.32 — broker propagation so the bot writes its trade log /
            # state into the per-broker data folder (separate P/L per broker).
            _broker_now = str(_s.get("broker_mode", "alpaca"))
            env.insert("APEX_BROKER", _broker_now)
            # V4.6.34 — when running on IBKR, hand the bot subprocess the
            # gateway address + its own client ID so core.broker_client can
            # connect.  Reads from the per-mode IBKR config the user set up
            # in Tools → IBKR.
            if _broker_now == "ibkr":
                _ibkr_cfg = dict(_s.get(f"ibkr_{_amode}") or {})
                # V4.6.58 — make flipping the Paper→Live switch "just work":
                # if the LIVE config hasn't been set up separately, inherit the
                # bot client-IDs / allocations (and host) configured under paper.
                # Only the PORT differs (live TWS = 7496), so we never inherit it.
                if _amode == "live" and not _ibkr_cfg.get("bots"):
                    _paper = _s.get("ibkr_paper", _s.get("ibkr", {})) or {}
                    for _k in ("bots", "host"):
                        if _k not in _ibkr_cfg and _k in _paper:
                            _ibkr_cfg[_k] = _paper[_k]
                env.insert("APEX_IBKR_HOST", str(_ibkr_cfg.get("host", "127.0.0.1")))
                env.insert("APEX_IBKR_PORT", str(_ibkr_cfg.get("port",
                    "7497" if _amode == "paper" else "7496")))
                _cid_for_side = "1"
                for _b in (_ibkr_cfg.get("bots") or []):
                    if isinstance(_b, dict) and str(_b.get("id", "")).upper() == str(self.side).upper():
                        _cid_for_side = str(_b.get("client_id", "1"))
                        break
                env.insert("APEX_IBKR_CLIENT_ID", _cid_for_side)
        except Exception:
            pass
        self.process.setProcessEnvironment(env)

        if frozen:
            # Installed build: sys.executable is APEX.exe, not python, and
            # the bot .py files live inside the exe. Launch the dedicated
            # --run-bot entry point with data living in DATA_DIR.
            # V4.6.15 — when side=UNIVERSE and the user picked a custom
            # script via the Universe tab dropdown, pass that script
            # path as a 4th arg so _run_bot can exec it instead of
            # the built-in universe_manager.
            # V4.6.25 — also pass script_path for ANY custom (non-built-in)
            # bot side so _run_bot knows exactly which file to exec.
            self.process.setWorkingDirectory(str(DATA_DIR))
            args = ["--run-bot", self.side]
            builtin = str(self.side).upper() in ("LONG", "SHORT", "DAY")
            if not builtin:
                try:
                    custom_path = str(self._broker_script_path())
                    if custom_path and "universe_manager" not in custom_path.lower():
                        args.append(custom_path)
                except Exception:
                    pass
            self.process.start(sys.executable, args)
        else:
            self.process.start(sys.executable, ["-u", str(run_path)])
        if self.process.waitForStarted(3000):
            self._set_running(True)
            self._log(f"Started {self.script_path.name}", C["green"])
            self._log("Loading libraries… the bot stays silent while it "
                      "imports (longbot ~1 min, daybot up to ~2 min). "
                      "This is normal — wait for its header.", C["muted"])
        else:
            self._log("Failed to start process", C["red"])

    def _broker_script_path(self):
        """V4.6.38 — IBKR custom bots run from a SEPARATE file
        (bots/<broker>/<name>) so the copy trading on IBKR can diverge from the
        Alpaca original (different sub-portfolio, possibly hand-edited later).
        The file is created by copying the Alpaca source the first time it's
        needed. Built-ins, the universe manager, encrypted (.apex) libraries
        and the Alpaca broker all fall through to the original path unchanged."""
        sp = self.script_path
        try:
            if str(self.side).upper() in ("LONG", "SHORT", "DAY", "UNIVERSE"):
                return sp
            import core.data as _D
            broker = str(_D.load_settings().get("broker_mode", "alpaca")).lower()
            if broker == "alpaca" or Path(sp).suffix != ".py":
                return sp
            dest_dir = DATA_DIR / "bots" / broker
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / Path(sp).name
            if not dest.exists() and Path(sp).exists():
                import shutil
                shutil.copy2(sp, dest)
                self._log(f"Created {broker} copy → bots/{broker}/{Path(sp).name} "
                          f"(edits here won't affect the Alpaca version)",
                          C["muted"])
            return dest if dest.exists() else sp
        except Exception as e:
            print(f"[bot] broker script resolve failed: {e}")
            return sp

    # Map pip-install names → importable module names for requirements
    # whose names diverge.  Add entries when AI-generated bots trip on a
    # new one — most pip names match the import name directly.
    _PIP_TO_IMPORT = {
        "scikit-learn":   "sklearn",
        "python-dotenv":  "dotenv",
        "pyyaml":         "yaml",
        "beautifulsoup4": "bs4",
        "pillow":         "PIL",
        "msgpack-python": "msgpack",
        "google-generativeai": "google.generativeai",
        "ib-async":       "ib_async",
        "ib_insync":      "ib_insync",
    }

    def _missing_requirements(self) -> list:
        """V4.6.35 — read the bot's META.requirements and return any pip
        packages that aren't importable from the frozen Python.  Custom
        bots declare extras (e.g. scikit-learn) which only get installed
        for cloud runs; locally they crash at import.  Surfacing this in
        pre-flight gives a helpful message instead of an opaque ImportError."""
        try:
            from pathlib import Path as _P
            from core.bot_meta import parse_meta
            import importlib.util as _imp
            script_path = getattr(self, "script_path", None) \
                          or getattr(self, "script", None)
            if not script_path:
                return []
            sp = _P(str(script_path))
            if not sp.exists():
                return []
            src = sp.read_text(encoding="utf-8", errors="replace")
            meta = parse_meta(src) or {}
            reqs = meta.get("requirements") or []
            if isinstance(reqs, str):
                reqs = [r.strip() for r in reqs.split(",")]
            missing = []
            for raw in reqs:
                name = str(raw).strip().lower()
                if not name:
                    continue
                # strip version pin
                name = name.split("==")[0].split(">=")[0].split("<")[0].strip()
                import_name = self._PIP_TO_IMPORT.get(name, name.replace("-", "_"))
                try:
                    if _imp.find_spec(import_name) is None:
                        missing.append((name, import_name))
                except Exception:
                    missing.append((name, import_name))
            return missing
        except Exception:
            return []

    def _is_non_trading_script(self) -> bool:
        """Universe-manager + universe-generator custom bots don't need
        Alpaca credentials — they only read market data and rewrite a
        *_universe.txt. Return True if this BotProcessWidget is wrapping
        one of those scripts. Used by start_bot's precheck to skip the
        'MUST ASSIGN API KEY' warning for non-trading scripts."""
        # Built-in: the Universe Manager tab uses side='UNIVERSE'
        if str(self.side).upper() in ("UNIVERSE", "UNIVERSE_MANAGER"):
            return True
        # Custom bot — peek at its META block for asset_type='universe'
        try:
            from pathlib import Path as _P
            from core.bot_meta import parse_meta
            script_path = getattr(self, "script_path", None) \
                          or getattr(self, "script", None)
            if script_path and _P(str(script_path)).exists():
                src = open(str(script_path), "r",
                           encoding="utf-8").read()
                meta = parse_meta(src) or {}
                if meta.get("asset_type", "").lower() == "universe":
                    return True
                name_low = (meta.get("name") or "").lower()
                if "universe" in name_low and \
                        "alpaca.trading" not in src:
                    return True
        except Exception:
            pass
        return False

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
                    on_done, payload: dict | None = None,
                    timeout: int = 15):
        """Spawn a QThread that calls the APEX server with the
        signed-in user's bearer token. on_done(ok: bool, data: dict)
        runs on the main thread when finished. `timeout` is the read
        timeout (longer for bot starts, which boot the IBKR gateway)."""
        from PyQt6.QtCore import QThread, pyqtSignal as _Sig
        from ui.login import load_auth, load_server_url
        token = (load_auth() or {}).get("token")
        if not token:
            self._log("Cloud mode needs a signed-in APEX account. "
                      "Open APEX and sign in.", C["red"])
            on_done(False, {"detail": "not signed in"})
            return
        url = f"{load_server_url()}{path}"
        # V4.6.41 — tag bot lifecycle calls with the active broker so the
        # SAME side can run on Alpaca and IBKR at the same time on the cloud
        # (the server keys each instance by user+side+broker). The desktop's
        # current broker mode is the broker for this start/stop/status/logs.
        if "/bots/" in path:
            sep = "&" if "?" in path else "?"
            url = f"{url}{sep}broker={self._broker_mode()}"

        class _W(QThread):
            done = _Sig(bool, dict)
            def __init__(self, m, u, t, p, to):
                super().__init__()
                self.m, self.u, self.t, self.p, self.to = m, u, t, p, to
            def run(self):
                import requests
                try:
                    fn = {"GET": requests.get, "POST": requests.post,
                          "PUT": requests.put}.get(self.m, requests.get)
                    r = fn(self.u, headers={"Authorization": f"Bearer {self.t}"},
                           json=self.p, timeout=self.to)
                    try:
                        body = r.json()
                    except Exception:
                        body = {"text": r.text}
                    self.done.emit(r.ok, body)
                except Exception as e:
                    self.done.emit(False, {"detail": str(e)})

        worker = _W(method, url, token, payload, timeout)
        worker.done.connect(on_done)
        worker.start()
        # Keep a reference so the worker isn't garbage-collected
        self._http_workers = getattr(self, "_http_workers", [])
        self._http_workers.append(worker)
        # Trim finished workers
        self._http_workers = [w for w in self._http_workers if w.isRunning()]

    def cloud_resume_if_running(self):
        """v1.2.1 — called on app startup. Query Oracle for live status
        regardless of the local 'cloud mode' toggle, because the bot
        might have been started on the cloud in a previous session and
        the local flag has since been reset. If the server reports
        running:true, restore the UI and resume log-tail polling.

        V4.6.41 — cloud instances are keyed per broker on the server, and
        _cloud_call tags this query with the desktop's active broker. So we
        simply attach to THIS broker's instance if it's running. A bot the
        user is also running on the OTHER broker is independent and left
        alone — that's the whole point of dual-broker support. (This replaces
        the v4.6.39/40 'stop the stray Alpaca cloud bot' hack, which is no
        longer needed now that Alpaca and IBKR cloud bots coexist.)"""
        def _on(ok, body):
            if not ok:
                return
            if not body.get("running", False):
                return
            self._cloud_running = True
            self._set_running(True)
            brk = str(body.get("broker", self._broker_mode())).upper()
            self._log(
                f"☁  Already running on Oracle [{brk}] (resumed) · pid "
                f"{body.get('pid','?')}",
                C["green"])
            self._start_cloud_polling()
        self._cloud_call("GET", f"/bots/{self.side}/status", _on)

    def _cloud_start(self):
        # V4.0.2 — if this is a CUSTOM bot (not LONG/SHORT/DAY) we have
        # to push the local .py to Oracle's private-bots dir before
        # asking the server to start it, otherwise bot_runner can't
        # find the script.
        if self.side.upper() not in ("LONG", "SHORT", "DAY"):
            self._cloud_upload_then_start()
            return
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
        # V4.6.82 — IBKR cold-starts boot the server-side gateway (login + API
        # init takes up to ~90s), so give the start call a long read timeout
        # instead of the default 15s (which surfaced as 'Read timed out').
        self._log("☁  (IBKR cold start can take up to ~90s while the gateway "
                  "logs in)", C["muted"])
        self._cloud_call("POST", f"/bots/{self.side}/start", _on, timeout=150)

    def _cloud_upload_then_start(self):
        """Custom-bot cloud start: upload the local .py (decrypt if
        the library is locked), then trigger /bots/{side}/start."""
        from pathlib import Path as _P
        if not self.script_path or not _P(self.script_path).exists():
            # If the script_path is .py but library is locked, look for .apex
            alt = None
            if self.script_path:
                alt = _P(self.script_path).with_suffix(".apex")
                if alt.exists():
                    try:
                        from core import secure
                        blob = secure.decrypt_file(alt)
                    except Exception as e:
                        self._log(f"Could not decrypt {alt.name}: {e}", C["red"])
                        return
                else:
                    self._log(f"Local script missing: {self.script_path}", C["red"])
                    return
            else:
                self._log("Custom bot has no script path.", C["red"])
                return
        else:
            blob = _P(self.script_path).read_bytes()

        from PyQt6.QtCore import QThread as _QT, pyqtSignal as _Sig
        from ui.login import load_auth, load_server_url

        slug = self.side.lower()

        # V4.6.19 — resolve the universe file the user assigned to
        # this bot (via the bot-tab dropdown) so we can ship its
        # current content to Oracle BEFORE the bot starts. Without
        # this hop the cloud bot reads stale or default tickers.
        from pathlib import Path as _P
        universe_blob = None
        universe_fname = ""
        try:
            import core.data as _D
            _s = _D.load_settings()
            script_id = _s.get(
                f"bot_universe_script_{self.side.upper()}", "")
            if script_id:
                if script_id == "built-in":
                    universe_fname = {
                        "LONG":  "longbot_universe.txt",
                        "SHORT": "shortbot_universe.txt",
                        "DAY":   "daybot_universe.txt",
                    }.get(self.side.upper(), "")
                else:
                    for entry in _s.get("universe_scripts", []) or []:
                        if str(entry.get("id", "")) == script_id:
                            universe_fname = entry.get("target", "")
                            break
            if universe_fname:
                lp = _P(str(DATA_DIR)) / universe_fname
                if lp.exists():
                    universe_blob = lp.read_bytes()
                    self._log(
                        f"☁  Uploading universe ({universe_fname}, "
                        f"{len(universe_blob)} bytes) to Oracle…",
                        C["muted"])
        except Exception as _ue:
            print(f"[cloud-start] universe lookup failed: {_ue}")

        class _UploadStart(_QT):
            done = _Sig(bool, str)
            def __init__(self_, universe_fname=universe_fname,
                         universe_blob=universe_blob):
                super().__init__()
                self_._u_fname = universe_fname
                self_._u_blob  = universe_blob
            def run(self_):
                import requests
                tok = (load_auth() or {}).get("token") or ""
                base = load_server_url()
                hdr  = {"Authorization": f"Bearer {tok}"}
                try:
                    # 0) Upload the universe file the user assigned
                    if self_._u_fname and self_._u_blob is not None:
                        try:
                            uu = requests.post(
                                f"{base}/bots/private/upload_universe",
                                headers=hdr,
                                data={"filename": self_._u_fname},
                                files={"file": (self_._u_fname,
                                                self_._u_blob,
                                                "text/plain")},
                                timeout=20)
                            if not uu.ok:
                                print(f"[cloud-start] universe upload "
                                      f"failed ({uu.status_code}): "
                                      f"{uu.text[:200]}")
                        except Exception as _e:
                            print(f"[cloud-start] universe upload "
                                  f"exception: {_e}")
                    # 1) Upload the bot script
                    up = requests.post(
                        f"{base}/bots/private/upload",
                        headers=hdr,
                        data={"slug": slug},
                        files={"file": ("bot.py", blob, "text/x-python")},
                        timeout=20)
                    if not up.ok:
                        self_.done.emit(False,
                            up.json().get("detail", up.text) if up.headers.get(
                                "content-type","").startswith("application/json")
                            else up.text)
                        return
                    # 2) Start. V4.6.82 — long read timeout: an IBKR cold
                    # start boots the server-side gateway (login + API init can
                    # take up to ~90s). 15s surfaced as 'Read timed out'.
                    st = requests.post(
                        f"{base}/bots/{slug.upper()}/start",
                        headers=hdr, timeout=150)
                    if not st.ok:
                        self_.done.emit(False,
                            st.json().get("detail", st.text) if st.headers.get(
                                "content-type","").startswith("application/json")
                            else st.text)
                        return
                    body = st.json()
                    self_.done.emit(True, str(body.get("pid", "?")))
                except Exception as e:
                    self_.done.emit(False, str(e))

        self._log("☁  Uploading custom bot to Oracle…", C["muted"])
        self._upload_worker = _UploadStart()
        def _on_done(ok, msg):
            if ok:
                self._cloud_running = True
                self._log(f"☁  Running on Oracle  ·  pid {msg}", C["green"])
                self._set_running(True)
                self._start_cloud_polling()
            else:
                self._log(f"Cloud start failed: {msg}", C["red"])
                self._set_running(False)
        self._upload_worker.done.connect(_on_done)
        self._upload_worker.start()

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
        self._cloud_poll_timer.start(60_000)   # V4.6.90 — 60s (was 30s)
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

        # Log tail — append the part we haven't seen yet. V4.6.90 — fetch a
        # SMALL tail (was 8000 lines every 30s, which flooded the UI thread and
        # the diff drifted as the 8000-line window slid). 250 lines is plenty
        # for live streaming; the full log is only pulled when the tab opens.
        def _on_logs(ok, body):
            if not ok: return
            text = body.get("log", "") or ""
            prev = getattr(self, "_cloud_last_log_text", "")
            if text and text != prev:
                # Append only the suffix that's genuinely new (robust to the
                # sliding tail window — compare on text, not a stale offset).
                if prev and text.startswith(prev):
                    new = text[len(prev):]
                elif prev and prev in text:
                    new = text[text.rindex(prev) + len(prev):]
                else:
                    new = text
                for line in new.splitlines():
                    if line.strip():
                        self._log(line)
                self._cloud_last_log_text = text
        self._cloud_call("GET", f"/bots/{self.side}/logs?tail=250", _on_logs)

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
        # V3.1.5 — delete the short-lived decrypted .py if any
        tmp = getattr(self, "_tmp_script", None)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            self._tmp_script = None
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
        # V4.6.64 — also guard combo boxes + spin boxes: when the cursor was over
        # one of these in a long form (e.g. Tools → IBKR client-ID rows) the
        # wheel got trapped and the page wouldn't scroll past it.
        from PyQt6.QtWidgets import QComboBox, QAbstractSpinBox
        w = obj
        guarded = None
        focusable = False
        while w is not None:
            if isinstance(w, ChartView):
                guarded, focusable = w, False
                break
            if isinstance(w, (QTextEdit, QAbstractItemView,
                              QComboBox, QAbstractSpinBox)):
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
            f"background:{C['panel']};border:none;"
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

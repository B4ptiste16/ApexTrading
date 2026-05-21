"""
APEX Login / Sign-Up Window — V7+
Shown before the main app when no valid auth token is stored.
"""

import json
from pathlib import Path

import requests
from PyQt6.QtCore import (
    Qt, QThread, QTimer, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import (
    QColor, QCursor, QLinearGradient, QPainter,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from core.paths import DATA_DIR
from ui.styles  import COLORS

C = COLORS

# ── Persisted paths ───────────────────────────────────────────────────────────

AUTH_FILE   = DATA_DIR / "apex_auth.json"
SRV_CFG     = DATA_DIR / "apex_server.json"
DEFAULT_URL = "http://localhost:8000"


# ── Auth-file helpers (used by main.py too) ───────────────────────────────────

def load_server_url() -> str:
    try:
        with open(SRV_CFG, encoding="utf-8") as f:
            return json.load(f).get("url", DEFAULT_URL).rstrip("/")
    except Exception:
        return DEFAULT_URL


def save_server_url(url: str) -> None:
    SRV_CFG.parent.mkdir(parents=True, exist_ok=True)
    with open(SRV_CFG, "w", encoding="utf-8") as f:
        json.dump({"url": url.rstrip("/")}, f, indent=2)


def save_auth(token: str, user: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": token, "user": user}, f, indent=2)


def load_auth() -> dict | None:
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_auth() -> None:
    try:
        AUTH_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Worker threads ────────────────────────────────────────────────────────────

class _PingWorker(QThread):
    result = pyqtSignal(bool)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            r = requests.get(f"{self.url}/health", timeout=4)
            self.result.emit(r.ok)
        except Exception:
            self.result.emit(False)


class AuthWorker(QThread):
    success = pyqtSignal(str, dict)   # token, user
    failure = pyqtSignal(str)         # error message

    def __init__(self, mode: str, payload: dict, server_url: str):
        super().__init__()
        self.mode       = mode          # "login" | "signup"
        self.payload    = payload
        self.server_url = server_url

    def run(self):
        try:
            r = requests.post(
                f"{self.server_url}/auth/{self.mode}",
                json=self.payload,
                timeout=12,
            )
            if r.ok:
                d = r.json()
                self.success.emit(d["token"], d["user"])
            else:
                try:
                    msg = r.json().get("detail", "Authentication failed.")
                except Exception:
                    msg = f"Server error ({r.status_code})"
                self.failure.emit(msg)
        except requests.ConnectionError:
            self.failure.emit(
                "Cannot reach APEX server.\n"
                "Make sure the server is running, or click  Configure  "
                "to set the correct server URL."
            )
        except requests.Timeout:
            self.failure.emit("Connection timed out — server may still be starting up.")
        except Exception as e:
            self.failure.emit(str(e))


class TokenVerifyWorker(QThread):
    valid   = pyqtSignal(dict)   # refreshed user dict
    invalid = pyqtSignal()
    offline = pyqtSignal()       # can't reach server — don't log out

    def __init__(self, token: str, server_url: str):
        super().__init__()
        self.token      = token
        self.server_url = server_url

    def run(self):
        try:
            r = requests.get(
                f"{self.server_url}/auth/me",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=8,
            )
            if r.ok:
                self.valid.emit(r.json())
            else:
                self.invalid.emit()
        except (requests.ConnectionError, requests.Timeout):
            self.offline.emit()
        except Exception:
            self.invalid.emit()


# ── Gradient background base ──────────────────────────────────────────────────

class _GradWidget(QWidget):
    def paintEvent(self, _):
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0.0, QColor(C["bg"]))
        g.setColorAt(1.0, QColor(C["bg2"]))
        p.fillRect(self.rect(), g)


# ── Shared widget factories ───────────────────────────────────────────────────

_INPUT_SS = f"""
QLineEdit {{
    background : {C['panel2']};
    color      : {C['text']};
    border     : 1px solid {C['border']};
    border-radius: 6px;
    padding    : 0 14px;
    font-family: 'JetBrains Mono';
    font-size  : 12px;
}}
QLineEdit:focus {{
    border     : 1px solid {C['green']};
    background : {C['panel']};
}}
QLineEdit:hover:!focus {{
    border-color: {C['muted']};
}}
"""

_TOGGLE_SS = f"""
QPushButton {{
    background   : {C['panel2']};
    color        : {C['muted']};
    border       : 1px solid {C['border']};
    border-radius: 6px;
    font-size    : 14px;
    padding      : 0;
}}
QPushButton:hover   {{ color:{C['text']}; border-color:{C['muted']}; }}
QPushButton:checked {{ color:{C['green']}; border-color:{C['green']}; }}
"""

_FIELD_LBL_SS = (
    f"font-family:'JetBrains Mono';font-size:10px;letter-spacing:1px;"
    f"color:{C['muted']};background:transparent;"
)

_LINK_SS = (
    f"QPushButton{{color:{C['green']};background:transparent;border:none;"
    f"font-size:10px;letter-spacing:0.5px;padding:0;text-align:left;}}"
    f"QPushButton:hover{{color:#4dcca8;}}"
)

_LINK2_SS = (
    f"QPushButton{{color:{C['purple']};background:transparent;border:none;"
    f"font-size:10px;letter-spacing:0.5px;padding:0;text-align:left;}}"
    f"QPushButton:hover{{color:#9ba3d9;}}"
)

_SUBMIT_SS = f"""
QPushButton {{
    background   : {C['green']};
    color        : {C['bg']};
    border       : none;
    border-radius: 7px;
    font-family  : 'JetBrains Mono';
    font-size    : 11px;
    font-weight  : 700;
    letter-spacing: 3px;
}}
QPushButton:hover    {{ background: #4dcca8; }}
QPushButton:disabled {{ background: {C['border']}; color:{C['muted']}; }}
"""


def _lbl(text: str) -> QLabel:
    w = QLabel(text)
    w.setStyleSheet(_FIELD_LBL_SS)
    return w


def _input(placeholder: str = "", password: bool = False) -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    f.setFixedHeight(40)
    f.setStyleSheet(_INPUT_SS)
    if password:
        f.setEchoMode(QLineEdit.EchoMode.Password)
    return f


def _pw_row(field: QLineEdit) -> QWidget:
    """Password input + show/hide toggle button."""
    wrap = QWidget()
    wrap.setStyleSheet("background:transparent;")
    hl = QHBoxLayout(wrap)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(4)
    hl.addWidget(field)
    toggle = QPushButton("👁")
    toggle.setFixedSize(40, 40)
    toggle.setCheckable(True)
    toggle.setStyleSheet(_TOGGLE_SS)
    toggle.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    toggle.toggled.connect(
        lambda on: field.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        )
    )
    hl.addWidget(toggle)
    return wrap


def _divider() -> QFrame:
    d = QFrame()
    d.setFrameShape(QFrame.Shape.HLine)
    d.setStyleSheet(f"border:none;border-top:1px solid {C['border']};")
    return d


# ── Adaptive QStackedWidget (auto-resizes to current page) ───────────────────

class _AdaptiveStack(QStackedWidget):
    def sizeHint(self):
        w = self.currentWidget()
        return w.sizeHint() if w else super().sizeHint()

    def minimumSizeHint(self):
        w = self.currentWidget()
        return w.minimumSizeHint() if w else super().minimumSizeHint()

    def setCurrentIndex(self, idx: int):
        super().setCurrentIndex(idx)
        self.updateGeometry()


# ── Login form ────────────────────────────────────────────────────────────────

class _LoginView(QWidget):
    submitted       = pyqtSignal(str, str, bool)   # identifier, password, remember
    go_signup       = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(self)
        vl.setSpacing(6)
        vl.setContentsMargins(0, 0, 0, 0)

        vl.addWidget(_lbl("EMAIL OR USERNAME"))
        self.id_f = _input("you@example.com")
        vl.addWidget(self.id_f)
        vl.addSpacing(4)

        vl.addWidget(_lbl("PASSWORD"))
        self.pw_f = _input(password=True)
        vl.addWidget(_pw_row(self.pw_f))
        vl.addSpacing(10)

        self.remember = QCheckBox("Remember me")
        self.remember.setChecked(True)
        self.remember.setStyleSheet(f"""
            QCheckBox {{
                color:{C['muted']};font-size:10px;
                spacing:8px;background:transparent;
            }}
            QCheckBox::indicator {{
                width:14px;height:14px;
                border:1px solid {C['border']};
                border-radius:3px;background:{C['panel2']};
            }}
            QCheckBox::indicator:checked {{
                background:{C['green']};border-color:{C['green']};
            }}
        """)
        vl.addWidget(self.remember)
        vl.addSpacing(14)

        self.btn = QPushButton("LOG IN")
        self.btn.setFixedHeight(44)
        self.btn.setStyleSheet(_SUBMIT_SS)
        self.btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn.clicked.connect(self._submit)
        vl.addWidget(self.btn)
        vl.addSpacing(18)

        vl.addWidget(_divider())
        vl.addSpacing(14)

        row = QHBoxLayout()
        no_acc = QLabel("No account?")
        no_acc.setStyleSheet(f"color:{C['muted']};font-size:10px;background:transparent;")
        lnk = QPushButton("Create one  →")
        lnk.setStyleSheet(_LINK_SS)
        lnk.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        lnk.clicked.connect(self.go_signup)
        row.addWidget(no_acc)
        row.addSpacing(6)
        row.addWidget(lnk)
        row.addStretch()
        vl.addLayout(row)

        self.id_f.returnPressed.connect(self._submit)
        self.pw_f.returnPressed.connect(self._submit)

    def _submit(self):
        ident = self.id_f.text().strip()
        pw    = self.pw_f.text()
        if not ident or not pw:
            return
        self.submitted.emit(ident, pw, self.remember.isChecked())

    def set_loading(self, v: bool):
        self.btn.setEnabled(not v)
        self.btn.setText("LOGGING IN…" if v else "LOG IN")

    def clear(self):
        self.pw_f.clear()


# ── Signup form ───────────────────────────────────────────────────────────────

class _SignupView(QWidget):
    submitted  = pyqtSignal(str, str, str, str)   # email, password, username, display_name
    go_login   = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(self)
        vl.setSpacing(6)
        vl.setContentsMargins(0, 0, 0, 0)

        vl.addWidget(_lbl("DISPLAY NAME  (shown in app)"))
        self.name_f = _input("e.g. Baptiste")
        vl.addWidget(self.name_f)
        vl.addSpacing(4)

        vl.addWidget(_lbl("EMAIL ADDRESS"))
        self.email_f = _input("you@example.com")
        vl.addWidget(self.email_f)
        vl.addSpacing(4)

        vl.addWidget(_lbl("USERNAME  (optional)"))
        self.user_f = _input("e.g. baptiste16")
        vl.addWidget(self.user_f)
        vl.addSpacing(4)

        vl.addWidget(_lbl("PASSWORD  (min. 8 characters)"))
        self.pw_f = _input(password=True)
        vl.addWidget(_pw_row(self.pw_f))
        vl.addSpacing(4)

        vl.addWidget(_lbl("CONFIRM PASSWORD"))
        self.pw2_f = _input(password=True)
        vl.addWidget(_pw_row(self.pw2_f))
        vl.addSpacing(14)

        self.btn = QPushButton("CREATE ACCOUNT")
        self.btn.setFixedHeight(44)
        self.btn.setStyleSheet(_SUBMIT_SS)
        self.btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn.clicked.connect(self._submit)
        vl.addWidget(self.btn)
        vl.addSpacing(18)

        vl.addWidget(_divider())
        vl.addSpacing(14)

        row = QHBoxLayout()
        have = QLabel("Already have an account?")
        have.setStyleSheet(f"color:{C['muted']};font-size:10px;background:transparent;")
        lnk = QPushButton("Log in  →")
        lnk.setStyleSheet(_LINK2_SS)
        lnk.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        lnk.clicked.connect(self.go_login)
        row.addWidget(have)
        row.addSpacing(6)
        row.addWidget(lnk)
        row.addStretch()
        vl.addLayout(row)

        self.pw2_f.returnPressed.connect(self._submit)

    def _submit(self):
        email   = self.email_f.text().strip()
        pw      = self.pw_f.text()
        pw2     = self.pw2_f.text()
        if not email or not pw:
            return
        self.submitted.emit(
            email, pw,
            self.user_f.text().strip(),
            self.name_f.text().strip(),
        )

    def passwords_match(self) -> bool:
        return self.pw_f.text() == self.pw2_f.text()

    def set_loading(self, v: bool):
        self.btn.setEnabled(not v)
        self.btn.setText("CREATING…" if v else "CREATE ACCOUNT")

    def clear(self):
        self.pw_f.clear()
        self.pw2_f.clear()


# ── Main login window ─────────────────────────────────────────────────────────

class LoginWindow(_GradWidget):
    """
    Shown at startup when no valid token exists.
    Emits:
      auth_success(token, user)  → main.py creates ApexWindow
      guest_mode()               → user chose to continue without an account
                                   (uses local API keys, no cloud sync)
    Back-compat alias:
      offline_mode               → same as guest_mode
    """

    auth_success = pyqtSignal(str, dict)
    guest_mode   = pyqtSignal()
    # alias kept so older main.py wiring still works
    offline_mode = guest_mode

    def __init__(self):
        super().__init__()
        self.setWindowTitle("APEX — Sign In")
        self.setMinimumSize(720, 580)
        self.resize(900, 700)
        self._server_url = load_server_url()
        self._workers: list[QThread] = []   # keep alive until finished

        self._build()
        self._center()
        QTimer.singleShot(300, self._ping)

    def _center(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - self.width())  // 2,
            (screen.height() - self.height()) // 2,
        )

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Close button row
        root.addWidget(self._close_row())

        # Branding
        root.addWidget(self._branding())

        # Card (horizontally centered)
        mid = QHBoxLayout()
        mid.addStretch()
        mid.addWidget(self._card())
        mid.addStretch()
        root.addLayout(mid)

        root.addStretch(1)

        # Server status row
        root.addWidget(self._srv_row())

    def _close_row(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(0, 10, 14, 0)
        hl.addStretch()
        btn = QPushButton("✕")
        btn.setFixedSize(28, 28)
        btn.setStyleSheet(f"""
            QPushButton {{
                background:transparent;color:{C['muted']};
                border:none;font-size:14px;border-radius:14px;
            }}
            QPushButton:hover {{
                background:rgba(255,255,255,0.07);color:{C['text']};
            }}
        """)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn.clicked.connect(QApplication.quit)
        hl.addWidget(btn)
        return w

    def _branding(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(w)
        vl.setSpacing(4)
        vl.setContentsMargins(0, 8, 0, 24)
        vl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        logo = QLabel("◈  APEX")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:28px;font-weight:800;"
            f"color:{C['green']};letter-spacing:6px;background:transparent;"
        )
        vl.addWidget(logo)

        sub = QLabel("TRADING PLATFORM")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(
            f"font-size:9px;color:{C['muted']};letter-spacing:5px;"
            f"background:transparent;"
        )
        vl.addWidget(sub)
        return w

    def _card(self) -> QFrame:
        card = QFrame()
        card.setFixedWidth(430)
        card.setStyleSheet(f"""
            QFrame {{
                background   : {C['panel']};
                border       : 1px solid {C['border']};
                border-radius: 12px;
            }}
        """)
        vl = QVBoxLayout(card)
        vl.setContentsMargins(38, 28, 38, 28)
        vl.setSpacing(0)

        # Title
        self._title = QLabel("LOG IN")
        self._title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:14px;font-weight:800;"
            f"color:{C['text']};letter-spacing:4px;background:transparent;"
            f"padding-bottom:18px;"
        )
        vl.addWidget(self._title)

        # Error banner (hidden until needed)
        self._err = QLabel("")
        self._err.setWordWrap(True)
        self._err.setVisible(False)
        self._err.setStyleSheet(f"""
            color:{C['red']};font-size:10px;
            background:rgba(199,92,107,0.10);
            border:1px solid rgba(199,92,107,0.30);
            border-radius:5px;padding:8px 10px;margin-bottom:12px;
        """)
        vl.addWidget(self._err)

        # Stacked views
        self._stack = _AdaptiveStack()
        self._stack.setStyleSheet("background:transparent;border:none;")

        self._lv = _LoginView()
        self._sv = _SignupView()
        self._stack.addWidget(self._lv)   # 0
        self._stack.addWidget(self._sv)   # 1
        vl.addWidget(self._stack)

        # ── V7.1+: "or" separator + Google + Guest buttons ─────────
        # These appear under both Login and Signup forms, giving the user
        # alternatives to the email/password flow without burying them in
        # the footer.
        vl.addSpacing(14)

        sep_row = QHBoxLayout()
        sep_row.setContentsMargins(0, 0, 0, 0)
        for _ in range(2):
            ln = QFrame()
            ln.setFrameShape(QFrame.Shape.HLine)
            ln.setStyleSheet(f"border:none;border-top:1px solid {C['border']};")
            sep_row.addWidget(ln, 1)
            if _ == 0:
                or_lbl = QLabel("  or  ")
                or_lbl.setStyleSheet(
                    f"color:{C['muted']};font-size:9px;letter-spacing:2px;"
                    f"background:transparent;")
                sep_row.addWidget(or_lbl)
        sep_w = QWidget()
        sep_w.setStyleSheet("background:transparent;")
        sep_w.setLayout(sep_row)
        vl.addWidget(sep_w)
        vl.addSpacing(12)

        # Google button (disabled — wired in a future release)
        self._google_btn = QPushButton("◯  Continue with Google")
        self._google_btn.setFixedHeight(40)
        self._google_btn.setEnabled(False)
        self._google_btn.setCursor(QCursor(Qt.CursorShape.ForbiddenCursor))
        self._google_btn.setToolTip("Coming soon")
        self._google_btn.setStyleSheet(f"""
            QPushButton {{
                background    : {C['panel2']};
                color         : {C['muted']};
                border        : 1px solid {C['border']};
                border-radius : 7px;
                font-family   : 'JetBrains Mono';
                font-size     : 10px;
                letter-spacing: 2px;
                text-align    : center;
            }}
            QPushButton:disabled {{
                color         : {C['muted']};
                background    : {C['panel2']};
            }}
        """)
        vl.addWidget(self._google_btn)
        vl.addSpacing(8)

        # Guest button (active — emits guest_mode)
        self._guest_btn = QPushButton("→  Continue as Guest")
        self._guest_btn.setFixedHeight(40)
        self._guest_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._guest_btn.setStyleSheet(f"""
            QPushButton {{
                background    : transparent;
                color         : {C['purple']};
                border        : 1px solid rgba(138,147,201,0.45);
                border-radius : 7px;
                font-family   : 'JetBrains Mono';
                font-size     : 10px;
                letter-spacing: 2px;
                text-align    : center;
            }}
            QPushButton:hover {{
                background    : rgba(138,147,201,0.12);
                border-color  : {C['purple']};
            }}
        """)
        self._guest_btn.clicked.connect(self.guest_mode)
        vl.addWidget(self._guest_btn)

        guest_hint = QLabel(
            "Guest mode: trade with your own Alpaca/IBKR keys, "
            "no cloud sync.")
        guest_hint.setWordWrap(True)
        guest_hint.setStyleSheet(
            f"color:{C['muted']};font-size:9px;background:transparent;"
            f"padding-top:4px;")
        vl.addWidget(guest_hint)

        # Wire view-switch links
        self._lv.go_signup.connect(self._show_signup)
        self._sv.go_login.connect(self._show_login)

        # Wire form submissions
        self._lv.submitted.connect(self._do_login)
        self._sv.submitted.connect(self._do_signup)

        return card

    def _srv_row(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        hl = QHBoxLayout(w)
        hl.setContentsMargins(30, 6, 30, 16)
        hl.setSpacing(6)

        self._srv_dot = QLabel("●")
        self._srv_dot.setStyleSheet(
            f"color:{C['muted']};font-size:8px;background:transparent;")
        hl.addWidget(self._srv_dot)

        self._srv_lbl = QLabel(f"Server: {self._server_url}")
        self._srv_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:10px;background:transparent;")
        hl.addWidget(self._srv_lbl)

        cfg = QPushButton("Configure")
        cfg.setStyleSheet(
            f"QPushButton{{color:{C['purple']};background:transparent;border:none;"
            f"font-size:10px;padding:0;}}"
            f"QPushButton:hover{{color:#9ba3d9;}}"
        )
        cfg.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cfg.clicked.connect(self._configure_server)
        hl.addWidget(cfg)

        hl.addStretch()

        # V7.1+: Continue-offline link removed from the footer —
        # the prominent "Continue as Guest" button in the card now
        # serves that purpose, keeping the footer focused on server
        # connection diagnostics.

        return w

    # ── View switching ────────────────────────────────────────────────────────

    def _show_login(self):
        self._title.setText("LOG IN")
        self._stack.setCurrentIndex(0)
        self._clear_err()
        QTimer.singleShot(0, self.adjustSize)

    def _show_signup(self):
        self._title.setText("CREATE ACCOUNT")
        self._stack.setCurrentIndex(1)
        self._clear_err()
        QTimer.singleShot(0, self.adjustSize)

    # ── Auth actions ──────────────────────────────────────────────────────────

    def _do_login(self, identifier: str, password: str, remember: bool):
        self._clear_err()
        self._lv.set_loading(True)
        w = AuthWorker("login", {"identifier": identifier, "password": password},
                       self._server_url)
        w.success.connect(lambda tok, usr: self._on_success(tok, usr, remember))
        w.failure.connect(self._on_failure)
        w.finished.connect(lambda: self._lv.set_loading(False))
        self._workers.append(w)
        w.start()

    def _do_signup(self, email: str, password: str, username: str, display_name: str):
        self._clear_err()
        if not self._sv.passwords_match():
            self._show_err("Passwords do not match.")
            return
        if len(password) < 8:
            self._show_err("Password must be at least 8 characters.")
            return
        self._sv.set_loading(True)
        w = AuthWorker("signup", {
            "email":        email,
            "password":     password,
            "username":     username   or None,
            "display_name": display_name or None,
        }, self._server_url)
        w.success.connect(lambda tok, usr: self._on_success(tok, usr, True))
        w.failure.connect(self._on_failure)
        w.finished.connect(lambda: self._sv.set_loading(False))
        self._workers.append(w)
        w.start()

    def _on_success(self, token: str, user: dict, remember: bool):
        if remember:
            save_auth(token, user)
        self.auth_success.emit(token, user)

    def _on_failure(self, msg: str):
        self._show_err(msg)

    # ── Error display ─────────────────────────────────────────────────────────

    def _show_err(self, msg: str):
        self._err.setText(msg)
        self._err.setVisible(True)
        QTimer.singleShot(0, self.adjustSize)

    def _clear_err(self):
        self._err.setVisible(False)
        self._err.setText("")

    # ── Server ping ───────────────────────────────────────────────────────────

    def _ping(self):
        w = _PingWorker(self._server_url)
        w.result.connect(self._set_srv_status)
        self._workers.append(w)
        w.start()

    @pyqtSlot(bool)
    def _set_srv_status(self, online: bool):
        color = C["green"] if online else C["red"]
        self._srv_dot.setStyleSheet(
            f"color:{color};font-size:8px;background:transparent;")
        self._srv_lbl.setStyleSheet(
            f"color:{'#4a566b' if online else C['red']};"
            f"font-size:10px;background:transparent;"
        )

    # ── Server config ─────────────────────────────────────────────────────────

    def _configure_server(self):
        url, ok = QInputDialog.getText(
            self, "Server URL",
            "Enter APEX server URL (e.g. http://1.2.3.4:8000):",
            text=self._server_url,
        )
        if ok and url.strip():
            self._server_url = url.strip().rstrip("/")
            save_server_url(self._server_url)
            self._srv_lbl.setText(f"Server: {self._server_url}")
            self._ping()

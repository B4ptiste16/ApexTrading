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

AUTH_FILE     = DATA_DIR / "apex_auth.json"
ACCOUNTS_FILE = DATA_DIR / "apex_accounts.json"   # V3 wave 5
SRV_CFG       = DATA_DIR / "apex_server.json"
DEFAULT_URL   = "http://localhost:8000"


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
    """Persist the ACTIVE session + also keep a copy in the multi-account
    list so the user can later one-click-switch back."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": token, "user": user}, f, indent=2)
    _upsert_saved_account(token, user)


def load_auth() -> dict | None:
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_auth() -> None:
    """Clear the ACTIVE session without forgetting the saved-accounts list,
    so the login window can still offer one-click sign-in to other
    accounts the user has previously used on this machine."""
    try:
        AUTH_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── V3 wave 5 — Multi-account storage ────────────────────────────────

def _load_accounts_file() -> list[dict]:
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("accounts", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_accounts_file(accounts: list[dict]) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, indent=2)


def _upsert_saved_account(token: str, user: dict) -> None:
    """Add or refresh this account's entry in the saved-accounts list."""
    if not user or not user.get("id"):
        return
    from datetime import datetime, timezone
    accounts = _load_accounts_file()
    uid = int(user["id"])
    accounts = [a for a in accounts if int(a.get("user", {}).get("id", 0)) != uid]
    accounts.insert(0, {
        "token":        token,
        "user":         user,
        "last_used_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_accounts_file(accounts[:20])  # keep the 20 most recent


def list_saved_accounts() -> list[dict]:
    """Returns the saved-accounts list (most-recent first). Each entry:
       {token, user: {id, username, display_name, email, ...}, last_used_at}"""
    return _load_accounts_file()


def activate_saved_account(user_id: int) -> dict | None:
    """Promote a saved account to the active session. Returns the
    promoted account dict, or None if not found."""
    accounts = _load_accounts_file()
    for a in accounts:
        if int(a.get("user", {}).get("id", 0)) == int(user_id):
            with open(AUTH_FILE, "w", encoding="utf-8") as f:
                json.dump({"token": a["token"], "user": a["user"]},
                          f, indent=2)
            from datetime import datetime, timezone
            a["last_used_at"] = datetime.now(timezone.utc).isoformat()
            # Move to front
            others = [x for x in accounts
                      if int(x.get("user", {}).get("id", 0)) != int(user_id)]
            _write_accounts_file([a] + others[:19])
            return a
    return None


def forget_saved_account(user_id: int) -> None:
    accounts = _load_accounts_file()
    _write_accounts_file([a for a in accounts
                          if int(a.get("user", {}).get("id", 0)) != int(user_id)])


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


class _GoogleOAuthWorker(QThread):
    """Spawn a loopback HTTP listener, drive the standard OAuth2 'native
    app' flow, then hand the resulting code to the APEX server for
    exchange. All network operations live off the Qt main thread."""
    success = pyqtSignal(str, dict)
    failure = pyqtSignal(str)

    def __init__(self, client_id: str, server_url: str):
        super().__init__()
        self.client_id  = client_id
        self.server_url = server_url

    def run(self):
        import http.server
        import secrets as _secrets
        import socket
        import threading
        import urllib.parse
        import webbrowser

        # 1. Pick a free loopback port. Google's Desktop-App client type
        # accepts ANY 127.0.0.1:N redirect URI without pre-registration.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        redirect_uri = f"http://127.0.0.1:{port}"
        state = _secrets.token_urlsafe(16)

        # 2. Build the auth URL
        params = {
            "client_id":     self.client_id,
            "redirect_uri":  redirect_uri,
            "response_type": "code",
            "scope":         "openid email profile",
            "state":         state,
            "access_type":   "online",
            "prompt":        "select_account",
        }
        auth_url = ("https://accounts.google.com/o/oauth2/v2/auth?"
                    + urllib.parse.urlencode(params))

        # 3. Spin up a single-request HTTP server to catch the redirect.
        result: dict = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self_inner):
                qs = urllib.parse.urlparse(self_inner.path).query
                qd = dict(urllib.parse.parse_qsl(qs))
                result["code"]  = qd.get("code", "")
                result["state"] = qd.get("state", "")
                result["error"] = qd.get("error", "")
                body = (
                    "<html><body style='font-family:sans-serif;"
                    "padding:40px;text-align:center;background:#0c0f16;"
                    "color:#d8dde8;'><h2 style='color:#3fb89a;letter-spacing:4px;'>"
                    "o APEX</h2><p>Sign-in received. You can close this tab "
                    "and return to BAPTOU.</p></body></html>"
                ).encode("utf-8")
                self_inner.send_response(200)
                self_inner.send_header("Content-Type", "text/html")
                self_inner.send_header("Content-Length", str(len(body)))
                self_inner.end_headers()
                self_inner.wfile.write(body)

            def log_message(self_inner, *args):  # silence noisy logs
                pass

        try:
            srv = http.server.HTTPServer(("127.0.0.1", port), _Handler)
        except Exception as e:
            self.failure.emit(f"Could not bind loopback port: {e}")
            return

        # Serve one request, then shut down. 5-minute hard cap.
        def _serve_one():
            srv.timeout = 300
            srv.handle_request()
        t = threading.Thread(target=_serve_one, daemon=True)
        t.start()

        # 4. Open the browser
        try:
            webbrowser.open(auth_url)
        except Exception as e:
            srv.server_close()
            self.failure.emit(f"Could not open browser: {e}")
            return

        # 5. Wait for the handler to populate `result`
        t.join(timeout=305)
        try:
            srv.server_close()
        except Exception:
            pass

        if result.get("error"):
            self.failure.emit(f"Google: {result['error']}")
            return
        if result.get("state") != state:
            self.failure.emit("OAuth state mismatch — aborted for safety.")
            return
        code = result.get("code")
        if not code:
            self.failure.emit("Sign-in window closed without completing.")
            return

        # 6. Hand the code to APEX server for the actual exchange.
        try:
            r = requests.post(
                f"{self.server_url}/auth/google/exchange",
                json={"code": code, "redirect_uri": redirect_uri},
                timeout=20,
            )
            if not r.ok:
                detail = r.json().get("detail", r.text) if r.headers.get(
                    "content-type", "").startswith("application/json") else r.text
                self.failure.emit(f"BAPTOU server: {detail}")
                return
            body = r.json()
            self.success.emit(body["token"], body["user"])
        except Exception as e:
            self.failure.emit(f"Token exchange failed: {e}")


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
                "Cannot reach BAPTOU server.\n"
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
                border:none;
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
        btn = QPushButton("X")
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
        """V4.1.0 — display assets/baptou_logo.png (the B graphic + the
        BAPTOU / TRADING text underneath, the user's actual logo) when
        the file exists. Falls back to a typographic version that
        matches the same aesthetic (big bold BAPTOU + thin -- TRADING --
        subtitle with green bracket lines)."""
        from pathlib import Path as _P
        from PyQt6.QtGui import QPixmap as _QPx
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(w)
        vl.setSpacing(4)
        vl.setContentsMargins(0, 8, 0, 24)
        vl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Locate the logo PNG — works in both dev (source root) and
        # frozen builds (sys._MEIPASS / assets/).
        import sys as _sys
        candidates = []
        meipass = getattr(_sys, "_MEIPASS", None)
        if meipass:
            candidates.append(_P(meipass) / "assets" / "baptou_logo.png")
        exe_dir = _P(_sys.executable).parent
        candidates.append(exe_dir / "_internal" / "assets" / "baptou_logo.png")
        candidates.append(_P(__file__).parent.parent / "assets" / "baptou_logo.png")

        logo_path = next((p for p in candidates if p.exists()), None)
        if logo_path is not None:
            pic = QLabel()
            pix = _QPx(str(logo_path))
            if not pix.isNull():
                # Scale to a sensible height; preserve aspect.
                pic.setPixmap(pix.scaledToHeight(
                    220, Qt.TransformationMode.SmoothTransformation))
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pic.setStyleSheet("background:transparent;")
            vl.addWidget(pic)
            return w

        # Typographic fallback — matches the new logo's styling
        logo = QLabel("BAPTOU")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:42px;font-weight:900;"
            f"color:#ffffff;letter-spacing:6px;background:transparent;")
        vl.addWidget(logo)
        # Sub with the bracket-line style from the logo
        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        sub_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for _ in range(2):
            line = QFrame()
            line.setFixedSize(34, 1)
            line.setStyleSheet(
                f"background:{C['green']};border:none;")
            sub_row.addWidget(line)
            if _ == 0:
                sub_txt = QLabel("TRADING")
                sub_txt.setStyleSheet(
                    f"color:{C['green']};font-family:'JetBrains Mono';"
                    f"font-size:11px;letter-spacing:6px;font-weight:700;"
                    f"background:transparent;")
                sub_row.addWidget(sub_txt)
        sub_w = QWidget()
        sub_w.setStyleSheet("background:transparent;")
        sub_w.setLayout(sub_row)
        vl.addWidget(sub_w)
        return w

    def _build_saved_accounts_section(self) -> QWidget:
        """V3 wave 5 — one-click sign-in for previously-used accounts.
        Hidden when no saved accounts exist."""
        wrap = QWidget()
        wrap.setStyleSheet("background:transparent;")
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 14)
        col.setSpacing(6)

        title = QLabel("CONTINUE AS")
        title.setStyleSheet(
            f"color:{C['muted']};font-size:9px;letter-spacing:3px;"
            f"font-weight:700;background:transparent;padding:0 0 4px 0;")
        col.addWidget(title)

        self._saved_rows_box = QWidget()
        self._saved_rows_box.setStyleSheet("background:transparent;")
        self._saved_rows_layout = QVBoxLayout(self._saved_rows_box)
        self._saved_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._saved_rows_layout.setSpacing(6)
        col.addWidget(self._saved_rows_box)

        self._populate_saved_accounts()
        wrap.setVisible(len(list_saved_accounts()) > 0)
        return wrap

    def _populate_saved_accounts(self):
        # Clear existing
        while self._saved_rows_layout.count():
            item = self._saved_rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        accounts = list_saved_accounts()
        for acc in accounts:
            self._saved_rows_layout.addWidget(self._make_saved_row(acc))

    def _make_saved_row(self, acc: dict) -> QWidget:
        u = acc.get("user", {})
        row = QFrame()
        row.setStyleSheet(f"""
            QFrame {{
                background    : {C['panel2']};
                border        : 1px solid {C['border']};
                border-radius : 7px;
            }}
            QFrame:hover {{
                border-color  : {C['muted']};
            }}
        """)
        hl = QHBoxLayout(row)
        hl.setContentsMargins(12, 8, 8, 8)
        hl.setSpacing(8)

        name_btn = QPushButton(
            f"{u.get('display_name') or u.get('username','?')}\n"
            f"  @{u.get('username','?')}")
        name_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        name_btn.setStyleSheet(f"""
            QPushButton {{
                background    : transparent;
                color         : {C['text']};
                border        : none;
                text-align    : left;
                font-family   : 'JetBrains Mono';
                font-size     : 11px;
                padding       : 2px 0;
            }}
            QPushButton:hover {{
                color         : {C['green']};
            }}
        """)
        name_btn.clicked.connect(
            lambda _, uid=u.get("id"): self._activate_saved(uid))
        hl.addWidget(name_btn, 1)

        forget = QPushButton("×")
        forget.setFixedSize(22, 22)
        forget.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        forget.setStyleSheet(f"""
            QPushButton {{
                background    : transparent;
                color         : {C['muted']};
                border        : none;
                font-size     : 14px;
            }}
            QPushButton:hover {{
                color         : {C['red']};
            }}
        """)
        forget.setToolTip("Forget this account")
        forget.clicked.connect(
            lambda _, uid=u.get("id"): self._forget_saved(uid))
        hl.addWidget(forget)
        return row

    def _activate_saved(self, user_id: int):
        if not user_id:
            return
        acc = activate_saved_account(int(user_id))
        if not acc:
            self._show_err("Could not switch to that account.")
            return
        self.auth_success.emit(acc["token"], acc["user"])

    def _forget_saved(self, user_id: int):
        forget_saved_account(int(user_id))
        self._populate_saved_accounts()
        if not list_saved_accounts():
            self._saved_section.setVisible(False)

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
            border:none;
            border-radius:5px;padding:8px 10px;margin-bottom:12px;
        """)
        vl.addWidget(self._err)

        # V3 wave 5 — saved accounts (one-click sign-in if previously
        # signed in on this machine).
        self._saved_section = self._build_saved_accounts_section()
        vl.addWidget(self._saved_section)

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

        # V3.0.2 — Google OAuth via standard loopback flow.
        self._google_btn = QPushButton("◯  Continue with Google")
        self._google_btn.setFixedHeight(40)
        self._google_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._google_btn.setStyleSheet(f"""
            QPushButton {{
                background    : {C['panel2']};
                color         : {C['text']};
                border        : 1px solid {C['border']};
                border-radius : 7px;
                font-family   : 'JetBrains Mono';
                font-size     : 10px;
                letter-spacing: 2px;
                text-align    : center;
            }}
            QPushButton:hover {{
                background    : {C['panel']};
                border-color  : {C['muted']};
            }}
            QPushButton:disabled {{
                color         : {C['muted']};
                background    : {C['panel2']};
            }}
        """)
        self._google_btn.clicked.connect(self._start_google_oauth)
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

    # ── Google OAuth (V3 wave 4) ──────────────────────────────────────────

    def _start_google_oauth(self):
        """Standard loopback flow:
          1. Spawn a one-shot HTTP server on 127.0.0.1:RANDOM_PORT.
          2. Open the user's default browser to Google's consent page.
          3. Google redirects back to our loopback URI with ?code=...
          4. We forward the code to APEX server's /auth/google/exchange.
          5. Save the returned APEX JWT and emit auth_success.
        All run inside a single background QThread so the UI never freezes."""
        # First check the server has Google OAuth configured
        self._google_btn.setEnabled(False)
        self._google_btn.setText("◯  Asking server…")
        try:
            r = requests.get(f"{self._server_url}/auth/google/config", timeout=6)
            cfg = r.json() if r.ok else {}
        except Exception as e:
            self._google_btn.setEnabled(True)
            self._google_btn.setText("◯  Continue with Google")
            self._show_err(f"Could not reach APEX server: {e}")
            return
        if not cfg.get("configured"):
            self._google_btn.setEnabled(True)
            self._google_btn.setText("◯  Continue with Google")
            self._show_err(
                "Google sign-in not configured on the server. Ask the "
                "admin to set GOOGLE_OAUTH_CLIENT_ID / SECRET.")
            return

        self._google_btn.setText("◯  Waiting for browser sign-in…")
        worker = _GoogleOAuthWorker(
            client_id=cfg["client_id"],
            server_url=self._server_url,
        )
        worker.success.connect(
            lambda tok, usr: self._on_google_done(worker, True, tok, usr, ""))
        worker.failure.connect(
            lambda msg: self._on_google_done(worker, False, "", {}, msg))
        self._workers.append(worker)
        worker.start()

    def _on_google_done(self, worker, ok: bool, token: str,
                        user: dict, err: str):
        self._google_btn.setEnabled(True)
        self._google_btn.setText("◯  Continue with Google")
        if ok:
            save_auth(token, user)
            self.auth_success.emit(token, user)
        else:
            self._show_err(err or "Google sign-in failed.")

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


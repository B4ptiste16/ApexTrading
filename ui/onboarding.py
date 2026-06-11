"""
V4.6.94 — Onboarding & broker connection help.

Two things live here, sharing one source of truth (BROKER_GUIDE):

  • InstructionsPanel — a collapsible "How to create & link an <broker>
    account" panel embedded in the Tools connection screens.
  • WelcomeWizard — a one-time, stepped welcome dialog shown the first time
    the app is opened on a fresh account: welcome → pick broker → connect
    steps → finish.

Keeping the steps in one dict means the wizard and the in-Tools panel never
drift apart.
"""

from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
    QDialog, QStackedWidget,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QDesktopServices
from PyQt6.QtCore import QUrl

from ui.styles import COLORS
import core.data as D

C = COLORS


# ── Single source of truth for the broker setup steps ────────────────────
BROKER_GUIDE = {
    "alpaca": {
        "name":  "Alpaca",
        "color": C["green"],
        "url":   "https://alpaca.markets",
        "tagline": "Free US stock & crypto brokerage with a paper-trading "
                   "(fake-money) mode — perfect for running bots risk-free.",
        "steps": [
            ("Create a free account",
             "Go to alpaca.markets and click Sign Up. Confirm your email and "
             "log in to the dashboard. No deposit is required for paper trading."),
            ("Switch to Paper Trading",
             "In the dashboard, use the toggle (top-left) to select "
             "“Paper Trading”. This is a fake-money account — bots "
             "trade here with zero real risk."),
            ("Generate API keys",
             "On the right side of the Paper dashboard, open “API Keys” "
             "and click Generate New Keys. Copy the Key ID and the Secret Key "
             "(the secret is shown only once — copy it now)."),
            ("Paste them into BAPTOU",
             "Open Tools → ALPACA · API KEYS. Paste the Key + Secret "
             "into a slot, set its “Assigned” bot, then Save. Your "
             "bot is now linked and ready to run."),
        ],
    },
    "ibkr": {
        "name":  "IBKR",
        "color": C["purple"],
        "url":   "https://www.interactivebrokers.com",
        "tagline": "Interactive Brokers — professional global brokerage. "
                   "BAPTOU runs the IBKR Gateway for you in the cloud so you "
                   "don’t have to keep software open.",
        "steps": [
            ("Open an IBKR account",
             "Go to interactivebrokers.com and click Open Account. Once "
             "approved, log in to Client Portal."),
            ("Enable a Paper Trading account",
             "In Client Portal: Settings → Account Settings → Paper "
             "Trading Account. Note the paper username/password IBKR gives you "
             "— bots trade on this fake-money account."),
            ("Connect via the cloud (recommended)",
             "Open Tools → IBKR · CLOUD 24/7. Enter your IBKR paper "
             "username + password, tick “Run on Oracle” and Save. "
             "BAPTOU launches and maintains the Gateway for you — nothing to "
             "install, runs 24/7."),
            ("Or connect locally (optional)",
             "Prefer to run it yourself? Install IB Gateway / TWS, then "
             "Configure → Settings → API → enable “ActiveX "
             "and Socket Clients” on port 7497 (paper). Enter host "
             "127.0.0.1 and port 7497 in Tools → IBKR."),
            ("Assign your bots",
             "In Tools → IBKR, give each bot a unique Client ID and an "
             "allocation %, then Save. You’re ready to trade."),
        ],
    },
}


def _logo_pixmap(broker: str, height: int):
    try:
        from ui.bot_card import broker_icon
        return broker_icon(broker, height)
    except Exception:
        return None


def _steps_widget(broker: str) -> QWidget:
    """The numbered step list + 'Open <broker> website' button."""
    g = BROKER_GUIDE.get(broker, {})
    col = g.get("color", C["purple"])
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(12)

    tag = QLabel(g.get("tagline", ""))
    tag.setStyleSheet(f"color:{C['muted']};font-size:11px;")
    tag.setWordWrap(True)
    v.addWidget(tag)

    for i, (head, body) in enumerate(g.get("steps", []), 1):
        row = QHBoxLayout()
        row.setSpacing(10)
        num = QLabel(str(i))
        num.setFixedSize(22, 22)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet(
            f"background:{col}33;color:{col};border-radius:11px;"
            f"font-size:11px;font-weight:800;")
        txt = QLabel(f"<b style='color:{C['text']}'>{head}</b><br>"
                     f"<span style='color:{C['muted']}'>{body}</span>")
        txt.setStyleSheet("font-size:11px;")
        txt.setWordWrap(True)
        row.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(txt, 1)
        v.addLayout(row)

    url = g.get("url")
    if url:
        btn = QPushButton(f"Open {g.get('name','broker')} website  ↗")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton{{background:{col}22;color:{col};border:none;"
            f"border-radius:6px;padding:7px 14px;font-size:11px;font-weight:700;}}"
            f"QPushButton:hover{{background:{col}33;}}")
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        v.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
    return w


class InstructionsPanel(QFrame):
    """Collapsible 'How to create & link a <broker> account' panel for the
    Tools connection screens. Collapsed by default; click the header to open."""

    def __init__(self, broker: str, parent=None, start_open: bool = False):
        super().__init__(parent)
        g = BROKER_GUIDE.get(broker, {})
        col = g.get("color", C["purple"])
        self.setStyleSheet(
            f"QFrame#instrPanel{{background:{C['panel']};border:1px solid "
            f"{col}44;border-radius:10px;}}")
        self.setObjectName("instrPanel")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(10)

        hdr = QPushButton()
        hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        hdr.setStyleSheet("QPushButton{background:transparent;border:none;text-align:left;}")
        hb = QHBoxLayout(hdr)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(8)
        logo = QLabel()
        pm = _logo_pixmap(broker, 18)
        if pm is not None:
            logo.setPixmap(pm)
        title = QLabel(f"How to create &amp; link a {g.get('name','broker')} account")
        title.setStyleSheet(f"color:{C['text']};font-size:12px;font-weight:700;")
        self._chevron = QLabel("▸")
        self._chevron.setStyleSheet(f"color:{col};font-size:12px;font-weight:800;")
        hb.addWidget(logo)
        hb.addWidget(title)
        hb.addStretch()
        hb.addWidget(self._chevron)
        hdr.clicked.connect(self._toggle)
        v.addWidget(hdr)

        self._body = _steps_widget(broker)
        self._body.setVisible(start_open)
        self._chevron.setText("▾" if start_open else "▸")
        v.addWidget(self._body)

    def _toggle(self):
        vis = not self._body.isVisible()
        self._body.setVisible(vis)
        self._chevron.setText("▾" if vis else "▸")


# ── First-run welcome wizard ─────────────────────────────────────────────
class WelcomeWizard(QDialog):
    """One-time stepped welcome: intro → pick broker → connect steps
    → finish. Sets the chosen broker_mode and marks onboarding complete."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to BAPTOU")
        self.setMinimumSize(640, 560)
        self.setStyleSheet(f"background:{C['bg']};")
        self._broker = D.load_settings().get("broker_mode", "alpaca")

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(16)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_intro())
        self._stack.addWidget(self._page_pick())
        self._stack.addWidget(self._page_connect())
        root.addWidget(self._stack, 1)

        # Nav row
        nav = QHBoxLayout()
        self._back_btn = QPushButton("Back")
        self._back_btn.setStyleSheet(self._btn_css(False))
        self._back_btn.clicked.connect(self._back)
        self._next_btn = QPushButton("Next")
        self._next_btn.setStyleSheet(self._btn_css(True))
        self._next_btn.clicked.connect(self._next)
        skip = QPushButton("Skip")
        skip.setStyleSheet(
            f"QPushButton{{background:transparent;color:{C['muted']};border:none;"
            f"font-size:11px;}}QPushButton:hover{{color:{C['text']};}}")
        skip.clicked.connect(self._finish)
        nav.addWidget(skip)
        nav.addStretch()
        nav.addWidget(self._back_btn)
        nav.addWidget(self._next_btn)
        root.addLayout(nav)
        self._sync_nav()

    def _btn_css(self, primary: bool) -> str:
        if primary:
            return (f"QPushButton{{background:{C['purple']};color:#0e1016;"
                    f"border:none;border-radius:7px;padding:9px 22px;"
                    f"font-size:12px;font-weight:800;}}"
                    f"QPushButton:hover{{background:{C['text']};}}")
        return (f"QPushButton{{background:{C['panel']};color:{C['text']};"
                f"border:1px solid {C['border']};border-radius:7px;"
                f"padding:9px 22px;font-size:12px;}}"
                f"QPushButton:hover{{border-color:{C['purple']};}}")

    # Page 0 — intro
    def _page_intro(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(14)
        v.addStretch()
        logo = QLabel()
        try:
            from pathlib import Path
            p = Path(__file__).resolve().parent.parent / "assets" / "baptou_logo.png"
            if p.exists():
                logo.setPixmap(QPixmap(str(p)).scaledToHeight(
                    72, Qt.TransformationMode.SmoothTransformation))
                logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
                v.addWidget(logo)
        except Exception:
            pass
        title = QLabel("Welcome to BAPTOU")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:26px;font-weight:800;"
            f"color:{C['text']};letter-spacing:2px;")
        v.addWidget(title)
        sub = QLabel("Run AI trading bots on your own brokerage account — "
                     "safely, on paper money. Let’s get you connected in "
                     "three quick steps.")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{C['muted']};font-size:13px;")
        v.addWidget(sub)
        v.addStretch()
        return w

    # Page 1 — pick broker
    def _page_pick(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(14)
        head = QLabel("Choose your broker")
        head.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:18px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        v.addWidget(head)
        info = QLabel("Pick where your bots will trade. You can switch later "
                      "from the broker chip in the top-right.")
        info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        info.setWordWrap(True)
        v.addWidget(info)

        self._broker_btns = {}
        for key in ("alpaca", "ibkr"):
            g = BROKER_GUIDE[key]
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(76)
            hb = QHBoxLayout(btn)
            hb.setContentsMargins(16, 10, 16, 10)
            hb.setSpacing(12)
            logo = QLabel()
            pm = _logo_pixmap(key, 26)
            if pm is not None:
                logo.setPixmap(pm)
            else:
                logo.setText(g["name"])
            txt = QLabel(f"<b style='color:{C['text']};font-size:13px'>{g['name']}</b>"
                         f"<br><span style='color:{C['muted']};font-size:10px'>"
                         f"{g['tagline']}</span>")
            txt.setWordWrap(True)
            hb.addWidget(logo)
            hb.addWidget(txt, 1)
            btn.clicked.connect(lambda _c, k=key: self._select_broker(k))
            self._broker_btns[key] = btn
            v.addWidget(btn)
        v.addStretch()
        self._restyle_broker_btns()
        return w

    def _select_broker(self, key: str):
        self._broker = key
        self._restyle_broker_btns()
        # rebuild connect page for the new broker
        self._stack.removeWidget(self._connect_page)
        self._connect_page.deleteLater()
        self._connect_page = self._page_connect()
        self._stack.insertWidget(2, self._connect_page)

    def _restyle_broker_btns(self):
        for key, btn in getattr(self, "_broker_btns", {}).items():
            sel = (key == self._broker)
            col = BROKER_GUIDE[key]["color"]
            btn.setChecked(sel)
            btn.setStyleSheet(
                f"QPushButton{{background:{C['panel']};border:2px solid "
                f"{col if sel else C['border']};border-radius:10px;text-align:left;}}")

    # Page 2 — connect steps
    def _page_connect(self) -> QWidget:
        self._connect_page = QWidget()
        v = QVBoxLayout(self._connect_page)
        v.setSpacing(12)
        g = BROKER_GUIDE.get(self._broker, {})
        head = QHBoxLayout()
        logo = QLabel()
        pm = _logo_pixmap(self._broker, 24)
        if pm is not None:
            logo.setPixmap(pm)
        title = QLabel(f"Connect {g.get('name','your broker')}")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:18px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        head.addWidget(logo)
        head.addWidget(title)
        head.addStretch()
        v.addLayout(head)
        v.addWidget(_steps_widget(self._broker))
        v.addStretch()
        return self._connect_page

    # Nav
    def _sync_nav(self):
        idx = self._stack.currentIndex()
        self._back_btn.setVisible(idx > 0)
        self._next_btn.setText("Finish" if idx == self._stack.count() - 1 else "Next")

    def _back(self):
        self._stack.setCurrentIndex(max(0, self._stack.currentIndex() - 1))
        self._sync_nav()

    def _next(self):
        idx = self._stack.currentIndex()
        if idx >= self._stack.count() - 1:
            self._finish()
            return
        self._stack.setCurrentIndex(idx + 1)
        self._sync_nav()

    def _finish(self):
        try:
            s = D.load_settings()
            s["broker_mode"] = self._broker
            s["onboarding_done"] = True
            import json
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[onboarding] save failed: {e}")
        self.accept()


def needs_onboarding() -> bool:
    """True when the welcome wizard hasn't been completed yet on this account."""
    try:
        return not bool(D.load_settings().get("onboarding_done", False))
    except Exception:
        return False

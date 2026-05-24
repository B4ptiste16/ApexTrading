"""
BAPTOU Credit Shop — buy APEX credits via Stripe Checkout.
Opened by clicking the ◊ credits chip in the app header.
"""

import webbrowser

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QGridLayout, QWidget,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from ui.styles import COLORS

C = COLORS

# Packages mirrored from server/payments.py (cent prices kept in sync)
_PACKAGES = [
    # (key,      label,    credits, cents, popular)
    ("starter", "Starter",  100,   99,   False),
    ("popular", "Popular",  500,   449,  True),
    ("power",   "Power",   1500,  1199,  False),
    ("mega",    "Mega",    5000,  3499,  False),
]


# ── Background workers ────────────────────────────────────────────

class _CheckoutWorker(QThread):
    result = pyqtSignal(str, str)   # url, error

    def __init__(self, server_url: str, token: str, package_key: str):
        super().__init__()
        self.server_url  = server_url
        self.token       = token
        self.package_key = package_key

    def run(self):
        import requests
        try:
            r = requests.post(
                self.server_url + "/payments/create-checkout",
                headers={"Authorization": "Bearer " + self.token},
                json={"package_key": self.package_key,
                      "server_url":  self.server_url},
                timeout=15,
            )
            if r.ok:
                self.result.emit(r.json().get("url", ""), "")
            else:
                detail = ""
                try:
                    detail = r.json().get("detail", r.text)
                except Exception:
                    detail = r.text
                self.result.emit("", str(detail))
        except Exception as e:
            self.result.emit("", str(e))


class _BalanceWorker(QThread):
    result = pyqtSignal(int, str)   # balance (-1 = error), error_msg

    def __init__(self, server_url: str, token: str):
        super().__init__()
        self.server_url = server_url
        self.token      = token

    def run(self):
        import requests
        try:
            r = requests.get(
                self.server_url + "/credits/me",
                headers={"Authorization": "Bearer " + self.token},
                timeout=10,
            )
            if r.ok:
                self.result.emit(int(r.json().get("balance", 0)), "")
            else:
                self.result.emit(-1, r.text)
        except Exception as e:
            self.result.emit(-1, str(e))


# ── Main dialog ───────────────────────────────────────────────────

class CreditShopDialog(QDialog):
    """2×2 card grid of credit packages. Buy → browser → Stripe → webhook."""

    balance_refreshed = pyqtSignal(int)   # parent updates its chip

    def __init__(self, parent=None, current_balance: int = 0):
        super().__init__(parent)
        self.setWindowTitle("BAPTOU — Credit Shop")
        self.setFixedWidth(560)
        self.setModal(True)
        self.setStyleSheet(
            f"background:{C['bg']};color:{C['text']};"
            f"QLabel{{background:transparent;border:none;}}"
        )
        self._balance = current_balance
        self._workers: list = []
        self._build()

    # ── helpers ──────────────────────────────────────────────────

    def _auth(self) -> tuple[str, str]:
        """Return (server_url, token). Empty strings = not signed in."""
        try:
            from ui.login import load_auth, load_server_url
            return load_server_url(), (load_auth() or {}).get("token", "")
        except Exception:
            return "", ""

    # ── layout ───────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(16)

        # Header row
        hdr = QHBoxLayout()
        title = QLabel("◈  CREDIT SHOP")
        title.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:17px;font-weight:800;"
            f"letter-spacing:4px;color:{C['text']};")
        self._bal_lbl = QLabel("◊  " + f"{self._balance:,}")
        self._bal_lbl.setStyleSheet(
            f"color:{C['yellow']};font-size:13px;font-weight:700;")
        ref_btn = QPushButton("↻")
        ref_btn.setObjectName("toolBtn")
        ref_btn.setFixedSize(26, 26)
        ref_btn.setToolTip("Refresh balance")
        ref_btn.clicked.connect(self._refresh_balance)
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._bal_lbl)
        hdr.addSpacing(4)
        hdr.addWidget(ref_btn)
        root.addLayout(hdr)

        sub = QLabel(
            "Credits are used to buy marketplace bots and run "
            "AI-powered bot generation.  1 ◊ = $0.01")
        sub.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # 2×2 package grid
        grid = QGridLayout()
        grid.setSpacing(10)
        self._buy_btns: dict[str, QPushButton] = {}
        for idx, (key, label, credits, cents, popular) in enumerate(_PACKAGES):
            grid.addWidget(
                self._make_card(key, label, credits, cents, popular),
                idx // 2, idx % 2,
            )
        root.addLayout(grid)

        # Status line
        self._status = QLabel(
            "Payments open in your browser — return here and click ↻ once done.")
        self._status.setStyleSheet(
            f"color:{C['muted']};font-size:10px;")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("toolBtn")
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.close)
        cr = QHBoxLayout()
        cr.addStretch()
        cr.addWidget(close_btn)
        root.addLayout(cr)

    def _make_card(self, key: str, label: str, credits: int,
                   cents: int, popular: bool) -> QFrame:
        dollars = cents / 100
        border  = C["yellow"] if popular else C["border"]
        frame   = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:1px solid {border};"
            f"border-radius:10px;"
        )
        v = QVBoxLayout(frame)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(6)

        if popular:
            pop = QLabel("⭐  MOST POPULAR")
            pop.setStyleSheet(
                f"color:{C['yellow']};font-size:8px;font-weight:700;"
                f"letter-spacing:2px;")
            v.addWidget(pop)

        name_lbl = QLabel(label)
        name_lbl.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:13px;font-weight:800;"
            f"letter-spacing:2px;color:{C['text']};")
        v.addWidget(name_lbl)

        credits_lbl = QLabel("◊  " + f"{credits:,}")
        credits_lbl.setStyleSheet(
            f"font-size:20px;font-weight:700;color:{C['green']};")
        v.addWidget(credits_lbl)

        price_row = QHBoxLayout()
        price_lbl = QLabel(f"${dollars:.2f}")
        price_lbl.setStyleSheet(
            f"font-size:15px;color:{C['text']};font-weight:600;")
        rate_lbl  = QLabel(
            f"  ${cents / credits / 100:.4f} / credit")
        rate_lbl.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        price_row.addWidget(price_lbl)
        price_row.addWidget(rate_lbl)
        price_row.addStretch()
        price_w = QWidget()
        price_w.setLayout(price_row)
        price_w.setStyleSheet("background:transparent;")
        v.addWidget(price_w)

        v.addSpacing(4)

        buy_btn = QPushButton(f"Buy  →  ${dollars:.2f}")
        buy_btn.setObjectName("addBotBtn" if popular else "toolBtn")
        buy_btn.setFixedHeight(36)
        buy_btn.clicked.connect(lambda _, k=key: self._buy(k))
        v.addWidget(buy_btn)

        self._buy_btns[key] = buy_btn
        return frame

    # ── actions ──────────────────────────────────────────────────

    def _buy(self, package_key: str):
        server_url, token = self._auth()
        if not token:
            self._set_status("⚠  Not signed in — log in first.", C["red"])
            return
        for btn in self._buy_btns.values():
            btn.setEnabled(False)
        self._set_status("Creating checkout session…", C["muted"])

        w = _CheckoutWorker(server_url, token, package_key)
        w.result.connect(self._on_checkout)
        w.start()
        self._workers.append(w)

    def _on_checkout(self, url: str, error: str):
        for btn in self._buy_btns.values():
            btn.setEnabled(True)
        if error:
            self._set_status("⚠  " + error, C["red"])
            return
        if url:
            webbrowser.open(url)
            self._set_status(
                "✓ Browser opened — complete your payment there. "
                "Click ↻ to refresh your balance once done.",
                C["green"],
            )

    def _refresh_balance(self):
        server_url, token = self._auth()
        if not token:
            return
        self._bal_lbl.setText("◊  …")
        w = _BalanceWorker(server_url, token)
        w.result.connect(self._on_balance)
        w.start()
        self._workers.append(w)

    def _on_balance(self, balance: int, error: str):
        if balance >= 0:
            self._balance = balance
            self._bal_lbl.setText("◊  " + f"{balance:,}")
            self._bal_lbl.setStyleSheet(
                f"color:{C['yellow']};font-size:13px;font-weight:700;")
            self.balance_refreshed.emit(balance)
        else:
            self._bal_lbl.setText("◊  —")

    def _set_status(self, text: str, color: str):
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color:{color};font-size:10px;")

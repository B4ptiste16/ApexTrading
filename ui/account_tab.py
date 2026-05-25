"""
APEX · Account tab  (V3 wave 4)
─────────────────────────────────────────────────────────────────────
Edit display name + phone, change password, re-verify email, see the
current verification state. All calls go to /auth/* on the APEX server.

Profile-picture upload + theme selection arrive in a follow-up wave.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QGridLayout, QMessageBox,
)

from ui.styles  import COLORS
from ui.widgets import ScrollContent, SectionHeader, NoScrollComboBox

C = COLORS


class _HttpWorker(QThread):
    done = pyqtSignal(bool, dict)

    def __init__(self, method: str, path: str,
                 payload: Optional[dict] = None):
        super().__init__()
        self.method = method
        self.path = path
        self.payload = payload

    def run(self):
        import requests
        from ui.login import load_auth, load_server_url
        tok = (load_auth() or {}).get("token") or ""
        if not tok:
            self.done.emit(False, {"detail": "Sign in first."})
            return
        try:
            fn = {"GET":  requests.get,
                  "POST": requests.post,
                  "PUT":  requests.put}.get(self.method, requests.get)
            r = fn(f"{load_server_url()}{self.path}",
                   headers={"Authorization": f"Bearer {tok}"},
                   json=self.payload if self.method in ("POST", "PUT") else None,
                   timeout=15)
            try:
                body = r.json()
            except Exception:
                body = {"text": r.text}
            self.done.emit(r.ok, body)
        except Exception as e:
            self.done.emit(False, {"detail": str(e)})


class AccountTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._workers: list = []
        self._me: dict = {}
        self._build()
        QTimer.singleShot(300, self.refresh)

    def refresh(self):
        w = _HttpWorker("GET", "/auth/me")
        self._spawn(w, self._on_me_loaded)

    # ── Build ──────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        # ─── HEADER (display) ────────────────────────────────────
        s.add(SectionHeader("ACCOUNT", C["purple"]))
        self._summary = self._build_summary_card()
        s.add(self._summary)

        # ─── PROFILE EDITOR ──────────────────────────────────────
        s.add(SectionHeader("PROFILE", C["green"]))
        s.add(self._build_profile_form())

        # ─── APPEARANCE (V3.1.6) ─────────────────────────────────
        s.add(SectionHeader("APPEARANCE", C["purple"]))
        s.add(self._build_theme_picker())

        # ─── PASSWORD ────────────────────────────────────────────
        s.add(SectionHeader("PASSWORD", C["yellow"]))
        s.add(self._build_password_form())

        # ─── EMAIL ───────────────────────────────────────────────
        s.add(SectionHeader("CHANGE EMAIL", C["orange"]))
        s.add(self._build_email_form())

        s.add_stretch()

    def _build_summary_card(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        g = QGridLayout(frame)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(20)
        g.setVerticalSpacing(8)

        def k(text: str) -> QLabel:
            l = QLabel(text)
            l.setStyleSheet(
                f"color:{C['muted']};font-size:9px;letter-spacing:2px;"
                f"font-weight:700;")
            return l

        def v() -> QLabel:
            l = QLabel("—")
            l.setStyleSheet(f"color:{C['text']};font-size:12px;")
            return l

        self._sum_username = v()
        self._sum_email    = v()
        self._sum_display  = v()
        self._sum_phone    = v()
        self._sum_verified = v()
        self._sum_google   = v()

        g.addWidget(k("USERNAME"),     0, 0); g.addWidget(self._sum_username, 0, 1)
        g.addWidget(k("EMAIL"),        1, 0); g.addWidget(self._sum_email,    1, 1)
        g.addWidget(k("DISPLAY NAME"), 2, 0); g.addWidget(self._sum_display,  2, 1)
        g.addWidget(k("PHONE"),        3, 0); g.addWidget(self._sum_phone,    3, 1)
        g.addWidget(k("VERIFIED"),     4, 0); g.addWidget(self._sum_verified, 4, 1)
        g.addWidget(k("GOOGLE LINKED"),5, 0); g.addWidget(self._sum_google,   5, 1)

        verify_row = QHBoxLayout()
        self._resend_btn = QPushButton("📧  Resend verification email")
        self._resend_btn.setObjectName("toolBtn")
        self._resend_btn.clicked.connect(self._resend_verification)
        self._verify_msg = QLabel("")
        self._verify_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        verify_row.addWidget(self._resend_btn)
        verify_row.addWidget(self._verify_msg)
        verify_row.addStretch()
        vw = QWidget(); vw.setLayout(verify_row)
        g.addWidget(vw, 6, 0, 1, 2)
        return frame

    def _build_profile_form(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        g = QGridLayout(frame)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        g.addWidget(self._lbl("Display name"), 0, 0)
        self._edit_display = QLineEdit()
        self._edit_display.setStyleSheet(self._input_css())
        g.addWidget(self._edit_display, 0, 1)

        g.addWidget(self._lbl("Phone (optional)"), 1, 0)
        self._edit_phone = QLineEdit()
        self._edit_phone.setStyleSheet(self._input_css())
        self._edit_phone.setPlaceholderText("e.g. +33 6 12 34 56 78")
        g.addWidget(self._edit_phone, 1, 1)

        save_row = QHBoxLayout()
        save = QPushButton("💾  Save profile")
        save.setObjectName("addBotBtn")
        save.clicked.connect(self._save_profile)
        self._profile_msg = QLabel("")
        self._profile_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        save_row.addWidget(save)
        save_row.addWidget(self._profile_msg)
        save_row.addStretch()
        sw = QWidget(); sw.setLayout(save_row)
        g.addWidget(sw, 2, 0, 1, 2)
        return frame

    def _build_theme_picker(self) -> QFrame:
        from ui.styles import THEMES
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        g = QGridLayout(frame)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        intro = QLabel(
            "Pick a colour palette. Applies live to most UI elements. "
            "A few card backgrounds refresh fully on next launch.")
        intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        intro.setWordWrap(True)
        g.addWidget(intro, 0, 0, 1, 2)

        g.addWidget(self._lbl("Theme"), 1, 0)
        self._theme_combo = NoScrollComboBox()
        for name in THEMES:
            self._theme_combo.addItem(name)
        # Pre-select current
        try:
            from core import data as _D
            current = _D.load_settings().get("theme", "Default (dark teal)")
            idx = self._theme_combo.findText(current)
            if idx >= 0:
                self._theme_combo.setCurrentIndex(idx)
        except Exception:
            pass
        g.addWidget(self._theme_combo, 1, 1)

        # Inline preview row — 5 swatches showing the colours of the
        # currently-selected theme so you can see what you're picking.
        self._theme_preview = QHBoxLayout()
        self._theme_preview.setContentsMargins(0, 4, 0, 0)
        self._theme_preview.setSpacing(6)
        pw = QWidget(); pw.setLayout(self._theme_preview)
        g.addWidget(self._lbl("Preview"), 2, 0)
        g.addWidget(pw, 2, 1)
        self._refresh_theme_preview()
        self._theme_combo.currentTextChanged.connect(
            lambda _t: self._refresh_theme_preview())

        row = QHBoxLayout()
        btn = QPushButton("🎨  Apply theme")
        btn.setObjectName("toolBtn")
        btn.clicked.connect(self._apply_theme)
        self._theme_msg = QLabel("")
        self._theme_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        row.addWidget(btn)
        row.addWidget(self._theme_msg)
        row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        g.addWidget(rw, 3, 0, 1, 2)
        return frame

    def _refresh_theme_preview(self):
        # Clear
        while self._theme_preview.count():
            item = self._theme_preview.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        # Build new swatches
        from ui.styles import THEMES
        name = self._theme_combo.currentText()
        palette = THEMES.get(name, {})
        for key in ("bg", "panel", "green", "red", "orange", "purple", "yellow"):
            sw = QLabel()
            sw.setFixedSize(22, 22)
            sw.setStyleSheet(
                f"background:{palette.get(key, '#000')};"
                f"border:none;border-radius:4px;")
            sw.setToolTip(f"{key}: {palette.get(key, '?')}")
            self._theme_preview.addWidget(sw)
        self._theme_preview.addStretch()

    def _apply_theme(self):
        name = self._theme_combo.currentText()
        try:
            from core import data as _D
            s = _D.load_settings()
            s["theme"] = name
            import json as _json
            with open(_D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _json.dump(s, f, indent=2)
        except Exception as e:
            self._theme_msg.setText(f"Save failed: {e}")
            self._theme_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return
        # Apply live: rebuild stylesheet from new palette and push to QApplication
        try:
            from ui.styles import apply_theme as _at
            from PyQt6.QtWidgets import QApplication as _QApp
            new_sheet = _at(name)
            app = _QApp.instance()
            if app:
                app.setStyleSheet(new_sheet)
        except Exception as e:
            print(f"[theme] live apply failed: {e}")
        self._theme_msg.setText(f"✓ {name} applied")
        self._theme_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        QTimer.singleShot(4000, lambda: self._theme_msg.setText(""))

    def _build_password_form(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        g = QGridLayout(frame)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        g.addWidget(self._lbl("Current password"), 0, 0)
        self._edit_pw_cur = QLineEdit()
        self._edit_pw_cur.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_pw_cur.setStyleSheet(self._input_css())
        g.addWidget(self._edit_pw_cur, 0, 1)

        g.addWidget(self._lbl("New password"), 1, 0)
        self._edit_pw_new = QLineEdit()
        self._edit_pw_new.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_pw_new.setStyleSheet(self._input_css())
        g.addWidget(self._edit_pw_new, 1, 1)

        g.addWidget(self._lbl("Confirm new"), 2, 0)
        self._edit_pw_conf = QLineEdit()
        self._edit_pw_conf.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_pw_conf.setStyleSheet(self._input_css())
        g.addWidget(self._edit_pw_conf, 2, 1)

        row = QHBoxLayout()
        btn = QPushButton("🔒  Change password")
        btn.setObjectName("toolBtn")
        btn.clicked.connect(self._change_password)
        self._password_msg = QLabel("")
        self._password_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        row.addWidget(btn)
        row.addWidget(self._password_msg)
        row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        g.addWidget(rw, 3, 0, 1, 2)
        return frame

    def _build_email_form(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        g = QGridLayout(frame)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        info = QLabel("Changing your email address resets verification "
                       "— you'll need to confirm the new one.")
        info.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        info.setWordWrap(True)
        g.addWidget(info, 0, 0, 1, 2)

        g.addWidget(self._lbl("New email"), 1, 0)
        self._edit_new_email = QLineEdit()
        self._edit_new_email.setStyleSheet(self._input_css())
        g.addWidget(self._edit_new_email, 1, 1)

        row = QHBoxLayout()
        btn = QPushButton("✉  Change email")
        btn.setObjectName("toolBtn")
        btn.clicked.connect(self._change_email)
        self._email_msg = QLabel("")
        self._email_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        row.addWidget(btn)
        row.addWidget(self._email_msg)
        row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        g.addWidget(rw, 2, 0, 1, 2)
        return frame

    def _lbl(self, text: str) -> QLabel:
        w = QLabel(text)
        w.setStyleSheet(f"color:{C['text']};font-size:11px;")
        return w

    def _input_css(self) -> str:
        return (
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:5px;"
            f"padding:6px 10px;font-family:'JetBrains Mono';font-size:11px;"
        )

    # ── Worker plumbing ────────────────────────────────────────────

    def _spawn(self, worker: _HttpWorker, on_done):
        worker.done.connect(on_done)
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    # ── Load /auth/me ──────────────────────────────────────────────

    def _on_me_loaded(self, ok: bool, body: dict):
        if not ok:
            return
        self._me = body
        self._sum_username.setText(body.get("username", "—"))
        self._sum_email.setText(body.get("email", "—"))
        self._sum_display.setText(body.get("display_name", "—"))
        self._sum_phone.setText(body.get("phone") or "—")
        verified = bool(body.get("is_verified"))
        self._sum_verified.setText("✓  yes" if verified else "✗  no")
        self._sum_verified.setStyleSheet(
            f"color:{C['green'] if verified else C['red']};font-size:12px;")
        self._sum_google.setText("✓  yes" if body.get("google_sub") else "—")

        # Pre-fill editable fields
        if not self._edit_display.text():
            self._edit_display.setText(body.get("display_name", ""))
        if not self._edit_phone.text():
            self._edit_phone.setText(body.get("phone") or "")

        # Hide verification button if already verified
        self._resend_btn.setVisible(not verified)

    # ── Actions ────────────────────────────────────────────────────

    def _resend_verification(self):
        self._verify_msg.setText("Sending…")
        self._verify_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        w = _HttpWorker("POST", "/auth/resend-verification")
        self._spawn(w, self._on_verify_resent)

    def _on_verify_resent(self, ok: bool, body: dict):
        if not ok:
            self._verify_msg.setText(body.get("detail", "Failed."))
            self._verify_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return
        if body.get("auto_verified"):
            self._verify_msg.setText("✓ Auto-verified (no email service configured).")
        elif body.get("already_verified"):
            self._verify_msg.setText("Already verified.")
        else:
            self._verify_msg.setText(f"✓ Sent to {body.get('sent_to')}")
        self._verify_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        QTimer.singleShot(500, self.refresh)

    def _save_profile(self):
        patch = {
            "display_name": self._edit_display.text().strip(),
            "phone":        self._edit_phone.text().strip(),
        }
        self._profile_msg.setText("Saving…")
        self._profile_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        w = _HttpWorker("PUT", "/auth/profile", payload=patch)
        self._spawn(w, self._on_profile_saved)

    def _on_profile_saved(self, ok: bool, body: dict):
        if ok:
            self._profile_msg.setText("Saved ✓")
            self._profile_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            self.refresh()
        else:
            self._profile_msg.setText(body.get("detail", "Save failed."))
            self._profile_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
        QTimer.singleShot(3000, lambda: self._profile_msg.setText(""))

    def _change_password(self):
        cur  = self._edit_pw_cur.text()
        new  = self._edit_pw_new.text()
        conf = self._edit_pw_conf.text()
        if len(new) < 8:
            self._password_msg.setText("Need at least 8 characters.")
            self._password_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return
        if new != conf:
            self._password_msg.setText("Passwords don't match.")
            self._password_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return
        self._password_msg.setText("Saving…")
        self._password_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        w = _HttpWorker("PUT", "/auth/password",
                        payload={"current_password": cur, "new_password": new})
        self._spawn(w, self._on_password_saved)

    def _on_password_saved(self, ok: bool, body: dict):
        if ok:
            if body.get("first_password"):
                self._password_msg.setText("Password set ✓ (Google-only account)")
            else:
                self._password_msg.setText("Password changed ✓")
            self._password_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            self._edit_pw_cur.clear()
            self._edit_pw_new.clear()
            self._edit_pw_conf.clear()
        else:
            self._password_msg.setText(body.get("detail", "Failed."))
            self._password_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
        QTimer.singleShot(4000, lambda: self._password_msg.setText(""))

    def _change_email(self):
        new = self._edit_new_email.text().strip().lower()
        if not new or "@" not in new:
            self._email_msg.setText("Invalid email.")
            self._email_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
            return
        if QMessageBox.question(
                self, "Change email",
                f"Change your email to {new}? You'll need to verify the new "
                f"address before it's active.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._email_msg.setText("Saving…")
        self._email_msg.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        w = _HttpWorker("PUT", "/auth/email", payload={"new_email": new})
        self._spawn(w, self._on_email_changed)

    def _on_email_changed(self, ok: bool, body: dict):
        if ok:
            msg = "Email changed ✓"
            if body.get("verification_sent"):
                msg += " — verification email sent"
            self._email_msg.setText(msg)
            self._email_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
            self._edit_new_email.clear()
            self.refresh()
        else:
            self._email_msg.setText(body.get("detail", "Failed."))
            self._email_msg.setStyleSheet(f"color:{C['red']};font-size:10px;")
        QTimer.singleShot(4000, lambda: self._email_msg.setText(""))

"""
APEX · Admin tab  (V3 wave 5)
─────────────────────────────────────────────────────────────────────
Only visible when the signed-in user has role ADMIN, SUB_BOSS_ADMIN
or BOSS_ADMIN. Lets you:

  • see every user with their email, role, and credit balance
  • grant or debit credits from any user
  • (BOSS_ADMIN only) change a user's role

If the signed-in user has role USER, the tab refuses to render and
displays a "not authorized" message.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QGridLayout, QMessageBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QInputDialog,
)
from PyQt6.QtGui import QColor

from ui.styles  import COLORS
from ui.widgets import ScrollContent, SectionHeader, NoScrollComboBox

C = COLORS


_ROLES = ["USER", "ADMIN", "SUB_BOSS_ADMIN", "BOSS_ADMIN"]


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


class AdminTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._workers: list = []
        self._my_role = "USER"
        self._users: list = []
        self._build()
        QTimer.singleShot(300, self.refresh)

    def refresh(self):
        """Always re-pull /auth/me first to confirm the caller is still
        an admin (handles the case where someone was demoted)."""
        w = _HttpWorker("GET", "/auth/me")
        self._spawn(w, self._on_me_loaded)

    def _build(self):
        s = self.scroll

        s.add(SectionHeader("ADMIN DASHBOARD", C["red"]))
        self._gate_msg = QLabel("")
        self._gate_msg.setStyleSheet(f"color:{C['red']};font-size:13px;padding:8px 0;")
        self._gate_msg.setWordWrap(True)
        s.add(self._gate_msg)

        self._role_lbl = QLabel("")
        self._role_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:11px;letter-spacing:2px;")
        s.add(self._role_lbl)

        # ─── USERS TABLE ─────────────────────────────────────
        self._users_header = SectionHeader("USERS", C["purple"])
        s.add(self._users_header)

        self._users_table = QTableWidget(0, 6)
        self._users_table.setHorizontalHeaderLabels(
            ["ID", "Username", "Email", "Display", "Role", "Credits"])
        self._users_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self._users_table.horizontalHeader().setStretchLastSection(True)
        self._users_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._users_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._users_table.setStyleSheet(
            f"background:{C['panel']};color:{C['text']};"
            f"border:none;border-radius:6px;"
            f"gridline-color:{C['border']};font-size:11px;")
        self._users_table.setFixedHeight(280)
        s.add(self._users_table)

        # ─── GRANT CREDITS ───────────────────────────────────
        self._grant_section = SectionHeader("GRANT / DEBIT CREDITS", C["yellow"])
        s.add(self._grant_section)
        grant_frame = QFrame()
        grant_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        g = QGridLayout(grant_frame)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)

        g.addWidget(self._lbl("Target username"), 0, 0)
        self._grant_user_edit = QLineEdit()
        self._grant_user_edit.setStyleSheet(self._input_css())
        self._grant_user_edit.setPlaceholderText("@username")
        g.addWidget(self._grant_user_edit, 0, 1)

        g.addWidget(self._lbl("Δ credits"), 1, 0)
        self._grant_delta = QSpinBox()
        self._grant_delta.setRange(-1_000_000, 1_000_000)
        self._grant_delta.setValue(100)
        self._grant_delta.setSingleStep(10)
        self._grant_delta.setFixedWidth(140)
        g.addWidget(self._grant_delta, 1, 1)

        g.addWidget(self._lbl("Reason"), 2, 0)
        self._grant_reason = QLineEdit()
        self._grant_reason.setStyleSheet(self._input_css())
        self._grant_reason.setPlaceholderText("e.g. signup bonus, refund, …")
        g.addWidget(self._grant_reason, 2, 1)

        row = QHBoxLayout()
        btn = QPushButton("💰  Grant")
        btn.setObjectName("addBotBtn")
        btn.clicked.connect(self._do_grant)
        self._grant_msg = QLabel("")
        self._grant_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        row.addWidget(btn); row.addWidget(self._grant_msg); row.addStretch()
        rw = QWidget(); rw.setLayout(row)
        g.addWidget(rw, 3, 0, 1, 2)

        self._grant_section_frame = grant_frame
        s.add(grant_frame)

        # ─── ROLE ASSIGNMENT (BOSS_ADMIN only) ──────────────
        self._role_section = SectionHeader("ASSIGN ROLE  (BOSS_ADMIN only)", C["orange"])
        s.add(self._role_section)
        role_frame = QFrame()
        role_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        rg = QGridLayout(role_frame)
        rg.setContentsMargins(16, 14, 16, 14)
        rg.setHorizontalSpacing(12)
        rg.setVerticalSpacing(10)

        rg.addWidget(self._lbl("Target user ID"), 0, 0)
        self._role_user_id = QSpinBox()
        self._role_user_id.setRange(1, 999_999)
        self._role_user_id.setFixedWidth(140)
        rg.addWidget(self._role_user_id, 0, 1)

        rg.addWidget(self._lbl("New role"), 1, 0)
        self._role_combo = NoScrollComboBox()
        for r in _ROLES:
            self._role_combo.addItem(r)
        rg.addWidget(self._role_combo, 1, 1)

        role_row = QHBoxLayout()
        role_btn = QPushButton("👑  Apply role")
        role_btn.setObjectName("toolBtn")
        role_btn.clicked.connect(self._do_role)
        self._role_msg = QLabel("")
        self._role_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        role_row.addWidget(role_btn); role_row.addWidget(self._role_msg); role_row.addStretch()
        rrw = QWidget(); rrw.setLayout(role_row)
        rg.addWidget(rrw, 2, 0, 1, 2)

        self._role_section_frame = role_frame
        s.add(role_frame)

        # ─── BOT CLASSIFY (V3.1.7) ────────────────────────────
        self._classify_section = SectionHeader(
            "FEATURE / RECOMMEND BOTS", C["green"])
        s.add(self._classify_section)
        classify_frame = QFrame()
        classify_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        cf = QGridLayout(classify_frame)
        cf.setContentsMargins(16, 14, 16, 14)
        cf.setHorizontalSpacing(12)
        cf.setVerticalSpacing(10)

        intro = QLabel(
            "Toggle the ⭐ FEATURED or ✦ RECOMMENDED flags on any bot in "
            "the marketplace. Picks show up at the top of the BOT MARKET "
            "tab for every user.")
        intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        intro.setWordWrap(True)
        cf.addWidget(intro, 0, 0, 1, 3)

        cf.addWidget(self._lbl("Bot slug"), 1, 0)
        self._classify_slug = QLineEdit()
        self._classify_slug.setStyleSheet(self._input_css())
        self._classify_slug.setPlaceholderText("e.g. spy-momentum")
        cf.addWidget(self._classify_slug, 1, 1, 1, 2)

        feat_btn = QPushButton("⭐  Toggle FEATURED")
        feat_btn.setObjectName("toolBtn")
        feat_btn.clicked.connect(lambda: self._do_classify("featured"))
        cf.addWidget(feat_btn, 2, 0)

        reco_btn = QPushButton("✦  Toggle RECOMMENDED")
        reco_btn.setObjectName("toolBtn")
        reco_btn.clicked.connect(lambda: self._do_classify("recommended"))
        cf.addWidget(reco_btn, 2, 1)

        self._classify_msg = QLabel("")
        self._classify_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        cf.addWidget(self._classify_msg, 3, 0, 1, 3)
        self._classify_section_frame = classify_frame
        s.add(classify_frame)

        # ─── MODERATION (V3.3.0) ──────────────────────────────
        self._mod_section = SectionHeader("MODERATION", C["red"])
        s.add(self._mod_section)
        mod_frame = QFrame()
        mod_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;border-left:3px solid {C['red']};")
        mf = QGridLayout(mod_frame)
        mf.setContentsMargins(16, 14, 16, 14)
        mf.setHorizontalSpacing(12)
        mf.setVerticalSpacing(10)

        mod_intro = QLabel(
            "<b>FLAG</b> hides a bot from the marketplace listings but "
            "leaves existing installs untouched.<br>"
            "<b>REMOVE</b> deletes the bot, refunds every buyer the "
            "credits they paid, fines the publisher 500 ◊, and bans "
            "them from publishing new bots for 7 days. Existing approved "
            "bots from the same publisher stay live.")
        mod_intro.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.6;")
        mod_intro.setWordWrap(True)
        mf.addWidget(mod_intro, 0, 0, 1, 3)

        mf.addWidget(self._lbl("Bot slug"), 1, 0)
        self._mod_slug = QLineEdit()
        self._mod_slug.setStyleSheet(self._input_css())
        self._mod_slug.setPlaceholderText("e.g. spy-momentum")
        mf.addWidget(self._mod_slug, 1, 1, 1, 2)

        mf.addWidget(self._lbl("Reason"), 2, 0)
        self._mod_reason = QLineEdit()
        self._mod_reason.setStyleSheet(self._input_css())
        self._mod_reason.setPlaceholderText(
            "Why is this bot being moderated?")
        mf.addWidget(self._mod_reason, 2, 1, 1, 2)

        btn_row = QHBoxLayout()
        flag_btn = QPushButton("🚩  Flag (hide)")
        flag_btn.setObjectName("toolBtn")
        flag_btn.clicked.connect(lambda: self._do_moderate("flag"))
        unflag_btn = QPushButton("✓  Unflag")
        unflag_btn.setObjectName("toolBtn")
        unflag_btn.clicked.connect(lambda: self._do_moderate("unflag"))
        remove_btn = QPushButton("⚠  Remove + refund")
        remove_btn.setObjectName("dangerBtn")
        remove_btn.clicked.connect(lambda: self._do_moderate("remove"))
        btn_row.addWidget(flag_btn)
        btn_row.addWidget(unflag_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        brw = QWidget(); brw.setLayout(btn_row)
        mf.addWidget(brw, 3, 0, 1, 3)

        self._mod_msg = QLabel("")
        self._mod_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        self._mod_msg.setWordWrap(True)
        mf.addWidget(self._mod_msg, 4, 0, 1, 3)
        self._mod_section_frame = mod_frame
        s.add(mod_frame)

        # ─── REVENUE SPLIT (V4.0.0 — BOSS_ADMIN only) ─────────
        self._split_section = SectionHeader(
            "REVENUE SPLIT  ·  BOSS_ADMIN only", C["purple"])
        s.add(self._split_section)
        split_frame = QFrame()
        split_frame.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;border-left:3px solid {C['purple']};")
        sf = QGridLayout(split_frame)
        sf.setContentsMargins(16, 14, 16, 14)
        sf.setHorizontalSpacing(12)
        sf.setVerticalSpacing(10)

        split_intro = QLabel(
            "Every paid bot sale gets split three ways. "
            "<b>SELLER %</b> goes to the bot's publisher; "
            "<b>ADMIN %</b> is split equally among every user with an "
            "admin role; <b>RUNNING %</b> goes to a virtual platform "
            "balance that covers Anthropic / Gemini keys + server "
            "costs. Total must = 100 %.")
        split_intro.setStyleSheet(f"color:{C['muted']};font-size:11px;line-height:1.6;")
        split_intro.setWordWrap(True)
        sf.addWidget(split_intro, 0, 0, 1, 4)

        for col, (label, key) in enumerate(
                (("SELLER %", "seller"),
                 ("ADMIN %",  "admin"),
                 ("RUNNING %", "running"))):
            sf.addWidget(self._lbl(label), 1, col)
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setSuffix(" %")
            spin.setFixedWidth(100)
            setattr(self, f"_split_{key}_spin", spin)
            sf.addWidget(spin, 2, col)

        self._running_balance_lbl = QLabel("Running balance: —")
        self._running_balance_lbl.setStyleSheet(
            f"color:{C['yellow']};font-size:11px;font-weight:700;")
        sf.addWidget(self._running_balance_lbl, 3, 0, 1, 3)

        split_row = QHBoxLayout()
        load_btn = QPushButton("⟳  Load current")
        load_btn.setObjectName("toolBtn")
        load_btn.clicked.connect(self._load_revenue_split)
        save_btn = QPushButton("💾  Save split")
        save_btn.setObjectName("addBotBtn")
        save_btn.clicked.connect(self._save_revenue_split)
        self._split_msg = QLabel("")
        self._split_msg.setStyleSheet(f"color:{C['green']};font-size:10px;")
        split_row.addWidget(load_btn)
        split_row.addWidget(save_btn)
        split_row.addWidget(self._split_msg)
        split_row.addStretch()
        srw = QWidget(); srw.setLayout(split_row)
        sf.addWidget(srw, 4, 0, 1, 4)

        self._split_section_frame = split_frame
        s.add(split_frame)

        s.add_stretch()

        # Default: hide everything until /auth/me confirms admin
        self._set_admin_visible(False, role="USER")

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

    def _spawn(self, w: _HttpWorker, on_done):
        w.done.connect(on_done)
        w.finished.connect(lambda _w=w: self._workers.remove(_w)
                                          if _w in self._workers else None)
        self._workers.append(w)
        w.start()

    # ── /auth/me check ─────────────────────────────────────

    def _on_me_loaded(self, ok: bool, body: dict):
        role = body.get("role", "USER") if ok else "USER"
        self._my_role = role
        is_admin = role in ("ADMIN", "SUB_BOSS_ADMIN", "BOSS_ADMIN")
        self._set_admin_visible(is_admin, role=role)
        if is_admin:
            self._refresh_users()

    def _set_admin_visible(self, visible: bool, role: str):
        if not visible:
            self._gate_msg.setText(
                f"❌  This tab is admin-only. Your role: {role}.")
            self._gate_msg.setVisible(True)
        else:
            self._gate_msg.setVisible(False)
        self._role_lbl.setText(f"YOUR ROLE: {role}")
        for w in (self._users_table, self._grant_section_frame,
                  self._classify_section_frame, self._mod_section_frame):
            w.setVisible(visible)
        # Role assignment + revenue split are BOSS_ADMIN-only
        boss = role == "BOSS_ADMIN"
        self._role_section_frame.setVisible(boss)
        if hasattr(self, "_split_section_frame"):
            self._split_section_frame.setVisible(boss)
        if boss:
            self._load_revenue_split()

    # ── Users table ────────────────────────────────────────

    def _refresh_users(self):
        w = _HttpWorker("GET", "/admin/users")
        self._spawn(w, self._on_users_loaded)

    def _on_users_loaded(self, ok: bool, body: dict):
        if not ok:
            return
        users = body.get("users", [])
        self._users = users
        self._users_table.setRowCount(len(users))
        for r, u in enumerate(users):
            for col, val in enumerate((
                u.get("id"), u.get("username"), u.get("email"),
                u.get("display_name"), u.get("role", "USER"),
                u.get("credit_balance", 0),
            )):
                item = QTableWidgetItem(str(val if val is not None else ""))
                if col == 4 and val and val != "USER":
                    item.setForeground(QColor(C["red"]))
                if col == 5:
                    item.setForeground(QColor(C["yellow"]))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self._users_table.setItem(r, col, item)
        self._users_table.resizeColumnsToContents()

    # ── Actions ────────────────────────────────────────────

    def _do_grant(self):
        uname  = self._grant_user_edit.text().strip().lower().lstrip("@")
        delta  = int(self._grant_delta.value())
        reason = self._grant_reason.text().strip() or "admin grant"
        if not uname:
            self._toast(self._grant_msg, "Enter a target username.", err=True)
            return
        target = next((u for u in self._users
                       if (u.get("username") or "").lower() == uname), None)
        if not target:
            self._toast(self._grant_msg,
                        f"@{uname} not found in the user list.", err=True)
            return
        if delta == 0:
            self._toast(self._grant_msg, "Delta cannot be zero.", err=True)
            return
        self._toast(self._grant_msg, "Granting…", err=False, transient=False)
        w = _HttpWorker("POST", "/admin/credits/grant",
                        payload={"user_id": target["id"], "delta": delta,
                                  "reason": reason})
        self._spawn(w, self._on_grant_done)

    def _on_grant_done(self, ok: bool, body: dict):
        if not ok:
            self._toast(self._grant_msg, body.get("detail", "Failed."), err=True)
            return
        self._toast(self._grant_msg,
                    f"✓ New balance: {body.get('balance', '?')}")
        self._refresh_users()

    def _do_role(self):
        target_id = int(self._role_user_id.value())
        role      = self._role_combo.currentText()
        if QMessageBox.question(
                self, "Apply role",
                f"Set user_id={target_id} to role {role}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        w = _HttpWorker("PUT", f"/admin/users/{target_id}/role",
                        payload={"role": role})
        self._spawn(w, self._on_role_done)

    def _on_role_done(self, ok: bool, body: dict):
        if not ok:
            self._toast(self._role_msg, body.get("detail", "Failed."), err=True)
            return
        self._toast(self._role_msg, f"✓ Role applied.")
        self._refresh_users()

    def _do_classify(self, flag: str):
        """Toggle the featured/recommended flag on a bot. We don't know
        the bot's current state from here, so we fetch it first → flip
        → write back."""
        slug = self._classify_slug.text().strip()
        if not slug:
            self._toast(self._classify_msg, "Enter a bot slug.", err=True)
            return
        # First fetch the current state
        getter = _HttpWorker("GET", f"/bots/{slug}")
        self._spawn(getter, lambda ok, body:
                    self._classify_step2(ok, body, slug, flag))

    def _classify_step2(self, ok: bool, body: dict, slug: str, flag: str):
        if not ok:
            self._toast(self._classify_msg,
                        body.get("detail", "Bot not found."), err=True)
            return
        current = bool(body.get(flag, 0))
        new_val = not current
        worker = _HttpWorker("PUT", f"/admin/bots/{slug}/classify",
                              payload={flag: new_val})
        self._spawn(worker, lambda ok, body:
                    self._on_classify_done(ok, body, slug, flag, new_val))

    def _on_classify_done(self, ok: bool, body: dict, slug: str,
                          flag: str, new_val: bool):
        if not ok:
            self._toast(self._classify_msg,
                        body.get("detail", "Failed."), err=True)
            return
        state = "ON" if new_val else "OFF"
        self._toast(self._classify_msg,
                    f"✓ {slug} · {flag.upper()} is now {state}")

    # ── V3.3.0 — moderation ─────────────────────────────────

    def _do_moderate(self, action: str):
        slug   = self._mod_slug.text().strip()
        reason = self._mod_reason.text().strip()
        if not slug:
            self._toast(self._mod_msg, "Enter a bot slug.", err=True)
            return
        if action == "remove":
            if QMessageBox.question(
                self, "Remove + refund?",
                f"Hard-remove '{slug}'?\n\n"
                f"• Every buyer gets their credits refunded\n"
                f"• Every installed copy is revoked (deleted from "
                f"their library on next launch)\n"
                f"• Publisher fined 500 credits + 7-day publish ban\n\n"
                f"Other approved bots from the same publisher stay live.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return
        path = {
            "flag":   f"/admin/bots/{slug}/flag",
            "unflag": f"/admin/bots/{slug}/unflag",
            "remove": f"/admin/bots/{slug}/remove",
        }[action]
        payload = {"reason": reason} if action in ("flag", "remove") else {}
        w = _HttpWorker("POST", path, payload=payload)
        self._spawn(w, lambda ok, body, a=action:
                    self._on_moderate_done(ok, body, a, slug))

    def _on_moderate_done(self, ok: bool, body: dict, action: str, slug: str):
        if not ok:
            self._toast(self._mod_msg, body.get("detail", "Failed."), err=True)
            return
        if action == "remove":
            self._toast(
                self._mod_msg,
                f"✓ '{slug}' removed. {body.get('refunded_users', 0)} "
                f"users refunded {body.get('refunded_credits', 0)} ◊; "
                f"publisher fined {body.get('fine_credits', 0)} ◊ + "
                f"banned until {body.get('publish_ban_until', '?')[:10]}.")
        elif action == "flag":
            self._toast(self._mod_msg,
                        f"✓ '{slug}' flagged. Hidden from marketplace.")
        else:
            self._toast(self._mod_msg, f"✓ '{slug}' unflagged.")

    # ── V4.0.0 — Revenue split ──────────────────────────────

    def _load_revenue_split(self):
        w = _HttpWorker("GET", "/admin/revenue-split")
        self._spawn(w, self._on_split_loaded)

    def _on_split_loaded(self, ok: bool, body: dict):
        if not ok:
            self._toast(self._split_msg,
                        body.get("detail", "Failed."), err=True)
            return
        split = body.get("split", {})
        self._split_seller_spin.setValue(int(split.get("seller_pct", 90)))
        self._split_admin_spin.setValue(int(split.get("admin_pct", 5)))
        self._split_running_spin.setValue(int(split.get("running_pct", 5)))
        bal = body.get("running_balance", 0)
        self._running_balance_lbl.setText(
            f"Running balance: {int(bal):,} ◊  "
            f"(= ${int(bal)/100:,.2f} at 1 ◊ = $0.01)")

    def _save_revenue_split(self):
        seller  = int(self._split_seller_spin.value())
        admin   = int(self._split_admin_spin.value())
        running = int(self._split_running_spin.value())
        total = seller + admin + running
        if total != 100:
            self._toast(self._split_msg,
                        f"Must sum to 100 % (got {total})", err=True)
            return
        w = _HttpWorker("PUT", "/admin/revenue-split",
                        payload={"seller_pct":  seller,
                                  "admin_pct":   admin,
                                  "running_pct": running})
        self._spawn(w, self._on_split_saved)

    def _on_split_saved(self, ok: bool, body: dict):
        if not ok:
            self._toast(self._split_msg,
                        body.get("detail", "Failed."), err=True)
            return
        self._toast(self._split_msg, "✓ Split saved")

    def _toast(self, label: QLabel, msg: str, *, err: bool = False,
                transient: bool = True):
        label.setText(msg)
        label.setStyleSheet(
            f"color:{C['red'] if err else C['green']};font-size:10px;")
        if transient:
            QTimer.singleShot(4000, lambda: label.setText(""))

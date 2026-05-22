"""
APEX · Friends tab  (V3.0.0)
─────────────────────────────────────────────────────────────────────
Add friends by username, accept / decline pending requests, browse a
friend's shared profile, and configure what to share with friends vs.
publicly. All calls go to the APEX server's /friends/* and
/share-settings endpoints — see server/friends.py.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QGridLayout, QCheckBox, QMessageBox, QSizePolicy,
)

from ui.styles  import COLORS
from ui.widgets import ScrollContent, SectionHeader

C = COLORS


# ── HTTP worker (thin wrapper so the UI never blocks) ───────────────

class _HttpWorker(QThread):
    done = pyqtSignal(bool, dict)   # ok, parsed body

    def __init__(self, method: str, path: str,
                 payload: Optional[dict] = None, params: Optional[dict] = None):
        super().__init__()
        self.method  = method
        self.path    = path
        self.payload = payload
        self.params  = params

    def run(self):
        import requests
        from ui.login import load_auth, load_server_url
        tok = (load_auth() or {}).get("token") or ""
        if not tok:
            self.done.emit(False, {"detail": "Sign in first."})
            return
        url = f"{load_server_url()}{self.path}"
        try:
            fn = {"GET": requests.get,
                  "POST": requests.post,
                  "PUT": requests.put,
                  "DELETE": requests.delete}.get(self.method, requests.get)
            r = fn(url,
                   headers={"Authorization": f"Bearer {tok}"},
                   json=self.payload if self.method in ("POST", "PUT") else None,
                   params=self.params,
                   timeout=15)
            try:
                body = r.json()
            except Exception:
                body = {"text": r.text}
            self.done.emit(r.ok, body)
        except Exception as e:
            self.done.emit(False, {"detail": str(e)})


# ── Tab widget ──────────────────────────────────────────────────────

class FriendsTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._workers: list[QThread] = []
        self._settings_loaded = False
        self._build()
        # Defer first refresh so the tab paints before hitting the network
        QTimer.singleShot(300, self.refresh)

    def refresh(self):
        self._refresh_friends()
        self._refresh_settings()

    # ── Layout ──────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        # ─── SEARCH / ADD ────────────────────────────────────────
        s.add(SectionHeader("ADD A FRIEND", C["purple"]))
        intro = QLabel(
            "Search for an APEX user by username and send them a "
            "friend request. They have to enable 'Discoverable' under "
            "Sharing below to show up here.")
        intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        intro.setWordWrap(True)
        s.add(intro)

        search_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Username or display name…")
        self._search_edit.setStyleSheet(self._input_css())
        self._search_edit.returnPressed.connect(self._do_search)
        search_btn = QPushButton("Search")
        search_btn.setObjectName("toolBtn")
        search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self._search_edit)
        search_row.addWidget(search_btn)
        sw = QWidget(); sw.setLayout(search_row)
        s.add(sw)

        self._search_results = QWidget()
        self._search_layout  = QVBoxLayout(self._search_results)
        self._search_layout.setContentsMargins(0, 6, 0, 6)
        self._search_layout.setSpacing(6)
        self._search_status = QLabel("")
        self._search_status.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._search_layout.addWidget(self._search_status)
        s.add(self._search_results)

        # ─── INCOMING REQUESTS ───────────────────────────────────
        s.add(SectionHeader("INCOMING REQUESTS", C["yellow"]))
        self._incoming_box = QWidget()
        self._incoming_layout = QVBoxLayout(self._incoming_box)
        self._incoming_layout.setContentsMargins(0, 4, 0, 4)
        self._incoming_layout.setSpacing(6)
        self._incoming_empty = QLabel("No incoming requests.")
        self._incoming_empty.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._incoming_layout.addWidget(self._incoming_empty)
        s.add(self._incoming_box)

        # ─── FRIENDS LIST ────────────────────────────────────────
        s.add(SectionHeader("FRIENDS", C["green"]))
        self._accepted_box = QWidget()
        self._accepted_layout = QVBoxLayout(self._accepted_box)
        self._accepted_layout.setContentsMargins(0, 4, 0, 4)
        self._accepted_layout.setSpacing(6)
        self._accepted_empty = QLabel("You haven't added any friends yet.")
        self._accepted_empty.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._accepted_layout.addWidget(self._accepted_empty)
        s.add(self._accepted_box)

        # ─── OUTGOING (pending) ──────────────────────────────────
        s.add(SectionHeader("SENT REQUESTS  (pending)", C["muted"]))
        self._outgoing_box = QWidget()
        self._outgoing_layout = QVBoxLayout(self._outgoing_box)
        self._outgoing_layout.setContentsMargins(0, 4, 0, 4)
        self._outgoing_layout.setSpacing(6)
        self._outgoing_empty = QLabel("No outgoing requests.")
        self._outgoing_empty.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:4px 0;")
        self._outgoing_layout.addWidget(self._outgoing_empty)
        s.add(self._outgoing_box)

        # ─── SHARE SETTINGS ──────────────────────────────────────
        s.add(SectionHeader("SHARING", C["orange"]))
        share_intro = QLabel(
            "Choose what's visible to friends and what's visible to "
            "everyone. Everything is OFF by default — sharing is opt-in.")
        share_intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        share_intro.setWordWrap(True)
        s.add(share_intro)
        s.add(self._build_share_grid())

        s.add_stretch()

    def _build_share_grid(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        g = QGridLayout(frame)
        g.setContentsMargins(16, 12, 16, 12)
        g.setHorizontalSpacing(20)
        g.setVerticalSpacing(8)

        def _h(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color:{C['muted']};font-size:9px;letter-spacing:2px;"
                f"font-weight:700;")
            return lbl

        g.addWidget(_h(""),            0, 0)
        g.addWidget(_h("FRIENDS"),     0, 1)
        g.addWidget(_h("EVERYONE"),    0, 2)

        # Row 0: discoverable + bot-download permission (special, one column)
        self._chk_discoverable = QCheckBox("Discoverable in user search")
        self._chk_discoverable.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._chk_discoverable.stateChanged.connect(self._on_share_changed)
        g.addWidget(self._chk_discoverable, 1, 0, 1, 3)

        self._chk_allow_dl = QCheckBox("Allow friends to install bots from my library")
        self._chk_allow_dl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._chk_allow_dl.stateChanged.connect(self._on_share_changed)
        g.addWidget(self._chk_allow_dl, 2, 0, 1, 3)

        # Per-stat toggles — friends and public columns
        self._share_widgets: dict[str, dict[str, QCheckBox]] = {}
        rows = [
            ("broker",  "Broker(s) used (Alpaca, IBKR…)"),
            ("daily",   "Daily P/L"),
            ("monthly", "Monthly P/L"),
            ("yearly",  "Yearly P/L"),
            ("bots",    "Bot library (names + presence)"),
        ]
        row_idx = 3
        for key, label in rows:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
            g.addWidget(lbl, row_idx, 0)
            cb_friends = QCheckBox()
            cb_public  = QCheckBox()
            cb_friends.stateChanged.connect(self._on_share_changed)
            cb_public.stateChanged.connect(self._on_share_changed)
            g.addWidget(cb_friends, row_idx, 1, Qt.AlignmentFlag.AlignCenter)
            g.addWidget(cb_public,  row_idx, 2, Qt.AlignmentFlag.AlignCenter)
            self._share_widgets[key] = {
                "friends": cb_friends,
                "public":  cb_public,
            }
            row_idx += 1

        self._share_status = QLabel("")
        self._share_status.setStyleSheet(f"color:{C['green']};font-size:10px;")
        g.addWidget(self._share_status, row_idx, 0, 1, 3)

        return frame

    # ── Helpers ─────────────────────────────────────────────────────

    def _input_css(self) -> str:
        return (
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:5px;"
            f"padding:6px 10px;font-family:'JetBrains Mono';font-size:11px;"
        )

    def _spawn(self, w: _HttpWorker, on_done):
        w.done.connect(on_done)
        w.finished.connect(lambda _w=w: self._workers.remove(_w)
                                          if _w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _clear_layout(self, layout, keep_first: QWidget = None):
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            w = item.widget() if item else None
            if w and w is not keep_first:
                w.deleteLater()

    # ── Search ──────────────────────────────────────────────────────

    def _do_search(self):
        q = self._search_edit.text().strip()
        if len(q) < 2:
            self._search_status.setText("Type at least 2 characters.")
            return
        self._clear_layout(self._search_layout, keep_first=self._search_status)
        self._search_status.setText("Searching…")

        worker = _HttpWorker("GET", "/friends/search", params={"q": q})
        self._spawn(worker, self._on_search_done)

    def _on_search_done(self, ok: bool, body: dict):
        if not ok:
            self._search_status.setText(body.get("detail", "Search failed."))
            return
        users = body.get("users", [])
        if not users:
            self._search_status.setText(
                "No matches. They might not be discoverable.")
            return
        self._search_status.setText("")
        for u in users:
            self._search_layout.addWidget(self._make_user_row(u, action="add"))

    # ── Friends refresh ─────────────────────────────────────────────

    def _refresh_friends(self):
        worker = _HttpWorker("GET", "/friends")
        self._spawn(worker, self._on_friends_loaded)

    def _on_friends_loaded(self, ok: bool, body: dict):
        if not ok:
            return
        incoming = body.get("incoming", [])
        outgoing = body.get("outgoing", [])
        accepted = body.get("accepted", [])

        self._clear_layout(self._incoming_layout, keep_first=self._incoming_empty)
        self._clear_layout(self._accepted_layout, keep_first=self._accepted_empty)
        self._clear_layout(self._outgoing_layout, keep_first=self._outgoing_empty)

        self._incoming_empty.setVisible(not incoming)
        self._accepted_empty.setVisible(not accepted)
        self._outgoing_empty.setVisible(not outgoing)

        for e in incoming:
            self._incoming_layout.addWidget(
                self._make_friendship_row(e, action="respond"))
        for e in accepted:
            self._accepted_layout.addWidget(
                self._make_friendship_row(e, action="view"))
        for e in outgoing:
            self._outgoing_layout.addWidget(
                self._make_friendship_row(e, action="cancel"))

    # ── Row factories ───────────────────────────────────────────────

    def _make_user_row(self, u: dict, action: str) -> QWidget:
        row = QFrame()
        row.setStyleSheet(
            f"background:{C['panel2']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 8, 14, 8)
        hl.setSpacing(10)

        meta = QVBoxLayout()
        title = QLabel(u.get("display_name") or u.get("username"))
        title.setStyleSheet(f"color:{C['text']};font-weight:700;font-size:12px;")
        sub = QLabel(f"@{u['username']}")
        sub.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        meta.addWidget(title)
        meta.addWidget(sub)
        mw = QWidget(); mw.setLayout(meta)
        hl.addWidget(mw, 1)

        if action == "add":
            btn = QPushButton("➕  Send request")
            btn.setObjectName("addBotBtn")
            btn.clicked.connect(lambda _, uname=u["username"]:
                                self._send_request(uname))
            hl.addWidget(btn)
        return row

    def _make_friendship_row(self, e: dict, action: str) -> QWidget:
        peer = e["peer"]
        row = QFrame()
        row.setStyleSheet(
            f"background:{C['panel2']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(14, 8, 14, 8)
        hl.setSpacing(10)

        meta = QVBoxLayout()
        title = QLabel(peer.get("display_name") or peer.get("username"))
        title.setStyleSheet(f"color:{C['text']};font-weight:700;font-size:12px;")
        sub = QLabel(f"@{peer['username']}")
        sub.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        meta.addWidget(title)
        meta.addWidget(sub)
        mw = QWidget(); mw.setLayout(meta)
        hl.addWidget(mw, 1)

        if action == "respond":
            accept = QPushButton("✓  Accept")
            accept.setObjectName("addBotBtn")
            accept.clicked.connect(
                lambda _, fid=e["friendship_id"]: self._respond(fid, True))
            decline = QPushButton("✕  Decline")
            decline.setObjectName("dangerBtn")
            decline.clicked.connect(
                lambda _, fid=e["friendship_id"]: self._respond(fid, False))
            hl.addWidget(accept)
            hl.addWidget(decline)
        elif action == "view":
            view = QPushButton("👁  View")
            view.setObjectName("toolBtn")
            view.clicked.connect(
                lambda _, uname=peer["username"]: self._view_profile(uname))
            unfriend = QPushButton("✕  Unfriend")
            unfriend.setObjectName("dangerBtn")
            unfriend.clicked.connect(
                lambda _, uid=peer["id"]: self._unfriend(uid, peer["username"]))
            hl.addWidget(view)
            hl.addWidget(unfriend)
        elif action == "cancel":
            cancel = QPushButton("✕  Cancel")
            cancel.setObjectName("dangerBtn")
            cancel.clicked.connect(
                lambda _, fid=e["friendship_id"]: self._respond(fid, False))
            hl.addWidget(cancel)
        return row

    # ── Actions ─────────────────────────────────────────────────────

    def _send_request(self, username: str):
        w = _HttpWorker("POST", "/friends/request",
                        payload={"username": username})
        self._spawn(w, lambda ok, body: self._after_action(
            ok, body, "Request sent." if ok else None))

    def _respond(self, friendship_id: int, accept: bool):
        w = _HttpWorker("POST", f"/friends/{friendship_id}/respond",
                        payload={"accept": accept})
        self._spawn(w, lambda ok, body: self._after_action(
            ok, body, ("Accepted." if accept else "Declined.") if ok else None))

    def _unfriend(self, other_id: int, username: str):
        if QMessageBox.question(
                self, "Unfriend",
                f"Remove @{username} from your friends?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        w = _HttpWorker("DELETE", f"/friends/{other_id}")
        self._spawn(w, lambda ok, body: self._after_action(
            ok, body, "Removed." if ok else None))

    def _view_profile(self, username: str):
        """For v3.0.0 we just pop a small dialog with the shared info.
        A richer profile page comes in a later wave."""
        w = _HttpWorker("GET", f"/users/{username}/profile")
        self._spawn(w, self._show_profile_dialog)

    def _show_profile_dialog(self, ok: bool, body: dict):
        if not ok:
            QMessageBox.warning(self, "Profile",
                                body.get("detail", "Could not load profile."))
            return
        u     = body.get("user", {})
        shows = body.get("shows", {})
        lines = [f"<b>{u.get('display_name') or u.get('username')}</b> "
                 f"&nbsp; <span style='color:#888'>@{u.get('username')}</span>",
                 f"<span style='color:#888;font-size:11px;'>"
                 f"Tier: {body.get('tier')}</span><br>"]
        if not any(shows.values()):
            lines.append("<i>This user hasn't shared anything with you.</i>")
        if shows.get("broker"):
            brokers = body.get("broker") or []
            lines.append(f"<b>Brokers:</b> {', '.join(brokers) or '—'}")
        pl = body.get("pl", {})
        for label, key in (("Today", "daily"),
                           ("This month", "monthly"),
                           ("This year", "yearly")):
            if shows.get(key) and key in pl:
                d = pl[key]
                sign = "+" if d["pl"] >= 0 else ""
                lines.append(
                    f"<b>{label}:</b> {sign}${d['pl']:,.2f} "
                    f"({sign}{d['pct']:.2f}%)")
        QMessageBox.information(self, "Friend profile",
                                "<br>".join(lines))

    def _after_action(self, ok: bool, body: dict, success_msg: str | None):
        if not ok:
            QMessageBox.warning(self, "Friends",
                                body.get("detail", "Action failed."))
            return
        self._refresh_friends()
        # Refresh search results too in case the row should update its state
        if self._search_edit.text().strip():
            self._do_search()

    # ── Share settings ──────────────────────────────────────────────

    def _refresh_settings(self):
        w = _HttpWorker("GET", "/share-settings")
        self._spawn(w, self._on_settings_loaded)

    def _on_settings_loaded(self, ok: bool, body: dict):
        if not ok:
            return
        # Temporarily disconnect signals so applying loaded state doesn't
        # immediately trigger _on_share_changed PUT calls.
        self._settings_loaded = False
        self._chk_discoverable.setChecked(bool(body.get("discoverable")))
        self._chk_allow_dl.setChecked(bool(body.get("allow_bot_download")))
        for key, pair in self._share_widgets.items():
            pair["friends"].setChecked(bool(body.get(f"share_{key}_friends")))
            pair["public"].setChecked(bool(body.get(f"share_{key}_public")))
        self._settings_loaded = True

    def _on_share_changed(self, _state):
        if not self._settings_loaded:
            return
        patch = {
            "discoverable":       int(self._chk_discoverable.isChecked()),
            "allow_bot_download": int(self._chk_allow_dl.isChecked()),
        }
        for key, pair in self._share_widgets.items():
            patch[f"share_{key}_friends"] = int(pair["friends"].isChecked())
            patch[f"share_{key}_public"]  = int(pair["public"].isChecked())
        self._share_status.setText("Saving…")
        self._share_status.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        w = _HttpWorker("PUT", "/share-settings", payload=patch)
        self._spawn(w, self._on_share_saved)

    def _on_share_saved(self, ok: bool, body: dict):
        if ok:
            self._share_status.setText("Saved ✓")
            self._share_status.setStyleSheet(
                f"color:{C['green']};font-size:10px;")
        else:
            self._share_status.setText(
                body.get("detail", "Save failed."))
            self._share_status.setStyleSheet(
                f"color:{C['red']};font-size:10px;")
        QTimer.singleShot(3000, lambda: self._share_status.setText(""))

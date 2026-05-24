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
    QStackedWidget,
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
        # V3 wave 5 — two views in a stack so the friend profile can
        # replace the friends list IN PLACE instead of popping a dialog.
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self.scroll = ScrollContent()
        self._stack.addWidget(self.scroll)       # index 0: list view

        self._profile_view = _FriendProfileView(self)
        self._stack.addWidget(self._profile_view)   # index 1: profile

        self._workers: list[QThread] = []
        self._settings_loaded = False
        self._build()
        QTimer.singleShot(300, self.refresh)

    def refresh(self):
        self._refresh_friends()
        self._refresh_settings()

    # ── Layout ──────────────────────────────────────────────────────

    def set_manual_mode(self, on: bool):
        """Called by main window when the user switches manual/auto mode.
        Shows a banner and refreshes the list so friend cards display the
        correct account context."""
        if hasattr(self, "_manual_banner"):
            self._manual_banner.setVisible(on)
        self._refresh_friends()

    def _build(self):
        s = self.scroll

        # ─── MANUAL MODE BANNER (hidden in auto mode) ─────────────
        self._manual_banner = QFrame()
        self._manual_banner.setStyleSheet(
            f"background:{C['panel']};border:none;border-radius:10px;"
            f"border-left:4px solid {C['orange']};")
        _bv = QVBoxLayout(self._manual_banner)
        _bv.setContentsMargins(20, 14, 20, 14)
        _bv.setSpacing(4)
        _btitle = QLabel("✋  MANUAL MODE — Friends' manual accounts")
        _btitle.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:13px;font-weight:800;"
            f"color:{C['orange']};letter-spacing:1px;")
        _bsub = QLabel(
            "You're seeing your friends' dedicated manual trading accounts — "
            "performance here is purely from manual orders, no bots.")
        _bsub.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        _bsub.setWordWrap(True)
        _bv.addWidget(_btitle)
        _bv.addWidget(_bsub)
        self._manual_banner.setVisible(False)
        s.add(self._manual_banner)

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
            # V4.0.0 — fetch each accepted friend's shared snapshot
            # asynchronously and inject under the row when it arrives
            self._fetch_friend_summary(e)
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

    def _fetch_friend_summary(self, e: dict):
        """V4.0.0 — pull the friend's profile in the background and
        attach a one-line summary under their row in the FRIENDS list."""
        peer = e.get("peer", {})
        uname = peer.get("username")
        if not uname:
            return
        w = _HttpWorker("GET", f"/users/{uname}/profile")
        self._spawn(w, lambda ok, body, u=uname:
                    self._inject_friend_summary(u, ok, body))

    def _inject_friend_summary(self, username: str, ok: bool, body: dict):
        if not ok:
            return
        shows = body.get("shows", {})
        # Build a compact line: "TODAY +$45 · MONTH +$120 · Alpaca"
        bits = []
        pl = body.get("pl", {}) or {}
        for label, key in (("Today", "daily"),
                            ("Month", "monthly"),
                            ("Year", "yearly")):
            if shows.get(key) and key in pl:
                d = pl[key]
                sign = "+" if d["pl"] >= 0 else ""
                color = C["green"] if d["pl"] >= 0 else C["red"]
                bits.append(
                    f"<span style='color:{color};'>{label} "
                    f"{sign}${d['pl']:,.0f}</span>")
            else:
                bits.append(
                    f"<span style='color:{C['muted']};'>{label} (not sharing)</span>")
        if shows.get("broker"):
            brokers = body.get("broker") or []
            bits.append(f"<span style='color:{C['muted']};'>· "
                        f"{', '.join(brokers) or '—'}</span>")
        else:
            bits.append(f"<span style='color:{C['muted']};'>· broker (not sharing)</span>")
        line = "  ".join(bits)
        # Find the matching accepted row and append the summary label
        for i in range(self._accepted_layout.count()):
            w = self._accepted_layout.itemAt(i).widget()
            if not isinstance(w, QFrame):
                continue
            # Look for a QPushButton inside whose tooltip is @username
            for btn in w.findChildren(QPushButton):
                if btn.toolTip() == f"@{username}":
                    summary = QLabel(line)
                    summary.setStyleSheet(
                        f"color:{C['muted']};font-size:10px;padding:4px 0 0 0;")
                    summary.setTextFormat(Qt.TextFormat.RichText)
                    summary.setWordWrap(True)
                    # Add to the row's layout
                    lay = w.layout()
                    if lay is not None:
                        lay.addWidget(summary)
                    break

    def _make_friendship_row(self, e: dict, action: str) -> QWidget:
        peer = e["peer"]
        row = QFrame()
        row.setStyleSheet(
            f"background:{C['panel2']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        # V4.0.0 — outer layout is now VERTICAL so an async friend-summary
        # line can stack underneath the header without breaking layout.
        outer = QVBoxLayout(row)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(4)
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)
        outer.addWidget(header)

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
        """V3 wave 5 — swap to the in-place profile view, fetch both the
        profile info and the friend's shareable bots, render in MetricCard
        style (matches the overview tab)."""
        self._profile_view.show_loading(username)
        self._stack.setCurrentIndex(1)

        # Parallel fetches: /profile + /bots
        prof = _HttpWorker("GET", f"/users/{username}/profile")
        bots = _HttpWorker("GET", f"/users/{username}/bots")
        # Track both completing before rendering
        state = {"profile": None, "bots": None}

        def _check():
            if state["profile"] is not None and state["bots"] is not None:
                self._profile_view.render(state["profile"], state["bots"])

        def _on_prof(ok, body):
            state["profile"] = body if ok else {"error": body.get("detail", "Failed")}
            _check()

        def _on_bots(ok, body):
            state["bots"] = body if ok else {"bots": [], "error": True}
            _check()

        self._spawn(prof, _on_prof)
        self._spawn(bots, _on_bots)

    def show_list_view(self):
        """Called by the profile view's back button."""
        self._stack.setCurrentIndex(0)

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


# ── In-place friend profile view (V3 wave 5) ────────────────────────

class _FriendProfileView(QWidget):
    """Full-tab view showing a friend's shared profile + their bot
    library with install buttons. Swapped into the FriendsTab's stack."""

    def __init__(self, parent_tab):
        super().__init__()
        self._parent_tab = parent_tab
        from ui.widgets import ScrollContent as _SC, SectionHeader as _SH
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = _SC()
        root.addWidget(self.scroll)

        # Back button row
        back_row = QHBoxLayout()
        back = QPushButton("←  Back to friends")
        back.setObjectName("toolBtn")
        back.clicked.connect(parent_tab.show_list_view)
        back_row.addWidget(back)
        back_row.addStretch()
        bw = QWidget(); bw.setLayout(back_row)
        self.scroll.add(bw)

        # Header card (name + username + tier)
        self._header_card = QFrame()
        self._header_card.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:10px;border-top:2px solid {C['purple']};")
        hv = QVBoxLayout(self._header_card)
        hv.setContentsMargins(20, 16, 20, 16)
        self._name_lbl = QLabel("")
        self._name_lbl.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:22px;font-weight:800;"
            f"color:{C['text']};letter-spacing:1px;")
        self._handle_lbl = QLabel("")
        self._handle_lbl.setStyleSheet(
            f"color:{C['muted']};font-size:11px;")
        self._tier_lbl = QLabel("")
        self._tier_lbl.setStyleSheet(
            f"color:{C['purple']};font-size:9px;letter-spacing:3px;"
            f"font-weight:700;padding-top:4px;")
        hv.addWidget(self._name_lbl)
        hv.addWidget(self._handle_lbl)
        hv.addWidget(self._tier_lbl)
        self.scroll.add(self._header_card)

        # Stats cards row (broker / day / month / year)
        from ui.widgets import MetricCard as _MC, SectionHeader as _SH2
        self.scroll.add(_SH2("SHARED STATS", C["green"]))
        self._stats_row = QHBoxLayout()
        self._stats_row.setSpacing(10)
        self._card_broker  = _MC("BROKERS",       "—")
        self._card_daily   = _MC("TODAY P/L",     "—")
        self._card_monthly = _MC("THIS MONTH",    "—")
        self._card_yearly  = _MC("THIS YEAR",     "—")
        for card in (self._card_broker, self._card_daily,
                     self._card_monthly, self._card_yearly):
            self._stats_row.addWidget(card)
        sw = QWidget(); sw.setLayout(self._stats_row)
        self.scroll.add(sw)

        # Bot library
        self.scroll.add(_SH2("BOTS SHARED", C["yellow"]))
        self._bots_box = QWidget()
        self._bots_layout = QVBoxLayout(self._bots_box)
        self._bots_layout.setContentsMargins(0, 4, 0, 4)
        self._bots_layout.setSpacing(8)
        self._bots_empty = QLabel("Nothing shared.")
        self._bots_empty.setStyleSheet(
            f"color:{C['muted']};font-size:11px;padding:6px 0;")
        self._bots_layout.addWidget(self._bots_empty)
        self.scroll.add(self._bots_box)

        self.scroll.add_stretch()

    def show_loading(self, username: str):
        self._name_lbl.setText(username)
        self._handle_lbl.setText("Loading…")
        self._tier_lbl.setText("")
        for c in (self._card_broker, self._card_daily,
                  self._card_monthly, self._card_yearly):
            c.update_value("—")
        self._clear_bots()

    def _clear_bots(self):
        while self._bots_layout.count():
            item = self._bots_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def render(self, profile: dict, bots_resp: dict):
        if profile.get("error"):
            self._handle_lbl.setText("Could not load profile.")
            return
        u     = profile.get("user", {})
        shows = profile.get("shows", {})
        self._name_lbl.setText(u.get("display_name") or u.get("username", "?"))
        self._handle_lbl.setText(f"@{u.get('username','?')}")
        tier = profile.get("tier", "public")
        if tier == "self":
            self._tier_lbl.setText("YOUR PROFILE")
        elif tier == "friends":
            self._tier_lbl.setText("FRIEND TIER")
        else:
            self._tier_lbl.setText("PUBLIC VIEW")

        # Broker card
        if shows.get("broker"):
            brokers = profile.get("broker") or []
            self._card_broker.update_value(
                ", ".join(brokers) if brokers else "—")
        else:
            self._card_broker.update_value("—",
                sub="not shared" if tier != "self" else None)

        # P&L cards
        pl = profile.get("pl", {})
        for key, card in (("daily",   self._card_daily),
                          ("monthly", self._card_monthly),
                          ("yearly",  self._card_yearly)):
            if shows.get(key) and pl.get(key):
                d = pl[key]
                sign = "+" if d["pl"] >= 0 else ""
                color = C["green"] if d["pl"] >= 0 else C["red"]
                card.update_value(f"{sign}${d['pl']:,.2f}", color,
                                   sub=f"{sign}{d['pct']:.2f}%")
            else:
                card.update_value("—",
                    sub="not shared" if tier != "self" else None)

        # Bots
        self._clear_bots()
        bots = (bots_resp or {}).get("bots", []) or []
        allow = bool((bots_resp or {}).get("allow_bot_download", False))
        if not bots:
            empty = QLabel(
                "No bots shared." if tier != "self" else
                "You haven't published any bots yet.")
            empty.setStyleSheet(f"color:{C['muted']};font-size:11px;padding:6px 0;")
            self._bots_layout.addWidget(empty)
            return
        for b in bots:
            self._bots_layout.addWidget(self._make_bot_row(b, allow_install=allow))

    def _make_bot_row(self, b: dict, *, allow_install: bool) -> QWidget:
        row = QFrame()
        row.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        v = QVBoxLayout(row)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)

        head = QHBoxLayout()
        name = QLabel(b.get("name", "?"))
        name.setStyleSheet(
            f"font-family:'Syne',sans-serif;font-size:13px;font-weight:700;"
            f"color:{C['text']};")
        head.addWidget(name)
        head.addStretch()
        if b.get("price_credits"):
            price = QLabel(f"{b['price_credits']} credits")
            price.setStyleSheet(
                f"color:{C['yellow']};font-size:11px;font-weight:600;")
            head.addWidget(price)
        else:
            price = QLabel("FREE")
            price.setStyleSheet(
                f"color:{C['green']};font-size:10px;letter-spacing:2px;"
                f"font-weight:700;")
            head.addWidget(price)
        hw = QWidget(); hw.setLayout(head)
        v.addWidget(hw)

        desc = QLabel(b.get("description") or "—")
        desc.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        desc.setWordWrap(True)
        v.addWidget(desc)

        meta = QLabel(
            f"⬇ {b.get('downloads',0)}   ·   "
            f"{b.get('size_bytes',0)//1024} KB   ·   "
            f"{b.get('philosophy') or 'unspecified'}"
            + (f"   ·   ★ {b['rating']:.1f}" if b.get("rating") else "")
        )
        meta.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        v.addWidget(meta)

        if allow_install:
            row_btn = QHBoxLayout()
            row_btn.addStretch()
            install = QPushButton("⬇  Install to my library")
            install.setObjectName("addBotBtn")
            install.clicked.connect(
                lambda _, slug=b["slug"], name=b["name"]:
                    self._install_bot(slug, name))
            row_btn.addWidget(install)
            bw = QWidget(); bw.setLayout(row_btn)
            v.addWidget(bw)
        return row

    def _install_bot(self, slug: str, name: str):
        """Use the existing public marketplace download endpoint + then
        register the bot in the local registry (same as MoreBotsTab does)."""
        from PyQt6.QtCore import QThread as _QT, pyqtSignal as _Sig
        from ui.login import load_server_url
        from core.paths import DATA_DIR
        import shutil

        url = load_server_url()

        class _DL(_QT):
            done = _Sig(bool, str, bytes)
            def run(self_):
                import requests
                try:
                    r = requests.get(f"{url}/bots/{slug}/download", timeout=20)
                    if r.ok:
                        self_.done.emit(True, "", r.content)
                    else:
                        self_.done.emit(False, f"HTTP {r.status_code}", b"")
                except Exception as e:
                    self_.done.emit(False, str(e), b"")

        def _on_done(ok, err, blob):
            if not ok:
                QMessageBox.warning(self, "Install failed", err)
                return
            bots_dir = DATA_DIR / "bots"
            bots_dir.mkdir(exist_ok=True)
            dest = bots_dir / f"{slug}.py"
            dest.write_bytes(blob)
            # Register in local bot_registry so MORE BOTS picks it up
            try:
                from core import data as _D
                import json as _json
                s = _D.load_settings()
                reg = s.get("bot_registry",
                            {"active": [], "silenced": [], "custom": []})
                existing = [c["id"] for c in reg.get("custom", [])]
                if slug not in existing:
                    reg.setdefault("custom", []).append({
                        "id":     slug, "label":  name,
                        "script": str(dest), "color":  C["purple"],
                    })
                    s["bot_registry"] = reg
                    with open(_D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                        _json.dump(s, f, indent=2)
            except Exception as e:
                print(f"[friend-install] registry update failed: {e}")
            QMessageBox.information(
                self, "Bot installed",
                f"'{name}' is now in your library. Open MORE BOTS → "
                f"AVAILABLE TO ADD to activate it.")

        self._dl_worker = _DL()
        self._dl_worker.done.connect(_on_done)
        self._dl_worker.start()

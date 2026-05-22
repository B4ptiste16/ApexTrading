"""
APEX Auth Server — Friends + share-settings (V3.0.0)
─────────────────────────────────────────────────────────────────────
Two-table model that lives next to the existing users / credentials
tables in apex_server.db.

  friendships     — pending/accepted pairs.  Symmetric: at most one row
                    per unordered (a, b) pair, with `requested_by`
                    pointing at whichever side initiated it.

  share_settings  — per-user toggles for what is visible to friends
                    (scope='friends') and to anyone (scope='public').
                    Default is everything OFF — sharing is opt-in.

Public API used by server/app.py:
  init_friends_db()
  search_users(query, limit=20, exclude_id=None) → [user-dict]
  send_request(from_id, to_id) → dict
  list_friends(user_id) → {"incoming": [...], "outgoing": [...], "accepted": [...]}
  respond_to_request(user_id, friendship_id, accept: bool) → dict
  remove_friend(user_id, other_id) → dict
  are_friends(a, b) → bool
  get_share_settings(user_id) → dict
  set_share_settings(user_id, patch: dict) → dict
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .database import _conn, get_user_by_id


# ── columns we let clients toggle on share_settings ─────────────────
_SHARE_FIELDS = {
    "discoverable",
    "share_broker_friends",  "share_broker_public",
    "share_daily_friends",   "share_daily_public",
    "share_monthly_friends", "share_monthly_public",
    "share_yearly_friends",  "share_yearly_public",
    "share_bots_friends",    "share_bots_public",
    "allow_bot_download",
}


def init_friends_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS friendships (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_a_id    INTEGER NOT NULL,
                user_b_id    INTEGER NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                requested_by INTEGER NOT NULL,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                UNIQUE(user_a_id, user_b_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS share_settings (
                user_id                INTEGER PRIMARY KEY,
                discoverable           INTEGER NOT NULL DEFAULT 0,
                share_broker_friends   INTEGER NOT NULL DEFAULT 0,
                share_broker_public    INTEGER NOT NULL DEFAULT 0,
                share_daily_friends    INTEGER NOT NULL DEFAULT 0,
                share_daily_public     INTEGER NOT NULL DEFAULT 0,
                share_monthly_friends  INTEGER NOT NULL DEFAULT 0,
                share_monthly_public   INTEGER NOT NULL DEFAULT 0,
                share_yearly_friends   INTEGER NOT NULL DEFAULT 0,
                share_yearly_public    INTEGER NOT NULL DEFAULT 0,
                share_bots_friends     INTEGER NOT NULL DEFAULT 0,
                share_bots_public      INTEGER NOT NULL DEFAULT 0,
                allow_bot_download     INTEGER NOT NULL DEFAULT 0,
                updated_at             TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_friend_a ON friendships(user_a_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_friend_b ON friendships(user_b_id)")
        c.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ordered_pair(a: int, b: int) -> tuple[int, int]:
    """Canonical ordering so (a, b) and (b, a) hash to the same row."""
    return (a, b) if a < b else (b, a)


def _public_user(u: dict) -> dict:
    """Strip sensitive fields before returning a user row to a peer."""
    if not u:
        return {}
    return {
        "id":           u["id"],
        "username":     u["username"],
        "display_name": u["display_name"],
    }


# ── search ──────────────────────────────────────────────────────────

def search_users(query: str, limit: int = 20,
                 exclude_id: Optional[int] = None) -> list[dict]:
    """Find discoverable users by username or display_name (case-insensitive
    prefix match). Excludes the caller via exclude_id so they don't see
    themselves in the result list."""
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    pattern = f"{q}%"
    with _conn() as c:
        # LEFT JOIN so users without a share_settings row still appear,
        # but we filter discoverable=1 explicitly for opt-in privacy.
        rows = c.execute("""
            SELECT u.id, u.username, u.display_name
            FROM users u
            JOIN share_settings s ON s.user_id = u.id
            WHERE s.discoverable = 1
              AND u.is_active = 1
              AND (LOWER(u.username) LIKE ? OR LOWER(u.display_name) LIKE ?)
              AND (? IS NULL OR u.id != ?)
            ORDER BY u.username
            LIMIT ?
        """, (pattern, pattern, exclude_id, exclude_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ── friend requests ─────────────────────────────────────────────────

def send_request(from_id: int, to_id: int) -> dict:
    if from_id == to_id:
        return {"ok": False, "detail": "You can't friend yourself."}
    if not get_user_by_id(to_id):
        return {"ok": False, "detail": "User not found."}
    a, b = _ordered_pair(from_id, to_id)
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM friendships WHERE user_a_id=? AND user_b_id=?",
            (a, b)).fetchone()
        if existing:
            st = existing["status"]
            if st == "accepted":
                return {"ok": False, "detail": "Already friends."}
            if st == "pending":
                if existing["requested_by"] == from_id:
                    return {"ok": False, "detail": "Request already pending."}
                # The other side previously requested — auto-accept.
                return respond_to_request(from_id, existing["id"], accept=True)
            if st == "blocked":
                return {"ok": False, "detail": "Cannot send request."}
        now = _now()
        cur = c.execute(
            """INSERT INTO friendships
               (user_a_id, user_b_id, status, requested_by, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (a, b, from_id, now, now))
        c.commit()
        return {"ok": True, "friendship_id": cur.lastrowid, "status": "pending"}


def respond_to_request(user_id: int, friendship_id: int, accept: bool) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM friendships WHERE id=?", (friendship_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "detail": "Request not found."}
        if user_id not in (row["user_a_id"], row["user_b_id"]):
            return {"ok": False, "detail": "Not your request."}
        if row["requested_by"] == user_id:
            return {"ok": False,
                    "detail": "You can't accept your own request."}
        if row["status"] != "pending":
            return {"ok": False,
                    "detail": f"Request already {row['status']}."}
        if accept:
            c.execute(
                "UPDATE friendships SET status='accepted', updated_at=? WHERE id=?",
                (_now(), friendship_id))
            c.commit()
            return {"ok": True, "status": "accepted"}
        else:
            c.execute("DELETE FROM friendships WHERE id=?", (friendship_id,))
            c.commit()
            return {"ok": True, "status": "declined"}


def remove_friend(user_id: int, other_id: int) -> dict:
    a, b = _ordered_pair(user_id, other_id)
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM friendships WHERE user_a_id=? AND user_b_id=?",
            (a, b))
        c.commit()
        return {"ok": cur.rowcount > 0,
                "removed": cur.rowcount}


def list_friends(user_id: int) -> dict:
    """Return three buckets:
      incoming  — pending requests sent TO this user
      outgoing  — pending requests sent BY this user
      accepted  — current friends"""
    out = {"incoming": [], "outgoing": [], "accepted": []}
    with _conn() as c:
        rows = c.execute(
            """SELECT f.*, ua.username AS a_username, ua.display_name AS a_display,
                              ub.username AS b_username, ub.display_name AS b_display
               FROM friendships f
               JOIN users ua ON ua.id = f.user_a_id
               JOIN users ub ON ub.id = f.user_b_id
               WHERE f.user_a_id=? OR f.user_b_id=?
               ORDER BY f.updated_at DESC""",
            (user_id, user_id)).fetchall()
    for r in rows:
        peer_id = r["user_b_id"] if r["user_a_id"] == user_id else r["user_a_id"]
        peer = {
            "id":           peer_id,
            "username":     r["b_username"] if r["user_a_id"] == user_id else r["a_username"],
            "display_name": r["b_display"]  if r["user_a_id"] == user_id else r["a_display"],
        }
        entry = {
            "friendship_id": r["id"],
            "peer":          peer,
            "status":        r["status"],
            "requested_by":  r["requested_by"],
            "created_at":    r["created_at"],
        }
        if r["status"] == "accepted":
            out["accepted"].append(entry)
        elif r["status"] == "pending":
            if r["requested_by"] == user_id:
                out["outgoing"].append(entry)
            else:
                out["incoming"].append(entry)
    return out


def are_friends(a: int, b: int) -> bool:
    ua, ub = _ordered_pair(a, b)
    with _conn() as c:
        row = c.execute(
            """SELECT 1 FROM friendships
               WHERE user_a_id=? AND user_b_id=? AND status='accepted'""",
            (ua, ub)).fetchone()
    return row is not None


# ── share settings ──────────────────────────────────────────────────

def _default_settings(user_id: int) -> dict:
    return {
        "user_id":               user_id,
        "discoverable":          0,
        "share_broker_friends":  0, "share_broker_public":  0,
        "share_daily_friends":   0, "share_daily_public":   0,
        "share_monthly_friends": 0, "share_monthly_public": 0,
        "share_yearly_friends":  0, "share_yearly_public":  0,
        "share_bots_friends":    0, "share_bots_public":    0,
        "allow_bot_download":    0,
        "updated_at":            _now(),
    }


def get_share_settings(user_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM share_settings WHERE user_id=?",
            (user_id,)).fetchone()
    if row:
        return dict(row)
    # Create-on-first-read so subsequent updates have a row to UPDATE.
    s = _default_settings(user_id)
    with _conn() as c:
        cols = ", ".join(s.keys())
        qs   = ", ".join(["?"] * len(s))
        try:
            c.execute(f"INSERT INTO share_settings ({cols}) VALUES ({qs})",
                      tuple(s.values()))
            c.commit()
        except sqlite3.IntegrityError:
            pass
    return s


def set_share_settings(user_id: int, patch: dict) -> dict:
    """Update only the toggles in _SHARE_FIELDS — silently ignore any
    extra keys the client sends so a stale build can't poison the row."""
    get_share_settings(user_id)   # ensure a row exists
    clean = {k: int(bool(v)) for k, v in patch.items() if k in _SHARE_FIELDS}
    if not clean:
        return get_share_settings(user_id)
    sets = ", ".join(f"{k}=?" for k in clean) + ", updated_at=?"
    vals = list(clean.values()) + [_now(), user_id]
    with _conn() as c:
        c.execute(f"UPDATE share_settings SET {sets} WHERE user_id=?", vals)
        c.commit()
    return get_share_settings(user_id)

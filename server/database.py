"""
APEX Auth Server — SQLite database layer
No external ORM; uses stdlib sqlite3 directly.
DB path: ~/apex_data/apex_server.db  (overridable via APEX_DB_PATH env var)
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── DB path ──────────────────────────────────────────────────────────────────

DB_PATH = Path(
    os.environ.get("APEX_DB_PATH",
                   Path.home() / "apex_data" / "apex_server.db")
)


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                username         TEXT    UNIQUE NOT NULL,
                email            TEXT    UNIQUE NOT NULL,
                hashed_password  TEXT    NOT NULL,
                display_name     TEXT    NOT NULL,
                created_at       TEXT    NOT NULL,
                is_active        INTEGER NOT NULL DEFAULT 1
            )
        """)
        # V3.0.2 — idempotent migrations to add Google OAuth + email
        # verification columns. SQLite has no "IF NOT EXISTS" for ADD
        # COLUMN, so we wrap each in a try/except.
        for ddl in (
            "ALTER TABLE users ADD COLUMN google_sub          TEXT",
            "ALTER TABLE users ADD COLUMN is_verified         INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN verification_token  TEXT",
            "ALTER TABLE users ADD COLUMN verification_sent_at TEXT",
            "ALTER TABLE users ADD COLUMN avatar_url          TEXT",
            "ALTER TABLE users ADD COLUMN phone               TEXT",
            "CREATE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub)",
            "CREATE INDEX IF NOT EXISTS idx_users_verif_token ON users(verification_token)",
        ):
            try:
                c.execute(ddl)
            except Exception:
                pass
        c.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(row) -> Optional[dict]:
    return dict(row) if row else None


# ── Queries ───────────────────────────────────────────────────────────────────

def get_user_by_id(uid: int) -> Optional[dict]:
    with _conn() as c:
        return _row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def get_user_by_email(email: str) -> Optional[dict]:
    with _conn() as c:
        return _row(
            c.execute("SELECT * FROM users WHERE email=?",
                      (email.strip().lower(),)).fetchone()
        )


def get_user_by_username(username: str) -> Optional[dict]:
    with _conn() as c:
        return _row(
            c.execute("SELECT * FROM users WHERE username=?",
                      (username.strip().lower(),)).fetchone()
        )


def list_user_ids() -> list[int]:
    """V7.1.12: used by the server-side scheduler to iterate every
    user once per minute and reconcile their scheduled bots."""
    with _conn() as c:
        return [r["id"] for r in
                c.execute("SELECT id FROM users WHERE is_active=1").fetchall()]


def create_user(username: str, email: str,
                hashed_password: str, display_name: str,
                google_sub: Optional[str] = None,
                is_verified: int = 0) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO users (username, email, hashed_password,
                                  display_name, created_at,
                                  google_sub, is_verified)
               VALUES (?,?,?,?,?,?,?)""",
            (username.strip().lower(), email.strip().lower(),
             hashed_password, display_name.strip(), now,
             google_sub, int(bool(is_verified))),
        )
        c.commit()
        return get_user_by_id(cur.lastrowid)


def get_user_by_google_sub(sub: str) -> Optional[dict]:
    if not sub:
        return None
    with _conn() as c:
        return _row(c.execute(
            "SELECT * FROM users WHERE google_sub=?", (sub,)
        ).fetchone())


def link_google_sub(user_id: int, sub: str) -> None:
    """Attach a Google subject ID to an existing email/password user so
    future Google sign-ins land on the same account."""
    with _conn() as c:
        c.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, user_id))
        c.commit()


def set_verification_token(user_id: int, token: Optional[str],
                            sent_at: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET verification_token=?, verification_sent_at=? WHERE id=?",
            (token, sent_at or (datetime.now(timezone.utc).isoformat()
                                if token else None), user_id),
        )
        c.commit()


def get_user_by_verification_token(token: str) -> Optional[dict]:
    if not token:
        return None
    with _conn() as c:
        return _row(c.execute(
            "SELECT * FROM users WHERE verification_token=?",
            (token,)).fetchone())


def mark_verified(user_id: int) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE users
               SET is_verified=1, verification_token=NULL, verification_sent_at=NULL
               WHERE id=?""", (user_id,))
        c.commit()


def update_user_profile(user_id: int, *,
                         display_name: Optional[str] = None,
                         avatar_url: Optional[str] = None,
                         phone: Optional[str] = None) -> dict:
    fields, vals = [], []
    if display_name is not None:
        fields.append("display_name=?"); vals.append(display_name.strip())
    if avatar_url is not None:
        fields.append("avatar_url=?"); vals.append(avatar_url.strip())
    if phone is not None:
        fields.append("phone=?"); vals.append(phone.strip())
    if not fields:
        return get_user_by_id(user_id)
    vals.append(user_id)
    with _conn() as c:
        c.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)
        c.commit()
    return get_user_by_id(user_id)


def update_user_password(user_id: int, hashed_password: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET hashed_password=? WHERE id=?",
                  (hashed_password, user_id))
        c.commit()


def update_user_email(user_id: int, new_email: str) -> dict:
    """Change email + invalidate verification (will need re-verifying)."""
    with _conn() as c:
        c.execute(
            """UPDATE users
               SET email=?, is_verified=0, verification_token=NULL
               WHERE id=?""",
            (new_email.strip().lower(), user_id))
        c.commit()
    return get_user_by_id(user_id)

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


def create_user(username: str, email: str,
                hashed_password: str, display_name: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO users (username, email, hashed_password,
                                  display_name, created_at)
               VALUES (?,?,?,?,?)""",
            (username.strip().lower(), email.strip().lower(),
             hashed_password, display_name.strip(), now),
        )
        c.commit()
        return get_user_by_id(cur.lastrowid)

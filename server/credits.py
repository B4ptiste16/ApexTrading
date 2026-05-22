"""
APEX Server — Credits ledger  (V3 wave 5)
─────────────────────────────────────────────────────────────────────
Two-table model:

  user_credits         (user_id, balance, updated_at)
  credit_transactions  (id, user_id, delta, reason, granted_by, created_at)

`balance` is the cached running sum of the user's deltas, kept in sync
on every grant so reads don't need to scan the whole transaction log.

`delta` is an INTEGER number of credits (positive = credit, negative =
debit). 1 credit = $0.01 by convention (set on the admin / accounting
side; the API doesn't know about real money yet).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .database import _conn


def init_credits_tables() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_credits (
                user_id    INTEGER PRIMARY KEY,
                balance    INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT    NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS credit_transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                delta       INTEGER NOT NULL,
                reason      TEXT    NOT NULL,
                granted_by  INTEGER,
                created_at  TEXT    NOT NULL
            )
        """)
        c.execute("""CREATE INDEX IF NOT EXISTS idx_credit_tx_user
                     ON credit_transactions(user_id)""")
        c.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_balance(user_id: int) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT balance FROM user_credits WHERE user_id=?", (user_id,)
        ).fetchone()
    return int(row["balance"]) if row else 0


def grant(user_id: int, delta: int, reason: str,
          granted_by: Optional[int] = None) -> dict:
    """Append a transaction + update the cached balance. delta can be
    negative for debits. `granted_by` is the admin's user_id (or None
    if the system did it, e.g. signup bonus)."""
    delta = int(delta)
    now = _now()
    with _conn() as c:
        # Ensure the user_credits row exists
        c.execute("""INSERT OR IGNORE INTO user_credits
                     (user_id, balance, updated_at) VALUES (?, 0, ?)""",
                  (user_id, now))
        c.execute("UPDATE user_credits SET balance = balance + ?, updated_at=? "
                  "WHERE user_id=?", (delta, now, user_id))
        c.execute("""INSERT INTO credit_transactions
                     (user_id, delta, reason, granted_by, created_at)
                     VALUES (?, ?, ?, ?, ?)""",
                  (user_id, delta, reason, granted_by, now))
        c.commit()
    return {"balance": get_balance(user_id), "delta": delta}


def list_transactions(user_id: int, limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM credit_transactions
               WHERE user_id=?
               ORDER BY id DESC LIMIT ?""",
            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]

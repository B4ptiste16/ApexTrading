"""
APEX Auth Server — Encrypted credential storage  (V7.1+)
─────────────────────────────────────────────────────────────────────
Stores each user's broker credentials (Alpaca API key + secret, IBKR
gateway URL/clientId, Anthropic key) encrypted at rest in the SQLite
database. Symmetric encryption uses Fernet from `cryptography`, with
the master key derived from APEX_JWT_SECRET (already configured per
deployment). No plaintext secrets are ever written to disk.

Schema:
    user_credentials(
        user_id      INTEGER PRIMARY KEY,
        encrypted    BLOB NOT NULL,
        updated_at   TEXT NOT NULL
    )

The `encrypted` column holds a Fernet token whose plaintext is a JSON
blob of {alpaca_long: {...}, alpaca_short: {...}, anthropic: "...", ...}.
The structure is intentionally flexible — fields can be added without
schema changes.
"""

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from . import auth
from .database import DB_PATH, _conn


# ── Fernet key derivation ─────────────────────────────────────────────

def _fernet() -> Fernet:
    """Derive a Fernet key from the JWT secret so deployments don't need
    a separate encryption key. Fernet keys must be 32 url-safe-base64
    bytes — SHA-256 of the secret gets us exactly 32 raw bytes."""
    raw = hashlib.sha256(auth.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


# ── Schema bootstrap ──────────────────────────────────────────────────

def init_credentials_table() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id      INTEGER PRIMARY KEY,
                encrypted    BLOB    NOT NULL,
                updated_at   TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        c.commit()


# ── CRUD ──────────────────────────────────────────────────────────────

def save_credentials(user_id: int, creds: dict) -> None:
    """Encrypt `creds` (a dict) and persist it for this user."""
    token = _fernet().encrypt(json.dumps(creds, separators=(",", ":")).encode())
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO user_credentials (user_id, encrypted, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   encrypted=excluded.encrypted,
                   updated_at=excluded.updated_at""",
            (user_id, token, now),
        )
        c.commit()


def load_credentials(user_id: int) -> Optional[dict]:
    """Return decrypted credentials dict for the user, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT encrypted FROM user_credentials WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        plain = _fernet().decrypt(row["encrypted"])
        return json.loads(plain)
    except (InvalidToken, json.JSONDecodeError, ValueError):
        # Decryption failed — likely the JWT_SECRET (and thus the derived
        # key) changed since the row was written. Treat as missing so the
        # client can re-upload. Never raise — keys can rotate cleanly.
        return None


def delete_credentials(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (user_id,))
        c.commit()

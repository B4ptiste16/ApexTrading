"""
APEX Auth Server — JWT + bcrypt utilities
Secret key: loaded from APEX_JWT_SECRET env var, or auto-generated and
persisted in ~/apex_data/.jwt_secret on first run.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import jwt

# ── Secret key ────────────────────────────────────────────────────────────────

def _load_or_create_secret() -> str:
    env = os.environ.get("APEX_JWT_SECRET")
    if env:
        return env
    secret_file = Path(
        os.environ.get("APEX_DB_PATH",
                       Path.home() / "apex_data" / "apex_server.db")
    ).parent / ".jwt_secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    key = secrets.token_hex(32)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(key)
    secret_file.chmod(0o600)
    return key


SECRET_KEY  = _load_or_create_secret()
ALGORITHM   = "HS256"
EXPIRE_DAYS = 30


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_token(user_id: int, email: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": exp},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None

"""
APEX Auth Server — Public bot marketplace  (V7.1+, scaffold)
─────────────────────────────────────────────────────────────────────
Users can upload their custom bot Python scripts to a shared library
that other users can browse, filter, and install.

Schema:
    public_bots(
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id      INTEGER NOT NULL,
        name          TEXT    NOT NULL,
        slug          TEXT    UNIQUE NOT NULL,
        description   TEXT    NOT NULL DEFAULT '',
        tags          TEXT    NOT NULL DEFAULT '',     -- comma-separated
        size_bytes    INTEGER NOT NULL,
        sha256        TEXT    NOT NULL,
        downloads     INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL,
        FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
    )

The actual .py file is stored on disk under DATA_DIR/marketplace/<slug>.py.
This keeps the SQLite database small and lets future versions stream
file content rather than fetch it from a BLOB column.

This file ships the schema + CRUD scaffolding; the FastAPI endpoints
in server/app.py expose it over HTTP.
"""

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .database import DB_PATH, _conn


MARKETPLACE_DIR = DB_PATH.parent / "marketplace"


def init_marketplace_table() -> None:
    MARKETPLACE_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS public_bots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id      INTEGER NOT NULL,
                name          TEXT    NOT NULL,
                slug          TEXT    UNIQUE NOT NULL,
                description   TEXT    NOT NULL DEFAULT '',
                tags          TEXT    NOT NULL DEFAULT '',
                size_bytes    INTEGER NOT NULL,
                sha256        TEXT    NOT NULL,
                downloads     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        c.commit()


def _slug(name: str) -> str:
    """Filesystem-safe ASCII slug. Falls back to a short hash if name is empty."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-.")
    return s or hashlib.sha1(name.encode()).hexdigest()[:8]


def upload_bot(*, owner_id: int, name: str, description: str,
               tags: list[str], file_bytes: bytes) -> dict:
    """Persist a new bot in the marketplace. Returns the row dict."""
    if len(file_bytes) > 1_000_000:           # 1 MB cap
        raise ValueError("Bot script too large (max 1 MB).")
    if not file_bytes.lstrip().startswith((b"#", b"\"\"\"", b"'''", b"import",
                                            b"from", b"def", b"class")):
        # Cheap sanity check — refuses obviously non-Python uploads
        raise ValueError("File doesn't look like a Python script.")

    slug = _slug(name)
    # If slug exists, append a short hash suffix
    with _conn() as c:
        row = c.execute("SELECT 1 FROM public_bots WHERE slug=?", (slug,)).fetchone()
    if row:
        slug = f"{slug}-{hashlib.sha1(file_bytes).hexdigest()[:6]}"

    sha = hashlib.sha256(file_bytes).hexdigest()
    dest = MARKETPLACE_DIR / f"{slug}.py"
    dest.write_bytes(file_bytes)

    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO public_bots
               (owner_id, name, slug, description, tags, size_bytes,
                sha256, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (owner_id, name, slug, description,
             ",".join(t.strip() for t in tags if t.strip()),
             len(file_bytes), sha, now),
        )
        c.commit()
        return _row(c.execute(
            "SELECT * FROM public_bots WHERE id=?", (cur.lastrowid,)
        ).fetchone())


def list_bots(*, q: str = "", tag: str = "",
              limit: int = 50, offset: int = 0) -> list[dict]:
    sql = "SELECT * FROM public_bots WHERE 1=1"
    params: list = []
    if q:
        sql += " AND (name LIKE ? OR description LIKE ?)"
        like = f"%{q}%"
        params += [like, like]
    if tag:
        sql += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    sql += " ORDER BY downloads DESC, created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with _conn() as c:
        return [_row(r) for r in c.execute(sql, params).fetchall()]


def get_bot(slug: str) -> Optional[dict]:
    with _conn() as c:
        return _row(c.execute(
            "SELECT * FROM public_bots WHERE slug=?", (slug,)
        ).fetchone())


def read_bot_file(slug: str) -> Optional[bytes]:
    p = MARKETPLACE_DIR / f"{slug}.py"
    return p.read_bytes() if p.exists() else None


def increment_downloads(slug: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE public_bots SET downloads = downloads + 1 WHERE slug=?",
            (slug,),
        )
        c.commit()


def delete_bot(*, slug: str, owner_id: int) -> bool:
    """Owner-only delete. Returns True if a row was removed."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM public_bots WHERE slug=? AND owner_id=?",
            (slug, owner_id),
        )
        c.commit()
        ok = cur.rowcount > 0
    if ok:
        try:
            os.remove(MARKETPLACE_DIR / f"{slug}.py")
        except FileNotFoundError:
            pass
    return ok


def _row(r) -> Optional[dict]:
    return dict(r) if r else None

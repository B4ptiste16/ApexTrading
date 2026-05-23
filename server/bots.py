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
        # V3 wave 5 — idempotent column adds. Need price + classification
        # so the new BOT MARKET tab can sort / filter properly.
        for ddl in (
            "ALTER TABLE public_bots ADD COLUMN price_credits INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN philosophy   TEXT    NOT NULL DEFAULT ''",
            "ALTER TABLE public_bots ADD COLUMN win_rate_pct REAL    NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN rating       REAL    NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN featured     INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN recommended  INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN active_users INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN runtime_hours REAL   NOT NULL DEFAULT 0",
            "ALTER TABLE public_bots ADD COLUMN visibility   TEXT    NOT NULL DEFAULT 'public'",  # public | friends_only
            # V3.3.0 — moderation
            "ALTER TABLE public_bots ADD COLUMN status         TEXT    NOT NULL DEFAULT 'active'",  # active | flagged | removed
            "ALTER TABLE public_bots ADD COLUMN flagged_reason TEXT",
            "ALTER TABLE public_bots ADD COLUMN flagged_by     INTEGER",
            "ALTER TABLE public_bots ADD COLUMN flagged_at     TEXT",
            "CREATE INDEX IF NOT EXISTS idx_public_bots_status ON public_bots(status)",
        ):
            try:
                c.execute(ddl)
            except Exception:
                pass
        c.commit()


def _slug(name: str) -> str:
    """Filesystem-safe ASCII slug. Falls back to a short hash if name is empty."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-.")
    return s or hashlib.sha1(name.encode()).hexdigest()[:8]


def upload_bot(*, owner_id: int, name: str, description: str,
               tags: list[str], file_bytes: bytes,
               philosophy: str = "", price_credits: int = 0,
               visibility: str = "public") -> dict:
    """Persist a new bot in the marketplace. Returns the row dict."""
    if len(file_bytes) > 1_000_000:           # 1 MB cap
        raise ValueError("Bot script too large (max 1 MB).")
    if not file_bytes.lstrip().startswith((b"#", b"\"\"\"", b"'''", b"import",
                                            b"from", b"def", b"class")):
        # Cheap sanity check — refuses obviously non-Python uploads
        raise ValueError("File doesn't look like a Python script.")
    if visibility not in ("public", "friends_only"):
        visibility = "public"

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
                sha256, created_at, philosophy, price_credits, visibility)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (owner_id, name, slug, description,
             ",".join(t.strip() for t in tags if t.strip()),
             len(file_bytes), sha, now,
             philosophy.strip(), max(0, int(price_credits)), visibility),
        )
        c.commit()
        return _row(c.execute(
            "SELECT * FROM public_bots WHERE id=?", (cur.lastrowid,)
        ).fetchone())


def list_bots(*, q: str = "", tag: str = "",
              philosophy: str = "",
              max_price: Optional[int] = None,
              min_win_rate: float = 0.0,
              section: str = "",       # "" | "featured" | "recommended" | "cheap" | "mine"
              owner_id: Optional[int] = None,
              sort: str = "downloads", # downloads | rating | win_rate | newest | cheapest
              limit: int = 50, offset: int = 0) -> list[dict]:
    # V3.3.0 — hide flagged + removed bots from every browse path.
    sql = "SELECT * FROM public_bots WHERE visibility='public' AND status='active'"
    params: list = []
    if q:
        sql += " AND (name LIKE ? OR description LIKE ?)"
        like = f"%{q}%"
        params += [like, like]
    if tag:
        sql += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    if philosophy:
        sql += " AND philosophy = ?"
        params.append(philosophy)
    if max_price is not None:
        sql += " AND price_credits <= ?"
        params.append(int(max_price))
    if min_win_rate > 0:
        sql += " AND win_rate_pct >= ?"
        params.append(float(min_win_rate))
    if section == "featured":
        sql += " AND featured = 1"
    elif section == "recommended":
        sql += " AND recommended = 1"
    elif section == "cheap":
        sql += " AND price_credits <= 100"
    elif section == "mine" and owner_id is not None:
        sql = sql.replace("visibility='public'", "1=1")  # show own friends-only too
        sql += " AND owner_id = ?"
        params.append(int(owner_id))

    order_map = {
        "downloads":  "downloads DESC, rating DESC",
        "rating":     "rating DESC, downloads DESC",
        "win_rate":   "win_rate_pct DESC, downloads DESC",
        "newest":     "created_at DESC",
        "cheapest":   "price_credits ASC, downloads DESC",
    }
    sql += f" ORDER BY {order_map.get(sort, order_map['downloads'])} LIMIT ? OFFSET ?"
    params += [limit, offset]
    with _conn() as c:
        return [_row(r) for r in c.execute(sql, params).fetchall()]


def list_bots_for_owner(owner_id: int) -> list[dict]:
    """Used by /bots/mine — publisher analytics screen."""
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM public_bots WHERE owner_id=?
               ORDER BY downloads DESC, created_at DESC""",
            (owner_id,)).fetchall()
    return [_row(r) for r in rows]


def list_bots_visible_to_friend(owner_id: int) -> list[dict]:
    """Return bots an owner shares with friends (visibility public or
    friends_only). The caller is expected to have already verified the
    friendship + the owner's share_bots_friends flag."""
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM public_bots
               WHERE owner_id=? AND visibility IN ('public','friends_only')
               ORDER BY downloads DESC""",
            (owner_id,)).fetchall()
    return [_row(r) for r in rows]


def update_bot_meta(*, slug: str, owner_id: int, **fields) -> bool:
    """Owner-only meta editor. Whitelisted columns only."""
    allowed = {"description", "tags", "price_credits", "philosophy",
               "win_rate_pct", "visibility"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return False
    sets = ", ".join(f"{k}=?" for k in clean)
    vals = list(clean.values()) + [slug, owner_id]
    with _conn() as c:
        cur = c.execute(
            f"UPDATE public_bots SET {sets} WHERE slug=? AND owner_id=?", vals)
        c.commit()
    return cur.rowcount > 0


def admin_classify(slug: str, *, featured: Optional[bool] = None,
                    recommended: Optional[bool] = None) -> bool:
    sets, vals = [], []
    if featured is not None:
        sets.append("featured=?"); vals.append(1 if featured else 0)
    if recommended is not None:
        sets.append("recommended=?"); vals.append(1 if recommended else 0)
    if not sets:
        return False
    vals.append(slug)
    with _conn() as c:
        cur = c.execute(
            f"UPDATE public_bots SET {', '.join(sets)} WHERE slug=?", vals)
        c.commit()
    return cur.rowcount > 0


def get_distinct_philosophies() -> list[str]:
    with _conn() as c:
        rows = c.execute(
            """SELECT DISTINCT philosophy FROM public_bots
               WHERE philosophy != '' AND visibility='public'
               ORDER BY philosophy""").fetchall()
    return [r["philosophy"] for r in rows]


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

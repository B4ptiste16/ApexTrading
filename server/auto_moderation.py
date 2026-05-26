"""
APEX · Algorithmic moderation  (v4.6.16)
─────────────────────────────────────────────────────────────────
Two algorithmic checks run periodically over the public marketplace.
No AI involved — both are deterministic.

1. find_copyright_violations()
   For every pair of public bots (a, b) submitted at times t_a < t_b,
   compute Jaccard similarity on their source-code n-gram fingerprints
   (the same `similarity.py` the publish endpoint already uses). If
   similarity >= COPYRIGHT_BLOCK (default 0.90), the LATER submission
   is flagged as a copyright violation of the earlier one.

2. validate_bot_metadata()
   For every public bot, parse its APEX-BOT-META block and check that
   what it CLAIMS matches what its code DOES:
     - META.asset_type='crypto' but code never imports / references
       crypto-specific libs (ccxt) AND never mentions BTC/ETH/USDT
       → flag as 'misdeclared asset_type'
     - META.ai_used='groq' but code has no groq.com calls
       AND no GROQ_API_KEY reference
       → flag as 'misdeclared ai_used'
     - META.requirements declares libs the code never imports
       → flag as 'phantom requirements'

When v4.6.16's predetermined-META path is in place (META sealed at
publish from the creating API key's provider), most ai_used /
asset_type mismatches become impossible to construct. The moderation
bot then targets only the cases the seal can't enforce (e.g. content
mismatches in description / method fields).

Both checks are idempotent — running twice produces the same flags.
Flags are persisted in `moderation_flags` table; admin can review,
clear, or escalate to a publish ban via the existing moderation.py
functions.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

# Lazy imports so this module is light to load
def _similarity():
    from . import similarity
    return similarity

def _marketplace():
    from . import marketplace
    return marketplace

def _moderation():
    from . import moderation
    return moderation

def _bot_meta():
    try:
        from . import bot_meta
    except ImportError:
        import bot_meta as bot_meta  # type: ignore
    return bot_meta


# ── Tunable thresholds ─────────────────────────────────────────

COPYRIGHT_BLOCK_THRESHOLD = 0.90    # ≥ this → flag the LATER bot
COPYRIGHT_WARN_THRESHOLD  = 0.75    # log-only, no flag


# ── Flags DB ───────────────────────────────────────────────────

def _conn():
    """Use the same SQLite file as the rest of the server."""
    from . import database
    return sqlite3.connect(database.DB_PATH)


def init_moderation_flags_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS moderation_flags (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL,
                kind        TEXT NOT NULL,
                detail      TEXT,
                created_at  REAL NOT NULL,
                cleared     INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_modflags_slug
            ON moderation_flags(slug)
        """)
        c.commit()


def _record_flag(slug: str, kind: str, detail: str):
    """Insert a flag, no-op if an identical (slug, kind) flag is
    already uncleared (idempotent across runs)."""
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM moderation_flags "
            "WHERE slug=? AND kind=? AND cleared=0 LIMIT 1",
            (slug, kind)).fetchone()
        if existing:
            return
        c.execute(
            "INSERT INTO moderation_flags "
            "(slug, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (slug, kind, detail, time.time()))
        c.commit()


def list_flags(only_active: bool = True) -> list[dict]:
    out = []
    sql = "SELECT id, slug, kind, detail, created_at, cleared FROM moderation_flags"
    if only_active:
        sql += " WHERE cleared=0"
    sql += " ORDER BY created_at DESC LIMIT 500"
    with _conn() as c:
        for row in c.execute(sql).fetchall():
            out.append({
                "id":         row[0],
                "slug":       row[1],
                "kind":       row[2],
                "detail":     row[3],
                "created_at": row[4],
                "cleared":    bool(row[5]),
            })
    return out


def clear_flag(flag_id: int):
    with _conn() as c:
        c.execute("UPDATE moderation_flags SET cleared=1 WHERE id=?",
                  (flag_id,))
        c.commit()


# ── Check 1: copyright (pairwise similarity) ──────────────────

def find_copyright_violations() -> list[dict]:
    """Pairwise Jaccard similarity over public bot sources. Flags the
    LATER submission when score >= COPYRIGHT_BLOCK_THRESHOLD. Returns
    the list of new flags raised this run."""
    mp = _marketplace()
    sim = _similarity()
    base = mp.MARKETPLACE_DIR
    if not base.exists():
        return []
    # Pull all .py files with their submitted_at from the DB
    rows = []
    try:
        with _conn() as c:
            for owner_id, slug, submitted_at in c.execute(
                "SELECT owner_id, slug, created_at FROM public_bots"
            ).fetchall():
                p = base / f"{slug}.py"
                if p.exists():
                    rows.append((owner_id, slug, submitted_at, p))
    except Exception as e:
        print(f"[auto-mod copyright] DB read failed: {e}")
        return []
    # Sort by submitted_at ascending so older = "first published"
    rows.sort(key=lambda r: r[2])
    # Pre-compute fingerprints once
    fps = {}
    for owner, slug, _, path in rows:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            fps[slug] = (owner, sim._fingerprint(src))
        except Exception:
            continue
    new_flags = []
    for i, (owner_i, slug_i, ts_i, _) in enumerate(rows):
        for owner_j, slug_j, ts_j, _ in rows[:i]:
            if owner_i == owner_j:
                continue  # users can copy themselves (versioning)
            fp_i = fps.get(slug_i, (None, set()))[1]
            fp_j = fps.get(slug_j, (None, set()))[1]
            score = sim.jaccard(fp_i, fp_j)
            if score >= COPYRIGHT_BLOCK_THRESHOLD:
                detail = (f"{score:.0%} match to earlier-submitted "
                          f"{slug_j!r} (owner {owner_j})")
                _record_flag(slug_i, "copyright", detail)
                new_flags.append({"slug": slug_i, "kind": "copyright",
                                  "detail": detail})
    return new_flags


# ── Check 2: metadata vs code consistency ─────────────────────

_CRYPTO_HINTS = ("ccxt", "BTC", "ETH", "USDT", "XBT", "binance",
                 "coinbase", "kraken")

_AI_HINTS = {
    "groq":      ("groq.com", "GROQ_API_KEY"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY", "claude"),
    "openai":    ("openai.com", "OPENAI_API_KEY", "gpt-"),
    "gemini":    ("generativelanguage.googleapis", "GOOGLE_AI_API_KEY",
                  "gemini"),
}


def _validate_one(slug: str, source: str) -> list[tuple[str, str]]:
    """Return list of (kind, detail) flags for this single bot."""
    bm = _bot_meta()
    meta = bm.parse_meta(source) or {}
    src_low = source.lower()
    flags: list[tuple[str, str]] = []

    # asset_type vs code
    asset = (meta.get("asset_type") or "").strip().lower()
    if asset == "crypto":
        if not any(hint.lower() in src_low for hint in _CRYPTO_HINTS):
            flags.append((
                "misdeclared_asset_type",
                f"META.asset_type='crypto' but code has no crypto "
                f"libs / symbols ({', '.join(_CRYPTO_HINTS)})"))

    # ai_used vs code (only checks if ai_used is in our known set)
    ai = (meta.get("ai_used") or "").strip().lower()
    if ai in _AI_HINTS:
        hints = _AI_HINTS[ai]
        if not any(h.lower() in src_low for h in hints):
            flags.append((
                "misdeclared_ai_used",
                f"META.ai_used='{ai}' but code references none of "
                f"{', '.join(hints)} — likely false advertising"))

    # requirements vs imports
    reqs = meta.get("requirements") or []
    if isinstance(reqs, list):
        imports = bm.scan_imports(source)
        # Map common alias: scikit-learn → sklearn etc. (already in
        # IMPORT_TO_PIP, but reverse lookup)
        reverse = {v: k for k, v in bm.IMPORT_TO_PIP.items()}
        normalized_imports = set(imports + [reverse.get(i, i) for i in imports])
        for req in reqs:
            req_low = str(req).strip().lower()
            if not req_low:
                continue
            # Check if any imported module matches this pip name
            matches = any(
                imp.lower() == req_low or
                bm.IMPORT_TO_PIP.get(imp, imp).lower() == req_low
                for imp in normalized_imports
            )
            if not matches:
                flags.append((
                    "phantom_requirement",
                    f"META declares requirement '{req}' but no matching "
                    f"import found in code (would burn the preflight "
                    f"installer's network bandwidth for nothing)"))

    return flags


def validate_bot_metadata() -> list[dict]:
    """Run the META-vs-code consistency check across every public bot.
    Returns new flags raised this run."""
    mp = _marketplace()
    base = mp.MARKETPLACE_DIR
    if not base.exists():
        return []
    new_flags = []
    for p in base.glob("*.py"):
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for kind, detail in _validate_one(p.stem, src):
            _record_flag(p.stem, kind, detail)
            new_flags.append({"slug": p.stem, "kind": kind,
                              "detail": detail})
    return new_flags


# ── Public runner (called from a scheduler) ───────────────────

def run_all_checks() -> dict:
    """Run both checks. Returns counts + new flags this run.
    Idempotent — calling repeatedly only adds genuinely-new violations."""
    init_moderation_flags_table()
    new_cp = find_copyright_violations()
    new_md = validate_bot_metadata()
    return {
        "ran_at":           time.time(),
        "new_copyright":    new_cp,
        "new_metadata":     new_md,
        "active_flag_count": len(list_flags(only_active=True)),
    }

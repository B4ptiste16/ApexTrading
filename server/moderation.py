"""
APEX · Moderation actions  (V3.3.0)
─────────────────────────────────────────────────────────────────────
Implements the publisher's choice from the V3.0.0.txt spec:

  Action     Consequence
  ──────     ───────────
  FLAG       Bot is hidden from the marketplace listings + browse search,
             but every user who already installed it KEEPS their copy.
             Publisher's other published bots are unaffected. Used when
             the bot looks suspicious but not confirmed-bad.

  REMOVE     Hard removal. The bot is deleted from the marketplace; all
             buyers get their credits refunded; every installed copy
             is marked as revoked so the desktop client deletes the
             local .py on next launch. Publisher takes a fixed credit
             fine AND a short publish-ban (e.g. 7 days).

  UNFLAG     Reverse FLAG (admin changed their mind).

A purchase log (bot_purchases) is added so refunds + revocations can
actually fan out to the right people. Free downloads are logged too —
credits_paid is just 0, but the row lets us notify the right users
about removed bots.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .database import _conn
from . import credits as credits_mod


# Fixed credit fine per V3.0.0 spec
FINE_CREDITS    = 500
# Publish-ban window after a removal
BAN_DAYS        = 7


def init_purchases_table() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_purchases (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id     INTEGER NOT NULL,
                bot_slug     TEXT    NOT NULL,
                credits_paid INTEGER NOT NULL DEFAULT 0,
                status       TEXT    NOT NULL DEFAULT 'active',  -- active | refunded | revoked
                purchased_at TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_purch_buyer "
                  "ON bot_purchases(buyer_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_purch_slug "
                  "ON bot_purchases(bot_slug)")
        c.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_purchase(buyer_id: int, slug: str, credits_paid: int = 0) -> None:
    """Called every time a user downloads a bot (free OR paid). Lets us
    later refund + revoke the right users."""
    with _conn() as c:
        c.execute(
            """INSERT INTO bot_purchases
               (buyer_id, bot_slug, credits_paid, status, purchased_at)
               VALUES (?, ?, ?, 'active', ?)""",
            (buyer_id, slug, max(0, int(credits_paid)), _now()))
        c.commit()


def list_revocations_for_user(buyer_id: int) -> list[str]:
    """Slugs the user used to have but were removed by moderation since
    their last sync. Returns slugs, marks them as 'revoked' so the desktop
    only sees each revocation once."""
    out: list[str] = []
    with _conn() as c:
        rows = c.execute(
            """SELECT DISTINCT bot_slug FROM bot_purchases
               WHERE buyer_id=? AND status='refunded'""",
            (buyer_id,)).fetchall()
        out = [r["bot_slug"] for r in rows]
        if out:
            qmarks = ",".join("?" * len(out))
            c.execute(
                f"UPDATE bot_purchases SET status='revoked' "
                f"WHERE buyer_id=? AND bot_slug IN ({qmarks})",
                (buyer_id, *out))
            c.commit()
    return out


def flag_bot(slug: str, *, reason: str, moderator_id: int) -> dict:
    """Mark a bot as hidden. Doesn't refund — used when the bot is
    suspicious but not confirmed bad yet."""
    with _conn() as c:
        cur = c.execute(
            """UPDATE public_bots
               SET status='flagged', flagged_reason=?, flagged_by=?, flagged_at=?
               WHERE slug=? AND status != 'removed'""",
            (reason, moderator_id, _now(), slug))
        c.commit()
    return {"ok": cur.rowcount > 0, "slug": slug, "status": "flagged"}


def unflag_bot(slug: str) -> dict:
    with _conn() as c:
        cur = c.execute(
            """UPDATE public_bots
               SET status='active', flagged_reason=NULL,
                   flagged_by=NULL, flagged_at=NULL
               WHERE slug=? AND status='flagged'""", (slug,))
        c.commit()
    return {"ok": cur.rowcount > 0, "slug": slug, "status": "active"}


def remove_bot(slug: str, *, reason: str, moderator_id: int) -> dict:
    """Hard removal with refund + publisher fine + publish ban.

    Steps:
      1. Mark every active purchase of this bot as 'refunded'
      2. Issue credit refunds to all paying buyers via credits.grant
      3. Mark the bot row as removed
      4. Charge the fixed fine + apply publish-ban to the publisher
      5. (Caller decides whether to also unlink the .py from disk —
         we keep it there so the marketplace history remains
         auditable, just gated behind status='removed'.)
    """
    with _conn() as c:
        # Find the publisher
        bot = c.execute("SELECT * FROM public_bots WHERE slug=?",
                        (slug,)).fetchone()
        if not bot:
            return {"ok": False, "detail": "Bot not found."}
        publisher_id = bot["owner_id"]
        # Snapshot active purchases for the refund loop
        purchases = c.execute(
            """SELECT id, buyer_id, credits_paid FROM bot_purchases
               WHERE bot_slug=? AND status='active'""",
            (slug,)).fetchall()

    refunds_count = 0
    refunded_credits = 0
    for p in purchases:
        if p["credits_paid"] > 0:
            credits_mod.grant(
                p["buyer_id"], p["credits_paid"],
                f"refund: bot '{slug}' removed by moderation",
                granted_by=moderator_id)
            refunded_credits += p["credits_paid"]
        refunds_count += 1

    with _conn() as c:
        c.execute(
            """UPDATE bot_purchases SET status='refunded'
               WHERE bot_slug=? AND status='active'""", (slug,))
        c.execute(
            """UPDATE public_bots
               SET status='removed', flagged_reason=?, flagged_by=?, flagged_at=?
               WHERE slug=?""",
            (reason, moderator_id, _now(), slug))
        # Publisher fine + ban
        ban_until = (datetime.now(timezone.utc)
                     + timedelta(days=BAN_DAYS)).isoformat()
        c.execute("UPDATE users SET publish_ban_until=? WHERE id=?",
                  (ban_until, publisher_id))
        c.commit()

    # Fixed credit fine on the publisher (negative grant). Allowed to go
    # negative — locks the publisher's account until they top up.
    credits_mod.grant(
        publisher_id, -FINE_CREDITS,
        f"moderation fine: '{slug}' removed",
        granted_by=moderator_id)

    return {
        "ok":              True,
        "slug":            slug,
        "publisher_id":    publisher_id,
        "refunded_users":  refunds_count,
        "refunded_credits": refunded_credits,
        "fine_credits":    FINE_CREDITS,
        "publish_ban_until": ban_until,
    }


def is_user_publish_banned(user_id: int) -> tuple[bool, Optional[str]]:
    """Returns (banned?, ban_until_iso or None)."""
    with _conn() as c:
        row = c.execute(
            "SELECT publish_ban_until FROM users WHERE id=?",
            (user_id,)).fetchone()
    if not row or not row["publish_ban_until"]:
        return False, None
    try:
        ban_until = datetime.fromisoformat(row["publish_ban_until"])
    except Exception:
        return False, None
    if ban_until > datetime.now(timezone.utc):
        return True, row["publish_ban_until"]
    return False, None

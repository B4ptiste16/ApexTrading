"""
APEX · Revenue split  (V4.0.0)
─────────────────────────────────────────────────────────────────────
When a buyer pays N credits for a bot, those credits get distributed
across three buckets per the BOSS_ADMIN-configured split:

  seller_pct   → the bot's publisher
  admin_pct    → split EQUALLY among every user with admin role
                  (ADMIN, SUB_BOSS_ADMIN, BOSS_ADMIN)
  running_pct  → kept as a virtual platform balance (covers AI keys,
                  server costs, etc. — not attached to any user)

Defaults: 90 / 5 / 5. Can be edited only by BOSS_ADMIN via the
admin tab's REVENUE SPLIT card.

Schema:
  system_config(key TEXT PK, value TEXT json, updated_at TEXT)

Stores both the split (as JSON) and the running-account balance (as
an integer string) under separate keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .database import _conn
from . import credits as credits_mod


SPLIT_KEY            = "revenue_split"
RUNNING_BALANCE_KEY  = "running_account_balance"

# Default split — adds up to 100 %
DEFAULT_SPLIT = {"seller_pct": 90, "admin_pct": 5, "running_pct": 5}


def init_revenue_table() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Seed defaults if missing
        now = datetime.now(timezone.utc).isoformat()
        c.execute("""INSERT OR IGNORE INTO system_config (key, value, updated_at)
                     VALUES (?, ?, ?)""",
                  (SPLIT_KEY, json.dumps(DEFAULT_SPLIT), now))
        c.execute("""INSERT OR IGNORE INTO system_config (key, value, updated_at)
                     VALUES (?, ?, ?)""",
                  (RUNNING_BALANCE_KEY, "0", now))
        c.commit()


def get_split() -> dict:
    with _conn() as c:
        row = c.execute("SELECT value FROM system_config WHERE key=?",
                        (SPLIT_KEY,)).fetchone()
    if not row:
        return dict(DEFAULT_SPLIT)
    try:
        return json.loads(row["value"])
    except Exception:
        return dict(DEFAULT_SPLIT)


def set_split(seller_pct: int, admin_pct: int, running_pct: int) -> dict:
    """Validate (must sum to 100) and persist."""
    total = int(seller_pct) + int(admin_pct) + int(running_pct)
    if total != 100:
        raise ValueError(
            f"Split must add up to 100 % (got {total}).")
    for v in (seller_pct, admin_pct, running_pct):
        if v < 0:
            raise ValueError("Percentages can't be negative.")
    payload = {
        "seller_pct":  int(seller_pct),
        "admin_pct":   int(admin_pct),
        "running_pct": int(running_pct),
    }
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute("""UPDATE system_config SET value=?, updated_at=?
                     WHERE key=?""",
                  (json.dumps(payload), now, SPLIT_KEY))
        c.commit()
    return payload


def get_running_balance() -> int:
    with _conn() as c:
        row = c.execute("SELECT value FROM system_config WHERE key=?",
                        (RUNNING_BALANCE_KEY,)).fetchone()
    try:
        return int(row["value"]) if row else 0
    except Exception:
        return 0


def _add_to_running(delta: int) -> int:
    with _conn() as c:
        current = get_running_balance()
        new_val = current + int(delta)
        now = datetime.now(timezone.utc).isoformat()
        c.execute("""UPDATE system_config SET value=?, updated_at=?
                     WHERE key=?""",
                  (str(new_val), now, RUNNING_BALANCE_KEY))
        c.commit()
    return new_val


def distribute_purchase(*, seller_id: int, total_credits: int,
                         bot_slug: str) -> dict:
    """Called by /bots/{slug}/download (or any future bot-purchase
    path). Splits the purchase amount across the three buckets and
    fires the appropriate credits.grant() calls.

    Returns a small dict summarising what landed where — useful for
    the admin dashboard / audit trail."""
    if total_credits <= 0:
        return {"distributed": False, "reason": "free purchase"}

    split = get_split()
    seller_credits   = total_credits * int(split["seller_pct"])  // 100
    admin_pool       = total_credits * int(split["admin_pct"])   // 100
    running_credits  = total_credits * int(split["running_pct"]) // 100

    # Remainder due to integer division → push into the running account
    remainder = total_credits - seller_credits - admin_pool - running_credits
    running_credits += remainder

    # 1. Seller
    if seller_credits > 0:
        credits_mod.grant(seller_id, seller_credits,
                          f"sale: {bot_slug}")

    # 2. Admin pool — split equally among every admin-role user
    admin_distributions = []
    if admin_pool > 0:
        with _conn() as c:
            admins = c.execute(
                """SELECT id FROM users
                   WHERE role IN ('ADMIN','SUB_BOSS_ADMIN','BOSS_ADMIN')
                     AND is_active=1""").fetchall()
        admin_ids = [a["id"] for a in admins]
        if admin_ids:
            per_admin = admin_pool // len(admin_ids)
            admin_remainder = admin_pool - per_admin * len(admin_ids)
            for i, aid in enumerate(admin_ids):
                # Distribute the remainder one credit at a time to
                # admins at the top of the list (deterministic).
                share = per_admin + (1 if i < admin_remainder else 0)
                if share > 0:
                    credits_mod.grant(aid, share,
                                      f"admin share: {bot_slug}")
                    admin_distributions.append({"admin_id": aid, "credits": share})
        else:
            # No admins exist yet — fold the share into running account
            running_credits += admin_pool

    # 3. Running account (platform costs)
    new_running = _add_to_running(running_credits) if running_credits else \
                   get_running_balance()

    return {
        "distributed":          True,
        "seller_credits":       seller_credits,
        "running_credits":      running_credits,
        "running_balance":      new_running,
        "admin_distributions":  admin_distributions,
        "split_used":           split,
    }

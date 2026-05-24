"""
APEX — Stripe payment gateway for credit top-ups.
─────────────────────────────────────────────────────────────────────
Env vars required on Oracle (add to /opt/apex_data/.env):
  STRIPE_SECRET_KEY     sk_test_...   (Stripe Dashboard → API keys)
  STRIPE_WEBHOOK_SECRET whsec_...     (Stripe Dashboard → Webhooks,
                                       or `stripe listen --forward-to ...`)

Packages below map to ad-hoc price_data in Checkout (no pre-created
Stripe Price/Product objects required).  Prices in USD cents.
1 ◊ credit = $0.01 (convention from TOS section 5).
"""

import os
from datetime import datetime, timezone

from .database import _conn

STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# ── Credit packages ────────────────────────────────────────────────
# key → {credits, amount_cents, label, popular}
CREDIT_PACKAGES: dict = {
    "starter": {"credits": 100,  "amount_cents": 99,   "label": "Starter", "popular": False},
    "popular": {"credits": 500,  "amount_cents": 449,  "label": "Popular", "popular": True},
    "power":   {"credits": 1500, "amount_cents": 1199, "label": "Power",   "popular": False},
    "mega":    {"credits": 5000, "amount_cents": 3499, "label": "Mega",    "popular": False},
}


# ── Schema ─────────────────────────────────────────────────────────

def init_payments_table() -> None:
    """Idempotent. Called from server lifespan."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS stripe_sessions (
                session_id   TEXT    PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                credits      INTEGER NOT NULL DEFAULT 0,
                package_key  TEXT    NOT NULL DEFAULT '',
                fulfilled_at TEXT
            )
        """)
        c.commit()


# ── Helpers ────────────────────────────────────────────────────────

def is_configured() -> bool:
    return bool(STRIPE_SECRET_KEY)


# ── Checkout session ───────────────────────────────────────────────

def create_checkout_session(user_id: int, package_key: str,
                             success_url: str, cancel_url: str) -> dict:
    """Create a Stripe Checkout Session and return {url, session_id}.

    success_url should contain the literal string {CHECKOUT_SESSION_ID}
    (not an f-string placeholder) so Stripe substitutes it on redirect.
    """
    pkg = CREDIT_PACKAGES.get(package_key)
    if not pkg:
        raise ValueError("Unknown package: " + repr(package_key))
    if not is_configured():
        raise RuntimeError("Stripe not configured — STRIPE_SECRET_KEY missing.")

    import stripe as _s
    _s.api_key = STRIPE_SECRET_KEY

    credits_str = str(pkg["credits"])
    session = _s.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": pkg["amount_cents"],
                "product_data": {
                    "name": "BAPTOU Credits — " + pkg["label"] + " Pack",
                    "description": (credits_str + " ◊ credits  ·  1 ◊ = $0.01"),
                },
            },
            "quantity": 1,
        }],
        metadata={
            "user_id":     str(user_id),
            "package_key": package_key,
            "credits":     credits_str,
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )

    # Pre-record for idempotency (webhook may arrive before client polls)
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO stripe_sessions
                (session_id, user_id, credits, package_key)
            VALUES (?, ?, ?, ?)
        """, (session.id, user_id, pkg["credits"], package_key))
        c.commit()

    return {"url": session.url, "session_id": session.id}


# ── Webhook ────────────────────────────────────────────────────────

def handle_webhook(payload_bytes: bytes, sig_header: str) -> None:
    """Verify Stripe signature and fulfil completed checkout sessions."""
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set.")

    import stripe as _s
    _s.api_key = STRIPE_SECRET_KEY

    try:
        event = _s.Webhook.construct_event(
            payload_bytes, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except _s.error.SignatureVerificationError as e:
        raise ValueError("Bad Stripe signature: " + str(e))

    if event["type"] == "checkout.session.completed":
        _fulfill(event["data"]["object"])


def _fulfill(session: dict) -> None:
    """Grant credits for a completed checkout session (idempotent)."""
    session_id = session["id"]
    meta       = session.get("metadata") or {}

    # Prefer metadata attached at session-creation time
    try:
        user_id     = int(meta["user_id"])
        credits     = int(meta["credits"])
        package_key = str(meta["package_key"])
    except (KeyError, ValueError, TypeError):
        # Fallback to DB lookup (e.g. metadata stripped by Stripe in some flows)
        with _conn() as c:
            row = c.execute(
                "SELECT user_id, credits, package_key FROM stripe_sessions "
                "WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row:
            print("[stripe] unknown session " + session_id, flush=True)
            return
        user_id     = row["user_id"]
        credits     = row["credits"]
        package_key = row["package_key"]

    # Idempotency guard — skip if already fulfilled
    with _conn() as c:
        existing = c.execute(
            "SELECT fulfilled_at FROM stripe_sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if existing and existing["fulfilled_at"]:
            print("[stripe] already fulfilled " + session_id, flush=True)
            return

    # Grant credits
    from . import credits as _cr
    _cr.grant(user_id, credits,
              "stripe:" + package_key + "(" + str(credits) + ")",
              granted_by=None)

    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE stripe_sessions SET fulfilled_at=? WHERE session_id=?",
            (now, session_id)
        )
        # Insert in case webhook arrived before the client round-tripped
        c.execute("""
            INSERT OR IGNORE INTO stripe_sessions
                (session_id, user_id, credits, package_key, fulfilled_at)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, user_id, credits, package_key, now))
        c.commit()

    print(
        "[stripe] granted " + str(credits) + " credits"
        " user=" + str(user_id) +
        " pkg=" + package_key +
        " sid=" + session_id,
        flush=True,
    )

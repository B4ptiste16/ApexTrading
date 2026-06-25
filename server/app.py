"""
APEX Auth Server — FastAPI  V7+
Endpoints:
  GET  /health         → liveness check
  POST /auth/signup    → create account, returns token + user
  POST /auth/login     → login with email/username + password, returns token + user
  GET  /auth/me        → verify token, returns user info
"""

import random
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Header, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import (auth, database, credentials as creds,
               bots as marketplace, web, bot_runner, scheduler,
               friends, oauth as google_oauth, email_send,
               credits as credits_mod,
               similarity, moderation,
               revenue, payments, stock_eval)
from .schemas import SignupRequest, LoginRequest


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    creds.init_credentials_table()
    marketplace.init_marketplace_table()
    # V4.6.16 — algorithmic moderation tables
    try:
        from . import auto_moderation as _auto_mod
        _auto_mod.init_moderation_flags_table()
    except Exception as _e:
        print(f"[startup] auto_moderation init failed: {_e}")
    friends.init_friends_db()
    credits_mod.init_credits_tables()
    moderation.init_purchases_table()
    revenue.init_revenue_table()
    payments.init_payments_table()
    promoted = database.bootstrap_boss_admin()
    if promoted:
        print(f"[bootstrap] promoted user_id={promoted} to BOSS_ADMIN",
              flush=True)
    # V7.1.12: kick off the once-a-minute schedule reconciliation loop.
    # Runs forever inside the FastAPI event loop until shutdown.
    scheduler.start_loop()
    yield
    # V7.1.11: stop tracked bots gracefully on uvicorn shutdown so a
    # service restart doesn't leave them orphaned with stale keys.
    try:
        scheduler.stop_loop()
    except Exception:
        pass
    try:
        bot_runner.shutdown_all()
    except Exception:
        pass


app = FastAPI(
    title="APEX Auth Server",
    version="1.0.0",
    description="Authentication backend for BAPTOU Trading Platform",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _public(user: dict) -> dict:
    """Strip hashed_password before returning user to client."""
    return {k: v for k, v in user.items() if k != "hashed_password"}


def _require_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    return authorization.removeprefix("Bearer ").strip()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "APEX Auth Server"}


@app.post("/auth/signup")
def signup(data: SignupRequest):
    email = data.email.strip().lower()

    # Validate
    if not email or "@" not in email:
        raise HTTPException(400, "Invalid email address.")
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    # Uniqueness checks
    if database.get_user_by_email(email):
        raise HTTPException(400, "An account with this email already exists.")

    # Derive username
    base_username = (data.username or "").strip().lower() or email.split("@")[0]
    username = base_username
    if database.get_user_by_username(username):
        username = f"{base_username}{random.randint(100, 999)}"

    display_name = (data.display_name or "").strip() or username

    hashed = auth.hash_password(data.password)
    # If Resend isn't configured on the server, auto-verify so the
    # user isn't stuck in an unverified state with no way to escape.
    auto_verify = not email_send.is_configured()
    user = database.create_user(username, email, hashed, display_name,
                                is_verified=1 if auto_verify else 0)
    # V3.1.4 — welcome bonus so the user can try MAKE BOT → "Free via
    # APEX" without immediately hitting the credit wall.
    try:
        credits_mod.grant(user["id"], SIGNUP_BONUS_CREDITS, "signup bonus")
    except Exception as e:
        print(f"[signup] credit bonus failed: {e}", flush=True)
    if not auto_verify:
        _send_verification_email(user)
    token = auth.create_token(user["id"], user["email"])
    return {"token": token, "user": _public(database.get_user_by_id(user["id"]))}


def _send_verification_email(user: dict) -> None:
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    database.set_verification_token(user["id"], token)
    try:
        email_send.send_verification(user["email"],
                                      user["display_name"] or user["username"],
                                      token)
    except Exception as e:
        print(f"[verify] send failed: {e}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# V3 wave 4 — Google OAuth + email verification + profile management
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/auth/google/config")
def auth_google_config():
    """Tells the client whether Google sign-in is available + the
    client_id needed to start the auth URL."""
    return {"configured": google_oauth.is_configured(),
            "client_id":  google_oauth.client_id()}


@app.post("/auth/google/exchange")
def auth_google_exchange(payload: dict):
    """Desktop sends {code, redirect_uri} after catching the OAuth
    callback on a loopback HTTP listener. We swap the code for the
    user's email via Google's token endpoint, then match or create
    an APEX account and return our own JWT."""
    code         = (payload or {}).get("code", "").strip()
    redirect_uri = (payload or {}).get("redirect_uri", "").strip()
    if not code or not redirect_uri:
        raise HTTPException(400, "Missing code or redirect_uri.")
    if not google_oauth.is_configured():
        raise HTTPException(
            503,
            "Google sign-in is not configured on this server.")
    try:
        info = google_oauth.exchange_code(code, redirect_uri)
    except Exception as e:
        raise HTTPException(400, f"Google sign-in failed: {e}")

    sub   = info["sub"]
    email = info["email"]
    name  = info["name"]

    # Match by Google subject ID first (rock-solid), then by email
    # (covers users who originally signed up with email/password and
    # are now adding Google as an auth method).
    user = database.get_user_by_google_sub(sub) if sub else None
    if not user:
        existing = database.get_user_by_email(email)
        if existing:
            if sub:
                database.link_google_sub(existing["id"], sub)
            user = database.get_user_by_id(existing["id"])
        else:
            # Brand-new user — derive a username and create them.
            base = google_oauth.random_username_from(email)
            uname = base
            while database.get_user_by_username(uname):
                uname = google_oauth.random_username_from(email)
            user = database.create_user(
                username=uname, email=email,
                hashed_password="__google_oauth__",
                display_name=name,
                google_sub=sub,
                # Google already proved they own the address.
                is_verified=1 if info.get("email_verified") else 0,
            )
            # V3.1.4 — same welcome bonus as the email/password signup
            try:
                credits_mod.grant(user["id"], SIGNUP_BONUS_CREDITS,
                                   "signup bonus (Google)")
            except Exception as e:
                print(f"[google-signup] credit bonus failed: {e}", flush=True)

    token = auth.create_token(user["id"], user["email"])
    return {"token": token, "user": _public(user)}


@app.get("/auth/verify")
def auth_verify(token: str):
    """Public link clicked from the verification email."""
    user = database.get_user_by_verification_token(token)
    if not user:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;"
            "padding:40px;text-align:center;'>"
            "<h2>Link expired or already used</h2>"
            "<p>Open APEX and request a new verification email "
            "from the Account tab.</p></body></html>",
            status_code=400)
    database.mark_verified(user["id"])
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;"
        "padding:40px;text-align:center;background:#0c0f16;color:#d8dde8;'>"
        "<h2 style='color:#3fb89a;letter-spacing:4px;'>◈ APEX</h2>"
        "<p>Email confirmed — you can close this tab and head back to "
        "APEX.</p></body></html>")


@app.post("/auth/resend-verification")
def auth_resend_verification(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    if user.get("is_verified"):
        return {"ok": True, "already_verified": True}
    if not email_send.is_configured():
        # Dev mode — just flip the bit so the user isn't stuck.
        database.mark_verified(user["id"])
        return {"ok": True, "auto_verified": True,
                "detail": "Email service not configured — auto-verified."}
    _send_verification_email(user)
    return {"ok": True, "sent_to": user["email"]}


@app.put("/auth/profile")
def auth_update_profile(payload: dict,
                         authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Body must be a JSON object.")
    updates = {
        "display_name": payload.get("display_name"),
        "avatar_url":   payload.get("avatar_url"),
        "phone":        payload.get("phone"),
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _public(user)
    updated = database.update_user_profile(user["id"], **updates)
    return _public(updated)


@app.put("/auth/password")
def auth_change_password(payload: dict,
                          authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    current_pw = (payload or {}).get("current_password", "")
    new_pw     = (payload or {}).get("new_password", "")
    if user["hashed_password"] == "__google_oauth__":
        # Google-only accounts can SET (not change) a password — they
        # weren't authenticated via password to begin with.
        if not new_pw or len(new_pw) < 8:
            raise HTTPException(400, "Password must be at least 8 characters.")
        database.update_user_password(user["id"], auth.hash_password(new_pw))
        return {"ok": True, "first_password": True}
    if not auth.verify_password(current_pw, user["hashed_password"]):
        raise HTTPException(401, "Current password is incorrect.")
    if not new_pw or len(new_pw) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")
    database.update_user_password(user["id"], auth.hash_password(new_pw))
    return {"ok": True}


@app.put("/auth/email")
def auth_change_email(payload: dict,
                       authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    new_email = (payload or {}).get("new_email", "").strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(400, "Invalid email address.")
    if database.get_user_by_email(new_email):
        raise HTTPException(400, "An account with this email already exists.")
    updated = database.update_user_email(user["id"], new_email)
    if email_send.is_configured():
        _send_verification_email(updated)
        sent = True
    else:
        database.mark_verified(updated["id"])
        sent = False
    return {"ok": True, "verification_sent": sent,
            "user": _public(database.get_user_by_id(user["id"]))}



@app.post("/auth/login")
def login(data: LoginRequest):
    ident = data.identifier.strip().lower()
    user  = database.get_user_by_email(ident) or database.get_user_by_username(ident)

    if not user or not auth.verify_password(data.password, user["hashed_password"]):
        raise HTTPException(401, "Invalid email / username or password.")

    if not user.get("is_active", 1):
        raise HTTPException(403, "This account has been disabled.")

    token = auth.create_token(user["id"], user["email"])
    return {"token": token, "user": _public(user)}


@app.get("/auth/me")
def get_me(authorization: str | None = Header(default=None)):
    token   = _require_bearer(authorization)
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(401, "Token expired or invalid.")

    user = database.get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(401, "User not found.")

    return _public(user)


# ──────────────────────────────────────────────────────────────────────────────
# V7.1+ — Encrypted credential storage (broker keys per user)
# ──────────────────────────────────────────────────────────────────────────────

def _current_user(authorization: str | None) -> dict:
    """Resolve the bearer token → user row, or raise 401."""
    token = _require_bearer(authorization)
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(401, "Token expired or invalid.")
    user = database.get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(401, "User not found.")
    return user


@app.put("/credentials")
def save_credentials(payload: dict,
                     authorization: str | None = Header(default=None)):
    """Encrypt+store the caller's broker credentials. Body is freeform JSON
    so future fields (IBKR clientId, Anthropic key, etc.) can be added
    without server schema changes."""
    user = _current_user(authorization)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Body must be a JSON object.")
    creds.save_credentials(user["id"], payload)
    return {"ok": True, "fields": list(payload.keys())}


@app.get("/credentials")
def get_credentials(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    data = creds.load_credentials(user["id"])
    return data or {}


@app.delete("/credentials")
def delete_credentials(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    creds.delete_credentials(user["id"])
    return {"ok": True}


# ── V4.6.121 — Shared weekly stock evaluation (Find Stocks) ──────────────────
# The evaluation is generated ON THE SERVER using the main account's AI keys and
# cached per ISO week, so every user sees the identical multi-model review for a
# stock. The client may POST its market snapshot to seed the (first) generation.
@app.post("/stocks/{symbol}/evaluation")
def stock_evaluation(symbol: str, payload: dict | None = None,
                     authorization: str | None = Header(default=None)):
    _current_user(authorization)   # auth-gate; any signed-in user may read
    body = payload if isinstance(payload, dict) else {}
    snapshot = body.get("snapshot") if isinstance(body.get("snapshot"), dict) else None
    force = bool(body.get("force"))
    try:
        return stock_eval.evaluate(symbol, snapshot, force=force)
    except Exception as e:
        raise HTTPException(500, f"Evaluation failed: {e}")


# ── V4.6.101: per-account desktop config (bot registry + per-bot settings +
# prefs = the desktop's apex_settings.json, minus secrets). Lets a switched
# account or a fresh/cleared machine reconstruct its config. API keys stay in
# the encrypted /credentials blob, never here. Stored per-user as a plain JSON
# file alongside the user's bot data. ───────────────────────────────────────
def _desktop_config_path(user_id: int):
    return bot_runner._user_data_dir(user_id) / "desktop_config.json"


def _user_bot_registry(user_id: int, broker: str = "alpaca",
                       mode: str = "paper") -> dict | None:
    """V4.6.105 — the user's REAL bot registry (active / silenced / custom) for
    a broker+mode, taken from their synced desktop-config. This is the source of
    truth for which bots the user currently has — so the web dashboard stops
    showing bots they deleted (the old code scanned private_bots/*.py files,
    which persist after a delete). Returns None if the user hasn't synced yet."""
    import json as _json
    p = _desktop_config_path(user_id)
    if not p.exists():
        return None
    try:
        cfg = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    # Key shape: bot_registry_<uid>_<broker>_<mode>. The <uid> is the desktop's
    # auth id, so match by the broker+mode suffix rather than guessing it.
    suffix = f"_{broker}_{mode}"
    for k, v in cfg.items():
        if k.startswith("bot_registry_") and k.endswith(suffix) and isinstance(v, dict):
            return v
    for k, v in cfg.items():            # legacy: no mode suffix
        if k.startswith("bot_registry_") and f"_{broker}" in k and isinstance(v, dict):
            return v
    if isinstance(cfg.get("bot_registry"), dict):
        return cfg["bot_registry"]
    return None


@app.get("/desktop-config")
def get_desktop_config(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    import json as _json
    p = _desktop_config_path(user["id"])
    if p.exists():
        try:
            return {"config": _json.loads(p.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return {"config": None}


@app.put("/desktop-config")
def put_desktop_config(payload: dict,
                       authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    cfg = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        raise HTTPException(400, "Body must be a JSON object {config: {...}}.")
    import json as _json
    p = _desktop_config_path(user["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(cfg), encoding="utf-8")
    return {"ok": True, "keys": len(cfg)}


# ── V7.1.12: per-user cloud schedule ───────────────────────────────

@app.get("/schedule")
def api_get_schedule(authorization: str | None = Header(default=None)):
    """Returns the list of bot sides this user has scheduled to
    auto-run on the cloud when the US market is open."""
    user = _current_user(authorization)
    return {"bots": scheduler.get_schedule(user["id"])}


@app.put("/schedule")
def api_set_schedule(payload: dict,
                     authorization: str | None = Header(default=None)):
    """Replace the scheduled-bot list. Body: {"bots": ["LONG","DAY",...]}.
    Empty list disables the cloud schedule for this user — the
    reconciliation loop will stop any bots it had started."""
    user  = _current_user(authorization)
    sides = payload.get("bots", []) if isinstance(payload, dict) else []
    if not isinstance(sides, list):
        raise HTTPException(400, "Body must be {'bots': [...]}")
    scheduler.set_schedule(user["id"], [str(s) for s in sides])
    return {"ok": True, "bots": scheduler.get_schedule(user["id"])}


# ──────────────────────────────────────────────────────────────────────────────
# V7.1+ — Public bot marketplace
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/bots")
def list_public_bots(q: str = "", tag: str = "",
                     limit: int = 50, offset: int = 0):
    """Browse the public bot library (no auth required to view)."""
    return {"bots": marketplace.list_bots(
        q=q, tag=tag, limit=min(limit, 100), offset=offset
    )}


# NOTE (V4.6.68): the catch-all GET /bots/{slug} is registered LATER in this
# file (after the static /bots/v2, /bots/mine, /bots/running, /bots/philosophies
# routes). FastAPI matches routes in definition order, so defining it here
# shadowed those static paths ("Bot not found" on the marketplace browse).


@app.get("/bots/{slug}/download")
def download_public_bot(slug: str,
                        authorization: str | None = Header(default=None)):
    """V3.3.0 — also records a purchase row + refuses to serve flagged
    or removed bots. Records the buyer's user_id when an auth header
    is supplied so moderation refunds know who to credit."""
    bot = marketplace.get_bot(slug)
    if not bot or bot.get("status") in ("flagged", "removed"):
        raise HTTPException(404, "Bot not available (may have been removed).")
    data = marketplace.read_bot_file(slug)
    if data is None:
        raise HTTPException(404, "Bot file not found.")
    # V4.0.0 — pre-flight balance check for PAID bots
    price = int(bot.get("price_credits", 0))
    # V4.6.117 — identify the buyer once; a bot's OWNER downloads their own bot
    # for FREE (no charge, no balance gate) — you don't buy what you published.
    user = None
    if authorization:
        try:
            user = _current_user(authorization)
        except Exception:
            user = None
    is_owner = bool(user and int(bot.get("owner_id", -1)) == int(user["id"]))
    charge = price > 0 and not is_owner
    if charge:
        if user is None:
            raise HTTPException(401, "Sign in to purchase paid bots.")
        bal = credits_mod.get_balance(user["id"])
        if bal < price:
            raise HTTPException(
                402,
                f"Insufficient credits — this bot costs {price} ◊, "
                f"you have {bal}. Top up first.")
    marketplace.increment_downloads(slug)
    # Log purchase + distribute revenue if we can identify the buyer
    try:
        if user is not None:
            moderation.record_purchase(user["id"], slug,
                                       credits_paid=(price if charge else 0))
            if charge:
                revenue.distribute_purchase(
                    seller_id=int(bot["owner_id"]),
                    total_credits=price,
                    bot_slug=slug,
                )
                from . import credits as _cr
                _cr.grant(user["id"], -price, f"purchase: {slug}")
    except Exception as _e:
        print(f"[purchase] {slug}: {_e}", flush=True)
    return Response(
        content=data, media_type="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="{slug}.py"'},
    )


@app.post("/bots")
async def upload_public_bot(
    name:        str = Form(...),
    description: str = Form(""),
    tags:        str = Form(""),     # comma-separated
    philosophy:  str = Form(""),     # V3.1.1
    price_credits: int = Form(0),    # V3.1.1
    visibility:  str = Form("public"),
    creator_ai:  str = Form(""),     # V4.1.0 — AI used to generate the bot
    runner_ai:   str = Form(""),     # V4.1.0 — AI used to run/score candidates
    broker:      str = Form(""),     # V4.1.0 — compatible broker (alpaca/ibkr/both)
    universe:    str = Form(""),     # V4.6.75 (#8) — public universe name or ''
    file:        UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = _current_user(authorization)
    # V3.3.0 — enforce publish-ban window from a previous moderation removal
    banned, until = moderation.is_user_publish_banned(user["id"])
    if banned:
        raise HTTPException(
            403,
            f"You're currently publish-banned (a previous bot was removed "
            f"by moderation). Ban lifts on {until}. "
            f"This doesn't affect bots you've already published — they "
            f"stay live.")
    blob = await file.read()
    # V3.3.0 — similarity gate (block ≥85 %, warn ≥60 %).
    # V4.6.8 — exclude the user's OWN previously-published bots from
    # the gate. Users are allowed to upload improved versions of
    # their own work; the gate is meant to stop them from copying
    # OTHER people's bots, not their own. Marketplace versioning
    # (proper diff against META.version + green-badge UX for buyers)
    # is the v4.6.9 step; this v4.6.8 fix unblocks the immediate
    # 'I can't republish my own crypto bot' user pain point.
    src_text = blob.decode("utf-8", errors="replace")
    own_slugs = []
    try:
        own_bots = marketplace.list_bots_for_owner(user["id"])
        own_slugs = [b.get("slug") for b in own_bots if b.get("slug")]
    except Exception:
        pass
    # V4.6.78 — compare against EVERY bot (including the user's own) so we can
    # catch identical re-publishes. Two rules:
    #   • ≥97% similar to ANY bot (incl. your own) → near-identical DUPLICATE,
    #     blocked. This stops the "3 copies of my crypto bot" problem.
    #   • ≥85% similar to a DIFFERENT owner's bot → copy of someone else's
    #     work, blocked. Your own bots in the 85–97% band are still allowed
    #     (genuine improvements / re-publishes).
    all_matches = similarity.compare_against_marketplace(
        src_text, marketplace.MARKETPLACE_DIR, skip_slugs=[])
    if all_matches and all_matches[0]["score"] >= 0.97:
        raise HTTPException(
            409,
            f"This is a near-identical DUPLICATE of '{all_matches[0]['slug']}' "
            f"(score {all_matches[0]['score']:.0%}). Edit that bot instead of "
            f"publishing another copy.")
    non_own = [m for m in all_matches if m.get("slug") not in own_slugs]
    matches = non_own   # keep the downstream similarity_warning band behaviour
    if non_own and non_own[0]["score"] >= 0.85:
        raise HTTPException(
            409,
            f"Too similar to '{non_own[0]['slug']}' "
            f"(score {non_own[0]['score']:.0%}). "
            f"Consider basing yours on that bot rather than re-publishing "
            f"a near-duplicate.")
    try:
        row = marketplace.upload_bot(
            owner_id=user["id"],
            name=name.strip(),
            description=description.strip(),
            tags=[t for t in tags.split(",") if t.strip()],
            file_bytes=blob,
            philosophy=philosophy,
            price_credits=price_credits,
            visibility=visibility,
            creator_ai=creator_ai,
            runner_ai=runner_ai,
            broker=broker,
            universe=universe,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Attach the similarity warning band when score >= 60 % but < 85 %
    if matches:
        row["similarity_warning"] = matches
    return row


@app.delete("/bots/{slug}")
def api_delete_own_bot(slug: str,
                       authorization: str | None = Header(default=None)):
    """V4.6.79 — owner unpublishes their OWN public bot. Used when the user
    deletes a personally-made bot in the desktop app, so it disappears from
    the market too. Only the owner can delete; admins use /admin/.../remove."""
    user = _current_user(authorization)
    ok = marketplace.delete_bot(slug=slug, owner_id=user["id"])
    if not ok:
        raise HTTPException(404, "No published bot by that slug that you own.")
    return {"ok": True, "deleted": slug}


@app.post("/bots/check-similarity")
async def check_similarity(
    file: UploadFile = File(...),
    skip_slug: str = Form(""),
    authorization: str | None = Header(default=None),
):
    """Standalone pre-flight: hand a candidate .py to the server, get
    back the top matches without actually publishing. Used by the
    desktop's publish dialog to show a warning before uploading."""
    _current_user(authorization)
    blob = await file.read()
    src_text = blob.decode("utf-8", errors="replace")
    matches = similarity.compare_against_marketplace(
        src_text, marketplace.MARKETPLACE_DIR,
        skip_slug=skip_slug.strip() or None)
    return {"matches": matches}


# ──────────────────────────────────────────────────────────────────────────────
# V3.3.0 — Moderation endpoints (admin-only) + revocation sync (any user)
# ──────────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# V4.0.2 — Private bot upload (custom bots → cloud-run without publishing)
# ──────────────────────────────────────────────────────────────────────────────


@app.post("/bots/private/upload")
async def api_private_bot_upload(
    slug:   str        = Form(...),
    file:   UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Upload a .py file to the user's private cloud-bot folder. Lets
    locally-created custom bots run on Oracle without first going
    through the public marketplace. The file is stored at
    /opt/apex_users/user_<id>/private_bots/<slug>.py and used by
    bot_runner when start_bot() is called with the same slug."""
    user = _current_user(authorization)
    slug_clean = slug.strip().lower()
    if not slug_clean or not slug_clean.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400,
            "Slug must be alphanumeric (plus - and _).")
    if slug_clean.upper() in ("LONG", "SHORT", "DAY"):
        raise HTTPException(400,
            "Slug collides with a built-in bot — pick a different name.")
    blob = await file.read()
    if len(blob) > 1_000_000:
        raise HTTPException(400, "Bot script too large (max 1 MB).")
    if not blob.lstrip().startswith((b"#", b"\"\"\"", b"'''", b"import",
                                       b"from", b"def", b"class")):
        raise HTTPException(400, "File doesn't look like a Python script.")
    dest_dir = bot_runner.private_bots_dir(user["id"])
    dest = dest_dir / f"{slug_clean}.py"
    dest.write_bytes(blob)
    return {"ok": True, "slug": slug_clean,
            "size_bytes": len(blob), "stored_at": str(dest)}


@app.post("/bots/private/upload_universe")
async def api_private_universe_upload(
    filename: str      = Form(...),
    file:     UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """V4.6.19 — upload a universe .txt file to the user's data dir on
    Oracle so cloud-running trading bots can read it. Universe scripts
    run locally on the user's laptop, so without this hop a crypto
    bot on Oracle would never see the latest universe the script
    produced.

    File ends up at /opt/apex_users/user_<id>/<filename>. The bot's
    load_universe() looks there via APEX_DATA_DIR/<filename>."""
    user = _current_user(authorization)
    clean = filename.strip()
    if not clean.endswith("_universe.txt"):
        raise HTTPException(400,
            "filename must end with _universe.txt")
    # Reject path traversal
    if "/" in clean or "\\" in clean or ".." in clean:
        raise HTTPException(400, "filename must be a basename, no path parts")
    blob = await file.read()
    if len(blob) > 100_000:  # 100 KB cap — universe files are tiny
        raise HTTPException(400, "universe file too large (max 100 KB)")
    user_dir = bot_runner.private_bots_dir(user["id"]).parent  # data dir
    dest = user_dir / clean
    try:
        dest.write_bytes(blob)
    except Exception as e:
        raise HTTPException(500, f"write failed: {e}")
    return {"ok": True, "filename": clean,
            "size_bytes": len(blob), "stored_at": str(dest)}


@app.get("/bots/private")
def api_private_bot_list(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    d = bot_runner.private_bots_dir(user["id"])
    return {"bots": [p.stem for p in sorted(d.glob("*.py"))]}


@app.delete("/bots/private/{slug}")
def api_private_bot_delete(slug: str,
                            authorization: str | None = Header(default=None)):
    """V4.6.105 — FULLY remove a custom bot from the cloud so a deleted bot can
    never linger (running, kept-alive by the watchdog, or as a ghost script):
      1) stop it on every broker + drop it from the always-on desired registry
      2) delete its uploaded script."""
    user = _current_user(authorization)
    s = slug.upper()
    stopped = []
    for broker in ("alpaca", "ibkr"):
        try:
            bot_runner.stop_bot(user["id"], s, broker, user_initiated=True)
            stopped.append(broker)
        except Exception as e:
            print(f"[private-delete] stop {s}/{broker}: {e}", flush=True)
    p = bot_runner.private_bots_dir(user["id"]) / f"{slug.lower()}.py"
    removed = False
    try:
        if p.exists():
            p.unlink()
            removed = True
    except Exception as e:
        print(f"[private-delete] unlink {slug}: {e}", flush=True)
    return {"ok": True, "stopped": stopped, "script_removed": removed}


@app.post("/admin/bots/{slug}/flag")
def api_admin_flag_bot(slug: str, payload: dict,
                       authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    reason = (payload or {}).get("reason", "").strip() or "(no reason given)"
    result = moderation.flag_bot(slug, reason=reason, moderator_id=user["id"])
    if not result.get("ok"):
        raise HTTPException(404, "Bot not found or already removed.")
    return result


@app.post("/admin/bots/{slug}/unflag")
def api_admin_unflag_bot(slug: str,
                         authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    result = moderation.unflag_bot(slug)
    if not result.get("ok"):
        raise HTTPException(404, "Bot not currently flagged.")
    return result


@app.post("/admin/bots/{slug}/remove")
def api_admin_remove_bot(slug: str, payload: dict,
                          authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    reason = (payload or {}).get("reason", "").strip() or "(no reason given)"
    result = moderation.remove_bot(slug, reason=reason, moderator_id=user["id"])
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "Removal failed."))
    return result


# ──────────────────────────────────────────────────────────────────────────────
# V4.0.1 — Terms of Service acceptance
# Desktop pops a modal on first launch + after each rebrand bump; the user
# must accept before the app unlocks. Server is the source of truth.
# ──────────────────────────────────────────────────────────────────────────────

TOS_VERSION = "1.0"
TOS_TEXT = """\
BAPTOU TRADING PLATFORM — TERMS & CONDITIONS  (v1.0)

By using BAPTOU Trading Platform (the "App"), you agree to the following:

1. AS-IS SERVICE
The App is provided as-is, without warranty of any kind. Trading bot
performance is not a guarantee of future results. Markets are volatile;
past performance is not indicative of future returns.

2. ASSUMPTION OF RISK
You assume all risk for trades executed by bots running through your
linked broker accounts. You authorise the App to place orders on your
behalf in accordance with each bot's logic. You are solely responsible
for monitoring your bots and your broker account.

3. NO LIABILITY FOR LOSSES (general rule)
Neither the App, its creators, contributors, hosts, nor any affiliated
parties shall be liable for any monetary losses, missed gains, slippage,
broker downtime, or other financial damages incurred while using the
App or any bot purchased / downloaded through it. This includes losses
caused by a bot's strategy, code defects, market conditions, broker
outages, or any third-party API issue.

4. MODERATION REFUND POLICY (the one exception)
If a published bot is flagged and removed by App moderators for
misrepresenting its behaviour, every buyer of that bot is refunded
the credits they paid for it. **This refund covers the purchase price
only.** The App is NOT liable for trading losses incurred while
running such a bot before its removal — only for the sale itself.

5. CREDITS
APEX / BAPTOU credits are a virtual currency used internally for
marketplace transactions. 1 credit conventionally = US $0.01. Credits
are not yet redeemable for real money; cash-out becomes available
when the App connects to a real banking provider in a future release.
Credit balances are kept on the App's centralised server.

6. PUBLISHER OBLIGATIONS
If you publish a bot, you warrant that:
  - You own or have the right to distribute the code
  - Your published description matches the bot's actual behaviour
  - You do not knowingly distribute malicious code

Misrepresentation may result in moderation removal, refunds to buyers,
a publish ban, and a fixed credit fine.

7. ACCOUNT TERMINATION
The App reserves the right to suspend or terminate accounts that
violate these terms or applicable laws.

8. PRIVACY
The App stores email, username, hashed password, optional Google
sub identifier, optional phone, and your encrypted broker API keys.
Your bot code, AI generation prompts, and trade logs are stored on
your local machine and (when cloud-running) on the App's server.

9. GOVERNING LAW
These terms are governed by the laws of the user's home jurisdiction
where mandatory, otherwise by the laws of the App operator's home
country.

By clicking "I Accept", you confirm that you have read, understood,
and agreed to the above.
"""


@app.get("/auth/tos")
def auth_tos_get(authorization: str | None = Header(default=None)):
    """Returns the current T&C text + the version + whether THIS user
    has accepted the current version. Desktop hits this on launch."""
    user = _current_user(authorization)
    accepted_at = user.get("accepted_tos_at")
    return {
        "version":     TOS_VERSION,
        "text":        TOS_TEXT,
        "accepted_at": accepted_at,
        "needs_accept": not bool(accepted_at),
    }


@app.post("/auth/tos/accept")
def auth_tos_accept(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    database.mark_tos_accepted(user["id"])
    return {"ok": True, "version": TOS_VERSION}


@app.get("/admin/moderation/flags")
def api_admin_moderation_flags(
    only_active: bool = True,
    authorization: str | None = Header(default=None)):
    """V4.6.16 — list every algorithmic-moderation flag raised by
    auto_moderation. Admin-only."""
    user = _current_user(authorization)
    _require_admin(user)
    from . import auto_moderation as _auto_mod
    return {"flags": _auto_mod.list_flags(only_active=only_active)}


@app.post("/admin/moderation/flags/{flag_id}/clear")
def api_admin_clear_flag(
    flag_id: int,
    authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    from . import auto_moderation as _auto_mod
    _auto_mod.clear_flag(flag_id)
    return {"ok": True}


@app.post("/admin/moderation/run")
def api_admin_moderation_run(
    authorization: str | None = Header(default=None)):
    """Manually trigger both algorithmic checks. Returns the
    summary so the admin sees what was flagged."""
    user = _current_user(authorization)
    _require_admin(user)
    from . import auto_moderation as _auto_mod
    return _auto_mod.run_all_checks()


# V4.6.16 — background runner: kicks the algorithmic moderation
# checks every hour. Lives in a daemon thread so it dies with the
# uvicorn process and never blocks shutdown.
def _start_moderation_cron():
    import threading
    def _loop():
        import time as _t
        while True:
            try:
                from . import auto_moderation as _auto_mod
                result = _auto_mod.run_all_checks()
                if result.get("new_copyright") or result.get("new_metadata"):
                    print(f"[auto-mod] flagged "
                          f"{len(result['new_copyright'])} copyright + "
                          f"{len(result['new_metadata'])} metadata issues",
                          flush=True)
            except Exception as e:
                print(f"[auto-mod] loop error: {e}", flush=True)
            _t.sleep(3600)  # hourly
    threading.Thread(target=_loop, daemon=True,
                     name="auto_moderation").start()


_start_moderation_cron()


# V4.6.27 — server-side bot auto-start scheduler. Runs in a daemon
# thread inside uvicorn so cloud bots auto-start at the US market
# open even when the user's desktop APEX is closed.
def _start_auto_scheduler():
    try:
        from . import auto_scheduler as _as
        _as.start_cron()
    except Exception as e:
        print(f"[startup] auto_scheduler init failed: {e}", flush=True)
_start_auto_scheduler()


# V4.6.28 — bot performance aggregator for optimal-value recs.
def _start_bot_optimums():
    try:
        from . import bot_optimums as _bo
        _bo.init_tables()
        _bo.start_cron()
    except Exception as e:
        print(f"[startup] bot_optimums init failed: {e}", flush=True)
_start_bot_optimums()


@app.get("/api/bots/{slug}/optimums", include_in_schema=False)
def api_bot_optimums(slug: str,
                     authorization: str | None = Header(default=None)):
    """V4.6.28 — return pooled optimal-setting recommendations for a
    bot, computed across all users. Client shows these as hints next
    to each setting control."""
    _ = _current_user(authorization)  # require auth
    from . import bot_optimums as _bo
    return _bo.get_optimums(slug)


@app.get("/admin/revenue-split")
def api_revenue_split_get(authorization: str | None = Header(default=None)):
    """Any admin can SEE the split. Only BOSS_ADMIN can change it."""
    user = _current_user(authorization)
    _require_admin(user)
    return {
        "split":           revenue.get_split(),
        "running_balance": revenue.get_running_balance(),
        "fixed_cost_note": "Running balance grows from every paid bot "
                            "sale and represents the pool that funds "
                            "APEX_*_KEY env vars + server hosting.",
    }


@app.put("/admin/revenue-split")
def api_revenue_split_set(payload: dict,
                          authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user, min_role="BOSS_ADMIN")
    try:
        new = revenue.set_split(
            seller_pct  = int(payload.get("seller_pct",  90)),
            admin_pct   = int(payload.get("admin_pct",   5)),
            running_pct = int(payload.get("running_pct", 5)),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "split": new}


@app.get("/bots/mine/revocations")
def api_my_revocations(authorization: str | None = Header(default=None)):
    """Desktop polls this on startup. Returns slugs the user had
    installed but were removed by moderation. The desktop removes
    them from the local registry + DATA_DIR/bots/{slug}.py(.apex).
    Each slug is returned exactly once — subsequent calls return [].
    """
    user = _current_user(authorization)
    slugs = moderation.list_revocations_for_user(user["id"])
    return {"revoked_slugs": slugs}


@app.delete("/bots/{slug}")
def delete_public_bot(slug: str,
                      authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    if not marketplace.delete_bot(slug=slug, owner_id=user["id"]):
        raise HTTPException(404, "Bot not found or not yours.")
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# V7.1+ — Web dashboard (phone-accessible)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def web_root(request: Request):
    """V7.1.14: render the public landing page instead of redirecting
    to /web/login. New visitors see the download CTA + a short pitch;
    signed-in visitors see their name + 'Open dashboard' in the top
    right. /web/login and /web/dashboard remain reachable directly."""
    user = web.user_from_cookie(request)
    return web.landing_page(user)


@app.get("/web/tos", include_in_schema=False)
def web_tos_page():
    """V4.0.1 — public T&C page, linked from every footer + cookie banner."""
    return web.tos_page()


@app.get("/web/login", include_in_schema=False)
def web_login_form(request: Request):
    return web.login_page()


@app.post("/web/login", include_in_schema=False)
def web_login_submit(identifier: str = Form(...),
                     password:   str = Form(...)):
    ident = identifier.strip().lower()
    user = database.get_user_by_email(ident) or database.get_user_by_username(ident)
    if not user or not auth.verify_password(password, user["hashed_password"]):
        return web.login_page(error="Invalid email/username or password.")
    token = auth.create_token(user["id"], user["email"])
    resp = RedirectResponse(url="/web/dashboard", status_code=303)
    # 30-day cookie (matches JWT expiry). HttpOnly so JS can't steal it.
    resp.set_cookie("apex_token", token, max_age=60 * 60 * 24 * 30,
                    httponly=True, samesite="lax")
    return resp


@app.get("/web/signup", include_in_schema=False)
def web_signup_form(request: Request):
    return web.signup_page()


@app.post("/web/signup", include_in_schema=False)
def web_signup_submit(
    email:        str = Form(...),
    password:     str = Form(...),
    password2:    str = Form(...),
    display_name: str = Form(""),
    username:     str = Form(""),
):
    """Mirror of POST /auth/signup, but renders HTML errors and sets a
    session cookie on success so the user goes straight to the dashboard."""
    pf = {"email": email, "display_name": display_name, "username": username}
    email_l = email.strip().lower()
    if not email_l or "@" not in email_l:
        return web.signup_page(error="Invalid email address.", prefill=pf)
    if len(password) < 8:
        return web.signup_page(
            error="Password must be at least 8 characters.", prefill=pf)
    if password != password2:
        return web.signup_page(error="Passwords do not match.", prefill=pf)
    if database.get_user_by_email(email_l):
        return web.signup_page(
            error="An account with this email already exists.", prefill=pf)

    base = (username or "").strip().lower() or email_l.split("@")[0]
    uname = base
    if database.get_user_by_username(uname):
        uname = f"{base}{random.randint(100, 999)}"

    dn = (display_name or "").strip() or uname
    hashed = auth.hash_password(password)
    user   = database.create_user(uname, email_l, hashed, dn)
    token  = auth.create_token(user["id"], user["email"])
    resp   = RedirectResponse(url="/web/dashboard", status_code=303)
    resp.set_cookie("apex_token", token, max_age=60 * 60 * 24 * 30,
                    httponly=True, samesite="lax")
    return resp


@app.get("/web/logout", include_in_schema=False)
def web_logout():
    resp = RedirectResponse(url="/web/login")
    resp.delete_cookie("apex_token")
    return resp


@app.get("/web/dashboard", include_in_schema=False)
def web_dashboard(request: Request, tab: str = "overview"):
    user = web.user_from_cookie(request)
    if not user:
        return RedirectResponse(url="/web/login")
    return web.dashboard_page(user, tab=tab)


# ──────────────────────────────────────────────────────────────────────────────
# V7.1.1 — Web dashboard live data + bot controls (JSON, cookie-authed)
# ──────────────────────────────────────────────────────────────────────────────

def _web_user(request: Request) -> dict:
    user = web.user_from_cookie(request)
    if not user:
        raise HTTPException(401, "Not signed in.")
    return user


def _alpaca_client_for(user_id: int, side: str, mode: str = "paper"):
    """Build an Alpaca TradingClient from the user's stored credentials,
    or return None when keys aren't linked or alpaca-py isn't installed.

    V4.6.135 — `mode` selects the account: 'paper' uses ALPACA_API_KEY_<SIDE>
    (paper=True); 'live' uses the separate ALPACA_API_KEY_LIVE_<SIDE> namespace
    (paper=False). Defaults to paper so existing callers are unaffected."""
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        return None
    data = creds.load_credentials(user_id) or {}
    live = str(mode).lower() == "live"
    suffix = "LIVE_" if live else ""
    k = data.get(f"ALPACA_API_KEY_{suffix}{side}")
    s = data.get(f"ALPACA_SECRET_KEY_{suffix}{side}")
    if not k or not s:
        return None
    try:
        return TradingClient(k, s, paper=not live)
    except Exception:
        return None


def _bot_state(user_id: int, side: str, broker: str = "alpaca") -> dict:
    """Returns equity / today's P/L / position count / running flag for one
    bot on a given broker. V4.6.65 — broker-aware + real running state (was
    hard-coded False, so the site never showed bots as running)."""
    b = (broker or "alpaca").lower()
    try:
        running = bool(bot_runner.is_running(user_id, side, b))
    except Exception:
        running = False

    if b == "ibkr":
        # Equity + holdings come from the bot's server-side sub-portfolio ledger
        # (no Alpaca account for IBKR).
        try:
            for led in bot_runner.list_ibkr_ledgers(user_id):
                if str(led.get("bot_id", "")).upper() == side.upper():
                    h = led.get("holdings", {}) or {}
                    npos = len([1 for v in h.values()
                                if abs(float(v or 0)) > 1e-9])
                    # V4.6.129 — real DAY P/L from the slice's previous-close
                    # baseline (was always None, so the site showed no IBKR P/L).
                    val  = float(led.get("value", 0) or 0)
                    base = float(led.get("day_baseline", 0) or 0)
                    tpl  = (val - base) if base > 0 else None
                    tpct = ((val - base) / base * 100) if base > 0 else None
                    return {"equity": led.get("value"), "today_pl": tpl,
                            "today_pct": tpct, "positions": npos,
                            "running": running, "broker": "ibkr"}
        except Exception:
            pass
        return {"equity": None, "today_pl": None, "today_pct": None,
                "positions": None, "running": running, "broker": "ibkr"}

    c = _alpaca_client_for(user_id, side)
    if c is None:
        return {"equity": None, "today_pl": None, "today_pct": None,
                "positions": None, "running": running, "broker": "alpaca"}
    try:
        acct = c.get_account()
        equity = float(acct.equity)
        last   = float(acct.last_equity)
        pl     = equity - last
        pct    = (pl / last * 100) if last else 0.0
        positions = c.get_all_positions()
        return {
            "equity":    equity,
            "today_pl":  pl,
            "today_pct": pct,
            "positions": len(positions),
            "running":   running,
            "broker":    "alpaca",
        }
    except Exception:
        return {"equity": None, "today_pl": None, "today_pct": None,
                "positions": None, "running": running, "broker": "alpaca"}


@app.get("/web/api/status", include_in_schema=False)
def web_api_status(request: Request):
    """V4.6.14 — dashboard now reports state for every bot the user
    has, not just the three built-ins. Discovers custom bots by
    scanning the user's private_bots/ directory on the server; sends
    a `sides` list so the JS dashboard can render rows for the full
    set (e.g. crypto bot shows up alongside LONG / SHORT / DAY)."""
    user = _web_user(request)
    linked = bool(creds.load_credentials(user["id"]))
    broker = (request.query_params.get("broker") or "alpaca").lower()

    # V4.6.105 — drive the dashboard from the user's SYNCED registry (active +
    # silenced), so bots they deleted disappear and silenced bots are flagged.
    silenced_set: set[str] = set()
    reg = _user_bot_registry(user["id"], broker, "paper")
    if reg is not None:
        active   = [str(s) for s in (reg.get("active") or [])]
        silenced = [str(s) for s in (reg.get("silenced") or [])]
        silenced_set = {s for s in silenced}
        # active (non-silenced) first, then silenced — registry is the truth
        ordered = [s for s in active if s not in silenced_set] + silenced
        seen: set[str] = set()
        sides = [s for s in ordered if not (s in seen or seen.add(s))]
    else:
        # Legacy fallback (user hasn't synced a config yet): built-ins + a
        # private_bots/*.py scan.
        sides = ["LONG", "SHORT", "DAY"]
        try:
            priv_dir = bot_runner.private_bots_dir(user["id"])
            if priv_dir.exists():
                for p in sorted(priv_dir.glob("*.py")):
                    slug = p.stem.upper()
                    if slug not in sides:
                        sides.append(slug)
        except Exception as e:
            print(f"[web_api_status] custom-bot scan failed: {e}")

    bots = {}
    for side in sides:
        st = _bot_state(user["id"], side, broker)
        st["silenced"] = side in silenced_set
        bots[side] = st
    running_count = sum(1 for s in bots.values() if s.get("running"))
    return {"linked": linked, "sides": sides, "bots": bots,
            "broker": broker, "running_count": running_count}


@app.post("/web/api/bots/{side}/start", include_in_schema=False)
def web_api_bot_start(side: str, request: Request):
    user   = _web_user(request)
    broker = (request.query_params.get("broker") or "alpaca").lower()
    result = bot_runner.start_bot(user["id"], side, broker)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "start failed"))
    return result


@app.post("/web/api/bots/{side}/stop", include_in_schema=False)
def web_api_bot_stop(side: str, request: Request):
    user   = _web_user(request)
    broker = (request.query_params.get("broker") or "alpaca").lower()
    result = bot_runner.stop_bot(user["id"], side, broker, user_initiated=True)
    if not result.get("ok"):
        raise HTTPException(500, result.get("detail", "stop failed"))
    return result


# ──────────────────────────────────────────────────────────────────────────────
# v1.2.0 — Free AI generation for the MAKE BOT tab. Uses APEX's own
# pooled Anthropic key (server env: APEX_ANTHROPIC_KEY) with Haiku to
# keep cost low. Rate-limited per user.
# ──────────────────────────────────────────────────────────────────────────────

import os as _os
import time as _time
import threading as _threading

_MAKEBOT_RATE: dict = {}                # user_id -> [timestamps]
_MAKEBOT_RATE_LOCK = _threading.Lock()
_MAKEBOT_RATE_WINDOW = 3600             # 1 hour
_MAKEBOT_RATE_LIMIT  = 5                # 5 calls per hour per user
MAKEBOT_COST_CREDITS = 10               # V3.1.4 — legacy default cost
SIGNUP_BONUS_CREDITS = 100              # V3.1.4 — every new signup gets a starting balance

# V4.6.16 — per-model credit pricing for Via-APEX generations.
# Lookup key = the `model` field of the request (the Make Bot UI
# sends the model id from its dropdown). Anything not listed falls
# back to MAKEBOT_COST_CREDITS. Own-key providers (user supplies their
# own API key in Tools) bypass this endpoint entirely and cost ZERO
# APEX credits — confirmed in the desktop client by routing to the
# provider's URL directly, not /api/makebot/generate.
MAKEBOT_MODEL_COSTS = {
    # Cheap / fast tier
    "baptou-haiku":                3,
    "claude-haiku-4-5-20251001":   3,
    "gpt-4o-mini":                 3,
    "gemini-2.5-flash":            2,
    "gemini-2.0-flash":            2,
    "llama-3.1-8b-instant":        1,
    # Mid tier
    "gemini-2.5-pro":              8,
    "gpt-4o":                      12,
    "llama-3.3-70b-versatile":     4,
    # Premium / Opus
    "claude-sonnet-4-5-20250929":  20,
    "gpt-5":                       25,
    "claude-opus-4-7-20251101":    40,
}


def _makebot_cost_for(model: str) -> int:
    """Look up the credit cost for a given Make Bot model id. Falls
    back to MAKEBOT_COST_CREDITS (10) if the model isn't in the
    pricing table — keeps unknown / new models from running free."""
    return int(MAKEBOT_MODEL_COSTS.get((model or "").strip(),
                                       MAKEBOT_COST_CREDITS))


def _check_makebot_rate(user_id: int) -> int:
    """Return remaining calls in the window (>=0). 0 = rate-limited."""
    now = _time.time()
    with _MAKEBOT_RATE_LOCK:
        hits = [t for t in _MAKEBOT_RATE.get(user_id, [])
                if (now - t) < _MAKEBOT_RATE_WINDOW]
        _MAKEBOT_RATE[user_id] = hits
        if len(hits) >= _MAKEBOT_RATE_LIMIT:
            return 0
        hits.append(now)
        return _MAKEBOT_RATE_LIMIT - len(hits)


@app.get("/api/makebot/pricing")
def api_makebot_pricing():
    """V4.6.16 — pricing table for the Make Bot credit-cost badge.
    Client reads on tab activate and shows per-model price. Own-key
    providers (the user supplies their own API key) bypass this
    endpoint and cost zero APEX credits."""
    return {
        "model_costs":  MAKEBOT_MODEL_COSTS,
        "default_cost": MAKEBOT_COST_CREDITS,
        "own_key_cost": 0,
    }


# V4.6.16 — AI usage aggregator for the web home page (v4.0.0 spec).
# Scans every public marketplace bot for its declared creator_ai +
# runner_ai fields, plus tallies recent make-bot generations from the
# credits-ledger description column. Used by the dashboard's AI USAGE
# pie chart + rolling line chart.
@app.get("/web/api/ai-stats", include_in_schema=False)
def web_api_ai_stats():
    """Aggregate AI provider usage across the whole platform.
    Returns:
      providers:  {"groq": 42, "anthropic": 12, …}  – bots created per provider
      runners:    {"anthropic": 88, "openai": 14, …} – bots' runtime AI
      total_bots: total public bots in marketplace
      gen_log:    last 100 make-bot generations [{provider, model, ts}, …]
    """
    out = {
        "providers":  {},
        "runners":    {},
        "total_bots": 0,
        "gen_log":    [],
    }
    # Provider usage from the marketplace
    try:
        rows = marketplace.list_bots(q="", tag="", section="all",
                                     owner_id=None, page=1, per_page=500)
        bots = rows.get("bots", []) if isinstance(rows, dict) else rows
        out["total_bots"] = len(bots)
        for b in bots:
            c = (b.get("creator_ai") or "unknown").lower().strip()
            r = (b.get("runner_ai")  or "unknown").lower().strip()
            out["providers"][c] = out["providers"].get(c, 0) + 1
            out["runners"][r]   = out["runners"].get(r, 0) + 1
    except Exception as e:
        print(f"[ai-stats] marketplace scan failed: {e}")
    # Recent make-bot generations from the credits-ledger
    try:
        with credits_mod._conn() as _c:
            cur = _c.execute(
                "SELECT delta, reason, created_at FROM credits_ledger "
                "WHERE reason LIKE 'make-bot %' "
                "ORDER BY id DESC LIMIT 100"
            )
            for delta, reason, ts in cur.fetchall():
                # reason format: 'make-bot <model> (pool|fallback)'
                parts = reason.split(" ")
                model = parts[1] if len(parts) >= 2 else "?"
                out["gen_log"].append({
                    "model": model,
                    "credits": abs(int(delta or 0)),
                    "ts": ts,
                })
    except Exception as e:
        print(f"[ai-stats] credits-ledger scan failed: {e}")
    return out


@app.get("/web/api/ai-returns", include_in_schema=False)
def web_api_ai_returns():
    """V4.6.22 — return cumulative % return over time grouped by AI
    provider. Two series:
      creator: keyed by META.ai_used (which AI BUILT the bot)
      runner:  keyed by META.runner_ai / META.ai_used (which AI RUNS it)
    Source: every running cloud bot's *_lifetime.jsonl snapshot file
    in /opt/apex_users/user_*/. Returns are cumulative from each bot's
    FIRST snapshot, so the chart shows bot-lifetime returns, not
    account-lifetime.
    Each point: {ts: epoch_seconds, pct: cumulative_return_percent}.
    Multiple bots under the same AI get averaged at each timestamp."""
    import json as _j
    from pathlib import Path as _P
    from collections import defaultdict
    import os as _os

    users_dir = _P(_os.environ.get(
        "APEX_USERS_DIR", "/opt/apex_users"))
    out_creator = defaultdict(list)  # ai_used -> [(ts, pct)]
    out_runner  = defaultdict(list)
    if not users_dir.exists():
        return {"creator": {}, "runner": {}}

    # V4.6.129 — readable default model per provider when the user didn't pin one.
    _PROV_DEFAULT = {"anthropic": "claude-haiku-4-5",
                     "google": "gemini-2.0-flash", "xai": "grok-2",
                     "groq": "llama-3.3-70b"}
    for user_dir in users_dir.glob("user_*"):
        # V4.6.129 — resolve this user's synced AI config so BUILT-IN bots
        # (LONG/SHORT/DAY — no private_bots META) are grouped by the model they
        # actually run on, instead of 'unknown'.
        try:
            _uid = int(str(user_dir.name).split("_")[-1])
        except Exception:
            _uid = None
        _blob = {}
        if _uid is not None:
            try:
                _blob = creds.load_credentials(_uid) or {}
            except Exception:
                _blob = {}

        def _model_for(side_slug: str) -> str:
            s = side_slug.upper()
            prov = (_blob.get(f"AI_PROVIDER_{s}") or _blob.get("AI_PROVIDER")
                    or "anthropic").lower()
            model = (_blob.get(f"AI_MODEL_{s}") or _blob.get("AI_MODEL")
                     or "").strip()
            return (model or _PROV_DEFAULT.get(prov, prov)).lower()

        # rglob → catch nested per-broker / per-mode dirs (ibkr/, alpaca-live/),
        # which is where IBKR + live bots write their lifetime snapshots.
        for ll in user_dir.rglob("*_lifetime.jsonl"):
            slug = ll.stem.replace("_lifetime", "")
            # Find the bot's META block (creator_ai / runner_ai)
            bot_py = user_dir / "private_bots" / f"{slug.lower()}.py"
            creator_ai, runner_ai = "", ""
            if bot_py.exists():
                try:
                    src = bot_py.read_text(encoding="utf-8",
                                           errors="replace")
                    try:
                        from . import bot_meta as _bm
                    except ImportError:
                        import bot_meta as _bm  # type: ignore
                    meta = _bm.parse_meta(src) or {}
                    creator_ai = (meta.get("ai_used") or "").lower()
                    runner_ai  = (meta.get("compatible_models") or
                                  [meta.get("ai_used")])
                    runner_ai  = runner_ai[0] if isinstance(runner_ai, list) \
                                 else runner_ai
                    runner_ai  = (runner_ai or "").lower()
                except Exception:
                    pass
            # Built-ins / custom bots without META → group by the user's AI model
            if not creator_ai or creator_ai == "unknown":
                creator_ai = _model_for(slug)
            if not runner_ai or runner_ai == "unknown":
                runner_ai = _model_for(slug)
            # Read snapshots
            try:
                with open(ll, "r", encoding="utf-8") as f:
                    rows = [_j.loads(l) for l in f if l.strip()]
            except Exception:
                continue
            if len(rows) < 2:
                continue
            base = float(rows[0].get("equity") or 0) or 1.0
            from datetime import datetime as _dt
            for r in rows:
                try:
                    ts = _dt.fromisoformat(r["ts"]).timestamp()
                    eq = float(r.get("equity") or 0)
                    pct = (eq - base) / base * 100
                    out_creator[creator_ai].append((ts, pct))
                    out_runner[runner_ai].append((ts, pct))
                except Exception:
                    continue

        # V4.6.129 — ledgers are the server-side source of truth for cloud bots
        # (there are no *_lifetime.jsonl server-side). Derive a start->now return
        # line per slice (allocated_cash -> last_value) so the AI charts plot
        # real data, grouped by the model each bot runs on.
        from datetime import datetime as _dt2
        for lg in user_dir.rglob("ledgers/*.json"):
            nm = lg.name
            if nm.startswith("account_") or nm.endswith(".rebalance.json"):
                continue
            try:
                lj = _j.loads(lg.read_text(encoding="utf-8"))
            except Exception:
                continue
            alloc = float(lj.get("allocated_cash") or 0)
            lastv = float(lj.get("last_value") or 0)
            if alloc <= 0 or lastv <= 0:
                continue
            lslug = str(lj.get("bot_id") or lg.stem.split("_")[0]).lower()
            lmodel = _model_for(lslug)
            try:
                t0 = _dt2.fromisoformat(lj.get("created")).timestamp()
                t1 = _dt2.fromisoformat(lj.get("updated")).timestamp()
            except Exception:
                continue
            lret = (lastv / alloc - 1.0) * 100.0
            out_creator[lmodel] += [(t0, 0.0), (t1, lret)]
            out_runner[lmodel]  += [(t0, 0.0), (t1, lret)]

    # Aggregate: average pct per timestamp per AI (rounded to nearest
    # 5 minutes so points from different bots align)
    def _aggregate(series):
        agg = {}
        for ai, points in series.items():
            bucket = defaultdict(list)
            for ts, pct in points:
                slot = int(ts // 300) * 300  # 5-min buckets
                bucket[slot].append(pct)
            ordered = sorted(bucket.keys())
            agg[ai] = [{"ts": k, "pct": sum(bucket[k]) / len(bucket[k])}
                       for k in ordered]
        return agg

    return {"creator": _aggregate(out_creator),
            "runner":  _aggregate(out_runner)}


@app.post("/api/makebot/generate")
def api_makebot_generate(payload: dict,
                         authorization: str | None = Header(default=None)):
    """V3.1.4 — Pooled Anthropic Haiku call gated by APEX credits.
    Costs MAKEBOT_COST_CREDITS per successful generation. Bearer-auth.
    Rate-limited as a secondary safeguard against runaway loops."""
    user = _current_user(authorization)

    # V4.6.16 — per-model pricing. Client sends 'model' in payload;
    # we look up its cost. Older clients without that field default
    # to the legacy MAKEBOT_COST_CREDITS (10).
    requested_model = (payload or {}).get("model", "") or "baptou-haiku"
    cost = _makebot_cost_for(requested_model)

    # 1. Pre-flight credit check
    balance = credits_mod.get_balance(user["id"])
    if balance < cost:
        raise HTTPException(
            402,                                   # Payment Required
            f"Insufficient APEX credits — need {cost} for "
            f"{requested_model}, you have {balance}. Switch to a cheaper "
            f"model in the Make Bot dropdown, plug in your own API key "
            f"(zero credits charged), or ask an admin to grant more credits.")

    # 2. Rate limit (defence-in-depth even after credits)
    if _check_makebot_rate(user["id"]) <= 0:
        raise HTTPException(
            429,
            f"Rate limit reached: {_MAKEBOT_RATE_LIMIT} APEX-pooled calls "
            f"per hour per user. Wait a bit or use your own API key.")

    # 3. Find an Anthropic key — APEX-pooled first, then the user's
    # own synced credential as a fallback (so the feature works even
    # before the server admin sets APEX_ANTHROPIC_KEY).
    server_key = (_os.environ.get("APEX_ANTHROPIC_KEY")
                  or _os.environ.get("ANTHROPIC_API_KEY"))
    used_pool = bool(server_key)
    if not server_key:
        try:
            stored = creds.load_credentials(user["id"]) or {}
            server_key = (stored.get("ANTHROPIC_API_KEY")
                          or stored.get("ANTHROPIC_KEY") or "")
        except Exception:
            server_key = ""
    if not server_key:
        raise HTTPException(503,
            "No Anthropic key available. Either ask the server admin to "
            "set APEX_ANTHROPIC_KEY, or sync your own key via the desktop "
            "Tools → ACCOUNT LINKING flow.")

    prompt = (payload or {}).get("prompt", "").strip()
    system = (payload or {}).get("system", "").strip()
    if not prompt or not system:
        raise HTTPException(400, "Missing 'prompt' or 'system' field.")
    try:
        import anthropic
    except ImportError:
        raise HTTPException(503,
            "Server missing 'anthropic' package. Run: pip install anthropic.")

    # 4. Make the call
    try:
        client = anthropic.Anthropic(api_key=server_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(p, "text", "") for p in resp.content)
    except Exception as e:
        raise HTTPException(500, f"Anthropic call failed: {e}")

    # 5. Charge credits AFTER successful generation. Both pool + fallback
    # paths charge — the "via APEX" pricing is the same regardless of
    # which key the server happens to use under the hood.
    result = credits_mod.grant(
        user["id"], -cost,
        f"make-bot {requested_model} ({'pool' if used_pool else 'fallback'})",
    )
    return {
        "text":             text,
        "credits_charged":  cost,
        "model":            requested_model,
        "new_balance":      result["balance"],
        "key_source":       "pool" if used_pool else "user-fallback",
    }


@app.get("/api/makebot/price")
def api_makebot_price(authorization: str | None = Header(default=None)):
    """Lets the desktop know the current cost + the caller's balance so it
    can render an accurate label on the 'Free via APEX' option."""
    user = _current_user(authorization)
    return {
        "cost":    MAKEBOT_COST_CREDITS,
        "balance": credits_mod.get_balance(user["id"]),
    }


# ──────────────────────────────────────────────────────────────────────────────
# V7.1.11 — Cloud-bot start/stop/status/logs  (bearer-auth, JSON)
# Same actions as /web/api/bots/{side}/* but for the desktop app and any
# external client. The /web/ variants stay cookie-authed for the phone.
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/bots/{side}/start")
def api_bot_start(side: str, broker: str | None = None,
                  authorization: str | None = Header(default=None)):
    # V4.6.41 — optional ?broker= lets the same side run on Alpaca AND IBKR
    # concurrently. Omitted → the user's per-side default (back-compat).
    user   = _current_user(authorization)
    result = bot_runner.start_bot(user["id"], side, broker)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "start failed"))
    return result


@app.post("/bots/{side}/stop")
def api_bot_stop(side: str, broker: str | None = None,
                 authorization: str | None = Header(default=None)):
    user   = _current_user(authorization)
    result = bot_runner.stop_bot(user["id"], side, broker, user_initiated=True)
    if not result.get("ok"):
        raise HTTPException(500, result.get("detail", "stop failed"))
    return result


@app.get("/bots/{side}/status")
def api_bot_status(side: str, broker: str | None = None,
                   authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return bot_runner.status(user["id"], side, broker)


@app.get("/bots/{side}/logs")
def api_bot_logs(side: str, tail: int = 4000, broker: str | None = None,
                 authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return {"side": side.upper(),
            "log":  bot_runner.tail_log(user["id"], side,
                                        n_chars=min(tail, 50_000),
                                        broker=broker)}


@app.get("/ibkr/ledgers")
def api_ibkr_ledgers(authorization: str | None = Header(default=None)):
    """V4.6.51 — each IBKR bot's sub-portfolio snapshot (cash, holdings, live
    value) so the desktop can show an allocation % that tracks performance."""
    user = _current_user(authorization)
    return {"ledgers": bot_runner.list_ibkr_ledgers(user["id"]),
            "account": bot_runner.read_ibkr_account(user["id"])}


@app.get("/ibkr/{side}/fills")
def api_ibkr_fills(side: str, mode: str = "paper", limit: int = 500,
                   authorization: str | None = Header(default=None)):
    """V4.6.63 — a cloud IBKR bot's recorded fills, for the desktop's Trade
    History / Recent Closed Trades / Trade Summary."""
    user = _current_user(authorization)
    return {"fills": bot_runner.list_ibkr_fills(user["id"], side, mode, limit)}


@app.post("/ibkr/{side}/liquidate")
def api_ibkr_liquidate(side: str, mode: str = "paper",
                       authorization: str | None = Header(default=None)):
    """V4.6.70 — stop a cloud IBKR bot, market-sell its sub-portfolio and delete
    its ledger so the desktop can remove it from the allocation table."""
    user = _current_user(authorization)
    return bot_runner.liquidate_ibkr_bot(user["id"], side, mode)


@app.post("/ibkr/{side}/rebalance")
def api_ibkr_rebalance(side: str, target_pct: float, mode: str = "paper",
                       authorization: str | None = Header(default=None)):
    """V4.6.51 — lower a bot's allocation: the running bot sells holdings next
    cycle to hand the excess cash back to the main account."""
    user = _current_user(authorization)
    return bot_runner.request_ibkr_rebalance(user["id"], side, target_pct, mode)


@app.post("/ibkr/reconcile")
def api_ibkr_reconcile(mode: str = "paper", execute: bool = False,
                       authorization: str | None = Header(default=None)):
    """V4.6.133 — reconcile the real IBKR account vs the bots' sub-portfolio
    ledgers. execute=false → report orphan (untracked) positions; execute=true →
    market-flatten the untracked excess so the account matches the bots."""
    user = _current_user(authorization)
    return bot_runner.reconcile_ibkr_orphans(user["id"], mode, execute=execute)


@app.post("/ibkr/reconcile_ledger")
def api_ibkr_reconcile_ledger(mode: str = "paper", execute: bool = False,
                              authorization: str | None = Header(default=None)):
    """V4.6.134 — mirror of /ibkr/reconcile: correct the bots' sub-portfolio
    ledgers DOWN to the real IBKR account. execute=false → report the phantom
    over-counts; execute=true → snap each over-counting ledger to reality and
    true-up cash at the mark price (writes ledger files only, places no orders)."""
    user = _current_user(authorization)
    return bot_runner.reconcile_ledger_to_broker(user["id"], mode, execute=execute)


@app.get("/bots/running")
def api_bots_running(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return {"bots": bot_runner.list_running(user["id"])}


@app.get("/universes")
def api_list_universes():
    """V4.6.73 — public themed universes (long_term / short / short_term /
    speculative / crypto / options), regenerated weekly on the server."""
    try:
        from . import universe_factory as uf
        return {"universes": uf.list_universes()}
    except Exception as e:
        return {"universes": [], "error": str(e)}


@app.get("/universes/{name}")
def api_get_universe(name: str):
    try:
        from . import universe_factory as uf
        txt = uf.read_universe(name)
        if txt is None:
            raise HTTPException(404, "Universe not found.")
        return Response(content=txt, media_type="text/plain")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


_UNIVERSE_REGEN_RUNNING = False


@app.post("/admin/universes/regenerate")
def api_regen_universes(x_admin_token: str | None = Header(default=None)):
    """V4.6.74 — regenerate ALL public universes on demand (don't wait for the
    Monday cron). Gated by the APEX_ADMIN_TOKEN env var. Runs in a background
    thread (yfinance over ~250 tickers takes a minute) so the request returns
    immediately."""
    global _UNIVERSE_REGEN_RUNNING
    want = _os.environ.get("APEX_ADMIN_TOKEN", "")
    if not want:
        raise HTTPException(503, "APEX_ADMIN_TOKEN is not set on the server.")
    if x_admin_token != want:
        raise HTTPException(403, "Bad admin token.")
    if _UNIVERSE_REGEN_RUNNING:
        return {"ok": True, "status": "already_running"}

    def _run():
        global _UNIVERSE_REGEN_RUNNING
        _UNIVERSE_REGEN_RUNNING = True
        try:
            from . import universe_factory as uf
            print("[universe-factory] manual regeneration starting…",
                  flush=True)
            counts = uf.generate_all()
            print(f"[universe-factory] manual regeneration done: {counts}",
                  flush=True)
        except Exception as e:
            print(f"[universe-factory] manual regeneration failed: {e}",
                  flush=True)
        finally:
            _UNIVERSE_REGEN_RUNNING = False

    import threading
    threading.Thread(target=_run, daemon=True,
                     name="universe_regen").start()
    return {"ok": True, "status": "started"}


@app.get("/web/api/bots/{side}/positions", include_in_schema=False)
def web_api_bot_positions(side: str, request: Request):
    """Return open positions for one bot's Alpaca account."""
    user = _web_user(request)
    c = _alpaca_client_for(user["id"], side.upper())
    if c is None:
        return {"positions": [], "linked": False}
    try:
        raw = c.get_all_positions()
        return {
            "positions": [
                {
                    "symbol":       p.symbol,
                    "qty":          float(p.qty),
                    "entry":        float(p.avg_entry_price),
                    "current":      float(p.current_price) if p.current_price else 0,
                    "pl":           float(p.unrealized_pl),
                    "pl_pct":       float(p.unrealized_plpc) * 100,
                    "market_value": float(p.market_value),
                }
                for p in raw
            ],
            "linked": True,
        }
    except Exception as e:
        return {"positions": [], "linked": True, "error": str(e)}


@app.get("/web/api/portfolio/history", include_in_schema=False)
def web_api_portfolio_history(request: Request,
                               side: str = "LONG", period: str = "1W"):
    """Return daily equity snapshots for one bot over the given period."""
    user = _web_user(request)
    c = _alpaca_client_for(user["id"], side.upper())
    if c is None:
        return {"history": [], "linked": False}
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        valid_periods = {"1D", "1W", "1M", "3M", "6M", "1A"}
        p = period if period in valid_periods else "1W"
        hist = c.get_portfolio_history(
            filter=GetPortfolioHistoryRequest(period=p, timeframe="1D")
        )
        result = []
        for ts, eq, pl in zip(
            getattr(hist, "timestamp", []),
            getattr(hist, "equity",    []),
            getattr(hist, "profit_loss", []),
        ):
            if eq is not None:
                result.append({"time": str(ts), "equity": eq,
                                "pl": pl or 0})
        return {"history": result, "linked": True}
    except Exception as e:
        return {"history": [], "linked": True, "error": str(e)}


# V4.6.129 — public market snapshot for the landing page graph (always has data,
# unlike the marketplace AI-return charts which are empty until users publish).
_MARKET_CACHE: dict = {"ts": 0.0, "data": None}


@app.get("/web/api/market", include_in_schema=False)
def web_api_market():
    """S&P 500 / Nasdaq / Dow — last ~30 daily closes as % from the start of the
    window. Cached 10 min so the landing page never hammers yfinance."""
    import time as _t
    if _MARKET_CACHE["data"] and (_t.time() - _MARKET_CACHE["ts"]) < 600:
        return _MARKET_CACHE["data"]
    out: dict = {}
    try:
        import yfinance as yf
        for nm, sym in (("S&P 500", "SPY"), ("Nasdaq", "QQQ"), ("Dow", "DIA")):
            try:
                h = yf.Ticker(sym).history(period="1mo")
                closes = [float(c) for c in h["Close"].tolist() if c == c]
                if len(closes) >= 2 and closes[0]:
                    base = closes[0]
                    out[nm] = [round((c / base - 1) * 100, 3) for c in closes]
            except Exception:
                pass
    except Exception as e:
        print(f"[web_api_market] {e}")
    data = {"series": out}
    _MARKET_CACHE.update({"ts": _t.time(), "data": data})
    return data


@app.get("/web/api/bot-types", include_in_schema=False)
def web_api_bot_types():
    """Platform breakdown of bots by kind: AI (LLM-driven), Algorithmic
    (rules/indicators, no LLM) and Machine Learning. Built-ins LONG/SHORT/DAY
    are AI; custom bots are classified from their META."""
    from pathlib import Path as _P
    import os as _os, json
    counts = {"AI": 0, "Algorithmic": 0, "Machine Learning": 0}
    users_dir = _P(_os.environ.get("APEX_USERS_DIR", "/opt/apex_users"))
    if not users_dir.exists():
        return {"counts": counts}
    try:
        from . import bot_meta as _bm
    except ImportError:
        import bot_meta as _bm  # type: ignore
    seen = set()
    for user_dir in users_dir.glob("user_*"):
        bots = set()
        # Server-side bots are tracked by their slice ledgers (no lifetime files).
        for lg in user_dir.rglob("ledgers/*.json"):
            nm = lg.name
            if nm.startswith("account_") or nm.endswith(".rebalance.json"):
                continue
            try:
                bid = json.loads(lg.read_text(encoding="utf-8")).get("bot_id")
            except Exception:
                bid = None
            bots.add(str(bid or lg.stem.split("_")[0]).lower())
        for slug in bots:
            key = (user_dir.name, slug)
            if key in seen:
                continue
            seen.add(key)
            if slug in ("long", "short", "day"):
                counts["AI"] += 1
                continue
            cat = "Algorithmic"
            bot_py = user_dir / "private_bots" / f"{slug}.py"
            if bot_py.exists():
                try:
                    meta = _bm.parse_meta(
                        bot_py.read_text(encoding="utf-8", errors="replace")) or {}
                    t = str(meta.get("bot_type") or meta.get("type") or "").lower()
                    if "ml" in t or "machine" in t or "learning" in t:
                        cat = "Machine Learning"
                    elif meta.get("ai_used") or meta.get("compatible_models"):
                        cat = "AI"
                except Exception:
                    pass
            counts[cat] += 1
    return {"counts": counts}


# ──────────────────────────────────────────────────────────────────────────────
# V3 wave 5 — Credits + admin role hierarchy
# Role precedence: BOSS_ADMIN > SUB_BOSS_ADMIN > ADMIN > USER
# Only BOSS_ADMIN can promote/demote other admins. ADMIN can grant
# credits and view the user list but can't change roles.
# ──────────────────────────────────────────────────────────────────────────────


def _require_admin(user: dict, min_role: str = "ADMIN") -> None:
    rank = {"USER": 0, "ADMIN": 1, "SUB_BOSS_ADMIN": 2, "BOSS_ADMIN": 3}
    if rank.get(user.get("role", "USER"), 0) < rank.get(min_role, 1):
        raise HTTPException(403, f"Requires {min_role} role.")


@app.get("/credits/me")
def api_credits_me(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return {
        "balance":      credits_mod.get_balance(user["id"]),
        "transactions": credits_mod.list_transactions(user["id"], limit=20),
    }


@app.get("/admin/users")
def api_admin_list_users(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    rows = database.list_all_users(limit=500)
    out = []
    for r in rows:
        r2 = {k: v for k, v in r.items() if k != "hashed_password"}
        r2["credit_balance"] = credits_mod.get_balance(r["id"])
        out.append(r2)
    return {"users": out}


@app.post("/admin/credits/grant")
def api_admin_grant_credits(payload: dict,
                            authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    target_id = (payload or {}).get("user_id")
    delta     = (payload or {}).get("delta", 0)
    reason    = (payload or {}).get("reason", "admin grant")
    if not target_id:
        raise HTTPException(400, "Missing user_id.")
    if not database.get_user_by_id(int(target_id)):
        raise HTTPException(404, "Target user not found.")
    try:
        delta = int(delta)
    except Exception:
        raise HTTPException(400, "delta must be an integer number of credits.")
    if delta == 0:
        raise HTTPException(400, "delta cannot be zero.")
    result = credits_mod.grant(int(target_id), delta, reason,
                                granted_by=user["id"])
    return {"ok": True, **result, "target": int(target_id)}


@app.put("/admin/users/{user_id}/role")
def api_admin_set_role(user_id: int, payload: dict,
                       authorization: str | None = Header(default=None)):
    actor = _current_user(authorization)
    _require_admin(actor, min_role="BOSS_ADMIN")
    role = (payload or {}).get("role", "")
    try:
        target = database.set_user_role(user_id, role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not target:
        raise HTTPException(404, "User not found.")
    return _public(target)


# ──────────────────────────────────────────────────────────────────────────────
# V3 wave 5 — Bot Market: filters + classify + publisher analytics + friend bots
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/bots/philosophies")
def api_bots_philosophies():
    return {"philosophies": marketplace.get_distinct_philosophies()}


@app.get("/bots/mine")
def api_bots_mine(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    bots = marketplace.list_bots_for_owner(user["id"])
    return {"bots": bots, "total_downloads": sum(b["downloads"] for b in bots)}


@app.put("/bots/{slug}")
def api_bot_update(slug: str, payload: dict,
                   authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Body must be a JSON object.")
    ok = marketplace.update_bot_meta(slug=slug, owner_id=user["id"], **payload)
    if not ok:
        raise HTTPException(404, "Bot not found or not yours.")
    return marketplace.get_bot(slug)


@app.put("/admin/bots/{slug}/classify")
def api_admin_classify(slug: str, payload: dict,
                       authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    _require_admin(user)
    ok = marketplace.admin_classify(
        slug,
        featured=payload.get("featured"),
        recommended=payload.get("recommended"),
    )
    if not ok:
        raise HTTPException(400, "Nothing to classify.")
    return marketplace.get_bot(slug)


@app.get("/users/{username}/bots")
def api_user_bots(username: str,
                  authorization: str | None = Header(default=None)):
    viewer = _current_user(authorization)
    target = database.get_user_by_username(username.strip().lower())
    if not target:
        raise HTTPException(404, "User not found.")
    settings = friends.get_share_settings(target["id"])
    is_friend = friends.are_friends(viewer["id"], target["id"])
    is_self   = viewer["id"] == target["id"]
    if is_self:
        bots = marketplace.list_bots_for_owner(target["id"])
    elif is_friend and settings.get("share_bots_friends"):
        bots = marketplace.list_bots_visible_to_friend(target["id"])
    elif settings.get("share_bots_public"):
        bots = [b for b in marketplace.list_bots_visible_to_friend(target["id"])
                if b.get("visibility") == "public"]
    else:
        bots = []
    return {
        "bots":               bots,
        "allow_bot_download": bool(settings.get("allow_bot_download", 0)),
        "is_friend":          is_friend,
        "is_self":            is_self,
    }


# Enhance the existing /bots endpoint with the new filter knobs.
# We keep the original signature as a thin wrapper.
@app.get("/bots/v2")
def api_bots_v2(q: str = "", tag: str = "", philosophy: str = "",
                 max_price: int = -1, min_win_rate: float = 0.0,
                 section: str = "", sort: str = "downloads",
                 limit: int = 50, offset: int = 0,
                 authorization: str | None = Header(default=None)):
    owner_id = None
    if section == "mine":
        user = _current_user(authorization)
        owner_id = user["id"]
    return {"bots": marketplace.list_bots(
        q=q, tag=tag, philosophy=philosophy,
        max_price=max_price if max_price >= 0 else None,
        min_win_rate=min_win_rate, section=section, owner_id=owner_id,
        sort=sort, limit=min(limit, 100), offset=offset,
    )}


# V4.6.68 — catch-all bot detail route, registered AFTER every static /bots/*
# route so it no longer shadows them (was returning "Bot not found" for
# /bots/v2, /bots/mine, etc., breaking the marketplace browse).
@app.get("/bots/{slug}")
def get_public_bot(slug: str):
    row = marketplace.get_bot(slug)
    if not row:
        raise HTTPException(404, "Bot not found.")
    return row


# ──────────────────────────────────────────────────────────────────────────────
# V3.0.0 — Friends + share-settings
# All endpoints are bearer-authed.
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/friends")
def api_friends_list(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return friends.list_friends(user["id"])


@app.post("/friends/request")
def api_friend_request(payload: dict,
                       authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    target_username = (payload or {}).get("username", "").strip().lower()
    target_id_raw   = (payload or {}).get("user_id")
    target = None
    if target_username:
        target = database.get_user_by_username(target_username)
    elif target_id_raw is not None:
        try:
            target = database.get_user_by_id(int(target_id_raw))
        except Exception:
            target = None
    if not target:
        raise HTTPException(404, "User not found.")
    result = friends.send_request(user["id"], target["id"])
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "Request failed."))
    return result


@app.post("/friends/{friendship_id}/respond")
def api_friend_respond(friendship_id: int, payload: dict,
                       authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    accept = bool((payload or {}).get("accept", False))
    result = friends.respond_to_request(user["id"], friendship_id, accept)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "Response failed."))
    return result


@app.delete("/friends/{other_user_id}")
def api_friend_remove(other_user_id: int,
                      authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return friends.remove_friend(user["id"], other_user_id)


@app.get("/friends/search")
def api_friend_search(q: str = "", limit: int = 20,
                      authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return {"users": friends.search_users(q, limit=min(limit, 50),
                                          exclude_id=user["id"])}


@app.get("/share-settings")
def api_share_get(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return friends.get_share_settings(user["id"])


@app.put("/share-settings")
def api_share_set(payload: dict,
                  authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Body must be a JSON object.")
    return friends.set_share_settings(user["id"], payload)


@app.get("/users/{username}/profile")
def api_user_profile(username: str,
                     authorization: str | None = Header(default=None)):
    """Return whatever the target user has chosen to share with this
    viewer. Friends see the 'friends' tier; non-friends see only the
    'public' tier (and may not see anything at all)."""
    viewer = _current_user(authorization)
    target = database.get_user_by_username(username.strip().lower())
    if not target:
        raise HTTPException(404, "User not found.")
    settings = friends.get_share_settings(target["id"])
    is_friend = friends.are_friends(viewer["id"], target["id"])
    is_self   = viewer["id"] == target["id"]
    tier      = "self" if is_self else ("friends" if is_friend else "public")

    def visible(key: str) -> bool:
        if is_self:
            return True
        if is_friend and settings.get(f"share_{key}_friends"):
            return True
        if settings.get(f"share_{key}_public"):
            return True
        return False

    out = {
        "user":      {"id": target["id"], "username": target["username"],
                      "display_name": target["display_name"]},
        "tier":      tier,
        "is_friend": is_friend,
        "shows": {
            "broker":  visible("broker"),
            "daily":   visible("daily"),
            "monthly": visible("monthly"),
            "yearly":  visible("yearly"),
            "bots":    visible("bots"),
        },
        "allow_bot_download": bool(settings.get("allow_bot_download", 0))
                              and (is_friend or settings.get("share_bots_public")),
    }
    # Pull live data only for fields the viewer can see — saves Alpaca
    # round-trips for fields that would just be redacted.
    if any(out["shows"].values()):
        # Broker is trivially derivable from the user's stored creds.
        if out["shows"]["broker"]:
            blob = creds.load_credentials(target["id"]) or {}
            brokers = []
            if any(k.startswith("ALPACA_API_KEY") for k in blob):
                brokers.append("Alpaca")
            if any(k.startswith("IBKR_") for k in blob):
                brokers.append("IBKR")
            out["broker"] = brokers
    # Per-period P&L is computed on demand from the target's Alpaca
    # accounts. Aggregate across LONG/SHORT/DAY for a single headline
    # number per period; the client can request a finer breakdown later.
    if out["shows"]["daily"] or out["shows"]["monthly"] or out["shows"]["yearly"]:
        out["pl"] = _pl_for_user(target["id"], out["shows"])
    return out


# ──────────────────────────────────────────────────────────────────────────────
# V4.2.0 — Stripe credit top-ups
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/payments/packages")
def api_payments_packages():
    """Public — returns available credit packages + whether Stripe is live."""
    return {
        "configured": payments.is_configured(),
        "packages": [
            {
                "key":          k,
                "label":        v["label"],
                "credits":      v["credits"],
                "amount_cents": v["amount_cents"],
                "popular":      v.get("popular", False),
            }
            for k, v in payments.CREDIT_PACKAGES.items()
        ],
    }


@app.post("/payments/create-checkout")
def api_create_checkout(payload: dict,
                         authorization: str | None = Header(default=None)):
    """Create a Stripe Checkout Session and return the redirect URL.
    Body: {package_key: str, server_url: str (optional)}"""
    user = _current_user(authorization)
    package_key = (payload or {}).get("package_key", "").strip()
    # server_url lets the desktop tell us its own public address so the
    # success/cancel pages point back to this server instance.
    server_url = (payload or {}).get("server_url", "").strip()
    if not server_url:
        server_url = _os.environ.get("APEX_PUBLIC_URL", "http://145.241.170.165:8000")
    if not package_key:
        raise HTTPException(400, "Missing package_key.")
    if not payments.is_configured():
        raise HTTPException(
            503,
            "Payment gateway not configured on this server. "
            "Ask the server admin to set STRIPE_SECRET_KEY.")
    # success_url uses the literal Stripe placeholder {CHECKOUT_SESSION_ID}
    # — it must NOT be inside an f-string or it would raise NameError.
    success_url = server_url + "/payments/success?session_id={CHECKOUT_SESSION_ID}"
    cancel_url  = server_url + "/payments/cancel"
    try:
        result = payments.create_checkout_session(
            user_id     = user["id"],
            package_key = package_key,
            success_url = success_url,
            cancel_url  = cancel_url,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return result


@app.post("/payments/webhook", include_in_schema=False)
async def api_stripe_webhook(request: Request):
    """Stripe sends POST here on checkout.session.completed.
    No bearer auth — Stripe signs the raw body with STRIPE_WEBHOOK_SECRET."""
    payload_bytes = await request.body()
    sig_header    = request.headers.get("stripe-signature", "")
    if not sig_header:
        raise HTTPException(400, "Missing Stripe-Signature header.")
    try:
        payments.handle_webhook(payload_bytes, sig_header)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        print("[stripe webhook] unhandled: " + str(e), flush=True)
        raise HTTPException(500, "Webhook processing failed.")
    return {"ok": True}


@app.get("/payments/success", include_in_schema=False)
def api_payments_success(session_id: str = ""):
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Payment successful — BAPTOU</title>
  <style>
    body{background:#0c0f16;color:#d8dde8;font-family:sans-serif;
         display:flex;align-items:center;justify-content:center;
         min-height:100vh;margin:0;}
    .card{text-align:center;padding:48px 40px;max-width:420px;}
    h2{color:#3eb8a4;font-size:26px;letter-spacing:3px;margin-bottom:12px;}
    p{color:#8899aa;line-height:1.7;}
    .icon{font-size:60px;margin-bottom:20px;}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10003;</div>
    <h2>PAYMENT SUCCESSFUL</h2>
    <p>Your credits have been added to your BAPTOU account.<br>
       Close this tab, return to the desktop app, and click&nbsp;<b>&#8635;</b>
       to see your new balance.</p>
  </div>
</body>
</html>""")


@app.get("/payments/cancel", include_in_schema=False)
def api_payments_cancel():
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Payment cancelled — BAPTOU</title>
  <style>
    body{background:#0c0f16;color:#d8dde8;font-family:sans-serif;
         display:flex;align-items:center;justify-content:center;
         min-height:100vh;margin:0;}
    .card{text-align:center;padding:48px 40px;max-width:420px;}
    h2{color:#e07b54;font-size:24px;letter-spacing:3px;margin-bottom:12px;}
    p{color:#8899aa;line-height:1.7;}
    .icon{font-size:60px;margin-bottom:20px;}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10005;</div>
    <h2>PAYMENT CANCELLED</h2>
    <p>No charge was made. Close this tab and return to the app
       whenever you&rsquo;re ready.</p>
  </div>
</body>
</html>""")


def _pl_for_user(user_id: int, shows: dict) -> dict:
    """Aggregate P&L across all linked bot accounts for the periods
    the viewer is allowed to see."""
    period_map = {"daily": "1D", "monthly": "1M", "yearly": "1A"}
    result: dict = {}
    for label, alpaca_period in period_map.items():
        if not shows.get(label):
            continue
        total_pl = 0.0
        first_eq = 0.0
        last_eq  = 0.0
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            for side in ("LONG", "SHORT", "DAY"):
                c = _alpaca_client_for(user_id, side)
                if c is None:
                    continue
                h = c.get_portfolio_history(
                    GetPortfolioHistoryRequest(period=alpaca_period,
                                                timeframe="1D"))
                eq = [e for e in (getattr(h, "equity", []) or []) if e]
                if len(eq) >= 2:
                    total_pl += eq[-1] - eq[0]
                    first_eq += eq[0]
                    last_eq  += eq[-1]
        except Exception:
            pass
        pct = ((last_eq / first_eq) - 1) * 100 if first_eq else 0.0
        result[label] = {"pl": round(total_pl, 2),
                          "pct": round(pct, 2)}
    return result


# ── leaderboard (V4.6.135) ──────────────────────────────────────────────────
_LB_PERIODS = {"daily": "1D", "weekly": "1W", "monthly": "1M"}
_LB_CACHE: dict = {}          # (user_id, period, mode) -> (ts, {"pct","pl"})
_LB_TTL = 300                 # seconds — re-use a user's return for 5 min


def _period_perf(user_id: int, period: str, mode: str):
    """Aggregate % return + $ P/L across a user's LONG/SHORT/DAY Alpaca
    accounts for one period+mode. Returns None when the user has no linked
    keys / no data for that account type (so they're left off the board).
    Cached per (user, period, mode) for _LB_TTL seconds."""
    import time as _t
    key = (user_id, period, mode)
    hit = _LB_CACHE.get(key)
    if hit and (_t.time() - hit[0]) < _LB_TTL:
        return hit[1]
    alpaca_period = _LB_PERIODS.get(period, "1D")
    total_pl = 0.0
    first_eq = 0.0
    last_eq  = 0.0
    have_data = False
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        for side in ("LONG", "SHORT", "DAY"):
            c = _alpaca_client_for(user_id, side, mode)
            if c is None:
                continue
            try:
                h = c.get_portfolio_history(
                    GetPortfolioHistoryRequest(period=alpaca_period,
                                               timeframe="1D"))
            except Exception:
                continue
            eq = [e for e in (getattr(h, "equity", []) or []) if e]
            if len(eq) >= 2:
                have_data = True
                total_pl += eq[-1] - eq[0]
                first_eq += eq[0]
                last_eq  += eq[-1]
    except Exception:
        pass
    if not have_data:
        _LB_CACHE[key] = (_t.time(), None)
        return None
    pct = ((last_eq / first_eq) - 1) * 100 if first_eq else 0.0
    out = {"pct": round(pct, 2), "pl": round(total_pl, 2)}
    _LB_CACHE[key] = (_t.time(), out)
    return out


@app.get("/leaderboard")
def api_leaderboard(period: str = "daily", mode: str = "paper",
                    scope: str = "global",
                    authorization: str | None = Header(default=None)):
    """V4.6.135 — ranked % return for opted-in users.
      period ∈ daily|weekly|monthly   mode ∈ paper|live   scope ∈ global|friends
    Opt-in only (friends.leaderboard_user_ids). The viewer always sees their own
    row even if not opted in; everyone else must have leaderboard_optin=1."""
    viewer = _current_user(authorization)
    period = period if period in _LB_PERIODS else "daily"
    mode   = "live" if str(mode).lower() == "live" else "paper"
    scope  = "friends" if str(scope).lower() == "friends" else "global"

    ids = friends.leaderboard_user_ids(viewer["id"], scope)
    entries = []
    for uid in ids:
        perf = _period_perf(uid, period, mode)
        if perf is None:
            # Keep the viewer visible (as 0) even with no data for this account.
            if uid != viewer["id"]:
                continue
            perf = {"pct": 0.0, "pl": 0.0}
        u = database.get_user_by_id(uid) or {}
        entries.append({
            "user_id":      uid,
            "username":     u.get("username", "?"),
            "display_name": u.get("display_name") or u.get("username", "?"),
            "pct":          perf["pct"],
            "pl":           perf["pl"],
            "is_self":      uid == viewer["id"],
            "opted_in":     uid != viewer["id"] or _viewer_opted_in(viewer["id"]),
        })
    entries.sort(key=lambda e: e["pct"], reverse=True)
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    return {"period": period, "mode": mode, "scope": scope,
            "you_opted_in": _viewer_opted_in(viewer["id"]),
            "entries": entries}


def _viewer_opted_in(user_id: int) -> bool:
    try:
        return bool(friends.get_share_settings(user_id).get("leaderboard_optin"))
    except Exception:
        return False


@app.post("/web/api/bots/{side}/liquidate", include_in_schema=False)
def web_api_bot_liquidate(side: str, request: Request):
    """Emergency: close every open position on this bot's Alpaca account
    at market. Works as long as the user has linked broker creds —
    doesn't require the bot service to be running."""
    user = _web_user(request)
    c = _alpaca_client_for(user["id"], side)
    if c is None:
        raise HTTPException(
            400, "No Alpaca keys linked for this bot. Sync them from "
                 "the desktop app's Tools tab first.")
    try:
        c.close_all_positions(cancel_orders=True)
        return {"ok": True, "detail": f"Liquidation order sent for {side}."}
    except Exception as e:
        raise HTTPException(500, f"Alpaca rejected the request: {e}")

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
               credits as credits_mod)
from .schemas import SignupRequest, LoginRequest


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    creds.init_credentials_table()
    marketplace.init_marketplace_table()
    friends.init_friends_db()
    credits_mod.init_credits_tables()
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
    description="Authentication backend for APEX Trading Platform",
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


@app.get("/bots/{slug}")
def get_public_bot(slug: str):
    row = marketplace.get_bot(slug)
    if not row:
        raise HTTPException(404, "Bot not found.")
    return row


@app.get("/bots/{slug}/download")
def download_public_bot(slug: str):
    data = marketplace.read_bot_file(slug)
    if data is None:
        raise HTTPException(404, "Bot file not found.")
    marketplace.increment_downloads(slug)
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
    file:        UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = _current_user(authorization)
    blob = await file.read()
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
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return row


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


def _alpaca_client_for(user_id: int, side: str):
    """Build an Alpaca TradingClient from the user's stored credentials,
    or return None when keys aren't linked or alpaca-py isn't installed."""
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        return None
    data = creds.load_credentials(user_id) or {}
    k = data.get(f"ALPACA_API_KEY_{side}")
    s = data.get(f"ALPACA_SECRET_KEY_{side}")
    if not k or not s:
        return None
    try:
        return TradingClient(k, s, paper=True)
    except Exception:
        return None


def _bot_state(user_id: int, side: str) -> dict:
    """Returns equity / today's P/L / position count / running flag for
    one bot. All fields are None when broker linking is missing or the
    Alpaca call fails — the dashboard renders that as "—"."""
    c = _alpaca_client_for(user_id, side)
    if c is None:
        return {"equity": None, "today_pl": None, "today_pct": None,
                "positions": None, "running": False}
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
            # "running" requires a bot-process registry that lives on the
            # Oracle server. Until step 5 (deploy bots to Oracle with
            # systemd / cron), we report False here; once the bot service
            # is up, we'll flip this to read the systemd unit state.
            "running":   False,
        }
    except Exception:
        return {"equity": None, "today_pl": None, "today_pct": None,
                "positions": None, "running": False}


@app.get("/web/api/status", include_in_schema=False)
def web_api_status(request: Request):
    user = _web_user(request)
    linked = bool(creds.load_credentials(user["id"]))
    bots = {side: _bot_state(user["id"], side)
            for side in ("LONG", "SHORT", "DAY")}
    return {"linked": linked, "bots": bots}


@app.post("/web/api/bots/{side}/start", include_in_schema=False)
def web_api_bot_start(side: str, request: Request):
    user   = _web_user(request)
    result = bot_runner.start_bot(user["id"], side)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "start failed"))
    return result


@app.post("/web/api/bots/{side}/stop", include_in_schema=False)
def web_api_bot_stop(side: str, request: Request):
    user   = _web_user(request)
    result = bot_runner.stop_bot(user["id"], side)
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
MAKEBOT_COST_CREDITS = 10               # V3.1.4 — credits charged per "Free via APEX" generation
SIGNUP_BONUS_CREDITS = 100              # V3.1.4 — every new signup gets a starting balance


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


@app.post("/api/makebot/generate")
def api_makebot_generate(payload: dict,
                         authorization: str | None = Header(default=None)):
    """V3.1.4 — Pooled Anthropic Haiku call gated by APEX credits.
    Costs MAKEBOT_COST_CREDITS per successful generation. Bearer-auth.
    Rate-limited as a secondary safeguard against runaway loops."""
    user = _current_user(authorization)

    # 1. Pre-flight credit check
    balance = credits_mod.get_balance(user["id"])
    if balance < MAKEBOT_COST_CREDITS:
        raise HTTPException(
            402,                                   # Payment Required
            f"Insufficient APEX credits — need {MAKEBOT_COST_CREDITS}, "
            f"you have {balance}. Switch the MAKE BOT provider to "
            f"Anthropic / OpenAI / OpenRouter with your own API key, "
            f"or ask an admin to grant you more credits.")

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
        user["id"], -MAKEBOT_COST_CREDITS,
        f"make-bot generation ({'pool' if used_pool else 'fallback'})",
    )
    return {
        "text":             text,
        "credits_charged":  MAKEBOT_COST_CREDITS,
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
def api_bot_start(side: str,
                  authorization: str | None = Header(default=None)):
    user   = _current_user(authorization)
    result = bot_runner.start_bot(user["id"], side)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "start failed"))
    return result


@app.post("/bots/{side}/stop")
def api_bot_stop(side: str,
                 authorization: str | None = Header(default=None)):
    user   = _current_user(authorization)
    result = bot_runner.stop_bot(user["id"], side)
    if not result.get("ok"):
        raise HTTPException(500, result.get("detail", "stop failed"))
    return result


@app.get("/bots/{side}/status")
def api_bot_status(side: str,
                   authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return bot_runner.status(user["id"], side)


@app.get("/bots/{side}/logs")
def api_bot_logs(side: str, tail: int = 4000,
                 authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return {"side": side.upper(),
            "log":  bot_runner.tail_log(user["id"], side, n_chars=min(tail, 50_000))}


@app.get("/bots/running")
def api_bots_running(authorization: str | None = Header(default=None)):
    user = _current_user(authorization)
    return {"bots": bot_runner.list_running(user["id"])}


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

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
               bots as marketplace, web, bot_runner, scheduler)
from .schemas import SignupRequest, LoginRequest


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    creds.init_credentials_table()
    marketplace.init_marketplace_table()
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
    user   = database.create_user(username, email, hashed, display_name)
    token  = auth.create_token(user["id"], user["email"])
    return {"token": token, "user": _public(user)}


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
    """Land at the dashboard if signed in, otherwise the login form."""
    user = web.user_from_cookie(request)
    return RedirectResponse(url=("/web/dashboard" if user else "/web/login"))


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
def web_dashboard(request: Request):
    user = web.user_from_cookie(request)
    if not user:
        return RedirectResponse(url="/web/login")
    return web.dashboard_page(user)


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

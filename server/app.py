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

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

from . import auth, database
from .schemas import SignupRequest, LoginRequest


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    yield


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

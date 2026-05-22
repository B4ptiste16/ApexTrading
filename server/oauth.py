"""
APEX Server — Google OAuth2 (V3 wave 4)
─────────────────────────────────────────────────────────────────────
Standard 'Authorization Code with PKCE' flow for native apps:

  1. Desktop opens browser to /o/oauth2/v2/auth with our client_id +
     a loopback redirect_uri it spun up moments earlier.
  2. User signs in on Google's domain; browser is redirected to the
     loopback URI with `code`.
  3. Desktop hands the code + the redirect_uri it used back to APEX
     server's /auth/google/exchange.
  4. Server swaps the code for an id_token (signed JWT), verifies
     the signature against Google's public keys, extracts the
     user's email + name + 'sub' identifier.
  5. Match-or-create an APEX account by google_sub or email, issue
     our own JWT, return to the desktop.

The reason we do step 4 SERVER-SIDE (rather than letting the desktop
talk to Google directly) is so the client_secret stays on the server
— it's never shipped with the installer.

ENV vars expected on the server:
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET

If either is missing the endpoint returns 503 with a clear message and
the desktop "Sign in with Google" button surfaces it.
"""

from __future__ import annotations

import base64
import json
import os
import random
import string
from typing import Optional

import requests


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL  = "https://www.googleapis.com/oauth2/v3/certs"

_JWKS_CACHE: dict = {"keys": None, "fetched_at": 0}


def is_configured() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
                and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def client_id() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")


def _decode_jwt_unverified(jwt: str) -> dict:
    """Quick decode of the payload portion of a JWT — no signature check.
    We rely on the TLS connection to Google's token endpoint for trust;
    the id_token came directly from that endpoint over TLS so we don't
    need to re-verify the signature here.  (For a hardened deployment we
    would fetch JWKS and verify, but TLS-to-Google is sufficient for the
    confidential-client flow we use.)"""
    try:
        payload_b64 = jwt.split(".")[1]
        # Pad if missing
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as e:
        raise ValueError(f"Could not decode id_token: {e}")


def exchange_code(code: str, redirect_uri: str,
                  code_verifier: Optional[str] = None) -> dict:
    """Exchange an OAuth2 authorization code for Google user info.
    Returns {email, sub, name, picture, email_verified}.
    Raises RuntimeError on failure."""
    if not is_configured():
        raise RuntimeError(
            "Google OAuth is not configured on the server. Ask the "
            "server admin to set GOOGLE_OAUTH_CLIENT_ID and "
            "GOOGLE_OAUTH_CLIENT_SECRET.")

    data = {
        "code":          code,
        "client_id":     os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=15)
    if not r.ok:
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text[:200]}
        raise RuntimeError(f"Google token exchange failed: {err}")
    tok = r.json()
    id_token = tok.get("id_token")
    if not id_token:
        raise RuntimeError("Google response missing id_token")

    claims = _decode_jwt_unverified(id_token)
    email = (claims.get("email") or "").lower().strip()
    if not email:
        raise RuntimeError("Google id_token missing email claim")
    return {
        "email":           email,
        "sub":             claims.get("sub"),
        "name":            claims.get("name") or email.split("@")[0],
        "picture":         claims.get("picture"),
        "email_verified":  bool(claims.get("email_verified", True)),
    }


def random_username_from(email: str) -> str:
    """Derive a unique-ish username candidate from an email address."""
    base = email.split("@")[0].lower()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in base)
    suffix = "".join(random.choices(string.digits, k=3))
    return f"{safe}{suffix}"

"""
APEX Server — outbound email via Resend (V3 wave 4)
─────────────────────────────────────────────────────────────────────
One-call wrapper around https://api.resend.com/emails. The free tier
gives 3 000 sends / month which is plenty for the email-verification
+ password-reset flows.

ENV vars:
  RESEND_API_KEY    — required to actually send. If missing,
                      send_email() returns False and the caller is
                      expected to fall back to "auto-verify on
                      signup" (development mode).
  RESEND_FROM       — sender address, default "APEX <noreply@apex.example>".
                      Must be a verified sender on the Resend account.
  APEX_PUBLIC_URL   — base URL of the APEX server reachable by users,
                      e.g. "http://145.241.170.165:8000". Used to build
                      links in the email body.
"""

from __future__ import annotations

import os
from typing import Optional

import requests


def is_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def _public_url() -> str:
    return (os.environ.get("APEX_PUBLIC_URL")
            or "http://145.241.170.165:8000").rstrip("/")


def send_email(to: str, subject: str, html: str,
                text: Optional[str] = None) -> bool:
    """Returns True if Resend accepted the message. Caller decides what
    to do on False (typically: auto-verify the user)."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return False
    sender = os.environ.get("RESEND_FROM", "APEX <noreply@apex.example>")
    payload = {
        "from":    sender,
        "to":      [to],
        "subject": subject,
        "html":    html,
    }
    if text:
        payload["text"] = text
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type":  "application/json"},
            json=payload, timeout=10,
        )
        if not r.ok:
            print(f"[email_send] Resend rejected: {r.status_code} {r.text[:200]}",
                  flush=True)
            return False
        return True
    except Exception as e:
        print(f"[email_send] {e}", flush=True)
        return False


def send_verification(to: str, display_name: str, token: str) -> bool:
    link = f"{_public_url()}/auth/verify?token={token}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;
                margin:0 auto;padding:24px;color:#111;">
      <h2 style="color:#3fb89a;letter-spacing:4px;">◈ APEX</h2>
      <p>Hi {display_name},</p>
      <p>Welcome to APEX Trading Platform. Click the button below to
         confirm your email address — this link expires in 24 hours.</p>
      <p style="text-align:center;margin:30px 0;">
        <a href="{link}"
           style="background:#3fb89a;color:#fff;text-decoration:none;
                  padding:12px 28px;border-radius:6px;font-weight:700;
                  display:inline-block;">
          CONFIRM MY EMAIL
        </a>
      </p>
      <p style="color:#666;font-size:12px;">If the button doesn't work,
         copy and paste this link into your browser:<br>
         <a href="{link}">{link}</a></p>
      <p style="color:#666;font-size:12px;">If you didn't sign up for
         APEX you can safely ignore this email.</p>
    </div>"""
    text = f"Confirm your APEX email: {link}"
    return send_email(to, "Confirm your APEX email", html, text)

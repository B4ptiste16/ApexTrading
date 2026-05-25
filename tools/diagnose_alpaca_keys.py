"""
APEX · Alpaca key diagnostic
─────────────────────────────────────────────────────────────────
Reads ALPACA_API_KEY_<SIDE> / ALPACA_SECRET_KEY_<SIDE> pairs from
your .env in %LocalAppData%\\APEX Trading Platform and tests each
one against Alpaca's paper-trading account endpoint.

Run from anywhere:
    python tools/diagnose_alpaca_keys.py

For each slot you'll see exactly what Alpaca returned:
    ✓ DAY     account OK     id=abc...  status=ACTIVE  cash=$100,000
    ✗ CRYPTO  HTTP 401       Alpaca rejects this key/secret pair
                              (mismatched pair, revoked, or live-vs-paper)
    -        empty           slot not configured

No keys are uploaded anywhere — this only calls
    GET https://paper-api.alpaca.markets/v2/account
with the local .env values, exactly like the bot does at startup.
"""
import os
import sys
from pathlib import Path

# Force UTF-8 stdout so Unicode arrows / bullets do not crash under cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)


def find_env() -> Path:
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "APEX Trading Platform" / ".env")
    candidates.append(Path.home() / ".apex_trading_data" / ".env")
    candidates.append(Path.cwd() / ".env")
    for c in candidates:
        if c.exists():
            return c
    print("ERROR: no .env found. Looked at:")
    for c in candidates:
        print(f"  {c}")
    sys.exit(2)


def parse_env(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def test_pair(side: str, key: str, secret: str) -> tuple:
    """Return (status, detail). status: 'ok' | 'bad' | 'empty' | 'error'."""
    if not key and not secret:
        return ("empty", "slot not configured")
    if not key or not secret:
        return ("bad", f"key={'set' if key else 'EMPTY'} "
                        f"secret={'set' if secret else 'EMPTY'} — "
                        f"both are required")
    if key != key.strip() or secret != secret.strip():
        return ("bad", "key or secret has leading/trailing whitespace")
    try:
        r = requests.get(
            "https://paper-api.alpaca.markets/v2/account",
            headers={
                "APCA-API-KEY-ID":     key,
                "APCA-API-SECRET-KEY": secret,
            },
            timeout=10,
        )
    except Exception as e:
        return ("error", f"network error: {e}")
    if r.status_code == 200:
        try:
            j = r.json()
            return ("ok",
                    f"id={j.get('id', '?')[:8]}…  "
                    f"status={j.get('status', '?')}  "
                    f"cash=${j.get('cash', '?')}")
        except Exception:
            return ("ok", "200 OK but JSON parse failed")
    if r.status_code == 401:
        return ("bad", "HTTP 401 — Alpaca rejects this key/secret pair "
                       "(mismatched, revoked, or live-not-paper)")
    if r.status_code == 403:
        return ("bad", "HTTP 403 — forbidden (account may be suspended)")
    return ("bad", f"HTTP {r.status_code} — {r.text[:120]}")


def fetch_server_creds() -> dict | None:
    """V4.6.3 — pull whatever the APEX server thinks our credentials
    are. This catches sync silently writing to the wrong user_id or
    the server returning stale data. Returns the decoded blob, or
    None if anything fails. Prints the failure reason inline."""
    apex_dir = find_env().parent
    auth_path = apex_dir / "apex_auth.json"
    srv_path  = apex_dir / "apex_server.json"
    try:
        import json as _j
        auth = _j.loads(auth_path.read_text(encoding="utf-8"))
        token = auth.get("token")
    except Exception as e:
        print(f"  [server] cannot read {auth_path.name}: {e}")
        return None
    if not token:
        print(f"  [server] no auth token stored — sign in first")
        return None
    server_url = "https://apex-api.openblock.club"
    try:
        srv = _j.loads(srv_path.read_text(encoding="utf-8"))
        server_url = srv.get("url", server_url).rstrip("/")
    except Exception:
        pass
    try:
        r = requests.get(
            f"{server_url}/credentials",
            headers={"Authorization": f"Bearer {token}"},
            timeout=12,
        )
    except Exception as e:
        print(f"  [server] network error: {e}")
        return None
    if r.status_code == 401:
        print(f"  [server] HTTP 401 — auth token expired, sign in again")
        return None
    if r.status_code != 200:
        print(f"  [server] HTTP {r.status_code} — {r.text[:200]}")
        return None
    try:
        return r.json() or {}
    except Exception as e:
        print(f"  [server] JSON parse failed: {e}")
        return None


def force_sync_to_server(env: dict) -> tuple[bool, str]:
    """V4.6.3 — push the current local .env to the APEX server using
    the stored auth token. Bypasses the GUI so a stuck Sync button
    or expired-session error can be debugged from the CLI."""
    apex_dir = find_env().parent
    auth_path = apex_dir / "apex_auth.json"
    srv_path  = apex_dir / "apex_server.json"
    try:
        import json as _j
        auth = _j.loads(auth_path.read_text(encoding="utf-8"))
        token = auth.get("token")
    except Exception as e:
        return (False, f"cannot read {auth_path.name}: {e}")
    if not token:
        return (False, "no auth token — sign in via APEX first")
    server_url = "https://apex-api.openblock.club"
    try:
        srv = _j.loads(srv_path.read_text(encoding="utf-8"))
        server_url = srv.get("url", server_url).rstrip("/")
    except Exception:
        pass
    payload = {k: v for k, v in env.items() if v}
    try:
        r = requests.put(
            f"{server_url}/credentials",
            headers={"Authorization": f"Bearer {token}"},
            json=payload, timeout=15,
        )
    except Exception as e:
        return (False, f"network error: {e}")
    if r.status_code == 401:
        return (False, "HTTP 401 — auth token expired, sign in again "
                       "via APEX (close + reopen, then log in)")
    if not r.ok:
        return (False, f"HTTP {r.status_code} — {r.text[:200]}")
    try:
        n = len(r.json().get("fields", []))
    except Exception:
        n = "?"
    return (True, f"server now has {n} keys for this user "
                  f"(was {len(payload)} sent)")


def diff_local_vs_server(env: dict, server_blob: dict) -> None:
    """Print a per-side comparison: does the server have the SAME key
    as the local .env, a DIFFERENT key, or no key at all? This is the
    smoking gun for cloud bot 401s — if the prefixes don't match, the
    sync silently failed and we need to push again."""
    print()
    print("─" * 78)
    print(" LOCAL .env  vs  APEX server credentials")
    print("─" * 78)
    sides = sorted(
        {k[len("ALPACA_API_KEY_"):] for k in env if k.startswith("ALPACA_API_KEY_")} |
        {k[len("ALPACA_API_KEY_"):] for k in server_blob if k.startswith("ALPACA_API_KEY_")}
    )
    width = max((len(s) for s in sides), default=6)
    print(f"  {'SLOT':<{width}}  LOCAL                   SERVER                  STATE")
    for side in sides:
        l_key = env.get(f"ALPACA_API_KEY_{side}", "")
        s_key = server_blob.get(f"ALPACA_API_KEY_{side}", "")
        l_sec = env.get(f"ALPACA_SECRET_KEY_{side}", "")
        s_sec = server_blob.get(f"ALPACA_SECRET_KEY_{side}", "")
        l_prefix = (l_key[:8] + "…") if l_key else "—"
        s_prefix = (s_key[:8] + "…") if s_key else "—"
        if not l_key and not s_key:
            state = "both empty"
        elif l_key and not s_key:
            state = "MISSING ON SERVER — sync hasn't run"
        elif s_key and not l_key:
            state = "ONLY ON SERVER — local was wiped"
        elif l_key == s_key and l_sec == s_sec:
            state = "OK match"
        elif l_key == s_key:
            state = "key match, SECRET differs"
        else:
            state = "KEY DIFFERS — server has stale value, RE-SYNC"
        print(f"  {side:<{width}}  {l_prefix:<22}  {s_prefix:<22}  {state}")


def main():
    env_path = find_env()
    print(f"reading: {env_path}\n")
    env = parse_env(env_path)

    # Discover every ALPACA_API_KEY_* in the file (covers custom bot slugs)
    sides = sorted(
        {k[len("ALPACA_API_KEY_"):] for k in env if k.startswith("ALPACA_API_KEY_")} |
        {"LONG", "SHORT", "DAY"}  # always show built-ins even if empty
    )

    width = max((len(s) for s in sides), default=6)
    glyph = {"ok": "[OK] ", "bad": "[BAD]", "empty": "[--] ", "error": "[ERR]"}
    print(f"{'STATUS':<6} {'SLOT':<{width}}  RESULT")
    print("-" * (8 + width + 60))
    bad_any = False
    for side in sides:
        key    = env.get(f"ALPACA_API_KEY_{side}", "")
        secret = env.get(f"ALPACA_SECRET_KEY_{side}", "")
        status, detail = test_pair(side, key, secret)
        print(f"{glyph[status]:<6} {side:<{width}}  {detail}")
        if status == "bad":
            bad_any = True
            # Show the first 8 chars of the key so the user can confirm
            # they're looking at the right one in Alpaca's dashboard.
            if key:
                print(f"       {'':<{width}}  → key prefix: {key[:8]}…  "
                      f"secret prefix: {secret[:8]}…  "
                      f"key len={len(key)}  secret len={len(secret)}")

    print()
    if bad_any:
        print("Next steps for any [BAD] slot:")
        print("  1. Log into Alpaca: https://app.alpaca.markets/paper/dashboard/overview")
        print("  2. Confirm you're on the SAME paper account whose key you pasted")
        print("  3. Regenerate the API key (the OLD one becomes dead the moment")
        print("     you generate a new one — never copy the old value)")
        print("  4. Copy BOTH the key AND the matching secret in a single shot")
        print("  5. Paste into the slot in APEX Tools → Save slots")
        print("  6. Re-run this script to confirm green")
    else:
        print("All configured slots authenticate cleanly against Alpaca paper.")
        print("Checking what the APEX server has stored for this user…")

    # V4.6.3 — server-side check: pull what the APEX server has stored
    # and diff against the local .env. This is the smoking gun for any
    # cloud bot 401 — if the server's key prefix differs from local,
    # the sync wrote stale data and needs a fresh push.
    print()
    print("─" * 78)
    print(" APEX SERVER STATE")
    print("─" * 78)
    server_blob = fetch_server_creds()
    if server_blob is None:
        print("(could not reach server — skipping server diff)")
    else:
        print(f"  server has {len(server_blob)} keys stored for this user")
        diff_local_vs_server(env, server_blob)
        # Final actionable verdict
        sync_needed = []
        for side in sides:
            l_key = env.get(f"ALPACA_API_KEY_{side}", "")
            s_key = server_blob.get(f"ALPACA_API_KEY_{side}", "")
            if l_key and l_key != s_key:
                sync_needed.append(side)
        if sync_needed:
            print()
            print(f"  Server keys are stale for: {', '.join(sync_needed)}")
            print(f"  Pushing fresh local .env to server now…")
            ok, detail = force_sync_to_server(env)
            if ok:
                print(f"  ✓ {detail}")
                # Re-fetch and re-diff
                server2 = fetch_server_creds()
                if server2 is not None:
                    print()
                    print("Re-checking after push:")
                    diff_local_vs_server(env, server2)
            else:
                print(f"  ✗ Push failed: {detail}")
                print(f"  Open APEX → Tools → ACCOUNT LINKING → "
                      f"'Sync keys to APEX server' and watch for errors.")


if __name__ == "__main__":
    main()

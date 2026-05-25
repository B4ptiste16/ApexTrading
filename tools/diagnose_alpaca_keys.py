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
        print("If a cloud bot still fails, the encrypted blob on the APEX")
        print("server may be stale — open Tools → ACCOUNT LINKING and click")
        print("'Sync keys to APEX server' to force a fresh upload.")


if __name__ == "__main__":
    main()

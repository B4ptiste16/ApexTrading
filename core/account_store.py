"""
V4.6.101 — Per-account data store: one-time migration, server-backed config,
and a local-cache cleaner.

Layout (desktop):
  DATA_DIR/                      ← shared login root (apex_auth/accounts/server)
  DATA_DIR/accounts/<user_id>/   ← ACCOUNT_DIR: this account's settings, .env,
                                    bots, ledgers, per-bot state, universes.

The server is the source of truth: keys live in the encrypted /credentials blob
and the rest of the account's desktop config (bot registry + per-bot settings +
prefs = apex_settings.json) lives in /desktop-config. On login we pull both into
ACCOUNT_DIR; on change we push apex_settings.json back. So a switched account (or
a fresh machine) reconstructs itself from the server, and the local copy can be
cleared to reclaim disk.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.paths import (DATA_DIR, ACCOUNT_DIR, active_account_id,
                        ensure_account_dir)

_MIGRATION_FLAG = DATA_DIR / "apex_account_migration_v101.json"

# Account-scoped items that lived directly in DATA_DIR before v4.6.101.
_ACCOUNT_ITEMS = [
    "apex_settings.json", ".env",
    "bots", "ibkr", "tradingview", "cache", "universe_scripts",
]
_ACCOUNT_GLOBS = [
    "*_lifetime.jsonl", "*_snapshots.jsonl", "*_state.json",
    "*_trade_log.jsonl", "*_analysis.txt", "*_watchlist.txt",
    "*_universe.txt", "*_charts", "daybot_*", "longv2_*", "shortv2_*",
]

# Disposable cache (re-fetched from the server's cloud ledgers / re-derived).
_CACHE_GLOBS = [
    "*_lifetime.jsonl", "*_snapshots.jsonl", "*_charts",
    "*_analysis.txt", "*_trade_log.jsonl", "cache",
]


def migrate_legacy_if_needed() -> None:
    """One-time: move pre-v4.6.101 shared data from the DATA_DIR root into the
    signed-in (primary) account's dir. Idempotent via a flag at the root.
    No-op on the server (ACCOUNT_DIR == DATA_DIR) or before login."""
    uid = active_account_id()
    if uid is None or ACCOUNT_DIR == DATA_DIR:
        return
    if _MIGRATION_FLAG.exists():
        return
    ensure_account_dir()
    moved: list[str] = []

    def _move(src: Path) -> None:
        if not src.exists():
            return
        dst = ACCOUNT_DIR / src.name
        if dst.exists():        # account dir already has it — don't clobber
            return
        try:
            shutil.move(str(src), str(dst))
            moved.append(src.name)
        except Exception as e:
            print(f"[migrate] {src.name}: {e}", flush=True)

    for name in _ACCOUNT_ITEMS:
        _move(DATA_DIR / name)
    for pat in _ACCOUNT_GLOBS:
        for src in list(DATA_DIR.glob(pat)):
            _move(src)

    try:
        _MIGRATION_FLAG.write_text(
            json.dumps({"uid": uid, "moved": moved}), encoding="utf-8")
    except Exception:
        pass
    print(f"[migrate] assigned {len(moved)} legacy item(s) to account {uid}",
          flush=True)


def repair_registry_paths() -> None:
    """V4.6.101 — the bot registry stores ABSOLUTE script paths; after the
    per-account migration moved bots/ into accounts/<id>/, old entries pointed
    at files that no longer exist (which crashed the bot-tab build:
    'NoneType' has no attribute 'name'). Rewrite any stale script path whose
    basename exists under this account's bots dir. Idempotent; runs every
    launch (cheap), so it also heals registries restored from the server."""
    if ACCOUNT_DIR == DATA_DIR:
        return
    sf = ACCOUNT_DIR / "apex_settings.json"
    if not sf.exists():
        return
    try:
        s = json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return
    bots = ACCOUNT_DIR / "bots"
    changed = False
    for key, reg in list(s.items()):
        if not (key.startswith("bot_registry") and isinstance(reg, dict)):
            continue
        for c in reg.get("custom", []) or []:
            try:
                p = Path(str(c.get("script", "")))
                if not str(p) or p.exists():
                    continue
                for ext in (p.suffix or ".py", ".py", ".apex"):
                    cand = bots / (p.stem + ext)
                    if cand.exists():
                        c["script"] = str(cand)
                        changed = True
                        break
            except Exception:
                continue
    if changed:
        try:
            sf.write_text(json.dumps(s, indent=2), encoding="utf-8")
            print("[migrate] repaired stale bot-script paths", flush=True)
        except Exception:
            pass


# ── server-backed config ─────────────────────────────────────────────────
def _log(msg: str) -> None:
    """V4.6.101 — config-sync diagnostics. The frozen GUI app has no console,
    so failures in the sync threads were invisible; append them to a small log
    at the shared root (capped) so they're debuggable from disk."""
    try:
        f = DATA_DIR / "apex_config_sync.log"
        prev = f.read_text(encoding="utf-8") if f.exists() else ""
        if len(prev) > 20_000:
            prev = prev[-10_000:]
        import datetime as _dt
        f.write_text(prev + f"{_dt.datetime.now().isoformat(timespec='seconds')} "
                     f"{msg}\n", encoding="utf-8")
    except Exception:
        pass
    try:
        print(f"[config-sync] {msg}", flush=True)
    except Exception:
        pass


def _auth():
    try:
        from ui.login import load_auth, load_server_url
        a = load_auth() or {}
        return a.get("token"), load_server_url()
    except Exception as e:
        _log(f"auth resolve failed: {e!r}")
        return None, None


def fetch_config_from_server() -> None:
    """On login: pull this account's keys (/credentials → .env) and desktop
    config (/desktop-config → apex_settings.json) into ACCOUNT_DIR, so a freshly
    switched account (or a clean machine) reconstructs itself from the server."""
    tok, url = _auth()
    if not tok or not url:
        return
    import requests
    ensure_account_dir()
    hdr = {"Authorization": f"Bearer {tok}"}
    # 1) desktop config → apex_settings.json (only if the server has one)
    try:
        r = requests.get(f"{url}/desktop-config", headers=hdr, timeout=12)
        if r.ok:
            cfg = (r.json() or {}).get("config")
            if isinstance(cfg, dict) and cfg:
                (ACCOUNT_DIR / "apex_settings.json").write_text(
                    json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[config] fetch desktop-config failed: {e}", flush=True)
    # 2) credentials → .env (reuse the data layer's writer, now account-scoped)
    try:
        r = requests.get(f"{url}/credentials", headers=hdr, timeout=12)
        if r.ok:
            blob = (r.json() or {}).get("credentials") or {}
            if isinstance(blob, dict) and blob:
                import core.data as D
                D.write_env_keys({k: v for k, v in blob.items()
                                  if isinstance(v, str) and v})
    except Exception as e:
        print(f"[config] fetch credentials failed: {e}", flush=True)


_SECRET_HINT = ("password", "secret", "api_key", "apikey", "token")


def _strip_secrets(obj):
    """Recursively drop keys that look like credentials (e.g. IBKR
    cloud_password, any *_secret / api_key) so the desktop-config blob never
    carries plaintext secrets — those live in the encrypted /credentials blob."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(h in kl for h in _SECRET_HINT):
                continue
            out[k] = _strip_secrets(v)
        return out
    if isinstance(obj, list):
        return [_strip_secrets(x) for x in obj]
    return obj


def push_config_to_server() -> None:
    """Push the account's apex_settings.json (secrets stripped) to
    /desktop-config (call debounced after a settings/registry change). Keys go
    to /credentials separately."""
    tok, url = _auth()
    if not tok or not url:
        return
    import requests
    try:
        import core.data as D
        cfg = _strip_secrets(D.load_settings())
    except Exception:
        return
    try:
        requests.put(f"{url}/desktop-config",
                     headers={"Authorization": f"Bearer {tok}"},
                     json={"config": cfg}, timeout=12)
    except Exception as e:
        print(f"[config] push desktop-config failed: {e}", flush=True)


def push_keys_to_server() -> bool:
    """V4.6.130 — push the local .env keys (incl. per-bot AI config
    AI_PROVIDER_<SIDE> / AI_MODEL_<SIDE> / AI_MODE_<SIDE>) to the encrypted
    /credentials blob, MERGING with what's already there so nothing is wiped.
    Called after a bot's AI model is changed so the cloud runner uses it on the
    bot's next start. Returns True on success."""
    tok, url = _auth()
    if not tok or not url:
        return False
    import requests
    try:
        import core.data as D
        keys = {k: v for k, v in (D.read_env_keys() or {}).items()
                if isinstance(v, str) and v}
        try:
            keys["APEX_ALPACA_MODE"] = D.load_settings().get("alpaca_mode", "paper")
        except Exception:
            pass
    except Exception:
        return False
    if not keys:
        return False
    hdr = {"Authorization": f"Bearer {tok}"}
    try:
        # GET-merge-PUT: /credentials replaces the whole blob, so preserve the
        # existing fields (IBKR login, other keys) and overlay ours.
        blob = {}
        g = requests.get(f"{url}/credentials", headers=hdr, timeout=15)
        if g.ok and isinstance(g.json(), dict):
            blob = g.json()
        blob.update(keys)
        r = requests.put(f"{url}/credentials", json=blob, headers=hdr, timeout=20)
        return bool(r.ok)
    except Exception as e:
        _log(f"push_keys_to_server failed: {e!r}")
        return False


def clear_local_cache() -> int:
    """Delete this account's disposable local cache (history/charts/logs). Keeps
    .env + apex_settings.json (re-fetchable but cheap, and avoids a re-login).
    Returns the number of items removed."""
    if ACCOUNT_DIR == DATA_DIR:
        return 0
    n = 0
    for pat in _CACHE_GLOBS:
        for p in list(ACCOUNT_DIR.glob(pat)):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                n += 1
            except Exception:
                pass
    return n

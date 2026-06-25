"""
APEX server-side bot auto-start scheduler  (v4.6.27)
─────────────────────────────────────────────────────────────────
Auto-starts cloud bots at the US market open WITHOUT requiring the
user's desktop APEX to be running. Replaces the v7.1.10 client-side
QTimer scheduler for the cloud-bot case.

Reads each user's `APEX_AUTO_SCHEDULE_<SIDE>` flag from their
encrypted credentials blob (synced from desktop). On the market-open
EDGE (closed → open), starts every bot whose flag is "1" for that
user — provided the bot was uploaded to /opt/apex_users/user_X/private_bots/
(custom) or is a built-in (LONG/SHORT/DAY).

Runs as a daemon thread inside the uvicorn process. Polls Alpaca's
clock every 60 seconds. Idempotent — if start_bot reports
'already_running', the scheduler silently moves on.
"""

from __future__ import annotations

import os
import threading
import time
import sqlite3
from typing import Optional


# ── Track previous market state to detect the closed → open edge ──

_PREV_OPEN: Optional[bool] = None
_PREV_OPEN_LOCK = threading.Lock()


def _market_is_open() -> Optional[bool]:
    """Returns True/False from Alpaca's market clock, or None if it
    can't be reached. Uses the server admin's pooled Alpaca key if set,
    else falls back to public market hours (US session 09:30–16:00 ET,
    Mon–Fri)."""
    server_key    = os.environ.get("APEX_ALPACA_PUBLIC_KEY")
    server_secret = os.environ.get("APEX_ALPACA_PUBLIC_SECRET")
    if server_key and server_secret:
        try:
            from alpaca.trading.client import TradingClient
            tc = TradingClient(server_key, server_secret, paper=True)
            return bool(tc.get_clock().is_open)
        except Exception as e:
            print(f"[auto-scheduler] alpaca clock failed: {e}", flush=True)
            # Fall through to time-based fallback
    # Time-based fallback: NYSE hours, ignoring half-days / holidays.
    # Good enough to avoid spurious starts before 09:30 ET or after
    # 16:00 ET. Half-days will trigger an extra start that the bot
    # itself can handle.
    from datetime import datetime, timezone, timedelta
    u = datetime.now(timezone.utc)
    # EDT (Mar–Nov) = UTC-4; EST otherwise = UTC-5
    off = -4 if 3 <= u.month <= 11 else -5
    et = u + timedelta(hours=off)
    if et.weekday() >= 5:  # Sat/Sun
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _enumerate_users_with_auto_start() -> list[tuple[int, list[str]]]:
    """For every user, return (user_id, [sides_with_auto_start_set]).
    Reads the credentials blob — the desktop syncs auto-start choices
    there alongside Alpaca keys via the v4.6.27 client patch."""
    try:
        from . import credentials as creds
    except ImportError:
        import credentials as creds  # type: ignore
    # Walk every row in user_credentials. Direct SQL is fine since
    # credentials.py doesn't have a list-all helper.
    out = []
    try:
        with sqlite3.connect(_db_path()) as c:
            rows = c.execute(
                "SELECT user_id FROM user_credentials"
            ).fetchall()
        for (uid,) in rows:
            blob = creds.load_credentials(uid) or {}
            sides = []
            for side in ("LONG", "SHORT", "DAY"):
                if str(blob.get(f"APEX_AUTO_SCHEDULE_{side}", "")) == "1":
                    sides.append(side)
            # Discover custom-bot slugs too
            try:
                from . import bot_runner as br
                priv = br.private_bots_dir(uid)
                if priv.exists():
                    for p in priv.glob("*.py"):
                        slug = p.stem.upper()
                        if str(blob.get(
                                f"APEX_AUTO_SCHEDULE_{slug}", "")) == "1":
                            sides.append(slug)
            except Exception:
                pass
            if sides:
                out.append((uid, sides))
    except Exception as e:
        print(f"[auto-scheduler] enumerate users failed: {e}", flush=True)
    return out


def _db_path() -> str:
    """Locate the same SQLite database the rest of the server uses."""
    try:
        from . import database
    except ImportError:
        import database  # type: ignore
    return database.DB_PATH


def _tick() -> None:
    """Single scheduler iteration. Detect open edge, start scheduled
    bots. Idempotent — re-invoking when market state hasn't changed
    is a no-op."""
    global _PREV_OPEN
    is_open = _market_is_open()
    if is_open is None:
        return
    with _PREV_OPEN_LOCK:
        prev = _PREV_OPEN
        _PREV_OPEN = is_open
    # Only act on the closed → open edge (avoid restarting every minute)
    if not (is_open and prev in (None, False)):
        return
    print(f"[auto-scheduler] market OPEN edge detected — starting "
          f"scheduled bots", flush=True)
    try:
        from . import bot_runner as br
    except ImportError:
        import bot_runner as br  # type: ignore
    started_total = 0
    for uid, sides in _enumerate_users_with_auto_start():
        for side in sides:
            try:
                result = br.start_bot(uid, side)
                if result.get("ok"):
                    if result.get("already_running"):
                        print(f"[auto-scheduler] user={uid} {side} "
                              f"already running pid={result.get('pid')}",
                              flush=True)
                    else:
                        started_total += 1
                        print(f"[auto-scheduler] user={uid} {side} "
                              f"started pid={result.get('pid')}",
                              flush=True)
                else:
                    print(f"[auto-scheduler] user={uid} {side} start "
                          f"failed: {result.get('detail')}", flush=True)
            except Exception as e:
                print(f"[auto-scheduler] user={uid} {side} exception: "
                      f"{e}", flush=True)
    print(f"[auto-scheduler] open-edge cycle complete — "
          f"{started_total} bot(s) started", flush=True)


def _watchdog_tick() -> None:
    """V4.6.63 — keep every DESIRED bot running 24/7. For each (user, side,
    broker) the user has started on the cloud, ensure it's alive; restart it if
    not. This is what makes cloud bots survive crashes, the desktop being
    closed, and server restarts — instead of only starting at the market-open
    edge. start_bot is idempotent (already_running is a no-op).

    V4.6.65 — single-flight across uvicorn workers: only the worker that grabs
    the lock runs the sweep, so we don't do the same work 3× or spam logs."""
    import fcntl as _fcntl, tempfile as _tf, os as _os
    _wd = None
    try:
        _wd = open(_os.path.join(_tf.gettempdir(), "apex_watchdog.lock"), "w")
        try:
            _fcntl.flock(_wd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            _wd.close()
            return  # another worker is running this sweep
    except Exception:
        _wd = None
    try:
        _watchdog_sweep()
    finally:
        if _wd is not None:
            try:
                _fcntl.flock(_wd, _fcntl.LOCK_UN)
            except Exception:
                pass
            _wd.close()


_LAST_UNIVERSE_WEEK = None


def _maybe_generate_universes() -> None:
    """V4.6.73 — regenerate the public themed universes once per week, on
    Monday at/after the US market open. Runs inside the single-flight watchdog
    sweep so only one worker does it."""
    global _LAST_UNIVERSE_WEEK
    from datetime import datetime, timezone, timedelta
    u = datetime.now(timezone.utc)
    off = -4 if 3 <= u.month <= 11 else -5      # EDT/EST
    et = u + timedelta(hours=off)
    if et.weekday() != 0:                        # Monday only
        return
    if (et.hour * 60 + et.minute) < (9 * 60 + 30):
        return
    wk = et.isocalendar()[1]
    if _LAST_UNIVERSE_WEEK == wk:
        return
    _LAST_UNIVERSE_WEEK = wk
    try:
        from . import universe_factory as _uf
    except ImportError:
        import universe_factory as _uf  # type: ignore
    try:
        print("[universe-factory] weekly regeneration starting…", flush=True)
        counts = _uf.generate_all()
        print(f"[universe-factory] done: {counts}", flush=True)
    except Exception as e:
        print(f"[universe-factory] generation failed: {e}", flush=True)


# V4.6.111 — back off bots whose start keeps FAILING for a deterministic config
# reason (e.g. no Alpaca key synced for that slot). Retrying every 45s forever
# spams the log and buries real problems; a 5-minute backoff still recovers
# quickly once the user syncs the missing credential. There is NO cap on how
# many bots run — every desired bot is started; this only paces doomed retries.
_START_BACKOFF: dict = {}          # (uid, side, broker) -> retry-after epoch
_START_BACKOFF_SECS = 300


_LAST_RECON: dict = {}        # (uid, mode) -> epoch of last reconcile
_RECON_INTERVAL = 3600        # hourly


def _maybe_reconcile_ibkr(br, is_open) -> None:
    """V4.6.134 — hourly (market hours only), keep the live IBKR account and the
    bots' sub-portfolio ledgers in sync IN BOTH DIRECTIONS so the account never
    silently diverges from what the app shows:
      (1) correct the LEDGERS down to IBKR  (phantom over-counts — ledger claims
          more than IBKR holds; writes ledger files, no orders), then
      (2) flatten IBKR ORPHANS  (IBKR holds more than any bot tracks; market
          orders the untracked excess).
    Self-healing: a no-op when there's no drift; both halves refuse to act on an
    empty/zero-position read (whole-account safety guards)."""
    if is_open is not True:
        return
    try:
        seen = set()
        for uid, side, broker in br.list_all_desired():
            base, mode = br._split_broker(broker)
            if base != "ibkr":
                continue
            key = (uid, mode)
            if key in seen:
                continue
            seen.add(key)
            if time.time() - _LAST_RECON.get(key, 0) < _RECON_INTERVAL:
                continue
            _LAST_RECON[key] = time.time()
            # (1) ledgers DOWN to IBKR first, so (2) then sees corrected ledgers.
            try:
                lr = br.reconcile_ledger_to_broker(uid, mode, execute=True)
                fixed = lr.get("plan", []) or [] if lr.get("executed") else []
                if fixed:
                    print(f"[reconcile] user={uid} {mode}: corrected "
                          f"{len(fixed)} ledger over-count(s) down to IBKR: "
                          f"{[(p['bot'], p['symbol'], p['from'], p['to']) for p in fixed]}",
                          flush=True)
                elif not lr.get("ok"):
                    print(f"[reconcile] user={uid} {mode} ledger-correct: "
                          f"{lr.get('detail')}", flush=True)
            except Exception as e:
                print(f"[reconcile] user={uid} {mode} ledger-correct failed: "
                      f"{e}", flush=True)
            # (2) flatten IBKR orphans (untracked excess).
            try:
                res = br.reconcile_ibkr_orphans(uid, mode, execute=True)
                n = len(res.get("sold", []) or [])
                if n:
                    print(f"[reconcile] user={uid} {mode}: flattened {n} "
                          f"orphan position(s): {res.get('sold')}", flush=True)
                elif not res.get("ok"):
                    print(f"[reconcile] user={uid} {mode}: "
                          f"{res.get('detail')}", flush=True)
            except Exception as e:
                print(f"[reconcile] user={uid} {mode} failed: {e}", flush=True)
    except Exception as e:
        print(f"[reconcile] sweep error: {e}", flush=True)


def _watchdog_sweep() -> None:
    try:
        _maybe_generate_universes()
    except Exception as e:
        print(f"[universe-factory] tick error: {e}", flush=True)
    try:
        from . import bot_runner as br
    except ImportError:
        import bot_runner as br  # type: ignore

    # V4.6.81 — ASSET-AWARE LIFECYCLE. Each desired bot follows its market:
    #   • crypto bots → run continuously (the crypto market is 24/7)
    #   • equity bots → run only during US market hours; the watchdog STOPS
    #                   them at the close and RESTARTS them at the next open.
    # Stops use user_initiated=False so the bot STAYS in the desired registry
    # and comes back automatically next session. Market state is fetched once
    # per sweep; if it's unknown (clock unreachable) we never stop a bot.
    is_open = _market_is_open()
    for uid, side, broker in br.list_all_desired():
        try:
            try:
                crypto = br.bot_asset_type(uid, side) == "crypto"
            except Exception:
                crypto = False
            # V4.6.112 — a crypto bot must never live on IBKR (no reliable Paxos
            # market data → it can't price/fill). Retire any that slipped in:
            # stop it and drop it from the registry so it stays gone. The user
            # re-creates it on Alpaca.
            if crypto and broker == "ibkr":
                if br.is_running(uid, side, broker):
                    br.stop_bot(uid, side, broker, user_initiated=True)
                    print(f"[watchdog] retired crypto bot {side} from IBKR "
                          f"(unsupported) — move it to Alpaca", flush=True)
                else:
                    try:
                        br.remove_desired(uid, side, broker)
                    except Exception:
                        pass
                continue
            should_run = crypto or (is_open is True)
            running = br.is_running(uid, side, broker)
            if should_run and not running:
                key = (uid, side, broker)
                if time.time() < _START_BACKOFF.get(key, 0):
                    continue                      # doomed start — backing off
                res = br.start_bot(uid, side, broker)
                if res.get("ok") and not res.get("already_running"):
                    _START_BACKOFF.pop(key, None)
                    print(f"[watchdog] started user={uid} {side}/{broker} "
                          f"pid={res.get('pid')}", flush=True)
                elif res.get("ok"):
                    _START_BACKOFF.pop(key, None)
                elif not res.get("ok"):
                    _START_BACKOFF[key] = time.time() + _START_BACKOFF_SECS
                    print(f"[watchdog] user={uid} {side}/{broker} start "
                          f"failed: {res.get('detail')} — retrying in "
                          f"{_START_BACKOFF_SECS}s", flush=True)
            elif (not should_run) and running and (is_open is False):
                # Equity bot + market confirmed CLOSED → stop until next open.
                br.stop_bot(uid, side, broker, user_initiated=False)
                print(f"[watchdog] stopped user={uid} {side}/{broker} at "
                      f"market close (will restart at open)", flush=True)
        except Exception as e:
            print(f"[watchdog] user={uid} {side}/{broker} exception: {e}",
                  flush=True)

    # V4.6.133 — keep IBKR accounts in sync with the bots' ledgers (hourly).
    try:
        _maybe_reconcile_ibkr(br, is_open)
    except Exception as e:
        print(f"[reconcile] tick error: {e}", flush=True)


def start_cron():
    """Spawn the scheduler thread. Idempotent — calling twice is a
    no-op because we check the thread name."""
    name = "auto_scheduler"
    for t in threading.enumerate():
        if t.name == name:
            return
    def _loop():
        # Wait ~10s after import so the FastAPI app is fully up before
        # we start polling.
        time.sleep(10)
        while True:
            try:
                _tick()            # seed APEX_AUTO_SCHEDULE bots at market open
            except Exception as e:
                print(f"[auto-scheduler] loop error: {e}", flush=True)
            try:
                _watchdog_tick()   # keep all desired bots alive 24/7
            except Exception as e:
                print(f"[watchdog] loop error: {e}", flush=True)
            time.sleep(45)
    threading.Thread(target=_loop, daemon=True, name=name).start()
    print(f"[auto-scheduler] started (edge auto-start + 45s keep-alive "
          f"watchdog)", flush=True)

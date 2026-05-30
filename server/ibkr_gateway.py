"""
APEX Auth Server — per-user IB Gateway manager  (v4.6.40)
─────────────────────────────────────────────────────────────────────
Runs an IBKR **paper** IB Gateway on the Oracle server, logged into the
user's own paper account, so the cloud bot runner can trade IBKR 24/7
WITHOUT the user keeping TWS open on their laptop.

Design
------
* ONE gateway per user, shared by all of that user's IBKR bots — exactly
  like the local model (each bot connects with its own clientId; the
  per-bot sub-portfolio ledger keeps them isolated).
* Login is automated with **IBC** (IBController) running IB Gateway under
  **Xvfb** (the Gateway is a Java GUI app even in "headless" use).
* Credentials come from the user's encrypted credential blob
  (server/credentials.py) — IBKR_USERNAME / IBKR_PASSWORD /
  IBKR_TRADING_MODE.  They are written to a per-user IBC config that lives
  under a 0700 dir and is rewritten on every (re)launch.
* PAPER ONLY for now.  Live accounts require IBKR Mobile 2FA, which can't
  be satisfied head-less; ensure_gateway() refuses a non-paper mode.

This module is import-safe and NEVER raises at import: all heavy paths are
inside functions and guarded.  ensure_gateway() raises a clear RuntimeError
(not a bare exception) when something is genuinely misconfigured so the
caller can surface a readable message and fall back.

Environment (set at deploy time on Oracle; all have sane defaults)
------------------------------------------------------------------
    APEX_IBC_PATH          IBC install dir            (default /opt/ibc)
    APEX_TWS_PATH          IB Gateway install root    (default /opt/ibgateway)
    APEX_TWS_VERSION       Gateway major version str  (default "10.30")
    APEX_JAVA_PATH         JRE dir IBC should use     (default "" = IBC autodetect)
    APEX_IBKR_GW_BASE_PORT first API port             (default 4100)
    APEX_IBKR_GW_STATE     per-user settings root     (default /opt/apex_users)
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

# ── Config (env-overridable at deploy) ──────────────────────────────────

IBC_PATH    = Path(os.environ.get("APEX_IBC_PATH",  "/opt/ibc"))
TWS_PATH    = Path(os.environ.get("APEX_TWS_PATH",  "/opt/ibgateway"))
TWS_VERSION = os.environ.get("APEX_TWS_VERSION",    "10.30")
JAVA_PATH   = os.environ.get("APEX_JAVA_PATH",      "")
BASE_PORT   = int(os.environ.get("APEX_IBKR_GW_BASE_PORT", "4100"))
STATE_ROOT  = Path(os.environ.get("APEX_IBKR_GW_STATE", "/opt/apex_users"))

# How long to wait for the API port to come up after a launch.
_LAUNCH_TIMEOUT_S = 90
# Range of ports we hand out (BASE_PORT .. BASE_PORT+_PORT_SPAN-1).
_PORT_SPAN = 400

# user_id -> Popen
_PROCS: dict[int, subprocess.Popen] = {}
_LOCK = threading.Lock()


# ── Port / path helpers ─────────────────────────────────────────────────

def user_port(user_id: int) -> int:
    """Deterministic, collision-free API port for this user."""
    return BASE_PORT + (int(user_id) % _PORT_SPAN)


def _user_dir(user_id: int) -> Path:
    d = STATE_ROOT / f"user_{user_id}" / "ibgateway"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _ibc_ini(user_id: int) -> Path:
    return _user_dir(user_id) / "config.ini"


def _pid_file(user_id: int) -> Path:
    return _user_dir(user_id) / "gateway.pid"


def _log_file(user_id: int) -> Path:
    return _user_dir(user_id) / "gateway.log"


def port_is_up(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is accepting TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# ── IBC config ──────────────────────────────────────────────────────────

def _write_ibc_ini(user_id: int, username: str, password: str,
                   mode: str, port: int) -> Path:
    """Write the per-user IBC config.ini.  Recreated every launch so a
    password change takes effect and stale creds never linger."""
    ini = _ibc_ini(user_id)
    # IBC reads IbLoginId/IbPassword from here when not passed on the CLI.
    # We pass user/pw on the CLI too, but keeping them here lets IBC's
    # auto-restart relogin without re-invoking ibcstart.
    content = "\n".join([
        f"IbLoginId={username}",
        f"IbPassword={password}",
        f"TradingMode={mode}",
        # Expose the API and let the bot connect without a manual popup.
        f"OverrideTwsApiPort={port}",
        "AcceptIncomingConnectionAction=accept",
        "AllowBlindTrading=yes",
        "ReadOnlyApi=no",
        # Headless hygiene: dismiss dialogs, no daily logoff prompt blocking.
        "DismissPasswordExpiryWarning=yes",
        "DismissNSEComplianceNotice=yes",
        "AcceptNonBrokerageAccountWarning=yes",
        "MinimizeMainWindow=yes",
        # IBKR forces a daily restart; let IBC restart rather than exit.
        "AutoRestartTime=11:45 PM",
        "IbAutoClosedown=no",
        # Paper has no 2FA, but be explicit so a stray prompt times out
        # instead of hanging the launch forever.
        "ExitAfterSecondFactorAuthenticationTimeout=yes",
        "SecondFactorAuthenticationExitInterval=60",
        "",
    ])
    ini.write_text(content, encoding="utf-8")
    try:
        os.chmod(ini, 0o600)
    except OSError:
        pass
    return ini


# ── Process lifecycle ───────────────────────────────────────────────────

def _ibcstart_script() -> Path:
    return IBC_PATH / "scripts" / "ibcstart.sh"


def _preflight() -> Optional[str]:
    """Return a human-readable reason the gateway can't be launched, or
    None when the host looks provisioned."""
    if shutil.which("xvfb-run") is None:
        return "xvfb-run not found (apt install xvfb)"
    sh = _ibcstart_script()
    if not sh.exists():
        return f"IBC not installed at {sh}"
    if not TWS_PATH.exists():
        return f"IB Gateway not installed at {TWS_PATH}"
    return None


def _read_pid(user_id: int) -> int:
    p = _pid_file(user_id)
    try:
        return int(p.read_text(encoding="utf-8").strip() or 0) if p.exists() else 0
    except (OSError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running(user_id: int) -> bool:
    """A gateway counts as running only if its API port is actually up —
    a live PID whose port isn't listening yet is 'still starting'."""
    return port_is_up(user_port(user_id))


def ensure_gateway(user_id: int, username: str, password: str,
                   mode: str = "paper") -> int:
    """Make sure this user's IB Gateway is logged in and its API port is
    listening.  Returns the API port.  Idempotent: a no-op (just returns
    the port) when already up.

    Raises RuntimeError with an actionable message when the host isn't
    provisioned or the gateway never comes up — the caller logs it and
    leaves the bot to retry, never crashing."""
    mode = (mode or "paper").lower()
    if mode != "paper":
        raise RuntimeError(
            "cloud IBKR is PAPER-only — live accounts need IBKR Mobile 2FA "
            "which can't be approved head-less on the server.")
    if not username or not password:
        raise RuntimeError("no IBKR paper username/password stored for user "
                           f"{user_id} (sync them from Tools → IBKR).")

    port = user_port(user_id)
    if port_is_up(port):
        return port

    reason = _preflight()
    if reason:
        raise RuntimeError(f"Oracle not provisioned for IBKR: {reason}")

    ini = _write_ibc_ini(user_id, username, password, mode, port)
    settings_dir = _user_dir(user_id) / "tws_settings"
    settings_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "xvfb-run", "-a",
        str(_ibcstart_script()), TWS_VERSION,
        "--gateway",
        f"--mode={mode}",
        f"--user={username}",
        f"--pw={password}",
        f"--tws-path={TWS_PATH}",
        f"--tws-settings-path={settings_dir}",
        f"--ibc-path={IBC_PATH}",
        f"--ibc-ini={ini}",
        "--on2fatimeout=exit",
    ]
    if JAVA_PATH:
        cmd.append(f"--java-path={JAVA_PATH}")

    log_fh = open(_log_file(user_id), "w", buffering=1, encoding="utf-8")
    log_fh.write(f"=== ibgateway launch user={user_id} mode={mode} "
                 f"port={port} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log_fh.flush()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(IBC_PATH),
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,   # detach: survives uvicorn reloads
        )
    except Exception as e:
        log_fh.close()
        raise RuntimeError(f"failed to spawn IB Gateway: {e}")

    with _LOCK:
        _PROCS[user_id] = proc
    try:
        _pid_file(user_id).write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass

    # Wait for the API port to come up (login + API init can take a while).
    deadline = time.time() + _LAUNCH_TIMEOUT_S
    while time.time() < deadline:
        if port_is_up(port):
            return port
        if proc.poll() is not None:
            raise RuntimeError(
                f"IB Gateway exited early (code {proc.returncode}) — see "
                f"{_log_file(user_id)}")
        time.sleep(2)
    raise RuntimeError(
        f"IB Gateway didn't open API port {port} within {_LAUNCH_TIMEOUT_S}s "
        f"— see {_log_file(user_id)}")


def stop_gateway(user_id: int) -> dict:
    """Stop this user's gateway.  Best-effort; never raises."""
    pid = _read_pid(user_id)
    with _LOCK:
        proc = _PROCS.pop(user_id, None)
    target_pid = (proc.pid if proc else 0) or pid
    if not _pid_alive(target_pid):
        try:
            _pid_file(user_id).unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "not_running": True}
    try:
        # Kill the whole process group (xvfb-run + java children).
        os.killpg(os.getpgid(target_pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(target_pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(50):
        if not _pid_alive(target_pid):
            break
        time.sleep(0.1)
    if _pid_alive(target_pid):
        try:
            os.killpg(os.getpgid(target_pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        _pid_file(user_id).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


def status(user_id: int) -> dict:
    port = user_port(user_id)
    return {
        "running": port_is_up(port),
        "port": port,
        "pid": _read_pid(user_id),
        "provision_error": _preflight(),
    }

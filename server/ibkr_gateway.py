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
# Virtual-display base. Oracle Linux has no `xvfb-run` (that's a Debian
# script), so we drive the `Xvfb` binary directly and hand each user a
# deterministic display number (:DISPLAY_BASE + user%span).
_DISPLAY_BASE = int(os.environ.get("APEX_IBKR_GW_DISPLAY_BASE", "100"))
# IB Gateway's built-in API socket defaults (per trading mode). IBC's
# OverrideTwsApiPort is meant to relocate it to user_port(), but some
# Gateway builds ignore the override on first launch and keep the default —
# so ensure_gateway accepts EITHER and persists whichever actually opened.
_DEFAULT_PORTS = {"paper": 4002, "live": 4001}

# user_id -> Popen  (the IB Gateway / IBC process)
_PROCS: dict[int, subprocess.Popen] = {}
# user_id -> Popen  (the per-user Xvfb backing the gateway's display)
_XVFB: dict[int, subprocess.Popen] = {}
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
        # IBKR allows only ONE session per login. IBKR forbids a new session
        # from overriding the user's PRIMARY desktop TWS, so 'secondary' is
        # the correct choice for a cloud gateway: it runs whenever the user's
        # TWS is closed (laptop off — the 24/7 case) and quietly yields if the
        # user opens TWS, instead of hanging or fighting for the session.
        "ExistingSessionDetectedAction=secondary",
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
    if shutil.which("Xvfb") is None:
        return "Xvfb not found (dnf install xorg-x11-server-Xvfb)"
    sh = _ibcstart_script()
    if not sh.exists():
        return f"IBC not installed at {sh}"
    if not TWS_PATH.exists():
        return f"IB Gateway not installed at {TWS_PATH}"
    return None


# ── Virtual display (Xvfb, managed directly — no xvfb-run on RHEL) ───────

def _display_num(user_id: int) -> int:
    """Deterministic X display number for this user."""
    return _DISPLAY_BASE + (int(user_id) % _PORT_SPAN)


def _xvfb_alive(n: int) -> bool:
    """V4.6.62 — True iff a real Xvfb process is serving display :n. Used to
    distinguish a live display from a stale lock/socket left by a -9 kill."""
    try:
        r = subprocess.run(["pgrep", "-f", f"Xvfb :{n}"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


def _ensure_xvfb(user_id: int) -> str:
    """Make sure an Xvfb is backing this user's display; return ':N'.
    Idempotent — reuses a live Xvfb (lock file present + pid alive)."""
    n = _display_num(user_id)
    disp = f":{n}"
    with _LOCK:
        proc = _XVFB.get(user_id)
        if proc is not None and proc.poll() is None:
            return disp
        # A previous run (or another worker) may already own this display.
        # V4.6.62 — but only trust the lock if a REAL Xvfb is actually backing
        # it. A -9 kill leaves a stale /tmp/.X{n}-lock + socket; trusting it
        # routed the Gateway to a dead display and it exited instantly (code
        # 1100/76). If the lock is stale, clear it + the socket and relaunch.
        if Path(f"/tmp/.X{n}-lock").exists():
            if _xvfb_alive(n):
                return disp
            for stale in (f"/tmp/.X{n}-lock", f"/tmp/.X11-unix/X{n}"):
                try:
                    os.remove(stale)
                except Exception:
                    pass
        xlog = open(_user_dir(user_id) / "xvfb.log", "w",
                    buffering=1, encoding="utf-8")
        proc = subprocess.Popen(
            ["Xvfb", disp, "-screen", "0", "1024x768x16", "-nolisten", "tcp"],
            stdout=xlog, stderr=subprocess.STDOUT, start_new_session=True)
        _XVFB[user_id] = proc
    # Give Xvfb a moment to create the display socket.
    for _ in range(20):
        if Path(f"/tmp/.X{n}-lock").exists():
            break
        time.sleep(0.1)
    return disp


def _stop_xvfb(user_id: int) -> None:
    with _LOCK:
        proc = _XVFB.pop(user_id, None)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass


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


def _port_file(user_id: int) -> Path:
    return _user_dir(user_id) / "api_port"


def _save_active_port(user_id: int, port: int) -> None:
    try:
        _port_file(user_id).write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def active_port(user_id: int, mode: str = "paper") -> int:
    """The API port this user's gateway is ACTUALLY serving on. Reads the
    port ensure_gateway persisted; falls back to the deterministic
    user_port(), then to whichever candidate is currently listening. The
    cloud bot runner uses this so it connects to the real port even when the
    Gateway ignored OverrideTwsApiPort and kept its default."""
    p = _port_file(user_id)
    try:
        if p.exists():
            v = int(p.read_text(encoding="utf-8").strip() or 0)
            if v > 0:
                return v
    except (OSError, ValueError):
        pass
    for cand in _candidate_ports(user_id, mode):
        if port_is_up(cand):
            return cand
    return user_port(user_id)


def _candidate_ports(user_id: int, mode: str = "paper") -> list[int]:
    """Ports a freshly-launched gateway might bind: the override target
    first, then the Gateway's built-in default for this mode."""
    out = [user_port(user_id)]
    d = _DEFAULT_PORTS.get((mode or "paper").lower(), 4002)
    if d not in out:
        out.append(d)
    return out


def is_running(user_id: int, mode: str = "paper") -> bool:
    """A gateway counts as running only if an API port is actually up —
    a live PID whose port isn't listening yet is 'still starting'."""
    return any(port_is_up(p) for p in _candidate_ports(user_id, mode))


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
    candidates = _candidate_ports(user_id, mode)
    # Idempotent: if a port is already serving, persist + return it.
    for cand in candidates:
        if port_is_up(cand):
            _save_active_port(user_id, cand)
            return cand

    reason = _preflight()
    if reason:
        raise RuntimeError(f"Oracle not provisioned for IBKR: {reason}")

    ini = _write_ibc_ini(user_id, username, password, mode, port)
    settings_dir = _user_dir(user_id) / "tws_settings"
    settings_dir.mkdir(parents=True, exist_ok=True)

    # Bring up this user's virtual display (Oracle Linux has no xvfb-run).
    display = _ensure_xvfb(user_id)

    cmd = [
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

    launch_env = dict(os.environ)
    launch_env["DISPLAY"] = display

    log_fh = open(_log_file(user_id), "w", buffering=1, encoding="utf-8")
    log_fh.write(f"=== ibgateway launch user={user_id} mode={mode} "
                 f"port={port} display={display} "
                 f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log_fh.flush()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(IBC_PATH), env=launch_env,
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

    # Wait for an API port to come up (login + API init can take a while).
    # The gateway may bind the override port OR its built-in default — accept
    # whichever opens and remember it so the bot connects to the real port.
    deadline = time.time() + _LAUNCH_TIMEOUT_S
    while time.time() < deadline:
        for cand in candidates:
            if port_is_up(cand):
                _save_active_port(user_id, cand)
                return cand
        if proc.poll() is not None:
            raise RuntimeError(
                f"IB Gateway exited early (code {proc.returncode}) — see "
                f"{_log_file(user_id)}")
        time.sleep(2)
    raise RuntimeError(
        f"IB Gateway didn't open an API port ({candidates}) within "
        f"{_LAUNCH_TIMEOUT_S}s — see {_log_file(user_id)}")


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
    # Tear down the backing virtual display too.
    _stop_xvfb(user_id)
    return {"ok": True}


def status(user_id: int, mode: str = "paper") -> dict:
    port = active_port(user_id, mode)
    return {
        "running": port_is_up(port),
        "port": port,
        "pid": _read_pid(user_id),
        "provision_error": _preflight(),
    }

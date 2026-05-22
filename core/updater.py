"""
APEX Auto-Updater
─────────────────────────────────────────────────────────────────
Checks a GitHub repository for new versions.
Downloads and applies updates without reinstalling.

Setup (one time):
  1. Create a GitHub repo (e.g. github.com/yourname/apex-trading)
  2. Push your project files there
  3. Create a file called version.json in the root with:
     {"version": "1.0.0", "notes": "Initial release"}
  4. Set GITHUB_REPO in this file to your repo

When you want to push an update:
  1. Edit version.json → bump the version number
  2. Push all changed files to GitHub
  3. The app will detect the new version on next launch and offer to update

─────────────────────────────────────────────────────────────────
"""

import os
import sys
import json
import zipfile
import shutil
import tempfile
from pathlib import Path
from typing import Optional

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── CONFIGURE THIS ────────────────────────────────────────
# Set to your GitHub repo: "username/repo-name"
GITHUB_REPO    = "B4ptiste16/ApexTrading"
CURRENT_VERSION = "1.0.0"


def _bundled_version_file() -> Path:
    """v3.1.2 — in a PyInstaller --onedir build the project source lives
    inside an archive, so Path(__file__).parent.parent doesn't reach the
    data directory. Data files (version.json, BOT_SKELETON.md, assets/)
    land under sys._MEIPASS instead.

    In source / dev mode, fall back to the project-root version.json so
    `python main.py` still picks up the right version."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "version.json"
    return Path(__file__).parent.parent / "version.json"


VERSION_FILE = _bundled_version_file()
# ─────────────────────────────────────────────────────────


def get_current_version() -> str:
    """Read version from the bundled (or dev) version.json."""
    if VERSION_FILE.exists():
        try:
            with open(VERSION_FILE) as f:
                return json.load(f).get("version", CURRENT_VERSION)
        except Exception:
            pass
    return CURRENT_VERSION


def check_for_update() -> Optional[dict]:
    """
    Check GitHub for a newer version.
    Returns update info dict if update available, None if up to date or error.
    """
    if not HAS_REQUESTS:
        return None

    try:
        url  = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/version.json"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return None

        remote = resp.json()
        remote_ver  = remote.get("version", "0.0.0")
        current_ver = get_current_version()

        if _version_gt(remote_ver, current_ver):
            return {
                "current":  current_ver,
                "latest":   remote_ver,
                "notes":    remote.get("notes", ""),
                "download": f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip",
                "installer": _latest_release_installer(),
            }
        return None

    except Exception as e:
        print(f"[updater] Check failed: {e}")
        return None


def _latest_release_installer() -> Optional[str]:
    """URL of the .exe asset on the newest GitHub Release (the packaged
    installer the frozen app self-installs). None if no release/asset."""
    if not HAS_REQUESTS:
        return None
    try:
        api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        r = requests.get(api, timeout=8)
        if r.status_code != 200:
            return None
        for a in r.json().get("assets", []):
            name = (a.get("name") or "").lower()
            if name.endswith(".exe"):
                return a.get("browser_download_url")
    except Exception as e:
        print(f"[updater] release lookup failed: {e}")
    return None


def _apply_installer(update_info: dict, progress_callback=None) -> tuple:
    """Frozen build: stream-download the release .exe, strip the
    Mark-of-the-Web (so SmartScreen does NOT silently block the silent
    install), and return success. The app does NOT exit here — the
    caller asks the user, then calls launch_downloaded_installer() to
    actually run it (the running app must exit at that point so the
    installer can replace files)."""
    try:
        if progress_callback:
            progress_callback(2, "Connecting...")
        with requests.get(update_info["installer"],
                          stream=True, timeout=60) as r:
            if r.status_code != 200:
                return False, f"Download failed: HTTP {r.status_code}"
            total = int(r.headers.get("content-length") or 0)
            dst   = Path(tempfile.gettempdir()) / "APEX_Setup_update.exe"
            done  = 0
            with open(dst, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress_callback:
                        if total:
                            pct = 2 + int(done * 96 / total)   # 2..98
                            msg = (f"Downloading update… "
                                   f"{done // 1024 // 1024} / "
                                   f"{total // 1024 // 1024} MB")
                        else:
                            pct = 50
                            msg = f"Downloading update… {done // 1024 // 1024} MB"
                        progress_callback(min(pct, 98), msg)

        # Strip Mark-of-the-Web so SmartScreen does NOT block the
        # silent execution of this freshly-downloaded unsigned .exe.
        try:
            os.remove(str(dst) + ":Zone.Identifier")
        except Exception:
            pass

        if progress_callback:
            progress_callback(100, "Download complete.")
        update_info["_local_path"] = str(dst)
        return True, (f"Downloaded v{update_info.get('latest','?')}  "
                      f"— ready to install.")
    except Exception as e:
        return False, f"Update failed: {e}"


def launch_downloaded_installer(local_path: str) -> None:
    """Called AFTER the user confirms.

    Writes a relauncher batch script to %TEMP% that:
      1. Waits for the old APEX.exe to fully exit (releases file locks)
      2. Runs the installer silently
      3. Pauses briefly so Windows can release file locks
      4. Launches the freshly-installed APEX
      5. Self-deletes
    Then exits this process so the installer can replace files.

    All steps log to %TEMP%\\apex_update.log so failures are debuggable.
    """
    import subprocess
    exe = sys.executable
    bat = Path(tempfile.gettempdir()) / "apex_update_relaunch.bat"
    # Use CRLF + cp1252 — Windows cmd's preferred batch format.
    # V7.1.5: replace the wait-for-exit loop with taskkill /F /T.
    # The wait loop was getting stuck because a running bot is its
    # own APEX.exe subprocess (main.py spawns APEX.exe --run-bot for
    # each bot via QProcess), and those subprocesses don't die when
    # the GUI exits — so tasklist always found at least one APEX.exe
    # and the loop never reached :run. taskkill /F /IM APEX.exe /T
    # force-kills the GUI plus all child bot processes in one shot.
    #
    # We also drop the explicit `start "" "{exe}"` relaunch — Inno's
    # [Run] section in installer.iss handles relaunch when the user
    # leaves the "Launch APEX" checkbox ticked at the end of the
    # wizard. Trying to do it from the batch as well risked spawning
    # two instances and triggering the single-instance mutex check
    # to silently exit one of them.
    #
    # Each step is logged separately so future failures are easy to
    # diagnose from %TEMP%\apex_update.log.
    # V7.1.7 fix: drop the /T (tree) flag from taskkill. With /T, the
    # process tree being killed *includes the cmd shell that's running
    # taskkill*, which Windows correctly refuses ("The process cannot
    # terminate itself") and leaves the batch in an undefined state —
    # the user saw Step 1 logged with errors but Step 2 never reached.
    # Plain `/F /IM APEX.exe` kills every APEX.exe by image name (GUI
    # + all --run-bot subprocesses) without touching cmd / taskkill /
    # this batch script.
    bat.write_text(
        "@echo off\r\n"
        "set LOG=%TEMP%\\apex_update.log\r\n"
        "echo. >> \"%LOG%\"\r\n"
        "echo === APEX Update %DATE% %TIME% === >> \"%LOG%\"\r\n"
        "echo Step 1: kill APEX.exe instances >> \"%LOG%\"\r\n"
        "taskkill /F /IM APEX.exe >> \"%LOG%\" 2>&1\r\n"
        "echo Step 2: settle (2s) >> \"%LOG%\"\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        "echo Step 3: launch installer >> \"%LOG%\"\r\n"
        f"echo Installer: \"{local_path}\" >> \"%LOG%\"\r\n"
        f"\"{local_path}\" /SP- /NORESTART\r\n"
        "echo Step 4: installer exit code: %ERRORLEVEL% >> \"%LOG%\"\r\n"
        "echo Step 5: done >> \"%LOG%\"\r\n"
        "del \"%~f0\" >nul 2>&1\r\n",
        encoding="cp1252",
    )
    # V7.1.5: CREATE_NO_WINDOW (0x08000000) instead of DETACHED_PROCESS.
    # The previous DETACHED_PROCESS flag left a visible cmd console
    # because Windows spawns its own console for the new cmd /c shell
    # — DETACHED only detaches from the *parent's* console, it doesn't
    # suppress a new one. CREATE_NO_WINDOW actually hides the cmd shell,
    # so the user only sees the Inno installer UI (which is a separate
    # process and stays visible by design).
    # CREATE_NO_WINDOW and DETACHED_PROCESS can't be combined per MSDN.
    CREATE_NO_WINDOW         = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )
    os._exit(0)


def download_and_apply(update_info: dict,
                       progress_callback=None) -> tuple:
    """
    Download the latest version zip from GitHub and apply it.
    Backs up current files before overwriting.
    Returns (success: bool, message: str)
    """
    if not HAS_REQUESTS:
        return False, "requests package not installed"

    # ── Installed (frozen) build: self-install the packaged release ──
    if getattr(sys, "frozen", False) and update_info.get("installer"):
        return _apply_installer(update_info, progress_callback)

    app_root = Path(__file__).parent.parent.parent  # project root

    try:
        # ── Download ──────────────────────────────────────
        if progress_callback:
            progress_callback(10, "Downloading update...")

        resp = requests.get(update_info["download"], stream=True, timeout=30)
        if resp.status_code != 200:
            return False, f"Download failed: HTTP {resp.status_code}"

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            for chunk in resp.iter_content(chunk_size=8192):
                tmp.write(chunk)

        if progress_callback:
            progress_callback(40, "Extracting...")

        # ── Extract ───────────────────────────────────────
        with tempfile.TemporaryDirectory() as extract_dir:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                zf.extractall(extract_dir)

            # GitHub zips have a root folder like "repo-main/"
            extracted = Path(extract_dir)
            subdirs   = list(extracted.iterdir())
            source    = subdirs[0] if subdirs else extracted

            if progress_callback:
                progress_callback(60, "Backing up current files...")

            # ── Backup ────────────────────────────────────
            backup_dir = app_root / f"backup_{get_current_version()}"
            backup_dir.mkdir(exist_ok=True)

            # Files to update — Python files and assets
            update_extensions = {".py", ".json", ".txt", ".md", ".css"}
            skip_dirs = {"__pycache__", ".git", "backup_*",
                         "longv2_charts", "shortv2_charts", "daybot_charts",
                         "bot_uploads"}

            for f in app_root.rglob("*"):
                if f.is_file() and f.suffix in update_extensions:
                    if not any(d in str(f) for d in skip_dirs):
                        rel = f.relative_to(app_root)
                        bk  = backup_dir / rel
                        bk.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, bk)

            if progress_callback:
                progress_callback(80, "Applying update...")

            # ── Apply ─────────────────────────────────────
            # Copy new files from source to app_root
            # Skip: .env, state files, log files, chart dirs, universe txts
            skip_files = {
                ".env", "daybot_state.json", "shortv2_state.json",
                "longv2_state.json", "daybot_universe.txt",
                "longbot_universe.txt", "shortbot_universe.txt",
                "daybot_watchlist.txt",
            }

            for src_file in source.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(source)
                    if rel.name in skip_files:
                        continue
                    if any(d in str(rel) for d in skip_dirs):
                        continue
                    dest = app_root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest)

        tmp_path.unlink(missing_ok=True)

        if progress_callback:
            progress_callback(100, "Update complete!")

        return True, f"Updated to v{update_info['latest']}. Restart the app to apply."

    except Exception as e:
        return False, f"Update failed: {e}"


def _version_gt(a: str, b: str) -> bool:
    """Returns True if version a > version b."""
    try:
        av = tuple(int(x) for x in a.split("."))
        bv = tuple(int(x) for x in b.split("."))
        return av > bv
    except Exception:
        return False


def restart_app():
    """Restart the application."""
    python = sys.executable
    os.execl(python, python, *sys.argv)

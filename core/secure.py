"""
APEX · Secure bot-file storage  (V3.1.5)
─────────────────────────────────────────────────────────────────────
Encrypts the user's local custom-bot library so the .py files can't be
read by File Explorer's preview pane, a text editor, or another app
that happens to scan the user's documents folder.

Honest disclaimer on the strength of this:
  • This is OBFUSCATION, not vault-grade encryption. The Fernet key
    used to wrap each file is a constant baked into the APEX binary.
    A determined attacker with access to APEX.exe can dump the key
    and decrypt everything offline.
  • What it DOES protect against:
      - Casual filesystem inspection (the encrypted blob is
        gibberish to text editors)
      - Backup / sync utilities (the .apex extension won't be
        previewed by Dropbox / OneDrive / etc.)
      - Co-workers / family who borrow your laptop
  • What it does NOT protect against:
      - Anyone who runs your APEX install and looks at the running
        Python process memory
      - Reverse-engineers willing to spend an afternoon with a
        disassembler
  • For real protection: run bots in CLOUD MODE (server-side, on
    Oracle). The bot source never lives on the user's filesystem,
    period.

File format:
  Encrypted files have the `.apex` extension. The bytes inside are a
  Fernet token = base64-urlsafe of (version + timestamp + IV + HMAC +
  ciphertext). Fernet handles authenticated encryption + tampering
  detection in one shot.

Public API:
  encrypt_file(src_path, dst_path) → None
  decrypt_file(src_path) → bytes
  encrypt_bytes(blob) → bytes
  decrypt_bytes(blob) → bytes
  lock_bot_library(bots_dir) → list[str]   # list of encrypted slugs
  unlock_bot_library(bots_dir) → list[str]
  is_locked(bots_dir) → bool
  decrypted_temp_file(apex_path) → Path    # decrypt to a temp .py
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Optional


# The Fernet key is derived from a constant phrase. Deterministic so
# every install agrees, which means the same encrypted .apex file is
# readable by any APEX install — *intentional*, otherwise installs
# from the marketplace would be unusable. If you want per-install
# keys later, derive from the user's JWT secret instead.
_APEX_LOCK_PHRASE = b"APEX::trading::bot::lock::v1::do-not-modify"

try:
    from cryptography.fernet import Fernet, InvalidToken
    _KEY = base64.urlsafe_b64encode(hashlib.sha256(_APEX_LOCK_PHRASE).digest())
    _F   = Fernet(_KEY)
    HAS_CRYPTO = True
except Exception:
    HAS_CRYPTO = False
    _F = None
    InvalidToken = Exception   # type: ignore


# Marker file the toggle drops next to the bots dir so we know if the
# library was deliberately locked (vs. just having a stray .apex file).
_LOCK_MARKER = ".apex_locked"


# ── Single-file ────────────────────────────────────────────────────

def encrypt_bytes(blob: bytes) -> bytes:
    if not HAS_CRYPTO:
        raise RuntimeError("'cryptography' package missing — pip install cryptography")
    return _F.encrypt(blob)


def decrypt_bytes(blob: bytes) -> bytes:
    if not HAS_CRYPTO:
        raise RuntimeError("'cryptography' package missing — pip install cryptography")
    try:
        return _F.decrypt(blob)
    except InvalidToken:
        raise ValueError("Could not decrypt — file is corrupt or "
                          "was encrypted with a different APEX version.")


def encrypt_file(src_path: Path, dst_path: Path) -> None:
    dst_path.write_bytes(encrypt_bytes(Path(src_path).read_bytes()))


def decrypt_file(src_path: Path) -> bytes:
    return decrypt_bytes(Path(src_path).read_bytes())


# ── Library-level locking ──────────────────────────────────────────

def is_locked(bots_dir: Path) -> bool:
    return (bots_dir / _LOCK_MARKER).exists()


def lock_bot_library(bots_dir: Path) -> list[str]:
    """Walk bots_dir, encrypt every *.py → *.apex, delete the originals.
    Returns the list of slugs that were locked."""
    bots_dir.mkdir(parents=True, exist_ok=True)
    locked: list[str] = []
    for p in bots_dir.glob("*.py"):
        try:
            encrypt_file(p, p.with_suffix(".apex"))
            p.unlink()
            locked.append(p.stem)
        except Exception as e:
            print(f"[secure] lock {p.name}: {e}", flush=True)
    (bots_dir / _LOCK_MARKER).write_text(
        "Locked by APEX. Do not delete this file unless you mean to "
        "unlock the library.\n", encoding="utf-8")
    return locked


def unlock_bot_library(bots_dir: Path) -> list[str]:
    """Reverse of lock_bot_library — decrypt every .apex → .py and
    delete the encrypted versions."""
    unlocked: list[str] = []
    for p in bots_dir.glob("*.apex"):
        try:
            plain = decrypt_file(p)
            p.with_suffix(".py").write_bytes(plain)
            p.unlink()
            unlocked.append(p.stem)
        except Exception as e:
            print(f"[secure] unlock {p.name}: {e}", flush=True)
    marker = bots_dir / _LOCK_MARKER
    if marker.exists():
        marker.unlink()
    return unlocked


# ── Runtime decryption to a short-lived temp file ──────────────────

def decrypted_temp_file(apex_path: Path) -> Path:
    """Decrypt a .apex file to a short-lived temp .py path so subprocess
    can run it. Caller is responsible for deleting the temp file when
    the subprocess exits.

    The temp file lives in the user's APPDATA temp folder with 0600
    permissions (best-effort on Windows — relies on user-folder ACLs)."""
    plain = decrypt_file(apex_path)
    tmpdir = Path(tempfile.gettempdir()) / "apex_unlock"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=apex_path.stem + "_", suffix=".py",
                                  dir=str(tmpdir))
    os.write(fd, plain)
    os.close(fd)
    try:
        os.chmod(name, 0o600)
    except Exception:
        pass
    return Path(name)


def find_bot_script(bots_dir: Path, slug: str) -> Optional[Path]:
    """Return the on-disk file for a bot slug, regardless of whether
    the library is currently locked or not. Caller checks the suffix
    to know if it needs decryption."""
    for ext in (".apex", ".py"):
        cand = bots_dir / f"{slug}{ext}"
        if cand.exists():
            return cand
    return None

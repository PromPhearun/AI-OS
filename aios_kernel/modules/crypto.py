"""At-rest encryption — AES-256-GCM (Phase 5, Slice 5.1).

Seals the vault (``credentials.json``) and checkpoint ``snapshot.json`` files so
secrets and agent state are unreadable on disk without the master key.
Encryption is *tamper-evident*: AES-GCM authenticates the ciphertext, so a
wrong key, a truncated file, or any bit flip fails ``Cipher.open`` with a clean
error (docs/08-security.md §8).

Key management::

    * explicit   — ``AIOS_MASTER_KEY`` env var: 256-bit key as 64 hex chars or
                   base64 (44 chars). Always wins over a keyfile.
    * generated  — ``AIOS_ENCRYPT=1`` auto-creates ``<data_root>/master.key``
                   (0600, atomic replace, ``secrets.token_bytes``) on first boot
                   and reuses it on later boots.
    * continuity — if ``<data_root>/master.key`` already exists it is used even
                   without ``AIOS_ENCRYPT=1``, so a data root stays encrypted
                   after the flag is toggled off (fail-secure).
    * disabled   — no env key, no flag, no keyfile: ``cipher_for`` returns None
                   and on-disk data stays plaintext (v1 behavior preserved).

On-disk framing::

    MAGIC(8) | nonce(12) | AESGCM ciphertext        # tag is appended by AESGCM

The frame magic lets loaders distinguish a sealed file from a legacy plaintext
file and fail closed instead of misreading either as the other.
"""

from __future__ import annotations

import base64
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"AIOSENC1"
_MAGIC_LEN = len(MAGIC)
_NONCE_LEN = 12
_KEY_BYTES = 32
_KEY_FILE_NAME = "master.key"
_KEY_FILE_MODE = 0o600


class Cipher:
    """AES-256-GCM seal/open using one 32-byte master key."""

    def __init__(self, key: bytes):
        if len(key) != _KEY_BYTES:
            raise ValueError(f"master key must be {_KEY_BYTES} bytes, got {len(key)}")
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes) -> bytes:
        """Encrypt+authenticate ``plaintext`` into a self-framed blob."""
        nonce = secrets.token_bytes(_NONCE_LEN)
        ciphertext = self._aead.encrypt(nonce, plaintext, None)
        return MAGIC + nonce + ciphertext

    def open(self, sealed: bytes) -> bytes:
        """Decrypt+verify a ``seal`` blob; raises on wrong key or tampering."""
        if not sealed.startswith(MAGIC) or len(sealed) <= _MAGIC_LEN + _NONCE_LEN:
            raise ValueError("not an AIOS sealed payload (bad magic or truncated)")
        nonce = sealed[_MAGIC_LEN : _MAGIC_LEN + _NONCE_LEN]
        ciphertext = sealed[_MAGIC_LEN + _NONCE_LEN :]
        # AESGCM authenticates: wrong key / tampered data raises InvalidTag.
        return self._aead.decrypt(nonce, ciphertext, None)


# ------------------------------------------------------------------ key mgmt
def _parse_key(raw: str) -> bytes:
    """Accept a 256-bit key as 64 hex chars or base64 (43-44 chars)."""
    raw = raw.strip()
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    if len(raw) in (43, 44):
        try:
            key = base64.b64decode(raw, validate=True)
            if len(key) == _KEY_BYTES:
                return key
        except ValueError:
            pass
    raise ValueError(
        "AIOS_MASTER_KEY must be 64 hex chars or base64 of a 32-byte key"
    )


def _read_keyfile(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(data) != _KEY_BYTES:
        raise ValueError(f"{path} is not a {_KEY_BYTES}-byte master key file")
    return data


def _load_or_create_keyfile(root: Path) -> bytes:
    """Return the existing keyfile or generate + atomically persist a new one."""
    existing = _read_keyfile(root / _KEY_FILE_NAME)
    if existing is not None:
        return existing
    key = secrets.token_bytes(_KEY_BYTES)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=_KEY_FILE_NAME, dir=root)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _KEY_FILE_MODE)
        os.replace(tmp, root / _KEY_FILE_NAME)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return key


def cipher_for(data_root: str | Path | None) -> Cipher | None:
    """Build the at-rest cipher for a data root, or None to stay plaintext.

    ``data_root`` is the kernel's data root (the directory that owns
    credentials.json, checkpoints/, session.json). Reads ``AIOS_MASTER_KEY`` /
    ``AIOS_ENCRYPT`` from the process environment at construction time.
    """
    root = Path(data_root) if data_root else None
    raw: bytes | None = None
    env_key = os.environ.get("AIOS_MASTER_KEY")
    if env_key:
        raw = _parse_key(env_key)
    elif root is not None:
        if os.environ.get("AIOS_ENCRYPT") == "1":
            raw = _load_or_create_keyfile(root)
        else:
            raw = _read_keyfile(root / _KEY_FILE_NAME)  # fail-secure continuity
    return Cipher(raw) if raw else None
"""Unit tests: AES-256-GCM at-rest cipher (Phase 5, Slice 5.1).

Covers the Cipher (seal/open framing, wrong-key + tamper rejection, random
nonce) and the key-management factory ``cipher_for`` (env hex/base64 keys,
``AIOS_ENCRYPT=1`` generated ``master.key`` with 0600 perms, fail-secure
continuity when the flag is toggled off).
"""

from __future__ import annotations

import base64
import os

import pytest

from aios_kernel.modules.crypto import (
    MAGIC,
    Cipher,
    _KEY_FILE_NAME,
    _parse_key,
    cipher_for,
)

KEY_HEX = "a" * 64
KEY_B64 = base64.b64encode(bytes.fromhex(KEY_HEX)).decode()
KEY_BYTES = bytes.fromhex(KEY_HEX)


def _cipher() -> Cipher:
    return Cipher(KEY_BYTES)


# ------------------------------------------------------------------ Cipher
def test_round_trip() -> None:
    sealed = _cipher().seal(b"hello world")
    assert sealed.startswith(MAGIC)
    assert _cipher().open(sealed) == b"hello world"


def test_wrong_key_rejected() -> None:
    other = Cipher(bytes.fromhex("b" * 64))
    with pytest.raises(Exception):
        other.open(_cipher().seal(b"top secret"))


def test_tampered_ciphertext_rejected() -> None:
    sealed = bytearray(_cipher().seal(b"integrity matters"))
    sealed[-1] ^= 0xFF  # flip one bit in the payload
    with pytest.raises(Exception):
        _cipher().open(bytes(sealed))


def test_bad_magic_rejected() -> None:
    with pytest.raises(ValueError, match="magic"):
        _cipher().open(b"NOTSEALED-garbage")


def test_truncated_payload_rejected() -> None:
    sealed = _cipher().seal(b"payload")
    with pytest.raises(ValueError, match="magic|truncated"):
        _cipher().open(sealed[: len(MAGIC) + 4])


def test_short_key_rejected() -> None:
    with pytest.raises(ValueError):
        Cipher(b"too-short")


def test_fresh_nonce_per_seal() -> None:
    c = _cipher()
    assert c.seal(b"same") != c.seal(b"same")  # randomized nonce each time


# ------------------------------------------------------------ key parsing
def test_parse_key_hex_and_base64() -> None:
    assert _parse_key(KEY_HEX) == KEY_BYTES
    assert _parse_key(KEY_B64) == KEY_BYTES
    assert _parse_key(f"  {KEY_HEX}\n") == KEY_BYTES  # whitespace tolerated
    with pytest.raises(ValueError):
        _parse_key("nope")


# ------------------------------------------------------------- cipher_for
def test_disabled_without_any_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIOS_MASTER_KEY", raising=False)
    monkeypatch.delenv("AIOS_ENCRYPT", raising=False)
    assert cipher_for(tmp_path) is None
    assert cipher_for(None) is None


def test_env_hex_key_wins_over_everything(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_HEX)
    c = cipher_for(tmp_path)
    assert c is not None and c.open(c.seal(b"x")) == b"x"
    # no keyfile is auto-created when an explicit env key is used
    assert not (tmp_path / _KEY_FILE_NAME).exists()


def test_env_base64_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_B64)
    c = cipher_for(None)
    assert c is not None and c.open(c.seal(b"x")) == b"x"


def test_env_invalid_key_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", "not-a-real-key")
    with pytest.raises(ValueError):
        cipher_for(None)


def test_generated_keyfile_mode_0600_and_reuse(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_ENCRYPT", "1")
    monkeypatch.delenv("AIOS_MASTER_KEY", raising=False)
    c1 = cipher_for(tmp_path)
    assert c1 is not None
    keyfile = tmp_path / _KEY_FILE_NAME
    assert keyfile.is_file()
    assert (keyfile.stat().st_mode & 0o777) == 0o600
    assert keyfile.read_bytes() not in (b"", os.urandom(32))  # non-trivial
    # a second boot reuses the exact same key
    c2 = cipher_for(tmp_path)
    assert c1.open(c2.seal(b"x")) == b"x"


def test_keyfile_continuity_after_flag_toggled_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_ENCRYPT", "1")
    monkeypatch.delenv("AIOS_MASTER_KEY", raising=False)
    c1 = cipher_for(tmp_path)
    monkeypatch.delenv("AIOS_ENCRYPT", raising=False)
    c2 = cipher_for(tmp_path)  # no flag, no env key, but keyfile exists
    assert c1 is not None and c2 is not None
    assert c1.open(c2.seal(b"x")) == b"x"
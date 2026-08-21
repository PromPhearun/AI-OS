"""Integration: AES-256-GCM at-rest encryption (Phase 5, Slice 5.1).

Checkpoint ``snapshot.json`` and vault ``credentials.json`` are sealed on disk
when the kernel has a master key; the manifest hash covers the ciphertext;
resume across a restart works with the right key and fails closed with the
wrong key or tampered data.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from aios_kernel import Kernel
from aios_kernel.errors import AiosError, E_NOENT
from aios_kernel.modules.crypto import MAGIC
from aios_kernel.modules.llm_core import MockLLM

from ..conftest import _base_spec

KEY_A = "aa" * 32  # 64 hex chars -> 32 bytes
KEY_B = "bb" * 32


@pytest.mark.asyncio
async def test_checkpoint_snapshot_sealed_on_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_A)
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        pid = await k.spawn_agent(_base_spec(name="enc-ckpt"))
        k.context.append(pid, "user", "secret plan: buy at dawn")
        cid = k.storage.checkpoint(pid)

        d = tmp_path / "checkpoints" / cid
        snap = (d / "snapshot.json").read_bytes()
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        assert snap.startswith(MAGIC)
        assert b"secret plan" not in snap  # plaintext never touches disk
        assert b'"spec"' not in snap
        # the manifest hash covers the ciphertext on disk (tamper detection)
        assert manifest["hash"] == hashlib.sha256(snap).hexdigest()

        # restore still round-trips through the cipher
        k.storage._checkpoints.pop(cid)
        ckpt = k.storage.get(cid)
        assert [m.content for m in ckpt.context][-1] == "secret plan: buy at dawn"
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_no_key_keeps_plaintext_behavior(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIOS_MASTER_KEY", raising=False)
    monkeypatch.delenv("AIOS_ENCRYPT", raising=False)
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        pid = await k.spawn_agent(_base_spec(name="plain"))
        k.context.append(pid, "user", "plaintext turn")
        cid = k.storage.checkpoint(pid)
        snap = (tmp_path / "checkpoints" / cid / "snapshot.json").read_bytes()
        assert not snap.startswith(MAGIC)
        assert b"plaintext turn" in snap
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_resume_round_trip_with_encryption(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_A)
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    pid = await k1.spawn_agent(_base_spec(name="enc-roundtrip", priority=3))
    k1.context.append(pid, "user", "persist me", pinned=True)
    k1.memory.write(pid, "tally", 7)
    k1.scheduler.account_llm(pid, tokens_in=4, tokens_out=5, cost=0.002)
    k1.storage.checkpoint(pid)
    # NB: do not shutdown k1 before k2 boots — a clean shutdown removes the
    # resume record (kill -> remove_session_record); --resume is the crash
    # path where the record is still on disk.

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.restore_session() == [pid]
        acb = k2.agent_manager.get(pid)
        assert acb.state.value == "suspended"
        ctx = k2.context.read(pid)
        assert [m["content"] for m in ctx] == ["You are a test agent.", "persist me"]
        assert ctx[-1]["pinned"] is True
        assert k2.memory.read(pid, "tally") == 7
        assert acb.usage.tokens_in == 4 and acb.usage.tokens_out == 5
    finally:
        await k2.shutdown()
    await k1.shutdown()


@pytest.mark.asyncio
async def test_wrong_key_on_resume_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_A)
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    pid = await k1.spawn_agent(_base_spec(name="enc-resume"))
    k1.context.append(pid, "user", "persist me")
    k1.storage.checkpoint(pid)
    # NB: keep k1 alive so the resume record stays on disk (see above).

    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_B)
    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        with pytest.raises(AiosError, match="decryption|integrity"):
            k2.restore_session()
    finally:
        await k2.shutdown()
    await k1.shutdown()


@pytest.mark.asyncio
async def test_tampered_sealed_snapshot_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_A)
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        pid = await k.spawn_agent(_base_spec(name="tamper-enc"))
        cid = k.storage.checkpoint(pid)
        k.storage._checkpoints.pop(cid)
        d = tmp_path / "checkpoints" / cid

        snap = bytearray((d / "snapshot.json").read_bytes())
        snap[-1] ^= 0xFF
        (d / "snapshot.json").write_bytes(bytes(snap))
        # sha256 over the mutated ciphertext now mismatches the manifest hash
        with pytest.raises(AiosError, match="integrity"):
            k.storage.get(cid)

        # attacker rewrites the manifest hash too -> GCM auth still catches it
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        manifest["hash"] = hashlib.sha256(bytes(snap)).hexdigest()
        (d / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        with pytest.raises(AiosError, match="decryption"):
            k.storage.get(cid)
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_vault_sealed_on_disk_and_wrong_key_fails_closed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_A)
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    k1.vault.set("api_token", "sk-super-secret-value")
    await k1.shutdown()

    creds = (tmp_path / "credentials.json").read_bytes()
    assert creds.startswith(MAGIC)
    assert b"sk-super-secret-value" not in creds

    # same key on a fresh boot -> secrets come back
    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.vault.get("api_token") == "sk-super-secret-value"
    finally:
        await k2.shutdown()

    # wrong key -> fail closed: empty vault, get_env raises E_NOENT
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_B)
    k3 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k3.vault.keys() == []
        with pytest.raises(AiosError) as ei:
            k3.vault.get("api_token")
        assert ei.value.code == E_NOENT
    finally:
        await k3.shutdown()


@pytest.mark.asyncio
async def test_tampered_sealed_vault_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_MASTER_KEY", KEY_A)
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    k1.vault.set("token", "sensitive-value")
    await k1.shutdown()

    p = tmp_path / "credentials.json"
    b = bytearray(p.read_bytes())
    b[-1] ^= 0xFF
    p.write_bytes(bytes(b))

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.vault.keys() == []
        with pytest.raises(AiosError):
            k2.vault.get("token")
    finally:
        await k2.shutdown()
        await k2.shutdown()
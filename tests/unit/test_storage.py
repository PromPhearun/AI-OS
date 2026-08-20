"""Unit tests: checkpoint completeness (kernel invariant #4).

A checkpoint captures context + memory + usage + budgets + spec; restore
must reproduce byte-identical observable state. Phase 2 adds the durable
on-disk layout (snapshot + manifest), sha256 integrity verification, and
the session resume set that powers ``--resume``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from aios_kernel import Kernel
from aios_kernel.errors import AiosError
from aios_kernel.modules.llm_core import MockLLM
from aios_kernel.modules.storage import Checkpoint

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_checkpoint_snapshots_full_state(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="ckpt-test"))
    # Mutate observable state
    kernel.context.append(pid, "user", "hello")
    kernel.context.append(pid, "assistant", "hi there")
    kernel.memory.write(pid, "rounds", 3)
    kernel.scheduler.account_llm(pid, tokens_in=4, tokens_out=4, cost=0.0)
    kernel.scheduler.account_tool(pid)

    cid = kernel.storage.checkpoint(pid, label="t1")
    ckpt: Checkpoint = kernel.storage.get(cid)
    assert ckpt.label == "t1"
    assert ckpt.pid == pid
    # context includes the pinned system message plus the appended turns
    assert [m.content for m in ckpt.context] == ["You are a test agent.", "hello", "hi there"]
    assert ckpt.memory["rounds"]["value"] == 3
    assert ckpt.usage.tokens_in == 4
    assert ckpt.usage.tool_calls == 1
    assert ckpt.spec["name"] == "ckpt-test"


@pytest.mark.asyncio
async def test_restore_is_byte_identical(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="restore-test"))
    kernel.context.append(pid, "user", "question", pinned=True)
    kernel.memory.write(pid, "answer", 42)
    kernel.scheduler.account_llm(pid, tokens_in=2, tokens_out=3, cost=0.0)

    cid = kernel.storage.checkpoint(pid)
    kernel.context.restore(pid, [])  # trash the live context
    kernel.memory.write(pid, "answer", -1)  # trash the live memory

    kernel.storage.restore(pid, cid)
    ctx = kernel.context.read(pid)
    assert ctx == [
        {"role": "system", "content": "You are a test agent.", "pinned": True},
        {"role": "user", "content": "question", "pinned": True},
    ]
    assert kernel.memory.read(pid, "answer") == 42
    acb = kernel.agent_manager.get(pid)
    assert acb.usage.tokens_in == 2 and acb.usage.tokens_out == 3


@pytest.mark.asyncio
async def test_checkpoint_ids_are_unique_and_sequential(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="seq-test"))
    ids = {kernel.storage.checkpoint(pid) for _ in range(5)}
    assert len(ids) == 5
    assert ids == {f"ck-{pid}-{i:06d}" for i in range(5)}


@pytest.mark.asyncio
async def test_checkpoint_of_missing_agent_raises(kernel: Kernel) -> None:
    with pytest.raises(Exception):
        kernel.storage.checkpoint(999_999)


# ---------------------------------------------------------------- durability
@pytest.mark.asyncio
async def test_checkpoint_files_on_disk(kernel: Kernel, tmp_path) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="disk-test"))
    kernel.context.append(pid, "user", "disk turn")
    cid = kernel.storage.checkpoint(pid, label="disk")
    d = tmp_path / "checkpoints" / cid
    assert (d / "snapshot.json").is_file()
    assert (d / "manifest.json").is_file()
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    snap = (d / "snapshot.json").read_bytes()
    assert manifest["hash"] == hashlib.sha256(snap).hexdigest()
    assert manifest["committed"] is True
    assert manifest["state"] == "ready"  # snapshot captured at spawn; agent not yet run
    assert manifest["pid"] == pid


@pytest.mark.asyncio
async def test_get_reloads_and_verifies_from_disk(kernel: Kernel, tmp_path) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="reload-test"))
    kernel.context.append(pid, "user", "hello again")
    cid = kernel.storage.checkpoint(pid)
    kernel.storage._checkpoints.pop(cid)  # evict cache -> must load from disk
    ckpt = kernel.storage.get(cid)
    assert ckpt.committed is True
    assert ckpt.hash  # integrity hash survived the round trip
    assert [m.content for m in ckpt.context][-1] == "hello again"


@pytest.mark.asyncio
async def test_tampered_checkpoint_rejected(kernel: Kernel, tmp_path) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="tamper-test"))
    cid = kernel.storage.checkpoint(pid)
    kernel.storage._checkpoints.pop(cid)
    snap = tmp_path / "checkpoints" / cid / "snapshot.json"
    with open(snap, "a", encoding="utf-8") as fh:
        fh.write("\n\"evil\"")
    with pytest.raises(AiosError, match="integrity"):
        kernel.storage.get(cid)


@pytest.mark.asyncio
async def test_free_for_removes_disk_checkpoints(kernel: Kernel, tmp_path) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="free-test"))
    cid = kernel.storage.checkpoint(pid)
    assert (tmp_path / "checkpoints" / cid).is_dir()
    kernel.storage.free_for(pid)
    assert not (tmp_path / "checkpoints" / cid).exists()


# --------------------------------------------------------------- resume set
@pytest.mark.asyncio
async def test_session_record_upsert_and_remove(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="sess-test"))
    kernel.context.append(pid, "user", "session turn")
    kernel.scheduler.account_llm(pid, tokens_in=2, tokens_out=3, cost=0.001)
    cid = kernel.storage.checkpoint(pid)
    assert kernel.storage.suspended_pids() == [pid]
    rec = [r for r in kernel.storage._load_session_records() if r["pid"] == pid][0]
    assert rec["checkpoint_id"] == cid
    assert rec["spec"]["name"] == "sess-test"
    assert rec["usage"]["tokens_in"] == 2
    assert rec["priority"] == 0
    kernel.storage.remove_session_record(pid)
    assert kernel.storage.suspended_pids() == []


@pytest.mark.asyncio
async def test_session_manifest_round_trip(tmp_path) -> None:
    """A fresh kernel restores pid/spec/priority/usage/budgets + state."""
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    pid = await k1.spawn_agent(_base_spec(name="roundtrip", priority=5))
    k1.context.append(pid, "user", "persist me", pinned=True)
    k1.memory.write(pid, "tally", 7)
    k1.scheduler.account_llm(pid, tokens_in=4, tokens_out=5, cost=0.002)
    cid = k1.storage.checkpoint(pid)

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.restore_session() == [pid]
        acb = k2.agent_manager.get(pid)
        assert acb.state.value == "suspended"
        assert acb.priority == 5
        assert acb.checkpoint_id == cid
        ctx = k2.context.read(pid)  # list of dicts (context.to_dict)
        assert [m["content"] for m in ctx] == ["You are a test agent.", "persist me"]
        assert ctx[-1]["pinned"] is True
        assert k2.memory.read(pid, "tally") == 7
        assert acb.usage.tokens_in == 4 and acb.usage.tokens_out == 5
        assert abs(acb.usage.cost_usd - 0.002) < 1e-9
        # kernel invariant #1: fresh spawns never collide with restored pids
        pid2 = await k2.spawn_agent(_base_spec(name="roundtrip"))
        assert pid2 > pid
    finally:
        await k2.shutdown()
    await k1.shutdown()
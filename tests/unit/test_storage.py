"""Unit tests: checkpoint completeness (kernel invariant #4).

A checkpoint captures context + memory + usage + budgets + spec; restore
must reproduce byte-identical observable state.
"""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
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
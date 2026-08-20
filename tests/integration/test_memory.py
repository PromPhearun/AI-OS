"""Integration tests: L3 memory syscalls through the full kernel ABI."""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_sdk.errors import AiosNoEntError

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_search_memory_syscall_roundtrip(kernel: Kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(
        ns, "proc", "promote critical facts to L3 before eviction",
        kind="procedural", tags=["memory", "policy"],
    )
    hits = await sc.search_memory("promote facts before eviction", top_k=3)
    assert hits and hits[0]["key"] == "proc"
    assert hits[0]["kind"] == "procedural"
    assert set(hits[0]["tags"]) == {"memory", "policy"}


@pytest.mark.asyncio
async def test_read_memory_returns_l3_value_and_missing_raises(kernel: Kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(ns, "fact", {"answer": 42}, kind="semantic")
    assert await sc.read_memory(ns, "fact") == {"answer": 42}
    with pytest.raises(AiosNoEntError):
        await sc.read_memory(ns, "missing")


@pytest.mark.asyncio
async def test_forget_memory_clears_whole_namespace(kernel: Kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(ns, "a", "alpha memory", kind="semantic")
    await sc.write_memory(ns, "b", "beta memory", kind="semantic")
    deleted = await sc.forget_memory(ns)
    assert deleted["deleted"] == 2
    assert await sc.search_memory("alpha memory") == []


@pytest.mark.asyncio
async def test_l3_is_independent_of_checkpoints(kernel: Kernel, session) -> None:
    """L3 is already durable; a checkpoint records refs, not copies
    (docs/04-memory.md §6) — the entry must still be searchable."""
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(ns, "lesson", "durable by construction", kind="episodic")
    cid = await sc.checkpoint(label="before-hiccup")
    assert cid
    hits = await sc.search_memory("durable lesson")
    assert hits and hits[0]["key"] == "lesson"
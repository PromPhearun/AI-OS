"""Unit tests: L3 long-term memory store (RAG) and namespace permissions."""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.llm_core import MockLLM
from aios_sdk.errors import AiosInvalError, AiosPermissionError
from aios_sdk.session import AgentSession

from ..conftest import _base_spec


async def _session(kernel: Kernel, spec: dict):
    pid = await kernel.spawn_agent(spec)
    return AgentSession(kernel, pid)


@pytest.mark.asyncio
async def test_l3_write_search_roundtrip_ranks_by_similarity(kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(ns, "q3", "Q3 revenue grew 18% to $2.4M", kind="episodic", tags=["finance"])
    await sc.write_memory(ns, "todo", "buy milk and eggs", kind="semantic")

    hits = await sc.search_memory("Q3 revenue growth", top_k=5)
    assert hits and hits[0]["key"] == "q3"
    assert hits[0]["kind"] == "episodic"
    assert "finance" in hits[0]["tags"]
    assert hits[0]["score"] > 0.0


@pytest.mark.asyncio
async def test_read_memory_falls_back_to_l3(kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(ns, "fact", "enterprise pipeline", kind="semantic")
    assert await sc.read_memory(ns, "fact") == "enterprise pipeline"


@pytest.mark.asyncio
async def test_search_memory_isolates_own_namespace(kernel, session) -> None:
    sc1 = await session(_base_spec(name="mem-a"))
    sc2 = await session(_base_spec(name="mem-b"))
    ns1 = f"agent:{sc1.pid}"
    await sc1.write_memory(ns1, "secret", "classified project x", kind="semantic")
    hits = await sc2.search_memory("classified project x")
    assert hits == []


@pytest.mark.asyncio
async def test_pool_access_denied_by_default(kernel, session) -> None:
    sc = await session(_base_spec(name="mem-deny"))
    with pytest.raises(AiosPermissionError):
        await sc.write_memory("company_knowledge", "k", 1, kind="semantic")


@pytest.mark.asyncio
async def test_read_only_pool_denies_write(kernel, session) -> None:
    spec = _base_spec(
        name="mem-ro", memory={"pools": [{"pool": "company_knowledge", "access": "read"}]}
    )
    sc = await session(spec)
    with pytest.raises(AiosPermissionError):
        await sc.write_memory("company_knowledge", "k", 1, kind="semantic")


@pytest.mark.asyncio
async def test_granted_pool_read_write_and_read_only(kernel, session) -> None:
    owner_spec = _base_spec(
        name="mem-owner", memory={"pools": [{"pool": "company_knowledge", "access": "read-write"}]}
    )
    reader_spec = _base_spec(
        name="mem-reader", memory={"pools": [{"pool": "company_knowledge", "access": "read"}]}
    )
    owner = await session(owner_spec)
    reader = await session(reader_spec)

    await owner.write_memory("company_knowledge", "policy", "refund policy is 30 days", kind="semantic")
    hits = await reader.search_memory("refund", namespace="company_knowledge")
    assert hits and hits[0]["key"] == "policy"

    # readers cannot write
    with pytest.raises(AiosPermissionError):
        await reader.write_memory("company_knowledge", "x", 1, kind="semantic")


@pytest.mark.asyncio
async def test_forget_memory_removes_entry(kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    await sc.write_memory(ns, "tmp", "ephemeral note", kind="semantic")
    assert await sc.search_memory("ephemeral note")
    deleted = await sc.forget_memory(ns, "tmp")
    assert deleted["deleted"] == 1
    assert await sc.search_memory("ephemeral note") == []


@pytest.mark.asyncio
async def test_l3_persists_across_kernel_restart(tmp_path) -> None:
    """L3 is durable on disk; a fresh kernel reloads entries from the WAL."""
    spec = _base_spec(name="mem-persist")
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM())
    pid1 = await k1.spawn_agent(spec)
    sc1 = AgentSession(k1, pid1)
    await sc1.write_memory(
        f"agent:{pid1}", "lesson", "handoff requires a validated spec", kind="episodic", tags=["ipc"]
    )
    await k1.shutdown()

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM())
    try:
        pid2 = await k2.spawn_agent(spec)
        sc2 = AgentSession(k2, pid2)
        hits = await sc2.search_memory("handoff validated spec")
        assert hits and hits[0]["key"] == "lesson"
    finally:
        await k2.shutdown()


@pytest.mark.asyncio
async def test_l3_rejects_non_json_value(kernel, session) -> None:
    sc = await session()
    ns = f"agent:{sc.pid}"
    with pytest.raises(AiosInvalError):
        await sc.write_memory(ns, "bad", object(), kind="semantic")
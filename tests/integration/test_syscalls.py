"""Integration tests: full kernel lifecycle through the SDK AgentSession.

These exercise the syscall ABI end-to-end (dispatch -> module -> audit) using
an in-process Kernel — the Phase 1 acceptance-test surface.
"""

from __future__ import annotations

import asyncio

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_get_pid_returns_identity(kernel: Kernel, session) -> None:
    sc = await session()
    info = await sc.get_pid()
    assert info["pid"] == sc.pid
    assert info["group_id"] == "test"


@pytest.mark.asyncio
async def test_spawn_child_from_agent(kernel: Kernel, session) -> None:
    sc = await session(_base_spec(name="parent"))
    child_pid = await sc.spawn(_base_spec(name="child"))
    child = kernel.agent_manager.get(child_pid)
    assert child.parent_pid == sc.pid
    assert child.state.value == "ready"
    # child is an independent process with its own workspace
    assert child.workspace != kernel.agent_manager.get(sc.pid).workspace


@pytest.mark.asyncio
async def test_context_roundtrip_and_generate_appends(kernel: Kernel, session) -> None:
    sc = await session()
    await sc.append_context("user", "hello world", pinned=True)
    msgs = await sc.read_context()
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "hello world"
    assert msgs[-1]["pinned"] is True

    reply = await sc.generate("first turn")
    assert reply["text"].startswith("[mock:")
    ctx = await sc.read_context()
    assert ctx[-1]["role"] == "assistant"
    assert ctx[-1]["content"] == reply["text"]


@pytest.mark.asyncio
async def test_memory_write_read_roundtrip(kernel: Kernel, session) -> None:
    sc = await session()
    await sc.write_memory("agent:1", "rounds", 3)
    assert await sc.read_memory("agent:1", "rounds") == 3


@pytest.mark.asyncio
async def test_get_env_returns_configured_value(kernel, session) -> None:
    kernel.vault.set("TEST_TOKEN", "shh")
    sc = await session()
    assert await sc.get_env("TEST_TOKEN") == "shh"


@pytest.mark.asyncio
async def test_log_records_to_agent_logs(kernel: Kernel, session) -> None:
    sc = await session()
    await sc.log("info", "checkpoint reached")
    assert kernel.agent_logs[sc.pid][-1]["message"] == "checkpoint reached"


@pytest.mark.asyncio
async def test_suspend_then_resume_restores_state(kernel: Kernel, session) -> None:
    """Phase 1 acceptance: suspend at a checkpoint, resume, context identical."""
    sc = await session()
    # A runner factory is required for resume; register a no-op async runner.
    async def _noop_runner(_pid):
        await asyncio.sleep(0)

    kernel.agent_manager._runner_factories[sc.pid] = _noop_runner

    await sc.append_context("user", "stay", pinned=True)
    await sc.write_memory("agent:1", "rounds", 2)

    cid = await sc.suspend(reason="test")
    acb = kernel.agent_manager.get(sc.pid)
    assert acb.state.value == "suspended"
    assert acb.checkpoint_id == cid["checkpoint_id"]

    await kernel.agent_manager.resume(sc.pid)
    acb = kernel.agent_manager.get(sc.pid)
    assert acb.state.value == "ready"
    assert acb.checkpoint_id is None
    # context survived the suspend/resume round trip
    msgs = await sc.read_context()
    assert msgs[-1]["content"] == "stay"
    assert await sc.read_memory("agent:1", "rounds") == 2


@pytest.mark.asyncio
async def test_get_usage_reflects_accounting(kernel: Kernel, session) -> None:
    sc = await session()
    before = await sc.get_usage()
    await sc.generate("tick")
    after = await sc.get_usage()
    assert after["tokens"] > before["tokens"]
    assert after["tool_calls"] == before["tool_calls"]  # no tools called yet


@pytest.mark.asyncio
async def test_exit_terminates_and_reaps(kernel: Kernel, session) -> None:
    sc = await session()
    pid = sc.pid
    await sc.exit(status="ok", message="finished")
    acb = kernel.agent_manager.get(pid)
    assert acb.state.value == "terminated"
    assert acb.exit_status == "ok"
    # reaped tombstone is still queryable
    rec = kernel.agent_manager.record(pid)
    assert rec["state"] == "terminated"


@pytest.mark.asyncio
async def test_kill_releases_process_table_and_resources(kernel: Kernel, session) -> None:
    """Phase 1 acceptance: kill removes from the process table + frees resources."""
    sc = await session()
    pid = sc.pid
    ws_root = kernel.workspaces.path_for(pid)
    assert ws_root.exists()
    kernel.agent_manager.kill(pid, reason="test")
    assert pid not in kernel.agent_manager._table
    assert pid in kernel.agent_manager._reaped
    assert not ws_root.exists()  # workspace cleaned up
    rec = kernel.agent_manager.record(pid)
    assert rec["exit_status"] == "killed"


@pytest.mark.asyncio
async def test_concurrent_llm_requests_serialize(kernel: Kernel, session) -> None:
    """Phase 1 acceptance: concurrent LLM requests serialize without data mixing."""
    sc1 = await session(_base_spec(name="a"))
    sc2 = await session(_base_spec(name="b"))
    # fire concurrently; each reply must correspond to its own input (echo mode)
    import asyncio

    r1, r2 = await asyncio.gather(sc1.generate("alpha"), sc2.generate("beta"))
    assert "[mock:alpha]" in r1["text"]
    assert "[mock:beta]" in r2["text"]
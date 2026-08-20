"""E2E tests: demo parity + suspend/resume + kill (roadmap acceptance)."""

from __future__ import annotations

import asyncio

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_demo_agents_write_notes_and_exit_ok(kernel: Kernel) -> None:
    """The exact example agents from examples/agents.py run to completion."""
    from aios_sdk import run_agents
    from examples.agents import demo_specs

    summaries = await run_agents(kernel, demo_specs())
    by_name = {s.name: s for s in summaries}
    assert by_name["researcher"].exit_status == "ok"
    assert by_name["writer"].exit_status == "ok"
    assert by_name["researcher"].tool_calls == 3  # 3 research notes
    assert by_name["writer"].tool_calls == 2  # 2 report sections


@pytest.mark.asyncio
async def test_suspend_resume_keeps_context_byte_identical(kernel: Kernel) -> None:
    """Acceptance: suspend at a checkpoint, resume, context byte-identical."""
    from aios_sdk import AgentRunner
    from aios_sdk.agent import AGENT_REGISTRY

    events = []

    async def worker(sc) -> bool:
        events.append("started")
        from aios_sdk.errors import AiosNoEntError

        try:
            rounds = await sc.read_memory("agent:1", "rounds")
        except AiosNoEntError:
            rounds = 0
        if rounds == 0:
            # first pass: build state, then suspend at a checkpoint
            await sc.append_context("user", "phase-1")
            await sc.write_memory("agent:1", "rounds", 1)
            await sc.suspend(reason="e2e")
            events.append("suspended")
            return False
        # resumed pass: the restored context must still contain phase-1
        ctx = await sc.read_context()
        assert any(m["content"] == "phase-1" for m in ctx), "context lost after resume"
        events.append("resumed-and-verified")
        return True

    AGENT_REGISTRY["e2e-worker"] = {"turn": worker, "spec": None}
    try:
        spec = _base_spec(name="e2e-worker")
        pid = await kernel.spawn_agent(
            spec, runner_factory=lambda pid: AgentRunner(kernel, pid, worker).run()
        )
        await kernel.agent_manager.wait_task(pid)

        acb = kernel.agent_manager.get(pid)
        assert acb.state.value == "suspended"
        assert acb.checkpoint_id is not None

        # operator resumes from the checkpoint
        await kernel.agent_manager.resume(pid)
        await kernel.agent_manager.wait_task(pid)

        rec = kernel.agent_manager.record(pid)  # tombstone persists after reap
        assert rec["state"] == "terminated"
        assert rec["exit_status"] == "ok"
        assert events == ["started", "suspended", "started", "resumed-and-verified"]
    finally:
        AGENT_REGISTRY.pop("e2e-worker", None)


@pytest.mark.asyncio
async def test_kill_releases_resources(kernel: Kernel) -> None:
    """Acceptance: kill removes from process table and releases resources."""
    from aios_sdk import AgentRunner
    from aios_sdk.agent import AGENT_REGISTRY

    async def sleeper(sc) -> bool:
        while True:
            await sc.sleep(50_000)  # never finishes on its own
            return False

    AGENT_REGISTRY["e2e-sleeper"] = {"turn": sleeper, "spec": None}
    try:
        spec = _base_spec(name="e2e-sleeper")
        pid = await kernel.spawn_agent(
            spec, runner_factory=lambda pid: AgentRunner(kernel, pid, sleeper).run()
        )
        await asyncio.sleep(0.1)  # let it start running

        assert pid in kernel.agent_manager._table
        ws = kernel.workspaces.path_for(pid)
        assert ws.exists()

        kernel.agent_manager.kill(pid, reason="e2e-kill")
        assert pid not in kernel.agent_manager._table
        assert pid in kernel.agent_manager._reaped
        assert not ws.exists()
        assert kernel.agent_manager.record(pid)["exit_status"] == "killed"
    finally:
        AGENT_REGISTRY.pop("e2e-sleeper", None)
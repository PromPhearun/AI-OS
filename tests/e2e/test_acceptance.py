"""E2E tests: Phase 1 acceptance scenarios (docs/11-roadmap.md §3).

These run real agents (SDK runners) against a live Kernel and assert the
observable end-to-end outcomes.
"""

from __future__ import annotations

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_two_agents_progress_concurrently(kernel: Kernel) -> None:
    """Acceptance: spawn 2 agents; both progress; both exit ok."""
    from aios_sdk import run_agents
    from aios_sdk.agent import AGENT_REGISTRY

    names = []

    async def turn_a(sc) -> bool:
        names.append("a")
        await sc.append_context("user", "a")
        return True

    async def turn_b(sc) -> bool:
        names.append("b")
        await sc.append_context("user", "b")
        return True

    AGENT_REGISTRY["e2e-a"] = {"turn": turn_a, "spec": None}
    AGENT_REGISTRY["e2e-b"] = {"turn": turn_b, "spec": None}
    try:
        specs = [_base_spec(name="e2e-a"), _base_spec(name="e2e-b")]
        summaries = await run_agents(kernel, specs)
        assert [s.name for s in summaries] == ["e2e-a", "e2e-b"]
        assert all(s.exit_status == "ok" for s in summaries)
        assert "a" in names and "b" in names  # both ran
        assert len(names) == 2
    finally:
        AGENT_REGISTRY.pop("e2e-a", None)
        AGENT_REGISTRY.pop("e2e-b", None)


@pytest.mark.asyncio
async def test_runaway_agent_suspended_at_budget(kernel: Kernel) -> None:
    """Acceptance: a runaway agent is hard-stopped at its budget."""
    from aios_sdk import AgentRunner
    from aios_sdk.agent import AGENT_REGISTRY

    calls = {"n": 0}

    async def runaway(sc) -> bool:
        calls["n"] += 1
        await sc.generate(f"spam {calls['n']}")
        return False  # never done; budgets must stop it

    AGENT_REGISTRY["e2e-runaway"] = {"turn": runaway, "spec": None}
    try:
        spec = _base_spec(
            name="e2e-runaway",
            budgets={"max_turns": 10, "tokens_per_min": 50},  # 33 tokens/turn echo
        )
        pid = await kernel.spawn_agent(
            spec, runner_factory=lambda pid: AgentRunner(kernel, pid, runaway).run()
        )
        await kernel.agent_manager.wait_task(pid)
        acb = kernel.agent_manager.get(pid)
        assert acb.state.value == "suspended"
        assert acb.exit_status == "budget"
        assert acb.checkpoint_id is not None
        assert calls["n"] < 10  # suspended long before max_turns, no cooperation needed
    finally:
        AGENT_REGISTRY.pop("e2e-runaway", None)


@pytest.mark.asyncio
async def test_crash_then_resume_brings_agents_back(tmp_path) -> None:
    """Acceptance (Phase 2): crash the kernel; --resume; every agent is back
    at its last committed checkpoint and keeps running under its budgets."""
    from aios_kernel.modules.llm_core import MockLLM
    from aios_sdk import AgentRunner
    from aios_sdk.agent import AGENT_REGISTRY
    from aios_sdk.control import ControlPlane

    calls = {"n": 0}

    async def runaway(sc) -> bool:
        calls["n"] += 1
        await sc.generate(f"spam {calls['n']}")
        return False  # never done; budgets must stop it

    AGENT_REGISTRY["e2e-restart"] = {"turn": runaway, "spec": None}
    try:
        spec = _base_spec(
            name="e2e-restart",
            budgets={"max_turns": 10, "tokens_per_min": 50},
        )
        k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
        pid = await k1.spawn_agent(
            spec, runner_factory=lambda pid: AgentRunner(k1, pid, runaway).run()
        )
        await k1.agent_manager.wait_task(pid)
        assert k1.agent_manager.get(pid).state.value == "suspended"
        assert k1.storage.suspended_pids() == [pid]
        # crash: no k1.shutdown() — the durable session file is all that survives
        turns_before = calls["n"]

        k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
        try:
            summaries = await ControlPlane(k2).resume_session()
            assert [s.pid for s in summaries] == [pid]
            assert summaries[0].name == "e2e-restart"
            # the runner re-attached; the agent ran again until its budget
            # hard-stopped it with a fresh checkpoint
            acb = k2.agent_manager.get(pid)
            assert acb.state.value == "suspended"
            assert acb.exit_status == "budget"
            assert calls["n"] > turns_before
            assert k2.storage.suspended_pids() == [pid]
        finally:
            await k2.shutdown()
    finally:
        AGENT_REGISTRY.pop("e2e-restart", None)
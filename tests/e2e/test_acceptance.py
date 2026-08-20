"""E2E tests: Phase 1 acceptance scenarios (docs/11-roadmap.md §3).

These run real agents (SDK runners) against a live Kernel and assert the
observable end-to-end outcomes.
"""

from __future__ import annotations

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec

_IPC = {
    "can_send_to": ["*"],
    "can_subscribe": ["*"],
    "can_publish": ["*"],
    "mailbox": {"max_queue_depth": 100, "ttl_s": 3600},
}


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
async def test_handoff_and_join_end_to_end(kernel: Kernel) -> None:
    """Acceptance (Phase 2): A hands a job to B over IPC, B runs it and
    replies, then A joins B and reads the result."""
    from aios_sdk import AgentRunner
    from aios_sdk.agent import AGENT_REGISTRY

    out: dict = {}

    async def turn_a(sc) -> bool:
        out["pid_a"] = sc.pid
        await sc.send_msg(
            out["pid_b"], {"spec": _base_spec(name="e2e-handoff-b", ipc=_IPC)}, type="handoff"
        )
        joined = await sc.join([out["pid_b"]], timeout_ms=5000)
        out["join"] = joined
        reply = await sc.recv_msg(100)
        out["reply"] = reply["msg"]
        return True

    async def turn_b(sc) -> bool:
        res = await sc.recv_msg(5000)
        out["handoff"] = res["msg"]
        spec = res["msg"]["body"]["spec"]
        spawned = await sc.spawn(spec)  # the handed-off spec is spawnable
        await sc.send_msg(out["pid_a"], {"handled": spec["name"], "spawned": spawned}, type="direct")
        return True

    AGENT_REGISTRY["e2e-handoff-a"] = {"turn": turn_a, "spec": None}
    AGENT_REGISTRY["e2e-handoff-b"] = {"turn": turn_b, "spec": None}
    try:
        pid_b = await kernel.spawn_agent(
            _base_spec(name="e2e-handoff-b", ipc=_IPC),
            runner_factory=lambda pid: AgentRunner(kernel, pid, turn_b).run(),
        )
        pid_a = await kernel.spawn_agent(
            _base_spec(name="e2e-handoff-a", ipc=_IPC),
            runner_factory=lambda pid: AgentRunner(kernel, pid, turn_a).run(),
        )
        out["pid_b"] = pid_b
        await kernel.agent_manager.wait_task(pid_a)
        await kernel.agent_manager.wait_task(pid_b)

        assert out["handoff"]["type"] == "handoff"
        assert out["handoff"]["body"]["spec"]["name"] == "e2e-handoff-b"
        assert out["join"]["timed_out"] is False
        result = out["join"]["results"][0]
        assert result["pid"] == pid_b
        assert result["exit_status"] == "ok"
        assert out["reply"]["body"]["handled"] == "e2e-handoff-b"
        spawned = out["reply"]["body"]["spawned"]
        # the handed-off spec was actually spawned inside B's turn
        assert kernel.agent_manager.peek(spawned) is not None
    finally:
        AGENT_REGISTRY.pop("e2e-handoff-a", None)
        AGENT_REGISTRY.pop("e2e-handoff-b", None)


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
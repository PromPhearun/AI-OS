"""Integration tests: durable checkpoints + the --resume boot path (Phase 2).

These run two sequential kernels against one ``data_root``: the first commits
a checkpoint and is then left without a clean shutdown (crash); the second
must rebuild every suspended agent from the session manifest at its last
committed, hash-verified checkpoint.
"""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.llm_core import MockLLM

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_second_kernel_restores_pids_and_observable_state(tmp_path) -> None:
    """restore_session() preserves pid, spec, priority, usage, budgets, and
    byte-identical context/memory from the last committed checkpoint."""
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    pid = await k1.spawn_agent(_base_spec(name="restart-agent", priority=3))
    k1.context.append(pid, "user", "before crash")
    k1.memory.write(pid, "rounds", 5)
    k1.scheduler.account_llm(pid, tokens_in=10, tokens_out=4, cost=0.01)
    cid = k1.storage.checkpoint(pid)
    # simulated crash: no clean shutdown, no finalize

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.restore_session() == [pid]
        acb = k2.agent_manager.get(pid)
        assert acb.state.value == "suspended"
        assert acb.checkpoint_id == cid
        assert acb.priority == 3
        assert [m["content"] for m in k2.context.read(pid)] == [
            "You are a test agent.",
            "before crash",
        ]
        assert k2.memory.read(pid, "rounds") == 5
        assert acb.usage.tokens_in == 10 and acb.usage.tokens_out == 4
        assert abs(acb.usage.cost_usd - 0.01) < 1e-9
        # budgets survive the restart: the restored agent is still capped
        assert acb.budgets.max_turns == 10
        assert acb.budgets.max_tool_calls == 10
    finally:
        await k2.shutdown()


@pytest.mark.asyncio
async def test_second_kernel_resumes_agents_to_completion(tmp_path) -> None:
    """Full --resume loop via the SDK: restore, re-attach a runner, run."""
    from aios_sdk.agent import AGENT_REGISTRY
    from aios_sdk.control import ControlPlane

    calls = {"n": 0}

    async def worker(sc) -> bool:
        calls["n"] += 1
        await sc.append_context("user", "post-restart")
        return True

    AGENT_REGISTRY["resume-worker"] = {"turn": worker, "spec": None}
    try:
        k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
        pid = await k1.spawn_agent(_base_spec(name="resume-worker"))
        k1.context.append(pid, "user", "pre-restart")
        k1.storage.checkpoint(pid)
        # simulated crash: no clean shutdown

        k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
        try:
            summaries = await ControlPlane(k2).resume_session()
            assert [s.pid for s in summaries] == [pid]
            assert summaries[0].name == "resume-worker"
            assert summaries[0].exit_status == "ok"
            # the restored agent's runner re-attached and ran one post-restart turn
            assert calls["n"] == 1
        finally:
            await k2.shutdown()
    finally:
        AGENT_REGISTRY.pop("resume-worker", None)
"""Unit tests: ACB/PID uniqueness (kernel invariant #1)."""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.agent_manager import AgentManager

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_pids_monotonic_and_unique(kernel: Kernel) -> None:
    pids = []
    for i in range(20):
        spec = _base_spec(name=f"agent-{i}")
        pid = await kernel.spawn_agent(spec)
        pids.append(pid)
    assert pids == sorted(pids)
    assert len(pids) == len(set(pids))
    assert pids[0] == 1  # first pid is 1


@pytest.mark.asyncio
async def test_pids_never_reused_after_reap(kernel: Kernel) -> None:
    """Reaped agents must not free their pid for reuse while the kernel lives."""
    am: AgentManager = kernel.agent_manager
    pid = await kernel.spawn_agent(_base_spec(name="ephemeral"))
    am.request_terminate(pid)
    am._drop(pid)  # force a reaped tombstone
    assert pid in am._reaped

    for _ in range(5):
        new_pid = await kernel.spawn_agent(_base_spec(name="next"))
        assert new_pid > pid


@pytest.mark.asyncio
async def test_acb_holds_spec_budgets_and_state(kernel: Kernel) -> None:
    spec = _base_spec(
        name="bounded",
        budgets={"max_turns": 3, "max_tool_calls": 4, "tokens_per_min": 1000},
    )
    pid = await kernel.spawn_agent(spec)
    acb = kernel.agent_manager.get(pid)
    assert acb.spec["name"] == "bounded"
    assert acb.budgets.max_turns == 3
    assert acb.budgets.max_tool_calls == 4
    assert acb.budgets.tokens_per_min == 1000
    assert acb.state.value == "ready"
    assert acb.workspace  # sandbox dir allocated at spawn
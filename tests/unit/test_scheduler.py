"""Unit tests: scheduler accounting, budgets, priority & aging.

Covers kernel invariant #3 — the scheduler *always* suspends an agent on
budget exhaustion (never relies on the agent's cooperation).
"""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_kernel.acb import AgentState
from aios_kernel.modules.scheduler import Scheduler

from ..conftest import _base_spec


def _mark_running(kernel: Kernel, pid: int) -> None:
    """Simulate the scheduler grant transition (wait_for_grant would do this)."""
    acb = kernel.agent_manager.get(pid)
    acb.state = AgentState.RUNNING
    kernel.scheduler._running = pid


@pytest.mark.asyncio
async def test_end_turn_terminates_at_max_turns(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="turn-bounded", budgets={"max_turns": 2}))
    acb = kernel.agent_manager.get(pid)
    kernel.scheduler.add_ready(pid)

    # Turn 1 -> continue
    _mark_running(kernel, pid)
    assert await kernel.scheduler.end_turn(pid) == "continue"
    # Turn 2 -> hits max_turns, hard-stop at TERMINATED
    _mark_running(kernel, pid)
    assert await kernel.scheduler.end_turn(pid) == "exit"
    assert acb.state is AgentState.TERMINATED
    assert acb.exit_status == "limit"


@pytest.mark.asyncio
async def test_budget_hard_stop_suspends(kernel: Kernel) -> None:
    """Token budget exceeded -> SUSPENDED + checkpoint recorded, not cooperation."""
    pid = await kernel.spawn_agent(
        _base_spec(name="token-bounded", budgets={"tokens_per_min": 10})
    )
    acb = kernel.agent_manager.get(pid)
    kernel.scheduler.add_ready(pid)

    kernel.scheduler.account_llm(pid, tokens_in=6, tokens_out=6, cost=0.0)
    _mark_running(kernel, pid)
    decision = await kernel.scheduler.end_turn(pid)
    assert decision == "suspend"
    assert acb.state is AgentState.SUSPENDED
    assert acb.exit_status == "budget"
    assert acb.checkpoint_id is not None  # checkpointed before suspend


@pytest.mark.asyncio
async def test_tool_call_budget_suspends(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="tool-bounded", budgets={"max_tool_calls": 1}))
    acb = kernel.agent_manager.get(pid)
    kernel.scheduler.add_ready(pid)

    kernel.scheduler.account_tool(pid)
    kernel.scheduler.account_tool(pid)  # 2 calls exceed max_tool_calls=1
    _mark_running(kernel, pid)
    decision = await kernel.scheduler.end_turn(pid)
    assert decision == "suspend"
    assert acb.state is AgentState.SUSPENDED


@pytest.mark.asyncio
async def test_accounting_accumulates(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="usage-tracker"))
    kernel.scheduler.account_llm(pid, tokens_in=10, tokens_out=5, cost=0.01)
    kernel.scheduler.account_tool(pid)
    kernel.scheduler.account_tool(pid)
    acb = kernel.agent_manager.get(pid)
    u = acb.usage
    assert u.tokens_in == 10
    assert u.tokens_out == 5
    assert u.tool_calls == 2
    assert u.total_tokens() == 15
    assert u.cost_usd == 0.01


@pytest.mark.asyncio
async def test_priority_ordering(kernel: Kernel) -> None:
    low = await kernel.spawn_agent(_base_spec(name="low", priority=0))
    high = await kernel.spawn_agent(_base_spec(name="high", priority=5))
    kernel.scheduler.add_ready(low)
    kernel.scheduler.add_ready(high)
    assert kernel.scheduler._pick() == high
    kernel.scheduler.remove(high)
    assert kernel.scheduler._pick() == low


@pytest.mark.asyncio
async def test_aging_boosts_waiting_agent(kernel: Kernel) -> None:
    """An agent waiting past AGING_RATE turns outranks a fresher high-priority peer."""
    older = await kernel.spawn_agent(_base_spec(name="old", priority=0))
    newer = await kernel.spawn_agent(_base_spec(name="new", priority=1))
    kernel.scheduler.add_ready(older)
    kernel.scheduler.add_ready(newer)

    older_acb = kernel.agent_manager.get(older)
    older_acb.wait_turns = kernel.scheduler.AGING_RATE + 1  # effective 1 >= newer's 1
    assert kernel.scheduler._effective(older_acb) >= kernel.scheduler._effective(
        kernel.agent_manager.get(newer)
    )


def test_effective_priority_formula() -> None:
    class _ACB:
        priority = 3
        wait_turns = 8

    sched = Scheduler.__new__(Scheduler)  # bare instance; only _effective is used
    assert sched._effective(_ACB()) == 5  # 3 + 8//4
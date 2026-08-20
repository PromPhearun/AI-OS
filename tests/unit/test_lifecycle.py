"""Unit tests: lifecycle state machine (kernel invariant #2)."""

from __future__ import annotations

import pytest

from aios_kernel.acb import AgentControlBlock, AgentState
from aios_kernel.errors import AiosError, E_STATE
from aios_kernel.lifecycle import allowed, transition

ALL_STATES = list(AgentState)


def _acb() -> AgentControlBlock:
    return AgentControlBlock(pid=1, spec={"name": "t"})


def test_all_states_spawned_ready() -> None:
    """SPAWNED is the initial state; READY follows spawn."""
    acb = _acb()
    assert acb.state is AgentState.SPAWNED
    transition(acb, AgentState.READY, "spawn")
    assert acb.state is AgentState.READY


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentState.SPAWNED, AgentState.RUNNING),
        (AgentState.READY, AgentState.READY),
        (AgentState.READY, AgentState.BLOCKED),
        (AgentState.RUNNING, AgentState.RUNNING),
        (AgentState.RUNNING, AgentState.SPAWNED),
        (AgentState.BLOCKED, AgentState.BLOCKED),
        (AgentState.BLOCKED, AgentState.RUNNING),
        (AgentState.SUSPENDED, AgentState.SUSPENDED),
        (AgentState.SUSPENDED, AgentState.RUNNING),
        (AgentState.TERMINATED, AgentState.READY),
        (AgentState.TERMINATED, AgentState.RUNNING),
        (AgentState.TERMINATED, AgentState.SUSPENDED),
    ],
)
def test_illegal_transitions_raise(current, target) -> None:
    acb = _acb()
    acb.state = current
    assert not allowed(current, target)
    with pytest.raises(AiosError) as exc:
        transition(acb, target, "test")
    assert exc.value.code == E_STATE
    assert acb.state is current  # state unchanged on failure


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentState.SPAWNED, AgentState.READY),
        (AgentState.SPAWNED, AgentState.TERMINATED),
        (AgentState.READY, AgentState.RUNNING),
        (AgentState.READY, AgentState.SUSPENDED),
        (AgentState.READY, AgentState.TERMINATED),
        (AgentState.RUNNING, AgentState.READY),
        (AgentState.RUNNING, AgentState.BLOCKED),
        (AgentState.RUNNING, AgentState.SUSPENDED),
        (AgentState.RUNNING, AgentState.TERMINATED),
        (AgentState.BLOCKED, AgentState.READY),
        (AgentState.BLOCKED, AgentState.SUSPENDED),
        (AgentState.BLOCKED, AgentState.TERMINATED),
        (AgentState.SUSPENDED, AgentState.READY),
        (AgentState.SUSPENDED, AgentState.TERMINATED),
    ],
)
def test_legal_transitions_apply(current, target) -> None:
    acb = _acb()
    acb.state = current
    transition(acb, target, "test")
    assert acb.state is target


def test_terminated_is_terminal() -> None:
    acb = _acb()
    transition(acb, AgentState.TERMINATED, "exit")
    for state in ALL_STATES:
        assert not allowed(AgentState.TERMINATED, state)


def test_transition_requires_reason_and_records_it() -> None:
    """The canonical happy path used by the scheduler."""
    acb = _acb()
    transition(acb, AgentState.READY, "spawn")
    transition(acb, AgentState.RUNNING, "grant")
    transition(acb, AgentState.SUSPENDED, "budget")
    assert acb.state is AgentState.SUSPENDED
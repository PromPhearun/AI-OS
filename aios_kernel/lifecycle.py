"""Agent lifecycle state machine.

Canonical states (docs/02-kernel.md §4):

    SPAWNED → READY → RUNNING → BLOCKED → SUSPENDED → TERMINATED

Illegal transitions raise ``AiosError(E_STATE)``. The scheduler and agent
manager are the only components allowed to call ``transition``.
"""

from __future__ import annotations

from .acb import AgentState
from .errors import AiosError, E_STATE

_ALLOWED: dict[AgentState, set[AgentState]] = {
    AgentState.SPAWNED: {AgentState.READY, AgentState.TERMINATED},
    AgentState.READY: {AgentState.RUNNING, AgentState.SUSPENDED, AgentState.TERMINATED},
    AgentState.RUNNING: {
        AgentState.READY,
        AgentState.BLOCKED,
        AgentState.SUSPENDED,
        AgentState.TERMINATED,
    },
    AgentState.BLOCKED: {AgentState.READY, AgentState.SUSPENDED, AgentState.TERMINATED},
    AgentState.SUSPENDED: {AgentState.READY, AgentState.TERMINATED},
    AgentState.TERMINATED: set(),
}


def allowed(current: AgentState, target: AgentState) -> bool:
    return target in _ALLOWED[current]


def transition(acb, target: AgentState, reason: str = "") -> None:
    """Validate and apply a lifecycle transition on an ACB."""
    if not allowed(acb.state, target):
        raise AiosError(
            E_STATE,
            f"illegal transition {acb.state.value} -> {target.value}"
            + (f" ({reason})" if reason else ""),
        )
    acb.state = target
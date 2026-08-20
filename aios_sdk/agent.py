"""Agent authoring: ``@agent`` decorator, the default turn runner, and the
host-side ``run_agents`` launcher.

Turn contract (Phase 1): an agent entry is ``async def turn(sc) -> bool``;
the kernel grants it one CPU slice per call and the SDK's runner drives
``wait_for_grant -> entry -> end_turn``. Returning ``True`` marks the agent
done; the runner exits it. Suspended/resumed agents restart their turn
function with byte-identical restored context/memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aios_kernel.acb import AgentState
from aios_kernel.errors import AiosError, E_INVAL

from .session import AgentSession
from .syscalls import reset_current_session, set_current_session

AGENT_REGISTRY: dict[str, callable] = {}


def agent(*, name: str | None = None, spec: dict | None = None):
    """Register a turn function as an agent definition.

    Usage::

        @agent(spec=SPEC_DICT)
        async def my_turn(sc) -> bool:
            ...
    """

    def deco(fn):
        resolved = name or fn.__name__
        if resolved in AGENT_REGISTRY:
            raise RuntimeError(f"duplicate agent definition: {resolved}")
        AGENT_REGISTRY[resolved] = {"turn": fn, "spec": spec}
        return fn

    return deco


class AgentRunner:
    """Default runner: one turn per grant; exits on done / suspend / kill."""

    def __init__(self, kernel, pid: int, turn_fn: callable):
        self.kernel = kernel
        self.pid = pid
        self.turn_fn = turn_fn

    async def run(self) -> None:
        while True:
            acb = self.kernel.agent_manager.get(self.pid)
            if acb.state in (AgentState.TERMINATED, AgentState.SUSPENDED):
                return
            await self.kernel.scheduler.wait_for_grant(self.pid)
            session = AgentSession(self.kernel, self.pid)
            token = set_current_session(session)
            try:
                done = await self.turn_fn(session)
            finally:
                reset_current_session(token)
            if done:
                acb = self.kernel.agent_manager.get(self.pid)
                if acb.state not in (AgentState.TERMINATED, AgentState.SUSPENDED):
                    acb.exit_status = "ok"
                    acb.exit_message = "turn returned done"
                    self.kernel.agent_manager.request_terminate(self.pid)
                return
            decision = await self.kernel.scheduler.end_turn(self.pid)
            if decision != "continue":
                return


@dataclass
class RunSummary:
    """Result of running a set of agents to completion."""

    pid: int
    name: str
    state: str
    exit_status: str | None
    exit_message: str | None
    turns: int
    tokens: int
    cost_usd: float
    tool_calls: int

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "state": self.state,
            "exit_status": self.exit_status,
            "exit_message": self.exit_message,
            "turns": self.turns,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "tool_calls": self.tool_calls,
        }


def summary_from_record(rec: dict) -> RunSummary:
    """Build a RunSummary from an ACB record / reaped tombstone."""
    return RunSummary(
        pid=rec["pid"],
        name=rec["name"],
        state=rec["state"],
        exit_status=rec["exit_status"],
        exit_message=rec["exit_message"],
        turns=rec["usage"]["turns"],
        tokens=rec["usage"]["total_tokens"],
        cost_usd=rec["usage"]["cost_usd"],
        tool_calls=rec["usage"]["tool_calls"],
    )


async def run_agents(kernel, specs: list[dict], *, timeout: float | None = None) -> list[RunSummary]:
    """Spawn every spec (entries resolved from AGENT_REGISTRY) and wait.

    Returns one summary per agent in spawn order.
    """
    pids: list[int] = []
    for spec in specs:
        entry = _entry_for(kernel, spec)
        factory = lambda pid, e=entry: AgentRunner(kernel, pid, e).run()
        pid = await kernel.spawn_agent(spec, runner_factory=factory)
        pids.append(pid)

    summaries = []
    for pid in pids:
        await kernel.agent_manager.wait_task(pid, timeout=timeout)
        rec = kernel.agent_manager.record(pid)
        summaries.append(summary_from_record(rec))
    return summaries


def _entry_for(kernel, spec: dict) -> callable:
    name = spec["name"]
    definition = AGENT_REGISTRY.get(name)
    if definition is None:
        raise AiosError(E_INVAL, f"no @agent registered for spec name '{name}'")
    return definition["turn"]
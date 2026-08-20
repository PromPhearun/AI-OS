"""Scheduler — cooperative, priority-ordered agent scheduling.

Phase 1 model (docs/03-scheduler.md):

  * one RUNNING slot — agents execute strictly one at a time;
  * READY agents are picked by (effective_priority desc, arrival seq asc);
  * preemption happens only at turn boundaries (the agent loop yields);
  * aging: waiting agents gain +1 effective priority every AGING_RATE waits;
  * budgets are checked at the end of every turn; exhaustion hard-stops the
    agent by checkpointing it and suspending it (E_BUDGET).
"""

from __future__ import annotations

import asyncio
import time

from ..acb import AgentState
from ..errors import AiosError, E_STATE
from ..lifecycle import transition
from ..syscalls.registry import register

GRANT_POLL_S = 0.01


class Scheduler:
    AGING_RATE = 4  # +1 effective priority every N waits

    def __init__(self, kernel):
        self.kernel = kernel
        self._ready: list[int] = []  # pids, FIFO within priority
        self._seq: dict[int, int] = {}
        self._next_seq = 0
        self._running: int | None = None

    # ------------------------------------------------------------- queue mgmt
    def add_ready(self, pid: int) -> None:
        if pid not in self._ready:
            self._ready.append(pid)
            self._seq[pid] = self._next_seq
            self._next_seq += 1

    def remove(self, pid: int) -> None:
        if pid in self._ready:
            self._ready.remove(pid)
            self._seq.pop(pid, None)
        if self._running == pid:
            self._running = None

    def is_running(self, pid: int) -> bool:
        return self._running == pid

    def _effective(self, acb) -> int:
        return acb.priority + acb.wait_turns // self.AGING_RATE

    def _pick(self) -> int | None:
        if not self._ready:
            return None
        return max(
            self._ready,
            key=lambda p: (
                self._effective(self.kernel.agent_manager.get(p)),
                -self._seq[p],
            ),
        )

    # ------------------------------------------------------------ grant proto
    async def wait_for_grant(self, pid: int) -> None:
        """Agent loop: block until this agent is granted the CPU (RUNNING)."""
        acb = self.kernel.agent_manager.get(pid)
        while True:
            if acb.state is not AgentState.READY or pid not in self._ready:
                return  # suspended / terminated / killed while waiting
            if self._running is None and self._pick() == pid:
                self._ready.remove(pid)
                self._seq.pop(pid, None)
                self._running = pid
                acb.wait_turns = 0
                acb.run_since = time.monotonic()
                transition(acb, AgentState.RUNNING, "grant")
                return
            acb.wait_turns += 1
            await asyncio.sleep(GRANT_POLL_S)

    # -------------------------------------------------------------- end of turn
    async def end_turn(self, pid: int) -> str:
        """Turn boundary: accounting, budget checks, next-state decision.

        Returns "continue" (requeued), "suspend" (budget/limit hard-stop),
        or "exit" (the loop must stop; state already decided elsewhere).
        """
        acb = self.kernel.agent_manager.get(pid)
        if acb.state in (AgentState.SUSPENDED, AgentState.TERMINATED):
            return "exit"

        if self._running == pid:
            self._running = None
        acb.usage.turns += 1
        if acb.run_since is not None:
            acb.usage.run_time_s += time.monotonic() - acb.run_since
            acb.run_since = None

        if acb.usage.turns >= acb.budgets.max_turns:
            acb.exit_status = "limit"
            acb.exit_message = f"max_turns ({acb.budgets.max_turns}) reached"
            transition(acb, AgentState.TERMINATED, "max_turns")
            return "exit"

        reason = self._budget_exceeded(acb)
        if reason:
            ckpt_id = self.kernel.storage.checkpoint(pid, label=f"budget:{reason}")
            acb.checkpoint_id = ckpt_id
            acb.exit_status = "budget"
            acb.exit_message = f"budget exceeded: {reason}"
            transition(acb, AgentState.SUSPENDED, "budget")
            return "suspend"

        transition(acb, AgentState.READY, "end_turn")
        self.add_ready(pid)
        return "continue"
# ---------------------------------------------------------------- budgets
    def account_llm(self, pid: int, tokens_in: int, tokens_out: int, cost: float) -> None:
        acb = self.kernel.agent_manager.get(pid)
        u = acb.usage
        u.tokens_in += tokens_in
        u.tokens_out += tokens_out
        u.cost_usd += cost
        now = time.monotonic()
        if now - u.token_window_start >= 60:
            u.token_window_start = now
            u.tokens_in_window = 0
        u.tokens_in_window += tokens_in + tokens_out

    def account_tool(self, pid: int) -> None:
        self.kernel.agent_manager.get(pid).usage.tool_calls += 1

    def _budget_exceeded(self, acb) -> str | None:
        b, u = acb.budgets, acb.usage
        if b.tokens_per_min and u.tokens_in_window > b.tokens_per_min:
            return f"tokens_per_min ({u.tokens_in_window}/{b.tokens_per_min})"
        if b.cost_per_hour_usd and u.cost_usd > b.cost_per_hour_usd:
            return f"cost_per_hour_usd (${u.cost_usd:.4f}/${b.cost_per_hour_usd})"
        if b.max_wall_clock_s and u.run_time_s > b.max_wall_clock_s:
            return f"max_wall_clock_s ({u.run_time_s:.1f}/{b.max_wall_clock_s}s)"
        if b.max_tool_calls and u.tool_calls > b.max_tool_calls:
            return f"max_tool_calls ({u.tool_calls}/{b.max_tool_calls})"
        return None

    # ------------------------------------------------------------ self-control
    async def yield_cpu(self, pid: int) -> dict:
        """Voluntarily give up the CPU mid-turn; returns on the next grant."""
        acb = self.kernel.agent_manager.get(pid)
        if self._running == pid:
            self._running = None
            transition(acb, AgentState.READY, "yield")
            self.add_ready(pid)
        await self.wait_for_grant(pid)
        return {"ok": True}

    async def sleep(self, pid: int, ms: float) -> dict:
        """Release the CPU, sleep, then reacquire before returning."""
        if self._running == pid:
            acb = self.kernel.agent_manager.get(pid)
            self._running = None
            transition(acb, AgentState.READY, "sleep")
            self.add_ready(pid)
        await asyncio.sleep(ms / 1000)
        await self.wait_for_grant(pid)
        return {"woke_at": time.time()}

    async def suspend(self, pid: int, reason: str = "operator") -> dict:
        """Checkpoint + suspend an agent (operator or self-suspend path)."""
        acb = self.kernel.agent_manager.get(pid)
        if acb.state is AgentState.TERMINATED:
            raise AiosError(E_STATE, f"agent {pid} is already terminated")
        if acb.state is AgentState.SUSPENDED:
            return {"checkpoint_id": acb.checkpoint_id}
        self.remove(pid)
        ckpt_id = self.kernel.storage.checkpoint(pid, label=reason)
        acb.checkpoint_id = ckpt_id
        transition(acb, AgentState.SUSPENDED, "suspend")
        self.kernel.audit.record(
            "agent.suspend", pid=pid, reason=reason, checkpoint=ckpt_id
        )
        return {"checkpoint_id": ckpt_id}


# ------------------------------------------------------------------ syscalls
@register("yield")
async def _yield(kernel, pid: int, args: dict) -> dict:
    return await kernel.scheduler.yield_cpu(pid)


@register("sleep")
async def _sleep(kernel, pid: int, args: dict) -> dict:
    return await kernel.scheduler.sleep(pid, args["ms"])


@register("suspend")
async def _suspend(kernel, pid: int, args: dict) -> dict:
    return await kernel.scheduler.suspend(pid, args.get("reason") or "self")


@register("resume")
async def _resume(kernel, pid: int, args: dict) -> dict:
    await kernel.agent_manager.resume(args["pid"])
    return {"ok": True}
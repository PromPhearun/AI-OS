"""Scheduler — cooperative, priority-ordered agent scheduling.

Phase 1 model (docs/03-scheduler.md):

  * one RUNNING slot — agents execute strictly one at a time;
  * READY agents are picked by (effective_priority desc, arrival seq asc);
  * preemption happens only at turn boundaries (the agent loop yields);
  * aging: waiting agents gain +1 effective priority every AGING_RATE waits;
  * budgets are checked at the end of every turn; exhaustion hard-stops the
    agent by checkpointing it and suspending it (E_BUDGET);
  * blocking syscalls (recv_msg / join) park the caller in BLOCKED via
    ``block``/``unblock`` and are woken by the IPC Manager via ``wake``
    (docs/03-scheduler.md §5).
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
        self._blocked_events: dict[int, asyncio.Event] = {}
        # Phase 4 observability (docs/10-ui.md §4 /v1/scheduler + benchmarks):
        self._epoch_start = time.monotonic()
        self._wait_since: dict[int, float] = {}  # pid -> monotonic when queueing
        self._wait_history: dict[int, list[float]] = {}  # pid -> last waited_ms
        self._dispatch_count = 0
        self._preempt_count = 0
        self._running_since: float | None = None

    def _record_wait(self, pid: int) -> None:
        """Register the start of a queued wait for ``pid`` (idempotent)."""
        self._wait_since.setdefault(pid, time.monotonic())

    def _end_wait(self, pid: int) -> float:
        """Close the wait interval; returns waited milliseconds (0 if untracked)."""
        started = self._wait_since.pop(pid, None)
        if started is None:
            return 0.0
        waited_ms = (time.monotonic() - started) * 1000.0
        hist = self._wait_history.setdefault(pid, [])
        hist.append(waited_ms)
        if len(hist) > 100:  # bounded per-agent history
            del hist[:-100]
        return waited_ms

    def _wait_stats(self, pid: int) -> dict:
        hist = self._wait_history.get(pid, [])
        current = self._wait_since.get(pid)
        waited_ms = (time.monotonic() - current) * 1000.0 if current else 0.0
        return {
            "waited_ms": round(hist[-1] if hist else waited_ms, 2),
            "current_wait_ms": round(waited_ms, 2),
            "avg_wait_ms": round(sum(hist) / len(hist), 2) if hist else 0.0,
            "max_wait_ms": round(max(hist), 2) if hist else 0.0,
            "wait_count": len(hist),
        }

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
            self._running_since = None
            self._preempt_count += 1  # RUNNING agent evicted (suspend/kill path)
        self._blocked_events.pop(pid, None)

    def is_running(self, pid: int) -> bool:
        return self._running == pid

    # ---------------------------------------------------------- blocking IPC
    def block(self, pid: int) -> asyncio.Event:
        """Park the caller: RUNNING -> BLOCKED, freeing the CPU slot.

        The returned event is set by ``wake`` when the agent's wait condition
        may be satisfied (message arrival). The blocked coroutine is expected
        to call ``unblock`` — then ``wait_for_grant`` — when it wakes or times
        out (docs/03-scheduler.md §5).
        """
        acb = self.kernel.agent_manager.get(pid)
        if self._running == pid:
            self._running = None
        transition(acb, AgentState.BLOCKED, "block")
        event = asyncio.Event()
        self._blocked_events[pid] = event
        return event

    def unblock(self, pid: int) -> None:
        """Return a BLOCKED agent to READY + requeue it (wait resolved)."""
        self._blocked_events.pop(pid, None)
        acb = self.kernel.agent_manager.peek(pid)
        if acb is None or acb.state is not AgentState.BLOCKED:
            return
        transition(acb, AgentState.READY, "unblock")
        self.add_ready(pid)

    def wake(self, pid: int) -> None:
        """Wake a BLOCKED agent whose wait condition may now be satisfied."""
        event = self._blocked_events.get(pid)
        acb = self.kernel.agent_manager.peek(pid)
        if acb is None or acb.state is not AgentState.BLOCKED:
            return
        transition(acb, AgentState.READY, "wake")
        self.add_ready(pid)
        if event is not None:
            self._blocked_events.pop(pid, None)
            event.set()

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
        self._record_wait(pid)
        while True:
            if acb.state is not AgentState.READY or pid not in self._ready:
                return  # suspended / terminated / killed while waiting
            if self._running is None and self._pick() == pid:
                self._ready.remove(pid)
                self._seq.pop(pid, None)
                self._running = pid
                acb.wait_turns = 0
                acb.run_since = time.monotonic()
                self._dispatch_count += 1
                self._end_wait(pid)
                self._running_since = acb.run_since
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
            # An agent holding an unresolved approval ticket is *parked*
            # (checkpointed + SUSPENDED) instead of terminated, so the ticket
            # survives and `approve` resumes the loop (docs/08-security §7).
            if self.kernel.access.has_pending(pid):
                ckpt_id = self.kernel.storage.checkpoint(pid, label="awaiting-approval")
                acb.checkpoint_id = ckpt_id
                acb.exit_status = "limit"
                acb.exit_message = "max_turns reached while awaiting operator approval"
                transition(acb, AgentState.SUSPENDED, "awaiting-approval")
                return "suspend"
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
        if acb.state is AgentState.BLOCKED:
            # Unpark an agent blocked in recv_msg/join so its in-flight syscall
            # unwinds cleanly at the next grant check (no E_STATE on resume).
            self.wake(pid)
        self.remove(pid)
        ckpt_id = self.kernel.storage.checkpoint(pid, label=reason)
        acb.checkpoint_id = ckpt_id
        transition(acb, AgentState.SUSPENDED, "suspend")
        self.kernel.audit.record(
            "agent.suspend", pid=pid, reason=reason, checkpoint=ckpt_id
        )
        return {"checkpoint_id": ckpt_id}

    # --------------------------------------------------------- observability
    def snapshot(self) -> dict:
        """A point-in-time view of the scheduler (docs/10-ui.md §4 /v1/scheduler).

        Powers the web desktop's Scheduler tab and the Phase 4 fairness /
        throughput benchmarks: queue depths, per-agent wait statistics,
        utilization, and dispatch/preemption counters.
        """
        now = time.monotonic()
        wall = max(now - self._epoch_start, 1e-9)

        agents: dict[int, dict] = {}
        run_seconds = 0.0
        for acb in self.kernel.agent_manager._table.values():
            if acb.state is AgentState.TERMINATED:
                continue
            ws = self._wait_stats(acb.pid)
            agents[acb.pid] = {
                "name": acb.spec.get("name", "?"),
                "state": acb.state.value,
                "priority": acb.priority,
                "effective_priority": self._effective(acb),
                "waited_ms": ws["waited_ms"],
                "current_wait_ms": ws["current_wait_ms"],
                "avg_wait_ms": ws["avg_wait_ms"],
                "max_wait_ms": ws["max_wait_ms"],
                "wait_count": ws["wait_count"],
                "run_time_s": round(acb.usage.run_time_s, 3),
            }
            if acb.run_since is not None and acb.state is AgentState.RUNNING:
                run_seconds += acb.usage.run_time_s + (now - acb.run_since)
            else:
                run_seconds += acb.usage.run_time_s

        ready_rows = []
        for pid in self._ready:
            acb = self.kernel.agent_manager.get(pid)
            ws = self._wait_stats(pid)
            ready_rows.append(
                {
                    "pid": pid,
                    "name": acb.spec.get("name", "?"),
                    "priority": acb.priority,
                    "effective_priority": self._effective(acb),
                    "waited_ms": ws["waited_ms"],
                    "current_wait_ms": ws["current_wait_ms"],
                }
            )

        all_wait: list[float] = []
        for hist in self._wait_history.values():
            all_wait.extend(hist)

        return {
            "running": self._running,
            "ready": ready_rows,
            "blocked": list(self._blocked_events),
            "queues": {
                "ready_depth": len(self._ready),
                "blocked_depth": len(self._blocked_events),
                "total_live": len(agents),
            },
            "utilization": {
                "run_time_s": round(run_seconds, 3),
                "wall_s": round(wall, 3),
                "percent": round((run_seconds / wall) * 100.0, 2),
            },
            "stats": {
                "dispatches": self._dispatch_count,
                "preemptions": self._preempt_count,
                "avg_wait_ms": round(sum(all_wait) / len(all_wait), 2) if all_wait else 0.0,
                "max_wait_ms": round(max(all_wait), 2) if all_wait else 0.0,
            },
            "agents": agents,
        }


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
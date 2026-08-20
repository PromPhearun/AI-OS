"""Agent Manager — PID allocation, the process table, and process lifecycle."""

from __future__ import annotations

import asyncio
import time

from ..acb import AgentControlBlock, AgentState, Budgets
from ..errors import AiosError, E_INVAL, E_NOENT, E_STATE
from ..lifecycle import transition
from ..specs import validate_spec
from ..syscalls.registry import register


class AgentManager:
    """Owns the process table; the only module allowed to create/remove ACBs."""

    def __init__(self, kernel):
        self.kernel = kernel
        self._table: dict[int, AgentControlBlock] = {}
        self._next_pid = 1
        self._tasks: dict[int, asyncio.Task] = {}
        self._runner_factories: dict[int, callable] = {}
        self._exit_events: dict[int, asyncio.Event] = {}
        self._reaped: dict[int, dict] = {}  # tombstone records for terminated agents

    # ------------------------------------------------------------------ table
    def get(self, pid: int) -> AgentControlBlock:
        acb = self._table.get(pid)
        if acb is None:
            raise AiosError(E_NOENT, f"no such agent: {pid}")
        return acb

    def peek(self, pid: int) -> AgentControlBlock | None:
        return self._table.get(pid)

    def count(self) -> int:
        return len(self._table)

    def list(self) -> list[dict]:
        return [acb.to_dict() for acb in sorted(self._table.values(), key=lambda a: a.pid)]

    def _allocate_pid(self) -> int:
        pid = self._next_pid
        self._next_pid += 1
        return pid

    # ------------------------------------------------------------------ spawn
    async def spawn(
        self,
        spec: dict,
        *,
        caller_pid: int | None = None,
        runner_factory: callable | None = None,
    ) -> int:
        """Create a new agent process.

        ``runner_factory`` (if given) must be a zero-arg callable returning an
        async ``run(pid)`` coroutine — supplied by the SDK at connect time.
        """
        validate_spec(spec)
        self.kernel.llm.validate_model(spec["llm"]["model"])

        pid = self._allocate_pid()
        acb = AgentControlBlock(
            pid=pid,
            spec=spec,
            priority=int(spec.get("priority", 0)),
            parent_pid=caller_pid,
            group_id=spec.get("group_id", "default"),
            budgets=Budgets.from_spec(spec),
        )
        acb.workspace = self.kernel.workspaces.create(pid)

        self._table[pid] = acb
        transition(acb, AgentState.READY, "spawn")
        self._exit_events[pid] = asyncio.Event()

        self.kernel.context.create(pid, system=spec["llm"].get("system"))
        self.kernel.memory.create_namespace(pid)
        self.kernel.agent_logs[pid] = []
        self.kernel.audit.record(
            "agent.spawn", pid=pid, spec_name=spec["name"], parent=caller_pid
        )

        # Every agent enters the scheduler's ready queue, whether or not an SDK
        # runner drives it; wait_for_grant grants RUNNING at the next slice.
        self.kernel.scheduler.add_ready(pid)
        if runner_factory is not None:
            self._runner_factories[pid] = runner_factory
            self._tasks[pid] = asyncio.create_task(self._run_agent(pid, runner_factory))
        return pid

    # ------------------------------------------------------------------- run
    async def _run_agent(self, pid: int, runner_factory) -> None:
        try:
            runner = runner_factory(pid)
            await runner
        except Exception as exc:  # noqa: BLE001 — last-resort fence
            acb = self._table.get(pid)
            if acb is not None and acb.exit_status is None:
                acb.exit_status = "error"
                acb.exit_message = "agent runtime error"
            self.kernel.audit.record(
                "agent.crash", pid=pid, error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            self.on_task_done(pid)

    def on_task_done(self, pid: int) -> None:
        """Called when the agent task ends. Suspended agents stay in the table."""
        acb = self._table.get(pid)
        if acb is None:
            return
        self._tasks.pop(pid, None)
        if acb.state is AgentState.SUSPENDED:
            return  # process lives; a later resume re-creates the task
        # TERMINATED (or anything else that unwound the loop): reap the process.
        self._drop(pid)

    def _drop(self, pid: int) -> None:
        acb = self._table.pop(pid, None)
        if acb is None:
            return
        self._reaped[pid] = acb.to_dict()  # keep a tombstone for ps/logs
        self.kernel.scheduler.remove(pid)
        self.kernel.context.free(pid)
        self.kernel.memory.free(pid)
        self.kernel.agent_logs.pop(pid, None)
        self.kernel.workspaces.remove(pid)
        ev = self._exit_events.pop(pid, None)
        if ev is not None:
            ev.set()

    def record(self, pid: int) -> dict | None:
        """Live ACB view, or the tombstone record of a reaped agent."""
        acb = self._table.get(pid)
        if acb is not None:
            return acb.to_dict()
        return self._reaped.get(pid)

# ------------------------------------------------------------- lifecycle
    def request_terminate(self, pid: int) -> None:
        """Handle the `exit` syscall: mark TERMINATED; the task reaps itself."""
        acb = self._table.get(pid)
        if acb is None:
            return
        self.kernel.scheduler.remove(pid)
        transition(acb, AgentState.TERMINATED, "exit")
        self.kernel.audit.record(
            "agent.exit",
            pid=pid,
            status=acb.exit_status,
            message=acb.exit_message,
        )

    def kill(self, pid: int, reason: str = "killed by operator") -> None:
        acb = self.get(pid)
        if acb.state is AgentState.TERMINATED:
            self._drop(pid)
            return
        task = self._tasks.get(pid)
        if task is not None and not task.done():
            task.cancel()
        acb.exit_status = "killed"
        acb.exit_message = reason
        self.kernel.audit.record("agent.kill", pid=pid, reason=reason)
        self._drop(pid)

    async def resume(self, pid: int) -> dict:
        """Resume a SUSPENDED agent from its checkpoint."""
        acb = self.get(pid)
        if acb.state is not AgentState.SUSPENDED:
            raise AiosError(
                E_STATE, f"agent {pid} is {acb.state.value}, not suspended"
            )
        if acb.checkpoint_id is None:
            raise AiosError(E_STATE, f"agent {pid} has no checkpoint to resume from")
        ckpt_id = acb.checkpoint_id
        self.kernel.storage.restore(pid, ckpt_id)
        acb.checkpoint_id = None
        acb.exit_status = None
        acb.exit_message = None
        transition(acb, AgentState.READY, "resume")
        self.kernel.scheduler.add_ready(pid)

        factory = self._runner_factories.get(pid)
        if factory is None:
            raise AiosError(E_INVAL, f"cannot resume {pid}: no runner factory")
        self._tasks[pid] = asyncio.create_task(self._run_agent(pid, factory))
        self.kernel.audit.record("agent.resume", pid=pid, checkpoint=ckpt_id)
        return {"ok": True}

    def shutdown_all(self, reason: str = "kernel shutdown") -> None:
        for pid in list(self._table):
            self.kill(pid, reason=reason)

    # ------------------------------------------------------------ wait helper
    async def wait_task(self, pid: int, timeout: float | None = None) -> str:
        """Wait until the agent task finishes (exit, crash, or suspension)."""
        task = self._tasks.get(pid)
        if task is None:
            acb = self._table.get(pid)
            if acb is None or acb.state is AgentState.TERMINATED:
                return "terminated"
            return acb.state.value
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            raise AiosError(
                E_TIMEOUT, f"agent {pid} did not finish within {timeout}s"
            ) from None
        return "done"


# ------------------------------------------------------------------ syscalls
@register("spawn")
async def _spawn(kernel, pid: int, args: dict) -> dict:
    child = await kernel.spawn_agent(args["spec"], caller_pid=pid)
    return {"pid": child}


@register("exit")
async def _exit(kernel, pid: int, args: dict) -> dict:
    acb = kernel.agent_manager.get(pid)
    acb.exit_status = args.get("status", "ok")
    acb.exit_message = args.get("message")
    kernel.agent_manager.request_terminate(pid)
    return {"ok": True}


@register("get_pid")
async def _get_pid(kernel, pid: int, args: dict) -> dict:
    acb = kernel.agent_manager.get(pid)
    return {"pid": pid, "parent_pid": acb.parent_pid, "group_id": acb.group_id}


@register("get_status")
async def _get_status(kernel, pid: int, args: dict) -> dict:
    target = args.get("pid") or pid
    acb = kernel.agent_manager.get(target)
    return {
        "state": acb.state.value,
        "usage": acb.usage.to_dict(),
        "checkpoint": acb.checkpoint_id,
    }


@register("get_usage")
async def _get_usage(kernel, pid: int, args: dict) -> dict:
    u = kernel.agent_manager.get(pid).usage
    return {
        "tokens": u.total_tokens(),
        "cost": round(u.cost_usd, 6),
        "tool_calls": u.tool_calls,
        "wall_clock": round(u.run_time_s, 3),
        "turns": u.turns,
    }
"""Control plane — host-side launch & supervision (docs/09-sdk.md §5)."""

from __future__ import annotations

import asyncio

from aios_kernel.errors import AiosError, E_NOENT

from .agent import AGENT_REGISTRY, AgentRunner, RunSummary, summary_from_record

PENDING = "pending"


class ControlPlane:
    def __init__(self, kernel):
        self.kernel = kernel

    # ----------------------------------------------------------------- launch
    async def launch(self, spec: dict, timeout: float | None = None) -> int:
        """Spawn one agent; run to completion unless budgets suspend it."""
        name = spec["name"]
        definition = AGENT_REGISTRY.get(name)
        if definition is None:
            raise AiosError(E_NOENT, f"no @agent registered for spec name '{name}'")
        turn_fn = definition["turn"]
        factory = lambda pid: AgentRunner(self.kernel, pid, turn_fn).run()
        pid = await self.kernel.spawn_agent(spec, runner_factory=factory)
        await self.kernel.agent_manager.wait_task(pid, timeout=timeout)
        return pid

    # ---------------------------------------------------------------- inspect
    def ps(self) -> list[dict]:
        """Process table: live ACBs plus reaped tombstones, pid-sorted."""
        rows = []
        for pid in sorted(self.kernel.agent_manager._table):
            rows.append(self.kernel.agent_manager.record(pid))
        for pid in sorted(self.kernel.agent_manager._reaped):
            rows.append(self.kernel.agent_manager.record(pid))
        return rows

    def logs(self, pid: int) -> list[dict]:
        return list(self.kernel.agent_logs.get(pid, []))

    # -------------------------------------------------------------- security
    def approvals(self, *, all: bool = True) -> list[dict]:
        """All approval tickets (host-side operator view)."""
        return self.kernel.access.list_tickets(all=all)

    async def approve(self, ticket_id: str) -> dict:
        """Approve a pending approval ticket (host-side operator action).

        If the owning agent parked itself while awaiting the decision, the
        kernel resumes it so the approved tool call can proceed (docs/
        08-security.md §7 human gates).
        """
        return await self.kernel.access.approve(ticket_id)

    def deny(self, ticket_id: str) -> dict:
        """Deny a pending approval ticket (host-side operator action)."""
        return self.kernel.access.deny(ticket_id)

    def verify_audit(self) -> dict:
        """Re-derive the audit hash chain; detect tampering."""
        return self.kernel.audit.verify()

    def mcp_servers(self) -> list[dict]:
        return self.kernel.mcp.list_servers()

    # ---------------------------------------------------------------- control
    async def suspend(self, pid: int, reason: str = "operator") -> dict:
        return await self.kernel.scheduler.suspend(pid, reason)

    async def resume(self, pid: int, runner_factory=None) -> dict:
        return await self.kernel.agent_manager.resume(pid, runner_factory=runner_factory)

    async def resume_session(self, timeout: float | None = None) -> list[RunSummary]:
        """--resume: restore every suspended agent from disk, re-attach
        runners (resolved from AGENT_REGISTRY by spec name), and run them.

        Agents whose budget is still exhausted are re-suspended by the
        scheduler with a fresh checkpoint — that is expected and observable.
        """
        pids = self.kernel.restore_session()
        summaries = []
        for pid in pids:
            acb = self.kernel.agent_manager.get(pid)
            name = acb.spec["name"]
            definition = AGENT_REGISTRY.get(name)
            if definition is None:
                raise AiosError(E_NOENT, f"no @agent registered for spec name '{name}'")
            turn_fn = definition["turn"]
            factory = lambda pid: AgentRunner(self.kernel, pid, turn_fn).run()
            await self.kernel.agent_manager.resume(pid, runner_factory=factory)
            await self.kernel.agent_manager.wait_task(pid, timeout=timeout)
            summaries.append(summary_from_record(self.kernel.agent_manager.record(pid)))
        return summaries

    async def kill(self, pid: int, reason: str = "killed by operator") -> None:
        self.kernel.agent_manager.kill(pid, reason=reason)

    async def wait(self, pid: int, timeout: float | None = None) -> str:
        return await self.kernel.agent_manager.wait_task(pid, timeout=timeout)

    async def shutdown(self) -> None:
        await self.kernel.shutdown()
"""Control plane — host-side launch & supervision (docs/09-sdk.md §5)."""

from __future__ import annotations

import asyncio

from aios_kernel.errors import AiosError, E_NOENT

from .agent import AGENT_REGISTRY, AgentRunner

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

    # ---------------------------------------------------------------- control
    async def suspend(self, pid: int, reason: str = "operator") -> dict:
        return await self.kernel.scheduler.suspend(pid, reason)

    async def resume(self, pid: int) -> dict:
        return await self.kernel.agent_manager.resume(pid)

    async def kill(self, pid: int, reason: str = "killed by operator") -> None:
        self.kernel.agent_manager.kill(pid, reason=reason)

    async def wait(self, pid: int, timeout: float | None = None) -> str:
        return await self.kernel.agent_manager.wait_task(pid, timeout=timeout)

    async def shutdown(self) -> None:
        await self.kernel.shutdown()
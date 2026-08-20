"""Integration tests: the request_permission approval path end-to-end
(docs/08-security.md §12 acceptance — grant/deny/expire are covered here and
in tests/unit/test_access.py)."""

from __future__ import annotations

import asyncio

import pytest

from aios_kernel import Kernel
from aios_sdk import AgentRunner
from aios_sdk.agent import AGENT_REGISTRY
from aios_sdk.control import ControlPlane
from aios_sdk.errors import AiosPermissionError

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_agent_enqueues_ticket_and_executes_after_operator_approves(kernel: Kernel) -> None:
    state = {"approval": None, "done": False}

    async def worker(sc):
        try:
            await sc.call_tool("fs.write", {"path": "note.txt", "content": "done"})
        except AiosPermissionError:
            if state["approval"] is None:
                ticket = await sc.request_permission(
                    "fs.write",
                    {"path": "note.txt", "content": "done"},
                    reason="write the project note",
                )
                state["approval"] = ticket["ticket_id"]
            return False  # come back next turn until an operator decides
        state["done"] = True
        return True

    AGENT_REGISTRY["approval-worker"] = {"turn": worker, "spec": None}
    try:
        spec = _base_spec(
            name="approval-worker",
            capabilities={"tools": [{"name": "fs.write", "needs_approval": True}]},
        )
        pid = await kernel.spawn_agent(
            spec, runner_factory=lambda pid: AgentRunner(kernel, pid, worker).run()
        )

        # wait for the agent to hit the approval gate and enqueue a ticket
        for _ in range(100):
            if state["approval"] is not None:
                break
            await asyncio.sleep(0.01)
        assert state["approval"] is not None

        cp = ControlPlane(kernel)
        assert (await cp.approve(state["approval"]))["status"] == "approved"

        await kernel.agent_manager.wait_task(pid)
        assert state["done"] is True
        rec = kernel.agent_manager.record(pid)
        assert rec["exit_status"] == "ok"
    finally:
        AGENT_REGISTRY.pop("approval-worker", None)
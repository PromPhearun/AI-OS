"""Kernel — the trusted computing base: wires all managers into one event loop.

Ownership map (docs/02-kernel.md §2):
  * audit       — append-only JSONL audit trail (every syscall recorded)
  * context     — per-agent LL1 message history
  * memory      — per-agent L2 working memory (in-memory, Phase 1)
  * workspaces  — one sandboxed directory per agent
  * storage     — in-memory checkpoints (disk lands in Phase 2)
  * vault       — get_env backend (non-secret config; Phase 1)
  * llm         — serialized LLM service with per-agent accounting
  * tools       — Tool Manager (registry + call_tool pipeline)
  * agent_manager — process table + lifecycle
  * scheduler   — priority/aging single-CPU scheduling + budgets
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import modules  # noqa: F401  (importing registers every syscall handler)
from .modules.agent_manager import AgentManager
from .modules.audit import AuditLog
from .modules.context import ContextManager
from .modules.llm_core import LLMCore
from .modules.memory import MemoryManager
from .modules.scheduler import Scheduler
from .modules.storage import StorageManager
from .modules.tools import ToolManager
from .modules.vault import Vault
from .modules.workspaces import WorkspaceManager
from .syscalls import dispatch


class Kernel:
    def __init__(
        self,
        *,
        llm_backend=None,
        audit_path: str | None = None,
        workspace_root: str | None = None,
        env: dict[str, str] | None = None,
        start: bool = True,
    ):
        self.audit = AuditLog(self, path=audit_path)
        self.context = ContextManager(self)
        self.memory = MemoryManager(self)
        self.workspaces = WorkspaceManager(workspace_root)
        self.storage = StorageManager(self)
        self.vault = Vault(self)
        self.llm = LLMCore(self, backend=llm_backend)
        self.tools = ToolManager(self)
        self.agent_manager = AgentManager(self)
        self.scheduler = Scheduler(self)
        self.agent_logs: dict[int, list[dict]] = {}
        if env:
            for key, value in env.items():
                self.vault.set(key, value)
        if start:
            self.audit.open()

    # ------------------------------------------------------------------ ABI
    async def execute(self, pid: int, name: str, args: dict) -> dict:
        """Execute one syscall on behalf of ``pid`` (see syscalls.registry)."""
        return await dispatch(self, pid, name, args)

    async def spawn_agent(self, spec: dict, *, caller_pid: int | None = None, runner_factory=None) -> int:
        return await self.agent_manager.spawn(spec, caller_pid=caller_pid, runner_factory=runner_factory)

    # ------------------------------------------------------------ lifecycle
    async def shutdown(self) -> None:
        """Terminate all agents and close audit; call once per Kernel."""
        self.agent_manager.shutdown_all(reason="kernel shutdown")
        await asyncio.sleep(0)  # let cancelled tasks unwind
        self.audit.close()
        if hasattr(self.llm.backend, "aclose"):
            await self.llm.backend.aclose()
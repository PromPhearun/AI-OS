"""Kernel — the trusted computing base: wires all managers into one event loop.

Ownership map (docs/02-kernel.md §2):
  * audit       — append-only JSONL audit trail (every syscall recorded)
  * context     — per-agent LL1 message history
  * memory      — per-agent L2 working memory (in-memory, Phase 1)
  * workspaces  — one sandboxed directory per agent
  * storage     — durable on-disk checkpoints + the session resume set
  * vault       — get_env backend (non-secret config; Phase 1)
  * llm         — serialized LLM service with per-agent accounting
  * tools       — Tool Manager (registry + call_tool pipeline)
  * agent_manager — process table + lifecycle
  * scheduler   — priority/aging single-CPU scheduling + budgets
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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
        data_root: str | None = None,
        env: dict[str, str] | None = None,
        start: bool = True,
    ):
        """data_root points audit/workspaces/checkpoints/session at one directory
        (default layout is ``aios-data``); an explicit audit_path or
        workspace_root wins over the data_root-derived default."""
        self.data_root = Path(data_root) if data_root else None
        self.audit = AuditLog(
            self, path=audit_path or (str(self.data_root / "audit.jsonl") if self.data_root else None)
        )
        self.context = ContextManager(self)
        self.memory = MemoryManager(self)
        self.workspaces = WorkspaceManager(
            workspace_root or (str(self.data_root / "workspaces") if self.data_root else None)
        )
        self.storage = StorageManager(
            self,
            root=(str(self.data_root / "checkpoints") if self.data_root else None),
            session_path=(str(self.data_root / "session.json") if self.data_root else None),
        )
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

    def restore_session(self) -> list[int]:
        """--resume boot path: rebuild suspended agents from the session manifest.

        Returns the restored pids, each parked SUSPENDED at its last committed
        checkpoint; the SDK re-attaches runners via ``resume``.
        """
        return self.storage.restore_session()

    # ------------------------------------------------------------ lifecycle
    async def shutdown(self) -> None:
        """Terminate all agents and close audit; call once per Kernel."""
        self.agent_manager.shutdown_all(reason="kernel shutdown")
        await asyncio.sleep(0)  # let cancelled tasks unwind
        self.audit.close()
        if hasattr(self.llm.backend, "aclose"):
            await self.llm.backend.aclose()
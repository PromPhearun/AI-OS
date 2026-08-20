"""AI OS SDK — the only library agents import (docs/09-sdk.md).

Agent side: ``@agent`` + ``aios.syscalls`` proxy.
Control side: ``ControlPlane`` for launch/supervise.
"""

from __future__ import annotations

from aios_kernel import AgentState, Kernel

from . import syscalls  # noqa: F401  (agent-side syscall proxy)
from .agent import AGENT_REGISTRY, AgentRunner, RunSummary, agent, run_agents
from .control import ControlPlane
from .errors import (
    AiosAbortError,
    AiosAgainError,
    AiosBudgetError,
    AiosBusyError,
    AiosError,
    AiosInternalError,
    AiosInvalError,
    AiosLlmError,
    AiosNoEntError,
    AiosNotImplError,
    AiosPermissionError,
    AiosStateError,
    AiosSyscallError,
    AiosTimeoutError,
    AiosToolError,
)
from .session import AgentSession

__version__ = "0.1.0"

__all__ = [
    "Kernel",
    "AgentState",
    "agent",
    "AgentSession",
    "AgentRunner",
    "RunSummary",
    "run_agents",
    "AGENT_REGISTRY",
    "ControlPlane",
    "syscalls",
    "AiosError",
    "AiosSyscallError",
    "AiosPermissionError",
    "AiosBudgetError",
    "AiosNoEntError",
    "AiosNotImplError",
    "AiosInvalError",
    "AiosStateError",
    "AiosBusyError",
    "AiosAgainError",
    "AiosToolError",
    "AiosLlmError",
    "AiosTimeoutError",
    "AiosAbortError",
    "AiosInternalError",
    "__version__",
]
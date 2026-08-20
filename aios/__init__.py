"""`aios` — documented top-level API surface (docs/09-sdk.md).

Thin alias over aios_sdk so agents can write ``from aios import agent,
syscalls as sc`` exactly as documented.
"""

from __future__ import annotations

from aios_sdk import (  # noqa: F401
    AGENT_REGISTRY,
    AgentRunner,
    AgentSession,
    AgentState,
    ControlPlane,
    Kernel,
    RunSummary,
    agent,
    run_agents,
    syscalls,
)
from aios_sdk.errors import *  # noqa: F401,F403
from aios_sdk.session import AgentSession as _Session

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
    "__version__",
]
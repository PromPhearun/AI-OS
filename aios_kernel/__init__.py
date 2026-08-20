"""AI OS kernel — the trusted computing base."""

from .acb import AgentState
from .errors import (
    AiosError,
    E_ABORT,
    E_AGAIN,
    E_BUDGET,
    E_BUSY,
    E_INTERNAL,
    E_INVAL,
    E_LLM,
    E_NOENT,
    E_NOTIMPL,
    E_PERM,
    E_STATE,
    E_TIMEOUT,
    E_TOOL,
)
from .kernel import Kernel

__version__ = "0.1.0"

__all__ = [
    "Kernel",
    "AgentState",
    "AiosError",
    "E_ABORT",
    "E_AGAIN",
    "E_BUDGET",
    "E_BUSY",
    "E_INTERNAL",
    "E_INVAL",
    "E_LLM",
    "E_NOENT",
    "E_NOTIMPL",
    "E_PERM",
    "E_STATE",
    "E_TIMEOUT",
    "E_TOOL",
    "__version__",
]
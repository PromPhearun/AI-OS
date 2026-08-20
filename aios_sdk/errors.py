"""SDK error mapping — one typed exception per syscall error code."""

from __future__ import annotations

from aios_kernel.errors import (  # noqa: F401  (re-exported for convenience)
    AiosError,
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


class AiosSyscallError(Exception):
    """Base class for syscall failures surfaced to agent code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AiosPermissionError(AiosSyscallError):
    pass


class AiosBudgetError(AiosSyscallError):
    pass


class AiosNoEntError(AiosSyscallError):
    pass


class AiosNotImplError(AiosSyscallError):
    pass


class AiosInvalError(AiosSyscallError):
    pass


class AiosStateError(AiosSyscallError):
    pass


class AiosBusyError(AiosSyscallError):
    pass


class AiosAgainError(AiosSyscallError):
    pass


class AiosToolError(AiosSyscallError):
    pass


class AiosLlmError(AiosSyscallError):
    pass


class AiosTimeoutError(AiosSyscallError):
    pass


class AiosInternalError(AiosSyscallError):
    pass


_CODE_TO_EXC: dict[str, type] = {
    "E_PERM": AiosPermissionError,
    "E_BUDGET": AiosBudgetError,
    "E_NOENT": AiosNoEntError,
    "E_NOTIMPL": AiosNotImplError,
    "E_INVAL": AiosInvalError,
    "E_STATE": AiosStateError,
    "E_BUSY": AiosBusyError,
    "E_AGAIN": AiosAgainError,
    "E_TOOL": AiosToolError,
    "E_LLM": AiosLlmError,
    "E_TIMEOUT": AiosTimeoutError,
    "E_INTERNAL": AiosInternalError,
}


def raise_for_error(error: dict) -> None:
    """Raise the typed exception matching a `{error: {code, message}}` payload."""
    exc_type = _CODE_TO_EXC.get(error.get("code", ""), AiosSyscallError)
    raise exc_type(error.get("code", "E_INTERNAL"), error.get("message", "syscall failed"))


__all__ = [
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
    "AiosInternalError",
    "raise_for_error",
    "E_PERM",
    "E_BUDGET",
    "E_NOENT",
    "E_NOTIMPL",
    "E_INVAL",
    "E_STATE",
    "E_BUSY",
    "E_AGAIN",
    "E_TOOL",
    "E_LLM",
    "E_TIMEOUT",
    "E_INTERNAL",
]
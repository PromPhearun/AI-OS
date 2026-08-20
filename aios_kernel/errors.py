"""Canonical error codes and exception type for AI OS.

Syscall failures are returned to agents as ``{error: {code, message}}``.
The message is intentionally generic; detailed traces go to the audit log
(see docs/08-security.md).
"""


class AiosError(Exception):
    """An AI OS kernel error with a canonical machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_result(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


# Canonical error codes (stable ABI surface).
E_PERM = "E_PERM"        # permission denied (deny by default)
E_BUDGET = "E_BUDGET"    # budget exhausted
E_NOENT = "E_NOENT"      # no such process / artifact / key
E_NOTIMPL = "E_NOTIMPL"  # syscall not implemented in this phase
E_INVAL = "E_INVAL"      # invalid arguments
E_STATE = "E_STATE"      # illegal lifecycle transition / wrong state
E_BUSY = "E_BUSY"        # resource busy (e.g. agent already running)
E_AGAIN = "E_AGAIN"      # temporarily unavailable; retry
E_TOOL = "E_TOOL"        # tool execution failed
E_LLM = "E_LLM"          # LLM backend failure
E_TIMEOUT = "E_TIMEOUT"  # operation timed out
E_INTERNAL = "E_INTERNAL"  # unexpected internal failure

__all__ = [
    "AiosError",
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
"""``aios.errors`` — typed syscall exceptions alias."""

from aios_sdk.errors import *  # noqa: F401,F403
from aios_sdk.errors import AiosSyscallError, raise_for_error

__all__ = ["AiosSyscallError", "raise_for_error"]
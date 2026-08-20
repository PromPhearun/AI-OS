"""Syscall ABI package.

Importing this package imports the kernel modules, which registers every
syscall handler (side-effect of the ``@register`` decorator).
"""

from .. import modules as _modules  # noqa: F401  (registers all handlers)
from .registry import args_hash, dispatch, register
from .schema import SCHEMAS, validate_args

__all__ = ["dispatch", "register", "SCHEMAS", "validate_args", "args_hash"]
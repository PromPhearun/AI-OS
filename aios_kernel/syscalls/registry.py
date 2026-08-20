"""Syscall ABI — versioned registry, argument validation, and dispatch.

Canonical syscall table lives in docs/02-kernel.md §5. Every syscall:

  1. is looked up in the registry (unknown → E_NOTIMPL),
  2. has its JSON args validated against a strict schema (bad → E_INVAL),
  3. is checked by Access Control's dispatch gate (deny by default; Phase 3),
  4. executes through the owning kernel module,
  5. is audited with `{ts, pid, syscall, args_hash, result, duration_ms}`.

Failures are returned to the caller as ``{error: {code, message}}`` — never
raised across the ABI boundary. Details go to the audit log only.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Awaitable, Callable

from ..errors import AiosError, E_INTERNAL, E_NOTIMPL
from .schema import SCHEMAS, validate_args

# name -> async handler(kernel, pid, args) -> dict
HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {}


def register(name: str) -> Callable:
    """Decorator that registers an async syscall handler by name."""

    def deco(fn):
        if name in HANDLERS:
            raise RuntimeError(f"duplicate syscall handler: {name}")
        HANDLERS[name] = fn
        return fn

    return deco


def args_hash(args: dict) -> str:
    """Short canonical hash of syscall args for the audit trail."""
    canonical = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


async def dispatch(kernel, pid: int, name: str, args: dict) -> dict:
    """Execute one syscall on behalf of ``pid``; always returns a result dict."""
    started = time.monotonic()

    def _audit(result: str, error_code: str | None = None) -> None:
        kernel.audit.record(
            "syscall",
            pid=pid,
            syscall=name,
            args_hash=args_hash(args),
            result=result,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            error_code=error_code,
        )

    handler = HANDLERS.get(name)
    if handler is None:
        _audit(f"error:{E_NOTIMPL}", E_NOTIMPL)
        return AiosError(
            E_NOTIMPL, f"syscall '{name}' is not implemented in this build"
        ).to_result()

    try:
        validate_args(name, args)
        kernel.access.check_syscall(pid, name, args)
        result = await handler(kernel, pid, args)
        _audit("ok")
        return result
    except AiosError as exc:
        _audit(f"error:{exc.code}", exc.code)
        return exc.to_result()
    except Exception as exc:  # noqa: BLE001 — last-resort fence; details to audit
        _audit("error:E_INTERNAL", E_INTERNAL)
        kernel.audit.record(
            "syscall.crash",
            pid=pid,
            syscall=name,
            error=f"{type(exc).__name__}: {exc}",
        )
        return AiosError(E_INTERNAL, "internal kernel error").to_result()
"""``aios.syscalls`` — module-level syscall proxy for agent code.

Inside an agent turn the runner sets a contextvar-bound session, so entries
can call ``sc.get_pid()`` etc. directly (mirrors docs/09-sdk.md §2).
"""

from __future__ import annotations

import contextvars

from .session import AgentSession

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("aios_current_session", default=None)


def _sc() -> AgentSession:
    session = _CURRENT.get()
    if session is None:
        raise RuntimeError("aios.syscalls used outside an agent turn")
    return session


def set_current_session(session: AgentSession | None) -> contextvars.Token | None:
    """Bound a session for the current (entry) context; returns the token."""
    if session is None:
        return None
    return _CURRENT.set(session)


def reset_current_session(token) -> None:
    if token is not None:
        _CURRENT.reset(token)


# ---------------------------------------------------------------- lifecycle
async def get_pid():
    return await _sc().get_pid()


async def spawn(spec: dict):
    return await _sc().spawn(spec)


async def exit(status: str = "ok", message: str | None = None):
    return await _sc().exit(status=status, message=message)


async def suspend(reason: str | None = None):
    return await _sc().suspend(reason)


async def resume(pid: int):
    return await _sc().resume(pid)


async def get_status(pid: int | None = None):
    return await _sc().get_status(pid)


async def get_usage():
    return await _sc().get_usage()


async def generate(user: str | None = None, *, temperature: float = 0.0, max_tokens: int | None = None):
    return await _sc().generate(user, temperature=temperature, max_tokens=max_tokens)


async def checkpoint(label: str | None = None):
    return await _sc().checkpoint(label)


# ------------------------------------------------------------------ context
async def read_context():
    return await _sc().read_context()


async def append_context(role: str, content: str, pinned: bool = False):
    return await _sc().append_context(role, content, pinned)


# ------------------------------------------------------------------- memory
async def write_memory(namespace: str, key: str, value, ttl: float | None = None):
    return await _sc().write_memory(namespace, key, value, ttl)


async def read_memory(namespace: str, key: str):
    return await _sc().read_memory(namespace, key)


# -------------------------------------------------------------------- tools
async def list_tools(query: str | None = None):
    return await _sc().list_tools(query)


async def call_tool(tool: str, args: dict):
    return await _sc().call_tool(tool, args)


async def cancel_tool(call_id: str):
    return await _sc().cancel_tool(call_id)


# ----------------------------------------------------------- security/util
async def get_env(key: str):
    return await _sc().get_env(key)


async def log(level: str, message: str):
    return await _sc().log(level, message)


# ---------------------------------------------------------------- scheduler
async def sleep(ms: float):
    return await _sc().sleep(ms)


async def yield_cpu():
    return await _sc().yield_cpu()
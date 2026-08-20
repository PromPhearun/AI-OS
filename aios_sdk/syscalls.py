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
async def write_memory(namespace: str, key: str, value, *, ttl: float | None = None, kind: str | None = None, tags: list[str] | None = None):
    return await _sc().write_memory(namespace, key, value, ttl=ttl, kind=kind, tags=tags)


async def read_memory(namespace: str, key: str):
    return await _sc().read_memory(namespace, key)


async def search_memory(query: str, *, namespace: str | None = None, top_k: int = 5, min_score: float | None = None):
    return await _sc().search_memory(query, namespace=namespace, top_k=top_k, min_score=min_score)


async def forget_memory(namespace: str, key: str | None = None):
    return await _sc().forget_memory(namespace, key)


# --------------------------------------------------------------- context
async def summarize_context(target_tokens: int | None = None):
    return await _sc().summarize_context(target_tokens)


# ------------------------------------------------------------- semantic fs
async def store_artifact(path: str, data: str, *, mime: str | None = None):
    return await _sc().store_artifact(path, data, mime=mime)


async def fs_read(path: str, *, max_bytes: int | None = None):
    return await _sc().fs_read(path, max_bytes=max_bytes)


async def fs_write(path: str, content: str, *, mime: str | None = None):
    return await _sc().fs_write(path, content, mime=mime)


async def fs_search(query: str, *, top_k: int = 5):
    return await _sc().fs_search(query, top_k=top_k)


# -------------------------------------------------------------------- tools
async def list_tools(query: str | None = None):
    return await _sc().list_tools(query)


async def call_tool(tool: str, args: dict):
    return await _sc().call_tool(tool, args)


async def cancel_tool(call_id: str):
    return await _sc().cancel_tool(call_id)


# ------------------------------------------------------- access control
async def get_permissions():
    return await _sc().get_permissions()


async def request_permission(tool: str, args: dict, reason: str | None = None):
    return await _sc().request_permission(tool, args, reason)


async def list_approvals(*, all: bool = False):
    return await _sc().list_approvals(all=all)


async def get_sandbox():
    return await _sc().get_sandbox()


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


# ---------------------------------------------------------------------- ipc
async def send_msg(
    to_pid: int,
    body: dict,
    *,
    type: str = "direct",
    reply_to: str | None = None,
    topic: str | None = None,
    priority: int = 50,
    trace_id: str | None = None,
    ttl_s: float | None = None,
):
    return await _sc().send_msg(
        to_pid,
        body,
        type=type,
        reply_to=reply_to,
        topic=topic,
        priority=priority,
        trace_id=trace_id,
        ttl_s=ttl_s,
    )


async def recv_msg(timeout_ms: float, *, filter: dict | None = None):
    return await _sc().recv_msg(timeout_ms, filter=filter)


async def subscribe(topic: str):
    return await _sc().subscribe(topic)


async def unsubscribe(topic: str):
    return await _sc().unsubscribe(topic)


async def publish(topic: str, payload: dict):
    return await _sc().publish(topic, payload)


async def join(pids: list[int], timeout_ms: float | None = None):
    return await _sc().join(pids, timeout_ms=timeout_ms)
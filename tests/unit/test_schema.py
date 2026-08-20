"""Unit tests: syscall argument schemas — strict validation (E_INVAL)."""

from __future__ import annotations

import pytest

from aios_kernel.errors import AiosError, E_INVAL
from aios_kernel.syscalls.registry import dispatch, register
from aios_kernel.syscalls.schema import SCHEMAS, validate_args


@pytest.mark.parametrize(
    ("name", "good"),
    [
        ("get_pid", {}),
        ("exit", {"status": "ok"}),
        ("exit", {"status": "ok", "message": "done"}),
        ("sleep", {"ms": 100}),
        ("suspend", {"reason": "user asked"}),
        ("suspend", {}),
        ("resume", {"pid": 7}),
        ("get_status", {}),
        ("get_status", {"pid": 3}),
        ("read_context", {}),
        ("append_context", {"role": "user", "content": "hi"}),
        ("append_context", {"role": "tool", "content": "res", "pinned": True}),
        ("write_memory", {"namespace": "ns", "key": "k", "value": 42}),
        ("write_memory", {"namespace": "ns", "key": "k", "value": [1, 2], "ttl": 5}),
        ("write_memory", {"namespace": "ns", "key": "k", "value": 1, "kind": "episodic", "tags": ["a", "b"]}),
        ("write_memory", {"namespace": "ns", "key": "k", "value": 1, "kind": "procedural"}),
        ("read_memory", {"namespace": "ns", "key": "k"}),
        ("search_memory", {"query": "q3 revenue"}),
        ("search_memory", {"query": "q", "namespace": "pool", "top_k": 10, "min_score": 0.3}),
        ("forget_memory", {"namespace": "agent:1"}),
        ("forget_memory", {"namespace": "agent:1", "key": "k"}),
        ("summarize_context", {}),
        ("summarize_context", {"target_tokens": 100}),
        ("list_tools", {}),
        ("list_tools", {"query": "fs"}),
        ("call_tool", {"tool": "fs.read", "args": {"path": "a"}}),
        ("cancel_tool", {"call_id": "abc"}),
        ("checkpoint", {"label": "t1"}),
        ("get_usage", {}),
        ("generate", {"user": "hello"}),
        ("generate", {"user": "hi", "temperature": 0.5, "max_tokens": 100}),
        ("get_env", {"key": "HOME"}),
        ("log", {"level": "info", "message": "m"}),
        ("send_msg", {"to_pid": 2, "body": {"text": "hi"}}),
        ("send_msg", {"to_pid": 2, "body": {}, "type": "reply", "reply_to": "m1"}),
        ("send_msg", {"to_pid": 2, "body": {"spec": {}}, "type": "handoff", "priority": 80, "ttl_s": 5}),
        ("recv_msg", {"timeout_ms": 500}),
        ("recv_msg", {"timeout_ms": 0, "filter": {"from_pid": 1, "type": "direct", "topic": "jobs"}}),
        ("subscribe", {"topic": "jobs.*"}),
        ("unsubscribe", {"topic": "jobs.*"}),
        ("publish", {"topic": "jobs.data", "payload": {"id": 1}}),
        ("join", {"pids": [1, 2]}),
        ("join", {"pids": [3], "timeout_ms": 1000}),
        ("store_artifact", {"path": "a.md", "data": "content"}),
        ("store_artifact", {"path": "a.md", "data": "content", "mime": "text/markdown"}),
        ("fs_read", {"path": "a.md"}),
        ("fs_read", {"path": "a.md", "max_bytes": 100}),
        ("fs_write", {"path": "a.md", "content": "hello"}),
        ("fs_write", {"path": "a.md", "content": "hello", "mime": "text/plain"}),
        ("fs_search", {"query": "q3"}),
        ("fs_search", {"query": "q3", "top_k": 3}),
    ],
)
def test_valid_args_pass(name, good) -> None:
    validate_args(name, good)


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("get_pid", {"pid": 1}),
        ("exit", {"status": 42}),
        ("sleep", {"ms": -1}),
        ("sleep", {}),
        ("resume", {}),
        ("resume", {"pid": 0}),
        ("resume", {"pid": "one"}),
        ("append_context", {"role": "admin", "content": "x"}),
        ("append_context", {"role": "user"}),
        ("write_memory", {}),
        ("write_memory", {"namespace": "", "key": "k", "value": 1}),
        ("write_memory", {"namespace": "n", "key": "k", "value": 1, "kind": "other"}),
        ("write_memory", {"namespace": "n", "key": "k", "value": 1, "tags": [1]}),
        ("read_memory", {"namespace": "ns"}),
        ("search_memory", {}),
        ("search_memory", {"query": ""}),
        ("search_memory", {"top_k": 0}),
        ("search_memory", {"min_score": 1.5}),
        ("forget_memory", {}),
        ("forget_memory", {"namespace": ""}),
        ("summarize_context", {"target_tokens": 0}),
        ("call_tool", {}),
        ("call_tool", {"tool": "fs.read"}),
        ("cancel_tool", {}),
        ("generate", {"temperature": 99}),
        ("generate", {"user": "x" * 4001}),
        ("get_env", {}),
        ("log", {"level": "verbose", "message": "x"}),
        ("log", {"message": "x"}),
        ("log", {"level": "info"}),
        ("send_msg", {}),
        ("send_msg", {"to_pid": 0, "body": {}}),
        ("send_msg", {"to_pid": 1}),  # missing body
        ("send_msg", {"to_pid": 1, "body": {}, "type": "broadcast"}),  # bad type
        ("recv_msg", {}),  # missing timeout
        ("recv_msg", {"timeout_ms": -1}),
        ("recv_msg", {"timeout_ms": 10, "filter": {"bogus": 1}}),
        ("subscribe", {}),
        ("subscribe", {"topic": ""}),
        ("publish", {"topic": "t"}),  # missing payload
        ("publish", {"payload": {}}),  # missing topic
        ("join", {}),  # missing pids
        ("join", {"pids": []}),  # minItems
        ("join", {"pids": [1, 1]}),  # uniqueItems
        ("join", {"pids": ["1"]}),  # not integers
        ("store_artifact", {}),
        ("store_artifact", {"path": "a.md"}),  # missing data
        ("store_artifact", {"data": "x"}),  # missing path
        ("fs_read", {}),
        ("fs_read", {"path": ""}),
        ("fs_write", {"path": "a"}),  # missing content
        ("fs_write", {"content": "x"}),  # missing path
        ("fs_search", {}),
        ("fs_search", {"query": ""}),
        ("fs_search", {"top_k": 101}),
    ],
)
def test_invalid_args_raise_e_inval(name, bad) -> None:
    with pytest.raises(AiosError) as exc:
        validate_args(name, bad)
    assert exc.value.code == E_INVAL


def test_all_registered_syscalls_have_schemas_or_are_optional() -> None:
    """Every implemented syscall must be either strictly schemad or opts out."""
    from aios_kernel import modules  # noqa: F401  (imports register handlers)
    from aios_kernel.syscalls.registry import HANDLERS

    for name in HANDLERS:
        assert name in SCHEMAS, f"missing schema for syscall '{name}'"


def test_registry_rejects_duplicate_handlers() -> None:
    with pytest.raises(RuntimeError):

        @register("get_pid")
        async def _dup(kernel, pid, args):
            return {}


@pytest.mark.asyncio
async def test_unknown_syscall_returns_e_notimpl(kernel, audit_lines) -> None:
    pid = await kernel.spawn_agent({"name": "x", "group_id": "g", "llm": {"model": "mock"}})
    result = await dispatch(kernel, pid, "frobnicate", {})
    assert "error" in result
    assert result["error"]["code"] == "E_NOTIMPL"
    entries = [e for e in audit_lines() if e.get("syscall") == "frobnicate"]
    assert entries and entries[-1]["result"].startswith("error:E_NOTIMPL")
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
        ("read_memory", {"namespace": "ns", "key": "k"}),
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
        ("read_memory", {"namespace": "ns"}),
        ("call_tool", {}),
        ("call_tool", {"tool": "fs.read"}),
        ("cancel_tool", {}),
        ("generate", {"temperature": 99}),
        ("generate", {"user": "x" * 4001}),
        ("get_env", {}),
        ("log", {"level": "verbose", "message": "x"}),
        ("log", {"message": "x"}),
        ("log", {"level": "info"}),
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
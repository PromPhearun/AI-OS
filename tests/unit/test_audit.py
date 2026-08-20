"""Unit tests: audit log — append-only, no secrets, per-syscall entries.

Kernel invariant #5 (Phase 1 scope: append-only JSONL; hash chaining lands
in Phase 3).
"""

from __future__ import annotations

import json

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_every_syscall_is_audited(kernel: Kernel, audit_lines) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="audited"))
    session = await _session(kernel, pid)

    await session.get_pid()
    await session.read_context()

    lines = audit_lines()
    syscalls = [e["syscall"] for e in lines if e.get("event") == "syscall"]
    assert "get_pid" in syscalls
    assert "read_context" in syscalls
    for e in lines:
        if e.get("event") == "syscall":
            assert {"ts", "event", "pid", "syscall", "args_hash", "result", "duration_ms"} <= set(e)
            assert len(e["args_hash"]) == 16


@pytest.mark.asyncio
async def test_audit_never_records_args_values(kernel: Kernel, audit_lines) -> None:
    """Secrets and payloads appear only as hashes, never inline."""
    pid = await kernel.spawn_agent(_base_spec(name="secret-agent"))
    session = await _session(kernel, pid)

    await session.write_memory("agent:1", "api_key", "sk-super-secret-123")
    await session.log("info", "password=hunter2")

    raw = open(kernel.audit.path, encoding="utf-8").read()
    assert "sk-super-secret-123" not in raw
    assert "hunter2" not in raw


@pytest.mark.asyncio
async def test_audit_records_errors_without_message(kernel: Kernel, audit_lines) -> None:
    """Failed syscalls are audited with code + generic message only."""
    pid = await kernel.spawn_agent(_base_spec(name="error-agent"))
    session = await _session(kernel, pid)

    with pytest.raises(Exception):
        await session.call_tool("fs.read", {"path": "/etc/passwd"})

    entries = [e for e in audit_lines() if e.get("event") == "syscall" and e.get("syscall") == "call_tool"]
    assert entries
    assert entries[-1]["result"].startswith("error:")


def test_audit_file_is_jsonl_lines(kernel) -> None:
    """Each line is one complete JSON object (append-only file format)."""
    for line in open(kernel.audit.path, encoding="utf-8"):
        assert line.strip()
        json.loads(line)


async def _session(kernel, pid):
    from aios_sdk.session import AgentSession

    return AgentSession(kernel, pid)
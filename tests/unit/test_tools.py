"""Unit tests: tool registry, grants, sandboxing, and pipeline rules."""

from __future__ import annotations

import os

import pytest

from aios_kernel import Kernel
from aios_kernel.errors import AiosError, E_INVAL, E_NOENT, E_PERM
from aios_kernel.modules.tools import _ws_path, SHELL_ALLOWLIST

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_ws_path_resolves_within_workspace(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="ws"))
    target = _ws_path(kernel, pid, "notes/a.md")
    ws_root = kernel.workspaces.path_for(pid)
    assert os.path.isabs(target)
    assert target.startswith(str(ws_root))


@pytest.mark.parametrize("bad", ["/etc/passwd", "~/x", "..", "../escape", "a/../../b"])
@pytest.mark.asyncio
async def test_ws_path_rejects_escapes(kernel, bad) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="ws-escape"))
    with pytest.raises(AiosError) as exc:
        _ws_path(kernel, pid, bad)
    assert exc.value.code in (E_INVAL, E_PERM)


def test_shell_allowlist_contains_known_binaries() -> None:
    assert {"ls", "cat", "echo", "pwd", "date", "head", "grep"} <= SHELL_ALLOWLIST
    assert "rm" not in SHELL_ALLOWLIST  # destructive binaries excluded by default
    assert "sh" not in SHELL_ALLOWLIST


@pytest.mark.asyncio
async def test_tool_grant_deny_by_default(kernel: Kernel) -> None:
    """An agent with no tool grants must be denied every call_tool."""
    pid = await kernel.spawn_agent(
        _base_spec(name="no-tools", capabilities={"tools": []})
    )
    session = await _session(kernel, pid)
    with pytest.raises(Exception) as exc:
        await session.call_tool("fs.write", {"path": "x.md", "content": "hi"})
    assert exc.type.__name__ == "AiosPermissionError"


@pytest.mark.asyncio
async def test_call_tool_writes_and_reads(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="rw"))
    session = await _session(kernel, pid)
    result = await session.call_tool("fs.write", {"path": "hello.txt", "content": "world"})
    assert result["result"]["bytes"] == 5

    read = await session.call_tool("fs.read", {"path": "hello.txt"})
    assert read["result"]["content"] == "world"
    assert "meta" in read


@pytest.mark.asyncio
async def test_fs_read_missing_file_raises(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="missing"))
    session = await _session(kernel, pid)
    with pytest.raises(Exception) as exc:
        await session.call_tool("fs.read", {"path": "nope.txt"})
    assert exc.type.__name__ == "AiosNoEntError"


@pytest.mark.asyncio
async def test_tool_args_schema_validation(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="schema"))
    session = await _session(kernel, pid)
    # content is required by fs.write
    with pytest.raises(Exception) as exc:
        await session.call_tool("fs.write", {"path": "x.md"})
    assert exc.type.__name__ == "AiosInvalError"


@pytest.mark.asyncio
async def test_list_tools_returns_registry_and_query_filter(kernel: Kernel) -> None:
    pid = await kernel.spawn_agent(_base_spec(name="list"))
    session = await _session(kernel, pid)
    tools = await session.list_tools()
    ids = {t["id"] for t in tools}
    assert {"fs.read", "fs.write", "shell.run"} <= ids

    only_fs = await session.list_tools(query="fs")
    assert all("fs" in t["id"] for t in only_fs)


async def _session(kernel, pid):
    from aios_sdk.session import AgentSession

    return AgentSession(kernel, pid)
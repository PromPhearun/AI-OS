"""Integration tests: built-in tools — fs.read, fs.write, shell.run."""

from __future__ import annotations

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec

# Agent spec granting all three built-in tools
TOOL_SPEC = _base_spec(
    name="tooled",
    capabilities={
        "tools": [
            {"name": "fs.read"},
            {"name": "fs.write"},
            {"name": "shell.run"},
        ]
    },
)


@pytest.mark.asyncio
async def test_shell_run_allowlisted_command(kernel: Kernel, session) -> None:
    sc = await session(TOOL_SPEC)
    result = await sc.call_tool("shell.run", {"command": "echo hello from shell"})
    assert result["result"]["code"] == 0
    assert "hello from shell" in result["result"]["stdout"]


@pytest.mark.asyncio
async def test_shell_run_runs_in_workspace_cwd(kernel: Kernel, session) -> None:
    sc = await session(TOOL_SPEC)
    await sc.call_tool("fs.write", {"path": "probe.txt", "content": "sandbox"})
    # the command sees the agent's workspace files (sandboxed cwd)
    result = await sc.call_tool("shell.run", {"command": "cat probe.txt"})
    assert "sandbox" in result["result"]["stdout"]
    # and does NOT see the repo root
    result = await sc.call_tool("shell.run", {"command": "ls aios_kernel"})
    assert result["result"]["code"] != 0


@pytest.mark.asyncio
async def test_shell_run_rejects_disallowed_binary(kernel: Kernel, session) -> None:
    sc = await session(TOOL_SPEC)
    with pytest.raises(Exception) as exc:
        await sc.call_tool("shell.run", {"command": "rm -rf /"})
    assert exc.type.__name__ == "AiosInvalError"


@pytest.mark.asyncio
async def test_shell_run_rejects_metacharacters(kernel: Kernel, session) -> None:
    sc = await session(TOOL_SPEC)
    for cmd in ["ls; rm -rf /", "cat a.txt && echo x", "echo $(rm -rf /)", "ls | grep x"]:
        with pytest.raises(Exception) as exc:
            await sc.call_tool("shell.run", {"command": cmd})
        assert exc.type.__name__ == "AiosInvalError"
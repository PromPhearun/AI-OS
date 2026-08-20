"""Integration tests: tool scheduler, sandboxes, and the escape suite
(docs/08-security.md §4, §10, §12 acceptance).

Covers: sandbox env (no host secrets; granted vault keys only), network-binary
denial, workspace-confined cwd, path traversal, rate-limit bursts, in-flight
cancellation, and the tool-call budget pre-check.
"""

from __future__ import annotations

import asyncio

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.tools import SHELL_ALLOWLIST, Tool
from aios_sdk.errors import AiosAbortError, AiosBudgetError, AiosBusyError, AiosInvalError, AiosPermissionError

from ..conftest import _base_spec

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
async def test_sandbox_env_does_not_leak_host_secrets(kernel: Kernel, session, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_TEST_SECRET", "topsecret-value")
    sc = await session(TOOL_SPEC)

    # the subprocess inherits no host secrets: no shell, no expansion, no env
    result = await sc.call_tool("shell.run", {"command": "echo $AIOS_TEST_SECRET"})
    assert "topsecret-value" not in result["result"]["stdout"]

    env = kernel.tools.sandbox_env(sc.pid)
    assert "AIOS_TEST_SECRET" not in env


@pytest.mark.asyncio
async def test_sandbox_env_includes_only_granted_vault_keys(kernel: Kernel, session) -> None:
    kernel.vault.set("DB_READONLY_URL", "postgres://ro@db")
    kernel.vault.set("DB_ADMIN_URL", "postgres://admin@db")
    spec = _base_spec(
        name="env-sandbox",
        env={"allowed_keys": ["DB_READONLY_URL"]},
        capabilities={"tools": [{"name": "shell.run"}]},
    )
    sc = await session(spec)
    env = kernel.tools.sandbox_env(sc.pid)
    assert env["DB_READONLY_URL"] == "postgres://ro@db"
    assert "DB_ADMIN_URL" not in env
    assert env["AIOS_PID"] == str(sc.pid)


@pytest.mark.asyncio
async def test_network_and_shell_binaries_not_allowlisted() -> None:
    """Acceptance: sandbox escape — network egress binaries are not available."""
    for binary in ("curl", "wget", "nc", "python", "python3", "sh", "bash", "rm", "sudo"):
        assert binary not in SHELL_ALLOWLIST


@pytest.mark.asyncio
async def test_get_sandbox_reports_profile(kernel: Kernel, session) -> None:
    sc = await session(TOOL_SPEC)
    sb = await sc.get_sandbox()
    assert sb["profile"] == "subprocess"
    assert sb["network"] == "none"
    assert sb["cwd"] == str(kernel.workspaces.path_for(sc.pid))
    assert sb["env_keys"] == []


@pytest.mark.asyncio
async def test_path_traversal_rejected_at_call_tool(kernel: Kernel, session) -> None:
    sc = await session(TOOL_SPEC)
    with pytest.raises(AiosInvalError):
        await sc.call_tool("fs.read", {"path": "/etc/passwd"})
    with pytest.raises(AiosInvalError):
        await sc.call_tool("fs.read", {"path": "../../../etc/passwd"})


@pytest.mark.asyncio
async def test_rate_limit_denies_burst(kernel: Kernel, session) -> None:
    async def _fast(kernel, pid, args):
        return {"output": "ok"}

    kernel.tools.register(
        Tool(
            id="rate.test",
            title="Rate limited",
            description="test tool",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_fast,
            rate_per_min=2,
        )
    )
    sc = await session(_base_spec(name="burst", capabilities={"tools": [{"name": "rate.test"}]}))
    await sc.call_tool("rate.test", {})
    await sc.call_tool("rate.test", {})
    with pytest.raises(AiosBusyError):
        await sc.call_tool("rate.test", {})


@pytest.mark.asyncio
async def test_cancel_tool_aborts_in_flight_call(kernel: Kernel, session) -> None:
    """Acceptance: cancel_tool aborts an in-flight subprocess with E_ABORT."""
    sc = await session(TOOL_SPEC)
    task = asyncio.create_task(sc.call_tool("shell.run", {"command": "sleep 5"}))
    await asyncio.sleep(0.3)
    assert kernel.tools._in_flight, "expected an in-flight tool call"
    call_id = next(iter(kernel.tools._in_flight))
    cancelled = await sc.cancel_tool(call_id)
    assert cancelled["cancelled"] is True
    with pytest.raises(AiosAbortError):
        await task


@pytest.mark.asyncio
async def test_tool_call_budget_precheck(kernel: Kernel, session) -> None:
    spec = _base_spec(
        name="budgeted",
        budgets={"max_tool_calls": 1},
        capabilities={"tools": [{"name": "fs.write"}]},
    )
    sc = await session(spec)
    await sc.call_tool("fs.write", {"path": "a.txt", "content": "x"})
    with pytest.raises(AiosBudgetError):
        await sc.call_tool("fs.write", {"path": "b.txt", "content": "y"})
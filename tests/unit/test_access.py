"""Unit tests: Access Control — permission snapshots, RBAC roles, and
approval tickets (docs/08-security.md §2–3, §12 acceptance)."""

from __future__ import annotations

import asyncio
import json

import pytest

from aios_kernel import Kernel
from aios_kernel.errors import AiosError, E_STATE
from aios_kernel.modules.llm_core import MockLLM
from aios_sdk.errors import AiosBusyError, AiosPermissionError
from aios_sdk.session import AgentSession

from ..conftest import _base_spec

_WRITE_APPROVAL_SPEC = _base_spec(
    name="approval-agent",
    capabilities={"tools": [{"name": "fs.write", "needs_approval": True}]},
)


async def _session(kernel, spec) -> AgentSession:
    pid = await kernel.spawn_agent(spec)
    return AgentSession(kernel, pid)


@pytest.mark.asyncio
async def test_snapshot_computed_at_spawn_and_immutable(kernel: Kernel, session) -> None:
    sc = await session(_base_spec(name="snap"))
    snap = await sc.get_permissions()
    assert snap["role"] == "standard"
    assert snap["operator"] is False
    assert snap["spawn"] is False
    assert snap["tools"]["fs.write"] == {"needs_approval": False, "approved": True, "args": {}}
    assert snap["env"]["allowed_keys"] == []
    # immutable: the agent has no syscall to modify its own snapshot
    again = await sc.get_permissions()
    assert again == snap


@pytest.mark.asyncio
async def test_empty_snapshot_denies_every_privileged_syscall(kernel: Kernel, session) -> None:
    """Acceptance (§12): a syscall with an empty permission snapshot → E_PERM."""
    sc = await session(_base_spec(name="bare", capabilities={"tools": []}))
    with pytest.raises(AiosPermissionError):
        await sc.spawn(_base_spec(name="child"))
    with pytest.raises(AiosPermissionError):
        await sc.get_env("ANYTHING")
    with pytest.raises(AiosPermissionError):
        await sc.call_tool("fs.write", {"path": "x", "content": "y"})


@pytest.mark.asyncio
async def test_get_env_denied_unless_key_granted(kernel: Kernel, session) -> None:
    kernel.vault.set("DB_READONLY_URL", "postgres://ro@db")
    sc = await session(_base_spec(name="env", env={"allowed_keys": ["DB_READONLY_URL"]}))
    assert await sc.get_env("DB_READONLY_URL") == "postgres://ro@db"
    with pytest.raises(AiosPermissionError):
        await sc.get_env("DB_ADMIN_URL")


@pytest.mark.asyncio
async def test_roles_file_in_data_root_merges_role_base_env(tmp_path) -> None:
    roles = {"operator": {"env": ["ROLE_SECRET"]}}
    (tmp_path / "roles.json").write_text(json.dumps(roles), encoding="utf-8")
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        sc = await _session(k, _base_spec(name="op", role="operator"))
        snap = await sc.get_permissions()
        assert snap["operator"] is True
        assert "ROLE_SECRET" in snap["env"]["allowed_keys"]
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_restricted_role_denies_subprocess_tool(kernel: Kernel, session) -> None:
    sc = await session(
        _base_spec(
            name="restricted",
            role="restricted",
            capabilities={"tools": [{"name": "shell.run"}]},
        )
    )
    with pytest.raises(AiosPermissionError):
        await sc.call_tool("shell.run", {"command": "echo hi"})


@pytest.mark.asyncio
async def test_operator_syscalls_require_operator_role(kernel: Kernel, session) -> None:
    sc = await session(_base_spec(name="plain"))
    with pytest.raises(AiosPermissionError):
        await sc.syscall("mcp_list")
    with pytest.raises(AiosPermissionError):
        await sc.syscall("verify_audit")
    with pytest.raises(AiosPermissionError):
        await sc.list_approvals(all=True)

    op = await session(_base_spec(name="op", capabilities={"operator": True}))
    assert (await op.syscall("mcp_list"))["servers"] == []
    assert (await op.syscall("verify_audit"))["valid"] is True
    assert await op.list_approvals(all=True) == []


@pytest.mark.asyncio
async def test_approval_ticket_approve_executes_once(kernel: Kernel, session) -> None:
    from aios_sdk.control import ControlPlane

    sc = await session(_WRITE_APPROVAL_SPEC)
    cp = ControlPlane(kernel)

    # approval-required tool denies until a ticket is approved
    with pytest.raises(AiosPermissionError):
        await sc.call_tool("fs.write", {"path": "a.txt", "content": "x"})

    ticket = await sc.request_permission(
        "fs.write", {"path": "a.txt", "content": "x"}, reason="need it"
    )
    assert ticket["status"] == "pending"
    assert (await sc.list_approvals())[0]["ticket_id"] == ticket["ticket_id"]

    assert (await cp.approve(ticket["ticket_id"]))["status"] == "approved"
    result = await sc.call_tool("fs.write", {"path": "a.txt", "content": "x"})
    assert result["result"]["bytes"] == 1

    # one-shot: a second call needs a fresh ticket
    with pytest.raises(AiosPermissionError):
        await sc.call_tool("fs.write", {"path": "b.txt", "content": "y"})


@pytest.mark.asyncio
async def test_approval_ticket_deny_blocks_execution(kernel: Kernel, session) -> None:
    from aios_sdk.control import ControlPlane

    sc = await session(_WRITE_APPROVAL_SPEC)
    cp = ControlPlane(kernel)

    ticket = await sc.request_permission("fs.write", {"path": "a.txt", "content": "x"})
    assert cp.deny(ticket["ticket_id"])["status"] == "denied"
    with pytest.raises(AiosPermissionError):
        await sc.call_tool("fs.write", {"path": "a.txt", "content": "x"})


@pytest.mark.asyncio
async def test_approval_ticket_expires(tmp_path, monkeypatch) -> None:
    from aios_sdk.control import ControlPlane

    monkeypatch.setenv("AIOS_APPROVAL_TTL_S", "0.05")
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        sc = await _session(k, _WRITE_APPROVAL_SPEC)
        ticket = await sc.request_permission("fs.write", {"path": "a.txt", "content": "x"})
        await asyncio.sleep(0.12)
        with pytest.raises(AiosError) as exc:
            await ControlPlane(k).approve(ticket["ticket_id"])
        assert exc.value.code == E_STATE
        with pytest.raises(AiosPermissionError):
            await sc.call_tool("fs.write", {"path": "a.txt", "content": "x"})
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_max_pending_approval_tickets(kernel: Kernel, session) -> None:
    spec = _base_spec(
        name="pending",
        capabilities={"tools": [{"name": "fs.write"}]},
        approvals={"max_pending": 1},
    )
    sc = await session(spec)
    await sc.request_permission("fs.write", {"path": "a", "content": "x"})
    with pytest.raises(AiosBusyError):
        await sc.request_permission("fs.write", {"path": "b", "content": "y"})


@pytest.mark.asyncio
async def test_request_permission_for_ungranted_tool_denied(kernel: Kernel, session) -> None:
    sc = await session(_base_spec(name="ungranted", capabilities={"tools": []}))
    with pytest.raises(AiosPermissionError):
        await sc.request_permission("fs.write", {"path": "a", "content": "x"})
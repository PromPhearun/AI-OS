"""Access Control — resolved permission snapshots, RBAC roles, approval tickets.

Phase 3 (docs/08-security.md §2–3, §6):

  * **Permission snapshots** — each agent's effective permission set (role base
    capabilities ∪ spec-declared grants) is computed once at spawn / ``--resume``
    restore and is **immutable for the agent's lifetime** (no syscall can modify
    it; agents cannot elevate themselves).
  * **RBAC roles** — loaded from ``roles.json`` (``AIOS_ROLES_PATH`` or the
    kernel's data root); the four canonical roles are always present and a
    custom file merges over them. ``operator`` is the only role that may
    approve tickets, register MCP servers, or verify the audit log.
  * **Approval tickets** — ``request_permission`` enqueues a ticket for a
    granted tool; execution of approval-required tools is deferred until an
    operator approves (one-shot, then consumed) or the ticket expires
    (default 10 min, ``AIOS_APPROVAL_TTL_S``).
  * **Dispatch gate** — ``check_syscall`` runs before every handler so
    privileged syscalls deny by default with ``E_PERM`` (no entry ⇒ denial).

The per-syscall permission model mirrors the resolved snapshot in
docs/08-security.md §3. Tools are checked here; memory/IPC enforce their own
grants in their owning modules (deny by default, unchanged from Phase 2).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from ..acb import AgentState
from ..errors import AiosError, E_BUSY, E_NOENT, E_PERM, E_STATE
from ..syscalls.registry import args_hash, register

DEFAULT_APPROVAL_TTL_S = 600.0


def _approval_ttl() -> float:
    """Read the operator-configurable ticket TTL at request time so tests and
    deployments can tune it per process without re-importing the module."""
    try:
        return float(os.environ.get("AIOS_APPROVAL_TTL_S", str(DEFAULT_APPROVAL_TTL_S)))
    except ValueError:
        return DEFAULT_APPROVAL_TTL_S

DEFAULT_ROLES: dict[str, dict] = {
    "operator": {
        "operator": True,
        "spawn": True,
        "tools": [],
        "env": [],
    },
    "standard": {
        "operator": False,
        "spawn": False,
        "tools": [],
        "env": [],
    },
    "restricted": {
        "operator": False,
        "spawn": False,
        "tools": [],
        "env": [],
        "deny_tools": ["shell.run"],  # no subprocess execution
    },
    "service": {
        "operator": False,
        "spawn": False,
        "tools": [],
        "env": [],
    },
}

# Syscalls gated on the caller's resolved snapshot (deny by default).
# Everything else is enforced by the owning kernel module (tools/memory/ipc).
_ENV_SYSCALLS = {"get_env"}
_SPAWN_SYSCALLS = {"spawn", "resume"}
_OPERATOR_SYSCALLS = {
    "approve_ticket",
    "deny_ticket",
    "verify_audit",
    "mcp_register",
    "mcp_unregister",
    "mcp_list",
}


def _ticket_id() -> str:
    return f"apr-{uuid.uuid4().hex[:12]}"


def _normalize_tool_grant(g: dict) -> dict:
    return {
        "needs_approval": bool(g.get("needs_approval")),
        "approved": bool(g.get("approved", True)),
        "args": dict(g.get("args") or {}),
    }


class AccessManager:
    """Owns permission snapshots, RBAC roles, and approval tickets."""

    def __init__(self, kernel):
        self.kernel = kernel
        self._roles: dict[str, dict] = self._load_roles()
        self._snapshots: dict[int, dict] = {}
        self._tickets: dict[str, dict] = {}
        self._approved: dict[tuple[int, str], str] = {}  # (pid, tool) -> ticket_id

    # ---------------------------------------------------------------- roles
    def _load_roles(self) -> dict[str, dict]:
        path = os.environ.get("AIOS_ROLES_PATH")
        if not path and self.kernel.data_root is not None:
            candidate = Path(self.kernel.data_root) / "roles.json"
            if candidate.exists():
                path = str(candidate)
        roles = dict(DEFAULT_ROLES)
        if not path:
            return roles
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            return roles  # fail closed with the built-in defaults
        if not isinstance(loaded, dict):
            return roles
        for name, body in loaded.items():
            if not isinstance(body, dict):
                continue
            base = roles.get(name, {})
            merged = {**base, **body}
            merged["tools"] = list(base.get("tools", [])) + list(body.get("tools", []) or [])
            merged["env"] = list(base.get("env", [])) + list(body.get("env", []) or [])
            roles[name] = merged
        return roles

    # -------------------------------------------------------------- snapshots
    def _snapshot_for(self, spec: dict) -> dict:
        cap = spec.get("capabilities") or {}
        role_name = str(spec.get("role") or "standard")
        role = self._roles.get(role_name, self._roles["standard"])
        tools: dict[str, dict] = {}
        for g in list(role.get("tools", [])) + list(cap.get("tools", []) or []):
            if not isinstance(g, dict) or not g.get("name"):
                continue
            if g["name"] in tools:
                continue  # spec-declared grants win over role base grants
            tools[g["name"]] = _normalize_tool_grant(g)
        allowed_keys: list[str] = []
        for key in list(role.get("env", [])) + list((spec.get("env") or {}).get("allowed_keys", []) or []):
            if key not in allowed_keys:
                allowed_keys.append(str(key))
        approvals_cfg = spec.get("approvals") or {}
        return {
            "role": role_name,
            "operator": bool(role.get("operator") or cap.get("operator")),
            "spawn": bool(role.get("spawn") or cap.get("operator") or cap.get("spawn")),
            "tools": tools,
            "env": {"allowed_keys": allowed_keys},
            "approvals": {"pending": 0, "max_pending": int(approvals_cfg.get("max_pending", 3))},
            "deny_tools": list(role.get("deny_tools", [])),
            "created_at": time.time(),
        }

    def spawn(self, pid: int, spec: dict) -> None:
        """Compute and freeze an agent's permission snapshot at spawn/restore."""
        snap = self._snapshot_for(spec)
        self._snapshots[pid] = snap
        self.kernel.audit.record(
            "access.spawn", pid=pid, role=snap["role"], operator=snap["operator"]
        )

    def restore(self, pid: int, spec: dict) -> None:
        self.spawn(pid, spec)

    def remove(self, pid: int) -> None:
        """Drop an agent's snapshot; cancel its pending tickets."""
        self._snapshots.pop(pid, None)
        for tid, ticket in self._tickets.items():
            if ticket["pid"] == pid and ticket["status"] == "pending":
                ticket["status"] = "cancelled"
        self._approved = {k: v for k, v in self._approved.items() if k[0] != pid}

    def snapshot(self, pid: int) -> dict:
        snap = self._snapshots.get(pid)
        if snap is None:
            raise AiosError(E_NOENT, f"agent {pid} has no permission snapshot")
        return dict(snap)

    # ----------------------------------------------------------- dispatch gate
    def check_syscall(self, pid: int, name: str, args: dict) -> None:
        """Deny-by-default gate; runs before every syscall handler.

        Raises ``AiosError(E_PERM)`` when the caller's resolved snapshot does
        not authorize the syscall. Other syscalls are enforced by their owning
        modules (tools/memory/ipc keep their Phase 2 deny-by-default grants).
        """
        snap = self._snapshots.get(pid)
        if snap is None:
            raise AiosError(E_PERM, f"agent {pid} has no permission snapshot")
        if name in _ENV_SYSCALLS:
            key = args.get("key", "")
            if key not in snap["env"]["allowed_keys"]:
                raise AiosError(E_PERM, f"agent {pid} is not allowed env key '{key}'")
        elif name in _SPAWN_SYSCALLS:
            if not snap["spawn"]:
                raise AiosError(E_PERM, f"agent {pid} has no spawn capability")
        elif name in _OPERATOR_SYSCALLS:
            if not snap["operator"]:
                raise AiosError(E_PERM, f"agent {pid} is not an operator")
        elif name == "list_approvals":
            if args.get("all") and not snap["operator"]:
                raise AiosError(E_PERM, f"agent {pid} is not an operator")

    # ---------------------------------------------------------------- tools
    def check_tool(self, pid: int, tool_id: str) -> dict:
        """Return the resolved grant for ``tool_id`` or raise ``E_PERM``."""
        snap = self._snapshots.get(pid)
        if snap is None:
            raise AiosError(E_PERM, f"agent {pid} has no permission snapshot")
        if tool_id in snap["deny_tools"]:
            raise AiosError(E_PERM, f"agent {pid} is denied tool '{tool_id}' by its role")
        grant = snap["tools"].get(tool_id)
        if grant is None:
            raise AiosError(E_PERM, f"agent {pid} is not granted tool '{tool_id}'")
        return dict(grant)

    def tool_needs_approval(self, pid: int, tool_id: str) -> bool:
        grant = self.check_tool(pid, tool_id)
        return bool(grant.get("needs_approval")) or grant.get("approved") is False

    # -------------------------------------------------------- approval tickets
    def _expire(self) -> None:
        now = time.time()
        for tid, ticket in list(self._tickets.items()):
            if ticket["status"] == "pending" and now > ticket["expires_at"]:
                ticket["status"] = "expired"
                self._approved.pop((ticket["pid"], ticket["tool"]), None)
                snap = self._snapshots.get(ticket["pid"])
                if snap is not None:
                    snap["approvals"]["pending"] = max(0, snap["approvals"]["pending"] - 1)

    def has_pending(self, pid: int) -> bool:
        """True when ``pid`` holds at least one unresolved approval ticket."""
        return any(t["pid"] == pid and t["status"] == "pending" for t in self._tickets.values())

    def request_permission(self, pid: int, tool: str, args: dict, reason: str | None = None) -> dict:
        self._expire()
        snap = self._snapshot(pid)
        if tool not in snap["tools"]:
            raise AiosError(E_PERM, f"agent {pid} is not granted tool '{tool}'")
        pending = snap["approvals"]["pending"]
        if pending >= snap["approvals"]["max_pending"]:
            raise AiosError(E_BUSY, f"agent {pid} already has {pending} pending approval requests")
        now = time.time()
        ticket = {
            "ticket_id": _ticket_id(),
            "pid": pid,
            "tool": tool,
            "args": dict(args or {}),
            "reason": reason or "",
            "status": "pending",
            "created_at": now,
            "expires_at": now + _approval_ttl(),
        }
        self._tickets[ticket["ticket_id"]] = ticket
        snap["approvals"]["pending"] = pending + 1
        self.kernel.audit.record(
            "approval.request",
            pid=pid,
            ticket=ticket["ticket_id"],
            tool=tool,
            args_hash=args_hash(args or {}),
            reason_hash=args_hash({"reason": reason}) if reason else None,
        )
        return {
            "ticket_id": ticket["ticket_id"],
            "status": "pending",
            "expires_at": ticket["expires_at"],
        }

    async def approve(self, ticket_id: str, *, by_pid: int | None = None) -> dict:
        """Approve a pending ticket and, if the owning agent parked itself
        while awaiting the decision, bring it back so it can consume the
        approval (docs/08-security.md §7 human gates)."""
        ticket = self._require_pending(ticket_id)
        ticket["status"] = "approved"
        ticket["approved_at"] = time.time()
        self._approved[ticket["pid"], ticket["tool"]] = ticket_id
        snap = self._snapshots.get(ticket["pid"])
        if snap is not None:
            snap["approvals"]["pending"] = max(0, snap["approvals"]["pending"] - 1)
        self.kernel.audit.record(
            "approval.approve",
            pid=ticket["pid"],
            ticket=ticket_id,
            tool=ticket["tool"],
            operator=by_pid,
        )
        await self._maybe_resume_agent(ticket["pid"])
        return {"ticket_id": ticket_id, "status": "approved", "tool": ticket["tool"]}

    async def _maybe_resume_agent(self, pid: int) -> None:
        """Resume a SUSPENDED agent that was parked awaiting this approval.

        Fails closed: if the agent is already running, was reaped, or has no
        checkpoint to resume from, the approval itself still stands.
        """
        acb = self.kernel.agent_manager.peek(pid)
        if acb is None or acb.state is not AgentState.SUSPENDED:
            return
        try:
            await self.kernel.agent_manager.resume(pid)
        except AiosError:
            return

    def deny(self, ticket_id: str, *, by_pid: int | None = None) -> dict:
        ticket = self._require_pending(ticket_id)
        ticket["status"] = "denied"
        ticket["denied_at"] = time.time()
        snap = self._snapshots.get(ticket["pid"])
        if snap is not None:
            snap["approvals"]["pending"] = max(0, snap["approvals"]["pending"] - 1)
        self.kernel.audit.record(
            "approval.deny", pid=ticket["pid"], ticket=ticket_id, tool=ticket["tool"], operator=by_pid
        )
        return {"ticket_id": ticket_id, "status": "denied", "tool": ticket["tool"]}

    def consume_approval(self, pid: int, tool: str) -> bool:
        """One-shot consumption of an approved, unexpired ticket. Returns True
        when execution is authorized (the ticket is consumed either way)."""
        key = (pid, tool)
        tid = self._approved.get(key)
        if tid is None:
            return False
        self._approved.pop(key, None)
        ticket = self._tickets.get(tid)
        if ticket is None or ticket["status"] != "approved":
            return False
        if time.time() > ticket["expires_at"]:
            ticket["status"] = "expired"
            return False
        return True

    def list_tickets(self, *, pid: int | None = None, all: bool = False) -> list[dict]:
        self._expire()
        tickets = self._tickets.values()
        if not all:
            tickets = [t for t in tickets if pid is None or t["pid"] == pid]
        return [
            {k: v for k, v in t.items() if k != "args"}  # args stay kernel-internal
            for t in sorted(tickets, key=lambda t: t["created_at"])
        ]

    def _require_pending(self, ticket_id: str) -> dict:
        self._expire()
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise AiosError(E_NOENT, f"no such approval ticket: {ticket_id}")
        if ticket["status"] != "pending":
            raise AiosError(E_STATE, f"ticket {ticket_id} is {ticket['status']}, not pending")
        return ticket

    def _snapshot(self, pid: int) -> dict:
        snap = self._snapshots.get(pid)
        if snap is None:
            raise AiosError(E_NOENT, f"agent {pid} has no permission snapshot")
        return snap


# ------------------------------------------------------------------ syscalls
@register("get_permissions")
async def _get_permissions(kernel, pid: int, args: dict) -> dict:
    return kernel.access.snapshot(pid)


@register("request_permission")
async def _request_permission(kernel, pid: int, args: dict) -> dict:
    return kernel.access.request_permission(
        pid, args["tool"], args.get("args") or {}, args.get("reason")
    )


@register("list_approvals")
async def _list_approvals(kernel, pid: int, args: dict) -> dict:
    return {"tickets": kernel.access.list_tickets(pid=pid, all=bool(args.get("all")))}


@register("approve_ticket")
async def _approve_ticket(kernel, pid: int, args: dict) -> dict:
    return await kernel.access.approve(args["ticket_id"], by_pid=pid)


@register("deny_ticket")
async def _deny_ticket(kernel, pid: int, args: dict) -> dict:
    return kernel.access.deny(args["ticket_id"], by_pid=pid)
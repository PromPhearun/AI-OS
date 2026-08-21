"""REST control plane — docs/10-ui.md §4.

Mirrors the host-side control SDK (aios_sdk.control.ControlPlane) over HTTP/JSON.
Every endpoint is rate-limited by middleware, audited by the
``ControlAuditMiddleware``, and returns kernel error envelopes on failure.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from aios_kernel.errors import AiosError, E_NOENT
from aios_sdk.agent import AGENT_REGISTRY, AgentRunner

from .auth import Principal
from .deps import get_kernel, require_auth, require_operator

router = APIRouter()

# ------------------------------------------------------------------ schemas
class TokenRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=256)


class AgentLaunch(BaseModel):
    spec: dict[str, Any]


class AgentAction(BaseModel):
    action: Literal["suspend", "resume", "kill"]
    reason: str | None = None


class OperatorMessage(BaseModel):
    body: dict[str, Any]
    type: str = "direct"
    priority: int = Field(default=50, ge=0, le=100)
    ttl_s: float | None = Field(default=None, gt=0, le=86400)


class FsSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=512)
    pid: int
    top_k: int = Field(default=5, ge=1, le=50)


# ------------------------------------------------------------------ helpers
def _ps(kernel) -> list[dict]:
    rows = []
    for pid in sorted(kernel.agent_manager._table):
        rec = kernel.agent_manager.record(pid)
        if rec is not None:
            rows.append(rec)
    for pid in sorted(kernel.agent_manager._reaped):
        rec = kernel.agent_manager.record(pid)
        if rec is not None:
            rows.append(rec)
    return rows


def _agent_or_404(kernel, pid: int) -> dict:
    rec = kernel.agent_manager.record(pid)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "E_NOENT", "message": f"no such agent: {pid}"}},
        )
    return rec


# ------------------------------------------------------------------ public
@router.get("/v1/health")
async def health(kernel=Depends(get_kernel)):
    """Liveness + minimal kernel info (no sensitive data)."""
    return {
        "status": "ok",
        "service": "aios-control",
        "agents": kernel.agent_manager.count(),
    }


@router.post("/v1/auth/token")
async def issue_token(req: TokenRequest, request: Request):
    """Exchange an API key for a short-lived JWT (web desktop / WebSocket)."""
    principal = request.app.state.auth.authenticate(req.api_key)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "E_PERM", "message": "invalid api key"}},
        )
    request.state.principal = principal
    token = request.app.state.auth.issue_token(principal)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": request.app.state.auth._jwt_ttl_s,
        "role": principal.role,
        "name": principal.name,
    }


# ------------------------------------------------------------------ agents
@router.post("/v1/agents", status_code=status.HTTP_201_CREATED)
async def launch_agent(
    req: AgentLaunch,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    """Launch one agent (spec name must resolve to a registered runner)."""
    definition = AGENT_REGISTRY.get(req.spec.get("name"))
    if definition is None:
        raise AiosError(
            E_NOENT, f"no @agent registered for spec name '{req.spec.get('name')}'"
        )
    factory = lambda pid: AgentRunner(kernel, pid, definition["turn"]).run()
    pid = await kernel.spawn_agent(req.spec, runner_factory=factory)
    kernel.audit.record(
        "control.launch", pid=pid, spec_name=req.spec.get("name"), by=principal.name
    )
    return {"pid": pid, "agent": kernel.agent_manager.record(pid)}


@router.get("/v1/agents")
async def list_agents(
    state: str | None = Query(default=None),
    group: str | None = Query(default=None),
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    rows = _ps(kernel)
    if state is not None:
        rows = [r for r in rows if r.get("state") == state]
    if group is not None:
        rows = [r for r in rows if r.get("group_id") == group]
    return {"agents": rows, "count": len(rows)}


@router.get("/v1/agents/{pid}")
async def get_agent(
    pid: int, kernel=Depends(get_kernel), principal: Principal = Depends(require_auth)
):
    return _agent_or_404(kernel, pid)


@router.patch("/v1/agents/{pid}")
async def agent_action(
    pid: int,
    req: AgentAction,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    if req.action == "suspend":
        result = await kernel.scheduler.suspend(pid, req.reason or "operator")
    elif req.action == "resume":
        result = await kernel.agent_manager.resume(pid)
    else:  # kill
        kernel.agent_manager.kill(pid, reason=req.reason or "killed by operator")
        result = {"ok": True}
    kernel.audit.record("control.action", pid=pid, action=req.action, by=principal.name)
    return {"pid": pid, "action": req.action, **result}


@router.delete("/v1/agents/{pid}")
async def kill_agent(
    pid: int,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    if pid not in kernel.agent_manager._table:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "E_NOENT", "message": f"no such agent: {pid}"}},
        )
    kernel.agent_manager.kill(pid, reason="killed via control plane")
    kernel.audit.record("control.kill", pid=pid, by=principal.name)
    return {"pid": pid, "ok": True}


@router.post("/v1/agents/{pid}/messages")
async def operator_message(
    pid: int,
    req: OperatorMessage,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    """Human-in-the-loop: operator -> agent over IPC (docs/06-ipc.md §9.4)."""
    msg = kernel.ipc.send_from_operator(
        pid, req.body, type=req.type, priority=req.priority, ttl_s=req.ttl_s
    )
    return {"msg_id": msg.msg_id, "delivered": True, "to_pid": pid}


@router.get("/v1/agents/{pid}/logs")
async def agent_logs(
    pid: int,
    limit: int = Query(default=200, ge=1, le=2000),
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    lines = list(kernel.agent_logs.get(pid, []))
    return {"pid": pid, "lines": lines[-limit:], "count": len(lines)}
# ------------------------------------------------------------------ scheduler
@router.get("/v1/scheduler")
async def scheduler_snapshot(
    kernel=Depends(get_kernel), principal: Principal = Depends(require_auth)
):
    return kernel.scheduler.snapshot()


# ------------------------------------------------------------------ llm status
@router.get("/v1/llm")
async def llm_status(
    kernel=Depends(get_kernel), principal: Principal = Depends(require_auth)
):
    """Provider failover health (Phase 4): state, retries, fallback routing."""
    return {"providers": kernel.llm.provider_status()}


# ------------------------------------------------------------------ approvals
@router.get("/v1/approvals")
async def list_approvals(
    kernel=Depends(get_kernel), principal: Principal = Depends(require_operator)
):
    return {"tickets": kernel.access.list_tickets(all=True)}


@router.post("/v1/approvals/{ticket_id}/approve")
async def approve_ticket(
    ticket_id: str,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_operator),
):
    return await kernel.access.approve(ticket_id)


@router.post("/v1/approvals/{ticket_id}/deny")
async def deny_ticket(
    ticket_id: str,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_operator),
):
    return kernel.access.deny(ticket_id)


# ------------------------------------------------------------------ tools / mcp
@router.get("/v1/tools")
async def list_tools(
    query: str | None = Query(default=None, max_length=128),
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    return {"tools": kernel.tools.list(query), "count": len(kernel.tools._tools)}


@router.get("/v1/mcp/servers")
async def list_mcp_servers(
    kernel=Depends(get_kernel), principal: Principal = Depends(require_auth)
):
    return {"servers": kernel.mcp.list_servers()}


# ------------------------------------------------------------------ filesystem
@router.post("/v1/fs/search")
async def fs_search(
    req: FsSearchRequest,
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_auth),
):
    hits = await kernel.fs.search(req.pid, req.query, top_k=req.top_k)
    return {"query": req.query, "pid": req.pid, "hits": hits, "count": len(hits)}


# ------------------------------------------------------------------ audit
@router.get("/v1/audit")
async def audit_query(
    event: str | None = Query(default=None, max_length=128),
    pid: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=5000),
    kernel=Depends(get_kernel),
    principal: Principal = Depends(require_operator),
):
    entries = kernel.audit.read(pid=pid, limit=limit)
    if event is not None:
        entries = [e for e in entries if e.get("event") == event]
    return {"entries": entries, "count": len(entries)}


@router.get("/v1/audit/verify")
async def audit_verify(
    kernel=Depends(get_kernel), principal: Principal = Depends(require_operator)
):
    return kernel.audit.verify()
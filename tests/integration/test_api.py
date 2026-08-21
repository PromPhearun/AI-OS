"""Integration tests: the aios_api control plane (FastAPI REST + WebSocket).

Phase 4 surface from docs/10-ui.md: JWT/API-key auth, RBAC, agent lifecycle
over REST (including suspend/resume of a *blocked* agent), WebSocket
console/feed, audit integrity with the no-secrets guarantee, security
headers, and rate limiting.

Regression coverage locked in here:

* suspend of an agent blocked in ``recv_msg`` must return promptly — it once
  hung the event loop (the blocked syscall unwound with a benign ``state``
  result the turn function retried in a tight CPU spin);
* kill of a blocked agent must record a ``terminated`` tombstone (not the
  stale ``blocked`` state);
* reaped agents return 404 on suspend/kill instead of 500.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from aios import agent
from aios_api import create_app
from aios_kernel import Kernel
from examples.agents import RESEARCHER_SPEC

ALPHA_KEY = "alpha-secret"
BRAVO_KEY = "bravo-secret"
API_KEYS_ENV = f"alice:{ALPHA_KEY}:operator,bob:{BRAVO_KEY}:standard"
H = {"x-api-key": ALPHA_KEY}

BLOCKER_SPEC = {
    "name": "blocker",
    "version": "1",
    "description": "Blocks on recv_msg (API lifecycle integration).",
    "group_id": "integration",
    "priority": 0,
    "llm": {"model": "mock", "system": "", "temperature": 0.0},
    "budgets": {"max_turns": 50, "max_tool_calls": 50},
    "capabilities": {"tools": []},
}


@agent(name="blocker")
async def blocker_turn(sc):
    await sc.log("info", "blocker ready")
    while True:
        reply = await sc.recv_msg(30000)
        msg = reply.get("msg")
        if msg is None:
            continue
        await sc.log("info", f"blocker got: {msg.get('body', {})}")
        if msg.get("body", {}).get("text") == "exit":
            return True


@pytest.fixture
def api(kernel, monkeypatch):
    """A control-plane app wired to the shared fresh kernel."""
    monkeypatch.setenv("AIOS_API_KEYS", API_KEYS_ENV)
    monkeypatch.setenv("AIOS_JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
    return create_app(kernel, agents_module="examples.agents", rate_limit=120)


@pytest.fixture
def client(api):
    with TestClient(api) as c:
        yield c


def _token(client) -> str:
    r = client.post("/v1/auth/token", json={"api_key": ALPHA_KEY})
    assert r.status_code == 200
    return r.json()["access_token"]


# ---------------------------------------------------------------- auth/RBAC
def test_dev_key_enabled_without_api_keys_env(monkeypatch, tmp_path) -> None:
    """`aios serve` with no AIOS_API_KEYS boots with the documented dev key."""
    from aios_api.auth import DEV_API_KEY

    monkeypatch.delenv("AIOS_API_KEYS", raising=False)
    app2 = create_app(
        Kernel(data_root=str(tmp_path / "dev-key")),
        agents_module="examples.agents",
    )
    with TestClient(app2) as c:
        r = c.post("/v1/auth/token", json={"api_key": DEV_API_KEY})
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "operator"
        assert body["name"] == "dev"
        # The issued JWT authenticates a control call.
        token = body["access_token"]
        r2 = c.get("/v1/scheduler", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200
        assert "queues" in r2.json()


def test_health_and_auth(client) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    assert client.post("/v1/auth/token", json={"api_key": "wrong-key"}).status_code == 401

    r = client.post("/v1/auth/token", json={"api_key": ALPHA_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "operator"
    assert body["access_token"]
    token = body["access_token"]

    # No credentials -> 401; either credential shape works; garbage is rejected.
    assert client.get("/v1/agents").status_code == 401
    assert client.get("/v1/agents", headers=H).status_code == 200
    assert (
        client.get("/v1/agents", headers={"authorization": f"Bearer {token}"}).status_code
        == 200
    )
    assert (
        client.get("/v1/agents", headers={"authorization": "Bearer garbage"}).status_code
        == 401
    )


def test_rbac_audit_operator_only(client) -> None:
    # `standard` role is denied the audit trail; `operator` may read it.
    assert client.get("/v1/audit", headers={"x-api-key": BRAVO_KEY}).status_code == 403
    r = client.get("/v1/audit", headers=H)
    assert r.status_code == 200
    assert "entries" in r.json()


# -------------------------------------------------------- control endpoints
def test_control_endpoints(client) -> None:
    assert "agents" in client.get("/v1/scheduler", headers=H).json()
    assert "providers" in client.get("/v1/llm", headers=H).json()
    assert "tools" in client.get("/v1/tools", headers=H).json()
    assert "servers" in client.get("/v1/mcp/servers", headers=H).json()
    r = client.get("/v1/approvals", headers=H)
    assert r.status_code == 200
    assert isinstance(r.json()["tickets"], list)
# --------------------------------------------------------------- lifecycle
def test_agent_lifecycle_suspend_resume_kill(client) -> None:
    token = _token(client)

    # A short-lived echo agent finishes on its own and is reaped.
    r = client.post("/v1/agents", json={"spec": RESEARCHER_SPEC}, headers=H)
    assert r.status_code == 201
    pid = r.json()["pid"]
    assert isinstance(pid, int)
    assert client.get(f"/v1/agents/{pid}", headers=H).status_code == 200
    assert client.get("/v1/agents/999999", headers=H).status_code == 404

    time.sleep(0.5)  # researcher runs its few turns, then exits

    # Finished agents are reaped -> control ops must 404 cleanly, not 500.
    r = client.patch(f"/v1/agents/{pid}", json={"action": "suspend"}, headers=H)
    assert r.status_code == 404
    r = client.delete(f"/v1/agents/{pid}", headers=H)
    assert r.status_code == 404

    # Blocker: launch -> BLOCKED -> suspend (must not hang) -> resume.
    r = client.post("/v1/agents", json={"spec": BLOCKER_SPEC}, headers=H)
    assert r.status_code == 201
    bpid = r.json()["pid"]
    assert isinstance(bpid, int)

    time.sleep(0.4)  # let it park inside recv_msg
    rec = client.get(f"/v1/agents/{bpid}", headers=H)
    assert rec.status_code == 200
    assert rec.json()["state"] == "blocked"

    r = client.patch(
        f"/v1/agents/{bpid}",
        json={"action": "suspend", "reason": "integration"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["action"] == "suspend"
    assert r.json()["checkpoint_id"]

    r = client.patch(f"/v1/agents/{bpid}", json={"action": "resume"}, headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Operator message wakes the resumed turn; it logs what it received.
    r = client.post(
        f"/v1/agents/{bpid}/messages",
        json={"body": {"text": "operator override"}, "type": "direct", "priority": 90},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["delivered"] is True

    time.sleep(0.4)
    lines = client.get(f"/v1/agents/{bpid}/logs", headers=H).json()["lines"]
    assert any("blocker got" in ln.get("message", "") for ln in lines)

    # Console WebSocket streams the agent's log lines.
    with client.websocket_connect(f"/v1/agents/{bpid}/ws/console?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "console"

    # Kill -> tombstone records `terminated` (used to keep the stale `blocked`).
    r = client.delete(f"/v1/agents/{bpid}", headers=H)
    assert r.status_code == 200
    tomb = client.get(f"/v1/agents/{bpid}", headers=H)
    assert tomb.status_code == 200
    assert tomb.json()["state"] == "terminated"
    assert tomb.json()["exit_status"] == "killed"
    assert tomb.json()["exit_message"] == "killed via control plane"

    assert client.delete(f"/v1/agents/{bpid}", headers=H).status_code == 404


# ------------------------------------------------------------ audit integrity
def test_audit_integrity_and_no_secrets(kernel, client) -> None:
    """Control requests are audit-logged; credentials never appear in them."""
    client.get("/v1/agents", headers=H)
    r = client.post("/v1/auth/token", json={"api_key": ALPHA_KEY})
    token = r.json()["access_token"]

    r = client.get("/v1/audit/verify", headers=H)
    assert r.status_code == 200
    assert r.json()["valid"] is True

    ctrl = [e for e in kernel.audit.read() if e.get("event") == "control.request"]
    assert len(ctrl) >= 3

    leak = [
        e
        for e in ctrl
        if any(
            secret in str(e) for secret in (ALPHA_KEY, BRAVO_KEY, token, "garbage")
        )
    ]
    assert leak == []


# ---------------------------------------------------------- security headers
def test_security_headers(client) -> None:
    r = client.get("/v1/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("content-security-policy", "").startswith("default-src 'none'")


# ----------------------------------------------------------- interactive docs
def test_docs_ui_servable_with_usable_csp(client) -> None:
    """/docs must be viewable in a browser: it keeps the security headers but
    gets a narrower CSP (the strict default-src 'none' policy would blank the
    Swagger page by blocking its CDN assets and inline boot script)."""
    r = client.get("/docs")
    assert r.status_code == 200
    assert r.headers.get("x-frame-options") == "DENY"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "cdn.jsdelivr.net" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    # The OpenAPI schema the Swagger page fetches gets the same policy.
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "connect-src 'self'" in r.headers.get("content-security-policy", "")

    # API routes keep the full strict policy.
    r = client.get("/v1/health")
    assert r.headers.get("content-security-policy", "") == (
        "default-src 'none'; frame-ancestors 'none'"
    )


# --------------------------------------------------------------- websocket feed
def test_ws_feed(client) -> None:
    token = _token(client)
    with client.websocket_connect(f"/v1/ws/feed?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] in ("audit", "scheduler", "processes")


# --------------------------------------------------------------- rate limiting
def test_rate_limiting(kernel, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_API_KEYS", API_KEYS_ENV)
    app2 = create_app(kernel, rate_limit=3, rate_window_s=60)
    with TestClient(app2) as c:
        for _ in range(3):
            assert c.get("/v1/health").status_code == 200
        r = c.get("/v1/health")
        assert r.status_code == 429
        assert "retry-after" in r.headers

# --------------------------------------------------------------- web desktop
def test_web_desktop_served_when_built(client) -> None:
    """The built React/TS desktop (web/dist) is served same-origin at `/` with
    a narrowed CSP (own scripts/styles + same-origin REST/WS); hashed assets get
    the same policy; API routes keep the strict policy; unknown /v1 paths are
    API 404s (not the SPA shell); and shell/asset requests are exempt from the
    audit trail (page-load noise), while control calls are still recorded."""
    import os

    from aios_api import web_dist_dir

    dist = web_dist_dir()
    if dist is None:
        pytest.skip("web/dist not built — run `cd web && npm run build`")

    r = client.get("/")
    assert r.status_code == 200
    assert "aios" in r.text.lower()
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self' ws: wss:" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers.get("x-frame-options") == "DENY"

    assets_dir = os.path.join(dist, "assets")
    js_files = sorted(f for f in os.listdir(assets_dir) if f.endswith(".js"))
    assert js_files, "web/dist has no JS bundle"
    r2 = client.get(f"/assets/{js_files[0]}")
    assert r2.status_code == 200
    assert r2.headers.get("content-security-policy", "") == csp

    # API routes keep the full strict policy.
    r3 = client.get("/v1/health")
    assert r3.headers.get("content-security-policy", "") == (
        "default-src 'none'; frame-ancestors 'none'"
    )

    # Unknown /v1 paths are API 404s, never the SPA shell.
    r4 = client.get("/v1/definitely-not-a-route")
    assert r4.status_code == 404
    assert "text/html" not in r4.headers.get("content-type", "")

    def _audited(path: str) -> bool:
        return any(
            e.get("path") == path
            for e in client.app.state.kernel.audit.read(limit=2000)
        )

    assert not _audited("/")
    assert not _audited(f"/assets/{js_files[0]}")
    assert _audited("/v1/health")
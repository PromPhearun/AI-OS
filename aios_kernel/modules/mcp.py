"""MCP client — Model Context Protocol tool servers (docs/07-tools.md §3).

Phase 3 implements the two v1 transports:

  * **stdio** — a server subprocess speaking JSON-RPC 2.0 over stdin/stdout;
  * **http** — JSON-RPC 2.0 over HTTP POST to an ``/mcp`` endpoint.

Every response is normalized into the kernel's envelope; server errors map onto
the canonical ABI codes. Tool schemas advertised by a server are **re-validated
and hardened** before registration (docs/07-tools.md §3): parameters must be a
JSON Schema object, extra properties are rejected, and string lengths are
capped — agents cannot smuggle oversized or malformed args into a tool call.

Registration is operator-only (enforced by the Access Control dispatch gate).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import uuid

import httpx

from ..errors import AiosError, E_INVAL, E_NOENT, E_STATE, E_TIMEOUT, E_TOOL
from ..syscalls.registry import register

MCP_DEFAULT_TIMEOUT_S = 30.0
MCP_STRING_MAX = 8192
MCP_MAX_TOOLS_PER_SERVER = 128


def _rpc_id() -> str:
    return str(uuid.uuid4())


def _normalize_response(payload) -> dict:
    """Return the result of a JSON-RPC 2.0 response or raise the mapped error."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise AiosError(E_TOOL, "MCP server returned a non-JSON-RPC response")
    if "error" in payload:
        err = payload.get("error") or {}
        code = err.get("code")
        message = str(err.get("message") or "MCP error")
        if code == -32602:
            raise AiosError(E_INVAL, message)
        raise AiosError(E_TOOL, message)
    result = payload.get("result")
    if result is None:
        raise AiosError(E_TOOL, "MCP server returned an empty result")
    return result


def harden_mcp_schema(schema) -> dict | None:
    """Re-validate and harden an MCP tool's ``inputSchema`` (07-tools §3)."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        return None
    hardened: dict = {"type": "object", "properties": {}, "additionalProperties": False}
    required: list[str] = []
    for key, prop in props.items():
        if not isinstance(key, str) or not isinstance(prop, dict):
            continue
        if prop.get("type") == "string":
            prop = dict(prop)
            prop["maxLength"] = min(int(prop.get("maxLength", MCP_STRING_MAX)), MCP_STRING_MAX)
        hardened["properties"][key] = prop
    for req in schema.get("required", []) or []:
        if isinstance(req, str) and req in hardened["properties"]:
            required.append(req)
    if required:
        hardened["required"] = required
    return hardened


class MCPClient:
    """Base JSON-RPC 2.0 client for one MCP server."""

    transport = "abstract"

    def __init__(
        self,
        kernel,
        server_id: str,
        endpoint: str,
        *,
        headers: dict | None = None,
        env: dict | None = None,
        timeout_s: float = MCP_DEFAULT_TIMEOUT_S,
    ):
        self.kernel = kernel
        self.server_id = server_id
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.env = dict(env or {})
        self.timeout_s = timeout_s

    async def list_tools(self) -> list[dict]:
        res = await self._request("tools/list", {})
        tools = res.get("tools") if isinstance(res, dict) else None
        if not isinstance(tools, list):
            raise AiosError(E_TOOL, f"MCP server '{self.server_id}' returned no tool list")
        return tools

    async def call_tool(self, name: str, args: dict, *, auth_header: str | None = None) -> dict:
        headers = dict(self.headers)
        if auth_header:
            headers["Authorization"] = auth_header
        res = await self._request(
            "tools/call", {"name": name, "arguments": dict(args or {})}, headers=headers
        )
        return res if isinstance(res, dict) else {"content": res}

    async def _request(self, method: str, params: dict, *, headers: dict | None = None) -> dict:  # pragma: no cover
        raise NotImplementedError


class MCPStdioClient(MCPClient):
    """One-shot stdio transport: spawn the server per request, speak one
    JSON-RPC message, and reap it. Server env is sanitized — only PATH plus the
    operator-declared ``env`` map; host secrets are never inherited."""

    transport = "stdio"

    def _subprocess_env(self) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(self.env)
        return env

    async def _request(self, method: str, params: dict, *, headers: dict | None = None) -> dict:
        try:
            parts = shlex.split(self.endpoint)
        except ValueError as exc:
            raise AiosError(E_INVAL, f"malformed MCP stdio command: {exc}") from None
        if not parts:
            raise AiosError(E_INVAL, "empty MCP stdio command")
        req = {"jsonrpc": "2.0", "id": _rpc_id(), "method": method, "params": params}
        try:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._subprocess_env(),
            )
        except OSError as exc:
            raise AiosError(E_TOOL, f"cannot start MCP server '{self.server_id}': {exc}") from None
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate((json.dumps(req) + "\n").encode("utf-8")), self.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise AiosError(E_TIMEOUT, f"MCP server '{self.server_id}' timed out") from None
        if proc.returncode != 0:
            raise AiosError(E_TOOL, f"MCP server '{self.server_id}' exited with {proc.returncode}")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except ValueError:
            raise AiosError(E_TOOL, f"MCP server '{self.server_id}' returned invalid JSON") from None
        return _normalize_response(payload)


class MCPHttpClient(MCPClient):
    """HTTP transport: JSON-RPC 2.0 POST to an ``/mcp`` endpoint."""

    transport = "http"

    async def _request(self, method: str, params: dict, *, headers: dict | None = None) -> dict:
        req = {"jsonrpc": "2.0", "id": _rpc_id(), "method": method, "params": params}
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.endpoint, json=req, headers=request_headers)
        except httpx.HTTPError as exc:
            raise AiosError(E_TOOL, f"MCP HTTP request to '{self.server_id}' failed: {exc}") from None
        if resp.status_code != 200:
            raise AiosError(E_TOOL, f"MCP server '{self.server_id}' returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise AiosError(E_TOOL, f"MCP server '{self.server_id}' returned invalid JSON") from None
        return _normalize_response(payload)


class MCPRegistry:
    """Owns the set of registered MCP servers; the Tool Manager mirrors their
    tools into the global registry on register/unregister."""

    def __init__(self, kernel):
        self.kernel = kernel
        self._clients: dict[str, MCPClient] = {}

    def list_servers(self) -> list[dict]:
        return [
            {"server_id": sid, "transport": client.transport}
            for sid, client in sorted(self._clients.items())
        ]

    async def register(
        self,
        server_id: str,
        transport: str,
        endpoint: str,
        *,
        headers: dict | None = None,
        env: dict | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if server_id in self._clients:
            raise AiosError(E_STATE, f"MCP server '{server_id}' is already registered")
        cls = {"stdio": MCPStdioClient, "http": MCPHttpClient}.get(transport)
        if cls is None:
            raise AiosError(E_INVAL, f"unknown MCP transport '{transport}'")
        client = cls(
            self.kernel,
            server_id,
            endpoint,
            headers=headers,
            env=env,
            timeout_s=timeout_s or MCP_DEFAULT_TIMEOUT_S,
        )
        await self.kernel.tools.register_mcp_server(server_id, client)
        self._clients[server_id] = client
        self.kernel.audit.record(
            "mcp.register", pid=None, server=server_id, transport=transport
        )

    def unregister(self, server_id: str) -> None:
        client = self._clients.pop(server_id, None)
        if client is None:
            raise AiosError(E_NOENT, f"MCP server '{server_id}' is not registered")
        self.kernel.tools.unregister_server(server_id)
        self.kernel.audit.record("mcp.unregister", pid=None, server=server_id)

    def get(self, server_id: str) -> MCPClient:
        client = self._clients.get(server_id)
        if client is None:
            raise AiosError(E_NOENT, f"MCP server '{server_id}' is not registered")
        return client

    async def call_tool(
        self, server_id: str, name: str, args: dict, *, auth_key: str | None = None
    ) -> dict:
        client = self.get(server_id)
        auth_header = None
        if auth_key:
            try:
                auth_header = f"Bearer {self.kernel.vault.get(auth_key)}"
            except AiosError:
                auth_header = None  # missing credential: fail closed at the server
        return await client.call_tool(name, args, auth_header=auth_header)


# ------------------------------------------------------------------ syscalls
@register("mcp_register")
async def _mcp_register(kernel, pid: int, args: dict) -> dict:
    await kernel.mcp.register(
        args["server_id"],
        args["transport"],
        args["endpoint"],
        headers=args.get("headers"),
        env=args.get("env"),
        timeout_s=args.get("timeout_s"),
    )
    return {"server_id": args["server_id"], "ok": True}


@register("mcp_unregister")
async def _mcp_unregister(kernel, pid: int, args: dict) -> dict:
    kernel.mcp.unregister(args["server_id"])
    return {"server_id": args["server_id"], "ok": True}


@register("mcp_list")
async def _mcp_list(kernel, pid: int, args: dict) -> dict:
    return {"servers": kernel.mcp.list_servers()}
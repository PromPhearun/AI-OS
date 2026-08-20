"""Integration tests: MCP client — stdio + http transports, error
normalization, schema hardening, operator-only registration, and prompt
injection containment (docs/07-tools.md §3, §7; threat T7)."""

from __future__ import annotations

import json
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aios_kernel import Kernel
from aios_sdk.errors import AiosNoEntError, AiosPermissionError, AiosToolError

from ..conftest import _base_spec
from ..fixtures.fake_mcp_server import McpError, handle

_FAKE_MCP_SCRIPT = str(Path(__file__).resolve().parent.parent / "fixtures" / "fake_mcp_server.py")
_STDIO_ENDPOINT = shlex.join([sys.executable, _FAKE_MCP_SCRIPT])


class _HttpMcpHandler(BaseHTTPRequestHandler):
    """Serves the same fake MCP over HTTP POST /mcp."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        resp = {"jsonrpc": "2.0", "id": req.get("id")}
        try:
            resp["result"] = handle(req.get("method"), req.get("params") or {})
        except McpError as exc:
            resp["error"] = {"code": exc.code, "message": exc.message}
        body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence test server logs
        pass


@pytest.mark.asyncio
async def test_stdio_register_list_and_call(kernel: Kernel, session) -> None:
    await kernel.mcp.register("fake", "stdio", _STDIO_ENDPOINT)

    # server tools are mirrored into the global registry with hardened schemas
    sc = await session(_base_spec(name="mcp", capabilities={"tools": [{"name": "fake.echo"}]}))
    ids = {t["id"] for t in await sc.list_tools()}
    assert {"fake.echo", "fake.boom", "fake.inject"} <= ids

    result = await sc.call_tool("fake.echo", {"text": "hello"})
    assert result["result"]["content"][0]["text"] == "hello (from mcp)"
    assert result["meta"]["tool"] == "fake.echo"


@pytest.mark.asyncio
async def test_mcp_error_normalized_to_e_tool(kernel: Kernel, session) -> None:
    await kernel.mcp.register("fake", "stdio", _STDIO_ENDPOINT)
    sc = await session(_base_spec(name="mcp-err", capabilities={"tools": [{"name": "fake.boom"}]}))
    with pytest.raises(AiosToolError):
        await sc.call_tool("fake.boom", {})


@pytest.mark.asyncio
async def test_http_transport(kernel: Kernel, session) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpMcpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        await kernel.mcp.register("httpfake", "http", endpoint)
        sc = await session(
            _base_spec(name="mcp-http", capabilities={"tools": [{"name": "httpfake.echo"}]})
        )
        result = await sc.call_tool("httpfake.echo", {"text": "over http"})
        assert result["result"]["content"][0]["text"] == "over http (from mcp)"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_unregister_removes_mcp_tools(kernel: Kernel, session) -> None:
    await kernel.mcp.register("fake", "stdio", _STDIO_ENDPOINT)
    sc = await session(_base_spec(name="mcp-ls"))
    assert "fake.echo" in {t["id"] for t in await sc.list_tools()}
    kernel.mcp.unregister("fake")
    assert "fake.echo" not in {t["id"] for t in await sc.list_tools()}
    with pytest.raises(AiosNoEntError):
        await sc.syscall("call_tool", {"tool": "fake.echo", "args": {"text": "x"}})


@pytest.mark.asyncio
async def test_agent_cannot_register_mcp_server(kernel: Kernel, session) -> None:
    sc = await session(_base_spec(name="not-op"))
    with pytest.raises(AiosPermissionError):
        await sc.syscall(
            "mcp_register",
            {"server_id": "fake", "transport": "stdio", "endpoint": _STDIO_ENDPOINT},
        )
    # operator agents may register
    op = await session(_base_spec(name="op", capabilities={"operator": True}))
    await op.syscall(
        "mcp_register",
        {"server_id": "fake", "transport": "stdio", "endpoint": _STDIO_ENDPOINT},
    )
    assert (await op.syscall("mcp_list"))["servers"][0]["server_id"] == "fake"


@pytest.mark.asyncio
async def test_injection_output_does_not_change_tool_grants(kernel: Kernel, session) -> None:
    """Acceptance (§12): tool output containing 'ignore previous instructions'
    must not change tool grant behavior."""
    await kernel.mcp.register("fake", "stdio", _STDIO_ENDPOINT)
    sc = await session(
        _base_spec(
            name="injected",
            capabilities={
                "tools": [
                    {"name": "fake.inject"},
                    {"name": "fake.echo"},
                    # deliberately NOT granted
                ]
            },
        )
    )
    # the untrusted output carries the hostile instruction...
    result = await sc.call_tool("fake.inject", {"text": "x"})
    assert "ignore previous instructions" in result["result"]["content"][0]["text"]
    # ...but the immutable permission snapshot still denies un-granted tools
    with pytest.raises(AiosPermissionError):
        await sc.call_tool("fs.write", {"path": "x", "content": "y"})
    # and granted tools keep working
    echo = await sc.call_tool("fake.echo", {"text": "still here"})
    assert "still here" in echo["result"]["content"][0]["text"]
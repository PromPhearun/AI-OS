"""Tool Manager — the kernel's device-driver layer (docs/07-tools.md).

Built-in tools (Phase 1):
    fs.read     — read a file from the agent's sandboxed workspace
    fs.write    — write a file into the agent's sandboxed workspace
    shell.run   — run an allowlisted, workspace-cwd subprocess

Phase 3 additions (this file):
  * full execution pipeline — validate → permission (Access Control resolved
    snapshot) → budget → schedule (sandbox slots + rate limits) → resolve auth
    → execute (deadline) → sanitize → record → return envelope;
  * tool scheduler — global subprocess-slot semaphore, per-tool/per-agent
    sliding-window rate limits, per-tool deadlines, and cooperative
    cancellation (`cancel_tool` aborts an in-flight call with `E_ABORT`);
  * sandboxed subprocess env — kernel-built from PATH + the agent's resolved
    `env.allowed_keys` (vault values) only; host secrets are never inherited;
  * MCP integration — servers registered through the MCP registry expose their
    tools here with hardened, re-validated schemas (docs/07-tools.md §3);
  * `get_sandbox` syscall — an agent can introspect its own sandbox profile.

Security model (unchanged + hardened):
  * deny-by-default: a tool an agent's spec does not list → E_PERM;
  * paths are resolved inside the agent's workspace; escapes → E_INVAL;
  * shell.run restricts binaries to an allowlist and forbids metacharacters;
  * outputs are size-capped before entering the agent's context;
  * every call is charged (max_tool_calls budget) and audited
    (`tool.exec`: {tool, args_hash, result_hash, cost, duration_ms}).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass

import jsonschema

from ..errors import (
    AiosError,
    E_ABORT,
    E_BUDGET,
    E_BUSY,
    E_INVAL,
    E_NOENT,
    E_PERM,
    E_TIMEOUT,
    E_TOOL,
)
from ..syscalls.registry import args_hash, register
from .mcp import MCP_MAX_TOOLS_PER_SERVER, harden_mcp_schema
from .sandbox import (
    DEFAULT_SANDBOX_IMAGE,
    build_container_command,
    docker_cli_env,
    has_metacharacters,
    DockerProbe,
)

SHELL_ALLOWLIST = {"ls", "cat", "head", "tail", "wc", "grep", "find", "pwd", "echo", "date", "sleep", "true", "false"}
DEFAULT_TOOL_TIMEOUT_S = 30.0
TOOL_SLOTS = int(os.environ.get("AIOS_TOOL_SLOTS", "4"))
RATE_WINDOW_S = 60.0


class _NullGuard:
    """async no-op context manager used for in-process tools (no slot taken)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_NULL_GUARD = _NullGuard()


@dataclass
class Tool:
    """Registered tool: id, discoverable metadata, JSON-schema parameters."""

    id: str
    title: str
    description: str
    parameters: dict
    handler: callable  # async (kernel, pid, args) -> dict
    max_output_chars: int = 32_000
    needs_approval: bool = False
    server: str = "builtin"          # "builtin" or "mcp://<server_id>"
    sandbox: str = "inprocess"       # inprocess | subprocess | mcp
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    rate_per_min: int = 0            # per-agent-per-tool cap (0 = unlimited)
    rate_per_min_global: int = 0     # kernel-wide per-tool cap (0 = unlimited)
    auth: str | None = None          # "credential:<vault-key>" — resolved at runtime

    def spec_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "parameters": self.parameters,
            "needs_approval": self.needs_approval,
            "max_output_chars": self.max_output_chars,
            "server": self.server,
            "sandbox": self.sandbox,
            "timeout_s": self.timeout_s,
            "rate_per_min": self.rate_per_min,
        }


def _ws_path(kernel, pid: int, rel: str) -> str:
    """Resolve a virtual path inside the agent's workspace (never escapes)."""
    return kernel.workspaces.resolve(pid, rel)


class ToolManager:
    def __init__(self, kernel):
        self.kernel = kernel
        self._tools: dict[str, Tool] = {}
        self._slots = asyncio.Semaphore(TOOL_SLOTS)
        self._in_flight: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._rate: dict[tuple[str, int], deque] = {}  # (tool, pid) -> call stamps
        self._rate_global: dict[str, deque] = {}       # tool -> call stamps
        # Container profile (Phase 5 Slice 5.2): fail-closed daemon probe + image.
        self._docker_probe = DockerProbe()
        self._sandbox_image = os.environ.get("AIOS_SANDBOX_IMAGE") or DEFAULT_SANDBOX_IMAGE
        self._register_builtins()

    # ------------------------------------------------------------- registry
    def register(self, tool: Tool) -> None:
        if tool.id in self._tools:
            raise RuntimeError(f"duplicate tool id: {tool.id}")
        self._tools[tool.id] = tool

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(tool_id)

    def list(self, query: str | None = None) -> list[dict]:
        q = (query or "").lower()
        return [
            t.spec_dict()
            for t in sorted(self._tools.values(), key=lambda t: t.id)
            if not q or q in t.id.lower() or q in t.description.lower()
        ]

    # ------------------------------------------------------------------- mcp
    async def register_mcp_server(self, server_id: str, client) -> None:
        """Advertise an MCP server's tools into the registry (hardened).

        Tool schemas are re-validated by the kernel before registration
        (docs/07-tools.md §3); unhardenable schemas are skipped (deny by
        default — the tool simply is not available).
        """
        tools = await client.list_tools()
        if len(tools) > MCP_MAX_TOOLS_PER_SERVER:
            raise AiosError(E_INVAL, f"MCP server '{server_id}' advertises too many tools")
        for t in tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            name = str(t["name"])
            tool_id = f"{server_id}.{name}"
            if tool_id in self._tools:
                raise AiosError(E_INVAL, f"MCP tool id collision: {tool_id}")
            params = harden_mcp_schema(t.get("inputSchema"))
            if params is None:
                continue
            self.register(
                Tool(
                    id=tool_id,
                    title=str(t.get("title") or name),
                    description=str(
                        t.get("description")
                        or f"Tool '{name}' provided by MCP server '{server_id}'"
                    ),
                    parameters=params,
                    handler=_mcp_tool_handler(server_id, name),
                    server=f"mcp://{server_id}",
                    sandbox="mcp",
                    timeout_s=client.timeout_s,
                    max_output_chars=32_000,
                )
            )

    def unregister_server(self, server_id: str) -> None:
        prefix = f"{server_id}."
        for tool_id in [tid for tid in self._tools if tid.startswith(prefix)]:
            del self._tools[tool_id]

    # ------------------------------------------------------------ permission
    def _grant_for(self, pid: int, tool_id: str) -> dict | None:
        """Resolved grant from the Access Control snapshot (deny by default)."""
        try:
            return self.kernel.access.check_tool(pid, tool_id)
        except AiosError:
            return None

    # -------------------------------------------------------------- schedule
    def _rate_check(self, tool: Tool, pid: int) -> None:
        now = time.time()
        if tool.rate_per_min > 0:
            stamps = self._rate.setdefault((tool.id, pid), deque())
            while stamps and now - stamps[0] > RATE_WINDOW_S:
                stamps.popleft()
            if len(stamps) >= tool.rate_per_min:
                raise AiosError(E_BUSY, f"rate limit exceeded for tool '{tool.id}'")
            stamps.append(now)
        if tool.rate_per_min_global > 0:
            stamps = self._rate_global.setdefault(tool.id, deque())
            while stamps and now - stamps[0] > RATE_WINDOW_S:
                stamps.popleft()
            if len(stamps) >= tool.rate_per_min_global:
                raise AiosError(E_BUSY, f"global rate limit exceeded for tool '{tool.id}'")
            stamps.append(now)

    # --------------------------------------------------------------- sandbox
    def sandbox_env(self, pid: int) -> dict:
        """Kernel-built subprocess env: PATH + LANG + agent identity + the
        agent's resolved ``env.allowed_keys`` (vault values) only. Host
        secrets are never inherited (docs/08-security.md §4)."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "AIOS_PID": str(pid),
            "AIOS_WORKSPACE": str(self.kernel.workspaces.path_for(pid)),
        }
        try:
            allowed = self.kernel.access.snapshot(pid)["env"]["allowed_keys"]
        except AiosError:
            allowed = []
        for key in allowed:
            try:
                env[key] = self.kernel.vault.get(key)
            except AiosError:
                continue
        return env

    def cancel(self, call_id: str) -> dict:
        """Request cancellation of an in-flight tool call (cooperative)."""
        event = self._cancel_events.get(call_id)
        if event is None:
            return {"ok": True, "cancelled": False, "call_id": call_id}
        event.set()
        return {"ok": True, "cancelled": True, "call_id": call_id}

    def _resolve_auth(self, tool: Tool) -> str | None:
        """Resolve a tool's ``credential:<key>`` from the vault at call time.

        The value is used only to build the outbound request; it never enters
        the agent's context, logs, or checkpoints (docs/07-tools.md §7).
        """
        if not tool.auth or not tool.auth.startswith("credential:"):
            return None
        key = tool.auth.split("credential:", 1)[1]
        try:
            return self.kernel.vault.get(key)
        except AiosError:
            return None  # missing credential: fail closed downstream

    async def call(self, pid: int, tool_id: str, args: dict) -> dict:
        """The canonical tool-execution pipeline (docs/07-tools.md §4)."""
        started = time.monotonic()
        tool = self._tools.get(tool_id)
        if tool is None:
            raise AiosError(E_NOENT, f"no such tool: '{tool_id}'")

        # 1. validate — strict JSON Schema (E_INVAL)
        try:
            jsonschema.validate(args, tool.parameters)
        except jsonschema.ValidationError as exc:
            raise AiosError(E_INVAL, f"invalid args for '{tool_id}': {exc.message}") from exc

        # 2. permission — resolved snapshot (deny by default); approval tickets
        grant = self._grant_for(pid, tool_id)
        if grant is None:
            raise AiosError(E_PERM, f"agent {pid} is not granted tool '{tool_id}'")
        if tool.needs_approval or grant.get("needs_approval") or grant.get("approved") is False:
            if not self.kernel.access.consume_approval(pid, tool_id):
                raise AiosError(
                    E_PERM,
                    f"tool '{tool_id}' requires operator approval; call request_permission() first",
                )

        # 3. budget — hard cap on total tool calls (E_BUDGET)
        acb = self.kernel.agent_manager.get(pid)
        if acb.budgets.max_tool_calls and acb.usage.tool_calls >= acb.budgets.max_tool_calls:
            raise AiosError(E_BUDGET, f"tool-call budget exhausted for agent {pid}")

        # 4. schedule — sliding-window rate limits + sandbox slot
        self._rate_check(tool, pid)
        guard = _NULL_GUARD if tool.sandbox == "inprocess" else self._slots
        async with guard:
            # 5. resolve — credential from vault (never into agent context)
            auth_value = self._resolve_auth(tool)
            # 6-9. execute (deadline), sanitize, record, return envelope
            return await self._execute_and_record(pid, tool, args, auth_value, started)

    async def _execute_and_record(
        self, pid: int, tool: Tool, args: dict, auth_value: str | None, started: float
    ) -> dict:
        call_id = f"tool-{uuid.uuid4().hex[:12]}"
        cancel_event = asyncio.Event()
        task = asyncio.create_task(self._execute(tool, pid, args, auth_value, cancel_event))
        self._in_flight[call_id] = task
        self._cancel_events[call_id] = cancel_event
        try:
            result = await task
        finally:
            self._in_flight.pop(call_id, None)
            self._cancel_events.pop(call_id, None)
        result = self._sanitize_result(result, tool)
        result_hash = self._hash_result(result)
        self.kernel.scheduler.account_tool(pid)
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        self.kernel.audit.record(
            "tool.exec",
            pid=pid,
            tool=tool.id,
            args_hash=args_hash(args),
            result_hash=result_hash,
            cost=0.0,
            duration_ms=duration_ms,
        )
        return {
            "result": result,
            "meta": {
                "call_id": call_id,
                "tool": tool.id,
                "duration_ms": duration_ms,
                "provider_ms": duration_ms,
                "cost": 0.0,
                "tokens": 0,
            },
        }

    async def _execute(
        self, tool: Tool, pid: int, args: dict, auth_value: str | None, cancel_event: asyncio.Event
    ) -> dict:
        handler = tool.handler
        try:
            if tool.sandbox == "subprocess":
                result = await handler(self.kernel, pid, args, cancel_event=cancel_event)
            else:
                result = await handler(self.kernel, pid, args)
        except AiosError:
            raise
        except Exception as exc:  # noqa: BLE001 — tool boundary fence
            raise AiosError(E_TOOL, f"tool '{tool.id}' failed") from exc
        return result

    def _sanitize_result(self, result: dict, tool: Tool) -> dict:
        cap = tool.max_output_chars

        def _walk(value):
            if isinstance(value, str):
                value = value.replace("\x00", "")
                if len(value) > cap:
                    value = value[:cap] + "\n... [truncated]"
                return value
            if isinstance(value, dict):
                return {k: _walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_walk(v) for v in value]
            return value

        return _walk(result)

    @staticmethod
    def _hash_result(result: dict) -> str:
        canonical = json.dumps(result, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[:16]

    # ------------------------------------------------------------- built-ins
    def _register_builtins(self) -> None:
        self.register(
            Tool(
                id="fs.read",
                title="Read a file",
                description="Read a file from the agent's sandboxed workspace. Paths are relative.",
                parameters={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 512},
                        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 100_000},
                    },
                    "additionalProperties": False,
                },
                handler=self._fs_read,
            )
        )
        self.register(
            Tool(
                id="fs.write",
                title="Write a file",
                description="Write a file into the agent's sandboxed workspace.",
                parameters={
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 512},
                        "content": {"type": "string", "maxLength": 1_000_000},
                    },
                    "additionalProperties": False,
                },
                handler=self._fs_write,
            )
        )
        self.register(
            Tool(
                id="shell.run",
                title="Run a shell command",
                description=(
                    "Run an allowlisted command in the agent's workspace. "
                    "Allowed: " + ", ".join(sorted(SHELL_ALLOWLIST))
                ),
                parameters={
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
                    },
                    "additionalProperties": False,
                },
                handler=self._shell_run,
                sandbox="subprocess",
            )
        )
# --------------------------------------------------------- tool handlers
    async def _fs_read(self, kernel, pid: int, args: dict) -> dict:
        return await kernel.fs.read(pid, args["path"], max_bytes=args.get("max_bytes") or 32_000)

    async def _fs_write(self, kernel, pid: int, args: dict) -> dict:
        return await kernel.fs.write(pid, args["path"], args["content"])

    async def _shell_run(self, kernel, pid: int, args: dict, *, cancel_event=None) -> dict:
        cmd = args["command"]
        parts = shlex.split(cmd)
        if not parts:
            raise AiosError(E_INVAL, "empty command")
        if parts[0] not in SHELL_ALLOWLIST:
            raise AiosError(E_INVAL, f"binary '{parts[0]}' is not allowlisted")
        if has_metacharacters(cmd):
            raise AiosError(E_INVAL, "shell metacharacters are not allowed")
        tool = self.get("shell.run")
        timeout = float(args.get("timeout_s") or tool.timeout_s or DEFAULT_TOOL_TIMEOUT_S)
        # docs/08-security.md §4: a spec-declared container profile executes the
        # allowlisted argv inside a throwaway Docker sandbox instead of a bare
        # subprocess. Any daemon failure fails closed (E_BUSY).
        acb = kernel.agent_manager.get(pid)
        sb = acb.spec.get("sandbox") or {}
        if sb.get("profile") == "container":
            return await self._shell_run_container(
                kernel, pid, parts, timeout, sb, cancel_event=cancel_event
            )
        env = kernel.tools.sandbox_env(pid)
        cwd = str(kernel.workspaces.path_for(pid))
        return await self._run_process(
            parts, env=env, cwd=cwd, timeout=timeout,
            cancel_event=cancel_event, label=parts[0],
        )

    async def _shell_run_container(
        self, kernel, pid: int, parts: list[str], timeout: float, sb: dict,
        *, cancel_event=None,
    ) -> dict:
        """Run an allowlisted argv as ``docker run`` (fail-closed sandbox)."""
        if not await self._docker_probe.available():
            raise AiosError(
                E_BUSY,
                "container sandbox unavailable: docker daemon is not reachable — failing closed",
            )
        network = sb.get("network", "none")
        if network not in ("none", "http", "all"):
            raise AiosError(
                E_INVAL,
                f"sandbox.network must be 'none', 'http', or 'all', got {network!r}",
            )
        proxy = None
        if network == "http":
            proxy = os.environ.get("AIOS_EGRESS_PROXY")
            if not proxy:
                raise AiosError(
                    E_PERM,
                    "sandbox.network='http' requires AIOS_EGRESS_PROXY "
                    "(an egress allowlist needs a filtering CONNECT proxy) — failing closed",
                )
        env = self.sandbox_env(pid)
        env["AIOS_WORKSPACE"] = "/ws"  # container path, never the host path
        try:
            docker_argv = build_container_command(
                workspace=str(kernel.workspaces.path_for(pid)),
                argv=parts,
                network=network,
                rlimits=dict(sb.get("rlimits") or {}),
                image=self._sandbox_image,
                proxy=proxy,
                env=env,
                call_id=f"{pid}-{uuid.uuid4().hex[:8]}",
            )
        except ValueError as exc:
            raise AiosError(E_INVAL, f"invalid container sandbox profile: {exc}") from None
        result = await self._run_process(
            docker_argv,
            env=docker_cli_env(),
            cwd=str(kernel.workspaces.path_for(pid)),
            timeout=timeout,
            cancel_event=cancel_event,
            label="docker",
        )
        if result["code"] == 125:
            raise AiosError(
                E_TOOL,
                f"docker failed: {result['stderr'].strip() or 'daemon error'}",
            )
        return result

    async def _run_process(
        self, argv: list[str], *, env: dict | None, cwd: str, timeout: float,
        cancel_event=None, label: str,
    ) -> dict:
        """Spawn ``argv``, stream to completion, honor timeout/cancellation."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AiosError(E_TOOL, f"cannot start '{label}': {exc}") from None
        comm = asyncio.create_task(proc.communicate())
        cwait = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            waiters = {comm}
            if cwait is not None:
                waiters.add(cwait)
            done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if cwait is not None and cwait in done:
                self._kill_proc(proc)
                await asyncio.wait({comm})
                raise AiosError(E_ABORT, f"{label}: cancelled")
            if comm not in done:
                self._kill_proc(proc)
                await asyncio.wait({comm})
                return {"code": -1, "stdout": "", "stderr": "timed out"}
            stdout, stderr = comm.result()
        except asyncio.CancelledError:
            self._kill_proc(proc)
            await asyncio.wait({comm})
            raise
        finally:
            if cwait is not None:
                cwait.cancel()
        return {
            "code": proc.returncode,
            "stdout": (stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (stderr or b"").decode("utf-8", errors="replace"),
        }

    @staticmethod
    def _kill_proc(proc) -> None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


# ------------------------------------------------------------------ syscalls
@register("list_tools")
async def _list_tools(kernel, pid: int, args: dict) -> dict:
    return {"tools": kernel.tools.list(args.get("query"))}


@register("call_tool")
async def _call_tool(kernel, pid: int, args: dict) -> dict:
    return await kernel.tools.call(pid, args["tool"], args["args"])


@register("cancel_tool")
async def _cancel_tool(kernel, pid: int, args: dict) -> dict:
    return kernel.tools.cancel(args["call_id"])


@register("get_sandbox")
async def _get_sandbox(kernel, pid: int, args: dict) -> dict:
    """Introspect the caller's sandbox profile (docs/08-security.md §4)."""
    spec = kernel.agent_manager.get(pid).spec
    sb = spec.get("sandbox") or {}
    snap = kernel.access.snapshot(pid)
    return {
        "profile": sb.get("profile", "subprocess"),
        "cwd": str(kernel.workspaces.path_for(pid)),
        "env_keys": list(snap["env"]["allowed_keys"]),
        "network": sb.get("network", "none"),
        "rlimits": dict(sb.get("rlimits") or {}),
    }


def _mcp_tool_handler(server_id: str, name: str):
    """Handler factory for a tool backed by an MCP server tool."""

    async def _handler(kernel, pid, args):
        return await kernel.mcp.call_tool(server_id, name, args)

    return _handler
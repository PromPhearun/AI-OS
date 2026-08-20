"""Tool Manager — the kernel's device-driver layer (docs/07-tools.md).

Phase 1 ships exactly three built-in tools behind the canonical ``list_tools``
/ ``call_tool`` / ``cancel_tool`` syscalls:

    fs.read     — read a file from the agent's sandboxed workspace
    fs.write    — write a file into the agent's sandboxed workspace
    shell.run   — run an allowlisted, workspace-cwd subprocess

Security model:
  * deny-by-default: a tool an agent's spec does not list → E_PERM;
  * paths are resolved inside the agent's workspace; escapes → E_INVAL;
  * shell.run restricts binaries to an allowlist and forbids metacharacters;
  * outputs are size-capped before entering the agent's context;
  * every call is charged (max_tool_calls budget) and audited.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass

import jsonschema

from ..errors import AiosError, E_INVAL, E_NOENT, E_PERM, E_TIMEOUT
from ..syscalls.registry import register

SHELL_ALLOWLIST = {"ls", "cat", "head", "tail", "wc", "grep", "find", "pwd", "echo", "date", "true", "false"}
DEFAULT_TOOL_TIMEOUT_S = 30.0


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

    def spec_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "parameters": self.parameters,
            "needs_approval": self.needs_approval,
            "max_output_chars": self.max_output_chars,
        }


def _ws_path(kernel, pid: int, rel: str) -> str:
    if not rel or rel.startswith(("/", "~")):
        raise AiosError(E_INVAL, f"path must be workspace-relative, got '{rel}'")
    root = kernel.workspaces.path_for(pid)
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise AiosError(E_INVAL, f"path escapes workspace: '{rel}'")
    return str(target)


class ToolManager:
    def __init__(self, kernel):
        self.kernel = kernel
        self._tools: dict[str, Tool] = {}
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

    # ------------------------------------------------------------ permission
    def _grant_for(self, pid: int, tool_id: str) -> dict | None:
        spec = self.kernel.agent_manager.get(pid).spec
        grants = spec.get("capabilities", {}).get("tools", [])
        return next((g for g in grants if g["name"] == tool_id), None)

    async def call(self, pid: int, tool_id: str, args: dict) -> dict:
        started = time.monotonic()
        grant = self._grant_for(pid, tool_id)
        if grant is None:
            raise AiosError(E_PERM, f"agent {pid} is not granted tool '{tool_id}'")
        tool = self._tools.get(tool_id)
        if tool is None:
            raise AiosError(E_NOENT, f"no such tool: '{tool_id}'")
        if grant.get("approved") is False or tool.needs_approval:
            raise AiosError(E_PERM, f"tool '{tool_id}' requires approval (Phase 3)")

        try:
            jsonschema.validate(args, tool.parameters)
        except jsonschema.ValidationError as exc:
            raise AiosError(E_INVAL, f"invalid args for '{tool_id}': {exc.message}") from exc

        call_id = str(uuid.uuid4())
        try:
            result = await asyncio.wait_for(
                tool.handler(self.kernel, pid, args), timeout=DEFAULT_TOOL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            raise AiosError(E_TIMEOUT, f"tool '{tool_id}' timed out") from None

        result = self._cap_output(result, tool.max_output_chars)
        self.kernel.scheduler.account_tool(pid)
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        return {
            "result": result,
            "meta": {
                "call_id": call_id,
                "tool": tool_id,
                "duration_ms": duration_ms,
            },
        }

    def _cap_output(self, result: dict, limit: int) -> dict:
        for key in ("stdout", "stderr", "content"):
            if key in result and isinstance(result[key], str) and len(result[key]) > limit:
                result[key] = result[key][:limit] + "\n... [truncated]"
        return result

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
            )
        )
# --------------------------------------------------------- tool handlers
    async def _fs_read(self, kernel, pid: int, args: dict) -> dict:
        target = _ws_path(kernel, pid, args["path"])
        if not os.path.isfile(target):
            raise AiosError(E_NOENT, f"no such file: {args['path']}")
        with open(target, "r", encoding="utf-8") as fh:
            content = fh.read(args.get("max_bytes") or 32_000)
        return {"path": args["path"], "content": content, "bytes": len(content)}

    async def _fs_write(self, kernel, pid: int, args: dict) -> dict:
        target = _ws_path(kernel, pid, args["path"])
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(args["content"])
        return {"path": args["path"], "bytes": len(args["content"])}

    async def _shell_run(self, kernel, pid: int, args: dict) -> dict:
        cmd = args["command"]
        parts = shlex.split(cmd)
        if not parts:
            raise AiosError(E_INVAL, "empty command")
        if parts[0] not in SHELL_ALLOWLIST:
            raise AiosError(E_INVAL, f"binary '{parts[0]}' is not allowlisted")
        if any(part in cmd for part in ("|", ";", "&&", "||", ">", "<", "`", "$(")):
            raise AiosError(E_INVAL, "shell metacharacters are not allowed")
        try:
            proc = subprocess.run(
                parts,
                cwd=kernel.workspaces.path_for(pid),
                capture_output=True,
                text=True,
                timeout=args.get("timeout_s") or 30,
            )
        except subprocess.TimeoutExpired:
            return {"code": -1, "stdout": "", "stderr": "timed out"}
        return {
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


# ------------------------------------------------------------------ syscalls
@register("list_tools")
async def _list_tools(kernel, pid: int, args: dict) -> dict:
    return {"tools": kernel.tools.list(args.get("query"))}


@register("call_tool")
async def _call_tool(kernel, pid: int, args: dict) -> dict:
    return await kernel.tools.call(pid, args["tool"], args["args"])


@register("cancel_tool")
async def _cancel_tool(kernel, pid: int, args: dict) -> dict:
    # Phase 1 tools execute synchronously to completion; nothing to cancel.
    return {"ok": True, "cancelled": False, "call_id": args["call_id"]}
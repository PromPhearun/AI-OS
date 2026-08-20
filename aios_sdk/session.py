"""AgentSession — the agent-side syscall client bound to one PID."""

from __future__ import annotations

from aios_kernel.syscalls.schema import validate_args
from .errors import AiosError, raise_for_error


class AgentSession:
    """In-process syscall session. Each agent turn receives its own instance."""

    def __init__(self, kernel, pid: int):
        self._kernel = kernel
        self._pid = pid

    @property
    def pid(self) -> int:
        return self._pid

    async def syscall(self, name: str, args: dict | None = None) -> dict:
        """Execute a syscall; raises the typed exception on failure.

        Convention: keys with ``None`` values are omitted (None means "not
        provided"), which keeps optional args schema-valid.
        """
        args = {k: v for k, v in (args or {}).items() if v is not None}
        try:
            validate_args(name, args)
        except AiosError as exc:
            raise_for_error(exc.to_result()["error"])
        result = await self._kernel.execute(self._pid, name, args)
        if "error" in result:
            raise_for_error(result["error"])
        return result

    # ------------------------------------------------------------ lifecycle
    async def spawn(self, spec: dict) -> int:
        return (await self.syscall("spawn", {"spec": spec}))["pid"]

    async def exit(self, status: str = "ok", message: str | None = None) -> dict:
        return await self.syscall("exit", {"status": status, "message": message})

    async def get_pid(self) -> dict:
        return await self.syscall("get_pid")

    async def suspend(self, reason: str | None = None) -> dict:
        return await self.syscall("suspend", {"reason": reason})

    async def resume(self, pid: int) -> dict:
        return await self.syscall("resume", {"pid": pid})

    async def get_status(self, pid: int | None = None) -> dict:
        return await self.syscall("get_status", {"pid": pid})

    async def get_usage(self) -> dict:
        return await self.syscall("get_usage")

    async def generate(
        self, user: str | None = None, *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> dict:
        """Kernel-mediated LLM turn; reply is appended to this agent's context."""
        return await self.syscall(
            "generate",
            {"user": user, "temperature": temperature, "max_tokens": max_tokens},
        )

    async def checkpoint(self, label: str | None = None) -> str:
        return (await self.syscall("checkpoint", {"label": label}))["checkpoint_id"]

    # -------------------------------------------------------------- context
    async def read_context(self) -> list[dict]:
        return (await self.syscall("read_context"))["messages"]

    async def append_context(self, role: str, content: str, pinned: bool = False) -> int:
        return (await self.syscall("append_context", {"role": role, "content": content, "pinned": pinned}))["tokens"]

    # --------------------------------------------------------------- memory
    async def write_memory(
        self,
        namespace: str,
        key: str,
        value,
        *,
        ttl: float | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Write to L2 working memory, or — with ``kind`` — to L3 long-term
        memory (embedding-indexed and persisted; docs/04-memory.md §4)."""
        return await self.syscall(
            "write_memory",
            {"namespace": namespace, "key": key, "value": value, "ttl": ttl, "kind": kind, "tags": tags},
        )

    async def read_memory(self, namespace: str, key: str):
        """Read from L2 first, then L3."""
        return (await self.syscall("read_memory", {"namespace": namespace, "key": key}))["value"]

    async def search_memory(
        self, query: str, *, namespace: str | None = None, top_k: int = 5, min_score: float | None = None
    ) -> list[dict]:
        """RAG retrieval: ranked hits [{namespace, key, kind, value, score, ...}]."""
        return (
            await self.syscall(
                "search_memory",
                {"query": query, "namespace": namespace, "top_k": top_k, "min_score": min_score},
            )
        )["hits"]

    async def forget_memory(self, namespace: str, key: str | None = None) -> dict:
        """Delete one L3 key (or the whole namespace); returns {deleted}."""
        return await self.syscall("forget_memory", {"namespace": namespace, "key": key})

    # --------------------------------------------------------------- context
    async def summarize_context(self, target_tokens: int | None = None) -> dict:
        """Collapse old turns into one summary; preserves pinned + recent N."""
        return await self.syscall("summarize_context", {"target_tokens": target_tokens})

    # ------------------------------------------------------------- semantic fs
    async def store_artifact(self, path: str, data: str, *, mime: str | None = None) -> str:
        """Write content into the sandbox and register an immutable artifact."""
        return (
            await self.syscall("store_artifact", {"path": path, "data": data, "mime": mime})
        )["artifact_id"]

    async def fs_read(self, path: str, *, max_bytes: int | None = None) -> dict:
        return await self.syscall("fs_read", {"path": path, "max_bytes": max_bytes})

    async def fs_write(self, path: str, content: str, *, mime: str | None = None) -> dict:
        return await self.syscall("fs_write", {"path": path, "content": content, "mime": mime})

    async def fs_search(self, query: str, *, top_k: int = 5) -> list[dict]:
        """Semantic FS: find artifacts by meaning, not path."""
        return (await self.syscall("fs_search", {"query": query, "top_k": top_k}))["hits"]

    # ---------------------------------------------------------------- tools
    async def list_tools(self, query: str | None = None) -> list[dict]:
        return (await self.syscall("list_tools", {"query": query}))["tools"]

    async def call_tool(self, tool: str, args: dict) -> dict:
        return await self.syscall("call_tool", {"tool": tool, "args": args})

    async def cancel_tool(self, call_id: str) -> dict:
        return await self.syscall("cancel_tool", {"call_id": call_id})

    # ------------------------------------------------------- security/util
    async def get_env(self, key: str) -> str:
        return (await self.syscall("get_env", {"key": key}))["value"]

    async def log(self, level: str, message: str) -> dict:
        return await self.syscall("log", {"level": level, "message": message})

    # ------------------------------------------------------------ scheduler
    async def sleep(self, ms: float) -> dict:
        return await self.syscall("sleep", {"ms": ms})

    async def yield_cpu(self) -> dict:
        return await self.syscall("yield", {"hint": "voluntary"})

    # ------------------------------------------------------------------- ipc
    async def send_msg(
        self,
        to_pid: int,
        body: dict,
        *,
        type: str = "direct",
        reply_to: str | None = None,
        topic: str | None = None,
        priority: int = 50,
        trace_id: str | None = None,
        ttl_s: float | None = None,
    ) -> dict:
        """Send one envelope; never blocks (returns {msg_id} immediately)."""
        return await self.syscall(
            "send_msg",
            {
                "to_pid": to_pid,
                "body": body,
                "type": type,
                "reply_to": reply_to,
                "topic": topic,
                "priority": priority,
                "trace_id": trace_id,
                "ttl_s": ttl_s,
            },
        )

    async def recv_msg(self, timeout_ms: float, *, filter: dict | None = None) -> dict:
        """Block up to timeout_ms for a matching message: {msg: {...} | None}."""
        return await self.syscall("recv_msg", {"timeout_ms": timeout_ms, "filter": filter})

    async def subscribe(self, topic: str) -> dict:
        return await self.syscall("subscribe", {"topic": topic})

    async def unsubscribe(self, topic: str) -> dict:
        return await self.syscall("unsubscribe", {"topic": topic})

    async def publish(self, topic: str, payload: dict) -> int:
        return (await self.syscall("publish", {"topic": topic, "payload": payload}))["delivered"]

    async def join(self, pids: list[int], timeout_ms: float | None = None) -> dict:
        """Wait until every listed agent is TERMINATED (or the deadline)."""
        return await self.syscall("join", {"pids": pids, "timeout_ms": timeout_ms})
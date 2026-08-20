"""Memory Manager — namespaced L2 working memory (in-memory for Phase 1).

Each agent owns a private ``agent:{pid}`` namespace and may also read/write
shared namespaces (IPC in Phase 2). Values are plain JSON; TTLs are optional.
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import AiosError, E_NOENT
from ..syscalls.registry import register


class MemoryManager:
    def __init__(self, kernel=None):
        self.kernel = kernel
        self._mem: dict[str, dict[str, dict]] = {}  # namespace -> key -> entry

    def namespace_for(self, pid: int) -> str:
        return f"agent:{pid}"

    def create_namespace(self, pid: int) -> None:
        self._mem.setdefault(self.namespace_for(pid), {})

    def write(
        self,
        pid: int,
        key: str,
        value: Any,
        *,
        namespace: str | None = None,
        ttl: float | None = None,
    ) -> None:
        ns = namespace or self.namespace_for(pid)
        self._mem.setdefault(ns, {})[key] = {"value": value, "ts": time.time(), "ttl": ttl}

    def read(self, pid: int, key: str, *, namespace: str | None = None) -> Any:
        ns = namespace or self.namespace_for(pid)
        entry = self._mem.get(ns, {}).get(key)
        if entry is None:
            raise AiosError(E_NOENT, f"no memory key '{key}' in '{ns}'")
        if entry["ttl"] and time.time() - entry["ts"] > entry["ttl"]:
            del self._mem[ns][key]
            raise AiosError(E_NOENT, f"memory key '{key}' expired")
        return entry["value"]

    def snapshot(self, pid: int) -> dict:
        """Copy of the agent's namespace for checkpointing."""
        return {
            k: {"value": v["value"], "ttl": v["ttl"]}
            for k, v in self._mem.get(self.namespace_for(pid), {}).items()
        }

    def restore(self, pid: int, snapshot: dict) -> None:
        self._mem[self.namespace_for(pid)] = {
            k: {"value": v["value"], "ts": time.time(), "ttl": v.get("ttl")}
            for k, v in snapshot.items()
        }

    def free(self, pid: int) -> None:
        self._mem.pop(self.namespace_for(pid), None)


# ------------------------------------------------------------------ syscalls
@register("write_memory")
async def _write_memory(kernel, pid: int, args: dict) -> dict:
    kernel.memory.write(
        pid,
        args["key"],
        args["value"],
        namespace=args["namespace"],
        ttl=args.get("ttl"),
    )
    return {"ok": True}


@register("read_memory")
async def _read_memory(kernel, pid: int, args: dict) -> dict:
    value = kernel.memory.read(pid, args["key"], namespace=args["namespace"])
    return {"value": value}
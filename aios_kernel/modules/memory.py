"""Memory Manager — L2 working memory + L3 long-term memory (RAG).

L2 (working): per-agent scratchpad, in-memory, checkpointed. Namespaced;
values are plain JSON; TTLs optional. Unchanged from Phase 1.

L3 (long-term, docs/04-memory.md §4): an embedding-indexed, persistent
store. ``write_memory`` with an explicit ``kind`` (episodic | semantic |
procedural) writes to L3; ``search_memory`` retrieves by cosine similarity;
``forget_memory`` deletes by key (or whole namespace). Entries are
append-logged to ``<root>/entries.jsonl`` (WAL pattern: durability before
visibility) and reloaded on kernel start — L3 survives restarts and is never
copied into checkpoints (docs/04-memory.md §6).

The semantic FS (``aios_kernel/modules/fs.py``) reuses the same vector store:
artifact writes are embedding-indexed into ``<root>/artifacts.jsonl`` and
found via ``fs_search`` (docs/05-storage.md §5).

Namespaces are isolated per agent (``agent:{pid}``) and shared pools are
granted by the spec's ``memory.pools[].access`` (deny by default -> E_PERM).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AiosError, E_INVAL, E_NOENT, E_PERM
from ..syscalls.registry import register
from .embedder import build_embedder_from_env, cosine


@dataclass
class L3Entry:
    """One embedding-indexed long-term memory entry."""

    ns: str
    key: str
    value: Any
    kind: str  # "episodic" | "semantic" | "procedural"
    tags: list[str] = field(default_factory=list)
    source_pid: int | None = None
    created_at: float = field(default_factory=time.time)
    ttl: float | None = None
    embed: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ns": self.ns,
            "key": self.key,
            "value": self.value,
            "kind": self.kind,
            "tags": list(self.tags),
            "source_pid": self.source_pid,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "embed": list(self.embed),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "L3Entry":
        return cls(
            ns=d["ns"],
            key=d["key"],
            value=d.get("value"),
            kind=d.get("kind", "semantic"),
            tags=list(d.get("tags", [])),
            source_pid=d.get("source_pid"),
            created_at=float(d.get("created_at", 0.0)),
            ttl=d.get("ttl"),
            embed=[float(v) for v in d.get("embed", [])],
        )


@dataclass
class ArtifactEntry:
    """One embedding-indexed artifact (semantic FS hit)."""

    artifact_id: str
    pid: int
    path: str
    mime: str | None
    snippet: str
    created_at: float = field(default_factory=time.time)
    embed: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "pid": self.pid,
            "path": self.path,
            "mime": self.mime,
            "snippet": self.snippet,
            "created_at": self.created_at,
            "embed": list(self.embed),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactEntry":
        return cls(
            artifact_id=d["artifact_id"],
            pid=int(d["pid"]),
            path=d["path"],
            mime=d.get("mime"),
            snippet=d.get("snippet", ""),
            created_at=float(d.get("created_at", 0.0)),
            embed=[float(v) for v in d.get("embed", [])],
        )


def _text_of(value: Any) -> str:
    """Textual form of a memory value for embedding."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


class MemoryManager:
    def __init__(self, kernel=None, *, root: str | None = None, embedder=None):
        self.kernel = kernel
        self._mem: dict[str, dict[str, dict]] = {}  # L2: namespace -> key -> entry
        self._l3: dict[tuple[str, str], L3Entry] = {}  # L3: (ns, key) -> entry
        self._artifacts: dict[str, ArtifactEntry] = {}  # artifact_id -> entry
        self.embedder = embedder or build_embedder_from_env()
        self._root = Path(root) if root else None
        self._entries_path = self._root / "entries.jsonl" if self._root else None
        self._artifacts_path = self._root / "artifacts.jsonl" if self._root else None
        if self._root:
            self._root.mkdir(parents=True, exist_ok=True)
        self._load_l3()

    # --------------------------------------------------------- secret hygiene
    def _redact_value(self, value: Any) -> Any:
        """Recursively scrub vault values before they touch kernel-owned
        persistence (checkpoints / JSONL logs) or the agent's context.
        Values already resolved from the vault are the only secrets the kernel
        can know about; everything else is the agent's own data (docs/
        08-security.md §5 hard rule: values never enter checkpoints)."""
        if self.kernel is None or not getattr(self.kernel, "vault", None):
            return value
        if isinstance(value, str):
            return self.kernel.vault.redact(value)
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        return value

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
        self._mem.setdefault(ns, {})[key] = {
            "value": self._redact_value(value),
            "ts": time.time(),
            "ttl": ttl,
        }

    def read(self, pid: int, key: str, *, namespace: str | None = None) -> Any:
        ns = namespace or self.namespace_for(pid)
        entry = self._mem.get(ns, {}).get(key)
        if entry is None:
            raise AiosError(E_NOENT, f"no memory key '{key}' in '{ns}'")
        if entry["ttl"] and time.time() - entry["ts"] > entry["ttl"]:
            del self._mem[ns][key]
            raise AiosError(E_NOENT, f"memory key '{key}' expired")
        return entry["value"]

    def read_any(self, pid: int, key: str, *, namespace: str | None = None) -> Any:
        """Read from L2 (working) first, then L3 (long-term)."""
        try:
            return self.read(pid, key, namespace=namespace)
        except AiosError:
            return self.read_l3(pid, key, namespace=namespace)

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

    # ------------------------------------------------------------ access ctl
    def _pool_grants(self, pid: int) -> dict[str, str]:
        spec = self.kernel.agent_manager.get(pid).spec
        return {
            g["pool"]: g["access"]
            for g in spec.get("memory", {}).get("pools", [])
            if isinstance(g, dict) and "pool" in g
        }

    def _can_access(self, pid: int, ns: str, *, write: bool) -> None:
        """Namespace permission, deny by default (docs/04-memory.md §5)."""
        if ns == self.namespace_for(pid):
            return
        access = self._pool_grants(pid).get(ns)
        if access is None:
            raise AiosError(E_PERM, f"namespace '{ns}' is not granted to agent {pid}")
        if write and access != "read-write":
            raise AiosError(E_PERM, f"namespace '{ns}' grants read-only access to agent {pid}")

    # ------------------------------------------------------------------ L3
    async def store_l3(
        self,
        pid: int,
        key: str,
        value: Any,
        *,
        namespace: str | None = None,
        kind: str = "semantic",
        tags: list[str] | None = None,
        ttl: float | None = None,
    ) -> dict:
        """Write one embedding-indexed L3 entry (durable before visible)."""
        ns = namespace or self.namespace_for(pid)
        self._can_access(pid, ns, write=True)
        try:
            json.dumps(value)  # strict: L3 values must survive the JSONL log
        except (TypeError, ValueError) as exc:
            raise AiosError(E_INVAL, f"L3 memory value must be JSON-serializable: {exc}") from exc
        value = self._redact_value(value)
        embed = await self.embedder.embed(f"{key}: {_text_of(value)}")
        entry = L3Entry(
            ns=ns,
            key=key,
            value=value,
            kind=kind,
            tags=list(tags or []),
            source_pid=pid,
            created_at=time.time(),
            ttl=ttl,
            embed=embed,
        )
        self._l3[(ns, key)] = entry
        self._append_jsonl(self._entries_path, entry.to_dict())
        return {"namespace": ns, "key": key}

    def read_l3(self, pid: int, key: str, *, namespace: str | None = None) -> Any:
        ns = namespace or self.namespace_for(pid)
        self._can_access(pid, ns, write=False)
        entry = self._l3.get((ns, key))
        if entry is None:
            raise AiosError(E_NOENT, f"no L3 memory key '{key}' in '{ns}'")
        if entry.ttl and time.time() - entry.created_at > entry.ttl:
            del self._l3[(ns, key)]
            raise AiosError(E_NOENT, f"L3 memory key '{key}' expired")
        return entry.value

    async def search_l3(
        self,
        pid: int,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[dict]:
        """Cosine-similarity retrieval over one permitted namespace."""
        ns = namespace or self.namespace_for(pid)
        self._can_access(pid, ns, write=False)
        qv = await self.embedder.embed(query)
        now = time.time()
        scored: list[tuple[float, L3Entry]] = []
        for (entry_ns, _key), entry in self._l3.items():
            if entry_ns != ns:
                continue
            if entry.ttl and now - entry.created_at > entry.ttl:
                continue
            score = cosine(qv, entry.embed)
            if score >= min_score:
                scored.append((score, entry))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {
                "namespace": entry.ns,
                "key": entry.key,
                "kind": entry.kind,
                "tags": list(entry.tags),
                "value": entry.value,
                "score": round(score, 6),
                "created_at": entry.created_at,
            }
            for score, entry in scored[:top_k]
        ]

    async def forget_l3(
        self, pid: int, key: str | None = None, *, namespace: str
    ) -> int:
        """Delete one key (or the whole namespace); returns deleted count."""
        ns = namespace
        self._can_access(pid, ns, write=True)
        deleted = 0
        if key is not None:
            l2 = self._mem.get(ns)
            if l2 and key in l2:
                del l2[key]
                deleted += 1
            if (ns, key) in self._l3:
                del self._l3[(ns, key)]
                deleted += 1
        else:
            l2 = self._mem.get(ns)
            if l2:
                deleted += len(l2)
                self._mem[ns] = {}
            for k in [k for (n, k) in self._l3 if n == ns]:
                del self._l3[(ns, k)]
                deleted += 1
        self._rewrite_entries()
        return deleted

    # ------------------------------------------------------ semantic FS index
    async def index_artifact(
        self, pid: int, path: str, content: str, *, mime: str | None = None
    ) -> str:
        """Embed and register one written artifact; returns its artifact_id."""
        artifact_id = uuid.uuid4().hex[:12]
        content = self._redact_value(content)
        embed = await self.embedder.embed(f"{path} {content[:4000]}")
        entry = ArtifactEntry(
            artifact_id=artifact_id,
            pid=pid,
            path=path,
            mime=mime,
            snippet=content[:200],
            created_at=time.time(),
            embed=embed,
        )
        self._artifacts[artifact_id] = entry
        self._append_jsonl(self._artifacts_path, entry.to_dict())
        return artifact_id

    async def search_artifacts(self, pid: int, query: str, *, top_k: int = 5) -> list[dict]:
        """Rank this agent's indexed artifacts by semantic similarity."""
        qv = await self.embedder.embed(query)
        scored: list[tuple[float, ArtifactEntry]] = []
        for entry in self._artifacts.values():
            if entry.pid == pid:
                scored.append((cosine(qv, entry.embed), entry))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {
                "artifact_id": entry.artifact_id,
                "path": entry.path,
                "mime": entry.mime,
                "snippet": entry.snippet,
                "score": round(score, 6),
                "created_at": entry.created_at,
            }
            for score, entry in scored[:top_k]
        ]

    # ------------------------------------------------------------ persistence
    def _append_jsonl(self, path: Path | None, obj: dict) -> None:
        if path is None:
            return
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _rewrite_entries(self) -> None:
        """Rewrite entries.jsonl without removed rows (forget is rare)."""
        if self._entries_path is None:
            return
        tmp = self._entries_path.with_name(self._entries_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for entry in self._l3.values():
                fh.write(json.dumps(entry.to_dict(), sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._entries_path)

    def _load_l3(self) -> None:
        """Rebuild the in-memory index from the durable JSONL logs."""
        if self._entries_path and self._entries_path.exists():
            for line in self._entries_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = L3Entry.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # skip corrupt rows; never crash the kernel
                self._l3[(entry.ns, entry.key)] = entry
        if self._artifacts_path and self._artifacts_path.exists():
            for line in self._artifacts_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = ArtifactEntry.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                self._artifacts[entry.artifact_id] = entry


# ------------------------------------------------------------------ syscalls
@register("write_memory")
async def _write_memory(kernel, pid: int, args: dict) -> dict:
    if args.get("kind"):
        # an explicit kind means L3: embedding-indexed and persistent
        await kernel.memory.store_l3(
            pid,
            args["key"],
            args["value"],
            namespace=args["namespace"],
            kind=args["kind"],
            tags=args.get("tags"),
            ttl=args.get("ttl"),
        )
    else:
        # plain write = L2 working memory (ephemeral, checkpointed)
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
    value = kernel.memory.read_any(pid, args["key"], namespace=args["namespace"])
    return {"value": value}


@register("search_memory")
async def _search_memory(kernel, pid: int, args: dict) -> dict:
    hits = await kernel.memory.search_l3(
        pid,
        args["query"],
        namespace=args.get("namespace"),
        top_k=args.get("top_k", 5),
        min_score=args.get("min_score", 0.0),
    )
    return {"hits": hits}


@register("forget_memory")
async def _forget_memory(kernel, pid: int, args: dict) -> dict:
    deleted = await kernel.memory.forget_l3(
        pid, args.get("key"), namespace=args["namespace"]
    )
    return {"deleted": deleted}
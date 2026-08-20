"""Semantic FS — the agent's virtual filesystem plus meaning-based search.

Serves the canonical storage syscalls (docs/02-kernel.md §5, docs/05-storage.md
§4-5):

    #26 store_artifact  {path, data, mime?} -> {artifact_id}
    #27 fs_read         {path, max_bytes?}  -> {content, bytes, mime}
    #28 fs_write        {path, content, mime?} -> {path, bytes, artifact_id, mime}
    #29 fs_search       {query, top_k?}     -> {hits[]}

All paths are *virtual* — resolved inside the agent's sandboxed workspace by
``WorkspaceManager.resolve``, so path traversal is impossible by construction.
Every successful write is embedding-indexed by the Memory Manager (the same
vector store as L3 memory); ``fs_search`` finds artifacts by meaning, not path.

The ``fs.read`` / ``fs.write`` tools delegate here, so the tool path and the
syscall path share one implementation and one semantic index.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import AiosError, E_INVAL, E_NOENT
from ..syscalls.registry import register

MAX_CONTENT = 1_000_000

_EXT_MIME = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".py": "text/x-python",
    ".html": "text/html",
    ".csv": "text/csv",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/plain",
    ".log": "text/plain",
}


def _sniff_mime(path: str, content: str) -> str:
    """Lightweight MIME sniffing: binary-looking content -> octet-stream,
    otherwise extension-hinted text. (Full magic-byte sniffing lands with
    binary artifact support; docs/05-storage.md §8 item 3.)"""
    for ch in content[:1024]:
        if ord(ch) < 9 or 14 <= ord(ch) < 32:
            return "application/octet-stream"
    return _EXT_MIME.get(Path(path).suffix.lower(), "text/plain")


class SemanticFS:
    """Owns the agent-visible virtual filesystem + semantic index queries."""

    def __init__(self, kernel):
        self.kernel = kernel

    # ------------------------------------------------------------- syscalls
    async def read(self, pid: int, rel: str, *, max_bytes: int = 32_000) -> dict:
        target = self.resolve(pid, rel)
        if not os.path.isfile(target):
            raise AiosError(E_NOENT, f"no such file: {rel}")
        with open(target, "r", encoding="utf-8") as fh:
            content = fh.read(max_bytes)
        return {
            "path": rel,
            "content": content,
            "bytes": len(content),
            "mime": _sniff_mime(rel, content),
        }

    async def write(self, pid: int, rel: str, content: str, *, mime: str | None = None) -> dict:
        if len(content) > MAX_CONTENT:
            raise AiosError(E_INVAL, "content exceeds 1MB limit")
        target = self.resolve(pid, rel)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        artifact_id = await self.kernel.memory.index_artifact(
            pid, rel, content, mime=mime or _sniff_mime(rel, content)
        )
        return {
            "path": rel,
            "bytes": len(content),
            "artifact_id": artifact_id,
            "mime": mime or _sniff_mime(rel, content),
        }

    async def store(self, pid: int, rel: str, data: str, *, mime: str | None = None) -> str:
        """store_artifact: write content, return the artifact_id."""
        meta = await self.write(pid, rel, data, mime=mime)
        return meta["artifact_id"]

    async def search(self, pid: int, query: str, *, top_k: int = 5) -> list[dict]:
        """fs_search: ranked artifact hits by meaning (not path)."""
        return await self.kernel.memory.search_artifacts(pid, query, top_k=top_k)

    # ---------------------------------------------------------- path safety
    def resolve(self, pid: int, rel: str) -> str:
        """Resolve a virtual path inside the agent's workspace (never escapes)."""
        return self.kernel.workspaces.resolve(pid, rel)


# ------------------------------------------------------------------ syscalls
@register("store_artifact")
async def _store_artifact(kernel, pid: int, args: dict) -> dict:
    artifact_id = await kernel.fs.store(
        pid, args["path"], args["data"], mime=args.get("mime")
    )
    return {"artifact_id": artifact_id}


@register("fs_read")
async def _fs_read(kernel, pid: int, args: dict) -> dict:
    return await kernel.fs.read(pid, args["path"], max_bytes=args.get("max_bytes") or 32_000)


@register("fs_write")
async def _fs_write(kernel, pid: int, args: dict) -> dict:
    return await kernel.fs.write(pid, args["path"], args["content"], mime=args.get("mime"))


@register("fs_search")
async def _fs_search(kernel, pid: int, args: dict) -> dict:
    hits = await kernel.fs.search(pid, args["query"], top_k=args.get("top_k", 5))
    return {"hits": hits}
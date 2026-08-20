"""Context Manager — per-agent LLM context (message history).

In-memory for Phase 1; checkpoint snapshots make suspend/resume restore the
exact same message list (byte-identical by construction — messages are deep
copied on save and on restore).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import AiosError, E_NOENT
from ..syscalls.registry import register


@dataclass
class Message:
    role: str
    content: str
    pinned: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "pinned": self.pinned}

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        """Rebuild a Message from a checkpointed (or metadata-bearing) dict."""
        return cls(
            d["role"],
            d["content"],
            pinned=bool(d.get("pinned", False)),
            meta=dict(d.get("meta", {})),
        )


class ContextManager:
    def __init__(self, kernel=None):
        self.kernel = kernel
        self._ctx: dict[int, list[Message]] = {}

    def create(self, pid: int, system: str | None = None) -> None:
        msgs = []
        if system:
            msgs.append(Message("system", system, pinned=True))
        self._ctx[pid] = msgs

    def get(self, pid: int) -> list[Message]:
        ctx = self._ctx.get(pid)
        if ctx is None:
            raise AiosError(E_NOENT, f"no context for agent {pid}")
        return ctx

    def read(self, pid: int) -> list[dict]:
        return [m.to_dict() for m in self.get(pid)]

    def append(self, pid: int, role: str, content: str, pinned: bool = False) -> None:
        self.get(pid).append(Message(role, content, pinned))

    def tokens(self, pid: int) -> int:
        return sum(len(m.content) // 4 + 4 for m in self.get(pid))

    def restore(self, pid: int, messages: list[Message]) -> None:
        """Deep-copy a checkpointed message list back into place."""
        self._ctx[pid] = [Message(m.role, m.content, m.pinned, dict(m.meta)) for m in messages]

    def free(self, pid: int) -> None:
        self._ctx.pop(pid, None)


# ------------------------------------------------------------------ syscalls
@register("read_context")
async def _read_context(kernel, pid: int, args: dict) -> dict:
    return {"messages": kernel.context.read(pid), "tokens": kernel.context.tokens(pid)}


@register("append_context")
async def _append_context(kernel, pid: int, args: dict) -> dict:
    kernel.context.append(pid, args["role"], args["content"], pinned=args.get("pinned", False))
    return {"tokens": kernel.context.tokens(pid)}
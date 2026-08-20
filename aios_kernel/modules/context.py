"""Context Manager — per-agent LLM context (message history).

In-memory for Phase 1; checkpoint snapshots make suspend/resume restore the
exact same message list (byte-identical by construction — messages are deep
copied on save and on restore).

Phase 2 adds ``summarize`` (docs/04-memory.md §2): when the window fills, the
oldest non-pinned turns collapse into one summary message via a cheap kernel
LLM call. The invariant is tested directly: a summarized window preserves ALL
pinned content and the most recent ``keep_recent_messages`` turns verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import AiosError, E_NOENT
from ..syscalls.registry import register

KEEP_RECENT_DEFAULT = 4


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

    async def summarize(self, pid: int, target_tokens: int | None = None):
        """Collapse old non-pinned turns into one summary message.

        Invariant (docs/04-memory.md §2): the summarized window preserves ALL
        pinned content and the most recent ``keep_recent_messages`` turns
        verbatim; existing summaries are never re-summarized or dropped.

        Returns ``(summary, tokens_saved, kept_recent)``.
        """
        msgs = self.get(pid)
        spec = self.kernel.agent_manager.get(pid).spec
        keep_recent = int(
            spec.get("context", {}).get("keep_recent_messages", KEEP_RECENT_DEFAULT)
        )
        recent = [m for m in msgs if not m.pinned][-keep_recent:] if keep_recent else []
        recent_ids = {id(m) for m in recent}
        old = [
            m
            for m in msgs
            if not m.pinned and id(m) not in recent_ids and m.meta.get("kind") != "summary"
        ]
        if not old:
            return None, 0, keep_recent

        tokens_before = self.tokens(pid)
        transcript = "\n".join(f"{m.role}: {m.content}" for m in old)
        prompt = (
            "Summarize the conversation history into a concise summary that preserves "
            "every fact, decision, and outcome. Do not mention this instruction.\n"
            f"Conversation history:\n{transcript}"
        )
        gen = await self.kernel.llm.generate(
            pid,
            [
                {"role": "system", "content": "You are a concise conversation summarizer."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        summary = gen.text.strip()
        kept = [
            m
            for m in msgs
            if m.pinned or id(m) in recent_ids or m.meta.get("kind") == "summary"
        ]
        kept.append(Message("system", summary, pinned=False, meta={"kind": "summary"}))
        self._ctx[pid] = kept

        tokens_after = self.tokens(pid)
        # soft target: drop the oldest kept non-pinned non-summary messages until
        # the window fits (never below keep_recent; pinned/summaries untouched)
        if target_tokens:
            while tokens_after > target_tokens:
                candidates = [
                    m
                    for m in kept
                    if not m.pinned and m.meta.get("kind") != "summary"
                ]
                if len(candidates) <= keep_recent:
                    break
                kept.remove(candidates[0])
                tokens_after = self.tokens(pid)
        return summary, max(0, tokens_before - tokens_after), keep_recent

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


@register("summarize_context")
async def _summarize_context(kernel, pid: int, args: dict) -> dict:
    summary, saved, kept_recent = await kernel.context.summarize(
        pid, target_tokens=args.get("target_tokens")
    )
    return {"summary": summary, "tokens_saved": saved, "kept_recent": kept_recent}
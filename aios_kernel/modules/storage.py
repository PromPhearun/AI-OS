"""Storage Manager — Phase 1 checkpoints are in-memory only.

A checkpoint is a frozen snapshot of an agent's *observable* kernel state:
context messages, memory namespace, usage counters, budgets, and spec.
Disk persistence lands in Phase 2; in-memory checkpoints already guarantee
byte-identical context across suspend/resume.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass

from ..errors import AiosError, E_NOENT
from .context import Message
from ..syscalls.registry import register


@dataclass
class Checkpoint:
    id: str
    pid: int
    ts: float
    label: str | None
    turn: int
    context: list[Message]
    memory: dict
    usage: object
    budgets: object
    spec: dict

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pid": self.pid,
            "ts": self.ts,
            "label": self.label,
            "turn": self.turn,
            "context": [m.to_dict() for m in self.context],
        }


class StorageManager:
    def __init__(self, kernel):
        self.kernel = kernel
        self._checkpoints: dict[str, Checkpoint] = {}
        self._seq = 0

    def checkpoint(self, pid: int, label: str | None = None) -> str:
        """Snapshot agent state and return the new checkpoint id."""
        acb = self.kernel.agent_manager.get(pid)
        ckpt = Checkpoint(
            id=f"ck-{pid}-{self._seq:06d}",
            pid=pid,
            ts=time.time(),
            label=label,
            turn=acb.usage.turns,
            context=[Message(m.role, m.content, m.pinned, dict(m.meta)) for m in self.kernel.context.get(pid)],
            memory=self.kernel.memory.snapshot(pid),
            usage=copy.deepcopy(acb.usage),
            budgets=copy.deepcopy(acb.budgets),
            spec=copy.deepcopy(acb.spec),
        )
        self._seq += 1
        self._checkpoints[ckpt.id] = ckpt
        return ckpt.id

    def restore(self, pid: int, checkpoint_id: str) -> Checkpoint:
        """Restore a checkpoint onto a live ACB (used by resume)."""
        ckpt = self._checkpoints.get(checkpoint_id)
        if ckpt is None:
            raise AiosError(E_NOENT, f"no checkpoint '{checkpoint_id}'")
        acb = self.kernel.agent_manager.get(pid)
        self.kernel.context.restore(pid, ckpt.context)
        self.kernel.memory.restore(pid, ckpt.memory)
        acb.usage = copy.deepcopy(ckpt.usage)
        acb.budgets = copy.deepcopy(ckpt.budgets)
        acb.spec = copy.deepcopy(ckpt.spec)
        return ckpt

    def get(self, checkpoint_id: str) -> Checkpoint:
        ckpt = self._checkpoints.get(checkpoint_id)
        if ckpt is None:
            raise AiosError(E_NOENT, f"no checkpoint '{checkpoint_id}'")
        return ckpt

    def list_for(self, pid: int) -> list[dict]:
        return [c.to_dict() for c in self._checkpoints.values() if c.pid == pid]

    def free_for(self, pid: int) -> None:
        for cid in [c.id for c in self._checkpoints.values() if c.pid == pid]:
            del self._checkpoints[cid]


# ------------------------------------------------------------------ syscalls
@register("checkpoint")
async def _checkpoint(kernel, pid: int, args: dict) -> dict:
    ckpt_id = kernel.storage.checkpoint(pid, label=args.get("label"))
    return {"checkpoint_id": ckpt_id}
"""Storage Manager — durable checkpoints + the session resume set (Phase 2).

A checkpoint is a frozen snapshot of an agent's *observable* kernel state:
context messages, memory namespace, usage counters, budgets, and spec.

On-disk layout (docs/05-storage.md §2-3)::

    <root>/<checkpoint_id>/
        snapshot.json   # context + memory + usage + budgets + spec
        manifest.json   # metadata + sha256 integrity hash + committed flag

Write path (kernel invariant #4): snapshot -> fsync -> manifest(committed)
-> fsync -> dir fsync -> ack. Restore verifies the snapshot's sha256 against
the manifest and refuses to load tampered checkpoints.

The session resume set (``aios-data/session.json``) is upserted atomically on
every committed checkpoint, so a crashed kernel can rebuild every suspended
agent at its last committed checkpoint via ``--resume``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..acb import Budgets, Usage
from ..errors import AiosError, E_INTERNAL, E_NOENT
from .context import Message
from ..syscalls.registry import register

KERNEL_VERSION = "0.1.0"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically and fsync the file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so new entries are durable (best-effort)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _messages_to_dicts(messages: list[Message]) -> list[dict]:
    return [
        {"role": m.role, "content": m.content, "pinned": m.pinned, "meta": dict(m.meta)}
        for m in messages
    ]


def _messages_from_dicts(items: list[dict]) -> list[Message]:
    return [Message.from_dict(i) for i in items]


@dataclass
class Checkpoint:
    """A frozen, durable snapshot of an agent's observable kernel state."""

    id: str
    pid: int
    ts: float
    label: str | None
    turn: int
    context: list[Message]
    memory: dict
    usage: Usage
    budgets: Budgets
    spec: dict
    state: str = "suspended"
    hash: str = ""
    committed: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pid": self.pid,
            "ts": self.ts,
            "label": self.label,
            "turn": self.turn,
            "state": self.state,
            "hash": self.hash,
            "committed": self.committed,
            "context": [m.to_dict() for m in self.context],
        }


class StorageManager:
    """Owns checkpoints (durable on disk) and the session resume set."""

    def __init__(self, kernel, *, root: str | None = None, session_path: str | None = None):
        self.kernel = kernel
        self._root = Path(root) if root else Path("aios-data") / "checkpoints"
        self._root.mkdir(parents=True, exist_ok=True)
        self._session_path = Path(session_path) if session_path else self._root.parent / "session.json"
        self._checkpoints: dict[str, Checkpoint] = {}
        self._seq = 0

    # ------------------------------------------------------------- checkpoints
    def checkpoint(self, pid: int, label: str | None = None) -> str:
        """Snapshot agent state, write it durably, ack, and update the resume set."""
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
            state=acb.state.value,
        )
        self._seq += 1
        self._write(ckpt)
        self._checkpoints[ckpt.id] = ckpt
        self.upsert_session_record(pid, ckpt.id)
        return ckpt.id

    def _write(self, ckpt: Checkpoint) -> None:
        """Durable write: snapshot -> fsync -> manifest(committed) -> fsync."""
        snapshot = {
            "context": _messages_to_dicts(ckpt.context),
            "memory": ckpt.memory,
            "usage": ckpt.usage.to_ckpt_dict(),
            "budgets": ckpt.budgets.to_ckpt_dict(),
            "spec": ckpt.spec,
        }
        snap_bytes = json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
        snap_hash = hashlib.sha256(snap_bytes).hexdigest()
        ckpt.hash = snap_hash
        ckpt.committed = True

        d = self._root / ckpt.id
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / "snapshot.json", snap_bytes)
        manifest = {
            "checkpoint_id": ckpt.id,
            "pid": ckpt.pid,
            "ts": ckpt.ts,
            "label": ckpt.label,
            "turn": ckpt.turn,
            "state": ckpt.state,
            "hash": snap_hash,
            "committed": True,
            "created_at": time.time(),
        }
        _atomic_write(d / "manifest.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))
        _fsync_dir(d)

    def restore(self, pid: int, checkpoint_id: str) -> Checkpoint:
        """Restore a checkpoint onto a live ACB (used by resume/--resume)."""
        ckpt = self._checkpoints.get(checkpoint_id)
        if ckpt is None:
            ckpt = self._load(checkpoint_id)
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
            ckpt = self._load(checkpoint_id)
        return ckpt

    def list_for(self, pid: int) -> list[dict]:
        return [c.to_dict() for c in self._checkpoints.values() if c.pid == pid]

    def free_for(self, pid: int) -> None:
        for cid in [c.id for c in self._checkpoints.values() if c.pid == pid]:
            self._checkpoints.pop(cid, None)
            shutil.rmtree(self._root / cid, ignore_errors=True)

    def _load(self, checkpoint_id: str) -> Checkpoint:
        """Load + integrity-verify a checkpoint from disk, then cache it."""
        d = self._root / checkpoint_id
        if not (d / "manifest.json").is_file() or not (d / "snapshot.json").is_file():
            raise AiosError(E_NOENT, f"no checkpoint '{checkpoint_id}' on disk")
        try:
            manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AiosError(E_INTERNAL, "checkpoint manifest is unreadable") from exc
        snap_bytes = (d / "snapshot.json").read_bytes()
        actual = hashlib.sha256(snap_bytes).hexdigest()
        expected = manifest.get("hash", "")
        if not expected or actual != expected:
            raise AiosError(E_INTERNAL, f"checkpoint '{checkpoint_id}' failed integrity check")
        try:
            snap = json.loads(snap_bytes)
        except json.JSONDecodeError as exc:
            raise AiosError(E_INTERNAL, "checkpoint snapshot is unreadable") from exc
        ckpt = Checkpoint(
            id=checkpoint_id,
            pid=int(manifest.get("pid", 0)),
            ts=float(manifest.get("ts", 0.0)),
            label=manifest.get("label"),
            turn=int(manifest.get("turn", 0)),
            context=_messages_from_dicts(snap.get("context", [])),
            memory=snap.get("memory", {}),
            usage=Usage.from_ckpt_dict(snap.get("usage", {})),
            budgets=Budgets.from_ckpt_dict(snap.get("budgets", {})),
            spec=snap.get("spec", {}),
            state=manifest.get("state", "suspended"),
            hash=expected,
            committed=bool(manifest.get("committed", False)),
        )
        self._checkpoints[checkpoint_id] = ckpt
        return ckpt

    # ------------------------------------------------------- session resume set
    def upsert_session_record(self, pid: int, checkpoint_id: str) -> None:
        """Add/refresh the durable resume-set entry for an agent (at checkpoint)."""
        acb = self.kernel.agent_manager.get(pid)
        records = [r for r in self._load_session_records() if r["pid"] != pid]
        records.append({
            "pid": pid,
            "name": acb.spec.get("name", "?"),
            "checkpoint_id": checkpoint_id,
            "spec": copy.deepcopy(acb.spec),
            "priority": acb.priority,
            "parent_pid": acb.parent_pid,
            "group_id": acb.group_id,
            "usage": acb.usage.to_ckpt_dict(),
            "budgets": acb.budgets.to_ckpt_dict(),
            "exit_status": acb.exit_status,
            "exit_message": acb.exit_message,
            "created_at": acb.created_at,
            "started_at": acb.started_at,
            "updated_at": time.time(),
        })
        self._write_session_records(records)

    def remove_session_record(self, pid: int) -> None:
        self._write_session_records([r for r in self._load_session_records() if r["pid"] != pid])

    def suspended_pids(self) -> list[int]:
        return [int(r["pid"]) for r in self._load_session_records()]

    def _load_session_records(self) -> list[dict]:
        try:
            data = json.loads(self._session_path.read_text(encoding="utf-8"))
            return [r for r in data.get("agents", []) if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            return []

    def _write_session_records(self, records: list[dict]) -> None:
        payload = {
            "kernel_version": KERNEL_VERSION,
            "saved_at": time.time(),
            "agents": records,
        }
        _atomic_write(
            self._session_path,
            json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8"),
        )

    # ---------------------------------------------------------------- --resume
    def restore_session(self) -> list[int]:
        """--resume boot path: rebuild every agent from the resume set.

        Returns the restored pids, each parked SUSPENDED at its last committed
        checkpoint (hash-verified); the SDK then re-attaches runners and
        resumes them.
        """
        records = self._load_session_records()
        restored: list[int] = []
        for rec in sorted(records, key=lambda r: r["pid"]):
            pid = self.kernel.agent_manager.restore_from_manifest(rec)
            self.restore(pid, rec["checkpoint_id"])
            restored.append(pid)
        return restored


# ------------------------------------------------------------------ syscalls
@register("checkpoint")
async def _checkpoint(kernel, pid: int, args: dict) -> dict:
    ckpt_id = kernel.storage.checkpoint(pid, label=args.get("label"))
    return {"checkpoint_id": ckpt_id}
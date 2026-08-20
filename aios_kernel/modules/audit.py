"""Audit Log — append-only JSONL trail of every kernel event.

Per docs/08-security.md: never log secrets, tokens, or PII. Syscall args are
recorded only as a canonical hash; error details go here, never to agents.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..syscalls.registry import args_hash, register


class AuditLog:
    def __init__(self, kernel, path: str | None = None):
        self.kernel = kernel
        self.path = path or os.environ.get("AIOS_AUDIT_PATH") or str(Path("aios-data") / "audit.jsonl")
        self._file = None
        self._entries: list[dict] = []

    def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")

    def record(self, event: str, *, pid: int | None = None, **fields) -> dict:
        entry = {"ts": time.time(), "event": event, "pid": pid}
        entry.update(fields)
        self._entries.append(entry)
        if self._file is not None:
            self._file.write(json.dumps(entry, default=str) + "\n")
            self._file.flush()
        return entry

    def read(self, pid: int | None = None, limit: int | None = None) -> list[dict]:
        entries = [e for e in self._entries if pid is None or e.get("pid") == pid]
        if limit is not None:
            entries = entries[-limit:]
        return entries

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None


# ------------------------------------------------------------------ syscalls
@register("log")
async def _log(kernel, pid: int, args: dict) -> dict:
    entry = {"ts": time.time(), "level": args["level"], "message": args["message"]}
    kernel.agent_logs.setdefault(pid, []).append(entry)
    # The audit trail records only a canonical hash of the message (never the
    # raw content — agent log lines may contain secrets/PII).
    kernel.audit.record(
        "agent.log",
        pid=pid,
        level=args["level"],
        message_hash=args_hash({"message": args["message"]}),
    )
    return {"ok": True}
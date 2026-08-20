"""Audit Log — append-only, hash-chained JSONL trail of every kernel event.

Per docs/08-security.md §6: every record carries ``seq`` (monotonic), a
``prev_hash`` link to the previous record, and its own ``hash`` (sha256 of the
canonical JSON body). Tampering anywhere in the chain is detectable with
``verify()`` — this is the tamper-evidence primitive (threat T8).

Never log secrets, tokens, or PII. Syscall args are recorded only as a
canonical hash; error details go here, never to agents.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ..syscalls.registry import args_hash, register

GENESIS_HASH = "0" * 64  # first record links to this synthetic predecessor


class AuditLog:
    def __init__(self, kernel, path: str | None = None):
        self.kernel = kernel
        self.path = path or os.environ.get("AIOS_AUDIT_PATH") or str(Path("aios-data") / "audit.jsonl")
        self._file = None
        self._entries: list[dict] = []
        self._seq = 0
        self._last_hash = GENESIS_HASH

    def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self._restore_tail()

    def _restore_tail(self) -> None:
        """Resume the chain from the log's last record (survives restarts)."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                last = None
                for line in fh:
                    if line.strip():
                        last = line
                if last:
                    rec = json.loads(last)
                    self._seq = int(rec.get("seq", 0))
                    self._last_hash = str(rec.get("hash", GENESIS_HASH))
        except (OSError, ValueError):
            pass

    @staticmethod
    def _canonical(entry: dict) -> str:
        body = {k: v for k, v in entry.items() if k != "hash"}
        return json.dumps(body, sort_keys=True, default=str)

    def record(self, event: str, *, pid: int | None = None, **fields) -> dict:
        self._seq += 1
        entry: dict = {
            "ts": time.time(),
            "event": event,
            "pid": pid,
            "seq": self._seq,
            "prev_hash": self._last_hash,
        }
        entry.update(fields)
        entry["hash"] = hashlib.sha256(self._canonical(entry).encode("utf-8")).hexdigest()
        self._entries.append(entry)
        self._last_hash = entry["hash"]
        if self._file is not None:
            self._file.write(json.dumps(entry, default=str) + "\n")
            self._file.flush()
        return entry

    def read(self, pid: int | None = None, limit: int | None = None) -> list[dict]:
        entries = [e for e in self._entries if pid is None or e.get("pid") == pid]
        if limit is not None:
            entries = entries[-limit:]
        return entries

    def verify(self) -> dict:
        """Re-derive the hash chain from disk and report the first bad index.

        Returns ``{"valid": bool, "entries": n, "first_bad": int | None}``.
        ``first_bad`` is the 0-based index of the first record that breaks the
        chain (wrong ``prev_hash``, wrong ``hash``, or unparseable JSON).
        """
        prev = GENESIS_HASH
        n = 0
        first_bad: int | None = None
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        # Unparseable record: the chain is broken at this index.
                        first_bad = n if first_bad is None else first_bad
                        n += 1
                        continue
                    if rec.get("prev_hash") != prev:
                        first_bad = n if first_bad is None else first_bad
                    expected = hashlib.sha256(self._canonical(rec).encode("utf-8")).hexdigest()
                    if rec.get("hash") != expected:
                        first_bad = n if first_bad is None else first_bad
                    prev = rec.get("hash", prev)
                    n += 1
        except OSError:
            return {"valid": False, "entries": n, "first_bad": first_bad}
        return {"valid": first_bad is None, "entries": n, "first_bad": first_bad}

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None


# ------------------------------------------------------------------ syscalls
@register("verify_audit")
async def _verify_audit(kernel, pid: int, args: dict) -> dict:
    return kernel.audit.verify()


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
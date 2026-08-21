"""Checkpoint I/O benchmark — wall time and on-disk bytes per snapshot size.

Spawns a fresh agent per size class, fills its context with N messages and
stores M artifacts, then checkpoints it and measures the durable write cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from aios_kernel import Kernel


@dataclass
class CheckpointEntry:
    messages: int
    artifacts: int
    checkpoint_ms: float
    bytes: int
    mib_s: float

    def as_dict(self) -> dict:
        return {
            "messages": self.messages,
            "artifacts": self.artifacts,
            "checkpoint_ms": self.checkpoint_ms,
            "bytes": self.bytes,
            "mib_s": self.mib_s,
        }


@dataclass
class CheckpointReport:
    entries: list[CheckpointEntry] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "benchmark": "checkpoint_io",
            "entries": [e.as_dict() for e in self.entries],
        }


def _spec() -> dict:
    return {
        "name": "bench-task",
        "version": "1",
        "description": "checkpoint-io benchmark agent",
        "group_id": "bench",
        "priority": 0,
        "llm": {"model": "mock", "system": "", "temperature": 0.0},
        "budgets": {"max_turns": 1, "max_tool_calls": 1},
        "capabilities": {"tools": []},
    }


async def _measure(kernel: Kernel, *, messages: int, artifacts: int) -> CheckpointEntry:
    pid = await kernel.spawn_agent(_spec())
    try:
        for i in range(messages):
            kernel.context.append(pid, "user", f"bench payload line {i}: " + "x" * 80)
        for i in range(artifacts):
            await kernel.fs.store(pid, f"art-{i}.txt", f"artifact {i} " + "y" * 200)
        t0 = time.monotonic()
        ckpt_id = kernel.storage.checkpoint(pid, label="bench")
        dt_ms = (time.monotonic() - t0) * 1000.0
        base = kernel.storage._root / ckpt_id
        total = sum(p.stat().st_size for p in base.rglob("*") if p.is_file())
        mib_s = (total / 1024 / 1024) / (dt_ms / 1000.0) if dt_ms else 0.0
        return CheckpointEntry(
            messages=messages,
            artifacts=artifacts,
            checkpoint_ms=round(dt_ms, 2),
            bytes=total,
            mib_s=round(mib_s, 2),
        )
    finally:
        kernel.agent_manager.kill(pid, reason="benchmark")  # sync reaper


async def run(
    kernel: Kernel,
    *,
    sizes: tuple[tuple[int, int], ...] = ((20, 5), (200, 20), (500, 50)),
) -> CheckpointReport:
    report = CheckpointReport()
    for messages, artifacts in sizes:
        report.entries.append(await _measure(kernel, messages=messages, artifacts=artifacts))
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    import json
    import tempfile

    from aios_kernel import Kernel
    from aios_kernel.modules.llm_core import MockLLM

    parser = argparse.ArgumentParser(description="aios checkpoint I/O benchmark")
    parser.add_argument("--data-root", default=None)
    args = parser.parse_args(argv)

    kernel = Kernel(
        data_root=args.data_root or tempfile.mkdtemp(prefix="aios-bench-"),
        llm_backend=MockLLM(mode="echo"),
    )
    try:
        report = asyncio.run(run(kernel))
        print(json.dumps(report.as_dict(), indent=2))
        return 0
    finally:
        asyncio.run(kernel.shutdown())


if __name__ == "__main__":
    raise SystemExit(main())
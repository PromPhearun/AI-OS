"""Fairness benchmark — roadmap acceptance: 10 heterogeneous agents, bounded
starvation, CPU-like utilization reported.

Ten agents with priorities 0..4 (two per priority) and different work sizes run
concurrently on the single RUNNING slot. Aging should keep the low-priority
jobs moving; we report max/avg wait, dispatches/preemptions, utilization
samples, and peak ready-queue depth.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from aios_kernel import Kernel
from aios_sdk import run_agents


@dataclass
class FairnessReport:
    agents: int
    wall_s: float
    dispatches: int
    preemptions: int
    avg_wait_ms: float
    max_wait_ms: float
    starvation_ratio: float  # max/avg — closer to 1.0 is more fair
    util_mean_pct: float
    util_max_pct: float
    ready_peak: int
    turns_total: int
    tasks_min: float
    starvation_threshold_ms: float
    passed: bool
    detail: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "benchmark": "fairness",
            "agents": self.agents,
            "wall_s": round(self.wall_s, 3),
            "dispatches": self.dispatches,
            "preemptions": self.preemptions,
            "avg_wait_ms": self.avg_wait_ms,
            "max_wait_ms": self.max_wait_ms,
            "starvation_ratio": round(self.starvation_ratio, 3),
            "util_mean_pct": self.util_mean_pct,
            "util_max_pct": self.util_max_pct,
            "ready_peak": self.ready_peak,
            "turns_total": self.turns_total,
            "tasks_min": round(self.tasks_min, 2),
            "starvation_threshold_ms": self.starvation_threshold_ms,
            "passed": self.passed,
            "per_agent": self.detail,
        }


def _spec(i: int, *, priority_range: int, timeout_s: float) -> dict:
    return {
        "name": "bench-fair",
        "version": "1",
        "description": f"fairness benchmark agent {i}",
        "group_id": "bench",
        "priority": i % priority_range,
        "llm": {"model": "mock", "system": "benchmark", "temperature": 0.0},
        "budgets": {"max_turns": 25, "max_tool_calls": 25, "max_wall_clock_s": timeout_s},
        "capabilities": {"tools": []},
    }


async def run(
    kernel: Kernel,
    *,
    agents: int = 10,
    priority_range: int = 5,
    sample_s: float = 0.02,
    timeout_s: float = 60.0,
    starvation_threshold_ms: float = 20_000.0,
) -> FairnessReport:
    import benchmarks.agents  # noqa: F401 — registers @agent entries

    specs = [_spec(i, priority_range=priority_range, timeout_s=timeout_s) for i in range(agents)]
    start = time.monotonic()
    util_samples: list[float] = []
    ready_peak = 0

    async def _sample() -> None:
        nonlocal ready_peak
        while True:
            snap = kernel.scheduler.snapshot()
            util_samples.append(snap["utilization"]["percent"])
            ready_peak = max(ready_peak, snap["queues"]["ready_depth"])
            await asyncio.sleep(sample_s)

    sampler = asyncio.create_task(_sample())
    try:
        summaries = await run_agents(kernel, specs, timeout=timeout_s)
    finally:
        sampler.cancel()

    wall_s = time.monotonic() - start
    snap = kernel.scheduler.snapshot()
    # The scheduler keeps bounded per-agent wait history after agents finish;
    # snapshot() aggregates it into the global stats below.
    waits = kernel.scheduler._wait_history  # benchmark reads scheduler internals
    detail: list[dict] = []
    for s in summaries:
        hist = waits.get(s.pid, [])
        rec = kernel.agent_manager.record(s.pid)
        detail.append(
            {
                "pid": s.pid,
                "priority": rec["priority"] if rec else 0,
                "turns": s.turns,
                "wait_count": len(hist),
                "avg_wait_ms": round(sum(hist) / len(hist), 2) if hist else 0.0,
                "max_wait_ms": round(max(hist), 2) if hist else 0.0,
            }
        )

    max_wait = snap["stats"]["max_wait_ms"]
    avg_wait = snap["stats"]["avg_wait_ms"]
    turns = sum(s.turns for s in summaries)
    return FairnessReport(
        agents=len(summaries),
        wall_s=round(wall_s, 3),
        dispatches=snap["stats"]["dispatches"],
        preemptions=snap["stats"]["preemptions"],
        avg_wait_ms=avg_wait,
        max_wait_ms=max_wait,
        starvation_ratio=(max_wait / avg_wait) if avg_wait else 0.0,
        util_mean_pct=round(sum(util_samples) / len(util_samples), 2) if util_samples else 0.0,
        util_max_pct=round(max(util_samples), 2) if util_samples else 0.0,
        ready_peak=ready_peak,
        turns_total=turns,
        tasks_min=round((turns / wall_s) * 60.0, 2) if wall_s else 0.0,
        starvation_threshold_ms=starvation_threshold_ms,
        passed=max_wait < starvation_threshold_ms,
        detail=detail,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    import json as _json

    from aios_kernel import Kernel
    from aios_kernel.modules.llm_core import MockLLM

    parser = argparse.ArgumentParser(description="aios fairness benchmark")
    parser.add_argument("--agents", type=int, default=10)
    parser.add_argument("--data-root", default=None)
    args = parser.parse_args(argv)

    import tempfile

    root = args.data_root or tempfile.mkdtemp(prefix="aios-bench-")
    kernel = Kernel(data_root=root, llm_backend=MockLLM(mode="echo"))
    try:
        import asyncio

        report = asyncio.run(run(kernel, agents=args.agents))
        print(_json.dumps(report.as_dict(), indent=2))
        print(f"PASS={report.passed}  (max_wait {report.max_wait_ms}ms < "
              f"threshold {report.starvation_threshold_ms}ms)")
        return 0 if report.passed else 1
    finally:
        import asyncio as _aio

        _aio.run(kernel.shutdown())


if __name__ == "__main__":
    raise SystemExit(main())
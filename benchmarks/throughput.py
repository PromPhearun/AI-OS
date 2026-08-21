"""Throughput benchmark — completed tasks (LLM turns) per minute.

A batch of ``bench-task`` agents each run six real task steps (LLM round +
artifact write). We report tasks/min over the whole wall clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from aios_kernel import Kernel
from aios_sdk import run_agents


@dataclass
class ThroughputReport:
    agents: int
    wall_s: float
    turns_total: int
    tool_calls_total: int
    tasks_min: float
    min_tasks_min: float
    passed: bool

    def as_dict(self) -> dict:
        return {
            "benchmark": "throughput",
            "agents": self.agents,
            "wall_s": round(self.wall_s, 3),
            "turns_total": self.turns_total,
            "tool_calls_total": self.tool_calls_total,
            "tasks_min": round(self.tasks_min, 2),
            "min_tasks_min": self.min_tasks_min,
            "passed": self.passed,
        }


def _spec(i: int, *, timeout_s: float) -> dict:
    return {
        "name": "bench-task",
        "version": "1",
        "description": f"throughput benchmark agent {i}",
        "group_id": "bench",
        "priority": 0,
        "llm": {"model": "mock", "system": "benchmark", "temperature": 0.0},
        "budgets": {"max_turns": 20, "max_tool_calls": 40, "max_wall_clock_s": timeout_s},
        "capabilities": {"tools": [{"name": "fs.write"}]},
    }


async def run(
    kernel: Kernel,
    *,
    agents: int = 8,
    timeout_s: float = 90.0,
    min_tasks_min: float = 60.0,
) -> ThroughputReport:
    import benchmarks.agents  # noqa: F401 — registers @agent entries

    specs = [_spec(i, timeout_s=timeout_s) for i in range(agents)]
    start = time.monotonic()
    summaries = await run_agents(kernel, specs, timeout=timeout_s)
    wall_s = time.monotonic() - start

    turns = sum(s.turns for s in summaries)
    tool_calls = sum(s.tool_calls for s in summaries)
    tasks_min = (turns / wall_s) * 60.0 if wall_s else 0.0
    return ThroughputReport(
        agents=len(summaries),
        wall_s=round(wall_s, 3),
        turns_total=turns,
        tool_calls_total=tool_calls,
        tasks_min=round(tasks_min, 2),
        min_tasks_min=min_tasks_min,
        passed=tasks_min >= min_tasks_min,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    import json
    import tempfile

    from aios_kernel import Kernel
    from aios_kernel.modules.llm_core import MockLLM

    parser = argparse.ArgumentParser(description="aios throughput benchmark")
    parser.add_argument("--agents", type=int, default=8)
    parser.add_argument("--data-root", default=None)
    args = parser.parse_args(argv)

    kernel = Kernel(
        data_root=args.data_root or tempfile.mkdtemp(prefix="aios-bench-"),
        llm_backend=MockLLM(mode="echo"),
    )
    try:
        report = asyncio.run(run(kernel, agents=args.agents))
        print(json.dumps(report.as_dict(), indent=2))
        print(f"PASS={report.passed}  ({report.tasks_min} tasks/min >= "
              f"{report.min_tasks_min})")
        return 0 if report.passed else 1
    finally:
        asyncio.run(kernel.shutdown())


if __name__ == "__main__":
    raise SystemExit(main())
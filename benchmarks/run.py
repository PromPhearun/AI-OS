"""Phase 4 benchmark suite — runs fairness, throughput, and checkpoint I/O
against a fresh kernel with the deterministic mock LLM, and writes a report
to ``benchmarks/reports/`` (JSON + Markdown).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time

from aios_kernel import Kernel
from aios_kernel.modules.llm_core import MockLLM

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


async def run_all(*, data_root: str | None = None) -> dict:
    import benchmarks.checkpoint_io as ckpt
    import benchmarks.fairness as fairness
    import benchmarks.throughput as throughput

    if data_root is None:
        data_root = tempfile.mkdtemp(prefix="aios-bench-")

    kernel = Kernel(data_root=data_root, llm_backend=MockLLM(mode="echo"))
    try:
        t0 = time.monotonic()
        fair = await fairness.run(kernel)
        thpt = await throughput.run(kernel)
        ck = await ckpt.run(kernel)
        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "data_root": data_root,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "fairness": fair.as_dict(),
            "throughput": thpt.as_dict(),
            "checkpoint_io": ck.as_dict(),
        }
    finally:
        await kernel.shutdown()


def render_markdown(report: dict) -> str:
    f = report["fairness"]
    t = report["throughput"]
    c = report["checkpoint_io"]
    lines = [
        "# aios — Phase 4 benchmark report",
        "",
        f"- timestamp: `{report['timestamp']}`",
        f"- suite wall time: `{report['elapsed_s']}s`",
        f"- data root: `{report['data_root']}`",
        "",
        "## Fairness — priority + aging, 10 heterogeneous agents",
        "",
        f"- agents: `{f['agents']}` (priorities 0–4, work `4 + pid % 5` steps)",
        f"- **avg wait**: `{f['avg_wait_ms']}ms`  **max wait**: `{f['max_wait_ms']}ms`",
        f"- starvation ratio (max/avg): `{f['starvation_ratio']}`",
        f"- dispatches: `{f['dispatches']}`  preemptions: `{f['preemptions']}`",
        f"- utilization: mean `{f['util_mean_pct']}%`, peak `{f['util_max_pct']}%`",
        f"- peak ready-queue depth: `{f['ready_peak']}`",
        f"- throughput: `{f['tasks_min']}` tasks/min over `{f['turns_total']}` turns",
        f"- **bounded starvation**: `{'PASS' if f['passed'] else 'FAIL'}` "
        f"(max wait < {f['starvation_threshold_ms']}ms)",
        "",
        "| pid | priority | turns | waits | avg (ms) | max (ms) |",
        "|-----|----------|-------|-------|----------|----------|",
    ]
    for d in f["per_agent"]:
        lines.append(
            f"| {d['pid']} | {d['priority']} | {d['turns']} | {d['wait_count']} "
            f"| {d['avg_wait_ms']} | {d['max_wait_ms']} |"
        )
    lines += [
        "",
        "## Throughput — batch of `bench-task` agents (LLM round + artifact write per step)",
        "",
        f"- agents: `{t['agents']}`  wall: `{t['wall_s']}s`",
        f"- turns: `{t['turns_total']}`  tool calls: `{t['tool_calls_total']}`",
        f"- **tasks/min**: `{t['tasks_min']}`  "
        f"`{'PASS' if t['passed'] else 'FAIL'}` (≥ {t['min_tasks_min']})",
        "",
        "## Checkpoint I/O cost",
        "",
        "| messages | artifacts | checkpoint (ms) | bytes | MiB/s |",
        "|----------|-----------|-----------------|-------|-------|",
    ]
    for e in c["entries"]:
        lines.append(
            f"| {e['messages']} | {e['artifacts']} | {e['checkpoint_ms']} "
            f"| {e['bytes']} | {e['mib_s']} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict) -> tuple[str, str]:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = report["timestamp"].replace(":", "").replace("+", "Z")
    json_path = os.path.join(REPORTS_DIR, f"benchmark-{stamp}.json")
    md_path = os.path.join(REPORTS_DIR, f"benchmark-{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="aios Phase 4 benchmark suite",
    )
    parser.add_argument("--data-root", default=None, help="kernel data root (default: tmp)")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    args = parser.parse_args(argv)

    report = asyncio.run(run_all(data_root=args.data_root))
    json_path, md_path = write_report(report)
    print(f"report written: {md_path}")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report))
    ok = report["fairness"]["passed"] and report["throughput"]["passed"]
    print(f"fairness PASS={report['fairness']['passed']}  "
          f"throughput PASS={report['throughput']['passed']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
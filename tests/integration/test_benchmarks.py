"""Phase 4 benchmark acceptance tests (docs/11-roadmap.md §6).

Marked ``benchmark`` so the suite can skip them on fast CI runs:
``pytest -m benchmark``. They reuse the deterministic mock LLM, so they are
fast and stable; the exit criteria mirror the roadmap:

* fairness — 10 heterogeneous agents complete with bounded starvation and
  utilization metrics reported;
* throughput — the batch completes at a minimum tasks/min;
* checkpoint I/O — snapshots are durable and the cost is measurable.
"""

from __future__ import annotations

import pytest

import benchmarks.checkpoint_io as ckpt
import benchmarks.fairness as fairness
import benchmarks.throughput as throughput

pytestmark = pytest.mark.benchmark


async def test_fairness_10_heterogeneous_agents_bounded_starvation(kernel) -> None:
    """Roadmap acceptance: max starvation below threshold; utilization reported."""
    report = await fairness.run(
        kernel,
        agents=10,
        timeout_s=60.0,
        starvation_threshold_ms=15_000.0,
    )
    assert len(report.detail) == 10
    assert report.passed, (
        f"max wait {report.max_wait_ms}ms exceeded threshold "
        f"{report.starvation_threshold_ms}ms"
    )
    # Every agent finished its work (heterogeneous sizes, all positive).
    assert all(d["turns"] > 0 for d in report.detail)
    assert report.turns_total > 0
    # CPU-like utilization metrics are reported.
    assert 0.0 <= report.util_mean_pct <= 100.0
    assert 0.0 <= report.util_max_pct <= 100.0
    assert report.util_max_pct >= report.util_mean_pct
    # The scheduler actually switched between agents.
    assert report.dispatches > 1
    assert report.tasks_min > 0


async def test_throughput_batch_meets_minimum_tasks_per_minute(kernel) -> None:
    report = await throughput.run(kernel, agents=8, timeout_s=90.0, min_tasks_min=60.0)
    assert report.passed, f"{report.tasks_min} tasks/min < {report.min_tasks_min}"
    # Each task agent completed its full 6-step workload.
    assert report.turns_total == report.agents * 6


async def test_checkpoint_io_measurable_and_durable(kernel) -> None:
    report = await ckpt.run(kernel, sizes=((20, 5), (200, 20)))
    assert len(report.entries) == 2
    for e in report.entries:
        assert e.checkpoint_ms > 0
        assert e.bytes > 0
        assert e.mib_s >= 0.0
    # Bigger snapshots take at least as many bytes on disk.
    assert report.entries[1].bytes > report.entries[0].bytes
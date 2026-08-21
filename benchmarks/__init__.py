"""Benchmark harness — Phase 4 (docs/11-roadmap.md §6).

Measures scheduler fairness (bounded starvation for heterogeneous agents),
throughput (tasks/min), and checkpoint I/O cost, using the deterministic
``mock`` LLM backend. Emits a JSON + Markdown report.

Run the whole suite::

    python -m benchmarks.run            # or: aios bench
    python -m benchmarks.run --json

Run one scenario::

    python -m benchmarks.fairness
    python -m benchmarks.throughput
    python -m benchmarks.checkpoint_io
"""
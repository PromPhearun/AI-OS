# Benchmarks — Phase 4 (docs/11-roadmap.md §6)

Deterministic scheduler + storage benchmarks against the **mock** LLM backend.
Reports land in `benchmarks/reports/` (JSON + Markdown).

```sh
python -m benchmarks.run          # full suite → benchmarks/reports/
python -m benchmarks.run --json   # ...and print JSON
python -m benchmarks.fairness     # one scenario
python -m benchmarks.throughput
python -m benchmarks.checkpoint_io
```

From the CLI: `aios bench`.

| Benchmark | Measures |
|---|---|
| Fairness | 10 heterogeneous agents (priorities 0–4): max/avg wait, starvation ratio, dispatches/preemptions, utilization mean/peak, peak ready depth |
| Throughput | batch of task agents: tasks (turns) per minute |
| Checkpoint I/O | durable snapshot wall time + on-disk bytes per context/artifact size |

The pytest acceptance tests (`tests/integration/test_benchmarks.py`, marker
`benchmark`) assert the roadmap exit criteria with the same harness:

```sh
pytest -m benchmark
```
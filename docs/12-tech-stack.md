# 12 — Tech Stack Decision

**Status:** Draft (v0.1)
**Relates to:** `11-roadmap.md`, `13-references.md`.

---

## 1. Decision Summary

| Layer | Choice (v1) | Rationale |
|---|---|---|
| **Kernel language** | **Python 3.11+** | Agent ecosystem is Python-first; AIOS reference is Python; fastest path to a working system |
| **SDK language** | Python | same as kernel; typed, minimal deps |
| **Control API** | FastAPI + WebSocket | mature async server; OpenAPI for the UI/CLI |
| **Model gateway (LLM Core)** | OpenAI-compatible client + LiteLLM routing layer | one interface for hosted + local (vLLM/Ollama); provider failover |
| **Embedding model** | local sentence-transformer (offline-capable) | no per-token API cost; privacy for L3/pools |
| **Vector store** | **embedded (LanceDB)** for v1 | no server to run; S3-compatible object store native; swap to Qdrant/pgvector behind an interface later |
| **Metadata DB** | SQLite | checkpoints/ACB/artifacts metadata; zero-ops; WAL mode |
| **Artifacts/checkpoints** | local FS (user-owned `aios-data/`) | v1 simplicity; object-store adapter interface reserved |
| **IPC bus** | in-process async bus | single-kernel v1; broker interface reserved for Phase 5 |
| **Sandboxing** | subprocess (rlimits, restricted cwd, no net) → container (Phase 5) | pragmatic; container profile later |
| **Tool protocol** | **MCP** (stdio + HTTP) | industry-standard tool interoperability |
| **Web UI** | React + TypeScript + Vite | fast, typed, large ecosystem |
| **Tests** | pytest + hypothesis (property) | standard, property tests for state machine |
| **Packaging** | hatchling (PEP 621) | modern, simple |

## 2. Why Python for the Kernel (and when that might change)

**For:** the agent/framework ecosystem, MCP clients, embeddings, and the reference AIOS project
are all Python; a Python kernel means the SDK, adapters, and tool servers are one language; fast
iteration is the dominant constraint for a v1.

**The escape hatch:** Phase 4 benchmarks identify hot paths; Phase 5 *optionally* re-implements
them in Rust behind the same interfaces — mirroring AIOS's `aios-rs` experiment. The module
interfaces in this spec are deliberately language-neutral (JSON syscalls), so a Rust core is a
drop-in, not a rewrite.

**Against (documented):** GIL limits true parallelism inside the kernel process. Mitigation: the
kernel is I/O-bound (async), and CPU-heavy work (embedding, checkpoint compression) runs in
worker processes/threads off the async loop.

## 3. Model Gateway Detail

- **Unified provider interface:** one `LLMBackend` protocol with `generate(messages, params)`.
- **Backends (v1):** OpenAI-compatible HTTP (covers OpenAI, Azure, Groq, together, local
  vLLM/Ollama), Anthropic via adapter, and a `EchoLLM`/`MockLLM` for tests.
- **Routing:** per-agent `model.provider/model_id` from the spec; failover list optional.
- **Metering:** token counts and cost are computed at the gateway (per-provider pricing table in
  kernel config) — budgets charge on gateway numbers, not provider-reported numbers.

## 4. Repo Structure (proposed)

```
aios/
├── pyproject.toml               # hatchling; extras: [langgraph, autogen, mcp, ui]
├── aios_kernel/                 # the kernel (trusted)
│   ├── syscalls/                # ABI definitions + dispatch (schema per syscall)
│   ├── modules/                 # agent_mgr, scheduler, context, memory, storage,
│   │                            #   ipc, tools, access, audit, llm_core
│   └── bootstrap.py             # boot sequence (01-overview §4)
├── aios_sdk/                    # `import aios` agent-side + control
├── aios_cli/                    # CLI shell
├── aios_api/                    # FastAPI control plane
├── adapters/                    # langgraph / autogen / crewai adapters
├── ui/                          # React + TS web desktop
├── specs/                       # canonical JSON Schemas + example agent specs
├── tests/                       # unit / integration / e2e / security / benchmarks
└── docs/                        # this blueprint
```

## 5. Dependencies (v1, deliberately lean)

- **Kernel:** `anyio` (async), `jsonschema` (validation), `httpx` (LLM/gateway), `pydantic`
  (internal models), `lancedb` + `sentence-transformers` (L3), `orjson` (fast JSON).
- **MCP:** official `mcp` Python SDK.
- **Control/API:** `fastapi`, `uvicorn`, `websockets`.
- **SDK:** `jsonschema` only (+ optional framework extras).
- **Security-critical choices:** `secrets` module for all ID/token generation (no
  `random`); no `eval`/`exec` anywhere; all external boundaries validated with JSON Schema;
  credentials only from vault env (never code/spec).

## 6. Alternatives Considered (and rejected for v1)

| Alternative | Why rejected for v1 |
|---|---|
| Rust kernel (full) | too slow to iterate; AIOS itself keeps Python primary |
| Go kernel | agent ecosystem weaker; JSON/async ergonomics worse than Python |
| External message broker (Redis/RabbitMQ) | single-host v1; interface reserved instead |
| pgvector/Qdrant server | ops burden; no need at single-kernel scale |
| Self-hosted model in-process | GPU/ops complexity; gateway supports local via vLLM/Ollama instead |
| Monorepo TS everything | agent-side ecosystem is Python; polyglot only where it pays (UI) |
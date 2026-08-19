# 11 — Implementation Roadmap

**Status:** Draft (v0.1)
**Relates to:** `12-tech-stack.md`, `13-references.md`.

---

## 1. Guiding Principles

- **Kernel first, surfaces later.** The syscall ABI is the contract; nothing pretty until it's solid.
- **Thin vertical slices.** Each phase delivers a *working* end-to-end slice (launch → run →
  tool → observe), not a hollow scaffold.
- **Tests as invariants.** Every kernel invariant in `02-kernel.md` §9 has a test.
- **Security from phase 1.** No "add security later" — deny-by-default exists from the first syscall.

## 2. Phase Map

| Phase | Name | Delivers | Exit criteria |
|---|---|---|---|
| **0** | Scaffold | Repo, packaging, CI, config, spec schemas | `aios --version`; CI green |
| **1** | MVP Kernel | Agent lifecycle, syscall ABI, agent scheduler, LLM core (single backend), in-memory memory, 3 built-in tools, CLI | two agents run concurrently with budgets; `aios ps`; checkpoint/resume works |
| **2** | State & Memory | L2/L3 memory + RAG, checkpoints on disk, artifacts, semantic FS, IPC (mailboxes, pub/sub, join) | suspend/resume across restart; two agents cooperate via handoff; `fs_search` works |
| **3** | Tooling & Security | MCP client, tool scheduler, sandboxes (subprocess), access control + approvals, audit log hardening | sandbox-escape suite passes; approval flow works; audit tamper test passes |
| **4** | Surfaces & Scale | Web desktop, REST/WS API, provider failover, rate-limit tuning, benchmarks | web desktop feature-complete; benchmark report (throughput, fairness) |
| **5** | Hardening | Container sandbox profile, at-rest encryption, OIDC, multi-kernel preview, Rust hot paths (optional) | security acceptance criteria (`08-security.md` §12) all green |

## 3. Phase 1 — MVP Kernel (detail)

**Scope**
- `aios_kernel`: syscall dispatch, ACB + lifecycle, agent scheduler (priority + aging, single
  process tree), LLM core (one OpenAI-compatible backend), in-memory Context/Memory Managers,
  budget accounting (tokens, cost, wall-clock).
- Built-in tools: `fs.read`, `fs.write`, `shell.run` (sandboxed subprocess, approval-gated).
- `aios` SDK (agent + control), `aios` CLI (`launch`, `ps`, `attach`, `logs`, `suspend`,
  `resume`, `kill`).
- Audit log v1 (append-only file, no hashing yet — hashing lands in Phase 3).

**Key acceptance tests**
- Spawn 2 agents; both progress; budgets enforced (a runaway agent is suspended at budget).
- Suspend at a checkpoint, resume, context is byte-identical.
- Concurrent LLM requests serialize through LLM core without data mixing.
- Kill removes from process table and releases resources.

## 4. Phase 2 — State & Memory (detail)

**Scope**
- Storage Manager: checkpoints on disk (manifest + snapshot), artifacts, virtual FS.
- Memory Manager: L3 (embedding store), shared pools, eviction + summarization, retrieval
  injection into L1.
- IPC: mailboxes, `send_msg`/`recv_msg`, pub/sub, `join`; message persistence in checkpoints.
- Semantic FS (`fs_search`) built on the L3 index.
- `--resume` boot path (restore suspended agents after restart).

**Key acceptance tests**
- Crash the kernel; `--resume`; every agent is back at its last committed checkpoint.
- A → handoff → B → result → A (`join`) completes end-to-end.
- `fs_search` finds a previously written artifact by meaning, not path.
- Context summarization preserves pinned items (invariant test).

## 5. Phase 3 — Tooling & Security (detail)

**Scope**
- MCP client (stdio + HTTP), tool registry hardening, tool scheduler (sandbox slots, rate
  limits, timeouts, cancel).
- Access Control: resolved permission snapshots, `request_permission` approval tickets, RBAC
  roles file, secret vault (`get_env`).
- Audit log v2: hash-chained append-only; sensitive-data hashing.
- Sandbox profile `subprocess` (rlimits, restricted cwd, no network by default).

**Key acceptance tests**
- `08-security.md` §12 checklist.
- Injection probe suite; permission snapshot immutability; secrets scanner.

## 6. Phase 4 — Surfaces & Scale (detail)

**Scope**
- Web desktop (React/TS), REST + WebSocket control plane, JWT auth.
- Provider failover (LLM core multi-backend), rate-limit tuning, scheduler benchmarks.
- Benchmark harness modeled on AIOS methodology: throughput (tasks/min), fairness
  (max/avg waiting time), preemption overhead, checkpoint I/O cost.

**Key acceptance tests**
- `attach` chat round-trips through IPC.
- Approval ticket approved from the web UI and honored by a blocked agent.
- Fairness benchmark: 10 heterogeneous agents — max starvation < threshold; CPU-like utilization
  metrics reported.

## 7. Phase 5 — Hardening (detail)

- Container sandbox profile (Docker/seccomp, network egress allowlist).
- At-rest encryption (AES-256-GCM) for checkpoints/artifacts.
- OIDC for humans; optional KMS for secrets.
- Multi-kernel preview: two kernels sharing IPC through a broker behind the existing IPC API.
- Optional Rust hot paths (checkpoint serialization, scheduler core) — informed by Phase 4
  benchmarks; only where measured hot.

## 8. Testing Strategy

| Layer | Approach |
|---|---|
| Unit | pytest per module; invariants from `02-kernel.md` §9 |
| Integration | kernel in a test fixture; syscall-level tests via SDK |
| E2E | scenario suites: parallel agents, handoff, budget kill, crash-resume |
| Security | sandbox escape suite, injection probes, secrets scanner, audit tamper test |
| Performance | benchmark harness (Phase 4); CI gates on regression |
| Property | state-machine legality property tests for lifecycle transitions |

## 9. Definition of Done (per phase)

- All acceptance tests for the phase pass in CI.
- Every syscall in the phase scope has docs + typed SDK binding + test.
- Security acceptance checklist items that apply to the phase are green.
- README/architecture docs updated to match reality (docs are living).

## 10. Suggested Sequencing Note

Phases 1–3 are the core bet and should be built **in that order**. The web desktop (Phase 4)
should not begin until the control API is stable — otherwise it churns against a moving ABI.

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Context switching cost dominates | benchmark early (Phase 1 harness); checkpoint policy tuning |
| Scheduler complexity | start with priority + aging only; WFQ groups in Phase 2 |
| MCP schema variance between servers | kernel-side re-validation + registry hardening (Phase 3) |
| Model cost explosion | budgets are Phase 1, not an afterthought |
| Scope creep (UI envy) | roadmap gates: UI only after control API freeze |
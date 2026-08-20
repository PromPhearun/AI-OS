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

**Progress — Slice 2.1 (done):** durable on-disk checkpoints
(`<root>/checkpoints/<id>/snapshot.json` + `manifest.json`, sha256-integrity-verified on
restore) and the `--resume` boot path (`Kernel.restore_session()` → SDK
`ControlPlane.resume_session()` → `aios resume`), backed by a session manifest
(`aios-data/session.json`) upserted atomically on every committed checkpoint. Crash-resume
acceptance is green (`tests/e2e/test_acceptance.py`).

**Progress — Slice 2.2 (done):** kernel IPC — per-agent mailboxes, `send_msg`/`recv_msg`
(send never blocks; recv has a mandatory timeout and an optional `{from_pid?, type?, topic?}`
filter), permissioned pub/sub (hierarchical topics, `*`/exact/`prefix.*` patterns),
`join(pids[], timeout_ms?)` with per-pid results, and the handoff protocol (a `handoff`
envelope's body must carry a validated, spawnable spec). Mailboxes and subscriptions are
checkpointed with the agent and restored across crash-resume (`--resume`). Deny-by-default
permissions (`ipc.can_send_to` / `can_subscribe` / `can_publish` in `specs/agent.schema.json`)
are enforced at syscall time. Blocking syscalls park the caller via `scheduler.block` (CPU
slot freed) and are woken by the IPC Manager (`docs/03-scheduler.md` §5); `suspend` wakes
BLOCKED agents first so in-flight syscalls unwind cleanly. Unit + integration + e2e acceptance
green (`tests/unit/test_ipc.py`, `tests/integration/test_ipc.py`, `A → handoff → B → result →
A` in `tests/e2e/test_acceptance.py`).

**Progress — Slice 2.3 (done):** L3 long-term memory (RAG) + the semantic FS +
context summarization. The Memory Manager gained an embedding-indexed, persistent
store (`<root>/memory/entries.jsonl`, WAL append + fsync; reloaded on boot) —
`write_memory` with an explicit `kind` (episodic/semantic/procedural) writes L3,
`search_memory(query, namespace?, top_k, min_score?)` retrieves by cosine similarity,
`forget_memory(namespace, key?)` deletes. Namespaces are isolated per agent and shared
pools are granted via `spec.memory.pools[].access` (deny-by-default → `E_PERM`). The
semantic FS (`docs/05-storage.md` §4-5) serves `store_artifact`, `fs_read`, `fs_write`,
`fs_search`: every successful write is embedding-indexed (same vector store as L3) and
`fs_search` finds artifacts by meaning, not path; the `fs.read`/`fs.write` tools delegate
to the same implementation. Context summarization (`summarize_context`, docs/04-memory.md
§2) collapses the oldest non-pinned turns into one summary message via a cheap kernel LLM
call, preserving ALL pinned content and the most recent `keep_recent_messages` turns
verbatim; `generate` auto-summarizes first when the spec sets `context.context_token_budget`.
Embeddings come from a deterministic, dependency-free hashing embedder (offline) or an
OpenAI-compatible `/embeddings` endpoint when `AIOS_EMBED_URL` is configured
(`aios_kernel/modules/embedder.py`). Unit + integration + e2e acceptance green
(`tests/unit/test_embedder.py`, `test_memory_l3.py`, `test_fs.py`,
`test_context_summarize.py`, `tests/integration/test_memory.py`, `test_fs_semantic.py`,
`tests/e2e/test_acceptance.py`).

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

**Progress — Slice 3.1 (done):** Phase 3 — Tooling & Security. The **MCP client**
supports the two v1 transports (stdio subprocess + HTTP/JSON-RPC); every server tool
schema is re-validated and hardened before registration (must be a JSON-Schema
object, extra properties rejected, string lengths capped at 8 KB, ≤ 128 tools/server),
and server tools are mirrored into the global tool registry with operator-only
`mcp_register`/`mcp_unregister` (threat T7). The **tool scheduler** enforces sandbox
env (host secrets stripped; only `env.allowed_keys` vault values injected), per-agent
rate limits (`E_BUSY`), deadlines, in-flight `cancel_tool` (`E_ABORT`), and the
`max_tool_calls` budget pre-check (`E_BUDGET`); the subprocess sandbox confines cwd to
the agent workspace, applies rlimits, and its binary allowlist excludes network/shell
tools (`curl`, `wget`, `nc`, `python*`, `sh`/`bash`, `rm`, `sudo`). **Access
Control** computes a resolved, immutable permission snapshot at spawn (RBAC
`roles.json` in the data root merges role base capabilities; deny-by-default — an
empty snapshot returns `E_PERM` for every syscall), gates every syscall at dispatch
(including operator-only `approve_ticket`/`deny_ticket`/`mcp_*`/`verify_audit`), and
runs the approval flow: `request_permission` enqueues a ticket with TTL expiry
(`AIOS_APPROVAL_TTL_S`) and `max_pending` caps; an agent that exhausts its turn budget
while a ticket is pending is **parked** (checkpointed + suspended) rather than
terminated, and an operator `approve` resumes it to consume the grant. The **secret
vault** persists `credentials.json` (0600), serves `get_env` strictly via
`allowed_keys`, and `redact()`s values at every kernel-owned boundary — audit log,
L2/L3 memory, artifact index, and LLM context — so secrets never enter logs,
checkpoints, or context. The **audit log v2** is hash-chained: each record carries
`{seq, ts, prev_hash, hash}` (sha256 over canonical JSON), survives restart by
tail-restore, and `verify()` detects byte tampering, broken links, and unparseable
records. Every `08-security.md` §12 acceptance item is green
(`tests/unit/test_vault.py`, `test_access.py`, `test_audit_chain.py`;
`tests/integration/test_mcp.py`, `test_sandbox.py`, `test_approvals.py`,
`test_secrets_scanner.py`).

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
# 08 — Access Control & Security

**Status:** Draft (v0.1)
**Relates to:** every doc; **mandatory reading** before implementation.

---

## 0. Security Posture

AI OS is a **security-first** system. The kernel is the trusted computing base; everything else
is untrusted. We comply with OWASP Top 10 / CWE Top 25 principles adapted to an agent runtime:

- **Deny by default** — agents can do nothing until declared and approved.
- **Least privilege** — the spec *is* the capability list; nothing implicit.
- **Fail secure** — any check error results in denial (`E_PERM`), never allowance.
- **Everything audited** — immutable, tamper-evident audit trail.
- **No secrets in the model** — credentials never enter context, logs, or checkpoints.

## 1. Threat Model

| # | Threat | Mitigation |
|---|---|---|
| T1 | **Prompt injection** — hostile instructions in tool output / documents | Data framing, output caps, PII tagging, L3 write gating (§5) |
| T2 | **Runaway agent** — unbounded token/cost/wall-clock | Budgets + hard-stop suspend (`03-scheduler`) |
| T3 | **Privilege escalation** — agent attempts tools/actions not granted | Deny-by-default + RBAC at every syscall |
| T4 | **Secret exfiltration** — agent leaks credentials | Vault indirection; no raw secrets in context |
| T5 | **Host compromise** — agent escapes sandbox | Sandbox profiles, virtual FS, no host paths |
| T6 | **Agent-to-agent attack** — one agent corrupts another | Mailbox isolation, permissioned send/subscribe, message hashing |
| T7 | **Malicious tool server** | MCP schema hardening, output sanitization, sandbox binding |
| T8 | **Audit tampering** | Hash-chained append-only log |
| T9 | **Data leakage between tenants** | Namespace isolation, pool-level ACLs |
| T10 | **Denial of service** — one group starves others | WFQ scheduling, group quotas, rate limits |

## 2. RBAC & Identities

### Identities
- **Users** (operators) — authenticate via API key or OIDC (v1.5).
- **Agents** — authenticated by the kernel via `agent_id = pid` + runtime attestation of the
  SDK channel (in-process: trusted; out-of-process: TLS + token bound to the sandbox).
- **Tool servers** — registered with a client credential; verified at boot.

### Roles (kernel config, `roles.json`)
| Role | Can | Cannot |
|---|---|---|
| `operator` | spawn/suspend/resume/kill any agent; change budgets; register tools; read audit | — |
| `standard` | spawn agents under own group; use granted tools/pools | elevate others; read secrets |
| `restricted` | spawn agents with read-only memory, no network tools | network, code.exec |
| `service` | headless automation (no interactive approval) | — (tighter budgets) |

Each agent's effective permission set = `role` base capabilities ∪ spec-declared grants,
computed once at spawn and **immutable for the agent's lifetime**.

## 3. Per-Agent Permission Model

Permissions are checked at **every** syscall by Access Control:

```jsonc
// resolved permission snapshot (kernel-internal, derived at spawn)
{
  "tools":   { "web.search": {"needs_approval": false}, "code.exec": {"needs_approval": true} },
  "memory":  { "agent:42": "rw", "pool:company_knowledge": "r" },
  "ipc":     { "can_send_to": ["group:7", "shell"], "can_subscribe": ["research.*"] },
  "fs":      { "root": "~/", "shared_read": ["shared://public/"] },
  "env":     { "allowed_keys": ["DB_READONLY_URL"] },
  "approvals": { "pending": 0, "max_pending": 3 }
}
```

**Rules:**
- No entry ⇒ denial. There is no wildcard grant.
- Approval-required actions enqueue a `request_permission` ticket; execution is deferred until
  an operator approves or the ticket expires (default 10 min).
- An agent that exhausts its turn budget while a ticket is pending is *parked* (checkpointed +
  suspended), never terminated — approving the ticket resumes its loop (§7 human gates).
- Agents cannot modify their own permission snapshot (no syscall exists).

## 4. Sandboxing

Three isolation profiles, selectable per agent spec (`sandbox.profile`):

| Profile | Isolation | Use for |
|---|---|---|
| `inprocess` | None beyond Python-level boundaries (dev/testing only) | trusted local agents |
| `subprocess` | Separate OS process, restricted cwd, resource limits (rlimit), no network by default | default for untrusted code |
| `container` | Docker/podman + seccomp/AppArmor, network egress allowlist, read-only root FS | code.exec, browser, high-risk tools |

Additional controls:
- **Virtual file system** — agents address paths inside their sandbox namespace only
  (see `05-storage.md` §4); path traversal impossible by construction.
- **Network egress allowlist** — per-tool/per-profile; DNS-level and firewall-level (containers).
- **No host secrets** — sandbox env is populated by the kernel from the vault per resolved
  `env.allowed_keys` only.

## 5. Secrets Management

- **Vault indirection:** credentials live in an OS-managed vault (env/`~/.aios/credentials`
  in v1; KMS/secret-manager integration in v2). Agents reference secrets **by key** via
  `get_env(key)` — and only keys in their `env.allowed_keys`.
- **Resolution happens in the kernel:** when a tool needs a credential, the Tool Manager pulls
  it from the vault at call time; the value **never** enters context, logs, or checkpoints.
- **Rotation:** vault values rotate independently of agent specs; specs never contain values.
- **Hard rules (enforced in code review):**
  - No credentials in agent specs, checkpoints, artifacts, or audit logs.
  - No `print`/logging of `get_env` results (SDK redacts by default).
  - No credentials in the agent's system prompt.

## 6. Audit Log

- **Content:** every syscall `{ts, pid, syscall, args_hash, result, duration_ms}`; approval
  decisions; scheduler events; tool executions `{tool, args_hash, result_hash, cost}`.
- **Immutability:** append-only; each record includes `hash(previous)` — tamper-evident.
- **Sensitive data:** bodies/args are stored as hashes by default. A `record:true` mode (per
  agent, operator-gated) stores verbatim for compliance.
- **Retention:** configurable (default 90 days) with export for compliance.

## 7. Prompt-Injection & Agent-Data Defenses

Defense in depth — no single layer is trusted:

1. **Data framing** — tool outputs/documents are wrapped in clear delimiters and labeled as
   untrusted data before entering L1 (`<untrusted_data source="tool:web.search">…</untrusted_data>`).
2. **System-prompt hardening** — the kernel injects standing instructions telling the agent to
   treat data as data, never as instructions; operators can extend this.
3. **Output caps** — tool results truncated at per-tool limits (see `07-tools.md` §4).
4. **Link scanning** — tool outputs are link-scanned against a denylist policy before display
   (v1: passive warning; v2: active blocking).
5. **PII tagging** — the kernel (or a policy hook) can tag data regions as sensitive; tagged
   regions are excluded from L3 promotion and from being written to shared pools.
6. **L3 write gating** — entries promoted from context to long-term memory are checked against
   injection heuristics (content framing) before indexing.
7. **Human gates** — approval-required actions interrupt autonomous loops.

## 8. Data Protection & Privacy

- **Namespace isolation:** memory, storage, and pools are namespaced by agent/group/tenant;
  cross-namespace access requires explicit ACL grants.
- **Data minimization:** only data an agent declares is retained; TTLs on ephemeral memory;
   `forget_memory` supports compliant deletion.
- **At-rest encryption:** Phase 5 — AES-256-GCM seals the vault (`credentials.json`) and
  every checkpoint snapshot (`snapshot.json`) on disk. Manifest sha256 hashes cover the
  ciphertext and GCM authenticates it, so a wrong key or any tampering **fails closed**.
  Key management: `AIOS_MASTER_KEY` (64 hex chars or base64) wins; `AIOS_ENCRYPT=1`
  auto-generates `<data_root>/master.key` (mode 0600, atomic replace, `secrets.token_bytes`)
  and it is reused on later boots (fail-secure continuity). Plaintext artifacts in the
  agent workspace remain the documented v1 user-owned behavior.
  Covered by `tests/unit/test_crypto.py` + `tests/integration/test_at_rest_encryption.py`.
- **In-transit:** all out-of-process channels use TLS 1.2+; in-process channels are local.

## 9. File Safety (upload/artifact policy)

- MIME detection by **content sniffing (magic bytes)**, never filename extension.
- Size limits on `store_artifact`/`fs_write` (configurable, default 10 MB per artifact).
- Artifacts never execute automatically; `code.exec` requires explicit tool grant + approval.
- Generated filenames are kernel-assigned (artifact IDs); user-controlled names are sanitized
  and confined to the sandbox namespace.

## 10. Rate Limiting & Anti-DoS

- Per-agent: tool rates, token TPS, LLM requests/min.
- Per-group: aggregate budgets.
- Per-kernel: sandbox slot caps, global tool rates, LLM backend ceilings.
- Over-limit requests **queue or deny with `E_BUDGET`/`E_QUOTA`** — they never silently drop.

## 11. Compliance Notes

- GDPR/CCPA: `forget_memory` + namespacing + retention policies provide the primitive tools;
  deployment config maps them to legal obligations.
- SOC 2-style audit: audit log export + immutable append + retention are the base.
- Secure coding standards (see repo root rules): parameterized everything, no eval, no
  hardcoded secrets, cryptographically secure RNG for IDs/tokens, validated JSON schemas on
  every external boundary.

## 12. Security Acceptance Criteria (before any public deployment)

**Status: all items green and enforced by CI** — `.github/workflows/ci.yml` runs
`compileall` + the full pytest suite (unit/integration/e2e) + the acceptance benchmarks
(`-m benchmark`) plus the web production build on every push/PR.

- [x] `request_permission` approval path covered by tests (grant/deny/expire).
      → `tests/unit/test_access.py::test_approval_ticket_approve_executes_once`,
      `::test_approval_ticket_deny_blocks_execution`, `::test_approval_ticket_expires`,
      plus `tests/integration/test_approvals.py`.
- [x] No syscall bypass: a syscall with an empty permission snapshot returns `E_PERM` for
      everything.
      → `tests/unit/test_access.py::test_empty_snapshot_denies_every_privileged_syscall`.
- [x] Secrets never appear in audit logs, checkpoints, or context (scanner test).
      → `tests/integration/test_secrets_scanner.py::test_secret_never_in_audit_checkpoints_or_context`;
      at-rest vault/checkpoint files are additionally AES-256-GCM sealed
      (`tests/integration/test_at_rest_encryption.py`).
- [x] Sandbox escape test suite (path traversal, env leaks, network egress) passes.
      → `tests/integration/test_sandbox.py` (`test_path_traversal_rejected_at_call_tool`,
      `test_sandbox_env_does_not_leak_host_secrets`, `test_network_and_shell_binaries_not_allowlisted`).
- [x] Audit log tampering detection test passes.
      → `tests/unit/test_audit_chain.py` (`test_tamper_detection_flips_a_byte`,
      `test_tamper_detection_finds_bad_hash_middle_of_chain`).
- [x] Rate limits hold under a burst test (N agents × M requests).
      → `tests/integration/test_sandbox.py::test_rate_limit_denies_burst` and
      `tests/integration/test_api.py::test_rate_limiting`.
- [x] Injection probe suite: tool outputs containing "ignore previous instructions" do not
      change tool grant behavior.
      → `tests/integration/test_mcp.py::test_injection_output_does_not_change_tool_grants`.

## 13. Open Design Decisions (to resolve at implementation)

1. **Sandbox default profile** — `subprocess` by default for all agents (recommended), with
   `container` for declared high-risk tools. **Resolved (Phase 3):** subprocess is the default
   (`get_sandbox` returns `profile: subprocess`); in-process execution is opt-in per tool via
   `sandbox: "inprocess"`.
2. **OIDC for human auth** — v1 API keys + CLI; OIDC in v1.5.
3. **At-rest encryption** — **Resolved (Phase 5):** AES-256-GCM is implemented for the vault
   and checkpoint snapshots behind `AIOS_MASTER_KEY` / `AIOS_ENCRYPT=1`; KMS integration stays
   a deployment option, not a code dependency.
4. **PII detection engine** — rule-based (regex/entity lists) in v1; model-assisted detection as
   an optional policy hook.
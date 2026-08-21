# 06 — Inter-Agent Communication (IPC)

**Status:** Draft (v0.1)
**Relates to:** `02-kernel.md` (syscalls 16–21), `03-scheduler.md` (blocking & wakeups), `08-security.md`.

---

## 1. Design Goals

1. **Message passing, not shared mutable state** — agents cooperate by sending messages; the
   kernel serializes everything at boundaries.
2. **Asynchronous by default** — `send_msg` never blocks; `recv_msg` has a mandatory timeout.
3. **Permissioned** — who may send to whom, and who may subscribe to what, is declared in specs.
4. **Auditable** — every message is logged (content hashes, not bodies for sensitive data).
5. **Durable where it matters** — mailboxes are checkpointed with their agent, so an agent
   resumes to a faithful mailbox.

## 2. Message Model

```jsonc
// IPC message (kernel-envelope)
{
  "msg_id": "msg-91c2",
  "type": "direct" | "reply" | "handoff" | "event",
  "from_pid": 42,
  "to_pid": 43,                  // for direct/reply/handoff
  "reply_to": "msg-77ab",        // for reply
  "topic": "research.done",      // for event
  "body": { "...": "JSON payload..." },
  "priority": 50,
  "expires_at": "2026-08-19T09:30:00Z",   // TTL for dead-lettering
  "trace_id": "tr-1f2e",                  // correlation across agents
  "sig": "hmac:...",                      // kernel-signed envelope (integrity)
  "created_at": "2026-08-19T09:00:01Z"
}
```

**Body guidance:** bodies should be self-describing JSON. Large payloads are stored as artifacts
(`store_artifact`) and referenced by ID — mailboxes never carry bulk data.

## 3. Mailboxes (syscalls 16–17)

- Every agent has a **mailbox**: a per-agent FIFO queue owned by the IPC Manager, checkpointed
  with the agent.
- `send_msg(to_pid, body, reply_to?)` → enqueue at target, wake target if `BLOCKED`.
- `recv_msg(timeout_ms, filter?)` → dequeue first matching message, or block until timeout.
  Filtering by `{from_pid?, type?, topic?}` lets agents implement selective wait.
- **Blocking semantics:** a `recv_msg` call transitions the agent to `BLOCKED` and frees its
  scheduler slot (see `03-scheduler.md` §5). The kernel wakes it on arrival or timeout.

### Mailbox policy
| Setting | Default | Meaning |
|---|---|---|
| `max_queue_depth` | 100 | overflow → oldest dropped + `E_OVERFLOW` note (dead-lettered to audit) |
| `ttl` | 1 hour | expired undelivered messages are dead-lettered |
| `delivery` | at-least-once | agents should be idempotent; see §6 |

## 4. Pub/Sub Event Bus (syscalls 18–20)

- Topics are hierarchical (`research.done`, `ops.agent.terminated`).
- `subscribe(topic)` + `publish(topic, payload)` — the kernel fans out to all subscribers whose
  specs allow that topic.
- Subscriptions are declared in the spec (`ipc.can_subscribe`), and publish rights are checked
  per topic at syscall time.
- Events are **fire-and-forget** (no `reply_to`); for request/response use direct messages.

## 5. Task Handoff Protocol

Handoff is a structured way to delegate work — the agent-OS equivalent of a pipe:

```
A: send_msg(B, {type: "handoff", body: {task, spec_ref, input_refs}})
B: recv_msg → accepts → runs task → send_msg(A, {type: "reply", reply_to, body: {result_ref}})
A: (optionally) join([B]) for synchronous orchestration
```

- The kernel enforces that `spec_ref` in a handoff is a **validated, spawnable spec** — a
  handoff is a lightweight spawn request, not arbitrary data.
- Handoff cancellation: A may `cancel_tool`-style abort via `send_msg(B, {type:"abort"})` if B
  declared `handoff.abortable`.

## 6. Synchronization (syscall 21: `join`)

- `join(pids[], timeout_ms)` — blocks until all listed agents reach a terminal-ish checkpoint
  (`TERMINATED`, `ERROR`, or explicit `checkpoint("milestone")`), or timeout.
- Returns per-pid results with status, so callers can handle partial success (see
  `03-scheduler.md` §9).

## 7. Ordering, Reliability & Idempotency

| Guarantee | Scope | Notes |
|---|---|---|
| **FIFO per (sender → mailbox)** | per pair | preserved; no global ordering |
| **At-least-once delivery** | default | retries on kernel-side transient failures |
| **Idempotency keys** | agents | `trace_id` lets receivers dedupe replays |
| **Exactly-once processing** | opt-in | via `trace_id` + receiver-side dedup store |

Agents are expected to treat messages as possibly re-delivered; the SDK exposes
`msg.trace_id` and a dedup helper (`aios.ipc.dedupe`).

## 8. Security Notes (details in `08-security.md`)

- **Send permission** is checked on every `send_msg`: sender must have `ipc.can_send_to`
  covering the target (or target's group).
- **Event topics** are permissioned at subscribe *and* publish time.
- Message **bodies are not audited verbatim** by default — the audit log records
  `hash(body)`; a separate, permissioned "record" mode may capture bodies for compliance.
- Mailboxes are **per-agent isolated**; no agent can read another's mailbox without that agent
  forwarding messages (no "peek" syscall exists).

## 9. Open Design Decisions (to resolve at implementation)

1. **In-kernel bus vs. embedded message broker** — v1 uses an in-process bus (single kernel).
   **Resolved (Phase 5):** a multi-kernel broker (`aios_kernel/modules/broker.py`, §11) routes
   IPC across kernels behind the same syscall API; event replay after a broker restart is not
   provided in the preview.
2. **Event replay** — do subscribers get missed events after a restart? Recommend: no replay in
   v1; state should come from shared pools/memory, not the event stream.
3. **Group messaging** — multicast to all agents in a group (`send_msg` with `to_group`)
   deferred to v1.5; subscribe/publish covers most broadcast needs today.
4. **Human-in-the-loop channel** — the shell is a first-class IPC peer: humans can `send_msg`
   to agents, and agents can request human approval via `request_permission` (see `08/10`).

## 10. Realization — Phase 2, Slice 2.2 (implemented)

Implemented in `aios_kernel/modules/ipc.py` (kernel), `aios_sdk/session.py` +
`aios_sdk/syscalls.py` (typed SDK bindings), and `specs/agent.schema.json` (`ipc` grants).

**Envelope** (`IpcMessage`): `{msg_id, type, from_pid, to_pid, reply_to, topic, body,
priority, trace_id, created_at, expires_at, sig}`. `sig` is a `sha256:` integrity checksum
over the canonical envelope payload (resolved decision: checksums, not a keyed HMAC —
tamper-evidence is provided by the hash-chained audit log instead, see `08-security.md` §6).
`from_dict` re-signs on load, so checkpointed mailboxes are integrity-anchored.

**Syscalls:** `send_msg`, `recv_msg`, `subscribe`, `unsubscribe`, `publish`, `join` — all
argument-schema-validated (strict, `E_INVAL`), all audited, all exposed as typed SDK methods.

- `send_msg(to_pid, body, type?, reply_to?, topic?, priority?, trace_id?, ttl_s?)` — never
  blocks; enqueues and `wake`s a BLOCKED receiver. Send permission is deny-by-default:
  `ipc.can_send_to` accepts `*`, `group:<id>`, or `pid:<n>` entries; a spec without `ipc`
  cannot send (`E_PERM`). `type="reply"` requires `reply_to` and inherits the original
  envelope's `trace_id` (found across all mailboxes by `msg_id`). `type="handoff"` requires
  `body.spec` to be a **schema-validated, spawnable** spec (`E_INVAL` otherwise).
- `recv_msg(timeout_ms, filter?)` — dequeues the first `{from_pid?, type?, topic?}` match, or
  parks the caller in `BLOCKED` (`scheduler.block`, CPU slot freed) until arrival or deadline;
  returns `{msg: {...} | None, reason: "timeout" | "state"}`. A `suspend` on a blocked agent
  wakes it first so the in-flight syscall unwinds without `E_STATE`.
- `subscribe`/`unsubscribe`/`publish` — hierarchical topics; a subscription pattern `*`, exact,
  or `prefix.*` matches (`jobs.*` matches `jobs.data`, not `jobs`). Delivery is fan-out into
  subscriber mailboxes via `enqueue`; `publish` returns the delivery count. Topic rights are
  checked at subscribe and publish time (`ipc.can_subscribe` / `ipc.can_publish`).
- `join(pids[], timeout_ms?)` — validates every target (`E_NOENT`), then parks the caller and
  polls (50 ms) until all targets reach `TERMINATED` (live or reaped — results fall back to the
  tombstone record so exit status survives reaping) or the deadline. Returns per-pid
  `{pid, status, exit_status, exit_message}` + `timed_out`.

**Mailbox policy:** per-agent FIFO; `ipc.mailbox.max_queue_depth` (default 100) → overflow
drops the oldest envelope (audit `ipc.overflow` dead-letter); `ipc.mailbox.ttl_s` (default
3600) → expired undelivered envelopes pruned on dequeue/enqueue (audit `ipc.dead_letter`).

**Checkpoint integration:** `IPCManager.snapshot(pid)` / `restore(pid, mailbox, subscriptions)`
are wired into `StorageManager.checkpoint()` / `restore()` and the `--resume` boot path — an
agent resumes to a faithful mailbox and subscription set (unit + crash-resume tests cover this).

**Tests:** `tests/unit/test_ipc.py` (envelope, topic match, permissions, mailbox policy,
checkpoint persistence), `tests/integration/test_ipc.py` (send/recv, filter, timeout,
wake-on-send, reply trace, pub/sub, unsubscribe, join, handoff), and the e2e acceptance
`A → handoff → B → result → A (join + recv)` in `tests/e2e/test_acceptance.py`.
## 11. Multi-Kernel Broker (Phase 5, Slice 5.4)

The multi-kernel preview lets two kernels share one IPC namespace so an agent on kernel A can
`send_msg`/`publish` to an agent on kernel B behind the *existing* IPC syscalls — no agent-level
API change. Implemented in `aios_kernel/modules/broker.py`:

- **`Broker`** — in-process authority: a global pid registry (`pid → (kernel_id, group_id)`),
  cross-kernel subscription fan-out, and per-kernel delivery callbacks. Pids come from one shared
  space (`allocate_pid`), so kernel-local counters cannot collide. Fail-closed: unknown pids and
  unknown kernels are never delivered to, and message bodies are never inspected — each kernel
  re-applies its own permission + audit rules on arrival (`IPCManager._broker_deliver`).
- **`BrokerServer` / `BrokerClient`** — optional TCP transport (newline-delimited JSON, loopback
  by default) so kernels in separate processes share one broker. One connection == one kernel;
  the first line must be `register`. The handshake is token-authenticated: `AIOS_BROKER_TOKEN`
  (or an explicit `token=`). A server with no configured token rejects every client (fail
  closed), a wrong token is refused, and a `kernel_id` already held by another connection is
  rejected — no pid/delivery hijacking by a local process.
- **Wiring** — `Kernel(broker=...)` attaches the manager; claims, releases, subscriptions, and
  remote routing are mirrored through the broker while local semantics are unchanged. A socket
  `BrokerClient` is auto-started by the kernel at construction. `join` across kernels is
  rejected (`E_INVAL`); `recv_msg` and `join` remain local operations.

**Tests:** `tests/unit/test_broker.py` — pid registry, fail-closed routing, cross-kernel pub/sub
fan-out, socket transport + auth (wrong/missing token, token-less server, duplicate `kernel_id`),
and two kernels wired over both the in-process broker and the token-protected socket broker.

**Known preview limits:** event replay after a broker restart is not provided; the operator starts
`BrokerServer` explicitly (it is not part of `aios serve`); restore-after-crash re-claims restored
pids through the same `ipc.create` path.

**Deferred from this slice:** milestone-checkpoint joins (v1 joins on `TERMINATED` only),
event replay, group multicast, and the shell as an IPC peer.
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

1. **In-kernel bus vs. embedded message broker** — v1 uses an in-process bus (single kernel);
   a future distributed kernel may swap in a broker behind the same IPC API.
2. **Event replay** — do subscribers get missed events after a restart? Recommend: no replay in
   v1; state should come from shared pools/memory, not the event stream.
3. **Group messaging** — multicast to all agents in a group (`send_msg` with `to_group`)
   deferred to v1.5; subscribe/publish covers most broadcast needs today.
4. **Human-in-the-loop channel** — the shell is a first-class IPC peer: humans can `send_msg`
   to agents, and agents can request human approval via `request_permission` (see `08/10`).
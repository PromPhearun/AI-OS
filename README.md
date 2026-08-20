# AI OS

> **LLM as OS, agents as apps.**
> An operating system whose "processes" are AI agents.

AI OS is a software kernel that manages AI agents as first-class citizens: their lifecycles,
scheduling, memory, inter-agent communication, tool access, persistence, and security — the way
a classic OS manages processes, but for the agent era.

## Status

**Phase 1 — MVP Kernel implemented.** Agent lifecycle, syscall ABI, agent scheduler,
LLM core (mock + OpenAI-compatible backend), in-memory context/memory, 3 built-in
tools (`fs.read`, `fs.write`, `shell.run`), audit log, and a working CLI.

**Phase 2 — durable checkpoints + `--resume` boot path implemented.** Checkpoints
are written to disk (sha256-verified snapshot + manifest, committed before ack), a
session manifest (`aios-data/session.json`) records every suspended agent, and
`aios resume` rebuilds a crashed kernel's agents at their last committed checkpoint.

**Phase 2 — kernel IPC implemented (Slice 2.2).** Per-agent mailboxes with
`send_msg`/`recv_msg` (send never blocks; recv blocks with a mandatory timeout and
an optional filter), permissioned pub/sub over hierarchical topics (`subscribe`/
`unsubscribe`/`publish`), the task-handoff protocol (handoff envelopes must carry a
validated, spawnable spec), and `join(pids, timeout_ms)` for synchronous
orchestration. Mailboxes and subscriptions are checkpointed with the agent, so a
crash-resumed agent wakes to a faithful mailbox. IPC permissions are deny-by-default
and declared in the agent spec (`ipc.can_send_to` / `can_subscribe` / `can_publish`).
See [`docs/11-roadmap.md`](docs/11-roadmap.md) for the phased plan.

## Quickstart

```bash
# Create a venv and install the package (+ dev extras for tests)
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# Run the demo (two example agents: researcher + writer)
.venv/bin/aios demo

# Run the full test suite (unit / integration / e2e)
.venv/bin/python -m pytest tests/ -q
```

Example `aios demo` output:

```
PID  NAME             STATE       EXIT     TURNS  TOKENS      COST TOOLCALLS
-----------------------------------------------------------------------------
   1  researcher       terminated  ok           3     126 $ 0.00000         3
   2  writer           terminated  ok           2      84 $ 0.00000         2
```

## Architecture

- **Kernel** (`aios_kernel/`) — the trusted computing base: syscall dispatch, ACB +
  lifecycle state machine, scheduler (priority + aging, single CPU), LLM core, context /
  memory / workspace / storage / vault managers, audit log.
- **SDK** (`aios_sdk/`) — the only library agents import: `@agent` decorator, `AgentSession`
  syscall client, `ControlPlane` for launch/supervise, `run_agents` launcher.
- **CLI** (`aios_cli/`) — `aios demo`, `aios run`, and `aios resume` control surface.

The design specification (15 documents, ~1,900 lines):

| | Document | One-liner |
|---|---|---|
| 00 | [Vision](docs/00-vision.md) | Thesis, problem statement, design principles |
| 01 | [Architecture Overview](docs/01-overview.md) | Layers, modules, boot sequence, data flows |
| 02 | [Kernel & Agent Model](docs/02-kernel.md) | Lifecycle state machine, agent spec, **canonical syscall ABI** |
| 03 | [Scheduler](docs/03-scheduler.md) | Agent/LLM/tool scheduling, preemption, budgets, context switching |
| 04 | [Memory Manager](docs/04-memory.md) | L1 context → L2 working → L3 long-term → shared pools |
| 05 | [Storage & Semantic FS](docs/05-storage.md) | Checkpoints, artifacts, natural-language file search |
| 06 | [IPC](docs/06-ipc.md) | Mailboxes, pub/sub, handoffs, synchronization |
| 07 | [Tool Manager](docs/07-tools.md) | Tool registry, MCP integration, sandboxed execution |
| 08 | [Security](docs/08-security.md) | Threat model, RBAC, sandboxing, secrets, audit |
| 09 | [SDK](docs/09-sdk.md) | Agent-side & control-side Python API, framework adapters |
| 10 | [Shell & UI](docs/10-ui.md) | CLI shell, web desktop, REST/WS control plane |
| 11 | [Roadmap](docs/11-roadmap.md) | Phased build plan (Phase 0 → 5), acceptance criteria |
| 12 | [Tech Stack](docs/12-tech-stack.md) | Python kernel + FastAPI + MCP + React (and why) |
| 13 | [References](docs/13-references.md) | Prior art (AIOS), standards, benchmark notes |

## Quickstart for Reviewers

```bash
# Read in this order:
docs/00-vision.md    # why
docs/01-overview.md  # what
docs/02-kernel.md    # the contract (syscall ABI)
docs/08-security.md  # the hard constraints
docs/11-roadmap.md   # how we'll build it
```

## Next Step

**Slices 2.1 + 2.2 of Phase 2 — State & Memory are delivered** (durable on-disk checkpoints
with sha256 integrity verification, the `--resume` boot path, the session manifest, and kernel
IPC: mailboxes, pub/sub, handoffs, and `join`). The remaining Phase 2 slices — semantic FS
(`fs_search`) and the memory/retrieval stack — land next, per
[`docs/11-roadmap.md`](docs/11-roadmap.md).

---

*Security-first design. Deny by default. Everything audited. Agents are processes.*
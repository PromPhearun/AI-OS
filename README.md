# AI OS

> **LLM as OS, agents as apps.**
> An operating system whose "processes" are AI agents.

AI OS is a software kernel that manages AI agents as first-class citizens: their lifecycles,
scheduling, memory, inter-agent communication, tool access, persistence, and security — the way
a classic OS manages processes, but for the agent era.

## Status

**Phase 0 — Architecture Blueprint.** The full design specification is drafted in [`docs/`](docs/README.md).
No implementation code yet — this repository currently contains the plan.

## Blueprint

The design spec is 15 documents, ~1,900 lines:

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

When the blueprint is approved, **Phase 0/1** (repo scaffold + MVP kernel) begins per
[`docs/11-roadmap.md`](docs/11-roadmap.md).

---

*Security-first design. Deny by default. Everything audited. Agents are processes.*
# AI OS — Architecture Blueprint

> **LLM as OS, agents as apps.**
> A complete design specification for an operating system whose "processes" are AI agents.

This repository contains the full architecture blueprint for **AI OS** — a software kernel that
manages AI agents as first-class citizens: their lifecycles, scheduling, memory, inter-agent
communication, tool access, persistence, and security.

## Document Map

| Doc | Title | Read this for |
|---|---|---|
| `00-vision.md` | Vision & Design Principles | The "why" — problem statement, thesis, principles, glossary |
| `01-overview.md` | Architecture Overview | The "what" — layers, components, boot sequence, data flows |
| `02-kernel.md` | Kernel & Agent Model | The "process" model — lifecycle, process table, syscall ABI |
| `03-scheduler.md` | Scheduler | Agent/LLM/tool scheduling, preemption, budgets, context switching |
| `04-memory.md` | Memory Manager | Context window, working/long-term memory, RAG, shared pools |
| `05-storage.md` | Storage & Semantic FS | Checkpoints, artifacts, natural-language file system |
| `06-ipc.md` | Inter-Agent Communication | Mailboxes, message bus, handoffs, synchronization |
| `07-tools.md` | Tool Manager | Tool registry, MCP compatibility, sandboxed execution |
| `08-security.md` | Access Control & Security | RBAC, sandboxing, secrets, audit, threat model |
| `09-sdk.md` | SDK & Syscall API | How developers write agents; framework adapters |
| `10-ui.md` | Shell & UI | CLI shell, web desktop, dashboards |
| `11-roadmap.md` | Implementation Roadmap | Phased build plan, milestones, acceptance criteria |
| `12-tech-stack.md` | Tech Stack Decision | Language, model gateway, storage, sandboxing choices |
| `13-references.md` | References | Research prior art (AIOS), standards, related work |

## Suggested Reading Order

1. **00** (vision) → **01** (overview) — the constitution
2. **02** (kernel) + **03** (scheduler) — the core
3. **04** (memory) + **05** (storage) — state
4. **06** (IPC) + **07** (tools) — interaction
5. **08** (security) — hard constraints
6. **09** (SDK) + **10** (UI) — surfaces
7. **11** (roadmap) + **12** (tech stack) — build plan

## Status

| Doc | Status |
|---|---|
| 00–13 | ✅ Drafted (v0.1) |

## Conventions Used Across This Spec

- **`aios_`** prefix for internal Python packages (`aios_kernel`, `aios_sdk`, ...)
- Syscall names are snake_case; syscall table is canonical in `02-kernel.md`
- Agent specs are JSON Schema-validated documents (canonical in `02-kernel.md`)
- All Mermaid diagrams render on GitHub
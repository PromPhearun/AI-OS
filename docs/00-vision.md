# 00 — Vision & Design Principles

**Status:** Draft (v0.1)
**Owner:** AI OS architecture working group

---

## 1. The Thesis

> **LLM as OS, agents as apps.**

A traditional operating system sits between **hardware** and **applications**, managing scarce
resources (CPU, memory, storage, devices) and giving programs a stable, safe abstraction to use
them. An AI OS does the same for the agent era: it sits between **AI infrastructure** (LLM
providers, embeddings, vector stores, tools, sandboxes) and **AI agents**, managing the resources
agents compete for and giving them a stable, safe abstraction — the **syscall API** — to use them.

Agents are not apps. They are **processes**: stateful, goal-directed, resource-consuming,
concurrently running, and — critically — hazardous if left unsupervised.

## 2. Problem Statement

Today's agent frameworks (LangGraph, AutoGen, CrewAI, ...) are **application-level libraries**.
Each one reinvents the same machinery and leaves fundamental systems problems unsolved:

| Problem | Symptom |
|---|---|
| **No resource management** | Any agent can burn unbounded tokens / cost / wall-clock time |
| **No scheduling** | Multiple agents contend for the same LLM without fairness or priorities |
| **No context management** | Context windows overflow; state is lost; no suspend/resume |
| **No shared memory** | Agents can't persist or share knowledge without bespoke plumbing |
| **No IPC** | Inter-agent communication is ad-hoc chat, not a system primitive |
| **No access control** | Tools are granted with all-or-nothing permissions; no audit trail |
| **No isolation** | A misbehaving agent can corrupt another agent's state or system files |
| **No lifecycle** | No notion of spawn/suspend/terminate, so long-running agents are fragile |
| **No portability** | Agents are welded to one framework and one model provider |

The core insight from prior art (AIOS, Rutgers, COLM 2025): **these are OS problems**, and they
deserve an OS-level answer.

## 3. What AI OS Provides

1. **A kernel** that owns agent lifecycles, scheduling, memory, storage, IPC, tools, and security.
2. **A syscall ABI** — a stable API that agents use to request kernel services. Agents stop
   talking to models and tools directly; they talk to the kernel.
3. **Resource governance** — token budgets, cost caps, rate limits, and quotas enforced by the
   kernel, not by the agent's good behavior.
4. **Context switching** — agents can be suspended and resumed, exactly like processes, enabling
   preemption, priorities, and crash recovery.
5. **Shared memory & IPC** — agents exchange knowledge through kernel-managed memory pools and
   message primitives.
6. **A semantic file system** — persistence that agents (and humans) can query in natural
   language, not just by path.
7. **A developer SDK** — agents are declared via a validated spec and become portable across
   frameworks and models.

## 4. Design Principles

1. **Agents are processes, not functions.** Every agent has an identity, a lifecycle, a state,
   a budget, and an owner.
2. **The kernel mediates everything.** No agent calls an LLM or tool directly; all access flows
   through syscalls so it can be scheduled, budgeted, and audited.
3. **Deny by default.** An agent can do nothing until its declared spec grants permission.
4. **Context is the OS's problem.** Agents declare their memory needs; the kernel compresses,
   summarizes, evicts, and retrieves.
5. **Everything is a resource.** Tokens, context, tool invocations, storage, wall-clock — all
   governed by budgets and quotas.
6. **Preemptible and resumable.** Any agent may be suspended at a checkpoint and restored later.
7. **Isolation with cooperation.** Agents are sandboxed from each other and the host, but can
   cooperate through explicit IPC primitives.
8. **Standard interfaces over proprietary ones.** Tools speak MCP; models speak an
   OpenAI-compatible gateway; agents speak the syscall ABI.
9. **Auditable by construction.** Every syscall, tool call, and state mutation is logged.
10. **Humans stay in the loop.** Privileged operations require human approval; the shell is a
    first-class surface, not an afterthought.

## 5. Goals (In Scope)

- Concurrent execution of many heterogeneous agents with fair scheduling.
- Suspend/resume and crash-recovery via checkpointing.
- Tiered memory: context window → working memory → long-term (RAG) → shared pools.
- Inter-agent message passing, pub/sub, and task handoff.
- Tool execution behind a sandboxed, rate-limited, permission-checked pipeline.
- MCP-native tool integration.
- RBAC, secrets management, and full audit logging.
- A CLI shell and a web desktop for launching/monitoring agents.
- An SDK compatible with common agent frameworks.

## 6. Non-Goals (Out of Scope for v1)

- Writing our own LLM from scratch.
- Replacing framework loop primitives (LangGraph *users* still write their graphs; AI OS runs them).
- Kernel-level OS work (no microkernel in Rust targeting bare metal) — though see roadmap Phase 4.
- Guaranteeing emergent multi-agent safety beyond the access-control and sandboxing mechanisms
  described here (this is an active research area).
- Distributed multi-host operation (deferred; the design reserves extension points).

## 7. Glossary

| Term | Meaning |
|---|---|
| **Agent** | A process managed by AI OS: an LLM instance with a goal, context, tools, and lifecycle. |
| **Agent spec** | The validated JSON declaration of an agent: model, tools, memory, budgets, permissions. |
| **Syscall** | A request from an agent to the kernel (`spawn`, `read_memory`, `call_tool`, ...). |
| **Process (agent) table** | Kernel registry of all live agents and their state. |
| **Checkpoint** | A serialized snapshot of an agent's full state, enabling suspend/resume. |
| **Context window (L1)** | The tokens currently resident in the model's context. |
| **Working memory (L2)** | Ephemeral scratchpad state attached to a running agent. |
| **Long-term memory (L3)** | Persistent, embedding-indexed knowledge (episodic/semantic/procedural). |
| **Shared memory pool** | A named, permissioned store multiple agents can read/write. |
| **Mailbox** | Per-agent receive queue for IPC messages. |
| **MCP** | Model Context Protocol — the open standard for tool servers. |
| **Semantic file system** | Storage queryable by natural language in addition to paths. |
| **Token budget** | An agent's allowance of tokens per interval; enforced by the scheduler. |
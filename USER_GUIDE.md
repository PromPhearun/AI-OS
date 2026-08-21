# AI OS — User Guide

> **LLM as OS, agents as apps.**
> A practical guide to running, building, and operating AI agents on AI OS.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Prerequisites & Installation](#2-prerequisites--installation)
3. [Quick Start](#3-quick-start)
4. [Writing Your First Agent](#4-writing-your-first-agent)
5. [Agent Spec Reference](#5-agent-spec-reference)
6. [CLI Reference](#6-cli-reference)
7. [Web Desktop](#7-web-desktop)
8. [REST API Reference](#8-rest-api-reference)
9. [Memory & Storage](#9-memory--storage)
10. [Inter-Agent Communication (IPC)](#10-inter-agent-communication-ipc)
11. [Tools & MCP](#11-tools--mcp)
12. [Security & Permissions](#12-security--permissions)
13. [Configuration](#13-configuration)
14. [Troubleshooting & FAQ](#14-troubleshooting--faq)

---

## 1. Introduction

### What Is AI OS?

AI OS is a software kernel that manages AI agents the way a traditional operating system manages processes. Each agent gets:

- **A lifecycle** — spawn, run, suspend, resume, terminate.
- **A scheduler** — fair, priority-based CPU (LLM) time with budgets and quotas.
- **Memory** — tiered from ephemeral context to persistent, searchable long-term knowledge.
- **Storage** — checkpoints for crash recovery and a semantic file system searchable by meaning.
- **IPC** — mailboxes, pub/sub, and task handoff between agents.
- **Tools** — sandboxed, permissioned access to external capabilities (MCP-compatible).
- **Security** — RBAC, deny-by-default permissions, approval workflows, and a tamper-evident audit log.

Agents never talk to LLMs or tools directly — they issue **syscalls** to the kernel, which schedules, budgets, sandboxes, and audits every action.

### Core Concepts

| Concept | What It Means |
|---------|---------------|
| **Agent** | A stateful, goal-directed AI process managed by the kernel. |
| **Agent Spec** | A JSON declaration of an agent's model, tools, budgets, and permissions. |
| **Syscall** | A request from an agent to the kernel (e.g. `call_tool`, `send_msg`, `write_memory`). |
| **Turn** | One execution slice granted to an agent by the scheduler. |
| **Checkpoint** | A serialized snapshot of an agent's full state, enabling suspend/resume and crash recovery. |
| **Kernel** | The trusted computing base that mediates all agent access to resources. |

### Glossary

| Term | Meaning |
|------|---------|
| **L1 Context Window** | Tokens currently resident in the model's context. |
| **L2 Working Memory** | Ephemeral scratchpad state attached to a running agent. |
| **L3 Long-Term Memory** | Persistent, embedding-indexed knowledge store. |
| **Mailbox** | Per-agent receive queue for IPC messages. |
| **MCP** | Model Context Protocol — the open standard for tool servers. |
| **RBAC** | Role-Based Access Control — permissions derived from roles and specs. |
| **ACB** | Agent Control Block — the kernel's internal record of a running agent. |

---

## 2. Prerequisites & Installation

### Requirements

| Component | Version | Purpose |
|-----------|---------|---------|
| **Python** | ≥ 3.11 | Kernel, SDK, CLI, API |
| **Node.js** | ≥ 18 | Web desktop (optional) |
| **npm** | ≥ 9 | Web desktop build (optional) |

### Installation

```bash
# Clone the repository
git clone <repo-url> aios
cd aios

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install AI OS in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify installation
aios --version
# → aios 0.1.0
```

### Building the Web Desktop (Optional)

```bash
cd web
npm install
npm run build    # emits web/dist — served by `aios serve`
cd ..
```

### Data Directory

AI OS stores all runtime data under `aios-data/` (configurable via `--data-root`):

```
aios-data/
├── kernel/            # config, roles, tool registry
├── agents/            # per-agent working directories
├── checkpoints/       # agent state snapshots
├── artifacts/         # files agents produce
├── memory/            # L3 long-term memory + artifact index
├── audit/             # immutable, hash-chained audit log
└── session.json       # session manifest for crash recovery
```

This directory is created automatically on first run and is listed in `.gitignore`.

---

## 3. Quick Start

### Run the Demo

The fastest way to see AI OS in action is the built-in demo, which runs two example agents (a researcher and a writer) concurrently:

```bash
aios demo
```

Output:

```
PID  NAME             STATE       EXIT     TURNS  TOKENS      COST   TOOLCALLS
--------------------------------------------------------------------------------
   1  researcher       TERMINATED  ok           3     126 $ 0.00000         3
   2  writer           TERMINATED  ok           2      84 $ 0.00000         2
```

Each agent ran its turn function multiple times, wrote files via the `fs.write` tool, and exited cleanly. The table shows per-agent resource usage.

### Run Your Own Agents

1. Create an agent spec JSON file (see [Agent Spec Reference](#5-agent-spec-reference)).
2. Write a Python module with `@agent`-decorated turn functions (see [Writing Your First Agent](#4-writing-your-first-agent)).
3. Run:

```bash
aios run specs/my_agent.json --agents-module my_agents
```

### Resume After a Crash

If the kernel crashes or is interrupted, agents that were checkpointed can be restored:

```bash
aios resume --agents-module my_agents
```

This reads `aios-data/session.json`, restores every suspended agent to its last committed checkpoint, and re-attaches runners.

---

## 4. Writing Your First Agent

### The Turn Function Contract

An agent in AI OS is a Python async function decorated with `@agent`. The kernel calls this function once per **turn** (scheduler grant). The function receives an `AgentSession` (`sc`) and returns:

- `False` — the agent is not done; the scheduler will grant another turn later.
- `True` — the agent is finished; the kernel terminates it.

```python
from aios import agent

@agent(name="greeter")
async def greeter(sc) -> bool:
    """A minimal agent that says hello once."""
    pid = (await sc.get_pid())["pid"]
    await sc.log("info", f"Agent {pid} says hello!")
    return True  # done after one turn
```

### A Multi-Turn Agent with Memory

```python
from aios import agent
from aios.errors import AiosNoEntError

@agent(name="counter")
async def counter(sc) -> bool:
    """Counts to 5 across multiple turns, persisting state in memory."""
    ns = f"agent:{(await sc.get_pid())['pid']}"

    # Read current count from working memory
    try:
        count = await sc.read_memory(ns, "count")
    except AiosNoEntError:
        count = 0

    count += 1
    await sc.write_memory(ns, "count", count)
    await sc.log("info", f"Count is now {count}")

    if count >= 5:
        await sc.log("info", "Reached 5 — done!")
        return True

    return False  # not done yet; ask for another turn
```

### Using Tools

```python
from aios import agent

@agent(name="note-taker")
async def note_taker(sc) -> bool:
    """Generates an LLM response and saves it as a file."""
    # Ask the LLM to generate content
    reply = await sc.generate("Write a brief note about AI OS.")

    # Save the result using the fs.write tool
    await sc.call_tool("fs.write", {
        "path": "note.md",
        "content": reply["text"],
    })

    await sc.log("info", "Note saved to note.md")
    return True
```

### Agent-to-Agent Communication

```python
from aios import agent

@agent(name="coordinator")
async def coordinator(sc) -> bool:
    """Spawns a worker, sends it a task, and waits for the result."""
    pid = (await sc.get_pid())["pid"]

    # Spawn a child agent
    child_pid = await sc.spawn({
        "name": "worker",
        "llm": {"model": "mock"},
        "budgets": {"max_turns": 3},
    })

    # Send a task to the child
    await sc.send_msg(child_pid, {
        "type": "handoff",
        "body": {"task": "Summarize Q3 results"},
    })

    # Wait for the reply (10 second timeout)
    reply = await sc.recv_msg(10_000, filter={"type": "reply"})
    await sc.log("info", f"Got reply: {reply}")

    return True
```

### Complete Example: Spec + Agent Module

**`specs/researcher.json`** — the agent spec:

```json
{
  "name": "researcher",
  "version": "1",
  "description": "Gathers research notes.",
  "group_id": "demo",
  "priority": 0,
  "llm": {
    "model": "mock",
    "system": "You are a research assistant. Be concise.",
    "temperature": 0.0
  },
  "budgets": {
    "max_turns": 6,
    "max_tool_calls": 20
  },
  "capabilities": {
    "tools": [
      {"name": "fs.write"}
    ]
  }
}
```

**`my_agents.py`** — the agent implementation:

```python
from aios import agent
from aios.errors import AiosNoEntError

@agent(name="researcher")
async def researcher(sc) -> bool:
    ns = f"agent:{(await sc.get_pid())['pid']}"
    try:
        rounds = await sc.read_memory(ns, "rounds")
    except AiosNoEntError:
        rounds = 0

    if rounds >= 3:
        await sc.log("info", "Research complete")
        return True

    reply = await sc.generate(f"Research step {rounds + 1}: gather data")
    await sc.call_tool("fs.write", {
        "path": f"note-{rounds + 1}.md",
        "content": reply["text"],
    })
    await sc.write_memory(ns, "rounds", rounds + 1)
    return False
```

Run it:

```bash
aios run specs/researcher.json --agents-module my_agents
```

---

## 5. Agent Spec Reference

The agent spec is a JSON document that declares everything about an agent. It is validated against a JSON Schema at spawn time — the spec **is** the capability list.

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Unique agent name (lowercase, alphanumeric, hyphens, underscores; `^[a-z][a-z0-9_-]{0,63}$`). Must match a registered `@agent` definition. |
| `llm` | `object` | LLM configuration (see below). |

### LLM Configuration

```json
{
  "llm": {
    "model": "gpt-4o",
    "system": "You are a helpful assistant.",
    "temperature": 0.0,
    "max_tokens": 4096,
    "provider": "openai",
    "failover": ["anthropic/claude-3-sonnet"]
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | `string` | `"mock"` | Model identifier (`"mock"` for testing, or any OpenAI-compatible model name). |
| `system` | `string` | — | System prompt injected at context assembly. |
| `temperature` | `number` | `0.0` | Sampling temperature (0–2). |
| `max_tokens` | `integer` | — | Max tokens per generation. |
| `provider` | `string` | — | Primary provider name (overrides model-string routing). |
| `failover` | `string[]` | — | Ordered list of fallback models. |

### Budgets

```json
{
  "budgets": {
    "max_turns": 10,
    "max_tool_calls": 50,
    "tokens_per_min": 40000,
    "cost_per_hour_usd": 5.0,
    "max_wall_clock_s": 3600
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_turns` | `integer` | — | Maximum number of scheduler turns. |
| `max_tool_calls` | `integer` | — | Maximum total tool invocations. |
| `tokens_per_min` | `integer` | — | Token budget per minute (enforced by LLM scheduler). |
| `cost_per_hour_usd` | `number` | — | Cost cap per hour in USD. |
| `max_wall_clock_s` | `integer` | — | Maximum wall-clock time in seconds. |

When a budget is exhausted, the agent is **suspended** (not terminated) — it can be resumed later.

### Capabilities (Tools & Permissions)

```json
{
  "capabilities": {
    "tools": [
      {"name": "fs.read"},
      {"name": "fs.write"},
      {"name": "web.search", "needs_approval": true},
      {"name": "shell.run", "args": {"allow_commands": ["pytest", "ls"]}}
    ],
    "spawn": false,
    "operator": false,
    "memory": true,
    "context": true
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tools` | `object[]` | `[]` | Tool grants. Each has `name`, optional `needs_approval` and `args`. |
| `spawn` | `boolean` | `false` | Allow this agent to spawn child agents. |
| `operator` | `boolean` | `false` | Operator privileges (approve/deny tickets, verify audit). |
| `memory` | `boolean` | `true` | Allow L2 working-memory syscalls. |
| `context` | `boolean` | `true` | Allow context read/append syscalls. |

### IPC Permissions

```json
{
  "ipc": {
    "can_send_to": ["group:demo", "shell"],
    "can_subscribe": ["research.*"],
    "can_publish": ["research.done"],
    "mailbox": {"max_queue_depth": 100, "ttl_s": 3600}
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `can_send_to` | `[]` | Recipients: `"*"` (any), `"group:<id>"`, or `"pid:<n>"`. |
| `can_subscribe` | `[]` | Topic patterns (`"*"`, exact, or `"prefix.*"`). |
| `can_publish` | `[]` | Topic patterns this agent may publish to. |
| `mailbox.max_queue_depth` | `100` | Max messages before oldest is dropped. |
| `mailbox.ttl_s` | `3600` | Message expiry time in seconds. |

### Memory Grants

```json
{
  "memory": {
    "pools": [
      {"pool": "company_knowledge", "access": "read"},
      {"pool": "team_research", "access": "read-write"}
    ]
  }
}
```

Pools not listed here are unreachable (`E_PERM`). Access is `"read"` or `"read-write"`.

### Context Policy

```json
{
  "context": {
    "context_token_budget": 8000,
    "keep_recent_messages": 4
  }
}
```

When `context_token_budget` is set, the kernel auto-summarizes old turns when the window fills, always preserving pinned content and the most recent `keep_recent_messages` turns verbatim.

### Sandbox Profile

```json
{
  "sandbox": {
    "profile": "subprocess",
    "network": "none",
    "rlimits": {"max_cpu_s": 5, "max_mem_mb": 256}
  }
}
```

| Field | Default | Options | Description |
|-------|---------|---------|-------------|
| `profile` | `"subprocess"` | `inprocess`, `subprocess`, `container` | Isolation level. |
| `network` | `"none"` | `none`, `http`, `all` | Network egress policy. |
| `rlimits` | — | — | Resource limits for subprocess/container profiles. |

### Other Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `version` | `string` | `"1"` | Spec version. |
| `description` | `string` | — | Human-readable description (max 500 chars). |
| `role` | `string` | `"standard"` | RBAC role: `operator`, `standard`, `restricted`, `service`. |
| `group_id` | `string` | `"default"` | Scheduling group (fair-share unit). |
| `priority` | `integer` | `0` | Scheduler priority (-20 to 20; higher runs first). |
| `entry` | `string` | — | Python import path of the agent module (future use). |
| `env.allowed_keys` | `string[]` | `[]` | Secret keys this agent may resolve via `get_env`. |
| `approvals.max_pending` | `integer` | `3` | Max concurrent pending approval tickets. |

### Full Example Spec

```json
{
  "name": "research-analyst",
  "version": "1",
  "description": "Performs competitive research and produces reports.",
  "role": "standard",
  "group_id": "team-a",
  "priority": 10,
  "llm": {
    "model": "gpt-4o",
    "system": "You are a senior research analyst.",
    "temperature": 0.2,
    "max_tokens": 4096
  },
  "budgets": {
    "max_turns": 20,
    "max_tool_calls": 100,
    "tokens_per_min": 40000,
    "cost_per_hour_usd": 10.0,
    "max_wall_clock_s": 1800
  },
  "capabilities": {
    "tools": [
      {"name": "fs.read"},
      {"name": "fs.write"},
      {"name": "web.search", "needs_approval": false}
    ],
    "spawn": false,
    "memory": true,
    "context": true
  },
  "ipc": {
    "can_send_to": ["group:team-a"],
    "can_subscribe": ["research.*"],
    "can_publish": ["research.done"]
  },
  "memory": {
    "pools": [{"pool": "company_knowledge", "access": "read"}]
  },
  "context": {
    "context_token_budget": 8000,
    "keep_recent_messages": 6
  },
  "sandbox": {"profile": "subprocess", "network": "http"},
  "env": {"allowed_keys": ["SEARCH_API_KEY"]}
}
```

---

## 6. CLI Reference

The `aios` CLI is the primary operator interface. All commands accept `--data-root` to override the data directory (default: `aios-data`).

### `aios --version`

Print the version and exit.

```bash
aios --version
# → aios 0.1.0
```

### `aios demo`

Run the bundled example agents (researcher + writer) and display a results table.

```bash
aios demo [--data-root DIR] [--timeout SECONDS]
```

### `aios run`

Run one or more agents from spec files.

```bash
aios run SPEC [SPEC...] [--agents-module MODULE] [--timeout SECONDS]
```

| Argument | Description |
|----------|-------------|
| `SPEC` | Path(s) to agent spec JSON file(s). |
| `--agents-module` | Python module containing `@agent`-decorated turn functions. |
| `--timeout` | Per-agent wait timeout in seconds. |

**Example:**

```bash
aios run specs/researcher.json specs/writer.json --agents-module examples.agents
```

### `aios resume`

Restore all agents from the last session's checkpoints and re-attach runners.

```bash
aios resume [--agents-module MODULE] [--timeout SECONDS]
```

This reads `aios-data/session.json`, restores every suspended agent, resolves their `@agent` definitions from the registry, and runs them to completion.

### `aios serve`

Start the control plane server (REST + WebSocket API) and optionally serve the web desktop.

```bash
aios serve [--host HOST] [--port PORT] [--agents-module MODULE] [--data-root DIR]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `8000` | Bind port. |
| `--agents-module` | — | Module to import for `@agent` registrations. |

When `web/dist` exists, the web desktop is served same-origin at `http://127.0.0.1:8000/`.

### `aios bench`

Run the Phase 4 benchmark suite (scheduler fairness, throughput, checkpoint I/O).

```bash
aios bench [--json] [--data-root DIR]
```

| Flag | Description |
|------|-------------|
| `--json` | Print the raw JSON report instead of the markdown table. |

Reports are written to `benchmarks/reports/`.

---

## 7. Web Desktop

The web desktop is a React + TypeScript + Vite operator dashboard for supervising your agent fleet.

### Building

```bash
cd web
npm install
npm run build    # emits web/dist
cd ..
```

### Running

**Production** — `aios serve` automatically mounts `web/dist` when present:

```bash
aios serve
# → http://127.0.0.1:8000/ serves the desktop
```

**Development** — run the Vite dev server with hot reload:

```bash
# Terminal 1: start the API server
aios serve --port 8000

# Terminal 2: start the Vite dev server
cd web
npm run dev
# → http://localhost:5173 (proxies /v1 to 127.0.0.1:8000)
```

### Authentication

The desktop authenticates via API keys:

1. **Production:** Set `AIOS_API_KEYS` environment variable (see [Configuration](#13-configuration)).
2. **Development:** Set `AIOS_DEV_KEY=1` to enable the built-in dev key `dev-key` (operator role).

Enter your API key on the login card. The desktop exchanges it for a short-lived JWT.

**Single sign-on (OIDC):** When `AIOS_OIDC_ISSUER` and `AIOS_OIDC_CLIENT_ID` are configured, a **Sign in with SSO** button appears. See [Configuration](#13-configuration) for OIDC environment variables.

### Panels

| Panel | What It Shows | Actions |
|-------|---------------|---------|
| **Processes** | Live process table (PID, name, state, priority, group, tokens, cost, wall time). | Spawn (paste spec JSON), suspend/resume/kill agents, attach console. |
| **Scheduler** | Queue depths (running/ready/blocked), utilization gauge, dispatch/preemption counters. | Read-only observability. |
| **Audit** | Filtered live audit stream with hash-chain verification. | Verify integrity, filter by event type or PID. |
| **Approvals** | Pending `request_permission` tickets. | One-click approve/deny (operator role required). |
| **Files** | Semantic file search for a chosen agent. | Enter a natural language query; ranked results by meaning. |
| **LLM** | Provider failover health (healthy/degraded/down, request counts, failures). | Read-only observability. |
| **Console** (drawer) | Live log tail + operator chat for a specific agent. | Send messages to the agent via its mailbox. |

---

## 8. REST API Reference

The control plane exposes a REST + WebSocket API. All endpoints (except health and auth) require authentication.

### Authentication

**1. Get a JWT token:**

```bash
curl -X POST http://127.0.0.1:8000/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your-api-key-here-min-24-chars"}'
```

Response:

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 3600,
  "role": "operator",
  "name": "alice"
}
```

**2. Use the token in subsequent requests:**

```bash
curl http://127.0.0.1:8000/v1/agents \
  -H "Authorization: Bearer eyJ..."
```

**3. WebSocket authentication:**

```bash
# Get a one-time WS token
WS_TOKEN=$(curl -s -X POST http://127.0.0.1:8000/v1/auth/ws-token \
  -H "Authorization: Bearer eyJ..." | jq -r .token)

# Connect with the token
wscat -c "ws://127.0.0.1:8000/v1/agents/1/ws/console?token=$WS_TOKEN"
```

### Endpoints

#### Health & Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/v1/health` | No | Liveness check + agent count. |
| `POST` | `/v1/auth/token` | No | Exchange API key for JWT. |
| `POST` | `/v1/auth/ws-token` | JWT | Get a one-time WebSocket handshake token. |
| `GET` | `/v1/auth/oidc/authorize` | No | Start OIDC login flow. |
| `GET` | `/v1/auth/oidc/callback` | No | OIDC provider callback. |
| `POST` | `/v1/auth/oidc/session` | Grant cookie | Exchange OIDC grant for JWT. |

#### Agents

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/v1/agents` | JWT | List all agents (process table). |
| `POST` | `/v1/agents` | JWT (operator) | Spawn a new agent from a spec. |
| `GET` | `/v1/agents/{pid}` | JWT | Get details for a specific agent. |
| `POST` | `/v1/agents/{pid}` | JWT (operator) | Suspend, resume, or kill an agent. |
| `GET` | `/v1/agents/{pid}/logs` | JWT | Get agent log lines (up to 2000). |
| `WS` | `/v1/agents/{pid}/ws/console` | WS token | Live console stream + operator chat. |

**Spawn an agent:**

```bash
curl -X POST http://127.0.0.1:8000/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"spec": {"name": "researcher", "llm": {"model": "mock"}, "budgets": {"max_turns": 6}}}'
```

**Suspend/Resume/Kill:**

```bash
curl -X POST http://127.0.0.1:8000/v1/agents/1 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "suspend", "reason": "operator requested"}'
```

#### Scheduler, LLM, Approvals, Tools, Files, Audit

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/v1/scheduler` | JWT | Scheduler snapshot (queues, utilization, stats). |
| `GET` | `/v1/llm` | JWT | Provider failover health status. |
| `GET` | `/v1/approvals` | JWT (operator) | List all approval tickets. |
| `POST` | `/v1/approvals/{id}/approve` | JWT (operator) | Approve a ticket. |
| `POST` | `/v1/approvals/{id}/deny` | JWT (operator) | Deny a ticket. |
| `GET` | `/v1/tools` | JWT | List registered tools (optional `?query=` filter). |
| `GET` | `/v1/mcp/servers` | JWT | List registered MCP servers. |
| `POST` | `/v1/fs/search` | JWT | Semantic file search for an agent. |
| `GET` | `/v1/audit` | JWT (operator) | Query audit entries (`?event=`, `?pid=`, `?limit=`). |
| `GET` | `/v1/audit/verify` | JWT (operator) | Verify audit hash-chain integrity. |

**Semantic file search:**

```bash
curl -X POST http://127.0.0.1:8000/v1/fs/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "the Q3 analysis", "pid": 1, "top_k": 5}'
```

#### WebSocket Feeds

| Path | Description |
|------|-------------|
| `WS /v1/agents/{pid}/ws/console` | Live agent console + operator chat. |
| `WS /v1/ws/agents` | Process table live feed. |
| `WS /v1/ws/scheduler` | Scheduler live feed. |
| `WS /v1/ws/audit` | Audit event live feed. |

---

## 9. Memory & Storage

### Memory Hierarchy

AI OS provides a four-tier memory system, all accessed through syscalls:

| Tier | Analogy | Contents | Persistence |
|------|---------|----------|-------------|
| **L1 Context Window** | CPU cache | Conversation messages, tool results, retrieved knowledge | In-memory; checkpointed |
| **L2 Working Memory** | RAM | Agent scratchpad, task state, intermediate reasoning | In-memory; flushed on suspend |
| **L3 Long-Term Memory** | Disk + index | Episodic, semantic, and procedural knowledge | Persistent (embedding-indexed) |
| **Shared Pools** | Shared memory | Named, permissioned stores multiple agents can access | Persistent |

### L1 — Context Window

The context window is managed automatically by the kernel. Each LLM turn assembles:

```
system prompt → retrieved L3 hits → recent conversation → current turn
```

Agents interact with L1 via:

```python
# Read the current context
messages = await sc.read_context()

# Add a message (optionally pinned to survive summarization)
await sc.append_context("user", "Remember: the deadline is Friday.", pinned=True)

# Manually trigger summarization to free context space
await sc.summarize_context(target_tokens=2000)
```

### L2 — Working Memory

Ephemeral per-agent scratchpad. Not indexed — fast reads/writes for task state:

```python
# Write a value
await sc.write_memory("agent:42", "step", 3)

# Read it back
step = await sc.read_memory("agent:42", "step")
```

### L3 — Long-Term Memory

Persistent, embedding-indexed store. Three kinds:

| Kind | What | When to Use |
|------|------|-------------|
| `episodic` | What happened | Task milestones, decisions, outcomes |
| `semantic` | What is known | Facts, summaries, documentation |
| `procedural` | How to do it | Reusable recipes, workflows |

```python
# Write to L3 (embedding-indexed and persisted)
await sc.write_memory(
    "agent:42", "q3_findings",
    {"revenue": "$4.2M", "growth": "12%"},
    kind="semantic", tags=["q3", "finance"],
)

# Search by meaning (cosine similarity)
results = await sc.search_memory("Q3 revenue analysis", top_k=5, min_score=0.3)

# Delete a memory
await sc.forget_memory("agent:42", "q3_findings")
```

### Shared Memory Pools

Agents can access shared, named pools (declared in the spec's `memory.pools`):

```python
# Read from a shared pool (requires spec grant)
results = await sc.search_memory(
    "company hiring policy", namespace="pool:company_knowledge", top_k=3,
)
```

### Semantic File System

Every file written by agents is embedding-indexed, enabling search by meaning:

```python
# Write a file
await sc.store_artifact("~/reports/q3.md", report_content, mime="text/markdown")

# Or use the fs_write syscall
await sc.fs_write("~/notes/meeting.md", "# Meeting Notes\n...")

# Search by meaning (not by path!)
hits = await sc.fs_search("the Q3 revenue analysis", top_k=5)
# Returns: [{artifact_id, path, snippet, score, created_at}, ...]

# Traditional path-based read still works
content = await sc.fs_read("~/reports/q3.md")
```

### Checkpoints

Checkpoints capture an agent's full state (L1 context, L2 working memory, IPC mailbox, subscriptions) for crash recovery:

```python
# Manual checkpoint
checkpoint_id = await sc.checkpoint(label="milestone-1")
```

Checkpoints are also created automatically before suspension and termination.

**Data layout:**

```
aios-data/checkpoints/<checkpoint_id>/
├── snapshot.json    # Full agent state (encrypted if AIOS_ENCRYPT=1)
└── manifest.json    # Metadata + SHA-256 integrity hash
```

---

## 10. Inter-Agent Communication (IPC)

Agents communicate through kernel-managed message passing — never through shared mutable state.

### Mailboxes

Every agent has a per-agent FIFO mailbox:

```python
# Send a message (never blocks)
await sc.send_msg(
    to_pid=43,
    body={"type": "task", "description": "Summarize the report"},
    type="direct",
)

# Receive a message (blocks up to timeout_ms)
result = await sc.recv_msg(timeout_ms=10_000, filter={"type": "reply"})
if result.get("msg"):
    print(result["msg"]["body"])
```

**Key properties:**
- `send_msg` never blocks — returns immediately with a `msg_id`.
- `recv_msg` has a **mandatory timeout** — agents cannot block forever.
- Messages are delivered at-least-once; agents should be idempotent.
- Mailboxes are checkpointed — a resumed agent wakes to its full mailbox.

### Pub/Sub

Hierarchical topic-based event bus:

```python
# Subscribe to a topic
await sc.subscribe("research.done")

# Publish an event (fires to all subscribers)
delivered = await sc.publish("research.done", {
    "report_id": "rpt-42",
    "summary": "Q3 analysis complete",
})

# Unsubscribe
await sc.unsubscribe("research.done")
```

Topic permissions are declared in the agent spec (`ipc.can_subscribe`, `ipc.can_publish`).

### Task Handoff

A structured protocol for delegating work between agents:

```python
# Agent A: delegate a task to Agent B
await sc.send_msg(child_pid, {
    "type": "handoff",
    "body": {"task": "Analyze competitor pricing", "input_refs": ["artifact:report-42"]},
})

# Agent B: receive and process the handoff
msg = await sc.recv_msg(30_000, filter={"type": "handoff"})
# ... do the work ...
await sc.send_msg(msg["msg"]["from_pid"], {
    "type": "reply",
    "reply_to": msg["msg"]["msg_id"],
    "body": {"result_ref": "artifact:analysis-7"},
})
```

### Join (Synchronous Orchestration)

Wait for one or more agents to terminate:

```python
# Spawn workers
pids = [await sc.spawn(worker_spec) for _ in range(3)]

# Wait for all to finish (30 second timeout)
result = await sc.join(pids, timeout_ms=30_000)
# Returns: {results: [{pid, status, exit_status, exit_message}, ...], timed_out: false}
```

---

## 11. Tools & MCP

### Built-in Tools

AI OS ships with three kernel-managed tools:

| Tool | Description | Sandbox |
|------|-------------|---------|
| `fs.read` | Read a file from the agent's sandbox namespace. | Sandboxed (virtual paths only) |
| `fs.write` | Write a file to the agent's sandbox namespace (embedding-indexed). | Sandboxed (virtual paths only) |
| `shell.run` | Execute a shell command in a sandboxed subprocess. | Requires approval by default |

```python
# Read a file
result = await sc.call_tool("fs.read", {"path": "note-1.md"})

# Write a file
await sc.call_tool("fs.write", {"path": "output/report.md", "content": "# Report\n..."})

# Run a shell command (may require approval)
result = await sc.call_tool("shell.run", {"command": "pytest tests/"})
```

### MCP Integration

AI OS is an MCP (Model Context Protocol) client. External tool servers (web search, code execution, databases, browsers) are registered at boot and accessed through the same `call_tool` syscall:

```python
# List available tools
tools = await sc.list_tools(query="search")

# Call an MCP tool
result = await sc.call_tool("web.search", {"query": "AI OS agent framework", "max_results": 5})

# Cancel a running tool call
await sc.cancel_tool(result["call_id"])
```

### Tool Approval Flow

Tools flagged with `needs_approval: true` in the spec require human approval before execution:

1. Agent calls `call_tool("code.exec", {...})`.
2. Kernel creates an approval ticket and parks the agent.
3. Operator reviews the ticket in the CLI or web desktop.
4. Operator approves → agent resumes and the tool executes.
5. Operator denies → agent receives `E_PERM`.

```python
# Agent-side: request permission explicitly
await sc.request_permission(
    "code.exec", {"command": "pytest"},
    reason="Running unit tests to verify changes",
)
```

**Operator approval (control side):**

```python
from aios_sdk import ControlPlane

cp = ControlPlane(kernel)
tickets = cp.approvals()           # list pending tickets
await cp.approve("ticket-id-91")   # approve
cp.deny("ticket-id-92")            # deny
```

Or via the REST API:

```bash
curl -X POST http://127.0.0.1:8000/v1/approvals/t-91/approve \
  -H "Authorization: Bearer $TOKEN"
```

---

## 12. Security & Permissions

AI OS is a **security-first** system. The kernel is the trusted computing base; everything else is untrusted.

### Core Principles

- **Deny by default** — agents can do nothing until their spec grants permission.
- **Least privilege** — the spec is the capability list; nothing is implicit.
- **Fail secure** — any check error results in denial, never allowance.
- **Everything audited** — immutable, tamper-evident audit trail.
- **No secrets in the model** — credentials never enter context, logs, or checkpoints.

### RBAC Roles

| Role | Can Do | Cannot Do |
|------|--------|-----------|
| `operator` | Spawn/suspend/resume/kill any agent; change budgets; register tools; read audit; approve tickets. | — |
| `standard` | Spawn agents under own group; use granted tools/pools. | Elevate others; read secrets beyond granted keys. |
| `restricted` | Spawn agents with read-only memory, no network tools. | Network access, code execution. |
| `service` | Headless automation (no interactive approval). | — (tighter budgets). |

### Per-Agent Permissions

An agent's effective permissions are computed **once at spawn** and are **immutable** for its lifetime:

```
effective = role_base_capabilities ∪ spec_declared_grants
```

Every syscall is checked by Access Control. An empty permission set returns `E_PERM` for everything.

### Sandbox Profiles

| Profile | Isolation | Use Case |
|---------|-----------|----------|
| `inprocess` | Runs in the kernel process. | Trusted, low-overhead tools. |
| `subprocess` | Separate process with rlimits, restricted cwd, no network. | Default for all agents. |
| `container` | Docker container with read-only rootfs, `--cap-drop ALL`, seccomp. | High-risk tools, untrusted code. |

### Secrets Vault

Secrets are stored in `aios-data/kernel/credentials.json` (AES-256-GCM encrypted when `AIOS_ENCRYPT=1` or `AIOS_MASTER_KEY` is set). Agents access secrets via:

```python
value = await sc.get_env("DB_READONLY_URL")
```

- Only keys listed in `env.allowed_keys` are accessible.
- Values are **redacted** in all kernel-owned boundaries: audit log, checkpoints, memory, context, and logs.

### Audit Log

Every syscall, tool call, and state mutation is recorded in a hash-chained, append-only log:

```bash
# Query audit entries via REST API
curl "http://127.0.0.1:8000/v1/audit?event=call_tool&limit=50" \
  -H "Authorization: Bearer $TOKEN"

# Verify hash-chain integrity
curl "http://127.0.0.1:8000/v1/audit/verify" \
  -H "Authorization: Bearer $TOKEN"
```

### At-Rest Encryption

Enable AES-256-GCM encryption for vault and checkpoint snapshots:

```bash
# Option 1: Provide a master key
export AIOS_MASTER_KEY="your-64-char-hex-key"

# Option 2: Auto-generate a key file
export AIOS_ENCRYPT=1
# Creates aios-data/master.key (mode 0600)
```

---

## 13. Configuration

AI OS is configured through environment variables. Set these before running `aios serve` or any CLI command.

### API Keys & Authentication

| Variable | Description | Example |
|----------|-------------|---------|
| `AIOS_API_KEYS` | Comma-separated API keys: `name:key:role`. Keys must be ≥ 24 characters. | `alice:my-secret-key-24chars:operator` |
| `AIOS_JWT_SECRET` | Secret for signing JWTs. Auto-generated if unset (tokens expire at restart). | Random 48+ char string |
| `AIOS_DEV_KEY` | Set to `1` to enable the built-in dev key `dev-key` (operator role) when no API keys are configured. | `1` |

### Encryption

| Variable | Description |
|----------|-------------|
| `AIOS_MASTER_KEY` | Hex or base64 master key for AES-256-GCM encryption of vault and checkpoints. |
| `AIOS_ENCRYPT` | Set to `1` to auto-generate a master key at `aios-data/master.key`. |

### LLM & Embeddings

| Variable | Description | Default |
|----------|-------------|---------|
| `AIOS_EMBED_URL` | OpenAI-compatible embeddings endpoint. If unset, uses a deterministic hashing embedder (offline). | — |

### OIDC (Single Sign-On)

| Variable | Description |
|----------|-------------|
| `AIOS_OIDC_ISSUER` | OIDC provider issuer URL (e.g. `https://accounts.google.com`). |
| `AIOS_OIDC_CLIENT_ID` | OIDC client ID. |
| `AIOS_OIDC_CLIENT_SECRET` | OIDC client secret. |
| `AIOS_OIDC_REDIRECT_URI` | Callback URL (default: auto-derived from request). |
| `AIOS_OIDC_SCOPES` | Space-separated scopes (default: `openid profile email`). |
| `AIOS_OIDC_ADMIN_EMAILS` | Comma-separated emails granted the `operator` role. |
| `AIOS_OIDC_OPERATOR_VALUES` | Comma-separated ID-token claim values granted the `operator` role. |
| `AIOS_OIDC_CLAIM` | ID-token claim to match against (default: `email`). |
| `AIOS_OIDC_POST_LOGIN` | Relative path to redirect after login (default: `/`). |
| `AIOS_OIDC_TIMEOUT_S` | Provider HTTP timeout in seconds. |
| `AIOS_OIDC_CACHE_TTL_S` | Discovery/JWKS cache TTL in seconds. |

### Multi-Kernel Broker

| Variable | Description |
|----------|-------------|
| `AIOS_BROKER_TOKEN` | Authentication token for cross-kernel IPC broker connections. |

### Web Desktop

| Variable | Description |
|----------|-------------|
| `AIOS_WEB_DIST` | Override path to the web desktop build (default: `web/dist`). |

---

## 14. Troubleshooting & FAQ

### Common Issues

**`aios: command not found`**

The package isn't installed or the virtual environment isn't activated:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

**`RuntimeError: aios.syscalls used outside an agent turn`**

You're calling syscall functions outside of an `@agent`-decorated turn function. Syscalls are only available inside agent turns where the kernel has bound a session.

**`AiosPermissionError: E_PERM`**

The agent's spec doesn't grant the required permission. Check:
- Is the tool listed in `capabilities.tools`?
- Is the memory pool listed in `memory.pools`?
- Is the IPC target in `ipc.can_send_to`?
- Does the tool require approval (`needs_approval: true`)?

**`AiosBudgetError: E_BUDGET`**

The agent has exhausted one of its budgets (turns, tokens, cost, wall-clock, or tool calls). The agent is suspended, not terminated — increase the budget in the spec and resume:

```bash
aios resume --agents-module my_agents
```

**`AiosTimeoutError: E_TIMEOUT`**

A blocking syscall (`recv_msg`, `join`) exceeded its timeout. This is expected behavior — always handle timeouts gracefully in agent code.

**`No @agent registered for spec name '...'`**

The agent spec's `name` field doesn't match any `@agent(name="...")` registration. Ensure:
1. The `--agents-module` flag points to the correct module.
2. The module is importable (on `sys.path`).
3. The `@agent(name=...)` matches the spec's `name` exactly.

**Web desktop shows "connection refused"**

Ensure `aios serve` is running:

```bash
aios serve --port 8000
```

If using the Vite dev server, ensure it's proxying correctly (check `web/vite.config.ts`).

**OIDC login fails**

- Verify `AIOS_OIDC_ISSUER` and `AIOS_OIDC_CLIENT_ID` are set correctly.
- For local dev with Vite, set `AIOS_OIDC_REDIRECT_URI=http://localhost:5173/v1/auth/oidc/callback`.
- Check that the OIDC provider allows the redirect URI.

### FAQ

**Q: Can I use a real LLM instead of the mock model?**

Yes. Set the `model` field in your agent spec to any OpenAI-compatible model name (e.g. `"gpt-4o"`, `"gpt-3.5-turbo"`). The kernel's LLM Core routes requests to the configured provider. For local models, use vLLM or Ollama with an OpenAI-compatible endpoint.

**Q: How do I add custom tools?**

Register an MCP tool server. The kernel's MCP client connects to stdio or HTTP/JSON-RPC tool servers at boot. See the MCP specification for how to build a tool server.

**Q: Can agents spawn other agents?**

Yes, if the spec sets `capabilities.spawn: true`. The child agent must also have a valid, registered spec.

**Q: How does crash recovery work?**

When the kernel starts with `aios resume`, it reads `aios-data/session.json`, which lists every agent that was suspended at the time of the last shutdown (or crash). Each agent is restored from its last committed checkpoint — context, working memory, mailbox, and subscriptions are all recovered.

**Q: Is the audit log tamper-proof?**

The audit log is hash-chained: each record contains a SHA-256 hash of the previous record. The `/v1/audit/verify` endpoint re-derives the chain and reports any tampering.

**Q: How do I run benchmarks?**

```bash
aios bench
```

This runs scheduler fairness, throughput, and checkpoint I/O benchmarks and writes a report to `benchmarks/reports/`.

---

## Further Reading

For deeper architectural details, see the design specification documents in `docs/`:

| Document | Topic |
|----------|-------|
| `docs/00-vision.md` | Vision, problem statement, design principles |
| `docs/01-overview.md` | Architecture layers, boot sequence, data flows |
| `docs/02-kernel.md` | Agent lifecycle, syscall ABI, agent spec schema |
| `docs/03-scheduler.md` | Scheduling algorithm, preemption, budgets |
| `docs/04-memory.md` | Memory hierarchy (L1/L2/L3), RAG, shared pools |
| `docs/05-storage.md` | Checkpoints, artifacts, semantic file system |
| `docs/06-ipc.md` | Mailboxes, pub/sub, handoff, multi-kernel broker |
| `docs/07-tools.md` | Tool registry, MCP integration, sandboxed execution |
| `docs/08-security.md` | Threat model, RBAC, sandboxing, secrets, audit |
| `docs/09-sdk.md` | SDK API reference, framework adapters |
| `docs/10-ui.md` | CLI shell, web desktop, REST/WS control plane |
| `docs/11-roadmap.md` | Implementation roadmap and acceptance criteria |
| `docs/12-tech-stack.md` | Technology choices and rationale |

---

*AI OS — Security-first. Deny by default. Everything audited. Agents are processes.*


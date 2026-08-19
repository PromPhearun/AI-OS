# 09 — SDK & Syscall API

**Status:** Draft (v0.1)
**Relates to:** `02-kernel.md` (canonical ABI), `10-ui.md`.

---

## 1. What the SDK Is

The AI OS SDK (`import aios`) is the **only library agents import**. It is a thin, dependency-light
client for the syscall ABI (see `02-kernel.md` §5) plus agent-authoring helpers. It is not a
framework — the kernel is the framework. The SDK has two faces:

1. **Agent side** — what agent code calls to use kernel services.
2. **Control side** — what operators/tools call to launch and supervise agents (shared with the shell).

## 2. Agent Definition

Agents are declared with a spec (canonical schema in `02-kernel.md` §4) and an entry function:

```python
from aios import agent, syscalls as sc

@agent(spec_file="specs/research_analyst.json")
def research_analyst(task: str) -> None:
    """Main entry point — runs inside the agent's sandbox."""
    results = sc.call_tool("web.search", {"query": task, "max_results": 5})
    report = summarize(results)
    sc.store_artifact("~/report.md", report, mime="text/markdown")
    sc.send_msg(sc.get_pid()["parent_pid"], {"type": "reply", "body": {"artifact": "~/report.md"}})
    sc.exit(0)
```

The kernel **runs** the entry function; the SDK wires `syscalls` to the kernel channel
(in-process or out-of-process JSON-RPC, transparently).

## 3. Agent-Side API (Python)

```python
# Lifecycle
sc.spawn(spec_ref, parent_pid=sc.get_pid()["pid"])      # returns pid
sc.exit(status=0, message="done")
sc.get_pid()
sc.suspend(reason="operator requested")
sc.resume(pid)
sc.get_status(pid=None)

# Context
sc.read_context()
sc.append_context(role="user", content="...", pin=True)
sc.summarize_context(target_tokens=2000)

# Memory
sc.write_memory(namespace="agent:42", key="findings", value={...}, ttl=None)
sc.read_memory(namespace="agent:42", key="findings")
sc.search_memory("Q3 revenue analysis", top_k=5, namespace="agent:42")

# IPC
sc.send_msg(to_pid=7, body={"type": "handoff", "task": "..."})
sc.recv_msg(timeout_ms=10_000, filter={"type": "reply"})
sc.subscribe("research.done"); sc.publish("research.done", {...}); sc.unsubscribe(...)
sc.join(pids=[5, 6], timeout_ms=30_000)

# Tools
sc.list_tools(query="search")
sc.call_tool("web.search", {"query": "..."})
sc.cancel_tool(call_id)

# Storage
sc.checkpoint(label="milestone-1")
sc.store_artifact("~/report.md", content, mime="text/markdown")
sc.fs_read("~/notes.txt"); sc.fs_write("~/notes.txt", "...")
sc.fs_search("the analysis I wrote about costs")

# Security & usage
sc.get_env("DB_READONLY_URL")                 # only allowed_keys
sc.request_permission("code.exec", "run_tests", "verify unit tests pass")
sc.get_usage()
```

**SDK guarantees:**
- Typed exceptions mapping 1:1 to syscall error codes (`AiosPermissionError`, ...).
- Client-side schema validation of every syscall args (fast `E_INVAL`).
- `get_env` values are redacted in `repr()` and logs.
- Idempotency helper `aios.ipc.dedupe(trace_id)` for at-least-once messaging.

## 4. Syscall Channel

```
SDK.syscall(name, args)
   ├── validate(args)                 # JSON Schema
   ├── serialize + envelope           # {abi_version, pid, syscall, args, trace_id}
   ├── in-process   → kernel.syscall(name, args)     # direct async
   └── out-of-process → JSON-RPC over local TLS socket
   └── receive {ok, result} | {error: {code, message}}
```

The channel is **symmetrical** for the control side (shell spawns agents through the same ABI).

## 5. Control-Side API (launch & supervise)

```python
import aios.control as aio

pid = aio.launch("specs/research_analyst.json", task="Q3 competitive landscape")
aio.ps()                      # process table view
aio.suspend(pid); aio.resume(pid); aio.kill(pid)
aio.approve_ticket(ticket_id="t-91", allow=True)
aio.stream_logs(pid)          # agent console + audit events
aio.attach(pid)               # interactive chat with a running agent
```

## 6. Framework Adapters

AI OS runs alongside popular agent frameworks rather than competing with their authoring model.
Adapters map framework concepts onto kernel syscalls:

| Framework | Adapter mapping |
|---|---|
| **LangGraph** | Graph nodes run as agent turns; graph state ↔ L2 working memory; `interrupt` ↔ `suspend`; checkpointer ↔ kernel checkpoint |
| **AutoGen** | ConversableAgent ↔ kernel agent; chat messages ↔ IPC `send_msg`/`recv_msg` |
| **CrewAI** | Crew ↔ scheduling group; Task ↔ handoff message; tool use ↔ `call_tool` |
| **Custom/ReAct loop** | trivially expressed with `read_context`/`append_context`/`call_tool` |

Adapter principle: **the kernel owns resources; the framework owns the loop logic.** A LangGraph
agent running on AI OS still expresses its graph, but every LLM call is a scheduled kernel
request, every tool call is a permissioned kernel call, and its state is kernel-checkpointed.

## 7. Spec Validation

- Specs are validated against the canonical JSON Schema **before** spawn (client + kernel
  double-validation).
- Rejected specs return structured `E_INVAL` details to the caller *without* echoing any
  embedded secret-looking fields.
- `aios.validate_spec(path)` is available to CI pipelines so spec authors get fast feedback.

## 8. SDK Packaging

- `aios` — agent-side runtime (`agent`, `syscalls`, `ipc`, `memory`, `tools`, `storage` helpers).
- `aios.control` — operator API (launch, ps, suspend/resume/kill, approvals, logs).
- `aios.adapters` — framework adapters (optional extras: `aios[langgraph]`, `aios[autogen]`).
- Python ≥ 3.11 (kernel + SDK); typed, `py.typed`, zero required runtime deps beyond a JSON-RPC
  transport and `jsonschema`.

## 9. Open Design Decisions (to resolve at implementation)

1. **Agent entry contract** — Python callable signature `(task: dict) -> None` (recommended)
   vs. a declarative-only model (no custom code). The hybrid (declarative spec + optional code)
   is recommended: many agents need no custom code at all.
2. **Streaming turns** — whether `sc.call_tool`/LLM turns support token streaming to the shell
   (nice UX; adds protocol complexity). Recommend: control-plane streaming only in v1.
3. **Multiple SDK languages** — Python first (ecosystem); JS/TS control SDK deferred to v1.5
   (UI can use the REST API instead).
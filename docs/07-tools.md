# 07 — Tool Manager

**Status:** Draft (v0.1)
**Relates to:** `02-kernel.md` (syscalls 22–24), `03-scheduler.md` (tool scheduler), `08-security.md`.

---

## 1. Role

The Tool Manager is the kernel's **device-driver layer**: it owns the registry of everything an
agent can *do* (search the web, run code, read files, hit APIs), and it is the **only** module
that may execute a tool. Agents never hold tool credentials or invoke tools directly.

## 2. Tool Registry & Tool Spec

Tools are registered by **tool servers** (drivers) at boot or hot-loaded at runtime. Each tool
has a validated spec:

```jsonc
{
  "id": "web.search",
  "version": "1.2",
  "title": "Web search",
  "description": "Search the web and return ranked results.",
  "server": "mcp://search-provider",      // how it's reached (MCP by default)
  "parameters": {                          // JSON Schema for args
    "type": "object",
    "properties": {
      "query": { "type": "string", "minLength": 1, "maxLength": 500 },
      "max_results": { "type": "integer", "minimum": 1, "maximum": 20 }
    },
    "required": ["query"]
  },
  "returns": { "type": "object", "properties": { "results": { "type": "array" } } },
  "auth": "credential:search_api",          // resolved at runtime, never in agent context
  "cost": { "tokens": 0, "money_usd": 0.001 },   // charged to caller's budget
  "limits": { "rate_per_min_per_agent": 10, "rate_per_min_global": 60, "timeout_s": 30 },
  "sandbox": "network:http",
  "needs_approval": false,
  "deprecated": false
}
```

**Registry invariants:**
- Tool IDs are globally unique; versioned; `deprecated` tools remain callable but flagged.
- The registry is *read-only to agents* (`list_tools`); only the kernel (or an operator with
  `admin.tools` role) registers/updates tools.
- An agent's spec must reference tools by ID; unknown IDs fail spec validation at spawn.

## 3. MCP (Model Context Protocol) Integration

MCP is the standard for tool servers (code-exec, browser, filesystem, DB, etc.). AI OS is an
**MCP client by default**:

- `server` field addresses an MCP server (stdio or HTTP/SSE transport in v1).
- `call_tool` → Tool Manager → MCP `tools/call`; results and MCP errors are normalized into
  the kernel's return envelope.
- MCP-provided tool schemas are **validated and hardened** by the kernel before registration:
  parameters are re-validated against the kernel's stricter JSON Schema (length/type/range
  limits) so agents can't smuggle oversized or malformed args.
- Non-MCP tools (kernel built-ins like `fs.read`, `memory.*`) are registered natively but expose
  the *same* spec shape — agents can't tell the difference.

## 4. Tool Execution Pipeline

Every `call_tool` runs through the pipeline (each step is audited):

```
call_tool(id, args)
 1. Validate     → args against JSON Schema (E_INVAL on fail)
 2. Permission   → Access Control: tool in allowlist? needs_approval?
                  → (E_PERM / approval ticket)
 3. Budget       → scheduler: cost, rate limits, max_tool_calls (E_BUDGET)
 4. Schedule     → tool scheduler: sandbox slot, concurrency cap
 5. Resolve      → auth credential from vault; substitute into request
 6. Execute      → MCP call / builtin handler (deadline enforced)
 7. Sanitize     → output size-capped; PII/link scanning (see 08-security)
 8. Record       → audit log: {call_id, tool, args_hash, result_hash, cost, duration}
 9. Return       → {result, meta: {call_id, tokens, cost, provider_ms}}
```

**Key properties:**
- **Sandbox by declaration:** each tool declares its `sandbox` capability; the kernel binds the
  execution to the matching sandbox profile (network egress allowlist, no host FS, etc.).
- **Output caps:** tool results are truncated at a per-tool `max_output_chars` (default 32 KB)
  before entering agent context — protecting both context budget and prompt-injection surface.
- **Cancellation:** `cancel_tool(call_id)` aborts at MCP-level cancel or deadline; agent wakes
  with `E_ABORT`.

## 5. Retries, Timeouts & Failures

| Scenario | Kernel behavior |
|---|---|
| Transient error (5xx, timeout) | bounded retries with exponential backoff (default 2) |
| Rate-limited (429) | wait-and-retry within caller's rate budget |
| Server crash / transport lost | `E_INTERNAL`, tool marked `degraded`, callers may retry |
| Hard failure (E_INVAL from server) | `E_INTERNAL` surfaced; no retry |
| Agent-cancel | `E_ABORT` |

## 6. Capability Discovery for Agents

- `list_tools(query?)` filters by name/description/keywords so agents can find tools at runtime.
- Tool descriptions are written to be **agent-parseable** (concise, declarative) so the model
  can pick tools reliably.
- The Context Manager can auto-inject *relevant* tool descriptions into L1 on demand
  (kernel-managed, bounded) to keep the prompt small.

## 7. Security Notes (details in `08-security.md`)

- **No raw credentials in context.** Secrets are resolved by the Tool Manager from the vault at
  execution time and never enter the agent's context window or logs.
- **Deny-by-default.** A tool the agent's spec does not list returns `E_PERM` — even if the
  registry has it.
- **Human gates.** Tools flagged `needs_approval` (e.g. `code.exec`, `email.send`) route through
  `request_permission` before execution.
- **Prompt-injection containment.** Tool output is treated as *untrusted data*: size-capped,
  link-scanned, and delimited in the context with explicit framing so model instructions inside
  tool results are less likely to hijack the agent (defense in depth — see §5 of `08`).

## 8. Open Design Decisions (to resolve at implementation)

1. **MCP transport support** — stdio + HTTP in v1; SSE long-poll if needed later. **Resolved
   (Phase 3):** stdio-subprocess and HTTP/JSON-RPC clients are implemented in
   `aios_kernel/modules/mcp.py`; SSE remains a future option.
2. **Tool descriptions auto-injection** — how aggressively to inject tool docs into L1 (bounded,
   relevance-ranked; default off until measured).
3. **Rate limit dimension** — per-agent vs. per-group vs. global defaults; recommend per-agent
   defaults overridable by group quota.
4. **Approval UX** — where human approval happens first: CLI prompt, web UI toast, or headless
   webhook (deployment-dependent).
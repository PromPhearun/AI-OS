# 05 — Storage Manager & Semantic File System

**Status:** Draft (v0.1)
**Relates to:** `02-kernel.md` (syscalls 25–29), `04-memory.md`, `08-security.md`.

---

## 1. What the Kernel Persists

| Kind | Examples | Backing store |
|---|---|---|
| **Agent state** | ACB, lifecycle, budgets, usage | metadata DB (SQLite) |
| **Checkpoints** | L1 context snapshot + L2 working memory + sandbox metadata | metadata DB + object store |
| **Artifacts** | files agents produce (reports, code, data) | object store (local FS in v1) |
| **Long-term memory** | embeddings + entries | vector store |
| **Shared pools** | pool entries | vector store + metadata DB |
| **Audit log** | syscall records, decisions | append-only log (hash-chained) |
| **Config** | kernel config, RBAC roles, tool registry | TOML/JSON files + DB |

Single-writer principle: each store has exactly one owning kernel module (Storage Manager owns
artifacts/checkpoints; Memory Manager owns memory; Audit owns the log; etc.).

## 2. Logical Layout

```
aios-data/
├── kernel/            # aios.toml, roles.json, tool-registry.json
├── agents/            # per-agent working dirs (sandbox view)
│   └── <pid>/
│       ├── spec.json          # copy of the validated spec
│       └── scratch/           # sandbox filesystem (allowed by fs_read/fs_write)
├── checkpoints/
│   └── <checkpoint_id>.snap   # L1+L2 snapshot (+ manifest)
├── artifacts/
│   └── <artifact_id>/         # content + meta.json (mime, pid, created_at)
├── memory/            # vector index + entry store (owned by Memory Manager)
├── pools/<pool_name>/ # shared pool data
└── audit/             # immutable, hash-chained log
```

## 3. Checkpoints (syscalls 6, 7, 25)

> **Phase 2 status — Slice 2.1:** implemented on-disk layout
> (`<root>/checkpoints/<id>/snapshot.json` + `manifest.json` with `hash`/`committed`,
> sha256-verified on restore) plus `aios-data/session.json`, the atomic resume set
> that powers `aios resume`.
>
> **Slice 2.2:** the snapshot now also carries `mailbox` (envelopes, re-signed on load)
> and `subscriptions`, so an agent resumed after a crash wakes to a faithful IPC state
> (see `06-ipc.md` §10).
>
> **Phase 5 (Slice 5.1):** `snapshot.json` is sealed with AES-256-GCM at rest when the
> kernel has a master key (`AIOS_MASTER_KEY`, or `AIOS_ENCRYPT=1` → `<root>/master.key`
> mode 0600). The manifest `hash` covers the ciphertext on disk and GCM authenticates it,
> so a wrong key or any tampering fails closed (`docs/08-security.md` §8). The plaintext
> layout below is unchanged when no key is present (v1 behavior).

A checkpoint is the **unit of resumability**:

```jsonc
// manifest of a checkpoint
{
  "checkpoint_id": "ckpt-8f3a",
  "pid": 42,
  "state": "SUSPENDED",
  "created_at": "...",
  "label": "mid-research",
  "parts": {
    "context": "ctx-snap-8f3a",        // L1: full message list
    "working_memory": "wm-snap-8f3a",  // L2: scratchpad JSON
    "sandbox": { "cwd": "/home/agent", "env_keys": ["..."] },  // secret VALUES never stored here
    "memory_refs": ["agent:42", "pool:company_knowledge"]      // L3 refs, not copies
  },
  "hash": "sha256:...",                // integrity
  "committed": true                    // durable before ack
}
```

- **Write path:** Context Manager serializes L1 → Memory Manager flushes L2 → Storage Manager
  writes parts + manifest → fsync → mark `committed` → ack `checkpoint_id`.
- **Restore path:** load manifest → verify hash → rebuild L1/L2 → re-link L3 refs → enqueue agent.
- **GC policy:** `max_snapshots` per agent (from spec); old snapshots are retained for rollback,
  then pruned by age/size policy.

## 4. Artifacts (syscalls 26–28)

> **Phase 2 status — Slice 2.3:** implemented. `store_artifact {path, data,
> mime?}` writes content into the sandbox and returns an `artifact_id`;
> `fs_read {path, max_bytes?}` and `fs_write {path, content, mime?}` are the
> canonical read/write syscalls (virtual paths, traversal-rejected by
> construction). The `fs.read` / `fs.write` tools delegate to the same
> implementation, so every write is embedding-indexed once.

- `store_artifact` writes content by path *within the agent's sandbox view*; the kernel assigns
  an `artifact_id` and records metadata.
- `fs_read` / `fs_write` operate on the agent's sandbox namespace — an agent can *never* address
  an absolute host path. Paths are virtualized:
  `~/report.md → aios-data/agents/<pid>/scratch/report.md`.
- MIME types are sniffed from content (magic bytes), never trusted from the filename
  (see `08-security.md` §File safety).
- Artifacts are immutable once written; updates create new versions (`artifact_id` changes).

## 5. Semantic File System (syscall 29: `fs_search`)

> **Phase 2 status — Slice 2.3:** implemented. Every successful `fs.write`
> tool call / `fs_write` / `store_artifact` is embedding-indexed into
> `aios-data/memory/artifacts.jsonl` (same vector store as L3 memory);
> `fs_search {query, top_k?}` returns ranked hits
> `{artifact_id, path, mime, snippet, score, created_at}` for the caller's own
> artifacts. Artifacts are namespaced by agent (isolation enforced; a global
> `shared://` publish namespace is deferred). Fallback path addressing always
> works (`fs_read`).

Files and checkpoints are **embedding-indexed** at write time, giving natural-language access:

- `fs_search("the Q3 revenue analysis I wrote last week", top_k=5)`
  → ranked hits `{artifact_id, path, snippet, score, created_at}`.
- The index is the same vector store as L3 memory; artifacts are namespaced by agent + a global
  `shared://` namespace for artifacts the owner publishes.
- Fallback: exact path queries always work (`fs_read`), so the semantic index is an *addition*,
  never a replacement for path addressing.

This mirrors prior art (LLM-based semantic file systems, e.g. AIOS-adjacent work) and is a
signature differentiator: **agents remember what they produced, not just where they put it.**

## 6. Backup, Restore & Integrity

- `aios-data` is designed to be snapshotted as a whole (directory-level backup).
- Restore = copy tree + verify manifest hashes; vector index rebuilds from entry store on demand.
- The audit log is **hash-chained**: each record contains the hash of the previous record,
  making silent tampering detectable at verification time.

## 7. Security Notes (details in `08-security.md`)

- **Path traversal is impossible by construction**: agents address virtual paths only; the
  kernel resolves them inside the sandbox namespace after allowlist checks.
- **Secret VALUES never live in checkpoints or artifacts** — only key names. Secrets resolve at
  runtime via `get_env` (Access Control vault).
- **Write amplification** is bounded: semantic indexing is async and batchable.

## 8. Open Design Decisions (to resolve at implementation)

1. **Vector store for v1** — embedded (e.g. LanceDB/Chroma in-process) vs. server
   (Qdrant/pgvector). Recommend embedded for v1; see `12-tech-stack.md`.
2. **Artifact versioning depth** — keep all versions vs. latest N. Recommend latest N (default 5)
   with full history for audit-pinned artifacts.
3. **Semantic index for binary files** — index only text/markdown/code MIME types in v1; binary
   files get metadata-only indexing.
4. **Checkpoint storage format** — JSON lines vs. msgpack. Recommend msgpack for L1/L2 snapshots
   (smaller, faster); JSON for manifests (auditable by hand).
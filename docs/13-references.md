# 13 — References & Prior Art

**Status:** Draft (v0.1)

---

## 1. Primary Prior Art

### AIOS — LLM Agent Operating System
- **Paper:** Kai Mei et al., "AIOS: LLM Agent Operating System", COLM 2025. arXiv:2403.16971.
- **Code:** https://github.com/agiresearch/AIOS
- **What we took:** the kernel/sdk split; the module set (scheduler, context, memory, storage,
  tool, access); the thesis that scheduling/context/memory are *kernel* concerns.
- **Where we diverge/go deeper:** explicit IPC/mailbox design, security/access-control depth,
  semantic file system, MCP-native tools, and a defined agent lifecycle/ABI versioning contract.

### Supporting AIOS-line work
- Ge et al., "LLM as OS, Agents as Apps: Envisioning AIOS, Agents and the AIOS-Agent Ecosystem"
  (arXiv:2312.03815) — the vision paper.
- Rama et al., "Cerebrum (AIOS SDK)" (NAACL 2025) — SDK/deployment/distribution model.
- Shi et al., "From Commands to Prompts: LLM-based Semantic File System for AIOS" (ICLR 2025) —
  the semantic FS idea we adopt for `fs_search`.
- Mei et al., "LiteCUA: Computer as MCP Server for Computer-Use Agent on AIOS" (arXiv:2505.18829)
  — MCP-first tooling pattern.
- Xu et al., "A-Mem: Agentic Memory for LLM Agents" (arXiv:2502.12110) — agentic memory design
  informing our L2/L3 split.

## 2. Standards & Protocols

| Standard | Why it matters here |
|---|---|
| **MCP — Model Context Protocol** | tool server standard; our tool layer speaks it (see `07-tools.md`) |
| **OpenAI-compatible API** | the de-facto model-serving interface; our LLM core uses it |
| **JSON Schema** | every syscall arg and agent spec is schema-validated |
| **OWASP Top 10 / CWE Top 25** | security baseline adapted in `08-security.md` |

## 3. Conceptual Background

- **Operating systems classic:** scheduling, IPC, memory tiers, checkpointing — see any standard
  OS text (Tanenbaum; Silberschatz) for the vocabulary this spec borrows deliberately.
- **Agent architectures:** ReAct (Yao et al., ICLR 2023); function-calling LLMs; multi-agent
  frameworks (AutoGen, LangGraph, CrewAI) — the ecosystems we run as adapters.
- **Agentic memory research:** surveys on LLM-agent memory mechanisms (e.g. Zhang et al.,
  arXiv:2404.13501) — informs L3 episodic/semantic/procedural split.

## 4. Companion Research Directions (monitored, not yet adopted)

- **Computer-use agents** (OSWorld, LiteCUA) — GUI automation as a tool; likely a Phase 5 tool.
- **Agent frameworks as orchestration** — the "frameworks vs OS" debate; our position is
  documented in `09-sdk.md` §6 (kernel owns resources, framework owns loop logic).
- **Multi-kernel / distributed agents** — our IPC interface reserves a broker swap (Phase 5).

## 5. Evaluation Benchmark Notes

Phase 4 benchmarks should follow AIOS-style methodology where practical:
- Throughput: tasks completed per unit time under concurrency.
- Correctness of context switch: resume fidelity (their evaluation section).
- Scalability: agents added → utilization/wait-time curves.
Public agent benchmarks (HumanEval, GAIA, SWE-bench-Lite) can be run *through* AI OS to show
the kernel adds negligible overhead to agent capability while adding governance.
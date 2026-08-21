"""Benchmark agent definitions (registered into AGENT_REGISTRY via @agent).

The benchmark specs name these entries; ``run_agents`` resolves them through
``_entry_for`` just like any user agent.
"""

from __future__ import annotations

from aios import agent
from aios.errors import AiosNoEntError


@agent(name="bench-fair")
async def bench_fair(sc) -> bool:
    """Heterogeneous scheduler load: one LLM turn + yield per step.

    Work scales with pid (``4 + pid % 5`` steps) so lower-priority agents are
    smaller jobs — with priority + aging, no agent should starve.
    """
    pid = (await sc.get_pid())["pid"]
    ns = f"agent:{pid}"  # own namespace — read/write requires ownership
    try:
        turns = await sc.read_memory(ns, "turns")
    except AiosNoEntError:
        turns = 0
    if turns >= 4 + (pid % 5):
        await sc.log("info", f"fair {pid} done after {turns} turns")
        return True
    await sc.generate(f"fair step {turns + 1}")
    await sc.yield_cpu()
    await sc.write_memory(ns, "turns", turns + 1)
    return False


@agent(name="bench-task")
async def bench_task(sc) -> bool:
    """Realistic task mix: one LLM round + one artifact write per step."""
    pid = (await sc.get_pid())["pid"]
    ns = f"agent:{pid}"  # own namespace — read/write requires ownership
    try:
        turns = await sc.read_memory(ns, "turns")
    except AiosNoEntError:
        turns = 0
    if turns >= 6:
        await sc.log("info", f"task {pid} done after {turns} turns")
        return True
    reply = await sc.generate(f"task step {turns + 1}")
    await sc.fs_write(f"out-{turns + 1}.md", reply["text"])
    await sc.write_memory(ns, "turns", turns + 1)
    return False
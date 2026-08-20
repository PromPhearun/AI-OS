"""Example agents and specs for the AI OS Phase 1 demo."""

from __future__ import annotations

from aios import agent
from aios.errors import AiosNoEntError

RESEARCHER_SPEC = {
    "name": "researcher",
    "version": "1",
    "description": "Gathers research notes (example agent).",
    "group_id": "demo",
    "priority": 0,
    "llm": {"model": "mock", "system": "You are a research assistant. Be concise.", "temperature": 0.0},
    "budgets": {"max_turns": 6, "max_tool_calls": 20},
    "capabilities": {"tools": [{"name": "fs.write"}]},
}

WRITER_SPEC = {
    "name": "writer",
    "version": "1",
    "description": "Drafts report sections (example agent).",
    "group_id": "demo",
    "priority": 0,
    "llm": {"model": "mock", "system": "You are a report writer. Be concise.", "temperature": 0.0},
    "budgets": {"max_turns": 4, "max_tool_calls": 20},
    "capabilities": {"tools": [{"name": "fs.write"}]},
}


def demo_specs() -> list[dict]:
    return [dict(RESEARCHER_SPEC), dict(WRITER_SPEC)]


@agent(name="researcher")
async def researcher(sc) -> bool:
    """One LLM round per turn; writes a progress note; done after 3 rounds."""
    ns = f"agent:{(await sc.get_pid())['pid']}"
    try:
        rounds = await sc.read_memory(ns, "rounds")
    except AiosNoEntError:
        rounds = 0
    if rounds >= 3:
        await sc.log("info", "research complete")
        return True
    reply = await sc.generate(f"research step {rounds + 1}: gather data for the report")
    await sc.call_tool("fs.write", {"path": f"note-{rounds + 1}.md", "content": reply["text"]})
    await sc.write_memory(ns, "rounds", rounds + 1)
    await sc.log("info", f"wrote note-{rounds + 1}.md")
    return False


@agent(name="writer")
async def writer(sc) -> bool:
    """One LLM round per turn; writes a section draft; done after 2 rounds."""
    ns = f"agent:{(await sc.get_pid())['pid']}"
    try:
        rounds = await sc.read_memory(ns, "rounds")
    except AiosNoEntError:
        rounds = 0
    if rounds >= 2:
        await sc.log("info", "report written")
        return True
    reply = await sc.generate(f"writing step {rounds + 1}: draft the report section")
    await sc.call_tool("fs.write", {"path": f"section-{rounds + 1}.md", "content": reply["text"]})
    await sc.write_memory(ns, "rounds", rounds + 1)
    return False
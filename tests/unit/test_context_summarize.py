"""Unit tests: context summarization preserves pinned + recent turns.

Invariant (docs/04-memory.md §2): a summarized window must preserve ALL
pinned content and the most recent ``keep_recent_messages`` turns verbatim.
"""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.llm_core import MockLLM
from aios_sdk.session import AgentSession

from ..conftest import _base_spec

_SUMMARY_SCRIPT = {"Summarize the conversation history": "condensed"}


def _script_llm(script: dict | None = None) -> MockLLM:
    """A script-mode MockLLM so summary text is short and deterministic."""
    return MockLLM(script=script or _SUMMARY_SCRIPT, default="ok", mode="script")


@pytest.mark.asyncio
async def test_summarize_preserves_pinned_and_recent_turns(tmp_path) -> None:
    k = Kernel(data_root=str(tmp_path), llm_backend=_script_llm())
    pid = await k.spawn_agent(_base_spec(name="summ"))
    sc = AgentSession(k, pid)
    try:
        await sc.append_context("user", "the pinned task brief", pinned=True)
        for i in range(1, 9):
            await sc.append_context("user", f"turn detail number {i}")

        res = await sc.summarize_context()
        assert res["summary"] == "condensed"
        assert res["tokens_saved"] > 0
        assert res["kept_recent"] == 4

        contents = [m["content"] for m in await sc.read_context()]
        assert "the pinned task brief" in contents   # pinned survives
        assert "turn detail number 5" in contents     # recent kept verbatim
        assert "turn detail number 8" in contents     # most recent kept
        assert "turn detail number 1" not in contents  # old turns summarized away
        assert "condensed" in contents                # summary message replaced them
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_summarize_noop_when_nothing_to_collapse(tmp_path) -> None:
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    pid = await k.spawn_agent(_base_spec(name="summ2"))
    sc = AgentSession(k, pid)
    try:
        await sc.append_context("user", "only a few turns")
        res = await sc.summarize_context()
        assert res["summary"] is None
        assert res["tokens_saved"] == 0
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_generate_auto_summarizes_over_budget(tmp_path) -> None:
    k = Kernel(
        data_root=str(tmp_path),
        llm_backend=_script_llm(script={"Summarize the conversation history": "condensed", "hello": "hi"}),
    )
    spec = _base_spec(name="auto", context={"context_token_budget": 60})
    pid = await k.spawn_agent(spec)
    sc = AgentSession(k, pid)
    try:
        for _ in range(30):
            await sc.append_context("user", "x" * 20)
        await sc.generate("hello")
        contents = [m["content"] for m in await sc.read_context()]
        assert "condensed" in contents  # eviction fired before the LLM turn
        assert "hi" in contents         # the actual reply (script mode) is there
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_pinned_survives_even_under_aggressive_target(tmp_path) -> None:
    k = Kernel(
        data_root=str(tmp_path),
        llm_backend=_script_llm(script={"Summarize the conversation history": "tiny"}),
    )
    spec = _base_spec(name="summ3", context={"keep_recent_messages": 2})
    pid = await k.spawn_agent(spec)
    sc = AgentSession(k, pid)
    try:
        await sc.append_context("user", "pinned fact", pinned=True)
        for i in range(1, 6):
            await sc.append_context("user", f"turn {i}")
        await sc.summarize_context(target_tokens=1)  # aggressively small target
        contents = [m["content"] for m in await sc.read_context()]
        assert "pinned fact" in contents                       # pinned untouched
        assert "turn 5" in contents and "turn 4" in contents   # keep_recent floor holds
        assert "tiny" in contents
    finally:
        await k.shutdown()
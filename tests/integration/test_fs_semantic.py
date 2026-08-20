"""Integration tests: semantic FS syscalls + the fs tools share one index."""

from __future__ import annotations

import pytest

from aios_kernel import Kernel

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_fs_write_via_syscall_then_search(kernel: Kernel, session) -> None:
    sc = await session()
    await sc.fs_write("reports/q3.md", "Q3 revenue analysis: 18% growth to $2.4M")
    hits = await sc.fs_search("Q3 revenue analysis growth")
    assert hits and hits[0]["path"] == "reports/q3.md"


@pytest.mark.asyncio
async def test_tool_write_and_syscall_search_share_index(kernel: Kernel, session) -> None:
    """The fs.write tool and the fs_search syscall reuse one semantic index."""
    sc = await session()
    result = await sc.call_tool(
        "fs.write", {"path": "board-notes.md", "content": "board meeting minutes: approve Q3 budget"}
    )
    assert result["result"]["artifact_id"]
    hits = await sc.fs_search("board meeting minutes budget")
    assert hits and hits[0]["path"] == "board-notes.md"


@pytest.mark.asyncio
async def test_store_artifact_writes_and_is_searchable(kernel: Kernel, session) -> None:
    sc = await session()
    artifact_id = await sc.store_artifact("draft.md", "draft of the Q3 revenue report", mime="text/markdown")
    assert artifact_id
    hits = await sc.fs_search("Q3 revenue report draft")
    assert hits and hits[0]["artifact_id"] == artifact_id


@pytest.mark.asyncio
async def test_artifacts_are_isolated_between_agents(kernel: Kernel, session) -> None:
    sc1 = await session(_base_spec(name="fs-owner"))
    sc2 = await session(_base_spec(name="fs-stranger"))
    await sc1.fs_write("secret.md", "classified launch plan for the new product")
    hits = await sc2.fs_search("classified launch plan")
    assert hits == []
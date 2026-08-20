"""Unit tests: semantic FS — virtual paths, artifacts, and fs_search ranking."""

from __future__ import annotations

import pytest

from aios_kernel import Kernel
from aios_sdk.errors import AiosInvalError, AiosNoEntError
from aios_sdk.session import AgentSession

from ..conftest import _base_spec


async def _session(kernel: Kernel, spec: dict | None = None):
    pid = await kernel.spawn_agent(spec or _base_spec(name="fs"))
    return AgentSession(kernel, pid)


@pytest.mark.asyncio
async def test_fs_write_and_read_roundtrip_via_syscalls(kernel, session) -> None:
    sc = await session()
    meta = await sc.fs_write("notes/report.md", "Q3 revenue analysis: grew 18%", mime="text/markdown")
    assert meta["path"] == "notes/report.md"
    assert meta["bytes"] == len("Q3 revenue analysis: grew 18%")
    assert meta["artifact_id"]
    out = await sc.fs_read("notes/report.md")
    assert out["content"] == "Q3 revenue analysis: grew 18%"
    assert out["mime"] == "text/markdown"


@pytest.mark.asyncio
async def test_store_artifact_returns_unique_ids(kernel, session) -> None:
    sc = await session()
    a1 = await sc.store_artifact("a.txt", "hello world alpha")
    a2 = await sc.store_artifact("b.txt", "hello world beta")
    assert a1 != a2
    assert len(a1) > 0 and len(a2) > 0


@pytest.mark.asyncio
async def test_fs_search_ranks_related_artifact_first(kernel, session) -> None:
    sc = await session()
    await sc.fs_write(
        "q3-report.md",
        "Q3 revenue analysis: revenue grew 18% to $2.4M driven by enterprise sales.",
    )
    await sc.fs_write("todo.md", "grocery list: milk, eggs, bread, butter")
    hits = await sc.fs_search("Q3 revenue growth analysis", top_k=2)
    assert len(hits) == 2
    assert hits[0]["path"] == "q3-report.md"
    assert hits[0]["score"] > hits[1]["score"]
    assert "snippet" in hits[0]


@pytest.mark.asyncio
async def test_fs_search_exact_query_ranks_file_first(kernel, session) -> None:
    sc = await session()
    await sc.fs_write("log.txt", "system boot ok")
    await sc.fs_write("notes.md", "standup notes for the platform team")
    hits = await sc.fs_search("system boot ok")
    assert hits and hits[0]["path"] == "log.txt"
    assert hits[0]["score"] > 0.5  # near-verbatim query ranks highest


@pytest.mark.asyncio
async def test_fs_path_traversal_rejected(kernel, session) -> None:
    sc = await session()
    for bad in ("/etc/passwd", "~/x", "..", "../escape", "a/../../b"):
        with pytest.raises(AiosInvalError):
            await sc.fs_read(bad)
        with pytest.raises(AiosInvalError):
            await sc.fs_write(bad, "x")


@pytest.mark.asyncio
async def test_fs_read_missing_file_returns_e_noent(kernel, session) -> None:
    sc = await session()
    with pytest.raises(AiosNoEntError):
        await sc.fs_read("nope.txt")
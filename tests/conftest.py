"""Shared pytest fixtures for the AI OS test suite."""

from __future__ import annotations

import json
import os
import sys

import pytest

# Ensure the repo root is importable regardless of cwd.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from aios_kernel import Kernel  # noqa: E402
from aios_kernel.modules.llm_core import MockLLM  # noqa: E402


def _base_spec(**overrides) -> dict:
    spec = {
        "name": "test-agent",
        "version": "1",
        "description": "pytest fixture agent",
        "group_id": "test",
        "priority": 0,
        "llm": {"model": "mock", "system": "You are a test agent.", "temperature": 0.0},
        "budgets": {"max_turns": 10, "max_tool_calls": 10},
        "capabilities": {"tools": [{"name": "fs.read"}, {"name": "fs.write"}]},
    }
    spec.update(overrides)
    return spec


@pytest.fixture
def spec() -> dict:
    return _base_spec()


@pytest.fixture
async def kernel(tmp_path, monkeypatch):
    """A fresh Kernel with per-test data dirs and a disabled LLM backend."""
    monkeypatch.setenv("AIOS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    k = Kernel(
        audit_path=str(tmp_path / "audit.jsonl"),
        workspace_root=str(tmp_path / "workspaces"),
        llm_backend=MockLLM(mode="echo"),
    )
    yield k
    await k.shutdown()


@pytest.fixture
def audit_lines(kernel):
    """All audit records for the current test, in order."""

    def _collect() -> list[dict]:
        path = kernel.audit.path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    return _collect


@pytest.fixture
def session(kernel, spec):
    """A live AgentSession bound to a spawned agent (no runner)."""

    async def _make(overrides=None) -> "AgentSession":
        from aios_sdk.session import AgentSession

        spec_dict = dict(spec)
        if overrides:
            spec_dict.update(overrides)
        pid = await kernel.spawn_agent(spec_dict)
        return AgentSession(kernel, pid)

    return _make
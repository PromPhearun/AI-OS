"""Integration test: secrets scanner (docs/08-security.md §12 acceptance).

A secret resolved via get_env must never appear in audit logs, checkpoints,
the session manifest, or the agent's context — even when the agent writes it
into its own workspace (the workspace is the agent's sandbox, not a
kernel-protected surface).
"""

from __future__ import annotations

import json

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.llm_core import MockLLM

from ..conftest import _base_spec

SECRET = "sk-super-secret-123"

_SPEC = _base_spec(
    name="leaker",
    env={"allowed_keys": ["API_KEY"]},
    capabilities={
        "tools": [
            {"name": "fs.write"},
            {"name": "fs.read"},
        ]
    },
)


def _scan_texts(root) -> list[str]:
    """All text of kernel-owned files under ``root``, excluding the agent
    workspaces (the agent's own sandbox) and the vault file itself (the vault
    is *supposed* to hold secrets at rest)."""
    texts: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "workspaces" in path.parts or path.name == "credentials.json":
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


@pytest.mark.asyncio
async def test_secret_never_in_audit_checkpoints_or_context(tmp_path) -> None:
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        from aios_sdk.session import AgentSession

        k.vault.set("API_KEY", SECRET)
        pid = await k.spawn_agent(dict(_SPEC))
        sc = AgentSession(k, pid)

        # resolve the secret, derive only a non-secret marker for context,
        # write the value into the workspace, and checkpoint — every
        # kernel-protected surface must stay clean
        value = await sc.get_env("API_KEY")
        assert value == SECRET
        await sc.append_context("user", f"resolved key of length {len(value)}")
        await sc.call_tool("fs.write", {"path": "note.md", "content": f"key={value}"})
        await sc.checkpoint(label="with-secret")

        # 1. audit log records only hashes — never the raw value
        audit_text = open(k.audit.path, encoding="utf-8").read()
        assert SECRET not in audit_text

        # 2. checkpoints + session manifest + memory carry no secrets
        for text in _scan_texts(tmp_path):
            assert SECRET not in text, "secret leaked into kernel-owned file"

        # 3. agent context holds only the derived marker (no raw secret)
        ctx = [m["content"] for m in k.context.read(pid)]
        assert all(SECRET not in m for m in ctx)

        # 4. the get_env syscall itself is audited with a hash, not the value
        entries = json.loads("[" + audit_text.replace("\n", ",").rstrip(",") + "]")
        env_calls = [e for e in entries if e.get("syscall") == "get_env"]
        assert env_calls
    finally:
        await k.shutdown()
"""Unit tests: the secret vault (docs/08-security.md §5) — persistence,
restrictive file mode, and deny-by-default get_env."""

from __future__ import annotations

import os
import stat

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.llm_core import MockLLM
from aios_sdk.errors import AiosPermissionError

from ..conftest import _base_spec


@pytest.mark.asyncio
async def test_set_persists_credentials_file(tmp_path) -> None:
    k = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        k.vault.set("API_KEY", "sk-super-secret-123")
        creds = tmp_path / "credentials.json"
        assert creds.exists()
        content = creds.read_text(encoding="utf-8")
        assert "sk-super-secret-123" in content
        # restrictive permissions: only the kernel process owner can read it
        mode = stat.S_IMODE(os.stat(creds).st_mode)
        assert mode & 0o077 == 0
    finally:
        await k.shutdown()


@pytest.mark.asyncio
async def test_vault_reloads_across_restart(tmp_path) -> None:
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    k1.vault.set("DB_PASSWORD", "hunter2")
    await k1.shutdown()

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.vault.get("DB_PASSWORD") == "hunter2"
        assert k2.vault.keys() == ["DB_PASSWORD"]
    finally:
        await k2.shutdown()


@pytest.mark.asyncio
async def test_get_env_deny_by_default_and_granted_value(kernel, session) -> None:
    kernel.vault.set("GITHUB_TOKEN", "ghp_verysecret")
    sc = await session(_base_spec(name="vault", env={"allowed_keys": ["GITHUB_TOKEN"]}))
    assert await sc.get_env("GITHUB_TOKEN") == "ghp_verysecret"

    sc2 = await session(_base_spec(name="no-vault"))
    with pytest.raises(AiosPermissionError):
        await sc2.get_env("GITHUB_TOKEN")
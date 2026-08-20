"""Vault — the get_env backend.

Phase 1 keeps a plain in-memory map of *non-secret* configuration keys that
operators populate at boot. Real secret management (KMS-backed, encrypted at
rest) lands in Phase 3 per docs/08-security.md. No secrets are accepted or
logged here by design.
"""

from __future__ import annotations

from ..errors import AiosError, E_NOENT
from ..syscalls.registry import register


class Vault:
    def __init__(self, kernel):
        self.kernel = kernel
        self._env: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self._env[key] = value

    def get(self, key: str) -> str:
        try:
            return self._env[key]
        except KeyError:
            raise AiosError(E_NOENT, f"env key '{key}' is not set") from None

    def keys(self) -> list[str]:
        return sorted(self._env)


# ------------------------------------------------------------------ syscalls
@register("get_env")
async def _get_env(kernel, pid: int, args: dict) -> dict:
    return {"value": kernel.vault.get(args["key"])}
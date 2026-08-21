"""Vault — the secret store behind get_env (docs/08-security.md §5).

Phase 3: secrets live on disk under the kernel's data root
(``<root>/credentials.json``, mode 0600, atomically replaced on every write)
— the operator's vault in v1; KMS/secret-manager integration is a v2 target.
Values are referenced by key only; ``get_env(key)`` requires the key in the
agent's resolved ``env.allowed_keys`` (Access Control, deny by default).

Hard rules enforced here and in Access Control:
  * agents never hold values — they hold keys;
  * values never enter audit logs, checkpoints, or context (hashed only);
  * ``credentials.json`` is readable only by the kernel process owner.

Phase 5: ``credentials.json`` is sealed with AES-256-GCM at rest when the
kernel has a master key (``AIOS_MASTER_KEY`` or ``AIOS_ENCRYPT=1`` — see
``modules/crypto.py``); a wrong key or tampered file fails closed (empty vault).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..errors import AiosError, E_NOENT
from ..syscalls.registry import register

REDACTED_MARKER = "[REDACTED]"


class Vault:
    def __init__(self, kernel, root: str | None = None, *, cipher=None):
        self.kernel = kernel
        self._env: dict[str, str] = {}
        self._path = os.environ.get("AIOS_CREDENTIALS_PATH") or root
        self._cipher = cipher  # AES-256-GCM at-rest cipher or None (plaintext)
        if self._path:
            self._load()

    # ---------------------------------------------------------------- store
    def _load(self) -> None:
        try:
            raw = Path(self._path).read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            self._env = {}  # unreadable vault: fail closed, never crash mid-boot
            return
        if self._cipher is not None:
            try:
                raw = self._cipher.open(raw)
            except Exception:
                self._env = {}  # wrong key / tampered file: fail closed (no secrets)
                return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._env = {}  # corrupt vault: fail closed, never crash mid-boot
            return
        if isinstance(data, dict):
            self._env = {str(k): str(v) for k, v in data.items()}

    def _persist(self) -> None:
        if not self._path:
            return
        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._env, sort_keys=True, indent=2).encode("utf-8")
        if self._cipher is not None:
            payload = self._cipher.seal(payload)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".credentials-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ api
    def set(self, key: str, value: str) -> None:
        self._env[key] = str(value)
        self._persist()

    def get(self, key: str) -> str:
        try:
            return self._env[key]
        except KeyError:
            raise AiosError(E_NOENT, f"env key '{key}' is not set") from None

    def keys(self) -> list[str]:
        return sorted(self._env)

    def redact(self, text: str) -> str:
        """Replace every known secret value with a redaction marker.

        Applied at kernel-owned persistence boundaries (memory indexing,
        checkpoints) so a value resolved from the vault can never leak into
        audit logs, checkpoints, or context (docs/08-security.md §5 hard
        rules). Longest values are replaced first so a shorter secret nested
        inside a longer one leaves no fragment behind.
        """
        result = str(text)
        if not self._env:
            return result
        for value in sorted(self._env.values(), key=len, reverse=True):
            if value:
                result = result.replace(value, REDACTED_MARKER)
        return result


# ------------------------------------------------------------------ syscalls
@register("get_env")
async def _get_env(kernel, pid: int, args: dict) -> dict:
    return {"value": kernel.vault.get(args["key"])}
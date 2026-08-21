"""Authentication for the control plane — API keys (humans) + JWT (web desktop).

Per docs/10-ui.md §4: API keys for humans (``operator``/``standard`` roles) and
JWT for the web desktop; every control request is itself audited.

Key material is read from the environment::

    AIOS_API_KEYS="alice:secret-one:operator,bob:secret-two:standard"

Each entry is ``name:key:role``; the name is the audit principal. JWT tokens are
HS256-signed with ``AIOS_JWT_SECRET`` (a fresh random secret is generated for
dev/demo when the env var is unset — tokens then expire at restart). If no API
keys are configured at all, a dev operator key ``dev-key`` is enabled and the
server warns loudly on startup.
"""

from __future__ import annotations

import datetime as _dt
import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt as _jwt

ROLES = ("operator", "standard")
ROLE_RANK = {"standard": 0, "operator": 1}
JWT_ALGORITHM = "HS256"
DEV_API_KEY = "dev-key"


def _role_rank(role: str) -> int:
    return ROLE_RANK.get(role, -1)


@dataclass(frozen=True)
class ApiKey:
    """A configured control-plane key: ``name`` (audit principal), ``key``, ``role``."""

    name: str
    key: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {self.role!r}")
        # The built-in dev key (auth.py:DEV_API_KEY) is intentionally short;
        # operator-configured keys still require a real minimum length.
        if self.key != DEV_API_KEY and len(self.key) < 8:
            raise ValueError("api keys must be at least 8 characters long")
        if not self.name or any(c.isspace() for c in self.name):
            raise ValueError(f"invalid api key name {self.name!r}")


@dataclass(frozen=True)
class Principal:
    """The authenticated caller attached to every audited control request."""

    name: str
    role: str

    def is_operator(self) -> bool:
        return _role_rank(self.role) >= _role_rank("operator")


def parse_api_keys_env(value: str | None) -> list[ApiKey]:
    """Parse ``AIOS_API_KEYS`` (``name:key:role,...``) into ApiKey objects."""
    if not value:
        return []
    keys: list[ApiKey] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"bad AIOS_API_KEYS entry {chunk!r}: expected name:key:role"
            )
        keys.append(ApiKey(name=parts[0], key=parts[1], role=parts[2]))
    return keys


class AuthManager:
    """Holds API keys, issues/verifies JWTs, and exposes the dev-key fallback."""

    def __init__(
        self,
        api_keys: list[ApiKey] | None = None,
        *,
        jwt_secret: str | None = None,
        jwt_ttl_s: int = 3600,
    ) -> None:
        self._keys: list[ApiKey] = list(api_keys or [])
        self._jwt_secret = jwt_secret or os.environ.get("AIOS_JWT_SECRET")
        self._generated_secret = False
        self._jwt_ttl_s = int(jwt_ttl_s)
        self._dev_enabled = False
        if self._jwt_secret is None:
            self._jwt_secret = secrets.token_urlsafe(48)
            self._generated_secret = True
        if not self._keys:
            # No keys configured anywhere: enable the documented dev key.
            self._keys.append(ApiKey(name="dev", key=DEV_API_KEY, role="operator"))
            self._dev_enabled = True

    @property
    def dev_enabled(self) -> bool:
        return self._dev_enabled

    @property
    def generated_secret(self) -> bool:
        return self._generated_secret

    def authenticate(self, candidate: str) -> Principal | None:
        """Constant-time API-key lookup; None when no key matches."""
        for key in self._keys:
            if hmac.compare_digest(key.key, candidate):
                return Principal(name=key.name, role=key.role)
        return None

    def issue_token(self, principal: Principal, *, now: float | None = None) -> str:
        now = now or time.time()
        payload = {
            "sub": principal.name,
            "role": principal.role,
            "iss": "aios-control",
            "iat": int(now),
            "exp": int(now + self._jwt_ttl_s),
            "jti": uuid.uuid4().hex,
        }
        return _jwt.encode(payload, self._jwt_secret, algorithm=JWT_ALGORITHM)

    def verify_token(self, token: str) -> Principal:
        """Decode + validate a JWT; raises ValueError on any failure."""
        try:
            payload = _jwt.decode(
                token, self._jwt_secret, algorithms=[JWT_ALGORITHM], issuer="aios-control"
            )
        except _jwt.PyJWTError as exc:
            raise ValueError(f"invalid token: {exc}") from exc
        role = str(payload.get("role", "standard"))
        if role not in ROLES:
            raise ValueError(f"token carries unknown role {role!r}")
        return Principal(name=str(payload.get("sub", "unknown")), role=role)


__all__ = [
    "ROLES",
    "DEV_API_KEY",
    "ApiKey",
    "Principal",
    "AuthManager",
    "parse_api_keys_env",
]
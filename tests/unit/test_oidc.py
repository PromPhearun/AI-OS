"""Unit tests for the OIDC + PKCE auth module (Slice 5.3).

Covers the RFC 7636 primitives, the single-use TTL session store (with a fake
monotonic clock), ID-token verification against a live RSA key (including the
fail-closed rejection of ``alg=none``, ``HS*``, tampered signatures, and claim
mismatches), role mapping, and config parsing from the environment.
"""

from __future__ import annotations

import json
import time

import pytest

from aios_api.auth import Principal
from aios_api.oidc import (
    GRANT_COOKIE,
    OidcClient,
    OidcConfig,
    OidcError,
    OidcSessionStore,
    generate_code_verifier,
    s256_challenge,
)

from tests.fixtures.oidc_provider import b64u, make_rsa_key, public_jwk, sign_id_token

ISSUER = "https://idp.example.test"
CLIENT_ID = "aios-web"


# --------------------------------------------------------------------- PKCE
class TestPkce:
    def test_verifier_is_43_128_urlsafe_chars(self) -> None:
        for _ in range(20):
            v = generate_code_verifier()
            assert 43 <= len(v) <= 128
            assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in v)

    def test_verifier_is_high_entropy(self) -> None:
        vs = {generate_code_verifier() for _ in range(64)}
        assert len(vs) == 64  # no collisions

    def test_s256_challenge_is_rfc7636(self) -> None:
        import base64
        import hashlib

        v = generate_code_verifier()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(v.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert s256_challenge(v) == expected

    def test_challenge_matches_provider_side(self) -> None:
        v = generate_code_verifier()
        assert s256_challenge(v) == s256_challenge(v)
        assert s256_challenge(v) != s256_challenge(v + "x")


# ------------------------------------------------------------- session store
class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class TestSessionStore:
    def test_txn_single_use(self) -> None:
        store = OidcSessionStore()
        store.create_txn("s1", "verifier-1", "nonce-1")
        assert store.consume_txn("s1") == ("verifier-1", "nonce-1")
        assert store.consume_txn("s1") is None  # replayed state fails

    def test_txn_unknown(self) -> None:
        assert OidcSessionStore().consume_txn("nope") is None

    def test_txn_expiry(self) -> None:
        clock = FakeClock()
        store = OidcSessionStore(txn_ttl_s=60.0, now_fn=clock)
        store.create_txn("s1", "v", "n")
        clock.now += 59.0
        assert store.consume_txn("s1") == ("v", "n")
        store.create_txn("s2", "v2", "n2")
        clock.now += 61.0
        assert store.consume_txn("s2") is None  # expired

    def test_grant_single_use(self) -> None:
        store = OidcSessionStore()
        token = store.issue_grant(Principal(name="oidc:alice", role="operator"))
        p = store.consume_grant(token)
        assert p is not None
        assert p.name == "oidc:alice"
        assert p.role == "operator"
        assert store.consume_grant(token) is None  # replay fails

    def test_grant_expiry(self) -> None:
        clock = FakeClock()
        store = OidcSessionStore(grant_ttl_s=120.0, now_fn=clock)
        token = store.issue_grant(Principal(name="oidc:alice", role="operator"))
        clock.now += 121.0
        assert store.consume_grant(token) is None

    def test_grant_garbage(self) -> None:
        assert OidcSessionStore().consume_grant("not-a-real-grant") is None

    # ------------------------------------------------------------- id-token verify
class _StubJwksClient(OidcClient):
    """OidcClient with a pre-loaded JWKS (no discovery/HTTP needed)."""

    def __init__(self, config: OidcConfig, jwks: list[dict]) -> None:
        super().__init__(config)
        self._jwks = jwks
        self._jwks_at = time.monotonic() + 1e9  # never refetch


def _client(jwks=None, **config_kwargs) -> tuple[_StubJwksClient, object]:
    key = make_rsa_key()
    jwks = jwks if jwks is not None else [public_jwk(key, "k1")]
    cfg = OidcConfig(issuer=ISSUER, client_id=CLIENT_ID, **config_kwargs)
    return _StubJwksClient(cfg, jwks), key


def _valid_token(key, nonce="nonce-1", **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u1", "exp": now + 300, "iat": now}
    claims.update(overrides)
    return sign_id_token(
        key, kid="k1", iss=ISSUER, aud=CLIENT_ID, sub="u1", nonce=nonce, exp=now + 300, claims=claims
    )


@pytest.mark.asyncio
async def test_verify_valid_rs256() -> None:
    client, key = _client()
    payload = await client.verify_id_token(_valid_token(key), nonce="nonce-1")
    assert payload["sub"] == "u1"
    assert payload["iss"] == ISSUER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override,error_code",
    [
        ({"iss": "https://evil.example"}, "E_PERM"),
        ({"aud": "someone-else"}, "E_PERM"),
        ({"exp": int(time.time()) - 3600}, "E_PERM"),
        ({"iat": int(time.time()) + 3600}, "E_PERM"),
        ({"nbf": int(time.time()) + 3600}, "E_PERM"),
    ],
)
async def test_verify_claim_mismatches_fail_closed(override, error_code) -> None:
    client, key = _client()
    token = _valid_token(key, **override)
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(token, nonce="nonce-1")
    assert ei.value.code == error_code


@pytest.mark.asyncio
async def test_verify_wrong_nonce() -> None:
    client, key = _client()
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(_valid_token(key, nonce="server-nonce"), nonce="other")
    assert ei.value.code == "E_PERM"


@pytest.mark.asyncio
async def test_verify_tampered_signature() -> None:
    client, key = _client()
    token = _valid_token(key)
    parts = token.split(".")
    forged = f"{parts[0]}.{parts[1]}.{b64u(bytes(256))}"
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(forged, nonce="nonce-1")
    assert ei.value.code == "E_PERM"


@pytest.mark.asyncio
async def test_verify_alg_none_rejected() -> None:
    client, _ = _client()
    now = int(time.time())
    header = b64u(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64u(
        json.dumps(
            {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u1", "exp": now + 300, "iat": now}
        ).encode()
    )
    token = f"{header}.{payload}."
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(token, nonce=None)
    assert ei.value.code == "E_INVAL"


@pytest.mark.asyncio
async def test_verify_hs256_rejected() -> None:
    client, key = _client()
    token = _valid_token(key)
    parts = token.split(".")
    forged_header = b64u(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    forged = f"{forged_header}.{parts[1]}.{parts[2]}"
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(forged, nonce="nonce-1")
    assert ei.value.code == "E_INVAL"


@pytest.mark.asyncio
async def test_verify_unknown_kid_rejected() -> None:
    client, key = _client()  # JWKS has kid "k1" only
    now = int(time.time())
    token = sign_id_token(
        key, kid="ghost-key", iss=ISSUER, aud=CLIENT_ID, sub="u1", exp=now + 300
    )
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(token, nonce=None)
    assert ei.value.code == "E_INVAL"


@pytest.mark.asyncio
async def test_verify_wrong_key_rejected() -> None:
    client, _ = _client()
    other_key = make_rsa_key()  # different key than the JWKS
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token(_valid_token(other_key), nonce="nonce-1")
    assert ei.value.code == "E_PERM"


@pytest.mark.asyncio
async def test_verify_malformed_token() -> None:
    client, _ = _client()
    with pytest.raises(OidcError) as ei:
        await client.verify_id_token("not.a.jwt", nonce=None)
    assert ei.value.code == "E_INVAL"


# ------------------------------------------------------------- role mapping
class TestRoleMapping:
    def _config(self, **kwargs) -> OidcConfig:
        return OidcConfig(issuer=ISSUER, client_id=CLIENT_ID, **kwargs)

    def test_default_no_mapping_fails_closed_to_standard(self) -> None:
        """With no role mapping configured, all OIDC users get standard (fail-closed)."""
        client = OidcClient(self._config())
        p = client.resolve_principal({"sub": "u1"})
        assert p.role == "standard"

    def test_admin_email_match(self) -> None:
        client = OidcClient(self._config(admin_emails=frozenset({"alice@example.com"})))
        p = client.resolve_principal({"sub": "u1", "email": "alice@example.com", "email_verified": True})
        assert p.role == "operator"

    def test_admin_email_non_match(self) -> None:
        client = OidcClient(self._config(admin_emails=frozenset({"alice@example.com"})))
        p = client.resolve_principal({"sub": "u1", "email": "mallory@example.com", "email_verified": True})
        assert p.role == "standard"

    def test_unverified_email_never_operator(self) -> None:
        client = OidcClient(self._config(admin_emails=frozenset({"alice@example.com"})))
        p = client.resolve_principal({"sub": "u1", "email": "alice@example.com", "email_verified": False})
        assert p.role == "standard"

    def test_operator_values_list_match(self) -> None:
        client = OidcClient(self._config(operator_values=frozenset({"aios-ops"})))
        p = client.resolve_principal({"sub": "u1", "groups": ["devs", "aios-ops"]})
        assert p.role == "operator"

    def test_operator_values_string_match(self) -> None:
        client = OidcClient(self._config(operator_values=frozenset({"aios-ops"})))
        p = client.resolve_principal({"sub": "u1", "groups": "aios-ops"})
        assert p.role == "operator"

    def test_operator_values_no_match(self) -> None:
        client = OidcClient(self._config(operator_values=frozenset({"aios-ops"})))
        p = client.resolve_principal({"sub": "u1", "groups": ["devs"]})
        assert p.role == "standard"

    def test_name_prefers_username_then_email_then_sub(self) -> None:
        client = OidcClient(self._config())
        assert client.resolve_principal({"sub": "s", "preferred_username": "bob"}).name == "oidc:bob"
        assert client.resolve_principal({"sub": "s", "email": "bob@example.com"}).name == "oidc:bob@example.com"
        assert client.resolve_principal({"sub": "s"}).name == "oidc:s"

    def test_userinfo_merges_over_id_token(self) -> None:
        client = OidcClient(self._config(admin_emails=frozenset({"alice@example.com"})))
        p = client.resolve_principal({"sub": "u1"}, {"email": "alice@example.com", "email_verified": True})
        assert p.role == "operator"


# ------------------------------------------------------------------- config
class TestOidcConfig:
    def test_from_env_disabled(self, monkeypatch) -> None:
        monkeypatch.delenv("AIOS_OIDC_ISSUER", raising=False)
        monkeypatch.delenv("AIOS_OIDC_CLIENT_ID", raising=False)
        cfg = OidcConfig.from_env()
        assert not cfg.enabled

    def test_from_env_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("AIOS_OIDC_ISSUER", "https://idp.example.test")
        monkeypatch.setenv("AIOS_OIDC_CLIENT_ID", "web")
        monkeypatch.setenv("AIOS_OIDC_ADMIN_EMAILS", "a@x.com, b@x.com")
        monkeypatch.setenv("AIOS_OIDC_OPERATOR_VALUES", "aios-ops, sre")
        cfg = OidcConfig.from_env()
        assert cfg.enabled
        assert cfg.admin_emails == frozenset({"a@x.com", "b@x.com"})
        assert cfg.operator_values == frozenset({"aios-ops", "sre"})
        assert cfg.scopes == ("openid", "profile", "email")

    def test_from_env_requires_both(self, monkeypatch) -> None:
        monkeypatch.setenv("AIOS_OIDC_ISSUER", "https://idp.example.test")
        monkeypatch.delenv("AIOS_OIDC_CLIENT_ID", raising=False)
        with pytest.raises(ValueError):
            OidcConfig.from_env()

    def test_rejects_non_http_issuer(self) -> None:
        with pytest.raises(ValueError):
            OidcConfig(issuer="not-a-url", client_id="web")

    def test_rejects_absolute_post_login(self) -> None:
        with pytest.raises(ValueError):
            OidcConfig(issuer=ISSUER, client_id="web", post_login_path="https://evil.example/")


def test_grant_cookie_constant() -> None:
    assert GRANT_COOKIE == "aios_oidc_grant"
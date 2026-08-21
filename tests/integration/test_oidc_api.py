"""Integration tests: the OIDC + PKCE human-auth flow end to end (Slice 5.3).

Drives the control plane through ``httpx.ASGITransport`` (same-loop, no
threading) with an injected :class:`OidcClient` whose outbound provider calls
hit a fake in-process OIDC provider (``tests.fixtures.oidc_provider``):

* status / authorize / callback / session endpoints;
* PKCE: the provider verifies the ``code_verifier`` against the challenge it
  saw at authorize time (``FakeOidcProvider.code_exchanges``);
* ID-token signature + nonce verification against the fake provider's JWKS;
* one-time HttpOnly grant cookie, single-use on replay, path-scoped;
* deny-by-default role mapping (admin emails, groups) incl. the userinfo
  fallback when the ID token omits the email;
* no code/verifier/grant ever reaches the audit log.
"""

from __future__ import annotations

import http.cookies
import urllib.parse

import httpx
import pytest

from aios_api import create_app
from aios_api.oidc import GRANT_COOKIE, OidcClient, OidcConfig
from tests.fixtures.oidc_provider import build_fake_provider

ISSUER = "https://idp.test"
CLIENT_ID = "aios-web"
REDIRECT_URI = "http://testserver/v1/auth/oidc/callback"


def _config(**kwargs) -> OidcConfig:
    base = dict(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
    )
    base.update(kwargs)
    return OidcConfig(**base)


@pytest.fixture
async def oidc(kernel, monkeypatch):
    """A control-plane app wired to a fake OIDC provider (no env config)."""
    monkeypatch.setenv("AIOS_JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
    fake = build_fake_provider(issuer=ISSUER, client_id=CLIENT_ID)
    transport = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app))
    oidc_client = OidcClient(
        _config(admin_emails=frozenset({"alice@example.com"})), http=transport
    )
    app = create_app(kernel, oidc_client=oidc_client)
    return fake, oidc_client, app


@pytest.fixture
async def ac(oidc):
    """An httpx client bound to the control-plane app (same-loop ASGI)."""
    fake, oidc_client, app = oidc
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        yield c, fake, oidc_client


async def _begin_flow(c: httpx.AsyncClient, fake) -> tuple[str, str]:
    """Start the PKCE flow and simulate the browser hop through the IdP."""
    r = await c.post("/v1/auth/oidc/authorize")
    assert r.status_code == 200, r.text
    authorize_url = r.json()["authorize_url"]
    assert authorize_url.startswith(f"{ISSUER}/authorize")

    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(authorize_url).query))
    assert qs["response_type"] == "code"
    assert qs["client_id"] == CLIENT_ID
    assert qs["code_challenge_method"] == "S256"
    assert qs["scope"] == "openid profile email"
    assert qs["state"] and qs["nonce"] and qs["code_challenge"]

    # The browser GETs the IdP URL; the IdP redirects back with code+state.
    prov = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app))
    redir = await prov.get(authorize_url, follow_redirects=False)
    assert redir.status_code == 302
    location = redir.headers["location"]
    assert location.startswith(REDIRECT_URI)
    cb_params = dict(
        urllib.parse.parse_qsl(urllib.parse.urlparse(location).query)
    )
    assert cb_params["state"] == qs["state"]
    return cb_params["code"], cb_params["state"]


async def _callback_and_grant(c: httpx.AsyncClient, code: str, state: str) -> str:
    """Hit the callback; return the one-time grant cookie value."""
    cb = await c.get(
        "/v1/auth/oidc/callback", params={"code": code, "state": state}
    )
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/"
    jar = http.cookies.SimpleCookie()
    jar.load(cb.headers["set-cookie"])
    assert GRANT_COOKIE in jar
    cookie = jar[GRANT_COOKIE]
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "lax"
    assert cookie["path"] == "/v1/auth/oidc"
    assert cookie["max-age"] == "120"
    return cookie.value


# ------------------------------------------------------------------- status
async def test_oidc_status_enabled(ac) -> None:
    c, _, _ = ac
    r = await c.get("/v1/auth/oidc")
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "issuer": ISSUER}


async def test_oidc_status_disabled_without_config(kernel, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
    app = create_app(kernel)  # no OIDC configured
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        assert (await c.get("/v1/auth/oidc")).json() == {"enabled": False}
        r = await c.post("/v1/auth/oidc/authorize")
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "E_NOENT"
        r2 = await c.post("/v1/auth/oidc/session")
        assert r2.status_code == 401


# ------------------------------------------------------------ happy path
async def test_full_pkce_login_sets_grant_then_jwt(ac) -> None:
    c, fake, _ = ac
    code, state = await _begin_flow(c, fake)

    grant = await _callback_and_grant(c, code, state)

    # The provider saw the correct PKCE verifier for the issued code.
    assert fake.code_exchanges and fake.code_exchanges[-1]["challenge_ok"] is True

    # Exchange the one-time grant for a normal aios JWT.
    c.cookies.set(GRANT_COOKIE, grant, path="/v1/auth/oidc")
    ex = await c.post("/v1/auth/oidc/session")
    assert ex.status_code == 200, ex.text
    body = ex.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "operator"
    assert body["name"] == "oidc:alice"
    token = body["access_token"]

    # The JWT authenticates control-plane calls like any other session.
    r = await c.get("/v1/scheduler", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "queues" in r.json()

    # Grant is single-use: replaying the session exchange is refused.
    r2 = await c.post("/v1/auth/oidc/session")
    assert r2.status_code == 401
    assert r2.json()["detail"]["error"]["code"] == "E_PERM"


async def test_callback_state_replay_rejected(ac) -> None:
    c, fake, _ = ac
    code, state = await _begin_flow(c, fake)

    cb1 = await c.get(
        "/v1/auth/oidc/callback", params={"code": code, "state": state}
    )
    assert cb1.status_code == 302

    # Same state replayed → the transaction was consumed, so it fails closed.
    cb2 = await c.get(
        "/v1/auth/oidc/callback", params={"code": code, "state": state}
    )
    assert cb2.status_code == 400
    assert cb2.json()["detail"]["error"]["code"] == "E_INVAL"


async def test_callback_with_unknown_state_rejected(ac) -> None:
    c, _, _ = ac
    r = await c.get(
        "/v1/auth/oidc/callback",
        params={"code": "some-code", "state": "no-such-txn"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "E_INVAL"


async def test_session_without_grant_cookie_rejected(ac) -> None:
    c, _, _ = ac
    r = await c.post("/v1/auth/oidc/session")
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "E_PERM"


async def _app_with_config(kernel, monkeypatch, **config_kwargs) -> tuple:
    """A control-plane app with a fresh fake provider + config combination."""
    monkeypatch.setenv("AIOS_JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
    fake = build_fake_provider(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        id_token_email=config_kwargs.pop("id_token_email", True),
        groups=config_kwargs.pop("groups", None),
    )
    client2 = OidcClient(
        _config(**config_kwargs),
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)),
    )
    app2 = create_app(kernel, oidc_client=client2)
    c2 = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2),
        base_url="http://testserver",
        follow_redirects=False,
    )
    return c2, fake


async def test_role_standard_when_email_not_allowed(kernel, monkeypatch) -> None:
    c2, fake = await _app_with_config(
        kernel, monkeypatch, admin_emails=frozenset({"other@example.com"})
    )
    async with c2:
        code, state = await _begin_flow(c2, fake)
        grant = await _callback_and_grant(c2, code, state)
        c2.cookies.set(GRANT_COOKIE, grant, path="/v1/auth/oidc")
        ex = await c2.post("/v1/auth/oidc/session")
        assert ex.status_code == 200
        assert ex.json()["role"] == "standard"


async def test_group_mapping_via_operator_values(kernel, monkeypatch) -> None:
    c2, fake = await _app_with_config(
        kernel,
        monkeypatch,
        groups=["devs", "aios-ops"],
        operator_values=frozenset({"aios-ops"}),
    )
    async with c2:
        code, state = await _begin_flow(c2, fake)
        grant = await _callback_and_grant(c2, code, state)
        c2.cookies.set(GRANT_COOKIE, grant, path="/v1/auth/oidc")
        ex = await c2.post("/v1/auth/oidc/session")
        assert ex.status_code == 200
        assert ex.json()["role"] == "operator"


async def test_userinfo_fallback_when_id_token_has_no_email(kernel, monkeypatch) -> None:
    c2, fake = await _app_with_config(
        kernel,
        monkeypatch,
        id_token_email=False,
        admin_emails=frozenset({"alice@example.com"}),
    )
    async with c2:
        code, state = await _begin_flow(c2, fake)
        grant = await _callback_and_grant(c2, code, state)
        c2.cookies.set(GRANT_COOKIE, grant, path="/v1/auth/oidc")
        ex = await c2.post("/v1/auth/oidc/session")
        assert ex.status_code == 200
        # Email arrived via userinfo, so the admin allowlist matched.
        assert ex.json()["role"] == "operator"
        assert ex.json()["name"] == "oidc:alice"


async def test_secrets_never_reach_audit_log(ac, oidc) -> None:
    c, fake, _ = ac
    _, _, app = oidc
    code, state = await _begin_flow(c, fake)
    grant = await _callback_and_grant(c, code, state)
    c.cookies.set(GRANT_COOKIE, grant, path="/v1/auth/oidc")
    assert (await c.post("/v1/auth/oidc/session")).status_code == 200

    entries = app.state.kernel.audit.read(limit=2000)
    blob = "\n".join(
        f"{e.get('method', '')} {e.get('path', '')} {e.get('status', '')} {e.get('actor', '')}"
        for e in entries
    )
    assert "/v1/auth/oidc/callback" in blob
    assert "/v1/auth/oidc/session" in blob
    # No code, verifier, grant token, or id_token material anywhere.
    assert code not in blob
    assert grant not in blob
    assert "verifier" not in blob.lower()
    assert "id_token" not in blob.lower()
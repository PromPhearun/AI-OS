"""Shared OIDC test fixtures: RSA key helpers, ID-token signing, and a fake
OIDC provider (FastAPI app) for the PKCE integration tests.

The fake provider implements just enough of the authorization-code + PKCE
flow to exercise the control plane's ``aios_api.oidc`` client:

* ``/.well-known/openid-configuration`` + ``/jwks`` (RS256, one key);
* ``/authorize`` — issues a code bound to the presented ``code_challenge``;
* ``/token`` — validates the ``code_verifier`` against the stored challenge
  and returns a signed ID token (nonce-bound) plus a bearer access token;
* ``/userinfo`` — returns the operator's claims (optionally the email that
  the ID token omits, to exercise the userinfo fallback path).

``verify_id_token`` correctly rejects the unsigned/HS256/tampered variants the
unit tests build directly with the helpers below.
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.parse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aios_api.oidc import s256_challenge

# Imported at module level (not inside the provider's _build_app) so the nested
# route handlers' ``request: Request`` annotations resolve via module globals.
from fastapi import Request  # noqa: E402


def b64u(data: bytes) -> str:
    """Base64url without padding (JWT segment encoding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    """Public RSA JWK for a signing key."""
    pub = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
        "e": b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")),
    }


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    """Public RSA JWK for a signing key."""
    pub = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
        "e": b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")),
    }


def sign_id_token(
    key: rsa.RSAPrivateKey,
    *,
    kid: str,
    iss: str,
    aud: str,
    sub: str,
    nonce: str | None = None,
    exp: int,
    iat: int | None = None,
    alg: str = "RS256",
    claims: dict | None = None,
) -> str:
    """Sign a JWT ID token with the given RSA key (RS256 by default)."""
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "exp": exp,
        "iat": iat if iat is not None else exp - 300,
        "auth_time": exp - 300,
    }
    if nonce is not None:
        payload["nonce"] = nonce
    if claims:
        payload.update(claims)
    h = b64u(json.dumps(header, separators=(",", ":")).encode())
    p = b64u(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{b64u(sig)}"


class FakeOidcProvider:
    """A minimal OIDC provider bound to an ``httpx.ASGITransport`` target.

    ``id_token_email`` controls whether the ID token carries ``email`` /
    ``email_verified`` (when False the claims only arrive through userinfo).
    ``issuer`` must equal the client's configured issuer so discovery and the
    issuer claim line up.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        id_token_email: bool = True,
        groups: list[str] | None = None,
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.id_token_email = id_token_email
        self.groups = groups or []
        self.kid = "test-key-1"
        self.key = make_rsa_key()
        self._codes: dict[str, dict] = {}
        self.code_exchanges: list[dict] = []
        self.app = self._build_app()

    # ------------------------------------------------------------- tokens
    def _id_token(self, nonce: str, sub: str) -> str:
        import time

        claims = {"sub": sub, "preferred_username": "alice"}
        if self.id_token_email:
            claims["email"] = "alice@example.com"
            claims["email_verified"] = True
        if self.groups:
            claims["groups"] = self.groups
        return sign_id_token(
            self.key,
            kid=self.kid,
            iss=self.issuer,
            aud=self.client_id,
            sub=sub,
            nonce=nonce,
            exp=int(time.time()) + 600,
            iat=int(time.time()),
            claims=claims,
        )

    # ------------------------------------------------------------- ASGI app
    def _build_app(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import RedirectResponse

        app = FastAPI(title="fake-oidc-provider")
        store = self._codes

        @app.get("/.well-known/openid-configuration")
        async def discovery():
            return {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
                "userinfo_endpoint": f"{self.issuer}/userinfo",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "id_token_signing_alg_values_supported": ["RS256"],
            }

        @app.get("/jwks")
        async def jwks():
            return {"keys": [public_jwk(self.key, self.kid)]}

        @app.get("/authorize")
        async def authorize(
            response_type: str,
            client_id: str,
            redirect_uri: str,
            scope: str,
            state: str,
            nonce: str,
            code_challenge: str,
            code_challenge_method: str,
        ):
            if (
                response_type != "code"
                or client_id != self.client_id
                or code_challenge_method != "S256"
            ):
                raise HTTPException(status_code=400, detail="bad authorize request")
            code = secrets.token_urlsafe(16)
            store[code] = {
                "challenge": code_challenge,
                "nonce": nonce,
                "sub": "sub-123",
            }
            sep = "&" if "?" in redirect_uri else "?"
            return RedirectResponse(
                f"{redirect_uri}{sep}code={code}&state={state}", status_code=302
            )

        @app.post("/token")
        async def token(request: Request):
            # URL-encoded body parsed manually (no python-multipart needed).
            raw = await request.body()
            form = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
            grant_type = form.get("grant_type")
            code = form.get("code")
            verifier = form.get("code_verifier")
            client_id = form.get("client_id")
            if grant_type != "authorization_code" or client_id != self.client_id:
                raise HTTPException(status_code=400, detail="bad grant")
            rec = store.pop(code, None) if code else None
            self.code_exchanges.append(
                {
                    "code": code,
                    "verifier": verifier,
                    "challenge_ok": bool(rec and s256_challenge(str(verifier)) == rec["challenge"]),
                }
            )
            if rec is None or s256_challenge(str(verifier)) != rec["challenge"]:
                raise HTTPException(status_code=400, detail="invalid code or verifier")
            return {
                "id_token": self._id_token(rec["nonce"], rec["sub"]),
                "access_token": secrets.token_urlsafe(24),
                "token_type": "Bearer",
                "expires_in": 300,
            }

        @app.get("/userinfo")
        async def userinfo():
            return {
                "sub": "sub-123",
                "preferred_username": "alice",
                "email": "alice@example.com",
                "email_verified": True,
            }

        return app


def build_fake_provider(**kwargs) -> FakeOidcProvider:
    return FakeOidcProvider(**kwargs)
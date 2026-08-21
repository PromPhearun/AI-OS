"""OIDC + PKCE human authentication for the control plane — Slice 5.3.

Implements the **authorization-code flow with PKCE** (RFC 7636) for the web
desktop against any standards-compliant OIDC provider:

1. ``POST /v1/auth/oidc/authorize`` builds the provider's authorization URL
   carrying ``code_challenge`` (S256), a random ``state`` and ``nonce``; the
   verifier/state/nonce are held server-side in a single-use, expiring store.
2. The provider redirects the human back to ``GET /v1/auth/oidc/callback``;
   the code is exchanged at the token endpoint (PKCE-verified), the ID token
   is cryptographically verified against the provider's JWKS (RS256/ES256,
   issuer/audience/expiry/nonce), and the resolved ``Principal`` is handed to
   the browser as a short-lived, one-time, HttpOnly grant cookie.
3. ``POST /v1/auth/oidc/session`` consumes that grant (single-use) and returns
   a normal aios JWT — the same token shape the API-key login returns, so the
   rest of the control plane is unchanged.

Configuration (environment)::

    AIOS_OIDC_ISSUER          https://idp.example.com/  (required to enable)
    AIOS_OIDC_CLIENT_ID       public client id (required)
    AIOS_OIDC_CLIENT_SECRET   optional — confidential client (PKCE still on)
    AIOS_OIDC_REDIRECT_URI    optional — defaults to <base>/v1/auth/oidc/callback
    AIOS_OIDC_SCOPES          "openid profile email" (default)
    AIOS_OIDC_ROLE_CLAIM      claim holding group/role values (default "groups")
    AIOS_OIDC_OPERATOR_VALUES comma-separated claim values => operator role
    AIOS_OIDC_ADMIN_EMAILS    comma-separated emails => operator role
    AIOS_OIDC_POST_LOGIN      relative path to land after login (default "/")
    AIOS_OIDC_TIMEOUT_S       HTTP timeout for provider calls (default 10)
    AIOS_OIDC_CACHE_TTL_S     discovery/JWKS cache TTL (default 300)

Role mapping is deny-by-default in the explicit direction: when a mapping is
configured the user only reaches ``operator`` if a mapped value matches,
otherwise ``standard``. With **no** mapping configured every authenticated
user is ``operator`` (the control plane's human surface is operator-oriented)
and the server logs a loud warning at startup. ``email_verified: false``
explicitly demotes an admin-email match to ``standard``.

Security posture:

* PKCE verifier, authorization state, and nonce are all high-entropy random
  values, held server-side, single-use, and TTL-expiring.
* The one-time grant cookie is HttpOnly + SameSite=Lax, path-scoped to
  ``/v1/auth/oidc``, hashed at rest, and consumed exactly once.
* ID tokens reject ``alg=none`` and symmetric ``HS*``; only RS256/384/512 and
  ES256/384 over the provider's published JWKS are accepted.
* No code, verifier, ID token, access token, or grant value ever reaches the
  audit log or any response body.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from .auth import Principal

# The one-time post-login grant cookie set by the callback and consumed by
# POST /v1/auth/oidc/session. Path-scoped so it never leaves the OIDC flow.
GRANT_COOKIE = "aios_oidc_grant"
GRANT_TTL_S = 120.0


class OidcError(Exception):
    """A control-plane OIDC failure mapped to an HTTP error envelope.

    ``status`` is the HTTP status the route returns; ``code``/``message`` go
    into the standard ``{error: {code, message}}`` envelope. Messages are
    deliberately generic — details belong in the audit log.
    """

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_result(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


# ------------------------------------------------------------------ RFC 7636
def generate_code_verifier() -> str:
    """A 64-char RFC 7636 PKCE verifier (43–128 unreserved chars)."""
    return secrets.token_urlsafe(48)[:64]


def s256_challenge(verifier: str) -> str:
    """RFC 7636 S256 code challenge: b64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except (binascii.Error, ValueError) as exc:
        raise OidcError("E_INVAL", "malformed id_token") from exc


def _b64u_to_int(value: str) -> int:
    return int.from_bytes(_b64u_decode(value), "big")


# -------------------------------------------------------------------- config
def _csv_env(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


@dataclass(frozen=True)
class OidcConfig:
    """Static OIDC provider configuration (reads environment at boot)."""

    issuer: str
    client_id: str
    client_secret: str | None = None
    redirect_uri: str | None = None
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    role_claim: str = "groups"
    operator_values: frozenset[str] = frozenset()
    admin_emails: frozenset[str] = frozenset()
    post_login_path: str = "/"
    timeout_s: float = 10.0
    cache_ttl_s: float = 300.0
    txn_ttl_s: float = 600.0

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id)

    def __post_init__(self) -> None:
        if self.enabled and not self.issuer.startswith(("http://", "https://")):
            raise ValueError(f"AIOS_OIDC_ISSUER must be an http(s) URL, got {self.issuer!r}")
        if not self.post_login_path.startswith("/") or self.post_login_path.startswith("//"):
            raise ValueError("post_login_path must be a relative URL starting with '/'")

    @classmethod
    def from_env(cls) -> "OidcConfig":
        """Build the config from AIOS_OIDC_* env vars."""
        issuer = os.environ.get("AIOS_OIDC_ISSUER", "").strip()
        client_id = os.environ.get("AIOS_OIDC_CLIENT_ID", "").strip()
        if bool(issuer) != bool(client_id):
            raise ValueError(
                "AIOS_OIDC_ISSUER and AIOS_OIDC_CLIENT_ID must be set together"
            )
        if not issuer:
            return cls(issuer="", client_id="")
        scopes = tuple(
            s for s in os.environ.get("AIOS_OIDC_SCOPES", "openid profile email").split() if s
        )
        return cls(
            issuer=issuer,
            client_id=client_id,
            client_secret=os.environ.get("AIOS_OIDC_CLIENT_SECRET") or None,
            redirect_uri=os.environ.get("AIOS_OIDC_REDIRECT_URI") or None,
            scopes=scopes,
            role_claim=os.environ.get("AIOS_OIDC_ROLE_CLAIM", "groups"),
            operator_values=frozenset(_csv_env("AIOS_OIDC_OPERATOR_VALUES")),
            admin_emails=frozenset(a.lower() for a in _csv_env("AIOS_OIDC_ADMIN_EMAILS")),
            post_login_path=os.environ.get("AIOS_OIDC_POST_LOGIN", "/"),
            timeout_s=float(os.environ.get("AIOS_OIDC_TIMEOUT_S", "10")),
            cache_ttl_s=float(os.environ.get("AIOS_OIDC_CACHE_TTL_S", "300")),
            txn_ttl_s=float(os.environ.get("AIOS_OIDC_TXN_TTL_S", "600")),
        )


# ----------------------------------------------------------- session store
class OidcSessionStore:
    """Server-side single-use stores for PKCE transactions and post-login grants.

    Both stores are TTL-expiring and thread-safe. A transaction is consumed at
    the callback (so a state cannot be replayed); a grant is consumed at the
    session exchange (so a stolen cookie cannot be replayed). ``now_fn``
    injects the clock (tests use a fake monotonic clock).
    """

    def __init__(
        self,
        *,
        txn_ttl_s: float = 600.0,
        grant_ttl_s: float = GRANT_TTL_S,
        now_fn=None,
    ) -> None:
        self._txn_ttl_s = txn_ttl_s
        self._grant_ttl_s = grant_ttl_s
        self._now_fn = now_fn or time.monotonic
        self._txns: dict[str, tuple[str, str, float]] = {}  # txn_id -> (verifier, nonce, exp)
        self._grants: dict[str, tuple[str, str, float]] = {}  # sha256 -> (name, role, exp)
        self._lock = threading.Lock()

    def create_txn(self, txn_id: str, verifier: str, nonce: str) -> None:
        now = self._now_fn()
        with self._lock:
            self._sweep(now)
            self._txns[txn_id] = (verifier, nonce, now + self._txn_ttl_s)

    def consume_txn(self, txn_id: str) -> tuple[str, str] | None:
        """Pop + return ``(verifier, nonce)``; None when unknown/expired/replayed."""
        now = self._now_fn()
        with self._lock:
            self._sweep(now)
            entry = self._txns.pop(txn_id, None)
        if entry is None or entry[2] < now:
            return None
        return entry[0], entry[1]

    def issue_grant(self, principal: Principal) -> str:
        """Return a one-time cookie value; only its sha256 is stored."""
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._now_fn()
        with self._lock:
            self._sweep(now)
            self._grants[digest] = (principal.name, principal.role, now + self._grant_ttl_s)
        return token

    def consume_grant(self, token: str) -> Principal | None:
        """Pop the grant; returns the Principal or None (invalid/expired/replayed)."""
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._now_fn()
        with self._lock:
            self._sweep(now)
            entry = self._grants.pop(digest, None)
        if entry is None or entry[2] < now:
            return None
        return Principal(name=entry[0], role=entry[1])

    def _sweep(self, now: float) -> None:
        self._txns = {k: v for k, v in self._txns.items() if v[2] >= now}
        self._grants = {k: v for k, v in self._grants.items() if v[2] >= now}


# ------------------------------------------------------------ OIDC client
_SIG_HASH = {
    "RS256": hashes.SHA256(),
    "RS384": hashes.SHA384(),
    "RS512": hashes.SHA512(),
    "ES256": hashes.SHA256(),
    "ES384": hashes.SHA384(),
}
_EC_CURVES = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1()}


def _verify_signature(alg: str, jwk: dict, message: bytes, signature: bytes) -> None:
    """Verify ``message`` against a JWK with ``cryptography`` (RS/ES only)."""
    kty = jwk.get("kty")
    hash_alg = _SIG_HASH.get(alg)
    if hash_alg is None:
        raise OidcError("E_INVAL", f"unsupported id_token alg {alg!r}")
    try:
        if kty == "RSA":
            pub = rsa.RSAPublicNumbers(_b64u_to_int(jwk["e"]), _b64u_to_int(jwk["n"])).public_key()
            pub.verify(signature, message, padding.PKCS1v15(), hash_alg)
        elif kty == "EC":
            curve = _EC_CURVES.get(jwk.get("crv", ""))
            if curve is None:
                raise OidcError("E_INVAL", f"unsupported EC curve {jwk.get('crv')!r}")
            pub = ec.EllipticCurvePublicNumbers(
                _b64u_to_int(jwk["x"]), _b64u_to_int(jwk["y"]), curve
            ).public_key()
            pub.verify(signature, message, ec.ECDSA(hash_alg))
        else:
            raise OidcError("E_INVAL", f"unsupported jwk kty {kty!r}")
    except OidcError:
        raise
    except Exception as exc:  # InvalidSignature and friends all fail closed
        raise OidcError("E_PERM", "id_token signature verification failed") from exc


def _add_query(url: str, params: dict[str, str]) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode(params)}"


class OidcClient:
    """Async OIDC discovery/JWKS/authorization/token-exchange client.

    ``http`` injects an ``httpx.AsyncClient`` (tests use an ASGI/Mock
    transport); when omitted the client opens a short-lived, time-bounded
    client per provider call. ``store`` injects the session store (defaults to
    a fresh one with the config's transaction TTL).
    """

    def __init__(
        self,
        config: OidcConfig,
        *,
        http: httpx.AsyncClient | None = None,
        store: OidcSessionStore | None = None,
    ) -> None:
        self.config = config
        self._http = http
        self.store = store or OidcSessionStore(txn_ttl_s=config.txn_ttl_s)
        self._discovery: dict | None = None
        self._discovery_at = 0.0
        self._jwks: list[dict] | None = None
        self._jwks_at = 0.0

    # ----------------------------------------------------------------- HTTP
    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        client = self._http
        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=self.config.timeout_s, follow_redirects=False)
        try:
            return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise OidcError("E_NET", "oidc provider unreachable", status=502) from exc
        finally:
            if owns:
                await client.aclose()

    async def _fetch_json(self, url: str, *, headers: dict | None = None) -> dict:
        resp = await self._request("GET", url, headers=headers)
        if resp.status_code != 200:
            raise OidcError("E_NET", f"oidc provider returned {resp.status_code}", status=502)
        try:
            return resp.json()
        except ValueError as exc:
            raise OidcError("E_INVAL", "oidc provider returned non-JSON", status=502) from exc

    # ------------------------------------------------------------ discovery
    async def _discover(self) -> dict:
        now = time.monotonic()
        if self._discovery is not None and now - self._discovery_at < self.config.cache_ttl_s:
            return self._discovery
        url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        doc = await self._fetch_json(url)
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
            if not doc.get(key):
                raise OidcError("E_INVAL", "oidc discovery missing required endpoints", status=502)
        self._discovery = doc
        self._discovery_at = now
        return doc

    async def _jwks_doc(self) -> list[dict]:
        now = time.monotonic()
        if self._jwks is not None and now - self._jwks_at < self.config.cache_ttl_s:
            return self._jwks
        disc = await self._discover()
        doc = await self._fetch_json(disc["jwks_uri"])
        keys = [k for k in doc.get("keys", []) if k.get("kty") in ("RSA", "EC")]
        self._jwks = keys
        self._jwks_at = now
        return keys

    async def _jwk_for(self, kid: str | None) -> dict:
        keys = await self._jwks_doc()
        for key in keys:
            if kid is None or key.get("kid") == kid:
                return key
        raise OidcError("E_INVAL", "no usable jwk for id_token signature")

    # --------------------------------------------------------- redirect URI
    def redirect_uri_for(self, request) -> str:
        """The callback URL the provider must send the human back to."""
        if self.config.redirect_uri:
            return self.config.redirect_uri
        return str(request.url_for("oidc_callback"))

    # ---------------------------------------------------------- start login
    async def start_authorization(self, redirect_uri: str) -> str:
        """Return the provider authorize URL for a fresh PKCE transaction."""
        disc = await self._discover()
        verifier = generate_code_verifier()
        nonce = secrets.token_urlsafe(24)
        state = secrets.token_urlsafe(32)  # doubles as the server txn id
        self.store.create_txn(state, verifier, nonce)
        return _add_query(
            disc["authorization_endpoint"],
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": s256_challenge(verifier),
                "code_challenge_method": "S256",
            },
        )

    # ------------------------------------------------------ complete login
    async def complete_authorization(self, *, code: str, state: str, redirect_uri: str) -> Principal:
        """Exchange the authorization code and resolve the OIDC Principal."""
        txn = self.store.consume_txn(state)
        if txn is None:
            raise OidcError(
                "E_INVAL",
                "authorization state unknown, expired, or replayed",
                status=400,
            )
        verifier, nonce = txn
        disc = await self._discover()

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": verifier,
        }
        headers = {}
        if self.config.client_secret:
            basic = base64.b64encode(
                f"{self.config.client_id}:{self.config.client_secret}".encode("ascii")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"

        resp = await self._request("POST", disc["token_endpoint"], data=data, headers=headers)
        if resp.status_code != 200:
            raise OidcError(
                "E_PERM", "oidc token exchange rejected by provider", status=502
            )
        try:
            token_body = resp.json()
        except ValueError as exc:
            raise OidcError("E_INVAL", "oidc token response not JSON", status=502) from exc

        id_token = token_body.get("id_token")
        access_token = token_body.get("access_token")
        if not id_token:
            raise OidcError("E_INVAL", "oidc token response missing id_token", status=502)

        claims = await self.verify_id_token(id_token, nonce=nonce)

        # Fetch userinfo only when the role mapping needs a claim the ID token
        # did not carry (deny-by-default: a failed userinfo fails the login).
        needs_userinfo = False
        if self.config.admin_emails and not claims.get("email"):
            needs_userinfo = True
        elif self.config.operator_values and claims.get(self.config.role_claim) is None:
            needs_userinfo = True
        userinfo: dict = {}
        if needs_userinfo and access_token:
            userinfo = await self.fetch_userinfo(access_token)

        return self.resolve_principal(claims, userinfo)

    async def fetch_userinfo(self, access_token: str) -> dict:
        disc = await self._discover()
        return await self._fetch_json(
            disc["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )

    # ------------------------------------------------------ ID token verify
    async def verify_id_token(self, token: str, *, nonce: str | None) -> dict:
        """Verify signature + claims of an ID token; return its payload.

        Rejects ``alg=none`` and any symmetric ``HS*`` algorithm (a public
        client cannot hold the key). Issuer, audience, expiry, nbf, and the
        per-transaction nonce must all validate; every failure raises
        :class:`OidcError` (fail closed).
        """
        parts = token.split(".")
        if len(parts) != 3:
            raise OidcError("E_INVAL", "malformed id_token")
        try:
            header = json.loads(_b64u_decode(parts[0]))
            payload = json.loads(_b64u_decode(parts[1]))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OidcError("E_INVAL", "malformed id_token") from exc

        alg = str(header.get("alg", ""))
        if alg == "none" or alg.startswith("HS"):
            raise OidcError("E_INVAL", f"unsafe id_token alg {alg!r}")

        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        signature = _b64u_decode(parts[2])
        jwk = await self._jwk_for(header.get("kid"))
        _verify_signature(alg, jwk, signing_input, signature)

        now = time.time()
        skew = 60.0
        if str(payload.get("iss", "")).rstrip("/") != self.config.issuer.rstrip("/"):
            raise OidcError("E_PERM", "id_token issuer mismatch")
        aud = payload.get("aud")
        if isinstance(aud, str):
            aud = [aud]
        if not aud or self.config.client_id not in (aud or []):
            raise OidcError("E_PERM", "id_token audience mismatch")
        if float(payload.get("exp", 0)) < now - skew:
            raise OidcError("E_PERM", "id_token expired")
        if float(payload.get("iat", 0)) > now + skew:
            raise OidcError("E_PERM", "id_token issued in the future")
        nbf = payload.get("nbf")
        if nbf is not None and float(nbf) > now + skew:
            raise OidcError("E_PERM", "id_token not yet valid")
        if nonce is not None and payload.get("nonce") != nonce:
            raise OidcError("E_PERM", "id_token nonce mismatch")
        return payload

    # ---------------------------------------------------------- role mapping
    def resolve_principal(self, claims: dict, userinfo: dict | None = None) -> Principal:
        """Map OIDC claims to a control-plane Principal.

        Deny-by-default: when ``admin_emails`` or ``operator_values`` is
        configured the user is ``operator`` only on a match and ``standard``
        otherwise; with neither configured, every authenticated OIDC user
        receives the ``standard`` role (fail-closed).  Operators are always
        explicitly granted.
        """
        merged = dict(claims)
        merged.update(userinfo or {})
        name = (
            merged.get("preferred_username")
            or merged.get("email")
            or merged.get("sub")
            or "unknown"
        )
        role = self._role_for(merged)
        return Principal(name=f"oidc:{name}", role=role)

    def _role_for(self, claims: dict) -> str:
        if self.config.admin_emails:
            email = str(claims.get("email") or "").lower()
            if claims.get("email_verified") is False:
                return "standard"  # unverified email never grants operator
            return "operator" if email in self.config.admin_emails else "standard"
        if self.config.operator_values:
            values = claims.get(self.config.role_claim)
            if isinstance(values, str):
                values = [values]
            if values and any(str(v) in self.config.operator_values for v in values):
                return "operator"
            return "standard"
        # No explicit role mapping configured — fail-closed to standard.
        return "standard"


__all__ = [
    "GRANT_COOKIE",
    "GRANT_TTL_S",
    "OidcError",
    "OidcConfig",
    "OidcSessionStore",
    "OidcClient",
    "generate_code_verifier",
    "s256_challenge",
]
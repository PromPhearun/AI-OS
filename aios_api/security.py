"""Control-plane security: rate limiting, security headers, request auditing.

Per docs/08-security.md: rate limit every endpoint, send the standard security
headers, keep CORS locked to a strict allowlist, and audit every control
request (never the credentials, bodies, or tokens).
"""

from __future__ import annotations

import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Cross-Origin-Opener-Policy": "same-origin",
}

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"

# FastAPI's Swagger UI page loads its assets from jsDelivr and boots via an
# inline script, then fetches /openapi.json — the strict `default-src 'none'`
# policy above would render the page blank. Give the docs paths a narrower
# policy: only their own CDN assets, the inline boot script, and the same-origin
# schema fetch; framing and every other resource class stay denied.
DOCS_PATHS = ("/docs", "/openapi.json")
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)

# The built web desktop (`web/dist`, mounted by create_app when present) is
# same-origin with the API: its shell at `/` and hashed assets under /assets/*
# get a policy that permits the app's own scripts/styles plus same-origin REST
# and WebSocket traffic — still no inline script, no CDNs, no framing. Every
# other path (the entire /v1 control surface included) keeps the strict policy.
WEB_SHELL_PATHS = ("/",)
WEB_ASSET_PREFIX = "/assets/"
WEB_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'"
)


class RateLimiter:
    """Sliding-window rate limiter keyed by client (IP or principal)."""

    def __init__(self, *, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_s). O(1) amortized per key."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False, max(self.window_s - (now - hits[0]), 0.0)
            hits.append(now)
            self._hits[key] = hits
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests past the limiter budget with a 429 + Retry-After.

    Keyed by ``principal:client-ip`` when authenticated, client IP otherwise.
    """

    def __init__(self, app, limiter: RateLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        # Static web-desktop assets are not control operations: do not count
        # them against the operator's control budget.
        if request.url.path.startswith(WEB_ASSET_PREFIX):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        principal = getattr(request.state, "principal", None)
        key = f"{principal.name}:{client}" if principal else client
        allowed, retry_after = self.limiter.allow(key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "E_AGAIN",
                        "message": "rate limit exceeded — retry later",
                    }
                },
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        path = request.url.path
        if path.startswith(DOCS_PATHS):
            # Keep every header above, but narrow the one that would render the
            # interactive API docs unusable in a browser.
            response.headers["Content-Security-Policy"] = DOCS_CSP
        elif path in WEB_SHELL_PATHS or path.startswith(WEB_ASSET_PREFIX):
            # The built web desktop is served same-origin; allow its own
            # scripts/styles and same-origin REST + WebSocket traffic.
            response.headers["Content-Security-Policy"] = WEB_CSP
        return response


class ControlAuditMiddleware(BaseHTTPMiddleware):
    """Record every control request to the kernel audit trail.

    Credentials are never logged: only method, path, status, principal, client
    IP and duration. The authenticated principal is read from
    ``request.state.principal`` (set by the auth dependency) — anonymous for
    public endpoints. Static web assets and the SPA shell are not control
    operations and are exempt (they would drown the trail in page-load noise).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        exempt = path in WEB_SHELL_PATHS or path.startswith(WEB_ASSET_PREFIX)
        start = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if not exempt:
                kernel = request.app.state.kernel
                principal = getattr(request.state, "principal", None)
                kernel.audit.record(
                    "control.request",
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    principal=principal.name if principal else "anonymous",
                    role=principal.role if principal else "none",
                    duration_ms=round((time.monotonic() - start) * 1000.0, 2),
                    client=request.client.host if request.client else "?",
                )


__all__ = [
    "SECURITY_HEADERS",
    "DEFAULT_CORS_ORIGINS",
    "RateLimiter",
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
    "ControlAuditMiddleware",
]
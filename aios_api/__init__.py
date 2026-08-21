"""FastAPI control plane for aios — docs/10-ui.md §4.

``create_app`` wires a running :class:`aios_kernel.Kernel` into a FastAPI app:

* JWT auth (API keys or OIDC/PKCE for humans, JWT for the web desktop)
* rate limiting on every HTTP endpoint (sliding window)
* strict CORS + security headers
* a kernel-audited request log (credentials never recorded)
* REST control endpoints + WebSocket console/feed streams
* the built web desktop (``web/dist``) when present, served same-origin
  with a narrowed CSP on the app shell + asset paths only
"""

from __future__ import annotations

import importlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aios import __version__
from aios_kernel import Kernel

from .auth import AuthManager, parse_api_keys_env
from .errors import register_exception_handlers
from .oidc import OidcClient, OidcConfig
from .routes import router
from .security import (
    DEFAULT_CORS_ORIGINS,
    ControlAuditMiddleware,
    RateLimiter,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from .ws import ws_router

logger = logging.getLogger("aios_api")


def web_dist_dir() -> str | None:
    """Directory holding a built web desktop, or None when absent.

    Resolves ``AIOS_WEB_DIST`` first, then the source checkout's ``web/dist``.
    """
    override = os.environ.get("AIOS_WEB_DIST")
    if override:
        return override if os.path.isdir(override) else None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(root, "web", "dist")
    return candidate if os.path.isdir(candidate) else None


def create_app(
    kernel: Kernel,
    *,
    agents_module: str | None = None,
    jwt_ttl_s: int = 3600,
    rate_limit: int = 240,
    rate_window_s: float = 60.0,
    cors_origins: str | None = None,
    shutdown_on_exit: bool = False,
    oidc_client: OidcClient | None = None,
) -> FastAPI:
    """Build the control-plane app around a live kernel.

    ``agents_module`` names the module whose ``@agent`` definitions back
    ``POST /v1/agents`` (imported at startup). ``shutdown_on_exit`` shuts the
    kernel down when the app lifespan exits (used by ``aios serve``).
    ``oidc_client`` injects an OIDC client (tests); by default one is built
    from the ``AIOS_OIDC_*`` environment when configured.
    """
    api_keys = parse_api_keys_env(os.environ.get("AIOS_API_KEYS"))
    auth = AuthManager(api_keys, jwt_ttl_s=jwt_ttl_s)
    limiter = RateLimiter(limit=rate_limit, window_s=rate_window_s)
    raw_origins = cors_origins or os.environ.get("AIOS_CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
    origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    if oidc_client is None:
        try:
            oidc_config = OidcConfig.from_env()
            oidc_client = OidcClient(oidc_config) if oidc_config.enabled else None
        except ValueError as exc:
            logger.error("OIDC disabled — invalid AIOS_OIDC_* configuration: %s", exc)
            oidc_client = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if agents_module:
            importlib.import_module(agents_module)
        if auth.dev_enabled:
            logger.warning(
                "no AIOS_API_KEYS configured — dev key 'dev-key' (operator role) enabled"
            )
        if auth.generated_secret:
            logger.warning(
                "AIOS_JWT_SECRET unset — ephemeral secret generated (tokens invalid after restart)"
            )
        if oidc_client is not None:
            logger.info("OIDC enabled (issuer=%s)", oidc_client.config.issuer)
            if not oidc_client.config.admin_emails and not oidc_client.config.operator_values:
                logger.warning(
                    "OIDC enabled with no AIOS_OIDC_ADMIN_EMAILS / AIOS_OIDC_OPERATOR_VALUES — "
                    "every authenticated OIDC user receives the operator role"
                )
        yield
        if shutdown_on_exit:
            await kernel.shutdown()

    app = FastAPI(
        title="aios control plane",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    app.state.kernel = kernel
    app.state.auth = auth
    app.state.limiter = limiter
    app.state.oidc = oidc_client
    app.state.config = {
        "rate_limit": rate_limit,
        "rate_window_s": rate_window_s,
        "jwt_ttl_s": jwt_ttl_s,
        "cors_origins": origins,
        "oidc_issuer": oidc_client.config.issuer if oidc_client else None,
    }

    # Middleware order: last added runs first. Audit outermost so rate-limit
    # rejections and CORS preflights are also recorded; CORS answers preflights
    # without reaching the rate limiter.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(ControlAuditMiddleware)

    register_exception_handlers(app)
    app.include_router(router)
    app.include_router(ws_router)

    # Serve the built web desktop (web/dist) same-origin: hashed assets under
    # /assets/* and an SPA catch-all for everything that is not an API route.
    web_dist = web_dist_dir()
    if web_dist:
        assets_dir = os.path.join(web_dist, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
        index = os.path.join(web_dist, "index.html")
        if os.path.isfile(index):

            @app.get("/{path:path}", include_in_schema=False)
            async def web_shell(path: str):
                # Registered routes (all /v1/*, /docs, /openapi.json, /assets)
                # match before this catch-all; anything else is SPA navigation.
                if path.startswith("v1/"):
                    raise HTTPException(status_code=404, detail="not found")
                return FileResponse(index)

        logger.info("web desktop mounted from %s", web_dist)
    else:
        logger.info(
            "web desktop not found (build with `cd web && npm run build` or set "
            "AIOS_WEB_DIST) — /docs is the browser surface"
        )
    return app


__all__ = ["create_app"]
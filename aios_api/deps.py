"""FastAPI dependencies for the control plane."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from aios_kernel import Kernel

from .auth import Principal


def get_kernel(request: Request) -> Kernel:
    return request.app.state.kernel


def _principal_from_request(request: Request) -> Principal | None:
    """Resolve the caller from X-API-Key or Authorization: Bearer <jwt>."""
    auth = request.app.state.auth
    api_key = request.headers.get("x-api-key")
    if api_key:
        return auth.authenticate(api_key)
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        try:
            return auth.verify_token(token.strip())
        except ValueError:
            return None
    return None


def require_auth(
    request: Request, principal: Principal | None = Depends(_principal_from_request)
) -> Principal:
    """Authenticate the caller; attach the principal for the audit middleware."""
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "E_PERM", "message": "authentication required"}},
            headers={"WWW-Authenticate": 'Bearer realm="aios-control"'},
        )
    request.state.principal = principal
    return principal


def require_operator(
    request: Request, principal: Principal = Depends(require_auth)
) -> Principal:
    if not principal.is_operator():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "E_PERM", "message": "operator role required"}},
        )
    return principal


__all__ = ["get_kernel", "require_auth", "require_operator"]
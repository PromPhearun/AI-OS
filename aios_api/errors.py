"""Error mapping for the control plane.

Kernel errors (``AiosError``) are returned as ``{error: {code, message}}`` with
an HTTP status derived from the canonical code. Messages are deliberately
generic; detailed traces live in the kernel audit log (docs/08-security.md).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aios_kernel.errors import AiosError, E_INVAL, E_NOENT, E_PERM, E_STATE, E_BUSY

_STATUS_BY_CODE = {
    E_INVAL: 400,
    E_PERM: 403,
    E_NOENT: 404,
    E_STATE: 409,
    E_BUSY: 409,
    "E_AGAIN": 503,
    "E_TIMEOUT": 504,
    "E_LLM": 502,
}


def status_for(code: str) -> int:
    return _STATUS_BY_CODE.get(code, 500)


async def _aios_error_handler(request: Request, exc: AiosError) -> JSONResponse:
    return JSONResponse(
        status_code=status_for(exc.code),
        content=exc.to_result(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AiosError, _aios_error_handler)


__all__ = ["status_for", "register_exception_handlers"]
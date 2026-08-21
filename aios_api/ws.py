"""WebSocket streams for the web desktop.

Two authenticated streams:

* ``/v1/ws/feed`` — global live feed: scheduler snapshot (1 Hz), process
  table deltas, and new audit entries as they land.
* ``/v1/agents/{pid}/ws/console`` — tail of an agent's console log lines.

Authentication uses a one-time, short-lived token issued by
``POST /v1/auth/ws-token`` (passed as ``?token=``).  Legacy JWT-in-query
is still accepted but the web desktop is expected to use the WS token
exchange.  Credentials never travel over the wire after the handshake
token, and audit entries are sent verbatim (they carry no secrets by
construction).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import Principal

ws_router = APIRouter()


async def _ws_principal(websocket: WebSocket) -> Principal | None:
    token = websocket.query_params.get("token", "")
    if not token:
        return None
    auth = websocket.app.state.auth
    # Try the single-use WS token store first (preferred path).
    principal = auth.consume_ws_token(token)
    if principal is not None:
        return principal
    # Fall back to JWT verification (backward compatibility).
    try:
        return auth.verify_token(token)
    except ValueError:
        return None


async def _accept_or_deny(websocket: WebSocket) -> Principal | None:
    principal = await _ws_principal(websocket)
    await websocket.accept()
    if principal is None:
        await websocket.close(code=4401, reason="unauthorized")
        return None
    return principal


@ws_router.websocket("/v1/ws/feed")
async def ws_feed(websocket: WebSocket):
    principal = await _accept_or_deny(websocket)
    if principal is None:
        return
    kernel = websocket.app.state.kernel
    last_len = len(kernel.audit._entries)
    tick = 0
    try:
        while True:
            entries = kernel.audit._entries[last_len:]
            if entries:
                last_len = len(kernel.audit._entries)
                await websocket.send_json({"type": "audit", "data": entries})
            tick += 1
            if tick % 4 == 0:  # ~1 Hz scheduler/process heartbeat
                await websocket.send_json(
                    {"type": "scheduler", "data": kernel.scheduler.snapshot()}
                )
                rows = []
                for pid in sorted(kernel.agent_manager._table):
                    rec = kernel.agent_manager.record(pid)
                    if rec is not None:
                        rows.append(rec)
                await websocket.send_json({"type": "processes", "data": rows})
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


@ws_router.websocket("/v1/agents/{pid}/ws/console")
async def agent_console(websocket: WebSocket, pid: int):
    principal = await _accept_or_deny(websocket)
    if principal is None:
        return
    kernel = websocket.app.state.kernel
    sent = 0
    try:
        while True:
            live = kernel.agent_manager._table.get(pid)
            dead = kernel.agent_manager._reaped.get(pid)
            if live is None and dead is None:
                await websocket.close(code=4004, reason="no such agent")
                return
            lines = kernel.agent_logs.get(pid, [])
            for i in range(sent, len(lines)):
                await websocket.send_json({"type": "console", "data": lines[i]})
            sent = len(lines)
            if live is None and dead is not None:
                # Agent finished / was reaped: drain the tail then end the stream.
                await websocket.close()
                return
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
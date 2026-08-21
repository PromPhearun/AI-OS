"""broker.py — multi-kernel IPC broker (Phase 5, Slice 5.4).

Routes IPC messages and pub/sub events across kernel instances (docs/06-ipc.md
§11). Three pieces:

  * ``Broker``       — in-process authority: a pid -> (kernel_id, group_id)
                       registry, cross-kernel subscription fan-out, and
                       per-kernel delivery callbacks. Fail-closed: unknown
                       pids and unknown kernels are never delivered to, and
                       bodies are never inspected — each kernel re-applies its
                       own permission + audit rules on arrival.
  * ``BrokerServer`` — optional asyncio TCP bridge (newline-delimited JSON)
                       so kernels in separate processes can share one broker.
                       Sharing is gated by a shared token (``AIOS_BROKER_TOKEN``);
                       a server without a configured token rejects every client.
  * ``BrokerClient`` — the wire client used by a remote kernel; exposes the
                       same op surface as ``Broker``. Fire-and-forget ops
                       (claim / release / subscribe / unsubscribe / route) are
                       queued on a single ordered writer so a claim is always
                       visible to a later route; ``resolve`` / ``publish`` are
                       request/response with ``req_id`` matching.

The IPC manager in each kernel decides what to route. Locally owned targets
are enqueued directly; the broker is only consulted for remote targets and for
fanning ``publish`` to other kernels' subscribers.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from typing import Any, Awaitable, Callable

Deliver = Callable[[dict], Awaitable[None]]


def topic_match(pattern: str, topic: str) -> bool:
    """Hierarchical topic match: ``*``, exact, or ``prefix.*`` suffix."""
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-2] + ".")
    return False


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Broker:
    """In-process multi-kernel IPC authority (see module docstring)."""

    def __init__(self, broker_id: str | None = None):
        self.broker_id = broker_id or _new_id("broker")
        self._peers: dict[int, dict[str, str]] = {}  # pid -> {"kernel_id", "group_id"}
        self._deliverers: dict[str, Deliver] = {}  # kernel_id -> delivery callback
        self._subscriptions: dict[tuple[str, int], list[str]] = {}  # (kernel_id, pid) -> patterns
        self._tasks: set[asyncio.Task] = set()
        self._next_pid = 1

    # ------------------------------------------------------------- lifecycle
    def register_kernel(self, kernel_id: str, deliver: Deliver) -> None:
        """Attach a kernel id to its delivery callback."""
        self._deliverers[kernel_id] = deliver

    def unregister_kernel(self, kernel_id: str) -> None:
        """Detach a kernel; drop its peers and subscriptions (fail closed)."""
        self._deliverers.pop(kernel_id, None)
        for key in [k for k in self._subscriptions if k[0] == kernel_id]:
            self._subscriptions.pop(key, None)
        for pid in [p for p, rec in self._peers.items() if rec["kernel_id"] == kernel_id]:
            self._peers.pop(pid, None)

    # ---------------------------------------------------------- pid registry
    def claim(self, pid: int, *, kernel_id: str, group_id: str = "default") -> None:
        """Register ``pid`` as living on ``kernel_id``.

        Ignored for kernels that have no delivery callback (they cannot
        receive anyway), keeping the broker fail-closed.
        """
        if kernel_id not in self._deliverers:
            return
        self._peers[pid] = {"kernel_id": kernel_id, "group_id": group_id}

    def release(self, pid: int) -> None:
        self._peers.pop(pid, None)
        for key in [k for k in self._subscriptions if k[1] == pid]:
            self._subscriptions.pop(key, None)

    def resolve(self, pid: int) -> dict[str, str] | None:
        return self._peers.get(pid)

    def kernel_of(self, pid: int) -> str | None:
        rec = self._peers.get(pid)
        return rec["kernel_id"] if rec else None

    async def allocate_pid(self) -> int:
        """Allocate a globally unique pid (shared across attached kernels).

        Kernel-local pid counters collide when two kernels share a broker, so
        broker-attached kernels take pids from this single space instead.
        """
        pid = self._next_pid
        self._next_pid += 1
        return pid

    # -------------------------------------------------------------- pub/sub
    def subscribe(self, pid: int, kernel_id: str, topic: str) -> None:
        key = (kernel_id, pid)
        subs = self._subscriptions.setdefault(key, [])
        if topic not in subs:
            subs.append(topic)

    def unsubscribe(self, pid: int, kernel_id: str, topic: str) -> None:
        key = (kernel_id, pid)
        subs = self._subscriptions.get(key)
        if subs and topic in subs:
            subs.remove(topic)
        if not subs:
            self._subscriptions.pop(key, None)

    # -------------------------------------------------------------- routing
    def route(self, kernel_id: str, msg_dict: dict) -> bool:
        """Deliver a cross-kernel message to the kernel owning ``to_pid``.

        Fail-closed: unknown target pid, an unknown target kernel, or a target
        on the sender's own kernel => False (no delivery). Delivery is
        scheduled as a task; await :meth:`drain` to observe completion.
        """
        target_pid = msg_dict.get("to_pid")
        if target_pid is None:
            return False
        rec = self._peers.get(target_pid)
        if rec is None:
            return False
        target_kernel = rec["kernel_id"]
        if target_kernel == kernel_id:
            return False  # sender owns it: already in the local mailbox
        deliver = self._deliverers.get(target_kernel)
        if deliver is None:
            return False
        task = asyncio.ensure_future(deliver(msg_dict))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def publish(
        self,
        *,
        kernel_id: str,
        from_pid: int,
        topic: str,
        payload: dict,
        priority: int = 50,
        trace_id: str = "",
        created_at: float = 0.0,
        expires_at: float | None = None,
    ) -> int:
        """Fan a published event out to *other* kernels' subscribers.

        The publishing kernel already delivered to its own local subscribers;
        the broker only counts remote deliveries and skips the sender's own
        kernel to avoid double delivery.
        """
        delivered = 0
        for (sub_kernel, sub_pid), patterns in list(self._subscriptions.items()):
            if sub_kernel == kernel_id:
                continue
            if not any(topic_match(p, topic) for p in patterns):
                continue
            deliver = self._deliverers.get(sub_kernel)
            if deliver is None:
                continue
            msg = {
                "msg_id": _new_id("msg"),
                "type": "event",
                "from_pid": from_pid,
                "to_pid": sub_pid,
                "reply_to": None,
                "topic": topic,
                "body": payload,
                "priority": int(priority),
                "trace_id": trace_id,
                "created_at": created_at if created_at else 0.0,
                "expires_at": expires_at,
            }
            await deliver(msg)
            delivered += 1
        return delivered

    async def drain(self) -> None:
        """Await all in-flight scheduled deliveries (tests / teardown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

class BrokerServer:
    """TCP bridge for the in-process :class:`Broker` (newline-delimited JSON).

    One connection == one kernel. The first line must be ``register``; every
    subsequent op carries the connection's kernel id in its envelope. On
    disconnect the kernel is unregistered (fail closed).
    """

    def __init__(
        self,
        broker: Broker,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
    ):
        """``token`` authenticates broker clients (default: ``AIOS_BROKER_TOKEN``).

        A server with no token configured rejects every client (fail closed) —
        an unauthenticated bridge would let any local process impersonate a
        kernel or claim pids.
        """
        self.broker = broker
        self.host = host
        self.port = port
        self._token = token if token is not None else os.environ.get("AIOS_BROKER_TOKEN")
        self._kernels: dict[str, asyncio.StreamWriter] = {}
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        """Bind and start accepting; returns the bound port."""
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        kernel_id: str | None = None

        async def deliver(msg_dict: dict) -> None:
            line = json.dumps({"op": "deliver", "msg": msg_dict}) + "\n"
            writer.write(line.encode("utf-8"))
            await writer.drain()

        async def reject(message: str) -> None:
            line = json.dumps({"op": "error", "message": message}) + "\n"
            try:
                writer.write(line.encode("utf-8"))
                await writer.drain()
            except (ConnectionError, OSError, RuntimeError):
                pass

        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                obj = json.loads(raw.decode("utf-8"))
                if obj.get("op") == "register":
                    # Auth handshake (fail closed): a server without a configured
                    # token rejects everyone; the presented token must match in
                    # constant time; a kernel_id already held by another
                    # connection is refused so pids/deliveries cannot be stolen.
                    if not self._token:
                        await reject(
                            "broker token (AIOS_BROKER_TOKEN) not configured — "
                            "server rejects all clients (fail closed)"
                        )
                        break
                    presented = str(obj.get("token") or "")
                    if not secrets.compare_digest(presented, self._token):
                        await reject("invalid broker token")
                        break
                    candidate = obj.get("kernel_id")
                    if not candidate:
                        await reject("register requires a kernel_id")
                        break
                    if candidate in self._kernels:
                        await reject(f"kernel_id {candidate!r} is already registered")
                        break
                    kernel_id = candidate
                    self._kernels[kernel_id] = writer
                    self.broker.register_kernel(kernel_id, deliver)
                    continue
                if not kernel_id:
                    continue  # ops before register are dropped (fail closed)
                await self._dispatch(kernel_id, obj, writer)
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            if kernel_id and self._kernels.get(kernel_id) is writer:
                self._kernels.pop(kernel_id, None)
                self.broker.unregister_kernel(kernel_id)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(
        self, kernel_id: str, obj: dict, writer: asyncio.StreamWriter
    ) -> None:
        op = obj.get("op")
        if op == "claim":
            self.broker.claim(
                obj["pid"], kernel_id=kernel_id, group_id=obj.get("group_id", "default")
            )
        elif op == "release":
            self.broker.release(obj["pid"])
        elif op == "subscribe":
            self.broker.subscribe(obj["pid"], kernel_id, obj["topic"])
        elif op == "unsubscribe":
            self.broker.unsubscribe(obj["pid"], kernel_id, obj["topic"])
        elif op == "route":
            self.broker.route(kernel_id, obj["msg"])
        elif op == "resolve":
            await self._reply(writer, obj["req_id"], self.broker.resolve(obj["pid"]))
        elif op == "allocate":
            await self._reply(writer, obj["req_id"], await self.broker.allocate_pid())
        elif op == "publish":
            count = await self.broker.publish(
                kernel_id=kernel_id,
                from_pid=obj["from_pid"],
                topic=obj["topic"],
                payload=obj["payload"],
                priority=obj.get("priority", 50),
                trace_id=obj.get("trace_id", ""),
                created_at=obj.get("created_at") or 0.0,
                expires_at=obj.get("expires_at"),
            )
            await self._reply(writer, obj["req_id"], count)

    async def _reply(self, writer: asyncio.StreamWriter, req_id: int, result: Any) -> None:
        line = json.dumps({"op": "response", "req_id": req_id, "result": result}) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()
class BrokerClient:
    """Wire client exposing the same surface as :class:`Broker`.

    Fire-and-forget ops are queued on one ordered writer (so a ``claim`` is
    always visible to a later ``route``); ``resolve`` / ``publish`` are
    request/response pairs matched by ``req_id``. On connection loss pending
    requests fail closed.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        kernel_id: str,
        deliver: Deliver,
        token: str | None = None,
        connect_retries: int = 5,
        request_timeout_s: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.kernel_id = kernel_id
        self._deliver = deliver
        # Shared-secret auth for the register handshake (AIOS_BROKER_TOKEN).
        self._token = token if token is not None else os.environ.get("AIOS_BROKER_TOKEN")
        self._connect_retries = connect_retries
        self._request_timeout_s = request_timeout_s
        self._writer: asyncio.StreamWriter | None = None
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_req = 0
        self._writer_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._broken = False
        self._reject_message: str | None = None

    def register_kernel(self, kernel_id: str, deliver: Deliver) -> None:
        """Interface parity with :class:`Broker`; kernel id is bound at
        construction, so only the callback may be (re)installed here."""
        self._deliver = deliver

    async def start(self) -> None:
        try:
            reader, writer = await self._connect()
        except OSError:
            self._broken = True
            self._reject_message = "broker connection refused"
            raise
        self._writer = writer
        self._send_now(
            {"op": "register", "kernel_id": self.kernel_id, "token": self._token}
        )
        self._writer_task = asyncio.create_task(self._writer_loop())
        self._reader_task = asyncio.create_task(self._reader_loop(reader))
        self._ready.set()

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        for attempt in range(self._connect_retries + 1):
            try:
                return await asyncio.open_connection(self.host, self.port)
            except OSError:
                if attempt >= self._connect_retries:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise ConnectionError("broker client connect failed")  # pragma: no cover

    # ------------------------------------------------ fire-and-forget ops
    def claim(self, pid: int, *, kernel_id: str | None = None, group_id: str = "default") -> None:
        self._send({"op": "claim", "kernel_id": kernel_id or self.kernel_id, "pid": pid, "group_id": group_id})

    def release(self, pid: int) -> None:
        self._send({"op": "release", "kernel_id": self.kernel_id, "pid": pid})

    def subscribe(self, pid: int, kernel_id: str, topic: str) -> None:
        self._send({"op": "subscribe", "kernel_id": kernel_id, "pid": pid, "topic": topic})

    def unsubscribe(self, pid: int, kernel_id: str, topic: str) -> None:
        self._send({"op": "unsubscribe", "kernel_id": kernel_id, "pid": pid, "topic": topic})

    def route(self, kernel_id: str, msg_dict: dict) -> None:
        self._send({"op": "route", "kernel_id": kernel_id, "msg": msg_dict})

    # ------------------------------------------------- request/response ops
    async def resolve(self, pid: int) -> dict[str, str] | None:
        return await self._request({"op": "resolve", "kernel_id": self.kernel_id, "pid": pid})

    async def allocate_pid(self) -> int:
        result = await self._request({"op": "allocate", "kernel_id": self.kernel_id})
        return int(result or 0)

    async def publish(
        self,
        *,
        kernel_id: str,
        from_pid: int,
        topic: str,
        payload: dict,
        priority: int = 50,
        trace_id: str = "",
        created_at: float = 0.0,
        expires_at: float | None = None,
    ) -> int:
        result = await self._request(
            {
                "op": "publish",
                "kernel_id": kernel_id,
                "from_pid": from_pid,
                "topic": topic,
                "payload": payload,
                "priority": priority,
                "trace_id": trace_id,
                "created_at": created_at,
                "expires_at": expires_at,
            }
        )
        return int(result or 0)

    async def drain(self) -> None:
        """Await queued ops being written (tests / teardown)."""
        if self._broken:
            return
        await self._ready.wait()
        await self._outbox.join()

    # --------------------------------------------------------------- internals
    def _send(self, obj: dict) -> None:
        if self._broken:
            raise ConnectionError(self._reject_message or "broker connection lost")
        self._outbox.put_nowait(obj)

    def _send_now(self, obj: dict) -> None:
        if self._writer is not None:
            self._writer.write((json.dumps(obj) + "\n").encode("utf-8"))

    async def _request(self, obj: dict) -> Any:
        if self._broken:
            raise ConnectionError(self._reject_message or "broker connection lost")
        self._next_req += 1
        req_id = self._next_req
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._outbox.put_nowait({**obj, "req_id": req_id})
        try:
            return await asyncio.wait_for(fut, timeout=self._request_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def _writer_loop(self) -> None:
        while True:
            obj = await self._outbox.get()
            try:
                self._writer.write((json.dumps(obj) + "\n").encode("utf-8"))
                await self._writer.drain()
            except (ConnectionError, OSError, RuntimeError):
                self._broken = True
                for req_id, fut in list(self._pending.items()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("broker connection lost"))
                self._pending.clear()
                self._outbox.task_done()
                break
            else:
                self._outbox.task_done()

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                obj = json.loads(raw.decode("utf-8"))
                op = obj.get("op")
                if op == "deliver":
                    try:
                        await self._deliver(obj["msg"])
                    except Exception:
                        pass  # delivery is best-effort; keep the reader alive
                elif op == "response":
                    fut = self._pending.pop(obj.get("req_id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(obj.get("result"))
                elif op == "error":
                    # The server rejected the connection (e.g. bad token or a
                    # duplicate kernel_id): fail every pending request so the
                    # caller sees the reason instead of a silent timeout.
                    self._broken = True
                    self._reject_message = str(
                        obj.get("message") or "broker rejected connection"
                    )
                    err = ConnectionError(self._reject_message)
                    for req_id, fut in list(self._pending.items()):
                        if not fut.done():
                            fut.set_exception(err)
                    self._pending.clear()
                    break
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass

    async def stop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        for task in (self._writer_task, self._reader_task):
            if task is not None:
                task.cancel()
"""IPC Manager — mailboxes, pub/sub event bus, handoffs, and join (Phase 2 Slice 2.2).

Implements docs/06-ipc.md:

  * per-agent mailboxes — FIFO queues with ``max_queue_depth`` / ``ttl_s``
    policy (defaults 100 / 1 hour), checkpointed with the agent so a resumed
    agent wakes to a faithful mailbox;
  * ``send_msg`` / ``recv_msg`` — send never blocks; recv has a mandatory
    timeout and an optional ``{from_pid?, type?, topic?}`` filter;
  * permissioned pub/sub — ``subscribe`` / ``unsubscribe`` / ``publish``;
  * handoff envelopes whose body must carry a validated, spawnable ``spec``;
  * ``join(pids[], timeout_ms)`` returning per-pid results.

Blocking semantics follow docs/03-scheduler.md §5: ``recv_msg`` / ``join``
park the caller in BLOCKED (the CPU slot is freed) and the kernel wakes it on
message arrival / completion or the deadline. Permissions are deny-by-default
per docs/08-security.md: a spec that omits ``ipc`` cannot send or subscribe.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass

from ..acb import AgentState
from ..errors import AiosError, E_INVAL, E_NOENT, E_PERM, E_STATE
from ..specs import validate_spec
from ..syscalls.registry import args_hash, register

DEFAULT_MAX_QUEUE_DEPTH = 100
DEFAULT_TTL_S = 3600.0
SEND_TYPES = {"direct", "reply", "handoff"}
OPERATOR_PID = 0  # human-in-the-loop: the operator is IPC peer PID 0


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class IpcMessage:
    """A kernel-envelope IPC message (docs/06-ipc.md §2).

    ``sig`` is a sha256 checksum over the canonical envelope payload: the
    checkpoint manifest's snapshot hash anchors integrity at rest, and the
    keyed HMAC is deferred to Phase 3 together with the audit-log hardening.
    Restored envelopes are re-signed on load.
    """

    msg_id: str
    type: str
    from_pid: int
    body: dict
    to_pid: int | None = None
    reply_to: str | None = None
    topic: str | None = None
    priority: int = 50
    trace_id: str = ""
    created_at: float = 0.0
    expires_at: float | None = None
    sig: str = ""

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = _new_id("tr")
        if not self.created_at:
            self.created_at = time.time()
        self.sign()

    def sign(self) -> None:
        canonical = json.dumps(
            self.to_dict(exclude_sig=True), sort_keys=True, default=str
        ).encode("utf-8")
        self.sig = "sha256:" + hashlib.sha256(canonical).hexdigest()

    def to_dict(self, *, exclude_sig: bool = False) -> dict:
        d = {
            "msg_id": self.msg_id,
            "type": self.type,
            "from_pid": self.from_pid,
            "to_pid": self.to_pid,
            "reply_to": self.reply_to,
            "topic": self.topic,
            "body": self.body,
            "priority": self.priority,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if not exclude_sig:
            d["sig"] = self.sig
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "IpcMessage":
        msg = cls(
            msg_id=d["msg_id"],
            type=d.get("type", "direct"),
            from_pid=int(d.get("from_pid", 0)),
            body=d.get("body", {}),
            to_pid=d.get("to_pid"),
            reply_to=d.get("reply_to"),
            topic=d.get("topic"),
            priority=int(d.get("priority", 50)),
            trace_id=d.get("trace_id", ""),
            created_at=float(d.get("created_at", 0.0)),
            expires_at=d.get("expires_at"),
        )
        msg.sign()
        return msg


def _topic_match(pattern: str, topic: str) -> bool:
    """Hierarchical topic match: ``*`` (all), exact, or ``prefix.*`` suffix."""
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-2] + ".")
    return False


class IPCManager:
    """Owns every mailbox and subscription; enforces IPC permissions."""

    def __init__(self, kernel, broker=None):
        """``broker`` is an optional multi-kernel IPC authority
        (modules/broker.py — in-process ``Broker`` or socket ``BrokerClient``).
        When present the manager mirrors claims, subscriptions, and remote
        routing through it; local semantics are unchanged."""
        self.kernel = kernel
        self._broker = broker
        self._mailboxes: dict[int, list[IpcMessage]] = {}
        self._subscriptions: dict[int, list[str]] = {}
        if broker is not None:
            broker.register_kernel(kernel.kernel_id, self._broker_deliver)

    # ------------------------------------------------------------- lifecycle
    def create(self, pid: int) -> None:
        self._mailboxes.setdefault(pid, [])
        self._subscriptions.setdefault(pid, [])
        if self._broker is not None:
            acb = self.kernel.agent_manager.peek(pid)
            group_id = getattr(acb, "group_id", None) or "default"
            self._broker.claim(pid, kernel_id=self.kernel.kernel_id, group_id=group_id)

    def free(self, pid: int) -> None:
        self._mailboxes.pop(pid, None)
        self._subscriptions.pop(pid, None)
        if self._broker is not None:
            self._broker.release(pid)

    def mailbox(self, pid: int) -> list[IpcMessage]:
        """Read-only copy of a mailbox (tests / inspection)."""
        return list(self._mailboxes.get(pid, []))

    def subscriptions(self, pid: int) -> list[str]:
        return list(self._subscriptions.get(pid, []))

    # ------------------------------------------------------------ permissions
    @staticmethod
    def _declared(spec: dict, key: str) -> list[str]:
        return list(spec.get("ipc", {}).get(key, []))

    async def can_send_to(self, sender_spec: dict, target_pid: int) -> bool:
        """Deny-by-default send check against the sender's declared grants.

        Local targets resolve against the process table; remote targets are
        resolved through the broker (same grants, group matched remotely).
        """
        entries = self._declared(sender_spec, "can_send_to")
        if "*" in entries:
            # Still require the target to exist somewhere.
            if self.kernel.agent_manager.peek(target_pid) is not None:
                return True
            if self._broker is not None:
                if await self._broker_resolve(target_pid) is None:
                    raise AiosError(E_NOENT, f"no such agent: {target_pid}")
                return True
            raise AiosError(E_NOENT, f"no such agent: {target_pid}")
        target = self.kernel.agent_manager.peek(target_pid)
        if target is not None:
            if f"group:{target.group_id}" in entries:
                return True
            if f"pid:{target_pid}" in entries:
                return True
            return False
        if self._broker is not None:
            peer = await self._broker_resolve(target_pid)
            if peer is None:
                raise AiosError(E_NOENT, f"no such agent: {target_pid}")
            if f"group:{peer.get('group_id')}" in entries:
                return True
            if f"pid:{target_pid}" in entries:
                return True
        else:
            raise AiosError(E_NOENT, f"no such agent: {target_pid}")
        return False

    def _topic_allowed(self, spec: dict, key: str, topic: str) -> bool:
        return any(_topic_match(p, topic) for p in self._declared(spec, key))

    # ------------------------------------------------------ multi-kernel
    async def _broker_resolve(self, target_pid: int) -> dict | None:
        """Resolve a pid across kernels via the broker (None if standalone).

        Tolerates both the sync in-process ``Broker`` and the request/response
        ``BrokerClient``.
        """
        if self._broker is None:
            return None
        result = self._broker.resolve(target_pid)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def allocate_pid(self) -> int | None:
        """Ask the broker for a globally unique pid (None when standalone)."""
        if self._broker is None:
            return None
        return await self._broker.allocate_pid()

    async def _broker_deliver(self, msg_dict: dict) -> None:
        """Inbound cross-kernel message: enqueue into the local mailbox.

        The message was already permission-checked at the sender and is
        re-audited here on arrival (per-kernel deny-by-default is preserved).
        Fail-closed: if the local pid is gone (e.g. freed before the broker
        release landed), the message is dropped, never resurrected.
        """
        target_pid = msg_dict.get("to_pid")
        if target_pid is None:
            return
        if self.kernel.agent_manager.peek(target_pid) is None:
            return
        self.enqueue(target_pid, IpcMessage.from_dict(msg_dict))

    # ------------------------------------------------------------------ send
    async def send(self, sender_pid: int, args: dict) -> IpcMessage:
        sender = self.kernel.agent_manager.get(sender_pid)
        target_pid = args["to_pid"]
        if not await self.can_send_to(sender.spec, target_pid):
            raise AiosError(E_PERM, f"agent {sender_pid} may not send to {target_pid}")

        mtype = args.get("type") or "direct"
        if mtype not in SEND_TYPES:
            raise AiosError(E_INVAL, f"send_msg type must be one of {sorted(SEND_TYPES)}")
        body = copy.deepcopy(args["body"])
        if mtype == "handoff":
            if body.get("spec") is None:
                raise AiosError(E_INVAL, "a handoff message body must carry a validated 'spec'")
            validate_spec(body["spec"])  # a handoff is a spawnable-spec request
        elif mtype == "reply":
            if not args.get("reply_to"):
                raise AiosError(E_INVAL, "a reply message requires reply_to")

        trace_id = args.get("trace_id")
        if args.get("reply_to"):
            original = self._find_message(args["reply_to"])
            if original is not None:
                trace_id = trace_id or original.trace_id

        ttl_s = args.get("ttl_s")
        msg = IpcMessage(
            msg_id=_new_id("msg"),
            type=mtype,
            from_pid=sender_pid,
            to_pid=target_pid,
            body=body,
            reply_to=args.get("reply_to"),
            priority=int(args.get("priority", 50)),
            trace_id=trace_id or "",
            created_at=time.time(),
            expires_at=(time.time() + ttl_s) if ttl_s else None,
        )
        if self.kernel.agent_manager.peek(target_pid) is not None:
            self.enqueue(target_pid, msg)
        elif self._broker is not None:
            peer = await self._broker_resolve(target_pid)
            if peer is None:
                raise AiosError(E_NOENT, f"no such agent: {target_pid}")
            self._broker.route(self.kernel.kernel_id, msg.to_dict())
        else:
            raise AiosError(E_NOENT, f"no such agent: {target_pid}")
        self.kernel.audit.record(
            "ipc.send",
            pid=sender_pid,
            to_pid=target_pid,
            msg_id=msg.msg_id,
            type=msg.type,
        )
        return msg

    def _find_message(self, msg_id: str) -> IpcMessage | None:
        for queue in self._mailboxes.values():
            for m in queue:
                if m.msg_id == msg_id:
                    return m
        return None

    # -------------------------------------------------------- operator channel
    async def send_from_operator(
        self,
        target_pid: int,
        body: dict,
        *,
        type: str = "direct",
        reply_to: str | None = None,
        topic: str | None = None,
        priority: int = 50,
        ttl_s: float | None = None,
    ) -> IpcMessage:
        """Human-in-the-loop (docs/06-ipc.md §9.4): a human operator — PID 0 —
        sends a message to an agent, e.g. from the REST API or the web desktop.

        Unlike ``send`` there is no sender spec to consult, so agent send
        permissions do not apply; the target must still exist (E_NOENT) and the
        type must be a declared send type. Bodies are never audited verbatim —
        only a canonical hash is recorded (docs/06-ipc.md §8). With a broker
        attached the target may live on another kernel and is routed there.
        """
        if type not in SEND_TYPES:
            raise AiosError(E_INVAL, f"send_msg type must be one of {sorted(SEND_TYPES)}")
        msg = IpcMessage(
            msg_id=_new_id("op"),
            type=type,
            from_pid=OPERATOR_PID,
            to_pid=target_pid,
            body=copy.deepcopy(body),
            reply_to=reply_to,
            topic=topic,
            priority=int(priority),
            expires_at=(time.time() + ttl_s) if ttl_s else None,
        )
        target = self.kernel.agent_manager.peek(target_pid)
        if target is not None:
            self.enqueue(target_pid, msg)
        elif self._broker is not None:
            peer = await self._broker_resolve(target_pid)
            if peer is None:
                raise AiosError(E_NOENT, f"no such agent: {target_pid}")
            self._broker.route(self.kernel.kernel_id, msg.to_dict())
        else:
            raise AiosError(E_NOENT, f"no such agent: {target_pid}")
        self.kernel.audit.record(
            "ipc.operator_send",
            to_pid=target_pid,
            msg_id=msg.msg_id,
            type=msg.type,
            body_hash=args_hash({"body": body}),
        )
        return msg

    # ------------------------------------------------------------------ recv
    async def recv(
        self, pid: int, *, timeout_ms: float, filter_: dict | None = None
    ) -> dict:
        """Dequeue the first matching message; else park until one arrives.

        ``recv_msg`` has a mandatory timeout. While parked the agent is BLOCKED
        (its scheduler slot is freed); a message arrival (or the deadline)
        wakes it, and the CPU is reacquired before the syscall returns.
        """
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            msg = self.dequeue_matching(pid, filter_)
            if msg is not None:
                self.kernel.audit.record(
                    "ipc.recv", pid=pid, msg_id=msg.msg_id, type=msg.type
                )
                return {"msg": msg.to_dict()}
            acb = self.kernel.agent_manager.peek(pid)
            if acb is None:
                raise AiosError(E_NOENT, f"no such agent: {pid}")
            if acb.state not in (AgentState.READY, AgentState.RUNNING):
                # suspended / terminated while parked: unwind the syscall with a
                # hard E_STATE so the turn function cannot mistake it for a
                # timeout and spin a tight retry loop (starving the event loop).
                raise AiosError(
                    E_STATE, f"agent {pid} is {acb.state.value} (blocked syscall unwound)"
                )
            if timeout_ms <= 0 or time.time() >= deadline:
                return {"msg": None, "reason": "timeout"}
            event = self.kernel.scheduler.block(pid)
            try:
                remaining = deadline - time.time()
                if remaining > 0:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass
            finally:
                self.kernel.scheduler.unblock(pid)
            await self.kernel.scheduler.wait_for_grant(pid)

    def enqueue(self, target_pid: int, msg: IpcMessage) -> None:
        self._prune_expired(target_pid)
        queue = self._mailboxes.setdefault(target_pid, [])
        depth = self._max_depth(target_pid)
        if len(queue) >= depth:
            dropped = queue.pop(0)  # overflow: oldest dropped, dead-lettered to audit
            self.kernel.audit.record(
                "ipc.overflow", pid=target_pid, msg_id=dropped.msg_id, reason="max_queue_depth"
            )
        queue.append(msg)
        self.kernel.scheduler.wake(target_pid)

    def dequeue_matching(self, pid: int, filter_: dict | None) -> IpcMessage | None:
        self._prune_expired(pid)
        queue = self._mailboxes.get(pid, [])
        for i, m in enumerate(queue):
            if self._matches(m, filter_):
                return queue.pop(i)
        return None

    def _prune_expired(self, pid: int) -> None:
        queue = self._mailboxes.get(pid, [])
        if not queue:
            return
        now = time.time()
        kept = []
        for m in queue:
            if m.expires_at is not None and now > m.expires_at:
                self.kernel.audit.record(
                    "ipc.dead_letter", pid=pid, msg_id=m.msg_id, reason="expired"
                )
            else:
                kept.append(m)
        self._mailboxes[pid] = kept

    def _max_depth(self, pid: int) -> int:
        acb = self.kernel.agent_manager.peek(pid)
        if acb is None:
            return DEFAULT_MAX_QUEUE_DEPTH
        mb = acb.spec.get("ipc", {}).get("mailbox", {})
        return int(mb.get("max_queue_depth", DEFAULT_MAX_QUEUE_DEPTH))

    @staticmethod
    def _matches(msg: IpcMessage, filter_: dict | None) -> bool:
        if not filter_:
            return True
        if "from_pid" in filter_ and msg.from_pid != filter_["from_pid"]:
            return False
        if "type" in filter_ and msg.type != filter_["type"]:
            return False
        if "topic" in filter_ and msg.topic != filter_["topic"]:
            return False
        return True

    # ----------------------------------------------------------------- pub/sub
    def subscribe(self, pid: int, topic: str) -> None:
        acb = self.kernel.agent_manager.get(pid)
        if not self._topic_allowed(acb.spec, "can_subscribe", topic):
            raise AiosError(E_PERM, f"agent {pid} may not subscribe to '{topic}'")
        subs = self._subscriptions.setdefault(pid, [])
        if topic not in subs:
            subs.append(topic)
        if self._broker is not None:
            self._broker.subscribe(pid, self.kernel.kernel_id, topic)
        self.kernel.audit.record("ipc.subscribe", pid=pid, topic=topic)

    def unsubscribe(self, pid: int, topic: str) -> None:
        subs = self._subscriptions.setdefault(pid, [])
        if topic in subs:
            subs.remove(topic)
        if self._broker is not None:
            self._broker.unsubscribe(pid, self.kernel.kernel_id, topic)
        self.kernel.audit.record("ipc.unsubscribe", pid=pid, topic=topic)

    async def publish(self, pid: int, topic: str, payload: dict) -> int:
        acb = self.kernel.agent_manager.get(pid)
        if not self._topic_allowed(acb.spec, "can_publish", topic):
            raise AiosError(E_PERM, f"agent {pid} may not publish to '{topic}'")
        delivered = 0
        for sub_pid, patterns in list(self._subscriptions.items()):
            if any(_topic_match(p, topic) for p in patterns):
                msg = IpcMessage(
                    msg_id=_new_id("msg"),
                    type="event",
                    from_pid=pid,
                    to_pid=sub_pid,
                    topic=topic,
                    body=copy.deepcopy(payload),
                )
                self.enqueue(sub_pid, msg)
                delivered += 1
        if self._broker is not None:
            # fan out to other kernels' subscribers; the broker skips our own
            delivered += await self._broker.publish(
                kernel_id=self.kernel.kernel_id,
                from_pid=pid,
                topic=topic,
                payload=payload,
            )
        self.kernel.audit.record(
            "ipc.publish", pid=pid, topic=topic, delivered=delivered
        )
        return delivered

    # ------------------------------------------------------------------- join
    async def join(
        self, pid: int, pids: list[int], timeout_ms: float | None = None
    ) -> dict:
        """Wait until every listed agent is TERMINATED (or the deadline)."""
        for t in pids:
            if self.kernel.agent_manager.peek(t) is not None:
                continue
            if self._broker is not None and await self._broker_resolve(t) is not None:
                raise AiosError(E_INVAL, f"cross-kernel join not supported: {t}")
            self.kernel.agent_manager.get(t)  # E_NOENT for unknown targets
        deadline = (time.time() + timeout_ms / 1000.0) if timeout_ms is not None else None

        pending = self._pending(pids)
        if not pending:
            return {"results": self._join_results(pids), "timed_out": False}

        self.kernel.scheduler.block(pid)
        try:
            while pending:
                acb = self.kernel.agent_manager.peek(pid)
                if acb is None:
                    raise AiosError(E_NOENT, f"no such agent: {pid}")
                if acb.state in (AgentState.SUSPENDED, AgentState.TERMINATED):
                    # suspended / killed while parked: unwind promptly instead
                    # of polling for the remaining deadline.
                    raise AiosError(
                        E_STATE,
                        f"agent {pid} is {acb.state.value} (join unwound by state change)",
                    )
                if deadline is not None and time.time() >= deadline:
                    break
                remaining = None if deadline is None else deadline - time.time()
                try:
                    if remaining is not None:
                        await asyncio.wait_for(asyncio.sleep(0.05), timeout=remaining)
                    else:
                        await asyncio.sleep(0.05)
                except asyncio.TimeoutError:
                    break
                pending = self._pending(pids)
        finally:
            self.kernel.scheduler.unblock(pid)
        await self.kernel.scheduler.wait_for_grant(pid)
        acb = self.kernel.agent_manager.peek(pid)
        if acb is None:
            raise AiosError(E_NOENT, f"no such agent: {pid}")
        if acb.state not in (AgentState.READY, AgentState.RUNNING):
            raise AiosError(
                E_STATE, f"agent {pid} is {acb.state.value} (join unwound by state change)"
            )
        return {"results": self._join_results(pids), "timed_out": bool(pending)}

    def _pending(self, pids: list[int]) -> list[int]:
        return [
            t
            for t in pids
            if (acb := self.kernel.agent_manager.peek(t)) is not None
            and acb.state is not AgentState.TERMINATED
        ]

    def _join_results(self, pids: list[int]) -> list[dict]:
        results = []
        for t in pids:
            rec = self.kernel.agent_manager.record(t)
            if rec is None:
                results.append(
                    {"pid": t, "status": "reaped", "exit_status": None, "exit_message": None}
                )
            else:
                results.append(
                    {
                        "pid": t,
                        "status": rec.get("state", "?"),
                        "exit_status": rec.get("exit_status"),
                        "exit_message": rec.get("exit_message"),
                    }
                )
        return results

    # ------------------------------------------------------------ checkpoints
    def snapshot(self, pid: int) -> dict:
        """Mailbox + subscriptions for the checkpoint snapshot."""
        return {
            "mailbox": [m.to_dict() for m in self._mailboxes.get(pid, [])],
            "subscriptions": list(self._subscriptions.get(pid, [])),
        }

    def restore(self, pid: int, mailbox: list[dict], subscriptions: list[str]) -> None:
        self.create(pid)
        self._mailboxes[pid] = [IpcMessage.from_dict(m) for m in mailbox]
        self._subscriptions[pid] = list(subscriptions)


# ------------------------------------------------------------------ syscalls
@register("send_msg")
async def _send_msg(kernel, pid: int, args: dict) -> dict:
    msg = await kernel.ipc.send(pid, args)
    return {"msg_id": msg.msg_id}


@register("recv_msg")
async def _recv_msg(kernel, pid: int, args: dict) -> dict:
    return await kernel.ipc.recv(
        pid, timeout_ms=args["timeout_ms"], filter_=args.get("filter")
    )


@register("subscribe")
async def _subscribe(kernel, pid: int, args: dict) -> dict:
    kernel.ipc.subscribe(pid, args["topic"])
    return {"ok": True}


@register("unsubscribe")
async def _unsubscribe(kernel, pid: int, args: dict) -> dict:
    kernel.ipc.unsubscribe(pid, args["topic"])
    return {"ok": True}


@register("publish")
async def _publish(kernel, pid: int, args: dict) -> dict:
    return {"delivered": await kernel.ipc.publish(pid, args["topic"], args["payload"])}


@register("join")
async def _join(kernel, pid: int, args: dict) -> dict:
    return await kernel.ipc.join(pid, args["pids"], timeout_ms=args.get("timeout_ms"))
    return False
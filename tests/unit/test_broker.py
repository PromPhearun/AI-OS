"""Unit tests: multi-kernel IPC broker (Phase 5, Slice 5.4).

Covers the in-process ``Broker`` authority (pid registry, fail-closed routing,
cross-kernel pub/sub fan-out), the ``BrokerServer``/``BrokerClient`` socket
transport, and the wiring into two kernels sharing one broker.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from aios_kernel import Kernel
from aios_kernel.errors import AiosError
from aios_kernel.modules.broker import Broker, BrokerClient, BrokerServer, topic_match
from aios_kernel.modules.llm_core import MockLLM

from ..conftest import _base_spec


def _ipc_spec(**overrides) -> dict:
    spec = _base_spec(
        ipc={
            "can_send_to": ["*"],
            "can_subscribe": ["*"],
            "can_publish": ["*"],
            "mailbox": {"max_queue_depth": 100, "ttl_s": 3600},
        }
    )
    spec.update(overrides)
    return spec


async def _noop_deliver(msg_dict: dict) -> None:
    return None


def _collector(inbox: dict, key: str):
    async def _collect(msg_dict: dict) -> None:
        inbox[key].append(msg_dict)

    return _collect


async def _await_inbox(inbox: dict, key: str, n: int, timeout: float = 2.0) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(inbox[key]) >= n:
            return inbox[key]
        await asyncio.sleep(0.01)
    return inbox[key]


async def _await_resolve(client: BrokerClient, pid: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        peer = await client.resolve(pid)
        if peer is not None:
            return peer
        await asyncio.sleep(0.01)
    return None


# --------------------------------------------------------------- topic match
def test_topic_match() -> None:
    assert topic_match("*", "a.b.c")
    assert topic_match("alerts", "alerts")
    assert topic_match("alerts.*", "alerts.denied")
    assert not topic_match("alerts.*", "alerts")
    assert not topic_match("alerts", "alerts.denied")


# ------------------------------------------------------------- broker: pids
def test_broker_claim_resolve_release() -> None:
    broker = Broker(broker_id="b1")
    broker.register_kernel("k1", _noop_deliver)
    broker.claim(11, kernel_id="k1", group_id="grp")
    assert broker.resolve(11) == {"kernel_id": "k1", "group_id": "grp"}
    assert broker.kernel_of(11) == "k1"
    broker.release(11)
    assert broker.resolve(11) is None
    assert broker.kernel_of(11) is None


def test_broker_claim_ignored_for_unregistered_kernel() -> None:
    broker = Broker()
    broker.claim(11, kernel_id="ghost")  # never registered => fail closed
    assert broker.resolve(11) is None


def test_broker_resolve_unknown_pid_is_none() -> None:
    broker = Broker()
    broker.register_kernel("k1", _noop_deliver)
    assert broker.resolve(999) is None
# -------------------------------------------------------------- broker: route
@pytest.mark.asyncio
async def test_broker_routes_cross_kernel_delivery() -> None:
    broker = Broker()
    received: dict[str, list] = {"k1": [], "k2": [], "k3": []}
    broker.register_kernel("k1", _collector(received, "k1"))
    broker.register_kernel("k2", _collector(received, "k2"))
    broker.register_kernel("k3", _collector(received, "k3"))
    broker.claim(11, kernel_id="k1", group_id="g1")
    broker.claim(21, kernel_id="k2", group_id="g2")
    msg = {"msg_id": "m1", "type": "direct", "from_pid": 11, "to_pid": 21, "body": {"x": 1}}
    assert broker.route("k1", msg) is True
    await broker.drain()
    assert received["k2"] == [msg]
    assert received["k3"] == []


@pytest.mark.asyncio
async def test_broker_route_fails_closed() -> None:
    broker = Broker()
    broker.register_kernel("k1", _noop_deliver)
    broker.register_kernel("k2", _noop_deliver)
    msg = {"msg_id": "m1", "from_pid": 11, "to_pid": 21, "body": {}}
    assert broker.route("k1", msg) is False  # pid 21 not claimed yet
    assert broker.route("k1", {}) is False  # missing to_pid
    broker.claim(21, kernel_id="k2", group_id="g2")
    assert broker.route("k1", msg) is True
    broker.claim(11, kernel_id="k1", group_id="g1")
    assert broker.route("k1", {"msg_id": "m2", "from_pid": 11, "to_pid": 11, "body": {}}) is False
    broker.release(21)
    assert broker.route("k1", msg) is False  # released => fail closed


# -------------------------------------------------------------- broker: pub/sub
@pytest.mark.asyncio
async def test_broker_publish_fans_out_to_other_kernels() -> None:
    broker = Broker()
    received: dict[str, list] = {"k1": [], "k2": [], "k3": []}
    broker.register_kernel("k1", _collector(received, "k1"))
    broker.register_kernel("k2", _collector(received, "k2"))
    broker.register_kernel("k3", _collector(received, "k3"))
    broker.subscribe(21, "k2", "alerts.*")
    broker.subscribe(22, "k2", "alerts.denied")
    broker.subscribe(31, "k3", "other")  # no match
    count = await broker.publish(
        kernel_id="k1", from_pid=11, topic="alerts.denied", payload={"sev": 5}
    )
    assert count == 2
    events = received["k2"]
    assert [e["to_pid"] for e in events] == [21, 22]
    assert all(e["type"] == "event" for e in events)
    assert events[0]["topic"] == "alerts.denied"
    assert events[0]["body"] == {"sev": 5}
    assert events[0]["from_pid"] == 11
    assert received["k3"] == []


@pytest.mark.asyncio
async def test_broker_publish_skips_senders_own_kernel() -> None:
    broker = Broker()
    received: dict[str, list] = {"k1": []}
    broker.register_kernel("k1", _collector(received, "k1"))
    broker.register_kernel("k2", _noop_deliver)
    broker.subscribe(11, "k1", "alerts.*")
    broker.subscribe(21, "k2", "alerts.*")
    count = await broker.publish(kernel_id="k1", from_pid=11, topic="alerts.up", payload={})
    assert count == 1
    assert received["k1"] == []  # local delivery is the publisher's job


@pytest.mark.asyncio
async def test_broker_unsubscribe_and_release_drop_subscriptions() -> None:
    broker = Broker()
    broker.register_kernel("k1", _noop_deliver)
    broker.register_kernel("k2", _noop_deliver)
    broker.subscribe(21, "k2", "alerts.*")
    broker.unsubscribe(21, "k2", "alerts.*")
    assert await broker.publish(kernel_id="k1", from_pid=11, topic="alerts.x", payload={}) == 0
    broker.subscribe(22, "k2", "alerts.*")
    broker.release(22)
    assert await broker.publish(kernel_id="k1", from_pid=11, topic="alerts.x", payload={}) == 0


@pytest.mark.asyncio
async def test_broker_unregister_kernel_drops_peers_and_subs() -> None:
    broker = Broker()
    broker.register_kernel("k1", _noop_deliver)
    broker.register_kernel("k2", _noop_deliver)
    broker.claim(21, kernel_id="k2", group_id="g2")
    broker.subscribe(21, "k2", "alerts.*")
    broker.unregister_kernel("k2")
    assert broker.resolve(21) is None
    assert await broker.publish(kernel_id="k1", from_pid=11, topic="alerts.x", payload={}) == 0


# ---------------------------------------------------------- socket transport
@pytest.fixture
async def sock_bridge():
    """One BrokerServer with two connected BrokerClients (k1, k2)."""
    broker = Broker(broker_id="socket-broker")
    server = BrokerServer(broker, token="test-token")
    port = await server.start()
    inbox: dict[str, list] = {"k1": [], "k2": []}

    async def _client(kernel_id: str) -> BrokerClient:
        client = BrokerClient(
            "127.0.0.1",
            port,
            kernel_id=kernel_id,
            deliver=_collector(inbox, kernel_id),
            token="test-token",
            connect_retries=0,
        )
        await client.start()
        return client

    c1 = await _client("k1")
    c2 = await _client("k2")
    try:
        yield broker, c1, c2, inbox
    finally:
        await c1.stop()
        await c2.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_socket_claim_and_resolve_roundtrip(sock_bridge) -> None:
    _, c1, c2, _ = sock_bridge
    c1.claim(11, group_id="g1")
    await c1.drain()
    peer = await _await_resolve(c2, 11)
    assert peer == {"kernel_id": "k1", "group_id": "g1"}
    assert await c1.resolve(999) is None


@pytest.mark.asyncio
async def test_socket_release_removes_peer(sock_bridge) -> None:
    _, c1, c2, _ = sock_bridge
    c1.claim(11, group_id="g1")
    await c1.drain()
    assert await _await_resolve(c2, 11) is not None
    c1.release(11)
    await c1.drain()
    await asyncio.sleep(0.05)  # let the server process the release
    assert await c1.resolve(11) is None


@pytest.mark.asyncio
async def test_socket_route_delivers_to_remote_kernel(sock_bridge) -> None:
    _, c1, c2, inbox = sock_bridge
    c1.claim(11, group_id="g1")
    c2.claim(21, group_id="g2")
    await c1.drain()
    await c2.drain()
    msg = {"msg_id": "m-1", "type": "direct", "from_pid": 11, "to_pid": 21, "body": {"hi": 1}}
    c1.route("k1", msg)
    await c1.drain()
    got = await _await_inbox(inbox, "k2", 1)
    assert got[0]["to_pid"] == 21
    assert got[0]["body"] == {"hi": 1}
    assert got[0]["msg_id"] == "m-1"


@pytest.mark.asyncio
async def test_socket_route_unknown_pid_is_not_delivered(sock_bridge) -> None:
    _, c1, _, inbox = sock_bridge
    c1.route("k1", {"msg_id": "m-ghost", "from_pid": 11, "to_pid": 4242, "body": {}})
    await c1.drain()
    await asyncio.sleep(0.05)  # give the server a chance to (not) deliver
    assert inbox["k1"] == []
    assert inbox["k2"] == []


@pytest.mark.asyncio
async def test_socket_publish_reaches_remote_subscribers(sock_bridge) -> None:
    _, c1, c2, inbox = sock_bridge
    c2.claim(21, group_id="g2")
    c2.subscribe(21, "k2", "alerts.*")
    await c2.drain()
    count = await c1.publish(
        kernel_id="k1", from_pid=11, topic="alerts.up", payload={"sev": 3}
    )
    assert count == 1
    got = await _await_inbox(inbox, "k2", 1)
    assert got[0]["topic"] == "alerts.up"
    assert got[0]["body"] == {"sev": 3}
    assert got[0]["from_pid"] == 11


@pytest.mark.asyncio
async def test_socket_unsubscribe_stops_delivery(sock_bridge) -> None:
    _, c1, c2, inbox = sock_bridge
    c2.claim(21, group_id="g2")
    c2.subscribe(21, "k2", "alerts.*")
    await c2.drain()
    count = await c1.publish(kernel_id="k1", from_pid=11, topic="alerts.up", payload={})
    assert count == 1
    await _await_inbox(inbox, "k2", 1)
    c2.unsubscribe(21, "k2", "alerts.*")
    await c2.drain()
    await asyncio.sleep(0.05)
    count = await c1.publish(kernel_id="k1", from_pid=11, topic="alerts.up", payload={})
    assert count == 0


# --------------------------------------------------- socket auth (fail closed)
@pytest.mark.asyncio
async def test_socket_register_rejects_wrong_token(monkeypatch) -> None:
    """A client presenting the wrong token is refused at register (fail closed)."""
    monkeypatch.delenv("AIOS_BROKER_TOKEN", raising=False)
    broker = Broker()
    server = BrokerServer(broker, token="sekrit")
    port = await server.start()
    try:
        bad = BrokerClient(
            "127.0.0.1", port, kernel_id="k-bad", deliver=_noop_deliver, token="nope"
        )
        await bad.start()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(bad.resolve(1), timeout=1.0)
        await bad.stop()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_socket_register_requires_client_token(monkeypatch) -> None:
    """A client with no token cannot reach a token-protected server."""
    monkeypatch.delenv("AIOS_BROKER_TOKEN", raising=False)
    broker = Broker()
    server = BrokerServer(broker, token="sekrit")
    port = await server.start()
    try:
        bare = BrokerClient(
            "127.0.0.1", port, kernel_id="k-bare", deliver=_noop_deliver
        )
        await bare.start()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(bare.resolve(1), timeout=1.0)
        await bare.stop()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_socket_server_without_token_fails_closed(monkeypatch) -> None:
    """A server with no token configured rejects every client."""
    monkeypatch.delenv("AIOS_BROKER_TOKEN", raising=False)
    broker = Broker()
    server = BrokerServer(broker)  # no token and env unset -> rejects all
    port = await server.start()
    try:
        client = BrokerClient(
            "127.0.0.1", port, kernel_id="k", deliver=_noop_deliver, token="anything"
        )
        await client.start()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(client.resolve(1), timeout=1.0)
        await client.stop()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_socket_duplicate_kernel_id_rejected() -> None:
    """A second connection registering the same kernel_id is refused, and the
    first connection keeps working (no pid/delivery hijack)."""
    broker = Broker()
    server = BrokerServer(broker, token="sekrit")
    port = await server.start()
    try:
        c1 = BrokerClient(
            "127.0.0.1", port, kernel_id="dup", deliver=_noop_deliver, token="sekrit"
        )
        c2 = BrokerClient(
            "127.0.0.1", port, kernel_id="dup", deliver=_noop_deliver, token="sekrit"
        )
        await c1.start()
        await c2.start()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(c2.resolve(1), timeout=1.0)
        assert await c1.resolve(999) is None  # first connection unaffected
        await c1.stop()
        await c2.stop()
    finally:
        await server.stop()


# ------------------------------------------------- kernel wiring (in-process)
async def _two_kernels(tmp_path, monkeypatch) -> tuple[Broker, Kernel, Kernel, int, int]:
    monkeypatch.setenv("AIOS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    broker = Broker(broker_id="multi")
    k1 = Kernel(
        data_root=str(tmp_path / "k1"),
        llm_backend=MockLLM(mode="echo"),
        kernel_id="kern-a",
        broker=broker,
    )
    k2 = Kernel(
        data_root=str(tmp_path / "k2"),
        llm_backend=MockLLM(mode="echo"),
        kernel_id="kern-b",
        broker=broker,
    )
    try:
        pid_a = await k1.spawn_agent(_ipc_spec(name="a"))
        pid_b = await k2.spawn_agent(_ipc_spec(name="b"))
        await broker.drain()
        yield broker, k1, k2, pid_a, pid_b
    finally:
        await k1.shutdown()
        await k2.shutdown()


@pytest.mark.asyncio
async def test_two_kernels_route_send_via_broker(tmp_path, monkeypatch) -> None:
    async for broker, k1, k2, pid_a, pid_b in _two_kernels(tmp_path, monkeypatch):
        # pid_b is invisible to k1's process table but resolvable via broker
        assert k1.agent_manager.peek(pid_b) is None
        assert await k1.ipc.can_send_to(_ipc_spec(name="a"), pid_b) is True
        sent = await k1.ipc.send(
            pid_a, {"to_pid": pid_b, "body": {"text": "cross-kernel"}, "type": "direct"}
        )
        await broker.drain()
        mailbox = k2.ipc.mailbox(pid_b)
        assert len(mailbox) == 1
        assert mailbox[0].body == {"text": "cross-kernel"}
        assert mailbox[0].from_pid == pid_a
        assert mailbox[0].msg_id == sent.msg_id


@pytest.mark.asyncio
async def test_two_kernels_send_to_unknown_pid_fails(tmp_path, monkeypatch) -> None:
    async for broker, k1, k2, pid_a, pid_b in _two_kernels(tmp_path, monkeypatch):
        with pytest.raises(AiosError) as exc:
            await k1.ipc.send(pid_a, {"to_pid": 424242, "body": {}, "type": "direct"})
        assert exc.value.code == "E_NOENT"


@pytest.mark.asyncio
async def test_two_kernels_operator_send_routes_remote(tmp_path, monkeypatch) -> None:
    async for broker, k1, k2, pid_a, pid_b in _two_kernels(tmp_path, monkeypatch):
        msg = await k1.ipc.send_from_operator(pid_b, {"text": "operator override"}, priority=90)
        await broker.drain()
        mailbox = k2.ipc.mailbox(pid_b)
        assert len(mailbox) == 1
        assert mailbox[0].body == {"text": "operator override"}
        assert mailbox[0].priority == 90
        assert mailbox[0].from_pid == 0  # OPERATOR_PID
        assert msg.to_pid == pid_b


@pytest.mark.asyncio
async def test_two_kernels_publish_fans_out(tmp_path, monkeypatch) -> None:
    async for broker, k1, k2, pid_a, pid_b in _two_kernels(tmp_path, monkeypatch):
        k2.ipc.subscribe(pid_b, "alerts.*")
        n = await k1.ipc.publish(pid_a, "alerts.up", {"sev": "low"})
        await broker.drain()
        assert n == 1  # one remote subscriber; the broker skips k1 itself
        mailbox = k2.ipc.mailbox(pid_b)
        assert len(mailbox) == 1
        assert mailbox[0].topic == "alerts.up"
        assert mailbox[0].from_pid == pid_a


@pytest.mark.asyncio
async def test_two_kernels_cross_kernel_join_rejected(tmp_path, monkeypatch) -> None:
    async for broker, k1, k2, pid_a, pid_b in _two_kernels(tmp_path, monkeypatch):
        with pytest.raises(AiosError) as exc:
            await k1.ipc.join(pid_a, [pid_b])
        assert exc.value.code == "E_INVAL"


@pytest.mark.asyncio
async def test_two_kernels_free_releases_broker_claim(tmp_path, monkeypatch) -> None:
    async for broker, k1, k2, pid_a, pid_b in _two_kernels(tmp_path, monkeypatch):
        assert broker.resolve(pid_a) == {"kernel_id": "kern-a", "group_id": "test"}
        k1.ipc.free(pid_a)
        assert broker.resolve(pid_a) is None
        with pytest.raises(AiosError) as exc:
            await k2.ipc.send(pid_b, {"to_pid": pid_a, "body": {}, "type": "direct"})
        assert exc.value.code == "E_NOENT"


@pytest.mark.asyncio
async def test_kernels_wire_over_socket_broker(tmp_path, monkeypatch) -> None:
    """Two kernels attached to a token-protected TCP broker route cross-kernel
    messages end-to-end (the kernel auto-starts the BrokerClient)."""
    monkeypatch.setenv("AIOS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    broker = Broker(broker_id="sock-multi")
    server = BrokerServer(broker, token="sekrit")
    port = await server.start()
    c1 = BrokerClient(
        "127.0.0.1", port, kernel_id="kern-a", deliver=_noop_deliver, token="sekrit"
    )
    c2 = BrokerClient(
        "127.0.0.1", port, kernel_id="kern-b", deliver=_noop_deliver, token="sekrit"
    )
    k1 = Kernel(
        data_root=str(tmp_path / "k1"),
        llm_backend=MockLLM(mode="echo"),
        kernel_id="kern-a",
        broker=c1,
    )
    k2 = Kernel(
        data_root=str(tmp_path / "k2"),
        llm_backend=MockLLM(mode="echo"),
        kernel_id="kern-b",
        broker=c2,
    )
    try:
        pid_a = await k1.spawn_agent(_ipc_spec(name="a"))
        pid_b = await k2.spawn_agent(_ipc_spec(name="b"))
        await c1.drain()
        await c2.drain()
        sent = await k1.ipc.send(
            pid_a, {"to_pid": pid_b, "body": {"text": "sock-cross"}, "type": "direct"}
        )
        await c1.drain()
        deadline = time.monotonic() + 2.0
        mailbox = []
        while time.monotonic() < deadline:
            mailbox = k2.ipc.mailbox(pid_b)
            if mailbox:
                break
            await asyncio.sleep(0.01)
        assert len(mailbox) == 1
        assert mailbox[0].body == {"text": "sock-cross"}
        assert mailbox[0].from_pid == pid_a
        assert mailbox[0].msg_id == sent.msg_id
    finally:
        await k1.shutdown()
        await k2.shutdown()
        await server.stop()
"""Integration tests: the IPC syscall ABI end-to-end through AgentSession.

Direct-session tests attach no AgentRunner, so agents that must *block* in
recv_msg/join first need the CPU: ``_grant`` gives every queued agent one slot
(releasing each back to READY) and leaves the target RUNNING — mirroring the
grant loop a real runner performs.
"""

from __future__ import annotations

import asyncio

import pytest

from aios_kernel import Kernel
from aios_kernel.acb import AgentState
from aios_kernel.lifecycle import transition
from aios_sdk.session import AgentSession

from ..conftest import _base_spec


def _ipc_spec(**overrides) -> dict:
    spec = _base_spec(
        ipc={
            "can_send_to": ["*"],
            "can_subscribe": ["*"],
            "can_publish": ["*"],
            "mailbox": {"max_queue_depth": 100, "ttl_s": 3600},
        },
        # Phase 3 deny-by-default: agents that spawn children need the grant
        capabilities={
            "tools": [{"name": "fs.read"}, {"name": "fs.write"}],
            "spawn": True,
        },
    )
    spec.update(overrides)
    return spec


async def _grant(kernel: Kernel, pid: int) -> None:
    """Give every queued agent one CPU slot, then leave ``pid`` RUNNING."""
    kernel.scheduler._running = None
    for other in list(kernel.scheduler._ready):
        await kernel.scheduler.wait_for_grant(other)
        acb = kernel.agent_manager.peek(other)
        if acb is not None and acb.state is AgentState.RUNNING:
            transition(acb, AgentState.READY, "test-release")
            kernel.scheduler._running = None  # manually released the CPU slot
            kernel.scheduler.add_ready(other)
    await kernel.scheduler.wait_for_grant(pid)


@pytest.mark.asyncio
async def test_send_recv_roundtrip(kernel: Kernel, session) -> None:
    a = await session(_ipc_spec(name="a"))
    b = await session(_ipc_spec(name="b"))
    sent = await a.send_msg(b.pid, {"text": "hello"}, priority=10, ttl_s=60)
    res = await b.recv_msg(100)
    msg = res["msg"]
    assert msg["body"] == {"text": "hello"}
    assert msg["from_pid"] == a.pid
    assert msg["to_pid"] == b.pid
    assert msg["type"] == "direct"
    assert msg["priority"] == 10
    assert msg["msg_id"] == sent["msg_id"]
    assert msg["sig"].startswith("sha256:")
    assert msg["expires_at"] is not None


@pytest.mark.asyncio
async def test_recv_filter_by_sender(kernel: Kernel, session) -> None:
    b = await session(_ipc_spec(name="b-filter"))
    x = await session(_ipc_spec(name="x"))
    y = await session(_ipc_spec(name="y"))
    await x.send_msg(b.pid, {"from": "x"})
    await y.send_msg(b.pid, {"from": "y"})
    # only y's envelope matches the from_pid filter; x's stays queued
    res = await b.recv_msg(100, filter={"from_pid": y.pid})
    assert res["msg"]["body"] == {"from": "y"}
    assert len(kernel.ipc.mailbox(b.pid)) == 1
    res2 = await b.recv_msg(100)
    assert res2["msg"]["body"] == {"from": "x"}


@pytest.mark.asyncio
async def test_recv_timeout(kernel: Kernel, session) -> None:
    b = await session(_ipc_spec(name="b-timeout"))
    await _grant(kernel, b.pid)
    res = await b.recv_msg(30)
    assert res == {"msg": None, "reason": "timeout"}


@pytest.mark.asyncio
async def test_send_wakes_blocked_receiver(kernel: Kernel, session) -> None:
    a = await session(_ipc_spec(name="wake-a"))
    b = await session(_ipc_spec(name="wake-b"))
    await _grant(kernel, b.pid)
    task = asyncio.create_task(b.recv_msg(2000))
    await asyncio.sleep(0.05)
    assert kernel.agent_manager.get(b.pid).state.value == "blocked"
    await a.send_msg(b.pid, {"wake": True})
    kernel.scheduler.remove(a.pid)  # drop the idle peer so b's regrant succeeds
    res = await asyncio.wait_for(task, timeout=2)
    assert res["msg"]["body"] == {"wake": True}


@pytest.mark.asyncio
async def test_reply_continues_trace(kernel: Kernel, session) -> None:
    a = await session(_ipc_spec(name="trace-a"))
    b = await session(_ipc_spec(name="trace-b"))
    sent = await a.send_msg(b.pid, {"q": "1"})
    await b.send_msg(a.pid, {"a": "42"}, type="reply", reply_to=sent["msg_id"])
    res = await a.recv_msg(100)
    assert res["msg"]["type"] == "reply"
    assert res["msg"]["reply_to"] == sent["msg_id"]
    # the reply inherits the original envelope's trace_id (found across mailboxes)
    assert res["msg"]["trace_id"]


@pytest.mark.asyncio
async def test_pubsub_hierarchical_delivery(kernel: Kernel, session) -> None:
    pub = await session(_ipc_spec(name="pub"))
    sub = await session(_ipc_spec(name="sub"))
    await sub.subscribe("jobs.*")
    await pub.publish("jobs.data", {"id": 1})
    await pub.publish("other.topic", {"id": 2})  # not matched by the pattern
    assert len(kernel.ipc.mailbox(sub.pid)) == 1

    res = await sub.recv_msg(100)
    assert res["msg"]["topic"] == "jobs.data"
    assert res["msg"]["body"] == {"id": 1}
    assert res["msg"]["type"] == "event"

    # deeper topics match the prefix too; publish reports the delivery count
    delivered = await pub.publish("jobs.error.detail", {"id": 3})
    assert delivered == 1
    assert len(kernel.ipc.mailbox(sub.pid)) == 1


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(kernel: Kernel, session) -> None:
    pub = await session(_ipc_spec(name="pub2"))
    sub = await session(_ipc_spec(name="sub2"))
    await sub.subscribe("events")
    await pub.publish("events", {"n": 1})
    await sub.unsubscribe("events")
    await pub.publish("events", {"n": 2})
    res = await sub.recv_msg(100)
    assert res["msg"]["body"] == {"n": 1}
    assert kernel.ipc.mailbox(sub.pid) == []  # the post-unsubscribe event never landed


@pytest.mark.asyncio
async def test_join_returns_per_pid_results(kernel: Kernel, session) -> None:
    parent = await session(_ipc_spec(name="parent"))
    child_pid = await parent.spawn(_ipc_spec(name="child"))
    child_sc = AgentSession(kernel, child_pid)
    await _grant(kernel, parent.pid)

    join_task = asyncio.create_task(parent.join([child_pid], timeout_ms=3000))
    await asyncio.sleep(0.05)
    assert kernel.agent_manager.get(parent.pid).state.value == "blocked"
    await child_sc.exit(status="ok", message="done work")
    res = await asyncio.wait_for(join_task, timeout=2)

    assert res["timed_out"] is False
    result = res["results"][0]
    assert result["pid"] == child_pid
    assert result["status"] == "terminated"
    assert result["exit_status"] == "ok"


@pytest.mark.asyncio
async def test_join_times_out_with_pending_results(kernel: Kernel, session) -> None:
    parent = await session(_ipc_spec(name="parent-to"))
    child_pid = await parent.spawn(_ipc_spec(name="stayer"))
    await _grant(kernel, parent.pid)
    kernel.scheduler.remove(child_pid)  # park the idle peer; only parent may run
    res = await parent.join([child_pid], timeout_ms=50)
    assert res["timed_out"] is True
    result = res["results"][0]
    assert result["pid"] == child_pid
    assert result["status"] == "ready"  # the child never terminated


@pytest.mark.asyncio
async def test_join_unknown_target_raises(kernel: Kernel, session) -> None:
    parent = await session(_ipc_spec(name="parent-ghost"))
    from aios_sdk.errors import AiosSyscallError

    with pytest.raises(AiosSyscallError):
        await parent.join([999_999])


@pytest.mark.asyncio
async def test_valid_handoff_delivered(kernel: Kernel, session) -> None:
    a = await session(_ipc_spec(name="handoff-a"))
    b = await session(_ipc_spec(name="handoff-b"))
    target_spec = _base_spec(name="handed-off-agent")
    await a.send_msg(b.pid, {"spec": target_spec}, type="handoff")
    res = await b.recv_msg(100)
    assert res["msg"]["type"] == "handoff"
    assert res["msg"]["body"]["spec"]["name"] == "handed-off-agent"
    # the delivered spec is schema-valid, so the receiver may spawn it
    from aios_kernel.specs import validate_spec

    validate_spec(res["msg"]["body"]["spec"])
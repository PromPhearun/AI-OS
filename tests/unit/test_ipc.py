"""Unit tests: IPC envelopes, topic matching, permissions, mailbox policy,
and checkpoint persistence of mailbox/subscriptions (Phase 2 Slice 2.2)."""

from __future__ import annotations

import time

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.ipc import IpcMessage, _topic_match
from aios_kernel.modules.llm_core import MockLLM
from aios_sdk.errors import AiosSyscallError

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


# ------------------------------------------------------------- envelope tests
def test_envelope_signs_and_roundtrips() -> None:
    msg = IpcMessage(msg_id="m1", type="direct", from_pid=1, to_pid=2, body={"x": 1})
    assert msg.trace_id.startswith("tr-")
    assert msg.created_at > 0
    assert msg.sig.startswith("sha256:")

    d = msg.to_dict()
    assert d["sig"] == msg.sig
    restored = IpcMessage.from_dict(d)
    assert restored.to_dict() == d  # canonical form is byte-identical

    # any body tamper changes the canonical envelope
    tampered = IpcMessage.from_dict(d)
    tampered.body = {"x": 2}
    assert tampered.to_dict() != d


def test_exclude_sig_drops_signature() -> None:
    msg = IpcMessage(msg_id="m2", type="event", from_pid=3, to_pid=4, topic="jobs.data", body={})
    assert "sig" not in msg.to_dict(exclude_sig=True)
    assert "sig" in msg.to_dict()


# ------------------------------------------------------------- topic matching
def test_topic_match() -> None:
    assert _topic_match("*", "any.topic.at.all") is True
    assert _topic_match("jobs.data", "jobs.data") is True
    assert _topic_match("jobs.*", "jobs.data") is True
    assert _topic_match("jobs.*", "jobs.data.result") is True
    assert _topic_match("jobs.*", "jobs") is False
    assert _topic_match("jobs.*", "other.data") is False
    assert _topic_match("jobs.data", "jobs") is False


# ---------------------------------------------------------------- permissions
@pytest.mark.asyncio
async def test_send_deny_by_default(kernel: Kernel, session) -> None:
    """A spec without any `ipc` grant cannot send (docs/08-security.md)."""
    sender = await session(_base_spec(name="no-ipc"))  # deliberately no ipc field
    receiver = await session(_ipc_spec(name="receiver"))
    with pytest.raises(AiosSyscallError, match="may not send"):
        await sender.send_msg(receiver.pid, {"text": "nope"})


@pytest.mark.asyncio
async def test_send_requires_declared_recipient(kernel: Kernel, session) -> None:
    sender = await session(_ipc_spec(name="sender", ipc={"can_send_to": ["pid:999"]}))
    receiver = await session(_ipc_spec(name="receiver"))
    with pytest.raises(AiosSyscallError, match="may not send"):
        await sender.send_msg(receiver.pid, {"text": "nope"})


@pytest.mark.asyncio
async def test_send_group_grant(kernel: Kernel, session) -> None:
    sender = await session(
        _ipc_spec(name="sender-g", group_id="g1", ipc={"can_send_to": ["group:g1"]})
    )
    teammate = await session(_ipc_spec(name="teammate", group_id="g1"))
    outsider = await session(_ipc_spec(name="outsider", group_id="g2"))
    await sender.send_msg(teammate.pid, {"text": "ok"})
    with pytest.raises(AiosSyscallError, match="may not send"):
        await sender.send_msg(outsider.pid, {"text": "nope"})


@pytest.mark.asyncio
async def test_subscribe_and_publish_deny_by_default(kernel: Kernel, session) -> None:
    sub = await session(_ipc_spec(name="sub", ipc={"can_send_to": ["*"]}))  # no topic grants
    with pytest.raises(AiosSyscallError, match="may not subscribe"):
        await sub.subscribe("jobs.*")
    with pytest.raises(AiosSyscallError, match="may not publish"):
        await sub.publish("jobs.data", {"x": 1})


# --------------------------------------------------------------- mailbox policy
@pytest.mark.asyncio
async def test_mailbox_overflow_drops_oldest(kernel: Kernel, session) -> None:
    target = await session(
        _ipc_spec(name="shallow", ipc={"can_send_to": ["*"], "mailbox": {"max_queue_depth": 2}})
    )
    sender = await session(_ipc_spec(name="flood"))
    for i in range(3):
        await sender.send_msg(target.pid, {"n": i})
    box = kernel.ipc.mailbox(target.pid)
    assert [m.body["n"] for m in box] == [1, 2]  # the oldest (n=0) was dropped


@pytest.mark.asyncio
async def test_ttl_prunes_expired(kernel: Kernel, session) -> None:
    target = await session(_ipc_spec(name="ttl-box"))
    sender = await session(_ipc_spec(name="ttl-sender"))
    await sender.send_msg(target.pid, {"keep": True})
    await sender.send_msg(target.pid, {"expire": True}, ttl_s=0.001)
    box = kernel.ipc.mailbox(target.pid)
    assert len(box) == 2
    box[1].expires_at = time.time() - 1  # force expiry before the next dequeue
    got = kernel.ipc.dequeue_matching(target.pid, {"from_pid": sender.pid})
    assert got is not None and got.body["keep"] is True
    assert kernel.ipc.mailbox(target.pid) == []  # expired envelope was pruned


@pytest.mark.asyncio
async def test_send_to_unknown_target_raises(kernel: Kernel, session) -> None:
    sender = await session(_ipc_spec(name="solo"))
    with pytest.raises(AiosSyscallError):
        await sender.send_msg(999_999, {"text": "ghost"})


@pytest.mark.asyncio
async def test_handoff_requires_valid_spawnable_spec(kernel: Kernel, session) -> None:
    sender = await session(_ipc_spec(name="handoff-sender"))
    receiver = await session(_ipc_spec(name="handoff-receiver"))
    # handoff without a spec is rejected
    with pytest.raises(AiosSyscallError, match="spec"):
        await sender.send_msg(receiver.pid, {"text": "no spec"}, type="handoff")
    # a schema-invalid spec is rejected at send time
    with pytest.raises(AiosSyscallError, match="invalid agent spec"):
        await sender.send_msg(receiver.pid, {"spec": {"name": "bogus"}}, type="handoff")
# ----------------------------------------------------- checkpoint persistence
@pytest.mark.asyncio
async def test_checkpoint_persists_mailbox_and_subscriptions(kernel: Kernel, session) -> None:
    receiver = await session(
        _ipc_spec(name="ckpt-box", ipc={"can_send_to": ["*"], "can_subscribe": ["*"]})
    )
    sender = await session(_ipc_spec(name="ckpt-sender"))
    await sender.send_msg(receiver.pid, {"payload": "durable"})
    await receiver.subscribe("jobs.*")

    cid = kernel.storage.checkpoint(receiver.pid)
    ckpt = kernel.storage.get(cid)
    assert [m.body["payload"] for m in ckpt.mailbox] == ["durable"]
    assert ckpt.subscriptions == ["jobs.*"]

    # trashing the live mailbox then restoring reproduces it faithfully
    kernel.ipc.free(receiver.pid)
    kernel.storage.restore(receiver.pid, cid)
    assert [m.body["payload"] for m in kernel.ipc.mailbox(receiver.pid)] == ["durable"]
    assert kernel.ipc.subscriptions(receiver.pid) == ["jobs.*"]


@pytest.mark.asyncio
async def test_checkpoint_disk_snapshot_has_mailbox(kernel: Kernel, session) -> None:
    receiver = await session(_ipc_spec(name="disk-box"))
    sender = await session(_ipc_spec(name="disk-sender"))
    await sender.send_msg(receiver.pid, {"v": 1})
    cid = kernel.storage.checkpoint(receiver.pid)
    kernel.storage._checkpoints.pop(cid)  # evict cache -> must load from disk
    ckpt = kernel.storage.get(cid)
    assert [m.body["v"] for m in ckpt.mailbox] == [1]


@pytest.mark.asyncio
async def test_restored_session_wakes_with_faithful_mailbox(tmp_path) -> None:
    """Crash-resume: a suspended agent's undelivered mail survives the restart."""
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    rid = await k1.spawn_agent(_ipc_spec(name="resume-box"))
    sid = await k1.spawn_agent(_ipc_spec(name="resume-sender"))
    await k1.ipc.send(sid, {"to_pid": rid, "body": {"msg": "waiting for you"}})
    await k1.scheduler.suspend(rid, "test")
    # no k1.shutdown(): simulate a crash so the session file is what survives

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        assert k2.restore_session() == [rid]
        acb = k2.agent_manager.get(rid)
        assert acb.state.value == "suspended"
        box = k2.ipc.mailbox(rid)
        assert [m.body["msg"] for m in box] == ["waiting for you"]
    finally:
        await k2.shutdown()
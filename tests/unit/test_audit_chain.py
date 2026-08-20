"""Unit tests: audit-log hash chaining + tamper detection (docs/08-security.md
§6; acceptance criterion: 'Audit log tampering detection test passes')."""

from __future__ import annotations

import hashlib
import json

import pytest

from aios_kernel import Kernel
from aios_kernel.modules.audit import GENESIS_HASH
from aios_kernel.modules.llm_core import MockLLM


def _sha256_canonical(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_every_record_has_chain_fields_and_recomputable_hash(kernel, session, audit_lines) -> None:
    sc = await session()
    await sc.append_context("user", "hello")
    await sc.log("info", "world")

    entries = audit_lines()
    assert len(entries) >= 2
    prev_hash = GENESIS_HASH
    prev_seq = 0
    for entry in entries:
        assert {"seq", "prev_hash", "hash"} <= set(entry)
        assert len(entry["hash"]) == 64 and len(entry["prev_hash"]) == 64
        assert entry["prev_hash"] == prev_hash
        assert entry["hash"] == _sha256_canonical(entry)
        assert entry["seq"] == prev_seq + 1
        prev_hash = entry["hash"]
        prev_seq = entry["seq"]


@pytest.mark.asyncio
async def test_verify_ok_on_pristine_log(kernel, session) -> None:
    sc = await session()
    await sc.append_context("user", "x")
    result = kernel.audit.verify()
    assert result["valid"] is True
    assert result["entries"] >= 1
    assert result["first_bad"] is None


@pytest.mark.asyncio
async def test_tamper_detection_flips_a_byte(kernel, session, audit_lines) -> None:
    sc = await session()
    await sc.append_context("user", "tamper me")

    path = kernel.audit.path
    raw = open(path, encoding="utf-8").read()
    lines = raw.splitlines()
    # corrupt one character inside the first syscall record (not the hashes)
    tampered = lines[0][:20] + ("X" if lines[0][20] != "X" else "Y") + lines[0][21:]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join([tampered, *lines[1:]]) + "\n")

    result = kernel.audit.verify()
    assert result["valid"] is False
    assert result["first_bad"] == 0


@pytest.mark.asyncio
async def test_tamper_detection_finds_bad_hash_middle_of_chain(kernel, session) -> None:
    sc = await session()
    await sc.append_context("user", "a")
    await sc.append_context("user", "b")
    await sc.append_context("user", "c")

    path = kernel.audit.path
    lines = open(path, encoding="utf-8").read().splitlines()
    # rewrite the second record's hash to a bogus value
    rec = json.loads(lines[1])
    rec["hash"] = "f" * 64
    lines[1] = json.dumps(rec)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    result = kernel.audit.verify()
    assert result["valid"] is False
    assert result["first_bad"] == 1


@pytest.mark.asyncio
async def test_chain_survives_restart(tmp_path) -> None:
    """A second kernel appends to the same log; the chain stays valid."""
    k1 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    await k1.spawn_agent({"name": "r1", "llm": {"model": "mock"}})
    await k1.shutdown()

    k2 = Kernel(data_root=str(tmp_path), llm_backend=MockLLM(mode="echo"))
    try:
        await k2.spawn_agent({"name": "r2", "llm": {"model": "mock"}})
        result = k2.audit.verify()
        assert result["valid"] is True
        assert result["entries"] >= 2
    finally:
        await k2.shutdown()
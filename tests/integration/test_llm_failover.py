"""Integration tests: LLM provider failover (Phase 4 — docs/11-roadmap.md §6).

Covers the multi-backend LLM core:

* a failing primary falls back to the configured failover chain;
* per-provider health degrades and marks a provider ``down`` at the
  consecutive-failure threshold (``provider_status()`` feeds ``/v1/llm``);
* dev mode (only ``mock``) silently routes unknown models/providers to mock;
* strict mode (a real backend configured) rejects unconfigured providers so a
  misconfigured primary can never be masked by silent fallback;
* a flaky provider recovers within its per-attempt retry budget.
"""

from __future__ import annotations

import pytest

from aios_kernel.errors import AiosError, E_LLM
from aios_kernel.modules.llm_core import (
    FAIL_THRESHOLD,
    Generation,
    LLMBackend,
    LLMCore,
    MockLLM,
)

MSG = [{"role": "user", "content": "hello"}]

BASE_SPEC = {
    "name": "llm-failover",
    "version": "1",
    "description": "llm failover integration test",
    "group_id": "test",
    "priority": 0,
    "llm": {"model": "mock", "system": "", "temperature": 0.0},
    "budgets": {"max_turns": 2, "max_tool_calls": 2},
    "capabilities": {"tools": []},
}


async def _spawn(kernel) -> int:
    """A live ACB so scheduler.account_llm has somewhere to write usage."""
    return await kernel.spawn_agent(dict(BASE_SPEC))


class FailingBackend(MockLLM):
    """MockLLM that raises E_LLM until its failure budget is spent."""

    def __init__(self, *, fails: int, provider: str = "primary"):
        super().__init__(provider=provider, mode="echo")
        self._fails = fails

    async def generate(self, messages, **kwargs) -> Generation:
        if self._fails > 0:
            self._fails -= 1
            raise AiosError(E_LLM, "simulated backend outage")
        return await super().generate(messages, **kwargs)


class FakeRealBackend(LLMBackend):
    """A non-mock backend (so the core runs in strict mode)."""

    def __init__(self, *, provider: str = "real", model: str = "real-model"):
        self.provider = provider
        self.model = model

    async def generate(self, messages, **kwargs) -> Generation:
        return Generation(text="real", provider=self.provider, model=self.model)


def _status(core: LLMCore) -> dict[str, dict]:
    return {p["provider"]: p for p in core.provider_status()}


async def test_primary_failure_falls_back_to_failover_provider(kernel) -> None:
    pid = await _spawn(kernel)
    core = LLMCore(
        kernel,
        backends={
            "primary": FailingBackend(fails=1_000_000, provider="primary"),
            "fallback": MockLLM(provider="fallback", mode="echo"),
        },
    )
    gen = await core.generate(
        pid,
        MSG,
        model="p-model",
        provider="primary",
        failover=["fallback"],
        max_retries=0,
    )
    assert gen.provider == "fallback"
    assert gen.text.startswith("[mock:")

    st = _status(core)
    assert st["primary"]["state"] == "degraded"
    assert st["primary"]["consecutive_failures"] == 1
    assert st["primary"]["failures"] == 1
    assert "simulated backend outage" in st["primary"]["last_error"]
    assert st["fallback"]["state"] == "healthy"
    assert st["fallback"]["requests"] == 1


async def test_health_marks_provider_down_at_consecutive_failure_threshold(kernel) -> None:
    pid = await _spawn(kernel)
    core = LLMCore(kernel, backends={"primary": FailingBackend(fails=10**9, provider="primary")})
    for i in range(FAIL_THRESHOLD):
        with pytest.raises(AiosError) as exc_info:
            await core.generate(pid, MSG, provider="primary", max_retries=0)
        assert exc_info.value.code == E_LLM
        st = _status(core)["primary"]
        assert st["consecutive_failures"] == i + 1
        assert st["failures"] == i + 1
        expected = "down" if i + 1 >= FAIL_THRESHOLD else "degraded"
        assert st["state"] == expected


async def test_dev_mode_silently_routes_unknown_model_to_mock(kernel) -> None:
    pid = await _spawn(kernel)
    core = LLMCore(kernel, backend=MockLLM(mode="echo"))
    gen = await core.generate(pid, MSG, model="gpt-anything")
    assert gen.provider == "mock"


async def test_strict_mode_rejects_unconfigured_provider(kernel) -> None:
    pid = await _spawn(kernel)
    core = LLMCore(
        kernel,
        backends={"mock": MockLLM(), "real": FakeRealBackend()},
    )
    with pytest.raises(AiosError) as exc_info:
        await core.generate(pid, MSG, provider="ghost", max_retries=0)
    assert exc_info.value.code == E_LLM
    assert "ghost" in exc_info.value.message


async def test_flaky_provider_recovers_within_retry_budget(kernel) -> None:
    pid = await _spawn(kernel)
    flaky = FailingBackend(fails=1, provider="primary")
    core = LLMCore(kernel, backends={"primary": flaky})
    gen = await core.generate(pid, MSG, provider="primary", max_retries=2, backoff_s=0.01)
    assert gen.provider == "primary"

    st = _status(core)["primary"]
    assert st["failures"] == 1
    assert st["consecutive_failures"] == 0  # reset on success
    assert st["state"] == "healthy"
    assert st["last_success_ts"] is not None


async def test_all_providers_failed_raises_aggregate_error(kernel) -> None:
    pid = await _spawn(kernel)
    core = LLMCore(
        kernel,
        backends={
            "primary": FailingBackend(fails=10**9, provider="primary"),
            "fallback": FailingBackend(fails=10**9, provider="fallback"),
        },
    )
    with pytest.raises(AiosError) as exc_info:
        await core.generate(
            pid,
            MSG,
            provider="primary",
            failover=["fallback"],
            max_retries=0,
        )
    assert exc_info.value.code == E_LLM
    assert "primary" in exc_info.value.message and "fallback" in exc_info.value.message


async def test_validate_llm_spec_resolves_failover_chain_in_dev_mode(kernel) -> None:
    core = LLMCore(kernel, backend=MockLLM(mode="echo"))
    # Unknown provider + unconfigured failover target resolve to mock in dev mode.
    core.validate_llm_spec({"model": "x", "provider": "nope", "failover": ["also-nope"]})
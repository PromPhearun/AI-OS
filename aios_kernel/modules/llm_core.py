"""LLM Core — the kernel's single LLM service.

Every LLM request in the kernel is serialized through ``LLMCore.generate``,
which holds a global lock (so concurrent agents can never interleave or mix
contexts), then applies per-agent token/cost accounting for budget checks.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from ..errors import AiosError, E_LLM
from ..syscalls.registry import register


@dataclass
class Generation:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    provider: str = "mock"


class LLMBackend:
    """Minimal backend interface. Deterministic by contract (no hidden state)."""

    async def generate(
        self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> Generation:
        raise NotImplementedError


class MockLLM(LLMBackend):
    """Deterministic, scriptable backend for development and tests.

    Modes:
      "echo"    — returns ``[mock:<last user content>]`` (correlates with input,
                  so cross-talk between agents is trivially detectable).
      "script"  — substring-matches the last user message against ``script``.
      "runaway" — returns ever-growing output; token counts scale with call
                  count (used for budget-exhaustion tests).
    """

    def __init__(
        self,
        *,
        script: dict[str, str] | None = None,
        default: str = "ok",
        mode: str = "echo",
        tokens_in: int = 16,
        tokens_out: int = 16,
        cost_per_1k_in: float = 0.0,
        cost_per_1k_out: float = 0.0,
        latency_s: float = 0.0,
    ):
        self.script = dict(script or {})
        self.default = default
        self.mode = mode
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_per_1k_in = cost_per_1k_in
        self.cost_per_1k_out = cost_per_1k_out
        self.latency_s = latency_s
        self.calls = 0
        self._in_flight = 0
        self.peak_concurrency = 0

    async def generate(
        self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> Generation:
        self.calls += 1
        self._in_flight += 1
        self.peak_concurrency = max(self.peak_concurrency, self._in_flight)
        try:
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
            last = messages[-1]["content"] if messages else ""
            if self.mode == "echo":
                text = f"[mock:{last}]"
                tokens_out = self.tokens_out
            elif self.mode == "runaway":
                text = f"[runaway:{self.calls}] " + "blah " * min(self.calls, 200)
                tokens_out = min(self.calls * 1000, 50_000)
            else:  # script
                text = next((v for k, v in self.script.items() if k in last), self.default)
                tokens_out = self.tokens_out
            tin = self.tokens_in + len(last) // 4
            cost = (tin / 1000) * self.cost_per_1k_in + (tokens_out / 1000) * self.cost_per_1k_out
            return Generation(
                text=text,
                tokens_in=tin,
                tokens_out=tokens_out,
                cost_usd=cost,
                provider="mock",
            )
        finally:
            self._in_flight -= 1


class OpenAICompatBackend(LLMBackend):
    """OpenAI-compatible chat-completions backend (inert unless configured).

    Activated by setting AIOS_LLM_URL (+ optional AIOS_LLM_API_KEY and
    AIOS_LLM_MODEL). No API key is ever logged or persisted.
    """

    def __init__(self, *, base_url: str, api_key: str = "", model: str = "gpt-4o-mini", timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._client = None

    async def generate(
        self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> Generation:
        import httpx

        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=headers, timeout=self.timeout_s
            )
        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage", {})
            return Generation(
                text=text,
                tokens_in=int(usage.get("prompt_tokens", 0)),
                tokens_out=int(usage.get("completion_tokens", 0)),
                cost_usd=0.0,  # pricing is configured at the control plane
                provider=self.model,
            )
        except AiosError:
            raise
        except Exception as exc:
            raise AiosError(E_LLM, f"LLM backend error: {type(exc).__name__}") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_backend_from_env() -> LLMBackend:
    url = os.environ.get("AIOS_LLM_URL")
    if url:
        return OpenAICompatBackend(
            base_url=url,
            api_key=os.environ.get("AIOS_LLM_API_KEY", ""),
            model=os.environ.get("AIOS_LLM_MODEL", "gpt-4o-mini"),
        )
    return MockLLM()


class LLMCore:
    """Serializes LLM requests; applies per-agent accounting."""

    def __init__(self, kernel, backend: LLMBackend | None = None):
        self.kernel = kernel
        self.backend = backend or build_backend_from_env()
        self._lock = asyncio.Lock()

    def validate_model(self, model: str) -> None:
        """Refuse to spawn agents whose model the kernel cannot serve."""
        if model == "mock":
            return
        if isinstance(self.backend, OpenAICompatBackend):
            return
        raise AiosError(
            E_LLM,
            f"model '{model}' is unavailable; only 'mock' is built in "
            "(set AIOS_LLM_URL / AIOS_LLM_API_KEY / AIOS_LLM_MODEL for a real backend)",
        )

    async def generate(
        self,
        pid: int,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> Generation:
        async with self._lock:
            gen = await self.backend.generate(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        self.kernel.scheduler.account_llm(pid, gen.tokens_in, gen.tokens_out, gen.cost_usd)
        return gen


# ------------------------------------------------------------------ syscalls
@register("generate")
async def _generate(kernel, pid: int, args: dict) -> dict:
    """Agent-mediated LLM turn: context in, model reply appended to context."""
    if args.get("user"):
        kernel.context.append(pid, "user", args["user"])
    messages = kernel.context.read(pid)
    gen = await kernel.llm.generate(
        pid,
        messages,
        temperature=args.get("temperature") or 0.0,
        max_tokens=args.get("max_tokens"),
    )
    kernel.context.append(pid, "assistant", gen.text)
    return {
        "text": gen.text,
        "tokens_in": gen.tokens_in,
        "tokens_out": gen.tokens_out,
        "cost_usd": gen.cost_usd,
    }
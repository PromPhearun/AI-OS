"""LLM Core — the kernel's multi-provider LLM service.

Every LLM request in the kernel is serialized through ``LLMCore.generate``,
which holds a global lock (so concurrent agents can never interleave or mix
contexts), then applies per-agent token/cost accounting for budget checks.

Provider failover (Phase 4):
  * Backends are registered by *provider name* (``_backends``). The env helpers
    register a real backend under ``openai`` (legacy ``AIOS_LLM_URL``) or
    parse ``AIOS_LLM_PROVIDERS`` for many. A deterministic ``MockLLM`` is always
    available under ``mock`` (offline/dev mode).
  * An agent's ``spec.llm`` selects the model (``model``), an optional explicit
    ``provider``, an ordered ``failover`` list, and per-attempt ``max_retries``
    with exponential ``backoff_s``. ``generate`` walks the plan and returns the
    first successful response; failures degrade that provider's health.
  * ``provider_status()`` exposes per-provider health for ``/v1/providers`` and
    the web desktop — operators see degraded/down providers and can fix them
    without restarting agents (the plan is resolved per request).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from ..errors import AiosError, E_INVAL, E_LLM
from ..syscalls.registry import register


@dataclass
class Generation:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    provider: str = "mock"
    model: str = "mock"


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
        provider: str = "mock",
    ):
        self.script = dict(script or {})
        self.default = default
        self.mode = mode
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_per_1k_in = cost_per_1k_in
        self.cost_per_1k_out = cost_per_1k_out
        self.latency_s = latency_s
        self.provider = provider
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
                provider=self.provider,
                model=self.provider,
            )
        finally:
            self._in_flight -= 1


class OpenAICompatBackend(LLMBackend):
    """OpenAI-compatible chat-completions backend (inert unless configured).

    Activated by setting AIOS_LLM_URL (+ optional AIOS_LLM_API_KEY and
    AIOS_LLM_MODEL), or via AIOS_LLM_PROVIDERS for many providers. No API key
    is ever logged or persisted.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout_s: float = 120.0,
        provider: str = "openai",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.provider = provider
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
                provider=self.provider,
                model=self.model,
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


def build_backends_from_env() -> dict[str, LLMBackend]:
    """Build the provider map from the environment.

    ``AIOS_LLM_PROVIDERS`` is a JSON object keyed by provider name::

        {
          "openai": {"url": "...", "api_key": "...", "model": "gpt-4o-mini"},
          "groq":   {"url": "...", "api_key": "...", "model": "llama-3.3-70b-versatile"}
        }

    The legacy single-backend variables (``AIOS_LLM_URL`` / ``AIOS_LLM_API_KEY``
    / ``AIOS_LLM_MODEL``) register under the provider name ``openai``. When
    nothing is configured, only the built-in ``mock`` provider is returned.
    """
    backends: dict[str, LLMBackend] = {}
    raw = os.environ.get("AIOS_LLM_PROVIDERS")
    if raw:
        import json

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AiosError(E_INVAL, f"AIOS_LLM_PROVIDERS is not valid JSON: {exc}") from exc
        for name, cfg in data.items():
            if not isinstance(cfg, dict) or not cfg.get("url"):
                raise AiosError(E_INVAL, f"AIOS_LLM_PROVIDERS['{name}'] needs a 'url'")
            backends[name] = OpenAICompatBackend(
                base_url=cfg["url"],
                api_key=str(cfg.get("api_key", "")),
                model=str(cfg.get("model", "gpt-4o-mini")),
                provider=name,
            )
    url = os.environ.get("AIOS_LLM_URL")
    if url:
        backends.setdefault(
            "openai",
            OpenAICompatBackend(
                base_url=url,
                api_key=os.environ.get("AIOS_LLM_API_KEY", ""),
                model=os.environ.get("AIOS_LLM_MODEL", "gpt-4o-mini"),
            ),
        )
    if "mock" not in backends:
        backends["mock"] = MockLLM()
    return backends


# Number of consecutive failures before a provider is marked "down".
FAIL_THRESHOLD = 3


class LLMCore:
    """Serializes LLM requests; routes across providers with failover.

    ``generate`` holds the global lock for the *entire* retry + failover walk,
    preserving the invariant that concurrent agents can never interleave or mix
    contexts. Accounting is applied once, for the successful provider.
    """

    def __init__(
        self,
        kernel,
        backend: LLMBackend | None = None,
        backends: dict[str, LLMBackend] | None = None,
    ):
        self.kernel = kernel
        self._lock = asyncio.Lock()
        self._backends: dict[str, LLMBackend] = {}
        for name, b in (backends or {}).items():
            self._register_backend(name, b)
        if backend is not None:
            self._register_backend(getattr(backend, "provider", "openai"), backend)
        if not self._backends:
            for name, b in build_backends_from_env().items():
                self._register_backend(name, b)
        if "mock" not in self._backends:
            self._register_backend("mock", MockLLM())

        real = [n for n, b in self._backends.items() if not isinstance(b, MockLLM)]
        self._default_provider = real[0] if real else "mock"
        self._strict = bool(real)  # with a real provider, unknown names are errors

        self._health: dict[str, dict] = {}
        for name in self._backends:
            self._health[name] = {
                "state": "healthy",
                "consecutive_failures": 0,
                "requests": 0,
                "failures": 0,
                "last_error": None,
                "last_success_ts": None,
                "last_failure_ts": None,
            }
        self._max_retries_default = int(os.environ.get("AIOS_LLM_MAX_RETRIES", "1"))
        self._backoff_base_s = float(os.environ.get("AIOS_LLM_BACKOFF_S", "0.25"))

    # ------------------------------------------------------------ registration
    def _register_backend(self, name: str, backend: LLMBackend) -> None:
        backend.provider = name  # name-normalized; backend keeps its own model
        self._backends[name] = backend

    @property
    def providers(self) -> list[str]:
        return list(self._backends)

    def get_backend(self, name: str) -> LLMBackend | None:
        return self._backends.get(name)

    # ------------------------------------------------------------------- plan
    def _plan(
        self,
        provider: str | None,
        model: str | None,
        failover: list[str] | None,
    ) -> list[tuple[str, str]]:
        """Ordered ``(provider, model)`` route from a spec's llm config.

        In dev mode (only the ``mock`` backend configured) any unresolvable
        model or provider silently routes to ``mock``. Once a real provider is
        configured, unknown provider names are hard errors — a misconfigured
        primary must never be masked by silent fallback.
        """
        backends = self._backends
        strict = self._strict
        unresolved: list[str] = []

        def resolve(entry: str) -> tuple[str, str] | None:
            if ":" in entry:
                p, m = entry.split(":", 1)
                if p in backends:
                    return (p, m)
                if not strict and "mock" in backends:
                    return ("mock", m)
                unresolved.append(f"provider '{p}' not configured")
                return None
            if entry in backends:
                b = backends[entry]
                return (entry, getattr(b, "model", entry))
            if "openai" in backends:
                return ("openai", entry)
            if not strict and "mock" in backends:
                return ("mock", entry)
            unresolved.append(f"model '{entry}' has no configured provider")
            return None

        if provider is not None:
            if provider in backends:
                primary = (provider, model or getattr(backends[provider], "model", provider))
            elif not strict and "mock" in backends:
                primary = ("mock", model or "mock")
            else:
                raise AiosError(E_LLM, f"llm.provider '{provider}' is not configured")
        elif model is not None:
            resolved = resolve(model)
            if resolved is None:
                raise AiosError(E_LLM, "unresolvable llm model: " + "; ".join(unresolved))
            primary = resolved
        else:
            primary = (
                self._default_provider,
                getattr(backends[self._default_provider], "model", self._default_provider),
            )

        plan = [primary]
        seen = {primary[0]}
        for entry in failover or []:
            resolved = resolve(entry)
            if resolved is None:
                continue  # best-effort: skip unconfigured failover targets
            if resolved[0] not in seen:
                plan.append(resolved)
                seen.add(resolved[0])
        return plan

    def validate_model(self, model: str) -> None:
        """Refuse to spawn agents whose model the kernel cannot serve."""
        self._plan(None, model, None)

    def validate_llm_spec(self, llm: dict) -> None:
        """Validate the full spec.llm block (model + provider + failover chain)."""
        self._plan(
            llm.get("provider"),
            llm.get("model"),
            list(llm.get("failover") or []),
        )

    # ------------------------------------------------------------ health
    def _mark_failure(self, provider: str, exc: AiosError) -> None:
        h = self._health[provider]
        h["consecutive_failures"] += 1
        h["failures"] += 1
        h["last_error"] = str(exc)
        h["last_failure_ts"] = time.time()
        h["state"] = "down" if h["consecutive_failures"] >= FAIL_THRESHOLD else "degraded"

    def _mark_success(self, provider: str) -> None:
        h = self._health[provider]
        h["consecutive_failures"] = 0
        h["last_error"] = None
        h["last_success_ts"] = time.time()
        h["state"] = "healthy"

    def provider_status(self) -> list[dict]:
        out = []
        for name, backend in self._backends.items():
            h = dict(self._health[name])
            out.append(
                {
                    "provider": name,
                    "model": getattr(backend, "model", name),
                    "state": h["state"],
                    "consecutive_failures": h["consecutive_failures"],
                    "requests": h["requests"],
                    "failures": h["failures"],
                    "last_error": h["last_error"],
                    "last_success_ts": h["last_success_ts"],
                    "last_failure_ts": h["last_failure_ts"],
                }
            )
        return out

    # ------------------------------------------------------------------ call
    async def generate(
        self,
        pid: int,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        failover: list[str] | None = None,
        max_retries: int | None = None,
        backoff_s: float | None = None,
    ) -> Generation:
        plan = self._plan(provider, model, failover)
        retries = max(0, max_retries if max_retries is not None else self._max_retries_default)
        backoff = backoff_s if backoff_s is not None else self._backoff_base_s

        async with self._lock:
            result: Generation | None = None
            errors: list[str] = []
            for prov, model_name in plan:
                backend = self._backends[prov]
                for attempt in range(retries + 1):
                    self._health[prov]["requests"] += 1
                    try:
                        gen = await backend.generate(
                            messages, temperature=temperature, max_tokens=max_tokens
                        )
                    except AiosError as exc:
                        errors.append(f"{prov}: {exc}")
                        self._mark_failure(prov, exc)
                    except Exception as exc:  # network / transport / protocol
                        wrapped = AiosError(E_LLM, f"LLM backend error: {type(exc).__name__}")
                        errors.append(f"{prov}: {wrapped}")
                        self._mark_failure(prov, wrapped)
                    else:
                        self._mark_success(prov)
                        gen.provider = prov
                        gen.model = model_name
                        result = gen
                        break
                    if attempt < retries:
                        await asyncio.sleep(backoff * (2**attempt))
                if result is not None:
                    break
            if result is None:
                raise AiosError(E_LLM, "all LLM providers failed: " + "; ".join(errors))

        self.kernel.scheduler.account_llm(pid, result.tokens_in, result.tokens_out, result.cost_usd)
        return result


# ------------------------------------------------------------------ syscalls
@register("generate")
async def _generate(kernel, pid: int, args: dict) -> dict:
    """Agent-mediated LLM turn: context in, model reply appended to context."""
    # Automatic eviction: if the spec sets context.context_token_budget and the
    # window is over budget, summarize first so assembly always fits.
    budget = kernel.agent_manager.get(pid).spec.get("context", {}).get("context_token_budget")
    if budget and kernel.context.tokens(pid) > budget:
        await kernel.context.summarize(pid, target_tokens=int(budget * 0.5))
    if args.get("user"):
        kernel.context.append(pid, "user", args["user"])
    messages = kernel.context.read(pid)
    llm_cfg = kernel.agent_manager.get(pid).spec.get("llm", {}) or {}
    gen = await kernel.llm.generate(
        pid,
        messages,
        temperature=args.get("temperature") or 0.0,
        max_tokens=args.get("max_tokens"),
        model=llm_cfg.get("model"),
        provider=llm_cfg.get("provider"),
        failover=list(llm_cfg.get("failover") or []),
        max_retries=llm_cfg.get("max_retries"),
        backoff_s=llm_cfg.get("backoff_s"),
    )
    kernel.context.append(pid, "assistant", gen.text)
    return {
        "text": gen.text,
        "tokens_in": gen.tokens_in,
        "tokens_out": gen.tokens_out,
        "cost_usd": gen.cost_usd,
        "provider": gen.provider,
        "model": gen.model,
    }
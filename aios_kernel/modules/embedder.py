"""Embedder abstraction for L3 long-term memory and the semantic FS.

Two backends (docs/04-memory.md §7):

  * ``HashingEmbedder``  — deterministic, dependency-free. Tokenizes
    lowercased alphanumeric words and hashes each into a fixed-dimension
    signed bag-of-words vector (blake2b), L2-normalized. Cosine similarity is
    keyword-overlap similarity: enough for "find the artifact by meaning"
    (the query shares words with the content) and fully offline.

  * ``OpenAICompatEmbedder`` — real embeddings via any OpenAI-compatible
    ``/embeddings`` endpoint (``AIOS_EMBED_URL`` / ``AIOS_EMBED_API_KEY`` /
    ``AIOS_EMBED_MODEL``). Used only when configured; never in tests.

``build_embedder_from_env()`` selects the backend. Vectors always travel with
their metadata (namespace, tags, source, TTL) so retrieval can filter before
similarity ranking — cosine similarity only, ``min_score`` is caller-calibrated.
"""

from __future__ import annotations

import hashlib
import math
import os
import re

DEFAULT_DIMENSION = 256


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; returns 0.0 for empty/mismatched vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class Embedder:
    """Minimal backend interface. ``embed`` is async by contract."""

    dimension: int = DEFAULT_DIMENSION

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(Embedder):
    """Deterministic hashed bag-of-words embedder (no external deps)."""

    _TOKEN_RE = re.compile(r"[a-z0-9]+")

    def __init__(self, dimension: int = DEFAULT_DIMENSION):
        self.dimension = dimension

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for token in self._TOKEN_RE.findall((text or "").lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if (digest[4] & 1) else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


class OpenAICompatEmbedder(Embedder):
    """OpenAI-compatible ``/embeddings`` client (httpx; async)."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "text-embedding-3-small",
        dimension: int | None = None,
        timeout_s: float = 30.0,
    ):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimension = dimension or DEFAULT_DIMENSION
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout_s
        )

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            "/embeddings", json={"model": self.model, "input": text}
        )
        resp.raise_for_status()
        vec = [float(v) for v in resp.json()["data"][0]["embedding"]]
        self.dimension = len(vec)
        return vec

    async def aclose(self) -> None:
        await self._client.aclose()


def build_embedder_from_env() -> Embedder:
    """Select the embedder from the environment (OpenAI-compatible if set)."""
    url = os.environ.get("AIOS_EMBED_URL")
    if url:
        return OpenAICompatEmbedder(
            base_url=url,
            api_key=os.environ.get("AIOS_EMBED_API_KEY", ""),
            model=os.environ.get("AIOS_EMBED_MODEL", "text-embedding-3-small"),
        )
    return HashingEmbedder()
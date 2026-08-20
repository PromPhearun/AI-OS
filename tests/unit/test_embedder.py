"""Unit tests: embedding backends and cosine similarity."""

from __future__ import annotations

import asyncio
import math

from aios_kernel.modules.embedder import (
    HashingEmbedder,
    build_embedder_from_env,
    cosine,
)


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    e = HashingEmbedder(dimension=128)
    a = asyncio.run(e.embed("the Q3 revenue analysis"))
    b = asyncio.run(e.embed("the Q3 revenue analysis"))
    assert a == b
    assert len(a) == 128
    norm = math.sqrt(sum(x * x for x in a))
    assert abs(norm - 1.0) < 1e-9


def test_cosine_ranks_related_text_over_unrelated() -> None:
    e = HashingEmbedder()
    q = asyncio.run(e.embed("Q3 revenue analysis"))
    hit = asyncio.run(e.embed("Q3 revenue analysis: revenue grew 18% to $2.4M"))
    miss = asyncio.run(e.embed("weather forecast for tokyo japan"))
    assert cosine(q, hit) > cosine(q, miss)


def test_cosine_mismatched_vectors_is_zero() -> None:
    assert cosine([1.0], [1.0, 2.0]) == 0.0
    assert cosine([], []) == 0.0


def test_build_embedder_defaults_to_hashing() -> None:
    assert isinstance(build_embedder_from_env(), HashingEmbedder)
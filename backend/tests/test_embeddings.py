"""TDD for the pluggable embedder (issue #12).

The default test/CI embedder is a deterministic, dependency-free hashing embedder, so the suite
never touches a network or a model. It must produce fixed-dimension, L2-normalized vectors whose
cosine similarity reflects lexical overlap — that property is what lets the hashing embedder stand
in for a real model when proving nearest-neighbour ordering (test_retrieval.py).
"""

from __future__ import annotations

import json
import math

import pytest

from app.embeddings import (
    HashingEmbedder,
    OllamaEmbedder,
    build_default_embedder,
    embed_in_batches,
    get_embedder,
)

DIM = 768


class _FakeResponse:
    """Stand-in for the urlopen() context manager — the one unavoidable boundary to stub."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def test_hashing_embedder_is_deterministic():
    e = HashingEmbedder(dim=DIM)
    assert e.embed_query("hello world") == e.embed_query("hello world")


def test_hashing_embedder_has_fixed_dim_and_unit_norm():
    e = HashingEmbedder(dim=DIM)
    v = e.embed_query("some words here")
    assert len(v) == DIM
    assert _norm(v) == pytest.approx(1.0)


def test_embed_documents_returns_one_vector_per_input():
    e = HashingEmbedder(dim=DIM)
    out = e.embed_documents(["alpha", "beta", "gamma"])
    assert len(out) == 3
    assert all(len(v) == DIM for v in out)


def test_embed_documents_is_empty_for_empty_input():
    assert HashingEmbedder(dim=DIM).embed_documents([]) == []


def test_lexical_overlap_scores_higher_than_unrelated_text():
    # Cosine similarity (a dot product on unit vectors) tracks shared tokens. This is precisely
    # what makes the hashing embedder good enough to exercise NN ordering without a real model.
    e = HashingEmbedder(dim=DIM)
    q = e.embed_query("invoice payment terms")
    related = e.embed_query("the invoice payment terms are net thirty")
    unrelated = e.embed_query("photosynthesis in tropical rainforest plants")
    assert _dot(q, related) > _dot(q, unrelated)


def test_empty_text_yields_a_zero_vector():
    # No tokens -> nothing to hash -> zero vector (can't be normalized). Real chunks are never
    # empty (chunking drops blanks); this just keeps the embedder total.
    e = HashingEmbedder(dim=DIM)
    v = e.embed_query("   \n\t ")
    assert len(v) == DIM
    assert _norm(v) == pytest.approx(0.0)


def test_get_embedder_reads_the_factory_setting(settings):
    settings.TENANTIQ_EMBEDDER_FACTORY = "app.embeddings.build_fake_embedder"
    e = get_embedder()
    assert isinstance(e, HashingEmbedder)
    assert e.dim == settings.TENANTIQ_EMBEDDING_DIM
    assert e.model


def test_ollama_embedder_posts_to_embed_and_parses_vectors(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["content_type"] = request.headers.get("Content-type")
        return _FakeResponse({"embeddings": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr("app.embeddings.urlopen", fake_urlopen)
    e = OllamaEmbedder(base_url="http://ollama:11434/", model="nomic-embed-text", dim=3)

    assert e.embed_documents(["hello"]) == [[0.1, 0.2, 0.3]]
    assert captured["url"] == "http://ollama:11434/api/embed"  # trailing slash trimmed
    assert captured["body"] == {"model": "nomic-embed-text", "input": ["hello"]}
    assert captured["content_type"] == "application/json"


def test_ollama_embed_query_returns_a_single_vector(monkeypatch):
    monkeypatch.setattr(
        "app.embeddings.urlopen",
        lambda request, timeout=None: _FakeResponse({"embeddings": [[1.0, 0.0]]}),
    )
    assert OllamaEmbedder(base_url="http://ollama:11434", model="m", dim=2).embed_query("hi") == [
        1.0,
        0.0,
    ]


def test_ollama_short_circuits_empty_input(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not call Ollama for empty input")

    monkeypatch.setattr("app.embeddings.urlopen", boom)
    assert (
        OllamaEmbedder(base_url="http://ollama:11434", model="m", dim=2).embed_documents([]) == []
    )


def test_build_default_embedder_reads_ollama_settings(settings):
    settings.OLLAMA_BASE_URL = "http://example:11434"
    settings.TENANTIQ_EMBEDDING_MODEL = "nomic-embed-text"
    settings.TENANTIQ_EMBEDDING_DIM = 768
    e = build_default_embedder()
    assert isinstance(e, OllamaEmbedder)
    assert e.model == "nomic-embed-text"
    assert e.dim == 768


def test_embed_in_batches_matches_unbatched_and_preserves_order():
    e = HashingEmbedder(dim=DIM)
    texts = [f"document number {i} about widgets" for i in range(10)]
    # Batching is an internal detail — the result must be identical to one big call, in order.
    assert embed_in_batches(e, texts, batch_size=3) == e.embed_documents(texts)


def test_embed_in_batches_calls_backend_once_per_batch():
    calls: list[int] = []

    class _Counting(HashingEmbedder):
        def embed_documents(self, texts):
            calls.append(len(texts))
            return super().embed_documents(texts)

    embed_in_batches(_Counting(dim=DIM), ["a", "b", "c", "d", "e"], batch_size=2)
    assert calls == [2, 2, 1]


def test_embed_in_batches_empty_is_no_call():
    calls: list[int] = []

    class _Counting(HashingEmbedder):
        def embed_documents(self, texts):
            calls.append(len(texts))
            return super().embed_documents(texts)

    assert embed_in_batches(_Counting(dim=DIM), [], batch_size=2) == []
    assert calls == []

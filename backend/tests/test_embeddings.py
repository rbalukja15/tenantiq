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
    EmbeddingConfigurationError,
    EmbeddingCountError,
    EmbeddingDimensionError,
    HashingEmbedder,
    OllamaEmbedder,
    VoyageEmbedder,
    build_default_embedder,
    build_voyage_embedder,
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


# --- #46: the embedder boundary rejects a count or dimension mismatch loudly ---


class _DropsTail(HashingEmbedder):
    """A backend that returns one fewer vector than it was given (truncated / contract drift)."""

    def embed_documents(self, texts):
        return super().embed_documents(texts)[:-1]


class _WrongDim(HashingEmbedder):
    """A backend whose vectors are the wrong width (operator pointed at a mismatched model)."""

    def embed_documents(self, texts):
        return [[0.0] * (self.dim + 1) for _ in texts]


class _OverCounts(HashingEmbedder):
    """A backend that returns one *more* vector than it was given."""

    def embed_documents(self, texts):
        vectors = super().embed_documents(texts)
        return vectors + [vectors[0]] if vectors else [[0.0] * self.dim]


class _NonSequence(HashingEmbedder):
    """A backend that returns ``None`` for a vector (e.g. Ollama on a model error)."""

    def embed_documents(self, texts):
        return [None for _ in texts]


def test_embed_in_batches_rejects_a_short_vector_count():
    # A backend returning n-1 vectors would silently drop the tail chunk; refuse it instead.
    with pytest.raises(EmbeddingCountError) as exc:
        embed_in_batches(_DropsTail(dim=DIM), ["a", "b", "c"], batch_size=100)
    assert "2" in str(exc.value) and "3" in str(exc.value)  # names the actual counts


def test_embed_in_batches_rejects_a_wrong_dimension():
    # A wrong-dim vector would later fail cryptically in pgvector; fail here with the config hint.
    with pytest.raises(EmbeddingDimensionError) as exc:
        embed_in_batches(_WrongDim(dim=DIM), ["a", "b"], batch_size=100)
    message = str(exc.value)
    assert str(DIM) in message and str(DIM + 1) in message  # expected vs actual dimension
    assert "TENANTIQ_EMBEDDING_DIM" in message  # points the operator at the setting


def test_embed_in_batches_count_check_spans_batches():
    # The count is validated over the whole result, not per batch, so a tail dropped in the last
    # batch is still caught when batching splits the work.
    with pytest.raises(EmbeddingCountError):
        embed_in_batches(_DropsTail(dim=DIM), ["a", "b", "c", "d", "e"], batch_size=2)


def test_embed_in_batches_rejects_an_over_count():
    # A mismatch in *either* direction is caught — an extra vector is as wrong as a missing one.
    with pytest.raises(EmbeddingCountError):
        embed_in_batches(_OverCounts(dim=DIM), ["a", "b"], batch_size=100)


def test_embed_in_batches_rejects_a_non_sequence_vector():
    # A backend returning a non-vector (None / scalar) must fail with a message that names the
    # problem — not a bare "object of type NoneType has no len()" from the length check.
    with pytest.raises(EmbeddingDimensionError) as exc:
        embed_in_batches(_NonSequence(dim=DIM), ["a", "b"], batch_size=100)
    message = str(exc.value)
    assert "TENANTIQ_EMBEDDING_DIM" in message
    assert "hashing-fake-v1" in message  # names the model, not a Python TypeError


# --- Voyage (#53, ADR-0020) -----------------------------------------------------------------------
#
# The hosted embedder. Stubbed at the same single boundary as Ollama's: everything below asserts on
# the request this builds, because that request is the whole contract — a wrong `input_type` costs
# retrieval quality silently, and a wrong `output_dimension` costs the pgvector column.


def _voyage_payload(*vectors: list[float], shuffled: bool = False) -> dict:
    data = [{"object": "embedding", "embedding": v, "index": i} for i, v in enumerate(vectors)]
    if shuffled:
        data.reverse()
    return {"object": "list", "data": data, "model": "voyage-4", "usage": {"total_tokens": 7}}


def _capture_voyage(monkeypatch, payload: dict) -> dict:
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["authorization"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return _FakeResponse(payload)

    monkeypatch.setattr("app.embeddings.urlopen", fake_urlopen)
    return captured


def test_voyage_embedder_posts_documents_with_the_document_input_type(monkeypatch):
    """`input_type` is a retrieval-quality lever, not a formality.

    Voyage prepends a different retrieval prompt for `document` and `query`, and getting it wrong
    degrades ranking without erroring — so the two Embedder methods must send different values. This
    is the one asymmetry the Protocol's split into embed_documents/embed_query exists to express.
    """
    captured = _capture_voyage(monkeypatch, _voyage_payload([0.1, 0.2, 0.3]))
    embedder = VoyageEmbedder(api_key="test-key", model="voyage-4", dim=3)

    assert embedder.embed_documents(["hello"]) == [[0.1, 0.2, 0.3]]
    assert captured["url"] == "https://api.voyageai.com/v1/embeddings"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["body"]["input_type"] == "document"
    assert captured["body"]["input"] == ["hello"]
    assert captured["body"]["model"] == "voyage-4"


def test_voyage_embedder_posts_a_query_with_the_query_input_type(monkeypatch):
    captured = _capture_voyage(monkeypatch, _voyage_payload([0.4, 0.5, 0.6]))
    embedder = VoyageEmbedder(api_key="test-key", model="voyage-4", dim=3)

    assert embedder.embed_query("hello") == [0.4, 0.5, 0.6]
    assert captured["body"]["input_type"] == "query"


def test_voyage_embedder_pins_the_output_dimension(monkeypatch):
    """The pgvector column is a fixed width, so the dimension is ours to state, not the model's.

    voyage-4 supports 256/512/1024/2048 and defaults to 1024 today. Sending it explicitly means a
    future change to that default cannot quietly start returning vectors the column will not take.
    """
    captured = _capture_voyage(monkeypatch, _voyage_payload([0.0] * 1024))
    VoyageEmbedder(api_key="k", model="voyage-4", dim=1024).embed_documents(["x"])

    assert captured["body"]["output_dimension"] == 1024


def test_voyage_embedder_refuses_silent_truncation(monkeypatch):
    """`truncation` defaults to true, which would embed a *prefix* of an over-long chunk.

    Chunks are ~800 tokens against a 32k limit, so this cannot happen without a bug elsewhere — and
    that is exactly why it should raise rather than return a vector for text nobody chose. A citation
    that resolves to a chunk whose embedding covered half of it is the quiet kind of wrong (#45).
    """
    captured = _capture_voyage(monkeypatch, _voyage_payload([0.1]))
    VoyageEmbedder(api_key="k", model="voyage-4", dim=1).embed_documents(["x"])

    assert captured["body"]["truncation"] is False


def test_voyage_embedder_restores_input_order_from_the_index_field(monkeypatch):
    """Vectors are zipped onto chunks by position, so a reordered response mislabels every chunk.

    The API documents that results come back in input order. This does not rely on it: the response
    carries an explicit `index`, and honouring it costs one sort while trusting it costs a corpus
    whose embeddings belong to the wrong text — a corruption nothing downstream could detect.
    """
    captured = _capture_voyage(monkeypatch, _voyage_payload([1.0, 0.0], [0.0, 1.0], shuffled=True))
    embedder = VoyageEmbedder(api_key="k", model="voyage-4", dim=2)

    assert embedder.embed_documents(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert captured["body"]["input"] == ["first", "second"]


def test_voyage_embedder_without_a_key_fails_before_making_a_request(monkeypatch):
    """No key means no call — a request that would 401 is still a request to a paid API.

    It is also the clearest possible failure: a deployment that forgot VOYAGE_API_KEY should be told
    that, not handed an authentication error from a vendor it may not know it was using.
    """

    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("VoyageEmbedder called the API with no key configured")

    monkeypatch.setattr("app.embeddings.urlopen", explode)

    with pytest.raises(EmbeddingConfigurationError, match="VOYAGE_API_KEY"):
        VoyageEmbedder(api_key="", model="voyage-4", dim=1024).embed_documents(["x"])


def test_voyage_embedder_makes_no_request_for_an_empty_batch(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("VoyageEmbedder called the API with nothing to embed")

    monkeypatch.setattr("app.embeddings.urlopen", explode)

    assert VoyageEmbedder(api_key="k", model="voyage-4", dim=1024).embed_documents([]) == []


def test_voyage_embedder_reports_a_short_response_as_a_count_error(monkeypatch):
    """One vector per input is the contract `embed_in_batches` enforces; catching it here names it."""
    _capture_voyage(monkeypatch, _voyage_payload([0.1, 0.2]))
    embedder = VoyageEmbedder(api_key="k", model="voyage-4", dim=2)

    with pytest.raises(EmbeddingCountError):
        embedder.embed_documents(["first", "second"])


def test_build_voyage_embedder_reads_its_configuration_from_settings(settings):
    settings.VOYAGE_API_KEY = "settings-key"
    settings.TENANTIQ_VOYAGE_MODEL = "voyage-4-lite"
    settings.TENANTIQ_EMBEDDING_DIM = 512

    embedder = build_voyage_embedder()

    assert embedder.api_key == "settings-key"
    assert embedder.model == "voyage-4-lite"
    assert embedder.dim == 512

"""Pluggable text embedding (issue #12, ADR-0004).

An :class:`Embedder` turns text into fixed-dimension vectors for semantic search. The concrete
implementation is chosen at runtime by ``TENANTIQ_EMBEDDER_FACTORY`` (a dotted path to a zero-arg
callable), mirroring the injectable token verifier (ADR-0002, #7): tests and CI use a deterministic,
dependency-free :class:`HashingEmbedder` so the suite never touches a network or a model, while
``make dev`` and real deployments use :class:`OllamaEmbedder`.

Anthropic has no embeddings API, so the project's "Ollama fallback" is the primary embedding source
here; a hosted provider (e.g. Voyage) can drop in later as another factory without touching callers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol, runtime_checkable
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils.module_loading import import_string

_TOKEN_RE = re.compile(r"\w+")


class EmbeddingError(RuntimeError):
    """An embedding backend violated its contract (wrong count or dimension) — see subclasses."""


class EmbeddingCountError(EmbeddingError):
    """The backend returned a different number of vectors than inputs.

    Possibly transient (a truncated / interrupted response), so ingestion lets it propagate and the
    Celery task retries; if it persists, the retries exhaust into an observable FAILED document.
    """


class EmbeddingDimensionError(EmbeddingError):
    """A returned vector's dimension does not match the configured ``TENANTIQ_EMBEDDING_DIM``.

    A static mis-configuration (e.g. pointing at a 1024-dim model with the column/index at 768) that
    cannot self-heal, so ingestion treats it as *permanent* and fails the document immediately rather
    than burning retry backoff on an error every attempt will hit.
    """


@runtime_checkable
class Embedder(Protocol):
    """Turns text into comparable vectors. ``dim`` is fixed; ``model`` identifies the source."""

    model: str
    dim: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic, dependency-free embedder (the "feature hashing" trick).

    Each token is hashed to a coordinate and a sign and accumulated; the vector is then L2-normalized.
    It carries no semantics beyond lexical overlap, but that is enough to exercise storage, indexing,
    and nearest-neighbour *ordering* hermetically — shared tokens raise the cosine similarity.
    """

    def __init__(self, dim: int = 768, *, model: str = "hashing-fake-v1") -> None:
        self.dim = dim
        self.model = model

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OllamaEmbedder:
    """Calls a local Ollama server's ``/api/embed`` (default ``nomic-embed-text``, 768-dim).

    Uses only the standard library (``urllib``) — no new runtime dependency, in keeping with the
    project's lean-deps grain (ADR-0003). The HTTP boundary is the only thing tests stub.
    """

    def __init__(self, *, base_url: str, model: str, dim: int, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read())
        return data["embeddings"]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def build_fake_embedder() -> Embedder:
    """Default embedder under pytest: deterministic, offline, no secrets."""
    return HashingEmbedder(settings.TENANTIQ_EMBEDDING_DIM)


def build_default_embedder() -> Embedder:
    """Default embedder outside tests: a local Ollama server."""
    return OllamaEmbedder(
        base_url=settings.OLLAMA_BASE_URL,
        model=settings.TENANTIQ_EMBEDDING_MODEL,
        dim=settings.TENANTIQ_EMBEDDING_DIM,
    )


def get_embedder() -> Embedder:
    """Instantiate the configured embedder (``TENANTIQ_EMBEDDER_FACTORY``)."""
    factory = settings.TENANTIQ_EMBEDDER_FACTORY
    if not callable(factory):
        factory = import_string(factory)
    return factory()


def embed_in_batches(embedder: Embedder, texts: list[str], batch_size: int) -> list[list[float]]:
    """Embed ``texts`` in fixed-size batches, preserving input order.

    Keeps each backend request bounded for large documents while returning exactly one vector per
    input. Identical to a single ``embed_documents`` call apart from how the work is chunked.

    This is the single choke point every ingestion path (``run_ingestion`` and the
    ``backfill_embeddings`` command) routes through, so it is where the backend's response is
    validated (#46): the returned vectors must match the inputs *one-to-one* and carry the
    configured dimension. Enforcing it here — before any caller ``zip``s vectors onto chunks — means
    a contract-violating backend can never silently drop the tail chunk or store a mis-sized vector.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(embedder.embed_documents(texts[start : start + batch_size]))
    _validate_vectors(
        vectors, expected_count=len(texts), expected_dim=embedder.dim, model=embedder.model
    )
    return vectors


def _validate_vectors(
    vectors: list[list[float]], *, expected_count: int, expected_dim: int, model: str
) -> None:
    """Assert the backend returned exactly ``expected_count`` vectors, each ``expected_dim`` wide.

    A count mismatch raises :class:`EmbeddingCountError` (possibly transient); a width mismatch
    raises :class:`EmbeddingDimensionError` (a permanent mis-configuration). Both messages name the
    actual numbers and the model so the failure reason points straight at the real problem.
    """
    if len(vectors) != expected_count:
        raise EmbeddingCountError(
            f"Embedding backend returned {len(vectors)} vector(s) for {expected_count} input(s) "
            f"(model={model!r}); refusing to persist a partial or mismatched set."
        )
    for vector in vectors:
        try:
            actual_dim = len(vector)
        except TypeError:
            # A non-sequence element (e.g. Ollama returns a null embedding on a model error) —
            # surface it as a dimension problem naming the model, not a bare len() TypeError.
            raise EmbeddingDimensionError(
                f"Embedding backend returned a non-vector value ({type(vector).__name__}) where a "
                f"{expected_dim}-dim vector was expected (TENANTIQ_EMBEDDING_DIM={expected_dim}, "
                f"model={model!r}); check the embedding backend / model configuration."
            ) from None
        if actual_dim != expected_dim:
            raise EmbeddingDimensionError(
                f"Embedding backend returned a {actual_dim}-dim vector but "
                f"TENANTIQ_EMBEDDING_DIM={expected_dim} (model={model!r}); "
                f"check the embedding model / dimension configuration."
            )

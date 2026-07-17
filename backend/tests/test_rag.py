"""TDD for the RAG query engine's retrieval + prompt assembly (#14).

Retrieval itself shipped in #12 (``app.retrieval.nearest_chunks``); this covers the remaining scope:
turning a question into a *grounded* prompt whose sources are numbered and carry the real chunk IDs
and character offsets (#45) a citation resolves to, with a configurable ``k`` and a similarity floor
below which we return "no relevant context" instead of padding the prompt. Generation (#15) and the
HTTP/streaming surface (#48) build on this seam; this layer makes no LLM or network call.

The pure prompt-assembly tests run anywhere. The retrieval-integration tests are Postgres-only
(pgvector distance operators), mirroring ``test_retrieval.py``; threshold paths use explicit vectors
so similarity is exact and the tests never flake.
"""

from __future__ import annotations

import math

import pytest
from django.db import connection

from app.embeddings import get_embedder
from app.models import Chunk, Document, Tenant
from app.rag import Source, build_grounded_prompt, retrieve_context
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)


# --- helpers --------------------------------------------------------------------------------------


def _tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def _seed(tenant: Tenant, texts: list[str]) -> Document:
    """Seed a document whose chunks are embedded from their own text (lexical similarity)."""
    embedder = get_embedder()
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        for i, text in enumerate(texts):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=text,
                char_count=len(text),
                start_offset=0,
                end_offset=len(text),
                embedding=embedder.embed_query(text),
                embedding_model=embedder.model,
            )
    return doc


def _seed_scored(tenant: Tenant, items: list[tuple[str, list[float]]]) -> Document:
    """Seed chunks with explicit embedding vectors so similarity to a query is exact."""
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        for i, (text, vector) in enumerate(items):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=text,
                char_count=len(text),
                start_offset=0,
                end_offset=len(text),
                embedding=vector,
                embedding_model="test",
            )
    return doc


def _orthogonal_to(vec: list[float]) -> list[float]:
    """A unit vector orthogonal to ``vec`` (cosine similarity exactly 0). The hashing embedder is
    sparse, so almost every dimension of a short query is zero — pick the first such dimension."""
    for i, x in enumerate(vec):
        if abs(x) < 1e-12:
            e = [0.0] * len(vec)
            e[i] = 1.0
            return e
    raise AssertionError("query vector has no zero dimension to be orthogonal to")


def _src(number: int, text: str, *, title: str = "Doc", chunk_id: int | None = None) -> Source:
    return Source(
        number=number,
        chunk_id=chunk_id if chunk_id is not None else number * 10,
        document_id=1,
        document_title=title,
        chunk_index=number - 1,
        start_offset=0,
        end_offset=len(text),
        similarity=0.9,
        text=text,
    )


# --- pure prompt assembly (backend-agnostic, no DB or vectors) -------------------------------------


def test_build_grounded_prompt_numbers_sources_and_keeps_the_question():
    sources = (
        _src(1, "Invoice terms are net thirty days.", title="Invoice"),
        _src(2, "Returns are accepted within fourteen days.", title="Refunds"),
    )
    system, user = build_grounded_prompt("when is payment due?", sources)

    assert "[1]" in user and "[2]" in user
    assert "Invoice terms are net thirty days." in user
    assert "Returns are accepted within fourteen days." in user
    assert "Invoice" in user and "Refunds" in user  # source titles surface for grounding
    assert "when is payment due?" in user


def test_system_prompt_encodes_the_grounding_invariant():
    system, _ = build_grounded_prompt("q", (_src(1, "some source text"),))
    lowered = system.lower()
    assert "only" in lowered  # answer only from the sources
    assert "cite" in lowered  # must cite
    assert "never" in lowered  # never invent/compute (CLAUDE.md invariant)


def test_build_grounded_prompt_with_no_sources_instructs_a_refusal():
    system, user = build_grounded_prompt("what is the capital of France?", ())

    assert "[1]" not in user
    assert "what is the capital of France?" in user  # question preserved
    assert "relevant" in user.lower() or "don't have" in user.lower()  # signals no context


# --- retrieval integration (Postgres/pgvector) ----------------------------------------------------


@requires_postgres
def test_retrieve_context_grounds_the_answer_in_tenant_chunks():
    a = _tenant("acme")
    _seed(
        a,
        [
            "Invoice payment terms are net thirty days after receipt.",
            "The office holiday party is scheduled for December.",
            "Refund policy: returns are accepted within fourteen days.",
        ],
    )
    with tenant_context(a):
        ctx = retrieve_context("when is payment due on an invoice?", k=3, min_similarity=0.0)
        real_ids = set(Chunk.objects.values_list("id", flat=True))

    assert ctx.has_context
    # Every source resolves to a real chunk id owned by this tenant.
    assert {s.chunk_id for s in ctx.sources} <= real_ids
    # Numbered 1..n, in descending similarity (nearest first).
    assert [s.number for s in ctx.sources] == list(range(1, len(ctx.sources) + 1))
    sims = [s.similarity for s in ctx.sources]
    assert sims == sorted(sims, reverse=True)
    # The payment chunk is the top source and its text is in the prompt the model will see.
    assert "payment terms" in ctx.sources[0].text
    assert ctx.sources[0].text in ctx.user_prompt


@requires_postgres
def test_sources_carry_offsets_for_citation_resolution():
    a = _tenant("acme")
    _seed(a, ["Invoice payment terms are net thirty days after receipt."])
    with tenant_context(a):
        ctx = retrieve_context("invoice payment terms", k=1, min_similarity=0.0)
        chunk = Chunk.objects.get()

    assert ctx.sources
    src = ctx.sources[0]
    assert src.chunk_id == chunk.id
    assert (src.start_offset, src.end_offset) == (chunk.start_offset, chunk.end_offset)


@requires_postgres
def test_below_threshold_returns_no_relevant_context():
    a = _tenant("acme")
    question = "quarterly invoice payment terms net thirty"
    qvec = get_embedder().embed_query(question)
    orth = _orthogonal_to(qvec)
    _seed_scored(a, [("holiday party in December", orth), ("weekly meeting notes", orth)])

    with tenant_context(a):
        ctx = retrieve_context(question, k=3, min_similarity=0.5)

    assert not ctx.has_context
    assert ctx.sources == ()
    assert question in ctx.user_prompt  # the question is still there...
    assert "relevant" in ctx.user_prompt.lower() or "don't have" in ctx.user_prompt.lower()


@requires_postgres
def test_threshold_keeps_only_chunks_that_clear_the_bar():
    a = _tenant("acme")
    question = "quarterly invoice payment terms net thirty"
    qvec = get_embedder().embed_query(question)
    orth = _orthogonal_to(qvec)
    # One chunk identical to the query (similarity 1.0); one orthogonal (similarity 0.0).
    _seed_scored(a, [("net thirty payment terms", qvec), ("unrelated party notes", orth)])

    with tenant_context(a):
        ctx = retrieve_context(question, k=3, min_similarity=0.5)

    assert [s.text for s in ctx.sources] == ["net thirty payment terms"]
    assert ctx.sources[0].similarity == pytest.approx(1.0, abs=1e-6)


@requires_postgres
def test_retrieve_context_respects_explicit_k():
    a = _tenant("acme")
    _seed(a, [f"document fragment number {i}" for i in range(8)])
    with tenant_context(a):
        ctx = retrieve_context("fragment", k=2, min_similarity=0.0)
    assert len(ctx.sources) <= 2


@requires_postgres
def test_retrieve_context_reads_k_from_settings(settings):
    settings.TENANTIQ_RETRIEVAL_TOP_K = 1
    a = _tenant("acme")
    _seed(a, [f"document fragment number {i}" for i in range(5)])
    with tenant_context(a):
        ctx = retrieve_context("fragment", min_similarity=0.0)  # k falls back to settings
    assert len(ctx.sources) <= 1


@requires_postgres
def test_retrieve_context_cannot_ground_in_another_tenants_chunks():
    a, b = _tenant("acme"), _tenant("globex")
    _seed(a, ["Acme confidential revenue numbers and product roadmap."])
    _seed(b, ["Globex weekly meeting notes, entirely unrelated."])

    with tenant_context(a):
        a_ids = set(Chunk.objects.values_list("id", flat=True))
    with tenant_context(b):
        ctx = retrieve_context("acme confidential revenue roadmap", k=5, min_similarity=0.0)

    assert all(s.chunk_id not in a_ids for s in ctx.sources)  # never A's chunks
    assert all("Acme" not in s.text for s in ctx.sources)
    assert "Acme" not in ctx.user_prompt  # and A's text never reaches the prompt


def test_unit_vector_helper_is_actually_orthogonal():
    # Guards the fixture itself: the "orthogonal" vector really has cosine similarity ~0.
    qvec = get_embedder().embed_query("quarterly invoice payment terms net thirty")
    orth = _orthogonal_to(qvec)
    dot = sum(x * y for x, y in zip(qvec, orth))
    assert math.isclose(dot, 0.0, abs_tol=1e-9)

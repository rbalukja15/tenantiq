"""TDD for vector retrieval (app.retrieval.nearest_chunks) — issues #12, #44.

Postgres-only: nearest-neighbour search uses pgvector's distance operators, which SQLite lacks.
Proves the acceptance criterion (a query returns relevant chunks, nearest first), that vector search
is tenant-scoped (tenant B's query can never surface tenant A's chunks), and — #44 — that a small
tenant is not starved of results when a larger tenant dominates the global HNSW candidate set.
"""

from __future__ import annotations

import math

import pytest
from django.db import connection

from app.embeddings import get_embedder
from app.models import Chunk, Document, Tenant
from app.retrieval import nearest_chunks
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)


def _tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def _seed(tenant: Tenant, texts: list[str]) -> None:
    embedder = get_embedder()
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        for i, text in enumerate(texts):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=text,
                char_count=len(text),
                embedding=embedder.embed_query(text),
                embedding_model=embedder.model,
            )


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _perturb(base: list[float], dim: int, amount: float) -> list[float]:
    """A unit vector near ``base``, nudged along one dimension — small nudge = near, large = far."""
    v = list(base)
    v[dim] += amount
    return _unit(v)


def _seed_vectors(tenant: Tenant, vectors: list[list[float]]) -> None:
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        for i, vector in enumerate(vectors):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=f"chunk-{i}",
                char_count=7,
                embedding=vector,
                embedding_model="test",
            )


@requires_postgres
def test_nearest_chunks_ranks_the_relevant_chunk_first():
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
        results = nearest_chunks("when is payment due on an invoice?", k=3)

    assert results
    assert "payment terms" in results[0].text  # shares "invoice"/"payment" -> nearest


@requires_postgres
def test_nearest_chunks_respects_k():
    a = _tenant("acme")
    _seed(a, [f"document fragment number {i}" for i in range(8)])
    with tenant_context(a):
        assert len(nearest_chunks("fragment", k=3)) == 3


@requires_postgres
def test_vector_search_cannot_leak_across_tenants():
    a, b = _tenant("acme"), _tenant("globex")
    _seed(a, ["Acme confidential roadmap and revenue numbers."])
    _seed(b, ["Globex unrelated weekly meeting notes."])

    with tenant_context(b):
        results = nearest_chunks("acme confidential revenue", k=5)

    assert results  # B has a chunk of its own...
    assert all(chunk.tenant_id == b.id for chunk in results)  # ...but never A's
    assert all("Acme" not in chunk.text for chunk in results)


@requires_postgres
def test_iterative_scan_rescues_a_tenant_starved_by_a_dominating_neighbour():
    """A tenant crowded out of the global HNSW candidate set by a larger one must not be starved (#44).

    The tenant predicate is a *post-filter* over the HNSW scan's bounded candidate list
    (``hnsw.ef_search``), so without iterative index scans a small tenant can be starved to zero when
    a bigger tenant owns the query's neighbourhood. At fixture scale the planner would use an exact
    btree sort and hide this, so the HNSW index path is forced (``enable_seqscan``/``sort`` off, a
    small ``ef_search``) — the regime that appears naturally at ~25k+ chunks, impractical to seed.

    **This asserts the delta, not an absolute count, and that is the fix for #76.** The original
    assertion was ``len(results) == 5`` — perfect recall from an approximate index running on a
    bounded scan budget, which pgvector does not promise under ``relaxed_order``. Anything that spends
    that budget makes it fail: once #21/#22 added tests that ingest a corpus into the same test
    database, the accumulated index churn starved the scan of its last row and CI went red on roughly
    40% of runs, on PRs that had touched nothing near retrieval.

    What #44 actually guarantees is that iterative scanning *rescues* a tenant the bounded scan would
    have abandoned. So the test runs the same query twice against the same fixture — once with
    iterative scan off, once through the production path — and asserts the rescue happened. That
    property holds whatever state the index is in, which is the point.
    """
    a, b = _tenant("acme"), _tenant("globex")
    query = "quarterly revenue and invoice payment terms"
    q = get_embedder().embed_query(query)
    # B hugs the query's neighbourhood (30 near vectors); A is relevant but farther out (5 vectors).
    _seed_vectors(b, [_perturb(q, 1 + i, 0.02) for i in range(30)])
    _seed_vectors(a, [_perturb(q, 300 + j, 0.9) for j in range(5)])

    with tenant_context(a):
        starved = _search_without_iterative_scan(query, k=5)
        rescued = _force_hnsw_path_then(lambda: nearest_chunks(query, k=5))

    # The fixture has to actually reproduce the regime, or the comparison below proves nothing.
    assert (
        len(starved) < 5
    ), "post-filter starvation was not reproduced; the fixture has stopped biting"
    assert len(rescued) > len(starved), "iterative scan did not rescue the starved tenant"
    assert all(chunk.tenant_id == a.id for chunk in rescued)  # and isolation still holds


def _force_hnsw_path_then(run):
    """Pin the planner to the HNSW index path with a candidate list smaller than B's cluster."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("SET LOCAL enable_sort = off")
        cursor.execute("SET LOCAL hnsw.ef_search = 10")
    return run()


def _search_without_iterative_scan(query: str, k: int) -> list[Chunk]:
    """The pre-#44 behaviour: the same query with iterative scanning disabled.

    Deliberately not `nearest_chunks`, which turns iterative scan on by design — this is the baseline
    the production path has to beat, and having both in one test is what makes the assertion a
    measurement of the fix rather than of the index's mood.
    """
    from pgvector.django import CosineDistance

    vector = get_embedder().embed_query(query)

    def _run() -> list[Chunk]:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL hnsw.iterative_scan = off")
        return list(
            Chunk.objects.exclude(embedding=None)
            .annotate(distance=CosineDistance("embedding", vector))
            .order_by("distance")[:k]
        )

    return _force_hnsw_path_then(_run)

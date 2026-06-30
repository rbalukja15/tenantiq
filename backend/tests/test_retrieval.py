"""TDD for vector retrieval (app.retrieval.nearest_chunks) — issue #12.

Postgres-only: nearest-neighbour search uses pgvector's distance operators, which SQLite lacks.
Proves the acceptance criterion (a query returns relevant chunks, nearest first) AND that vector
search is tenant-scoped — tenant B's query can never surface tenant A's chunks.
"""

from __future__ import annotations

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

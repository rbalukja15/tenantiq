"""Vector retrieval: find the chunks most similar to a query (#12).

This is the nearest-neighbour primitive the RAG query engine (M3) will build on. It deliberately
stays small — embed the query, order the tenant's chunks by cosine distance, return the top ``k``.

Tenant isolation is automatic and load-bearing: ``Chunk.objects`` is the tenant-scoped manager
(ADR-0002, Layer 1), so a search can only ever see the active tenant's chunks — and Postgres RLS
(Layer 2) backs that at the database. Callers must run inside a ``tenant_context``.
"""

from __future__ import annotations

from pgvector.django import CosineDistance

from app.embeddings import get_embedder
from app.models import Chunk


def nearest_chunks(query: str, k: int = 5) -> list[Chunk]:
    """Return the active tenant's ``k`` chunks most similar to ``query`` (nearest first)."""
    query_vector = get_embedder().embed_query(query)
    return list(
        Chunk.objects.exclude(embedding=None).order_by(CosineDistance("embedding", query_vector))[
            :k
        ]
    )

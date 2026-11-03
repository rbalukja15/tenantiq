"""Vector retrieval: find the chunks most similar to a query (#12, #44).

This is the nearest-neighbour primitive the RAG query engine (M3) will build on. It deliberately
stays small — embed the query, order the tenant's chunks by cosine distance, return the top ``k``.

Tenant isolation is automatic and load-bearing: ``Chunk.objects`` is the tenant-scoped manager
(ADR-0002, Layer 1), so a search can only ever see the active tenant's chunks — and Postgres RLS
(Layer 2) backs that at the database. Callers must run inside a ``tenant_context``.

Each search logs one line (#26, ADR-0024): the model that embedded the query, how many chunks came
back, and the time spent embedding and searching, so a slow answer can be traced to the provider or
the index. Never the query itself.
"""

from __future__ import annotations

import logging
import time

from django.db import connection
from pgvector.django import CosineDistance

from app.embeddings import get_embedder
from app.log_context import ms_since
from app.models import Chunk

logger = logging.getLogger(__name__)


def nearest_chunks(query: str, k: int = 5) -> list[Chunk]:
    """Return the active tenant's ``k`` chunks most similar to ``query`` (nearest first).

    Only chunks embedded by the active embedder's model are candidates (#53). Two models of the same
    width produce vectors the column accepts and the distance operator happily compares, but the
    numbers mean nothing across spaces: after a provider switch without a re-embed, an unfiltered
    search would return confident neighbours from the wrong one. Filtering on ``embedding_model``
    turns that into no results, which the caller already answers as "no relevant context".
    """
    started = time.perf_counter()
    embedder = get_embedder()
    query_vector = embedder.embed_query(query)
    embed_ms = ms_since(started)
    _enable_iterative_index_scan()
    chunks = list(
        Chunk.objects.exclude(embedding=None)
        .filter(embedding_model=embedder.model)
        .annotate(distance=CosineDistance("embedding", query_vector))
        .order_by("distance")[:k]
    )
    # Iterative scan uses relaxed_order (below), which trades exact index order for recall, so the k
    # survivors can come back slightly out of order. Re-rank them by their true distance — cheap at
    # this k — to keep the "nearest first" contract exact.
    chunks.sort(key=lambda chunk: chunk.distance)
    duration_ms = ms_since(started)
    logger.info(
        "retrieved %d of %d chunks in %d ms",
        len(chunks),
        k,
        duration_ms,
        extra={
            "event": "retrieval",
            "embedding_model": embedder.model,
            "k": k,
            "candidates": len(chunks),
            "embed_ms": embed_ms,
            "search_ms": duration_ms - embed_ms,
            "duration_ms": duration_ms,
        },
    )
    return chunks


def _enable_iterative_index_scan() -> None:
    """Make the HNSW scan keep fetching candidates until ``k`` survive the tenant filter (#44).

    The tenant predicate (scoped manager + RLS) is applied *after* the HNSW index returns its
    bounded ``hnsw.ef_search`` candidate list, so on the shared, all-tenant index a tenant whose
    rows fall outside that global candidate set can be starved of results — for a large tenant,
    down to zero. Iterative index scans (pgvector 0.8+) re-scan with a growing candidate list until
    ``k`` rows survive the filter. We use ``relaxed_order`` because it recalls more within the scan
    budget than ``strict_order`` (which stops early and can still return fewer than ``k``); exact
    ordering is then restored in Python by :func:`nearest_chunks`. ``SET LOCAL`` scopes this to the
    surrounding transaction (``tenant_context`` opens one on Postgres), so it never leaks across a
    pooled connection. Per-tenant partitioning / partial indexes are the longer-term scale path
    (ADR-0004).
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")

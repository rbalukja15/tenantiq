"""Migration 0014 widens the embedding column 768 -> 1024 and discards every vector (#53, ADR-0020).

Two of its properties can only be proven against Postgres, so this is Postgres-only:

- it reaches **every tenant's** rows despite FORCE'd RLS, which is why it clears by DDL — an UPDATE
  run with no tenant set matches none of them;
- it **reverses**, both from an empty table and from a backfilled column, which is the state any
  real rollback starts from. The first version could do neither: it cast 1024-dim vectors to 768
  (`expected 768 dimensions, not 1024`), and on an empty table it left the column with no width for
  the recreated HNSW index to refuse (`column does not have dimensions`).

The rows are written with raw SQL under each tenant's GUC because the ORM model describes the latest
schema, not the 0013 one the seed has to fit.
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from app.tenant_context import GUC

pytestmark = pytest.mark.django_db(transaction=True)

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="the vector column and RLS are Postgres-only"
)

BEFORE = [("app", "0013_user_display_name")]
AFTER = [("app", "0014_alter_chunk_embedding")]
TENANTS = {
    "11111111-1111-1111-1111-111111111111": "acme",
    "22222222-2222-2222-2222-222222222222": "globex",
}


def _migrate(targets) -> None:
    # A fresh executor per move: it reads the applied set from the database, which the previous move
    # just changed.
    MigrationExecutor(connection).migrate(targets)


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _as_tenant(tenant_id: str, sql: str, params: list | None = None) -> list[tuple]:
    """Run ``sql`` with the tenant's GUC set, as the app role — so RLS applies, like the app."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [GUC, tenant_id])
        cursor.execute(sql, params or [])
        return cursor.fetchall() if cursor.description else []


def _seed(dims: int) -> None:
    with connection.cursor() as cursor:
        for tenant_id, slug in TENANTS.items():
            cursor.execute(
                "INSERT INTO app_tenant (id, slug, name, oidc_issuer, oidc_client_id, is_active,"
                " created_at) VALUES (%s, %s, %s, %s, %s, true, now())",
                [tenant_id, slug, slug, f"https://keycloak.test/realms/{slug}", slug],
            )
    for tenant_id, slug in TENANTS.items():
        _as_tenant(
            tenant_id,
            "INSERT INTO app_document (title, created_at, tenant_id, content_type, file,"
            " original_filename, size_bytes, status, attempts, error, updated_at)"
            " VALUES (%s, now(), %s, 'text/plain', %s, %s, 4, 'ready', 1, '', now())",
            [f"{slug}.txt", tenant_id, f"{slug}.txt", f"{slug}.txt"],
        )
        _as_tenant(
            tenant_id,
            "INSERT INTO app_chunk (index, text, char_count, token_estimate, created_at,"
            " document_id, tenant_id, embedding, embedding_model, start_offset, end_offset)"
            " SELECT 0, %s, 4, 1, now(), id, tenant_id,"
            " array_fill(0.1::real, ARRAY[%s])::vector, 'nomic-embed-text', 0, 4"
            " FROM app_document",
            [f"{slug} text", dims],
        )


def _chunks(tenant_id: str) -> list[tuple]:
    return _as_tenant(
        tenant_id, "SELECT text, embedding IS NULL, embedding_model FROM app_chunk ORDER BY id"
    )


def _column_type() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
            " WHERE attrelid = 'app_chunk'::regclass AND attname = 'embedding'"
        )
        return cursor.fetchone()[0]


def _has_hnsw_index() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'app_chunk_embedding_hnsw'")
        return cursor.fetchone() is not None


@requires_postgres
def test_0014_clears_every_tenants_vectors_and_reverses_from_a_backfilled_column():
    latest = _latest()
    try:
        _migrate(BEFORE)
        _seed(dims=768)

        # The trap the DDL exists for: with no tenant set, FORCE'd RLS hides every row from DML.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE app_chunk SET embedding = NULL")
            assert cursor.rowcount == 0

        _migrate(AFTER)
        assert _column_type() == "vector(1024)"
        assert _has_hnsw_index()
        for tenant_id, slug in TENANTS.items():
            # Every tenant's vector and label are gone; the text the citations resolve to is not.
            assert _chunks(tenant_id) == [(f"{slug} text", True, "")]

        # What `backfill_embeddings` leaves behind, and so where a real rollback starts.
        for tenant_id in TENANTS:
            _as_tenant(
                tenant_id,
                "UPDATE app_chunk SET embedding = array_fill(0.3::real, ARRAY[1024])::vector,"
                " embedding_model = 'voyage-4'",
            )

        _migrate(BEFORE)
        assert _column_type() == "vector(768)"
        assert _has_hnsw_index()
        for tenant_id, slug in TENANTS.items():
            assert _chunks(tenant_id) == [(f"{slug} text", True, "")]
    finally:
        _migrate(latest)

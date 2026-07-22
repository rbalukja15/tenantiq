"""TDD for Layer 2: Postgres row-level security — the database backstop (ADR-0002).

Skipped off Postgres (SQLite has no RLS). These tests bypass the ORM with raw SQL to prove the
*database* refuses cross-tenant reads and writes even when application scoping is circumvented —
and that it bites the app's own role (non-superuser, FORCE RLS), not merely some restricted role.
"""

from __future__ import annotations

import pytest
from django.db import Error, connection, transaction

from app.models import Chunk, Document, Tenant
from app.tenant_context import GUC, tenant_context

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row-level security is a Postgres-only backstop"
)


def make_tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


@requires_postgres
def test_rls_filters_raw_select_to_the_active_tenant():
    a, b = make_tenant("acme"), make_tenant("globex")
    with tenant_context(a):
        Document.objects.create(title="a-doc")
    with tenant_context(b):
        Document.objects.create(title="b-doc")

    with tenant_context(a):  # GUC = acme
        with connection.cursor() as cur:
            cur.execute("SELECT title FROM app_document")  # ORM scoping deliberately bypassed
            assert [row[0] for row in cur.fetchall()] == ["a-doc"]
    with tenant_context(b):  # GUC = globex
        with connection.cursor() as cur:
            cur.execute("SELECT title FROM app_document")
            assert [row[0] for row in cur.fetchall()] == ["b-doc"]


@requires_postgres
def test_rls_returns_no_rows_when_no_tenant_is_set():
    a = make_tenant("acme")
    with tenant_context(a):
        Document.objects.create(title="a-doc")
    with connection.cursor() as cur:
        cur.execute("RESET app.current_tenant")  # simulate a request with no tenant resolved
        cur.execute("SELECT count(*) FROM app_document")
        assert cur.fetchone()[0] == 0


@requires_postgres
def test_rls_filters_chunks_to_the_active_tenant():
    # The new tenant-owned app_chunk table (0006) must get the same DB backstop as documents.
    a, b = make_tenant("acme"), make_tenant("globex")
    with tenant_context(a):
        doc = Document.objects.create(title="d")
        Chunk.objects.create(document=doc, index=0, text="acme-chunk", char_count=10)
    with tenant_context(a):
        with connection.cursor() as cur:
            cur.execute("SELECT text FROM app_chunk")
            assert [row[0] for row in cur.fetchall()] == ["acme-chunk"]
    with tenant_context(b):
        with connection.cursor() as cur:
            cur.execute("SELECT text FROM app_chunk")
            assert cur.fetchall() == []


@requires_postgres
def test_rls_rejects_writing_another_tenants_row():
    a, b = make_tenant("acme"), make_tenant("globex")
    with pytest.raises(Error):  # WITH CHECK violation; atomic() rolls back its savepoint cleanly
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT set_config(%s, %s, true)", [GUC, str(a.id)])
                cur.execute(
                    "INSERT INTO app_document (title, created_at, tenant_id) VALUES (%s, now(), %s)",
                    ["x", str(b.id)],
                )


@requires_postgres
def test_rls_rejects_updating_another_tenants_row():
    # The USING clause hides B's row from A's session, so a raw UPDATE targeting it matches nothing.
    a, b = make_tenant("acme"), make_tenant("globex")
    with tenant_context(b):
        doc = Document.objects.create(title="b-doc")
    with tenant_context(a):  # GUC = acme
        with connection.cursor() as cur:
            cur.execute("UPDATE app_document SET title = 'hacked' WHERE id = %s", [str(doc.id)])
            assert cur.rowcount == 0  # nothing updated — B's row is invisible to A
    with tenant_context(b):
        doc.refresh_from_db()
        assert doc.title == "b-doc"  # untouched


@requires_postgres
def test_rls_rejects_deleting_another_tenants_row():
    a, b = make_tenant("acme"), make_tenant("globex")
    with tenant_context(b):
        doc = Document.objects.create(title="b-doc")
    with tenant_context(a):  # GUC = acme
        with connection.cursor() as cur:
            cur.execute("DELETE FROM app_document WHERE id = %s", [str(doc.id)])
            assert cur.rowcount == 0  # nothing deleted — B's row is invisible to A
    with tenant_context(b):
        assert Document.objects.filter(id=doc.id).exists()  # still there


@requires_postgres
def test_every_tenant_owned_table_has_forced_rls():
    """Meta-guard: every concrete ``TenantOwnedModel`` table must have RLS enabled + forced with the
    tenant-isolation policy. A new tenant-owned table added without its RLS migration fails here — so
    Layer 2 no longer depends on remembering a hand-written migration per table (#50)."""
    from django.apps import apps

    from app.models import TenantOwnedModel

    tables = sorted(
        model._meta.db_table
        for model in apps.get_models()
        if issubclass(model, TenantOwnedModel) and not model._meta.abstract
    )
    assert {"app_document", "app_chunk"} <= set(tables)  # not vacuous: the known tables are present

    with connection.cursor() as cur:
        for table in tables:
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [table],
            )
            flags = cur.fetchone()
            assert flags is not None, f"{table} missing from pg_class"
            enabled, forced = flags
            assert enabled, f"RLS is not ENABLED on {table} — add its RLS migration"
            assert forced, f"RLS is not FORCED on {table} — the app role would bypass the policy"

            cur.execute(
                "SELECT qual, with_check FROM pg_policies WHERE tablename = %s AND policyname = %s",
                [table, "tenant_isolation"],
            )
            policy = cur.fetchone()
            assert policy is not None, f"no tenant_isolation policy on {table}"
            qual, with_check = policy
            for clause in (qual, with_check):
                assert clause and "current_setting" in clause and "tenant_id" in clause

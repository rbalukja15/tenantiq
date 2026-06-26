"""TDD for Layer 2: Postgres row-level security — the database backstop (ADR-0002).

Skipped off Postgres (SQLite has no RLS). These tests bypass the ORM with raw SQL to prove the
*database* refuses cross-tenant reads and writes even when application scoping is circumvented —
and that it bites the app's own role (non-superuser, FORCE RLS), not merely some restricted role.
"""

from __future__ import annotations

import pytest
from django.db import Error, connection, transaction

from app.models import Document, Tenant
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

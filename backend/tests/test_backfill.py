"""TDD for the embeddings backfill command (#12).

``manage.py backfill_embeddings`` fills chunks whose embedding is NULL — chunks created before #12,
or by a future re-chunk — tenant by tenant, respecting tenant scoping. Idempotent: a second run is a
no-op. ``--tenant`` limits it to one tenant.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from app.models import Chunk, Document, Tenant
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db


def _tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def _chunk_without_embedding(tenant: Tenant, text: str) -> None:
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        # bulk_create skips save() and we omit embedding -> it stays NULL, like a pre-#12 chunk.
        Chunk.objects.bulk_create(
            [Chunk(tenant=tenant, document=doc, index=0, text=text, char_count=len(text))]
        )


def test_backfill_fills_null_embeddings():
    a = _tenant("acme")
    _chunk_without_embedding(a, "some text that still needs an embedding vector")
    with tenant_context(a):
        assert Chunk.objects.filter(embedding__isnull=True).count() == 1

    call_command("backfill_embeddings")

    with tenant_context(a):
        assert Chunk.objects.filter(embedding__isnull=True).count() == 0
        chunk = Chunk.objects.get()
        assert chunk.embedding is not None
        assert chunk.embedding_model


def test_backfill_is_idempotent():
    a = _tenant("acme")
    _chunk_without_embedding(a, "text")
    call_command("backfill_embeddings")

    out = StringIO()
    call_command("backfill_embeddings", stdout=out)

    with tenant_context(a):
        assert Chunk.objects.filter(embedding__isnull=True).count() == 0
    assert "0 chunk" in out.getvalue()  # nothing left to do


def test_backfill_can_target_a_single_tenant():
    a, b = _tenant("acme"), _tenant("globex")
    _chunk_without_embedding(a, "acme text")
    _chunk_without_embedding(b, "globex text")

    call_command("backfill_embeddings", "--tenant", "acme")

    with tenant_context(a):
        assert Chunk.objects.filter(embedding__isnull=True).count() == 0
    with tenant_context(b):
        assert Chunk.objects.filter(embedding__isnull=True).count() == 1  # left untouched

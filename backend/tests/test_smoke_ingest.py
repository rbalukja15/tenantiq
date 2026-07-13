"""Hermetic guard for the `smoke_ingest` command (#23).

The command is the live acceptance check for `docker compose up` (real broker + worker + Ollama).
Here it runs under eager Celery with the hashing embedder, so the suite exercises its logic — create
a tenant + document, drive it to READY, verify every chunk is embedded — without any live services.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from app.models import Chunk, Document, Tenant
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db


def test_smoke_ingest_drives_a_document_to_ready(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    out = StringIO()

    call_command("smoke_ingest", "--tenant", "smoke", "--timeout", "10", stdout=out)

    assert "READY" in out.getvalue()
    tenant = Tenant.objects.get(slug="smoke")
    with tenant_context(tenant):
        doc = Document.objects.filter(title="smoke.txt").latest("id")
        assert doc.status == Document.Status.READY
        chunks = Chunk.objects.filter(document=doc)
        assert chunks.exists()
        assert not chunks.filter(embedding__isnull=True).exists()  # every chunk embedded

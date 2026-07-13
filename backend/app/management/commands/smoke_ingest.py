"""Smoke-test the composed stack end to end (#23).

Ingests a small document the way the app does — enqueue to Celery, let the *real* worker parse,
chunk, and embed it via the *real* Ollama backend — and waits for it to reach READY. This proves the
worker and embedder services actually run and are wired to the same database + media volume; it is
the acceptance check for `docker compose up`. Run it against the running stack:

    docker compose exec backend python manage.py smoke_ingest      # or: make smoke

It goes through the ingestion pipeline rather than the authenticated HTTP upload (which needs a live
Keycloak realm + token); the API/auth path is covered by the test suite.
"""

from __future__ import annotations

import time

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from app.models import Chunk, Document, Tenant
from app.tasks import ingest_document
from app.tenant_context import tenant_context

_SAMPLE = (
    "TenantIQ smoke test document.\n\n"
    "Invoice payment terms are net thirty days after receipt. Late payments accrue interest at one "
    "percent per month.\n\n"
    "The refund policy allows returns within fourteen days of delivery for a full refund."
).encode()


class Command(BaseCommand):
    help = "Ingest a sample document through the real worker + embedder and wait for READY (#23)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", default="smoke", help="Tenant slug to use (created if new)."
        )
        parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for READY.")

    def handle(self, *args, **options):
        slug = options["tenant"]
        tenant, _ = Tenant.objects.get_or_create(
            slug=slug,
            defaults={
                "name": slug.title(),
                "oidc_issuer": f"https://keycloak.invalid/realms/{slug}",
                "oidc_client_id": slug,
            },
        )

        with tenant_context(tenant):
            doc = Document.objects.create(
                title="smoke.txt",
                content_type="text/plain",
                original_filename="smoke.txt",
                size_bytes=len(_SAMPLE),
                file=ContentFile(_SAMPLE, name="smoke.txt"),
            )
        self.stdout.write(f"queued document {doc.id} for tenant '{slug}'; waiting for the worker…")
        ingest_document.delay(doc.id, tenant.id)  # real broker -> real worker -> real embedder

        deadline = time.monotonic() + options["timeout"]
        status = doc.status  # bound up front so a non-positive --timeout still times out cleanly
        while time.monotonic() < deadline:
            with tenant_context(tenant):
                doc.refresh_from_db()
                status = doc.status
            if status == Document.Status.READY:
                with tenant_context(tenant):
                    total = Chunk.objects.filter(document=doc).count()
                    embedded = Chunk.objects.filter(document=doc, embedding__isnull=False).count()
                if embedded != total or total == 0:
                    raise CommandError(
                        f"document READY but embeddings are incomplete: {embedded}/{total} chunks."
                    )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"READY — {total} chunk(s), all embedded. Worker + embedder are live."
                    )
                )
                return
            if status == Document.Status.FAILED:
                raise CommandError(f"ingestion FAILED: {doc.error}")
            time.sleep(2)

        raise CommandError(
            f"timed out after {options['timeout']}s (last status: {status}). "
            "Is the worker running and can it reach Ollama?"
        )

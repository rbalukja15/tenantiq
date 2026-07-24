"""Re-ingest documents so a pipeline change applies to already-stored data (#16 PII backfill).

Redaction runs at ingest, so documents ingested before #16 still carry raw PII in their stored
chunks. This command re-runs the idempotent ingestion pipeline (``app.ingestion.run_ingestion``) for
each matching document, tenant by tenant. Re-ingestion re-reads the file, redacts, re-chunks and
re-embeds, replacing the old chunk set atomically — so no chunk is left holding pre-redaction PII and
no embedding is left stale. It is also the general "re-chunk / re-embed after a strategy change" tool.

Tenant-scoped: document ids are collected inside each tenant's ``tenant_context`` (both isolation
layers apply), so a backfill limited to one tenant never touches another's data.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from app.ingestion import run_ingestion
from app.models import Document, Tenant
from app.tenant_context import tenant_context


class Command(BaseCommand):
    help = "Re-ingest documents (re-parse, redact, re-chunk, re-embed), tenant by tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Limit the backfill to this tenant slug.")
        parser.add_argument(
            "--status",
            help="Only re-ingest documents in this status (default: any document that has chunks).",
        )

    def handle(self, *args, **options):
        tenants = Tenant.objects.all()
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])

        total = 0
        for tenant in tenants:
            # Collect ids inside the scoped context; run_ingestion re-enters its own context per doc.
            with tenant_context(tenant):
                docs = Document.objects.all()
                if options["status"]:
                    docs = docs.filter(status=options["status"])
                else:
                    # Default target is "documents that hold chunks" — the ones that could carry
                    # pre-redaction PII — NOT status=ready: a re-ingest that failed permanently leaves
                    # stale chunks on a now-FAILED/PROCESSING document, which a status=ready filter
                    # would strand (review #16). run_ingestion is idempotent, so re-touching a READY
                    # doc is safe.
                    docs = docs.filter(chunks__isnull=False).distinct()
                doc_ids = list(docs.values_list("id", flat=True))
            for doc_id in doc_ids:
                run_ingestion(doc_id, tenant.id)
            if doc_ids:
                total += len(doc_ids)
                self.stdout.write(f"{tenant.slug}: re-ingested {len(doc_ids)} document(s)")

        self.stdout.write(self.style.SUCCESS(f"Re-ingestion complete: {total} document(s)."))

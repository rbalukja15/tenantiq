"""Generate embeddings for chunks that don't have a current one (#12, #53).

Use after changing the embedding model, or to embed chunks created before #12. A chunk needs one
when its ``embedding`` is NULL or its ``embedding_model`` is not the active embedder's: retrieval
searches only chunks the active model embedded (#53), so a corpus left over from a switch between
two models of the same width would otherwise stay unsearchable. A chunk's text is stored exactly as
ingestion embedded it (redacted and chunked), so re-embedding needs no source file and no re-ingest.
Runs tenant by tenant inside a ``tenant_context`` so both isolation layers apply (the scoped manager
+ RLS), and is idempotent: a second run finds nothing to do.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from app.embeddings import embed_in_batches, get_embedder
from app.models import Chunk, Tenant
from app.tenant_context import tenant_context


class Command(BaseCommand):
    help = "Embed chunks with no embedding, or one from another model, tenant by tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Limit the backfill to this tenant slug.")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=settings.TENANTIQ_EMBED_BATCH_SIZE,
            help="How many chunks to embed per backend call.",
        )

    def handle(self, *args, **options):
        tenants = Tenant.objects.all()
        if options["tenant"]:
            tenants = tenants.filter(slug=options["tenant"])

        embedder = get_embedder()
        total = 0
        for tenant in tenants:
            with tenant_context(tenant):
                stale = Q(embedding__isnull=True) | ~Q(embedding_model=embedder.model)
                pending = list(Chunk.objects.filter(stale).order_by("id"))
                if not pending:
                    continue
                # embed_in_batches validates the count + dimension of the backend's response (#46),
                # so a contract-violating backend aborts the command here with a clear error instead
                # of writing a mismatched set; strict=True guards the one-to-one zip belt-and-braces.
                vectors = embed_in_batches(
                    embedder, [chunk.text for chunk in pending], options["batch_size"]
                )
                for chunk, vector in zip(pending, vectors, strict=True):
                    chunk.embedding = vector
                    chunk.embedding_model = embedder.model
                Chunk.objects.bulk_update(pending, ["embedding", "embedding_model"])
                total += len(pending)
                self.stdout.write(f"{tenant.slug}: embedded {len(pending)} chunk(s)")

        self.stdout.write(self.style.SUCCESS(f"Backfill complete: {total} chunk(s) embedded."))

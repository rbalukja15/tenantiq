"""Generate embeddings for chunks that don't have one yet (#12).

Use after changing the embedding model, or to embed chunks created before #12. Runs tenant by
tenant inside a ``tenant_context`` so both isolation layers apply (the scoped manager + RLS), and is
idempotent — it only touches rows where ``embedding IS NULL``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from app.embeddings import embed_in_batches, get_embedder
from app.models import Chunk, Tenant
from app.tenant_context import tenant_context


class Command(BaseCommand):
    help = "Backfill embeddings for chunks whose embedding is NULL, tenant by tenant."

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
                pending = list(Chunk.objects.filter(embedding__isnull=True).order_by("id"))
                if not pending:
                    continue
                vectors = embed_in_batches(
                    embedder, [chunk.text for chunk in pending], options["batch_size"]
                )
                for chunk, vector in zip(pending, vectors):
                    chunk.embedding = vector
                    chunk.embedding_model = embedder.model
                Chunk.objects.bulk_update(pending, ["embedding", "embedding_model"])
                total += len(pending)
                self.stdout.write(f"{tenant.slug}: embedded {len(pending)} chunk(s)")

        self.stdout.write(self.style.SUCCESS(f"Backfill complete: {total} chunk(s) embedded."))

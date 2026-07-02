"""Add the per-chunk embedding vector + its source model (#12, ADR-0004).

``VectorExtension`` runs ``CREATE EXTENSION IF NOT EXISTS vector`` first (a no-op off Postgres) so
the ``vector(768)`` column can be created. SQLite tolerates the column type (lax typing), so the
fast unit-test path still migrates; the HNSW index that makes search fast is Postgres-only (0008).
"""

from __future__ import annotations

import pgvector.django.vector
from django.db import migrations, models
from pgvector.django import VectorExtension


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0006_chunk_rls"),
    ]

    operations = [
        VectorExtension(),
        migrations.AddField(
            model_name="chunk",
            name="embedding",
            field=pgvector.django.vector.VectorField(
                blank=True, dimensions=768, editable=False, null=True
            ),
        ),
        migrations.AddField(
            model_name="chunk",
            name="embedding_model",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]

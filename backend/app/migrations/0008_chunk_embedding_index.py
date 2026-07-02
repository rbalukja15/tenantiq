"""Add an HNSW index on the chunk embedding for fast cosine nearest-neighbour search (#12).

Managed by hand (like the RLS migrations) rather than via ``Meta.indexes`` so it stays Postgres-only:
HNSW and the ``vector_cosine_ops`` operator class don't exist on SQLite, so this is a no-op there.
``CosineDistance`` ordering works with or without the index — the index just makes it scale.
"""

from __future__ import annotations

from django.db import migrations

INDEX = "app_chunk_embedding_hnsw"
TABLE = "app_chunk"


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} "
            "USING hnsw (embedding vector_cosine_ops)"
        )


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {INDEX}")


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0007_chunk_embedding"),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index),
    ]

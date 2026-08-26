"""Widen the embedding column from 768 to 1024 dimensions (#53, ADR-0020).

The production embedder is now Voyage (`voyage-4`), and locally Ollama runs `mxbai-embed-large`.
Both emit **1024**-dim vectors; `nomic-embed-text`'s 768 does not fit and 768 is not one of the
widths `voyage-4` can emit, so there is no configuration that avoids this.

**Every existing embedding is discarded.** A 768-dim vector cannot be widened into a meaningful
1024-dim one — the numbers mean nothing in the new space — so the honest operation is to drop them
and re-embed. `manage.py backfill_embeddings` then refills exactly the rows this nulls, because it
only touches `embedding IS NULL`. Documents keep their text, their chunks and their offsets; only
the vectors go. Until the backfill runs, retrieval returns nothing (`nearest_chunks` excludes null
embeddings) rather than returning wrong things.

Three ordering constraints, each of which fails loudly if ignored:

1. **The HNSW index is dropped first.** It is built on `vector(768)` and an index cannot survive its
   column changing width.
2. **The data is cleared by DDL, not by an UPDATE.** `app_chunk` has FORCE'd row-level security
   (ADR-0002), and a migration runs with no `app.current_tenant` GUC set — so `UPDATE app_chunk SET
   embedding = NULL` matches **zero rows** and silently succeeds, leaving every old vector in place
   for the ALTER to choke on. RLS governs DML, not DDL, so `ALTER TABLE ... TYPE ... USING NULL`
   reaches every row regardless of tenant. This is the same trap that bit the evaluation harness's
   cleanup in #21.
3. **The index is recreated last**, on the new column.

SQLite has neither RLS nor a real vector type, so it takes the ORM path: the historical model here
carries a plain manager (custom managers are not used in migrations), which is what makes an
unscoped update possible at all.
"""

from __future__ import annotations

import pgvector.django.vector
from django.db import migrations

INDEX = "app_chunk_embedding_hnsw"
TABLE = "app_chunk"


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {INDEX}")


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} "
            "USING hnsw (embedding vector_cosine_ops)"
        )


def clear_embeddings(apps, schema_editor):
    """Discard every stored vector and the model label that described it."""
    if schema_editor.connection.vendor == "postgresql":
        # DDL, so RLS does not filter it — see the module docstring. `USING NULL` rewrites the table
        # with an empty column, which also makes the AlterField below a formality rather than a cast
        # that would fail on 768-dim data.
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {TABLE} ALTER COLUMN embedding TYPE vector USING NULL")
            cursor.execute(
                f"ALTER TABLE {TABLE} ALTER COLUMN embedding_model TYPE varchar(100) USING ''"
            )
        return
    apps.get_model("app", "Chunk").objects.update(embedding=None, embedding_model="")


def unclear_embeddings(apps, schema_editor):
    """Reversing this migration cannot bring the old vectors back; it only restores the width."""
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {TABLE} ALTER COLUMN embedding TYPE vector USING NULL")


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0013_user_display_name"),
    ]

    operations = [
        migrations.RunPython(drop_index, create_index),
        migrations.RunPython(clear_embeddings, unclear_embeddings),
        migrations.AlterField(
            model_name="chunk",
            name="embedding",
            field=pgvector.django.vector.VectorField(
                blank=True, dimensions=1024, editable=False, null=True
            ),
        ),
        migrations.RunPython(create_index, drop_index),
    ]

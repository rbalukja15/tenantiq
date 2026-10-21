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

Four ordering constraints, each of which fails loudly if ignored:

1. **The HNSW index is dropped first.** It is built on `vector(768)` and an index cannot survive its
   column changing width.
2. **The data is cleared by DDL, not by an UPDATE.** `app_chunk` has FORCE'd row-level security
   (ADR-0002), and a migration runs with no `app.current_tenant` GUC set — so `UPDATE app_chunk SET
   embedding = NULL` matches **zero rows** and silently succeeds, leaving every old vector in place
   for the ALTER to choke on. RLS governs DML, not DDL, so `ALTER TABLE ... TYPE ... USING NULL`
   reaches every row regardless of tenant. This is the same trap that bit the evaluation harness's
   cleanup in #21.
3. **The index is recreated last**, on the new column.
4. **Reversing clears the column before narrowing it.** Django unwinds operations last-first, so the
   clear that serves the reverse sits *after* the AlterField: it runs first on the way back, and the
   AlterField's reverse then casts an empty column to `vector(768)`. Clearing in the AlterField's
   own position fails both ways back: a backfilled column cannot be cast to `vector(768)`
   (`expected 768 dimensions, not 1024`), and an empty one is left with no width at all, which the
   HNSW index, recreated as the last step back, refuses (`column does not have dimensions`).

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
    """Discard every stored vector and the model label that described it.

    Runs in both directions (constraint 4): forward before the AlterField widens the column, and in
    reverse before it narrows it. Reversing cannot bring the old vectors back; it restores the width.
    """
    if schema_editor.connection.vendor == "postgresql":
        # DDL, so RLS does not filter it — see the module docstring. `USING NULL` rewrites the table
        # with an empty column, which also makes the AlterField a formality in either direction rather
        # than a cast that would fail on vectors of the other width.
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {TABLE} ALTER COLUMN embedding TYPE vector USING NULL")
            cursor.execute(
                f"ALTER TABLE {TABLE} ALTER COLUMN embedding_model TYPE varchar(100) USING ''"
            )
        return
    apps.get_model("app", "Chunk").objects.update(embedding=None, embedding_model="")


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0013_user_display_name"),
    ]

    operations = [
        migrations.RunPython(drop_index, create_index),
        migrations.RunPython(clear_embeddings, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="chunk",
            name="embedding",
            field=pgvector.django.vector.VectorField(
                blank=True, dimensions=1024, editable=False, null=True
            ),
        ),
        # The reverse's clear (constraint 4). Unwound before the AlterField, so its narrowing cast
        # meets an empty column rather than 1024-dim vectors.
        migrations.RunPython(migrations.RunPython.noop, clear_embeddings),
        migrations.RunPython(create_index, drop_index),
    ]

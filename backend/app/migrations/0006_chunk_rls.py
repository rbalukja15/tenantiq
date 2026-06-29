"""Extend Layer 2 (Postgres RLS, ADR-0002) to the new tenant-owned ``app_chunk`` table.

Same forced policy as ``0003_tenant_rls`` — a no-op off Postgres. Each new tenant-owned table needs
its own RLS migration because migrations are immutable once merged.
"""

from __future__ import annotations

from django.db import migrations

TENANT_OWNED_TABLES = ["app_chunk"]

POLICY = "tenant_isolation"
GUC = "app.current_tenant"
PREDICATE = f"tenant_id = NULLIF(current_setting('{GUC}', true), '')::uuid"


def enable_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TENANT_OWNED_TABLES:
            cursor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            cursor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            cursor.execute(
                f"CREATE POLICY {POLICY} ON {table} "
                f"USING ({PREDICATE}) WITH CHECK ({PREDICATE})"
            )


def disable_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TENANT_OWNED_TABLES:
            cursor.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
            cursor.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            cursor.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0005_chunk"),
    ]

    operations = [
        migrations.RunPython(enable_rls, disable_rls),
    ]

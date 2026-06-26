"""Layer 2 of ADR-0002: enable + force Postgres row-level security on every tenant-owned table.

The policy reads the ``app.current_tenant`` session variable that the app sets with ``SET LOCAL``
per request (see ``app.tenant_context``). ``FORCE`` makes the policy apply to the table owner too,
so the (non-superuser) app role cannot read or write across tenants even via raw SQL. A no-op off
Postgres so the SQLite test path is unaffected.
"""

from __future__ import annotations

from django.db import migrations

# Every table inheriting TenantOwnedModel. New tenant-owned models add their table here.
TENANT_OWNED_TABLES = ["app_document"]

POLICY = "tenant_isolation"
GUC = "app.current_tenant"
# No tenant set -> NULL (or '' once the GUC has been touched on a pooled connection) -> NULLIF
# normalizes both to NULL, so the predicate matches no row and never raises on an empty ''::uuid.
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
        ("app", "0002_document"),
    ]

    operations = [
        migrations.RunPython(enable_rls, disable_rls),
    ]

-- Bootstraps the non-superuser application role for the TenantIQ backend.
--
-- Postgres row-level security (ADR-0002, Layer 2) is bypassed by superusers and BYPASSRLS roles,
-- so the app must connect as an ordinary role for the policies to actually enforce. This role
-- owns the schema (so migrations can create/alter tables) and can create databases (so the test
-- runner can build its throwaway test DB), but is NOT a superuser and cannot bypass RLS.
--
-- Runs once, as the bootstrap superuser, on first DB init (mounted into
-- /docker-entrypoint-initdb.d). To re-run after changing it: `docker compose down -v`.
-- CI mirrors this in .github/workflows/ci.yml.
CREATE ROLE tenantiq_app LOGIN PASSWORD 'tenantiq_app' NOSUPERUSER NOBYPASSRLS CREATEDB;
GRANT ALL ON DATABASE tenantiq TO tenantiq_app;
ALTER SCHEMA public OWNER TO tenantiq_app;

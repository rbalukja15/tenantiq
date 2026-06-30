-- Provision the pgvector extension as the superuser (#12, ADR-0004).
--
-- pgvector 0.8 is not a "trusted" extension, so the non-superuser tenantiq_app role cannot
-- CREATE EXTENSION itself — an admin must install it. We create it in template1 so every database
-- cloned from it (notably the pytest test database) inherits it, and in the app database directly
-- (which was created from template0 before this script ran). The app's 0007 migration then finds
-- the extension already present and its CREATE EXTENSION IF NOT EXISTS is a privilege-free no-op.
\connect template1
CREATE EXTENSION IF NOT EXISTS vector;
\connect tenantiq
CREATE EXTENSION IF NOT EXISTS vector;

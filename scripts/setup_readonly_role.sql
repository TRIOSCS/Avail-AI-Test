-- setup_readonly_role.sql — create the least-privilege `availai_ro` login role used by
-- the Claude Code `postgres` MCP server for read-only DB inspection.
--
-- WHY: the MCP should never be able to mutate the live DB. `availai_ro` has SELECT only
-- (no INSERT/UPDATE/DELETE/DDL, no SUPERUSER/CREATEDB/CREATEROLE). ALTER DEFAULT
-- PRIVILEGES makes future tables created by the app role (`availai`) auto-grant SELECT,
-- so new migrations stay readable without re-running the GRANT.
--
-- RUN (as the `availai` superuser; supply the password as a psql variable so it is never
-- written to disk):
--   docker exec -i availai-db-1 psql -U availai -d availai \
--     -v ON_ERROR_STOP=1 -v ro_pw="$(openssl rand -hex 24)" -f - < scripts/setup_readonly_role.sql
-- Then put the same password into the `postgres` MCP connection string in
-- ~/.claude/settings.json: postgresql://availai_ro:<pw>@127.0.0.1:5432/availai
--
-- Idempotent: safe to re-run (creates the role if missing, otherwise just resets its
-- password and re-applies the grants).

SELECT (NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'availai_ro'))::text AS need_create \gset
\if :need_create
CREATE ROLE availai_ro LOGIN PASSWORD :'ro_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\else
ALTER ROLE availai_ro LOGIN PASSWORD :'ro_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\endif

GRANT CONNECT ON DATABASE availai TO availai_ro;
GRANT USAGE ON SCHEMA public TO availai_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO availai_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO availai_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE availai IN SCHEMA public GRANT SELECT ON TABLES TO availai_ro;

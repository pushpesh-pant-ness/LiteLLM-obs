-- Local mode only (the bundled gateway Postgres). Never applied to a production database.
-- Lock the public schema to LiteLLM's own user, so grafana_ro / rpt_* cannot even list its tables.
ALTER DEFAULT PRIVILEGES FOR ROLE litellm IN SCHEMA public REVOKE ALL ON TABLES FROM grafana_ro;
REVOKE USAGE ON SCHEMA public FROM grafana_ro;
REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO litellm;

-- Replication user for postgres-replica (idempotent; password from psql variable repl_pw).
SELECT format('CREATE ROLE replicator WITH LOGIN REPLICATION PASSWORD %L', :'repl_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') \gexec
SELECT format('ALTER ROLE replicator WITH LOGIN REPLICATION PASSWORD %L', :'repl_pw') \gexec
ALTER SYSTEM SET wal_keep_size = '256MB';
SELECT pg_reload_conf();

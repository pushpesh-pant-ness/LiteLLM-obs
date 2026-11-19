-- grafana_ro (admin Grafana org) may read the reporting schema - and nothing else.
-- No LiteLLM tables: no keys, credentials, users, or prompt/response text.
-- Runs in both modes (local Postgres, or the production LiteLLM database in attach mode), so it
-- only touches roles and schemas this stack owns. Password from psql variable ro_pw.
SELECT format('CREATE ROLE grafana_ro WITH LOGIN PASSWORD %L', :'ro_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') \gexec
SELECT format('ALTER ROLE grafana_ro WITH LOGIN PASSWORD %L', :'ro_pw') \gexec

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_ro', current_database()) \gexec
GRANT USAGE ON SCHEMA reporting TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO grafana_ro;
ALTER ROLE grafana_ro SET search_path = reporting;
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
ALTER ROLE grafana_ro SET statement_timeout = '30s';

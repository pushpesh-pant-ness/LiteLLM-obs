-- Lock LiteLLM's public schema to LiteLLM's own user, so grafana_ro cannot even list its tables
-- (keys, credentials, users). Idempotent; applied by service db-setup on every `up`.
ALTER DEFAULT PRIVILEGES FOR ROLE litellm IN SCHEMA public REVOKE ALL ON TABLES FROM grafana_ro;
REVOKE USAGE ON SCHEMA public FROM grafana_ro;
REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO litellm;

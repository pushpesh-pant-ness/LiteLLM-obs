#!/bin/bash
# Runs once, on the first start of an empty pg-data volume.
# Creates LiteLLM's database + owner and the read-only grafana_ro login.
# Grafana's grants on the reporting views are applied later by service db-setup (postgres/sql/),
# after LiteLLM has created its tables.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" \
     -v litellm_pw="$LITELLM_DB_PASSWORD" -v ro_pw="$GRAFANA_RO_DB_PASSWORD" <<-'EOSQL'
	SELECT format('CREATE USER litellm WITH PASSWORD %L', :'litellm_pw') \gexec
	CREATE DATABASE litellm OWNER litellm;
	SELECT format('CREATE USER grafana_ro WITH PASSWORD %L', :'ro_pw') \gexec
	ALTER USER grafana_ro SET default_transaction_read_only = on;
	ALTER USER grafana_ro SET statement_timeout = '30s';
	GRANT CONNECT ON DATABASE litellm TO grafana_ro;
EOSQL

#!/bin/bash
# Runs once, on the first start of an empty lf-pg-data volume: Langfuse's own Postgres.
# Separate from the gateway DB so Langfuse load can never slow down LiteLLM.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -v lf_pw="$LANGFUSE_DB_PASSWORD" <<-'EOSQL'
	SELECT format('CREATE USER langfuse WITH PASSWORD %L', :'lf_pw') \gexec
	CREATE DATABASE langfuse OWNER langfuse;
	REVOKE CONNECT ON DATABASE langfuse FROM PUBLIC;
	GRANT CONNECT ON DATABASE langfuse TO langfuse;
EOSQL

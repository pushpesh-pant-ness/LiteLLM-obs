#!/bin/bash
# Runs once, on first start of an empty langfuse-db volume: Langfuse's own Postgres.
# Kept separate from the gateway DB so Langfuse load can never slow down LiteLLM.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
	CREATE USER langfuse WITH PASSWORD '${LANGFUSE_DB_PASSWORD}';
	CREATE DATABASE langfuse OWNER langfuse;
	REVOKE CONNECT ON DATABASE langfuse FROM PUBLIC;
	GRANT CONNECT ON DATABASE langfuse TO langfuse;
EOSQL

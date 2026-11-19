#!/bin/bash
# Runs once, on first start of an empty gateway Postgres volume.
# Creates: the litellm DB, the read-only grafana_ro user, and the replicator user that the
# postgres-replica service streams from. (Langfuse has its own Postgres: service langfuse-db.)
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
	CREATE USER litellm WITH PASSWORD '${LITELLM_DB_PASSWORD}';
	CREATE DATABASE litellm OWNER litellm;

	CREATE USER grafana_ro WITH PASSWORD '${GRAFANA_RO_DB_PASSWORD}';
	ALTER USER grafana_ro SET default_transaction_read_only = on;
	ALTER USER grafana_ro SET statement_timeout = '30s';

	CREATE USER replicator WITH REPLICATION PASSWORD '${REPLICATION_PASSWORD}';
	ALTER SYSTEM SET wal_keep_size = '256MB';
EOSQL

# Allow the replica to stream WAL (password auth, compose network only).
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"

# Read access for Grafana is granted on the reporting schema only, after LiteLLM migrates its
# tables - see postgres/sql/ (service db-setup).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname litellm <<-EOSQL
	GRANT CONNECT ON DATABASE litellm TO grafana_ro;
EOSQL

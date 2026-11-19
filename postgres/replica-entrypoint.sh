#!/bin/bash
# Streaming read replica of the gateway Postgres (service postgres-replica).
# First start: clone the primary with pg_basebackup. Afterwards: start as a hot standby.
# Grafana reads from here, so heavy dashboard queries never touch the primary.
set -euo pipefail

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: cloning primary..."
  until pg_isready -h postgres -p 5432 -U replicator >/dev/null 2>&1; do sleep 2; done
  mkdir -p "$PGDATA"
  chown -R postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
  PGPASSWORD="$REPLICATION_PASSWORD" gosu postgres \
    pg_basebackup -h postgres -p 5432 -U replicator -D "$PGDATA" -X stream -R -P
  echo "replica: clone done"
fi
chown -R postgres:postgres "$PGDATA"
exec gosu postgres postgres -c hot_standby=on -c max_standby_streaming_delay=60s

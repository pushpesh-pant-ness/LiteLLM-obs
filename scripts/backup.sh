#!/usr/bin/env bash
# Back up everything that cannot be rebuilt from this repo:
#   - LiteLLM Postgres (teams, keys, budgets, spend = money source of truth)
#   - Langfuse Postgres (users, orgs, projects, settings)
#   - ClickHouse (Langfuse traces)
# Backups go to the pg-backups / ch-backups Docker volumes (not the repo, not OneDrive).
# Keeps the last $KEEP_DAYS days.  Schedule it nightly, e.g. cron:  15 2 * * *  cd /srv/llm-obs && ./scripts/backup.sh
#
#   ./scripts/backup.sh          # back up now
#   ./scripts/backup.sh --list   # list existing backups
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
KEEP_DAYS=${KEEP_DAYS:-7}
TS=$(date -u +%Y%m%d-%H%M%S)
CH_PW=$(grep -E '^CLICKHOUSE_PASSWORD=' .env | cut -d= -f2-)

# Langfuse DB lives in its own Postgres once phase 2 is applied (service langfuse-db).
# attach mode (setup.yml): the LiteLLM database is production's - backed up by its owners, not here.
LOCAL_DB=true; [[ "$(grep -E '^OBS_MODE=' .env | cut -d= -f2-)" == attach ]] && LOCAL_DB=false
LF_SVC=postgres
if docker compose ps --services --status running 2>/dev/null | grep -qx langfuse-db; then LF_SVC=langfuse-db; fi

if [[ "${1:-}" == "--list" ]]; then
  $LOCAL_DB && { echo "== Postgres backups (pg-backups volume)"; docker compose exec -T postgres sh -c 'ls -lh /backups | tail -n +2'; }
  [[ $LF_SVC == langfuse-db ]] && { echo "== Langfuse DB backups"; docker compose exec -T langfuse-db sh -c 'ls -lh /backups | tail -n +2'; }
  echo "== ClickHouse backups (ch-backups volume)"; docker compose exec -T clickhouse sh -c 'ls -lh /backups | tail -n +2'
  exit 0
fi

if $LOCAL_DB; then
  echo "== LiteLLM Postgres"
  docker compose exec -T postgres sh -c "pg_dump -U postgres -Fc litellm > /backups/litellm-$TS.dump && ls -lh /backups/litellm-$TS.dump"
else
  echo "== LiteLLM Postgres: production database (attach mode) - not backed up here"
fi
echo "== Langfuse Postgres ($LF_SVC)"
docker compose exec -T "$LF_SVC" sh -c "pg_dump -U postgres -Fc langfuse > /backups/langfuse-$TS.dump && ls -lh /backups/langfuse-$TS.dump"
if $LOCAL_DB; then
  echo "== Postgres roles (needed to restore grants)"
  docker compose exec -T postgres sh -c "pg_dumpall -U postgres --roles-only > /backups/roles-$TS.sql"
fi

echo "== ClickHouse"
# The volume is created root-owned; ClickHouse runs as uid 101.
docker compose exec -T -u root clickhouse chown 101:101 /backups
docker compose exec -T clickhouse clickhouse-client --user clickhouse --password "$CH_PW" \
  -q "BACKUP DATABASE default TO Disk('backups', 'clickhouse-$TS.zip')" | awk '{print "  backup id " $1 ": " $2}'

echo "== Prune backups older than $KEEP_DAYS days"
$LOCAL_DB && docker compose exec -T postgres find /backups -type f -mtime +"$KEEP_DAYS" -print -delete
[[ $LF_SVC == langfuse-db ]] && docker compose exec -T langfuse-db find /backups -type f -mtime +"$KEEP_DAYS" -print -delete
docker compose exec -T clickhouse find /backups -type f -mtime +"$KEEP_DAYS" -print -delete
echo "Backup $TS done."

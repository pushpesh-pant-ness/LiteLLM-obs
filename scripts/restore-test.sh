#!/usr/bin/env bash
# Prove the newest backups can actually be restored - without touching live data.
# Restores each into a scratch database (restore_check), compares row counts with live, drops it.
# Run after scripts/backup.sh, and at least monthly.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
CH_PW=$(grep -E '^CLICKHOUSE_PASSWORD=' .env | cut -d= -f2-)
# attach mode (setup.yml): the LiteLLM database is production's - backed up by its owners, not here.
LOCAL_DB=true; [[ "$(grep -E '^OBS_MODE=' .env | cut -d= -f2-)" == attach ]] && LOCAL_DB=false
LF_SVC=postgres
if docker compose ps --services --status running 2>/dev/null | grep -qx langfuse-db; then LF_SVC=langfuse-db; fi
fail=0

pg_check() {  # service db file-prefix "table1 table2 ..."
  local svc=$1 db=$2 prefix=$3 tables=$4
  local f; f=$(docker compose exec -T "$svc" sh -c "ls -1t /backups/$prefix-*.dump 2>/dev/null | head -1")
  [[ -z "$f" ]] && { echo "  FAIL  no $prefix backup found - run scripts/backup.sh"; fail=1; return; }
  echo "  restoring $f into restore_check ($svc)"
  docker compose exec -T "$svc" sh -c "dropdb -U postgres --if-exists restore_check && createdb -U postgres restore_check && pg_restore -U postgres -d restore_check --no-owner $f" >/dev/null
  for t in $tables; do
    live=$(docker compose exec -T "$svc" psql -U postgres -d "$db" -tAc "select count(*) from \"$t\"")
    rest=$(docker compose exec -T "$svc" psql -U postgres -d restore_check -tAc "select count(*) from \"$t\"")
    # live may have grown since the backup; restored must be > 0 when live is, and never more than live
    if [[ "$rest" -gt 0 || "$live" -eq 0 ]] && [[ "$rest" -le "$live" ]]; then
      echo "  PASS  $db.$t: restored $rest rows (live now $live)"
    else
      echo "  FAIL  $db.$t: restored $rest rows, live $live"; fail=1
    fi
  done
  docker compose exec -T "$svc" dropdb -U postgres restore_check
}

if $LOCAL_DB; then
  echo "== LiteLLM Postgres"
  pg_check postgres litellm litellm "LiteLLM_TeamTable LiteLLM_VerificationToken LiteLLM_SpendLogs LiteLLM_DailyTeamSpend"
fi
echo "== Langfuse Postgres"
pg_check "$LF_SVC" langfuse langfuse "users organizations projects api_keys"

echo "== ClickHouse"
ch() { docker compose exec -T clickhouse clickhouse-client --user clickhouse --password "$CH_PW" -q "$1"; }
f=$(docker compose exec -T clickhouse sh -c "ls -1t /backups/clickhouse-*.zip 2>/dev/null | head -1 | xargs -r basename")
if [[ -z "$f" ]]; then
  echo "  FAIL  no ClickHouse backup found"; fail=1
else
  echo "  restoring $f into database restore_check"
  ch "DROP DATABASE IF EXISTS restore_check SYNC"
  ch "RESTORE DATABASE default AS restore_check FROM Disk('backups', '$f')" >/dev/null
  for t in traces observations; do
    live=$(ch "SELECT count() FROM default.$t"); rest=$(ch "SELECT count() FROM restore_check.$t")
    if [[ "$rest" -gt 0 && "$rest" -le "$live" ]]; then echo "  PASS  clickhouse.$t: restored $rest rows (live now $live)"
    else echo "  FAIL  clickhouse.$t: restored $rest, live $live"; fail=1; fi
  done
  ch "DROP DATABASE restore_check SYNC"
fi

if (( fail )); then echo "RESTORE TEST FAILED"; exit 1; fi
echo "RESTORE TEST PASSED"

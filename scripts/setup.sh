#!/usr/bin/env bash
# One command from setup.yml to a verified stack. Safe to re-run (every step is idempotent).
#
#   ./scripts/setup.sh            # everything: config -> checks -> up -> tenants -> verify
#   ./scripts/setup.sh --check    # only validate setup.yml and test connections; changes nothing outside .env
#   ./scripts/setup.sh --no-verify
#
# Steps
#   1. .env          created with random secrets if missing (scripts/gen-env.sh)
#   2. render        setup.yml -> managed block in .env (+ prometheus/prometheus.attach.yml)
#   3. preflight     attach mode: production LiteLLM, admin key, callbacks, metrics targets, database
#   4. tenants file  attach mode: created from the teams in LiteLLM if missing -> stops for review
#   5. up            docker compose up -d (which services depends on the mode)
#   6. tenants       scripts/apply-tenants.sh (teams/callbacks, DB scopes, Grafana + Langfuse orgs, alerts)
#   7. verify        scripts/verify.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

CHECK_ONLY=false; VERIFY=true
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=true ;;
    --no-verify) VERIFY=false ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

ROOT=$(pwd -W 2>/dev/null || pwd)          # Windows path for Docker Desktop under Git Bash
SETUP_FILE=${SETUP_FILE:-setup.yml}           # e.g. SETUP_FILE=setup.prod.yml ./scripts/setup.sh
PY_IMAGE=python:3.12-alpine
PG_IMAGE=postgres:16
step() { echo; echo "######## $1"; }
envval() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
py() { docker run --rm --pull never -e SETUP_FILE="$SETUP_FILE" -v "$ROOT:/w" -w /w "$PY_IMAGE" python -I scripts/setup.py "$@"; }

for img in "$PY_IMAGE" "$PG_IMAGE"; do
  docker image inspect "$img" >/dev/null 2>&1 || { echo "image $img is not on this machine (pull it once it is cleared)"; exit 1; }
done

if [[ ! -f "$SETUP_FILE" ]]; then
  cp setup.example.yml "$SETUP_FILE"
  echo "Created $SETUP_FILE from setup.example.yml - fill it in (mode, LiteLLM URL, database...), then run this again."
  exit 1
fi

step "1/7 .env"
if [[ -f .env ]]; then echo "  exists (secrets kept)"; else ./scripts/gen-env.sh >/dev/null && echo "  created with random secrets"; fi

step "2/7 $SETUP_FILE -> .env"
py render || { echo; echo "Fix the above and run again."; exit 1; }
MODE=$(envval OBS_MODE)

step "3/7 preflight"
if [[ "$MODE" == "attach" ]]; then
  py preflight || { echo; echo "Fix the above and run again (./scripts/setup.sh --check to re-test)."; exit 1; }
  if [[ "$(envval DB_SETUP)" == "auto" ]]; then
    # Can admin_user create what db-setup needs? (read-only queries; password via env, not argv)
    q="SELECT current_user,
              (SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user),
              has_database_privilege(current_database(), 'CREATE'),
              to_regclass('public.\"LiteLLM_SpendLogs\"') IS NOT NULL,
              to_regclass('public.\"LiteLLM_DailyTeamSpend\"') IS NOT NULL,
              CASE WHEN to_regclass('public.\"LiteLLM_SpendLogs\"') IS NULL THEN false
                   ELSE has_table_privilege('public.\"LiteLLM_SpendLogs\"', 'SELECT') END,
              pg_is_in_recovery()"
    if res=$(PGPASSWORD="$(envval DB_ADMIN_PASSWORD)" docker run --rm --pull never -e PGPASSWORD "$PG_IMAGE" \
          psql "host=$(envval DB_ADMIN_HOST) port=$(envval DB_ADMIN_PORT) dbname=$(envval LITELLM_DB_NAME) user=$(envval DB_ADMIN_USER) sslmode=$(envval DB_SSLMODE) connect_timeout=10 application_name=llm-obs-preflight" \
          -tA -F' ' -c "$q" 2>&1); then
      read -r who createrole create spendlogs daily readable standby <<<"$res"
      ok=true
      chk() { if [[ "$2" == t ]]; then echo "  PASS  $1"; else echo "  FAIL  $1"; ok=false; fi; }
      echo "  PASS  database login as $who"
      chk "$who may create roles (CREATEROLE) - for grafana_ro and one role per team/org" "$createrole"
      chk "$who may create schemas in $(envval LITELLM_DB_NAME) (CREATE)" "$create"
      chk "LiteLLM tables exist (LiteLLM_SpendLogs, LiteLLM_DailyTeamSpend)" "$([[ $spendlogs == t && $daily == t ]] && echo t || echo f)"
      chk "$who can read LiteLLM_SpendLogs (the reporting views read it)" "$readable"
      chk "connected to the primary (not a read-only replica)" "$([[ $standby == f ]] && echo t || echo f)"
      $ok || { echo; echo "Grant the missing rights, or set database.setup: manual in setup.yml (a DBA runs the SQL)."; exit 1; }
    else
      echo "  FAIL  database login: $(echo "$res" | tail -2)"
      echo; echo "Check database.* in setup.yml and DB_ADMIN_PASSWORD in .env."; exit 1
    fi
  else
    echo "  database.setup: manual - SQL for the DBA is written in step 6"
  fi
else
  echo "  local mode: LiteLLM and Postgres run here"
  if [[ -f certs/corp-root-ca.pem && ! -f certs/ca-bundle.pem ]]; then ./scripts/build-ca-bundle.sh; fi
fi
$CHECK_ONLY && { echo; echo "Check passed. Run ./scripts/setup.sh to set everything up."; exit 0; }

TENANTS_FILE=$(envval TENANTS_FILE)
step "4/7 tenants file (bootstrap/$TENANTS_FILE)"
if [[ ! -f "bootstrap/$TENANTS_FILE" ]]; then
  if [[ "$MODE" != "attach" ]]; then echo "  bootstrap/$TENANTS_FILE is missing"; exit 1; fi
  docker compose run --rm --no-deps -T bootstrap --discover "$TENANTS_FILE" 2>&1 | { grep -v -E '^ ?Container ' || true; }
  echo
  echo "Review bootstrap/$TENANTS_FILE now: remove teams you do not want, add members (who may see which"
  echo "team or org), set \"sensitive\": true where prompts must not be stored. Then run ./scripts/setup.sh again."
  exit 0
fi
docker compose run --rm --no-deps -T bootstrap --check 2>&1 | { grep -v -E '^ ?Container ' || true; }

step "5/7 start containers"
docker compose up -d --remove-orphans 2>&1 | grep -v -E ' (Running|Started|Healthy|Waiting|Created)$' || true
docker compose ps --format '  {{.Service}}: {{.State}} {{.Health}}'

step "6/7 tenants (LiteLLM teams, DB scopes, Grafana + Langfuse access, alert routes)"
./scripts/apply-tenants.sh

if $VERIFY; then
  step "7/7 verify"
  ./scripts/verify.sh
fi

echo
echo "Ready:"
echo "  Grafana   $(envval GRAFANA_PUBLIC_URL)      admin / GRAFANA_ADMIN_PASSWORD in .env"
echo "  Langfuse  $(envval LANGFUSE_PUBLIC_URL)      LANGFUSE_ADMIN_EMAIL / LANGFUSE_ADMIN_PASSWORD in .env"
echo "  Accounts  bootstrap/test-users.json (members + test viewers, if local accounts are on)"
[[ "$MODE" == "attach" ]] && echo "  LiteLLM   $(envval LITELLM_URL) (production - see litellm/prod-config-snippet.yaml if preflight warned)"
exit 0

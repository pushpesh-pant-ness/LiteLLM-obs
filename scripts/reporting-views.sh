#!/usr/bin/env bash
# Drop / re-create the reporting views in the LiteLLM database (both modes).
#
#   ./scripts/reporting-views.sh drop      # BEFORE upgrading LiteLLM
#   ./scripts/reporting-views.sh create    # AFTER LiteLLM has started and migrated
#
# Why: the views read LiteLLM's tables. A LiteLLM migration that changes or drops a column a view
# uses fails with "cannot alter type of a column used by a view" - so drop them first. Roles,
# passwords and Grafana are kept; dashboards show errors only between drop and create.
# `drop` removes the reporting schema and every scope_* view (CASCADE); nothing of LiteLLM's.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

case "${1:-}" in
  drop)
    docker compose --profile db-setup run --rm --no-deps -T --entrypoint psql db-setup \
      -v ON_ERROR_STOP=1 -q -c "SET client_min_messages = warning" -c "DROP SCHEMA IF EXISTS reporting CASCADE" 2>&1 | grep -v -E '^ ?Container ' || true
    echo "reporting views dropped - upgrade LiteLLM, then: ./scripts/reporting-views.sh create"
    ;;
  create)
    docker compose --profile db-setup up --no-deps db-setup 2>&1 | grep -E 'db-setup:|ERROR|FATAL' || true
    echo "reporting views re-created - run ./scripts/verify.sh"
    ;;
  *)
    echo "usage: $0 drop|create"; exit 2 ;;
esac

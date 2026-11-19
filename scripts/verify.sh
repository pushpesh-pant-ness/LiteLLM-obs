#!/usr/bin/env bash
# One command to prove the whole stack works. Run after every change and every upgrade.
#
#   ./scripts/verify.sh          # free: mock model only
#   ./scripts/verify.sh --real   # also one short real call per team (fractions of a cent)
#
# Stages: containers healthy -> alert rule unit tests -> naming check -> end-to-end checks
# (services, config, Prometheus, provider TLS, traffic, spend, Langfuse redaction, Grafana).
set -uo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

failed=()
stage() { echo; echo "######## $1"; }

stage "1/4 containers"
expected="langfuse-db prometheus alertmanager grafana langfuse-web langfuse-worker clickhouse redis local-s3"
# attach mode (setup.yml): LiteLLM and its database are the production ones, not containers here
if [[ "$({ grep -E '^OBS_MODE=' .env || true; } | cut -d= -f2-)" != "attach" ]]; then
  expected="postgres postgres-replica litellm litellm-redis $expected"
fi
status=$(docker compose --profile traces ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null)
for svc in $expected; do
  line=$(echo "$status" | awk -v s="$svc" '$1==s')
  if [[ -z "$line" ]]; then
    echo "  FAIL  $svc is not running"; failed+=("container $svc")
  elif echo "$line" | grep -q -E 'running( healthy)?$|running $'; then
    echo "  PASS  $svc ($(echo "$line" | cut -d' ' -f2-))"
  else
    echo "  FAIL  $svc: $(echo "$line" | cut -d' ' -f2-)"; failed+=("container $svc")
  fi
done

stage "2/4 alert rules (promtool)"
if ./scripts/test-alerts.sh; then echo "  PASS  alert rules"; else failed+=("alert rules"); fi

stage "3/4 naming (tenants file)"
if docker compose run --rm --no-deps -T bootstrap --check; then echo "  PASS  naming"; else failed+=("naming"); fi

stage "4/4 end-to-end checks"
if docker compose run --rm -T checks "$@"; then :; else failed+=("end-to-end checks"); fi

echo
if (( ${#failed[@]} )); then
  echo "VERIFY FAILED: ${failed[*]}"
  exit 1
fi
echo "VERIFY PASSED"

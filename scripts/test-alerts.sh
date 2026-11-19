#!/usr/bin/env bash
# 1) Unit-test the alert rules with promtool (no running stack needed).
# 2) With --slack: send a synthetic alert through the running Alertmanager to Slack.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: don't rewrite container paths
HOSTDIR="$(pwd -W 2>/dev/null || pwd)"

echo "== promtool: rule syntax + unit tests"
docker run --rm --entrypoint promtool -v "$HOSTDIR/prometheus:/p" -w /p prom/prometheus:v3.1.0 check rules alerts.yml
docker run --rm --entrypoint promtool -v "$HOSTDIR/prometheus:/p" -w /p prom/prometheus:v3.1.0 test rules alerts.test.yml

if [[ "${1:-}" == "--slack" ]]; then
  port=$(grep -E '^ALERTMANAGER_PORT=' .env 2>/dev/null | cut -d= -f2); port=${port:-9093}
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "== sending synthetic alert to Alertmanager on :$port (watch #llm-alerts)"
  curl -sf -X POST "http://localhost:$port/api/v2/alerts" -H 'Content-Type: application/json' -d "[{
    \"labels\": {\"alertname\": \"SyntheticTestAlert\", \"severity\": \"info\", \"team_alias\": \"team-test\"},
    \"annotations\": {\"summary\": \"Test alert from scripts/test-alerts.sh\", \"description\": \"If you see this in Slack, alert routing works. It resolves in 5 minutes.\"},
    \"startsAt\": \"$now\"}]"
  echo "sent. Check: docker compose logs alertmanager | grep -i notify"
fi

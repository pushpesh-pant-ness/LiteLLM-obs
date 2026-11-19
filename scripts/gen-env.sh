#!/usr/bin/env bash
# Creates .env from .env.example with random secrets. Refuses to overwrite an existing .env.
# Usage (Git Bash / Linux / macOS):  ./scripts/gen-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then echo ".env already exists - not touching it"; exit 1; fi

hex() { openssl rand -hex "$1"; }
cp .env.example .env

sub() { # sub KEY VALUE
  sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak
}

sub LITELLM_MASTER_KEY       "sk-$(hex 24)"
sub LITELLM_SALT_KEY         "sk-$(hex 24)"
sub LITELLM_UI_PASSWORD      "$(hex 12)"
sub POSTGRES_PASSWORD        "$(hex 16)"
sub LITELLM_DB_PASSWORD      "$(hex 16)"
sub LANGFUSE_DB_PASSWORD     "$(hex 16)"
sub GRAFANA_RO_DB_PASSWORD   "$(hex 16)"
sub REPLICATION_PASSWORD     "$(hex 16)"
sub LITELLM_REDIS_PASSWORD   "$(hex 16)"
sub GRAFANA_ADMIN_PASSWORD   "$(hex 12)"
sub LANGFUSE_PUBLIC_KEY      "pk-lf-$(hex 16)"
sub LANGFUSE_SECRET_KEY      "sk-lf-$(hex 16)"
sub LANGFUSE_ADMIN_PASSWORD  "$(hex 12)"
sub LANGFUSE_SALT            "$(hex 16)"
sub LANGFUSE_NEXTAUTH_SECRET "$(hex 24)"
sub LANGFUSE_ENCRYPTION_KEY  "$(hex 32)"
sub CLICKHOUSE_PASSWORD      "$(hex 16)"
sub REDIS_PASSWORD           "$(hex 16)"

[[ -f alertmanager/slack_webhook_url ]] || echo "https://hooks.slack.com/services/REPLACE/ME/PLEASE" > alertmanager/slack_webhook_url

echo "Wrote .env with random secrets. Add your provider keys, then: docker compose up -d"

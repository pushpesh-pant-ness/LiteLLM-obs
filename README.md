# LiteLLM observability stack

LiteLLM's own tracking produces the numbers, Grafana shows them, Prometheus alerts on them, and
Langfuse lets you debug single requests. **Admin sees everything, an org sees its teams, a team sees
only itself** - in Grafana and in Langfuse. Everything runs in Docker Compose on local volumes.

```
Apps --> LiteLLM x2 (:4000, :4001) ---------------------------> LLM providers (retries, fallbacks)
           |- litellm-redis      shared limits, budgets, cooldowns between the instances
           |- Postgres (gateway) --stream--> postgres-replica --> reporting views --> Grafana orgs
           |- Prometheus --> Grafana "Live" (admin) + Alertmanager --> Slack (platform + per team)
           '- team callbacks --> Langfuse org/project per team (redacted for sensitive teams)
                                 Langfuse: own Postgres (langfuse-db) + ClickHouse + Redis + local-s3
Networks:  edge = published / needs internet   internal = databases & queues, no internet, no ports
```

| URL | What | Login |
|---|---|---|
| http://localhost:4000/ui (or :4001) | LiteLLM admin UI | `LITELLM_UI_USERNAME` / `LITELLM_UI_PASSWORD` |
| http://localhost:3001 | Grafana. Main org = admin (*Live* + *Cost*, all teams); one org per team and per LiteLLM org | `admin` / `GRAFANA_ADMIN_PASSWORD` |
| http://localhost:3000 | Langfuse. Org `LLM Observability` (old shared project) + one org per team | `LANGFUSE_ADMIN_EMAIL` / `LANGFUSE_ADMIN_PASSWORD` |
| http://localhost:9090 / :9093 | Prometheus / Alertmanager (admin only) | - |

All ports bind to 127.0.0.1. Sample members **U1** (team-search), **U2** (team-ranking), **U3**
(team-contracts) - set in `tenants.json` `members` - plus test viewers per scope (`viewer-team-search`,
`viewer-org-engineering`, ...); logins and passwords are in `bootstrap/test-users.json` - they log in to Grafana and Langfuse and see
only their scope. Real users come from Entra ID later (`docs/PRODUCTION.md`, phase 7).

## Setup: one file, one command

```bash
cp setup.example.yml setup.yml     # fill it in
./scripts/setup.sh --check         # validate + test connections (changes nothing outside .env)
./scripts/setup.sh                 # .env -> containers -> tenants/access -> verify  (idempotent, re-run any time)
```

`setup.yml` `mode` decides what runs:

| mode | For | Runs here | LiteLLM |
|---|---|---|---|
| `attach` | **production** - next to the LiteLLM you already run | Grafana, Prometheus, Alertmanager, Langfuse | yours; teams/budgets/keys untouched (`tenants: observe`) |
| `local` | laptop demo, testing changes | everything above **plus** LiteLLM x2, Redis, Postgres + replica | bundled; teams/keys from `tenants.json` |

Production walkthrough, what it changes in prod and day-2 tasks: **`docs/PROD_QUICKSTART.md`**.
`setup.sh` writes a marked block in `.env` (and `prometheus/prometheus.attach.yml`), so plain
`docker compose ...` and every script below use the right mode afterwards. Git Bash / Linux / macOS.

## Teams, access and onboarding

Edit **`bootstrap/tenants.json`**, then run `./scripts/apply-tenants.sh` (idempotent). It applies:

| Where | What each team / org gets |
|---|---|
| LiteLLM | team, monthly budget (`1mo`, resets on the 1st), allowed models, keys with tags + per-key budget and RPM/TPM limits |
| Postgres | read-only role `rpt_team_<x>` / `rpt_org_<x>` + schema of views filtered to its team ids |
| Grafana | its own org, a datasource logging in as that role (on the replica), the *Cost* dashboard |
| Langfuse | its own org + project; the team's LiteLLM callback sends traces there; viewers invited |
| Alertmanager | team alerts go to the platform channel **and** the team's channel |

Why per-org/per-team *organizations*: free Grafana cannot restrict a datasource inside one org, and
free Langfuse has no project-level roles - so isolation is done with orgs, and enforced in the data
(each Grafana org's datasource can only read its own rows), never only in a dashboard filter.
Adding a team, step by step: `docs/PRODUCTION.md` > *Runbook: add a team*.

## Verify (run after every change and every upgrade)

```bash
./scripts/verify.sh          # free: mock model only, ~3 min
./scripts/verify.sh --real   # + short real calls (every model of sensitive teams), < $0.001
```

~90 checks: containers healthy, alert-rule unit tests, naming; services; `tenants.json` matches LiteLLM
(models, budgets, key limits, callbacks); both LiteLLM instances healthy and scraped; **rate limit holds
across instances**; **fallback works**; provider HTTPS through the corporate proxy; traffic + 403s per
key; spend logged per team; traces in each team's own Langfuse project and **sensitive prompts / answers /
reasoning never stored**; every Grafana query runs with no blank or stale names; Grafana reads the
**replica**; `grafana_ro` cannot read LiteLLM tables; **every test user sees exactly its own scope** in
Grafana and Langfuse; per-team alert routing. Proven to fail on purpose (stale model name, reasoning
plugin off). Exit code 1 on any failure.

## Backups

```bash
./scripts/backup.sh          # LiteLLM DB + Langfuse DB + ClickHouse -> backup volumes, keeps 7 days
./scripts/restore-test.sh    # restores the newest backups into scratch DBs and compares row counts
```

Schedule `backup.sh` nightly and `restore-test.sh` monthly (cron on the server). Backups live in the
`pg-backups`, `lf-pg-backups` and `ch-backups` volumes - ship them off the host in production.

## Models

`mock-gpt-4o-mini` answers without a provider key (priced like gpt-4o-mini) - the whole pipeline can be
tested for free. `zz-verify-failing` always fails; verify uses it to prove fallbacks. Real models are
added in the UI (Models + Endpoints) or `litellm/config.yaml`. The **public model name** is what apps
send and what goes in a team's `models` list; the provider route stays behind it:

| Public name | Provider route | Credential |
|---|---|---|
| `gpt-oss-120b` | `groq/openai/gpt-oss-120b` | `groqq` |
| `gpt-oss-20b`  | `groq/openai/gpt-oss-20b`  | `groqq` |
| `qwen3.8-27b`  | `groq/qwen/qwen3.8-27b`    | `groqq` |

A key can call only its team's models (403 otherwise); `GET /v1/models` with a key lists them.
Renaming a model breaks apps using the old name until `tenants.json` + `apply-tenants.sh` are updated.
Fallbacks per model go in `router_settings.fallbacks` (needs a 2nd provider for the same model; a
fallback model must be allowed for the same teams).

## Alerts

Rules: team budget above 80% / exhausted, error rate above 5% (model calls only, per team, ignores low
traffic), provider errors, p95 latency above 30s, LiteLLM down, Langfuse logging failures.
Budget rules use min/max by team, so several LiteLLM instances don't double-alert.
Webhooks (secrets, git-ignored): `alertmanager/slack_webhook_url` (platform, `#llm-alerts`) and
`alertmanager/slack/<team>.url` (per team; channel from `tenants.json` `alerts.slack_channel`).

```bash
./scripts/test-alerts.sh            # promtool unit tests for every rule
./scripts/test-alerts.sh --slack    # also send a synthetic alert to Slack
```

## Security notes

- **Networks:** databases, Redis, ClickHouse, the S3 store and the Langfuse worker are on an internal
  network with **no internet and no host ports** (Postgres admin: `docker compose exec postgres psql -U postgres`).
- **Limits:** every container has log rotation (10 MB x 3) and a memory limit.
- **Prometheus** scrapes with a key that can call only `/metrics`; per-user / per-request labels are dropped.
- **Grafana** reads the **replica** as `grafana_ro` (admin org: `reporting` schema) or `rpt_*` (team/org
  orgs: their own schema) - read-only, 30s timeout, no access to LiteLLM tables, keys or prompt text.
- **Sensitive teams** (`"sensitive": true`): prompt, answer and model *reasoning* are redacted before
  Langfuse. LiteLLM 1.106 does not redact Groq's `reasoning` field (it quotes the prompt), so
  `litellm/redact_reasoning.py` blanks it for every redacted call; apps still get the full response.
- **Shared logins** (LiteLLM UI from `.env`, Grafana `admin`) are for local use; replace with SSO and
  named admins before sharing (phase 7). Langfuse sign-up is off (`apply-tenants.sh` opens it only
  briefly to create local test users).
- Secrets: `.env`, `bootstrap/generated-keys.json`, `bootstrap/scope-credentials.json`,
  `bootstrap/langfuse-projects.json`, `bootstrap/test-users.json`, `postgres/generated/`,
  `prometheus/secrets/`, `alertmanager/slack*` - all git-ignored.

## Corporate network (TLS inspection)

Netskope re-signs HTTPS. `scripts/build-ca-bundle.sh` makes every container trust it, including the
Ness intermediate that LiteLLM's Python 3.13 needs (strict X509) - see `certs/README.md`.

## What's where

| Path | Purpose |
|---|---|
| `setup.yml` (from `setup.example.yml`) | **Edit this first** - mode, production LiteLLM + database, server URLs/ports, accounts |
| `scripts/setup.sh`, `scripts/setup.py` | One-command setup: render `.env`, preflight, up, tenants, verify |
| `compose.attach.yml` | Attach-mode override: no bundled LiteLLM/Postgres; db-setup/Grafana/Prometheus point at production |
| `litellm/prod-config-snippet.yaml` | What to merge into the production LiteLLM config (callbacks, redaction hook) |
| `scripts/reporting-views.sh` | Drop / re-create the reporting views around a LiteLLM upgrade |
| `bootstrap/tenants.json` | Local mode: orgs, teams, budgets, models, keys, tags, limits, alert channel |
| `bootstrap/tenants.prod.json` | Attach mode: written from LiteLLM's teams by `setup.sh`; you add members + `sensitive` |
| `scripts/apply-tenants.sh` | Applies tenants.json everywhere (LiteLLM, Postgres, Grafana, Langfuse, Alertmanager) |
| `bootstrap/bootstrap.py` | LiteLLM teams/keys/limits/callbacks + scoped DB roles; enforces `NAMING.md` |
| `bootstrap/tenant_access.py`, `langfuse_admin.py` | Grafana orgs + Langfuse orgs/projects/viewers |
| `bootstrap/tenant_alerts.py` | Generates `alertmanager/alertmanager.yml` (platform + per-team routes) |
| `scripts/verify.sh`, `checks/verify.py` | End-to-end checks |
| `scripts/backup.sh`, `scripts/restore-test.sh` | Backups and restore proof |
| `postgres/sql/` | Reporting views + `grafana_ro` grants (service `db-setup`) |
| `postgres/replica-entrypoint.sh` | Streaming replica bootstrap |
| `litellm/config.yaml`, `litellm/redact_reasoning.py` | Proxy config (Redis, retries, fallbacks); reasoning redaction hook |
| `prometheus/`, `grafana/dashboards/` | Alert rules + tests; *Live* (Prometheus, admin) and *Cost* (Postgres, every org) |
| `docs/BUILD_PLAN.md`, `docs/PRODUCTION.md` | Phase plan + status; runbooks for phases 5-7, production move, adding a team |

## Limits worth knowing

- LiteLLM orgs and enforced spend-log retention need **LiteLLM Enterprise** (licence -> phase 7); until
  then the org lives in team metadata and old request logs are not pruned automatically.
- Langfuse traces have **no retention** yet (a paid Langfuse feature) - plan a cleanup job or a limit.
- Prometheus `increase()` is an estimate; the *Cost* dashboard (Postgres) is exact. Prometheus runs with
  `created-timestamp-zero-ingestion` so new series are counted from their first call.
- Two LiteLLM instances: inside the stack `litellm:4000` reaches whichever is up (tested); on the host,
  ports 4000/4001 map to one instance each until the TLS load balancer (phase 5) sits in front.
- `bootstrap/langfuse_admin.py` uses Langfuse's internal UI API (no free public API exists for orgs /
  projects); re-check after Langfuse upgrades - the UI fallback is in `docs/PRODUCTION.md`.
- Out of support / to upgrade (phase 6): Grafana 11.5, Prometheus 3.1, ClickHouse 25.3, Langfuse v3
  (security patches end January 2027).

## Weekly habit

- **Monday**: Cost dashboard > *Top 5 spenders this week*; teams check their own Grafana org.
- **Monthly**: *Monthly finance export* > Inspect > Data > Download CSV; run `restore-test.sh`.
- **Quarterly**: review budgets and key limits, unused keys, cheaper models.

## Reset

```bash
docker compose --profile traces --profile tools down        # stop, keep data
docker compose --profile traces --profile tools down -v     # stop and DELETE all data
```

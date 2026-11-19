# Production quick start: attach to the LiteLLM you already run

One file (`setup.yml`), one command (`./scripts/setup.sh`). This stack runs **next to** production
LiteLLM: Grafana, Prometheus, Alertmanager and Langfuse. It does not run a LiteLLM itself.

```
LiteLLM (prod, Enterprise) --metrics--> Prometheus --> Grafana Live + Alertmanager --> Slack
     |  \--traces per team-----------> Langfuse (one org + project per team, redacted for sensitive teams)
     '-- Postgres (prod) --replica--> reporting views --> Grafana Cost (one Grafana org per team / org)
```

## What changes in production (and what does not)

| Where | Change | Reversible |
|---|---|---|
| LiteLLM config | `litellm/prod-config-snippet.yaml`: prometheus callback + reasoning-redaction hook (one rolling restart) | remove the lines |
| LiteLLM teams | metadata keys `org`, `sensitive`, `langfuse_project_id`; a team-level Langfuse callback (only if the team has no callbacks pointing elsewhere - `overwrite_team_callbacks`) | `/team/{id}/disable_logging` or re-set |
| LiteLLM keys | one key `infra-prometheus-scraper-prod` that may only call `/metrics` | delete the key |
| LiteLLM database | schemas `reporting` + `scope_*` (views only, no data copied) and roles `grafana_ro`, `rpt_*` (read-only, 30 s timeout) | `DROP SCHEMA ... CASCADE; DROP ROLE ...` |
| **Not touched** | budgets, models, keys, users, LiteLLM's own tables (`tenants: observe`) | - |

## Before you start

- **Server:** Docker with Compose 2.24.4 or newer (`!reset` / `!override` in compose.attach.yml); ~6 GB RAM free; these images already on it
  (`PULL_POLICY=never`): `python:3.12-alpine`, `postgres:16`, `redis:7`, `prom/prometheus:v3.1.0`,
  `prom/alertmanager:v0.28.1`, `grafana/grafana:11.5.2`, `langfuse/langfuse:3`,
  `langfuse/langfuse-worker:3`, `clickhouse/clickhouse-server:25.3`.
- **Network:**
  - observability server -> each LiteLLM replica's port (metrics)
  - observability server -> LiteLLM DB primary and replica (5432)
  - LiteLLM -> observability server's Langfuse port (traces)
- **LiteLLM admin key:** the master key, or better, a key with the `proxy_admin` role made for this stack.
- **Database role:** one with `CREATEROLE` and `CREATE` on the LiteLLM database. LiteLLM's own DB user
  has `CREATE`, but usually not `CREATEROLE`. If you can't get one, use `database.setup: manual`
  (your DBA runs the generated SQL).

## Five steps

```bash
cp setup.example.yml setup.yml       # 1. fill in: LiteLLM URL + replicas, database, server URLs
./scripts/setup.sh --check           # 2. creates .env; add LITELLM_MASTER_KEY + DB_ADMIN_PASSWORD, re-run until no FAIL
                                     # 3. any WARN about callbacks -> apply litellm/prod-config-snippet.yaml in prod
./scripts/setup.sh                   # 4. writes bootstrap/tenants.prod.json from LiteLLM's teams and stops:
                                     #    add members, mark sensitive teams, drop teams you don't want
./scripts/setup.sh                   # 5. starts everything, applies access, runs verify
```

`--check` changes nothing outside `.env`. It tests:
- LiteLLM reachable, and the key is admin
- prometheus callback and redaction hook present
- every metrics target reachable
- both database hosts reachable
- the DB role can create roles and schemas, and LiteLLM's tables exist

Step 5's `verify` is **read-only against production**: no test calls, no keys created. For sensitive
teams it reads the latest stored traces and fails if any input, output or reasoning text is not
`redacted-by-litellm`.

## The tenants file in attach mode

`bootstrap/tenants.prod.json` matches teams by `team_id`, so renaming a team in LiteLLM is fine.
Only these fields are used:

```json
{"orgs": [{"alias": "org-engineering", "members": [{"login": "lead", "name": "Eng Lead", "email": "lead@company.com"}],
           "teams": [{"alias": "team-search", "team_id": "…", "sensitive": false,
                      "members": [{"login": "u1", "name": "U1", "email": "u1@company.com"}]}]}]}
```

- A member on a **team** sees that team. A member on an **org** sees all its teams. Admin sees everything.
- `accounts.members: local` gives them Grafana and Langfuse logins with generated passwords, stored in
  `bootstrap/test-users.json`. `sso`: Langfuse invites by email (the invite is accepted at first sign-in
  with that email; for invite *e-mails* set Langfuse `SMTP_CONNECTION_URL` + `EMAIL_FROM_ADDRESS`); Grafana logins come from Entra ID
  (`docs/PRODUCTION.md`, phase 7).

## Day 2

| Task | Do |
|---|---|
| New team in LiteLLM | add it to `tenants.prod.json` (`docker compose run --rm --no-deps bootstrap --discover tenants.new.json` lists all teams with ids), then `./scripts/apply-tenants.sh` |
| New member | add to `members`, `./scripts/apply-tenants.sh` |
| **Upgrade LiteLLM** | `./scripts/reporting-views.sh drop` -> upgrade -> `./scripts/reporting-views.sh create` -> `./scripts/verify.sh`. A migration that changes a column a view reads would fail otherwise. Manual DB mode: the DBA drops and re-applies `dba-apply.sql`. |
| Move / change anything | edit `setup.yml`, re-run `./scripts/setup.sh` (idempotent) |
| Backups | `./scripts/backup.sh` backs up Langfuse (Postgres + ClickHouse). The LiteLLM DB is production's: its owners back it up. |

## Local mode on a laptop

The same command runs the whole stack, LiteLLM included, for demos and for testing changes:
`mode: local` in `setup.yml` (`setup.yml` on this machine is set that way).

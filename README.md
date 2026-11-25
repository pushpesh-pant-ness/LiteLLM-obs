# LLM Gateway (LiteLLM + observability)

A self-contained, Docker Compose LLM gateway for one organisation: a single OpenAI-compatible
endpoint in front of Groq, Gemini and OpenRouter, with fixed teams, per-team budgets and keys, a
Redis response cache, and full observability (Langfuse traces per team, Prometheus metrics,
Grafana dashboards). Request processing is a **hook pipeline**: observability is the first hook;
routing, prompt compression or guardrails plug in the same way.

```
Apps ──► LiteLLM ×2 (:4000, :4001) ──────────────────────────────────────► Groq · Gemini · OpenRouter
          │  hook pipeline: observability ► cache-isolation ► (your hooks)
          ├─ postgres      keys, teams, budgets, spend logs ───────────► Grafana "Cost"
          ├─ redis-state   rate limits, budgets, cooldowns, spend buffer (shared by replicas)
          ├─ redis-cache   response cache, one namespace per team
          ├─ prometheus    metrics ──────────────────────────────────────► Grafana "Live"
          └─ langfuse      traces, one project per team (+ worker, langfuse-db, clickhouse,
                           langfuse-redis, local-s3)
```

All data stays on local Docker volumes. Everything the stack needs is in this folder.

## Quick start

1. Docker Desktop running (Compose v2).
2. Start everything (Windows):

   ```powershell
   .\run.ps1
   ```

   Git Bash / Linux / macOS: `./run.sh`. If PowerShell blocks scripts:
   `powershell -ExecutionPolicy Bypass -File .\run.ps1`.

3. On the first run `.env` is created with every password, secret and team key generated. Add your
   provider keys to it (`GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`), then run
   `.\run.ps1` again: the models of those providers are added and granted to their teams.
4. Check it end to end:

   ```powershell
   .\run.ps1 test
   ```

One command does the whole build-and-run: validate `config/`, fill `.env`, generate the LiteLLM /
Prometheus / Grafana config, pull the images, start the containers, wait until they are healthy,
then provision teams, keys and Langfuse projects. It is idempotent: re-run it after any change.

| URL | What | Login (values in `.env`) |
|---|---|---|
| http://localhost:4000 | OpenAI-compatible API (replica 2: `:4001`) | `Bearer <KEY_TEAM_KEY>` for apps, `LITELLM_MASTER_KEY` for admin |
| http://localhost:4000/ui | LiteLLM admin UI | `LITELLM_UI_USERNAME` / `LITELLM_UI_PASSWORD` |
| http://localhost:3000 | Langfuse: org *AIxOffice*, one project per team | `LANGFUSE_ADMIN_EMAIL` / `LANGFUSE_ADMIN_PASSWORD` |
| http://localhost:3001 | Grafana: *LiteLLM - Live*, *LiteLLM - Cost* | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` |
| http://localhost:9090 | Prometheus | – |

Call it like OpenAI, with a team key from `.env`:

```powershell
$key = (Select-String -Path .env -Pattern '^KEY_SEARCH_CHATBOT_PROD=(.*)').Matches[0].Groups[1].Value
Invoke-RestMethod http://localhost:4000/v1/chat/completions -Method Post `
  -Headers @{ Authorization = "Bearer $key" } -ContentType 'application/json' `
  -Body '{"model":"gpt-oss-20b","messages":[{"role":"user","content":"hi"}]}'
```

## Project layout

```
.
├── run.ps1 / run.sh        the runner: build + run end to end, test, provision, logs, down
├── .env.example            template of .env (ALL credentials; .env itself is git-ignored)
├── compose.yaml            every service, network and volume
├── config/                 what you edit
│   ├── stack.yaml          project name, bind address, replicas, retries, cache, image tags
│   ├── ports.yaml          fixed port of every service
│   ├── models.yaml         model catalog: public names -> provider routes, fallbacks, prices
│   ├── teams.yaml          the org and its fixed teams: models, budgets, limits, keys
│   └── hooks.yaml          the hook pipeline: which hooks run, in what order, with what settings
├── gateway/                what runs inside LiteLLM
│   ├── litellm.base.yaml   static LiteLLM settings (DB, Redis, Prometheus labels)
│   ├── entrypoint.py       the one callback LiteLLM loads: builds the hook pipeline
│   └── hooks/              base.py, pipeline.py, observability.py, cache_isolation.py, _template.py
├── tools/                  run in containers by the runner (LiteLLM image, no local Python)
│   ├── render.py           config/ + .env -> .build/ (validates everything first)
│   ├── provision.py        teams, keys, hook setup (Langfuse projects), metrics key
│   ├── selftest.py         end-to-end test (`run test`)
│   ├── langfuse_admin.py   Langfuse project / API-key creation
│   └── build_ca_bundle.py, fetch-intermediate.sh   corporate TLS inspection (certs/)
├── services/               config of the supporting services
│   ├── postgres/           DB init, reporting views + read-only role for Grafana
│   ├── grafana/            datasources, dashboards (Live, Cost)
│   ├── prometheus/         scrape config template
│   └── local-s3/           tiny S3-compatible blob store for Langfuse (local volume)
├── certs/                  corporate root / intermediate CA (git-ignored *.pem), see certs/README.md
├── docs/HOOKS.md           how to write and add a hook
└── images.lock             image digests in use (written by every run, for the security review)

Generated, git-ignored:  .env   .build/ (rendered config, contains secrets)   .state/ (Langfuse team project keys)
```

## Configuration

### `.env`: all credentials in one place

| Group | Variables | Notes |
|---|---|---|
| Providers | `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | A provider's models are added only when its key is set. |
| Admin logins | `LITELLM_UI_*`, `LITELLM_MASTER_KEY`, `GRAFANA_ADMIN_*`, `LANGFUSE_ADMIN_*` | Generated if empty. |
| Service secrets | Postgres, Redis, ClickHouse, Langfuse, local-s3 | Generated; *(fixed)* ones must not change after the first start. |
| Team keys | `KEY_<TEAM>_<KEY>` | One per key in `teams.yaml`, generated, **fixed**: the same value is loaded into LiteLLM on every run. Change it and re-run to rotate. |

Only provider keys reach the LiteLLM containers' environment (`.build/litellm.env`); the other
services get only the secrets they need.

### `config/models.yaml`: models

| Public name | Provider route | Mode | Fallback |
|---|---|---|---|
| `gemini-3.6-flash` | `gemini/gemini-3.6-flash` | chat | – |
| `gemini-embedding-001` | `gemini/gemini-embedding-001` | embedding | – |
| `gpt-oss-120b` | `groq/openai/gpt-oss-120b` | chat | `gpt-oss-120b-openrouter` |
| `gpt-oss-20b` | `groq/openai/gpt-oss-20b` | chat | `gpt-oss-20b-openrouter` |
| `qwen3.8-27b` | `groq/qwen/qwen3.8-27b` | chat | – |
| `gpt-oss-*-openrouter` | `openrouter/openai/gpt-oss-*` | chat (hidden, fallback only) | – |
| `selftest-mock` | mock (canned answer, no provider) | chat (internal, used by `run test`) | – |

When every Groq deployment of `gpt-oss-*` fails, LiteLLM retries on OpenRouter. OpenRouter routes
are not in LiteLLM's bundled price list, so `models.yaml` sets their price; otherwise budgets would
count them as $0. To add a model, add an entry, list it in a team and re-run.

### `config/teams.yaml`: the org and its fixed teams

| Team | Models | Budget | Team limits | Keys (budget / rpm / tpm) | Traces |
|---|---|---|---|---|---|
| `search` | gemini-3.6-flash, gemini-embedding-001, gpt-oss-120b, gpt-oss-20b | $500 / month | 600 rpm, 2M tpm | `search-chatbot-prod` ($400 / 120 / 200k), `search-chatbot-dev` ($25 / 20 / 20k) | full |
| `ranking` | gemini-embedding-001, gpt-oss-20b, qwen3.8-27b | $200 / month | 300 rpm, 1M tpm | `ranking-reranker-prod` ($200 / 120 / 200k) | full |
| `contracts` | gemini-3.6-flash, qwen3.8-27b, gpt-oss-120b | $300 / month | 120 rpm, 500k tpm | `contracts-reviewer-prod` ($300 / 60 / 100k) | **redacted** |

What each run does with this file:

- **LiteLLM**: one team per entry, with the allowed models (plus their fallbacks and
  `selftest-mock`), budget, rpm/tpm, and metadata (org, observability settings). One key per entry,
  with the fixed secret from `.env`. A key can call only its team's models, anything else gets
  HTTP 401/403, and `GET /v1/models` lists exactly those models.
- **Langfuse**: the org (`org.id` / `org.name`) is created on the first start. Each team gets its
  own **project** and project API key, and LiteLLM sends that team's traces there. Calls without a
  team (admin, UI) go to the `gateway-default` project.
- **Grafana**: *LiteLLM - Cost* groups spend by org, team, key and the key's tags (`proj:`, `cc:`, `env:`).

Budgets reset on the 1st of the month (UTC). Rate limits count across all replicas through
`redis-state`. Removing an entry deletes nothing; leftovers are reported.

### `config/ports.yaml`: port map

| Service | Port | Reachable from |
|---|---|---|
| litellm | 4000 (replica N: 4000+N-1) | host + Docker network |
| langfuse | 3000 | host + Docker network |
| grafana | 3001 | host + Docker network |
| prometheus | 9090 | host + Docker network |
| postgres | 5432 | internal network only |
| redis_state | 6379 | internal network only |
| redis_cache | 6380 | internal network only |
| langfuse_db | 5433 | internal network only |
| langfuse_redis | 6381 | internal network only |
| langfuse_worker | 3030 | internal network only |
| local_s3 | 9100 | internal network only |
| clickhouse_http / _native | 8123 / 9000 | internal network only (fixed by the image) |

Each service listens on the **same** port inside Docker and, for the published ones, on the host.
The runner turns the map into `PORT_*` variables. `compose.yaml`, the generated LiteLLM config
(Postgres URL, both Redis hosts/ports, Langfuse host), Prometheus and Grafana all read them, so a
change here is a one-line edit. The runner rejects duplicate or overlapping ports. Published ports
bind to `server.bind` in `stack.yaml` (`127.0.0.1` by default).

### Redis: state vs cache

| | `redis-state` | `redis-cache` |
|---|---|---|
| Holds | rpm/tpm counters, budgets between DB writes, cooldowns, spend-update buffer | cached responses (chat, embeddings, text completion) |
| Persistence | AOF (survives restarts) | none (starts cold) |
| Eviction | `volatile-lru`, so only keys with a TTL can go and the spend buffer is never dropped | `allkeys-lru`, `cache.max_memory` |

The cache is on by default (`stack.yaml > cache`, TTL 1 h). Identical requests are answered from
Redis (response header `x-litellm-cache-key`). The **cache-isolation** hook gives every team its own
namespace, so two teams never share an answer. A client can skip the cache per request with
`"cache": {"no-cache": true}`.

### `config/hooks.yaml`: the hook pipeline

```yaml
hooks:
  - name: observability        # tags, trace routing, redaction, request log + Langfuse setup
    module: observability
    enabled: true
    fail_open: true
    litellm: { callbacks: [prometheus], success_callback: [langfuse], failure_callback: [langfuse] }
  - name: cache-isolation      # per-team cache namespace
    module: cache_isolation
    enabled: true
    fail_open: false
```

LiteLLM loads a single callback, `gateway/entrypoint.py`, which runs the enabled hooks in order at
every stage of a request (`pre_call`, `post_call`, `on_error`, response headers, logging). A hook
can also have a `provision()` step that `run provision` executes; observability uses it to create
the Langfuse team projects. To add an **autorouter**, **prompt compression** or a guardrail, write
`gateway/hooks/<module>.py` (copy `_template.py`), add an entry here and re-run. No other file
changes. Every response carries `x-gateway-hooks: observability,cache-isolation` so you can see
which hooks ran. Full guide: [docs/HOOKS.md](docs/HOOKS.md).

## Observability

| Where | What |
|---|---|
| Langfuse, project per team | Every request: model, prompt and answer (masked for `redact_content` teams), tokens, cost, latency, errors. Tags `team:<id>`, `org:<id>` and the key's tags; trace user = key alias (or the request's `user`); `metadata.session_id` groups a conversation. |
| Grafana *LiteLLM - Live* | Prometheus: requests, errors, latency, tokens, per model / team / replica. |
| Grafana *LiteLLM - Cost* | Postgres reporting views (read-only role): spend by org, team, key, model, `cc:` / `proj:` tag, monthly finance export, drill-down to the Langfuse session. |
| `.\run.ps1 logs litellm` | One `gateway.request {...}` JSON line per request (team, key, model, tokens, cost, latency, cache hit, status; never prompt text). |

Prompt text is never stored in Postgres (`store_prompts_in_spend_logs: false`). Grafana reads only
the `reporting` views as `grafana_ro`, never LiteLLM's tables or keys. Prometheus scrapes with a
key that may call only `/metrics`.

## Commands

| Command | What |
|---|---|
| `.\run.ps1` | Build and run end to end (idempotent). |
| `.\run.ps1 test` | End-to-end self-test with temporary keys. Makes one real 16-token call per provider model. |
| `.\run.ps1 provision` | Re-apply `teams.yaml` and hook setup without restarting. |
| `.\run.ps1 render` | Validate `config/` and regenerate `.build/` only. |
| `.\run.ps1 status` / `logs [service]` | Container states / follow logs. |
| `.\run.ps1 compose <args>` | Any `docker compose` command with the generated env, e.g. `compose exec postgres psql -U postgres -d litellm`. |
| `.\run.ps1 pull` | Pull the images of `stack.yaml` (writes `images.lock`). |
| `.\run.ps1 down` | Stop and keep data. Delete everything: `.\run.ps1 compose down -v`. |

Same commands with `./run.sh`.

## Self-test

`run test` checks the running stack and exits 1 on any failure:

- every replica ready
- every enabled model served
- the hook pipeline active
- each fixed team key sees exactly its team's models
- every allowed model answers
- every other model is refused
- a repeated request is a cache hit, while the same request from another team is not
- the rpm limit holds across replicas
- a spent budget blocks
- each team's trace lands in its own Langfuse project with its tags, redacted for `contracts`
- Prometheus scrapes every replica
- spend is booked per team

**Last run (2026-11-25, LiteLLM 1.110.3 and 1.111.0, no provider keys set): 20 passed, 0 failed.** Provider
model calls are added to the test automatically once their keys are in `.env`.

## Security notes

- Published ports bind to `127.0.0.1`. For `bind: 0.0.0.0`, put TLS in front and set `public_host`.
- Databases and queues sit on an `internal` Docker network: no host port, no internet.
- `.env`, `.build/` and `.state/` hold secrets and are git-ignored. Never commit them.
- Corporate TLS inspection (Netskope): put the root CA in `certs/` and the runner builds
  `certs/ca-bundle.pem` for LiteLLM. See [certs/README.md](certs/README.md).
- Image tags: `config/stack.yaml`. `images.pull: always` (default) pulls the latest patch of each
  release line on every run. `images.lock` records the exact digests in use, so you can send it for
  clearance, then pin the tags and switch to `missing` or `never`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `models skipped: ... (set GROQ_API_KEY in .env)` | Add the key to `.env`, re-run. |
| `configuration problems - nothing started` | The message names the file and field; fix it and re-run. |
| Langfuse step fails with "admin login failed" | The Langfuse admin password was changed in the UI; put the new one in `.env` (`LANGFUSE_ADMIN_PASSWORD`). |
| Langfuse step fails after a Langfuse upgrade | Project creation uses Langfuse's internal UI API. As a workaround, create the team's project and an API key in the Langfuse UI, then add them to `.state/state.json` under `langfuse_projects.<team>` (`project_id`, `public_key`, `secret_key`) and re-run. |
| Provider calls fail with `certificate verify failed` | TLS inspection: see `certs/README.md`. |
| Port already in use | Change it in `config/ports.yaml`, re-run. |

## History

Earlier versions of this repository held an "attach mode" stack for a production LiteLLM
Enterprise (per-team Grafana orgs, Postgres replica, Alertmanager, backups) and later a
`standalone/` folder. Both are in git history (`c892f04` and earlier).

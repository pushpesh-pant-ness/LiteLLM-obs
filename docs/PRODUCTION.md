# Production runbooks: phases 5-7 and the move to a shared server

Phases 0-4 are built and verified on the local stack (see `docs/BUILD_PLAN.md`). The phases below
need things this machine does not have: **images cleared by security**, **IT** (server, DNS, Entra ID)
and the **LiteLLM Enterprise licence**. Each runbook lists the exact steps and a starting config.

> Configs in this file are **drafts, untested** until the images are cleared. After each phase:
> `./scripts/verify.sh --real` must be green, then commit.

---

## Phase 5 - Edge: TLS ingress + egress proxy

**Needs:** cleared `nginx` (official) and `ubuntu/squid` images, a TLS certificate + DNS name.

### 5a. TLS ingress / load balancer (nginx)
Goal: one HTTPS entry point; spreads traffic over both LiteLLM instances (fixes "host port 4000 down
when one instance stops"); Grafana and Langfuse behind the same certificate.

1. `docker compose` - new service `ingress` (nginx) on the `edge` network, the **only** service with
   host ports (443). Remove `ports:` from litellm, grafana, langfuse-web, prometheus, alertmanager.
2. `ingress/nginx.conf` (draft):
   ```nginx
   upstream litellm { server litellm:4000 max_fails=2 fail_timeout=10s; }  # docker DNS: all replicas
   resolver 127.0.0.11 valid=10s;                                         # re-resolve when replicas change
   server {
     listen 443 ssl;  server_name llm.example.internal;
     ssl_certificate /certs/tls.crt;  ssl_certificate_key /certs/tls.key;
     client_max_body_size 20m;  proxy_read_timeout 300s;                  # long LLM calls + streaming
     location / { proxy_pass http://litellm; proxy_buffering off; proxy_next_upstream error timeout http_502 http_503; }
   }
   server { listen 443 ssl; server_name grafana.example.internal;  location / { proxy_pass http://grafana:3000; } }
   server { listen 443 ssl; server_name langfuse.example.internal; location / { proxy_pass http://langfuse-web:3000; } }
   # Prometheus / Alertmanager: admin network only, or behind SSO (oauth2-proxy) - never public.
   ```
3. `.env`: `LANGFUSE_PUBLIC_URL=https://langfuse.example.internal` (Grafana session links) and
   Langfuse `NEXTAUTH_URL` to the same; Grafana `GF_SERVER_ROOT_URL=https://grafana.example.internal`.
4. Verify: stop one LiteLLM instance -> calls through the ingress keep returning 200.

### 5b. Egress proxy with provider allow-list (Squid)
Goal: only LiteLLM talks to the internet, only to approved providers; Netskope trust lives in one place.

1. New service `egress` (squid) on `edge`; move `litellm` and `alertmanager` to an `egress-only`
   network (internal: true) that also contains `egress`, so their only way out is the proxy.
2. `egress/squid.conf` (draft):
   ```
   acl providers dstdomain api.groq.com api.openai.com api.anthropic.com .openai.azure.com hooks.slack.com
   acl SSL_ports port 443
   http_access deny !SSL_ports
   http_access allow CONNECT providers
   http_access deny all
   http_port 3128
   ```
3. LiteLLM / Alertmanager env: `HTTPS_PROXY=http://egress:3128`, `NO_PROXY=litellm,postgres,litellm-redis,langfuse-web,...`
4. Verify (add to `checks/verify.py`): `api.groq.com` reachable via proxy; `example.com` refused;
   LiteLLM without proxy env cannot connect at all.

---

## Phase 6 - Upgrades + telemetry

**Needs:** cleared images: `grafana/grafana:12.4.x`, current `prom/prometheus` LTS, `prom/alertmanager`,
`clickhouse/clickhouse-server:26.4`, `langfuse/langfuse:4` + `langfuse-worker:4`, a pinned LiteLLM
release, `otel/opentelemetry-collector-contrib`, `grafana/mimir`. **Deadline:** Langfuse v3 security
patches end January 2027.

Order (one step at a time; `./scripts/backup.sh` before each, `./scripts/verify.sh --real` after each):

1. **LiteLLM** -> pinned release tag/digest (`LITELLM_IMAGE` in `.env`). Re-check that
   `litellm/redact_reasoning.py` is still needed: verify's reasoning test must stay green; if a release
   redacts `reasoning` itself, the plugin can go.
2. **Prometheus + Alertmanager** -> current versions. `promtool` tests run against the new image
   (update the tag in `scripts/test-alerts.sh`).
3. **Grafana 11.5 -> 12.4.x.** Provisioned dashboards are plain JSON; check the Postgres datasource
   plugin id is unchanged. Verify's "all queries run" catches breakage.
4. **ClickHouse 25.3 -> 26.4** (while still on Langfuse v3 - official order), then
5. **Langfuse v3 -> v4** following the official guide (staged migration modes). Re-run
   `scripts/apply-tenants.sh`: `bootstrap/langfuse_admin.py` uses Langfuse's internal UI API, which may
   change in v4 - if a call fails, do that step in the UI (runbook below) and fix the module.
6. **OTel collector** (optional, after v4): LiteLLM `otel` callback -> collector -> per-team Langfuse
   projects (routing connector on the `team` attribute) with redaction in a `transform` processor.
   Run in parallel with the existing callbacks, prove parity with the marker test, then retire the
   team callbacks and the reasoning plugin.
7. **Mimir** (per-team live metrics): Prometheus `remote_write` once per team with
   `write_relabel_configs` keeping `team_alias="<team>"` and header `X-Scope-OrgID: <team>`, plus one
   unfiltered write for the admin tenant. Each Grafana team org gets a Prometheus datasource on its
   tenant, and the Live dashboard can then be imported per team (generate the remote_write blocks
   from `tenants.json` in `tenant_alerts.py`-style).

---

## Phase 7 - Identity + Enterprise

**Needs:** LiteLLM Enterprise licence key; IT: Entra ID app registrations + groups.

1. **Licence:** `LITELLM_LICENSE=...` in `.env` (never in chat/git), restart LiteLLM.
   - `./scripts/apply-tenants.sh`: bootstrap creates real LiteLLM orgs (it already tries and falls back),
     teams get `organization_id`. `reporting.teams.org` can then read the real org instead of metadata.
   - Spend-log retention (30 days, `general_settings`) becomes enforced - verify by checking the oldest
     `LiteLLM_SpendLogs` row after 31 days.
   - Org admins / team admins in the LiteLLM UI: assign via the UI or `/organization/member_add`.
2. **SSO, one Entra app per tool, groups decide access:**
   | Tool | Settings |
   |---|---|
   | LiteLLM | `GENERIC_CLIENT_ID/SECRET`, `GENERIC_AUTHORIZATION_ENDPOINT`... (Entra) ; map groups to teams |
   | Grafana | `GF_AUTH_AZUREAD_*`; `org_mapping = "grp-llm-admin:1:Admin, grp-team-search:<org id>:Viewer, grp-org-engineering:<org id>:Viewer"` |
   | Langfuse | `AUTH_AZURE_AD_CLIENT_ID/SECRET/TENANT_ID`; keep `AUTH_DISABLE_SIGNUP=true`; memberships via the org Members page (invite by email - accepted at first SSO sign-in) |
   Then set `CREATE_TEST_USERS=false` and delete the `viewer-*` test users.
3. **Turn off shared logins:** after a named admin exists in each tool - LiteLLM
   `general_settings.disable_env_credential_login: true`; rotate the master key; Grafana `admin` password.
4. **Secrets out of `.env`:** Azure Key Vault (LiteLLM Enterprise supports it for provider keys);
   Postgres/Grafana support `*_FILE` secrets; the rest via the deployment platform's secret store.
5. **Apps authenticate with Entra tokens** (LiteLLM JWT auth, Enterprise) instead of long-lived keys.

---

## Moving to the shared server

**Next to an existing production LiteLLM (the normal case): `docs/PROD_QUICKSTART.md`** -
`setup.yml` with `mode: attach`, then `./scripts/setup.sh`. The steps below are for running the whole
stack, LiteLLM included, on a server (`mode: local`).

1. VM with Docker (or a container platform), DNS name, TLS certificate. 16 GB RAM is enough for this
   stack at current limits; add ClickHouse disk for trace volume.
2. Internal registry: push only security-cleared images, pin by digest, keep `PULL_POLICY=never`/`missing`.
3. Copy the repo (not `.env`, not volumes), `cp setup.example.yml setup.yml` (`mode: local`, server
   URLs/bind), run `./scripts/build-ca-bundle.sh` if the server is behind TLS inspection.
4. `./scripts/setup.sh` (creates `.env`, starts everything, applies tenants, verifies), then add
   provider keys and run `./scripts/verify.sh --real`.
5. Cron: `./scripts/backup.sh` nightly, `./scripts/restore-test.sh` monthly; ship backups off the host.
6. Langfuse blob store: replace `local-s3` with real object storage (S3-compatible or Azure Blob; set
   `LANGFUSE_S3_*` / Azure settings) - `local-s3` is single-node only.

---

## Runbook: add a team

1. Add it to `bootstrap/tenants.json` (alias `team-<name>`, budget, models, keys with tags + limits,
   optional `"sensitive": true`, optional `"alerts": {"slack_channel": "#..."}`).
2. `./scripts/apply-tenants.sh` - creates the LiteLLM team + keys, its DB role/views, its Grafana org,
   its Langfuse org + project + key, its alert route. New keys -> `bootstrap/generated-keys.json`.
3. Put the team's Slack webhook in `alertmanager/slack/team-<name>.url`, reload Alertmanager.
4. `./scripts/verify.sh` - the new team is checked automatically (traffic, spend, traces, isolation).

**If `apply-tenants.sh` cannot create the Langfuse org/project** (e.g. after a Langfuse upgrade): in the
Langfuse UI create organization `team-<name>`, project `team-<name>`, a project API key; put them in
`bootstrap/langfuse-projects.json` as `{"team-<name>": {"org_id", "project_id", "public_key", "secret_key"}}`
and re-run `apply-tenants.sh`.

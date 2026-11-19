# Naming rules

Agree these before creating anything - they end up in every cost report and are hard to change later.
`bootstrap/bootstrap.py` refuses a `tenants.json` that breaks them (`docker compose run --rm bootstrap --check`).

| Thing | Pattern | Example | Notes |
|---|---|---|---|
| Org | `org-<unit>` | `org-engineering` | One per business unit or client. LiteLLM orgs are Enterprise-only, so the org is stored on each team as `metadata.org` and the Cost dashboard groups by it. |
| Team | `team-<name>` | `team-search` | One per team. Owns the monthly budget. |
| Key alias | `<team>-<app>-<env>` | `search-chatbot-prod` | `<team>` is the team name without `team-`. `<env>` is one of `prod`, `staging`, `dev`, `test`. One key per app per environment. |
| Tags | exactly `proj:`, `cc:`, `env:` | `proj:chatbot`, `cc:1042`, `env:prod` | Set on the **key**, so developers can't forget them. `env:` must match the alias suffix. `cc:` is the finance cost center. |

Lowercase letters, digits and hyphens only.

## Adding a team or app

1. Add it to `bootstrap/tenants.json`.
2. `./scripts/apply-tenants.sh` - creates what's missing and re-applies budgets, key limits, models,
   access (Grafana org, Langfuse org/project, DB scope) and alert routes.
3. Give the new key from `bootstrap/generated-keys.json` to the app owner (it is shown only once), then remove it from that file.
4. `./scripts/verify.sh` - the new team is checked automatically.

## Budgets and limits

| Field | Where | Meaning |
|---|---|---|
| `max_budget`, `budget_duration` | team | Monthly team budget (`1mo` resets on the 1st, UTC). Requests are rejected at 100%. |
| `max_budget`, `budget_duration` | key | Budget for one app/env. Keep dev keys small so a loop can't drain the team. |
| `rpm_limit`, `tpm_limit` | key | Requests / tokens per minute. Enforced across all LiteLLM instances (shared Redis). |
| `models` | team | Public model names the team's keys may call (403 otherwise). |
| `sensitive` | team | Redact prompt, answer and reasoning in Langfuse. |
| `members` | team or org | People who may see it in Grafana + Langfuse: `[{"login": "u1", "name": "U1", "email": "..."}]`. On a team: that team only. On an org: all its teams (org lead). Locally created with a password (`bootstrap/test-users.json`); in production matched by email at SSO sign-in. |
| `alerts.slack_channel` | team | Team's own alert channel (default `#llm-alerts-<name>`); webhook in `alertmanager/slack/<team>.url`. |

Mark a team `"sensitive": true` to replace prompt/response text with `redacted-by-litellm` in Langfuse.
Cost, tokens and latency are still recorded.

## What apps should send

```python
client.chat.completions.create(
    model="gpt-oss-120b",            # public model name from the team's list
    messages=[...],
    extra_body={"metadata": {"session_id": conversation_id}},  # groups a conversation in Langfuse
)
```

Don't put user IDs or request IDs in tags - tags are for reporting dimensions only.

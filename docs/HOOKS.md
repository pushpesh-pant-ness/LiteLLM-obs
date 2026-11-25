# Hooks

Hooks are small Python classes that see, and may change, every request the gateway handles.
Observability is the first hook. Routing, prompt compression, guardrails or custom logging are
added the same way, without touching LiteLLM's config or `compose.yaml`.

## How it fits together

```
config/hooks.yaml ──(run.ps1: tools/render.py)──► .build/hooks.json ──► /etc/litellm/hooks.json
                                              └─► .build/litellm.yaml: callbacks: [..., gateway.entrypoint.pipeline]

request ─► LiteLLM auth (key, team, model access, budget, rpm)
        ─► pipeline.pre_call      hook 1 ► hook 2 ► ...   (may change model, messages, metadata)
        ─► cache lookup / provider call (router: retries, fallbacks)
        ─► pipeline.post_call     hook 1 ► hook 2 ► ...   (may replace the response)
        ─► response headers       x-gateway-hooks + headers hooks add
        ─► logging                log_success / log_failure (cost, tokens, cache hit known)
```

`gateway/entrypoint.py` is the only thing LiteLLM loads. It builds a `HookPipeline`
(`gateway/hooks/pipeline.py`), a LiteLLM `CustomLogger`, from the rendered hook list, and the
pipeline calls each enabled hook in the order of `config/hooks.yaml`.

## Writing a hook

1. Copy `gateway/hooks/_template.py` to `gateway/hooks/<module>.py`.
2. Subclass `Hook`, override what you need, and end the file with `HOOK = YourClass`.
3. Add it to `config/hooks.yaml`:

   ```yaml
   - name: prompt-compression
     module: prompt_compression
     enabled: true
     fail_open: true
     settings: { min_chars: 16000, keep_last_messages: 4 }
   ```

4. Re-run `.\run.ps1`. It validates the entry (the module must exist), regenerates the config and
   restarts LiteLLM. The `x-gateway-hooks` response header now includes the new hook.

### Methods

| Method | When | Return |
|---|---|---|
| `async pre_call(ctx, data)` | after auth, before cache / provider | `None` (changed `data` in place) or a new dict; raise `fastapi.HTTPException` to refuse |
| `async post_call(ctx, data, response)` | after a successful call | `None` or a replacement response |
| `async on_error(ctx, data, exception)` | after a failed call | – |
| `async response_headers(ctx, data, response)` | before the response is sent | dict of extra headers or `None` |
| `async log_success(log, kwargs)` / `log_failure(...)` | after logging (cost, tokens, cache hit) | – |
| `classmethod provision(ctx, settings)` | on `run provision` / every run, in the provision tool | – |

`ctx` (`RequestContext`) has `team_id`, `team_alias`, `team_metadata` (org, observability, ... set
from `teams.yaml`), `org`, `key_alias`, `key_hash`, `key_metadata`, `user_id`, `call_type`,
`model`, `auth` (the raw LiteLLM `UserAPIKeyAuth`) and `metadata(data)`. That last one returns the
request's metadata dict, where tags, trace fields and the cache namespace go.

`log` is LiteLLM's `StandardLoggingPayload`: `model_group`, `response_cost`, `prompt_tokens`,
`completion_tokens`, `cache_hit`, `metadata.user_api_key_team_alias`, and so on.

Provisioning `ctx` (`tools/provision.py: ProvisionContext`) has `teams` (resolved, with LiteLLM
`team_id`), `org`, `litellm(method, path, body, ok=...)`, `langfuse_admin()`,
`update_team_metadata(team_id, patch)`, `state` + `save_state()` (persisted in `.state/state.json`)
and `say(msg)`.

### Error policy (`fail_open`)

| Raised in a hook | `fail_open: true` | `fail_open: false` |
|---|---|---|
| `fastapi.HTTPException` | request refused with that status (deliberate) | same |
| any other exception | logged, request continues without the hook | request fails (HTTP 500) |

Use `fail_open: false` for anything that protects data or policy: guardrails, cache isolation,
redaction. Errors in logging stages are always logged and ignored.

### Rules of thumb

- **Model access is checked before hooks run.** A hook that changes `data["model"]`, such as an
  autorouter, must itself pick only from the caller's allowed models:
  `ctx.auth.team_models` (empty = all). The template shows this.
- Keep `pre_call` fast: it adds latency to every request. Do network calls in `log_*`.
- Put per-team switches in `teams.yaml` team metadata (as `observability.redact_content` does)
  and read them from `ctx.team_metadata`, so no restart is needed when only a team changes.
- Never log prompt text from a hook; Langfuse already holds it, and masks it for redacted teams.
- Order matters between hooks that change the same field. The cache key is computed after ALL
  `pre_call` hooks, so it always reflects the final (e.g. compressed or rerouted) request.

## Built-in hooks

| Hook | Runtime | Provisioning |
|---|---|---|
| `observability` | Tags `team:<id>` / `org:<id>`, trace name and user, `mask_input` / `mask_output` for `redact_content` teams, one `gateway.request` JSON log line per request. Enables LiteLLM's `prometheus` + `langfuse` integrations through its `litellm:` block. | Langfuse project + API key per team, LiteLLM team callback so the team's traces go to its project (`per_team_projects`). |
| `cache-isolation` | Sets the cache namespace `gw:team:<team id>` (or `key:` / `global`), overriding anything the client sends. | – |

## Ideas for later hooks

| Hook | `pre_call` does |
|---|---|
| autorouter | pick the cheapest allowed model that fits the prompt size / task (`data["model"]`) |
| prompt-compression | summarise or trim old messages above a size threshold |
| pii-guard | detect / mask PII in `data["messages"]`; `fail_open: false` |
| request-quota | per-user quotas from `data["user"]` on top of key limits |

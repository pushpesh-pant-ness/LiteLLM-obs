"""Template for a new hook. Copy to gateway/hooks/<your_module>.py, rename the class, keep the
methods you need, then add it to config/hooks.yaml:

    - name: my-hook
      module: my_module          # this file, without .py
      enabled: true
      fail_open: true            # false = an error in the hook rejects the request
      settings: { any: value }   # -> self.settings

Re-run the runner (it restarts LiteLLM with the new hook list). The `x-gateway-hooks` response
header lists the hooks that ran. Full guide: docs/HOOKS.md.

Example below: a minimal "autorouter" that sends long prompts to a long-context model.
"""

from __future__ import annotations

from .base import Hook, RequestContext


class ExampleHook(Hook):
    async def pre_call(self, ctx: RequestContext, data: dict) -> dict | None:
        # ctx.team_alias, ctx.key_alias, ctx.team_metadata, ctx.model, ctx.call_type ...
        # Change `data` in place, or return a new dict. Raise fastapi.HTTPException to refuse.
        #
        # NOTE: LiteLLM checked the key's model access BEFORE hooks run. A hook that changes
        # data["model"] must stay within the team's models itself, e.g.:
        #     allowed = set(ctx.auth.team_models or [])
        long_model = self.settings.get("long_prompt")
        limit = int(self.settings.get("long_prompt_chars", 32000))
        messages = data.get("messages") or []
        size = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
        allowed = set(getattr(ctx.auth, "team_models", None) or [])
        if long_model and size > limit and (not allowed or long_model in allowed):
            data["model"] = long_model
            ctx.metadata(data).setdefault("tags", []).append(f"routed:{long_model}")
        return data

    async def post_call(self, ctx: RequestContext, data: dict, response):
        return None   # or a modified response

    async def response_headers(self, ctx: RequestContext, data: dict, response) -> dict | None:
        return None   # e.g. {"x-my-hook": "1"}

    async def log_success(self, log: dict, kwargs: dict) -> None:
        # log = LiteLLM StandardLoggingPayload: model_group, response_cost, total_tokens,
        # cache_hit, metadata.user_api_key_team_alias, ...
        return None

    @classmethod
    def provision(cls, ctx, settings: dict) -> None:
        # Runs on `run provision`: create external resources the hook needs.
        # ctx.teams, ctx.org, ctx.litellm(method, path, body), ctx.state, ctx.say(...)
        return None


# The pipeline loads exactly this name.
HOOK = ExampleHook

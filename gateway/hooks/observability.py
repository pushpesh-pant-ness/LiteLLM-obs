"""Observability hook: makes every request traceable per org / team / key.

Runtime (every request, inside LiteLLM):
  pre_call     tags team:<id> and org:<id> (Langfuse trace tags + LiteLLM spend tags), sets the
               Langfuse trace name and user, and masks prompt / answer in Langfuse for teams with
               observability.redact_content (config/teams.yaml).
  log_success  one JSON line per request on stdout ("gateway.request {...}"): team, key, model,
  log_failure  tokens, cost, latency, cache hit, status. Never prompt text.

Provisioning (`run provision`, tools/provision.py):
  per_team_projects: true -> one Langfuse project per team in the org of config/teams.yaml, a
  project API key for it, and a LiteLLM team callback so that team's traces land in its own
  project. Traffic without a team (admin / UI) goes to the default project.

Transport itself is LiteLLM's built-in `langfuse` and `prometheus` integrations, switched on by
this hook's `litellm:` block in config/hooks.yaml.
"""

from __future__ import annotations

import json
import sys

from .base import Hook, RequestContext

_PROJECT_KEY_NOTE = "litellm team traces (gateway provisioning)"


class Observability(Hook):
    # ---------------------------------------------------------------- runtime
    async def pre_call(self, ctx: RequestContext, data: dict) -> None:
        md = ctx.metadata(data)
        team = ctx.team_alias or "none"

        if self.settings.get("tag_requests", True):
            tags = md.get("tags")
            tags = list(tags) if isinstance(tags, (list, tuple)) else []
            for tag in (f"team:{team}", f"org:{ctx.org}" if ctx.org else None):
                if tag and tag not in tags:
                    tags.append(tag)
            md["tags"] = tags

        md.setdefault("trace_name", f"{team}/{ctx.call_type or 'call'}")
        if not md.get("trace_user_id"):
            md["trace_user_id"] = data.get("user") or ctx.key_alias or "admin"

        obs = ctx.team_metadata.get("observability") or {}
        if obs.get("redact_content"):
            md["mask_input"] = True
            md["mask_output"] = True
        return None

    async def log_success(self, log: dict, kwargs: dict) -> None:
        if self.settings.get("log_requests", True):
            self._emit(log, "success")

    async def log_failure(self, log: dict, kwargs: dict) -> None:
        if self.settings.get("log_requests", True):
            self._emit(log, "failure")

    @staticmethod
    def _emit(log: dict, status: str) -> None:
        md = log.get("metadata") or {}
        start, end = log.get("startTime"), log.get("endTime")
        line = {
            "status": log.get("status") or status,
            "team": md.get("user_api_key_team_alias"),
            "key": md.get("user_api_key_alias"),
            "model": log.get("model_group") or log.get("model"),
            "route": log.get("model"),
            "call_type": log.get("call_type"),
            "prompt_tokens": log.get("prompt_tokens"),
            "completion_tokens": log.get("completion_tokens"),
            "cost_usd": log.get("response_cost"),
            "latency_ms": round((end - start) * 1000) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None,
            "cache_hit": bool(log.get("cache_hit")),
            "request_id": log.get("id"),
        }
        if status == "failure":
            err = log.get("error_information") or {}
            line["error"] = err.get("error_class") or log.get("error_str")
        sys.stdout.write("gateway.request " + json.dumps(line, default=str) + "\n")
        sys.stdout.flush()

    # ---------------------------------------------------------------- provisioning
    @classmethod
    def provision(cls, ctx, settings: dict) -> None:
        if not settings.get("per_team_projects", True):
            ctx.say("observability: per_team_projects off - all traces go to the default Langfuse project")
            for team in ctx.teams:
                ctx.litellm("DELETE", f"/team/{team['team_id']}/callback/langfuse", ok=(200, 404, 400))
            return

        admin = ctx.langfuse_admin()
        org_id = ctx.org["id"]
        known = ctx.state.setdefault("langfuse_projects", {})
        for team in ctx.teams:
            project_id, created = admin.ensure_project(org_id, team["id"])
            entry = known.get(team["id"]) or {}
            if entry.get("project_id") == project_id and ctx.langfuse_key_works(entry.get("public_key"), entry.get("secret_key"), project_id):
                key_state = "kept"
            else:
                pk, sk = admin.create_project_key(project_id, _PROJECT_KEY_NOTE)
                entry = {"project_id": project_id, "public_key": pk, "secret_key": sk}
                known[team["id"]] = entry
                ctx.save_state()
                key_state = "new key"

            # Route the team's traces (success + failure) to its project. LiteLLM returns the
            # callback keys redacted, so the state file remembers which key was registered.
            current = ctx.litellm("GET", f"/team/{team['team_id']}/callback")
            if not (_has_langfuse_callback(current) and entry.get("routed_key") == entry["public_key"]
                    and entry.get("routed_team_id") == team["team_id"]):
                ctx.litellm("DELETE", f"/team/{team['team_id']}/callback/langfuse", ok=(200, 404, 400))
                ctx.litellm("POST", f"/team/{team['team_id']}/callback", {
                    "callback_name": "langfuse",
                    "callback_type": "success_and_failure",
                    "callback_vars": {
                        "langfuse_public_key": entry["public_key"],
                        "langfuse_secret_key": entry["secret_key"],
                        "langfuse_host": ctx.langfuse_internal_url,
                    },
                })
                entry.update(routed_key=entry["public_key"], routed_team_id=team["team_id"])
                ctx.save_state()
                routed = "routed"
            else:
                routed = "already routed"
            ctx.update_team_metadata(team["team_id"], {"langfuse_project_id": project_id})
            ctx.say(f"langfuse project {team['id']:<14} {'created' if created else 'exists '}  ({key_state}, traces {routed})")


def _has_langfuse_callback(res) -> bool:
    """True when the team sends both successes and failures to Langfuse."""
    data = res.get("data", res) if isinstance(res, dict) else {}
    return ("langfuse" in (data.get("success_callbacks") or data.get("success_callback") or [])
            and "langfuse" in (data.get("failure_callbacks") or data.get("failure_callback") or []))


HOOK = Observability

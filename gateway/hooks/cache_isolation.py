"""Cache-isolation hook: one response-cache namespace per team (or per key).

LiteLLM's cache key is built from the model + messages + params only, so without this hook two
teams sending the same prompt would share one cached answer. This hook puts each request in a
namespace of the cache Redis:  <namespace>:team:<team id>  (scope: team, the default)
                               <namespace>:key:<key hash>   (scope: key)
scope: global turns isolation off (everyone shares the cache).

It always overwrites a client-supplied namespace, so a caller cannot read another team's
entries - hence fail_open: false in config/hooks.yaml.
"""

from __future__ import annotations

import os

from .base import Hook, RequestContext


class CacheIsolation(Hook):
    async def pre_call(self, ctx: RequestContext, data: dict) -> None:
        scope = self.settings.get("scope", "team")
        if scope not in ("team", "key", "global"):
            raise ValueError(f"cache-isolation: scope must be team | key | global, got {scope!r}")
        md = ctx.metadata(data)
        if scope == "global":
            md.pop("redis_namespace", None)
            return None
        base = self.settings.get("namespace") or os.environ.get("CACHE_NAMESPACE", "gw")
        owner = ctx.team_id if scope == "team" else ctx.key_hash
        md["redis_namespace"] = f"{base}:{scope}:{owner or 'none'}"
        return None


HOOK = CacheIsolation

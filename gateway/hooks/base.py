"""Hook base class and the request context every hook receives.

A hook overrides only the methods it needs. All runtime methods are async and run inside the
LiteLLM proxy process, in the order of config/hooks.yaml:

    pre_call(ctx, data)                  before the provider call. Change `data` in place (model,
                                         messages, metadata, ...) or return a new dict. Raise
                                         fastapi.HTTPException to reject the request.
    post_call(ctx, data, response)       after a successful call, before the client gets it.
                                         Return a replacement response, or None to keep it.
    on_error(ctx, data, exception)       after a failed call (logging / alerting).
    response_headers(ctx, data, response)
                                         extra HTTP response headers (dict) or None.
    log_success(log, kwargs) / log_failure(log, kwargs)
                                         after the call is fully logged (cost, tokens, cache hit
                                         known). `log` = LiteLLM's StandardLoggingPayload (dict).

Setup-time (runs once per `run provision`, in the provision tool, not in the proxy):

    provision(ctx, settings)             classmethod. Create what the hook needs outside LiteLLM
                                         (e.g. Langfuse projects). See tools/provision.py
                                         ProvisionContext for what `ctx` offers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


def request_metadata(data: dict) -> dict:
    """The metadata dict LiteLLM reads for this request (created if missing).

    Chat / embeddings routes use data["metadata"]; a few routes (e.g. /v1/messages, batches) use
    data["litellm_metadata"]. Tags, trace fields, cache namespace etc. all go here.
    """
    key = "litellm_metadata" if isinstance(data.get("litellm_metadata"), dict) else "metadata"
    md = data.get(key)
    if not isinstance(md, dict):
        md = {}
        data[key] = md
    return md


@dataclass
class RequestContext:
    """Who is calling and what - built from LiteLLM's auth result for every request."""

    call_type: str = ""
    model: str = ""
    team_id: str | None = None
    team_alias: str | None = None
    team_metadata: dict = field(default_factory=dict)
    key_alias: str | None = None
    key_hash: str | None = None
    key_metadata: dict = field(default_factory=dict)
    user_id: str | None = None
    auth: Any = None  # the raw litellm UserAPIKeyAuth, for anything not copied above

    @classmethod
    def from_auth(cls, auth: Any, call_type: str = "", data: dict | None = None) -> "RequestContext":
        def attr(name):
            return getattr(auth, name, None) if auth is not None else None

        return cls(
            call_type=str(call_type or ""),
            model=str((data or {}).get("model") or ""),
            team_id=attr("team_id"),
            team_alias=attr("team_alias"),
            team_metadata=dict(attr("team_metadata") or {}),
            key_alias=attr("key_alias"),
            key_hash=attr("token") or attr("api_key"),
            key_metadata=dict(attr("metadata") or {}),
            user_id=attr("user_id"),
            auth=auth,
        )

    @property
    def org(self) -> str | None:
        return self.team_metadata.get("org")

    def metadata(self, data: dict) -> dict:
        return request_metadata(data)


class Hook:
    """Base class. Subclass it in gateway/hooks/<module>.py and set `HOOK = YourClass` there."""

    def __init__(self, name: str, settings: dict | None = None):
        self.name = name
        self.settings = dict(settings or {})
        self.log = logging.getLogger(f"gateway.hooks.{name}")

    # ---------------------------------------------------------------- runtime (proxy)
    async def pre_call(self, ctx: RequestContext, data: dict) -> dict | None:
        return None

    async def post_call(self, ctx: RequestContext, data: dict, response: Any) -> Any:
        return None

    async def on_error(self, ctx: RequestContext, data: dict, exception: Exception) -> None:
        return None

    async def response_headers(self, ctx: RequestContext, data: dict, response: Any) -> dict | None:
        return None

    async def log_success(self, log: dict, kwargs: dict) -> None:
        return None

    async def log_failure(self, log: dict, kwargs: dict) -> None:
        return None

    # ---------------------------------------------------------------- setup time (provision tool)
    @classmethod
    def provision(cls, ctx: Any, settings: dict) -> None:
        return None

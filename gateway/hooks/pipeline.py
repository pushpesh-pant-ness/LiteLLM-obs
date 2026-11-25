"""The hook pipeline: the single LiteLLM callback that runs every enabled hook in order.

LiteLLM calls this object at each stage of a request; it hands the request to each hook of
config/hooks.yaml (rendered to /etc/litellm/hooks.json) in turn. Error policy per hook:
  - fastapi.HTTPException raised by a hook  -> always rejects the request (deliberate refusal)
  - any other exception, fail_open: true    -> logged, the request continues without that hook
  - any other exception, fail_open: false   -> the request fails
Logging-stage errors are always logged and swallowed (the response is already on its way).
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any

from fastapi import HTTPException
from litellm._logging import verbose_proxy_logger as log
from litellm.integrations.custom_logger import CustomLogger

from .base import Hook, RequestContext

HEADER = "x-gateway-hooks"


def load_hooks(entries: list[dict]) -> list[tuple[Hook, bool]]:
    """[(hook instance, fail_open)] for the enabled entries, in order."""
    hooks = []
    for e in entries:
        if not e.get("enabled", True):
            continue
        module = importlib.import_module(f"hooks.{e['module']}")
        cls = getattr(module, "HOOK", None)
        if not (isinstance(cls, type) and issubclass(cls, Hook)):
            raise TypeError(f"gateway/hooks/{e['module']}.py must set HOOK = <a Hook subclass>")
        hooks.append((cls(e["name"], e.get("settings")), bool(e.get("fail_open", True))))
    return hooks


class HookPipeline(CustomLogger):
    def __init__(self, hooks: list[tuple[Hook, bool]]):
        super().__init__()
        self.hooks = hooks
        self.names = ",".join(h.name for h, _ in hooks)
        print(f"gateway hooks loaded: {self.names or '(none)'}", file=sys.stdout, flush=True)

    @classmethod
    def from_file(cls, path: str) -> "HookPipeline":
        with open(path, encoding="utf-8") as f:
            return cls(load_hooks(json.load(f).get("hooks", [])))

    async def _guard(self, hook: Hook, fail_open: bool, stage: str, coro) -> Any:
        try:
            return await coro
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 - the policy is the point
            if fail_open:
                log.exception("gateway hook %s failed in %s (fail_open: request continues): %s", hook.name, stage, e)
                return None
            log.exception("gateway hook %s failed in %s (fail_open false: request rejected)", hook.name, stage)
            raise HTTPException(status_code=500, detail=f"gateway hook {hook.name} failed") from e

    # ---------------------------------------------------------------- request stages
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        ctx = RequestContext.from_auth(user_api_key_dict, call_type, data)
        for hook, fail_open in self.hooks:
            out = await self._guard(hook, fail_open, "pre_call", hook.pre_call(ctx, data))
            if isinstance(out, dict):
                data = out
                ctx.model = str(data.get("model") or ctx.model)
        return data

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        ctx = RequestContext.from_auth(user_api_key_dict, data.get("call_type", ""), data)
        for hook, fail_open in self.hooks:
            out = await self._guard(hook, fail_open, "post_call", hook.post_call(ctx, data, response))
            if out is not None:
                response = out
        return response

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        ctx = RequestContext.from_auth(user_api_key_dict, "", request_data)
        for hook, _ in self.hooks:
            try:
                await hook.on_error(ctx, request_data, original_exception)
            except Exception:  # noqa: BLE001
                log.exception("gateway hook %s failed in on_error", hook.name)
        return None

    async def async_post_call_response_headers_hook(self, data, user_api_key_dict, response,
                                                    request_headers=None, litellm_call_info=None):
        ctx = RequestContext.from_auth(user_api_key_dict, "", data)
        headers = {HEADER: self.names}
        for hook, _ in self.hooks:
            try:
                extra = await hook.response_headers(ctx, data, response)
            except Exception:  # noqa: BLE001
                log.exception("gateway hook %s failed in response_headers", hook.name)
                continue
            if extra:
                headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    # ---------------------------------------------------------------- logging stages
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        payload = kwargs.get("standard_logging_object") or {}
        for hook, _ in self.hooks:
            try:
                await hook.log_success(payload, kwargs)
            except Exception:  # noqa: BLE001
                log.exception("gateway hook %s failed in log_success", hook.name)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        payload = kwargs.get("standard_logging_object") or {}
        for hook, _ in self.hooks:
            try:
                await hook.log_failure(payload, kwargs)
            except Exception:  # noqa: BLE001
                log.exception("gateway hook %s failed in log_failure", hook.name)

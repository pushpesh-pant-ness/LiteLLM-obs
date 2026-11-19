"""Close a gap in LiteLLM's message redaction: model *reasoning* text.

For "sensitive" teams LiteLLM replaces prompt and answer with "redacted-by-litellm" before logging,
but reasoning models on some providers (e.g. Groq gpt-oss) return their chain of thought in a
`reasoning` field that LiteLLM 1.106 does not redact - and it usually quotes the user's prompt.

This logging hook runs before Langfuse (and every other logger) receives a call. When LiteLLM is
already redacting that call (same check LiteLLM uses: team setting, header, or global flag), it
blanks the reasoning fields too. It edits a copy, so the response sent to the app is unchanged.
Registered in config.yaml:  callbacks: [..., "redact_reasoning.proxy_handler_instance"]
"""

import copy

from litellm._logging import verbose_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.redact_messages import should_redact_message_logging

REDACTED = "redacted-by-litellm"
TEXT_KEYS = ("reasoning", "reasoning_content")
DROP_KEYS = ("thinking_blocks",)


def _scrub(msg):
    if msg is None:
        return
    if isinstance(msg, dict):
        targets = [msg, msg.get("provider_specific_fields")]
        for t in targets:
            if not isinstance(t, dict):
                continue
            for k in TEXT_KEYS:
                if t.get(k) is not None:
                    t[k] = REDACTED
            for k in DROP_KEYS:
                if t.get(k) is not None:
                    t[k] = None
        return
    for k in TEXT_KEYS + DROP_KEYS:
        if getattr(msg, k, None) is not None:
            setattr(msg, k, REDACTED if k in TEXT_KEYS else None)
    _scrub(getattr(msg, "provider_specific_fields", None))


def _scrub_choices(choices):
    for c in choices or []:
        if isinstance(c, dict):
            _scrub(c.get("message"))
            _scrub(c.get("delta"))
        else:
            _scrub(getattr(c, "message", None))
            _scrub(getattr(c, "delta", None))


class RedactReasoning(CustomLogger):
    def _apply(self, kwargs, result):
        try:
            if not should_redact_message_logging(kwargs):
                return kwargs, result
            result = copy.deepcopy(result)  # never touch the object returned to the client
            _scrub_choices(result.get("choices") if isinstance(result, dict) else getattr(result, "choices", None))
            slo = kwargs.get("standard_logging_object")
            if isinstance(slo, dict) and isinstance(slo.get("response"), dict):
                slo = dict(slo)
                slo["response"] = copy.deepcopy(slo["response"])
                _scrub_choices(slo["response"].get("choices"))
                kwargs["standard_logging_object"] = slo
        except Exception as e:  # never break logging
            verbose_logger.error("redact_reasoning: %s", e)
        return kwargs, result

    def logging_hook(self, kwargs, result, call_type):
        return self._apply(kwargs, result)

    async def async_logging_hook(self, kwargs, result, call_type):
        return self._apply(kwargs, result)


proxy_handler_instance = RedactReasoning()

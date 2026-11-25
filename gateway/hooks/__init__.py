"""Gateway hooks: small classes that see (and may change) every request LiteLLM handles.

    base.py             Hook base class + RequestContext (what a hook gets)
    pipeline.py         runs the enabled hooks in order (the one object LiteLLM calls)
    observability.py    tags, trace routing, redaction, request log + Langfuse provisioning
    cache_isolation.py  per-team response-cache namespace
    _template.py        copy this to write a new hook

Enable / order / configure hooks in config/hooks.yaml. Guide: docs/HOOKS.md.
"""

from .base import Hook, RequestContext, request_metadata

__all__ = ["Hook", "RequestContext", "request_metadata"]

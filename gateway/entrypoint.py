"""What LiteLLM loads: `litellm_settings.callbacks: ["gateway.entrypoint.pipeline"]`.

LiteLLM resolves that path relative to its config file (/etc/litellm/config.yaml), so this file is
mounted at /etc/litellm/gateway/entrypoint.py. It builds ONE callback object - the hook pipeline -
from the hook list the runner rendered from config/hooks.yaml (/etc/litellm/hooks.json).
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from hooks.pipeline import HookPipeline  # noqa: E402

pipeline = HookPipeline.from_file(
    os.environ.get("GATEWAY_HOOKS_FILE", os.path.join(os.path.dirname(_HERE), "hooks.json"))
)

"""Send test traffic through the proxy with every key in generated-keys.json.

    docker compose run --rm --entrypoint python bootstrap smoke_test.py [N]          # mock model, free
    docker compose run --rm --entrypoint python bootstrap smoke_test.py --real [N]   # real models (costs money)

Mock mode: each key sends N calls to mock-gpt-4o-mini, then one call to a model that does not exist.
Real mode: each key sends N short calls (default 2) to every real model its team may use, per
tenants.json, then one call to a real model the team may NOT use (expect HTTP 403).
Each key's calls share one Langfuse session.
"""

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

URL = os.environ.get("LITELLM_URL", "http://localhost:4000").rstrip("/")
TENANTS = os.environ.get("TENANTS_FILE", "tenants.json")
MOCK = "mock-gpt-4o-mini"
REAL = "--real" in sys.argv
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
N = int(_args[0]) if _args else (2 if REAL else 5)
MAX_TOKENS = int(os.environ.get("SMOKE_MAX_TOKENS", "300"))  # keeps real calls cheap


def call(key, model, text, session):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "metadata": {"session_id": session, "generation_name": "smoke-test"},
    }
    if REAL:
        body["max_tokens"] = MAX_TOKENS
    req = urllib.request.Request(
        URL + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read())
            cost = float(r.headers.get("x-litellm-response-cost") or 0)
            return r.status, res, cost
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300], 0.0


def team_models():
    """key alias -> list of models its team may use (from tenants.json)."""
    with open(TENANTS, encoding="utf-8") as f:
        cfg = json.load(f)
    out = {}
    for org in cfg["orgs"]:
        for team in org.get("teams", []):
            for key in team.get("keys", []):
                out[key["alias"]] = team.get("models", [])
    return out


with open("generated-keys.json", encoding="utf-8") as f:
    keys = json.load(f)

if not REAL:
    for k in keys:
        session = f"smoke-{uuid.uuid4().hex[:8]}"
        ok = 0
        for i in range(N):
            status, _, _ = call(k["key"], MOCK, f"Smoke test {i}: summarise our refund policy in one line.", session)
            ok += status == 200
        bad_status, _, _ = call(k["key"], "model-this-team-cannot-use", "should fail", session)
        print(f"{k['key_alias']:<28} {ok}/{N} ok, forbidden-model call -> HTTP {bad_status}, session {session}")
    sys.exit(0)

allowed = team_models()
all_real = sorted({m for ms in allowed.values() for m in ms if m != MOCK})
failures = 0
total_cost = 0.0
for k in keys:
    models = [m for m in allowed.get(k["key_alias"], []) if m != MOCK]
    session = f"real-{k['key_alias']}-{uuid.uuid4().hex[:6]}"
    print(f"{k['key_alias']}  (team {k['team']}, session {session})")
    if not models:
        print("  no real models assigned in tenants.json")
    for m in models:
        ok, tokens, cost, err = 0, 0, 0.0, ""
        for i in range(N):
            status, res, c = call(k["key"], m, f"Test {i}: in one short sentence, what is a refund policy?", session)
            if status == 200:
                ok += 1
                tokens += res.get("usage", {}).get("total_tokens", 0)
                cost += c
            else:
                err = f" - HTTP {status}: {str(res)[:160]}"
        failures += ok < N
        total_cost += cost
        print(f"  {m:<30} {ok}/{N} ok  {tokens:>5} tokens  ${cost:.6f}{err}")
    forbidden = next((m for m in all_real if m not in allowed.get(k["key_alias"], [])), None)
    if forbidden:
        status, _, _ = call(k["key"], forbidden, "should be blocked", session)
        verdict = "blocked as expected" if status in (401, 403) else "NOT BLOCKED - check team models"
        failures += status not in (401, 403)
        print(f"  {forbidden:<30} not allowed -> HTTP {status} ({verdict})")

print(f"\nTotal cost of this run: ${total_cost:.6f}")
sys.exit(1 if failures else 0)

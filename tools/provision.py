"""Applies config/teams.yaml + the hooks' setup to the running stack. Idempotent.

Run by the runner on every `up`, or alone with `run provision` (compose service `provision`,
LiteLLM image). Reads what tools/render.py resolved in .build/ (teams.json, models.json, hooks.json).

  1. metrics key   a LiteLLM key that may ONLY call /metrics, for Prometheus (never the master key)
  2. models        LiteLLM serves every model the catalog enabled
  3. teams         one LiteLLM team per entry: models, budget, rpm/tpm, metadata (org, observability)
  4. keys          one key per entry, with the FIXED secret from .env (KEY_<TEAM>_<KEY>);
                   a changed secret in .env replaces the key (rotation)
  5. hooks         each enabled hook's provision() (observability: Langfuse project per team)
Nothing is deleted when removed from the config; leftovers are reported.
"""

import hashlib
import importlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = "/w"
BUILD = os.path.join(ROOT, ".build")
STATE_FILE = os.path.join(ROOT, ".state", "state.json")
URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
LANGFUSE_URL = os.environ.get("LANGFUSE_URL", "http://langfuse-web:3000").rstrip("/")
MASTER = os.environ["LITELLM_MASTER_KEY"]
METRICS_TOKEN = "/prom-secrets/litellm_metrics_token"
METRICS_ALIAS = "infra-prometheus-scraper"
TEMP_PREFIX = "selftest-"

sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "gateway"))
from langfuse_admin import LangfuseAdmin, project_of_key  # noqa: E402
from hooks.base import Hook  # noqa: E402


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path}: HTTP {status}: {str(body)[:300]}")
        self.status, self.body = status, body


def api(method, path, body=None, key=MASTER, ok=(200,)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(errors="replace"), e.code
    try:
        res = json.loads(raw) if raw else {}
    except ValueError:
        res = {"raw": raw}
    if status not in ok:
        raise ApiError(method, path, status, res)
    return res


def load(name):
    with open(os.path.join(BUILD, name), encoding="utf-8") as f:
        return json.load(f)


def say(msg):
    print(f"  {msg}", flush=True)


# ------------------------------------------------------------------ context handed to hooks
class ProvisionContext:
    """What a hook's provision(ctx, settings) gets."""

    def __init__(self, org, teams, state):
        self.org = org                    # {"id", "name"}
        self.teams = teams                # resolved teams + "team_id" (LiteLLM)
        self.state = state                # persisted to .state/state.json (save_state())
        self.env = dict(os.environ)
        self.langfuse_internal_url = LANGFUSE_URL
        self._lf = None

    def litellm(self, method, path, body=None, ok=(200,)):
        return api(method, path, body, ok=ok)

    def langfuse_admin(self):
        if self._lf is None:
            self._lf = LangfuseAdmin(LANGFUSE_URL, os.environ["LANGFUSE_ADMIN_EMAIL"], os.environ["LANGFUSE_ADMIN_PASSWORD"])
        return self._lf

    def langfuse_key_works(self, public_key, secret_key, project_id=None):
        got = project_of_key(LANGFUSE_URL, public_key, secret_key)
        return got is not None and (project_id is None or got == project_id)

    def update_team_metadata(self, team_id, patch):
        info = api("GET", f"/team/info?team_id={team_id}")
        md = dict((info.get("team_info") or {}).get("metadata") or {})
        if all(md.get(k) == v for k, v in patch.items()):
            return
        api("POST", "/team/update", {"team_id": team_id, "metadata": {**md, **patch}})

    def save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, STATE_FILE)

    def say(self, msg):
        say(msg)


# ------------------------------------------------------------------ 1 metrics key
def metrics_key():
    if os.path.exists(METRICS_TOKEN):
        with open(METRICS_TOKEN, encoding="utf-8") as f:
            token = f.read().strip()
        try:
            api("GET", "/metrics/", key=token)
            say(f"metrics key {METRICS_ALIAS} ok (kept)")
            return
        except (ApiError, ValueError):
            pass
    api("POST", "/key/delete", {"key_aliases": [METRICS_ALIAS]}, ok=(200, 400, 404))
    res = api("POST", "/key/generate", {"key_alias": METRICS_ALIAS, "allowed_routes": ["/metrics", "/metrics/"],
                                        "metadata": {"purpose": "prometheus scraping"}})
    tmp = METRICS_TOKEN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(res["key"])
    os.chmod(tmp, 0o644)   # Prometheus runs as nobody
    os.replace(tmp, METRICS_TOKEN)
    say(f"metrics key {METRICS_ALIAS} created (may only call /metrics)")


# ------------------------------------------------------------------ 3 teams / 4 keys
def list_teams():
    res = api("GET", "/team/list")
    teams = res if isinstance(res, list) else res.get("teams", [])
    return {t.get("team_alias"): t for t in teams if t.get("team_alias")}


def list_keys():
    out, page = {}, 1
    while True:
        res = api("GET", f"/key/list?return_full_object=true&size=100&page={page}")
        keys = res.get("keys", [])
        for k in keys:
            if isinstance(k, dict) and k.get("key_alias"):
                out[k["key_alias"]] = k
        if page >= int(res.get("total_pages") or 1) or not keys:
            return out
        page += 1


def ensure_team(t, org, live):
    body = {"team_alias": t["id"], "models": t["models"], "max_budget": t["max_budget"],
            "budget_duration": t["budget_period"], "rpm_limit": t["rpm"], "tpm_limit": t["tpm"]}
    meta = {"org": org["id"], "org_name": org["name"], "team_name": t["name"], "observability": t["observability"]}
    summary = f"${t['max_budget']}/{t['budget_period']}, models: {', '.join(t['models'])}"
    if t["id"] in live:
        cur = live[t["id"]]
        api("POST", "/team/update", {"team_id": cur["team_id"], **body, "metadata": {**(cur.get("metadata") or {}), **meta}})
        say(f"team {t['id']:<12} updated  ({summary})")
        return cur["team_id"]
    res = api("POST", "/team/new", {**body, "metadata": meta})
    say(f"team {t['id']:<12} created  ({summary})")
    return res["team_id"]


def ensure_key(k, team_id, live):
    secret = os.environ.get(k["env"], "")
    if not secret.startswith("sk-"):
        raise SystemExit(f"  FAIL  .env: {k['env']} is missing (re-run the runner, it generates it)")
    hashed = hashlib.sha256(secret.encode()).hexdigest()
    limits = {"max_budget": k["budget_usd"], "budget_duration": k["budget_period"] if k["budget_usd"] is not None else None,
              "rpm_limit": k["rpm"], "tpm_limit": k["tpm"]}
    limits = {f: v for f, v in limits.items() if v is not None}
    txt = ", ".join(f"{f}={v}" for f, v in limits.items()) or "no limits"
    cur = live.get(k["alias"])
    if cur and cur.get("token") == hashed and cur.get("team_id") == team_id:
        api("POST", "/key/update", {"key": hashed, **limits,
                                    "metadata": {**(cur.get("metadata") or {}), "tags": k["tags"]}})
        say(f"key  {k['alias']:<22} updated  ({txt})")
        return
    action = "rotated " if cur else "created "
    if cur:
        api("POST", "/key/delete", {"key_aliases": [k["alias"]]})
    # Tags go in metadata.tags (drives spend tags); top-level `tags` is a LiteLLM Enterprise field.
    api("POST", "/key/generate", {"key": secret, "key_alias": k["alias"], "team_id": team_id, **limits,
                                  "metadata": {"tags": k["tags"]}})
    say(f"key  {k['alias']:<22} {action} ({txt}; secret = .env {k['env']})")


# ------------------------------------------------------------------ main
def main():
    teams_cfg, models, hooks = load("teams.json"), load("models.json"), load("hooks.json")
    org, teams = teams_cfg["org"], teams_cfg["teams"]
    for i in range(36):
        try:
            api("GET", "/health/liveliness")
            break
        except (ApiError, urllib.error.URLError, OSError):
            time.sleep(5)

    print("== metrics key")
    metrics_key()

    print("== models")
    served = {m["id"] for m in api("GET", "/v1/models").get("data", [])}
    want = [m["name"] for m in models["enabled"]]
    missing = [m for m in want if m not in served]
    if missing:
        raise SystemExit(f"  FAIL  LiteLLM does not serve {missing} - check `run logs litellm`")
    say(f"served: {', '.join(want)}")
    for name, creds in models["disabled"].items():
        say(f"skipped: {name} (no {', '.join(creds)} in .env)")

    print(f"== teams (org {org['id']})")
    live_teams, live_keys = list_teams(), list_keys()
    for t in teams:
        t["team_id"] = ensure_team(t, org, live_teams)
        for k in t["keys"]:
            ensure_key(k, t["team_id"], live_keys)

    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    ctx = ProvisionContext(org, teams, state)
    print("== hooks")
    for h in hooks["hooks"]:
        cls = getattr(importlib.import_module(f"hooks.{h['module']}"), "HOOK")
        if cls.provision.__func__ is Hook.provision.__func__:
            say(f"{h['name']}: nothing to provision")
            continue
        cls.provision(ctx, h["settings"])
    ctx.save_state()

    wanted_t = {t["id"] for t in teams}
    wanted_k = {k["alias"] for t in teams for k in t["keys"]} | {METRICS_ALIAS}
    extra_t = sorted(a for a in live_teams if a not in wanted_t)
    extra_k = sorted(a for a in live_keys if a not in wanted_k and not a.startswith(TEMP_PREFIX))
    if extra_t or extra_k:
        say(f"note: in LiteLLM but not in config/teams.yaml (left alone): teams {extra_t or '-'}, keys {extra_k or '-'}")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        print(f"  FAIL  LiteLLM API: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"  FAIL  {e}")
        sys.exit(1)

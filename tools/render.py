"""config/*.yaml + .env  ->  .build/  (everything the containers read). Run by the runner.

    python -I tools/render.py            validate, fill .env, write .build/
    python -I tools/render.py --images   print the image list (one per line)

Runs inside the LiteLLM image (Python + PyYAML), with the repo mounted at /w.

.env        created from .env.example if missing. Empty (auto) values get random ones; every
            team key of config/teams.yaml gets a KEY_<TEAM>_<KEY> line. Set values never change.
.build/     regenerated on every run, git-ignored, contains secrets - never edit or commit:
  compose.env         settings + port map + images + credentials, read by `docker compose`
  litellm.yaml        LiteLLM config = gateway/litellm.base.yaml + models + cache + hooks
  litellm.env         provider credentials only (what the LiteLLM containers get)
  hooks.json          enabled hooks, in order (read by gateway/entrypoint.py)
  models.json, teams.json   resolved catalog / teams (read by provision + selftest)
  prometheus.yml, grafana/dashboards/   port map / public URLs filled in
"""

import copy
import json
import os
import re
import secrets
import shutil
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
BUILD = os.path.join(ROOT, ".build")
ENV = os.path.join(ROOT, ".env")
ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
HOOKS_DIR = os.path.join(ROOT, "gateway", "hooks")
CERT_BUNDLE = os.path.join(ROOT, "certs", "ca-bundle.pem")
CORP_ROOT = os.path.join(ROOT, "certs", "corp-root-ca.pem")

DEFAULT_PROJECT_ID = "gateway-default"   # Langfuse project for traffic without a team
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
IMAGE_KEYS = ["litellm", "postgres", "redis", "clickhouse", "langfuse", "langfuse_worker",
              "grafana", "prometheus", "python"]
PORT_KEYS = ["litellm", "langfuse", "grafana", "prometheus", "postgres", "redis_state", "redis_cache",
             "langfuse_db", "langfuse_redis", "langfuse_worker", "local_s3", "clickhouse_http",
             "clickhouse_native"]
FIXED_PORTS = {"clickhouse_http": 8123, "clickhouse_native": 9000}   # set by the ClickHouse image


def _hex(n):
    return lambda: secrets.token_hex(n)


AUTO = {
    "LITELLM_UI_PASSWORD": _hex(12),
    "LITELLM_MASTER_KEY": lambda: "sk-" + secrets.token_hex(24),
    "LITELLM_SALT_KEY": lambda: "sk-" + secrets.token_hex(24),
    "GRAFANA_ADMIN_PASSWORD": _hex(12),
    "LANGFUSE_ADMIN_PASSWORD": _hex(12),
    "LANGFUSE_PUBLIC_KEY": lambda: "pk-lf-" + secrets.token_hex(16),
    "LANGFUSE_SECRET_KEY": lambda: "sk-lf-" + secrets.token_hex(16),
    "POSTGRES_PASSWORD": _hex(16),
    "LITELLM_DB_PASSWORD": _hex(16),
    "GRAFANA_RO_DB_PASSWORD": _hex(16),
    "REDIS_STATE_PASSWORD": _hex(16),
    "REDIS_CACHE_PASSWORD": _hex(16),
    "LANGFUSE_DB_PASSWORD": _hex(16),
    "LANGFUSE_REDIS_PASSWORD": _hex(16),
    "CLICKHOUSE_PASSWORD": _hex(16),
    "LANGFUSE_SALT": _hex(16),
    "LANGFUSE_NEXTAUTH_SECRET": _hex(24),
    "LANGFUSE_ENCRYPTION_KEY": _hex(32),
    "LOCAL_S3_ACCESS_KEY": lambda: "s3-" + secrets.token_hex(8),
    "LOCAL_S3_SECRET_KEY": _hex(24),
}
URL_SAFE = {"POSTGRES_PASSWORD", "LITELLM_DB_PASSWORD", "GRAFANA_RO_DB_PASSWORD", "REDIS_STATE_PASSWORD",
            "REDIS_CACHE_PASSWORD", "LANGFUSE_DB_PASSWORD", "LANGFUSE_REDIS_PASSWORD", "CLICKHOUSE_PASSWORD"}
REQUIRED = ["LITELLM_UI_USERNAME", "GRAFANA_ADMIN_USER", "LANGFUSE_ADMIN_EMAIL", "LANGFUSE_ADMIN_NAME"]

errors, warnings = [], []


def fail_now(msg):
    print(f"  FAIL  {msg}")
    sys.exit(1)


def load_yaml(name):
    path = os.path.join(CONFIG, name)
    if not os.path.exists(path):
        fail_now(f"config/{name} is missing")
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        fail_now(f"config/{name}: {e}")


def env_key(team_id, key_name):
    return "KEY_" + re.sub(r"[^A-Z0-9]", "_", f"{team_id}_{key_name}".upper())


# ------------------------------------------------------------------ .env
LINE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<val>.*)$")


def load_env(extra_keys):
    """Read .env (create from example), fill blanks / missing keys, return {key: value}."""
    if not os.path.exists(ENV):
        shutil.copyfile(ENV_EXAMPLE, ENV)
        print("  created .env from .env.example")
    with open(ENV, encoding="utf-8") as f:
        lines = f.read().splitlines()

    values, out, filled = {}, [], []
    for line in lines:
        m = LINE.match(line.strip())
        if not m:
            out.append(line)
            continue
        key, val = m.group("key"), m.group("val").strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if not val and key in AUTO:
            val = AUTO[key]()
            filled.append(key)
            line = f"{key}={val}"
        values[key] = val
        out.append(line)

    for key in AUTO:
        if key not in values:
            values[key] = AUTO[key]()
            filled.append(key)
            out.append(f"{key}={values[key]}")
    new_keys = [k for k in extra_keys if not values.get(k)]
    for key in new_keys:
        values[key] = "sk-" + secrets.token_hex(24)
        out = [l for l in out if not re.match(rf"^{key}=\s*$", l.strip())]
        out.append(f"{key}={values[key]}")
        filled.append(key)

    if filled:
        with open(ENV, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out).rstrip("\n") + "\n")
        print(f"  .env: generated {len(filled)} value(s): {', '.join(filled)}")
    else:
        print("  .env: all values set (nothing generated)")
    return values


def check_env(env):
    for key in REQUIRED:
        if not env.get(key):
            errors.append(f".env: {key} is empty")
    for key, val in env.items():
        if "'" in val or "\n" in val or "$" in val:
            errors.append(f".env: {key} must not contain ' or $")
        if key in URL_SAFE and not re.fullmatch(r"[A-Za-z0-9._~-]+", val):
            errors.append(f".env: {key} may only use letters, digits and . _ ~ - (it goes into a URL)")
    if not env.get("LITELLM_MASTER_KEY", "").startswith("sk-"):
        errors.append(".env: LITELLM_MASTER_KEY must start with sk-")
    if not re.fullmatch(r"[0-9a-f]{64}", env.get("LANGFUSE_ENCRYPTION_KEY", "")):
        errors.append(".env: LANGFUSE_ENCRYPTION_KEY must be exactly 64 hex characters")
    if "@" not in env.get("LANGFUSE_ADMIN_EMAIL", ""):
        errors.append(".env: LANGFUSE_ADMIN_EMAIL must be an e-mail address")
    if len(env.get("LANGFUSE_ADMIN_PASSWORD", "")) < 8:
        errors.append(".env: LANGFUSE_ADMIN_PASSWORD needs at least 8 characters")
    for key, val in env.items():
        if key.startswith("KEY_") and not val.startswith("sk-"):
            errors.append(f".env: team key {key} must start with sk-")


# ------------------------------------------------------------------ config/stack.yaml + ports.yaml
def stack_settings(stack):
    def num(path, lo, hi, default=None):
        cur = stack
        for p in path.split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            cur = default
        if not isinstance(cur, int) or not lo <= cur <= hi:
            errors.append(f"config/stack.yaml: {path} must be a number {lo}..{hi}, got {cur!r}")
            return default or lo
        return cur

    project = str(stack.get("project", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        errors.append("config/stack.yaml: project may only use lowercase letters, digits, - and _")
    pull = str((stack.get("images") or {}).get("pull", "missing"))
    if pull not in ("always", "missing", "never"):
        errors.append(f"config/stack.yaml: images.pull must be always | missing | never, got {pull!r}")
    images = {}
    for k in IMAGE_KEYS:
        v = (stack.get("images") or {}).get(k)
        if not v:
            errors.append(f"config/stack.yaml: images.{k} is missing")
        images[k] = str(v or "")
    cache = stack.get("cache") or {}
    return {
        "project": project,
        "pull": pull,
        "bind": str((stack.get("server") or {}).get("bind", "127.0.0.1")),
        "host": str((stack.get("server") or {}).get("public_host", "localhost")),
        "replicas": num("litellm.replicas", 1, 8, 2),
        "num_retries": num("litellm.num_retries", 0, 10, 2),
        "timeout": num("litellm.timeout_seconds", 1, 3600, 120),
        "allowed_fails": num("litellm.allowed_fails", 1, 100, 3),
        "cooldown": num("litellm.cooldown_seconds", 1, 3600, 30),
        "cache_enabled": bool(cache.get("enabled", True)),
        "cache_ttl": num("cache.ttl_seconds", 1, 30 * 86400, 3600),
        "cache_maxmem": str(cache.get("max_memory", "256mb")),
        "cache_types": list(cache.get("call_types") or ["acompletion", "aembedding", "atext_completion"]),
        "retention": str((stack.get("prometheus") or {}).get("retention", "30d")),
        "images": images,
    }


def port_map(ports_cfg, replicas):
    ports = (ports_cfg or {}).get("ports") or {}
    out = {}
    for k in PORT_KEYS:
        v = ports.get(k)
        if not isinstance(v, int) or not 1 <= v <= 65535:
            errors.append(f"config/ports.yaml: ports.{k} must be a port number, got {v!r}")
            v = 0
        if k in FIXED_PORTS and v != FIXED_PORTS[k]:
            errors.append(f"config/ports.yaml: ports.{k} is fixed by the image at {FIXED_PORTS[k]}")
        out[k] = v
    for k in ports:
        if k not in PORT_KEYS:
            errors.append(f"config/ports.yaml: unknown service {k!r} (known: {', '.join(PORT_KEYS)})")
    last = out["litellm"] + replicas - 1
    others = {k: v for k, v in out.items() if k != "litellm"}
    seen = {}
    for k, v in others.items():
        if v in seen:
            errors.append(f"config/ports.yaml: {k} and {seen[v]} both use port {v}")
        seen[v] = k
        if out["litellm"] <= v <= last:
            errors.append(f"config/ports.yaml: {k} ({v}) overlaps LiteLLM's replica ports {out['litellm']}-{last}")
    return out, last


# ------------------------------------------------------------------ config/models.yaml
def resolve_models(cfg, env):
    providers = cfg.get("providers") or {}
    models = cfg.get("models") or []
    names = [m.get("name") for m in models]
    for n in {n for n in names if names.count(n) > 1}:
        errors.append(f"config/models.yaml: model {n!r} defined twice")
    enabled, disabled = {}, {}
    for m in models:
        name = str(m.get("name", ""))
        if not MODEL_NAME.match(name):
            errors.append(f"config/models.yaml: model name {name!r}: lowercase letters, digits, . _ -")
            continue
        mode = m.get("mode", "chat")
        if mode not in ("chat", "embedding"):
            errors.append(f"config/models.yaml: {name}: mode must be chat | embedding")
        deps = m.get("deployments") or []
        if not deps:
            errors.append(f"config/models.yaml: {name}: needs at least one deployment")
        usable, missing = [], set()
        for d in deps:
            prov = d.get("provider")
            if prov not in providers:
                errors.append(f"config/models.yaml: {name}: unknown provider {prov!r}")
                continue
            if not d.get("model"):
                errors.append(f"config/models.yaml: {name}: a deployment has no `model` route")
                continue
            cred = (providers[prov] or {}).get("credential")
            if cred and not env.get(cred):
                missing.add(cred)
                continue
            usable.append({**d, "_credential": cred})
        for fb in m.get("fallbacks") or []:
            if fb not in names:
                errors.append(f"config/models.yaml: {name}: fallback {fb!r} is not a model")
        entry = {"name": name, "mode": mode, "hidden": bool(m.get("hidden")), "internal": bool(m.get("internal")),
                 "fallbacks": list(m.get("fallbacks") or []), "deployments": usable}
        if usable:
            enabled[name] = entry
        else:
            disabled[name] = sorted(missing)
    for e in enabled.values():
        e["fallbacks"] = [f for f in e["fallbacks"] if f in enabled]
    return enabled, disabled


def model_list(enabled):
    out = []
    for m in enabled.values():
        for d in m["deployments"]:
            params = {k: v for k, v in d.items() if k not in ("provider", "_credential")}
            if d["_credential"]:
                params["api_key"] = f"os.environ/{d['_credential']}"
            out.append({"model_name": m["name"], "litellm_params": params,
                        "model_info": {"mode": m["mode"]}})
    return out


# ------------------------------------------------------------------ config/teams.yaml
def resolve_teams(cfg, enabled, disabled):
    org = cfg.get("org") or {}
    if not NAME.match(str(org.get("id", ""))):
        errors.append("config/teams.yaml: org.id must be lowercase letters, digits, hyphens")
    org = {"id": str(org.get("id", "")), "name": str(org.get("name") or org.get("id", ""))}
    catalog = set(enabled) | set(disabled)
    internal = [n for n, m in enabled.items() if m["internal"]]
    teams, seen = [], set()
    for t in cfg.get("teams") or []:
        tid = str(t.get("id", ""))
        where = f"config/teams.yaml: team {tid!r}"
        if not NAME.match(tid):
            errors.append(f"{where}: id must be lowercase letters, digits, hyphens")
        if tid in seen:
            errors.append(f"{where}: defined twice")
        seen.add(tid)
        listed = list(t.get("models") or [])
        if not listed:
            errors.append(f"{where}: models must list at least one model")
        for m in listed:
            if m not in catalog:
                errors.append(f"{where}: unknown model {m!r} (config/models.yaml)")
            elif m in enabled and (enabled[m]["hidden"] or enabled[m]["internal"]):
                errors.append(f"{where}: {m!r} is hidden/internal - it is granted automatically")
        effective = []
        for m in listed:
            if m in enabled:
                effective += [m] + [f for f in enabled[m]["fallbacks"] if f not in effective]
        dropped = [m for m in listed if m in disabled]
        if dropped:
            warnings.append(f"team {tid}: models without a provider key skipped: {', '.join(dropped)}")
        effective = list(dict.fromkeys(effective + internal))
        if not effective:
            errors.append(f"{where}: no usable model (set a provider key in .env) - LiteLLM would treat [] as ALL models")
        budget = t.get("budget") or {}
        if not isinstance(budget.get("max_usd"), (int, float)):
            errors.append(f"{where}: budget.max_usd must be a number")
        limits = t.get("limits") or {}
        keys, key_names = [], set()
        for k in t.get("keys") or []:
            kn = str(k.get("name", ""))
            if not NAME.match(kn):
                errors.append(f"{where}: key name {kn!r}: lowercase letters, digits, hyphens")
            if kn in key_names:
                errors.append(f"{where}: key {kn!r} defined twice")
            key_names.add(kn)
            for f in ("budget_usd", "rpm", "tpm"):
                if f in k and not isinstance(k[f], (int, float)):
                    errors.append(f"{where}: key {kn}: {f} must be a number")
            keys.append({"alias": f"{tid}-{kn}", "env": env_key(tid, kn), "budget_usd": k.get("budget_usd"),
                         "budget_period": k.get("budget_period", budget.get("period", "1mo")),
                         "rpm": k.get("rpm"), "tpm": k.get("tpm"), "tags": [str(x) for x in k.get("tags") or []]})
        teams.append({
            "id": tid, "name": str(t.get("name") or tid), "models": effective, "dropped_models": dropped,
            "max_budget": budget.get("max_usd"), "budget_period": str(budget.get("period", "1mo")),
            "rpm": limits.get("rpm"), "tpm": limits.get("tpm"),
            "observability": dict(t.get("observability") or {}), "keys": keys,
        })
    return org, teams


# ------------------------------------------------------------------ config/hooks.yaml
def resolve_hooks(cfg):
    out, names = [], set()
    for h in cfg.get("hooks") or []:
        name, module = str(h.get("name", "")), str(h.get("module", ""))
        if not NAME.match(name):
            errors.append(f"config/hooks.yaml: hook name {name!r}: lowercase letters, digits, hyphens")
        if name in names:
            errors.append(f"config/hooks.yaml: hook {name!r} listed twice")
        names.add(name)
        if not h.get("enabled", True):
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", module) or module.startswith("_"):
            errors.append(f"config/hooks.yaml: {name}: module {module!r} must be a python module name")
        elif not os.path.exists(os.path.join(HOOKS_DIR, module + ".py")):
            errors.append(f"config/hooks.yaml: {name}: gateway/hooks/{module}.py does not exist")
        lit = h.get("litellm") or {}
        for k, v in lit.items():
            if k not in ("callbacks", "success_callback", "failure_callback"):
                errors.append(f"config/hooks.yaml: {name}: litellm.{k} not supported (callbacks, success_callback, failure_callback)")
            elif not isinstance(v, list):
                errors.append(f"config/hooks.yaml: {name}: litellm.{k} must be a list")
        out.append({"name": name, "module": module, "enabled": True, "fail_open": bool(h.get("fail_open", True)),
                    "litellm": lit, "settings": dict(h.get("settings") or {})})
    return out


# ------------------------------------------------------------------ LiteLLM config
def litellm_config(s, p, enabled, hooks):
    with open(os.path.join(ROOT, "gateway", "litellm.base.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg = copy.deepcopy(cfg)
    gs = cfg.setdefault("general_settings", {})
    gs["coordination_redis"] = {"host": "redis-state", "port": p["redis_state"],
                                "password": "os.environ/REDIS_STATE_PASSWORD"}
    rs = cfg.setdefault("router_settings", {})
    rs.update({"redis_host": "redis-state", "redis_port": p["redis_state"],
               "redis_password": "os.environ/REDIS_STATE_PASSWORD",
               "num_retries": s["num_retries"], "timeout": s["timeout"],
               "allowed_fails": s["allowed_fails"], "cooldown_time": s["cooldown"]})
    fallbacks = [{m["name"]: m["fallbacks"]} for m in enabled.values() if m["fallbacks"]]
    if fallbacks:
        rs["fallbacks"] = fallbacks
    ls = cfg.setdefault("litellm_settings", {})
    if s["cache_enabled"]:
        ls["cache"] = True
        ls["cache_params"] = {"type": "redis", "host": "redis-cache", "port": p["redis_cache"],
                              "password": "os.environ/REDIS_CACHE_PASSWORD", "ttl": s["cache_ttl"],
                              "namespace": "gw", "supported_call_types": s["cache_types"]}
    for key in ("callbacks", "success_callback", "failure_callback"):
        items = list(ls.get(key) or [])
        for h in hooks:
            for c in h["litellm"].get(key) or []:
                if c not in items:
                    items.append(c)
        if key == "callbacks" and hooks:
            items.append("gateway.entrypoint.pipeline")
        if items:
            ls[key] = items
    cfg["model_list"] = model_list(enabled)
    return {"model_list": cfg.pop("model_list"), **cfg}


# ------------------------------------------------------------------ write
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    stack = load_yaml("stack.yaml")
    s = stack_settings(stack)
    if "--images" in sys.argv:
        print("\n".join(v for v in s["images"].values() if v))
        return
    p, litellm_last = port_map(load_yaml("ports.yaml"), s["replicas"])
    teams_cfg = load_yaml("teams.yaml")
    key_envs = [env_key(t.get("id", ""), k.get("name", "")) for t in teams_cfg.get("teams") or [] for k in t.get("keys") or []]
    env = load_env(key_envs)
    check_env(env)
    enabled, disabled = resolve_models(load_yaml("models.yaml"), env)
    org, teams = resolve_teams(teams_cfg, enabled, disabled)
    hooks = resolve_hooks(load_yaml("hooks.yaml"))
    if errors:
        print("  configuration problems - nothing started:")
        for e in errors:
            print(f"  FAIL  {e}")
        sys.exit(1)

    host = s["host"]
    has_bundle = os.path.exists(CERT_BUNDLE) or (os.path.exists(CORP_ROOT) and os.path.getsize(CORP_ROOT) > 0)
    settings = {
        "COMPOSE_PROJECT_NAME": s["project"],
        "PULL_POLICY": "never" if s["pull"] == "never" else "missing",
        "BIND_ADDRESS": s["bind"],
        "PUBLIC_HOST": host,
        "LITELLM_REPLICAS": s["replicas"],
        "PORT_LITELLM_LAST": litellm_last,
        **{f"PORT_{k.upper()}": v for k, v in p.items()},
        "LITELLM_PUBLIC_URL": f"http://{host}:{p['litellm']}",
        "LANGFUSE_PUBLIC_URL": f"http://{host}:{p['langfuse']}",
        "GRAFANA_PUBLIC_URL": f"http://{host}:{p['grafana']}",
        "PROMETHEUS_PUBLIC_URL": f"http://{host}:{p['prometheus']}",
        "PROMETHEUS_RETENTION": s["retention"],
        "REDIS_CACHE_MAXMEMORY": s["cache_maxmem"],
        "LANGFUSE_ORG_ID": org["id"],
        "LANGFUSE_ORG_NAME": org["name"],
        "LANGFUSE_DEFAULT_PROJECT_ID": DEFAULT_PROJECT_ID,
        "LITELLM_CA_FILE": "/certs/ca-bundle.pem" if has_bundle else "/etc/ssl/certs/ca-certificates.crt",
        **{f"IMG_{k.upper()}": v for k, v in s["images"].items()},
    }
    if any("'" in str(v) for v in settings.values()):
        fail_now("config values must not contain a single quote")

    lines = ["# GENERATED by tools/render.py from config/*.yaml + .env - do not edit. Contains secrets.", ""]
    lines += [f"{k}='{v}'" for k, v in settings.items()] + ["", "# --- .env"]
    lines += [f"{k}='{v}'" for k, v in env.items()]
    write(os.path.join(BUILD, "compose.env"), "\n".join(lines) + "\n")

    creds = sorted({d["_credential"] for m in enabled.values() for d in m["deployments"] if d["_credential"]})
    write(os.path.join(BUILD, "litellm.env"),
          "# GENERATED - provider credentials for the LiteLLM containers only.\n"
          + "".join(f"{c}={env[c]}\n" for c in creds))

    header = "# GENERATED by tools/render.py - edit gateway/litellm.base.yaml and config/*.yaml instead.\n"
    write(os.path.join(BUILD, "litellm.yaml"),
          header + yaml.safe_dump(litellm_config(s, p, enabled, hooks), sort_keys=False, width=120))
    write(os.path.join(BUILD, "hooks.json"), json.dumps({"hooks": hooks}, indent=2) + "\n")
    write(os.path.join(BUILD, "models.json"), json.dumps({
        "enabled": [{k: m[k] for k in ("name", "mode", "hidden", "internal", "fallbacks")} for m in enabled.values()],
        "disabled": disabled}, indent=2) + "\n")
    write(os.path.join(BUILD, "teams.json"), json.dumps({
        "org": org, "default_langfuse_project": DEFAULT_PROJECT_ID, "teams": teams}, indent=2) + "\n")

    with open(os.path.join(ROOT, "services", "prometheus", "prometheus.yml"), encoding="utf-8") as f:
        prom = f.read()
    for k, v in p.items():
        prom = prom.replace("{{PORT_%s}}" % k.upper(), str(v))
    write(os.path.join(BUILD, "prometheus.yml"), prom)

    src = os.path.join(ROOT, "services", "grafana", "dashboards")
    for name in sorted(os.listdir(src)):
        if name.endswith(".json"):
            with open(os.path.join(src, name), encoding="utf-8") as f:
                text = f.read().replace("http://localhost:3000", settings["LANGFUSE_PUBLIC_URL"])
            write(os.path.join(BUILD, "grafana", "dashboards", name), text)

    # ---- summary
    print(f"  project {s['project']}: {s['replicas']} LiteLLM replica(s) on {p['litellm']}-{litellm_last}, "
          f"cache {'on (ttl ' + str(s['cache_ttl']) + 's)' if s['cache_enabled'] else 'off'}, images.pull {s['pull']}")
    print(f"  models  enabled: {', '.join(enabled) or '-'}")
    for name, missing in disabled.items():
        print(f"  models  skipped: {name} (set {', '.join(missing)} in .env)")
    print(f"  teams   {', '.join(t['id'] for t in teams)} in org {org['id']}, "
          f"{sum(len(t['keys']) for t in teams)} keys")
    print(f"  hooks   {', '.join(h['name'] for h in hooks) or '(none)'}")
    for w in warnings:
        print(f"  note    {w}")
    if not has_bundle:
        print("  note    no certs/corp-root-ca.pem - fine unless HTTPS is intercepted (certs/README.md)")


if __name__ == "__main__":
    main()

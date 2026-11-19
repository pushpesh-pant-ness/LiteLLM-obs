"""End-to-end checks for the LiteLLM observability stack (stdlib only).

Run via scripts/verify.sh, or directly:
    docker compose run --rm checks            # free: mock model only
    docker compose run --rm checks --real     # + one short real call per team (fractions of a cent)

Every check prints PASS / FAIL / SKIP. Exit code 1 if anything failed.
Each section guards against a problem we actually hit - see the comment above it.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

LITELLM = os.environ.get("LITELLM_URL", "http://litellm:4000")
GRAFANA = os.environ.get("GRAFANA_URL", "http://grafana:3000")
LANGFUSE = os.environ.get("LANGFUSE_URL", "http://langfuse-web:3000")
PROM = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ALERTMGR = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093")
MASTER = os.environ["LITELLM_MASTER_KEY"]
GRAFANA_AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
LANGFUSE_AUTH = "Basic " + base64.b64encode(
    f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
TENANTS = os.environ.get("TENANTS_FILE", "/bootstrap/tenants.json")
KEYS = os.environ.get("KEYS_FILE", "/bootstrap/generated-keys.json")
SCRAPER_TOKEN = os.environ.get("SCRAPER_TOKEN_FILE", "/prom-secrets/litellm_metrics_token")
MOCK = "mock-gpt-4o-mini"
REAL = "--real" in sys.argv
# attach mode (setup.yml): LiteLLM is the production one. Nothing is sent through it and nothing is
# created in it: traffic, rate-limit, fallback and provider checks are skipped; traces are checked
# passively (sensitive teams: the latest stored traces must be redacted).
ATTACH = os.environ.get("OBS_MODE") == "attach"
EXPECT_REPLICA = os.environ.get("EXPECT_REPLICA", "true").lower() == "true"
LF_CALLBACK_HOST = os.environ.get("LANGFUSE_CALLBACK_HOST", "http://langfuse-web:3000")
RUN = f"verify-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
REDACTED = "redacted-by-litellm"

results = []


def record(section, name, ok, detail=""):
    state = "PASS" if ok is True else "SKIP" if ok is None else "FAIL"
    results.append((state, section, name, detail))
    print(f"  {state:<4}  {name}" + (f"  - {detail}" if detail else ""), flush=True)


def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(errors="replace"), e.code
    except Exception as e:  # connection refused, timeout ...
        return 0, str(e)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def litellm(method, path, body=None, key=MASTER, timeout=120):
    return http(method, LITELLM + path, body, {"Authorization": f"Bearer {key}"}, timeout)


def poll(fn, timeout, every=3):
    """Call fn() until it returns something truthy or timeout (seconds)."""
    end = time.time() + timeout
    while True:
        out = fn()
        if out or time.time() > end:
            return out
        time.sleep(every)


def section(title):
    print(f"\n== {title}", flush=True)


# ---------------------------------------------------------------- load tenants + keys
with open(TENANTS, encoding="utf-8") as f:
    tenants = json.load(f)
team_cfg = {t["alias"]: {**t, "_org": o["alias"]} for o in tenants["orgs"] for t in o.get("teams", [])}
key_team = {k["alias"]: t["alias"] for t in team_cfg.values() for k in t.get("keys", [])}
try:
    with open(KEYS, encoding="utf-8") as f:
        keys = {k["key_alias"]: k["key"] for k in json.load(f)}
except FileNotFoundError:
    keys = {}

# ---------------------------------------------------------------- 1. services
section("Services")
for name, url, auth in [
    ("LiteLLM", LITELLM + "/health/liveliness", None),
    ("Grafana", GRAFANA + "/api/health", None),
    ("Langfuse", LANGFUSE + "/api/public/health", None),
    ("Prometheus", PROM + "/-/ready", None),
    ("Alertmanager", ALERTMGR + "/-/ready", None),
]:
    s, b = http("GET", url)
    record("services", f"{name} is up", s == 200, "" if s == 200 else f"HTTP {s}: {str(b)[:100]}")

# ---------------------------------------------------------------- 2. LiteLLM config
# Guards: team model lists pointing at renamed models (all requests failed); sensitive team without
# redaction; metrics key able to call models.
section("LiteLLM configuration")
s, info = litellm("GET", "/model/info")
models = {m["model_name"]: m for m in (info.get("data", []) if isinstance(info, dict) else [])}
route_to_name = {m["litellm_params"].get("model"): n for n, m in models.items()}
for alias, t in team_cfg.items():
    if "models" not in t:  # observe mode: the team's models are managed in LiteLLM
        continue
    missing = [m for m in t.get("models", []) if m not in models]
    record("config", f"{alias}: every model in tenants.json exists in LiteLLM", not missing,
           f"missing {missing}" if missing else "")

s, teams = litellm("GET", "/team/list")
teams = teams if isinstance(teams, list) else []
team_ids = {t["team_alias"]: t["team_id"] for t in teams}
for alias, t in team_cfg.items():
    live = next((x for x in teams if x.get("team_alias") == alias), None)
    if not live:
        record("config", f"{alias}: exists in LiteLLM", False, "run bootstrap")
        continue
    if "models" in t:  # observe mode: models are managed in LiteLLM, not in the tenants file
        drift = sorted(set(live.get("models") or []) ^ set(t.get("models", [])))
        record("config", f"{alias}: LiteLLM model list matches tenants.json", not drift, f"differs: {drift}" if drift else "")
    s, cb = litellm("GET", f"/team/{live['team_id']}/callback")
    d = cb.get("data", {}) if isinstance(cb, dict) else {}
    has_lf = "langfuse" in (d.get("success_callbacks") or []) and "langfuse" in (d.get("failure_callbacks") or [])
    redact = str((d.get("callback_vars") or {}).get("turn_off_message_logging", "")).lower() == "true"
    host = (d.get("callback_vars") or {}).get("langfuse_host")
    host_ok = host == LF_CALLBACK_HOST  # traces go to THIS stack's Langfuse, not somewhere else
    want = bool(t.get("sensitive"))
    record("config", f"{alias}: Langfuse callback to this stack, redaction={'on' if want else 'off'}",
           has_lf and host_ok and redact == want,
           f"callbacks ok={has_lf}, redaction={redact}" + ("" if host_ok else f", langfuse_host={host} (expected {LF_CALLBACK_HOST})"))
    if "max_budget" in t:
        b_ok = live.get("max_budget") == t.get("max_budget") and live.get("budget_duration") == t.get("budget_duration")
        record("config", f"{alias}: budget ${t.get('max_budget')}/{t.get('budget_duration')}", b_ok,
               f"live ${live.get('max_budget')}/{live.get('budget_duration')}, resets {live.get('budget_reset_at')}")

# Guards: a dev key without limits draining the team budget.
s, kl = litellm("GET", "/key/list?return_full_object=true&size=100")
live_keys = {k.get("key_alias"): k for k in (kl.get("keys", []) if isinstance(kl, dict) else []) if isinstance(k, dict)}
for t in team_cfg.values():
    for k in t.get("keys", []):
        lk = live_keys.get(k["alias"], {})
        want = {f: k[f] for f in ("max_budget", "budget_duration", "rpm_limit", "tpm_limit") if f in k}
        got = {f: lk.get(f) for f in want}
        record("config", f"{k['alias']}: key limits applied", got == want, "" if got == want else f"want {want}, live {got}")

# Guards: a dead instance behind the service name; instances not sharing state.
section("Gateway instances (HA)")
if ATTACH:
    instances = []
    record("ha", "instances, shared rate limit, fallback", None, "attach mode: production LiteLLM is not tested with traffic")
else:
    import socket
    host = LITELLM.split("//", 1)[1].split(":")[0]
    port = int(LITELLM.rsplit(":", 1)[1]) if LITELLM.count(":") > 1 else 4000
    ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
    instances = [f"http://{ip}:{port}" for ip in ips]
    healthy = [u for u in instances if http("GET", u + "/health/liveliness")[0] == 200]
    record("ha", f"{len(instances)} LiteLLM instances, all healthy", len(instances) >= 2 and healthy == instances,
           f"healthy {len(healthy)}/{len(instances)}")

    # Guards: rate limits not enforced - and, with several instances, not SHARED through Redis.
    # The calls deliberately alternate between instances: 2/min only holds if they count together.
    s, tmp = litellm("POST", "/key/generate", {"key_alias": f"zz-{RUN}-ratelimit", "rpm_limit": 2, "models": [MOCK],
                                               "metadata": {"tags": ["proj:verify", "cc:infra", "env:test"]}})
    if s == 200 and isinstance(tmp, dict) and tmp.get("key"):
        codes = []
        for i in range(4):
            base = instances[i % len(instances)] if instances else LITELLM
            codes.append(http("POST", base + "/chat/completions", {"model": MOCK, "messages": [{"role": "user", "content": "rate"}]},
                              {"Authorization": f"Bearer {tmp['key']}"})[0])
        record("ha", f"rpm limit shared across {len(instances)} instances (2/min key: 3rd+ call rejected)",
               codes[:2] == [200, 200] and 429 in codes[2:], f"status codes {codes} (alternating instances)")
        litellm("POST", "/key/delete", {"keys": [tmp["key"]]})
    else:
        record("ha", "rpm limit enforced", False, f"could not create test key: HTTP {s}")

    # Guards: fallbacks / retries not configured. zz-verify-failing always fails; it must fall back to the mock.
    s, b = litellm("POST", "/chat/completions", {"model": "zz-verify-failing", "messages": [{"role": "user", "content": "fallback"}]})
    fell_back = s == 200 and isinstance(b, dict) and "mock response" in json.dumps(b.get("choices", "")).lower()
    record("ha", "failing model falls back to the next model", fell_back, f"HTTP {s}" + ("" if fell_back else f": {str(b)[:120]}"))
section("LiteLLM configuration (continued)")

# Guards: instances not sharing Redis (limits multiply by instance count), or Redis only via the
# REDIS_* env fallback (the UI's Redis settings look empty and it is easy to "fix" it wrongly).
s, cr = litellm("GET", "/coordination_redis/settings")
src = cr.get("source") if isinstance(cr, dict) else None
if s != 200:
    record("config", "coordination Redis configured", None, f"HTTP {s} - endpoint not in this LiteLLM version")
elif ATTACH:
    record("config", "production LiteLLM has a coordination Redis", bool(src), f"source: {src}")
else:
    record("config", "coordination Redis set in config.yaml (general_settings.coordination_redis)",
           src == "coordination_redis", f"source: {src}")

try:
    scraper = open(SCRAPER_TOKEN, encoding="utf-8").read().strip()
except FileNotFoundError:
    scraper = ""
if scraper:
    s1, _ = litellm("GET", "/metrics/", key=scraper)
    s2, _ = litellm("POST", "/chat/completions", {"model": MOCK, "messages": [{"role": "user", "content": "x"}]}, key=scraper)
    record("config", "metrics key can read /metrics", s1 == 200, f"HTTP {s1}")
    record("config", "metrics key cannot call models", s2 in (401, 403), f"HTTP {s2}")
else:
    record("config", "metrics key", None, "no token file")

# ---------------------------------------------------------------- 3. Prometheus
# Guards: scrape broken (401), Live spend undercount (created-timestamp flag), broken rule files.
section("Prometheus")
s, tg = http("GET", PROM + "/api/v1/targets")
lt = [t for t in (tg.get("data", {}).get("activeTargets", []) if isinstance(tg, dict) else []) if t["labels"].get("job") == "litellm"]
want_n = len(instances) if not ATTACH else len(lt)
record("prometheus", f"every LiteLLM instance scraped and up ({len([t for t in lt if t['health'] == 'up'])}/{want_n})",
       bool(lt) and len(lt) == want_n and all(t["health"] == "up" for t in lt),
       "; ".join(t.get("lastError", "") for t in lt if t["health"] != "up"))
s, fl = http("GET", PROM + "/api/v1/status/flags")
feat = fl.get("data", {}).get("enable-feature", "") if isinstance(fl, dict) else ""
record("prometheus", "created-timestamp-zero-ingestion enabled", "created-timestamp-zero-ingestion" in feat, feat or "flag missing")
s, rl = http("GET", PROM + "/api/v1/rules")
rules = [r for g in (rl.get("data", {}).get("groups", []) if isinstance(rl, dict) else []) for r in g["rules"]]
bad = [r["name"] for r in rules if r.get("health") not in ("ok", "unknown")]
record("prometheus", f"alert rules loaded and healthy ({len(rules)})", len(rules) >= 7 and not bad, f"bad: {bad}" if bad else "")

# ---------------------------------------------------------------- 4. provider TLS
# Guards: Netskope / corporate CA breaking provider HTTPS ("CA cert does not include key usage").
section("Provider TLS")
if ATTACH:
    record("tls", "provider TLS", None, "attach mode: provider connectivity is production LiteLLM's own")
else:
    providers = {}
    for n, m in models.items():
        p = m["litellm_params"].get("custom_llm_provider") or m["litellm_params"].get("model", "").split("/")[0]
        if n != MOCK and not n.startswith("zz-") and not m["litellm_params"].get("mock_response") and p and p not in providers:
            providers[p] = m["litellm_params"]["model"]
    if not providers:
        record("tls", "provider TLS", None, "no real models configured")
    for p, route in providers.items():
        s, b = litellm("POST", "/health/test_connection",
                       {"mode": "chat", "litellm_params": {"model": route, "api_key": "verify-dummy-key"}})
        txt = json.dumps(b).lower()
        tls_bad = any(w in txt for w in ("certificate_verify_failed", "key usage extension", "ssl:"))
        reached = any(w in txt for w in ("invalid api key", "invalid_api_key", "401", "unauthorized", "authentication"))
        record("tls", f"{p}: HTTPS to provider works", reached and not tls_bad,
               "reached provider (auth error with dummy key, as expected)" if reached and not tls_bad else txt[:160])

# ---------------------------------------------------------------- 5. traffic through team keys
# Guards: keys/teams broken, model access not enforced.
section("Traffic" + (" (real models)" if REAL else " (mock model)"))
if ATTACH:
    calls = []
    record("traffic", "test calls through team keys", None, "attach mode: no test traffic is sent to production")
else:
    calls = []  # (key_alias, team, model, request_id, marker, session)
    if not keys:
        record("traffic", "generated keys", False, "bootstrap/generated-keys.json missing - run bootstrap")
    for alias, key in keys.items():
        team = key_team.get(alias)
        if not team:
            continue
        t = team_cfg[team]
        real_models = [m for m in t.get("models", []) if m != MOCK]
        if REAL and real_models:
            # Sensitive teams: test EVERY model - reasoning models (gpt-oss) leak differently than others.
            to_test = real_models if t.get("sensitive") else real_models[:1]
        else:
            to_test = [MOCK]
        for model in to_test:
            marker = f"VERIFYMARK{uuid.uuid4().hex[:10]}"
            session = f"{RUN}-{alias}-{model.replace('/', '_')}"
            body = {"model": model, "max_tokens": 200, "metadata": {"session_id": session, "generation_name": "verify"},
                    "messages": [{"role": "user", "content": f"{marker}: reply with the single word ok."}]}
            s, b = litellm("POST", "/chat/completions", body, key=key)
            ok = s == 200 and isinstance(b, dict) and b.get("choices")
            record("traffic", f"{alias} -> {model}", bool(ok), "" if ok else f"HTTP {s}: {str(b)[:140]}")
            if ok:
                calls.append((alias, team, model, b.get("id"), marker, session))
        others = [m for m in models if m not in t.get("models", [])]
        forbidden = others[0] if others else "model-this-team-cannot-use"
        s, _ = litellm("POST", "/chat/completions", {"model": forbidden, "messages": [{"role": "user", "content": "x"}]}, key=key)
        record("traffic", f"{alias} blocked from {forbidden}", s in (401, 403), f"HTTP {s}")

# ---------------------------------------------------------------- 6. spend recorded
# Guards: spend not written, wrong team, $0 for a priced model (missing price).
section("Spend in Postgres")
for alias, team, model, rid, _, _ in calls:
    def find():
        s, b = litellm("GET", f"/spend/logs?request_id={rid}")
        return b[0] if isinstance(b, list) and b else None
    row = poll(find, 150, 5)
    if not row:
        record("spend", f"{alias} {model}: request logged", False, "not in spend logs after 150s")
        continue
    team_ok = row.get("team_id") == team_ids.get(team)
    record("spend", f"{alias} {model}: logged with team {team}, cost > 0", team_ok and float(row.get("spend") or 0) > 0,
           f"spend={row.get('spend')} team_ok={team_ok}")

# ---------------------------------------------------------------- 7. Langfuse traces + redaction
# Guards: traces not arriving; sensitive prompt / answer / REASONING leaking (the gpt-oss leak).
section("Langfuse traces and redaction")


try:
    with open("/bootstrap/langfuse-projects.json", encoding="utf-8") as f:
        team_projects = json.load(f)
except FileNotFoundError:
    team_projects = {}


def project_auth(team):
    p = team_projects.get(team)
    if not p:
        return LANGFUSE_AUTH
    return "Basic " + base64.b64encode(f"{p['public_key']}:{p['secret_key']}".encode()).decode()


def trace_text(session, auth):
    s, b = http("GET", f"{LANGFUSE}/api/public/traces?sessionId={session}", headers={"Authorization": auth})
    traces = b.get("data", []) if isinstance(b, dict) else []
    if not traces:
        return None
    parts = [json.dumps(traces)]
    for t in traces:
        s, o = http("GET", f"{LANGFUSE}/api/public/observations?traceId={t['id']}", headers={"Authorization": auth})
        obs = o.get("data", []) if isinstance(o, dict) else []
        if not obs:
            return None  # observations not ingested yet
        parts.append(json.dumps(obs))
    return "\n".join(parts)


for alias, team, model, _, marker, session in calls:
    text = poll(lambda: trace_text(session, project_auth(team)), 120, 5)
    where = "team project" if team in team_projects else "admin project"
    if text is None:
        record("langfuse", f"{alias} {model}: trace arrived in {where}", False, f"no trace for session {session} after 120s")
        continue
    if team in team_projects:
        # Guards: traces leaking into the shared admin project instead of the team's own.
        stray = trace_text(session, LANGFUSE_AUTH)
        record("langfuse", f"{alias} {model}: trace in its team project only", stray is None,
               "also found in the admin project" if stray else "")
    sensitive = bool(team_cfg[team].get("sensitive"))
    if sensitive:
        leaks = text.count(marker)
        record("langfuse", f"{alias} {model} (sensitive): prompt never stored", leaks == 0 and REDACTED in text,
               f"marker found {leaks}x" if leaks else "input, output and reasoning redacted")
        if REAL and '"reasoning"' in text:
            ok = any(f'"reasoning"{sep}"{REDACTED}' in text for sep in (": ", ":"))
            record("langfuse", f"{alias} {model}: reasoning field redacted", ok, "" if ok else "reasoning stored unredacted")
    else:
        record("langfuse", f"{alias} {model}: trace arrived with the prompt", marker in text,
               "" if marker in text else "marker missing from trace")

# Attach mode: no test calls, so check what production traffic already stored.
def unredacted(items):
    out = []
    for x in items:
        for fld in ("input", "output"):
            v = x.get(fld)
            if v not in (None, "", {}, []) and REDACTED not in json.dumps(v):
                out.append(f"{x['id'][:8]}.{fld}")
        txt = json.dumps(x)
        if '"reasoning"' in txt and not any(f'"reasoning"{sep}"{REDACTED}' in txt for sep in (": ", ":")):
            out.append(f"{x['id'][:8]}.reasoning")
    return out


if ATTACH:
    for team in team_cfg:
        if team not in team_projects:
            record("langfuse", f"{team}: has its own Langfuse project", False, "run scripts/apply-tenants.sh")
            continue
        auth = project_auth(team)
        s1, tr = http("GET", f"{LANGFUSE}/api/public/traces?limit=25", headers={"Authorization": auth})
        s2, ob = http("GET", f"{LANGFUSE}/api/public/observations?limit=25", headers={"Authorization": auth})
        traces = tr.get("data", []) if isinstance(tr, dict) else []
        obs = ob.get("data", []) if isinstance(ob, dict) else []
        if s1 != 200 or s2 != 200:
            record("langfuse", f"{team}: project readable with its key", False, f"HTTP {s1}/{s2}")
            continue
        if not team_cfg[team].get("sensitive"):
            record("langfuse", f"{team}: project readable with its key", True, f"{len(traces)} recent traces")
            continue
        if not traces and not obs:
            record("langfuse", f"{team} (sensitive): stored traces are redacted", None, "no traces yet - send a request with a team key")
            continue
        leaks = unredacted(traces) + unredacted(obs)
        record("langfuse", f"{team} (sensitive): latest {len(traces)} traces / {len(obs)} observations redacted",
               not leaks, f"unredacted: {leaks[:4]}" if leaks else "input, output and reasoning redacted")

# ---------------------------------------------------------------- 8. Grafana
# Guards: broken panel queries; blank team/model rows (admin calls); stale model names after a rename;
# grafana_ro reading secrets or prompt text.
section("Grafana dashboards")


def grafana_query(panel, target, frm, to="now"):
    q = dict(target)
    q["datasource"] = panel.get("datasource")
    if "expr" in q:
        q["expr"] = q["expr"].replace("$team", ".*")
    q.setdefault("intervalMs", 60000)
    q.setdefault("maxDataPoints", 500)
    s, b = http("POST", GRAFANA + "/api/ds/query", {"queries": [q], "from": frm, "to": to}, {"Authorization": GRAFANA_AUTH})
    if s != 200 or not isinstance(b, dict):
        return None, f"HTTP {s}: {str(b)[:160]}"
    r = b["results"][target["refId"]]
    return r, r.get("error")


for uid, frm in (("litellm-live", "now-6h"), ("litellm-cost", "now-30d")):
    s, d = http("GET", f"{GRAFANA}/api/dashboards/uid/{uid}", headers={"Authorization": GRAFANA_AUTH})
    if s != 200:
        record("grafana", f"dashboard {uid} exists", False, f"HTTP {s}")
        continue
    errors, problems, n = [], [], 0
    for p in d["dashboard"]["panels"]:
        for t in p.get("targets", []):
            n += 1
            r, err = grafana_query(p, t, frm)
            if err:
                errors.append(f"{p['title']}: {str(err)[:100]}")
                continue
            for fr in r.get("frames", []):
                fields = fr["schema"]["fields"]
                vals = fr["data"]["values"]
                names = [f.get("name") for f in fields]
                # time series: series label must not be blank / "None"
                for f in fields[1:]:
                    for k, v in (f.get("labels") or {}).items():
                        if k in ("team_alias", "requested_model", "model_group") and v in ("", "None"):
                            problems.append(f"{p['title']}: series with {k}='{v}'")
                # tables: blank Team / Model / metric, or a provider route shown instead of its public name
                for col in ("Team", "Model", "metric"):
                    if col in names and vals:
                        for v in vals[names.index(col)]:
                            if v in ("", None):
                                problems.append(f"{p['title']}: blank {col}")
                            elif col == "Model" and v in route_to_name and route_to_name[v] != v:
                                problems.append(f"{p['title']}: shows route '{v}' instead of '{route_to_name[v]}'")
    record("grafana", f"{uid}: all {n} queries run", not errors, "; ".join(errors[:3]))
    record("grafana", f"{uid}: no blank teams/models or stale model names", not problems, "; ".join(sorted(set(problems))[:3]))

for sql, should_work, label in [
    ('SELECT spend FROM requests LIMIT 1', True, "grafana_ro can read the reporting views"),
    ('SELECT spend FROM public."LiteLLM_SpendLogs" LIMIT 1', False, "grafana_ro cannot read LiteLLM tables directly"),
    ('SELECT 1 FROM "LiteLLM_VerificationToken" LIMIT 1', False, "grafana_ro cannot read keys"),
    ('SELECT messages FROM "LiteLLM_SpendLogs" LIMIT 1', False, "grafana_ro cannot read prompt text"),
    ('SELECT 1 FROM "LiteLLM_UserTable" LIMIT 1', False, "grafana_ro cannot read users"),
]:
    r, err = grafana_query({"datasource": {"type": "grafana-postgresql-datasource", "uid": "litellm-pg"}},
                           {"refId": "A", "rawSql": sql, "format": "table"}, "now-1h")
    worked = r is not None and not err
    record("grafana", label, worked == should_work, (err or "")[:100] if worked != should_work else "")

# Guards: Grafana silently reading the primary (heavy dashboards slowing the gateway), replica lagging.
r, err = grafana_query({"datasource": {"type": "grafana-postgresql-datasource", "uid": "litellm-pg"}},
                       {"refId": "A", "format": "table", "rawSql":
                        "SELECT pg_is_in_recovery()::int AS standby, "
                        "COALESCE(EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp()), 0)::int AS lag_s"}, "now-1h")
try:
    standby, lag = (r["frames"][0]["data"]["values"][0][0], r["frames"][0]["data"]["values"][1][0]) if not err else (None, None)
except (KeyError, IndexError, TypeError):
    standby, lag = None, None
if EXPECT_REPLICA:
    record("grafana", "Grafana reads the streaming replica, not the primary", standby == 1, err or f"standby={standby}")
    record("grafana", "replica is current (last replay < 10 min ago)", lag is not None and lag < 600, f"last replay {lag}s ago")
else:
    record("grafana", "Grafana reads a replica", None, f"database.read_host not set - Grafana reads the primary (standby={standby})")

# ---------------------------------------------------------------- 9. tenant isolation
# Guards: a team (or org) seeing another team's data in Grafana or Langfuse.
section("Tenant isolation (admin sees all, org sees its teams, team sees itself)")
try:
    with open("/bootstrap/test-users.json", encoding="utf-8") as f:
        test_users = json.load(f)
    with open("/bootstrap/scope-credentials.json", encoding="utf-8") as f:
        scopes = json.load(f)
except FileNotFoundError:
    test_users, scopes = {}, {}
id_to_team = {v: k for k, v in team_ids.items()}
if not test_users:
    record("isolation", "test users", None, "none - run scripts/apply-tenants.sh with CREATE_TEST_USERS=true")


def as_user(method, path, login, body=None, org=None):
    h = {"Authorization": "Basic " + base64.b64encode(f"{login}:{test_users[login]['password']}".encode()).decode()}
    if org:
        h["X-Grafana-Org-Id"] = str(org)
    return http(method, GRAFANA + path, body, h)


team_sql = {"queries": [{"refId": "A", "datasource": {"uid": "litellm-pg"}, "format": "table",
                         "rawSql": "SELECT string_agg(DISTINCT team, ',' ORDER BY team) FROM daily_team_spend"}],
            "from": "now-30d", "to": "now"}
for sc in scopes.values():
    login = f"viewer-{sc['alias']}"
    if login not in test_users:
        continue
    want = sorted(id_to_team.get(i, i) for i in sc["team_ids"])
    s, orgs = as_user("GET", "/api/user/orgs", login)
    names = sorted(o["name"] for o in orgs) if isinstance(orgs, list) else orgs
    record("isolation", f"Grafana {login}: member of '{sc['alias']}' only", names == [sc["alias"]], f"orgs {names}")
    s, r = as_user("POST", "/api/ds/query", login, team_sql)
    try:
        got = sorted((r["results"]["A"]["frames"][0]["data"]["values"][0][0] or "").split(","))
    except (KeyError, IndexError, TypeError):
        got = f"HTTP {s}"
    record("isolation", f"Grafana {login}: data = {','.join(want)}", got == want, f"sees {got}")
    s, _ = as_user("POST", "/api/ds/query", login, team_sql, org=1)
    record("isolation", f"Grafana {login}: cannot query the admin org", s in (401, 403), f"HTTP {s}")

sys.path.insert(0, "/bootstrap")
try:
    from langfuse_admin import LangfuseAdmin
except ImportError:
    LangfuseAdmin = None
org_of_team = {t: c["_org"] for t, c in team_cfg.items()}
for sc in scopes.values():
    login = f"viewer-{sc['alias']}"
    if login not in test_users or not LangfuseAdmin:
        continue
    want = sorted(t for t in team_cfg if (t == sc["alias"] if sc["kind"] == "team" else org_of_team[t] == sc["alias"]))
    try:
        orgs = LangfuseAdmin(LANGFUSE, f"{login}@example.com", test_users[login]["password"]).orgs()
        got = sorted(o["name"] for o in orgs)
        roles = {o.get("role") for o in orgs}
    except RuntimeError as e:
        got, roles = str(e)[:80], set()
    record("isolation", f"Langfuse {login}: sees {','.join(want)} only, as viewer", got == want and roles <= {"VIEWER"},
           f"sees {got}, roles {sorted(r for r in roles if r)}")

# Named members from tenants.json (e.g. U1/U2/U3): Grafana data + Langfuse trace access, own vs others.
import urllib.parse
members = {}
for o in tenants["orgs"]:
    for alias, holder in [(o["alias"], o)] + [(t["alias"], t) for t in o.get("teams", [])]:
        for m in holder.get("members", []):
            members.setdefault(m["login"], {"name": m.get("name", m["login"]), "scopes": []})["scopes"].append(alias)
for login, m in members.items():
    if login not in test_users:
        record("isolation", f"member {m['name']}", None, "not created (CREATE_TEST_USERS=false?)")
        continue
    want = sorted({t for t, c in team_cfg.items() if t in m["scopes"] or c["_org"] in m["scopes"]})
    s, r = as_user("POST", "/api/ds/query", login, team_sql)
    try:
        got = sorted((r["results"]["A"]["frames"][0]["data"]["values"][0][0] or "").split(","))
    except (KeyError, IndexError, TypeError):
        got = f"HTTP {s}"
    record("isolation", f"member {m['name']} ({login}): Grafana data = {','.join(want)}", got == want, f"sees {got}")
    if not LangfuseAdmin:
        continue
    try:
        sess = LangfuseAdmin(LANGFUSE, test_users[login].get("email") or f"{login}@example.com", test_users[login]["password"])
    except RuntimeError as e:
        record("isolation", f"member {m['name']}: Langfuse login", False, str(e)[:80])
        continue
    allowed, denied_ok = [], []
    for t, p in list(team_projects.items()) + [("admin", {"project_id": "litellm-proxy"})]:
        q = urllib.parse.quote(json.dumps({"json": {"projectId": p["project_id"], "filter": [], "searchQuery": None,
                                                    "searchType": ["id"], "orderBy": None, "page": 0, "limit": 1}}))
        st, _ = sess._req("GET", f"/api/trpc/traces.countAll?input={q}")
        (allowed if st == 200 else denied_ok).append(t)
    record("isolation", f"member {m['name']} ({login}): Langfuse traces readable only in {','.join(want)}",
           sorted(allowed) == want, f"readable {sorted(allowed)}, denied {sorted(denied_ok)}")

# Guards: team alerts not reaching the team (or the platform channel missing them).
section("Alert routing")
probe = f"verify-{uuid.uuid4().hex[:6]}"
for team in team_cfg:
    alert = [{"labels": {"alertname": "VerifyRouting", "severity": "none", "team_alias": team, "probe": probe},
              "annotations": {"summary": "verify.py routing probe - ignore"},
              "endsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 120))}]
    http("POST", ALERTMGR + "/api/v2/alerts", alert)


def receivers_for(team):
    s, groups = http("GET", ALERTMGR + "/api/v2/alerts/groups?filter=alertname%3D%22VerifyRouting%22&filter=probe%3D%22" + probe + "%22")
    out = set()
    for g in groups if isinstance(groups, list) else []:
        for a in g.get("alerts", []):
            if a["labels"].get("team_alias") == team:
                out.add(g["receiver"]["name"])
    return out


for team in team_cfg:
    got = poll(lambda: receivers_for(team) if len(receivers_for(team)) >= 2 else None, 30, 2) or receivers_for(team)
    record("alerts", f"{team} alerts -> platform + {team}", got == {"platform", team}, f"receivers {sorted(got)}")

# ---------------------------------------------------------------- summary
fails = [r for r in results if r[0] == "FAIL"]
print(f"\n{len([r for r in results if r[0] == 'PASS'])} passed, {len(fails)} failed, "
      f"{len([r for r in results if r[0] == 'SKIP'])} skipped  (run {RUN})")
for state, sec, name, detail in fails:
    print(f"  FAIL [{sec}] {name}: {detail}")
sys.exit(1 if fails else 0)

"""End-to-end test of the running gateway. Run: `run test` (compose service `selftest`).

Uses TEMPORARY keys only (alias selftest-*, deleted at the end); the team keys from .env are only
used for a read-only /v1/models call. Provider models get ONE real call each (max 16 tokens).

  gateway   every replica ready, DB + cache connected, every enabled model served
  hooks     x-gateway-hooks header lists the hooks of config/hooks.yaml
  teams     each team key sees exactly its team's models; allowed models answer; others refused
  cache     a repeated request is a cache hit; the same request from another team is not
  limits    a key's rpm limit holds across all replicas; a spent key budget blocks
  traces    a team's request lands in ITS Langfuse project, tagged; redacted teams send no text
  metrics   Prometheus scrapes every replica
  spend     cost is booked on the team
Exit code 1 on any failure.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MASTER = os.environ["LITELLM_MASTER_KEY"]
URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
LANGFUSE = os.environ.get("LANGFUSE_URL", "http://langfuse-web:3000").rstrip("/")
PROM = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
PROJECT = os.environ["PROJECT"]
PORT = os.environ.get("PORT_LITELLM", "4000")
REPLICAS = [f"http://{PROJECT}-litellm-{i}:{PORT}" for i in range(1, int(os.environ.get("REPLICAS", "1")) + 1)]
MOCK = "selftest-mock"
DENIED = ("not allowed to access model", "team not allowed", "key not allowed", "does not have access",
          "not available for this api key")

results = []
created = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""), flush=True)


def skip(name, why):
    print(f"  SKIP  {name}  - {why}", flush=True)


def call(method, path, key=MASTER, body=None, base=URL, timeout=60, headers_out=None, auth=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Authorization": auth or f"Bearer {key}"}
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if headers_out is not None:
                headers_out.update({k.lower(): v for k, v in r.headers.items()})
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"raw": str(e)}


def err(body):
    e = body.get("error", body) if isinstance(body, dict) else body
    return str((e.get("message") if isinstance(e, dict) else e) or json.dumps(body))


def request(key, model, mode="chat", base=URL, text=None, session=None, headers_out=None):
    md = {"session_id": session or f"selftest-{uuid.uuid4().hex[:8]}"}
    if mode == "embedding":
        return call("POST", "/v1/embeddings", key, {"model": model, "input": text or "selftest", "metadata": md},
                    base=base, timeout=150, headers_out=headers_out)
    return call("POST", "/v1/chat/completions", key, {
        "model": model, "messages": [{"role": "user", "content": text or "Reply with one word: pong"}],
        "max_tokens": 16, "metadata": md}, base=base, timeout=150, headers_out=headers_out)


def temp_key(alias, team_id=None, **limits):
    alias = f"selftest-{alias}"
    call("POST", "/key/delete", MASTER, {"key_aliases": [alias]})
    body = {"key_alias": alias, "metadata": {"tags": ["proj:selftest", "cc:test", "env:test"]}, **limits}
    if team_id:
        body["team_id"] = team_id
    status, res = call("POST", "/key/generate", MASTER, body)
    if status != 200:
        raise SystemExit(f"  FAIL  could not create temporary key {alias}: HTTP {status} {err(res)[:200]}")
    created.append(alias)
    return res["key"]


def lf_traces(project_keys, session):
    auth = "Basic " + base64.b64encode(f"{project_keys['public_key']}:{project_keys['secret_key']}".encode()).decode()
    q = urllib.parse.urlencode({"sessionId": session, "limit": 5})
    status, body = call("GET", f"/api/public/traces?{q}", base=LANGFUSE, auth=auth)
    return body.get("data", []) if status == 200 else []


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    teams_cfg = load("/w/.build/teams.json")
    models = {m["name"]: m for m in load("/w/.build/models.json")["enabled"]}
    hooks = [h["name"] for h in load("/w/.build/hooks.json")["hooks"]]
    state = load("/w/.state/state.json") if os.path.exists("/w/.state/state.json") else {}
    lf_projects = state.get("langfuse_projects", {})
    teams = teams_cfg["teams"]
    _, tl = call("GET", "/team/list")
    tl = tl if isinstance(tl, list) else tl.get("teams", [])
    ids = {t["team_alias"]: t["team_id"] for t in tl if t.get("team_alias")}

    print("\n  -- gateway")
    for base in REPLICAS:
        status, body = call("GET", "/health/readiness", base=base)
        check(f"{base.split('//')[1]} ready (db {body.get('db')})",
              status == 200 and body.get("db") == "connected", "" if status == 200 else err(body)[:120])
    _, served = call("GET", "/v1/models")
    served = sorted(m["id"] for m in served.get("data", []))
    check("every enabled model is served", set(models) <= set(served), f"served {served}")

    if not teams or teams[0]["id"] not in ids:
        check("teams provisioned", False, "run: run provision")
        return finish()
    first = teams[0]
    key_a = temp_key(f"{first['id']}-a", ids[first["id"]])

    print("\n  -- hooks")
    hdr = {}
    status, _ = request(key_a, MOCK, headers_out=hdr)
    got = hdr.get("x-gateway-hooks", "")
    check(f"x-gateway-hooks = {','.join(hooks)}", status == 200 and got == ",".join(hooks), f"HTTP {status}, header {got!r}")

    print("\n  -- teams")
    for t in teams:
        tid = ids.get(t["id"])
        if not tid:
            check(f"team {t['id']} exists in LiteLLM", False, "run: run provision")
            continue
        for k in t["keys"]:
            secret = os.environ.get(k["env"], "")
            status, listed = call("GET", "/v1/models", secret)
            got = sorted(m["id"] for m in listed.get("data", []))
            check(f"{k['alias']}: fixed key from .env sees exactly the team's models",
                  status == 200 and got == sorted(t["models"]), f"HTTP {status}, got {got}")
        key = temp_key(f"{t['id']}-models", tid)
        for i, m in enumerate(t["models"]):
            if m == MOCK or models.get(m, {}).get("hidden"):
                continue
            status, body = request(key, m, models[m]["mode"], base=REPLICAS[i % len(REPLICAS)])
            detail = "" if status == 200 else err(body)[:120]
            check(f"{t['id']}: allowed {m:<24} -> 200", status == 200, detail)
        for m in served:
            if m in t["models"]:
                continue
            status, body = request(key, m, models.get(m, {}).get("mode", "chat"))
            refused = status in (400, 401, 403) and any(h in err(body).lower() for h in DENIED)
            check(f"{t['id']}: refused {m:<24} -> HTTP {status}", refused, "" if refused else err(body)[:120])

    print("\n  -- cache (redis-cache, per-team namespaces)")
    if len(teams) > 1 and teams[1]["id"] in ids:
        prompt = f"cache probe {uuid.uuid4().hex}"
        h1, h2, h3 = {}, {}, {}
        request(key_a, MOCK, text=prompt, headers_out=h1)
        time.sleep(1)
        request(key_a, MOCK, text=prompt, base=REPLICAS[-1], headers_out=h2)
        key_b = temp_key(f"{teams[1]['id']}-b", ids[teams[1]["id"]])
        request(key_b, MOCK, text=prompt, headers_out=h3)
        check("repeat request (other replica) is a cache hit", "x-litellm-cache-key" in h2 and "x-litellm-cache-key" not in h1,
              f"first {h1.get('x-litellm-cache-key')!r}, second {h2.get('x-litellm-cache-key')!r}")
        check(f"same request from team {teams[1]['id']} is NOT served from {first['id']}'s cache",
              "x-litellm-cache-key" not in h3, f"cache key {h3.get('x-litellm-cache-key')!r}")
    else:
        skip("cache isolation", "needs two teams")

    print(f"\n  -- limits (temporary keys on team {first['id']})")
    key = temp_key("rpm", ids[first["id"]], rpm_limit=5)
    codes = [request(key, MOCK, base=REPLICAS[i % len(REPLICAS)], text=f"rpm {i} {uuid.uuid4().hex}")[0] for i in range(8)]
    check(f"rpm_limit 5 holds across {len(REPLICAS)} replicas (8 calls alternating)",
          codes.count(200) == 5 and all(c == 429 for c in codes[5:]), f"status codes {codes}")
    key = temp_key("budget", ids[first["id"]], max_budget=0.00001)
    status, body = request(key, MOCK, text=f"budget {uuid.uuid4().hex}")
    start, blocked = time.time(), None
    while time.time() - start < 120 and status == 200:
        time.sleep(10)
        status, body = request(key, MOCK, base=REPLICAS[-1], text=f"budget {uuid.uuid4().hex}")
        if status != 200:
            blocked = int(time.time() - start)
    check("key budget $0.00001 blocks calls once spent", blocked is not None and "budget" in err(body).lower(),
          f"blocked after ~{blocked}s (HTTP {status})" if blocked is not None else "never blocked within 120s")

    print("\n  -- traces (Langfuse, one project per team)")
    if not lf_projects:
        skip("per-team traces", "no team projects (observability.per_team_projects off?)")
    for t in teams:
        keys = lf_projects.get(t["id"])
        if not keys or t["id"] not in ids:
            continue
        session = f"selftest-{t['id']}-{uuid.uuid4().hex[:8]}"
        k = temp_key(f"{t['id']}-trace", ids[t["id"]])
        request(k, MOCK, text=f"trace probe {session}", session=session)
        traces, start = [], time.time()
        while not traces and time.time() - start < 90:
            time.sleep(5)
            traces = lf_traces(keys, session)
        tr = traces[0] if traces else {}
        tags = tr.get("tags") or []
        check(f"{t['id']}: trace in project {t['id']} with tag team:{t['id']}",
              bool(traces) and f"team:{t['id']}" in tags, f"{len(traces)} trace(s), tags {tags}")
        if t["observability"].get("redact_content"):
            text = json.dumps(tr.get("input")) + json.dumps(tr.get("output"))
            check(f"{t['id']}: prompt/answer redacted in Langfuse", bool(traces) and "trace probe" not in text and "pong" not in text,
                  text[:100])

    print("\n  -- metrics (Prometheus)")
    status, body = call("GET", "/api/v1/query?query=" + urllib.parse.quote('up{job="litellm"}'), base=PROM)
    ups = [r["value"][1] for r in (body.get("data") or {}).get("result", [])] if status == 200 else []
    check(f"Prometheus scrapes {len(REPLICAS)} replica(s)", len(ups) == len(REPLICAS) and all(v == "1" for v in ups),
          f"up = {ups}")

    print("\n  -- spend booked per team (spend buffer flushes in batches)")
    pending = {t["id"] for t in teams if t["id"] in ids}
    start = time.time()
    while pending and time.time() - start < 150:
        time.sleep(15)
        for a in list(pending):
            _, info = call("GET", f"/team/info?team_id={ids[a]}")
            if ((info.get("team_info") or {}).get("spend") or 0) > 0:
                pending.discard(a)
    for t in teams:
        if t["id"] in ids:
            check(f"{t['id']} spend booked", t["id"] not in pending)
    return finish()


def finish():
    if created:
        call("POST", "/key/delete", MASTER, {"key_aliases": created})
        print(f"\n  temporary keys deleted: {len(created)}")
    failed = results.count(False)
    print(f"\n  {len(results) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        if created and results is not None:
            call("POST", "/key/delete", MASTER, {"key_aliases": created})

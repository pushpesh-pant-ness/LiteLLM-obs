"""setup.yml -> .env + generated configs, and preflight checks (stdlib only).

Called by scripts/setup.sh inside the python:3.12-alpine image (repo mounted at /w):
    python -I scripts/setup.py render      # validate setup.yml, write the managed block of .env,
                                            # prometheus/prometheus.attach.yml (attach mode)
    python -I scripts/setup.py preflight   # attach mode: can we reach the production LiteLLM?
    python -I scripts/setup.py get KEY     # print one resolved value (for setup.sh)

setup.yml uses a small YAML subset: nested `key: value` maps, `- item` lists, inline [a, b] lists,
comments, quoted strings, true/false/null, numbers. No anchors, no multi-line strings.
"""

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request

ROOT = os.environ.get("SETUP_ROOT", "/w")
SETUP = os.path.join(ROOT, os.environ.get("SETUP_FILE", "setup.yml"))
ENV = os.path.join(ROOT, ".env")
BEGIN = "# >>> managed by scripts/setup.sh from setup.yml - edit setup.yml, not these lines"
END = "# <<< end of setup.yml block"


class SetupError(Exception):
    pass


# ------------------------------------------------------------------ YAML subset
def _strip_comment(line):
    out, quote = [], None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(c)
    return "".join(out)


def _scalar(s, n):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    if s.startswith("["):
        if not s.endswith("]"):
            raise SetupError(f"setup.yml line {n}: unterminated [list]")
        inner = s[1:-1].strip()
        return [_scalar(x, n) for x in inner.split(",")] if inner else []
    if s.startswith("{"):
        raise SetupError(f"setup.yml line {n}: inline {{maps}} are not supported - use indented lines")
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def parse_yaml(text):
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        s = _strip_comment(raw).rstrip()
        if not s.strip():
            continue
        lead = s[: len(s) - len(s.lstrip())]
        if "\t" in lead:
            raise SetupError(f"setup.yml line {n}: use spaces, not tabs, for indentation")
        lines.append((n, len(lead), s.strip()))

    def block(i, indent):
        if lines[i][2].startswith("- ") or lines[i][2] == "-":
            out = []
            while i < len(lines) and lines[i][1] == indent and lines[i][2].startswith("-"):
                item = lines[i][2][1:].strip()
                if not item or (":" in item and not item.startswith(("'", '"')) and re.match(r"^[\w.-]+:\s", item)):
                    raise SetupError(f"setup.yml line {lines[i][0]}: only lists of plain values are supported")
                out.append(_scalar(item, lines[i][0]))
                i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][1] == indent:
            n, _, s = lines[i]
            if s.startswith("-"):
                raise SetupError(f"setup.yml line {n}: list item where a 'key: value' was expected")
            m = re.match(r"^([\w.-]+)\s*:(\s+(.*))?$", s)
            if not m:
                raise SetupError(f"setup.yml line {n}: expected 'key: value', got {s!r}")
            key, rest = m.group(1), (m.group(3) or "").strip()
            if key in out:
                raise SetupError(f"setup.yml line {n}: '{key}' appears twice")
            i += 1
            if rest:
                out[key] = _scalar(rest, n)
            elif i < len(lines) and (lines[i][1] > indent or (lines[i][1] == indent and lines[i][2].startswith("-"))):
                out[key], i = block(i, lines[i][1])
            else:
                out[key] = None
        if i < len(lines) and lines[i][1] > indent:
            raise SetupError(f"setup.yml line {lines[i][0]}: unexpected indentation")
        return out, i

    if not lines:
        return {}
    value, i = block(0, lines[0][1])
    if i < len(lines):
        raise SetupError(f"setup.yml line {lines[i][0]}: unexpected indentation")
    if not isinstance(value, dict):
        raise SetupError("setup.yml must be a map of settings")
    return value


# ------------------------------------------------------------------ .env
def read_env(path=ENV):
    """Compose-style .env: KEY=VALUE, '#' comments, inline ' # comment' after unquoted values."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if v[:1] in "'\"" and v[-1:] == v[:1] and len(v) >= 2:
                    v = v[1:-1]
                else:
                    v = re.split(r"\s+#", v, 1)[0].strip()
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


def write_env_block(values, path=ENV):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    kept, inside = [], False
    for line in lines:
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if inside:
            continue
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            continue  # the block below now owns this key
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = ["", BEGIN] + [f"{k}={v}" for k, v in values.items()] + [END, ""]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(kept + block))


# ------------------------------------------------------------------ settings
def get(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] is None:
            return default
        cur = cur[part]
    return cur


def load_setup():
    try:
        with open(SETUP, encoding="utf-8") as f:
            return parse_yaml(f.read())
    except FileNotFoundError:
        raise SetupError("setup.yml not found - copy setup.example.yml to setup.yml and fill it in") from None


def hostport(value, default_port):
    value = str(value)
    if ":" in value:
        h, p = value.rsplit(":", 1)
        return h, int(p)
    return value, default_port


def resolve(cfg):
    """setup.yml -> (env values for the managed .env block, generated files, warnings)."""
    errors, warnings, files = [], [], {}
    mode = get(cfg, "mode", "local")
    if mode not in ("local", "attach"):
        errors.append("mode must be 'local' or 'attach'")

    bind = str(get(cfg, "server.bind", "127.0.0.1"))
    ports = {k: int(get(cfg, f"server.ports.{k}", d)) for k, d in
             (("grafana", 3001), ("langfuse", 3000), ("prometheus", 9090), ("alertmanager", 9093))}
    grafana_url = str(get(cfg, "server.grafana_url", f"http://localhost:{ports['grafana']}")).rstrip("/")
    langfuse_url = str(get(cfg, "server.langfuse_url", f"http://localhost:{ports['langfuse']}")).rstrip("/")
    members = get(cfg, "accounts.members", "local")
    if members not in ("local", "sso"):
        errors.append("accounts.members must be 'local' or 'sso'")
    test_viewers = bool(get(cfg, "accounts.test_viewers", mode == "local"))

    env = {
        "OBS_MODE": mode,
        "PULL_POLICY": get(cfg, "images.pull_policy", "never"),
        "BIND_ADDRESS": bind,
        "GRAFANA_PORT": ports["grafana"],
        "LANGFUSE_PORT": ports["langfuse"],
        "PROMETHEUS_PORT": ports["prometheus"],
        "ALERTMANAGER_PORT": ports["alertmanager"],
        "GRAFANA_PUBLIC_URL": grafana_url,
        "LANGFUSE_PUBLIC_URL": langfuse_url,
        "CREATE_TEST_USERS": str(test_viewers).lower(),
        "MEMBER_ACCOUNTS": members,
    }
    if get(cfg, "project_name"):
        env["COMPOSE_PROJECT_NAME"] = get(cfg, "project_name")

    if mode == "local":
        env.update({
            "COMPOSE_FILE": "docker-compose.yml",
            "COMPOSE_PROFILES": "traces",
            "LITELLM_IMAGE": get(cfg, "images.litellm", "ghcr.io/berriai/litellm:main-stable"),
            "LITELLM_REPLICAS": int(get(cfg, "local.litellm_replicas", 2)),
            "LITELLM_PORT": int(get(cfg, "local.litellm_port", 4000)),
            "LITELLM_PORT_LAST": int(get(cfg, "local.litellm_port", 4000)) + int(get(cfg, "local.litellm_replicas", 2)) - 1,
            "LITELLM_URL": "http://litellm:4000",
            "TENANTS_MODE": "manage",
            "TENANTS_FILE": get(cfg, "litellm.tenants_file", "tenants.json"),
            "OVERWRITE_TEAM_CALLBACKS": "true",
            "LANGFUSE_CALLBACK_HOST": "http://langfuse-web:3000",
            "DB_SETUP": "auto",
            "DB_ADMIN_HOST": "postgres", "DB_ADMIN_PORT": 5432, "DB_ADMIN_USER": "postgres",
            "LITELLM_DB_NAME": "litellm", "DB_SSLMODE": "prefer",
            "GRAFANA_PG_HOST": "postgres-replica:5432", "GRAFANA_PG_SSLMODE": "disable",
            "EXPECT_REPLICA": "true",
            "PROMETHEUS_CONFIG": "prometheus.yml",
            "LITELLM_CA_FILE": "/etc/ssl/certs/ca-certificates.crt",
        })
        return env, files, warnings, errors

    # ---------------- attach: use the LiteLLM that already runs (production)
    url = str(get(cfg, "litellm.url", "")).rstrip("/")
    if not re.match(r"^https?://", url):
        errors.append("litellm.url must be the http(s):// address of your LiteLLM (admin API)")
    targets = get(cfg, "litellm.metrics.targets", [])
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        errors.append("litellm.metrics.targets: list every LiteLLM replica as host:port (Prometheus scrapes each one)")
    scheme = get(cfg, "litellm.metrics.scheme", "https" if url.startswith("https") else "http")
    ca = get(cfg, "litellm.ca_file", "") or ""
    if ca and not ca.startswith("certs/"):
        errors.append("litellm.ca_file must be a file in certs/ (e.g. certs/ca-bundle.pem)")
    elif ca and not os.path.exists(os.path.join(ROOT, ca)):
        errors.append(f"litellm.ca_file {ca} does not exist")
    ca_in_container = "/certs/" + ca[len("certs/"):] if ca else ""
    tenants_mode = get(cfg, "litellm.tenants", "observe")
    if tenants_mode not in ("observe", "manage"):
        errors.append("litellm.tenants must be 'observe' or 'manage'")
    tenants_file = get(cfg, "litellm.tenants_file", "tenants.prod.json")
    if "/" in tenants_file or "\\" in tenants_file:
        errors.append("litellm.tenants_file is a file name inside bootstrap/ (no folders)")

    db_host = get(cfg, "database.host")
    db_port = int(get(cfg, "database.port", 5432))
    sslmode = get(cfg, "database.sslmode", "require")
    if sslmode not in ("disable", "require", "verify-ca", "verify-full"):
        errors.append("database.sslmode must be disable, require, verify-ca or verify-full (what both psql and Grafana support)")
    db_setup = get(cfg, "database.setup", "auto")
    if db_setup not in ("auto", "manual"):
        errors.append("database.setup must be 'auto' or 'manual'")
    if not db_host:
        errors.append("database.host: the Postgres server LiteLLM uses")
    admin_user = get(cfg, "database.admin_user")
    if db_setup == "auto" and not admin_user:
        errors.append("database.admin_user is needed with database.setup: auto")
    read_host = get(cfg, "database.read_host") or db_host
    read_port = int(get(cfg, "database.read_port", db_port))
    cb_host = str(get(cfg, "server.langfuse_url_for_litellm", langfuse_url)).rstrip("/")
    if re.search(r"//(localhost|127\.0\.0\.1)\b", cb_host):
        warnings.append("server.langfuse_url_for_litellm points at localhost: LiteLLM in another container/host "
                        "cannot send traces there - use this server's DNS name (or host.docker.internal for a local test)")
    if bind == "127.0.0.1" and not re.search(r"//(localhost|127\.0\.0\.1|host\.docker\.internal)\b", cb_host):
        warnings.append("server.bind is 127.0.0.1 but LiteLLM must reach Langfuse at "
                        f"{cb_host} - set server.bind: 0.0.0.0 (or this server's IP)")

    sep = ":"
    env.update({
        "COMPOSE_FILE": f"docker-compose.yml{sep}compose.attach.yml",
        "COMPOSE_PATH_SEPARATOR": sep,
        "COMPOSE_PROFILES": "traces,db-setup" if db_setup == "auto" else "traces",
        "LITELLM_URL": url,
        "TENANTS_MODE": tenants_mode,
        "TENANTS_FILE": tenants_file,
        "OVERWRITE_TEAM_CALLBACKS": str(bool(get(cfg, "litellm.overwrite_team_callbacks", False))).lower(),
        "LANGFUSE_CALLBACK_HOST": cb_host,
        "LITELLM_CA_FILE": ca_in_container or "/etc/ssl/certs/ca-certificates.crt",
        "DB_SETUP": db_setup,
        "DB_ADMIN_HOST": db_host or "",
        "DB_ADMIN_PORT": db_port,
        "DB_ADMIN_USER": admin_user or "",
        "LITELLM_DB_NAME": get(cfg, "database.name", "litellm"),
        "DB_SSLMODE": sslmode,
        "GRAFANA_PG_HOST": f"{read_host}:{read_port}",
        "GRAFANA_PG_SSLMODE": sslmode,
        "EXPECT_REPLICA": "true" if get(cfg, "database.read_host") else "false",
        "PROMETHEUS_CONFIG": "prometheus.attach.yml",
        "METRICS_TARGETS": ",".join(str(t) for t in targets),
    })
    if scheme not in ("http", "https"):
        errors.append("litellm.metrics.scheme must be http or https")
    tls = ""
    if scheme == "https":
        tls = (f"    tls_config:\n      ca_file: {ca_in_container}\n" if ca_in_container
               else "    # tls_config: { ca_file: /certs/... }   # set litellm.ca_file for a private CA\n")
    files["prometheus/prometheus.attach.yml"] = f"""# GENERATED by scripts/setup.sh from setup.yml (mode: attach) - edit setup.yml instead.
global:
  scrape_interval: 15s
  evaluation_interval: 30s

rule_files:
  - /etc/prometheus/alerts.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]

scrape_configs:
  # Every production LiteLLM replica, scraped directly (not through the load balancer, which would
  # return a different replica each time and make counters jump).
  - job_name: litellm
    metrics_path: /metrics/
    scheme: {scheme}
{tls}    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/litellm_metrics_token
    static_configs:
      - targets: {json.dumps([str(t) for t in targets])}

  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
"""
    return env, files, warnings, errors


def secrets_problems(env_values, mode, db_setup):
    secrets = read_env()
    out = []
    for k in ("LITELLM_MASTER_KEY", "GRAFANA_ADMIN_PASSWORD", "LANGFUSE_ADMIN_PASSWORD", "GRAFANA_RO_DB_PASSWORD"):
        if not secrets.get(k) or "CHANGE_ME" in secrets.get(k, ""):
            out.append(f"{k} is empty in .env")
    if mode == "attach" and db_setup == "auto" and not secrets.get("DB_ADMIN_PASSWORD"):
        out.append("DB_ADMIN_PASSWORD is empty in .env (password of database.admin_user)")
    return out


def cmd_render():
    cfg = load_setup()
    env, files, warnings, errors = resolve(cfg)
    if errors:
        raise SetupError("setup.yml has problems:\n  - " + "\n  - ".join(errors))
    write_env_block(env)
    if env["OBS_MODE"] == "attach" and "DB_ADMIN_PASSWORD" not in read_env():
        with open(ENV, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n# Production (attach mode) secrets - fill in:\n"
                    "# LITELLM_MASTER_KEY above = the production master key or a proxy-admin key\n"
                    "DB_ADMIN_PASSWORD=\n")
    for path, content in files.items():
        with open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    print(f"  mode: {env['OBS_MODE']}" + (f"  ->  LiteLLM {env['LITELLM_URL']}" if env["OBS_MODE"] == "attach" else ""))
    print(f"  .env: {len(env)} settings written (block between the setup.yml markers)")
    for path in files:
        print(f"  wrote {path}")
    for w in warnings:
        print(f"  WARN  {w}")
    problems = secrets_problems(env, env["OBS_MODE"], env.get("DB_SETUP"))
    if problems:
        raise SetupError("secrets missing:\n  - " + "\n  - ".join(problems))


# ------------------------------------------------------------------ preflight (attach)
def http(method, url, key=None, timeout=15):
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(errors="replace"), e.code
    except Exception as e:  # DNS, refused, TLS
        return 0, str(e)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def cmd_preflight():
    cfg = load_setup()
    env, _, _, errors = resolve(cfg)
    if errors:
        raise SetupError("run render first: " + "; ".join(errors))
    if env["OBS_MODE"] != "attach":
        print("  local mode: nothing to check outside this machine")
        return
    if env["LITELLM_CA_FILE"].startswith("/certs/"):
        os.environ["SSL_CERT_FILE"] = os.path.join(ROOT, "certs", env["LITELLM_CA_FILE"][len("/certs/"):])
    key = read_env().get("LITELLM_MASTER_KEY", "")
    url = env["LITELLM_URL"]
    fails, warns = [], []

    def report(ok, msg, detail="", warn=False):
        state = "PASS" if ok else ("WARN" if warn else "FAIL")
        print(f"  {state:<4}  {msg}" + (f"  - {detail}" if detail else ""))
        if not ok:
            (warns if warn else fails).append(msg)

    s, b = http("GET", url + "/health/liveliness")
    report(s == 200, f"LiteLLM answers at {url}", "" if s == 200 else f"HTTP {s}: {str(b)[:160]}")
    s, b = http("GET", url + "/team/list", key)
    report(s == 200, "LITELLM_MASTER_KEY is accepted as admin",
           "" if s == 200 else f"HTTP {s} - put the production master key (or a proxy-admin key) in .env")
    if s == 200:
        n = len(b) if isinstance(b, list) else len(b.get("teams", []))
        print(f"        {n} teams in LiteLLM")
    s, b = http("GET", url + "/organization/list", key)
    report(s == 200, "LiteLLM organizations available (Enterprise)", "" if s == 200 else f"HTTP {s} - teams are grouped by tenants file only", warn=True)
    s, b = http("GET", url + "/get/config/callbacks", key)
    names = [c.get("name", "") for c in (b.get("callbacks", []) if isinstance(b, dict) else [])]
    report("prometheus" in names, "LiteLLM has the prometheus callback",
           "" if "prometheus" in names else "add litellm/prod-config-snippet.yaml to the production config", warn=True)
    report(any("redact_reasoning" in n for n in names), "LiteLLM has the reasoning-redaction hook",
           "" if any("redact_reasoning" in n for n in names) else
           "needed for sensitive teams on reasoning models - see litellm/prod-config-snippet.yaml", warn=True)
    for t in env["METRICS_TARGETS"].split(","):
        host, port = hostport(t, 4000)
        try:
            socket.create_connection((host, port), 5).close()
            report(True, f"metrics target {t} reachable")
        except OSError as e:
            report(False, f"metrics target {t} reachable", str(e))
    for label, hp in (("LiteLLM database", f"{env['DB_ADMIN_HOST']}:{env['DB_ADMIN_PORT']}"),
                      ("database Grafana reads", env["GRAFANA_PG_HOST"])):
        host, port = hostport(hp, 5432)
        try:
            socket.create_connection((host, port), 5).close()
            report(True, f"{label} {hp} reachable")
        except OSError as e:
            report(False, f"{label} {hp} reachable", str(e))
    print(f"  NOTE  LiteLLM must be able to reach Langfuse at {env['LANGFUSE_CALLBACK_HOST']} (traces are sent there)")
    if fails:
        raise SetupError(f"preflight failed: {', '.join(fails)}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "render"
    try:
        if cmd == "render":
            cmd_render()
        elif cmd == "preflight":
            cmd_preflight()
        elif cmd == "get":
            env, _, _, _ = resolve(load_setup())
            print(env.get(sys.argv[2], ""))
        else:
            raise SetupError(f"unknown command {cmd}")
    except SetupError as e:
        print(f"  ERROR {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()

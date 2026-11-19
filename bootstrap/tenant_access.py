"""Per-team / per-org access in Grafana and Langfuse (stdlib only, idempotent).

    ./scripts/apply-tenants.sh        # runs bootstrap -> db-setup -> this script

Grafana (scripted end to end):
  - main org = admin: everything (provisioned dashboards, Prometheus, all teams)
  - one Grafana org per LiteLLM org and per team, each with ONE datasource that logs in as that
    scope's read-only Postgres role (sees only its teams), plus the same Cost dashboard
  - test viewer users per scope (CREATE_TEST_USERS=true, local only; real users come from Entra ID)

Langfuse (OSS: access is per organization, so one Langfuse org per team):
  - creating the org + an org API key is a one-time UI step (no free API) -> langfuse-orgs.json
  - this script then creates the team's project + a project API key (-> langfuse-projects.json),
    which bootstrap.py puts on the team's LiteLLM callback so its traces land there
  - memberships: team viewers in their team org; org leads in every team org of their org
"""

import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

GRAFANA = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
GRAFANA_AUTH = "Basic " + base64.b64encode(f"admin:{os.environ.get('GRAFANA_ADMIN_PASSWORD', '')}".encode()).decode()
LANGFUSE = os.environ.get("LANGFUSE_URL", "http://langfuse-web:3000").rstrip("/")
TENANTS = os.environ.get("TENANTS_FILE", "tenants.json")
SCOPE_CREDS = os.environ.get("SCOPE_CREDS", "scope-credentials.json")
TEST_USERS = os.environ.get("TEST_USERS_FILE", "test-users.json")
LF_ORGS = os.environ.get("LANGFUSE_ORGS_FILE", "langfuse-orgs.json")
LF_PROJECTS = os.environ.get("LANGFUSE_PROJECTS_FILE", "langfuse-projects.json")
LF_USER_IDS = os.environ.get("LANGFUSE_USER_IDS_FILE", "langfuse-user-ids.json")
DASHBOARD = os.environ.get("TEAM_DASHBOARD", "/dashboards/litellm-cost.json")
PG_HOST = os.environ.get("GRAFANA_PG_HOST", "postgres-replica:5432")
CREATE_TEST_USERS = os.environ.get("CREATE_TEST_USERS", "true").lower() == "true"
# local = tenants.json members get Grafana + Langfuse logins with generated passwords; sso = invites only
MEMBER_ACCOUNTS = os.environ.get("MEMBER_ACCOUNTS", "local")
DB_NAME = os.environ.get("LITELLM_DB_NAME", "litellm")
PG_SSLMODE = os.environ.get("GRAFANA_PG_SSLMODE", "disable")
LANGFUSE_PUBLIC_URL = os.environ.get("LANGFUSE_PUBLIC_URL", "http://localhost:3000")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def gf(method, path, body=None, org=None):
    h = {"Authorization": GRAFANA_AUTH}
    if org:
        h["X-Grafana-Org-Id"] = str(org)
    return http(method, GRAFANA + path, body, h)


def must(status, body, what, ok=(200,)):
    if status not in ok:
        sys.exit(f"  ! {what} failed: HTTP {status}: {str(body)[:300]}")
    return body


# ------------------------------------------------------------------ Grafana
def ensure_org(name):
    s, b = gf("GET", f"/api/orgs/name/{name}")
    if s == 200:
        return b["id"], False
    b = must(*gf("POST", "/api/orgs", {"name": name}), f"create org {name}")
    return b["orgId"], True


def ensure_datasource(org_id, scope):
    body = {"name": "LiteLLM Postgres", "uid": "litellm-pg", "type": "grafana-postgresql-datasource", "access": "proxy",
            "url": PG_HOST, "user": scope["role"], "isDefault": True,
            "jsonData": {"database": DB_NAME, "sslmode": PG_SSLMODE, "postgresVersion": 1600, "timescaledb": False,
                         "maxOpenConns": 5},
            "secureJsonData": {"password": scope["password"]}}
    s, _ = gf("GET", "/api/datasources/uid/litellm-pg", org=org_id)
    if s == 200:
        must(*gf("PUT", "/api/datasources/uid/litellm-pg", body, org=org_id), "update datasource")
    else:
        must(*gf("POST", "/api/datasources", body, org=org_id), "create datasource")


def ensure_dashboard(org_id, dash):
    s, b = gf("POST", "/api/folders", {"uid": "litellm", "title": "LiteLLM"}, org=org_id)
    if s not in (200, 409, 412) and not (s == 400 and "exists" in str(b).lower()):
        must(s, b, "create folder")
    d = dict(dash)
    d.pop("id", None)
    for v in d.get("templating", {}).get("list", []):
        if v.get("name") == "langfuse_url":
            v["query"] = LANGFUSE_PUBLIC_URL
            v["current"] = {"text": LANGFUSE_PUBLIC_URL, "value": LANGFUSE_PUBLIC_URL}
    must(*gf("POST", "/api/dashboards/db", {"dashboard": d, "folderUid": "litellm", "overwrite": True}, org=org_id),
         "import dashboard")


def ensure_admin_member(org_id):
    s, b = gf("POST", f"/api/orgs/{org_id}/users", {"loginOrEmail": "admin", "role": "Admin"})
    if s not in (200, 409):
        must(s, b, "add admin to org")


def test_password(old):
    # Langfuse requires letters, digits and a special character.
    if old and any(not c.isalnum() for c in old):
        return old
    return secrets.token_hex(10) + "-Lf1!"


def ensure_user(login, org_ids, users, name=None, email=None):
    """Local Grafana user that is a Viewer in exactly `org_ids` (and nowhere else, not the main org)."""
    pw = test_password(users.get(login, {}).get("password"))
    email = email or f"{login}@example.com"
    s, b = gf("GET", f"/api/users/lookup?loginOrEmail={login}")
    if s == 200:
        uid = b["id"]
        gf("PUT", f"/api/admin/users/{uid}/password", {"password": pw})
    else:
        b = must(*gf("POST", "/api/admin/users", {"name": name or login, "login": login, "email": email,
                                                  "password": pw, "OrgId": org_ids[0]}), f"create user {login}")
        uid = b["id"]
    for org_id in org_ids:
        s, b = gf("POST", f"/api/orgs/{org_id}/users", {"loginOrEmail": login, "role": "Viewer"})
        if s not in (200, 409):
            must(s, b, f"add {login} to org {org_id}")
    s, orgs = gf("GET", f"/api/users/{uid}/orgs")
    for o in orgs if isinstance(orgs, list) else []:
        if o["orgId"] not in org_ids:
            gf("DELETE", f"/api/orgs/{o['orgId']}/users/{uid}")
    gf("POST", f"/api/users/{uid}/using/{org_ids[0]}")
    users[login] = {"password": pw, "email": email, "name": name or login, "grafana_orgs": org_ids}


def members_by_login(cfg):
    """tenants.json members -> {login: {"name", "email", "scopes": [team/org aliases]}}.
    A member listed on a team sees that team; listed on an org (an org lead) sees all its teams."""
    out = {}
    for org in cfg["orgs"]:
        holders = [(org["alias"], org)] + [(t["alias"], t) for t in org.get("teams", [])]
        for alias, holder in holders:
            for m in holder.get("members", []):
                e = out.setdefault(m["login"], {"name": m.get("name"), "email": m.get("email"), "scopes": []})
                e["scopes"].append(alias)
    return out


def grafana(cfg, scopes, users):
    with open(DASHBOARD, encoding="utf-8") as f:
        dash = json.load(f)
    print("Grafana")
    org_of_scope = {}
    for name, sc in scopes.items():
        org_id, created = ensure_org(sc["alias"])
        ensure_admin_member(org_id)
        ensure_datasource(org_id, sc)
        ensure_dashboard(org_id, dash)
        sc["grafana_org_id"] = org_id
        org_of_scope[sc["alias"]] = org_id
        line = f"  org  {sc['alias']:<18} id={org_id:<3} {'created' if created else 'updated'}: datasource as {sc['role']}, Cost dashboard"
        if CREATE_TEST_USERS:
            login = f"viewer-{sc['alias']}"
            ensure_user(login, [org_id], users)
            line += f", test user {login}"
        print(line)
    if MEMBER_ACCOUNTS == "local":
        for login, m in members_by_login(cfg).items():
            ensure_user(login, [org_of_scope[s] for s in m["scopes"]], users, m["name"], m["email"])
            print(f"  user {login:<18} ({m['name'] or login}) viewer in: {', '.join(m['scopes'])}")


# ------------------------------------------------------------------ Langfuse
def project_key_works(key):
    auth = "Basic " + base64.b64encode(f"{key['public_key']}:{key['secret_key']}".encode()).decode()
    s, b = http("GET", LANGFUSE + "/api/public/projects", None, {"Authorization": auth})
    return s == 200 and any(p.get("id") == key.get("project_id") for p in (b.get("data") or []))


def langfuse(cfg, users):
    from langfuse_admin import LangfuseAdmin
    projects = load(LF_PROJECTS, {})
    admin = LangfuseAdmin(LANGFUSE, os.environ["LANGFUSE_ADMIN_EMAIL"], os.environ["LANGFUSE_ADMIN_PASSWORD"])
    print("Langfuse (one org + project per team; org leads join every team org of their org)")
    for org in cfg["orgs"]:
        for t in org.get("teams", []):
            team = t["alias"]
            org_id, new_org = admin.ensure_org(team)
            pid, new_proj = admin.ensure_project(org_id, team)
            entry = projects.get(team, {})
            if entry.get("project_id") != pid or not project_key_works(entry):
                pk, sk = admin.create_project_key(pid, "litellm team callback (bootstrap)")
                entry = {"org_id": org_id, "project_id": pid, "public_key": pk, "secret_key": sk}
                key_note = "new API key"
            else:
                key_note = "API key ok"
            entry["org_id"] = org_id
            projects[team] = entry
            members = []
            wanted = [f"viewer-{team}", f"viewer-{org['alias']}"] if CREATE_TEST_USERS else []
            # tenants.json members of this team, and org leads (members of the team's org)
            for login, m in members_by_login(cfg).items():
                if team in m["scopes"] or org["alias"] in m["scopes"]:
                    wanted.append((login, m.get("email") or f"{login}@example.com"))
            for w in wanted:
                login, email = (w, f"{w}@example.com") if isinstance(w, str) else w
                members.append(f"{login} ({admin.add_member(org_id, email, 'VIEWER')})")
            print(f"  {team:<16} org {'created' if new_org else 'ok'}, project {'created' if new_proj else 'ok'} "
                  f"({pid}), {key_note}" + (f"; viewers: {', '.join(members)}" if members else ""))
    save(LF_PROJECTS, projects)


def main():
    cfg = load(TENANTS, None)
    scopes = load(SCOPE_CREDS, {})
    if not scopes:
        sys.exit(f"no {SCOPE_CREDS} - run bootstrap first")
    users = load(TEST_USERS, {})
    if "--langfuse-only" not in sys.argv:
        grafana(cfg, scopes, users)
        save(SCOPE_CREDS, scopes)
    if "--grafana-only" not in sys.argv:
        langfuse(cfg, users)
    if CREATE_TEST_USERS or MEMBER_ACCOUNTS == "local":
        save(TEST_USERS, users)
        print(f"\nlocal accounts (members + test viewers) and passwords: bootstrap/{TEST_USERS}")


if __name__ == "__main__":
    main()

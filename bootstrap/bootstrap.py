"""Create LiteLLM orgs -> teams -> keys from tenants.json (stdlib only, idempotent).

    docker compose run --rm bootstrap                       # apply the tenants file
    docker compose run --rm bootstrap --check               # validate naming only, no API calls
    docker compose run --rm bootstrap --discover FILE       # write a tenants file from the teams in LiteLLM

TENANTS_MODE (setup.yml litellm.tenants):
    manage   create / update orgs, teams, budgets, models and keys from the tenants file (local mode)
    observe  the teams already exist (production): only team metadata (org, sensitive, Langfuse
             project) and the team's Langfuse trace callback are set. Budgets, models, keys untouched.

New keys are written to generated-keys.json (keep it secret; it is git-ignored).
LiteLLM only shows a key once, so a key that already exists is reported but not re-printed.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = os.environ.get("LITELLM_URL", "http://localhost:4000").rstrip("/")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")
TENANTS = os.environ.get("TENANTS_FILE", "tenants.json")
OUT = os.environ.get("KEYS_OUT", "generated-keys.json")
MODE = os.environ.get("TENANTS_MODE", "manage")
OVERWRITE_CALLBACKS = os.environ.get("OVERWRITE_TEAM_CALLBACKS", "true").lower() == "true"
DB_NAME = os.environ.get("LITELLM_DB_NAME", "litellm")

ORG_RE = re.compile(r"^org-[a-z0-9]+(-[a-z0-9]+)*$")
TEAM_RE = re.compile(r"^team-[a-z0-9]+(-[a-z0-9]+)*$")
ENVS = {"prod", "staging", "dev", "test"}
REQUIRED_TAGS = ("proj", "cc", "env")

# For "sensitive": true teams, a team-level callback setting makes LiteLLM replace prompt and
# response text with "redacted-by-litellm" before it reaches Langfuse. Spend, tokens, latency
# and the trace itself are still recorded. (Verified on LiteLLM 1.106; key metadata
# mask_input/mask_output is NOT honoured by the current Langfuse integration.)
#
# Every team gets this team-level Langfuse callback (success AND failure). The global config has
# no Langfuse failure_callback, because auth-time rejections are logged before team settings
# apply and would leak a sensitive team's prompt. See litellm/config.yaml.
#
# Once tenant_access.py has created the team's own Langfuse org + project, the callback also carries
# that project's keys, so the team's traces land in its own project (written to langfuse-projects.json).
LF_PROJECTS = os.environ.get("LANGFUSE_PROJECTS_FILE", "langfuse-projects.json")
LF_HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000")


def team_langfuse_project(alias):
    try:
        with open(LF_PROJECTS, encoding="utf-8") as f:
            return json.load(f).get(alias)
    except FileNotFoundError:
        return None


def team_langfuse_callback(sensitive, project=None):
    vars_ = {"turn_off_message_logging": "true" if sensitive else "false"}
    if project:
        vars_.update({"langfuse_public_key": project["public_key"], "langfuse_secret_key": project["secret_key"],
                      "langfuse_host": LF_HOST})
    return {"callback_name": "langfuse", "callback_type": "success_and_failure", "callback_vars": vars_}


# ------------------------------------------------------------------ validation
def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def validate_observe(cfg):
    """Production teams keep their own names: only require them present and unique as DB names."""
    errors, seen = [], {}
    for org in cfg.get("orgs", []):
        if not org.get("alias"):
            errors.append("every org needs an alias")
        for team in org.get("teams", []):
            alias = team.get("alias")
            if not alias:
                errors.append(f"a team in {org.get('alias')!r} has no alias")
                continue
            if team.get("keys"):
                errors.append(f"{alias}: keys are not managed in observe mode - remove them or use tenants: manage")
            name = scope_name(alias, "team")
            if name in seen:
                errors.append(f"teams {seen[name]!r} and {alias!r} map to the same database role {name}")
            seen[name] = alias
    return errors


def validate(cfg):
    if MODE == "observe":
        return validate_observe(cfg)
    errors = []
    seen_keys = set()
    for org in cfg.get("orgs", []):
        if not ORG_RE.match(org.get("alias", "")):
            errors.append(f"org alias {org.get('alias')!r} must look like org-<name>")
        for team in org.get("teams", []):
            talias = team.get("alias", "")
            if not TEAM_RE.match(talias):
                errors.append(f"team alias {talias!r} must look like team-<name>")
                continue
            short = talias[len("team-"):]
            for key in team.get("keys", []):
                kalias = key.get("alias", "")
                m = re.match(rf"^{re.escape(short)}-([a-z0-9]+(?:-[a-z0-9]+)*)-([a-z]+)$", kalias)
                if not m or m.group(2) not in ENVS:
                    errors.append(f"key alias {kalias!r} must be {short}-<app>-<{'|'.join(sorted(ENVS))}>")
                if kalias in seen_keys:
                    errors.append(f"key alias {kalias!r} is used twice")
                seen_keys.add(kalias)
                tags = dict(t.split(":", 1) for t in key.get("tags", []) if ":" in t)
                for req in REQUIRED_TAGS:
                    if req not in tags:
                        errors.append(f"key {kalias!r} is missing tag {req}:<value>")
                if m and tags.get("env") and tags["env"] != m.group(2):
                    errors.append(f"key {kalias!r}: env tag {tags['env']!r} does not match alias suffix {m.group(2)!r}")
    return errors


# ------------------------------------------------------------------ api helpers
class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status, self.body = status, body


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        URL + path, data=data, method=method,
        headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace")) from None


def list_orgs():
    res = api("GET", "/organization/list")
    return res if isinstance(res, list) else res.get("organizations", [])


def list_teams():
    res = api("GET", "/team/list")
    return res if isinstance(res, list) else res.get("teams", [])


def list_key_aliases():
    """key alias -> hashed token (used to update limits on keys that already exist)."""
    aliases, page = {}, 1
    while True:
        res = api("GET", f"/key/list?return_full_object=true&size=100&page={page}")
        keys = res.get("keys", [])
        for k in keys:
            if isinstance(k, dict) and k.get("key_alias"):
                aliases[k["key_alias"]] = k.get("token")
        if page >= int(res.get("total_pages") or 1) or not keys:
            return aliases
        page += 1


# ------------------------------------------------------------------ apply
def ensure_org(org, existing, orgs_supported):
    if not orgs_supported:
        return None, orgs_supported
    alias = org["alias"]
    if alias in existing:
        print(f"  org  {alias:<28} exists")
        return existing[alias], True
    body = {"organization_alias": alias}
    for f in ("max_budget", "budget_duration", "models"):
        if f in org:
            body[f] = org[f]
    try:
        res = api("POST", "/organization/new", body)
    except ApiError as e:
        if e.status in (400, 401, 403) and ("enterprise" in e.body.lower() or "premium" in e.body.lower()):
            print(f"  ! organizations need LiteLLM Enterprise on this version - creating teams without an org\n    ({e.body[:160]})")
            return None, False
        raise
    print(f"  org  {alias:<28} created")
    return res["organization_id"], True


def team_metadata(team_id):
    info = api("GET", f"/team/info?team_id={team_id}")
    return (info.get("team_info") or {}).get("metadata") or {}


def merge_metadata(team_id, alias, ours):
    """Merge our keys into the team's metadata. LiteLLM replaces metadata as a whole on update, so the
    team's own keys and its registered callbacks (encrypted at rest; re-encryption is idempotent) are
    written back unchanged. Refuses if LiteLLM handed back masked secrets - writing those would erase them."""
    current = team_metadata(team_id)
    md = {**current, **ours}
    if md == current:
        return False
    if any(m in json.dumps({k: current.get(k) for k in ("logging", "callback_settings")}) for m in ("REDACTED", "***")):
        print(f"  ! team {alias}: LiteLLM returned masked callback secrets - metadata NOT updated (it would erase them)")
        return False
    api("POST", "/team/update", {"team_id": team_id, "metadata": md})
    return True


def callback_owned(team_id):
    """May this stack set the team's Langfuse callback? Yes if the team has none, it already points at
    this stack's Langfuse, or OVERWRITE_TEAM_CALLBACKS=true. -> (owned, callback names, langfuse_host)"""
    cur = api("GET", f"/team/{team_id}/callback").get("data") or {}
    names = set((cur.get("success_callbacks") or []) + (cur.get("failure_callbacks") or []))
    host = (cur.get("callback_vars") or {}).get("langfuse_host")
    return (OVERWRITE_CALLBACKS or not names or host == LF_HOST), names, host


def our_metadata(team, owned):
    # org alias kept in metadata: real LiteLLM orgs are Enterprise-only, and the reporting views
    # group teams by metadata->>'org' (falling back to the LiteLLM organization).
    md = {"org": team["_org"], "sensitive": bool(team.get("sensitive"))}
    project = team_langfuse_project(team["alias"])
    if project and owned:  # Grafana links requests to this Langfuse project - only if traces go there
        md["langfuse_project_id"] = project["project_id"]
    return md


def ensure_team(team, org_id, existing):
    alias = team["alias"]
    body = {"team_alias": alias, "max_budget": team.get("max_budget"),
            "budget_duration": team.get("budget_duration", "30d")}
    if team.get("models"):
        body["models"] = team["models"]
    if alias in existing:
        team_id = existing[alias]
        api("POST", "/team/update", {"team_id": team_id, **body})
        owned, _, _ = callback_owned(team_id)
        merge_metadata(team_id, alias, our_metadata(team, owned))
        print(f"  team {alias:<28} exists (budget ${team.get('max_budget')}/{body['budget_duration']} re-applied)")
        return team_id
    body["metadata"] = our_metadata(team, True)
    if org_id:
        body["organization_id"] = org_id
    res = api("POST", "/team/new", body)
    print(f"  team {alias:<28} created (budget ${team.get('max_budget')}/{body['budget_duration']})")
    return res["team_id"]


def observe_team(team, by_alias, by_id):
    """Production team: find it and merge our metadata keys into its own. Nothing else changes."""
    alias = team["alias"]
    team_id = team.get("team_id") or by_alias.get(alias)
    if not team_id or team_id not in by_id:
        sys.exit(f"  ! team {alias!r} ({team.get('team_id') or 'no team_id'}) does not exist in LiteLLM - "
                 "fix the tenants file (bootstrap --discover NEW_FILE lists the current teams)")
    owned, _, _ = callback_owned(team_id)
    if merge_metadata(team_id, alias, our_metadata(team, owned)):
        print(f"  team {alias:<28} metadata set (org, sensitive{', Langfuse project' if owned else ''}); "
              "budget/models/keys untouched")
    else:
        print(f"  team {alias:<28} ok (budget/models/keys untouched)")
    return team_id


def set_team_callback(team_id, alias, sensitive):
    """Team-level Langfuse callback. A team whose callbacks point somewhere else is left alone unless
    OVERWRITE_TEAM_CALLBACKS=true (production teams may already log to their own tools)."""
    owned, names, host = callback_owned(team_id)
    if not owned:
        print(f"  ! team {alias:<26} already has callbacks {sorted(names)} (langfuse_host={host}) - left alone.\n"
              f"    Set litellm.overwrite_team_callbacks: true in setup.yml to send its traces to this stack.")
        return
    if "langfuse" in names:  # LiteLLM refuses a second entry with the same name: remove ours first
        try:
            api("DELETE", f"/team/{team_id}/callback/langfuse")
        except ApiError as e:
            if e.status not in (404, 405) or "not registered" in e.body:
                raise
            # LiteLLM without the DELETE route: only safe if langfuse is the team's only callback
            if names - {"langfuse"}:
                print(f"  ! team {alias}: this LiteLLM cannot remove one callback and the team has others "
                      f"{sorted(names - {'langfuse'})} - left alone")
                return
            api("POST", f"/team/{team_id}/disable_logging")
    project = team_langfuse_project(alias)
    api("POST", f"/team/{team_id}/callback", team_langfuse_callback(sensitive, project))
    where = f"Langfuse project {project['project_id']}" if project else "admin Langfuse project"
    print(f"  team {alias:<28} traces -> {where}" + (" (prompt/response redacted)" if sensitive else ""))


def discover(path):
    """Write a tenants file (observe mode) from the teams that exist in LiteLLM."""
    if os.path.exists(path):
        sys.exit(f"{path} exists - not overwriting")
    try:
        orgs = {o["organization_id"]: o.get("organization_alias") or o["organization_id"] for o in list_orgs()}
    except ApiError:
        orgs = {}
    groups, skipped = {}, []
    for t in list_teams():
        if not t.get("team_alias"):
            skipped.append(t.get("team_id"))
            continue
        md = t.get("metadata") or {}
        org = md.get("org") or orgs.get(t.get("organization_id")) or "org-unassigned"
        groups.setdefault(org, []).append({"alias": t["team_alias"], "team_id": t["team_id"],
                                           "sensitive": bool(md.get("sensitive", False)), "members": []})
    cfg = {"_comment": ("Observe mode (setup.yml litellm.tenants: observe). These teams exist in LiteLLM and are "
                        "matched by team_id. Add members {login, name, email} who may see a team - or put them on an "
                        "org to see all its teams - and set sensitive: true where prompts must not be stored. Remove "
                        "teams you do not want here. Budgets, models and keys stay managed in LiteLLM. "
                        "Then run ./scripts/setup.sh again."),
           "orgs": [{"alias": o, "members": [], "teams": sorted(ts, key=lambda x: x["alias"])}
                    for o, ts in sorted(groups.items())]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    n = sum(len(ts) for ts in groups.values())
    print(f"wrote bootstrap/{os.path.basename(path)}: {len(groups)} org(s), {n} team(s)"
          + (f"; skipped {len(skipped)} team(s) without an alias" if skipped else ""))


KEY_LIMITS = ("max_budget", "budget_duration", "rpm_limit", "tpm_limit")


def key_limits(key):
    return {f: key[f] for f in KEY_LIMITS if key.get(f) is not None}


def describe_limits(lim):
    parts = []
    if "max_budget" in lim:
        parts.append(f"${lim['max_budget']}/{lim.get('budget_duration', '-')}")
    if "rpm_limit" in lim:
        parts.append(f"{lim['rpm_limit']} rpm")
    if "tpm_limit" in lim:
        parts.append(f"{lim['tpm_limit']} tpm")
    return ", ".join(parts) or "no limits"


def ensure_key(key, team, team_id, existing):
    alias = key["alias"]
    lim = key_limits(key)
    if alias in existing:
        if lim and existing[alias]:
            api("POST", "/key/update", {"key": existing[alias], **lim})
            print(f"  key  {alias:<28} exists (limits re-applied: {describe_limits(lim)})")
        else:
            print(f"  key  {alias:<28} exists")
        return None
    metadata = {"tags": key["tags"]}
    body = {"team_id": team_id, "key_alias": alias, "metadata": metadata, "tags": key["tags"], **lim}
    try:
        res = api("POST", "/key/generate", body)
    except ApiError as e:
        if "tags" in e.body.lower() and e.status in (400, 401, 403):
            # Some versions gate the top-level `tags` field; metadata.tags still drives spend tags.
            body.pop("tags")
            res = api("POST", "/key/generate", body)
        else:
            raise
    print(f"  key  {alias:<28} created ({describe_limits(lim)})")
    return {"key_alias": alias, "team": team["alias"], "tags": key["tags"], "key": res["key"]}


SCRAPER_ALIAS = "infra-prometheus-scraper-prod"
SCRAPER_FILE = os.environ.get("SCRAPER_TOKEN_FILE", "")


def ensure_scraper_key(existing):
    """A key that may only call /metrics, for Prometheus. Written to prometheus/secrets/."""
    if not SCRAPER_FILE:
        return
    if os.path.exists(SCRAPER_FILE) and os.path.getsize(SCRAPER_FILE) > 0:
        print(f"  key  {SCRAPER_ALIAS:<28} exists (token file present)")
        return
    if SCRAPER_ALIAS in existing:
        # LiteLLM shows a key only once: the old one is useless without its file, so replace it.
        api("POST", "/key/delete", {"key_aliases": [SCRAPER_ALIAS]})
        print(f"  key  {SCRAPER_ALIAS:<28} token file missing - old key deleted, creating a new one")
    res = api("POST", "/key/generate", {
        "key_alias": SCRAPER_ALIAS,
        "allowed_routes": ["/metrics", "/metrics/"],
        "metadata": {"tags": ["proj:observability", "cc:infra", "env:prod"]},
    })
    with open(SCRAPER_FILE, "w", encoding="utf-8") as f:
        f.write(res["key"])
    print(f"  key  {SCRAPER_ALIAS:<28} created (metrics-only) -> prometheus/secrets/")


# ------------------------------------------------------------------ scoped read access
# One read-only Postgres role per team and per org. Each role sees a schema of views with the SAME
# names as the admin `reporting` schema, filtered to its team ids, so one Grafana dashboard works
# for admin, org and team. SQL -> postgres/generated/30-scopes.sql (applied by service db-setup),
# passwords -> bootstrap/scope-credentials.json (git-ignored; used for the Grafana datasources).
SCOPES_SQL = os.environ.get("SCOPES_SQL", "")
SCOPE_CREDS = os.environ.get("SCOPE_CREDS", "scope-credentials.json")


def scope_name(alias, prefix):
    """team-search -> team_search, "Search Team" -> team_search_team (safe Postgres role/schema name)."""
    s = slug(alias)
    if s.startswith(prefix + "_"):
        s = s[len(prefix) + 1:]
    return f"{prefix}_{s}"[:50]


def write_scopes(cfg, teams):
    if not SCOPES_SQL:
        return
    import secrets
    try:
        with open(SCOPE_CREDS, encoding="utf-8") as f:
            creds = json.load(f)
    except FileNotFoundError:
        creds = {}
    scopes = []
    for org in cfg["orgs"]:
        org_team_ids = [teams[t["alias"]] for t in org.get("teams", [])]
        scopes.append(("org", org["alias"], scope_name(org["alias"], "org"), org_team_ids))
        for t in org.get("teams", []):
            scopes.append(("team", t["alias"], scope_name(t["alias"], "team"), [teams[t["alias"]]]))

    out = ["-- GENERATED by bootstrap/bootstrap.py from tenants.json - do not edit. Contains passwords: git-ignored.",
           "-- Applied by service db-setup. One read-only role + one schema of filtered views per org and team.", ""]
    for kind, alias, name, ids in scopes:
        role, schema = f"rpt_{name}", f"scope_{name}"
        pw = creds.get(name, {}).get("password") or secrets.token_hex(16)
        creds[name] = {"kind": kind, "alias": alias, "role": role, "schema": schema, "password": pw, "team_ids": ids}
        idlist = ", ".join(f"'{i}'" for i in ids)
        out += [
            f"-- {kind} {alias}",
            f"SELECT 'CREATE ROLE {role} LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') \\gexec",
            f"ALTER ROLE {role} WITH LOGIN PASSWORD '{pw}';",
            f"ALTER ROLE {role} SET search_path = {schema};",
            f"ALTER ROLE {role} SET default_transaction_read_only = on;",
            f"ALTER ROLE {role} SET statement_timeout = '30s';",
            f"GRANT CONNECT ON DATABASE {DB_NAME} TO {role};",
            f"CREATE SCHEMA IF NOT EXISTS {schema};",
            f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;",
            f"CREATE OR REPLACE VIEW {schema}.teams AS SELECT * FROM reporting.teams WHERE team_id IN ({idlist});",
            f"CREATE OR REPLACE VIEW {schema}.requests AS SELECT * FROM reporting.requests WHERE team_id IN ({idlist});",
            f"CREATE OR REPLACE VIEW {schema}.daily_team_spend AS SELECT * FROM reporting.daily_team_spend WHERE team_id IN ({idlist});",
            f"CREATE OR REPLACE VIEW {schema}.model_names AS SELECT * FROM reporting.model_names;",
            f"CREATE OR REPLACE VIEW {schema}.daily_tag_spend AS",
            "  SELECT r.start_time::date AS day, tag, SUM(r.spend) AS spend, COUNT(*)::bigint AS requests,",
            "         SUM(r.prompt_tokens)::bigint AS prompt_tokens, SUM(r.completion_tokens)::bigint AS completion_tokens",
            f"  FROM {schema}.requests r",
            "  CROSS JOIN LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(r.tags::jsonb) = 'array' THEN r.tags::jsonb ELSE '[]'::jsonb END) AS tag",
            "  GROUP BY 1, 2;",
            f"GRANT USAGE ON SCHEMA {schema} TO {role};",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role};",
            "",
        ]
    with open(SCOPES_SQL, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))
    with open(SCOPE_CREDS, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    print(f"\nscoped read access: {len(scopes)} roles ({', '.join(c['role'] for c in creds.values())})")
    print("  -> postgres/generated/30-scopes.sql (run: docker compose up db-setup)")


def main():
    if "--discover" in sys.argv:
        if not MASTER:
            sys.exit("LITELLM_MASTER_KEY is not set")
        discover(sys.argv[sys.argv.index("--discover") + 1])
        return
    with open(TENANTS, encoding="utf-8") as f:
        cfg = json.load(f)
    errors = validate(cfg)
    if errors:
        print("Naming check failed:\n  - " + "\n  - ".join(errors))
        sys.exit(2)
    print(f"Naming check passed ({MODE} mode, {os.path.basename(TENANTS)}).")
    if "--check" in sys.argv:
        return
    if not MASTER:
        sys.exit("LITELLM_MASTER_KEY is not set")

    orgs_supported = True
    try:
        orgs = {o.get("organization_alias"): o.get("organization_id") for o in list_orgs()}
    except ApiError as e:
        print(f"  ! cannot list organizations ({e}); continuing without orgs")
        orgs, orgs_supported = {}, False
    live_teams = list_teams()
    teams = {t.get("team_alias"): t.get("team_id") for t in live_teams}
    by_id = {t.get("team_id"): t for t in live_teams}
    key_aliases = list_key_aliases()

    print("infra")
    ensure_scraper_key(key_aliases)

    new_keys = []
    for org in cfg["orgs"]:
        print(f"{org['alias']}")
        org_id = None
        if MODE == "manage":
            org_id, orgs_supported = ensure_org(org, orgs, orgs_supported)
        for team in org.get("teams", []):
            team["_org"] = org["alias"]
            if MODE == "observe":
                team_id = observe_team(team, teams, by_id)
            else:
                team_id = ensure_team(team, org_id, teams)
            teams[team["alias"]] = team_id
            set_team_callback(team_id, team["alias"], bool(team.get("sensitive")))
            for key in team.get("keys", []):
                created = ensure_key(key, team, team_id, key_aliases)
                if created:
                    new_keys.append(created)

    write_scopes(cfg, teams)

    if new_keys:
        previous = []
        if os.path.exists(OUT):
            with open(OUT, encoding="utf-8") as f:
                previous = json.load(f)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(previous + new_keys, f, indent=2)
        print(f"\n{len(new_keys)} new key(s) written to bootstrap/{OUT} - hand them to the app owners and keep the file secret.")
    else:
        print("\nNothing new to create.")


if __name__ == "__main__":
    main()

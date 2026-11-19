"""Langfuse OSS admin helper: the calls the Langfuse UI itself makes (stdlib only).

Free self-hosted Langfuse has no public API for creating organizations, projects or project keys
(that is the Enterprise "admin API"). This module signs in as the Langfuse admin user and calls the
same internal endpoints the web UI uses (tRPC). They are NOT a stable public API: after a Langfuse
upgrade, re-run scripts/apply-tenants.sh + scripts/verify.sh, and if a call fails, do the step in the
UI instead (see docs/BUILD_PLAN.md, phase 3 runbook).
"""

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request


class LangfuseAdmin:
    def __init__(self, base, email, password):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._login(email, password)

    def _req(self, method, path, data=None, headers=None, form=False):
        body = None
        h = dict(headers or {})
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with self.op.open(r, timeout=60) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def _login(self, email, password):
        s, csrf = self._req("GET", "/api/auth/csrf")
        if s != 200:
            raise RuntimeError(f"Langfuse csrf failed: HTTP {s}")
        s, b = self._req("POST", "/api/auth/callback/credentials",
                         {"email": email, "password": password, "csrfToken": csrf["csrfToken"],
                          "callbackUrl": self.base, "json": "true"}, form=True)
        if not any(c.name.endswith("session-token") for c in self.jar):
            raise RuntimeError(f"Langfuse login failed (HTTP {s}): {str(b)[:200]}")

    def session(self):
        s, b = self._req("GET", "/api/auth/session")
        return b if s == 200 else {}

    def orgs(self):
        """[{id, name, projects: [{id, name}]}] visible to the admin user."""
        return (self.session().get("user") or {}).get("organizations", [])

    def call(self, procedure, payload):
        s, b = self._req("POST", f"/api/trpc/{procedure}", {"json": payload})
        if s != 200:
            msg = b.get("error", {}).get("json", {}).get("message") if isinstance(b, dict) else str(b)[:200]
            raise RuntimeError(f"{procedure}: HTTP {s}: {msg}")
        return b.get("result", {}).get("data", {}).get("json")

    # --- the operations bootstrap needs
    def ensure_org(self, name):
        for o in self.orgs():
            if o["name"] == name:
                return o["id"], False
        res = self.call("organizations.create", {"name": name})
        return res["id"], True

    def ensure_project(self, org_id, name):
        for o in self.orgs():
            if o["id"] == org_id:
                for p in o.get("projects", []):
                    if p["name"] == name:
                        return p["id"], False
        res = self.call("projects.create", {"orgId": org_id, "name": name})
        return res["id"], True

    def create_project_key(self, project_id, note):
        res = self.call("projectApiKeys.create", {"projectId": project_id, "note": note})
        return res["publicKey"], res["secretKey"]

    def add_member(self, org_id, email, role="VIEWER"):
        """Adds an existing user, or creates a pending invitation (accepted when the user first signs in)."""
        try:
            self.call("members.create", {"orgId": org_id, "email": email, "orgRole": role})
            return "added"
        except RuntimeError as e:
            if "already" in str(e).lower():
                return "already a member"
            raise

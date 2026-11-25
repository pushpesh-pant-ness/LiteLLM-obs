"""Langfuse OSS admin client: signs in as the admin user and uses the endpoints the Langfuse web UI
itself calls (stdlib only).

Self-hosted Langfuse without an Enterprise licence has no public API for creating projects or
project API keys, so this is the only automated way. These internal (tRPC) endpoints are not a
stable public API: if a Langfuse upgrade breaks them, `run provision` says so - create the team's
project + API key in the UI and put them in .state/state.json (README > Troubleshooting).
"""

import base64
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class LangfuseAdmin:
    def __init__(self, base, email, password, wait_seconds=180):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._wait(wait_seconds)
        self._login(email, password)

    def _req(self, method, path, data=None, form=False, headers=None):
        body, h = None, dict(headers or {})
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
                return resp.status, (json.loads(raw) if raw.strip()[:1] in ("{", "[") else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def _wait(self, seconds):
        end = time.time() + seconds
        while True:
            try:
                s, _ = self._req("GET", "/api/public/health")
                if s == 200:
                    return
            except (urllib.error.URLError, OSError):
                pass
            if time.time() > end:
                raise RuntimeError(f"Langfuse at {self.base} not healthy after {seconds}s")
            time.sleep(5)

    def _login(self, email, password):
        s, csrf = self._req("GET", "/api/auth/csrf")
        if s != 200 or not isinstance(csrf, dict):
            raise RuntimeError(f"Langfuse csrf failed: HTTP {s}")
        s, b = self._req("POST", "/api/auth/callback/credentials",
                         {"email": email, "password": password, "csrfToken": csrf["csrfToken"],
                          "callbackUrl": self.base, "json": "true"}, form=True)
        if not any(c.name.endswith("session-token") for c in self.jar):
            raise RuntimeError(f"Langfuse admin login failed (HTTP {s}) - is LANGFUSE_ADMIN_PASSWORD in .env "
                               "still the password of LANGFUSE_ADMIN_EMAIL?")

    def orgs(self):
        """[{id, name, projects: [{id, name}]}] visible to the admin user."""
        s, b = self._req("GET", "/api/auth/session")
        return ((b if s == 200 and isinstance(b, dict) else {}).get("user") or {}).get("organizations", [])

    def call(self, procedure, payload):
        s, b = self._req("POST", f"/api/trpc/{procedure}", {"json": payload})
        if s != 200:
            msg = b.get("error", {}).get("json", {}).get("message") if isinstance(b, dict) else str(b)[:200]
            raise RuntimeError(f"Langfuse {procedure}: HTTP {s}: {msg}")
        return b.get("result", {}).get("data", {}).get("json")

    def ensure_project(self, org_id, name):
        """(project_id, created) for project `name` in org `org_id`."""
        for o in self.orgs():
            if o["id"] == org_id:
                for p in o.get("projects", []):
                    if p["name"] == name:
                        return p["id"], False
                break
        else:
            raise RuntimeError(f"Langfuse org {org_id!r} not found for the admin user (headless init ran?)")
        res = self.call("projects.create", {"orgId": org_id, "name": name})
        return res["id"], True

    def create_project_key(self, project_id, note):
        res = self.call("projectApiKeys.create", {"projectId": project_id, "note": note})
        return res["publicKey"], res["secretKey"]


def project_of_key(base, public_key, secret_key):
    """Project id a project API key belongs to, or None if the key does not work."""
    if not public_key or not secret_key:
        return None
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    req = urllib.request.Request(base.rstrip("/") + "/api/public/projects", headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode()).get("data") or []
            return data[0]["id"] if data else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return None

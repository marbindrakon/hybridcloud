"""A small Keystone v3 over HTTP for the tests of eda/common/files/shc_openstack.py (and the roles that run it).

It is not a mock of the helper: the helper (and openstacksdk / keystoneauth for the site account's sign-in) talk HTTP
to it, and it enforces the Keystone behaviours the platform's jobs depend on, as proven on a site's Keystone
(the spike's rows in brackets):
  - a password set revokes every earlier token of the user, also tokens obtained with its application credentials
    [S1 "earlier token after the reset" 404; "a token obtained with the credential, after a later password reset"];
  - a token issued in the same second as a password set is refused on its first use (401) [S1, N1]; the "second" is
    `granularity` seconds long (tests may shorten it, the order of events is what counts);
  - a restricted application credential cannot create or delete credentials (403), cannot set a password (403),
    and lists its owner's credentials (200) [S3];
  - only the owner creates its application credentials (403 for anybody else, admin included); a domain manager
    cannot delete another user's credential (403) [S5, N3];
  - an application credential signs in only while its owner holds one of its roles on the project; a user's
    credentials go with the user [S2];
  - a domain manager may manage users, groups, projects of its domain and grant manager/member/reader where actor,
    project and token are of that domain; anything else 403 [S5]; it may not read the federation API (403) [N4];
  - `admin` (any scope) may do everything except create another user's credential.
Every request and its status is recorded in `log` (bodies without passwords or secrets: the test greps the helper's
output for the secrets, so the fake keeps no copy it could leak itself).
"""
import json
import secrets as _secrets
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GRANTABLE_BY_MANAGER = {"manager", "member", "reader"}
IMPLIED = {"admin": ["manager", "member", "reader"], "manager": ["member", "reader"], "member": ["reader"]}


def _id():
    return uuid.uuid4().hex


class Keystone:
    def __init__(self, granularity=1.0):
        self.g = granularity
        self.lock = threading.RLock()
        self.domains, self.projects, self.users, self.groups = {}, {}, {}, {}
        self.roles = {}
        self.assignments = set()          # (actor kind, actor id, scope kind, scope id, role id)
        self.memberships = set()          # (user id, group id)
        self.app_creds = {}               # id -> {user, project, name, secret, roles, unrestricted, ...}
        self.tokens = {}                  # token -> record
        self.idps, self.protocols, self.mappings = {}, {}, {}
        self.log = []
        self.faults = []                  # tracebacks of the fake's own errors (a test asserts there are none)
        self.fail_next = []               # [(method, path prefix, status, message)] answered once, in order
        self.on_request = None            # optional hook(method, path) run before a request is handled
        for r in ("admin", "manager", "member", "reader", "load-balancer_member"):
            rid = _id()
            self.roles[rid] = {"id": rid, "name": r, "domain_id": None}
        self.default = self.add_domain("Default", did="default")
        self.server = None

    # ---- set-up helpers -------------------------------------------------------------------------------------------
    def role(self, name):
        return next(r["id"] for r in self.roles.values() if r["name"] == name)

    def add_domain(self, name, did=None):
        did = did or _id()
        self.domains[did] = {"id": did, "name": name, "enabled": True, "description": ""}
        return did

    def add_project(self, name, domain_id, pid=None):
        pid = pid or _id()
        self.projects[pid] = {"id": pid, "name": name, "domain_id": domain_id, "description": "", "enabled": True}
        return pid

    def add_user(self, name, domain_id, password=None, description="", options=None):
        uid = _id()
        self.users[uid] = {"id": uid, "name": name, "domain_id": domain_id, "enabled": True,
                           "description": description, "options": dict(options or {}), "password": password,
                           "revoked_before": -1}
        return uid

    def add_group(self, name, domain_id, description=""):
        gid = _id()
        self.groups[gid] = {"id": gid, "name": name, "domain_id": domain_id, "description": description}
        return gid

    def grant(self, kind, actor, scope_kind, scope_id, role_name):
        self.assignments.add((kind, actor, scope_kind, scope_id, self.role(role_name)))

    def site_account(self, roles=("admin",), domain_scope=None, name="site-admin"):
        """A site account: a password user of the Default domain with roles on its own project (admin) or, for a
        domain manager, a user of the tenant domain with manager there. Returns (user id, password, project id)."""
        pw = _secrets.token_urlsafe(16)
        dom = domain_scope or self.default
        uid = self.add_user(name, dom, password=pw)
        pid = self.add_project(name + "-project", dom)
        for r in roles:
            self.grant("user", uid, "project", pid, r)
            if domain_scope:
                self.grant("user", uid, "domain", domain_scope, r)
        return uid, pw, pid

    def start(self):
        ks = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln) if ln else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                try:
                    status, out, headers = ks.handle(method, self.path, dict(self.headers), body)
                except Exception as e:  # noqa: BLE001 - a fault of the fake: a 500 the test sees, with the cause
                    import traceback
                    ks.faults.append(traceback.format_exc())
                    status, out, headers = 500, {"error": {"code": 500, "message": f"fake: {type(e).__name__}"}}, None
                data = b"" if out is None else json.dumps(out).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

            def do_PATCH(self):
                self._do("PATCH")

            def do_DELETE(self):
                self._do("DELETE")

            def do_HEAD(self):
                self._do("HEAD")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def clouds_yaml(self, path, user, password, project=None, domain="Default", region="RegionOne", v3=True):
        import yaml
        auth = {"auth_url": self.url + ("/v3" if v3 else ""), "username": user, "password": password,
                "user_domain_name": domain}
        if project:
            auth.update({"project_name": project, "project_domain_name": domain})
        with open(path, "w") as f:
            yaml.safe_dump({"clouds": {"openstack": {"auth": auth, "region_name": region, "interface": "public",
                                                     "identity_api_version": 3}}}, f)
        return path

    # ---- time ----------------------------------------------------------------------------------------------------
    def tick(self):
        return int(time.time() / self.g)

    # ---- request handling ------------------------------------------------------------------------------------------
    def handle(self, method, path, headers, body):
        with self.lock:
            if self.on_request:
                self.on_request(method, path)
            status, out, h = self._route(method, path, headers, body)
            self.log.append((method, path.split("?")[0], status))
            return status, out, h

    def _err(self, status, msg):
        return status, {"error": {"code": status, "message": msg, "title": "x"}}, None

    def _route(self, method, path, headers, body):
        u = urllib.parse.urlsplit(path)
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path.rstrip("/") or "/"
        for i, (m, prefix, st, msg) in enumerate(self.fail_next):
            if m == method and p.startswith(prefix):
                del self.fail_next[i]
                return self._err(st, msg)
        if method == "GET" and p == "/":
            return 300, {"versions": {"values": [self._version()]}}, None
        if method == "GET" and p == "/v3":
            return 200, {"version": self._version()}, None
        if p == "/v3/auth/tokens" and method == "POST":
            return self._auth(body or {})
        if method == "GET" and p.startswith("/v3/auth/OS-FEDERATION/identity_providers/") and p.endswith("/websso"):
            # the per-provider sign-in start: the web server sends the browser to the provider (no token needed)
            idp = self.idps.get(p.split("/")[5])
            if not idp:
                return self._err(404, "Could not find Identity Provider")
            return 302, None, {"Location": (idp.get("remote_ids") or ["https://idp.example.org"])[0] + "/auth"}
        tok = self._token(headers.get("X-Auth-Token"))
        if tok is None:
            return self._err(401, "The request you have made requires authentication.")
        if p == "/v3/auth/tokens":
            sub = self._token(headers.get("X-Subject-Token"))
            if method in ("GET", "HEAD"):
                return (200, {"token": sub["body"]}, None) if sub else self._err(404, "token not found")
            if method == "DELETE":
                self.tokens.pop(headers.get("X-Subject-Token"), None)
                return 204, None, None
        parts = p.split("/")[2:]  # after /v3
        return self._resource(method, parts, q, tok, body or {})

    def _version(self):
        return {"id": "v3.14", "status": "stable", "updated": "2020-04-07T00:00:00Z",
                "links": [{"rel": "self", "href": self.url + "/v3/"}],
                "media-types": [{"base": "application/json", "type": "application/vnd.openstack.identity-v3+json"}]}

    # ---- tokens --------------------------------------------------------------------------------------------------
    def effective_roles(self, uid, scope_kind, scope_id):
        groups = {g for (u, g) in self.memberships if u == uid}
        ids = {r for (k, a, sk, sid, r) in self.assignments if sk == scope_kind and sid == scope_id
               and ((k == "user" and a == uid) or (k == "group" and a in groups))}
        names = {self.roles[r]["name"] for r in ids}
        for n in list(names):
            names.update(IMPLIED.get(n, []))
        return names

    def _auth(self, body):
        ident = (body.get("auth") or {}).get("identity") or {}
        scope = (body.get("auth") or {}).get("scope")
        methods = ident.get("methods") or []
        cred = None
        if "password" in methods:
            pu = (ident.get("password") or {}).get("user") or {}
            user = self.users.get(pu.get("id")) if pu.get("id") else self._user_by_name(pu)
            if not user or not user["enabled"] or user["password"] is None or pu.get("password") != user["password"]:
                return self._err(401, "The request you have made requires authentication.")
        elif "application_credential" in methods:
            ac = ident.get("application_credential") or {}
            cred = self.app_creds.get(ac.get("id"))
            user = self.users.get(cred["user"]) if cred else None
            if not cred or not user or not user["enabled"] or ac.get("secret") != cred["secret"]:
                return self._err(401, "The request you have made requires authentication.")
            scope = {"project": {"id": cred["project"]}}
        else:
            return self._err(401, "unsupported method")
        rec = {"user": user["id"], "issued": self.tick(), "methods": methods, "cred": cred["id"] if cred else None}
        tb = {"methods": methods, "user": {"id": user["id"], "name": user["name"],
                                           "domain": {"id": user["domain_id"],
                                                      "name": self.domains[user["domain_id"]]["name"]}},
              "issued_at": "2026-01-01T00:00:00.000000Z", "expires_at": "2099-01-01T00:00:00.000000Z",
              "catalog": [{"type": "identity", "name": "keystone", "id": "ks",
                           "endpoints": [{"id": "e1", "interface": i, "region": "RegionOne", "region_id": "RegionOne",
                                          "url": self.url} for i in ("public", "internal", "admin")]}]}
        if scope and "project" in scope:
            sp = scope["project"]
            proj = self.projects.get(sp.get("id")) if sp.get("id") else self._by_name(self.projects, sp)
            if not proj:
                return self._err(401, "project not found")
            names = self.effective_roles(user["id"], "project", proj["id"])
            if cred:
                names = names & set(cred["roles"])
            if not names:
                return self._err(401, f"User {user['id']} has no access to project {proj['id']}")
            rec.update({"scope": ("project", proj["id"]), "roles": names})
            tb["project"] = {"id": proj["id"], "name": proj["name"],
                             "domain": {"id": proj["domain_id"], "name": self.domains[proj["domain_id"]]["name"]}}
        elif scope and "domain" in scope:
            sd = scope["domain"]
            dom = self.domains.get(sd.get("id")) if sd.get("id") else next(
                (d for d in self.domains.values() if d["name"] == sd.get("name")), None)
            if not dom:
                return self._err(401, "domain not found")
            names = self.effective_roles(user["id"], "domain", dom["id"])
            if not names:
                return self._err(401, f"User {user['id']} has no access to domain {dom['id']}")
            rec.update({"scope": ("domain", dom["id"]), "roles": names})
            tb["domain"] = {"id": dom["id"], "name": dom["name"]}
        else:
            rec.update({"scope": None, "roles": set()})
        if cred:
            tb["application_credential"] = {"id": cred["id"], "name": cred["name"],
                                            "restricted": not cred["unrestricted"]}
        tb["roles"] = [{"id": self.role(n), "name": n} for n in sorted(rec["roles"])]
        rec["body"] = tb
        token = "tok-" + _secrets.token_hex(16)
        self.tokens[token] = rec
        return 201, {"token": tb}, {"X-Subject-Token": token}

    def _token(self, t):
        rec = self.tokens.get(t) if t else None
        if not rec:
            return None
        user = self.users.get(rec["user"])
        if not user or not user["enabled"] or rec["issued"] <= user["revoked_before"]:
            return None
        if rec["cred"] and rec["cred"] not in self.app_creds:
            return None
        return rec

    def _user_by_name(self, pu):
        d = pu.get("domain") or {}
        did = d.get("id") or next((x["id"] for x in self.domains.values() if x["name"] == d.get("name")), None)
        return next((x for x in self.users.values() if x["name"] == pu.get("name") and x["domain_id"] == did), None)

    def _by_name(self, coll, spec):
        d = spec.get("domain") or {}
        did = d.get("id") or next((x["id"] for x in self.domains.values() if x["name"] == d.get("name")), None)
        return next((x for x in coll.values() if x["name"] == spec.get("name") and x["domain_id"] == did), None)

    # ---- policy --------------------------------------------------------------------------------------------------
    @staticmethod
    def is_admin(tok):
        return "admin" in tok["roles"]

    @staticmethod
    def manager_of(tok, domain_id):
        return tok["scope"] == ("domain", domain_id) and "manager" in tok["roles"]

    def _forbidden(self, what="identity"):
        return self._err(403, f"You are not authorized to perform the requested action: {what}.")

    # ---- resources -----------------------------------------------------------------------------------------------
    def _resource(self, method, parts, q, tok, body):
        n = len(parts)
        head = parts[0] if parts else ""
        if head == "roles":
            if n == 1 and method == "GET":
                rs = [r for r in self.roles.values() if "name" not in q or r["name"] == q["name"]]
                return 200, {"roles": rs}, None
            if n == 2 and method == "GET":
                r = self.roles.get(parts[1])
                return (200, {"role": r}, None) if r else self._err(404, "Could not find role")
        if head == "domains":
            if n == 1 and method == "GET":
                ds = [d for d in self.domains.values() if "name" not in q or d["name"] == q["name"]]
                return 200, {"domains": ds}, None
            if n == 2 and method == "GET":
                d = self.domains.get(parts[1])
                return (200, {"domain": d}, None) if d else self._err(404, "Could not find domain")
        if head == "projects":
            return self._projects(method, parts, q, tok, body)
        if head == "users":
            return self._users(method, parts, q, tok, body)
        if head == "groups":
            return self._groups(method, parts, q, tok, body)
        if head == "role_assignments" and method == "GET":
            return self._role_assignments(q, tok)
        if head == "OS-FEDERATION":
            if not self.is_admin(tok):
                return self._forbidden("identity:get_identity_provider")
            if parts[1] == "identity_providers" and n == 3:
                ip = self.idps.get(parts[2])
                return (200, {"identity_provider": ip}, None) if ip else self._err(404, "Could not find Identity Provider")
            if parts[1] == "identity_providers" and n == 5 and parts[3] == "protocols":
                pr = self.protocols.get((parts[2], parts[4]))
                return (200, {"protocol": pr}, None) if pr else self._err(404, "Could not find federated protocol")
            if parts[1] == "mappings" and n == 3:
                mp = self.mappings.get(parts[2])
                return (200, {"mapping": mp}, None) if mp else self._err(404, "Could not find mapping")
        return self._err(404, "The resource could not be found.")

    def _may_domain(self, tok, domain_id):
        return self.is_admin(tok) or self.manager_of(tok, domain_id)

    def _projects(self, method, parts, q, tok, body):
        n = len(parts)
        if n == 1 and method == "GET":
            did = q.get("domain_id")
            if not (self.is_admin(tok) or (did and self.manager_of(tok, did))):
                return self._forbidden("identity:list_projects")
            ps = [p for p in self.projects.values() if (not did or p["domain_id"] == did)
                  and ("name" not in q or p["name"] == q["name"])]
            return 200, {"projects": ps}, None
        if n == 1 and method == "POST":
            p = body.get("project") or {}
            if not self._may_domain(tok, p.get("domain_id")):
                return self._forbidden("identity:create_project")
            if any(x["name"] == p.get("name") and x["domain_id"] == p.get("domain_id") for x in self.projects.values()):
                return self._err(409, "Conflict occurred attempting to store project")
            pid = self.add_project(p["name"], p["domain_id"])
            self.projects[pid]["description"] = p.get("description", "")
            return 201, {"project": self.projects[pid]}, None
        proj = self.projects.get(parts[1]) if n >= 2 else None
        if n == 2 and method == "GET":
            if not proj:
                return self._err(404, "Could not find project")
            return 200, {"project": proj}, None
        if n == 2 and method == "DELETE":
            if not proj:
                return self._err(404, "Could not find project")
            if not self._may_domain(tok, proj["domain_id"]):
                return self._forbidden("identity:delete_project")
            del self.projects[proj["id"]]
            self.assignments = {a for a in self.assignments if not (a[2] == "project" and a[3] == proj["id"])}
            return 204, None, None
        if n == 5 and parts[2] in ("users", "groups") and parts[3] and parts[4] == "roles" or n == 6 and parts[4] == "roles":
            if not proj:
                return self._err(404, "Could not find project")
            kind = parts[2][:-1]
            actor = (self.users if kind == "user" else self.groups).get(parts[3])
            if not actor:
                return self._err(404, f"Could not find {kind}")
            if n == 5 and method == "GET":
                rs = [self.roles[r] for (k, a, sk, sid, r) in self.assignments
                      if k == kind and a == actor["id"] and sk == "project" and sid == proj["id"]]
                return 200, {"roles": rs}, None
            role = self.roles.get(parts[5]) if n == 6 else None
            if not role:
                return self._err(404, "Could not find role")
            key = (kind, actor["id"], "project", proj["id"], role["id"])
            allowed = self.is_admin(tok) or (
                self.manager_of(tok, proj["domain_id"]) and actor["domain_id"] == proj["domain_id"]
                and role["name"] in GRANTABLE_BY_MANAGER)
            if method in ("HEAD", "GET"):
                return (204, None, None) if key in self.assignments else self._err(404, "not found")
            if not allowed:
                return self._forbidden("identity:create_grant" if method == "PUT" else "identity:revoke_grant")
            if method == "PUT":
                self.assignments.add(key)
                return 204, None, None
            if method == "DELETE":
                if key not in self.assignments:
                    return self._err(404, "Could not find role assignment")
                self.assignments.discard(key)
                return 204, None, None
        return self._err(404, "The resource could not be found.")

    def _users(self, method, parts, q, tok, body):
        n = len(parts)
        if n == 1 and method == "GET":
            did = q.get("domain_id")
            if not (self.is_admin(tok) or (did and self.manager_of(tok, did))):
                return self._forbidden("identity:list_users")
            us = [self._user_view(u) for u in self.users.values() if (not did or u["domain_id"] == did)
                  and ("name" not in q or u["name"] == q["name"])]
            return 200, {"users": us}, None
        if n == 1 and method == "POST":
            u = body.get("user") or {}
            if not self._may_domain(tok, u.get("domain_id")):
                return self._forbidden("identity:create_user")
            if any(x["name"] == u.get("name") and x["domain_id"] == u.get("domain_id") for x in self.users.values()):
                return self._err(409, "Conflict occurred attempting to store user - Duplicate entry")
            uid = self.add_user(u["name"], u["domain_id"], password=u.get("password"),
                                description=u.get("description", ""), options=u.get("options"))
            self.users[uid]["enabled"] = u.get("enabled", True)
            return 201, {"user": self._user_view(self.users[uid])}, None
        user = self.users.get(parts[1]) if n >= 2 else None
        if n >= 2 and not user:
            return self._err(404, f"Could not find user: {parts[1]}")
        own = tok["user"] == (user or {}).get("id")
        if n == 2 and method == "GET":
            if not (own or self._may_domain(tok, user["domain_id"])):
                return self._forbidden("identity:get_user")
            return 200, {"user": self._user_view(user)}, None
        if n == 2 and method == "PATCH":
            if tok["cred"] or not self._may_domain(tok, user["domain_id"]):
                return self._forbidden("identity:update_user")
            u = body.get("user") or {}
            if "password" in u:
                user["password"] = u["password"]
                user["revoked_before"] = self.tick()
            if "options" in u:
                user["options"].update(u["options"] or {})
            for k in ("description", "enabled", "name"):
                if k in u:
                    user[k] = u[k]
            return 200, {"user": self._user_view(user)}, None
        if n == 2 and method == "DELETE":
            if not self._may_domain(tok, user["domain_id"]):
                return self._forbidden("identity:delete_user")
            del self.users[user["id"]]
            self.app_creds = {k: v for k, v in self.app_creds.items() if v["user"] != user["id"]}
            self.assignments = {a for a in self.assignments if not (a[0] == "user" and a[1] == user["id"])}
            self.memberships = {m for m in self.memberships if m[0] != user["id"]}
            return 204, None, None
        if n == 3 and parts[2] == "projects" and method == "GET":
            if not (own or self.is_admin(tok)):
                return self._forbidden("identity:list_user_projects")
            ps = [self.projects[sid] for (k, a, sk, sid, r) in self.assignments
                  if k == "user" and a == user["id"] and sk == "project" and sid in self.projects]
            return 200, {"projects": ps}, None
        if parts[2] == "password" and method == "POST":
            if user["options"].get("lock_password"):
                return self._err(400, "You cannot change your password at this time due to password policy")
            return self._err(400, "not supported here")
        if parts[2] == "application_credentials":
            return self._app_creds(method, parts, q, tok, body, user, own)
        return self._err(404, "The resource could not be found.")

    def _user_view(self, u):
        return {k: v for k, v in u.items() if k not in ("password", "revoked_before")}

    def _app_creds(self, method, parts, q, tok, body, user, own):
        n = len(parts)
        mine = [c for c in self.app_creds.values() if c["user"] == user["id"]]
        if n == 3 and method == "GET":
            if not (own or self.is_admin(tok)):
                return self._forbidden("identity:list_application_credentials")
            out = [self._cred_view(c) for c in mine if "name" not in q or c["name"] == q["name"]]
            return 200, {"application_credentials": out}, None
        if n == 3 and method == "POST":
            if not own:
                return self._forbidden("Cannot create an application credential for another user")
            if tok["cred"] and not self.app_creds[tok["cred"]]["unrestricted"]:
                return self._forbidden("Using method 'application_credential' is not allowed for managing "
                                       "additional application credentials")
            if not tok["scope"] or tok["scope"][0] != "project":
                return self._err(401, "a project-scoped token is required")
            c = body.get("application_credential") or {}
            roles = [r.get("name") for r in (c.get("roles") or [])] or sorted(tok["roles"])
            if not set(roles) <= set(tok["roles"]):
                return self._err(400, "the requested roles are not all held by the user on the project")
            if any(x["name"] == c.get("name") for x in mine):
                return self._err(409, "Conflict occurred attempting to store application_credential - Duplicate")
            cid = _id()
            rec = {"id": cid, "user": user["id"], "project": tok["scope"][1], "name": c.get("name"),
                   "description": c.get("description"), "secret": _secrets.token_urlsafe(48),
                   "roles": roles, "unrestricted": bool(c.get("unrestricted", False)),
                   "expires_at": c.get("expires_at"), "created_at": "2026-11-14T00:00:00.000000"}
            self.app_creds[cid] = rec
            return 201, {"application_credential": dict(self._cred_view(rec), secret=rec["secret"])}, None
        if n == 4:
            c = self.app_creds.get(parts[3])
            if not c or c["user"] != user["id"]:
                return self._err(404, "Could not find Application Credential")
            if method == "GET":
                if not (own or self.is_admin(tok)):
                    return self._forbidden("identity:get_application_credential")
                return 200, {"application_credential": self._cred_view(c)}, None
            if method == "DELETE":
                if own and tok["cred"] and not self.app_creds[tok["cred"]]["unrestricted"]:
                    return self._forbidden("Using method 'application_credential' is not allowed for managing "
                                           "additional application credentials")
                if not (own or self.is_admin(tok)):
                    return self._forbidden("identity:delete_application_credential")
                del self.app_creds[c["id"]]
                return 204, None, None
        return self._err(404, "The resource could not be found.")

    def _cred_view(self, c):
        return {"id": c["id"], "name": c["name"], "description": c["description"], "project_id": c["project"],
                "roles": [{"id": self.role(r), "name": r} for r in c["roles"]], "unrestricted": c["unrestricted"],
                "expires_at": c["expires_at"], "user_id": c["user"], "created_at": c["created_at"]}

    def _groups(self, method, parts, q, tok, body):
        n = len(parts)
        if n == 1 and method == "GET":
            did = q.get("domain_id")
            if not (self.is_admin(tok) or (did and self.manager_of(tok, did))):
                return self._forbidden("identity:list_groups")
            gs = [g for g in self.groups.values() if (not did or g["domain_id"] == did)
                  and ("name" not in q or g["name"] == q["name"])]
            return 200, {"groups": gs}, None
        if n == 1 and method == "POST":
            g = body.get("group") or {}
            if not self._may_domain(tok, g.get("domain_id")):
                return self._forbidden("identity:create_group")
            if any(x["name"] == g.get("name") and x["domain_id"] == g.get("domain_id") for x in self.groups.values()):
                return self._err(409, "Conflict occurred attempting to store group - Duplicate entry")
            gid = self.add_group(g["name"], g["domain_id"], g.get("description", ""))
            return 201, {"group": self.groups[gid]}, None
        grp = self.groups.get(parts[1]) if n >= 2 else None
        if n == 2 and not grp:
            return self._err(404, f"Could not find group: {parts[1]}")
        if n == 2 and method == "GET":
            if not self._may_domain(tok, grp["domain_id"]):
                return self._forbidden("identity:get_group")
            return 200, {"group": grp}, None
        if n == 2 and method == "DELETE":
            if not self._may_domain(tok, grp["domain_id"]):
                return self._forbidden("identity:delete_group")
            del self.groups[grp["id"]]
            self.assignments = {a for a in self.assignments if not (a[0] == "group" and a[1] == grp["id"])}
            self.memberships = {m for m in self.memberships if m[1] != grp["id"]}
            return 204, None, None
        return self._err(404, "The resource could not be found.")

    def _role_assignments(self, q, tok):
        pid = q.get("scope.project.id")
        if not self.is_admin(tok):
            ok = (pid and pid in self.projects and self.manager_of(tok, self.projects[pid]["domain_id"])) or (
                q.get("user.id") == tok["user"]) or (
                q.get("group.id") in self.groups and self.manager_of(tok, self.groups[q["group.id"]]["domain_id"]))
            if not ok:
                return self._forbidden("identity:list_role_assignments")
        out = []
        for (k, a, sk, sid, r) in sorted(self.assignments):
            if q.get("user.id") and not (k == "user" and a == q["user.id"]):
                continue
            if q.get("group.id") and not (k == "group" and a == q["group.id"]):
                continue
            if pid and not (sk == "project" and sid == pid):
                continue
            e = {k: {"id": a}, "role": {"id": r}, "scope": {sk: {"id": sid}}}
            if q.get("include_names"):
                e["role"]["name"] = self.roles[r]["name"]
                coll = self.projects if sk == "project" else self.domains
                if sid in coll:
                    e["scope"][sk]["name"] = coll[sid]["name"]
            out.append(e)
        return 200, {"role_assignments": out}, None

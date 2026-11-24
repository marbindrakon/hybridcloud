"""fake_keycloak.Keycloak with what the sign-in of a tenant's cluster needs besides client roles: a client's full
representation (its protocol mappers and browser flow override), protocol mappers, composite client roles, a third
automation client of purpose flows (realm-management manage-realm: only its token may touch authentication flows, and
it may touch nothing else), authentication flows with sub-flows, executions and their configurations, as the
identity provider's admin API answers them (services/.../admin/AuthenticationManagementResource.java); and users in
groups.

`signs_in(user, client_id)` and `groups_claim(user, client_id)` say what the identity provider would do for a person
at the client, by the rules read in its source: a conditional sub-flow runs when every one of its conditions matches
and is skipped when one does not or when it has none (DefaultAuthenticationFlow.isConditionalSubflowDisabled);
"Condition - User Role" matches when negate != the person holds the role, and never when the role does not exist
(ConditionalRoleAuthenticator.matchCondition); a person holds a role through a group of theirs or a parent group and
through composites (RoleUtils.hasRole, hasRoleFromGroup); the client-role mapper puts the person's roles of that
client, composites expanded, into the claim."""
import json
import secrets
import uuid

import fake_keycloak

PROVIDERS = {"auth-cookie", "identity-provider-redirector", "auth-username-password-form", "conditional-user-configured",
             "auth-otp-form", "conditional-user-role", "deny-access-authenticator", "auth-spnego"}
CONDITIONS = {"conditional-user-configured", "conditional-user-role"}


class Keycloak(fake_keycloak.Keycloak):
    def __init__(self, realm="sovereign-tenants"):
        super().__init__(realm)
        self.automation["flows"] = ("kc-flows", secrets.token_urlsafe(12))
        self.purpose = {}        # token -> purpose
        self.flows = {}          # id -> {id, alias, description, providerId, topLevel, builtIn}
        self.executions = {}     # id -> {id, parent, flowId?, provider?, requirement, priority, config?}
        self.configs = {}        # id -> {id, alias, config}
        self.users = {}          # name -> [group paths]

    def vault_entry(self):
        out = super().vault_entry()
        out.update({"flowsClientId": self.automation["flows"][0], "flowsClientSecret": self.automation["flows"][1]})
        return out

    def add_client(self, client_id, mappers=None):
        cid = super().add_client(client_id)
        self.clients[client_id].update({"protocolMappers": list(mappers or []), "authenticationFlowBindingOverrides": {},
                                        "enabled": True})
        return cid

    def add_user(self, name, *groups):
        self.users[name] = [g.strip("/") for g in groups]

    # ── what the identity provider would do ──────────────────────────────────────────────────────────────────────
    def _client_by_id(self, cid):
        return next((c for c in self.clients.values() if c["id"] == cid), None)

    def _role_by_id(self, rid):
        for c in self.clients.values():
            for r in c["roles"].values():
                if r["id"] == rid:
                    return c, r
        return None, None

    def _expand(self, client, names):
        seen, todo = set(), list(names)
        while todo:
            n = todo.pop()
            if n in seen or n not in client["roles"]:
                continue
            seen.add(n)
            for rid in client["roles"][n].get("composites", []):
                _, r = self._role_by_id(rid)
                if r is not None:
                    todo.append(r["name"])
        return seen

    def held(self, user, client_id):
        client = self.clients[client_id]
        direct = set()
        for path in self.users[user]:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):           # the group and its parents
                g = self.groups.get("/".join(parts[:i]))
                if g:
                    direct.update(g["mappings"].get(client["id"], []))
        return self._expand(client, direct)

    def groups_claim(self, user, client_id):
        client = self.clients[client_id]
        claim = set()
        for m in client["protocolMappers"]:
            cfg = m.get("config") or {}
            if cfg.get("claim.name") != "groups":
                continue
            if m.get("protocolMapper") == "oidc-usermodel-client-role-mapper":
                claim |= self.held(user, cfg.get("usermodel.clientRoleMapping.clientId"))
            elif m.get("protocolMapper") == "oidc-group-membership-mapper":
                full = cfg.get("full.path") == "true"
                claim |= {("/" + p) if full else p.split("/")[-1] for p in self.users[user]}
        return sorted(claim)

    def _children(self, flow_id):
        return sorted((e for e in self.executions.values() if e["parent"] == flow_id), key=lambda e: e["priority"])

    def _condition(self, e, user):
        cfg = (self.configs.get(e.get("config")) or {}).get("config") or {}
        if e["provider"] == "conditional-user-role":
            want = cfg.get("condUserRole", "")
            for cid, c in self.clients.items():        # <clientId>.<role>: the first dot whose head is a client
                if want.startswith(cid + ".") and want[len(cid) + 1:] in c["roles"]:
                    return (cfg.get("negate") == "true") != (want[len(cid) + 1:] in self.held(user, cid))
            return False
        return False                                   # conditional-user-configured: no one-time code set up

    def _denies(self, flow_id, user):
        """True when the flow would end in Deny Access for this person (authenticating itself always succeeds)."""
        for e in self._children(flow_id):
            if e["requirement"] == "DISABLED":
                continue
            if "flowId" in e:
                if e["requirement"] == "CONDITIONAL":
                    conds = [c for c in self._children(e["flowId"]) if c.get("provider") in CONDITIONS
                             and c["requirement"] != "DISABLED"]
                    if not conds or not all(self._condition(c, user) for c in conds):
                        continue
                if e["requirement"] in ("REQUIRED", "CONDITIONAL") and self._denies(e["flowId"], user):
                    return True
            elif e["provider"] == "deny-access-authenticator" and e["requirement"] == "REQUIRED":
                return True
        return False

    def signs_in(self, user, client_id):
        client = self.clients[client_id]
        if not client.get("enabled", True):
            return False
        bound = (client.get("authenticationFlowBindingOverrides") or {}).get("browser")
        if not bound:
            return True                                 # the realm's browser flow: anyone of the realm
        return not self._denies(bound, user)

    def deny_message(self, client_id):
        bound = self.clients[client_id]["authenticationFlowBindingOverrides"].get("browser")
        for e in self.executions.values():
            if e.get("provider") == "deny-access-authenticator" and self._top(e) == bound:
                return (self.configs.get(e.get("config")) or {}).get("config", {}).get("denyErrorMessage")
        return None

    def _top(self, e):
        parent = e["parent"]
        while True:
            up = next((x for x in self.executions.values() if x.get("flowId") == parent), None)
            if up is None:
                return parent
            parent = up["parent"]

    def flow(self, alias):
        return next((f for f in self.flows.values() if f["alias"] == alias), None)

    def add_flow(self, alias, description=""):
        fid = uuid.uuid4().hex
        self.flows[fid] = {"id": fid, "alias": alias, "description": description, "providerId": "basic-flow",
                           "topLevel": True, "builtIn": False}
        return fid

    # ── the admin API ────────────────────────────────────────────────────────────────────────────────────────────
    def handle(self, method, path, headers, raw):
        if self._ours(method, path):
            return self._handle(method, path, headers, raw)
        if self.purpose.get(headers.get("Authorization", "")[7:]) == "flows":
            return 403, {"error": "HTTP 403 Forbidden"}      # manage-realm alone does not manage clients or groups
        return super().handle(method, path, headers, raw)

    def _ours(self, method, path):
        import urllib.parse
        p = urllib.parse.urlsplit(path).path.strip("/").split("/")
        rest = p[3:] if p[:3] == ["admin", "realms", self.realm] else []
        if p[-3:] == ["protocol", "openid-connect", "token"]:
            return True
        if rest[:1] == ["authentication"]:
            return True
        if rest[:1] == ["clients"] and len(rest) == 2:
            return True
        if rest[:1] == ["clients"] and len(rest) >= 3 and rest[2] == "protocol-mappers":
            return True
        if rest[:1] == ["clients"] and len(rest) == 5 and rest[2] == "roles" and rest[4] == "composites":
            return True
        return False

    def _handle(self, method, path, headers, raw):
        import urllib.parse
        u = urllib.parse.urlsplit(path)
        p = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/")]
        if p[-3:] == ["protocol", "openid-connect", "token"]:
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            for purpose, cred in self.automation.items():
                if (form.get("client_id"), form.get("client_secret")) == cred:
                    tok = "kc-" + secrets.token_hex(12)
                    self.tokens.add(tok)
                    self.purpose[tok] = purpose
                    return 200, {"access_token": tok, "token_type": "Bearer", "expires_in": 300}
            return 401, {"error": "invalid_client"}
        tok = headers.get("Authorization", "")[7:]
        if tok not in self.tokens:
            return 401, {"error": "HTTP 401 Unauthorized"}
        body = json.loads(raw) if raw else None
        rest = p[3:]
        if rest[0] == "authentication":
            if self.purpose.get(tok) != "flows":
                return 403, {"error": "HTTP 403 Forbidden"}
            return self._auth(method, rest[1:], body)
        if self.purpose.get(tok) == "flows":
            return 403, {"error": "HTTP 403 Forbidden"}
        client = self._client_by_id(rest[1])
        if client is None:
            return 404, {"error": "Could not find client"}
        if len(rest) == 2:
            if method == "GET":
                return 200, {k: v for k, v in client.items() if k != "roles"}
            if method == "PUT":
                for k in ("protocolMappers", "authenticationFlowBindingOverrides", "enabled", "redirectUris"):
                    if k in body:
                        client[k] = body[k]
                return 204, None
        if rest[2] == "protocol-mappers":
            if method == "POST" and len(rest) == 4:
                client["protocolMappers"].append(dict(body, id=uuid.uuid4().hex))
                return 201, None
            if method == "DELETE" and len(rest) == 5:
                client["protocolMappers"] = [m for m in client["protocolMappers"] if m["id"] != rest[4]]
                return 204, None
        if rest[2] == "roles" and rest[4] == "composites":
            role = client["roles"].get(rest[3])
            if role is None:
                return 404, {"error": "Could not find role"}
            comps = role.setdefault("composites", [])
            if method == "GET":
                return 200, [self._role_by_id(i)[1] for i in comps]
            if method == "POST":
                for r in body:
                    if r["id"] not in comps:
                        comps.append(r["id"])
                return 204, None
            if method == "DELETE":
                role["composites"] = [i for i in comps if i not in {r["id"] for r in body}]
                return 204, None
        return 404, {"error": "fake: no such resource"}

    def _auth(self, method, rest, body):
        by_alias = {f["alias"]: f for f in self.flows.values()}
        if rest == ["flows"] and method == "GET":
            return 200, list(self.flows.values())
        if rest == ["flows"] and method == "POST":
            if body["alias"] in by_alias:
                return 409, {"errorMessage": "Flow %s already exists" % body["alias"]}
            fid = self.add_flow(body["alias"], body.get("description", ""))
            return 201, None
        if len(rest) == 2 and rest[0] == "flows" and method == "GET":
            f = self.flows.get(rest[1])
            return (200, f) if f else (404, {"error": "Could not find flow"})
        if len(rest) == 2 and rest[0] == "flows" and method == "DELETE":
            f = self.flows.get(rest[1])
            if f is None:
                return 404, {"error": "Flow not found"}
            self._delete_flow(f["id"])
            return 204, None
        if len(rest) >= 3 and rest[0] == "flows" and rest[2] == "executions":
            parent = by_alias.get(rest[1])
            if parent is None:
                return 404 if method != "POST" else 400, {"error": "Parent flow doesn't exist"}
            if len(rest) == 3 and method == "GET":
                return 200, self._list(parent["id"], 0)
            if len(rest) == 3 and method == "PUT":
                e = self.executions.get(body["id"])
                if e is None:
                    return 404, {"error": "Illegal execution"}
                e["priority"] = body.get("priority", 0)
                e["requirement"] = body["requirement"]
                return 204, None
            if len(rest) == 4 and rest[3] == "flow" and method == "POST":
                if body["alias"] in by_alias:
                    return 409, {"errorMessage": "New flow alias name already exists"}
                fid = self.add_flow(body["alias"], body.get("description", ""))
                self.flows[fid]["topLevel"] = False
                self._add_exec(parent["id"], {"flowId": fid})
                return 201, None
            if len(rest) == 4 and rest[3] == "execution" and method == "POST":
                if body["provider"] not in PROVIDERS:
                    return 400, {"error": "No authentication provider found for id: " + body["provider"]}
                self._add_exec(parent["id"], {"provider": body["provider"]})
                return 201, None
        if len(rest) == 3 and rest[0] == "executions" and rest[2] == "config" and method == "POST":
            e = self.executions.get(rest[1])
            if e is None:
                return 404, {"error": "Illegal execution"}
            if any(c["alias"] == body["alias"] and c["id"] != e.get("config") for c in self.configs.values()):
                return 409, {"errorMessage": "Authentication execution configuration %s already exists" % body["alias"]}
            self.configs.pop(e.get("config"), None)
            cid = uuid.uuid4().hex
            self.configs[cid] = {"id": cid, "alias": body["alias"], "config": dict(body.get("config") or {})}
            e["config"] = cid
            return 201, None
        if len(rest) == 2 and rest[0] == "config" and method == "GET":
            c = self.configs.get(rest[1])
            return (200, c) if c else (404, {"error": "Could not find authenticator config"})
        return 404, {"error": "fake: no such resource"}

    def _add_exec(self, parent, extra):
        sib = self._children(parent)
        eid = uuid.uuid4().hex
        # a sub-flow starts disabled; an authenticator with one requirement choice gets it, else disabled
        self.executions[eid] = dict({"id": eid, "parent": parent, "requirement": "DISABLED",
                                     "priority": (sib[-1]["priority"] + 1) if sib else 0}, **extra)
        return eid

    def _list(self, flow_id, level):
        out = []
        for i, e in enumerate(self._children(flow_id)):
            rep = {"id": e["id"], "requirement": e["requirement"], "level": level, "index": i, "priority": e["priority"]}
            if "flowId" in e:
                f = self.flows[e["flowId"]]
                rep.update({"authenticationFlow": True, "displayName": f["alias"], "flowId": f["id"],
                            "description": f["description"]})
                out.append(rep)
                out.extend(self._list(f["id"], level + 1))
            else:
                rep.update({"providerId": e["provider"], "displayName": e["provider"]})
                if e.get("config"):
                    rep["authenticationConfig"] = e["config"]
                out.append(rep)
        return out

    def _delete_flow(self, fid):
        for e in [x for x in self.executions.values() if x["parent"] == fid]:
            if "flowId" in e:
                self._delete_flow(e["flowId"])
            self.configs.pop(e.get("config"), None)
            del self.executions[e["id"]]
        del self.flows[fid]

"""fake_keycloak.Keycloak with what a tenant's cluster's sign-in client needs besides client roles: a client's full
representation (attributes, grants, protocol mappers), protocol mappers, realm client scopes with their mappers and
a client's default and optional client scope links, as the identity provider's admin API answers them; and users in
groups, with realm roles.

`token_claims(user, client_id, path, scope)` says what the identity provider would put in the groups claim of a
token the client gets for a person, by the rules of its documentation: the client's own mappers, the mappers of its
default client scopes always, those of its optional client scopes when the authorization request names them in
`scope` (Server Administration Guide, "Link client scope with the client"); a client-role mapper puts the person's
roles of that client (through a group of theirs or a parent group); a realm-role mapper (the `microprofile-jwt`
scope's `groups`) the person's realm roles; a group-membership mapper the person's group names. Every way to finish a
sign-in (the browser flow, a reset of the password from the login page, a brokered provider) ends in a token of the
client made the same way; a password grant needs the client's direct access grants."""
import uuid

import fake_keycloak

PATHS = ("browser", "reset-credentials", "brokered", "password-grant")


class Keycloak(fake_keycloak.Keycloak):
    def __init__(self, realm="sovereign-tenants"):
        super().__init__(realm)
        self.users = {}          # name -> {groups: [paths], realmRoles: [names]}
        self.scopes = {}         # id -> {id, name, protocolMappers}
        self.realm_default = []  # names of the realm's default client scopes (linked to a client when it is made)
        self.realm_optional = []
        self.secrets = {}        # client uuid -> secret
        self.composites = {}     # realm role -> [(client id, client role)] it holds (made by a realm administrator)

    def add_client(self, client_id, mappers=None, default_scopes=(), optional_scopes=(), **fields):
        cid = super().add_client(client_id)
        self.clients[client_id].update(dict({
            "protocolMappers": list(mappers or []), "attributes": {}, "enabled": True, "redirectUris": [],
            "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
            "defaultScopes": [self.scope_id(n) for n in default_scopes],
            "optionalScopes": [self.scope_id(n) for n in optional_scopes]}, **fields))
        return cid

    def add_scope(self, name, mappers=()):
        sid = uuid.uuid4().hex
        self.scopes[sid] = {"id": sid, "name": name, "protocol": "openid-connect",
                            "protocolMappers": [dict(m, id=uuid.uuid4().hex) for m in mappers]}
        return sid

    def scope_id(self, name):
        return next(s["id"] for s in self.scopes.values() if s["name"] == name)

    def add_user(self, name, *groups, realm_roles=()):
        self.users[name] = {"groups": [g.strip("/") for g in groups], "realmRoles": list(realm_roles)}

    # ── what the identity provider would do ──────────────────────────────────────────────────────────────────────
    def held(self, user, client_id):
        client = self.clients[client_id]
        out = set()
        for path in self.users[user]["groups"]:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):           # the group and its parents
                g = self.groups.get("/".join(parts[:i]))
                if g:
                    out.update(g["mappings"].get(client["id"], []))
        for realm_role in self.users[user]["realmRoles"]:     # a realm role that is a composite of client roles
            out.update(r for cid, r in self.composites.get(realm_role, []) if cid == client_id)
        return {r for r in out if r in client["roles"]}

    def _mapped(self, user, mapper):
        cfg = mapper.get("config") or {}
        name = cfg.get("claim.name") or ""
        if not (name == "groups" or name.startswith("groups.")):
            return set()
        kind = mapper.get("protocolMapper")
        if kind == "oidc-usermodel-client-role-mapper":
            return self.held(user, cfg.get("usermodel.clientRoleMapping.clientId"))
        if kind == "oidc-usermodel-realm-role-mapper":
            return set(self.users[user]["realmRoles"])
        if kind == "oidc-group-membership-mapper":
            full = cfg.get("full.path") == "true"
            return {("/" + p) if full else p.split("/")[-1] for p in self.users[user]["groups"]}
        return {"<%s>" % kind}

    def token_claims(self, user, client_id, path="browser", scope=()):
        """The groups claim of the client's token for this person, or None when no token is given."""
        client = self.clients[client_id]
        if path not in PATHS:
            raise ValueError(path)
        if path == "password-grant" and not client.get("directAccessGrantsEnabled"):
            return None
        mappers = list(client["protocolMappers"])
        for sid in client["defaultScopes"]:
            mappers += self.scopes[sid]["protocolMappers"]
        for sid in client["optionalScopes"]:
            if self.scopes[sid]["name"] in scope:
                mappers += self.scopes[sid]["protocolMappers"]
        claim = set()
        for m in mappers:
            claim |= self._mapped(user, m)
        return sorted(claim)

    # ── the admin API ────────────────────────────────────────────────────────────────────────────────────────────
    def _rep(self, c):
        return {k: v for k, v in c.items() if k not in ("roles", "defaultScopes", "optionalScopes")}

    def handle(self, method, path, headers, raw):
        import json
        import urllib.parse
        u = urllib.parse.urlsplit(path)
        p = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/")]
        rest = p[3:] if p[:3] == ["admin", "realms", self.realm] else None
        ours = rest is not None and (
            rest == ["clients"]
            or (rest[:1] == ["clients"] and len(rest) == 2)
            or (rest[:1] == ["clients"] and len(rest) >= 3 and rest[2] in (
                "protocol-mappers", "default-client-scopes", "optional-client-scopes", "client-secret"))
            or rest[:1] == ["client-scopes"])
        if not ours:
            return super().handle(method, path, headers, raw)
        if headers.get("Authorization", "")[7:] not in self.tokens:
            return 401, {"error": "HTTP 401 Unauthorized"}
        body = json.loads(raw) if raw else None
        if rest == ["clients"] and method == "POST":
            if body["clientId"] in self.clients:
                return 409, {"errorMessage": "Client %s already exists" % body["clientId"]}
            fields = {k: v for k, v in body.items() if k != "clientId"}
            mappers = fields.pop("protocolMappers", [])
            self.add_client(body["clientId"], mappers=mappers, default_scopes=self.realm_default,
                            optional_scopes=self.realm_optional, **fields)
            return 201, None
        if rest == ["clients"]:
            q = dict(urllib.parse.parse_qsl(u.query))
            c = self.clients.get(q.get("clientId"))
            return 200, ([self._rep(c)] if c else [])
        if rest[0] == "client-scopes":
            s = self.scopes.get(rest[1]) if len(rest) == 2 else None
            return (200, s) if (s and method == "GET") else (404, {"error": "Could not find client scope"})
        client = next((c for c in self.clients.values() if c["id"] == rest[1]), None)
        if client is None:
            return 404, {"error": "Could not find client"}
        if len(rest) == 2:
            if method == "GET":
                return 200, self._rep(client)
            if method == "DELETE":
                del self.clients[client["clientId"]]
                return 204, None
            if method == "PUT":
                for k, v in body.items():
                    if k not in ("id", "clientId", "roles"):
                        client[k] = v
                return 204, None
        if rest[2] == "client-secret" and method == "GET":
            return 200, {"type": "secret", "value": self.secrets.setdefault(client["id"], "kc-secret-" + uuid.uuid4().hex)}
        if rest[2] == "protocol-mappers":
            if method == "POST" and len(rest) == 4:
                client["protocolMappers"].append(dict(body, id=uuid.uuid4().hex))
                return 201, None
            if method == "DELETE" and len(rest) == 5:
                client["protocolMappers"] = [m for m in client["protocolMappers"] if m["id"] != rest[4]]
                return 204, None
        if rest[2] in ("default-client-scopes", "optional-client-scopes"):
            key = "defaultScopes" if rest[2].startswith("default") else "optionalScopes"
            if method == "GET" and len(rest) == 3:
                return 200, [{"id": i, "name": self.scopes[i]["name"]} for i in client[key]]
            if method == "DELETE" and len(rest) == 4:
                client[key] = [i for i in client[key] if i != rest[3]]
                return 204, None
        return 404, {"error": "fake: no such resource"}

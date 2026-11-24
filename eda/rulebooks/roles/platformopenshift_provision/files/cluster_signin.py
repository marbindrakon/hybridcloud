#!/usr/bin/env python3
"""The groups claim of a tenant's OpenShift cluster: its sign-in client in the tenant realm of the identity provider.

The cluster's client gets one client role per access group of the cluster, <tenant>.<group>, mapped to the access
group's directory group (common/tasks/services_signin_groups.yml makes them, as for the services cluster and the
sites). This helper makes the client's groups claim hold the person's roles of this client and nothing else, so the
cluster sees the groups <tenant>.<group> and never another tenant's group of the same name:

* one client-role mapper of the client itself writes the claim `groups`; any other mapper of the client that writes
  `groups` (the older group-membership mapper sent the last part of every directory group of the person, whoever's
  tenant) goes;
* a client scope linked to the client whose mappers write `groups` is unlinked from this client. The identity
  provider applies a client's default client scopes to every token of the client and its optional client scopes when
  the authorization request asks for them in `scope` (Server Administration Guide, "Link client scope with the
  client"); the realm's default client scopes are linked to a client when it is made ("Realm default client scopes"),
  so a realm whose `microprofile-jwt` scope (a realm-role mapper of claim `groups`) is optional would otherwise let a
  person who adds `microprofile-jwt` to the authorization request put the realm's roles into the cluster's groups
  claim. The scope itself is not changed (other clients keep it).

A claim name `groups.<x>` writes a nested object under `groups`, so it counts as writing `groups` too.

Everyone of the realm can still authenticate at the client: who gets rights on the cluster is decided by the claim
and the cluster's bindings, not by the sign-in.

Reads one JSON request on stdin, prints one JSON answer on stdout. The token comes in the request (never printed).
  {"action": "ensure", "url", "realm", "clientsToken", "clientId"}
      -> {"ok": true, "changed": [what was changed]}
Exits non-zero with {"error": ...} when the identity provider answers what the request cannot handle.
"""
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

MAPPER_NAME = "groups"
MAPPER_TYPE = "oidc-usermodel-client-role-mapper"
CLAIM = "groups"


class Api:
    def __init__(self, url, realm):
        self.base = url.rstrip("/") + "/admin/realms/" + urllib.parse.quote(realm, safe="")
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, token, method, path, body=None, ok=(200, 201, 204), missing_ok=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
            if missing_ok and status == 404:
                return None
            if status not in ok:
                raise RuntimeError("%s %s answered %s" % (method, path.split("?")[0], status))
        if status not in ok:
            raise RuntimeError("%s %s answered %s" % (method, path.split("?")[0], status))
        return json.loads(raw) if raw else None


def q(value):
    return urllib.parse.quote(str(value), safe="")


def writes_groups(mapper):
    """True when the mapper writes the claim groups (or an object nested under it)."""
    name = str((mapper.get("config") or {}).get("claim.name") or "")
    return name == CLAIM or name.startswith(CLAIM + ".")


def find_client(api, token, client_id):
    found = [c for c in api.call(token, "GET", "/clients?clientId=" + q(client_id)) or []
             if c.get("clientId") == client_id]
    if not found:
        raise RuntimeError("the sign-in client %s does not exist" % client_id)
    return api.call(token, "GET", "/clients/" + q(found[0]["id"]))


def wanted_mapper(client_id):
    return {"usermodel.clientRoleMapping.clientId": client_id, "claim.name": CLAIM, "multivalued": "true",
            "jsonType.label": "String", "id.token.claim": "true", "access.token.claim": "true",
            "userinfo.token.claim": "true"}


def ensure_mapper(api, token, client, client_id, changed):
    """The groups claim: the person's roles of this client, nothing else."""
    want = wanted_mapper(client_id)
    cid = client["id"]
    good = False
    for m in client.get("protocolMappers") or []:
        cfg = m.get("config") or {}
        is_ours = m.get("protocolMapper") == MAPPER_TYPE and all(cfg.get(k) == v for k, v in want.items())
        if is_ours and not good:
            good = True
            continue
        if writes_groups(m) or m.get("name") == MAPPER_NAME:
            api.call(token, "DELETE", "/clients/%s/protocol-mappers/models/%s" % (q(cid), q(m["id"])))
            changed.append("mapper %s removed" % m.get("name"))
    if not good:
        api.call(token, "POST", "/clients/%s/protocol-mappers/models" % q(cid),
                 {"name": MAPPER_NAME, "protocol": "openid-connect", "protocolMapper": MAPPER_TYPE, "config": want})
        changed.append("groups mapper")


def scope_writes_groups(api, token, scope_id):
    scope = api.call(token, "GET", "/client-scopes/" + q(scope_id), missing_ok=True) or {}
    return any(writes_groups(m) for m in scope.get("protocolMappers") or [])


def linked_scopes(api, token, cid):
    """[(kind, scope)] of the client's default and optional client scopes."""
    return [(kind, s) for kind in ("default", "optional")
            for s in api.call(token, "GET", "/clients/%s/%s-client-scopes" % (q(cid), kind)) or []]


def ensure_scopes(api, token, client, changed):
    """No client scope linked to this client (default or optional) writes the groups claim."""
    cid = client["id"]
    for kind, s in linked_scopes(api, token, cid):
        if scope_writes_groups(api, token, s["id"]):
            api.call(token, "DELETE", "/clients/%s/%s-client-scopes/%s" % (q(cid), kind, q(s["id"])))
            changed.append("%s client scope %s unlinked" % (kind, s.get("name")))


def sources(api, token, client_id):
    """What writes the groups claim of the client's tokens now: [mapper or scope description]."""
    client = find_client(api, token, client_id)
    out = ["mapper %s (%s)" % (m.get("name"), m.get("protocolMapper"))
           for m in client.get("protocolMappers") or [] if writes_groups(m)]
    out += ["%s client scope %s" % (kind, s.get("name")) for kind, s in linked_scopes(api, token, client["id"])
            if scope_writes_groups(api, token, s["id"])]
    return out


def ensure(req):
    api = Api(req["url"], req["realm"])
    ct, client_id = req["clientsToken"], req["clientId"]
    changed = []
    client = find_client(api, ct, client_id)
    ensure_mapper(api, ct, client, client_id, changed)
    ensure_scopes(api, ct, client, changed)
    # read back: the client's own mapper is the claim's only source
    now = sources(api, ct, client_id)
    if now != ["mapper %s (%s)" % (MAPPER_NAME, MAPPER_TYPE)]:
        raise RuntimeError("the groups claim of the sign-in client has other sources: %s" % ", ".join(now or ["none"]))
    return {"ok": True, "changed": changed}


def main():
    try:
        req = json.load(sys.stdin)
        for key in ("url", "realm", "clientId"):
            if not str(req.get(key) or "").strip():
                raise ValueError("cluster_signin: %s is required" % key)
        action = req.get("action")
        if action != "ensure":
            raise ValueError("cluster_signin: unknown action %r" % (action,))
        out = ensure(req)
    except Exception as e:  # noqa: BLE001 - every failure becomes the job's log line (no token is in it)
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

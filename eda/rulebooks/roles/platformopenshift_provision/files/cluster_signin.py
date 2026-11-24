#!/usr/bin/env python3
"""Who may sign in to a tenant's OpenShift cluster: its sign-in client in the tenant realm of the identity provider.

The cluster's client gets one client role per access group of the cluster, <tenant>.<group>, mapped to the access
group's directory group (common/tasks/services_signin_groups.yml makes them, as for the services cluster and the
sites). This helper does the rest of the client's side:

* the groups claim of the client's tokens holds the person's roles of this client (a client-role mapper), so the
  cluster sees the groups <tenant>.<group> and nothing of another tenant;
* only a person who holds one of the client's access-group roles can sign in to the client. The identity provider
  has no per-client "who may sign in" setting; its documented way is a browser flow with a conditional sub-flow of
  "Condition - User Role" (negated) and "Deny Access" (Server Administration Guide, "Explicitly deny/allow access in
  conditional flows"), bound to the client as its browser flow override. The condition checks one role, so every
  access-group role of the client is made a composite holding the client's own role "sign-in": whoever holds an
  access-group role holds "sign-in", nobody else does. A refused person sees the identity provider's error page with
  the sentence of DENY_MESSAGE and is not sent back to the cluster.

The flow keeps the realm's own sign-in steps (the session cookie, the identity-provider redirector, user name and
password, a one-time code for a person who set one up) and adds the check after them, outside the steps a session
cookie skips, so a person already signed in to the realm is checked too.

Everything is found by the client (its id from the client's own record) and by the mark the platform writes: the
flow's description and the "sign-in" role's attribute name the cluster. A flow of the wanted alias without the mark
is refused (NameTakenOnSite), never changed or deleted.

Reads one JSON request on stdin, prints one JSON answer on stdout. The tokens come in the request (never printed).
  {"action": "ensure", "url", "realm", "clientsToken", "flowsToken", "clientId", "wanted": [role names],
   "mark": "<namespace>/<cluster>", "cluster": "<cluster>", "tenant": "<tenant>"}
      -> {"ok": true, "flowId", "alias", "changed": [what was changed]}
       | {"ok": false, "reason": "NameTakenOnSite", "message": ...}
  {"action": "remove", "url", "realm", "flowsToken", "clientId", "mark"}
      -> {"ok": true, "removed": [flow aliases]}
Exits non-zero with {"error": ...} when the identity provider answers what the request cannot handle.
"""
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

MARKER = "sign-in"
MARK_ATTRIBUTE = "platformopenshift"
DESCRIPTION = "Sign-in to OpenShift cluster %s of %s: only people in one of its access groups. Managed by the platform (%s)."
DENY_MESSAGE = ("You cannot sign in to this cluster: you are not in one of its access groups. Ask the people who run "
                "the cluster to add you to one of them.")
SUFFIXES = ("", "-b")          # the flow is rebuilt under the other alias, bound, and only then the old one removed

MAPPER_NAME = "groups"
MAPPER_TYPE = "oidc-usermodel-client-role-mapper"


class Refused(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


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


def desired(alias, client_id):
    """The flow: (level, "flow"|"exec", sub-flow alias or provider, requirement, config or None)."""
    return [
        (0, "flow", alias + "-authenticate", "REQUIRED", None),
        (1, "exec", "auth-cookie", "ALTERNATIVE", None),
        (1, "exec", "identity-provider-redirector", "ALTERNATIVE", None),
        (1, "flow", alias + "-forms", "ALTERNATIVE", None),
        (2, "exec", "auth-username-password-form", "REQUIRED", None),
        (2, "flow", alias + "-second-factor", "CONDITIONAL", None),
        (3, "exec", "conditional-user-configured", "REQUIRED", None),
        (3, "exec", "auth-otp-form", "REQUIRED", None),
        (0, "flow", alias + "-people", "CONDITIONAL", None),
        (1, "exec", "conditional-user-role", "REQUIRED",
         {"alias": alias + "-people-role", "config": {"condUserRole": client_id + "." + MARKER, "negate": "true"}}),
        (1, "exec", "deny-access-authenticator", "REQUIRED",
         {"alias": alias + "-people-deny", "config": {"denyErrorMessage": DENY_MESSAGE}}),
    ]


def description(req):
    return DESCRIPTION % (req["cluster"], req["tenant"], req["mark"])


def find_client(api, token, client_id):
    found = [c for c in api.call(token, "GET", "/clients?clientId=" + q(client_id)) or []
             if c.get("clientId") == client_id]
    if not found:
        raise RuntimeError("the sign-in client %s does not exist" % client_id)
    return api.call(token, "GET", "/clients/" + q(found[0]["id"]))


def flows_by_alias(api, token):
    return {f.get("alias"): f for f in api.call(token, "GET", "/authentication/flows") or []}


def structure(api, token, alias):
    """What the flow holds now, in the form of desired()."""
    out = []
    for e in api.call(token, "GET", "/authentication/flows/%s/executions" % q(alias)) or []:
        if e.get("authenticationFlow"):
            out.append((e.get("level"), "flow", e.get("displayName"), e.get("requirement"), None))
            continue
        cfg = None
        if e.get("authenticationConfig"):
            c = api.call(token, "GET", "/authentication/config/" + q(e["authenticationConfig"]), missing_ok=True) or {}
            cfg = {"alias": c.get("alias"), "config": c.get("config") or {}}
        out.append((e.get("level"), "exec", e.get("providerId"), e.get("requirement"), cfg))
    return out


def build(api, token, alias, client_id, desc):
    """Make the flow of desired() under this alias (it does not exist)."""
    api.call(token, "POST", "/authentication/flows",
             {"alias": alias, "description": desc, "providerId": "basic-flow", "topLevel": True, "builtIn": False})
    parents = {0: alias}
    for level, kind, name, _req, cfg in desired(alias, client_id):
        parent = parents[level]
        if kind == "flow":
            api.call(token, "POST", "/authentication/flows/%s/executions/flow" % q(parent),
                     {"alias": name, "type": "basic-flow", "provider": "registration-page-form", "description": desc})
            parents[level + 1] = name
        else:
            api.call(token, "POST", "/authentication/flows/%s/executions/execution" % q(parent), {"provider": name})
    want = desired(alias, client_id)
    have = api.call(token, "GET", "/authentication/flows/%s/executions" % q(alias)) or []
    if len(have) != len(want):
        raise RuntimeError("the new sign-in flow %s holds %d steps, %d were added" % (alias, len(have), len(want)))
    for e, (level, kind, name, requirement, cfg) in zip(have, want):
        if e.get("requirement") != requirement:
            api.call(token, "PUT", "/authentication/flows/%s/executions" % q(alias),
                     {"id": e["id"], "requirement": requirement, "priority": e.get("priority", 0)})
        if cfg is not None:
            api.call(token, "POST", "/authentication/executions/%s/config" % q(e["id"]), cfg)


def ensure_mapper(api, token, client, client_id, changed):
    """The groups claim: the person's roles of this client, nothing else (an older group-membership mapper goes)."""
    want = {"usermodel.clientRoleMapping.clientId": client_id, "claim.name": "groups", "multivalued": "true",
            "jsonType.label": "String", "id.token.claim": "true", "access.token.claim": "true",
            "userinfo.token.claim": "true"}
    cid = client["id"]
    good = False
    for m in client.get("protocolMappers") or []:
        cfg = m.get("config") or {}
        is_ours = m.get("protocolMapper") == MAPPER_TYPE and all(cfg.get(k) == v for k, v in want.items())
        if is_ours and not good:
            good = True
            continue
        # anything else that writes the groups claim of this client goes (the old group-membership mapper sent the
        # last part of every directory group of the person, whoever's tenant)
        if cfg.get("claim.name") == "groups" or m.get("name") == MAPPER_NAME:
            api.call(token, "DELETE", "/clients/%s/protocol-mappers/models/%s" % (q(cid), q(m["id"])))
            changed.append("mapper %s removed" % m.get("name"))
    if not good:
        api.call(token, "POST", "/clients/%s/protocol-mappers/models" % q(cid),
                 {"name": MAPPER_NAME, "protocol": "openid-connect", "protocolMapper": MAPPER_TYPE, "config": want})
        changed.append("groups mapper")


def ensure_roles(api, token, client, req, changed):
    """The client's role sign-in (marked with the cluster) and the access-group roles that hold it."""
    cid = client["id"]
    base = "/clients/%s/roles" % q(cid)
    marker = api.call(token, "GET", "%s/%s" % (base, q(MARKER)), missing_ok=True)
    attrs = {MARK_ATTRIBUTE: [req["mark"]]}
    text = "Everyone who may sign in to OpenShift cluster %s (%s)" % (req["cluster"], req["mark"])
    if marker is None:
        api.call(token, "POST", base, {"name": MARKER, "description": text, "attributes": attrs})
        marker = api.call(token, "GET", "%s/%s" % (base, q(MARKER)))
        changed.append("role " + MARKER)
    elif (marker.get("attributes") or {}).get(MARK_ATTRIBUTE) != [req["mark"]]:
        # the client is this cluster's own (found by its recorded id): its sign-in role is marked as the cluster's
        api.call(token, "PUT", "%s/%s" % (base, q(MARKER)), {"name": MARKER, "description": text, "attributes": attrs})
    wanted = set(req.get("wanted") or [])
    for role in api.call(token, "GET", base) or []:
        name = role.get("name")
        if name == MARKER:
            continue
        comps = api.call(token, "GET", "%s/%s/composites" % (base, q(name))) or []
        holds = any(c.get("id") == marker["id"] for c in comps)
        if name in wanted and not holds:
            api.call(token, "POST", "%s/%s/composites" % (base, q(name)), [{"id": marker["id"], "name": MARKER}])
            changed.append("role %s may sign in" % name)
        elif name not in wanted and holds:
            api.call(token, "DELETE", "%s/%s/composites" % (base, q(name)), [{"id": marker["id"], "name": MARKER}])
            changed.append("role %s may no longer sign in" % name)
    missing = wanted - {r.get("name") for r in api.call(token, "GET", base) or []}
    if missing:
        raise RuntimeError("the sign-in client has no role %s" % ", ".join(sorted(missing)))


def ensure(req):
    api = Api(req["url"], req["realm"])
    ct, ft, client_id = req["clientsToken"], req["flowsToken"], req["clientId"]
    changed = []
    client = find_client(api, ct, client_id)
    ensure_mapper(api, ct, client, client_id, changed)
    ensure_roles(api, ct, client, req, changed)

    desc = description(req)
    flows = flows_by_alias(api, ft)
    by_id = {f.get("id"): f for f in flows.values()}
    bound_id = (client.get("authenticationFlowBindingOverrides") or {}).get("browser") or ""
    bound = by_id.get(bound_id)
    ours = {s: flows.get(client_id + s) for s in SUFFIXES}
    keep = None
    if bound is not None and bound.get("description") == desc and bound.get("alias") in [client_id + s for s in SUFFIXES]:
        if structure(api, ft, bound["alias"]) == desired(bound["alias"], client_id):
            keep = bound
    if keep is None:
        # build under the alias that is not bound now; an existing flow of that alias is ours only with our mark
        current = bound.get("alias") if bound is not None else None
        target = client_id + (SUFFIXES[1] if current == client_id else SUFFIXES[0])
        other = flows.get(target)
        if other is not None:
            if other.get("description") != desc:
                raise Refused("NameTakenOnSite", "The identity provider already holds a sign-in flow named %s that the "
                              "platform did not make for this cluster; nothing was changed." % target)
            api.call(ft, "DELETE", "/authentication/flows/" + q(other["id"]))
        build(api, ft, target, client_id, desc)
        if structure(api, ft, target) != desired(target, client_id):
            raise RuntimeError("the sign-in flow %s does not hold the steps it was given" % target)
        keep = flows_by_alias(api, ft)[target]
        changed.append("flow " + target)
    if bound_id != keep["id"]:
        client = find_client(api, ct, client_id)
        overrides = dict(client.get("authenticationFlowBindingOverrides") or {}, browser=keep["id"])
        client["authenticationFlowBindingOverrides"] = overrides
        api.call(ct, "PUT", "/clients/" + q(client["id"]), client)
        changed.append("client bound to " + keep["alias"])
    # the flow no longer bound goes, when it is ours
    for s, f in ours.items():
        if f is not None and f.get("id") != keep["id"] and f.get("description") == desc:
            api.call(ft, "DELETE", "/authentication/flows/" + q(f["id"]))
            changed.append("old flow %s removed" % f.get("alias"))
    # read back: the client is bound to a flow of the wanted steps
    again = find_client(api, ct, client_id)
    if (again.get("authenticationFlowBindingOverrides") or {}).get("browser") != keep["id"]:
        raise RuntimeError("the sign-in client is not bound to its sign-in flow")
    return {"ok": True, "flowId": keep["id"], "alias": keep["alias"], "changed": changed}


def remove(req):
    api = Api(req["url"], req["realm"])
    ft, client_id = req["flowsToken"], req["clientId"]
    desc_tail = "(%s)." % req["mark"]
    removed = []
    for s in SUFFIXES:
        f = flows_by_alias(api, ft).get(client_id + s)
        if f is not None and str(f.get("description") or "").endswith(desc_tail):
            api.call(ft, "DELETE", "/authentication/flows/" + q(f["id"]), ok=(204,), missing_ok=True)
            removed.append(f.get("alias"))
    return {"ok": True, "removed": removed}


def main():
    try:
        req = json.load(sys.stdin)
        for key in ("url", "realm", "clientId", "mark"):
            if not str(req.get(key) or "").strip():
                raise ValueError("cluster_signin: %s is required" % key)
        action = req.get("action")
        if action == "ensure":
            out = ensure(req)
        elif action == "remove":
            out = remove(req)
        else:
            raise ValueError("cluster_signin: unknown action %r" % (action,))
    except Refused as e:
        out = {"ok": False, "reason": e.reason, "message": e.message}
    except Exception as e:  # noqa: BLE001 - every failure becomes the job's log line (no token is in it)
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

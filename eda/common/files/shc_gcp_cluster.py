#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.6): an OpenShift cluster in a Google Cloud environment, installed by Hive without
keys. Runs with the platform identity and enters the environment's project by impersonating its member account.
Prints one JSON line.

  shc_gcp_cluster.py hive-creds --config '<json>' --build B
      the credential configuration Hive's install and uninstall pods present (an external-account document: the
      pod's own service-account token at the hub's shc-central pool, impersonating the project's member account;
      no secret material inside) -> result {"osServiceAccount.json": ...}
  shc_gcp_cluster.py wif-up --config '<json>' --cluster B --issuer URL --release IMAGE --manifests-dir DIR
      workload identity federation for the cluster's operators: a pool + OIDC provider for the cluster's own issuer
      (audience openshift), one service account per CredentialsRequest of the release (its predefined roles, a
      custom role for listed permissions), impersonable by the request's Kubernetes service accounts; the
      credentials Secret manifests (service_account.json) in DIR
  shc_gcp_cluster.py wif-down --config '<json>' --cluster B
  shc_gcp_cluster.py dns-alias --config '<json>' --state up|down --zone Z --name N [--network VPC] [--wait-seconds S]
      public records of an Internal cluster's internal load balancers (api, *.apps) in the environment's zone

config: {"project", "region", "hub": {"projectNumber", "hivePool", "hiveProvider"}, "memberServiceAccount"}
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shc_gcp_api as g  # noqa: E402
from shc_aws_cluster import credentials_requests, genkey, jwks  # noqa: E402

IAM = "https://iam.googleapis.com/v1"
DNS = "https://dns.googleapis.com/dns/v1"
POD_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
OPERATOR_TOKEN = "/var/run/secrets/openshift/serviceaccount/token"
STS = "https://sts.googleapis.com/v1/token"


def fail(msg, **extra):
    g.fail(msg, **extra)


def short_id(text, limit, prefix=""):
    """A resource id of at most `limit` chars: the text, or its head plus a hash; starts with a letter, ends alnum."""
    text = re.sub(r"[^a-z0-9-]", "-", text.lower())
    if not text.startswith(prefix):
        text = prefix + text
    if len(text) > limit:
        h = hashlib.sha256(text.encode()).hexdigest()[:6]
        text = text[: limit - 7].rstrip("-") + "-" + h
    out = text.strip("-")
    if not out[0].isalpha():
        out = "s" + out[1:]
    return out


# Google's limits for the names this helper builds (the build name is at most 40 characters of [a-z0-9-])
POOL_ID_MAX = 32            # workload identity pool id: 4-32 of [a-z0-9-]
POOL_DISPLAY_NAME_MAX = 32  # workload identity pool display name
SA_ID_MAX = 30              # service account id: 6-30 of [a-z0-9-], starts with a letter
SA_DISPLAY_NAME_MAX = 100   # service account display name
ROLE_ID_MAX = 64            # custom role id: 3-64 of [a-zA-Z0-9_.]
ROLE_TITLE_MAX = 100        # custom role title


def labelled_name(cluster, ns, name, limit):
    """"<cluster> <ns>/<name>" in at most `limit` characters: the build name stays whole (it is what the platform
    finds the cluster's objects by), the CredentialsRequest's namespace/name is cut to the room left."""
    room = limit - len(cluster) - 1
    if room <= 0:
        return cluster[:limit]
    return "%s %s" % (cluster, ("%s/%s" % (ns, name))[:room])


def pool_id(cluster):
    return short_id(cluster, POOL_ID_MAX, "shc-")


def sa_account_id(cluster, ns, name):
    return short_id("%s-%s-%s" % (cluster, ns, name), SA_ID_MAX)


def role_id(cluster, ns, name):
    """One custom role per request: several requests share the secret name "cloud-credentials" (ingress,
    cloud-network-config-controller), so the id carries the namespace."""
    rid = re.sub(r"[^a-zA-Z0-9_.]", "_", "shc_%s_%s_%s" % (cluster, ns, name))
    if len(rid) > ROLE_ID_MAX:
        rid = rid[:ROLE_ID_MAX - 7] + "_" + hashlib.sha256(rid.encode()).hexdigest()[:6]
    return rid


def external_account(audience, sa_email, token_file, project=None):
    doc = {"type": "external_account", "audience": audience,
           "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "token_url": STS,
           "service_account_impersonation_url": "%s/projects/-/serviceAccounts/%s:generateAccessToken" % (
               "https://iamcredentials.googleapis.com/v1", sa_email),
           "credential_source": {"file": token_file, "format": {"type": "text"}}}
    if project:
        doc["project_id"] = project  # Hive derives the project from the credentials; external-account files carry none
    return doc


class Cluster:
    def __init__(self, cfg, cluster):
        if not re.match(r"^[a-z0-9-]{1,40}$", cluster):
            fail("cluster id must be 1-40 of [a-z0-9-]", cluster=cluster)
        self.cfg, self.cluster, self.project = cfg, cluster, cfg["project"]
        self.ident = g.Identity()
        self.tok = self.ident.project(self.project)
        self.changed = []
        p = g.http("GET", "%s/projects/%s" % (g.CRM, self.project), token=self.tok)
        self.number = (p.get("name") or "").split("/")[-1]
        self.pool_id = pool_id(cluster)
        self.pool = "%s/projects/%s/locations/global/workloadIdentityPools/%s" % (IAM, self.project, self.pool_id)
        self.provider = self.pool + "/providers/oidc"
        self.audience = "//iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/providers/oidc" % (self.number, self.pool_id)

    # ---- the pool and provider for the cluster's issuer
    def pool_up(self, issuer):
        pool = g.http("GET", self.pool, token=self.tok, ok404=True)
        if pool and pool.get("state") == "DELETED":
            g.http("POST", self.pool + ":undelete", {}, token=self.tok)
            self.changed.append("pool undeleted")
            pool = None
            for _ in range(30):
                time.sleep(5)
                pool = g.http("GET", self.pool, token=self.tok, ok404=True)
                if pool and pool.get("state") == "ACTIVE":
                    break
        if not pool:
            g.http("POST", "%s/projects/%s/locations/global/workloadIdentityPools?workloadIdentityPoolId=%s" % (IAM, self.project, self.pool_id),
                   {"displayName": self.cluster[:POOL_DISPLAY_NAME_MAX], "description": "OpenShift cluster %s (SHC)" % self.cluster}, token=self.tok)
            self.changed.append("pool " + self.pool_id)
            for _ in range(30):
                time.sleep(5)
                pool = g.http("GET", self.pool, token=self.tok, ok404=True)
                if pool and pool.get("state") == "ACTIVE":
                    break
        prov = g.http("GET", self.provider, token=self.tok, ok404=True)
        want = {"displayName": "oidc", "attributeMapping": {"google.subject": "assertion.sub"},
                "oidc": {"issuerUri": issuer, "allowedAudiences": ["openshift"]}}
        if prov and prov.get("state") == "DELETED":
            g.http("POST", self.provider + ":undelete", {}, token=self.tok)
            self.changed.append("provider undeleted")
            prov = None
            time.sleep(10)
        if not prov:
            g.http("POST", self.pool + "/providers?workloadIdentityPoolProviderId=oidc", want, token=self.tok)
            self.changed.append("provider oidc")
        elif (prov.get("oidc") or {}).get("issuerUri") != issuer:
            fail("provider %s has issuer %s, want %s" % (self.provider, (prov.get("oidc") or {}).get("issuerUri"), issuer))

    # ---- one service account per CredentialsRequest
    def sa_email(self, account_id):
        return "%s@%s.iam.gserviceaccount.com" % (account_id, self.project)

    def ensure_sa(self, account_id, ref):
        email = self.sa_email(account_id)
        sa = g.http("GET", "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, email), token=self.tok, ok404=True)
        if not sa:
            g.http("POST", "%s/projects/%s/serviceAccounts" % (IAM, self.project), {
                "accountId": account_id, "serviceAccount": {"displayName": labelled_name(self.cluster, ref["namespace"], ref["name"], SA_DISPLAY_NAME_MAX),
                                                            "description": "shc-cluster=" + self.cluster}}, token=self.tok)
            self.changed.append("service account " + account_id)
            time.sleep(3)
        return email

    def project_policy_add(self, member, roles):
        """Bind roles to a member on the project (one read-modify-write with etag)."""
        for attempt in range(10):
            pol = g.http("POST", "%s/projects/%s:getIamPolicy" % (g.CRM, self.project), {"options": {"requestedPolicyVersion": 3}}, token=self.tok)
            pol.setdefault("bindings", [])
            changed = False
            for role in roles:
                b = next((x for x in pol["bindings"] if x["role"] == role and not x.get("condition")), None)
                if not b:
                    b = {"role": role, "members": []}
                    pol["bindings"].append(b)
                if member not in b["members"]:
                    b["members"].append(member)
                    changed = True
            if not changed:
                return False
            try:
                g.http("POST", "%s/projects/%s:setIamPolicy" % (g.CRM, self.project), {"policy": pol}, token=self.tok)
                return True
            except g.GcpError as e:
                # 409: etag race; 400 "does not exist": a service account created seconds ago is not yet visible
                # to IAM (read-after-write lag of up to a minute)
                if not (e.code == 409 or (e.code == 400 and "does not exist" in str(e))) or attempt == 9:
                    raise
                time.sleep(2 if e.code == 409 else 10)

    def custom_role_up(self, role_id, permissions, title):
        name = "projects/%s/roles/%s" % (self.project, role_id)
        r = g.http("GET", "%s/%s" % (IAM, name), token=self.tok, ok404=True)
        if r and r.get("deleted"):
            g.http("POST", "%s/%s:undelete" % (IAM, name), {}, token=self.tok)
            r = g.http("GET", "%s/%s" % (IAM, name), token=self.tok)
            self.changed.append("custom role undeleted " + role_id)
        if not r:
            g.http("POST", "%s/projects/%s/roles" % (IAM, self.project), {"roleId": role_id, "role": {
                "title": title[:ROLE_TITLE_MAX], "includedPermissions": sorted(permissions), "stage": "GA"}}, token=self.tok)
            self.changed.append("custom role " + role_id)
        elif sorted(r.get("includedPermissions", [])) != sorted(permissions):
            g.http("PATCH", "%s/%s?updateMask=includedPermissions" % (IAM, name), {"includedPermissions": sorted(permissions)}, token=self.tok)
            self.changed.append("custom role updated " + role_id)
        return name

    def sa_binding_up(self, email, subjects):
        """The request's Kubernetes service accounts (through the cluster's pool) may impersonate the SA."""
        url = "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, email)
        pol = g.http("POST", url + ":getIamPolicy", {}, token=self.tok)
        members = ["principal://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/subject/%s" % (self.number, self.pool_id, s)
                   for s in subjects]
        b = next((x for x in pol.get("bindings", []) if x["role"] == "roles/iam.workloadIdentityUser"), None)
        if b and sorted(b.get("members", [])) == sorted(members):
            return
        pol.setdefault("bindings", [])
        pol["bindings"] = [x for x in pol["bindings"] if x["role"] != "roles/iam.workloadIdentityUser"]
        pol["bindings"].append({"role": "roles/iam.workloadIdentityUser", "members": members})
        for attempt in range(10):  # the same read-after-write lag on a new service account's own policy
            try:
                g.http("POST", url + ":setIamPolicy", {"policy": pol}, token=self.tok)
                break
            except g.GcpError as e:
                if not (e.code == 400 and "does not exist" in str(e)) or attempt == 9:
                    raise
                time.sleep(10)
        self.changed.append("binding " + email.split("@")[0])

    def up(self, issuer, release, mdir):
        crs = credentials_requests(release, "GCPProviderSpec")
        self.pool_up(issuer)
        os.makedirs(mdir, exist_ok=True)
        want, files = set(), []
        for cr in crs:
            spec = cr["spec"]
            ref = spec["secretRef"]
            sas = spec.get("serviceAccountNames") or []
            if not sas:
                fail("CredentialsRequest without serviceAccountNames", name=cr["metadata"]["name"])
            account_id = sa_account_id(self.cluster, ref["namespace"], ref["name"])
            email = self.ensure_sa(account_id, ref)
            want.add(email)
            ps = spec["providerSpec"]
            roles = list(ps.get("predefinedRoles") or [])
            if ps.get("permissions"):
                rid = role_id(self.cluster, ref["namespace"], ref["name"])
                roles.append(self.custom_role_up(rid, ps["permissions"],
                                                 labelled_name(self.cluster, ref["namespace"], ref["name"], ROLE_TITLE_MAX)))
            if self.project_policy_add("serviceAccount:" + email, roles):
                self.changed.append("roles for " + account_id)
            self.sa_binding_up(email, ["system:serviceaccount:%s:%s" % (ref["namespace"], sa) for sa in sas])
            fn = "%s-%s-credentials.yaml" % (ref["namespace"], ref["name"])
            doc = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                   "metadata": {"name": ref["name"], "namespace": ref["namespace"]},
                   "stringData": {"service_account.json": json.dumps(external_account(self.audience, email, OPERATOR_TOKEN))}}
            import yaml
            with open(os.path.join(mdir, fn), "w") as f:
                yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False)
            files.append(fn)
        for sa in self.cluster_sas():
            if sa["email"] not in want:
                g.http("DELETE", "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, sa["email"]), token=self.tok)
                self.changed.append("removed service account " + sa["email"].split("@")[0])
        return {"cluster": self.cluster, "project": self.project, "projectNumber": self.number, "issuer": issuer,
                "pool": self.pool_id, "audience": self.audience, "serviceAccounts": sorted(want), "manifests": sorted(files)}

    def cluster_sas(self):
        return [s for s in g.list_all("%s/projects/%s/serviceAccounts" % (IAM, self.project), "accounts", self.tok)
                if (s.get("description") or "") == "shc-cluster=" + self.cluster]

    def down(self):
        for sa in self.cluster_sas():
            g.http("DELETE", "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, sa["email"]), token=self.tok)
            self.changed.append("removed service account " + sa["email"].split("@")[0])
        for r in g.list_all("%s/projects/%s/roles" % (IAM, self.project), "roles", self.tok, {"showDeleted": "false"}):
            if owns_role(self.cluster, r):
                g.http("DELETE", "%s/%s" % (IAM, r["name"]), token=self.tok)
                self.changed.append("removed custom role " + r["name"].rsplit("/", 1)[1])
        pool = g.http("GET", self.pool, token=self.tok, ok404=True)
        if pool and pool.get("state") == "ACTIVE":
            g.http("DELETE", self.pool, token=self.tok)
            self.changed.append("pool deleted (recoverable 30 days)")
        return {"cluster": self.cluster, "project": self.project, "pool": self.pool_id}


def hive_creds(cfg, build):
    hub = cfg["hub"]
    audience = "//iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/providers/%s" % (
        hub["projectNumber"], hub.get("hivePool", "shc-central"), hub.get("hiveProvider", "central"))
    sa = "%s@%s.iam.gserviceaccount.com" % (cfg.get("memberServiceAccount", "shc-automation"), cfg["project"])
    return {"osServiceAccount.json": json.dumps(external_account(audience, sa, POD_TOKEN, cfg["project"]), indent=1)}


def owns_role(cluster, role):
    """Whether a custom role (as listed: name, title) is this cluster's: its id has the cluster's head and its title
    starts with the whole build name and a space, so a cluster whose build name starts another's never takes it."""
    rid = str(role.get("name") or "").rsplit("/", 1)[-1]
    head = "shc_%s_" % re.sub(r"[^a-zA-Z0-9_.]", "_", cluster)
    return rid.startswith(head) and str(role.get("title") or "").startswith(cluster + " ")


def infra_prefix(name):
    """The head of the installer's infrastructure id for a cluster name (as shc_aws_cluster.infra_prefix)."""
    return re.sub(r"[^a-z0-9-]", "-", str(name).lower())[:21].rstrip("-")


def select_rules(rules, name, network):
    """The cluster's internal API and ingress forwarding rules ({kind: rule}) of a region's rules: the API rule is
    exactly "<infra id>-api-internal" with an id of this cluster's form; an ingress rule carries no cluster id, so it is
    taken only when the network holds one cluster (one API rule). Two clusters whose ids fit the name are refused."""
    api_pat = re.compile(r"^(%s-[a-z0-9]{5})-api-internal$" % re.escape(infra_prefix(name)))
    mine = [r for r in rules if r.get("loadBalancingScheme") == "INTERNAL" and r.get("IPAddress")
            and (not network or (r.get("network") or "").endswith("/networks/" + network))]
    ids = {m.group(1) for m in (api_pat.match(r.get("name") or "") for r in mine) if m}
    if len(ids) > 1:
        fail("more than one cluster in the network fits the name %s (%s); no alias is written" % (name, ", ".join(sorted(ids))))
    found = {}
    apis = [r for r in mine if api_pat.match(r.get("name") or "")]
    if apis:
        found["api"] = apis[0]
    all_api = [r for r in mine if (r.get("name") or "").endswith("-api-internal")]
    ingress = [r for r in mine if "openshift-ingress/router-default" in (r.get("description") or "")]
    if apis and len(all_api) == 1 and len(ingress) == 1:
        found["apps"] = ingress[0]
    return found


def dns_alias(cfg, state, zone, name, network, wait=0):
    ident = g.Identity()
    tok = ident.project(cfg["project"])
    base = "%s/projects/%s" % (g.COMPUTE, cfg["project"])
    zn = zone.rstrip(".") + "."
    zones = [z for z in g.list_all("%s/projects/%s/managedZones" % (DNS, cfg["project"]), "managedZones", tok)
             if z["dnsName"] == zn and z.get("visibility", "public") == "public"]
    if not zones:
        fail("public zone %s not found" % zone)
    z = zones[0]
    names = {"api": "api.%s.%s" % (name, zn), "apps": "*.apps.%s.%s" % (name, zn)}
    zurl = "%s/projects/%s/managedZones/%s" % (DNS, cfg["project"], z["name"])

    def have(n):
        rr = g.list_all(zurl + "/rrsets", "rrsets", tok, {"name": n, "type": "A"})
        return rr[0] if rr else None

    if state == "down":
        gone = []
        for n in names.values():
            rr = have(n)
            if rr:
                g.http("POST", zurl + "/changes", {"deletions": [rr]}, token=tok)
                gone.append(n)
        return {"zone": zone, "removed": gone}
    deadline = time.time() + wait
    done = {}
    while True:
        rules = g.list_all(base + "/regions/%s/forwardingRules" % cfg["region"], "items", tok)
        for kind, r in select_rules(rules, name, network).items():
            if kind in done:
                continue
            cur = have(names[kind])
            body = {"name": names[kind], "type": "A", "ttl": 60, "rrdatas": [r["IPAddress"]]}
            if not cur:
                g.http("POST", zurl + "/changes", {"additions": [body]}, token=tok)
            elif cur.get("rrdatas") != [r["IPAddress"]]:
                g.http("POST", zurl + "/changes", {"deletions": [cur], "additions": [body]}, token=tok)
            done[kind] = r["IPAddress"]
        if len(done) == 2 or time.time() >= deadline:
            break
        time.sleep(20)
    if "api" not in done:
        fail("the cluster's internal API load balancer did not appear in %s" % (network or cfg["region"]), aliases=done)
    return {"zone": zone, "aliases": done, "complete": len(done) == 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("command", choices=["genkey", "jwks", "hive-creds", "wif-up", "wif-down", "dns-alias"])
    ap.add_argument("--key-file")
    ap.add_argument("--build")
    ap.add_argument("--cluster")
    ap.add_argument("--issuer")
    ap.add_argument("--release")
    ap.add_argument("--manifests-dir")
    ap.add_argument("--state", choices=["up", "down"])
    ap.add_argument("--zone")
    ap.add_argument("--name")
    ap.add_argument("--network")
    ap.add_argument("--wait-seconds", type=int, default=0)
    a = ap.parse_args()
    try:
        if a.command in ("genkey", "jwks"):
            if not a.key_file:
                fail("--key-file is required")
            r = genkey(a.key_file) if a.command == "genkey" else jwks(a.key_file)
            print(json.dumps({"ok": True, "changed": a.command == "genkey", "result": r}))
            return
        cfg = json.loads(a.config or "{}")
        if a.command == "hive-creds":
            if not (a.config and a.build):
                fail("--config and --build are required")
            print(json.dumps({"ok": True, "changed": [], "result": hive_creds(cfg, a.build)}))
            return
        if a.command == "dns-alias":
            if not (a.config and a.state and a.zone and a.name):
                fail("--config, --state, --zone and --name are required")
            print(json.dumps({"ok": True, "changed": [], "result": dns_alias(cfg, a.state, a.zone, a.name, a.network, a.wait_seconds)}, default=str))
            return
        if not (a.config and a.cluster):
            fail("--config and --cluster are required")
        c = Cluster(cfg, a.cluster)
        if a.command == "wif-up":
            if not (a.issuer and a.release and a.manifests_dir):
                fail("--issuer, --release and --manifests-dir are required")
            r = c.up(a.issuer, a.release, a.manifests_dir)
        else:
            r = c.down()
        print(json.dumps({"ok": True, "changed": c.changed, "result": r}, default=str))
    except g.GcpError as e:
        fail(str(e), code=e.code)


if __name__ == "__main__":
    main()

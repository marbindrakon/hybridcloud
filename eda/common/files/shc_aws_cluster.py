#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §12): an OpenShift cluster on AWS in STS (Manual credentials) mode, without ccoctl.
Its service-account issuer is https://<issuer host>/<cluster> (the platform's issuer, published before the
install from the cluster's own signing key); its operators get IAM roles in the cluster's account that trust only
that issuer and their service accounts. Runs with the job's AWS web identity and enters the account through its
`shc-automation` role (external ID shc-<account>). Idempotent; prints one JSON line.

  shc_aws_cluster.py genkey --key-file PATH            new RSA-4096 signing key (PKCS#1 PEM, mode 0600)
  shc_aws_cluster.py jwks --key-file PATH              the key set (public key) of a signing key
  shc_aws_cluster.py --config '<json>' sts-up --cluster ID --issuer URL --release IMAGE --manifests-dir DIR
      IAM OIDC provider for the issuer (client IDs openshift, sts.amazonaws.com); per AWS CredentialsRequest of the
      release a role (path /shc/<cluster>/) trusting the issuer's tokens of its service accounts, with the request's
      policy; roles of this cluster no longer requested are removed. Writes the credentials Secret manifest of each
      request into DIR (no secrets in them: role ARN + projected token path).
  shc_aws_cluster.py --config '<json>' sts-down --cluster ID --issuer URL
      the cluster's roles and its OIDC provider.

config: {"account", "region", "memberRole", "entry"}

CredentialsRequests come from the release image's release-manifests (registry API, anonymous pull as quay.io's
openshift-release-dev repositories allow; an auth file can be given with REGISTRY_AUTH_FILE): AWS ones included in
self-managed high-availability clusters, not limited to a non-default feature set, and whose feature gates are enabled
in the release's default feature set (as `oc adm release extract --credentials-requests --included`).
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

import yaml

TAGS = [{"Key": "shc-managed", "Value": "true"}]
TOKEN_FILE = "/var/run/secrets/openshift/serviceaccount/token"


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(1)


# ---- signing key -----------------------------------------------------------------------------------------------
def genkey(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if os.path.exists(path) and os.path.getsize(path) > 0:
        fail("key file exists; not overwriting", keyFile=path)
    k = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()))
    return {"keyFile": path, "kid": jwks(path)["keys"][0]["kid"]}


def jwks(path):
    """The key set as Kubernetes publishes it: kid = base64url(sha256(DER SubjectPublicKeyInfo))."""
    from cryptography.hazmat.primitives import serialization
    k = serialization.load_pem_private_key(open(path, "rb").read(), password=None).public_key()
    der = k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    n = k.public_numbers()
    return {"keys": [{"use": "sig", "kty": "RSA", "kid": b64(hashlib.sha256(der).digest()), "alg": "RS256",
                      "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
                      "e": b64(n.e.to_bytes((n.e.bit_length() + 7) // 8, "big"))}]}


# ---- release image: CredentialsRequests ------------------------------------------------------------------------
def _parse_image(ref):
    m = re.match(r"^([^/]+)/(.+?)(?:@(sha256:[0-9a-f]{64})|:([\w][\w.-]*))$", ref)
    if not m:
        fail("cannot parse release image " + ref)
    return m.group(1), m.group(2), m.group(3) or m.group(4)


def _registry_auth(host):
    p = os.environ.get("REGISTRY_AUTH_FILE")
    if p and os.path.exists(p):
        a = json.load(open(p)).get("auths", {}).get(host, {}).get("auth")
        if a:
            return "Basic " + a
    return None


class Registry:
    def __init__(self, host, repo):
        self.host, self.repo, self.token = host, repo, None

    def _open(self, url, accept=None, retry=True):
        h = {"Accept": accept} if accept else {}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120)
        except urllib.error.HTTPError as e:
            if e.code != 401 or not retry:
                raise
            chal = e.headers.get("WWW-Authenticate", "")
            p = dict(re.findall(r'(\w+)="([^"]*)"', chal))
            q = {"service": p.get("service", ""), "scope": "repository:%s:pull" % self.repo}
            hh = {}
            basic = _registry_auth(self.host)
            if basic:
                hh["Authorization"] = basic
            r = urllib.request.urlopen(urllib.request.Request(p["realm"] + "?" + urllib.parse.urlencode(q),
                                                              headers=hh), timeout=60)
            d = json.load(r)
            self.token = d.get("token") or d.get("access_token")
            return self._open(url, accept, retry=False)

    def manifest(self, ref):
        acc = ",".join(["application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"])
        m = json.load(self._open("https://%s/v2/%s/manifests/%s" % (self.host, self.repo, ref), acc))
        if "manifests" in m:  # an index: the linux/amd64 image
            pick = [x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "amd64"
                    and x.get("platform", {}).get("os") == "linux"]
            if not pick:
                fail("release index has no linux/amd64 image")
            return self.manifest(pick[0]["digest"])
        return m

    def blob(self, digest):
        return self._open("https://%s/v2/%s/blobs/%s" % (self.host, self.repo, digest))


def credentials_requests(image):
    host, repo, ref = _parse_image(image)
    reg = Registry(host, repo)
    layers = reg.manifest(ref)["layers"]
    found, gates = [], set()
    for layer in reversed(layers):  # release-manifests sit in the image's top layer
        data = reg.blob(layer["digest"]).read()
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
        seen = False
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for mem in t:
                name = mem.name.lstrip("./")
                if not name.startswith("release-manifests/"):
                    continue
                seen = True
                if not mem.isfile() or not name.endswith((".yaml", ".yml")):
                    continue
                text = t.extractfile(mem).read().decode("utf-8", "replace")
                if "CredentialsRequest" not in text and "SelfManagedHA-Default" not in name:
                    continue
                for doc in yaml.safe_load_all(text):
                    if isinstance(doc, dict) and doc.get("kind") == "CredentialsRequest":
                        found.append(doc)
                    elif isinstance(doc, dict) and doc.get("kind") == "FeatureGate" and "SelfManagedHA-Default" in name:
                        for fg in (doc.get("status") or {}).get("featureGates") or []:
                            gates |= {g["name"] for g in fg.get("enabled") or []}
        if seen:
            break
    out = []
    for cr in found:
        spec = cr.get("spec", {})
        if spec.get("providerSpec", {}).get("kind") != "AWSProviderSpec":
            continue
        ann = cr.get("metadata", {}).get("annotations", {}) or {}
        if ann.get("include.release.openshift.io/self-managed-high-availability") != "true":
            continue
        fs = ann.get("release.openshift.io/feature-set")
        if fs and "Default" not in [x.strip() for x in fs.split(",")]:
            continue
        # gated manifests: every listed gate enabled ("-gate": disabled) in the default feature set
        fg = [g.strip() for g in ann.get("release.openshift.io/feature-gate", "").split(",") if g.strip()]
        if any((g[1:] in gates) if g.startswith("-") else (g not in gates) for g in fg):
            continue
        out.append(cr)
    if not out:
        fail("no AWS CredentialsRequests found in " + image)
    return out


# ---- AWS -------------------------------------------------------------------------------------------------------
def session(cfg):
    import boto3
    sts = boto3.client("sts")
    if cfg.get("entry", "automation") == "org-access":
        kw = {"RoleArn": "arn:aws:iam::%s:role/OrganizationAccountAccessRole" % cfg["account"]}
    else:
        kw = {"RoleArn": "arn:aws:iam::%s:role/%s" % (cfg["account"], cfg.get("memberRole", "shc-automation")),
              "ExternalId": "shc-%s" % cfg["account"]}
    c = sts.assume_role(RoleSessionName="shc-cluster", **kw)["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=cfg["region"])


def canon(x):
    """A policy document as IAM may hand it back: one-element lists as scalars, lists sorted."""
    if isinstance(x, dict):
        return {k: canon(v) for k, v in x.items()}
    if isinstance(x, list):
        x = sorted((canon(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True))
        return x[0] if len(x) == 1 else x
    return x


def same(a, b):
    return json.dumps(canon(a), sort_keys=True) == json.dumps(canon(b), sort_keys=True)


def role_name(cluster, ns, secret):
    full = "%s-%s-%s" % (cluster, ns, secret)
    return full if len(full) <= 64 else full[:55] + "-" + hashlib.sha256(full.encode()).hexdigest()[:8]


def issuer_hostpath(issuer):
    return re.sub(r"^https://", "", issuer).rstrip("/")


class Cluster:
    def __init__(self, cfg, cluster, issuer):
        if not re.match(r"^[a-z0-9-]{1,40}$", cluster):
            fail("cluster id must be 1-40 of [a-z0-9-]", cluster=cluster)
        self.cfg, self.cluster, self.issuer = cfg, cluster, issuer.rstrip("/")
        self.iam = session(cfg).client("iam")
        self.account = cfg["account"]
        self.provider_arn = "arn:aws:iam::%s:oidc-provider/%s" % (self.account, issuer_hostpath(self.issuer))
        self.path = "/shc/%s/" % cluster
        self.changed = []

    def tags(self):
        return TAGS + [{"Key": "shc-cluster", "Value": self.cluster}]

    def provider_up(self):
        try:
            p = self.iam.get_open_id_connect_provider(OpenIDConnectProviderArn=self.provider_arn)
            want = {"openshift", "sts.amazonaws.com"}
            for cid in want - set(p.get("ClientIDList", [])):
                self.iam.add_client_id_to_open_id_connect_provider(OpenIDConnectProviderArn=self.provider_arn,
                                                                   ClientID=cid)
                self.changed.append("provider client " + cid)
        except self.iam.exceptions.NoSuchEntityException:
            self.iam.create_open_id_connect_provider(Url=self.issuer, ClientIDList=["openshift", "sts.amazonaws.com"],
                                                     Tags=self.tags())
            self.changed.append("provider " + issuer_hostpath(self.issuer))

    def roles(self):
        out = []
        for page in self.iam.get_paginator("list_roles").paginate(PathPrefix=self.path):
            out += page["Roles"]
        return out

    def role_up(self, cr):
        spec = cr["spec"]
        ref = spec["secretRef"]
        sas = spec.get("serviceAccountNames") or []
        if not sas:
            fail("CredentialsRequest without serviceAccountNames", name=cr["metadata"]["name"])
        name = role_name(self.cluster, ref["namespace"], ref["name"])
        hp = issuer_hostpath(self.issuer)
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Federated": self.provider_arn},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {hp + ":sub": ["system:serviceaccount:%s:%s" % (ref["namespace"], sa)
                                                         for sa in sas]}}}]}
        stmts = []
        for e in spec["providerSpec"].get("statementEntries", []):
            s = {"Effect": e["effect"], "Action": e["action"], "Resource": e["resource"]}
            if e.get("policyCondition"):
                s["Condition"] = e["policyCondition"]
            stmts.append(s)
        policy = {"Version": "2012-10-17", "Statement": stmts}
        try:
            r = self.iam.get_role(RoleName=name)["Role"]
            if r["Path"] != self.path:
                fail("role exists outside this cluster's path", role=name, path=r["Path"])
            if not same(r["AssumeRolePolicyDocument"], trust):
                self.iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
                self.changed.append("trust " + name)
        except self.iam.exceptions.NoSuchEntityException:
            r = self.iam.create_role(RoleName=name, Path=self.path, AssumeRolePolicyDocument=json.dumps(trust),
                                     MaxSessionDuration=3600, Tags=self.tags(),
                                     Description="OpenShift %s: %s/%s" % (self.cluster, ref["namespace"], ref["name"])
                                     )["Role"]
            self.changed.append("role " + name)
        pname = "%s-policy" % ref["name"]
        try:
            have = self.iam.get_role_policy(RoleName=name, PolicyName=pname)["PolicyDocument"]
        except self.iam.exceptions.NoSuchEntityException:
            have = None
        if have is None or not same(have, policy):
            self.iam.put_role_policy(RoleName=name, PolicyName=pname, PolicyDocument=json.dumps(policy))
            self.changed.append("policy " + name)
        return name, r["Arn"], ref

    def role_down(self, name):
        for p in self.iam.list_role_policies(RoleName=name)["PolicyNames"]:
            self.iam.delete_role_policy(RoleName=name, PolicyName=p)
        for p in self.iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            self.iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        self.iam.delete_role(RoleName=name)
        self.changed.append("removed role " + name)

    def up(self, release, mdir):
        crs = credentials_requests(release)
        self.provider_up()
        os.makedirs(mdir, exist_ok=True)
        want, files = set(), []
        for cr in crs:
            name, arn, ref = self.role_up(cr)
            want.add(name)
            fn = "%s-%s-credentials.yaml" % (ref["namespace"], ref["name"])
            doc = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                   "metadata": {"name": ref["name"], "namespace": ref["namespace"]},
                   "stringData": {"credentials": "[default]\nsts_regional_endpoints = regional\nrole_arn = %s\n"
                                                 "web_identity_token_file = %s\n" % (arn, TOKEN_FILE)}}
            with open(os.path.join(mdir, fn), "w") as f:
                yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False)
            files.append(fn)
        for r in self.roles():
            if r["RoleName"] not in want:
                self.role_down(r["RoleName"])
        return {"cluster": self.cluster, "account": self.account, "issuer": self.issuer,
                "provider": self.provider_arn, "roles": sorted(want), "manifests": sorted(files)}

    def down(self):
        for r in self.roles():
            self.role_down(r["RoleName"])
        try:
            self.iam.delete_open_id_connect_provider(OpenIDConnectProviderArn=self.provider_arn)
            self.changed.append("removed provider " + issuer_hostpath(self.issuer))
        except self.iam.exceptions.NoSuchEntityException:
            pass
        return {"cluster": self.cluster, "account": self.account, "provider": self.provider_arn}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("command", choices=["genkey", "jwks", "sts-up", "sts-down"])
    ap.add_argument("--key-file")
    ap.add_argument("--cluster")
    ap.add_argument("--issuer")
    ap.add_argument("--release")
    ap.add_argument("--manifests-dir")
    a = ap.parse_args()
    if a.command in ("genkey", "jwks"):
        if not a.key_file:
            fail("--key-file is required")
        r = genkey(a.key_file) if a.command == "genkey" else jwks(a.key_file)
        print(json.dumps({"ok": True, "changed": a.command == "genkey", "result": r}))
        return
    if not (a.config and a.cluster and a.issuer):
        fail("--config, --cluster and --issuer are required")
    c = Cluster(json.loads(a.config), a.cluster, a.issuer)
    if a.command == "sts-up":
        if not (a.release and a.manifests_dir):
            fail("--release and --manifests-dir are required")
        r = c.up(a.release, a.manifests_dir)
    else:
        r = c.down()
    print(json.dumps({"ok": True, "changed": c.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

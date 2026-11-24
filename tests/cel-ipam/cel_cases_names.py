#!/usr/bin/env python3
"""CEL envtest for the rule for names: metadata.name of every kind whose name a person chooses and that reaches a
platform is a letter first, then lower-case letters, digits and single hyphens, a letter or digit last, at most 30
characters (a gateway's 21); a cluster on Google Cloud may not be named like "google". The platform's configuration
kinds keep the API server's own rule. An object made before the rule keeps working: its status, its spec and its
deletion are still accepted. The status keys the jobs read back are kept, and an OpenStack environment's
spec.project of the release before is accepted and ignored.

  python3 -I tests/cel-ipam/cel_cases_names.py --crds operator/primary/helm/templates/crds.yaml [--report out.json]

Needs KUBEBUILDER_ASSETS (etcd, kube-apiserver, kubectl) and openssl. tests/unit/test_api_names.py checks the
same names against the job's copy of the rule (eda/common/files/shc_names.py) and runs without an API server.
Example names only.
"""
import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema-replay"))
import replay  # noqa: E402

GROUP = "hybridsovereign.redhat"
API = f"{GROUP}/v1alpha1"

# kind -> the longest name it takes
RULED = {
    "Entity": 30, "CloudOSO": 30, "CloudVirt": 30, "CloudAWS": 30, "CloudGCP": 30, "HybridNetwork": 30,
    "NetworkPlacement": 30, "PlatformOpenshift": 30, "Rbac": 30, "Project": 30, "Team": 30, "Assignment": 30,
    "Vault": 30, "VaultKV": 30, "QuayOrg": 30, "AAPOrg": 30, "AttachmentCircuit": 30, "CloudGateway": 21,
    "CloudInfrastructure": 30, "HybridFabric": 30, "ProviderEdge": 30,
}
# the platform's configuration kinds: no rule of ours on the name
UNRULED = ["RbacConfig", "AAPConfig", "QuayConfig", "DNSConfig", "UIHealthChecker", "Iaac", "Persona",
           "OpenStackMigration", "TransportLink"]

ACCEPTED = ["a", "web", "acme-corp", "web-2", "a1b2", "core-prod"]
REFUSED = ["1web", "-web", "web-", "web--prod", "Web", "web.prod", "web_prod"]  # plus the limit + 1 per kind
GOOGLE_REFUSED = ["goog", "googweb", "acme-google", "my-g00gle-web", "go0gle-x", "googles"]
GOOGLE_ACCEPTED = ["web", "goo-gle", "acme-goo", "gogle", "agoog"]

NAME_DENY = "metadata.name: Invalid value"
GOOGLE_DENY = "may not start with \"goog\""


ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLES = {}


def load_samples():
    """kind -> the spec of its first sample (samples/), so that an object is valid apart from its name."""
    import yaml
    for f in sorted((ROOT / "samples").rglob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text()):
            if d and str(d.get("apiVersion", "")).startswith(GROUP) and d["kind"] not in SAMPLES:
                SAMPLES[d["kind"]] = d.get("spec") or {}
    return SAMPLES


def obj(kind, name, spec=None):
    spec = copy.deepcopy(SAMPLES.get(kind, {})) if spec is None else spec
    return {"apiVersion": API, "kind": kind, "metadata": {"name": name}, "spec": spec}


def gcp_cluster(name, type_="gcp"):
    block = {"environment": "env", "workerCount": 2}
    return obj("PlatformOpenshift", name, {"type": type_, type_: block})


def without_name_rules(crds):
    """The CRDs as they were before the rule: no rule on metadata.name, no rule on the cluster's Google name."""
    out = {}
    for n, crd in crds.items():
        crd = copy.deepcopy(crd)
        for v in crd["spec"]["versions"]:
            s = v["schema"]["openAPIV3Schema"]
            md = s.get("properties", {}).get("metadata")
            if md and "properties" in md:
                md.pop("properties")
            rules = [r for r in s.get("x-kubernetes-validations", []) if "goog" not in r["rule"]]
            if rules:
                s["x-kubernetes-validations"] = rules
            else:
                s.pop("x-kubernetes-validations", None)
        out[n] = crd
    return out


class Run:
    def __init__(self, kube, ns):
        self.kube, self.ns, self.results = kube, ns, []

    def check(self, cid, desc, ok, detail=""):
        self.results.append({"id": cid, "desc": desc, "pass": bool(ok), "detail": detail[:400]})
        print(f"{'PASS' if ok else 'FAIL'} {cid:<10} {desc}" + ("" if ok else f"\n     {detail[:300]}"))

    def dry_create(self, o):
        r = self.kube.run(["create", "--dry-run=server", "-n", self.ns, "-f", "-"], body=o)
        return r.returncode, r.stderr.strip()

    def create(self, o):
        r = self.kube.run(["create", "-n", self.ns, "-f", "-"], body=o)
        return r.returncode, r.stderr.strip()


def plural(crds, kind):
    for crd in crds.values():
        if crd["spec"]["names"]["kind"] == kind:
            return crd["spec"]["names"]["plural"]
    raise SystemExit(f"no CRD of kind {kind}")


def name_cases(run, crds):
    for kind in RULED:
        limit = RULED[kind]
        for n in ACCEPTED + ["x" * limit]:
            _, err = run.dry_create(obj(kind, n))
            run.check(f"N-{kind[:8]}", f"{kind} {n[:12]!r} ({len(n)}) accepted by the rule", NAME_DENY not in err, err)
        for n in REFUSED + ["x" * (limit + 1)]:
            _, err = run.dry_create(obj(kind, n))
            run.check(f"N-{kind[:8]}", f"{kind} {n[:12]!r} ({len(n)}) refused with the rule",
                      NAME_DENY in err and "at most %d characters" % limit in err, err)
    for kind in UNRULED:
        for n in ["web.prod", "x" * 40]:
            _, err = run.dry_create(obj(kind, n))
            run.check(f"U-{kind[:8]}", f"{kind} {n[:12]!r} not refused by the rule for names", NAME_DENY not in err, err)


def sample_cases(run):
    """Every sample of the repository is accepted as it is (names and the bounded spec fields)."""
    import yaml
    for f in sorted((ROOT / "samples").rglob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text()):
            if not d or not str(d.get("apiVersion", "")).startswith(GROUP):
                continue
            d = copy.deepcopy(d)
            d["metadata"].pop("namespace", None)
            rc, err = run.dry_create(d)
            run.check("S-sample", f"{f.relative_to(ROOT)} {d['kind']} {d['metadata']['name']!r} accepted", rc == 0, err)


def google_cases(run):
    for n in GOOGLE_REFUSED:
        rc, err = run.dry_create(gcp_cluster(n))
        run.check("G-refuse", f"Google Cloud cluster {n!r} refused", rc != 0 and GOOGLE_DENY in err, err)
    for n in GOOGLE_ACCEPTED:
        rc, err = run.dry_create(gcp_cluster(n))
        run.check("G-accept", f"Google Cloud cluster {n!r} accepted", rc == 0, err)
    for t in ("openstack", "aws"):
        rc, err = run.dry_create(gcp_cluster("acme-google", t))
        run.check("G-other", f"{t} cluster 'acme-google' accepted (the rule is Google Cloud's)", rc == 0, err)


OLD = [("CloudOSO", "old.env-with-a-name-over-thirty-chars", None),
       ("PlatformOpenshift", "1web", {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3}}),
       ("PlatformOpenshift", "google-web", {"type": "gcp", "gcp": {"environment": "env", "workerCount": 2}}),
       ("Entity", "acme--corp", None),
       ("Rbac", "admins.old", None)]


def old_object_cases(run, kube, crds):
    """Objects made before the rule: created on the CRDs without it, then the CRDs with the rule are applied."""
    for kind, n, spec in OLD:
        o = obj(kind, n, spec)
        o["metadata"]["finalizers"] = ["example.invalid/hold"]
        rc, err = run.create(o)
        run.check("O-setup", f"{kind} {n!r} created before the rule", rc == 0, err)
    kube.apply_docs(list(crds.values()))
    replay.wait_established(kube, list(crds))
    time.sleep(3)
    for kind, n, _ in OLD:
        p = plural(crds, kind)
        r = kube.run(["patch", p, n, "-n", run.ns, "--type=merge", "--subresource=status",
                      "-p", json.dumps({"status": {"summary": "The name is not allowed."}})])
        run.check("O-status", f"{kind} {n!r}: a status write is accepted", r.returncode == 0, r.stderr)
        r = kube.run(["patch", p, n, "-n", run.ns, "--type=merge",
                      "-p", json.dumps({"metadata": {"labels": {"example": "x"}}})])
        run.check("O-meta", f"{kind} {n!r}: a label change is accepted", r.returncode == 0, r.stderr)
        r = kube.run(["delete", p, n, "-n", run.ns, "--wait=false"])
        run.check("O-delete", f"{kind} {n!r}: a delete is accepted", r.returncode == 0, r.stderr)
        r = kube.run(["patch", p, n, "-n", run.ns, "--type=merge", "-p", '{"metadata":{"finalizers":null}}'])
        run.check("O-final", f"{kind} {n!r}: the finalizer can be removed", r.returncode == 0, r.stderr)


def removal_cases(run, kube):
    """A writer that still sends a job's address or the floating IPs' network id: both are dropped by the server."""
    o = obj("PlatformOpenshift", "pruned", {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3}})
    rc, err = run.create(o)
    run.check("R-setup", "a cluster for the status writes", rc == 0, err)
    body = {"status": {"edaJobs": [{"name": "platformopenshift-reconcile", "jobId": "7", "status": "running",
                                    "timestamp": "2030-01-01T00:00:00Z", "url": "https://controller.example/x"}],
                       "floatingIPs": {"network": "n-1", "api": {"address": "192.0.2.10", "id": "f-1"}},
                       "slug": "abcde"}}
    r = kube.run(["patch", "platformopenshifts", "pruned", "-n", run.ns, "--type=merge", "--subresource=status",
                  "-p", json.dumps(body)])
    run.check("R-write", "the status write is accepted", r.returncode == 0, r.stderr)
    got, _, err = kube.json(["get", "platformopenshifts", "pruned", "-n", run.ns])
    st = (got or {}).get("status", {})
    job = (st.get("edaJobs") or [{}])[0]
    run.check("R-url", "the job's address is dropped, jobId kept", "url" not in job and job.get("jobId") == "7",
              json.dumps(st))
    run.check("R-fip", "the floating IPs' network id is dropped, the address kept",
              "network" not in st.get("floatingIPs", {}) and st["floatingIPs"]["api"]["address"] == "192.0.2.10",
              json.dumps(st))


def kept_cases(run, kube):
    """Status keys the jobs write and read back are kept by the server (the static walk of every write is
    tests/unit/test_status_writes_kept.py; these are its findings, on a real server)."""
    o = obj("PlatformOpenshift", "kept", {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3}})
    rc, err = run.create(o)
    run.check("K-setup", "a cluster for the status writes", rc == 0, err)
    fips = {"api": {"address": "192.0.2.10", "id": "f-1", "description": "OpenShift cluster web: API"},
            "ingress": {"address": "192.0.2.11", "id": "f-2", "description": "OpenShift cluster web: ingress"}}
    got = status_round_trip(run, kube, "platformopenshifts", "kept", {"floatingIPs": fips})
    run.check("K-fip", "the floating IPs keep their description, address and id",
              (got.get("floatingIPs") or {}) == fips, json.dumps(got))
    o = obj("AAPOrg", "kept")
    rc, err = run.create(o)
    run.check("K-setup", "an automation organization for the status writes", rc == 0, err)
    maps = [{"id": "12", "name": "acme_web-admin"}]
    got = status_round_trip(run, kube, "aaporgs", "kept", {"gatewayOrgId": "7", "signInMaps": maps})
    run.check("K-aaporg", "the organization keeps its gateway id and its sign-in maps",
              got.get("gatewayOrgId") == "7" and got.get("signInMaps") == maps, json.dumps(got))
    o = obj("CloudInfrastructure", "kept")
    rc, err = run.create(o)
    run.check("K-setup", "a site for the status writes", rc == 0, err)
    sign_in = {"clientId": "openshift-web", "idpName": "acme"}
    got = status_round_trip(run, kube, "cloudinfrastructures", "kept", {"site": {"signIn": sign_in}})
    run.check("K-site", "an OpenShift site keeps its sign-in client and button name",
              ((got.get("site") or {}).get("signIn") or {}) == sign_in, json.dumps(got))


def status_round_trip(run, kube, plural_name, name, status):
    r = kube.run(["patch", plural_name, name, "-n", run.ns, "--type=merge", "--subresource=status",
                  "-p", json.dumps({"status": status})])
    run.check("K-write", f"{plural_name}/{name}: the status write is accepted without a warning",
              r.returncode == 0 and "unknown field" not in r.stderr, r.stderr)
    got, _, _err = kube.json(["get", plural_name, name, "-n", run.ns])
    return (got or {}).get("status", {})


def project_cases(run, kube):
    """An OpenStack environment's spec.project is accepted and ignored for one release: a manifest of the release
    before (which had to set it) applies by every path the platform's clients use, and so does one without it."""
    with_project = obj("CloudOSO", "with-project", {"cloudRef": {"name": "site"}, "project": "web"})
    without = obj("CloudOSO", "without-project", {"cloudRef": {"name": "site"}})
    for o in (with_project, without):
        n = o["metadata"]["name"]
        rc, err = run.create(o)
        run.check("P-create", f"CloudOSO {n!r}: kubectl create (strict field validation) accepts it",
                  rc == 0 and "unknown field" not in err, err)
        r = kube.run(["apply", "--server-side", "--field-manager=gitops", "-n", run.ns, "-f", "-"], body=o)
        run.check("P-ssa", f"CloudOSO {n!r}: server-side apply (the GitOps controller's path) accepts it",
                  r.returncode == 0, r.stderr)
        r = kube.run(["replace", "-n", run.ns, "-f", "-"], body=o)
        run.check("P-replace", f"CloudOSO {n!r}: replace accepts it", r.returncode == 0, r.stderr)
    got, _, err = kube.json(["get", "cloudosos", "with-project", "-n", run.ns])
    run.check("P-kept", "the field is stored as given (accepted, no job reads it)",
              ((got or {}).get("spec") or {}).get("project") == "web", err or json.dumps(got))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--crds", required=True)
    ap.add_argument("--assets", default=os.environ.get("KUBEBUILDER_ASSETS", ""))
    ap.add_argument("--report")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    replay.KUBECTL = str(Path(args.assets) / "kubectl")
    crds = replay.load_crds(args.crds)
    load_samples()
    env = replay.Envtest(args.assets, args.keep)
    results = []
    try:
        env.start()
        kube = replay.Kube(env.kubeconfig)
        old = without_name_rules(crds)
        r = kube.run(["create", "-f", "-"], body={"apiVersion": "v1", "kind": "List", "items": list(old.values())})
        if r.returncode != 0:
            print(f"CRD install refused:\n{r.stderr}", file=sys.stderr)
            return 1
        replay.wait_established(kube, list(old))
        kube.run(["create", "namespace", "names-old"], check=True)
        kube.run(["create", "namespace", "names"], check=True)
        old_run = Run(kube, "names-old")
        old_object_cases(old_run, kube, crds)
        run = Run(kube, "names")
        name_cases(run, crds)
        google_cases(run)
        sample_cases(run)
        removal_cases(run, kube)
        kept_cases(run, kube)
        project_cases(run, kube)
        results = old_run.results + run.results
    finally:
        env.stop()
    failed = [r for r in results if not r["pass"]]
    print(f"\nnames: {len(results) - len(failed)}/{len(results)} checks pass")
    if args.report:
        Path(args.report).write_text(json.dumps(results, indent=1) + "\n")
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(main())

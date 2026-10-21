#!/usr/bin/env python3
"""SAR matrix: ask the Kubernetes API who may do what in a tenant, with SubjectAccessReviews.

A SubjectAccessReview names the user and groups explicitly, so no real users, no impersonation and no change on the
cluster are needed: the runner is safe against a live cluster.

Rows:
  generated  From the tenant's Entity spec.namespaceRbac and the role matrix the EDA jobs render
             (eda/rulebooks/roles/entity_provision/vars/main.yml, entity_named_roles): each access group
             <entity>.<rbac> gets exactly its roles' verbs on its roles' kinds in entity-<entity> (crud = get, list,
             watch, create, update, patch, delete; read = get, list, watch; <kind>/status get), and is denied every
             other (kind, verb) of the API group there. Kinds come from the CRD sources.
  cases      tests/rbac-access/cases/*.yaml, each a list of
               {id, resolves: [UX-..], principal: {user?, groups: [...]}, namespace, resource: {group, resource,
                subresource?, name?}, verbs: [...], expect: allow|deny}
             Placeholders: {entity}, {other_entity}, {automation} (the platform automation username),
             {group:<spec_key>} (the first access group mapped to that key; the case is skipped when none is),
             principal groups ["{group:*}"] = one case per mapped access group,
             resource "@shc-kinds" = every kind of the API group.

Clusters:
  live (default)       the current KUBECONFIG; the Entity is read from the cluster unless --entity-spec is given.
  --cluster envtest    a throwaway API server (tests/schema-replay/replay.py's envtest backend): the CRDs are
                       installed, and with --render-roles the tenant's Roles and RoleBindings are created by the
                       production task file (entity_provision/tasks/namespace_roles.yml, through
                       render-entity-roles.yml, sign-in groups stubbed); --automation-rules FILE binds a ClusterRole
                       with those rules to the automation username.

Known deviations (--known-deviations, default tests/rbac-access/known-deviations.yaml): [{id, finding, note}]; a
failing row with a listed id is reported as "known", not as a failure, and a listed row that passes is reported as
"fixed" so the entry can be removed.

Exit 1 on any unexpected allow or deny.

usage: python3 -I tests/rbac-access/sar_matrix.py --entity NAME [--other-entity NAME] [--entity-spec FILE]
         [--profile generated|<case file stem> ...] [--cluster live|envtest] [--render-roles] [--automation-rules FILE]
         [--users u1,u2] [--report OUT]
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("sar_matrix: PyYAML is required", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
GROUP = "hybridsovereign.redhat"
AUTOMATION = "system:serviceaccount:open-cluster-management-agent-addon:shc-automation"
CRUD = ["get", "list", "watch", "create", "update", "patch", "delete"]
READ = ["get", "list", "watch"]
ROLE_VARS = ROOT / "eda/rulebooks/roles/entity_provision/vars/main.yml"
CRD_SOURCES = ROOT / "operator/config/crd/bases"


def shc_kinds():
    """(plural, namespaced) of every kind, from the CRD sources."""
    out = []
    for f in sorted(CRD_SOURCES.glob("crd-*.yaml")):
        c = yaml.safe_load(f.read_text())
        out.append((c["spec"]["names"]["plural"], c["spec"]["scope"] == "Namespaced"))
    return out


def role_matrix():
    return yaml.safe_load(ROLE_VARS.read_text())["entity_named_roles"]


class Api:
    def __init__(self, kubectl, kubeconfig=None):
        self.kubectl = kubectl
        self.kubeconfig = kubeconfig

    def run(self, args, body=None):
        cmd = [self.kubectl] + (["--kubeconfig", self.kubeconfig] if self.kubeconfig else []) + args
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
        return r

    def sar(self, user, groups, namespace, resource, verb):
        attrs = {"verb": verb, "group": resource.get("group", ""), "resource": resource["resource"]}
        if namespace:
            attrs["namespace"] = namespace
        if resource.get("subresource"):
            attrs["subresource"] = resource["subresource"]
        if resource.get("name"):
            attrs["name"] = resource["name"]
        spec = {"resourceAttributes": attrs, "groups": list(groups or [])}
        if user:
            spec["user"] = user
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview", "spec": spec}
        r = self.run(["create", "-f", "-", "-o", "json"], body=body)
        if r.returncode != 0:
            raise RuntimeError(f"SubjectAccessReview failed: {r.stderr.strip()}")
        st = json.loads(r.stdout).get("status") or {}
        return bool(st.get("allowed")) and not st.get("denied"), st.get("reason", "")


def access_groups(entity, ns_rbac):
    """{spec_key: [<entity>.<rbac>, ...]} for the keys mapped in namespaceRbac."""
    return {k: [f"{entity}.{r}" for r in (v or [])] for k, v in (ns_rbac or {}).items() if v}


def generated_rows(entity, ns_rbac, kinds):
    roles = {r["spec_key"]: r for r in role_matrix()}
    groups = access_groups(entity, ns_rbac)
    by_group = {}
    for key, gs in groups.items():
        for g in gs:
            by_group.setdefault(g, set()).add(key)
    rows = []
    ns = f"entity-{entity}"
    for g, keys in sorted(by_group.items()):
        allowed = {}
        for k in keys:
            r = roles.get(k)
            if r is None:
                continue
            for res in r.get("crud_resources") or []:
                allowed.setdefault((res, ""), set()).update(CRUD)
                allowed.setdefault((res, "status"), set()).update(READ)
            for res in r.get("read_resources") or []:
                allowed.setdefault((res, ""), set()).update(READ)
                allowed.setdefault((res, "status"), set()).update(READ)
        for plural, namespaced in kinds:
            if not namespaced:
                continue
            for sub, verbs in (("", CRUD), ("status", ["get", "update", "patch"])):
                for verb in verbs:
                    expect = "allow" if verb in allowed.get((plural, sub), set()) else "deny"
                    rows.append({"id": f"gen-{g}-{plural}{'-status' if sub else ''}-{verb}",
                                 "resolves": [], "principal": {"groups": [g, "system:authenticated",
                                                                         "system:authenticated:oauth"]},
                                 "namespace": ns,
                                 "resource": {"group": GROUP, "resource": plural, **({"subresource": sub} if sub else {})},
                                 "verbs": [verb], "expect": expect, "source": "generated"})
    return rows


def expand_cases(cases, entity, other, ns_rbac, kinds, source):
    groups = access_groups(entity, ns_rbac)
    out = []
    skipped = []
    plurals = [p for p, _ in kinds]

    def subst(v, extra=None):
        if isinstance(v, str):
            s = v.replace("{entity}", entity).replace("{other_entity}", other).replace("{automation}", AUTOMATION)
            for k, val in (extra or {}).items():
                s = s.replace(k, val)
            return s
        if isinstance(v, list):
            return [subst(x, extra) for x in v]
        if isinstance(v, dict):
            return {k: subst(x, extra) for k, x in v.items()}
        return v

    for c in cases:
        c = copy.deepcopy(c)
        c["source"] = source
        pgroups = (c.get("principal") or {}).get("groups") or []
        variants = [dict()]
        if "{group:*}" in pgroups:
            all_groups = sorted({g for gs in groups.values() for g in gs})
            if not all_groups:
                skipped.append((c["id"], "no access group mapped"))
                continue
            variants = [{"{group:*}": g} for g in all_groups]
        missing = [p for p in json.dumps(c).split("{group:")[1:] if not p.startswith("*}")]
        keys = [m.split("}")[0] for m in missing]
        extra_base = {}
        bad = False
        for k in keys:
            if not groups.get(k):
                skipped.append((c["id"], f"no access group mapped to {k}"))
                bad = True
                break
            extra_base["{group:" + k + "}"] = groups[k][0]
        if bad:
            continue
        for var in variants:
            extra = {**extra_base, **var}
            x = subst(c, extra)
            if len(variants) > 1:
                x["id"] = f"{c['id']}[{var['{group:*}']}]"
            res = x["resource"]
            if res.get("resource") == "@shc-kinds":
                for p in plurals:
                    y = copy.deepcopy(x)
                    y["resource"] = {**res, "resource": p}
                    y["id"] = f"{x['id']}-{p}"
                    out.append(y)
            else:
                out.append(x)
    return out, skipped


def load_cases(profiles):
    out = {}
    for f in sorted((HERE / "cases").glob("*.yaml")):
        if profiles and f.stem not in profiles:
            continue
        out[f.stem] = yaml.safe_load(f.read_text()) or []
    return out


def find_kubectl(assets):
    for c in ((Path(assets) / "kubectl") if assets else None, shutil.which("kubectl"), shutil.which("oc")):
        if c and Path(c).exists():
            return str(c)
    raise SystemExit("sar_matrix: no kubectl")


def envtest_cluster(assets, keep):
    sys.path.insert(0, str(ROOT / "tests" / "schema-replay"))
    import replay  # noqa: E402  (the replay harness's envtest backend)
    replay.KUBECTL = find_kubectl(assets)
    b = replay.Envtest(assets, keep)
    b.start()
    return b, replay


def render_roles(backend, replay_mod, entity, ns_rbac):
    """Create the tenant's Roles and RoleBindings in the throwaway cluster with the production task file."""
    kube = replay_mod.Kube(backend.kubeconfig)
    crds = replay_mod.load_crds(ROOT / "operator/primary/helm/templates/crds.yaml")
    kube.apply_docs(list(crds.values()))
    replay_mod.wait_established(kube, list(crds))
    ns = f"entity-{entity}"
    kube.apply_docs([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}])
    rbacs = sorted({r for v in (ns_rbac or {}).values() for r in (v or [])})
    for r in rbacs:
        out, _, err = kube.json(["create", "-f", "-"], body={
            "apiVersion": f"{GROUP}/v1alpha1", "kind": "Rbac", "metadata": {"name": r, "namespace": ns},
            "spec": {"config": "sar-matrix", "description": "sar matrix"}})
        if out is None:
            raise RuntimeError(f"creating Rbac {r}: {err}")
        out["status"] = {"group": f"{entity}/{r}"}
        st, _, err = kube.json(["replace", "--subresource=status", "-f", "-"], body=out)
        if st is None:
            raise RuntimeError(f"Rbac {r} status: {err}")
    kc = yaml.safe_load(Path(backend.kubeconfig).read_text())
    server = kc["clusters"][0]["cluster"]["server"]
    token = kc["users"][0]["user"]["token"]
    with tempfile.TemporaryDirectory() as d:
        vars_file = Path(d) / "vars.json"
        vars_file.write_text(json.dumps({"services_api_host": server, "services_bearer_token": token,
                                         "render_entity": entity, "render_namespace_rbac": ns_rbac or {}}))
        os.chmod(vars_file, 0o600)
        r = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local",
                            str(HERE / "render-entity-roles.yml"), "-e", f"@{vars_file}"],
                           capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"render-entity-roles.yml failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}")
    roles = json.loads(kube.run(["-n", ns, "get", "roles,rolebindings", "-o", "json"]).stdout)["items"]
    return [f"{x['kind']}/{x['metadata']['name']}" for x in roles]


def bind_automation(backend, replay_mod, rules_file):
    rules = yaml.safe_load(Path(rules_file).read_text())
    if isinstance(rules, dict):
        rules = rules.get("rules", rules)
    kube = replay_mod.Kube(backend.kubeconfig)
    kube.apply_docs([
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "sar-automation"},
         "rules": rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": "sar-automation"},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "sar-automation"},
         "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": AUTOMATION}]}])


def user_groups(api, users):
    """{user: [groups]} from the cluster's OpenShift Groups (read-only)."""
    r = api.run(["get", "groups.user.openshift.io", "-o", "json"])
    if r.returncode != 0:
        raise RuntimeError(f"reading groups: {r.stderr.strip()}")
    out = {u: ["system:authenticated", "system:authenticated:oauth"] for u in users}
    for g in json.loads(r.stdout).get("items", []):
        for u in g.get("users") or []:
            if u in out:
                out[u].append(g["metadata"]["name"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--entity", required=True)
    ap.add_argument("--other-entity", default="sar-other-tenant")
    ap.add_argument("--entity-spec", help="YAML/JSON Entity spec (or a whole Entity) instead of reading the cluster")
    ap.add_argument("--entity-namespace", default="sovereign-cloud")
    ap.add_argument("--profile", action="append", help="generated and/or case file stems (default: all)")
    ap.add_argument("--cluster", choices=("live", "envtest"), default="live")
    ap.add_argument("--render-roles", action="store_true", help="envtest: render the tenant's Roles first")
    ap.add_argument("--automation-rules", help="envtest: ClusterRole rules (YAML list) bound to the automation user")
    ap.add_argument("--assets", default=os.environ.get("KUBEBUILDER_ASSETS", ""))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--users", help="comma-separated users: also run the generated rows with their real groups")
    ap.add_argument("--known-deviations", default=str(HERE / "known-deviations.yaml"))
    ap.add_argument("--report")
    a = ap.parse_args()

    profiles = set(a.profile or [])
    kinds = shc_kinds()
    backend = replay_mod = None
    if a.cluster == "envtest":
        backend, replay_mod = envtest_cluster(a.assets, a.keep)
        api = Api(replay_mod.KUBECTL, backend.kubeconfig)
    else:
        api = Api(find_kubectl(a.assets))
    report = {"entity": a.entity, "cluster": a.cluster, "rows": [], "skipped": [], "rendered": []}
    try:
        if a.entity_spec:
            spec = yaml.safe_load(Path(a.entity_spec).read_text()) or {}
            spec = spec.get("spec", spec)
        elif a.cluster == "live":
            r = api.run(["-n", a.entity_namespace, "get", f"entities.{GROUP}", a.entity, "-o", "json"])
            if r.returncode != 0:
                raise SystemExit(f"sar_matrix: reading Entity {a.entity}: {r.stderr.strip()}")
            spec = json.loads(r.stdout).get("spec") or {}
        else:
            spec = {}
        ns_rbac = spec.get("namespaceRbac") or {}
        if backend and a.render_roles:
            report["rendered"] = render_roles(backend, replay_mod, a.entity, ns_rbac)
        if backend and a.automation_rules:
            bind_automation(backend, replay_mod, a.automation_rules)

        rows = []
        if not profiles or "generated" in profiles:
            rows += generated_rows(a.entity, ns_rbac, kinds)
        for stem, cases in load_cases(profiles - {"generated"} if profiles else None).items():
            x, skipped = expand_cases(cases, a.entity, a.other_entity, ns_rbac, kinds, stem)
            rows += x
            report["skipped"] += [{"id": i, "why": w} for i, w in skipped]
        if a.users:
            ug = user_groups(api, [u.strip() for u in a.users.split(",") if u.strip()])
            for u, gs in ug.items():
                gen = generated_rows(a.entity, ns_rbac, kinds)
                # the user is allowed what any of their access groups is allowed
                best = {}
                for row in gen:
                    k = (row["resource"]["resource"], row["resource"].get("subresource", ""), row["verbs"][0])
                    if row["principal"]["groups"][0] in gs:
                        best[k] = "allow" if row["expect"] == "allow" or best.get(k) == "allow" else "deny"
                    else:
                        best.setdefault(k, "deny")
                for (res, sub, verb), expect in sorted(best.items()):
                    rows.append({"id": f"user-{u}-{res}{'-status' if sub else ''}-{verb}", "resolves": [],
                                 "principal": {"user": u, "groups": gs}, "namespace": f"entity-{a.entity}",
                                 "resource": {"group": GROUP, "resource": res, **({"subresource": sub} if sub else {})},
                                 "verbs": [verb], "expect": expect, "source": "users"})

        known = {k["id"]: k for k in (yaml.safe_load(Path(a.known_deviations).read_text()) or [])} \
            if Path(a.known_deviations).exists() else {}
        for row in rows:
            p = row["principal"] or {}
            for verb in row["verbs"]:
                allowed, reason = api.sar(p.get("user"), p.get("groups"), row.get("namespace"), row["resource"], verb)
                got = "allow" if allowed else "deny"
                ok = got == row["expect"]
                state = "ok" if ok else "UNEXPECTED"
                if row["id"] in known:
                    state = "known" if not ok else "fixed"
                report["rows"].append({"id": row["id"], "source": row["source"], "verb": verb,
                                       "expect": row["expect"], "got": got, "state": state,
                                       "resolves": row.get("resolves") or [],
                                       "finding": (known.get(row["id"]) or {}).get("finding", ""),
                                       "reason": reason if not ok else ""})
    finally:
        if backend:
            backend.stop()

    bad = [r for r in report["rows"] if r["state"] == "UNEXPECTED"]
    counts = {}
    for r in report["rows"]:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=1) + "\n")
    for r in report["rows"]:
        if r["state"] != "ok":
            print(f"  {r['state']:<10} {r['id']} {r['verb']}: expected {r['expect']}, got {r['got']}"
                  f"{(' (' + r['finding'] + ')') if r['finding'] else ''}{(' ' + r['reason']) if r['reason'] else ''}")
    for s in report["skipped"]:
        print(f"  skipped    {s['id']}: {s['why']}")
    if report["rendered"]:
        print(f"  rendered   {', '.join(report['rendered'])}")
    print(f"sar_matrix: entity {a.entity} ({a.cluster}): {len(report['rows'])} checks, "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f", {len(report['skipped'])} skipped")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Behaviour of the GitOps admission policies (shc-fabric-ledger-guard, shc-gitops-managed, shc-gitops-delete-guard)
and, with --tenant-namespace-guard, of shc-tenant-namespace-delete-guard, in a replay cluster.

Run by test-policies.sh through `replay.py --exec`: KUBECONFIG points at the throwaway API server that holds the
baseline objects and the rendered policies; REPLAY_KUBECTL is the kubectl to use. Every request that could change
an object is a server-side dry run, so the checks can run in any order.

usage: policy_checks.py --gitops-mode Warn|Deny [--fabric NS/NAME] [--object KIND/NS/NAME] [--unmarked KIND/NS/NAME]
                        [--tenant-namespace-guard]
"""
import argparse
import json
import os
import subprocess
import sys

AUTOMATION = "system:serviceaccount:open-cluster-management-agent-addon:shc-automation"
ARGO = "system:serviceaccount:kube-system:argocd-manager"
NS_CONTROLLER = "system:serviceaccount:kube-system:namespace-controller"
USER = "replay-tenant-user"
RESULTS = []


PLATFORM_GROUP = "sovereign-admin"


def kubectl(args, as_user=None, body=None, as_groups=()):
    cmd = [os.environ.get("REPLAY_KUBECTL", "kubectl")] + args + ([f"--as={as_user}"] if as_user else [])
    cmd += [f"--as-group={g}" for g in as_groups]
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}{(': ' + detail) if detail and not ok else ''}")


def setup(fabric, obj):
    fns, fname = fabric.split("/")
    kind, ons, oname = obj.split("/")
    binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
               "metadata": {"name": "replay-policy-users"},
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
               "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": u}
                            for u in (USER, AUTOMATION, ARGO, NS_CONTROLLER)]}
    rc, _, err = kubectl(["apply", "-f", "-"], body=binding)
    if rc:
        sys.exit(f"policy_checks: binding: {err}")
    rc, _, err = kubectl(["-n", ons, "label", kind, oname, "hybridsovereign.redhat/managed-by=gitops", "--overwrite"])
    rc2, _, err2 = kubectl(["-n", ons, "annotate", kind, oname, "--overwrite",
                            f"hybridsovereign.redhat/gitops-source=test-app/{kind}s/{oname}"])
    if rc or rc2:
        sys.exit(f"policy_checks: marking {obj}: {err or err2}")
    return fns, fname, kind, ons, oname


def fabric_checks(fns, fname):
    def delete_dry():
        return kubectl(["-n", fns, "delete", "hybridfabric", fname, "--dry-run=server"])

    rc, _, err = delete_dry()
    check("P12 denies deleting a fabric without the label whose status holds allocations",
          rc != 0 and "still holds" in err, err.strip())
    kubectl(["-n", fns, "label", "hybridfabric", fname, "hybridsovereign.redhat/allocations=11", "--overwrite"])
    rc, _, err = delete_dry()
    check("P12 denies deleting a fabric labelled with 11 allocations, naming the count",
          rc != 0 and "11 address allocations" in err, err.strip())
    rc, _, err = kubectl(["-n", fns, "delete", "hybridfabric", fname, "--dry-run=server"], as_user=AUTOMATION)
    check("P12 has no exemption (automation denied too)", rc != 0 and "still holds" in err, err.strip())
    kubectl(["-n", fns, "label", "hybridfabric", fname, "hybridsovereign.redhat/allocations=0", "--overwrite"])
    rc, _, err = delete_dry()
    check("P12 allows deleting a fabric labelled 0", rc == 0, err.strip())
    kubectl(["-n", fns, "label", "hybridfabric", fname, "hybridsovereign.redhat/allocations=bogus", "--overwrite"])
    rc, _, err = delete_dry()
    check("P12 treats an unparsable label as holding allocations", rc != 0, err.strip())
    kubectl(["-n", fns, "label", "hybridfabric", fname, "hybridsovereign.redhat/allocations=3", "--overwrite"])
    kubectl(["-n", fns, "annotate", "hybridfabric", fname, "hybridsovereign.redhat/allow-delete-with-allocations=test",
             "--overwrite"])
    rc, _, err = delete_dry()
    check("P12 break-glass annotation allows the delete", rc == 0, err.strip())
    kubectl(["-n", fns, "annotate", "hybridfabric", fname, "hybridsovereign.redhat/allow-delete-with-allocations-"])


def gitops_checks(mode, kind, ns, name):
    deny = mode == "Deny"

    def patch(body, as_user):
        return kubectl(["-n", ns, "patch", kind, name, "--type=merge", "-p", json.dumps(body), "--dry-run=server"],
                       as_user=as_user)

    def flagged(rc, err):
        return (rc != 0 and "managed in Git" in err) if deny else (rc == 0 and "managed in Git" in err)

    spec_change = {"spec": {"displayName": "changed by a replay test"}}
    rc, _, err = patch(spec_change, USER)
    check(f"P4 ({mode}) flags a spec change by a non-exempt user, naming the Git source",
          flagged(rc, err) and "test-app/" in err, err.strip())
    for who in (AUTOMATION, ARGO):
        rc, _, err = patch(spec_change, who)
        check(f"P4 exempts {who}", rc == 0 and "managed in Git" not in err, err.strip())
    rc, _, err = patch({"metadata": {"annotations": {"ansible.sdk.operatorframework.io/reconcileNow": "x"}}}, USER)
    check("P4 lets a metadata-only change through", rc == 0 and "managed in Git" not in err, err.strip())
    rc, _, err = patch({"metadata": {"labels": {"hybridsovereign.redhat/managed-by": None}}}, USER)
    check(f"P4 ({mode}) flags removing the managed-by label", flagged(rc, err), err.strip())
    rc, _, err = patch({"metadata": {"annotations": {"hybridsovereign.redhat/gitops-override": "incident 1"}},
                        **spec_change}, USER)
    check("P4 break-glass override annotation lets the change through", rc == 0 and "managed in Git" not in err,
          err.strip())
    rc, _, err = kubectl(["-n", ns, "patch", kind, name, "--subresource=status", "--type=merge", "-p",
                          json.dumps({"status": {"message": "status write"}}), "--dry-run=server"], as_user=USER)
    check("P4 does not match status writes", rc == 0 and "managed in Git" not in err, err.strip())


def delete_guard_checks(kind, ns, name, unmarked):
    def delete_dry(k, n, nm, as_user=USER):
        return kubectl(["-n", n, "delete", k, nm, "--dry-run=server"], as_user=as_user)

    rc, _, err = delete_dry(kind, ns, name)
    check("delete guard denies deleting a GitOps-managed object, naming the Git source",
          rc != 0 and "managed in Git" in err and "test-app/" in err, err.strip())
    for who in (AUTOMATION, ARGO, NS_CONTROLLER):
        rc, _, err = delete_dry(kind, ns, name, who)
        check(f"delete guard exempts {who}", rc == 0, err.strip())
    if unmarked:
        uk, uns, un = unmarked.split("/")
        rc, _, err = delete_dry(uk, uns, un)
        check("delete guard does not match an object without the managed-by label", rc == 0, err.strip())
    rc, _, err = kubectl(["-n", ns, "annotate", kind, name, "hybridsovereign.redhat/gitops-override=replay test",
                          "--overwrite"])
    if rc:
        check("delete guard: set the override annotation", False, err.strip())
        return
    rc, _, err = delete_dry(kind, ns, name)
    check("delete guard break-glass override annotation lets the delete through", rc == 0, err.strip())
    kubectl(["-n", ns, "annotate", kind, name, "hybridsovereign.redhat/gitops-override-"])


def tenant_namespace_guard_checks():
    """shc-tenant-namespace-delete-guard (UX-102): a tenant namespace is deleted through its Entity."""
    ns, entity, plain = "entity-replay-guard", "replay-guard", "replay-plain-ns"
    for name, labels in ((ns, {"hybridsovereign.redhat/entity": entity}), (plain, {})):
        body = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}
        rc, _, err = kubectl(["apply", "-f", "-"], body=body)
        if rc:
            check(f"namespace guard: create {name}", False, err.strip())
            return

    def delete_dry(name, as_user=USER, groups=()):
        return kubectl(["delete", "namespace", name, "--dry-run=server"], as_user=as_user, as_groups=groups)

    def annotate(value):
        arg = "hybridsovereign.redhat/entity-deleting" + (f"={value}" if value is not None else "-")
        return kubectl(["annotate", "namespace", ns, arg] + (["--overwrite"] if value is not None else []))

    rc, _, err = delete_dry(ns)
    check("namespace guard denies a direct delete of a tenant namespace, naming the Entity",
          rc != 0 and f"Delete the tenant (Entity {entity})" in err, err.strip())
    rc, _, err = delete_dry(ns, groups=(PLATFORM_GROUP,))
    check("namespace guard denies a platform group too (exemptPlatformGroups false)",
          rc != 0 and "Delete the tenant" in err, err.strip())
    rc, _, err = delete_dry(ns, AUTOMATION)
    check("namespace guard exempts platform automation", rc == 0, err.strip())
    rc, _, err = delete_dry(plain)
    check("namespace guard does not match a namespace without the tenant label", rc == 0, err.strip())
    annotate("another-entity")
    rc, _, err = delete_dry(ns)
    check("namespace guard denies when the annotation names another Entity", rc != 0, err.strip())
    annotate(entity)
    rc, _, err = delete_dry(ns)
    check("namespace guard admits the delete once the Entity's deletion marked the namespace", rc == 0, err.strip())
    annotate(None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gitops-mode", choices=("Warn", "Deny"), required=True)
    ap.add_argument("--fabric", default="sovereign-cloud/lab")
    ap.add_argument("--object", default="cloudinfrastructure/sovereign-cloud/iaas1")
    ap.add_argument("--unmarked", default="",
                    help="KIND/NS/NAME of an object of a guarded kind without the managed-by label (optional)")
    ap.add_argument("--tenant-namespace-guard", action="store_true",
                    help="also check shc-tenant-namespace-delete-guard (rendered with exemptPlatformGroups false)")
    a = ap.parse_args()
    fns, fname, kind, ons, oname = setup(a.fabric, a.object)
    fabric_checks(fns, fname)
    gitops_checks(a.gitops_mode, kind, ons, oname)
    delete_guard_checks(kind, ons, oname, a.unmarked)
    if a.tenant_namespace_guard:
        tenant_namespace_guard_checks()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"policy_checks: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

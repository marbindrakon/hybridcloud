#!/usr/bin/env python3
"""Behaviour of the GitOps admission policies (shc-fabric-ledger-guard, shc-gitops-managed) in a replay cluster.

Run by test-policies.sh through `replay.py --exec`: KUBECONFIG points at the throwaway API server that holds the
baseline objects and the rendered policies; REPLAY_KUBECTL is the kubectl to use. Every request that could change
an object is a server-side dry run, so the checks can run in any order.

usage: policy_checks.py --gitops-mode Warn|Deny [--fabric NS/NAME] [--object KIND/NS/NAME]
"""
import argparse
import json
import os
import subprocess
import sys

AUTOMATION = "system:serviceaccount:open-cluster-management-agent-addon:shc-automation"
ARGO = "system:serviceaccount:kube-system:argocd-manager"
USER = "replay-tenant-user"
RESULTS = []


def kubectl(args, as_user=None, body=None):
    cmd = [os.environ.get("REPLAY_KUBECTL", "kubectl")] + args + ([f"--as={as_user}"] if as_user else [])
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
                            for u in (USER, AUTOMATION, ARGO)]}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gitops-mode", choices=("Warn", "Deny"), required=True)
    ap.add_argument("--fabric", default="sovereign-cloud/lab")
    ap.add_argument("--object", default="cloudinfrastructure/sovereign-cloud/iaas1")
    a = ap.parse_args()
    fns, fname, kind, ons, oname = setup(a.fabric, a.object)
    fabric_checks(fns, fname)
    gitops_checks(a.gitops_mode, kind, ons, oname)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"policy_checks: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

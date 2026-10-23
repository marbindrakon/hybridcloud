"""The Assignment hub account reaches tenant-built clusters only (R2B-F1).

An enforcing ACM ConfigurationPolicy runs with cluster-admin rights on every cluster its Placement selects, and a
Placement selects only from the ManagedClusterSets bound in its namespace. So:
- the assignment-central-rbac chart binds the policy namespace to the tenant cluster set alone (never default or
  global) and gives the account no ManagedClusterSetBinding or bind rights;
- the central chart creates that set, hands its name to the rbac chart, binds it in the namespace of the
  PlatformOpenshift per-cluster policies, and guards it with two admission policies;
- the PlatformOpenshift jobs label every cluster they build into the same set.
"""
import os
import re
import shutil
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT, by_name, load  # noqa: E402

RBAC_CHART = os.path.join(ROOT, "charts", "charts", "assignment-central-rbac")
CENTRAL_CHART = os.path.join(ROOT, "bootstrap", "helm", "central")
PO_ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
ASSIGN_DEFAULTS = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_provision", "defaults", "main.yml")
SET_LABEL = "cluster.open-cluster-management.io/clusterset"


def helm(chart, *args):
    out = subprocess.run(["helm", "template", "x", chart, *args], capture_output=True, text=True)
    docs = [d for d in yaml.safe_load_all(out.stdout) if d] if out.returncode == 0 else []
    return out, docs


def central(*templates, sets=()):
    args = []
    for t in templates:
        args += ["--show-only", t]
    for s in sets:
        args += ["--set", s]
    out, docs = helm(CENTRAL_CHART, *args)
    assert out.returncode == 0, out.stderr
    return docs


def job_default_set():
    raw = load(os.path.join(PO_ROLE, "defaults", "main.yml"))["platformopenshift_managed_cluster_set"]
    m = re.search(r"default\('([a-z0-9-]+)'", raw)
    assert m, raw
    return m.group(1)


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class RbacChart(unittest.TestCase):
    def test_binds_only_the_tenant_set(self):
        out, docs = helm(RBAC_CHART)
        self.assertEqual(out.returncode, 0, out.stderr)
        binds = [d for d in docs if d["kind"] == "ManagedClusterSetBinding"]
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["spec"]["clusterSet"], "sovereign-tenant-clusters")
        self.assertEqual(binds[0]["metadata"]["name"], binds[0]["spec"]["clusterSet"])

    def test_account_cannot_bind_sets(self):
        _, docs = helm(RBAC_CHART)
        self.assertFalse([d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")])
        role = next(d for d in docs if d["kind"] == "Role")
        resources = {r for rule in role["rules"] for r in rule["resources"]}
        self.assertEqual(resources, {"policies", "placementbindings", "placements"})

    def test_refuses_default_and_global(self):
        for cs in ("default", "global", ""):
            out, _ = helm(RBAC_CHART, "--set", f"clusterSet={cs}")
            self.assertNotEqual(out.returncode, 0, cs)
            self.assertIn("clusterSet must name a set of tenant clusters", out.stderr)


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class CentralChart(unittest.TestCase):
    def test_one_name_everywhere(self):
        docs = central("templates/centralCluster/acm-tenant-clusterset.yaml",
                       "templates/centralCluster/acm-policy-clusterset-binding.yaml",
                       "templates/hybridSovereignOperators/assignment-central-rbac-application.yaml")
        name = next(d for d in docs if d["kind"] == "ManagedClusterSet")["metadata"]["name"]
        self.assertEqual(name, job_default_set())
        app = next(d for d in docs if d["kind"] == "Application")
        rbac_values = yaml.safe_load(app["spec"]["source"]["helm"]["values"])
        self.assertEqual(rbac_values["clusterSet"], name)
        self.assertEqual(rbac_values["namespace"], load(ASSIGN_DEFAULTS)["acm_policy_namespace"])
        gitops = [d for d in docs if d["kind"] == "ManagedClusterSetBinding"]
        self.assertEqual({d["spec"]["clusterSet"] for d in gitops}, {"default", name})

    def test_admission_guards(self):
        docs = central("templates/centralCluster/acm-tenant-clusterset.yaml",
                       sets=["acmTenantClusterSet.name=t-set", "acmTenantClusterSet.platformClusters={p1,p2}"])
        vaps = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"}
        vapbs = {d["spec"]["policyName"]: d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding"}
        self.assertEqual(set(vaps), set(vapbs))
        for b in vapbs.values():
            self.assertEqual(b["spec"]["validationActions"], ["Deny"])
        bind = vaps["sovereign-tenant-clusterset-binding"]["spec"]
        self.assertEqual(bind["matchConstraints"]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "sovereign-cloud-helpers"})
        self.assertEqual(bind["validations"][0]["expression"], "object.spec.clusterSet == 't-set'")
        # the garbage collector's finalizer removal on an old binding must pass
        self.assertEqual([m["expression"] for m in bind["matchConditions"]],
                         ["!has(object.metadata.deletionTimestamp)"])
        members = vaps["sovereign-tenant-clusterset-members"]["spec"]
        self.assertIn("== 't-set'", members["matchConditions"][0]["expression"])
        self.assertIn('["p1","p2"]', members["validations"][0]["expression"])
        self.assertIn("local-cluster", members["validations"][0]["expression"])

    def test_can_be_turned_off(self):
        out, _ = helm(CENTRAL_CHART, "--show-only", "templates/centralCluster/acm-tenant-clusterset.yaml",
                      "--set", "acmTenantClusterSet.enabled=false")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("could not find template", out.stderr)


class Jobs(unittest.TestCase):
    def test_openstack_build_values_name_the_set(self):
        with open(os.path.join(PO_ROLE, "templates", "build-values.yaml.j2")) as f:
            self.assertIn('managedClusterSet: "{{ platformopenshift_managed_cluster_set }}"', f.read())

    def test_imported_clusters_name_the_set(self):
        block = by_name(load(os.path.join(PO_ROLE, "tasks", "aws_clusterbuild_postinstall.yml")),
                        "Import the cluster into the hub")
        mc = by_name(block["block"], "The ManagedCluster")["kubernetes.core.k8s"]["definition"]
        self.assertEqual(mc["metadata"]["labels"][SET_LABEL], "{{ platformopenshift_managed_cluster_set }}")


if __name__ == "__main__":
    unittest.main()

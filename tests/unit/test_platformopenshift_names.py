"""Names the PlatformOpenshift jobs derive from a build name fit their limits (Review R3 N1).

A build name is <prefix>-<readable>-<8 hex> (files/po_preflight.py build-name), at most 53 characters on OpenStack
(a Helm release's limit) and 40 on AWS and Google Cloud; a recorded build name of an older job is kept as it is (up to
53). Every name derived from it for an object with a length limit must fit for every build name the scheme can
produce, stay unique per build and deterministic:
- the ACM objects in openshift-gitops (keycloak_oidc.yml, cluster_rbac.yml): ACM refuses a policy whose namespace
  and name together exceed 62 characters (its copy on a managed cluster is <namespace>.<name>, also a label value of
  at most 63); a Placement's name is a label value of its decisions (63); template names stay within 63 as well;
- the teardown (delete_acm.yml) removes the objects under the names the provision job used, and under the build name
  itself when that differs (an older job's objects).
Example names only.
"""
import importlib.util
import os
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402
from play_helper import HAVE_ANSIBLE, ROOT, load, walk  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
TASKS = os.path.join(ROLE, "tasks")
spec = importlib.util.spec_from_file_location("po_preflight", os.path.join(ROLE, "files", "po_preflight.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

ACM_NS = "openshift-gitops"
COMBINED = 62      # ACM: policy namespace + policy name
LABEL = 63         # a label value (the replicated policy's root-policy label, a Placement's decisions)
ACM_KINDS = {"Policy", "Placement", "PlacementBinding", "ConfigurationPolicy"}
URL_KINDS = {"policies": "Policy", "placements": "Placement", "placementbindings": "PlacementBinding"}


def build(entity, name, prefix="shc-oso", limit=53):
    return P.build_name({"prefix": prefix, "entity": entity, "name": name, "max": limit})["buildName"]


# every shape the scheme can produce at its longest, plus recorded names of the older scheme (no digest, up to 53)
WORST = [
    build("e" * 56, "c" * 200),                                   # OpenStack, the readable part cut, 53
    build("acme-corp", "web-frontend"),                           # the review's example
    build("e" * 56, "c" * 200, "shc-aws", 40),                    # AWS, 40
    build("e" * 56, "c" * 200, "shc-gcp", 40),                    # Google Cloud, 40
    build("uxr-corp", "demo-cluster1", "shc-gcp", 40),
    "shc-oso-" + "a" * 45,                                        # an older job's recorded name, 53
    "shc-oso-acme-prod",                                          # a short recorded name: its objects keep their names
]


def acm_vars(build_name):
    names = getattr(P, "acm_names", None)
    base = names({"buildName": build_name, "namespace": ACM_NS})["base"] if names else build_name
    return {"cluster_name": build_name, "acm_base": base}


def render(text, variables):
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(text).render(**variables)


def acm_object_names(path):
    """(kind, name template) of every ACM object name a task file writes or reads in openshift-gitops."""
    out = []
    for task in walk(load(path)):
        stack = [task]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("kind") in ACM_KINDS and isinstance(node.get("metadata"), dict) and "name" in node["metadata"]:
                    out.append((node["kind"], node["metadata"]["name"]))
                if isinstance(node.get("placementRef"), dict):
                    out.append(("Placement", node["placementRef"]["name"]))
                for s in node.get("subjects") or []:
                    if isinstance(s, dict) and s.get("kind") == "Policy":
                        out.append(("Policy", s["name"]))
                url = node.get("url")
                if isinstance(url, str) and f"/namespaces/{ACM_NS}/" in url:
                    plural = url.rsplit(f"/namespaces/{ACM_NS}/", 1)[1].split("/")
                    if len(plural) == 2:
                        out.append((URL_KINDS[plural[0]], plural[1]))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return out


class AcmNames(unittest.TestCase):
    FILES = [os.path.join(TASKS, "keycloak_oidc.yml"), os.path.join(TASKS, "cluster_rbac.yml")]

    def test_every_acm_name_fits_for_every_build_name(self):
        found = {}
        for path in self.FILES:
            for kind, tmpl in acm_object_names(path):
                found.setdefault(kind, set()).add(tmpl)
        self.assertEqual(set(found), ACM_KINDS)
        for b in WORST:
            v = acm_vars(b)
            for kind, tmpls in found.items():
                for t in tmpls:
                    n = render(t, v)
                    if kind == "Policy":
                        self.assertLessEqual(len(ACM_NS) + len(n), COMBINED, (b, n))
                        self.assertLessEqual(len(f"{ACM_NS}.{n}"), LABEL, (b, n))
                    self.assertLessEqual(len(n), LABEL, (kind, b, n))
                    self.assertRegex(n, r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", (kind, b, n))

    def test_an_existing_build_keeps_its_names(self):
        # names that fit were made by earlier jobs under the build name: they stay
        for b in ("shc-oso-acme-prod", "shc-oso-acme-prod-3c627a32", "x" * 32):
            self.assertEqual(acm_vars(b)["acm_base"], b)

    def test_unique_and_deterministic(self):
        many = {build(f"t{i % 7}" + "e" * 40, f"cluster-{i}" + "c" * 30) for i in range(300)}
        many |= {build(f"t{i % 7}" + "e" * 40, f"cluster-{i}" + "c" * 30, "shc-aws", 40) for i in range(300)}
        many |= {"shc-oso-" + "a" * 44 + str(i) for i in range(10)}
        bases = {b: acm_vars(b)["acm_base"] for b in many}
        self.assertEqual(len(set(bases.values())), len(many))
        for b in list(many)[:20]:
            self.assertEqual(acm_vars(b)["acm_base"], bases[b])

    def test_the_short_form_keeps_the_readable_head(self):
        b = build("acme-corp", "web-frontend")
        base = acm_vars(b)["acm_base"]
        self.assertNotEqual(base, b)
        self.assertTrue(base.startswith("shc-oso-acme-corp-web"), base)
        self.assertRegex(base, r"-[0-9a-f]{8}$")

    def test_no_acm_name_from_the_build_name_alone(self):
        for path in self.FILES:
            for kind, tmpl in acm_object_names(path):
                self.assertNotIn("cluster_name", tmpl, (path, kind, tmpl))


class TenantRbacReadsTheCluster(unittest.TestCase):
    def test_every_provider_names_the_cluster_object(self):
        # cluster_rbac.yml reads the PlatformOpenshift by cluster_cr_name, else by cluster_name (the build name, not
        # an object's name): every provider passes the object's own name
        for f in ("openstack.yml", "aws.yml", "gcp.yml"):
            incs = [t for t in walk(load(os.path.join(TASKS, f)))
                    if t.get("ansible.builtin.include_tasks") == "keycloak_oidc.yml"]
            self.assertEqual(len(incs), 1, f)
            self.assertEqual(incs[0]["vars"].get("cluster_cr_name"), "{{ ansible_operator_meta.name }}", f)


class TeardownAcmNames(unittest.TestCase):
    def deleted(self, build_name):
        rc, tr, out = H.run("platformopenshift_teardown", "delete_acm.yml",
                            {"central_api_host": "https://198.18.0.10:6443", "central_bearer_token": "token-not-in-data",
                             "po_build_name": build_name})
        self.assertEqual(rc, 0, out)
        return [(t["k8s"]["kind"], t["k8s"]["name"]) for t in tr if "k8s" in t and t["k8s"]["namespace"] == ACM_NS]

    def test_the_teardown_removes_what_the_provision_job_made(self):
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        b = build("acme-corp", "web-frontend")
        base = acm_vars(b)["acm_base"]
        got = self.deleted(b)
        for kind, suffix in (("Policy", "keycloak-oidc"), ("Placement", "keycloak-oidc-placement"),
                             ("PlacementBinding", "keycloak-oidc-binding"), ("Policy", "cluster-rbac"),
                             ("Placement", "cluster-rbac-placement"), ("PlacementBinding", "cluster-rbac-binding")):
            self.assertIn((kind, f"{base}-{suffix}"), got)
            self.assertIn((kind, f"{b}-{suffix}"), got)     # an older job's objects under the build name

    def test_a_short_build_name_is_removed_once(self):
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        got = self.deleted("shc-oso-acme-prod")
        self.assertEqual(len(got), 6)
        self.assertIn(("Policy", "shc-oso-acme-prod-keycloak-oidc"), got)


if __name__ == "__main__":
    unittest.main()

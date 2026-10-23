"""The ACM creator credential reaches services as the Secret the Assignment jobs read (OD48).

The primary operator chart renders ExternalSecret assignment-acm-creator-sa (off by default) from the services
store, with the two keys the jobs decode (token, ca.crt), in the namespace and under the name the jobs' defaults
name (assignment_operator_namespace, acm_central_sa_secret_name).
"""
import os
import shutil
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT, by_name, load  # noqa: E402

CHART = os.path.join(ROOT, "operator", "primary", "helm")
TEMPLATE = "templates/externalsecret-acm-creator.yaml"
PROV_DEFAULTS = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_provision", "defaults", "main.yml")
TEARDOWN = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_teardown", "tasks", "main.yml")


def render(*sets):
    args = ["helm", "template", "hybridsovereign-primary-operator", CHART, "--show-only", TEMPLATE]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True)
    return out


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class ExternalSecret(unittest.TestCase):
    def test_off_by_default(self):
        out = render()
        self.assertNotEqual(out.returncode, 0)  # --show-only of an empty template fails
        self.assertIn("could not find template", out.stderr)

    def test_matches_what_the_jobs_read(self):
        out = render("assignmentAcmCreator.enabled=true")
        self.assertEqual(out.returncode, 0, out.stderr)
        es = yaml.safe_load(out.stdout)
        defaults = load(PROV_DEFAULTS)
        self.assertEqual(es["kind"], "ExternalSecret")
        self.assertEqual(es["metadata"]["name"], defaults["acm_central_sa_secret_name"])
        self.assertEqual(es["spec"]["target"]["name"], defaults["acm_central_sa_secret_name"])
        self.assertEqual(es["metadata"]["namespace"], defaults["assignment_operator_namespace"])
        # the teardown sets the same two names itself
        parse = by_name(load(TEARDOWN), "Parse event payload")["ansible.builtin.set_fact"]
        self.assertEqual(parse["acm_central_sa_secret_name"], defaults["acm_central_sa_secret_name"])
        self.assertEqual(parse["assignment_operator_namespace"], defaults["assignment_operator_namespace"])
        self.assertEqual({d["secretKey"] for d in es["spec"]["data"]}, {"token", "ca.crt"})
        self.assertEqual({d["remoteRef"]["property"] for d in es["spec"]["data"]}, {"token", "ca.crt"})
        self.assertEqual({d["remoteRef"]["key"] for d in es["spec"]["data"]}, {"assignment-acm-creator"})
        self.assertEqual(es["spec"]["secretStoreRef"], {"name": "vault-backend", "kind": "ClusterSecretStore"})

    def test_values_override(self):
        out = render("assignmentAcmCreator.enabled=true", "assignmentAcmCreator.vaultPath=x/y",
                     "assignmentAcmCreator.secretStoreName=other")
        es = yaml.safe_load(out.stdout)
        self.assertEqual({d["remoteRef"]["key"] for d in es["spec"]["data"]}, {"x/y"})
        self.assertEqual(es["spec"]["secretStoreRef"]["name"], "other")


if __name__ == "__main__":
    unittest.main()

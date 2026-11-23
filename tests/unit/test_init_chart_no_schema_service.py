"""The init chart (bootstrap/helm/init) no longer carries the Argo CD schema compatibility service: rendered with its
defaults and with every bootstrap layer on, it contains no APIService and no object named or namespaced
argocd-schema-fix or schema-fix-*. An aggregated API of the product's own that nothing needs only makes the hub's
namespace deletes wait while it is unavailable."""
import os
import shutil
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

INIT = os.path.join(ROOT, "bootstrap", "helm", "init")


def render(*sets):
    args = ["helm", "template", "t", INIT]
    for s in sets:
        args += ["--set", s]
    p = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise AssertionError(p.stderr[-2000:])
    return [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]


def schema_service_objects(docs):
    def ours(value):
        return value == "argocd-schema-fix" or str(value).startswith("schema-fix-")
    return ["%s/%s" % (d.get("kind"), d["metadata"].get("name")) for d in docs
            if d.get("kind") == "APIService" or ours(d["metadata"].get("name"))
            or ours(d["metadata"].get("namespace"))]


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class NoSchemaService(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(schema_service_objects(render()), [])

    def test_with_the_operator_bootstrap_and_every_layer_on(self):
        for sets in (["bootstrap.operator=true"],
                     ["bootstrap.operator=true", "bootstrap.secrets=true", "bootstrap.applicationset=true",
                      "clusters.services.bearerToken=example-token"]):
            with self.subTest(sets=sets):
                docs = render(*sets)
                self.assertTrue(docs)
                self.assertEqual(schema_service_objects(docs), [])

    def test_no_value_is_left_behind(self):
        with open(os.path.join(INIT, "values.yaml")) as f:
            values = yaml.safe_load(f)
        self.assertNotIn("argocdSchemaFix", values)
        self.assertNotIn("argocd-schema-fix", values["pullSecretNamespaces"])


if __name__ == "__main__":
    unittest.main()

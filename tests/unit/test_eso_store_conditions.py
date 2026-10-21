"""vault-secret-store spec.conditions: the namespace limit on the services ClusterSecretStore.

The store's conditions are the only thing that keeps an ExternalSecret (read) or a PushSecret (write) in a
namespace outside the platform list from using the store's Vault role. These tests pin the render path:
- the chart's default render carries no conditions (an unconditioned store keeps its old shape);
- conditions given to the chart reach spec.conditions unchanged;
- the central Application forwards vaultSecretStoreServices.values.conditions to the chart, end to end;
- when central values set conditions, they point at a chart version that renders them (an older chart
  ignores the key and leaves the store open to every namespace without any error), and every entry is a
  well-formed exact namespace list.
"""
import json
import os
import shutil
import subprocess
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHART = os.path.join(ROOT, "charts/charts/vault-secret-store")
CENTRAL = os.path.join(ROOT, "bootstrap/helm/central")
CENTRAL_APP = "templates/servicesCluster/vault-backend-store-services-application.yaml"
HELM = shutil.which("helm")


def helm_template(chart, *args):
    out = subprocess.run([HELM, "template", "probe", chart, *args], check=True, capture_output=True,
                         text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def store(docs):
    stores = [d for d in docs if d.get("kind") == "ClusterSecretStore"]
    if len(stores) != 1:
        raise AssertionError("expected one ClusterSecretStore, got %d" % len(stores))
    return stores[0]


@unittest.skipIf(yaml is None, "PyYAML not installed")
@unittest.skipIf(HELM is None, "helm not installed")
class StoreConditions(unittest.TestCase):
    def test_default_render_has_no_conditions(self):
        spec = store(helm_template(CHART))["spec"]
        self.assertNotIn("conditions", spec)
        self.assertIn("provider", spec)

    def test_conditions_reach_the_store(self):
        conditions = [{"namespaces": ["ns-a", "ns-b"]}, {"namespaceSelector": {"matchLabels": {"k": "v"}}}]
        spec = store(helm_template(CHART, "--set-json", "conditions=" + json.dumps(conditions)))["spec"]
        self.assertEqual(spec["conditions"], conditions)

    def test_central_application_forwards_conditions(self):
        conditions = [{"namespaces": ["ns-a", "ns-b"]}]
        docs = helm_template(CENTRAL, "--show-only", CENTRAL_APP, "--set-json",
                             "vaultSecretStoreServices.values.conditions=" + json.dumps(conditions))
        self.assertEqual(len(docs), 1)
        values = yaml.safe_load(docs[0]["spec"]["source"]["helm"]["values"])
        self.assertEqual(values["conditions"], conditions)
        # The forwarded values, rendered by the local chart, give the conditioned store.
        tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), "shc-vss-values-%d.yaml" % os.getpid())
        try:
            with open(tmp, "w") as fh:
                yaml.safe_dump(values, fh)
            spec = store(helm_template(CHART, "-f", tmp))["spec"]
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self.assertEqual(spec["conditions"], conditions)
        self.assertEqual(spec["provider"]["vault"]["auth"]["kubernetes"]["mountPath"], "kubernetes-services")

    def test_central_values_conditions_need_the_chart_that_renders_them(self):
        with open(os.path.join(CENTRAL, "values.yaml")) as fh:
            central = yaml.safe_load(fh)
        vss = central.get("vaultSecretStoreServices") or {}
        conditions = (vss.get("values") or {}).get("conditions") or []
        if not conditions:
            self.skipTest("central values set no services store conditions (store open to every namespace)")
        with open(os.path.join(CHART, "Chart.yaml")) as fh:
            chart_version = yaml.safe_load(fh)["version"]
        self.assertEqual(vss.get("chartVersion"), chart_version,
                         "conditions set, but the Application pins a chart other than the one that renders them")
        for entry in conditions:
            self.assertEqual(set(entry), {"namespaces"}, "exact namespace lists only: %r" % entry)
            names = entry["namespaces"]
            self.assertTrue(names and all(isinstance(n, str) and n for n in names), entry)
            self.assertEqual(len(names), len(set(names)), "duplicate namespace in %r" % entry)


if __name__ == "__main__":
    unittest.main()

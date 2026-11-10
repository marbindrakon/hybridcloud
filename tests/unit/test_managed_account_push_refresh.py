"""The central chart's managed-account PushSecrets (sovereign-central.managedAccount: the site accounts' and the services
automation account's kubeconfig in Vault) refresh every 5 minutes by default, so a token ACM re-issues after a cluster
was off reaches Vault within minutes; managedAccounts.pushRefreshInterval and a block's pushRefreshInterval override it."""
import os
import shutil
import subprocess
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHART = os.path.join(ROOT, "bootstrap", "helm", "central")


def pushsecrets(*sets):
    args = ["helm", "template", "sovereign-central", CHART]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True, timeout=300).stdout
    return {(d["metadata"]["namespace"], d["metadata"]["name"]): d["spec"]["refreshInterval"]
            for d in yaml.safe_load_all(out)
            if d and d.get("kind") == "PushSecret" and "sovereign-cloud/target" in d["metadata"].get("labels", {})}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class ManagedAccountPushRefresh(unittest.TestCase):
    def test_default_is_five_minutes(self):
        ps = pushsecrets()
        self.assertIn(("services", "shc-automation"), ps)
        self.assertTrue(any(n == "shc-automation" and ns != "services" for ns, n in ps), ps)
        self.assertTrue(any(n == "shc-cloudinfra" for _, n in ps), ps)
        self.assertEqual(set(ps.values()), {"5m"}, ps)

    def test_global_override(self):
        self.assertEqual(set(pushsecrets("managedAccounts.pushRefreshInterval=2m").values()), {"2m"})

    def test_block_override(self):
        ps = pushsecrets("servicesCluster.automation.pushRefreshInterval=30s")
        self.assertEqual(ps[("services", "shc-automation")], "30s")
        self.assertEqual({v for k, v in ps.items() if k != ("services", "shc-automation")}, {"5m"})


if __name__ == "__main__":
    unittest.main()

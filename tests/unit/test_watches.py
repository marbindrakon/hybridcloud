"""Operator watches stay inside the controller budget (concurrency, periods, status ownership)."""
import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = {"namespace": os.path.join(ROOT, "operator/namespace/watches.yaml"),
         "primary": os.path.join(ROOT, "operator/primary/watches.yaml")}
# Kinds that only wait on their own inputs: no periodic re-run (a failed one is re-checked by shc-tenancy).
NO_PERIOD = {"Rbac", "Team", "Project", "Persona", "Vault", "VaultKV", "AAPOrg", "QuayOrg"}
BUDGET = 30


@unittest.skipIf(yaml is None, "PyYAML not installed")
class Watches(unittest.TestCase):
    def load(self, key):
        with open(FILES[key]) as fh:
            return yaml.safe_load(fh)

    def test_budget(self):
        for key in FILES:
            watches = self.load(key)
            self.assertLessEqual(sum(w.get("maxConcurrentReconciles", 1) for w in watches), BUDGET, key)
            for w in watches:
                self.assertIn("maxConcurrentReconciles", w, w["kind"])

    def test_manage_status_off(self):
        for key in FILES:
            for w in self.load(key):
                self.assertIs(w.get("manageStatus"), False, w["kind"])

    def test_no_period_kinds(self):
        for key in FILES:
            for w in self.load(key):
                if w["kind"] in NO_PERIOD:
                    self.assertNotIn("reconcilePeriod", w, w["kind"])


if __name__ == "__main__":
    unittest.main()

"""Allocation count behind the HybridFabric label hybridsovereign.redhat/allocations (shc_ledger.py)."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_ledger():
    # shc_ledger imports requests/urllib3 at module level; the count needs neither.
    for name in ("requests", "urllib3"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.disable_warnings = lambda *a, **k: None
            sys.modules[name] = stub
    spec = importlib.util.spec_from_file_location("shc_ledger", ROOT / "eda/common/files/shc_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Shape of a live ledger (one tenant network on three sites, four gateways, the fabric's own root index).
LEDGER = {
    "alloc": {
        "attachLink": {}, "attachVlan": {},
        "egress": {"HybridNetwork/entity-a/core": 0},
        "l2vni": {"NetworkPlacement/entity-a/core-s1": 151001, "NetworkPlacement/entity-a/core-s2": 151000},
        "peIndex": {"CloudGateway/platform/s1": 2, "CloudGateway/platform/s2": 3, "CloudGateway/platform/s3": 4,
                    "CloudGateway/platform/s4": 5, "HybridFabric/platform/lab": 1},
        "vni": {"NetworkPlacement/entity-a/core-s1": 51001, "NetworkPlacement/entity-a/core-s2": 51000,
                "NetworkPlacement/entity-a/core-s3": 51002},
        "vpnTarget": {"HybridNetwork/entity-a/core": 10000},
    },
    "pools": {},
}


class Recorder:
    def __init__(self, code=200):
        self.calls = []
        self.code = code

    def call(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, body, content_type))
        return self.code, {}


class LedgerCountTest(unittest.TestCase):
    def setUp(self):
        self.m = load_ledger()

    def test_counts_everything_but_the_fabric_itself(self):
        self.assertEqual(self.m.tenant_allocations(LEDGER, "platform", "lab"), 11)

    def test_only_the_fabric_root(self):
        led = {"alloc": {"peIndex": {"HybridFabric/platform/lab": 1}}}
        self.assertEqual(self.m.tenant_allocations(led, "platform", "lab"), 0)

    def test_other_fabric_owner_counts(self):
        led = {"alloc": {"peIndex": {"HybridFabric/platform/other": 1}}}
        self.assertEqual(self.m.tenant_allocations(led, "platform", "lab"), 1)

    def test_empty_ledger(self):
        self.assertEqual(self.m.tenant_allocations({}, "platform", "lab"), 0)

    def test_label_patch(self):
        api = Recorder()
        self.assertTrue(self.m.publish_label(api, {"namespace": "platform", "fabric": "lab"}, LEDGER))
        method, path, body, ctype = api.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertTrue(path.endswith("/namespaces/platform/hybridfabrics/lab"))
        self.assertEqual(body, {"metadata": {"labels": {"hybridsovereign.redhat/allocations": "11"}}})
        self.assertEqual(ctype, "application/merge-patch+json")

    def test_label_patch_is_best_effort(self):
        self.assertFalse(self.m.publish_label(Recorder(code=403), {"namespace": "p", "fabric": "f"}, LEDGER))


if __name__ == "__main__":
    unittest.main()

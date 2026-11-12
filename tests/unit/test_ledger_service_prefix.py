"""The fabric ledger's kind servicePrefix (eda/common/files/shc_ledger.py): a cluster prefix a HybridNetwork accepts into
the service VRF is recorded with a value that orders it after every prefix held when it came (first come first
served: the network job and the fabric's service plan refuse the later of two overlapping prefixes by this value),
also after releases, and when the pool's end is reached the held values are renumbered in their order. The kind is
counted (status.allocated / available, the allocations label of the fabric's deletion guard). Against an in-memory
API; example names only."""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_ledger as L  # noqa: E402

NS, FABRIC = "platform-ns", "fab"


class FakeApi:
    """The ConfigMap of one ledger and the HybridFabric's status/label patches."""

    def __init__(self, pools):
        led = {"version": 1, "fabric": FABRIC, "pools": pools, "alloc": {k: {} for k in L.KINDS}}
        self.cm = {"metadata": {"name": f"fabric-ledger-{FABRIC}", "resourceVersion": "1"},
                   "data": {"ledger.json": json.dumps(led)}}
        self.patches = []

    def call(self, method, path, body=None, content_type=None):
        if method == "GET" and "/configmaps/" in path:
            return 200, copy.deepcopy(self.cm)
        if method == "PUT" and "/configmaps/" in path:
            if body["metadata"]["resourceVersion"] != self.cm["metadata"]["resourceVersion"]:
                return 409, {}
            body = copy.deepcopy(body)
            body["metadata"]["resourceVersion"] = str(int(body["metadata"]["resourceVersion"]) + 1)
            self.cm = body
            return 200, body
        if method == "PATCH":
            self.patches.append((path, body))
            return 200, {}
        return 404, {"message": "not found"}

    def alloc(self, kind="servicePrefix"):
        return json.loads(self.cm["data"]["ledger.json"])["alloc"][kind]


def owner(net, prefix):
    return f"HybridNetwork/acme/{net}/{prefix}"


def allocate(api, o, kind="servicePrefix"):
    return L.ledger_allocate(api, {"namespace": NS, "fabric": FABRIC, "kind": kind, "owner": o})["value"]


def release(api, o, kind="servicePrefix"):
    return L.ledger_release(api, {"namespace": NS, "fabric": FABRIC, "kind": kind, "owner": o})


class ServicePrefixOrder(unittest.TestCase):
    def test_the_kind_is_known(self):
        self.assertIn("servicePrefix", L.KINDS)
        self.assertIn("servicePrefix", L.ORDERED_KINDS)

    def test_a_later_prefix_ranks_after_every_prefix_held_also_after_a_release(self):
        api = FakeApi({"servicePrefix": [1, 1048575]})
        a = allocate(api, owner("n1", "192.0.2.15/32"))
        b = allocate(api, owner("n1", "192.0.2.16/32"))
        c = allocate(api, owner("n2", "192.0.2.17/32"))
        self.assertLess(a, b)
        self.assertLess(b, c)
        release(api, owner("n1", "192.0.2.15/32"))
        # lowest free would hand the released value to the newcomer and rank it before b and c
        d = allocate(api, owner("n3", "192.0.2.15/32"))
        held = api.alloc()
        self.assertGreater(d, max(v for o, v in held.items() if o != owner("n3", "192.0.2.15/32")))
        # an owner keeps its value
        self.assertEqual(allocate(api, owner("n1", "192.0.2.16/32")), b)

    def test_at_the_pool_end_the_held_values_are_renumbered_in_their_order(self):
        api = FakeApi({"servicePrefix": [1, 3]})
        for p in ("a", "b", "c"):
            allocate(api, owner("n1", p))
        release(api, owner("n1", "a"))
        v = allocate(api, owner("n2", "d"))
        held = api.alloc()
        self.assertEqual(sorted(held, key=held.get), [owner("n1", "b"), owner("n1", "c"), owner("n2", "d")])
        self.assertEqual(v, 3)
        with self.assertRaisesRegex(RuntimeError, "servicePrefix pool 1-3 is exhausted"):
            allocate(api, owner("n2", "e"))

    def test_other_kinds_keep_the_lowest_free_value(self):
        api = FakeApi({"egress": [0, 9]})
        self.assertEqual(allocate(api, "HybridNetwork/acme/n1", "egress"), 0)
        self.assertEqual(allocate(api, "HybridNetwork/acme/n2", "egress"), 1)
        release(api, "HybridNetwork/acme/n1", "egress")
        self.assertEqual(allocate(api, "HybridNetwork/acme/n3", "egress"), 0)

    def test_counted_for_the_status_and_the_deletion_guard(self):
        api = FakeApi({"servicePrefix": [1, 10]})
        allocate(api, owner("n1", "192.0.2.15/32"))
        status = [b for p, b in api.patches if p.endswith("/status")][-1]["status"]
        self.assertEqual((status["allocated"]["servicePrefix"], status["available"]["servicePrefix"]), (1, 9))
        label = [b for p, b in api.patches if not p.endswith("/status")][-1]
        self.assertEqual(label["metadata"]["labels"][L.ALLOCATIONS_LABEL], "1")



class ServiceHalvesTheIndexPool(unittest.TestCase):
    """Review R1, unconfirmed R12: with the service on, the root index pool is the /31s of one half of egress.linkPool
    (egress links below, service links above); where that half is the limit, a fabric whose networks already hold a
    higher root index is refused by ledger-ensure with the pool and the values it would drop (the fabric fails, the
    nodes are not touched), never renumbered or cut silently."""

    def test_the_pool_size(self):
        import shc_fabric as F
        eg = {"enabled": True, "parentInterface": "bond1.100", "gateway": "203.0.113.1", "prefixLength": 24,
              "linkPool": "169.254.0.0/24", "vrf": {"name": "egress", "table": 9000},
              "nodes": {"bgw-1": {"address": "203.0.113.13", "natPool": "203.0.113.128/25"}}}
        self.assertEqual(F.egress_check(eg, ["bgw-1"])["poolSize"], 128)
        self.assertEqual(F.egress_check(eg, ["bgw-1"], service=True)["poolSize"], 64)

    def test_a_held_index_above_the_new_end_is_refused(self):
        api = FakeApi({"egress": [0, 127]})
        led = json.loads(api.cm["data"]["ledger.json"])
        led["alloc"]["egress"] = {"HybridNetwork/acme/a": 3, "HybridNetwork/acme/b": 100}
        api.cm["data"]["ledger.json"] = json.dumps(led)
        with self.assertRaises(RuntimeError) as e:
            L.ledger_ensure(api, {"namespace": NS, "fabric": FABRIC, "fabric_uid": "u", "pools": {"egress": [0, 63]}})
        self.assertIn("egress 0-63 would drop allocated [100]", str(e.exception))
        self.assertEqual(api.alloc("egress"), {"HybridNetwork/acme/a": 3, "HybridNetwork/acme/b": 100})

if __name__ == "__main__":
    unittest.main()

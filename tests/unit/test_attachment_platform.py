"""AttachmentCircuits of ceKind platform in files/shc_fabric.py: check-circuit (the refusals NotPlatformNamespace,
ServiceNotOffered, InvalidSpec for accept entries, FamilyUnavailable; ceConfig advertisedRanges and acceptedRanges),
attachments-plan (a platform circuit is planned under platform, in the service VRF, never in a network's VRF or the
circuits' filter table; skipped without the service VRF or outside the fabric's namespace; its VLAN unique with the
customer circuits'), and the customer circuits' deny list and drop toward the hub's ranges (infrastructure.hub, as
platformSources before: unchanged without hub ranges). Documentation and private ranges, example names only."""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_attachment_ipv6 as T  # noqa: E402
from test_attachment_ipv6 import shc_fabric as F  # noqa: E402

HUB = "198.51.100.0/24"
SERVICE = {"enabled": True, "vrf": {"name": "service", "table": 9001}, "ports": [6443, 443], "hubEndpoints": [], "range": ["198.51.100.0/28"],
           "attachableRanges": ["192.0.2.0/24", "203.0.113.0/25"]}
REAL = {"service": SERVICE, "infrastructure": {"hub": [HUB, "2001:db8:5::/48"]}}


def platform_circuit(name="hub-a", ns="platform", vlan=3099, index=9, auth="none"):
    r = T.realization(index, [], vlan, auth=auth)
    for k in ("vrf", "vpnTarget", "accept"):
        r.pop(k)
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"ceKind": "platform"},
            "status": {"fabric": "lab", "realization": r}}


def req(circuits, service=SERVICE, **kw):
    r = T.plan_req(circuits)
    for c in circuits:
        owner = f"AttachmentCircuit/{c['metadata']['namespace']}/{c['metadata']['name']}"
        r["ledger"]["attachVlan"][owner] = c["status"]["realization"]["vlan"]
        r["ledger"]["attachLink"][owner] = c["status"]["realization"]["linkIndex"]
    r["passwords"] = [f"{c['metadata']['namespace']}/{c['metadata']['name']}" for c in circuits]
    r.update({"service": service, "fabricNamespace": "platform"}, **kw)
    return r


class CheckCircuit(unittest.TestCase):
    def check(self, **kw):
        q = {"ceKind": "platform", "namespace": "platform", "fabricNamespace": "platform", "accept": [],
             "realization": copy.deepcopy(REAL)}
        q.update(kw)
        return F.check_circuit(q)

    def test_accepted(self):
        out = self.check()
        self.assertEqual((out["errors"], out["reason"]), ([], ""))
        # what the hub's router configures, in its order: announce the service range, refuse the published ranges
        # from the platform (any length), then accept the attachable ranges (or any other IPv4 prefix up to /32), and
        # a maximum-prefix figure
        ce = out["ceConfig"]
        self.assertEqual(list(ce), ["acceptedRanges", "refusedRanges", "advertisedRanges", "maxPrefixes"])
        self.assertEqual(ce["acceptedRanges"], ["198.51.100.0/28 le 32"])
        self.assertEqual(ce["refusedRanges"][:2], ["0.0.0.0/0", "198.51.100.0/24 le 32"])
        self.assertTrue(all(":" not in e for e in ce["refusedRanges"]))
        self.assertEqual(ce["advertisedRanges"], ["192.0.2.0/24 le 32", "203.0.113.0/25 le 32"])
        self.assertEqual(ce["maxPrefixes"], 1000)
        r = copy.deepcopy(REAL)
        for att in ([], ["0.0.0.0/0"]):
            r["service"]["attachableRanges"] = att
            # without attachable ranges: refuse the published ranges, then any other IPv4 prefix up to /32
            self.assertEqual(self.check(realization=r)["ceConfig"]["advertisedRanges"], ["0.0.0.0/0 le 32"])
        self.assertTrue(self.check(advertise=["192.0.2.0/24"])["notes"])

    def test_refusals(self):
        self.assertEqual(self.check(namespace="acme")["reason"], "NotPlatformNamespace")
        r = copy.deepcopy(REAL)
        r["service"]["enabled"] = False
        out = self.check(realization=r)
        self.assertEqual(out["reason"], "ServiceNotOffered")
        self.assertEqual(out["errors"], ["the fabric offers no service routing domain for platform circuits"])
        out = self.check(accept=["198.51.100.0/24 le 32"])
        self.assertEqual(out["reason"], "InvalidSpec")
        self.assertIn("routing.accept is not allowed", out["errors"][0])
        out = self.check(family=[4, 6])
        self.assertEqual(out["reason"], "FamilyUnavailable")
        # the first reason named, every message kept
        out = self.check(namespace="acme", accept=["10.0.0.0/8"])
        self.assertEqual(out["reason"], "NotPlatformNamespace")
        self.assertEqual(len(out["errors"]), 2)

    def test_customer_unchanged(self):
        q = {"accept": ["10.50.0.0/16 le 32"], "space": ["10.60.0.0/16"], "reserved": ["10.255.0.0/24"]}
        self.assertEqual(F.check_circuit(dict(q, ceKind="customer")), F.check_circuit(q))
        # the IPv6 hub ranges are platform ranges for a customer's IPv6 accept entries
        q6 = {"accept": ["2001:db8:5:1::/64"], "space": ["2001:db8:60::/48"], "attachments": T.AT, "core": T.CORE}
        self.assertEqual(F.check_circuit(q6)["errors"], [])
        self.assertIn("accept entry '2001:db8:5:1::/64' lies inside the platform range 2001:db8:5::/48",
                      F.check_circuit(dict(q6, hub6=["2001:db8:5::/48"]))["errors"])


class Plan(unittest.TestCase):
    def test_platform_circuit_planned_apart(self):
        cust = T.circuit("c1", T.realization(0, T.V4_ACCEPT, 3000))
        out = F.attachments_plan(req([cust, platform_circuit()]))
        self.assertEqual([c["circuit"] for c in out["circuits"]], ["acme/c1"])
        self.assertEqual(out["skipped"], [])
        p = out["platform"]
        self.assertEqual(len(p), 1)
        p = p[0]
        self.assertEqual((p["circuit"], p["kind"], p["id"], p["vrf"], p["table"], p["vlan"], p["device"]),
                         ("platform/hub-a", "platform", "AC3099", "service", 9001, 3099, "bond1.3099"))
        self.assertEqual(sorted(p["nodes"]), T.NODES)
        self.assertEqual(p["ce"], F.circuit_plan({"attachments": T.AT, "nodes": T.NODES, "index": 9})["ce"])
        # not a leg of a network VRF, not in the circuits' filter table, not a network's root VRF
        for n in out["nodes"].values():
            self.assertEqual([c["circuit"] for c in n["circuits"]], ["acme/c1"])
        self.assertEqual({v["vrf"] for vs in out["vrfs"].values() for v in vs}, {"e10005"})
        self.assertNotIn("platform/hub-a", [s["circuit"] for s in out["status"]])
        # the bearer interface is a parent all the same (its VLANs in the pool are the platform's)
        self.assertIn("bond1", out["nodes"]["bgw-1"]["parents"])

    def test_skipped(self):
        cases = [
            (req([platform_circuit()], service=dict(SERVICE, enabled=False)),
             "the fabric offers no service VRF (realization service.enabled)"),
            (req([platform_circuit(ns="acme")]), "a platform circuit lives in the fabric's namespace only"),
            (req([platform_circuit(vlan=3200)]), "VLAN 3200 is outside the pool 3000-3099"),
            (req([platform_circuit(auth="md5")], passwords=[]), "no usable BGP password in the platform's secret store"),
        ]
        for r, why in cases:
            with self.subTest(why=why):
                out = F.attachments_plan(r)
                self.assertEqual(out.get("platform", []), [])
                self.assertEqual([s["reason"] for s in out["skipped"]], [why])
        # one VLAN per bearer, whichever kind took it first (namespace/name order)
        out = F.attachments_plan(req([T.circuit("c1", T.realization(0, T.V4_ACCEPT, 3099)), platform_circuit()]))
        self.assertEqual(out["skipped"], [{"circuit": "platform/hub-a", "reason": "VLAN 3099 is also acme/c1's"}])

    def test_customer_deny_and_drop_from_the_hub(self):
        cust = T.circuit("c1", T.realization(0, T.V4_ACCEPT, 3000))
        base = T.plan_req([cust])
        base.update(reserved=T.RESERVED4[:4], platformSources=[])
        without = F.attachments_plan(copy.deepcopy(base))
        with_hub = F.attachments_plan(dict(copy.deepcopy(base), reserved=T.RESERVED4[:4] + [HUB], platformSources=[HUB]))
        self.assertNotIn("198.51.100.0/24 le 32", without["circuits"][0]["deny"])
        self.assertIn("198.51.100.0/24 le 32", with_hub["circuits"][0]["deny"])
        self.assertEqual(with_hub["nodes"]["bgw-1"]["platformSources"], [HUB])
        self.assertEqual(without["nodes"]["bgw-1"]["platformSources"], [])
        # nothing else changes, and the answer has no platform circuit
        self.assertEqual(without.get("platform", []), [])
        a, b = copy.deepcopy(without), copy.deepcopy(with_hub)
        for x in (a, b):
            x["circuits"][0].pop("deny")
            for vs in x["vrfs"].values():
                for v in vs:
                    for c in v["circuits"]:
                        c.pop("deny")
            for s in x["status"]:
                s.pop("policy")
            for n in x["nodes"].values():
                n.pop("platformSources")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()

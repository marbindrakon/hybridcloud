"""IPv6 on attachment circuits (design/IPV6-PLAN.md §3.7, §11.13).

- the planner (eda/common/files/shc_fabric.py): accept entries of both families, the IPv6 link pool check, the IPv6
  link by the circuit's link index, the accept list check per family (the refusals of the circuit job), the
  attachments plan's "ipv6" sub-dict, the per-node entries and root VRFs, the policy hash;
- a circuit without IPv6 entries plans byte for byte what the release before planned (BASE below, read from Git;
  those comparisons are skipped without it);
- the circuit job (attachmentcircuit_provision): the realization and the CE's parameters (same revision without IPv6),
  the refusals, the sessions per family (the node script against a fake vtysh) and the report.

The CRD's per-family CEL rules run on envtest in tests/cel-ipam/cel_cases.py (group attachment); the CRD text checks
here always run. Documentation and private ranges only."""
import copy
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_fabric  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
AC_TASKS = os.path.join(ROLES, "attachmentcircuit_provision", "tasks", "main.yml")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-attachmentcircuit.yaml")
# the release before IPv6 on circuits
BASE = "51c8506"


def base_text(rel):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{rel}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def base_module():
    text = base_text("eda/common/files/shc_fabric.py")
    if text is None:
        return None
    tmp = tempfile.mkdtemp(prefix="shc-fabric-base-")
    path = os.path.join(tmp, "shc_fabric_base.py")
    with open(path, "w") as f:
        f.write(text)
    spec = importlib.util.spec_from_file_location("shc_fabric_base", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE_MOD = base_module()

NODES = ["bgw-1", "bgw-2"]
CORE = {"loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24",
        "loopbackPool6": "2001:db8:4005:ffff::/64", "icmpSource6": "2001:db8:4005:1::/64"}
TRANSLATION = {"iaas1": {"pref64": "2001:db8:4001:66::/96", "dns64Resolvers": ["fd00:53::10"]}, "gcp": {"provider": True}}
AT = {"enabled": True, "vlanPool": {"start": 3000, "end": 3099}, "linkPool": "10.255.128.0/20", "linkPrefixLength": 29,
      "linkPool6": "2001:db8:4005:100::/56", "reserved6": ["2001:db8:4005::/52"],
      "providerEdges": {"lab-border": {"bearers": {"bgw-trunk": {"interface": "bond1", "mtu": 1500}}}}}
AT4 = {k: v for k, v in AT.items() if k not in ("linkPool6", "reserved6")}
RESERVED4 = ["10.255.128.0/20", "10.255.0.0/24", "10.255.1.0/24", "169.254.0.0/16", "172.18.230.0/24"]
BLOCK = "2001:db8:4005:4000::/56"


def network(name="g10", v6=True, internet=True, prefixes=("10.60.0.0/16",)):
    st = {"fabric": "lab", "addressSpace": [{"prefix": p, "family": 4} for p in prefixes]}
    spec = {"prefixes": list(prefixes), "egress": {"internet": internet}}
    if v6:
        spec["platformPrefixes"] = [{"family": 6}]
        st["addressSpace"].append({"prefix": BLOCK, "family": 6, "source": "Platform"})
        st["ipv6"] = {"mode": "Platform", "prefixes": [BLOCK], "block": {"prefix": BLOCK, "prefixLength": 56,
                                                                          "source": "Allocated"}}
    return {"metadata": {"namespace": "acme", "name": name}, "spec": spec, "status": st}


def placement(name="g10-iaas1", net="g10", prefixes=("10.60.1.0/24", "2001:db8:4005:4000::/64")):
    return {"metadata": {"namespace": "acme", "name": name}, "spec": {"network": net},
            "status": {"prefixes": list(prefixes)}}


def realization(index, accept, vlan, ipv6=None, advertise_default=False, auth="md5", bfd=True, target=10005):
    """A circuit's status.realization as the circuit job records it (circuit-plan for the addresses)."""
    plan = shc_fabric.circuit_plan({"attachments": AT, "nodes": NODES, "index": index, "ipv6": ipv6})
    r = {"vlan": vlan, "linkIndex": index, "link": plan["link"], "nodes": plan["nodes"], "ce": plan["ce"],
         "vrf": f"e{target}", "vpnTarget": target, "bearer": "bgw-trunk", "interface": "bond1", "mtu": 1500,
         "peerAsn": 65012, "authentication": auth, "passwordPath": "fabric/circuits/acme/x" if auth == "md5" else "",
         "maxPrefixes": 10, "maxPrefixAction": "teardown", "bfd": bfd, "accept": list(accept), "acceptsDefault": False,
         "advertiseNetworkPrefixes": True, "advertiseDefault": advertise_default, "rev": f"rev{index}"}
    if ipv6:
        p6 = plan["ipv6"]
        r.update({"link6": p6["link"], "ce6": p6["ce"],
                  "nodes": [dict(n, address6=m["address"]) for n, m in zip(plan["nodes"], p6["nodes"])]})
    return r


def circuit(name, real, net="g10"):
    return {"metadata": {"namespace": "acme", "name": name}, "spec": {"network": net},
            "status": {"fabric": "lab", "realization": real}}


def plan_req(circuits, networks=None, placements=None, egress=None, attachments=AT, ipv6_core=True):
    owners = [f"AttachmentCircuit/acme/{c['metadata']['name']}" for c in circuits]
    return {"action": "attachments-plan", "fabric": "lab", "attachments": attachments, "nodes": NODES,
            "tableBase": 100000, "domainAsn": 65010, "circuits": circuits,
            "networks": networks if networks is not None else [network()],
            "placements": placements if placements is not None else [placement()],
            "ledger": {"attachVlan": {o: c["status"]["realization"]["vlan"] for o, c in zip(owners, circuits)},
                       "attachLink": {o: c["status"]["realization"]["linkIndex"] for o, c in zip(owners, circuits)},
                       "vpnTarget": {"HybridNetwork/acme/g10": 10005, "HybridNetwork/acme/g11": 10006}},
            "egress": egress if egress is not None else {
                "acme/g10": {"vpnTarget": 10005, "index": 3, "sourceAddresses": ["172.18.237.67"], "internet": True,
                             "platform": [], "ipv6": True, "prefixes6": ["2001:db8:4005:4000::/64"], "primary": "bgw-2"}},
            "egressNodes": {}, "reserved": RESERVED4, "platformSources": ["172.18.230.0/24"],
            "passwords": [f"acme/{c['metadata']['name']}" for c in circuits],
            "core": CORE if ipv6_core else {}, "translation": TRANSLATION}


def without_platform_reach(plan):
    """a plan of the release before without the retired platform reach key of its root VRFs (always [] there)"""
    for vs in (plan.get("vrfs") or {}).values():
        for v in vs:
            if v.get("platform") == []:
                v.pop("platform")
    return plan


V4_ACCEPT = ["10.50.0.0/16 le 32"]
V6_ACCEPT = ["10.50.0.0/16 le 32", "2001:db8:50::/48 le 64"]


# ---- accept entries, collapse ------------------------------------------------------------------------------------

class AcceptEntryTest(unittest.TestCase):
    def test_both_families(self):
        net, ge, le = shc_fabric.accept_entry("2001:db8:50::/48 ge 56 le 64")
        self.assertEqual((str(net), ge, le), ("2001:db8:50::/48", 56, 64))
        self.assertEqual(shc_fabric.entry_text(net, ge, le), "2001:db8:50::/48 ge 56 le 64")
        self.assertEqual(str(shc_fabric.accept_entry("::/0")[0]), "::/0")
        self.assertEqual(shc_fabric.accept_entry("2001:db8:50::/48 le 128")[2], 128)
        self.assertEqual(shc_fabric.accept_entry("10.50.0.0/16 le 32")[2], 32)
        # canonical text from a non-canonical spelling
        self.assertEqual(shc_fabric.entry_text(*shc_fabric.accept_entry("2001:DB8:0050::/48 le 64")),
                         "2001:db8:50::/48 le 64")

    def test_malformed(self):
        for e in ("2001:db8:50::/48 le 129", "2001:db8:50::/48 ge 48", "2001:db8:50::/48 ge 64 le 56",
                  "2001:db8:50::1/48", "10.50.0.0/16 le 33", "10.50.0.0/16 ge 64", "10.50.0.1/16",
                  "2001:db8:50::/48 le 64 ge 56", "fe80::%eth0/64", "nonsense", ""):
            self.assertIsNone(shc_fabric.accept_entry(e), e)

    def test_same_ipv4_answers_as_the_release(self):
        if BASE_MOD is None:
            self.skipTest(f"{BASE} not in this checkout")
        for e in ("10.50.0.0/16", "10.50.0.0/16 le 24", "10.50.0.0/16 ge 20 le 32", "0.0.0.0/0", "10.50.0.0/16 le 33",
                  "10.50.0.1/16", "10.50.0.0/16 ge 24 le 20"):
            self.assertEqual(shc_fabric.accept_entry(e), BASE_MOD.accept_entry(e), e)

    def test_collapse_per_family(self):
        import ipaddress
        n = [ipaddress.ip_network(c) for c in ("2001:db8::/48", "2001:db8:0:1::/64", "10.0.0.0/8", "10.1.0.0/16",
                                               "2001:db8:1::/48", "2001:db8::/48")]
        self.assertEqual([str(x) for x in shc_fabric.collapse(n)], ["10.0.0.0/8"])
        self.assertEqual(sorted(str(x) for x in shc_fabric.collapse(n, 6)), ["2001:db8:1::/48", "2001:db8::/48"])


# ---- realization check -------------------------------------------------------------------------------------------

class AttachmentsCheckTest(unittest.TestCase):
    def check(self, **over):
        at = dict(AT, **over)
        return shc_fabric.attachments_check(at, NODES, CORE)

    def test_valid(self):
        r = self.check()
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["links"], 512)
        self.assertEqual(shc_fabric.attachments_check(AT4, NODES, CORE)["errors"], [])
        self.assertEqual(self.check(linkPool6="")["errors"], [])
        self.assertEqual(self.check(linkPool6="2001:db8:4005:100::/64")["errors"], [])

    def test_refused(self):
        self.assertIn("too small (at least a /64)", self.check(linkPool6="2001:db8:4005:100::/80")["errors"][0])
        self.assertIn("is not an IPv6 network", self.check(linkPool6="10.254.0.0/16")["errors"][0])
        self.assertIn("linkPool6", self.check(linkPool6="2001:db8:4005:100::1/56")["errors"][0])
        self.assertIn("overlaps core.loopbackPool6", self.check(linkPool6="2001:db8:4005:ff00::/56")["errors"][0])
        self.assertIn("overlaps core.icmpSource6", self.check(linkPool6="2001:db8:4005::/56")["errors"][0])
        self.assertIn("reserved6: 10.0.0.0/8 is not an IPv6 network", self.check(reserved6=["10.0.0.0/8"])["errors"][0])
        self.assertIn("reserved6", self.check(reserved6=["nonsense"])["errors"][0])

    def test_check_fabric_reports_it(self):
        spec = {"borderGateway": {"nodes": [{"name": n} for n in NODES]}, "domainAsn": 65010}
        real = {"core": CORE, "attachments": dict(AT, linkPool6="2001:db8:4005:100::/80")}
        self.assertTrue(any("linkPool6" in e for e in shc_fabric.check_fabric({"spec": spec, "realization": real})["errors"]))

    def test_platform_ranges6(self):
        got = [str(n) for n in shc_fabric.platform_ranges6(AT, CORE, TRANSLATION)]
        # linkPool6 and icmpSource6 lie inside reserved6; the loopbacks and the translation prefix stay
        self.assertEqual(got, ["2001:db8:4001:66::/96", "2001:db8:4005::/52", "2001:db8:4005:ffff::/64"])
        self.assertEqual(shc_fabric.platform_ranges6({}, {}, {}), [])
        self.assertEqual([str(n) for n in shc_fabric.platform_ranges6({"linkPool6": "2001:db8:4005:100::/56"}, {}, None)],
                         ["2001:db8:4005:100::/56"])


# ---- circuit-plan ------------------------------------------------------------------------------------------------

class CircuitPlanTest(unittest.TestCase):
    def plan(self, index, ipv6=None, at=AT):
        return shc_fabric.circuit_plan({"attachments": at, "nodes": NODES, "index": index, "ipv6": ipv6})

    def test_ipv4_as_the_release(self):
        if BASE_MOD is None:
            self.skipTest(f"{BASE} not in this checkout")
        for i in (0, 7, 511, 512):
            for ipv6 in (None, False):
                self.assertEqual(self.plan(i, ipv6), BASE_MOD.circuit_plan({"attachments": AT, "nodes": NODES, "index": i}))

    def test_ipv6_link_by_index(self):
        p = self.plan(0, True)
        self.assertEqual(p["errors"], [])
        self.assertEqual(p["link"], "10.255.128.0/29")
        self.assertEqual(p["ipv6"], {"link": "2001:db8:4005:100::/64", "prefixLength": 64,
                                     "nodes": [{"name": "bgw-1", "address": "2001:db8:4005:100::1"},
                                               {"name": "bgw-2", "address": "2001:db8:4005:100::2"}],
                                     "ce": "2001:db8:4005:100::3", "ceSecond": "2001:db8:4005:100::4"})
        # the IPv4 side is the same as without IPv6
        self.assertEqual({k: v for k, v in p.items() if k != "ipv6"}, self.plan(0))
        self.assertEqual(self.plan(26, True)["ipv6"]["link"], "2001:db8:4005:11a::/64")
        self.assertEqual(self.plan(255, True)["ipv6"]["ce"], "2001:db8:4005:1ff::3")

    def test_no_ipv6_link_left(self):
        p = self.plan(256, True)
        self.assertEqual(p, {"errors": ["no IPv6 link is left for this circuit"], "reason": "FamilyUnavailable"})
        # IPv4-only circuits at the same index are not affected
        self.assertEqual(self.plan(256)["errors"], [])

    def test_no_ipv6_on_the_fabric(self):
        p = self.plan(0, True, at=AT4)
        self.assertEqual(p, {"errors": ["IPv6 is not offered on attachment circuits of this fabric"],
                             "reason": "FamilyUnavailable"})


# ---- check-circuit -----------------------------------------------------------------------------------------------

class CheckCircuitTest(unittest.TestCase):
    SPACE = ["10.60.0.0/16", "10.60.0.0/16", BLOCK, BLOCK]

    def check(self, accept, space=None, at=AT, egress=True, egress6=True, adv=False):
        return shc_fabric.check_circuit({"accept": accept, "space": self.SPACE if space is None else space,
                                         "reserved": RESERVED4, "egressInternet": egress, "advertiseDefault": adv,
                                         "attachments": at, "core": CORE, "translation": TRANSLATION,
                                         "egressInternet6": egress6})

    def test_ipv4_as_the_release(self):
        if BASE_MOD is None:
            self.skipTest(f"{BASE} not in this checkout")
        for accept in (V4_ACCEPT, ["0.0.0.0/0"], ["10.60.1.0/24"], ["10.0.0.0/8 le 32"], ["10.255.128.0/24"],
                       ["10.50.0.0/16", "172.16.0.0/12 le 24"]):
            for egress in (True, False):
                for adv in (True, False):
                    new = self.check(accept, egress=egress, adv=adv)
                    old = BASE_MOD.check_circuit({"accept": accept, "space": self.SPACE, "reserved": RESERVED4,
                                                  "egressInternet": egress, "advertiseDefault": adv})
                    self.assertEqual({k: new[k] for k in old}, old, (accept, egress, adv))
                    self.assertEqual((new["familyErrors"], new["ipv6"], new["acceptsDefault6"]), ([], False, False))

    def test_ipv6_accepted(self):
        r = self.check(V6_ACCEPT)
        self.assertEqual((r["errors"], r["familyErrors"], r["ipv6"]), ([], [], True))
        self.assertEqual(r["notes"], [])

    def test_inside_the_network_or_the_platform(self):
        cases = {"2001:db8:4005:4000::/64": "lies inside the network's address space 2001:db8:4005:4000::/56",
                 "2001:db8:4005:100::/64": "lies inside the platform range 2001:db8:4005::/52",
                 "2001:db8:4005:ffff::/80 le 128": "lies inside the platform range 2001:db8:4005:ffff::/64",
                 "2001:db8:4001:66::/120": "lies inside the platform range 2001:db8:4001:66::/96"}
        for e, why in cases.items():
            r = self.check([e])
            self.assertEqual(r["errors"], [f"accept entry '{e}' {why}"], e)
        # covering an IPv6 range only gets a note
        r = self.check(["2001:db8::/32 le 64"])
        self.assertEqual(r["errors"], [])
        self.assertIn("routes overlapping the network's address space 2001:db8:4005:4000::/56 are never accepted from "
                      "the CE", r["notes"])
        self.assertIn("routes overlapping the platform range 2001:db8:4005::/52 are never accepted from the CE", r["notes"])

    def test_family_refusals(self):
        r = self.check(V6_ACCEPT, at=AT4)
        self.assertEqual(r["familyErrors"], ["IPv6 is not offered on attachment circuits of this fabric"])
        r = self.check(V6_ACCEPT, space=["10.60.0.0/16"])
        self.assertEqual(r["familyErrors"], ["the network has no IPv6 address space"])
        # IPv4-only lists never get a family refusal
        self.assertEqual(self.check(V4_ACCEPT, at=AT4, space=["10.60.0.0/16"])["familyErrors"], [])

    def test_malformed(self):
        r = self.check(["2001:db8:50::/48 le 129", "10.50.0.0/16"])
        self.assertEqual(r["errors"], ["accept entry '2001:db8:50::/48 le 129' is not '<prefix>[ ge <n>][ le <n>]'"])

    def test_default_route_notes(self):
        r = self.check(["::/0", "10.50.0.0/16"])
        self.assertTrue(r["acceptsDefault6"])
        self.assertFalse(r["acceptsDefault"])
        self.assertIn("::/0 is never accepted from the CE", r["notes"])
        self.assertIn("::/0 is never accepted from the CE", self.check(["::/0"], egress6=False)["notes"])
        note = "no IPv6 default route is advertised: the network has no IPv6 internet egress"
        self.assertIn(note, self.check(V6_ACCEPT, adv=True, egress6=False)["notes"])
        self.assertNotIn(note, self.check(V6_ACCEPT, adv=True)["notes"])
        self.assertNotIn(note, self.check(V4_ACCEPT, adv=True, egress6=False)["notes"])


# ---- attachments-plan --------------------------------------------------------------------------------------------

class AttachmentsPlanTest(unittest.TestCase):
    def ipv4_circuits(self):
        return [circuit("c2", realization(1, V4_ACCEPT, 3001)),
                circuit("c3", realization(2, ["10.70.0.0/16", "0.0.0.0/0"], 3002, advertise_default=True,
                                          auth="none", bfd=False)),
                circuit("c4", realization(3, ["10.80.0.0/16 le 24"], 3003, target=10006), net="g11")]

    def networks(self):
        return [network(), network("g11", v6=False, internet=False, prefixes=("10.61.0.0/16",))]

    def test_ipv4_circuits_byte_identical_to_the_release(self):
        if BASE_MOD is None:
            self.skipTest(f"{BASE} not in this checkout")
        for egress in (None, {}):
            for at in (AT, AT4):
                req = plan_req(self.ipv4_circuits(), networks=self.networks(),
                               placements=[placement(), placement("g11-x", "g11", ("10.61.2.0/24",))], egress=egress,
                               attachments=at)
                new, old = shc_fabric.attachments_plan(copy.deepcopy(req)), BASE_MOD.attachments_plan(copy.deepcopy(req))
                self.assertEqual(json.dumps(new, sort_keys=True), json.dumps(without_platform_reach(old), sort_keys=True))
                self.assertEqual(len(new["circuits"]), 3)
                self.assertNotIn("ipv6", json.dumps(new["circuits"]))

    def test_ipv4_circuit_unchanged_next_to_an_ipv6_one(self):
        c2 = self.ipv4_circuits()[0]
        alone = shc_fabric.attachments_plan(plan_req([copy.deepcopy(c2)]))
        both = shc_fabric.attachments_plan(plan_req([circuit("c1", realization(0, V6_ACCEPT, 3000, ipv6=True)),
                                                     copy.deepcopy(c2)]))
        pick = lambda out, key: [x for x in out if x["circuit"] == key]  # noqa: E731
        self.assertEqual(pick(both["circuits"], "acme/c2"), pick(alone["circuits"], "acme/c2"))
        self.assertEqual(pick(both["status"], "acme/c2"), pick(alone["status"], "acme/c2"))
        for n in NODES:
            self.assertEqual(pick(both["nodes"][n]["circuits"], "acme/c2"), pick(alone["nodes"][n]["circuits"], "acme/c2"))
        if BASE_MOD is not None:
            self.assertEqual(json.dumps(alone, sort_keys=True),
                             json.dumps(without_platform_reach(BASE_MOD.attachments_plan(plan_req([copy.deepcopy(c2)]))),
                                        sort_keys=True))

    def test_ipv6_plan_contract(self):
        out = shc_fabric.attachments_plan(plan_req([circuit("c1", realization(0, V6_ACCEPT, 3000, ipv6=True))]))
        self.assertEqual(out["skipped"], [])
        c = out["circuits"][0]
        # the IPv4 side: only the IPv4 entries
        self.assertEqual(c["accept"], ["10.50.0.0/16 le 32"])
        self.assertEqual(c["advertise"], ["10.60.1.0/24 le 32"])
        self.assertEqual(c["ipv6"], {
            "link": "2001:db8:4005:100::/64", "prefixLength": 64, "ce": "2001:db8:4005:100::3",
            "nodes": {"bgw-1": "2001:db8:4005:100::1", "bgw-2": "2001:db8:4005:100::2"},
            "accept": ["2001:db8:50::/48 le 64"],
            "deny": ["2001:db8:4005:4000::/56 le 128", "2001:db8:4001:66::/96 le 128", "2001:db8:4005::/52 le 128",
                     "2001:db8:4005:ffff::/64 le 128", "::/0"],
            "advertise": ["2001:db8:4005:4000::/64 le 128"],
            "denyDefault": True, "advertiseDefault": False,
            "sources": ["2001:db8:50::/48", "2001:db8:4005:100::/64"]})
        self.assertEqual(out["status"][0]["families"], [4, 6])
        for i, n in enumerate(NODES):
            e = out["nodes"][n]["circuits"][0]
            self.assertEqual(e["address6"], f"2001:db8:4005:100::{i + 1}/64")
            self.assertEqual(e["link6"], "2001:db8:4005:100::/64")
            self.assertEqual(e["sources6"], ["2001:db8:50::/48", "2001:db8:4005:100::/64"])
            self.assertEqual(e["address"], f"10.255.128.{i + 1}/29")
            v = out["vrfs"][n][0]
            self.assertEqual(v["circuits"][0]["address6"], f"2001:db8:4005:100::{i + 1}/64")
            self.assertEqual(v["circuits"][0]["ipv6"], c["ipv6"])
            self.assertEqual(v["circuits"][0]["address"], f"10.255.128.{i + 1}")

    def test_advertise_default_and_egress(self):
        r = realization(0, V6_ACCEPT, 3000, ipv6=True, advertise_default=True)
        p6 = shc_fabric.attachments_plan(plan_req([circuit("c1", r)]))["circuits"][0]["ipv6"]
        self.assertEqual(p6["advertise"], ["2001:db8:4005:4000::/64 le 128", "::/0"])
        self.assertTrue(p6["advertiseDefault"])
        # no IPv6 internet egress for the network: no ::/0 either way
        eg = {"acme/g10": {"vpnTarget": 10005, "index": 3, "internet": True, "platform": [], "ipv6": False}}
        p6 = shc_fabric.attachments_plan(plan_req([circuit("c1", r)], egress=eg))["circuits"][0]
        self.assertTrue(p6["advertiseDefault"])           # IPv4 still has its default
        self.assertEqual(p6["ipv6"]["advertise"], ["2001:db8:4005:4000::/64 le 128"])
        self.assertIn("::/0", p6["ipv6"]["deny"])             # never accepted, with or without egress
        self.assertTrue(p6["ipv6"]["denyDefault"])
        self.assertFalse(p6["ipv6"]["advertiseDefault"])

    def test_ipv6_only_accept_list(self):
        r = realization(0, ["2001:db8:50::/48 le 64", "2001:db8:51::/48"], 3000, ipv6=True)
        c = shc_fabric.attachments_plan(plan_req([circuit("c1", r)]))["circuits"][0]
        self.assertEqual(c["accept"], [])
        self.assertEqual(c["ipv6"]["accept"], ["2001:db8:50::/48 le 64", "2001:db8:51::/48"])
        self.assertEqual(c["ipv6"]["sources"], ["2001:db8:50::/48", "2001:db8:51::/48", "2001:db8:4005:100::/64"])

    def test_sources_collapse(self):
        r = realization(0, ["2001:db8:50::/48 le 64", "2001:db8:50:1::/64", "::/0"], 3000, ipv6=True)
        c = shc_fabric.attachments_plan(plan_req([circuit("c1", r)]))["circuits"][0]
        self.assertEqual(c["ipv6"]["sources"], ["::/0"])

    def test_policy_hash(self):
        r6 = realization(0, V6_ACCEPT, 3000, ipv6=True)
        h6 = shc_fabric.attachments_plan(plan_req([circuit("c1", r6)]))["status"][0]["policy"]
        r4 = realization(0, V4_ACCEPT, 3000)
        h4 = shc_fabric.attachments_plan(plan_req([circuit("c1", r4)]))["status"][0]["policy"]
        self.assertNotEqual(h6, h4)
        # another IPv6 accept entry changes the hash
        r6b = realization(0, V6_ACCEPT + ["2001:db8:52::/48"], 3000, ipv6=True)
        self.assertNotEqual(shc_fabric.attachments_plan(plan_req([circuit("c1", r6b)]))["status"][0]["policy"], h6)

    def test_skipped_without_an_ipv6_link(self):
        r = realization(0, V6_ACCEPT, 3000)                   # IPv6 entries, no IPv6 link recorded
        out = shc_fabric.attachments_plan(plan_req([circuit("c1", r)]))
        self.assertEqual(out["circuits"], [])
        self.assertIn("without a usable IPv6 link", out["skipped"][0]["reason"])
        r = realization(0, V6_ACCEPT, 3000, ipv6=True)        # the pool gone from the realization
        out = shc_fabric.attachments_plan(plan_req([circuit("c1", r)], attachments=AT4))
        self.assertIn("without a usable IPv6 link", out["skipped"][0]["reason"])
        r = realization(0, V6_ACCEPT, 3000, ipv6=True)        # a node's IPv6 address missing
        r["nodes"][1].pop("address6")
        out = shc_fabric.attachments_plan(plan_req([circuit("c1", r)]))
        self.assertEqual(out["skipped"][0]["reason"], "no IPv6 address for every node on the IPv6 link")

    def test_rollback_removes_ipv6(self):
        # removing the IPv6 entries: the circuit job drops link6/ce6/address6; the plan is the IPv4-only one again
        r = realization(0, V4_ACCEPT, 3000)
        out = shc_fabric.attachments_plan(plan_req([circuit("c1", r)]))
        self.assertNotIn("ipv6", out["circuits"][0])
        self.assertNotIn("families", out["status"][0])
        self.assertNotIn("address6", out["nodes"]["bgw-1"]["circuits"][0])

    def test_cli(self):
        req = plan_req([circuit("c1", realization(0, V6_ACCEPT, 3000, ipv6=True))])
        p = subprocess.run([sys.executable, "-I", os.path.join(FILES, "shc_fabric.py")], input=json.dumps(req),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["circuits"][0]["ipv6"]["link"], "2001:db8:4005:100::/64")


class CheckNetworkTest(unittest.TestCase):
    """A network's IPv6 prefix that holds an IPv6 accept entry of its circuit is refused, as for IPv4."""

    def test_ipv6_accept_entry(self):
        r = shc_fabric.check_network({"prefixes": ["2001:db8:50::/48"], "reserved": [],
                                      "circuits": [{"name": "c1", "accept": ["2001:db8:50:1::/64 le 128"]}]})
        self.assertEqual(r["circuits"], ["prefix 2001:db8:50::/48 holds accept entry '2001:db8:50:1::/64 le 128' of "
                                         "AttachmentCircuit c1"])
        r = shc_fabric.check_network({"prefixes": ["10.50.0.0/16"], "reserved": [],
                                      "circuits": [{"name": "c1", "accept": ["2001:db8:50:1::/64"]}]})
        self.assertEqual(r["errors"], [])


# ---- the CRD (text; the CEL rules run on envtest) ----------------------------------------------------------------

class CrdTest(unittest.TestCase):
    def setUp(self):
        crd = load(CRD)
        self.schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]

    def test_accept_rules_per_family(self):
        accept = self.schema["properties"]["spec"]["properties"]["routing"]["properties"]["accept"]
        rules = accept["items"]["x-kubernetes-validations"]
        text = json.dumps(rules)
        self.assertNotIn("family() == 4 &&", text)              # no rule refuses IPv6 any more
        self.assertIn("family() == 4 ? 32 : 128", text)
        self.assertIn("2001:db8:50::/48 le 64", accept["description"])
        msgs = [r["message"] for r in rules]
        self.assertIn("ge must not exceed le", msgs)
        self.assertIn("the entry must start with an IPv4 or IPv6 network prefix (host bits zero)", msgs)
        # the format rule admits what accept_entry admits (spot check with the same regex)
        fmt = re.compile(rules[0]["rule"].split("'")[1])
        for e in ("2001:db8:50::/48 ge 56 le 128", "10.50.0.0/16 le 24", "::/0"):
            self.assertTrue(fmt.match(e), e)
            self.assertIsNotNone(shc_fabric.accept_entry(e), e)
        self.assertFalse(fmt.match("fe80::%eth0/64"))

    def test_status_fields(self):
        st = self.schema["properties"]["status"]["properties"]
        self.assertEqual(st["sessions"]["items"]["properties"]["family"]["type"], "integer")
        self.assertIn("peer", st["sessions"]["items"]["properties"])
        for k in ("address6", "secondAddress6", "neighbors6"):
            self.assertIn(k, st["ceConfig"]["properties"])
        self.assertTrue(st["ceConfig"]["x-kubernetes-preserve-unknown-fields"])
        self.assertIn("link6", st["realization"]["description"])

    def test_chart_copy_in_step(self):
        p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "scripts", "gen-crds.py"), "--check"],
                           capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


# ---- the circuit job ---------------------------------------------------------------------------------------------

def ac_tasks():
    return load(AC_TASKS)


def base_ac_tasks():
    text = base_text("eda/rulebooks/roles/attachmentcircuit_provision/tasks/main.yml")
    return yaml.safe_load(text) if text is not None else None


def job_vars(plan, accept=V4_ACCEPT):
    return {"ac_vlan": 3000, "ac_link_index": 0, "ac_plan": plan, "ac_asn": 65010, "ac_auth_type": "md5",
            "ac_vault_path": "fabric/circuits/acme/c1", "ac_secret_name": "c1-bgp",
            "ac_bgp": {"peerAsn": 65012, "bfd": True, "maxPrefixes": {"limit": 10}},
            "ac_network": {"status": {"realization": {"vpnTarget": 10005}}},
            "ac_bearer_real": {"interface": "bond1", "mtu": 1500},
            "ac_policy": {"acceptsDefault": False},
            "fab_spec": {"edgeRef": {"name": "lab-border", "bearer": "bgw-trunk"},
                         "routing": {"accept": accept, "advertise": {"default": False}}},
            "fab_st": {}}


RECORD = ["The circuit's realization and the CE's parameters", "The circuit's IPv6 link and the CE's IPv6 parameters",
          "The realization's revision"]


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RecordTest(unittest.TestCase):
    def record(self, tasks, plan, accept=V4_ACCEPT):
        picked = [copy.deepcopy(t) for t in tasks if t.get("name") in RECORD]
        return run_play(picked + [write_out("{'real': ac_real, 'ce': ac_ce_config}")], job_vars(plan, accept))

    def test_same_revision_without_ipv6(self):
        old_tasks = base_ac_tasks()
        if old_tasks is None:
            self.skipTest(f"{BASE} not in this checkout")
        plan = shc_fabric.circuit_plan({"attachments": AT, "nodes": NODES, "index": 0})
        from play_helper import walk
        new = self.record(list(walk(ac_tasks())), plan)
        old = self.record(list(walk(old_tasks)), plan)
        self.assertEqual(new, old)
        self.assertNotIn("link6", new["real"])
        self.assertNotIn("address6", new["ce"])

    def test_status_clears_ipv6_without_it(self):
        # the status is merged: a circuit whose IPv6 accept entries were removed must null the IPv6 keys
        from play_helper import walk
        picked = [copy.deepcopy(t) for t in walk(ac_tasks()) if t.get("name") in RECORD + ["The circuit's status parts"]]
        plan = shc_fabric.circuit_plan({"attachments": AT, "nodes": NODES, "index": 0})
        out = run_play(picked + [write_out("{'real': ac_real_status, 'ce': ac_ce_config_status, 'r': ac_real}")],
                       job_vars(plan))
        self.assertEqual({k: out["ce"][k] for k in ("address6", "secondAddress6", "neighbors6")},
                         {"address6": None, "secondAddress6": None, "neighbors6": None})
        self.assertEqual((out["real"]["link6"], out["real"]["ce6"]), (None, None))
        self.assertNotIn("link6", out["r"])                    # the realization (and its revision) unchanged
        self.assertEqual(out["real"]["rev"], out["r"]["rev"])  # the patch carries the revision
        plan6 = shc_fabric.circuit_plan({"attachments": AT, "nodes": NODES, "index": 0, "ipv6": True})
        out6 = run_play(picked + [write_out("{'ce': ac_ce_config_status}")], job_vars(plan6, V6_ACCEPT))
        self.assertEqual(out6["ce"]["address6"], "2001:db8:4005:100::3/64")

    def test_ipv6_fields(self):
        from play_helper import walk
        plan = shc_fabric.circuit_plan({"attachments": AT, "nodes": NODES, "index": 0, "ipv6": True})
        out = self.record(list(walk(ac_tasks())), plan, V6_ACCEPT)
        r, ce = out["real"], out["ce"]
        self.assertEqual(r["link6"], "2001:db8:4005:100::/64")
        self.assertEqual(r["ce6"], "2001:db8:4005:100::3")
        self.assertEqual(r["nodes"], [{"name": "bgw-1", "address": "10.255.128.1", "address6": "2001:db8:4005:100::1"},
                                      {"name": "bgw-2", "address": "10.255.128.2", "address6": "2001:db8:4005:100::2"}])
        self.assertEqual(r["accept"], V6_ACCEPT)
        self.assertEqual(len(r["rev"]), 12)
        self.assertEqual(ce["address6"], "2001:db8:4005:100::3/64")
        self.assertEqual(ce["secondAddress6"], "2001:db8:4005:100::4/64")
        self.assertEqual(ce["neighbors6"], [{"name": "bgw-1", "address": "2001:db8:4005:100::1", "asn": 65010},
                                            {"name": "bgw-2", "address": "2001:db8:4005:100::2", "asn": 65010}])
        self.assertEqual(ce["neighbors"], [{"name": "bgw-1", "address": "10.255.128.1", "asn": 65010},
                                           {"name": "bgw-2", "address": "10.255.128.2", "asn": 65010}])
        self.assertEqual(ce["address"], "10.255.128.3/29")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RefusalTaskTest(unittest.TestCase):
    def run_refusal(self, check_result, names):
        tasks = [{"ansible.builtin.set_fact": {"fab_fail_reason": "", "fab_fail_msg": ""}}]
        for n in names:
            t = copy.deepcopy(by_name(ac_tasks(), n))
            t.pop("ansible.builtin.fail", None)
            if "ansible.builtin.set_fact" in t:
                tasks.append(t)
        tasks.append(write_out("{'reason': fab_fail_reason, 'msg': fab_fail_msg}"))
        return run_play(tasks, {"fab_name": "c1", "fab_check_result": check_result})

    def test_family_unavailable(self):
        out = self.run_refusal({"errors": [], "familyErrors": ["the network has no IPv6 address space"]},
                               ["Refuse IPv6 the circuit can't have"])
        self.assertEqual(out, {"reason": "FamilyUnavailable",
                               "msg": "AttachmentCircuit c1: the network has no IPv6 address space"})
        out = self.run_refusal({"errors": [], "familyErrors": []}, ["Refuse IPv6 the circuit can't have"])
        self.assertEqual(out["reason"], "")

    def test_link_refusal_reason(self):
        out = self.run_refusal({"errors": ["no IPv6 link is left for this circuit"], "reason": "FamilyUnavailable"},
                               ["Refuse a link the platform can't number"])
        self.assertEqual(out, {"reason": "FamilyUnavailable", "msg": "AttachmentCircuit c1: no IPv6 link is left for this circuit"})
        out = self.run_refusal({"errors": ["link index 9 is outside 10.255.128.0/29 (/29: 1 links)"]},
                               ["Refuse a link the platform can't number"])
        self.assertEqual(out["reason"], "ProvisionFailed")

    def test_refusals_before_the_ledger(self):
        names = [t.get("name") for t in ac_tasks()[0]["block"]]
        self.assertLess(names.index("Stop on IPv6 the circuit can't have"), names.index("The circuit's VLAN"))
        self.assertLess(names.index("Stop on IPv6 the circuit can't have"),
                        names.index("Refuse an accept list that reaches into the network or the platform"))


def session_script():
    """The node-side script of the session task (between <<'PY' and PY)."""
    t = by_name(ac_tasks(), "The sessions from each node")
    cmd = t["ansible.builtin.shell"]["cmd"]
    return cmd.split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]


FAKE_VTYSH = r'''#!/usr/bin/env python3
import json, sys
cmd = sys.argv[2]
peer = cmd.split()[5]
data = json.load(open(%r))
print(json.dumps({peer: data[peer]} if peer in data else {}))
'''


class SessionScriptTest(unittest.TestCase):
    def run_script(self, args, neighbours):
        tmp = tempfile.mkdtemp(prefix="shc-vtysh-")
        with open(os.path.join(tmp, "data.json"), "w") as f:
            json.dump(neighbours, f)
        vt = os.path.join(tmp, "vtysh")
        with open(vt, "w") as f:
            f.write(FAKE_VTYSH % os.path.join(tmp, "data.json"))
        os.chmod(vt, os.stat(vt).st_mode | stat.S_IXUSR)
        p = subprocess.run([sys.executable, "-"] + args, input=session_script(), capture_output=True, text=True,
                           timeout=60, env=dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", "")))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_per_family(self):
        nb = {"10.255.128.3": {"bgpState": "Established", "bgpTimerUpString": "01:00:00",
                               "addressFamilyInfo": {"ipv4Unicast": {"acceptedPrefixCounter": 2, "sentPrefixCounter": 1}},
                               "peerBfdInfo": {"status": "Up"}},
              "2001:db8:4005:100::3": {"bgpState": "Idle", "lastResetDueTo": "Maximum Number of Prefixes Reached",
                                       "addressFamilyInfo": {"ipv6Unicast": {"acceptedPrefixCounter": 0}}}}
        out = self.run_script(["e10005", "4=10.255.128.3", "6=2001:db8:4005:100::3"], nb)
        self.assertEqual(out[0], {"family": 4, "peer": "10.255.128.3", "state": "Established", "established": "01:00:00",
                                  "accepted": 2, "advertised": 1, "lastReset": "", "bfd": "Up"})
        self.assertEqual(out[1]["family"], 6)
        self.assertEqual(out[1]["state"], "Idle")
        self.assertEqual(out[1]["lastReset"], "Maximum Number of Prefixes Reached")
        self.assertEqual(out[1]["accepted"], 0)

    def test_ipv4_only_and_not_configured(self):
        out = self.run_script(["e10005", "4=10.255.128.3"], {})
        self.assertEqual(out, [{"family": 4, "peer": "10.255.128.3", "state": "NotConfigured", "established": None,
                                "accepted": 0, "advertised": 0, "lastReset": "", "bfd": None}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SessionsTaskTest(unittest.TestCase):
    REAL6 = {"vrf": "e10005", "ce": "10.255.128.3", "ce6": "2001:db8:4005:100::3",
             "nodes": [{"name": "bgw-1", "address": "10.255.128.1", "address6": "2001:db8:4005:100::1"},
                       {"name": "bgw-2", "address": "10.255.128.2", "address6": "2001:db8:4005:100::2"}]}

    def keep(self, results, real):
        t = copy.deepcopy(by_name(ac_tasks(), "Keep the sessions"))
        return run_play([t, write_out("ac_sessions")], {"_ac_sessions": {"results": results}, "ac_real": real})

    def test_both_families_and_an_unreachable_node(self):
        s4 = {"family": 4, "peer": "10.255.128.3", "state": "Established", "accepted": 1, "advertised": 1}
        s6 = {"family": 6, "peer": "2001:db8:4005:100::3", "state": "Active", "accepted": 0, "advertised": 0}
        out = self.keep([{"item": "bgw-1", "stdout": json.dumps([s4, s6])},
                         {"item": "bgw-2", "unreachable": True, "msg": "ssh: connect timed out"}], self.REAL6)
        self.assertEqual(out[0], dict(s4, node="bgw-1", address="10.255.128.1"))
        self.assertEqual(out[1], dict(s6, node="bgw-1", address="2001:db8:4005:100::1"))
        self.assertEqual(out[2], {"node": "bgw-2", "address": "10.255.128.2", "family": 4, "peer": "10.255.128.3",
                                  "state": "Unknown", "lastReset": "ssh: connect timed out"})
        self.assertEqual(out[3]["family"], 6)
        self.assertEqual(out[3]["address"], "2001:db8:4005:100::2")
        self.assertEqual(out[3]["state"], "Unknown")

    def test_ipv4_only(self):
        real = {"ce": "10.255.128.3", "nodes": [{"name": "bgw-1", "address": "10.255.128.1"}]}
        s4 = {"family": 4, "peer": "10.255.128.3", "state": "Established"}
        out = self.keep([{"item": "bgw-1", "stdout": json.dumps([s4])}], real)
        self.assertEqual(out, [dict(s4, node="bgw-1", address="10.255.128.1")])
        out = self.keep([{"item": "bgw-1", "msg": "down"}], real)
        self.assertEqual([s["family"] for s in out], [4])

    def report(self, sessions, real=None):
        t = copy.deepcopy(by_name(ac_tasks(), "Report the circuit"))
        expr = "{'outcome': fab_outcome, 'reason': fab_reason, 'message': fab_message, 'waiting': fab_waiting_for}"
        task = dict(write_out(expr), vars=t["vars"])
        return run_play([task], {"ac_sessions": sessions, "ac_real": real or self.REAL6, "ac_vlan": 3000,
                                 "ac_plan": {"ce": "10.255.128.3"}, "ac_entry": {"nodes": NODES},
                                 "fab_real": {"attachments": {"maxPrefixRestart": 2}}, "ac_status": {},
                                 "ac_asn": 65010, "ac_policy": {"notes": []}, "ac_ce_config": {}})

    def test_ready_needs_every_family(self):
        up = lambda n, f: {"node": n, "family": f, "state": "Established", "accepted": 1, "advertised": 1}  # noqa: E731
        out = self.report([up("bgw-1", 4), up("bgw-1", 6), up("bgw-2", 4), up("bgw-2", 6)])
        self.assertEqual((out["outcome"], out["reason"]), ("ready", "Ready"))
        self.assertTrue(out["message"].startswith("Established on bgw-1, bgw-1 IPv6, bgw-2, bgw-2 IPv6;"))
        down6 = {"node": "bgw-2", "family": 6, "state": "Idle", "lastReset": "Maximum Number of Prefixes Reached"}
        out = self.report([up("bgw-1", 4), up("bgw-1", 6), up("bgw-2", 4), down6])
        self.assertEqual((out["outcome"], out["reason"]), ("pending", "PartiallyEstablished"))
        self.assertIn("bgw-2 IPv6 Idle Maximum Number of Prefixes Reached", out["message"])
        self.assertIn("more routes than routing.bgp.maxPrefixes allows", out["message"])
        self.assertEqual(out["waiting"], ["the CE: BGP from bgw-2 IPv6"])

    def test_ipv4_message_as_before(self):
        real = {"ce": "10.255.128.3", "nodes": []}
        out = self.report([{"node": "bgw-1", "family": 4, "state": "Active", "lastReset": ""},
                           {"node": "bgw-2", "family": 4, "state": "Active", "lastReset": ""}], real)
        self.assertEqual(out["message"], "Realized on bgw-1, bgw-2 (VLAN 3000, CE 10.255.128.3); BGP bgw-1 Active, "
                                         "bgw-2 Active: configure the CE from status.ceConfig")
        self.assertEqual(out["waiting"], ["the CE: BGP from bgw-1, bgw-2"])
        old_tasks = base_ac_tasks()
        if old_tasks is not None:
            # the release's report on the same IPv4 sessions (without family) says the same
            t = copy.deepcopy(by_name(old_tasks, "Report the circuit"))
            expr = "{'outcome': fab_outcome, 'reason': fab_reason, 'message': fab_message, 'waiting': fab_waiting_for}"
            sessions = [{"node": "bgw-1", "state": "Established", "accepted": 3, "advertised": 1},
                        {"node": "bgw-2", "state": "Idle", "lastReset": "Maximum Number of Prefixes Reached"}]
            old = run_play([dict(write_out(expr), vars=t["vars"])],
                           {"ac_sessions": sessions, "ac_vlan": 3000, "ac_plan": {"ce": "10.255.128.3"},
                            "ac_entry": {"nodes": NODES}, "fab_real": {"attachments": {"maxPrefixRestart": 2}},
                            "ac_status": {}, "ac_asn": 65010, "ac_policy": {"notes": []}, "ac_ce_config": {},
                            "ac_real": real})
            new = self.report([dict(s, family=4) for s in sessions], real)
            self.assertEqual(new, old)

    def test_session_command_per_family(self):
        t = copy.deepcopy(by_name(ac_tasks(), "The sessions from each node"))
        task = {"ansible.builtin.set_fact": {"c": t["ansible.builtin.shell"]["cmd"]}}
        first = lambda real: run_play([task, write_out("c")], {"ac_real": real}).splitlines()[0]  # noqa: E731
        self.assertEqual(first(self.REAL6), "python3 - 'e10005' '4=10.255.128.3' '6=2001:db8:4005:100::3' <<'PY'")
        self.assertEqual(first({"vrf": "e10005", "ce": "10.255.128.3"}), "python3 - 'e10005' '4=10.255.128.3'  <<'PY'")


class WiringTest(unittest.TestCase):
    """The fabric job hands the planner what it needs for IPv6; the realization defaults ship IPv6 off."""

    def test_attachments_plan_request(self):
        t = by_name(load(os.path.join(ROLES, "hybridfabric_provision", "tasks", "main.yml")),
                    "Plan the attachment circuits on the nodes")
        req = t["vars"]["fab_check"]
        self.assertIn("fab_real.core", req["core"])
        self.assertIn("fab_real.translation", req["translation"])
        self.assertIn("hf_egress.networks", req["egress"])

    def test_defaults(self):
        real = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]
        self.assertEqual(real["attachments"]["linkPool6"], "")
        self.assertEqual(real["attachments"]["reserved6"], [])

    def test_check_request(self):
        t = by_name(ac_tasks(), "Check the accept list")
        req = t["vars"]["fab_check"]
        for k in ("attachments", "core", "translation", "egressInternet6"):
            self.assertIn(k, req)
        t = by_name(ac_tasks(), "The circuit's link addresses")
        self.assertIn("ac_policy.ipv6", t["vars"]["fab_check"]["ipv6"])


if __name__ == "__main__":
    unittest.main()

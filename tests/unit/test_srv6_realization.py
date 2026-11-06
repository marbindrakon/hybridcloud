"""The SRv6 core transport in eda/common/files/shc_fabric.py: the realization keys core.transport,
core.transportByNode and core.srv6.block (defaults, check-fabric's validation), the SRv6 locator pe-addresses
returns (locator, locatorNode), the node-transport sub-command, and the SID block among the platform's IPv6 ranges.

With every node on mpls and no block, check-fabric, pe-addresses, platform_ranges6 and check-circuit answer exactly
what the release before answered (BASE below, read from Git; those comparisons are skipped without it), apart from
the new keys. Documentation and private ranges only; the SID block examples are made up."""
import copy
import importlib.util
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT, load  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_fabric as F  # noqa: E402

# the release before the SRv6 transport
BASE = "72f86002"


def base_module():
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:eda/common/files/shc_fabric.py"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    path = os.path.join(tempfile.mkdtemp(prefix="shc-fabric-base-"), "shc_fabric_base_srv6.py")
    with open(path, "w") as f:
        f.write(p.stdout)
    spec = importlib.util.spec_from_file_location("shc_fabric_base_srv6", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE_MOD = base_module()

REAL = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]
BLOCK = "5f00:db8::/32"
CORE = {"bridge": "br-core", "loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24", "isisArea": "49.0001",
        "srgb": {"start": 16000, "end": 23999}}
CORE6 = dict(CORE, loopbackPool6="2001:db8:4005:ffff::/64", sidOffset6=1000, icmpSource6="2001:db8:4005:1::/64")
SRV6 = dict(CORE6, transport="srv6", srv6={"block": BLOCK})
NODES = ["bgw-1", "bgw-2"]


def spec(nodes=None):
    return {"borderGateway": {"nodes": [{"name": n} for n in (NODES if nodes is None else nodes)]}}


def fabric(core, nodes=None, **real):
    return F.check_fabric({"spec": spec(nodes), "realization": dict({"core": core}, **real)})


def errs(core, nodes=None, **real):
    return " | ".join(fabric(core, nodes, **real)["errors"])


class Defaults(unittest.TestCase):
    def test_realization_keys(self):
        core = REAL["core"]
        self.assertEqual(core["transport"], "mpls")
        self.assertEqual(core["transportByNode"], {})
        self.assertEqual(core["srv6"], {"block": ""})

    def test_defaults_pass(self):
        r = fabric(REAL["core"])
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["transports"], {"bgw-1": "mpls", "bgw-2": "mpls"})

    def test_whole_default_realization_passes(self):
        r = F.check_fabric({"spec": spec(), "realization": copy.deepcopy(REAL)})
        self.assertEqual(r["errors"], [])


CORPUS = [
    CORE, CORE6, dict(CORE6, icmpSource6=""), dict(CORE, icmpSource6="2001:db8:4005:1::/64"),
    dict(CORE6, loopbackPool6="10.254.0.0/24"), dict(CORE6, icmpSource6="2001:db8:4005:ffff::/96"),
    dict(CORE6, sidOffset6=200), dict(CORE, loopbackPool="10.255.0.0/23", linkSubnet="10.255.2.0/23"),
    dict(CORE, linkSubnet="10.255.0.0/24"), dict(CORE, loopbackPool="x"),
    dict(REAL["core"]), dict(REAL["core"], loopbackPool6="2001:db8:4005:ffff::/64"),
]
AT6 = {"enabled": True, "vlanPool": {"start": 3000, "end": 3099}, "linkPool": "10.255.128.0/20", "linkPrefixLength": 29,
       "linkPool6": "2001:db8:4005:100::/56", "reserved6": ["2001:db8:4005::/52"]}


@unittest.skipIf(BASE_MOD is None, f"release {BASE} not readable from Git")
class AsTheRelease(unittest.TestCase):
    """mpls everywhere and no block: the release's answers, plus the new keys only."""

    def test_check_fabric(self):
        for core in CORPUS:
            for nodes in (NODES, [], ["bgw-1"] * 2):
                for real in ({}, {"attachments": AT6}):
                    req = {"spec": spec(nodes), "realization": dict({"core": copy.deepcopy(core)}, **real)}
                    new, old = F.check_fabric(copy.deepcopy(req)), BASE_MOD.check_fabric(copy.deepcopy(req))
                    self.assertEqual(set(new["transports"].values()) - {"mpls"}, set(), core)
                    new.pop("transports")
                    self.assertEqual(new, old, (core, nodes, real))

    def test_pe_addresses(self):
        for core in (CORE, CORE6, dict(CORE6, icmpSource6="")):
            for index in (1, 2, 254, 255, 0):
                for kw in ({}, {"nodeOrdinal": 1}, {"nodeOrdinal": "2"}, {"nodeOrdinal": ""}, {"nodeOrdinal": 0}):
                    req = dict({"core": core, "index": index}, **kw)
                    new, old = F.pe_addresses(copy.deepcopy(req)), BASE_MOD.pe_addresses(copy.deepcopy(req))
                    if not old["errors"]:
                        self.assertEqual((new.pop("locator"), new.pop("locatorNode")), ("", 0))
                    self.assertEqual(new, old, req)

    def test_platform_ranges_and_circuits(self):
        tr = {"site-vm": {"pref64": "2001:db8:4001:66::/96"}}
        for core in (CORE, CORE6, REAL["core"]):
            self.assertEqual(F.platform_ranges6(AT6, core, tr), BASE_MOD.platform_ranges6(AT6, core, tr))
            req = {"accept": ["2001:db8:4005:ffff::/64 le 128", "fd00:10::/48", "10.20.0.0/16"],
                   "space": ["10.10.0.0/16", "fd00:1::/48"], "reserved": ["10.255.0.0/24"], "attachments": AT6,
                   "core": core, "translation": tr}
            self.assertEqual(F.check_circuit(copy.deepcopy(req)), BASE_MOD.check_circuit(copy.deepcopy(req)))


class CheckTransport(unittest.TestCase):
    def test_srv6_everywhere(self):
        r = fabric(SRV6)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["transports"], {"bgw-1": "srv6", "bgw-2": "srv6"})

    def test_one_node(self):
        r = fabric(dict(CORE6, transportByNode={"bgw-2": "srv6"}, srv6={"block": BLOCK}))
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["transports"], {"bgw-1": "mpls", "bgw-2": "srv6"})
        # and the other way round: srv6 the default, one node back on mpls
        r = fabric(dict(SRV6, transportByNode={"bgw-1": "mpls"}))
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["transports"], {"bgw-1": "mpls", "bgw-2": "srv6"})

    def test_empty_values_are_mpls(self):
        r = fabric(dict(CORE, transport="", transportByNode={"bgw-1": ""}))
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["transports"], {"bgw-1": "mpls", "bgw-2": "mpls"})
        self.assertEqual(fabric(dict(CORE, transport=None, transportByNode=None))["errors"], [])

    def test_unknown_transport(self):
        self.assertIn("core.transport 'MPLS' is not one of mpls, srv6", errs(dict(CORE, transport="MPLS")))
        self.assertIn("core.transportByNode.bgw-1 'sr-mpls' is not one of", errs(dict(CORE, transportByNode={"bgw-1": "sr-mpls"})))

    def test_unknown_node(self):
        e = errs(dict(CORE, transportByNode={"bgw-3": "mpls"}))
        self.assertIn("core.transportByNode.bgw-3 names no node of borderGateway.nodes", e)

    def test_by_node_not_a_mapping(self):
        self.assertIn("core.transportByNode is not a mapping", errs(dict(CORE, transportByNode=["bgw-1"])))

    def test_srv6_needs_the_ipv6_core(self):
        e = errs(dict(CORE, transport="srv6", srv6={"block": BLOCK}))
        self.assertIn("srv6 (nodes bgw-1, bgw-2) needs the dual-stack core (core.loopbackPool6)", e)
        e = errs(dict(CORE, transportByNode={"bgw-2": "srv6"}, srv6={"block": BLOCK}))
        self.assertIn("srv6 (nodes bgw-2) needs the dual-stack core", e)

    def test_srv6_needs_a_block(self):
        for core in (dict(CORE6, transport="srv6"), dict(CORE6, transport="srv6", srv6={"block": ""}),
                     dict(CORE6, transport="srv6", srv6={})):
            self.assertIn("needs core.srv6.block", errs(core))

    def test_malformed_block_named_once(self):
        e = fabric(dict(CORE6, transport="srv6", srv6={"block": "5f00:db8::1/32"}))["errors"]
        self.assertEqual(len(e), 1, e)
        self.assertIn("core.srv6.block", e[0])
        self.assertIn("core.srv6 is not a mapping", errs(dict(CORE6, srv6="5f00:db8::/32")))


class CheckBlock(unittest.TestCase):
    def test_exactly_a_32_inside_5f00(self):
        for b in ("5f00:db8::/31", "5f00:db8::/33", "5f00:db8::/48", "2001:db8::/32", "5f01:db8::/32", "10.0.0.0/32"):
            e = errs(dict(SRV6, srv6={"block": b}))
            self.assertIn("core.srv6.block", e, b)
        self.assertEqual(errs(dict(SRV6, srv6={"block": "5f00:ffff::/32"})), "")
        self.assertEqual(errs(dict(SRV6, srv6={"block": "5f00::/32"})), "")

    def test_checked_while_every_node_is_mpls(self):
        self.assertEqual(errs(dict(CORE6, srv6={"block": BLOCK})), "")
        self.assertIn("is not a /32 inside 5f00::/16", errs(dict(CORE6, srv6={"block": "2001:db8::/32"})))
        # a block without the IPv6 core is fine while no node uses it
        self.assertEqual(errs(dict(CORE, srv6={"block": BLOCK})), "")

    def test_apart_from_the_core_ranges(self):
        e = errs(dict(SRV6, loopbackPool6="5f00:db8:ffff::/64"))
        self.assertIn(f"core.srv6.block {BLOCK} overlaps core.loopbackPool6 5f00:db8:ffff::/64", e)
        e = errs(dict(SRV6, icmpSource6="5f00:db8:1::/64"))
        self.assertIn(f"core.srv6.block {BLOCK} overlaps core.icmpSource6 5f00:db8:1::/64", e)
        # a pool that holds the whole block overlaps it too
        e = errs(dict(SRV6, icmpSource6="5f00::/16"))
        self.assertIn("overlaps core.icmpSource6 5f00::/16", e)

    def test_apart_from_the_circuit_links(self):
        at = dict(AT6, linkPool6="5f00:db8:4005::/56")
        e = errs(SRV6, attachments=at)
        self.assertIn(f"attachments.linkPool6 5f00:db8:4005::/56 overlaps core.srv6.block {BLOCK}", e)
        self.assertEqual(errs(SRV6, attachments=AT6), "")

    def test_apart_from_the_routed_egress_space(self):
        eg = {"ipv6": {"routedPrefixes": ["5f00:db8:8000::/33"]}}
        self.assertIn("overlaps egress.ipv6.routedPrefixes 5f00:db8:8000::/33", errs(SRV6, egress=eg))
        eg = {"ipv6": {"routedPrefixes": ["2001:db8:4000::/40"]}}
        self.assertEqual(errs(SRV6, egress=eg), "")

    def test_nodes_fit_the_node_position(self):
        many = [f"bgw-{i}" for i in range(1, 257)]
        self.assertIn("256 border gateway nodes do not fit", errs(SRV6, nodes=many))
        self.assertNotIn("do not fit", errs(SRV6, nodes=many[:255]))
        # without a block the node count is not limited by it
        self.assertNotIn("do not fit", errs(CORE6, nodes=many))

    def test_indexes_fit_the_index_field(self):
        big = dict(SRV6, loopbackPool="10.255.0.0/23", linkSubnet="10.255.2.0/23")
        self.assertEqual(fabric(big)["peIndexMax"], 510)
        self.assertIn("the highest provider-edge index 510 does not fit", errs(big))
        # one of the two pools at most 256 addresses keeps the index at 254
        self.assertEqual(errs(dict(big, linkSubnet="10.255.2.0/24")), "")
        self.assertNotIn("does not fit", errs(dict(CORE6, loopbackPool="10.255.0.0/23", linkSubnet="10.255.2.0/23")))


class Locator(unittest.TestCase):
    def pe(self, core, index, **kw):
        return F.pe_addresses(dict({"core": core, "index": index}, **kw))

    def test_layout(self):
        r = self.pe(SRV6, 2, nodeOrdinal=1)
        self.assertEqual(r["errors"], [])
        self.assertEqual((r["locator"], r["locatorNode"]), ("5f00:db8:102::/48", 0x102))
        self.assertEqual(self.pe(SRV6, 1, nodeOrdinal=2)["locator"], "5f00:db8:201::/48")
        self.assertEqual(self.pe(SRV6, 254, nodeOrdinal=255)["locator"], "5f00:db8:fffe::/48")
        self.assertEqual(self.pe(dict(SRV6, srv6={"block": "5f00:1::/32"}), 16, nodeOrdinal=1)["locator"],
                         "5f00:1:110::/48")

    def test_unique_across_nodes_while_loopbacks_repeat(self):
        seen = {}
        for o in (1, 2, 3):
            for i in (1, 2, 3, 254):
                r = self.pe(SRV6, i, nodeOrdinal=o)
                self.assertNotIn(r["locator"], seen)
                seen[r["locator"]] = (o, i)
                self.assertEqual(r["loopbackIp6"], self.pe(SRV6, i, nodeOrdinal=1)["loopbackIp6"])
                net = ipaddress.ip_network(r["locator"])
                self.assertTrue(net.subnet_of(ipaddress.ip_network(BLOCK)))
                self.assertEqual(net.prefixlen, 48)

    def test_other_keys_unchanged(self):
        a, b = self.pe(CORE6, 2, nodeOrdinal=1), self.pe(SRV6, 2, nodeOrdinal=1)
        for k in ("locator", "locatorNode"):
            a.pop(k), b.pop(k)
        self.assertEqual(a, b)

    def test_without_block_or_position(self):
        self.assertEqual((lambda r: (r["locator"], r["locatorNode"]))(self.pe(CORE6, 2, nodeOrdinal=1)), ("", 0))
        self.assertEqual((lambda r: (r["locator"], r["locatorNode"]))(self.pe(SRV6, 2)), ("", 0))
        self.assertEqual((lambda r: (r["locator"], r["locatorNode"]))(self.pe(SRV6, 2, nodeOrdinal="")), ("", 0))

    def test_numbers_as_text(self):
        self.assertEqual(self.pe(SRV6, "3", nodeOrdinal="2")["locator"], "5f00:db8:203::/48")

    def test_canonical_text(self):
        r = self.pe(dict(SRV6, srv6={"block": "5F00:0DB8:0000::/32"}), 10, nodeOrdinal=1)
        self.assertEqual(r["locator"], "5f00:db8:10a::/48")

    def test_out_of_range(self):
        self.assertIn("does not fit the SRv6 locator", self.pe(SRV6, 2, nodeOrdinal=256)["errors"][0])
        self.assertTrue(self.pe(SRV6, 2, nodeOrdinal=0)["errors"])
        big = dict(SRV6, loopbackPool="10.255.0.0/23", linkSubnet="10.255.2.0/23")
        self.assertTrue(self.pe(big, 256, nodeOrdinal=1)["errors"])
        self.assertEqual(self.pe(big, 256)["errors"], [], "no position: no locator to fit")

    def test_bad_block(self):
        self.assertIn("core.srv6.block", self.pe(dict(SRV6, srv6={"block": "2001:db8::/32"}), 2, nodeOrdinal=1)["errors"][0])

    def test_cli(self):
        req = {"action": "pe-addresses", "core": SRV6, "index": "2", "nodeOrdinal": "1"}
        p = subprocess.run([sys.executable, "-I", os.path.join(FILES, "shc_fabric.py")], input=json.dumps(req),
                           capture_output=True, text=True, check=True)
        r = json.loads(p.stdout)
        self.assertEqual((r["locator"], r["locatorNode"], r["loopbackIp6"]),
                         ("5f00:db8:102::/48", 258, "2001:db8:4005:ffff::2"))


class NodeTransport(unittest.TestCase):
    def nt(self, core, node):
        return F.node_transport_cmd({"core": core, "node": node})

    def test_resolution(self):
        self.assertEqual(self.nt({}, "bgw-1"), {"errors": [], "transport": "mpls"})
        self.assertEqual(self.nt(REAL["core"], "bgw-1")["transport"], "mpls")
        self.assertEqual(self.nt({"transport": "srv6"}, "bgw-1")["transport"], "srv6")
        core = {"transport": "mpls", "transportByNode": {"bgw-2": "srv6"}}
        self.assertEqual(self.nt(core, "bgw-1")["transport"], "mpls")
        self.assertEqual(self.nt(core, "bgw-2")["transport"], "srv6")
        self.assertEqual(self.nt({"transport": "srv6", "transportByNode": {"bgw-2": ""}}, "bgw-2")["transport"], "srv6")

    def test_invalid(self):
        r = self.nt({"transport": "srv6", "transportByNode": {"bgw-1": "SRv6"}}, "bgw-1")
        self.assertEqual(r["transport"], "")
        self.assertIn("'SRv6' of node bgw-1 is not one of mpls, srv6", r["errors"][0])

    def test_cli(self):
        req = {"action": "node-transport", "core": {"transportByNode": {"bgw-2": "srv6"}}, "node": "bgw-2"}
        p = subprocess.run([sys.executable, "-I", os.path.join(FILES, "shc_fabric.py")], input=json.dumps(req),
                           capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(p.stdout), {"errors": [], "transport": "srv6"})


class PlatformRange(unittest.TestCase):
    def test_block_in_platform_ranges6(self):
        got = [str(n) for n in F.platform_ranges6(AT6, SRV6, {})]
        self.assertIn(BLOCK, got)
        self.assertEqual([str(n) for n in F.platform_ranges6({}, {"srv6": {"block": BLOCK}}, None)], [BLOCK])
        # unusable values are left out
        self.assertEqual(F.platform_ranges6({}, {"srv6": {"block": "x"}}, None), [])
        self.assertEqual(F.platform_ranges6({}, {"srv6": "x"}, None), [])

    def test_circuit_may_not_accept_into_the_block(self):
        req = {"accept": ["5f00:db8:102::/48", "5f00::/16 le 64"], "space": ["10.10.0.0/16", "fd00:1::/48"],
               "reserved": [], "attachments": AT6, "core": SRV6}
        r = F.check_circuit(req)
        self.assertIn(f"accept entry '5f00:db8:102::/48' lies inside the platform range {BLOCK}", r["errors"])
        self.assertIn(f"routes overlapping the platform range {BLOCK} are never accepted from the CE", r["notes"])


if __name__ == "__main__":
    unittest.main()

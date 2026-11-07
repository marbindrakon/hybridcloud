"""IPv6 path MTU discovery from the internet to a site behind a small leg, both transports: a gateway namespace sources
its Packet Too Big (and Time Exceeded, Unreachable) from its own ICMPv6 source (core.icmpSource6: network + (node
position << 16) + provider-edge index), so the root's egress filter must admit those sources from a network.
- The plan (shc_fabric.py egress-plan): every network's src6 holds its block and the node's /112 of core.icmpSource6,
  which holds the ICMPv6 sources of that node's root and gateways (pe-addresses' icmp6 for every index) and nothing
  of another node's or of a cloud edge router's (ordinal 100 and up); without the pool, the block alone as before.
- The apply renders the set src6_<tag> without a member another member covers (the root's own ICMPv6 source, which
  the node's job adds, lies in the slice; nftables refuses overlapping elements in an interval set), and nft accepts it.
Documentation prefixes only."""
import copy
import ipaddress
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_egress_ipv6 as E  # noqa: E402
import test_root_circuit_ipv6 as C  # noqa: E402
from test_shc_fabric_apply import ApplyCase  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6, nft_check, root_full  # noqa: E402

sys.path.insert(0, os.path.join(C.ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

POOL = "2001:db8:4005:1::/64"
CORE = {"loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24", "loopbackPool6": "2001:db8:4005:ffff::/64",
        "icmpSource6": POOL}


def icmp6(ordinal, index):
    r = F.pe_addresses({"core": CORE, "index": index, "nodeOrdinal": ordinal})
    assert not r["errors"], r
    return ipaddress.ip_interface(r["icmp6"]).ip


class Plan(unittest.TestCase):
    def plan(self, core):
        r = F.egress_plan(E.plan_request(core=core))
        self.assertEqual(r["errors"], [])
        return {n: {e["vrf"]: e["ipv6"]["src6"] for e in r["nodes"][n]["networks"] if "ipv6" in e}
                for n in ("bgw-1", "bgw-2")}

    def test_slice_per_node(self):
        src = self.plan(dict(E.CORE6, icmpSource6=POOL))
        self.assertEqual(src["bgw-1"], {"e10000": [E.BLOCK, "2001:db8:4005:1::1:0/112"],
                                        "e10001": ["2001:db8:4005:4300::/56", "2001:db8:4005:1::1:0/112"]})
        self.assertEqual(src["bgw-2"]["e10000"], [E.BLOCK, "2001:db8:4005:1::2:0/112"])
        for node, ordinal, other in (("bgw-1", 1, 2), ("bgw-2", 2, 1)):
            mine = ipaddress.ip_network(src[node]["e10000"][1])
            for index in (1, 2, 6, 254):  # the root (1) and gateways on that node
                self.assertIn(icmp6(ordinal, index), mine, (node, index))
                self.assertNotIn(icmp6(other, index), mine, (node, index))  # the other node's
            for ordinal_edge in (100, 101):  # cloud edge routers' ICMPv6 sources (their own ordinals)
                self.assertNotIn(icmp6(ordinal_edge, 2), mine)
            self.assertEqual(mine.num_addresses, 65536)

    def test_without_the_pool_as_before(self):
        src = self.plan(dict(E.CORE6))
        self.assertEqual(src["bgw-1"]["e10000"], [E.BLOCK])
        src = self.plan(dict(E.CORE6, icmpSource6=""))
        self.assertEqual(src["bgw-2"]["e10000"], [E.BLOCK])

    def test_without_ipv6_egress_nothing(self):
        req = E.plan_request(core=dict(E.CORE6, icmpSource6=POOL))
        del req["egress"]["ipv6"]
        r = F.egress_plan(req)
        self.assertEqual(r["errors"], [])
        self.assertTrue(all("ipv6" not in e for e in r["nodes"]["bgw-1"]["networks"]))

    def test_slice_function(self):
        core = {"icmpSource6": "2001:db8:4005:1::/96"}
        self.assertEqual(str(F.icmp6_slice(core, 1)), "2001:db8:4005:1::1:0/112")
        self.assertEqual(str(F.icmp6_slice(core, 65535)), "2001:db8:4005:1::ffff:0/112")
        with self.assertRaises(ValueError):
            F.icmp6_slice(core, 65536)
        with self.assertRaises(ValueError):
            F.icmp6_slice(core, 0)
        self.assertIsNone(F.icmp6_slice({}, 1))

    def test_cli(self):
        req = E.plan_request(core=dict(E.CORE6, icmpSource6=POOL))
        p = subprocess.run([sys.executable, os.path.join(C.ROOT, "eda", "common", "files", "shc_fabric.py")],
                           input=json.dumps(req), capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = json.loads(p.stdout)
        self.assertEqual(r["nodes"]["bgw-2"]["networks"][0]["ipv6"]["src6"], [E.BLOCK, "2001:db8:4005:1::2:0/112"])


def root_conf(src6, **over):
    c = root_full(**over)
    c["egress"]["networks"][0]["ipv6"]["src6"] = list(src6)
    return c


class Apply(ApplyCase):
    SET = "  set src6_10000 { type ipv6_addr; flags interval; elements = { %s }; }"

    def nft(self, conf):
        h = self.h
        h.conf("root", conf)
        h.ok(self, "root")
        return h.get_state()["ns"][""]["nft"]["inet shc_egress6"]

    def test_slice_admitted_on_both_transports(self):
        for t, extra in (("mpls", {}), ("srv6", ROOT_SRV6)):
            with self.subTest(transport=t):
                text = self.nft(root_conf([E.BLOCK, "2001:db8:4005:1::1:0/112"], **extra))
                # the root's own ICMPv6 source (2001:db8:4005:1::1:1, from egress.ipv6.icmp6) lies in the slice
                self.assertIn(self.SET % (E.BLOCK + ", 2001:db8:4005:1::1:0/112"), text)
                self.assertIn('oifname "xe10000" ip6 saddr != @src6_10000 counter drop', text)
                nft_check(self, text)

    def test_without_the_slice_as_before(self):
        text = self.nft(root_conf([E.BLOCK]))
        self.assertIn(self.SET % (E.BLOCK + ", 2001:db8:4005:1::1:1"), text)
        nft_check(self, text)

    def test_covered_members_dropped_in_order(self):
        text = self.nft(root_conf(["2001:db8:4005:1::1:0/112", E.BLOCK, "2001:db8:4005:4200::/64",
                                   "2001:db8:4005:1::1:0/112"]))
        self.assertIn(self.SET % ("2001:db8:4005:1::1:0/112, " + E.BLOCK), text)
        nft_check(self, text)

    def test_nft_refuses_the_overlap(self):
        """why the apply drops covered members: nftables refuses an element inside another"""
        text = self.nft(root_conf([E.BLOCK, "2001:db8:4005:1::1:0/112"]))
        overlap = text.replace(E.BLOCK + ", 2001:db8:4005:1::1:0/112 }",
                               E.BLOCK + ", 2001:db8:4005:1::1:0/112, 2001:db8:4005:1::1:1 }")
        self.assertNotEqual(overlap, text)
        err = C.nft_check(overlap)
        self.assertIsNotNone(err)
        self.assertIn("conflicting intervals", err)


if __name__ == "__main__":
    unittest.main()

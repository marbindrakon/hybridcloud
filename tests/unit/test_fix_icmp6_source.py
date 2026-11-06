"""The ICMPv6 errors a namespace sends for a packet in one of its VRFs carry that VRF's ICMPv6 source (icmp6) on the SRv6
core transport too, as on MPLS. A packet a SID decapsulates into a VRF came in by the core leg (gateway core0, root
core bridge), which is outside every VRF: the kernel then picks the error's source among the default VRF's addresses
(the IPv6 loopback, a core address that is never announced outside the core) instead of the VRF's. The root's egress
filter drops such an error on its way to the internet (the gateways' Packet Too Big: path MTU discovery from the
internet to a site behind a small leg stalls), and a tenant sees a core address in the others.
shc-fabric-apply, transport srv6, with icmp6: the guard table inet shc_core_guard gains chain errors (output, filter
type: a changed source must not reroute the packet out of its VRF): an ICMPv6 error leaving by the core leg toward an
address outside the core (the SID block, loopbackPool6, link-local) takes icmp6 as its source. Without icmp6 the table
is as before; transport mpls has no such table (unchanged). The guard action keeps the chain like the rest of the
table. Run against the fake kernel of test_shc_fabric_apply; `nft -c` where an unprivileged network namespace allows.
Documentation prefixes and an arbitrary block of 5f00::/16 only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import ApplyCase, writes  # noqa: E402
from test_srv6_fabric_apply import BLOCK, GW_SRV6, ROOT_SRV6, gw_full, nft_check, root_full  # noqa: E402

TYPES = "icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem }"
HOOK = "    type filter hook output priority filter; policy accept;"


def rule(leg, core, src):
    return ('    oifname "%s" ip6 daddr != { %s } %s counter ip6 saddr set %s comment "VRF errors from the ICMP source"'
            % (leg, core, TYPES, src))


class ErrorsChain(ApplyCase):
    def guard(self, ns):
        return self.h.get_state()["ns"][ns]["nft"]["inet shc_core_guard"]

    def test_root_and_gateway(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        out, log = h.ok(self, "root")
        self.keep("icmp6-source-root", log, out)
        g = self.guard("")
        want = rule("br-core", "2001:db8:4005:ffff::/64, %s, fe80::/10" % BLOCK, "2001:db8:4005:1::1:1")
        self.assertIn("  chain errors {\n%s\n%s\n  }\n" % (HOOK, want), g)
        # the guard's own chain as before, and still the first change of the run
        self.assertIn('    iifname != { "br-core", "lo" } ip6 daddr %s counter drop comment ' % BLOCK, g)
        self.assertEqual(writes(log)[0], "W [root] nft -f -")
        nft_check(self, g)
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        out, log = h.ok(self, "gw-iaas1")
        self.keep("icmp6-source-gw", log, out)
        g = self.guard("gw-iaas1")
        self.assertIn(rule("core0", "2001:db8:4005:ffff::/64, %s, fe80::/10" % BLOCK, "2001:db8:4005:1::1:2"), g)
        nft_check(self, g)
        # a second run of each changes nothing
        for name in ("root", "gw-iaas1"):
            out, _ = h.ok(self, name)
            self.assertTrue(out.strip().endswith("changed=no"), (name, out))

    def test_without_icmp6_as_before(self):
        h = self.h
        h.conf("root", root_full(**dict(ROOT_SRV6, icmp6="")))
        h.ok(self, "root")
        g = self.guard("")
        self.assertNotIn("chain errors", g)
        self.assertNotIn("saddr set", g)
        self.assertIn('    iifname != { "br-core", "lo" } ip6 daddr %s counter drop comment ' % BLOCK, g)

    def test_core_prefixes_merged(self):
        """the destinations that keep their source form one interval set: a pool inside the block is merged into it
        (nftables refuses overlapping elements)"""
        h = self.h
        h.conf("root", root_full(**dict(ROOT_SRV6, loopbackPool6="5f00:1234:ffff::/64",
                                        loopback6="5f00:1234:ffff::1/128")))
        h.ok(self, "root")
        g = self.guard("")
        self.assertIn(rule("br-core", "%s, fe80::/10" % BLOCK, "2001:db8:4005:1::1:1"), g)
        nft_check(self, g)

    def test_mpls_has_no_such_table(self):
        h = self.h
        h.conf("root", root_full())
        h.ok(self, "root")
        self.assertNotIn("inet shc_core_guard", self.h.get_state()["ns"][""]["nft"])
        self.assertFalse([t for t in self.h.get_state()["ns"][""]["nft"].values() if "saddr set" in t])

    def test_guard_action_restores_the_chain(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        want = self.guard("")
        st = h.get_state()
        cut = want.index("  chain errors {")
        st["ns"][""]["nft"]["inet shc_core_guard"] = want[:cut] + "}\n"
        h.set_state(st)
        rc, out, err, log = h.run("root", "guard")
        self.assertEqual((rc, out.strip()), (0, "guard=restored"), err)
        self.assertEqual(self.guard(""), want)
        rc, out, err, log = h.run("root", "guard")
        self.assertEqual((rc, out.strip(), writes(log)), (0, "guard=ok", []))


if __name__ == "__main__":
    unittest.main()

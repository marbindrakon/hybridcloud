"""The SRv6 locator announced as a prefix of lo instead of a redistributed discard route. FRR lost the redistribution
when watchfrr restarted an instance's daemons after zebra died (the locator gone from IS-IS, the site cut off until a
reboot); a passive interface's prefix is announced on every start of isisd. shc-fabric-apply, transport srv6, root
and gateway: the anchor (the locator's first address with the locator's length, nodad noprefixroute) on lo once lo
holds its loopbacks, the locator blackholed in the main table at metric 4278198272 (an unallocated SID dropped
without an answer), both remembered in <instance>.locator6; a second run changes nothing; a changed locator replaces
both; the root's down, and a run with mpls that finds the file, remove them; mpls without the file issues no command
for them. Run against the fake kernel of test_shc_fabric_apply. Documentation prefixes and blocks of 5f00::/16."""
import copy
import ipaddress
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import ApplyCase, writes  # noqa: E402
from test_srv6_fabric_apply import GW_SRV6, ROOT_SRV6, gw_full, root_full  # noqa: E402

METRIC = "4278198272"


def anchor(loc):
    n = ipaddress.ip_network(loc)
    return str(n.network_address + 1), n.prefixlen


class LocatorOnLo(ApplyCase):
    def lo6(self, ns):
        return [a for a in self.h.link(ns, "lo")["addrs"] if a["family"] == 6]

    def blackholes(self, ns):
        return [r for r in self.h.get_state()["ns"][ns]["routes6"] if r["type"] == "blackhole"]

    def check(self, ns, loc, inst):
        a, plen = anchor(loc)
        mine = [x for x in self.lo6(ns) if x["local"] == a]
        self.assertEqual(len(mine), 1, self.lo6(ns))
        self.assertEqual((mine[0]["prefixlen"], mine[0]["nodad"], mine[0].get("noprefixroute")), (plen, True, True))
        self.assertEqual(self.blackholes(ns), [{"type": "blackhole", "dst": loc, "metric": METRIC, "table": "main"}])
        with open(os.path.join(self.h.etc, inst + ".locator6")) as f:
            self.assertEqual(f.read(), "%s/%d %s\n" % (a, plen, loc))

    def test_anchor_is_no_sid(self):
        """micro-SIDs (32-bit block, 16-bit node, 16-bit function): the anchor's function field is 0 and FRR allocates
        functions from 1; the anchor is the locator's first address"""
        for loc in ("5f00:1234:101::/48", "5f00:1234:ffff::/48"):
            a, plen = anchor(loc)
            self.assertEqual((int(ipaddress.ip_address(a)) >> 64) & 0xffff, 0)
            self.assertEqual(plen, 48)
            self.assertIn(ipaddress.ip_address(a), ipaddress.ip_network(loc))

    def test_root_and_gateway(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        out, log = h.ok(self, "root")
        self.keep("locator-lo-root", log, out)
        self.check("", "5f00:1234:101::/48", "root")
        # after the IPv6 loopback, before FRR (the apply runs first at boot)
        self.assertIn("W [root] ip -6 addr add 5f00:1234:101::1/48 dev lo nodad noprefixroute", writes(log))
        self.assertIn("W [root] ip -6 route replace blackhole 5f00:1234:101::/48 metric %s table main" % METRIC,
                      writes(log))
        out, log = h.ok(self, "root")
        self.assertTrue(out.strip().endswith("changed=no"), out)
        self.assertEqual([l for l in writes(log) if "blackhole" in l or "noprefixroute" in l], [])
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        out, log = h.ok(self, "gw-iaas1")
        self.keep("locator-lo-gw", log, out)
        self.check("gw-iaas1", "5f00:1234:102::/48", "gw-iaas1")
        self.assertIn("W [gw-iaas1] ip -6 addr add 5f00:1234:102::1/48 dev lo nodad noprefixroute", writes(log))
        # the root namespace is not touched by the gateway's run
        self.check("", "5f00:1234:101::/48", "root")
        out, _ = h.ok(self, "gw-iaas1")
        self.assertTrue(out.strip().endswith("changed=no"), out)
        h.ok(self, "gw-iaas1", "down")
        self.assertFalse(os.path.exists(os.path.join(h.etc, "gw-iaas1.locator6")))

    def test_locator_changed(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        c = copy.deepcopy(ROOT_SRV6)
        c["srv6"]["locator"] = "5f00:1234:201::/48"
        h.conf("root", root_full(**c))
        out, _ = h.ok(self, "root")
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.check("", "5f00:1234:201::/48", "root")
        self.assertEqual([x for x in self.lo6("") if x["local"] == "5f00:1234:101::1"], [])

    def test_root_down_and_mpls(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.ok(self, "root", "down")
        self.assertEqual([x for x in self.lo6("") if x["local"].startswith("5f00:")], [])
        self.assertEqual(self.blackholes(""), [])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.locator6")))
        # mpls after srv6 (the operator re-applies): the file found, the anchor and the blackhole go
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("root", root_full())
        out, log = h.ok(self, "root")
        self.assertEqual([x for x in self.lo6("") if x["local"].startswith("5f00:")], [])
        self.assertEqual(self.blackholes(""), [])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.locator6")))
        # mpls without the file: no command for it at all
        out, log = h.ok(self, "root")
        self.assertEqual([l for l in log if "blackhole" in l or l.endswith("] ip -6 addr show dev lo")], [])


if __name__ == "__main__":
    unittest.main()

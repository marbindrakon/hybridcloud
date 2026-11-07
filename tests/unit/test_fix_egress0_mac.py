"""egress0, the root's macvlan on the hand-off VLAN, had a random MAC at every creation. The hand-off router keeps
the MAC it learned for the proxied NAT pool addresses for hours, so after a blade's reboot the pool's return traffic
went to the old MAC (measured 2026-11-07: half the IPv4 internet flows of the g5 guests lost until an ARP
announcement). egress0 now carries a MAC derived from the egress address, and every apply announces the egress
address and the proxied pool addresses on the VLAN (a best-effort raw ARP; refused in this harness, reported)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import ROOT_CONF, ApplyCase, blade, writes  # noqa: E402

EGRESS = "172.18.237.13"  # ROOT_CONF's egress address
EMAC = "02:e9:ac:12:ed:0d"


class Egress0Mac(ApplyCase):
    def test_stable_mac_and_announcements(self):
        h = self.h
        self.assertEqual(ROOT_CONF["egress"]["address"].split("/")[0], EGRESS)
        h.conf("root", ROOT_CONF)
        rc, out, err, log = h.run("root")
        self.assertEqual(rc, 0, err)
        link = h.get_state()["ns"][""]["links"]["egress0"]
        self.assertEqual(link["address"], EMAC)
        self.assertIn("W [root] ip link add egress0 link bond1.237 address %s type macvlan mode bridge" % EMAC, log)
        self.assertNotIn("egress-mac=changed", out)
        self.assertIn("arp-announce=failed", err)  # the harness has no raw socket: reported on stderr, not fatal
        # a second run: nothing changes, no MAC command
        out2, log2 = h.ok(self, "root")
        self.assertEqual([l for l in writes(log2) if "egress0" in l and "address" in l], [])
        self.assertNotIn("egress-mac=changed", out2)

    def test_existing_random_mac_is_replaced(self):
        h = self.h
        h.conf("root", ROOT_CONF)
        h.ok(self, "root")
        st = h.get_state()
        st["ns"][""]["links"]["egress0"]["address"] = "5e:1a:ce:f7:10:c7"  # a macvlan from before this change
        h.set_state(st)
        out, log = h.ok(self, "root")
        self.assertIn("W [root] ip link set egress0 address %s" % EMAC, log)
        self.assertIn("egress-mac=changed", out)
        self.assertEqual(h.get_state()["ns"][""]["links"]["egress0"]["address"], EMAC)


if __name__ == "__main__":
    unittest.main()

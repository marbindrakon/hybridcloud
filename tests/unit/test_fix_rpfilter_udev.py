"""On the border gateway blades, udev's rule for a new net device runs systemd-sysctl for the device, which applies
the host's sysctl.d defaults (RHEL: net.ipv4.conf.*.rp_filter = 1) moments after the device was created. At a boot
(2026-11-07) that landed after the SRv6 root apply had set its network VRFs to 0, so decapsulated IPv4 into them was
dropped until the next apply. The apply now waits for udev (udevadm settle) before setting the VRF's rp_filter, and
the guard action (every 15 s) sets it back to 0 if anything moved it. The fake kernel applies udev's default after
the call that follows the device's creation (the measured worst case)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import ApplyCase, blade, writes  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6, root_full  # noqa: E402


class RpFilterAgainstUdev(ApplyCase):
    def setUp(self):
        super().setUp()
        st = blade()
        st["ns"][""]["sysctl"].update({"net/ipv4/conf/all/rp_filter": "0", "net/ipv4/conf/default/rp_filter": "1"})
        self.h.set_state(st)
        self.h.conf("root", root_full(**ROOT_SRV6))

    def test_fresh_build_waits_for_udev(self):
        h = self.h
        _, log = h.ok(self, "root")
        sc = h.get_state()["ns"][""]["sysctl"]
        for vrf in ("e10000",):
            self.assertEqual(sc["net/ipv4/conf/%s/rp_filter" % vrf], "0", vrf)
        # the settle comes between the VRF's creation and its sysctl
        i = next(n for n, l in enumerate(log) if l.startswith("W [root] ip link add e10000 type vrf"))
        after = log[i + 1:i + 4]
        self.assertTrue(any(l.startswith("R [root] udevadm settle") or l.startswith("W [root] udevadm settle")
                            for l in after), after)
        self.assertNotIn("udev_pending", h.get_state())

    def test_without_the_wait_udev_wins(self):
        # the defect: the same sequence with udev's event handled after the apply's sysctl (the fake kernel's order
        # when nothing waits for it) ends with rp_filter 1 on the VRF
        h = self.h
        rc, out, err, log = h.run("root", fail=r"udevadm settle")
        self.assertEqual(rc, 0, err)  # the wait is best effort: its failure does not fail the apply
        sc = h.get_state()["ns"][""]["sysctl"]
        self.assertEqual(sc["net/ipv4/conf/e10000/rp_filter"], "1")

    def test_guard_action_restores_it(self):
        h = self.h
        h.ok(self, "root")
        st = h.get_state()
        st["ns"][""]["sysctl"]["net/ipv4/conf/e10000/rp_filter"] = "1"  # a sysctl run of the host's own
        h.set_state(st)
        rc, out, err, log = h.run("root", "guard")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip().splitlines(), ["guard=ok", "rpfilter=restored e10000"])
        self.assertEqual(h.get_state()["ns"][""]["sysctl"]["net/ipv4/conf/e10000/rp_filter"], "0")
        rc, out, err, log = h.run("root", "guard")
        self.assertEqual(out.strip(), "guard=ok")
        self.assertEqual(writes(log), [])


if __name__ == "__main__":
    unittest.main()

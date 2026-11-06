"""shc-fabric-apply, both transports (fake kernel of test_shc_fabric_apply): a VRF other than the one the configuration
names for a table the configuration will use refuses the apply before its first write (exit 4, the VRF and the table
named): on the root the egress, network and circuit VRFs' tables, on a fresh host and on a built one; in a gateway's
namespace that exists already its VNIs. A VRF of the host's own on a table the fabric does not use is left alone.
Before, on srv6 such a VRF made the apply fail part-way with the kernel's "Table is used by another VRF" and leave
state behind; on mpls the two VRFs would have shared the table. Documentation prefixes only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_netkernel as FK  # noqa: E402
from test_shc_fabric_apply import ApplyCase, blade, writes  # noqa: E402
from test_srv6_fabric_apply import GW_SRV6, ROOT_SRV6, gw_full, root_full  # noqa: E402

TRANSPORTS = (("mpls", {}, {}), ("srv6", ROOT_SRV6, GW_SRV6))
MSG = "shc-fabric-apply: a VRF that is not this instance's holds a table it will use: "


def with_vrfs(st, ns, *vrfs):
    k = FK.Kernel(st)
    for name, table in vrfs:
        k.add_link(ns, name, "vrf", table=table)["up"] = True
    return st


class ForeignVrf(ApplyCase):
    def refused(self, instance, st, msg):
        h = self.h
        h.set_state(st)
        rc, out, err, log = h.run(instance)
        self.assertEqual(rc, 4, err)
        self.assertIn(msg, err)
        self.assertEqual(writes(log), [])
        self.assertEqual(h.get_state(), st)
        return log

    def test_root_fresh_host(self):
        for t, rx, _ in TRANSPORTS:
            with self.subTest(transport=t):
                self.h.conf("root", root_full(**rx))
                for vrfs, want in (([("fv1", "110000")], "VRF fv1 holds table 110000, which this configuration gives to "
                                                         "e10000"),
                                   ([("fv1", "9000")], "VRF fv1 holds table 9000, which this configuration gives to egress"),
                                   ([("fv1", "110001")], "VRF fv1 holds table 110001, which this configuration gives to "
                                                         "e10001"),
                                   ([("fv1", "110000"), ("fv2", "110000")],
                                    "VRF fv1 holds table 110000, which this configuration gives to e10000; VRF fv2 holds "
                                    "table 110000, which this configuration gives to e10000")):
                    log = self.refused("root", with_vrfs(blade(), "", *vrfs), MSG + want)
                    self.keep("fix-l6-root-%s-%s" % (t, "-".join(v for v, _ in vrfs)), log)

    def test_root_built(self):
        """a VRF of the host's own put on a fabric table after the root was built: the next apply is refused"""
        for t, rx, _ in TRANSPORTS:
            with self.subTest(transport=t):
                h = self.h
                h.set_state(blade())
                h.conf("root", root_full(**rx))
                h.ok(self, "root")
                st = h.get_state()
                st["ns"][""]["sysctl"]["net/vrf/strict_mode"] = "0"  # (strict mode would refuse its creation)
                self.refused("root", with_vrfs(st, "", ("fv1", "110001")),
                             MSG + "VRF fv1 holds table 110001, which this configuration gives to e10001")

    def test_host_vrf_elsewhere_left_alone(self):
        for t, rx, _ in TRANSPORTS:
            with self.subTest(transport=t):
                h = self.h
                h.set_state(with_vrfs(blade(), "", ("mgmt", "10")))
                h.conf("root", root_full(**rx))
                out, log = h.ok(self, "root")
                self.assertTrue(out.strip().endswith("changed=yes"), out)
                self.assertEqual(h.get_state()["ns"][""]["links"]["mgmt"]["table"], "10")
                self.assertEqual([l for l in writes(log) if "mgmt" in l], [])

    def test_gateway(self):
        for t, rx, gx in TRANSPORTS:
            with self.subTest(transport=t):
                h = self.h
                h.set_state(blade())
                h.conf("root", root_full(**rx))
                h.ok(self, "root")
                h.conf("gw-iaas1", gw_full(**gx))
                h.ok(self, "gw-iaas1")
                built = h.get_state()
                # its own VRFs are its own: a second run passes
                out, _ = h.ok(self, "gw-iaas1")
                self.assertTrue(out.strip().endswith("changed=no"), out)
                st = h.get_state()
                self.assertEqual(st, built)
                st["ns"]["gw-iaas1"]["sysctl"]["net/vrf/strict_mode"] = "0"
                log = self.refused("gw-iaas1", with_vrfs(st, "gw-iaas1", ("x1", "51002")),
                                   MSG + "VRF x1 holds table 51002, which this configuration gives to t51002")
                self.keep("fix-l6-gw-%s" % t, log)


if __name__ == "__main__":
    import unittest
    unittest.main()

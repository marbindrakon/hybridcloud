"""shc-fabric-apply, the VRFs' IPv4 separation on both transports (fake kernel of test_shc_fabric_apply): the IPv4 rule
"lookup local" behind the VRFs' l3mdev rule (32765, added before the one at 0 goes) and an IPv4 unreachable default in
every VRF table, in the root namespace and in every gateway namespace; a second run changed=no; the rule put back at
0 is moved again; a VRF that goes takes its default (also without the IPv6 keys); root down restores the kernel's
order; an IPv4 policy rule of the host's own between 0 and 32765 refuses the apply before its first write (exit 4),
and a refused step fails it (exit 4). Documentation prefixes only."""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import CIRCUIT, GW_CONF, ROOT_CONF, ApplyCase, blade, index, writes  # noqa: E402
from test_srv6_fabric_apply import GW_SRV6, KERNEL4, ROOT_SRV6, SRV6_4, gw_full, root_full, rules4, unreach4  # noqa: E402

TRANSPORTS = (("mpls", {}, {}), ("srv6", ROOT_SRV6, GW_SRV6))
REFUSED = "shc-fabric-apply: IPv4 policy rules between 0 and 32765 would come before the local table: "


class BothTransports(ApplyCase):
    def each(self, fn):
        for name, root_extra, gw_extra in TRANSPORTS:
            with self.subTest(transport=name):
                self.h.set_state(blade())
                for f in os.listdir(self.h.etc):
                    os.unlink(os.path.join(self.h.etc, f))
                fn(name, root_extra, gw_extra)

    def test_root(self):
        def case(t, rx, gx):
            h = self.h
            h.conf("root", root_full(**rx))
            out, log = h.ok(self, "root")
            self.keep("fix-l1-root-%s" % t, log, out)
            st = h.get_state()
            self.assertEqual(rules4(st, ""), SRV6_4)
            add = index(log, r"^W \[root\] ip -4 rule add pref 32765 table local$")
            self.assertLess(add, index(log, r"^W \[root\] ip -4 rule del pref 0 table local$"))
            self.assertLess(add, index(log, r"^W \[root\] ip link add br-core "))
            self.assertEqual(unreach4(st, ""), ["110000", "110001", "9000"])
            vrf_tables = sorted(str(l["table"]) for l in st["ns"][""]["links"].values() if l["kind"] == "vrf")
            self.assertEqual(unreach4(st, ""), vrf_tables)
            out2, log2 = h.ok(self, "root")
            self.assertTrue(out2.strip().endswith("changed=no"), out2)
            self.assertEqual([l for l in writes(log2) if " -4 " in l], [])
            # counted: the rule put back at 0 by hand is moved again
            st = h.get_state()
            st["ns"][""]["rules4"].append({"pref": "0", "table": "local"})
            h.set_state(st)
            out3, _ = h.ok(self, "root")
            self.assertTrue(out3.strip().endswith("changed=yes"), out3)
            self.assertEqual(rules4(h.get_state(), ""), SRV6_4)
            # a circuit's VRF goes: its IPv4 unreachable default with it
            c = root_full(**rx)
            c["attachments"]["circuits"] = []
            h.conf("root", c)
            h.ok(self, "root")
            self.assertEqual(unreach4(h.get_state(), ""), ["110000", "9000"])
            # down: the kernel's order again (added before deleted), the VRFs' routes gone
            _, log4 = h.ok(self, "root", "down")
            self.assertEqual(rules4(h.get_state(), ""), sorted(KERNEL4 + [{"pref": "1000", "table": "[l3mdev-table]"}],
                                                               key=lambda r: int(r["pref"])))
            self.assertLess(index(log4, r"^W \[root\] ip -4 rule add pref 0 table local$"),
                            index(log4, r"^W \[root\] ip -4 rule del pref 32765 table local$"))
            self.assertEqual(unreach4(h.get_state(), ""), [])
        self.each(case)

    def test_gateway(self):
        def case(t, rx, gx):
            h = self.h
            h.conf("root", root_full(**rx))
            h.ok(self, "root")
            h.conf("gw-iaas1", gw_full(**gx))
            out, log = h.ok(self, "gw-iaas1")
            self.keep("fix-l1-gw-%s" % t, log, out)
            st = h.get_state()
            self.assertEqual(rules4(st, "gw-iaas1"), SRV6_4)
            self.assertEqual(unreach4(st, "gw-iaas1"), ["51000", "51002"])
            # all of it in the namespace: the root namespace's rules as the root left them
            self.assertEqual([l for l in log if " -4 rule " in l and not l.startswith(("W [gw-iaas1]", "R [gw-iaas1]"))
                              and l != "R [root] ip -4 rule show"], [])
            self.assertEqual([l for l in writes(log) if " -4 " in l and not l.startswith("W [gw-iaas1]")], [])
            out2, log2 = h.ok(self, "gw-iaas1")
            self.assertTrue(out2.strip().endswith("changed=no"), out2)
            self.assertEqual([l for l in writes(log2) if " -4 " in l], [])
            h.conf("gw-iaas1", gw_full(**dict(gx, vrfs=GW_CONF["vrfs"][:1])))
            _, log3 = h.ok(self, "gw-iaas1")
            self.assertEqual(unreach4(h.get_state(), "gw-iaas1"), ["51000"])
            self.assertLess(index(log3, r"^W \[gw-iaas1\] ip link del t51002$"),
                            index(log3, r"^W \[gw-iaas1\] ip -4 route del unreachable default metric 4278198272 "
                                        r"table 51002$"))
        self.each(case)

    def test_foreign_rule_refused(self):
        def case(t, rx, gx):
            h = self.h
            st = blade()
            st["ns"][""]["rules4"] = KERNEL4 + [{"pref": "100", "table": "200"}]
            h.set_state(st)
            h.conf("root", root_full(**rx))
            rc, out, err, log = h.run("root")
            self.keep("fix-l1-foreign-rule-%s" % t, log, out + " | stderr: " + err)
            self.assertEqual(rc, 4, err)
            self.assertIn(REFUSED + "100:\tfrom all lookup 200", err)
            self.assertEqual(writes(log), [])
            self.assertEqual(h.get_state(), st)
            # a gateway's namespace that exists already
            h.set_state(blade())
            h.ok(self, "root")
            h.conf("gw-iaas1", gw_full(**gx))
            h.ok(self, "gw-iaas1")
            st = h.get_state()
            st["ns"]["gw-iaas1"]["rules4"].append({"pref": "500", "table": "77"})
            h.set_state(st)
            rc, out, err, log = h.run("gw-iaas1")
            self.assertEqual(rc, 4, err)
            self.assertIn(REFUSED + "500:\tfrom all lookup 77", err)
            self.assertEqual(writes(log), [])
            self.assertEqual(h.get_state(), st)
            # the kernel's l3mdev rule is not a foreign one (a second VRF-using root run)
            self.assertIn({"pref": "1000", "table": "[l3mdev-table]"}, st["ns"][""]["rules4"])
        self.each(case)

    def test_failing_step(self):
        def case(t, rx, gx):
            h = self.h
            h.conf("root", root_full(**rx))
            rc, out, err, _ = h.run("root", fail=r"^\[root\] ip -4 rule add pref 32765 table local$")
            self.assertEqual(rc, 4, err)
            self.assertIn("shc-fabric-apply: the IPv4 local table behind the VRFs' rule failed", err)
            h.set_state(blade())
            rc, out, err, _ = h.run("root", fail=r"^\[root\] ip -4 route replace unreachable default .* table 9000$")
            self.assertEqual(rc, 4, err)
            self.assertIn("shc-fabric-apply: the IPv4 unreachable default of table 9000 failed", err)
        self.each(case)

    def test_ipv4_only_core(self):
        """mpls without the IPv6 keys: the same IPv4 separation; a removed root VRF's default goes too (its table is
        read for it, the only per-device read added to such a run)"""
        h = self.h
        c = copy.deepcopy(ROOT_CONF)
        c["attachments"]["circuits"] = [copy.deepcopy(CIRCUIT)]
        h.conf("root", c)
        h.ok(self, "root")
        h.conf("gw-iaas1", GW_CONF)
        h.ok(self, "gw-iaas1")
        st = h.get_state()
        self.assertEqual(rules4(st, ""), SRV6_4)
        self.assertEqual(rules4(st, "gw-iaas1"), SRV6_4)
        self.assertEqual(unreach4(st, ""), ["110000", "110001", "9000"])
        self.assertEqual(unreach4(st, "gw-iaas1"), ["51000", "51002"])
        c["attachments"]["circuits"] = []
        h.conf("root", c)
        _, log = h.ok(self, "root")
        index(log, r"^R \[root\] ip -j -d link show e10001$")
        self.assertEqual(unreach4(h.get_state(), ""), ["110000", "9000"])


if __name__ == "__main__":
    import unittest
    unittest.main()

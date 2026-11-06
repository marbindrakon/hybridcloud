"""shc-fabric-apply with the core transport key (transport, srv6 {block, locator}), run against the fake kernel of
test_shc_fabric_apply. transport absent or "mpls": exactly today's calls, output, kernel state and files. transport
"srv6", root and gateway, each in its own namespace: the guard table inet shc_core_guard before anything else, the
strict VRF mode (refused, and the apply failed naming the VRFs, while two VRFs share a table), the seg6 sysctls, the
rule to the block at pref 999, the encapsulation source; no MPLS step; a second run changed=no; a failing step, a
failing IPv6 core step and the root's router advertisement check fail the apply (on mpls: the warning, exit 0); down
leaves nothing in the root namespace. The netfilter additions on srv6 (zones by the VRF device in prerouting, the
IPv6 MSS clamp toward xo<n>, the circuits' platform-source drop in prerouting, the gateway's forward clamp toward
br<vni>) and, where an unprivileged network namespace allows it, `nft -c` of every srv6 rule set.
Documentation prefixes and an arbitrary block of 5f00::/16 only."""
import copy
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_egress_ipv6 as E  # noqa: E402
import test_root_circuit_ipv6 as C  # noqa: E402
from test_shc_fabric_apply import (CIRCUIT, GW6, GW_CONF, ROOT6, WG6, WG_CONF, ApplyCase, blade, index,  # noqa: E402
                                   writes)

BLOCK = "5f00:1234::/32"
ROOT_SRV6 = {"transport": "srv6", "srv6": {"block": BLOCK, "locator": "5f00:1234:101::/48"}}
GW_SRV6 = {"transport": "srv6", "srv6": {"block": BLOCK, "locator": "5f00:1234:102::/48"}}
WG_SRV6 = {"transport": "srv6", "srv6": {"block": BLOCK, "locator": "5f00:1234:106::/48"}}
SRV6_WORDS = ("seg6", "strict_mode", "rule", " sr tunsrc", "shc_core_guard")


def root_full(**over):
    """the root with IPv6 egress, an IPv4 circuit and an IPv6 circuit (the richest JSON the tests know)"""
    c = E.root_conf6()
    c["attachments"]["circuits"] = [copy.deepcopy(CIRCUIT), copy.deepcopy(dict(C.CIRCUIT2, **{
        k: v for k, v in C.CIRCUIT6.items() if k in ("address6", "link6", "sources6", "egress6")}))]
    c.update(copy.deepcopy(over))
    return c


def gw_full(**over):
    c = dict(copy.deepcopy(GW_CONF), **GW6)
    c["icmp6Announced"] = True
    c.update(copy.deepcopy(over))
    return c


def ns_writes(log, ns):
    return [l for l in writes(log) if l.startswith("W [%s] " % ns)]


def nft_check(case, text):
    err = C.nft_check(text)  # raises SkipTest where nft -c can't run here
    case.assertIsNone(err, text)


class MplsUnchanged(ApplyCase):
    """transport absent, "mpls", or "mpls" with srv6 keys present: the same calls, output, state and files."""

    def sequence(self, extra):
        h = self.h
        h.set_state(blade())
        for f in os.listdir(h.etc):
            os.unlink(os.path.join(h.etc, f))
        runs = []
        for n in range(2):
            for name, conf in (("root", root_full()), ("gw-iaas1", gw_full()),
                               ("gw-aws1", dict(copy.deepcopy(WG_CONF), **WG6))):
                h.conf(name, dict(conf, **extra))
                runs.append(h.ok(self, name))
        for name in ("gw-aws1", "gw-iaas1", "root"):
            runs.append(h.ok(self, name, "down"))
        return runs, h.get_state(), sorted(os.listdir(h.etc))

    def test_transport_mpls_issues_todays_calls(self):
        base = self.sequence({})
        for extra in ({"transport": "mpls"}, {"transport": "mpls", "srv6": {"block": BLOCK, "locator": ""}}):
            got = self.sequence(extra)
            for (o_out, o_log), (n_out, n_log) in zip(base[0], got[0]):
                self.assertEqual(o_log, n_log, extra)
                self.assertEqual(o_out, n_out, extra)
            self.assertEqual(base[1], got[1])
            self.assertEqual(base[2], got[2])
        self.keep("srv6-mpls-unchanged", [l for _, log in base[0] for l in log])
        # and none of it touches SRv6
        self.assertEqual([l for _, log in base[0] for l in log if any(w in l for w in SRV6_WORDS)], [])


class RootSrv6(ApplyCase):
    def up(self, **over):
        self.h.conf("root", root_full(**dict(ROOT_SRV6, **over)))
        return self.h.ok(self, "root")

    def test_fresh_root(self):
        h = self.h
        out, log = self.up()
        self.keep("srv6-root-run1", log, out)
        w = writes(log)
        # the guard is the first change of the run; the rest of the SRv6 settings follow it, before the core bridge
        self.assertEqual(w[0], "W [root] nft -f -")
        guard = h.get_state()["ns"][""]["nft"]["inet shc_core_guard"]
        self.assertIn('    iifname != { "br-core", "lo" } ip6 daddr %s counter drop comment ' % BLOCK, guard)
        self.assertIn("type filter hook prerouting priority raw; policy accept;", guard)
        bridge = index(log, r"^W \[root\] ip link add br-core ")
        for pat in (r"^W \[root\] sysctl -qw net\.vrf\.strict_mode=1$", r"^W \[root\] sysctl -qw net/ipv6/conf/all/seg6_enabled=1$",
                    r"^W \[root\] sysctl -qw net/ipv6/seg6_flowlabel=1$",
                    r"^W \[root\] ip -6 rule add to %s lookup main pref 999$" % re.escape(BLOCK),
                    r"^W \[root\] ip sr tunsrc set 2001:db8:4005:ffff::1$"):
            self.assertLess(0, index(log, pat))
            self.assertLess(index(log, pat), bridge, pat)
        # the core leg once it exists, before it comes up
        leg = index(log, r"^W \[root\] sysctl -qw net/ipv6/conf/br-core/seg6_enabled=1$")
        self.assertLess(bridge, leg)
        self.assertLess(leg, index(log, r"^W \[root\] ip link set br-core mtu 9100 up$"))
        sc = h.get_state()["ns"][""]["sysctl"]
        for k in ("net/vrf/strict_mode", "net/ipv6/conf/all/seg6_enabled", "net/ipv6/conf/default/seg6_enabled",
                  "net/ipv6/seg6_flowlabel", "net/ipv6/conf/br-core/seg6_enabled"):
            self.assertEqual(sc.get(k), "1", k)
        st = h.get_state()["ns"][""]
        self.assertEqual(st["rules6"], [{"pref": "999", "to": BLOCK, "table": "main"}])
        self.assertEqual(st["tunsrc"], "2001:db8:4005:ffff::1")
        # no MPLS step: no module, no MPLS sysctl written
        self.assertEqual([l for l in w if "mpls" in l], [])
        self.assertEqual({k: v for k, v in sc.items() if k.startswith("net/mpls/")}, {"net/mpls/platform_labels": "0"})
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        # a second run changes nothing; of the SRv6 state only the guard table is loaded again (as every table here)
        out2, log2 = h.ok(self, "root")
        self.keep("srv6-root-run2", log2, out2)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if any(x in l for x in ("seg6", "strict_mode", " rule ", " sr "))], [])
        self.assertEqual([l for l in writes(log2) if "mpls" in l], [])

    def tables(self, transport):
        h = self.h
        h.set_state(blade())
        h.conf("root", root_full(**transport))
        h.ok(self, "root")
        nft = h.get_state()["ns"][""]["nft"]
        return {k: nft[k] for k in ("ip shc_egress", "inet shc_egress6", "ip shc_attach", "ip6 shc_attach6")}

    def test_rule_sets(self):
        mpls, srv6 = self.tables({}), self.tables(ROOT_SRV6)

        def expect(table, *edits):
            text = mpls[table]
            for old, new in edits:
                self.assertEqual(text.count(old), 1, (table, old))
                text = text.replace(old, new)
            self.assertEqual(srv6[table], text, table)

        self.keep("srv6-root-rulesets", "\n\n".join(srv6.values()).splitlines())
        # shc_egress: the network VRF's zone in prerouting too; nothing else
        expect("ip shc_egress", ('    ct zone set iifname map { "xe10000" : 1 }',
                                 '    ct zone set iifname map { "xe10000" : 1, "e10000" : 1 }'))
        # shc_egress6: the same, and the return clamp in the egress VRF (after the clamp's base chains)
        clamp_out = ("  chain clamp_output { type filter hook output priority mangle; policy accept; "
                     "meta nfproto ipv6 jump clamp; }\n")
        expect("inet shc_egress6",
               ('    meta nfproto ipv6 ct zone set iifname map { "xe10000" : 1 }',
                '    meta nfproto ipv6 ct zone set iifname map { "xe10000" : 1, "e10000" : 1 }'),
               (clamp_out, clamp_out + "  chain clamp_return {\n    type filter hook forward priority mangle; policy accept;\n"
                '    meta nfproto ipv6 oifname "xo*" tcp flags & syn == syn tcp option maxseg size > 1360 counter '
                'tcp option maxseg size set 1360 comment "mss in, at the egress veth"\n  }\n'))
        # shc_attach: the VRFs' zones in prerouting, the platform sources dropped before routing
        zone_out = '    ct zone set oifname map { "bond1.3001" : 2, "bond1.3002" : 2, "e10001" : 2 }\n  }\n'
        expect("ip shc_attach",
               ('    ct zone set iifname map { "bond1.3001" : 2, "bond1.3002" : 2 }',
                '    ct zone set iifname map { "bond1.3001" : 2, "bond1.3002" : 2, "e10001" : 2 }'),
               (zone_out, zone_out + "  chain from_circuit {\n    type filter hook prerouting priority filter; policy accept;\n"
                '    iifname { "bond1.3001", "bond1.3002" } ip daddr { 172.18.230.0/24 } counter drop comment '
                "\"the platform's sources\"\n  }\n"))
        expect("ip6 shc_attach6", ('    ct zone set iifname map { "bond1.3002" : 2 }',
                                   '    ct zone set iifname map { "bond1.3002" : 2, "e10001" : 2 }'))
        guard = self.h.get_state()["ns"][""]["nft"]["inet shc_core_guard"]
        for text in list(srv6.values()) + [guard]:
            nft_check(self, text)

    def test_strict_mode_refused(self):
        h = self.h
        st = blade()
        import fake_netkernel as FK
        kern = FK.Kernel(st)
        kern.add_link("", "red", "vrf", table="77")["up"] = True
        kern.add_link("", "blue", "vrf", table="77")["up"] = True
        h.set_state(st)
        h.conf("root", root_full(**ROOT_SRV6))
        rc, out, err, log = h.run("root")
        self.keep("srv6-root-strict-refused", log, out + " | stderr: " + err)
        self.assertEqual(rc, 4, err)
        self.assertIn("net.vrf.strict_mode=1 refused; VRFs that share a table: table 77: red, blue", err)
        self.assertIn("transport srv6: net.vrf.strict_mode failed", err)
        # fail closed: the guard was loaded, nothing of the core came up
        self.assertIn("inet shc_core_guard", h.get_state()["ns"][""]["nft"])
        self.assertNotIn("br-core", h.get_state()["ns"][""]["links"])

    def test_failing_step_fails_the_apply(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        rc, out, err, _ = h.run("root", fail=r"ip sr tunsrc set")
        self.assertEqual(rc, 4, err)
        self.assertIn("transport srv6: the encapsulation source failed", err)
        h.set_state(blade())
        rc, out, err, _ = h.run("root", fail=r"seg6_flowlabel=1")
        self.assertEqual(rc, 4, err)
        self.assertIn("transport srv6: the SRv6 settings failed", err)
        h.set_state(blade())
        rc, out, err, _ = h.run("root", fail=r"\[root\] sysctl -qw net/ipv6/conf/br-core/seg6_enabled=1")
        self.assertEqual(rc, 4, err)

    def test_configuration_errors(self):
        h = self.h
        for over, msg in (({"loopback6": ""}, "the IPv6 loopback (loopback6) is required"),
                          ({"srv6": {"block": BLOCK, "locator": "5f00:9999:101::/48"}}, "is not inside the block"),
                          ({"srv6": {"block": "5f00:1234::1/32", "locator": "5f00:1234:101::/48"}}, "is no IPv6 prefix"),
                          ({"srv6": {}}, "srv6.block None is no IPv6 prefix")):
            h.conf("root", root_full(**dict(ROOT_SRV6, **over)))
            rc, out, err, log = h.run("root")
            self.assertEqual(rc, 4, (over, err))
            self.assertIn(msg, err)
            self.assertEqual(writes(log), [], over)
        h.conf("root", root_full(transport="bogus"))
        rc, _, err, _ = h.run("root")
        self.assertEqual((rc, err.strip()), (2, "unknown transport bogus"))

    def test_root_down(self):
        h = self.h
        self.up()
        out, log = h.ok(self, "root", "down")
        self.keep("srv6-root-down", log, out)
        st = h.get_state()["ns"][""]
        self.assertEqual(st["rules6"], [])
        self.assertEqual(st["tunsrc"], "::")
        self.assertNotIn("inet shc_core_guard", st["nft"])
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        # the guard goes last
        self.assertLess(index(log, r"^W \[root\] ip -6 rule del pref 999 "),
                        index(log, r"^W \[root\] nft delete table inet shc_core_guard$"))


class GatewaySrv6(ApplyCase):
    def setUp(self):
        super().setUp()
        self.h.conf("root", root_full(**ROOT_SRV6))
        self.h.ok(self, "root")
        self.root_after = self.h.get_state()["ns"][""]

    def test_fresh_gateways(self):
        h = self.h
        for name, conf, src in (("gw-iaas1", gw_full(**GW_SRV6), "2001:db8:4005:ffff::2"),
                                ("gw-aws1", dict(copy.deepcopy(WG_CONF), **dict(WG6, **WG_SRV6)), "2001:db8:4005:ffff::6")):
            h.conf(name, conf)
            out, log = h.ok(self, name)
            self.keep("srv6-%s-run1" % name, log, out)
            w = ns_writes(log, name)
            self.assertEqual(w[0], "W [%s] nft -f -" % name)  # the guard: the namespace's first change
            self.assertLess(index(log, r"^W \[root\] ip netns add %s$" % name), index(log, r"^W \[%s\] nft -f -$" % name))
            st = h.get_state()["ns"][name]
            self.assertIn('iifname != { "core0", "lo" } ip6 daddr %s counter drop' % BLOCK, st["nft"]["inet shc_core_guard"])
            for k in ("net/vrf/strict_mode", "net/ipv6/conf/all/seg6_enabled", "net/ipv6/conf/default/seg6_enabled",
                      "net/ipv6/seg6_flowlabel", "net/ipv6/conf/core0/seg6_enabled"):
                self.assertEqual(st["sysctl"].get(k), "1", (name, k))
            self.assertEqual(st["rules6"], [{"pref": "999", "to": BLOCK, "table": "main"}])
            self.assertEqual(st["tunsrc"], src)
            self.assertEqual([l for l in writes(log) if "mpls" in l], [])
            self.assertFalse([k for k in st["sysctl"] if k.startswith("net/mpls/")])
            # every SRv6 call in the namespace; the root namespace as after the root's run
            self.assertEqual([l for l in log if any(x in l for x in SRV6_WORDS) and not l.startswith(("W [%s]" % name, "R [%s]" % name))], [])
            mss = st["nft"]["inet shc_gw_mss"]
            self.assertIn('  chain clamp {\n    type filter hook forward priority mangle; policy accept;\n'
                          '    oifname "vx*" tcp flags syn tcp option maxseg size set rt mtu\n'
                          '    oifname "br*" tcp flags syn tcp option maxseg size set rt mtu\n  }', mss)
            nft_check(self, mss.replace("add table inet shc_gw_mss\n", "table inet shc_gw_mss\n"))
            nft_check(self, st["nft"]["inet shc_core_guard"])
            out2, log2 = h.ok(self, name)
            self.assertTrue(out2.strip().endswith("changed=no"), out2)
            self.assertEqual([l for l in writes(log2) if any(x in l for x in ("seg6", "strict_mode", " rule ", " sr "))], [])

    def test_root_namespace_writes_as_on_mpls(self):
        """the gateway's calls in the root namespace (veth, VLAN move) are the same as on mpls; the one more
        `modprobe vrf` loads the module for the strict mode (modules are the host's, not a namespace's)"""
        h = self.h
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        _, log_s = h.ok(self, "gw-iaas1")
        h.set_state(blade())
        h.conf("root", root_full())
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full())
        _, log_m = h.ok(self, "gw-iaas1")
        self.assertEqual(ns_writes(log_s, "root")[:2], ["W [root] ip netns add gw-iaas1", "W [root] modprobe vrf"])
        self.assertEqual(ns_writes(log_s, "root")[:1] + ns_writes(log_s, "root")[2:], ns_writes(log_m, "root"))

    def test_down_leaves_the_root_namespace_as_it_was(self):
        h = self.h
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        h.ok(self, "gw-iaas1")
        out, log = h.ok(self, "gw-iaas1", "down")
        self.assertNotIn("gw-iaas1", h.get_state()["ns"])
        self.assertEqual(h.get_state()["ns"][""], self.root_after)
        self.assertTrue(out.strip().endswith("changed=yes"), out)


class IPv6CoreRequired(ApplyCase):
    """transport srv6: both families ride the IPv6 core, so an IPv6 step that fails, and the root's router
    advertisement check, fail the apply; on mpls both keep the warning line and exit 0"""

    RA = {"type": "unicast", "dst": "default", "via": "fe80::1", "dev": "bond0.10", "proto": "ra", "metric": "1024",
          "table": "main"}

    def ra_blade(self):
        st = blade()
        st["ns"][""]["routes6"].append(dict(self.RA))
        return st

    def test_ra_precheck_fails_the_apply(self):
        h = self.h
        h.set_state(self.ra_blade())
        h.conf("root", root_full(**ROOT_SRV6))
        rc, out, err, log = h.run("root")
        self.keep("srv6-root-ra-precheck", log, out + " | stderr: " + err)
        self.assertEqual(rc, 4, err)
        self.assertIn("transport srv6: a host interface relies on router advertisements", err)
        self.assertIn("warning: interface bond0.10 relies on router advertisements", out)
        self.assertEqual(writes(log), [])  # nothing changed: the check comes first
        # mpls: as before, the warning and exit 0
        h.set_state(self.ra_blade())
        h.conf("root", root_full())
        rc, out, err, _ = h.run("root")
        self.assertEqual(rc, 0, err)
        self.assertIn("IPv6 core not applied", out)

    def test_root_ipv6_step_fails_the_apply(self):
        h = self.h
        refuse = r"^\[root\] sysctl -qw net/ipv6/conf/br-core/accept_ra=0$"
        h.conf("root", root_full(**ROOT_SRV6))
        rc, out, err, log = h.run("root", fail=refuse)
        self.keep("srv6-root-v6-step-refused", log, out + " | stderr: " + err)
        self.assertEqual(rc, 4, err)
        self.assertIn("transport srv6: IPv6 settings of the core bridge br-core failed (the IPv6 core carries both "
                      "families)", err)
        self.assertNotIn("IPv6 core not applied", out)
        h.set_state(blade())
        h.conf("root", root_full())
        rc, out, err, _ = h.run("root", fail=refuse)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: IPv6 settings of the core bridge br-core failed; IPv6 core not applied", out)

    def test_gateway_ipv6_step_fails_the_apply(self):
        h = self.h
        refuse = r"^\[gw-iaas1\] ip addr add \S+ dev t51000 nodad$"
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        rc, out, err, log = h.run("gw-iaas1", fail=refuse)
        self.keep("srv6-gw-v6-step-refused", log, out + " | stderr: " + err)
        self.assertEqual(rc, 4, err)
        self.assertIn("transport srv6: IPv6 in VRF t51000 failed", err)
        h.set_state(blade())
        h.conf("root", root_full())
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full())
        rc, out, err, _ = h.run("gw-iaas1", fail=refuse)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: IPv6 in VRF t51000 failed; IPv6 core not applied", out)


if __name__ == "__main__":
    unittest.main()

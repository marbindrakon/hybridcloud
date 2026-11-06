"""shc-fabric-apply with the core transport key (transport, srv6 {block, locator}), run against the fake kernel of
test_shc_fabric_apply. transport absent, "mpls" and "mpls" with srv6 keys: the same calls, output, kernel state and
files, none of them an SRv6 one (against the script before the SRv6 transport: test_fix_base_script). transport
"srv6", root and gateway, each in its own namespace: the guard table inet shc_core_guard before anything else, the
strict VRF mode (refused, and the apply failed naming the VRFs, while two VRFs share a table), the seg6 sysctls, the
rule to the block at pref 999, the encapsulation source; no MPLS step; a second run changed=no; a failing step, a
failing IPv6 core step and the root's router advertisement check fail the apply (on mpls: the warning, exit 0); the
IPv4 local table behind the VRFs' rule with an unreachable default in every VRF; down leaves nothing in the root
namespace. The netfilter additions on srv6 (zones by the VRF device in prerouting, the circuits' platform-source drop
in prerouting, the gateway's clamp toward br<vni> on forward instead of output) and, where an unprivileged network
namespace allows it, `nft -c` of every srv6 rule set.
Documentation prefixes and an arbitrary block of 5f00::/16 only."""
import copy
import os
import re
import subprocess
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
SRV6_WORDS = ("seg6", "strict_mode", " -6 rule", " sr tunsrc", "shc_core_guard")


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
        for pat in (r"^W \[root\] sysctl -qw net\.vrf\.strict_mode=1$", r"^W \[root\] sysctl -qw net/ipv6/conf/all/seg6_enabled=0$",
                    r"^W \[root\] sysctl -qw net/ipv6/conf/default/seg6_enabled=0$",
                    r"^W \[root\] ip -6 route replace unreachable %s metric 4278198272 table main$" % re.escape(BLOCK),
                    r"^W \[root\] sysctl -qw net/ipv6/seg6_flowlabel=1$",
                    r"^W \[root\] ip -6 rule add to %s lookup main pref 999$" % re.escape(BLOCK),
                    r"^W \[root\] ip sr tunsrc set 2001:db8:4005:ffff::1$"):
            self.assertLess(0, index(log, pat))
            self.assertLess(index(log, pat), bridge, pat)
        # the core leg once it exists, before it comes up (read: it inherited 0 from default)
        leg = index(log, r"^R \[root\] sysctl -n net/ipv6/conf/br-core/seg6_enabled$")
        self.assertLess(bridge, leg)
        self.assertLess(leg, index(log, r"^W \[root\] ip link set br-core mtu 9100 up$"))
        sc = h.get_state()["ns"][""]["sysctl"]
        for k in ("net/vrf/strict_mode", "net/ipv6/seg6_flowlabel"):
            self.assertEqual(sc.get(k), "1", k)
        # no routing header accepted: all, default, the core leg and every device the run created
        self.assertEqual(sorted(k for k, v in sc.items() if k.endswith("/seg6_enabled") and v != "0"), [])
        # (lo predates the run: the fake holds no value for it, the kernel's own is 0)
        for d in ("all", "default", "br-core", "egress", "egress0", "e10000", "e10001", "xe10000", "xo10000",
                  "bond1.3001", "bond1.3002"):
            self.assertEqual(sc.get("net/ipv6/conf/%s/seg6_enabled" % d), "0", d)
        # the block unreachable in main at the highest metric
        self.assertIn({"type": "unreachable", "dst": BLOCK, "metric": "4278198272", "table": "main"},
                      h.get_state()["ns"][""]["routes6"])
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
        # shc_egress6: the same (the return clamp in the egress VRF is on both transports: test_fix_mss_clamp)
        expect("inet shc_egress6",
               ('    meta nfproto ipv6 ct zone set iifname map { "xe10000" : 1 }',
                '    meta nfproto ipv6 ct zone set iifname map { "xe10000" : 1, "e10000" : 1 }'))
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
        self.assertIn("transport srv6: net.vrf.strict_mode=1 refused; VRFs that share a table: table 77: red, blue", err)
        # refused before the run's first write: the host as it was found
        self.assertEqual(writes(log), [])
        self.assertEqual(h.get_state(), st)

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
        h.ok(self, "root")
        st = h.get_state()
        st["ns"][""]["sysctl"]["net/ipv6/conf/br-core/seg6_enabled"] = "1"
        h.set_state(st)
        rc, out, err, _ = h.run("root", fail=r"\[root\] sysctl -qw net/ipv6/conf/br-core/seg6_enabled=0")
        self.assertEqual(rc, 4, err)
        self.assertIn("transport srv6: seg6_enabled on the core leg br-core failed", err)

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
        self.assertEqual([r for r in st["routes6"] if r["table"] == "main" and r["type"] == "unreachable"], [])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.guard")))
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
            for k in ("net/vrf/strict_mode", "net/ipv6/seg6_flowlabel"):
                self.assertEqual(st["sysctl"].get(k), "1", (name, k))
            for d in ["all", "default"] + sorted(set(st["links"]) - {"lo"}):  # lo: as in test_fresh_root
                self.assertEqual(st["sysctl"].get("net/ipv6/conf/%s/seg6_enabled" % d), "0", (name, d))
            self.assertIn({"type": "unreachable", "dst": BLOCK, "metric": "4278198272", "table": "main"}, st["routes6"])
            self.assertEqual(st["rules6"], [{"pref": "999", "to": BLOCK, "table": "main"}])
            self.assertEqual(st["tunsrc"], src)
            self.assertEqual([l for l in writes(log) if "mpls" in l], [])
            self.assertFalse([k for k in st["sysctl"] if k.startswith("net/mpls/")])
            # every SRv6 call in the namespace; the root namespace as after the root's run
            self.assertEqual([l for l in log if any(x in l for x in SRV6_WORDS) and not l.startswith(("W [%s]" % name, "R [%s]" % name))], [])
            mss = st["nft"]["inet shc_gw_mss"]
            self.assertIn('  chain clamp {\n    type filter hook forward priority mangle; policy accept;\n'
                          '    oifname "br*" tcp flags syn tcp option maxseg size set rt mtu\n  }\n}', mss)
            self.assertNotIn("clamp_core", mss)
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


def seg6_open(st, ns, devs):
    """an earlier configuration that accepted routing headers: all, default and the devices at 1"""
    sc = st["ns"][ns]["sysctl"]
    for d in ["all", "default"] + list(devs):
        sc["net/ipv6/conf/%s/seg6_enabled" % d] = "1"
    return st


class RoutingHeadersOff(ApplyCase):
    """transport srv6 accepts no routing header: a re-apply closes what an earlier configuration opened, and every
    device created afterwards inherits 0; the management interface is never touched"""

    def test_root_reapply_closes(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        st = h.get_state()
        devs = [d for d in st["ns"][""]["links"] if d != "bond0.10"]
        h.set_state(seg6_open(st, "", devs + ["bond0.10"]))
        out, log = h.ok(self, "root")
        self.keep("srv6-root-seg6-closed", log, out)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        sc = h.get_state()["ns"][""]["sysctl"]
        for d in ["all", "default", "br-core", "egress0", "e10000", "e10001", "xe10000", "xo10000", "bond1.3001", "lo"]:
            self.assertEqual(sc["net/ipv6/conf/%s/seg6_enabled" % d], "0", d)
        self.assertEqual(sc["net/ipv6/conf/bond0.10/seg6_enabled"], "1")  # the management interface: untouched
        self.assertEqual([l for l in log if "bond0" in l], [])
        out2, _ = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)

    def test_new_devices_inherit_0(self):
        """default was 1 (an earlier run): it is set to 0 before any device of the run exists"""
        h = self.h
        h.set_state(seg6_open(blade(), "", []))
        h.conf("root", root_full(**ROOT_SRV6))
        _, log = h.ok(self, "root")
        default0 = index(log, r"^W \[root\] sysctl -qw net/ipv6/conf/default/seg6_enabled=0$")
        self.assertLess(default0, index(log, r"^W \[root\] ip link add br-core "))
        self.assertLess(default0, index(log, r"^W \[root\] ip link add egress "))

    def test_gateway_reapply_closes(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        h.ok(self, "gw-iaas1")
        st = h.get_state()
        h.set_state(seg6_open(st, "gw-iaas1", st["ns"]["gw-iaas1"]["links"]))
        out, _ = h.ok(self, "gw-iaas1")
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        st = h.get_state()["ns"]["gw-iaas1"]
        for d in ["all", "default"] + sorted(st["links"]):
            self.assertEqual(st["sysctl"]["net/ipv6/conf/%s/seg6_enabled" % d], "0", d)


class ReversePathRoot(ApplyCase):
    """transport srv6: the root's network VRFs filter no reverse paths (0: strict and loose both drop decapsulated
    IPv4, measured); the host-wide all must be 0 (the larger of all and the device counts), else the root's apply
    fails before it changes anything; the egress VRF and the circuits' devices stay as they are; mpls: as created"""

    def el9_blade(self, all_="0"):
        st = blade()
        st["ns"][""]["sysctl"].update({"net/ipv4/conf/all/rp_filter": all_, "net/ipv4/conf/default/rp_filter": "1"})
        return st

    def test_network_vrfs_off(self):
        h = self.h
        h.set_state(self.el9_blade())
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        sc = h.get_state()["ns"][""]["sysctl"]
        for vrf in ("e10000", "e10001"):
            self.assertEqual(sc["net/ipv4/conf/%s/rp_filter" % vrf], "0", vrf)
        for dev in ("bond1.3001", "bond1.3002"):
            self.assertEqual(sc["net/ipv4/conf/%s/rp_filter" % dev], "1", dev)
        self.assertEqual(sc["net/ipv4/conf/egress/rp_filter"], "1")  # left as it is (input of nothing decapsulated)
        self.assertEqual(sc["net/ipv4/conf/all/rp_filter"], "0")
        out2, log2 = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if "rp_filter" in l and "bond1." not in l], [])
        # mpls: as created
        h.set_state(self.el9_blade())
        h.conf("root", root_full())
        _, log = h.ok(self, "root")
        self.assertEqual(h.get_state()["ns"][""]["sysctl"]["net/ipv4/conf/e10000/rp_filter"], "1")
        self.assertEqual([l for l in log if "rp_filter=0" in l], [])

    def test_host_all_not_0_fails(self):
        h = self.h
        for all_ in ("1", "2"):
            h.set_state(self.el9_blade(all_))
            h.conf("root", root_full(**ROOT_SRV6))
            rc, out, err, log = h.run("root")
            self.keep("srv6-root-rp-filter-all-%s" % all_, log, out + " | stderr: " + err)
            self.assertEqual(rc, 4, err)
            self.assertIn("transport srv6: net.ipv4.conf.all.rp_filter is %s; the root's network VRFs need it 0" % all_,
                          err)
            self.assertEqual(writes(log), [])
            self.assertEqual(h.get_state(), self.el9_blade(all_))  # the host as it was found; all never written
        # mpls does not mind
        h.set_state(self.el9_blade("1"))
        h.conf("root", root_full())
        h.ok(self, "root")
        # a gateway sets its own namespace's all to 0, whatever the host's
        h.set_state(self.el9_blade())
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        h.ok(self, "gw-iaas1")
        self.assertEqual(h.get_state()["ns"]["gw-iaas1"]["sysctl"]["net/ipv4/conf/all/rp_filter"], "0")


class UnreachableBlock(ApplyCase):
    def test_kept_and_counted(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        route = {"type": "unreachable", "dst": BLOCK, "metric": "4278198272", "table": "main"}
        st = h.get_state()
        st["ns"][""]["routes6"].remove(route)
        st["ns"][""]["routes6"].append(dict(route, dst="5f00:9999::/32"))  # an earlier block's
        h.set_state(st)
        out, log = h.ok(self, "root")
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        main = [r for r in h.get_state()["ns"][""]["routes6"] if r["table"] == "main"]
        self.assertEqual(main, [route])
        index(log, r"^W \[root\] ip -6 route del unreachable 5f00:9999::/32 metric 4278198272 table main$")
        out2, log2 = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if " route " in l], [])


class GuardAction(ApplyCase):
    """shc-fabric-apply <instance> guard: guard=ok while the table is as the last load left it, else it is loaded again
    (guard=restored); guard=none on mpls or without the gateway's namespace"""

    def guard(self, name):
        rc, out, err, log = self.h.run(name, "guard")
        self.assertEqual(rc, 0, err)
        return out.strip(), log

    def test_root(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        out, log = self.guard("root")
        self.assertEqual(out, "guard=ok")
        self.assertEqual(writes(log), [])
        want = h.get_state()["ns"][""]["nft"]["inet shc_core_guard"]
        for damage in ("gone", "altered"):
            st = h.get_state()
            if damage == "gone":
                del st["ns"][""]["nft"]["inet shc_core_guard"]
            else:
                st["ns"][""]["nft"]["inet shc_core_guard"] = want.replace("counter drop", "counter accept")
            h.set_state(st)
            out, log = self.guard("root")
            self.keep("srv6-guard-root-" + damage, log, out)
            self.assertEqual(out, "guard=restored", damage)
            self.assertEqual(writes(log), ["W [root] nft -f -"])
            self.assertEqual(h.get_state()["ns"][""]["nft"]["inet shc_core_guard"], want)
            self.assertEqual(self.guard("root")[0], "guard=ok")
        # a missing listing (an apply older than the action) counts as altered: loaded again
        os.unlink(os.path.join(h.etc, "root.guard"))
        self.assertEqual(self.guard("root")[0], "guard=restored")
        self.assertEqual(self.guard("root")[0], "guard=ok")

    def test_gateway_and_none(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        self.assertEqual(self.guard("gw-iaas1")[0], "guard=none")  # no namespace yet
        h.ok(self, "gw-iaas1")
        self.assertEqual(self.guard("gw-iaas1")[0], "guard=ok")
        st = h.get_state()
        del st["ns"]["gw-iaas1"]["nft"]["inet shc_core_guard"]
        h.set_state(st)
        out, log = self.guard("gw-iaas1")
        self.assertEqual(out, "guard=restored")
        self.assertEqual(writes(log), ["W [gw-iaas1] nft -f -"])
        h.ok(self, "gw-iaas1", "down")
        self.assertFalse(os.path.exists(os.path.join(h.etc, "gw-iaas1.guard")))
        self.assertEqual(self.guard("gw-iaas1")[0], "guard=none")
        # mpls: nothing at all
        h.conf("root", root_full())
        out, log = self.guard("root")
        self.assertEqual((out, log), ("guard=none", []))

    def test_unknown_action(self):
        self.h.conf("root", root_full())
        rc, _, err, log = self.h.run("root", "sideways")
        self.assertEqual((rc, err.strip(), writes(log)), (2, "unknown action sideways", []))


KERNEL4 = [{"pref": "0", "table": "local"}, {"pref": "32766", "table": "main"}, {"pref": "32767", "table": "default"}]
SRV6_4 = [{"pref": "1000", "table": "[l3mdev-table]"}, {"pref": "32765", "table": "local"},
          {"pref": "32766", "table": "main"}, {"pref": "32767", "table": "default"}]


def rules4(st, ns):
    return sorted(st["ns"][ns].get("rules4", KERNEL4), key=lambda r: int(r["pref"]))


def unreach4(st, ns):
    return sorted(r["table"] for r in st["ns"][ns].get("routes4", [])
                  if r["type"] == "unreachable" and r["dst"] == "default" and r["metric"] == "4278198272")


class LocalRuleOrder(ApplyCase):
    """transport srv6, IPv4: the local table behind the VRFs' l3mdev rule (32765, added before the rule at 0 goes), so a
    packet in a VRF never reaches the default VRF's local addresses, and every VRF's table ends in an unreachable
    default; root down restores the kernel's order (the same on mpls: test_fix_vrf_ipv4)"""

    def test_root(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        out, log = h.ok(self, "root")
        self.keep("srv6-root-local-rule", log, out)
        st = h.get_state()
        self.assertEqual(rules4(st, ""), SRV6_4)
        self.assertLess(index(log, r"^W \[root\] ip -4 rule add pref 32765 table local$"),
                        index(log, r"^W \[root\] ip -4 rule del pref 0 table local$"))
        self.assertLess(index(log, r"^W \[root\] ip -4 rule del pref 0 table local$"), index(log, r"^W \[root\] ip link add br-core "))
        self.assertEqual(unreach4(st, ""), ["110000", "110001", "9000"])
        out2, log2 = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if " -4 " in l], [])
        # counted: the rule put back by hand is moved again
        st = h.get_state()
        st["ns"][""]["rules4"].append({"pref": "0", "table": "local"})
        h.set_state(st)
        out3, _ = h.ok(self, "root")
        self.assertTrue(out3.strip().endswith("changed=yes"), out3)
        self.assertEqual(rules4(h.get_state(), ""), SRV6_4)
        # a circuit's VRF removed: its IPv4 unreachable default with it
        c = root_full(**ROOT_SRV6)
        c["attachments"]["circuits"] = []
        h.conf("root", c)
        h.ok(self, "root")
        self.assertEqual(unreach4(h.get_state(), ""), ["110000", "9000"])
        # down: the kernel's order again (added before deleted), the VRFs' routes gone
        out4, log4 = h.ok(self, "root", "down")
        self.assertEqual(rules4(h.get_state(), ""), sorted(KERNEL4 + [{"pref": "1000", "table": "[l3mdev-table]"}],
                                                          key=lambda r: int(r["pref"])))
        self.assertLess(index(log4, r"^W \[root\] ip -4 rule add pref 0 table local$"),
                        index(log4, r"^W \[root\] ip -4 rule del pref 32765 table local$"))
        self.assertEqual(unreach4(h.get_state(), ""), [])

    def test_gateway(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        out, log = h.ok(self, "gw-iaas1")
        st = h.get_state()
        self.assertEqual(rules4(st, "gw-iaas1"), SRV6_4)
        self.assertEqual(unreach4(st, "gw-iaas1"), ["51000", "51002"])
        self.assertEqual([l for l in log if " -4 rule " in l and not l.startswith(("W [gw-iaas1]", "R [gw-iaas1]"))], [])
        out2, log2 = h.ok(self, "gw-iaas1")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if " -4 " in l], [])
        h.conf("gw-iaas1", gw_full(**dict(GW_SRV6, vrfs=GW_CONF["vrfs"][:1])))
        h.ok(self, "gw-iaas1")
        self.assertEqual(unreach4(h.get_state(), "gw-iaas1"), ["51000"])

    def test_foreign_rule_refused(self):
        h = self.h
        st = blade()
        st["ns"][""]["rules4"] = KERNEL4 + [{"pref": "100", "table": "200"}]
        h.set_state(st)
        h.conf("root", root_full(**ROOT_SRV6))
        rc, out, err, log = h.run("root")
        self.keep("srv6-root-foreign-rule", log, out + " | stderr: " + err)
        self.assertEqual(rc, 4, err)
        self.assertIn("shc-fabric-apply: IPv4 policy rules between 0 and 32765 would come before the local table: "
                      "100:\tfrom all lookup 200", err)
        # refused before the run's first write: the host as it was found
        self.assertEqual(writes(log), [])
        self.assertEqual(h.get_state(), st)


class CheckThenAct(unittest.TestCase):
    """A check like "ip addr show dev X | grep -q A || ip addr add A dev X" under pipefail reads "absent" when grep exits
    at the first match while ip is still writing (ip dies of SIGPIPE). The script's helper shows reads the output to
    its end first. A fake ip that prints the matching line, waits and prints more: the old form adds an address that
    is there, the script's addr_ensure and shows never do."""

    FAKE_IP = """#!/bin/bash
if [ "$1" = addr ] && [ "$2" = add ]; then echo "add $*" >> "$ADDS"; exit 2; fi
printf '1: lo: <LOOPBACK,UP> mtu 65536\\n    inet 10.255.0.2/32 scope global lo\\n'
sleep 0.3
printf '    inet6 ::1/128 scope host\\n'
"""

    def setUp(self):
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="shc-pipe-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "bin"))
        with open(os.path.join(self.tmp, "bin", "ip"), "w") as f:
            f.write(self.FAKE_IP)
        os.chmod(os.path.join(self.tmp, "bin", "ip"), 0o755)
        self.adds = os.path.join(self.tmp, "adds")
        with open(os.path.join(C.ROOT, C.SCRIPT_REL)) as f:
            text = f.read()
        self.funcs = "\n".join(re.search(r"^%s\(\) \{.*?^\}\n|^%s\(\) \{.*\}\n" % (n, n), text, re.S | re.M).group(0)
                               for n in ("shows", "addr_ensure"))

    def run_bash(self, body):
        path = os.path.join(self.tmp, "t.sh")
        with open(path, "w") as f:
            f.write("set -euo pipefail\nNSX=''\n" + self.funcs + "\n" + body + "\necho done\n")
        env = dict(os.environ, PATH=os.path.join(self.tmp, "bin") + os.pathsep + os.environ["PATH"], ADDS=self.adds)
        p = subprocess.run(["bash", path], env=env, capture_output=True, text=True, timeout=30)
        adds = open(self.adds).read() if os.path.exists(self.adds) else ""
        if os.path.exists(self.adds):
            os.unlink(self.adds)
        return p, adds

    def test_old_form_misreads(self):
        p, adds = self.run_bash('ip addr show dev lo | grep -q " 10.255.0.2/32 " || ip addr add 10.255.0.2/32 dev lo')
        self.assertNotEqual(p.returncode, 0)  # the add was attempted and refused: errexit
        self.assertIn("add addr add 10.255.0.2/32 dev lo", adds)

    def test_script_helpers(self):
        p, adds = self.run_bash('addr_ensure lo 10.255.0.2/32\nshows -q " 10.255.0.2/32 " ip addr show dev lo\n'
                                '! shows -q " 10.255.0.9/32 " ip addr show dev lo')
        self.assertEqual((p.returncode, adds), (0, ""), p.stderr)
        self.assertTrue(p.stdout.strip().endswith("done"))
        # absent: added
        p, adds = self.run_bash("addr_ensure lo 10.255.0.9/32 || true")
        self.assertIn("add addr add 10.255.0.9/32 dev lo", adds)

    def test_no_early_exit_pipeline_left(self):
        with open(os.path.join(C.ROOT, C.SCRIPT_REL)) as f:
            code = [l for l in f.read().splitlines() if not l.lstrip().startswith("#")]
        bad = [l for l in code if re.search(r"(?<!\|)\|(?!\|)\s*(grep\s+-[A-Za-z]*[qm]|head\b|sed\s+[^|]*q\b)", l)
               or re.search(r'echo "\$[A-Za-z_]+" \| grep', l)]
        self.assertEqual(bad, [])


class RefusedBeforeAnyWrite(ApplyCase):
    """transport srv6, a gateway whose namespace exists already: an IPv4 policy rule of its own between 0 and 32765,
    or two VRFs on one table while strict mode is off, refuse the apply before its first write (exit 4, the state and
    the call log as found)"""

    def gateway_up(self):
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        h.ok(self, "gw-iaas1")

    def refused(self, st, msg):
        h = self.h
        h.set_state(st)
        rc, out, err, log = h.run("gw-iaas1")
        self.assertEqual(rc, 4, err)
        self.assertIn(msg, err)
        self.assertEqual(writes(log), [])
        self.assertEqual(h.get_state(), st)

    def test_foreign_rule_in_the_namespace(self):
        self.gateway_up()
        st = self.h.get_state()
        st["ns"]["gw-iaas1"]["rules4"].append({"pref": "500", "table": "77"})
        self.refused(st, "shc-fabric-apply: IPv4 policy rules between 0 and 32765 would come before the local table: "
                         "500:\tfrom all lookup 77")

    def test_vrfs_on_one_table_in_the_namespace(self):
        self.gateway_up()
        st = self.h.get_state()
        import fake_netkernel as FK
        sc = st["ns"]["gw-iaas1"]["sysctl"]
        sc["net/vrf/strict_mode"] = "0"
        # (a VRF on one of the gateway's own tables is refused before this: test_fix_foreign_vrf)
        FK.Kernel(st).add_link("gw-iaas1", "x1", "vrf", table="77")
        FK.Kernel(st).add_link("gw-iaas1", "x2", "vrf", table="77")
        self.refused(st, "transport srv6: net.vrf.strict_mode=1 refused; VRFs that share a table: table 77: x1, x2")

    def test_fresh_namespace_not_read_in_the_root(self):
        """no namespace yet: nothing to check there, and nothing of the gateway's checks runs in the root namespace"""
        h = self.h
        h.conf("root", root_full(**ROOT_SRV6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw_full(**GW_SRV6))
        _, log = h.ok(self, "gw-iaas1")
        first = index(log, r"^W \[root\] ip netns add gw-iaas1$")
        self.assertEqual([l for l in log[:first] if " -4 rule " in l or "strict_mode" in l or "rp_filter" in l], [])


if __name__ == "__main__":
    unittest.main()

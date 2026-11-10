"""The fabric orphan check knows the kernel and host state of the core transport and of the VRFs' IPv4 separation.

fabric_orphan_check nodes.yml reads each border gateway node of a fabric and reports what no CR accounts for. What
the apply creates today is the fabric's own, per the fabric's core transport (realization core.transport), and is not
reported: on both transports the IPv4 rule `lookup local` at 32765 and the IPv4 unreachable default (metric
4278198272) of every VRF table; on srv6 the SID block's unreachable route in each namespace's main table, the IPv6
rule `999: to <block> lookup main`, the nft table `inet shc_core_guard`, the units `shc-core-guard@<instance>` of the
instances on the node and their guard files `/etc/shc-fabric/<instance>.guard`. Reported (coreOrphans): an IPv4
unreachable default in a table no VRF uses, an IPv4 `lookup local` rule anywhere else (0: a packet from a VRF reaches
the default VRF's addresses), the srv6 pieces on an mpls fabric or for another block, a guard unit or file of an
instance that is not on the node, and on an srv6 fabric MPLS routes and the MPLS module file.

Real listings (tests/fixtures/fix-orphan-core/, from the replica of one border gateway node at the integrated
commit 2bd83086, p-srv6/replica/out/p8-*-1/evidence/): srv6-root-ip6-unreachable.txt and
srv6-gw-site-a-ip6-unreachable.txt are the unreachable lines of `ip -6 route show table all` of the root and of
gw-site-a (dfix/), srv6-root-ip6-rule.txt the root's `ip -6 rule show` (dfix/), srv6-ip4-rule.txt a namespace's
`ip -4 rule show` on srv6 (steady/F.txt) and mpls-ip4-rule.txt one on mpls before the rule moved on both transports
(steady/L.txt). The code reads iproute2's JSON (`ip -j`); the tests turn the text into the fields it reads, in the
shapes iproute2 prints (checked with iproute2 in a throwaway namespace: `dst` and `dstlen` of a rule, `l3mdev`, no
`table` key for the main table). Hand-built: the nft, systemctl and find answers."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_fabric_orphan_nodes as on  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "fix-orphan-core")
BLOCK = "5f00:b046::/32"
FIND = ("find /etc/shc-fabric /etc/systemd/system /etc/systemd/system/timers.target.wants /etc/modules-load.d "
        "-maxdepth 1 ( -name *.guard -o -name shc-core-guard@* -o -name shc-fabric.conf )")
UNITS = "systemctl list-units --all --plain --no-legend shc-core-guard@*"


def text(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def routes_json(listing):
    """`ip -j ... route show table all type unreachable` from the text form."""
    out = []
    for line in listing.splitlines():
        w = line.split()
        if not w or w[0] != "unreachable":
            continue
        r = {"type": "unreachable", "dst": w[1], "flags": []}
        if "table" in w:
            r["table"] = w[w.index("table") + 1]
        if "metric" in w:
            r["metric"] = int(w[w.index("metric") + 1])
        out.append(r)
    return json.dumps(out)


def rules_json(listing):
    """`ip -j rule show` from the text form."""
    out = []
    for line in listing.splitlines():
        w = line.split()
        if not w:
            continue
        r = {"priority": int(w[0].rstrip(":")), "src": "all"}
        if "to" in w:
            d = w[w.index("to") + 1]
            r["dst"], r["dstlen"] = d.split("/")[0], int(d.split("/")[1])
        if w[-1] == "[l3mdev-table]":
            r["l3mdev"] = None
        else:
            r["table"] = w[-1]
        out.append(r)
    return json.dumps(out)


def vrf_links(*pairs):
    return json.dumps([on.vrf(n, t) for n, t in pairs])


def node(transport, root_v6=None, gw_v6=None, rule6=None, rule4=None, guard=False, tunsrc="::", mpls_routes="",
         files="", units="", gw="gw-site-a"):
    """The answers for a node with the root and one gateway (VRFs as in the replica: egress 9000 and e10000 110000 in
    the root, t51000 and t51003 in the gateway)."""
    rule4 = rule4 if rule4 is not None else text("srv6-ip4-rule.txt")
    a = {"ip netns list": "%s (id: 0)\n" % gw,
         "ip -j -d link show type vlan": "[]", "vtysh -c show bfd peers json": "[]", "nft list table ip6 shc_attach6": "", "nft list table ip shc_root_in": "",
         "nft list table ip6 shc_root_in6": "",
         FIND: files, UNITS: units}
    per = {"": (vrf_links(("egress", 9000), ("e10000", 110000)),
                root_v6 if root_v6 is not None else text("srv6-root-ip6-unreachable.txt"),
                rule6 if rule6 is not None else text("srv6-root-ip6-rule.txt")),
           "ip netns exec %s " % gw: (vrf_links(("t51000", 51000), ("t51003", 51003)),
                                      gw_v6 if gw_v6 is not None else text("srv6-gw-site-a-ip6-unreachable.txt"),
                                      rule6 if rule6 is not None else text("srv6-root-ip6-rule.txt"))}
    for x, (links, v6, r6) in per.items():
        a[x + "ip -j -d link show type vrf"] = links
        a[x + "ip -j -6 route show table all type unreachable"] = routes_json(v6)
        a[x + "ip -j -6 addr show"] = "[]"
        a[x + "ip -j -4 route show table all type unreachable"] = json.dumps(
            [{"type": "unreachable", "dst": "default", "table": t, "metric": 4278198272, "flags": []}
             for t in (("9000", "110000") if x == "" else ("51000", "51003"))])
        a[x + "ip -j -6 rule show"] = rules_json(r6)
        a[x + "ip -j -4 rule show"] = rules_json(rule4)
        a[x + "nft list tables"] = "table inet shc_core_guard\ntable inet shc_gw_mss\n" if guard else "table ip shc_egress\n"
        a[x + "ip sr tunsrc show"] = "tunsrc addr %s\n" % tunsrc
        a[x + "ip -f mpls route show"] = mpls_routes
    return a


def run(answers, transport, block=BLOCK):
    """main() of the node read on a node with these answers, for this fabric transport and block."""
    ns = {"__name__": "orphan_core_under_test"}
    exec(compile(on.code(dict(on.VARS)), "<node read>", "exec"), ns)
    return ns["main"](on.Fake(answers), [], 1, 0, "", transport=transport, block=block)


SRV6_FILES = ("/etc/shc-fabric/root.guard\n/etc/shc-fabric/gw-site-a.guard\n/etc/systemd/system/shc-core-guard@.service\n"
              "/etc/systemd/system/shc-core-guard@.timer\n/etc/systemd/system/timers.target.wants/shc-core-guard@root.timer\n"
              "/etc/systemd/system/timers.target.wants/shc-core-guard@gw-site-a.timer\n")
SRV6_UNITS = ("shc-core-guard@gw-site-a.service loaded inactive dead shc core guard gw-site-a\n"
              "shc-core-guard@gw-site-a.timer loaded active waiting shc core guard gw-site-a\n"
              "shc-core-guard@root.service loaded inactive dead shc core guard root\n"
              "shc-core-guard@root.timer loaded active waiting shc core guard root\n")


class Srv6NodeInOrder(unittest.TestCase):
    def test_nothing_of_the_srv6_core_is_an_orphan(self):
        out = run(node("srv6", guard=True, tunsrc="2001:db8:4005:ffff::1", files=SRV6_FILES, units=SRV6_UNITS), "srv6")
        self.assertEqual(out["coreOrphans"], [])
        # the block route in the main tables was a finding before ("belongs to no VRF")
        self.assertEqual(out["ipv6Orphans"], [])
        self.assertEqual(out["netns"], {"gw-site-a": ["t51000", "t51003"], "root": ["e10000", "egress"]})


class OtherTransportsState(unittest.TestCase):
    def test_srv6_pieces_on_an_mpls_fabric(self):
        out = run(node("mpls", guard=True, tunsrc="2001:db8:4005:ffff::1", files=SRV6_FILES, units=SRV6_UNITS), "mpls")
        f = out["coreOrphans"]
        for ns in ("gw-site-a", "root"):
            self.assertIn("IPv6 unreachable 5f00:b046::/32 in the main table (%s): an SRv6 SID block route, the fabric "
                          "core transport is mpls" % ns, f)
            self.assertIn("IPv6 rule 999 to 5f00:b046::/32 lookup main (%s): the SRv6 SID block rule, the fabric core "
                          "transport is mpls" % ns, f)
            self.assertIn("nft table inet shc_core_guard (%s): the SRv6 core guard, the fabric core transport is mpls" % ns, f)
            self.assertIn("SRv6 encapsulation source 2001:db8:4005:ffff::1 (%s): the fabric core transport is mpls" % ns, f)
        self.assertIn("unit shc-core-guard@root.timer: the SRv6 core guard, but the fabric core transport is mpls", f)
        self.assertIn("guard file /etc/shc-fabric/gw-site-a.guard: the SRv6 core guard, but the fabric core transport is "
                      "mpls", f)
        self.assertIn("unit file /etc/systemd/system/shc-core-guard@.timer: the SRv6 core guard, but the fabric core "
                      "transport is mpls", f)
        # an inactive service between two timer runs counts through its timer only
        self.assertNotIn("unit shc-core-guard@root.service: the SRv6 core guard, but the fabric core transport is mpls", f)
        self.assertEqual(out["ipv6Orphans"], [])

    def test_mpls_pieces_on_an_srv6_fabric(self):
        out = run(node("srv6", guard=True, mpls_routes="16 as to 16 via inet 10.255.1.1 dev br-core\n17 dev t51000 \n",
                       files=SRV6_FILES + "/etc/modules-load.d/shc-fabric.conf\n", units=SRV6_UNITS), "srv6")
        f = out["coreOrphans"]
        self.assertIn("2 MPLS route(s) (root): MPLS state, but the fabric core transport is srv6", f)
        self.assertIn("/etc/modules-load.d/shc-fabric.conf loads the MPLS modules at boot, but the fabric core transport "
                      "is srv6", f)

    def test_a_clean_mpls_node(self):
        out = run(node("mpls", root_v6=text("srv6-root-ip6-unreachable.txt").replace("unreachable 5f00:b046::/32 dev lo "
                                                                                     "metric 4278198272 pref medium\n", ""),
                       gw_v6=text("srv6-gw-site-a-ip6-unreachable.txt").replace(
                           "unreachable 5f00:b046::/32 dev lo metric 4278198272 pref medium\n", ""),
                       rule6="0:\tfrom all lookup local\n1000:\tfrom all lookup [l3mdev-table]\n32766:\tfrom all lookup main\n"),
                  "mpls", block="")
        self.assertEqual(out["coreOrphans"], [])


class Srv6Mismatches(unittest.TestCase):
    def test_another_block(self):
        f = run(node("srv6", guard=True, files=SRV6_FILES, units=SRV6_UNITS), "srv6", block="5f00:db8::/32")["coreOrphans"]
        self.assertIn("IPv6 unreachable 5f00:b046::/32 in the main table (root): an SRv6 SID block route, not the fabric "
                      "SID block 5f00:db8::/32", f)
        self.assertIn("IPv6 rule 999 to 5f00:b046::/32 lookup main (root): the SRv6 SID block rule, not the fabric SID "
                      "block 5f00:db8::/32", f)

    def test_guard_of_an_instance_that_is_gone(self):
        files = SRV6_FILES + "/etc/shc-fabric/gw-site-b.guard\n"
        units = SRV6_UNITS + "shc-core-guard@gw-site-b.timer loaded active waiting shc core guard gw-site-b\n"
        f = run(node("srv6", guard=True, files=files, units=units), "srv6")["coreOrphans"]
        self.assertEqual(f, ["unit shc-core-guard@gw-site-b.timer: the SRv6 core guard of gw-site-b, which is not on the node",
                             "guard file /etc/shc-fabric/gw-site-b.guard: the SRv6 core guard of gw-site-b, which is not on "
                             "the node"])
        # an enabled timer whose unit is not loaded counts through its link
        f = run(node("srv6", guard=True, files=SRV6_FILES + "/etc/systemd/system/timers.target.wants/shc-core-guard@gw-x.timer\n",
                     units=SRV6_UNITS), "srv6")["coreOrphans"]
        self.assertEqual(f, ["unit shc-core-guard@gw-x.timer: the SRv6 core guard of gw-x, which is not on the node"])


class Ipv4Separation(unittest.TestCase):
    def test_the_local_rule_at_0_is_a_finding_on_both_transports(self):
        for transport, kw in (("mpls", {}), ("srv6", {"guard": True, "files": SRV6_FILES, "units": SRV6_UNITS})):
            with self.subTest(transport):
                kw = dict(kw, rule4=text("mpls-ip4-rule.txt"))
                if transport == "mpls":
                    kw.update(root_v6="", gw_v6="", rule6="")
                f = run(node(transport, **kw), transport, block=BLOCK if transport == "srv6" else "")["coreOrphans"]
                self.assertEqual(f, ["IPv4 rule lookup local at 0 (gw-site-a), not at 32765 behind the VRFs rule: a packet "
                                     "from a VRF reaches the addresses of the default VRF",
                                     "IPv4 rule lookup local at 0 (root), not at 32765 behind the VRFs rule: a packet "
                                     "from a VRF reaches the addresses of the default VRF"])
        both = "0:\tfrom all lookup local\n" + text("srv6-ip4-rule.txt")
        f = run(node("mpls", rule4=both, root_v6="", gw_v6="", rule6=""), "mpls", block="")["coreOrphans"]
        self.assertIn("IPv4 rule lookup local at 0, 32765 (root), not at 32765 behind the VRFs rule: a packet from a VRF "
                      "reaches the addresses of the default VRF", f)

    def test_ipv4_unreachable_default_of_a_gone_vrf(self):
        a = node("mpls", root_v6="", gw_v6="", rule6="")
        a["ip netns exec gw-site-a ip -j -4 route show table all type unreachable"] = json.dumps(
            [{"type": "unreachable", "dst": "default", "table": t, "metric": m, "flags": []}
             for t, m in (("51000", 4278198272), ("51004", 4278198272), ("51005", 1024))])
        f = run(a, "mpls", block="")["coreOrphans"]
        self.assertEqual(f, ["IPv4 unreachable default in table 51004 (gw-site-a) belongs to no VRF"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class KeepAndCollect(unittest.TestCase):
    def test_core_findings_carry_the_node_and_reach_the_report(self):
        n = {"netns": {}, "rootDevices": [], "rootBfd": [], "ipv6Orphans": [],
             "coreOrphans": ["IPv4 rule lookup local at 0 (root), not at 32765"]}
        start = {"ansible.builtin.set_fact": {"foc_nodes": []}}
        out = run_play([start, by_name(load(on.NODES), on.T_KEEP), by_name(load(on.MAIN), on.T_COLLECT),
                        write_out("{'nodes': foc_nodes, 'findings': foc_findings}")],
                       {"foc_fabric": {"metadata": {"name": "lab"}},
                        "_foc_node": {"results": [{"item": {"name": "bgw-1"}, "stdout": json.dumps(n)},
                                                  {"item": {"name": "bgw-2"}, "stdout": json.dumps({"netns": {}, "rootDevices": []})}]},
                        "fab_check_result": {"findings": []}})
        self.assertEqual(out["findings"], ["node bgw-1: IPv4 rule lookup local at 0 (root), not at 32765"])
        self.assertEqual(out["nodes"][1]["coreOrphans"], [])

    def test_the_transport_and_block_are_rendered(self):
        flat = lambda t: " ".join(t.split())  # noqa: E731
        for core, want in (({}, 'transport="mpls", block="")'),
                           ({"transport": "srv6", "srv6": {"block": BLOCK}}, 'transport="srv6", block="5f00:b046::/32")')):
            v = dict(on.VARS, fab_real={"core": dict(core, icmpSource6="")})
            self.assertIn(want, flat(on.shell(v)))
            sh = by_name(load(on.NODES), on.T_LIST)["ansible.builtin.shell"]
            got = run_play([{"ansible.builtin.set_fact": {"rendered": sh}}, write_out("rendered")], v)
            self.assertIn(want, flat(got))


if __name__ == "__main__":
    unittest.main()

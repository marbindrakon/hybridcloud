"""IPv6 internet egress (design/IPV6-PLAN.md §3.2-§3.6, §11.7): the realization check and plan (shc_fabric.py
egress-plan), the root FRR template (the egress VRF's sessions, each network VRF's veth session and VPN export, the
discard routes, the standby's community and prepend), the node apply (egress0's address and IPv6 MTU, the filter table
inet shc_egress6 loaded before FRR, removed again without the keys; a gateway VRF's covering routes), the fail-closed
guard, and the status side (the network's family row, the placement's copy of it, the fabric's node selection and
IPv6 egress status with Degraded and the alert). The plays run in ansible-playbook (skipped without it); the apply runs
against the fake kernel of test_shc_fabric_apply. Documentation prefixes only."""
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_frr_templates as T  # noqa: E402
from test_shc_fabric_apply import GW6, GW_CONF, ROOT6, ROOT_CONF, ApplyCase, is_v6, writes  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HF = os.path.join(ROLES, "hybridfabric_provision", "tasks")
HN = os.path.join(ROLES, "hybridnetwork_provision", "tasks")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
GUARD = os.path.join(ROOT, "eda", "common", "files", "bgw", "shc-egress6-guard")

ROUTED = "2001:db8:4005::/48"
BLOCK = "2001:db8:4005:4200::/56"
P64 = ["2001:db8:4005:4200::/64", "2001:db8:4005:4201::/64"]
UPSTREAM = [{"address": "2001:db8:237::2", "asn": 65002}, {"address": "2001:db8:237::3", "asn": 65002}]
E6 = {"enabled": True, "routedPrefixes": [ROUTED], "asn": 65011, "upstream": UPSTREAM,
      "nodes": {"bgw-1": {"address": "2001:db8:237::13/64"}, "bgw-2": {"address": "2001:db8:237::14/64"}},
      "denyDestinations6": ["fc00::/7", "2001:db8:f000::/36"], "allowDestinations6": [ROUTED, "2001:db8:66::/96"],
      "resolverDestinations6": ["2001:db8:53::53"], "externalSitePrefixes": {"acme/ext": ["2001:db8:4005:7000::/64"]}}
EGRESS4 = {"enabled": True, "vrf": {"name": "egress", "table": 9000}, "parentInterface": "bond1.237",
           "gateway": "198.51.100.1", "prefixLength": 24,
           "nodes": {"bgw-1": {"address": "198.51.100.13", "natPool": "198.51.100.64/26"},
                     "bgw-2": {"address": "198.51.100.14", "natPool": "198.51.100.128/26"}},
           "denyDestinations": ["10.0.0.0/8", "172.16.0.0/12"]}
CORE6 = {"loopbackPool6": "2001:db8:4005:ffff::/64"}


def plan_request(**over):
    req = {"action": "egress-plan", "domainAsn": 65010, "nodes": ["bgw-1", "bgw-2"], "core": CORE6,
           "translation": {"site1": {"pref64": "2001:db8:66::/96"}},
           "egress": dict(EGRESS4, ipv6=copy.deepcopy(E6)),
           "networks": [{"network": "acme/core", "vpnTarget": 10000, "index": 0, "internet": True, "ipv4": True,
                         "ipv6": {"block": BLOCK, "prefixes": [P64[1], P64[0]]}},
                        {"network": "acme/v6only", "vpnTarget": 10001, "index": 1, "internet": True, "ipv4": False,
                         "ipv6": {"block": "2001:db8:4005:4300::/56", "prefixes": []}},
                        {"network": "acme/v4", "vpnTarget": 10002, "index": 2, "internet": True}]}
    req.update(over)
    return req


class Plan(unittest.TestCase):
    def test_nodes_networks_and_lists(self):
        r = F.egress_plan(plan_request())
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["ipv6"])
        n1 = r["nodes"]["bgw-1"]["ipv6"]
        self.assertEqual((n1["address"], n1["ip"], n1["asn"], n1["wanMtu"], n1["mss"]),
                         ("2001:db8:237::13/64", "2001:db8:237::13", 65011, 1420, 1360))
        self.assertEqual(n1["upstream"], UPSTREAM)
        self.assertEqual(n1["routed"], [ROUTED])
        # the translator-embedded forms of the IPv4 deny list, per translation prefix
        self.assertEqual(n1["platDeny6"], ["2001:db8:66::a00:0/104", "2001:db8:66::ac10:0/108"])
        self.assertEqual(n1["external6"], ["2001:db8:4005:7000::/64"])
        self.assertEqual(n1["resolvers6"], ["2001:db8:53::53"])
        nets = {e["vrf"]: e for e in r["nodes"]["bgw-2"]["networks"]}
        # primary = nodes[index % 2]: core (index 0) is primary on bgw-1, so bgw-2 is its standby
        self.assertEqual(nets["e10000"]["ipv6"], {"block": BLOCK, "prefixes": P64, "standby": True, "src6": [BLOCK]})
        self.assertFalse(nets["e10001"]["ipv6"]["standby"])
        self.assertNotIn("ipv6", nets["e10002"])
        # a network without IPv4 gets no NAT address; the IPv4-only one keeps its
        self.assertEqual((nets["e10001"]["nat"], nets["e10001"]["internet"]), ("", False))
        self.assertEqual(nets["e10002"]["nat"], "198.51.100.130")
        self.assertEqual(r["networks"]["acme/core"]["prefixes6"], P64)
        self.assertEqual((r["networks"]["acme/core"]["primary"], r["networks"]["acme/v6only"]["primary"]), ("bgw-1", "bgw-2"))
        self.assertEqual((r["networks"]["acme/v4"]["ipv6"], r["networks"]["acme/v4"]["prefixes6"]), (False, []))

    def test_without_ipv6_nothing_changes(self):
        req = plan_request()
        del req["egress"]["ipv6"]
        r = F.egress_plan(req)
        self.assertEqual(r["errors"], [])
        self.assertFalse(r["ipv6"])
        self.assertNotIn("ipv6", r["nodes"]["bgw-1"])
        self.assertTrue(all("ipv6" not in e for e in r["nodes"]["bgw-1"]["networks"]))
        self.assertFalse(r["networks"]["acme/core"]["ipv6"])

    def test_refusals(self):
        bad = plan_request()
        bad["egress"]["ipv6"].update(asn=65010, routedPrefixes=["2001:db8::/64"])
        bad["egress"]["ipv6"]["nodes"]["bgw-2"]["address"] = "2001:db8:238::14/64"
        bad["core"] = {}
        errs = "\n".join(F.egress_plan(bad)["errors"])
        for want in ("dual-stack core", "asn 65010 is the fabric's own", "leaves no room", "not on one hand-off network",
                     "is not on the upstream"):
            self.assertIn(want, errs)
        # a block outside the routed space, a prefix outside its block
        req = plan_request()
        req["networks"][0]["ipv6"] = {"block": "2001:db8:5005::/56", "prefixes": []}
        req["networks"][1]["ipv6"]["prefixes"] = ["2001:db8:4005:4400::/64"]
        errs = "\n".join(F.egress_plan(req)["errors"])
        self.assertIn("outside the routed prefixes", errs)
        self.assertIn("outside the network's block", errs)

    def test_check_fabric_carries_the_ipv6_errors(self):
        spec = {"domainAsn": 65010, "vniPool": {"start": 51000, "end": 51999}, "l2vniPool": {"start": 61000, "end": 61999},
                "borderGateway": {"nodes": [{"name": "bgw-1"}, {"name": "bgw-2"}]}}
        real = {"core": {"loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24", "srgb": {"start": 16000, "end": 23999}},
                "vpnTargetPool": {"start": 10000, "end": 10999}, "egress": dict(EGRESS4, ipv6=dict(E6, upstream=[]))}
        r = F.check_fabric({"spec": spec, "realization": real})
        self.assertTrue(any("upstream is required" in e for e in r["errors"]))
        self.assertTrue(any("dual-stack core" in e for e in r["errors"]))

    def test_plat_deny6(self):
        self.assertEqual(F.plat_deny6({"a": {"pref64": "2001:db8:66::/96"}, "b": {"pref64": "2001:db8:67::/96"}, "c": {}},
                                      ["192.168.0.0/16", "bad", "10.0.0.0/8"]),
                         ["2001:db8:66::a00:0/104", "2001:db8:66::c0a8:0/112", "2001:db8:67::a00:0/104", "2001:db8:67::c0a8:0/112"])
        self.assertEqual(F.plat_deny6({"a": {"pref64": "2001:db8:66::/96"}}, ["192.168.0.0/16", "10.0.0.0/8"]),
                         ["2001:db8:66::a00:0/104", "2001:db8:66::c0a8:0/112"])
        self.assertEqual(F.plat_deny6({}, ["10.0.0.0/8"]), [])


def root_vars(standby):
    v = T.root_vars(v6=True)
    v["fab_real"]["egress"]["ipv6"] = {"enabled": True}
    r = F.egress_plan(plan_request())
    node = "bgw-2" if standby else "bgw-1"
    v["bgw_node"] = {"name": node}
    e = r["nodes"][node]
    net = next(n for n in e["networks"] if n["vrf"] == "e10000")
    v["hf_node_egress"] = {"vrf": "egress", "gateway": "198.51.100.1", "address": e["address"], "platformSources": [],
                           "networks": [net], "ipv6": e["ipv6"]}
    v["hf_node_vrfs"] = [dict(net, egress=True, circuits=[], families=[4, 6])]
    return v


class RootTemplate(unittest.TestCase):
    def test_standby_node(self):
        out = T.render(T.RT, root_vars(standby=True))
        # the egress VRF: its own AS and router id, the upstream sessions (timers 10/30, no BFD, no password), the
        # veth session to the network's VRF, both route-maps per session (RFC 8212)
        self.assertIn("router bgp 65011 vrf egress\n bgp router-id 198.51.100.14\n", out)
        self.assertIn(" neighbor 2001:db8:237::2 remote-as 65002\n neighbor 2001:db8:237::2 description upstream\n"
                      " neighbor 2001:db8:237::2 timers 10 30\n neighbor 2001:db8:237::2 timers connect 5\n", out)
        self.assertNotIn("bfd", out.split("router bgp 65011")[1])
        self.assertNotRegex(out, r"neighbor \S+ password")
        self.assertIn(" neighbor xo10000 interface v6only remote-as external\n", out)
        self.assertIn("  neighbor xo10000 route-map SHC-EGRESS-FROM-NET6 in\n  neighbor xo10000 route-map SHC-EGRESS-TO-NET6 out\n"
                      "  neighbor xo10000 maximum-prefix 1000\n", out)
        self.assertIn("  neighbor 2001:db8:237::3 route-map SHC-EGRESS-FROM-UP6 in\n  neighbor 2001:db8:237::3 route-map SHC-EGRESS-TO-UP6 out\n"
                      "  neighbor 2001:db8:237::3 maximum-prefix 10\n", out)
        # the prefix policy: the default in, the placement prefixes (inside the routed space, 49-64) out, prepended
        self.assertIn("ipv6 prefix-list SHC-ROUTED6 seq 5 permit 2001:db8:4005::/48 ge 49 le 64\n", out)
        self.assertIn("ipv6 prefix-list SHC-EGRESS-STANDBY6 seq 5 permit 2001:db8:4005:4200::/64\n", out)
        self.assertIn("route-map SHC-EGRESS-TO-UP6 permit 10\n match ipv6 address prefix-list SHC-EGRESS-STANDBY6\n"
                      " set as-path prepend 65011 65011\nexit\nroute-map SHC-EGRESS-TO-UP6 permit 20\n"
                      " match ipv6 address prefix-list SHC-ROUTED6\nexit\n", out)
        self.assertIn("route-map SHC-EGRESS-FROM-UP6 permit 20\n match ipv6 address prefix-list SHC-EXTERNAL6\n", out)
        # the network VRF: veth session, aggregates, VPN export of the default only, marked on the standby
        self.assertIn(" neighbor xe10000 interface v6only remote-as external\n", out)
        self.assertIn("  aggregate-address 2001:db8:4005:4200::/64\n  aggregate-address 2001:db8:4005:4201::/64\n"
                      "  neighbor xe10000 activate\n  neighbor xe10000 route-map E10000-FROM-EG6 in\n"
                      "  neighbor xe10000 route-map E10000-TO-EG6 out\n  neighbor xe10000 maximum-prefix 10\n"
                      "  label vpn export auto\n  rd vpn export 65010:10000\n  rt vpn both 65010:10000\n"
                      "  nexthop vpn export 2001:db8:4005:ffff::1\n  route-map vpn export E10000-VPN6\n", out)
        self.assertIn("route-map E10000-VPN6 deny 5\n match ipv6 address prefix-list E10000-PL6\nexit\n"
                      "route-map E10000-VPN6 permit 10\n match ipv6 address prefix-list SHC-DEFAULT6\n"
                      " set community 65010:998 additive\nexit\nroute-map E10000-VPN6 permit 20\nexit\n", out)
        # the discard routes in both VRFs
        self.assertEqual(out.count(" ipv6 route 2001:db8:4005::/48 reject 254\n"), 2)
        self.assertIn("IPv6 internet egress (standby node)", out)

    def test_primary_node(self):
        out = T.render(T.RT, root_vars(standby=False))
        self.assertIn("router bgp 65011 vrf egress\n bgp router-id 198.51.100.13\n", out)
        self.assertNotIn("SHC-EGRESS-STANDBY6", out)
        self.assertNotIn("as-path prepend", out)
        self.assertNotIn("set community 65010:998", out)
        self.assertIn("route-map SHC-EGRESS-TO-UP6 permit 20\n", out)
        self.assertIn("IPv6 internet egress (primary node)", out)

    def test_nothing_without_the_keys(self):
        v = root_vars(standby=False)
        del v["hf_node_egress"]["ipv6"]
        del v["hf_node_egress"]["networks"][0]["ipv6"]
        del v["hf_node_vrfs"][0]["ipv6"]
        v["hf_node_vrfs"][0]["families"] = [4]
        out = T.render(T.RT, v)
        for s in ("router bgp 65011", "SHC-ROUTED6", " reject 254", "xe10000", "address-family ipv6 unicast"):
            self.assertNotIn(s, out)

    def test_a_network_without_placement_prefixes_announces_nothing(self):
        v = root_vars(standby=True)
        v["hf_node_egress"]["networks"][0]["ipv6"]["prefixes"] = []
        v["hf_node_vrfs"][0]["ipv6"]["prefixes"] = []
        out = T.render(T.RT, v)
        self.assertIn("ipv6 prefix-list E10000-PL6 seq 5 deny ::/0 le 128\n", out)
        self.assertNotIn("aggregate-address", out)
        self.assertNotIn("SHC-EGRESS-STANDBY6", out)


def root_conf6():
    c = dict(copy.deepcopy(ROOT_CONF), **ROOT6)
    r = F.egress_plan(plan_request())
    e = r["nodes"]["bgw-1"]
    c["egress"]["ipv6"] = dict(e["ipv6"], icmp6=ROOT6["icmp6"])
    net = next(n for n in e["networks"] if n["vrf"] == "e10000")
    c["egress"]["networks"][0]["ipv6"] = net["ipv6"]
    return c


class Apply(ApplyCase):
    def test_root_ipv6_egress_up_idempotent_and_removed(self):
        h = self.h
        h.conf("root", root_conf6())
        out, log = h.ok(self, "root")
        self.keep("root-egress6-run1", log, out)
        st = h.get_state()["ns"][""]
        # egress0: the hand-off address, the IPv6 MTU; the filter table loaded in the same run
        self.assertEqual(h.addrs6("", "egress0"), ["2001:db8:237::13/64"])
        self.assertEqual(h.sysctl("", "net/ipv6/conf/egress0/mtu"), "1420")
        nft = st["nft"]["inet shc_egress6"]
        self.assertIn("table inet shc_egress6 {", nft)
        self.assertIn("set src6_10000 { type ipv6_addr; flags interval; elements = { %s, 2001:db8:4005:1::1:1 }; }" % BLOCK, nft)
        self.assertIn("set plat_deny6 { type ipv6_addr; flags interval; elements = { 2001:db8:66::a00:0/104, 2001:db8:66::ac10:0/108 }; }", nft)
        self.assertIn("set resolvers6 { type ipv6_addr; elements = { 2001:db8:53::53 }; }", nft)
        self.assertIn('meta nfproto ipv6 ct zone set iifname map { "xe10000" : 1 }', nft)
        self.assertIn('oifname "xe10000" ip6 saddr != @src6_10000 counter drop comment "source outside the network\'s platform space"', nft)
        self.assertIn('oifname "xe*" ip6 daddr @plat_deny6 counter drop', nft)
        self.assertLess(nft.index("@plat_deny6"), nft.index("@allow6 accept"))
        self.assertLess(nft.index("@allow6 accept"), nft.index("@deny6 counter drop"))
        self.assertIn('oifname "xe*" ip6 daddr @resolvers6 meta l4proto { tcp, udp } th dport 53 accept', nft)
        self.assertIn("tcp option maxseg size > 1360 counter tcp option maxseg size set 1360", nft)
        self.assertIn('iifname "xe*" meta nfproto ipv6 counter drop comment "nothing unsolicited into a network"', nft)
        self.assertIn('iifname "egress0" ip6 saddr @external6 accept', nft)
        self.assertIn('iifname "egress0" ip6 saddr @routed6 counter drop comment "routed source from upstream"', nft)
        self.assertLess(nft.index("@external6 accept"), nft.index("@routed6 counter drop"))
        # the IPv4 table is untouched, still there
        self.assertIn("ip shc_egress", st["nft"])
        self.assertTrue(os.path.exists(os.path.join(h.etc, "root.egress6")))
        # a second run changes nothing and writes nothing
        out2, log2 = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if (is_v6(l) and "egress0" in l) or "shc_egress6" in l], [])
        # the keys removed: the table, the address and the IPv6 MTU go; nothing IPv6 is left on egress0
        c = root_conf6()
        del c["egress"]["ipv6"]
        del c["egress"]["networks"][0]["ipv6"]
        h.conf("root", c)
        out3, log3 = h.ok(self, "root")
        self.assertTrue(out3.strip().endswith("changed=yes"), out3)
        self.assertNotIn("inet shc_egress6", h.get_state()["ns"][""]["nft"])
        self.assertEqual(h.addrs6("", "egress0"), [])
        self.assertEqual(h.sysctl("", "net/ipv6/conf/egress0/mtu"), "9000")
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.egress6")))
        # and a run after that issues no IPv6 egress command at all
        out4, log4 = h.ok(self, "root")
        self.assertTrue(out4.strip().endswith("changed=no"), out4)
        self.assertEqual([l for l in writes(log4) if (is_v6(l) and "egress0" in l) or "shc_egress6" in l], [])
        self.assertEqual([l for l in log4 if re.search(r"ip (-j )?(-br )?-6 addr show dev egress0", l)], [])

    def test_filter_load_failure_fails_the_apply(self):
        h = self.h
        h.conf("root", root_conf6())
        rc, out, err, log = h.run("root", fail=r"nft -f -")
        # the IPv4 table is loaded the same way, so the first refused load is the IPv4 one; both fail the apply
        self.assertNotEqual(rc, 0)

    def test_down_removes_the_table(self):
        h = self.h
        h.conf("root", root_conf6())
        h.ok(self, "root")
        h.ok(self, "root", "down")
        self.assertNotIn("inet shc_egress6", h.get_state()["ns"][""]["nft"])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.egress6")))

    def test_gateway_covering_routes(self):
        h = self.h
        h.conf("root", dict(copy.deepcopy(ROOT_CONF), **ROOT6))
        h.ok(self, "root")
        gw = dict(copy.deepcopy(GW_CONF), **GW6)
        gw["vrfs"][0]["covering6"] = ["2001:DB8:4005:4200::/56", ROUTED]
        h.conf("gw-iaas1", gw)
        out, log = h.ok(self, "gw-iaas1")
        routes = h.get_state()["ns"]["gw-iaas1"]["routes6"]

        def covers(table):
            return sorted(r["dst"] for r in routes if r["type"] == "unreachable" and r["table"] == table and r["dst"] != "default")

        self.assertEqual(covers("51000"), sorted(["2001:db8:4005::/48", "2001:db8:4005:4200::/56"]))
        self.assertEqual(covers("51002"), [])
        self.assertEqual(h.unreachable6("gw-iaas1"), ["51000", "51002"])
        out2, log2 = h.ok(self, "gw-iaas1")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        # one prefix dropped: its route goes, the other stays
        gw["vrfs"][0]["covering6"] = [ROUTED]
        h.conf("gw-iaas1", gw)
        h.ok(self, "gw-iaas1")
        routes = h.get_state()["ns"]["gw-iaas1"]["routes6"]
        self.assertEqual(covers("51000"), ["2001:db8:4005::/48"])
        # the VRF removed: its covering route and default go with it
        gw["vrfs"] = gw["vrfs"][1:]
        h.conf("gw-iaas1", gw)
        h.ok(self, "gw-iaas1")
        routes = h.get_state()["ns"]["gw-iaas1"]["routes6"]
        self.assertEqual(covers("51000"), [])
        self.assertEqual(h.unreachable6("gw-iaas1"), ["51002"])


class Guard(unittest.TestCase):
    """shc-egress6-guard with nft, vtysh, systemctl and logger answered by shims (jq is the real one)."""

    def setUp(self):
        if not shutil.which("jq"):
            self.skipTest("jq not installed")
        self.tmp = tempfile.mkdtemp(prefix="shc-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.conf = os.path.join(self.tmp, "root.json")
        self.mark = os.path.join(self.tmp, "shut")
        self.calls = os.path.join(self.tmp, "calls")
        self.running = os.path.join(self.tmp, "running")
        with open(GUARD) as f:
            text = f.read().replace("/etc/shc-fabric/root.json", self.conf).replace("/run/shc-egress6-guard.shut", self.mark)
        self.script = os.path.join(self.tmp, "guard")
        with open(self.script, "w") as f:
            f.write(text)
        shims = {
            "nft": 'exit "${FAKE_NFT_RC:-0}"\n',
            "systemctl": "exit 0\n",
            "logger": 'echo "logger $*" >> "%s"\n' % self.calls,
            "vtysh": ('if [ "$2" = "show running-config bgpd" ]; then cat "%s"; exit 0; fi\n'
                      'echo "vtysh $*" >> "%s"\n' % (self.running, self.calls)),
        }
        for name, body in shims.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write("#!/bin/bash\n" + body)
            os.chmod(p, 0o755)
        with open(self.conf, "w") as f:
            json.dump({"egress": {"vrf": "egress", "ipv6": {"asn": 65011, "upstream": UPSTREAM}}}, f)
        self.set_running(" neighbor 2001:db8:237::2 remote-as 65002\n neighbor 2001:db8:237::3 remote-as 65002\n")

    def set_running(self, text):
        with open(self.running, "w") as f:
            f.write("router bgp 65011 vrf egress\n" + text)

    def run_guard(self, table_present):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_NFT_RC="0" if table_present else "1")
        p = subprocess.run(["bash", self.script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        try:
            with open(self.calls) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_table_present_nothing_happens(self):
        self.assertEqual(self.run_guard(True), "")
        self.assertFalse(os.path.exists(self.mark))

    def test_table_missing_shuts_the_upstreams_once(self):
        calls = self.run_guard(False)
        self.assertIn("vtysh -c configure terminal -c router bgp 65011 vrf egress -c neighbor 2001:db8:237::2 shutdown", calls)
        self.assertIn("neighbor 2001:db8:237::3 shutdown", calls)
        self.assertIn("fail closed", calls)
        with open(self.mark) as f:
            self.assertEqual(sorted(f.read().split()), ["2001:db8:237::2", "2001:db8:237::3"])
        # already shut: no second shutdown
        self.set_running(" neighbor 2001:db8:237::2 remote-as 65002\n neighbor 2001:db8:237::2 shutdown\n"
                         " neighbor 2001:db8:237::3 remote-as 65002\n neighbor 2001:db8:237::3 shutdown\n")
        os.remove(self.calls)
        self.assertEqual(self.run_guard(False), "")
        # the table is back: the ones this guard shut are opened again, the marker goes
        calls = self.run_guard(True)
        self.assertIn("-c no neighbor 2001:db8:237::2 shutdown", calls)
        self.assertIn("-c no neighbor 2001:db8:237::3 shutdown", calls)
        self.assertFalse(os.path.exists(self.mark))

    def test_a_neighbour_not_in_the_running_configuration_is_left_alone(self):
        self.set_running(" neighbor 2001:db8:237::2 remote-as 65002\n")
        calls = self.run_guard(False)
        self.assertIn("neighbor 2001:db8:237::2 shutdown", calls)
        self.assertNotIn("2001:db8:237::3", calls)

    def test_without_ipv6_egress_nothing(self):
        with open(self.conf, "w") as f:
            json.dump({"egress": {"vrf": "egress"}}, f)
        self.assertEqual(self.run_guard(False), "")


class NodeWaitScript(unittest.TestCase):
    """The root node's wait step with IPv6 egress: the shell parses and its IPv6 egress check reports and fails as
    designed (ip, sysctl, vtysh answered from fixtures)."""

    def render(self):
        t = by_name(load(os.path.join(HF, "node_root.yml")), "{{ bgw_node.name }}: wait for BGP and IS-IS")
        env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
        env.filters["to_json"] = json.dumps
        egress = root_vars(standby=False)["hf_node_egress"]
        return env.from_string(t["ansible.builtin.shell"]["cmd"]).render(
            hf_node_egress=egress, hf_node_vrfs=[], hf_pe={"loopback6": ""}, fab_spec={"domainAsn": 65010},
            fab_real={"core": {"loopbackPool": "10.255.0.0/24", "loopbackPool6": ""}}, hf_root={"bridge": "br-core", "link": "10.255.1.1/24"})

    def code(self):
        sh = self.render()
        m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", sh, re.S)
        return sh, textwrap.dedent(m.group(1))

    def execute(self, answers, table=True):
        sh, code = self.code()
        subprocess.run(["bash", "-n"], input=sh, text=True, check=True)
        ns = {"__name__": "wait_under_test"}
        exec(compile(code, "<wait>", "exec"), ns)
        calls = []

        def run(cmd):
            calls.append(" ".join(cmd))
            return answers.get(" ".join(cmd), "")

        class Done:
            returncode = 0 if table else 1
        real = subprocess.run
        subprocess.run = lambda *a, **k: Done()
        out = io.StringIO()
        try:
            with contextlib_redirect(out):
                rc = ns["main"](run)
        finally:
            subprocess.run = real
        line = [l for l in out.getvalue().splitlines() if l.startswith("egress6=")][-1]
        return rc, json.loads(line.split("=", 1)[1]), calls

    GOOD = {
        "ip -j -6 addr show dev egress0": json.dumps([{"ifname": "egress0", "addr_info": [{"local": "2001:db8:237::13", "prefixlen": 64}]}]),
        "sysctl -n net/ipv6/conf/egress0/mtu": "1420\n",
        "vtysh -c show bgp vrf egress ipv6 unicast summary json": json.dumps({"peers": {
            "2001:db8:237::2": {"state": "Established"}, "2001:db8:237::3": {"state": "Active"}}}),
        "vtysh -c show ipv6 route vrf egress ::/0 json": json.dumps({"::/0": [{"protocol": "bgp"}]}),
        "vtysh -c show ipv6 route vrf e10000 ::/0 json": json.dumps({"::/0": [{"protocol": "bgp"}]}),
    }

    def test_healthy(self):
        rc, rec, _ = self.execute(self.GOOD)
        self.assertEqual(rc, 0)
        self.assertEqual((rec["healthy"], rec["default"], rec["table"], rec["networks"]), (True, True, True, 1))
        self.assertEqual([s["state"] for s in rec["upstream"]], ["Established", "Active"])

    def test_sessions_down_is_reported_not_failed(self):
        a = dict(self.GOOD, **{"vtysh -c show bgp vrf egress ipv6 unicast summary json": json.dumps({"peers": {
            "2001:db8:237::2": {"state": "Active"}, "2001:db8:237::3": {"state": "Connect"}}}),
            "vtysh -c show ipv6 route vrf egress ::/0 json": "{}"})
        rc, rec, calls = self.execute(a)
        self.assertEqual(rc, 0)
        self.assertEqual((rec["healthy"], rec["default"]), (False, False))
        # without the default in the egress VRF the network VRFs are not asked for it
        self.assertNotIn("vtysh -c show ipv6 route vrf e10000 ::/0 json", calls)

    def test_failures(self):
        a = dict(self.GOOD, **{"vtysh -c show ipv6 route vrf e10000 ::/0 json": "{}"})
        self.assertEqual(self.execute(a)[0], 1)
        a = dict(self.GOOD, **{"ip -j -6 addr show dev egress0": json.dumps([{"ifname": "egress0", "addr_info": [
            {"local": "2001:db8:237::13", "prefixlen": 64, "tentative": True}]}])})
        self.assertEqual(self.execute(a)[0], 1)
        self.assertEqual(self.execute(self.GOOD, table=False)[0], 1)
        a = dict(self.GOOD, **{"vtysh -c show bgp vrf egress ipv6 unicast summary json": json.dumps({"peers": {
            "2001:db8:237::2": {"state": "Established"}}})})
        rc, rec, _ = self.execute(a)
        self.assertEqual(rc, 1)
        self.assertEqual(rec["upstream"][1]["state"], "Missing")


import contextlib  # noqa: E402


def contextlib_redirect(out):
    return contextlib.redirect_stdout(out)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NetworkStatus(unittest.TestCase):
    def rows(self, **vars_):
        t = load(os.path.join(HN, "egress_status.yml"))[0]
        base = {"hn_v4": ["10.65.0.0/16"], "hn_mode6": "Platform", "hn_egress_want": True, "hn_egress_want6": True,
                "hn_egress_offered": True, "hn_egress_offered6": True, "hn_fabric_name": "lab",
                "hn_egress_carried": {"network": "acme/g", "sourceAddresses": ["198.51.100.64"], "ipv6": True},
                "hn_egress6_degraded": False, "hn_egress6_degraded_nodes": []}
        base.update(vars_)
        out = run_play([t, write_out("{'egress': hn_egress, 'note': hn_egress_note}")], base)
        return out["egress"], out["note"]

    def row6(self, egress):
        return next((r for r in egress.get("families", []) if r["family"] == 6), None)

    def test_ready_routed(self):
        e, note = self.rows()
        self.assertEqual(self.row6(e), {"family": 6, "state": "ready", "mode": "Routed"})
        self.assertEqual(e["internet"], "ready")
        self.assertIn("; IPv6 internet egress", note)

    def test_degraded(self):
        e, note = self.rows(hn_egress6_degraded=True, hn_egress6_degraded_nodes=["bgw-2"])
        r = self.row6(e)
        self.assertEqual((r["state"], r["reason"]), ("ready", "Degraded"))
        self.assertIn("bgw-2", r["message"])
        self.assertIn("degraded", note)

    def test_pending_while_the_fabric_has_not_realized_it(self):
        e, note = self.rows(hn_egress_carried={"network": "acme/g", "sourceAddresses": ["198.51.100.64"], "ipv6": False})
        self.assertEqual(self.row6(e), {"family": 6, "state": "pending", "mode": "Routed"})
        self.assertIn("being added", note)

    def test_not_offered_and_address_choice(self):
        e, _ = self.rows(hn_egress_offered6=False, hn_egress_carried={"network": "acme/g", "sourceAddresses": ["198.51.100.64"]})
        self.assertEqual(self.row6(e)["reason"], "NotOffered")
        e, _ = self.rows(hn_mode6="Tenant", hn_egress_want6=False)
        self.assertEqual(self.row6(e)["reason"], "AddressChoice")

    def test_removing_and_ipv6_only_overall(self):
        e, note = self.rows(hn_egress_want=False, hn_egress_want6=False)
        self.assertEqual(self.row6(e)["state"], "removing")
        e, _ = self.rows(hn_v4=[], hn_egress_carried={"network": "acme/g", "sourceAddresses": [], "internet": False, "ipv6": True})
        self.assertEqual(e["internet"], "ready")
        self.assertEqual([r["family"] for r in e["families"]], [6])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricSelection(unittest.TestCase):
    def networks(self, nets, placements, v6on=True):
        t = by_name(load(os.path.join(HF, "main.yml")), "Set the networks with internet egress or platform reach")
        alloc = {"egress": {"HybridNetwork/acme/core": 0, "HybridNetwork/acme/t6": 1, "HybridNetwork/acme/v4": 2},
                 "vpnTarget": {"HybridNetwork/acme/core": 10000, "HybridNetwork/acme/t6": 10001, "HybridNetwork/acme/v4": 10002}}
        return run_play([t, write_out("hf_egress_networks")],
                        {"fab_name": "lab", "fab_ledger_result": {"ledger": {"alloc": alloc}},
                         "_hf_networks": {"resources": nets}, "_hf_placements": {"resources": placements},
                         "fab_real": {"egress": {"enabled": True, "ipv6": {"enabled": v6on}}}, "fab_st": {}})

    def net(self, name, prefixes, mode, block=None, internet=True, fabric="lab"):
        st = {"fabric": fabric, "ipv6": {"mode": mode}}
        if block:
            st["ipv6"]["block"] = {"prefix": block}
        return {"metadata": {"namespace": "acme", "name": name}, "spec": {"prefixes": prefixes, "egress": {"internet": internet}},
                "status": st}

    def pl(self, name, network, subnets, deleting=False):
        md = {"namespace": "acme", "name": name}
        if deleting:
            md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        return {"metadata": md, "spec": {"network": network}, "status": {"subnets": subnets}}

    def test_selection(self):
        nets = [self.net("core", ["10.60.0.0/16"], "Platform", BLOCK),
                self.net("t6", ["2001:db8:100::/48"], "Tenant"),
                self.net("v4", ["10.61.0.0/16"], "None")]
        pls = [self.pl("core-a", "core", [{"family": 4, "prefix": "10.60.0.0/24"}, {"family": 6, "prefix": P64[0]}]),
               self.pl("core-b", "core", [{"family": 6, "prefix": P64[1]}]),
               self.pl("core-gone", "core", [{"family": 6, "prefix": "2001:db8:4005:4202::/64"}], deleting=True),
               self.pl("t6-a", "t6", [{"family": 6, "prefix": "2001:db8:100:1::/64"}])]
        out = {e["network"]: e for e in self.networks(nets, pls)}
        self.assertEqual(out["acme/core"]["ipv6"], {"block": BLOCK, "prefixes": P64})
        self.assertEqual((out["acme/core"]["ipv4"], out["acme/core"]["index"], out["acme/core"]["vpnTarget"]), (True, 0, 10000))
        self.assertNotIn("ipv6", out["acme/t6"])
        self.assertFalse(out["acme/t6"]["ipv4"])
        self.assertNotIn("ipv6", out["acme/v4"])
        # IPv6 egress off: no network has an ipv6 entry
        out = {e["network"]: e for e in self.networks(nets, pls, v6on=False)}
        self.assertTrue(all("ipv6" not in e for e in out.values()))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricIPv6Status(unittest.TestCase):
    def status(self, results, prev=None, minutes=5):
        t = by_name(load(os.path.join(HF, "main.yml")), "Set the IPv6 egress status")
        out = run_play([t, write_out("{'status': hf_egress6_status, 'alert': hf_egress6_alert}")],
                       {"hf_egress": {"ipv6": True}, "hf_node_results": results,
                        "fab_st": {"realization": {"egress": {"ipv6": prev or {}}}},
                        "fab_real": {"egress": {"ipv6": {"sessionDownAlertMinutes": minutes}}}})
        return out["status"], out["alert"]

    def node(self, name, healthy):
        return {"name": name, "egressIpv6": {"address": "2001:db8:237::13/64", "healthy": healthy, "default": healthy,
                                             "table": True, "upstream": []}}

    def test_healthy(self):
        s, alert = self.status([self.node("bgw-1", True), self.node("bgw-2", True)])
        self.assertEqual((s["enabled"], s["degraded"], s["degradedNodes"], s["degradedSince"]), (True, False, [], ""))
        self.assertFalse(alert)
        self.assertEqual(sorted(s["nodes"]), ["bgw-1", "bgw-2"])

    def test_degraded_keeps_since_and_alerts_after_the_minutes(self):
        s, alert = self.status([self.node("bgw-1", True), self.node("bgw-2", False)])
        self.assertEqual((s["degraded"], s["degradedNodes"]), (True, ["bgw-2"]))
        self.assertTrue(s["degradedSince"])
        self.assertFalse(alert)
        old = {"degraded": True, "degradedSince": "2026-01-01T00:00:00Z"}
        s2, alert2 = self.status([self.node("bgw-1", True), self.node("bgw-2", False)], prev=old)
        self.assertEqual(s2["degradedSince"], "2026-01-01T00:00:00Z")
        self.assertTrue(alert2)
        # recovered: since cleared, no alert
        s3, alert3 = self.status([self.node("bgw-1", True), self.node("bgw-2", True)], prev=old)
        self.assertEqual((s3["degraded"], s3["degradedSince"]), (False, ""))
        self.assertFalse(alert3)

    def test_off(self):
        t = by_name(load(os.path.join(HF, "main.yml")), "Set the IPv6 egress status")
        out = run_play([t, write_out("hf_egress6_status")],
                       {"hf_egress": {"ipv6": False}, "hf_node_results": [{"name": "bgw-1"}], "fab_st": {}, "fab_real": {"egress": {}}})
        self.assertEqual(out, {"enabled": False})

    def test_families_of_the_root_vrfs(self):
        t = by_name(load(os.path.join(HF, "main.yml")), "Keep the attachment plan")
        vrfs = {"bgw-1": [{"vrf": "e10000", "ipv6": {"block": BLOCK}}, {"vrf": "e10001"}]}
        out = run_play([t, write_out("hf_attach")],
                       {"fab_check_result": {"vrfs": vrfs}, "fab_real": {"attachments": {}}, "fab_st": {}})
        self.assertEqual([v["families"] for v in out["vrfs"]["bgw-1"]], [[4, 6], [4]])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementStatus(unittest.TestCase):
    def status(self, enabled, row=None):
        t = by_name(load(os.path.join(NP, "main.yml")), "Describe the placement's IPv6")
        # (an extra var can't be overwritten by set_fact: the expression is written to another fact)
        t = {"vars": t["vars"], "ansible.builtin.set_fact": {"_s": t["ansible.builtin.set_fact"]["np_status"]}}
        net = {"status": {"egress": {"families": ([row] if row else []) + [{"family": 4, "state": "ready"}]}}}
        return run_play([t, write_out("_s")],
                        {"fab_real": {"placement": {"ipv6": {"addressMode": {"CloudVirt": "dhcpv6-stateful"}}},
                                      "egress": {"ipv6": {"enabled": enabled}}},
                         "np_backend_kind": "CloudVirt", "np_network": net, "np_status": {}, "np_prefixes_v6": [P64[0]]})

    def test_copies_the_network_row(self):
        s = self.status(True, {"family": 6, "state": "ready", "mode": "Routed", "reason": "Degraded", "message": "m"})
        self.assertEqual(s["ipv6"], {"addressing": "DHCPv6", "internet": {"state": "ready", "reason": "Degraded", "message": "m"}})
        s = self.status(True, {"family": 6, "state": "ready", "mode": "Routed"})
        self.assertEqual(s["ipv6"]["internet"], {"state": "ready"})

    def test_no_row_no_internet_key(self):
        self.assertEqual(self.status(True), {"ipv6": {"addressing": "DHCPv6"}})

    def test_not_offered_while_off(self):
        self.assertEqual(self.status(False)["ipv6"]["internet"]["reason"], "NotOffered")


if __name__ == "__main__":
    unittest.main()

"""IPv6 on attachment circuits, the border gateway side (design/IPV6-PLAN.md §3.7, R-gw §6): the root FRR template
(a second neighbour per circuit on its IPv6 link, its IPv6 prefix lists and route-maps, the network VRF's IPv6 unicast
family also for a circuit-only network, no `nexthop vpn export` once a CE's IPv6 routes are in the VRF), the node
apply (the device's IPv6 sysctls and address, the table ip6 shc_attach6 loaded before IPv6 comes on and removed with
the last IPv6 circuit), the fabric job's enrichment of the plan (families, egress6) and its guard, and the orphan
check (stale IPv6 addresses, table and BFD sessions; nothing for live ones).

A circuit without IPv6 renders and applies byte for byte what the release before (BASE, the V9b merge) did: the FRR
configuration, the calls the apply script makes and the kernel state it leaves. The circuits' inputs follow the
attachments-plan contract (an optional "ipv6" sub-dict per circuit; address6, link6, sources6 per node entry), built
by hand here. The apply runs against the fake kernel of test_shc_fabric_apply; the generated nftables text is also
checked by nft itself (`nft -c` in an unprivileged user and network namespace) where that works. Documentation
prefixes only."""
import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_frr_templates as T  # noqa: E402
import test_egress_ipv6 as E  # noqa: E402
import test_fabric_orphan_nodes as O  # noqa: E402
import test_shc_fabric_apply as A  # noqa: E402
from test_shc_fabric_apply import CIRCUIT, ROOT6, ROOT_CONF, ApplyCase, blade, index, writes  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

# the release before IPv6 on circuits (the V9b merge)
BASE = "51c8506"
SCRIPT_REL = "eda/common/files/bgw/shc-fabric-apply"
HF = os.path.join(ROOT, "eda", "rulebooks", "roles", "hybridfabric_provision", "tasks")

LINK6 = "2001:db8:4005:100::/64"
CE6 = "2001:db8:4005:100::3"
V6 = {"link": LINK6, "prefixLength": 64, "ce": CE6,
      "nodes": {"bgw-1": "2001:db8:4005:100::1", "bgw-2": "2001:db8:4005:100::2"},
      "accept": ["2001:db8:50::/48 le 64"],
      "deny": ["2001:db8:4005:4200::/56 le 128", "2001:db8:4005::/52 le 128", "::/0"],
      "advertise": ["2001:db8:4005:4200::/64 le 128"], "denyDefault": True, "advertiseDefault": False,
      "sources": ["2001:db8:50::/48", LINK6]}


def git_show(rel):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{rel}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def base_render(variables):
    """The release's render, without the descriptions of interface neighbours (gone since: frr-reload.py 8.5)."""
    text = git_show(os.path.relpath(T.RT, ROOT))
    return None if text is None else T.without_interface_descriptions(T.render_text(text, variables))


def with_v6(c, **over):
    """A VRF circuit entry (root_vrfs) with the IPv6 sub-dict and the node's address6."""
    return dict(c, ipv6=dict(copy.deepcopy(V6), **over), address6="2001:db8:4005:100::1/64")


def circuit_only(**over):
    """The dual-stack core, network e10001 with its circuit AC1 over IPv6 too (no IPv6 egress anywhere)."""
    v = T.root_vars(v6=True, families=[4])
    vrf = v["hf_node_vrfs"][1]
    vrf["circuits"] = [with_v6(vrf["circuits"][0], **over)]
    vrf["families"] = [4, 6]
    return v


def combined():
    """IPv6 egress on (the primary node) and a circuit with IPv6 in the same network VRF e10000."""
    v = E.root_vars(standby=False)
    c = copy.deepcopy(T.root_vars()["hf_node_vrfs"][1]["circuits"][0])
    v["hf_node_vrfs"][0]["circuits"] = [with_v6(c, advertise=["2001:db8:4005:4200::/64 le 128", "::/0"],
                                                advertiseDefault=True)]
    return v


def vrf_block(text, vrf):
    return text.split(f"router bgp 65010 vrf {vrf}\n")[1].split("\nexit\n")[0]


def af(text, vrf, fam):
    body = vrf_block(text, vrf)
    m = re.search(rf" address-family {fam} unicast\n(.*?) exit-address-family", body, re.S)
    return m.group(1).splitlines() if m else None


# the lines a circuit's IPv6 side may add to the release's render (and the VRF comment on the IPv6 family)
ADDED = [r"^!", r"^ !$", r"^\{#", r"^ipv6 prefix-list AC\d+-(ACCEPT6|DENY6|OUT6) seq \d+ permit ", r"^route-map AC\d+-(IN6|OUT6) (permit|deny) \d+$",
         r"^ match ipv6 address prefix-list AC\d+-(DENY6|ACCEPT6|OUT6)$", r"^exit$",
         r"^ neighbor 2001:db8:4005:100::3 (remote-as \d+|description AC\d+6 \S+|password \S+|timers 10 30|bfd)$",
         r"^ address-family ipv6 unicast$", r"^ exit-address-family$",
         r"^  neighbor 2001:db8:4005:100::3 (activate|route-map AC\d+-(IN6 in|OUT6 out)|maximum-prefix \d+( restart \d+| warning-only)?)$",
         r"^  (label vpn export auto|rd vpn export \S+|rt vpn both \S+|export vpn|import vpn)$"]


class TemplateIPv4Unchanged(unittest.TestCase):
    """Without an IPv6 circuit the root renders exactly what the release rendered."""

    def check(self, variables, name):
        new = T.render(T.RT, variables)
        old = base_render(variables)
        if old is None:
            self.skipTest(f"{BASE} not in this checkout")
        self.assertEqual(new, old, name)

    def test_ipv4_core(self):
        self.check(T.root_vars(), "ipv4 core, no families")
        self.check(T.root_vars(families=[4]), "ipv4 core")

    def test_dual_stack_core(self):
        self.check(T.root_vars(v6=True, families=[4]), "dual-stack core")

    def test_ipv6_egress_with_an_ipv4_circuit(self):
        v = E.root_vars(standby=True)
        v["hf_node_vrfs"][0]["circuits"] = [copy.deepcopy(T.root_vars()["hf_node_vrfs"][1]["circuits"][0])]
        self.check(v, "IPv6 egress, IPv4 circuit in the same VRF")
        self.assertIn("  nexthop vpn export 2001:db8:4005:ffff::1\n", T.render(T.RT, v))

    def test_md5_and_warning(self):
        v = T.root_vars(v6=True, families=[4])
        v["hf_node_vrfs"][1]["circuits"][0].update(authentication="md5", maxPrefixAction="warning")
        v["hf_circuit_passwords"] = {"entity-acme/ce1": "dummy-not-a-secret"}
        self.check(v, "md5 circuit")


class TemplateCircuitIPv6(unittest.TestCase):
    def setUp(self):
        self.v = circuit_only()
        self.out = T.render(T.RT, self.v)
        T.save("root-circuit-ipv6.conf", self.out)

    def test_second_neighbour(self):
        body = vrf_block(self.out, "e10001")
        self.assertIn(" neighbor 10.255.128.3 remote-as 64512\n neighbor 10.255.128.3 description AC1 entity-acme/ce1\n"
                      " neighbor 10.255.128.3 timers 10 30\n neighbor 10.255.128.3 bfd\n"
                      f" neighbor {CE6} remote-as 64512\n neighbor {CE6} description AC16 entity-acme/ce1\n"
                      f" neighbor {CE6} timers 10 30\n neighbor {CE6} bfd\n", body)
        self.assertNotRegex(body, r"password")

    def test_each_neighbour_in_its_own_family(self):
        v4, v6 = af(self.out, "e10001", "ipv4"), af(self.out, "e10001", "ipv6")
        self.assertTrue(any("10.255.128.3 activate" in ln for ln in v4))
        self.assertFalse(any(CE6 in ln for ln in v4), v4)
        self.assertFalse(any("10.255.128.3" in ln for ln in v6), v6)
        self.assertEqual(v6, [f"  neighbor {CE6} activate", f"  neighbor {CE6} route-map AC1-IN6 in",
                              f"  neighbor {CE6} route-map AC1-OUT6 out", f"  neighbor {CE6} maximum-prefix 100 restart 2",
                              "  label vpn export auto", "  rd vpn export 65010:10001", "  rt vpn both 65010:10001",
                              "  export vpn", "  import vpn"])

    def test_no_nexthop_vpn_export_and_no_egress_maps(self):
        v6 = "\n".join(af(self.out, "e10001", "ipv6"))
        self.assertNotIn("nexthop vpn export", v6)
        self.assertNotIn("route-map vpn", v6)
        # the IPv4 family keeps the IPv4 rule (no next hop with a circuit)
        self.assertNotIn("nexthop vpn export", "\n".join(af(self.out, "e10001", "ipv4")))
        # the network without circuits keeps its IPv4 next hop and gets no IPv6 family
        self.assertIn("  nexthop vpn export 10.255.0.1\n", vrf_block(self.out, "e10000"))
        self.assertIsNone(af(self.out, "e10000", "ipv6"))

    def test_policy(self):
        self.assertIn("! AttachmentCircuit entity-acme/ce1 over IPv6 (link 2001:db8:4005:100::/64, CE 2001:db8:4005:100::3): "
                      "the same rules from the\n! IPv6 lists; to the CE the network's IPv6 placement prefixes\n"
                      "ipv6 prefix-list AC1-ACCEPT6 seq 5 permit 2001:db8:50::/48 le 64\n"
                      "ipv6 prefix-list AC1-DENY6 seq 5 permit 2001:db8:4005:4200::/56 le 128\n"
                      "ipv6 prefix-list AC1-DENY6 seq 10 permit 2001:db8:4005::/52 le 128\n"
                      "ipv6 prefix-list AC1-DENY6 seq 15 permit ::/0\n"
                      "ipv6 prefix-list AC1-OUT6 seq 5 permit 2001:db8:4005:4200::/64 le 128\n!\n"
                      "route-map AC1-IN6 deny 10\n match ipv6 address prefix-list AC1-DENY6\nexit\n!\n"
                      "route-map AC1-IN6 permit 20\n match ipv6 address prefix-list AC1-ACCEPT6\nexit\n!\n"
                      "route-map AC1-OUT6 permit 10\n match ipv6 address prefix-list AC1-OUT6\nexit\n!\n", self.out)
        # the IPv4 lists are untouched: no IPv6 entry in them, no IPv4 entry in the IPv6 ones
        self.assertNotRegex(self.out, r"ip prefix-list \S+ seq \d+ permit [0-9a-f]*:")
        self.assertNotRegex(self.out, r"ipv6 prefix-list AC\S+ seq \d+ permit \d+\.\d+")

    def test_only_additions_against_the_release(self):
        old = base_render(self.v)
        if old is None:
            self.skipTest(f"{BASE} not in this checkout")
        diff = list(difflib.ndiff(old.splitlines(), self.out.splitlines()))
        self.assertEqual([d for d in diff if d.startswith("- ")], [])
        bad = [d[2:] for d in diff if d.startswith("+ ") and not any(re.search(p, d[2:]) for p in ADDED)]
        self.assertEqual(bad, [])

    def test_md5_warning_and_nothing_to_advertise(self):
        v = circuit_only(advertise=[])
        v["hf_node_vrfs"][1]["circuits"][0].update(authentication="md5", maxPrefixAction="warning")
        v["hf_circuit_passwords"] = {"entity-acme/ce1": "dummy-not-a-secret"}
        out = T.render(T.RT, v)
        self.assertIn(f" neighbor {CE6} password dummy-not-a-secret\n", out)
        self.assertEqual(out.count("password dummy-not-a-secret"), 2)
        self.assertIn(f"  neighbor {CE6} maximum-prefix 100 warning-only\n", out)
        self.assertIn("route-map AC1-OUT6 deny 10\nexit\n", out)
        self.assertNotIn("AC1-OUT6 seq", out)
        # no restart: the limit closes the session for good
        v["hf_attach_restart"] = 0
        v["hf_node_vrfs"][1]["circuits"][0]["maxPrefixAction"] = "teardown"
        self.assertIn(f"  neighbor {CE6} maximum-prefix 100\n", T.render(T.RT, v))

    def test_ipv6_only_accept_list_admits_no_ipv4(self):
        """Every accept entry IPv6: the IPv4 session stays and its accept list denies everything, explicitly."""
        v = circuit_only()
        v["hf_node_vrfs"][1]["circuits"][0]["accept"] = []
        out = T.render(T.RT, v)
        self.assertIn("ip prefix-list AC1-ACCEPT seq 5 deny 0.0.0.0/0 le 32\n", out)
        self.assertEqual(len(re.findall(r"^ip prefix-list AC1-ACCEPT ", out, re.M)), 1)
        self.assertIn("route-map AC1-IN permit 20\n match ip address prefix-list AC1-ACCEPT\nexit\n", out)
        # the same guard on the IPv6 list (the planner gives a circuit IPv6 only with IPv6 entries)
        v["hf_node_vrfs"][1]["circuits"][0]["ipv6"]["accept"] = []
        self.assertIn("ipv6 prefix-list AC1-ACCEPT6 seq 5 deny ::/0 le 128\n", T.render(T.RT, v))

    def test_a_second_circuit_without_ipv6_stays_ipv4(self):
        v = circuit_only()
        c2 = dict(T.root_vars()["hf_node_vrfs"][1]["circuits"][0], id="AC2", circuit="entity-acme/ce2", vlan=3001,
                  ce="10.255.128.11")
        v["hf_node_vrfs"][1]["circuits"].append(c2)
        out = T.render(T.RT, v)
        for s in ("AC2-ACCEPT6", "AC2-DENY6", "AC2-OUT6", "AC2-IN6", "description AC26"):
            self.assertNotIn(s, out)
        self.assertFalse(any("10.255.128.11" in ln for ln in af(out, "e10001", "ipv6")))
        self.assertIn("  neighbor 10.255.128.11 activate\n", out)
        # its own lines are those of the release (only the IPv6 lines of AC1 are new)
        old = base_render(v)
        if old is not None:
            removed = [d for d in difflib.ndiff(old.splitlines(), out.splitlines()) if d.startswith("- ")]
            self.assertEqual(removed, [])

    def test_without_ipv6_byte_identical_to_the_same_input(self):
        """Removing the IPv6 sub-dict (the rollback) renders the release's text again."""
        v = circuit_only()
        c = v["hf_node_vrfs"][1]["circuits"][0]
        del c["ipv6"], c["address6"]
        v["hf_node_vrfs"][1]["families"] = [4]
        self.assertEqual(T.render(T.RT, v), T.render(T.RT, T.root_vars(v6=True, families=[4])))

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_ansible_template_module_agrees(self):
        tmp = tempfile.mkdtemp(prefix="shc-frr-render-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dest = os.path.join(tmp, "frr.conf")
        run_play([{"name": "render", "ansible.builtin.template": {"src": T.RT, "dest": dest}}, write_out("'ok'")],
                 self.v)
        with open(dest) as f:
            self.assertEqual(f.read(), self.out)


class TemplateCombined(unittest.TestCase):
    """IPv6 egress and a circuit with IPv6 in the same network VRF."""

    def setUp(self):
        self.v = combined()
        self.out = T.render(T.RT, self.v)
        T.save("root-circuit-ipv6-egress6.conf", self.out)

    def test_family(self):
        v6 = af(self.out, "e10000", "ipv6")
        self.assertEqual(v6, ["  aggregate-address 2001:db8:4005:4200::/64", "  aggregate-address 2001:db8:4005:4201::/64",
                              "  neighbor xe10000 activate", "  neighbor xe10000 route-map E10000-FROM-EG6 in",
                              "  neighbor xe10000 route-map E10000-TO-EG6 out", "  neighbor xe10000 maximum-prefix 10",
                              f"  neighbor {CE6} activate", f"  neighbor {CE6} route-map AC1-IN6 in",
                              f"  neighbor {CE6} route-map AC1-OUT6 out", f"  neighbor {CE6} maximum-prefix 100 restart 2",
                              "  label vpn export auto", "  rd vpn export 65010:10000", "  rt vpn both 65010:10000",
                              "  route-map vpn export E10000-VPN6", "  route-map vpn import E10000-IMPORT6",
                              "  export vpn", "  import vpn"])
        self.assertIn("ipv6 prefix-list AC1-OUT6 seq 10 permit ::/0\n", self.out)
        self.assertIn("to the CE the network's IPv6 placement prefixes and the default route\n", self.out)

    def test_against_the_release_only_the_next_hop_goes(self):
        old = base_render(self.v)
        if old is None:
            self.skipTest(f"{BASE} not in this checkout")
        diff = list(difflib.ndiff(old.splitlines(), self.out.splitlines()))
        self.assertEqual([d[2:] for d in diff if d.startswith("- ")], ["  nexthop vpn export 2001:db8:4005:ffff::1"])
        bad = [d[2:] for d in diff if d.startswith("+ ") and not any(re.search(p, d[2:]) for p in ADDED)]
        self.assertEqual(bad, [])
        # the egress VRF itself is untouched
        self.assertEqual(old.split("router bgp 65011 vrf egress")[1], self.out.split("router bgp 65011 vrf egress")[1])


# ------------------------------------------------------------------------------------------------------------ apply
CIRCUIT6 = dict(CIRCUIT, address6="2001:db8:4005:100::1/64", link6=LINK6, sources6=["2001:db8:50::/48", LINK6],
                egress6=False)
CIRCUIT2 = dict(CIRCUIT, circuit="c2", device="bond1.3002", vlan=3002, address="192.0.2.5/30", link="192.0.2.6",
                nat=True)


def root_conf(*circuits):
    c = dict(copy.deepcopy(ROOT_CONF), **ROOT6)
    c["attachments"]["circuits"] = [copy.deepcopy(x) for x in circuits]
    return c


def nft_check(text):
    """None when nft accepted the text (`nft -c` in a private user and network namespace), else its error; raises
    SkipTest where that can't run here."""
    if not (shutil.which("nft") and shutil.which("unshare")):
        raise unittest.SkipTest("nft or unshare not installed")
    probe = subprocess.run(["unshare", "-rn", "nft", "list", "tables"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise unittest.SkipTest("no unprivileged network namespace here: " + probe.stderr.strip())
    p = subprocess.run(["unshare", "-rn", "nft", "-c", "-f", "-"], input=text, capture_output=True, text=True)
    return None if p.returncode == 0 else p.stderr


class ApplyIPv4Unchanged(ApplyCase):
    """IPv4-only circuits (with and without the IPv6 core): the calls and the final state of the release's script (but
    for the differences listed in test_fix_base_script)."""

    def setUp(self):
        super().setUp()
        base = git_show(SCRIPT_REL)
        if base is None:
            self.skipTest(f"{BASE} not in this checkout")
        self.h.scripts["old"] = self.h.copy_script(base, "apply-old")

    def test_same_calls_and_state(self):
        """(but for the differences the fixes on both transports made, listed in test_fix_base_script)"""
        import test_fix_base_script as B
        for conf in (root_conf(CIRCUIT, CIRCUIT2), dict(root_conf(CIRCUIT), **{k: "" for k in ROOT6})):
            r = A.BaselineTest.run_both(self, conf, [])
            (old_logs, old_state), (new_logs, new_state) = r["old"], r["new"]
            B.assert_runs_like_base(self, "circuits", old_logs, new_logs)
            B.assert_state_like_base(self, "circuits", old_state, new_state)
            self.assertNotIn("ip6 shc_attach6", new_state["ns"][""]["nft"])


class ApplyCircuitIPv6(ApplyCase):
    def up(self, *circuits):
        self.h.conf("root", root_conf(*circuits))
        return self.h.ok(self, "root")

    def test_up_order_and_idempotent(self):
        h = self.h
        out, log = self.up(CIRCUIT6, CIRCUIT2)
        self.keep("root-circuit6-run1", log, out)
        st = h.get_state()["ns"][""]
        # the device: no RAs or SLAAC, IPv6 on, the node's /64 (nodad) next to its link-local
        for k, want in (("accept_ra", "0"), ("autoconf", "0"), ("disable_ipv6", "0")):
            self.assertEqual(h.sysctl("", f"net/ipv6/conf/bond1.3001/{k}"), want, k)
        self.assertEqual(h.addrs6("", "bond1.3001"), ["2001:db8:4005:100::1/64"])
        self.assertTrue(h.has_ll("", "bond1.3001"))
        self.assertTrue(next(a for a in h.link("", "bond1.3001")["addrs"] if a["local"] == "2001:db8:4005:100::1")["nodad"])
        self.assertEqual(h.sysctl("", "net/ipv6/conf/e10001/disable_ipv6"), "0")
        # the IPv4-only circuit keeps IPv6 off
        self.assertEqual(h.sysctl("", "net/ipv6/conf/bond1.3002/disable_ipv6"), "1")
        self.assertEqual(h.addrs6("", "bond1.3002", ll=True), [])
        # the table first (after the modules), then the device's IPv6 sysctls, all before the device comes up (a new
        # device has IPv6 on from the namespace default: disable_ipv6 needs no write, and is never set to 1 here)
        i = index(log, r"modprobe 8021q")
        j = index(log, r"nft -f -", i)
        k_ra = index(log, r"sysctl -qw net/ipv6/conf/bond1\.3001/accept_ra=0", j)
        k_ac = index(log, r"sysctl -qw net/ipv6/conf/bond1\.3001/autoconf=0", j)
        k_up = index(log, r"ip link set bond1\.3001 mtu 1500 up", j)
        self.assertLess(max(k_ra, k_ac), k_up)
        self.assertEqual([l for l in log if "bond1.3001/disable_ipv6=1" in l], [])
        self.assertIn("ip6 shc_attach6", st["nft"])
        self.assertIn("ip shc_attach", st["nft"])
        self.assertTrue(os.path.exists(os.path.join(h.etc, "root.attach6")))
        # a second run changes nothing and writes nothing IPv6
        out2, log2 = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if "shc_attach6" in l or "bond1.3001/" in l and "ipv6" in l
                          or "2001:db8:4005:100::" in l], [])
        # the state snapshot looks at the device's IPv6 and the table
        self.assertTrue([l for l in log2 if re.search(r"ip -br -6 addr show dev bond1\.3001", l)])
        self.assertTrue([l for l in log2 if "nft -s list table ip6 shc_attach6" in l])

    def test_nft_text(self):
        self.up(CIRCUIT6, CIRCUIT2)
        nft = self.h.get_state()["ns"][""]["nft"]["ip6 shc_attach6"]
        self.keep("root-circuit6-nft", nft.splitlines())
        self.assertTrue(nft.startswith("table ip6 shc_attach6\ndelete table ip6 shc_attach6\ntable ip6 shc_attach6 {\n"))
        self.assertIn("  set src_3001 { type ipv6_addr; flags interval; elements = { ::, 2001:db8:50::/48, "
                      "2001:db8:4005:100::/64, fe80::/10 }; }\n", nft)
        self.assertIn('    ct zone set iifname map { "bond1.3001" : 2 }\n', nft)
        self.assertIn('    ct zone set oifname map { "bond1.3001" : 2, "e10001" : 2 }\n', nft)
        self.assertIn('    iifname "bond1.3001" ip6 saddr != @src_3001 counter drop comment "source outside the circuit\'s prefixes"\n', nft)
        nd = "{ nd-router-solicit, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert }"
        self.assertIn(f'    iifname "bond1.3001" ip6 saddr {{ ::, fe80::/10, {LINK6} }} icmpv6 type {nd} accept\n', nft)
        self.assertIn(f'    meta sdifname "bond1.3001" ip6 saddr {LINK6} tcp dport 179 accept\n'
                      f'    meta sdifname "bond1.3001" ip6 saddr {LINK6} tcp sport 179 accept\n', nft)
        self.assertIn('    meta sdifname { "bond1.3001" } udp dport { 3784, 3785 } accept\n', nft)
        self.assertIn('    meta sdifname { "bond1.3001" } counter drop comment "only BGP, BFD and ICMPv6 from a circuit to the node"\n', nft)
        self.assertIn('    meta sdifname { "bond1.3001" } oifname "xe*" counter drop comment "no provider egress for this circuit"\n', nft)
        # the order inside input: neighbour discovery, BGP, then the drop
        self.assertLess(nft.index("nd-neighbor-solicit"), nft.index("tcp dport 179"))
        self.assertLess(nft.index("tcp dport 179"), nft.index('counter drop comment "only BGP'))
        # the IPv4-only circuit is not in it
        self.assertNotIn("bond1.3002", nft)
        err = nft_check(nft)
        self.assertIsNone(err, err)
        # the IPv4 table is that of the same circuits without IPv6
        v4 = self.h.get_state()["ns"][""]["nft"]["ip shc_attach"]
        self.assertIsNone(nft_check(v4))
        self.h.set_state(blade())
        self.h.conf("root", root_conf(CIRCUIT, CIRCUIT2))
        self.h.ok(self, "root")
        self.assertEqual(self.h.get_state()["ns"][""]["nft"]["ip shc_attach"], v4)

    def test_egress6_open_and_overlapping_sources(self):
        c = dict(CIRCUIT6, egress6=True, sources6=["2001:db8:50::/48", "2001:db8:50:7::/64", LINK6])
        self.up(c)
        nft = self.h.get_state()["ns"][""]["nft"]["ip6 shc_attach6"]
        self.assertNotIn('oifname "xe*"', nft)
        # the /64 inside the /48 is merged (an interval set refuses overlapping elements)
        self.assertIn("elements = { ::, 2001:db8:50::/48, 2001:db8:4005:100::/64, fe80::/10 }", nft)
        self.assertIsNone(nft_check(nft))

    def test_sources_any(self):
        """An accept entry ::/0 le 128 collapses sources6 to ::/0 (the planner's form): the set is just that."""
        self.up(dict(CIRCUIT6, sources6=["::/0"]))
        nft = self.h.get_state()["ns"][""]["nft"]["ip6 shc_attach6"]
        self.assertIn("set src_3001 { type ipv6_addr; flags interval; elements = { ::/0 }; }", nft)
        self.assertIsNone(nft_check(nft))

    def test_address_change_and_rollback(self):
        h = self.h
        self.up(CIRCUIT6, CIRCUIT2)
        # a stray global address is replaced by the planned one
        h.add_addr("", "bond1.3001", "2001:db8:4005:1ff::1/64")
        out, _ = self.up(CIRCUIT6, CIRCUIT2)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.assertEqual(h.addrs6("", "bond1.3001"), ["2001:db8:4005:100::1/64"])
        # the IPv6 keys gone (the circuit back to IPv4 only): IPv6 off on the device, the table and its marker gone
        out, log = self.up(CIRCUIT, CIRCUIT2)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.assertEqual(h.sysctl("", "net/ipv6/conf/bond1.3001/disable_ipv6"), "1")
        self.assertEqual(h.addrs6("", "bond1.3001", ll=True), [])
        self.assertNotIn("ip6 shc_attach6", h.get_state()["ns"][""]["nft"])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.attach6")))
        # IPv6 goes off at the device before the table goes
        self.assertLess(index(log, r"bond1\.3001/disable_ipv6=1"), index(log, r"nft delete table ip6 shc_attach6"))
        # and from then on no IPv6 command for the circuits at all
        out, log = self.up(CIRCUIT, CIRCUIT2)
        self.assertTrue(out.strip().endswith("changed=no"), out)
        self.assertEqual([l for l in log if "shc_attach6" in l or re.search(r"-6 addr show dev bond1", l)], [])

    def test_down_and_no_attachments(self):
        h = self.h
        self.up(CIRCUIT6)
        c = root_conf()
        del c["attachments"]
        h.conf("root", c)
        h.ok(self, "root")
        self.assertNotIn("ip6 shc_attach6", h.get_state()["ns"][""]["nft"])
        self.assertNotIn("bond1.3001", h.get_state()["ns"][""]["links"])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.attach6")))
        self.up(CIRCUIT6)
        h.ok(self, "root", "down")
        self.assertNotIn("ip6 shc_attach6", h.get_state()["ns"][""]["nft"])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.attach6")))

    def test_egress_deny_list_gates_only_the_egress(self):
        """The IPv6 egress deny list (denyDestinations6, documentation space in the lab) drops only what leaves the
        network VRF toward the egress VRF (its veth xe<T>): traffic between a placement and a CE never takes that
        device, and the circuits' table has no destination list at all."""
        c = E.root_conf6()
        c["attachments"]["circuits"] = [dict(CIRCUIT6, vrf="e10000", table=110000, zone=1)]
        self.h.conf("root", c)
        self.h.ok(self, "root")
        nft = self.h.get_state()["ns"][""]["nft"]
        deny = [ln.strip() for ln in nft["inet shc_egress6"].splitlines() if "@deny6" in ln or "@plat_deny6" in ln]
        self.assertTrue(deny)
        self.assertTrue(all(ln.startswith('oifname "xe') for ln in deny), deny)
        self.assertNotIn("daddr", nft["ip6 shc_attach6"])

    def test_filter_load_failure_fails_before_ipv6_comes_on(self):
        h = self.h
        h.conf("root", root_conf(CIRCUIT6))
        rc, out, err, log = h.run("root", fail=r"nft -f -")
        self.assertNotEqual(rc, 0)
        self.assertEqual([l for l in writes(log) if "bond1.3001/disable_ipv6=0" in l], [])


# ------------------------------------------------------------------------------------------------- fabric job tasks
@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricJob(unittest.TestCase):
    def plan(self):
        c4 = {"circuit": "acme/ce2", "vrf": "e10002", "network": "acme/n2", "vpnTarget": 10002}
        c6 = {"circuit": "acme/ce1", "vrf": "e10001", "network": "acme/n1", "vpnTarget": 10001,
              "ipv6": dict(copy.deepcopy(V6), advertiseDefault=True)}
        node = lambda c, **x: dict({"circuit": c["circuit"], "device": "bond1.3000", "vrf": c["vrf"]}, **x)
        return {"circuits": [c6, c4], "status": [], "skipped": [], "errors": [],
                "nodes": {"bgw-1": {"circuits": [node(c6, address6="2001:db8:4005:100::1/64", link6=LINK6,
                                                      sources6=V6["sources"]), node(c4)], "vlanPool": [3000, 3099]}},
                "vrfs": {"bgw-1": [{"vrf": "e10000", "ipv6": {"prefixes": []}, "circuits": []},
                                   {"vrf": "e10001", "circuits": [dict(c6, address6="x")]},
                                   {"vrf": "e10002", "circuits": [c4]}]}}

    def test_keep_the_attachment_plan(self):
        task = by_name(load(os.path.join(HF, "main.yml")), "Keep the attachment plan")
        out = run_play([{"ansible.builtin.set_fact": {"fab_st": {}, "fab_real": {"attachments": {}}}}, task,
                        write_out("hf_attach")], {"fab_check_result": self.plan()})
        self.assertEqual([v["families"] for v in out["vrfs"]["bgw-1"]], [[4, 6], [4, 6], [4]])
        c6, c4 = out["nodes"]["bgw-1"]["circuits"]
        self.assertIs(c6["egress6"], True)
        self.assertNotIn("egress6", c4)
        self.assertEqual(c4, self.plan()["nodes"]["bgw-1"]["circuits"][1])
        self.assertEqual(out["nodes"]["bgw-1"]["vlanPool"], [3000, 3099])

    def test_guard_without_the_ipv6_core(self):
        task = by_name(load(os.path.join(HF, "node_root.yml")), "{{ bgw_node.name }}: IPv6 on attachment circuits needs the IPv6 core")
        variables = {"fab_name": "lab", "bgw_node": {"name": "bgw-1"}, "hf_pe": {"loopback6": ""},
                     "hf_node_attach": {"circuits": [{"circuit": "acme/ce1", "address6": "2001:db8:4005:100::1/64"}]}}
        with self.assertRaises(AssertionError) as e:
            run_play([task, write_out("'ok'")], variables)
        self.assertIn("carry IPv6, but the core has no IPv6 loopback", str(e.exception))
        self.assertEqual(run_play([task, write_out("'ok'")], dict(variables, hf_pe={"loopback6": "2001:db8:4005:ffff::1/128"})), "ok")
        self.assertEqual(run_play([task, write_out("'ok'")], dict(variables, hf_node_attach={"circuits": [{"circuit": "c"}]})), "ok")


class NodeVerify(unittest.TestCase):
    """The root's wait task checks a circuit's IPv6 side only for a circuit with IPv6."""

    def render(self, circuit):
        task = by_name(load(os.path.join(HF, "node_root.yml")), "{{ bgw_node.name }}: wait for BGP and IS-IS")
        env = jinja2.Environment(trim_blocks=True, undefined=jinja2.ChainableUndefined)
        v = {"fab_spec": {"domainAsn": 65010}, "fab_real": {"core": {"loopbackPool": "10.255.0.0/24"}},
             "hf_root": {"bridge": "br-core", "link": "10.255.1.1/24"}, "hf_node_egress": {}, "hf_pe": {},
             "bgw_node": {"name": "bgw-1"},
             "hf_node_vrfs": [{"vrf": "e10001", "circuits": [dict({"circuit": "acme/ce1", "device": "bond1.3000",
                                                                   "address": "10.255.128.1", "ce": "10.255.128.3",
                                                                   "peerAsn": 64512, "bfd": True}, **circuit)]}]}
        return env.from_string(task["ansible.builtin.shell"]["cmd"]).render(**v)

    def test_ipv6_checks(self):
        sh = self.render({"ipv6": V6})
        self.assertIn("ip -br -6 addr show dev bond1.3000 | grep -q ' 2001:db8:4005:100::1/'", sh)
        self.assertIn("nft list table ip6 shc_attach6 >/dev/null", sh)
        self.assertIn(f"vtysh -c 'show bgp vrf e10001 neighbors {CE6} json' | grep -q '\"remoteAs\":64512'", sh)
        self.assertNotIn("shc_attach6", self.render({}))
        self.assertNotIn("-6 addr", self.render({}))


# ---------------------------------------------------------------------------------------------------- orphan check
class OrphanNodeRead(unittest.TestCase):
    def test_addresses_and_table(self):
        a = dict(O.ANSWERS, **{"ip -j -6 addr show dev bond1.3001": json.dumps([
            {"ifname": "bond1.3001", "addr_info": [{"family": "inet6", "local": "2001:db8:4005:100::1", "prefixlen": 64},
                                                   {"family": "inet6", "local": "fe80::2:1", "prefixlen": 64}]}]),
            "nft list table ip6 shc_attach6": "table ip6 shc_attach6 {\n}\n"})
        out, calls = O.execute(a)
        self.assertEqual(out["rootDevices"], [{"name": "bond1.3001", "master": "e10001", "addrs6": ["2001:db8:4005:100::1/64"]}])
        self.assertIs(out["rootAttach6"], True)
        self.assertIn(["nft", "list", "table", "ip6", "shc_attach6"], calls)


def report(attachments, node):
    fabric = {"kind": "HybridFabric", "metadata": {"namespace": "sovereign-cloud", "name": "lab"},
              "status": {"realization": {"attachments": attachments}}}
    req = {"crs": {"fabrics": [fabric]}, "nodes": [dict({"fabric": "lab", "node": "bgw-1",
                                                         "netns": {"root": ["e10001"]}}, **node)]}
    return F.orphan_report(req)["findings"]


ATT = {"circuit": "acme/ce1", "vrf": "e10001", "device": "bond1.3001", "nodes": ["bgw-1", "bgw-2"]}
DEV6 = {"name": "bond1.3001", "master": "e10001", "addrs6": ["2001:db8:4005:100::1/64"]}
BFD6 = {"peer": CE6, "vrf": "e10001", "interface": "bond1.3001", "status": "up"}
BFD4 = {"peer": "10.255.128.3", "vrf": "e10001", "interface": "bond1.3001", "status": "up"}


class OrphanReport(unittest.TestCase):
    def test_live_ipv6_circuit_is_no_finding(self):
        self.assertEqual(report([dict(ATT, families=[4, 6])],
                                {"rootDevices": [DEV6], "rootAttach6": True, "rootBfd": [BFD4, BFD6]}), [])

    def test_ipv4_circuit_as_before(self):
        self.assertEqual(report([ATT], {"rootDevices": [dict(DEV6, addrs6=[])], "rootBfd": [BFD4]}), [])
        # a node read from before the IPv6 keys
        self.assertEqual(report([ATT], {"rootDevices": [{"name": "bond1.3001", "master": "e10001"}], "rootBfd": [BFD4]}), [])

    def test_stale_ipv6_pieces(self):
        f = report([ATT], {"rootDevices": [DEV6], "rootAttach6": True, "rootBfd": [BFD4, BFD6]})
        self.assertEqual(f, [
            "node bgw-1: circuit device bond1.3001 holds IPv6 address(es) 2001:db8:4005:100::1/64, but its circuit has "
            "no IPv6 session",
            "node bgw-1: the attachment circuits' IPv6 filter table (ip6 shc_attach6) is loaded, but no circuit on the "
            "node has an IPv6 session",
            f"node bgw-1: BFD session to {CE6} in VRF e10001 (bond1.3001, up) belongs to no IPv6 circuit session"])

    def test_vrf_gone_reported_once(self):
        f = report([], {"netns": {"root": []}, "rootBfd": [BFD6]})
        self.assertEqual(f, [f"node bgw-1: BFD session to {CE6} in VRF e10001 (bond1.3001, up) outlives its VRF"])


if __name__ == "__main__":
    unittest.main()

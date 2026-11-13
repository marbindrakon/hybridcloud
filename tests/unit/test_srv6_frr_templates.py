"""The border gateway FRR templates (frr-gateway.conf.j2, frr-root.conf.j2) per core transport.

- mpls (fab_node_transport undefined or "mpls", any fab_frr_major): byte for byte what the release before the SRv6
  transport rendered (BASE below, read from Git; skipped without it), for every representative input of the existing
  template tests.
- srv6: no MPLS in the configuration (no `mpls enable`, no segment routing in IS-IS, no VPN labels); a locator per
  provider edge in the text form of the node's FRR version, announced by IS-IS as a prefix of the passive lo (the
  apply holds its anchor there): no discard static, prefix-list, route-map or redistribution; the VPN sessions between the IPv6 loopbacks with the extended next hop;
  one SID per VRF; `nexthop vpn export` the IPv6 loopback where the template sets one today and absent where it sets
  none. Every other line (site sessions, EVPN, filters, route-maps, egress and circuit sessions, communities) is the
  same as with mpls.

Renders with jinja2 the way the Ansible template module does (test_frr_templates.render); with ansible-playbook
present one srv6 render per template also runs through the template module. SHC_RENDER_OUT=<dir> writes the srv6
renders there. Documentation and private ranges only; the locators are made-up values inside 5f00::/16."""
import copy
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, run_play, write_out  # noqa: E402
import test_frr_templates as T  # noqa: E402
import test_egress_ipv6 as E  # noqa: E402
import test_root_circuit_ipv6 as C  # noqa: E402

# the release before the SRv6 core transport
BASE = "72f86002"
GW_LOC = "5f00:db8:102::/48"
RT_LOC = "5f00:db8:101::/48"
GW_LO6, RT_LO6 = "2001:db8:4005:ffff::2", "2001:db8:4005:ffff::1"


def git_show(path):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{os.path.relpath(path, ROOT)}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def gw_inputs():
    """Representative gateway inputs of the existing tests."""
    md5 = T.gw_vars(password="dummy", upstream=("192.0.2.30", "203.0.113.30"))
    return {"ipv4 core": T.gw_vars(), "ipv4 core, ipv4 filter": T.gw_vars(v4filter=True),
            "ipv4 core, password and upstream reflectors": md5,
            "ipv4 core, ipv6 family": T.gw_vars(families=(4, 6)),
            "dual-stack core": T.gw_vars(v6=True, families=(4, 6)),
            "dual-stack core, ipv6-only placement": T.gw_vars(v6=True, families=(6,)),
            "dual-stack core, ipv6 egress, ipv4 filter": T.gw_vars(v6=True, families=(4, 6), egress6=True, v4filter=True),
            "dual-stack core, upstream reflectors": T.gw_vars(v6=True, families=(4, 6), upstream=("192.0.2.30", "203.0.113.30"))}


def root_inputs():
    """Representative root inputs of the existing tests."""
    md5 = T.root_vars(v6=True, families=[4])
    md5["hf_node_vrfs"][1]["circuits"][0].update(authentication="md5", maxPrefixAction="warning")
    md5["hf_circuit_passwords"] = {"entity-acme/ce1": "dummy-not-a-secret"}
    return {"ipv4 core, no families": T.root_vars(), "ipv4 core": T.root_vars(families=[4]),
            "dual-stack core": T.root_vars(v6=True, families=[4]),
            "dual-stack core, ipv6-only vrfs": T.root_vars(v6=True, families=[6]),
            "md5 circuit": md5, "ipv6 circuit": C.circuit_only(), "ipv6 egress and ipv6 circuit": C.combined(),
            "ipv6 egress, standby": E.root_vars(standby=True), "ipv6 egress, primary": E.root_vars(standby=False)}


def srv6(variables, major=8):
    v = copy.deepcopy(variables)
    v["fab_node_transport"] = "srv6"
    v["fab_frr_major"] = major
    for k, loc in (("cg_pe", GW_LOC), ("hf_pe", RT_LOC)):
        if k in v:
            v[k]["locator"] = loc
    return v


def gw_srv6(major=8, **kw):
    return srv6(T.gw_vars(v6=True, families=(4, 6), **kw), major)


def block(text, start):
    """The lines of the top-level context that starts with the line `start`, up to its `exit`."""
    lines = text.splitlines()
    i = lines.index(start)
    j = lines.index("exit", i)
    return lines[i:j + 1]


def vrf_block(text, vrf):
    return text.split(f"router bgp 65010 vrf {vrf}\n")[1].split("\nexit\n")[0]


def af(text, vrf, fam):
    m = re.search(rf" address-family {fam} unicast\n(.*?) exit-address-family", vrf_block(text, vrf), re.S)
    return m.group(1).splitlines() if m else None


class MplsUnchanged(unittest.TestCase):
    """mpls, by default or by name, renders what the release rendered, whatever the FRR version."""

    def check(self, path, inputs):
        base = git_show(path)
        for name, v in inputs.items():
            plain = T.render(path, v)
            for extra in ({"fab_node_transport": "mpls"}, {"fab_node_transport": "mpls", "fab_frr_major": 10},
                          {"fab_frr_major": "10"}):
                self.assertEqual(T.render(path, dict(v, **extra)), plain, f"{name} with {extra}")
            if base is not None:
                want = T.render_text(base, v)
                if path == T.RT:
                    # the root's interface neighbours have no description since (frr-reload.py 8.5)
                    want = T.without_interface_descriptions(want)
                self.assertEqual(plain, want, f"{name}: differs from {BASE}")
        if base is None:
            self.skipTest(f"{BASE} not in this checkout")

    def test_gateway(self):
        self.check(T.GW, gw_inputs())

    def test_root(self):
        self.check(T.RT, root_inputs())


# the core transport's lines of a gateway, per transport (comments and the lines that close a block aside)
GW_MPLS_ONLY = [r"^ mpls enable$", r"^interface t5\d+$", r"^ segment-routing (on|global-block \d+ \d+|prefix \S+ index \d+)$",
                r"^  label vpn export auto$",
                r"^ neighbor 10\.255\.0\.1 (remote-as 65010|description border gateway route reflector|update-source lo)$",
                r"^  neighbor 10\.255\.0\.1 activate$"]
GW_SRV6_ONLY = [r"^segment-routing$",
                r"^ srv6$", r"^  encapsulation$", rf"^   source-address {GW_LO6}$", r"^  locators$", r"^   locator MAIN$",
                rf"^    prefix {GW_LOC} block-len 32 node-len 16( func-bits 16)?$", r"^    behavior usid$",
                rf"^ neighbor {RT_LO6} (remote-as 65010|description border gateway route reflector|update-source {GW_LO6}|capability extended-nexthop)$",
                r"^ segment-routing srv6$", r"^  locator MAIN$", rf"^  neighbor {RT_LO6} activate$",
                r"^ sid vpn per-vrf export auto$"]
STRUCTURE = {"!", " !", "exit", " exit", "  exit", "   exit"}


def without(text, patterns, lo6=None):
    """The render's lines outside the core transport: comments, block ends and the given transport lines dropped.
    With lo6 (an mpls render) every `nexthop vpn export` reads as that IPv6 loopback: on srv6 the next hop is set in
    the same places, always to the IPv6 loopback."""
    rx = re.compile("|".join(patterns))
    out = [ln for ln in text.splitlines() if not ln.startswith("!") and ln not in STRUCTURE and not rx.search(ln)]
    return [f"  nexthop vpn export {lo6}" if lo6 and ln.startswith("  nexthop vpn export ") else ln for ln in out]


def transport_lines(text, patterns):
    rx = re.compile("|".join(patterns))
    return [ln for ln in text.splitlines() if rx.search(ln)]


class GatewaySrv6(unittest.TestCase):
    def setUp(self):
        self.out = T.render(T.GW, gw_srv6())
        T.save("gw-srv6-frr8.conf", self.out)

    def test_no_mpls(self):
        for v in (gw_srv6(), gw_srv6(10), gw_srv6(egress6=True, v4filter=True, password="dummy", upstream=("192.0.2.30",))):
            out = T.render(T.GW, v)
            self.assertNotIn("mpls", out)
            self.assertNotRegex(out, r"(?m)^ segment-routing (on|global-block|prefix)")
            self.assertNotIn("label vpn export", out)
            self.assertNotIn("10.255.0.1", out)

    def test_locator_frr8(self):
        self.assertNotRegex(self.out, r"(?m)^ipv6 route ")  # no discard static: the apply blackholes the locator
        self.assertEqual(block(self.out, "segment-routing"),
                         ["segment-routing", " srv6", "  locators", "   locator MAIN",
                          f"    prefix {GW_LOC} block-len 32 node-len 16 func-bits 16", "    behavior usid",
                          "   exit", "  exit", " exit", "exit"])

    def test_locator_frr10(self):
        for major in (10, "10", 11):
            out = T.render(T.GW, gw_srv6(major))
            self.assertEqual(block(out, "segment-routing"),
                             ["segment-routing", " srv6", "  encapsulation", f"   source-address {GW_LO6}", "  exit",
                              "  locators", "   locator MAIN", f"    prefix {GW_LOC} block-len 32 node-len 16",
                              "    behavior usid", "   exit", "  exit", " exit", "exit"], major)
        # FRR 8 when the version is not given
        v = gw_srv6()
        del v["fab_frr_major"]
        self.assertEqual(T.render(T.GW, v), self.out)

    def test_isis_carries_the_locator_alone(self):
        self.assertEqual(block(self.out, "router isis CORE"),
                         ["router isis CORE", " is-type level-2-only", " net 49.0001.0000.0000.0002.00", "exit"])
        # the locator is a prefix of the passive lo (the apply holds its anchor there): nothing redistributed
        self.assertNotIn("CORE-LOCATOR", self.out)
        self.assertNotIn("redistribute", block(self.out, "router isis CORE"))
        self.assertEqual(block(self.out, "interface lo"),
                         ["interface lo", " ip router isis CORE", " ipv6 router isis CORE", " isis passive", "exit"])
        self.assertEqual(block(self.out, "interface core0"),
                         ["interface core0", " ip router isis CORE", " ipv6 router isis CORE", "exit"])
        self.assertNotRegex(self.out, r"(?m)^interface t5")

    def test_session_to_the_root(self):
        bgp = self.out.split("router bgp 65010\n")[1].split("\nexit\n")[0]
        self.assertIn(f" neighbor {RT_LO6} remote-as 65010\n neighbor {RT_LO6} description border gateway route reflector\n"
                      f" neighbor {RT_LO6} update-source {GW_LO6}\n neighbor {RT_LO6} capability extended-nexthop\n !\n"
                      " segment-routing srv6\n  locator MAIN\n exit\n", bgp)
        self.assertIn(f" address-family ipv4 vpn\n  neighbor {RT_LO6} activate\n exit-address-family\n !\n"
                      f" address-family ipv6 vpn\n  neighbor {RT_LO6} activate\n exit-address-family", bgp)
        # no timers on the session, as today; the root session never had a password
        self.assertNotRegex(bgp, rf"neighbor {re.escape(RT_LO6)} (timers|password|bfd)")

    def test_site_sessions_keep_their_settings(self):
        out = T.render(T.GW, gw_srv6(password="dummy", upstream=("192.0.2.30",)))
        for ln in (" neighbor SITE password dummy", " neighbor SITE bfd", " neighbor UPSTREAM password dummy",
                   " neighbor UPSTREAM update-source 192.0.2.4", "  neighbor SITE soft-reconfiguration inbound",
                   "  neighbor UPSTREAM route-map FROM-SITE in", "  advertise-all-vni"):
            self.assertIn(ln + "\n", out)

    def test_vrfs(self):
        t51000 = vrf_block(self.out, "t51000")
        self.assertIn(" bgp router-id 192.0.2.4\n no bgp default ipv4-unicast\n sid vpn per-vrf export auto\n !\n", t51000)
        self.assertEqual(af(self.out, "t51000", "ipv4"),
                         ["  rd vpn export 65010:51000", "  rt vpn both 65010:10000", f"  nexthop vpn export {GW_LO6}",
                          "  route-map vpn export TO-VPN-10000", "  export vpn", "  import vpn"])
        self.assertEqual(af(self.out, "t51000", "ipv6"),
                         ["  network ::/0 route-map GW-LOCAL", "  rd vpn export 65010:51000", "  rt vpn both 65010:10000",
                          f"  nexthop vpn export {GW_LO6}", "  route-map vpn export TO-VPN-10000", "  export vpn", "  import vpn"])
        self.assertIn(" sid vpn per-vrf export auto\n", vrf_block(self.out, "t51003"))
        self.assertEqual(self.out.count("sid vpn per-vrf export auto"), 2)

    def test_only_the_transport_differs(self):
        for kw in ({}, {"egress6": True, "v4filter": True}, {"password": "dummy", "upstream": ("192.0.2.30", "203.0.113.30")},
                   {"families": (6,)}):
            v = T.gw_vars(v6=True, **dict({"families": (4, 6)}, **kw))
            mpls = T.render(T.GW, v)
            for major in (8, 10):
                new = T.render(T.GW, srv6(v, major))
                # every other line, in the same order
                self.assertEqual(without(new, GW_SRV6_ONLY), without(mpls, GW_MPLS_ONLY, GW_LO6), (kw, major))
                # and no srv6 line in the mpls render, no mpls line in the srv6 render
                self.assertEqual(transport_lines(mpls, GW_SRV6_ONLY), [], kw)
                self.assertEqual(transport_lines(new, GW_MPLS_ONLY), [], kw)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_ansible_template_module_agrees(self):
        dest = os.path.join(tempfile.mkdtemp(prefix="shc-frr-render-"), "frr.conf")
        variables = gw_srv6(10)
        variables["fab_frr_major"] = "10"
        run_play([{"name": "render", "ansible.builtin.template": {"src": T.GW, "dest": dest}}, write_out("'ok'")], variables)
        with open(dest) as f:
            self.assertEqual(f.read(), T.render(T.GW, gw_srv6(10)))


# the core transport's lines of the root, per transport (comments and the lines that close a block aside)
RT_MPLS_ONLY = [r"^ mpls enable$", r"^ segment-routing (on|global-block \d+ \d+|prefix \S+ index \d+)$", r"^  label vpn export auto$",
                r"^ neighbor PE update-source lo$", r"^ bgp listen range 10\.255\.0\.0/24 peer-group PE$"]
RT_SRV6_ONLY = [r"^segment-routing$", r"^ srv6$", r"^  encapsulation$", rf"^   source-address {RT_LO6}$",
                r"^  locators$", r"^   locator MAIN$", rf"^    prefix {RT_LOC} block-len 32 node-len 16( func-bits 16)?$",
                r"^    behavior usid$", rf"^ neighbor PE (update-source {RT_LO6}|capability extended-nexthop)$",
                r"^ bgp listen range 2001:db8:4005:ffff::/64 peer-group PE$", r"^ segment-routing srv6$",
                r"^  locator MAIN$", r"^ sid vpn per-vrf export auto$"]


class RootSrv6(unittest.TestCase):
    def setUp(self):
        self.out = T.render(T.RT, srv6(C.combined()))
        T.save("root-srv6-frr8.conf", self.out)

    def test_no_mpls(self):
        for name, v in root_inputs().items():
            if not v["hf_pe"]["loopback6"]:
                continue  # srv6 needs the IPv6 core
            for major in (8, 10):
                out = T.render(T.RT, srv6(v, major))
                self.assertNotIn("mpls", out, name)
                self.assertNotRegex(out, r"(?m)^ segment-routing (on|global-block|prefix)", name)
                self.assertNotIn("label vpn export", out, name)
                self.assertNotIn("listen range 10.255.0.0/24", out, name)

    def test_locator(self):
        self.assertNotIn(f"ipv6 route {RT_LOC}", self.out)
        self.assertNotIn("CORE-LOCATOR", self.out)
        self.assertEqual(block(self.out, "segment-routing"),
                         ["segment-routing", " srv6", "  locators", "   locator MAIN",
                          f"    prefix {RT_LOC} block-len 32 node-len 16 func-bits 16", "    behavior usid",
                          "   exit", "  exit", " exit", "exit"])
        self.assertEqual(block(T.render(T.RT, srv6(C.combined(), 10)), "segment-routing"),
                         ["segment-routing", " srv6", "  encapsulation", f"   source-address {RT_LO6}", "  exit",
                          "  locators", "   locator MAIN", f"    prefix {RT_LOC} block-len 32 node-len 16",
                          "    behavior usid", "   exit", "  exit", " exit", "exit"])

    def test_isis_carries_the_locator_alone(self):
        # nothing redistributed: the locator is a prefix of the passive lo (the apply holds its anchor there)
        self.assertEqual(block(self.out, "router isis CORE"),
                         ["router isis CORE", " is-type level-2-only", " net 49.0001.0000.0000.0001.00", "exit"])
        self.assertEqual(block(self.out, "interface br-core"),
                         ["interface br-core", " ip router isis CORE", " ipv6 router isis CORE", " isis priority 127", "exit"])
        self.assertEqual(block(self.out, "interface lo"),
                         ["interface lo", " ip router isis CORE", " ipv6 router isis CORE", " isis passive", "exit"])

    def test_reflector_and_pe_sessions(self):
        bgp = self.out.split("router bgp 65010\n")[1].split("\nexit\n")[0]
        self.assertIn(" neighbor PE peer-group\n neighbor PE remote-as 65010\n"
                      f" neighbor PE update-source {RT_LO6}\n neighbor PE capability extended-nexthop\n"
                      " bgp listen range 2001:db8:4005:ffff::/64 peer-group PE\n !\n segment-routing srv6\n  locator MAIN\n exit\n", bgp)
        self.assertIn(" address-family ipv4 vpn\n  neighbor PE activate\n  neighbor PE route-reflector-client\n exit-address-family\n", bgp)
        self.assertIn(" address-family ipv6 vpn\n  neighbor PE activate\n  neighbor PE route-reflector-client\n"
                      "  neighbor PE route-map SHC-RR-IN6 in\n exit-address-family", bgp)
        self.assertNotRegex(bgp, r"neighbor PE (timers|password|bfd)")
        self.assertIn(" bgp cluster-id 10.255.0.1\n", bgp)

    def test_circuit_vrf_sets_no_next_hop(self):
        # the network with a circuit (and the IPv6 egress): no nexthop vpn export in either family, as today
        for fam in ("ipv4", "ipv6"):
            self.assertFalse([ln for ln in af(self.out, "e10000", fam) if "nexthop" in ln], fam)
        self.assertIn(" no bgp default ipv4-unicast\n sid vpn per-vrf export auto\n neighbor xe10000 interface v6only remote-as external\n",
                      vrf_block(self.out, "e10000"))
        # the circuit-only network keeps the same rule
        out = T.render(T.RT, srv6(C.circuit_only()))
        for fam in ("ipv4", "ipv6"):
            self.assertFalse([ln for ln in af(out, "e10001", fam) if "nexthop" in ln], fam)
        self.assertIn(" sid vpn per-vrf export auto\n", vrf_block(out, "e10001"))
        # an md5 circuit keeps its password and timers
        v = root_inputs()["md5 circuit"]
        out = T.render(T.RT, srv6(v))
        self.assertIn(" neighbor 10.255.128.3 password dummy-not-a-secret\n neighbor 10.255.128.3 timers 10 30\n", out)
        self.assertIn("  neighbor 10.255.128.3 maximum-prefix 100 warning-only\n", out)

    def test_vrf_without_circuit_uses_the_ipv6_loopback(self):
        out = T.render(T.RT, srv6(T.root_vars(v6=True, families=[4])))
        self.assertEqual(af(out, "e10000", "ipv4"),
                         ["  redistribute static route-map SHC-EGRESS-DEFAULT", "  rd vpn export 65010:10000",
                          "  rt vpn both 65010:10000", f"  nexthop vpn export {RT_LO6}", "  export vpn", "  import vpn"])
        # the IPv6 egress VRF without circuits: its explicit next hop, the IPv6 loopback, as today
        out = T.render(T.RT, srv6(E.root_vars(standby=True)))
        self.assertIn(f"  nexthop vpn export {RT_LO6}\n", "\n".join(af(out, "e10000", "ipv6")) + "\n")
        self.assertIn(f"  nexthop vpn export {RT_LO6}\n", "\n".join(af(out, "e10000", "ipv4")) + "\n")
        self.assertNotIn("nexthop vpn export 10.255.0.1", out)

    def test_egress_vrf_has_no_sid(self):
        egress = self.out.split("router bgp 65011 vrf egress\n")[1].split("\nexit\n")[0]
        self.assertNotIn("sid vpn", egress)
        self.assertNotIn("vpn", egress)
        # a VRF that exports nothing to the VPN gets no SID
        v = srv6(T.root_vars(v6=True, families=[6]))
        self.assertNotIn("sid vpn", T.render(T.RT, v))

    def test_only_the_transport_differs(self):
        for name, v in root_inputs().items():
            if not v["hf_pe"]["loopback6"]:
                continue  # srv6 needs the IPv6 core
            mpls = T.render(T.RT, v)
            for major in (8, 10):
                new = T.render(T.RT, srv6(v, major))
                self.assertEqual(without(new, RT_SRV6_ONLY), without(mpls, RT_MPLS_ONLY, RT_LO6), (name, major))
                self.assertEqual(transport_lines(mpls, RT_SRV6_ONLY), [], name)
                self.assertEqual(transport_lines(new, RT_MPLS_ONLY), [], name)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_ansible_template_module_agrees(self):
        dest = os.path.join(tempfile.mkdtemp(prefix="shc-frr-render-"), "frr.conf")
        variables = srv6(C.combined())
        run_play([{"name": "render", "ansible.builtin.template": {"src": T.RT, "dest": dest}}, write_out("'ok'")], variables)
        with open(dest) as f:
            self.assertEqual(f.read(), self.out)


if __name__ == "__main__":
    unittest.main()

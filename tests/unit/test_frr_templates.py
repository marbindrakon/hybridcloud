"""The border gateway FRR templates (cloudgateway_provision frr-gateway.conf.j2, hybridfabric_provision
frr-root.conf.j2) with and without the core's IPv6 transport.

- IPv4-only core (no IPv6 loopback, every VRF IPv4 only): the root renders byte for byte what the last release
  rendered; the gateway too, except the per-network TO-VPN-<rt> export maps and the IPv6 site-import entries of
  FROM-SITE (neutral for IPv4 routes).
- Dual-stack core: IPv6 loopbacks in IS-IS with their own prefix SID, the IPv6 VPN address family on the sessions to
  the route reflector, per VRF of a placement with IPv6 the IPv6 unicast family with the IPv6 loopback as VPN next
  hop, the placements' IPv6 prefixes in the site-import filter; the IPv6 egress standby entry of GW-T5 only with
  IPv6 egress on.

Renders with jinja2 the way the Ansible template module does (trim_blocks, trailing newline kept); with
ansible-playbook present, one render also runs through the template module to prove the two agree. The release to
compare with is read from Git (BASE below); without it those comparisons are skipped. SHC_RENDER_OUT=<dir> writes
the rendered files there. Documentation and private ranges only."""
import copy
import difflib
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, run_play, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
GW = os.path.join(ROLES, "cloudgateway_provision", "templates", "frr-gateway.conf.j2")
RT = os.path.join(ROLES, "hybridfabric_provision", "templates", "frr-root.conf.j2")
# the release before the core's IPv6 transport
BASE = "fd6432b"
# the release before the site-import filter's per-prefix sequence numbers
BASE_SEQ = "7c23801"
OUT = os.environ.get("SHC_RENDER_OUT", "")


def to_bool(v):
    """Ansible's bool filter."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "on", "1", "true", "y")


def to_hash(data, hashtype="sha1"):
    """Ansible's hash filter."""
    return hashlib.new(hashtype, str(data).encode()).hexdigest()


def seq_of(prefix):
    """The site-import filter's sequence number of a prefix (the template's rule, no collision)."""
    return int(to_hash(prefix, "sha256")[:8], 16) % 3999999991 + 10


def env():
    e = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    e.filters["bool"] = to_bool
    e.filters["hash"] = to_hash
    return e


def render_text(text, variables):
    return env().from_string(text).render(**variables)


def render(path, variables):
    with open(path) as f:
        return render_text(f.read(), variables)


def base_text(path):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{os.path.relpath(path, ROOT)}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def save(name, text):
    if OUT:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, name), "w") as f:
            f.write(text)


REAL = {"gatewayCommunity": 999,
        "core": {"bridge": "br-core", "loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24",
                 "srgb": {"start": 16000, "end": 23999}, "loopbackPool6": "", "sidOffset6": 1000, "icmpSource6": ""},
        "gateway": {"siteImportFilter": {"ipv4": False, "ipv6": True}},
        "egress": {"enabled": True, "ipv6": {"enabled": False, "standbyCommunity": 998, "standbyLocalPref": 50}}}


def pe(index, v6):
    out = {"index": index, "loopback": f"10.255.0.{index}/32", "loopbackIp": f"10.255.0.{index}",
           "net": f"49.0001.0000.0000.{index:04x}.00", "sid": 16000 + index,
           "loopback6": "", "loopbackIp6": "", "sid6": 0, "icmp6": ""}
    if v6:
        out.update({"loopback6": f"2001:db8:4005:ffff::{index:x}/128", "loopbackIp6": f"2001:db8:4005:ffff::{index:x}",
                    "sid6": 17000 + index})
    return out


def gw_vars(v6=False, families=(4,), egress6=False, v4filter=False, password="", upstream=()):
    real = copy.deepcopy(REAL)
    if v6:
        real["core"].update(loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
    real["egress"]["ipv6"]["enabled"] = egress6
    real["gateway"]["siteImportFilter"]["ipv4"] = v4filter
    fam = list(families)
    vrfs = [{"placement": "entity-acme/core-site1", "vrf": "t51000", "vni": 51000, "vpnTarget": 10000, "mtu": 9000,
             "families": fam, "prefixes4": ["10.60.1.0/24"],
             "prefixes6": ["2001:db8:100:1::/64"] if 6 in fam else []},
            {"placement": "entity-acme/batch-site1", "vrf": "t51003", "vni": 51003, "vpnTarget": 10001, "mtu": 9000,
             "families": [4], "prefixes4": ["10.61.1.0/24"], "prefixes6": []}]
    return {"fab_name": "site1", "cg_site_name": "site1", "bgw_node": {"name": "bgw-1"},
            "cg_fabric": {"spec": {"domainAsn": 65010}},
            "fab_spec": {"siteUnderlay": {"cidr": "192.0.2.0/24", "gateway": "192.0.2.1",
                                          "upstreamRouteReflectors": list(upstream)}, "bfd": True},
            "fab_real": real, "cg_vrfs": vrfs, "cg_upstream_remote": [r for r in upstream if not r.startswith("192.0.2.")],
            "cg_pe": pe(2, v6), "cg_root_pe": pe(1, v6), "cg_node_addr": "192.0.2.4", "cg_bgp_password": password}


def root_vars(v6=False, families=None):
    real = copy.deepcopy(REAL)
    if v6:
        real["core"].update(loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
    egress = {"vrf": "egress", "gateway": "198.51.100.1", "platformSources": ["198.51.100.128/25"],
              "networks": [{"network": "entity-acme/core", "vrf": "e10000", "vpnTarget": 10000, "nat": "198.51.100.64",
                            "xeIp": "169.254.0.1", "xoIp": "169.254.0.0", "internet": True, "platform": []}]}
    vrf = {"network": "entity-acme/core", "vrf": "e10000", "vpnTarget": 10000, "rd": "65010:10000",
           "nat": "198.51.100.64", "platform": [], "egress": True, "circuits": []}
    circuit = {"id": "AC1", "circuit": "entity-acme/ce1", "vlan": 3000, "ce": "10.255.128.3", "peerAsn": 64512,
               "accept": ["203.0.113.0/24"], "deny": ["10.60.0.0/16"], "advertise": ["10.60.1.0/24"],
               "advertiseDefault": False, "authentication": "none", "bfd": True, "maxPrefixes": 100,
               "maxPrefixAction": "teardown"}
    vrf2 = {"network": "entity-acme/batch", "vrf": "e10001", "vpnTarget": 10001, "rd": "65010:10001", "nat": "",
            "platform": [], "egress": False, "circuits": [circuit]}
    vrfs = [vrf, vrf2]
    if families is not None:
        vrfs = [dict(v, families=list(families)) for v in vrfs]
    return {"fab_name": "lab", "bgw_node": {"name": "bgw-1"}, "hf_root": {"bridge": "br-core"},
            "fab_spec": {"domainAsn": 65010}, "fab_real": real, "hf_pe": pe(1, v6), "hf_node_egress": egress,
            "hf_node_vrfs": vrfs, "hf_circuit_passwords": {}, "hf_attach_restart": 2}


# the only lines in which the gateway's IPv4-only render may differ from the release's
GW_ALLOWED = [r"^!", r"^route-map (FROM-SITE|TO-VPN)\b", r"^ match (community GW-ORIG|ipv6 address prefix-list (SITE|ANY)-V6)$",
              r"^ set extcommunity (none|rt \d+:\d+)$", r"^exit$", r"^ipv6 prefix-list (SITE|ANY)-V6 ",
              r"^  route-map vpn export TO-VPN(-\d+)?$"]


def changed_lines(a, b):
    return [ln[2:] for ln in difflib.ndiff(a.splitlines(), b.splitlines()) if ln[:2] in ("- ", "+ ")]


def section(text, start):
    """The lines of every block that starts with `start` (up to its exit)."""
    out, on = [], False
    for ln in text.splitlines():
        if ln.startswith(start):
            on = True
        if on:
            out.append(ln)
            if ln == "exit":
                on = False
    return out


class GatewayIPv4Core(unittest.TestCase):
    def setUp(self):
        self.base = base_text(GW)

    def compare(self, variables, name):
        new = render(GW, variables)
        if name:
            save(name, new)
        if self.base is None:
            self.skipTest(f"{BASE} not in this checkout")
        old = render_text(self.base, variables)
        if name:
            save(name.replace(".conf", f".{BASE}.conf"), old)
        bad = [ln for ln in changed_lines(old, new) if not any(re.search(p, ln) for p in GW_ALLOWED)]
        self.assertEqual(bad, [], "lines changed outside TO-VPN and FROM-SITE")
        return new

    def test_same_as_release_except_policy(self):
        new = self.compare(gw_vars(), "gw-ipv4-core.conf")
        self.assertNotIn("ipv6 router isis", new)
        self.assertNotIn("ipv6 vpn", new)
        self.assertNotIn("address-family ipv6 unicast", new)
        self.assertNotIn("advertise ipv6", new)
        self.assertNotIn("EGRESS-STANDBY", new)
        self.assertNotIn("route-map TO-VPN permit", new)

    def test_same_with_upstream_reflectors_and_password(self):
        # not written out: the render holds a (dummy) session password
        self.compare(gw_vars(password="dummy", upstream=("192.0.2.30", "203.0.113.30")), None)

    def test_ipv6_family_without_ipv6_core_renders_ipv4_only(self):
        new = render(GW, gw_vars(families=(4, 6)))
        self.assertNotIn("address-family ipv6 unicast", new)
        self.assertNotIn("advertise ipv6", new)
        # the placement's IPv6 prefix is still admitted from the site (the filter does not depend on the core)
        self.assertIn(f"ipv6 prefix-list SITE-V6 seq {seq_of('2001:db8:100:1::/64')} permit 2001:db8:100:1::/64 le 128", new)

    def test_policy_section(self):
        new = render(GW, gw_vars())
        self.assertEqual(section(new, "route-map FROM-SITE"),
                         ["route-map FROM-SITE deny 10", " match community GW-ORIG", "exit",
                          "route-map FROM-SITE permit 20", " match ipv6 address prefix-list SITE-V6", "exit",
                          "route-map FROM-SITE deny 30", " match ipv6 address prefix-list ANY-V6", "exit",
                          "route-map FROM-SITE permit 40", "exit"])
        self.assertIn("ipv6 prefix-list SITE-V6 seq 5 deny ::/0 le 128\nipv6 prefix-list ANY-V6 seq 5 permit ::/0 le 128\n", new)
        self.assertNotIn("SITE-V4", new)
        self.assertEqual(section(new, "route-map TO-VPN"),
                         ["route-map TO-VPN-10000 permit 10", " set extcommunity none", " set extcommunity rt 65010:10000", "exit",
                          "route-map TO-VPN-10001 permit 10", " set extcommunity none", " set extcommunity rt 65010:10001", "exit"])
        self.assertIn("  route-map vpn export TO-VPN-10000\n", new)
        self.assertIn("  route-map vpn export TO-VPN-10001\n", new)

    def test_filter_off(self):
        v = gw_vars()
        v["fab_real"]["gateway"]["siteImportFilter"]["ipv6"] = False
        new = render(GW, v)
        self.assertNotIn("prefix-list", new)
        self.assertEqual(section(new, "route-map FROM-SITE"),
                         ["route-map FROM-SITE deny 10", " match community GW-ORIG", "exit", "route-map FROM-SITE permit 40", "exit"])

    def test_ipv4_filter(self):
        new = render(GW, gw_vars(v4filter=True))
        self.assertIn(f"ip prefix-list SITE-V4 seq {seq_of('10.60.1.0/24')} permit 10.60.1.0/24 le 32\n"
                      f"ip prefix-list SITE-V4 seq {seq_of('10.61.1.0/24')} permit 10.61.1.0/24 le 32\n", new)
        self.assertIn("ip prefix-list ANY-V4 seq 5 permit 0.0.0.0/0 le 32\n", new)
        self.assertEqual([ln for ln in section(new, "route-map FROM-SITE") if ln.startswith("route-map")],
                         ["route-map FROM-SITE deny 10", "route-map FROM-SITE permit 20", "route-map FROM-SITE permit 22",
                          "route-map FROM-SITE deny 30", "route-map FROM-SITE deny 32", "route-map FROM-SITE permit 40"])
        self.assertIn("route-map FROM-SITE permit 22\n match ip address prefix-list SITE-V4\nexit\n", new)

    def test_no_vrfs(self):
        v = gw_vars()
        v["cg_vrfs"] = []
        new = render(GW, v)
        self.assertNotIn("TO-VPN", new)
        self.assertIn("ipv6 prefix-list SITE-V6 seq 5 deny ::/0 le 128", new)

    def test_status_without_families(self):
        v = gw_vars()
        for x in v["cg_vrfs"]:
            del x["families"], x["prefixes4"], x["prefixes6"]
        self.assertEqual(render(GW, v), render(GW, gw_vars()))


class GatewayDualStackCore(unittest.TestCase):
    def setUp(self):
        self.out = render(GW, gw_vars(v6=True, families=(4, 6)))
        save("gw-dual-stack-core.conf", self.out)

    def test_isis(self):
        self.assertIn("interface lo\n ip router isis CORE\n ipv6 router isis CORE\n isis passive\n mpls enable\nexit\n", self.out)
        self.assertIn("interface core0\n ip router isis CORE\n ipv6 router isis CORE\n mpls enable\nexit\n", self.out)
        self.assertIn(" segment-routing prefix 10.255.0.2/32 index 2\n"
                      " segment-routing prefix 2001:db8:4005:ffff::2/128 index 1002\nexit\n", self.out)

    def test_ipv6_vpn_to_the_reflector(self):
        self.assertIn(" address-family ipv4 vpn\n  neighbor 10.255.0.1 activate\n exit-address-family\n !\n"
                      " address-family ipv6 vpn\n  neighbor 10.255.0.1 activate\n exit-address-family\nexit\n", self.out)

    def test_vrf_families(self):
        t51000 = self.out.split("router bgp 65010 vrf t51000\n")[1].split("\nexit\n")[0]
        self.assertIn(" address-family ipv4 unicast\n  label vpn export auto\n  rd vpn export 65010:51000\n"
                      "  rt vpn both 65010:10000\n  nexthop vpn export 10.255.0.2\n  route-map vpn export TO-VPN-10000\n", t51000)
        self.assertIn(" address-family ipv6 unicast\n  network ::/0\n  label vpn export auto\n  rd vpn export 65010:51000\n"
                      "  rt vpn both 65010:10000\n  nexthop vpn export 2001:db8:4005:ffff::2\n"
                      "  route-map vpn export TO-VPN-10000\n  export vpn\n  import vpn\n exit-address-family\n", t51000)
        self.assertIn("  advertise ipv4 unicast route-map GW-T5\n  advertise ipv6 unicast route-map GW-T5\n", t51000)
        t51003 = self.out.split("router bgp 65010 vrf t51003\n")[1].split("\nexit\n")[0]
        self.assertNotIn("ipv6", t51003)

    def test_site_v6_list(self):
        self.assertIn(f"ipv6 prefix-list SITE-V6 seq {seq_of('2001:db8:100:1::/64')} permit 2001:db8:100:1::/64 le 128\n"
                      "ipv6 prefix-list ANY-V6 seq 5 permit ::/0 le 128\n", self.out)
        self.assertNotIn("deny ::/0", self.out)

    def test_no_standby_entry(self):
        self.assertNotIn("EGRESS-STANDBY", self.out)
        self.assertNotIn("route-map GW-T5 permit 5", self.out)

    def test_ipv6_only_placement(self):
        v = gw_vars(v6=True, families=(6,))
        out = render(GW, v)
        t51000 = out.split("router bgp 65010 vrf t51000\n")[1].split("\nexit\n")[0]
        self.assertNotIn("address-family ipv4 unicast", t51000)
        self.assertNotIn("advertise ipv4", t51000)
        self.assertIn("address-family ipv6 unicast", t51000)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_ansible_template_module_agrees(self):
        tmp = tempfile.mkdtemp(prefix="shc-frr-render-")
        dest = os.path.join(tmp, "frr.conf")
        variables = gw_vars(v6=True, families=(4, 6))
        run_play([{"name": "render", "ansible.builtin.template": {"src": GW, "dest": dest}}, write_out("'ok'")],
                 variables)
        with open(dest) as f:
            self.assertEqual(f.read(), self.out)


class SiteFilterSequences(unittest.TestCase):
    """SITE-V6 / SITE-V4 entries carry a sequence number derived from their prefix: another placement's prefix coming
    or going never renumbers an entry (a reload would delete and re-add it and could flap that tenant's routes)."""

    P1, P2, P3 = "2001:db8:100:1::/64", "2001:db8:100:2::/64", "2001:db8:200::/64"

    @staticmethod
    def lines(text, name):
        return [ln for ln in text.splitlines() if ln.startswith(f"ip{'v6' if name.endswith('V6') else ''} prefix-list {name} ")]

    def with_prefixes(self, p6, p4=("10.60.1.0/24",)):
        v = gw_vars(v6=True, families=(4, 6), v4filter=True)
        v["cg_vrfs"] = [{"placement": f"entity-acme/p{i}", "vrf": f"t{51000 + i}", "vni": 51000 + i, "vpnTarget": 10000,
                         "mtu": 9000, "families": [4, 6], "prefixes4": list(p4[i:i + 1]), "prefixes6": [q]}
                        for i, q in enumerate(p6)]
        return render(GW, v)

    def test_seq_is_the_prefix_hash(self):
        out = self.with_prefixes([self.P1])
        self.assertEqual(self.lines(out, "SITE-V6"), [f"ipv6 prefix-list SITE-V6 seq {seq_of(self.P1)} permit {self.P1} le 128"])
        self.assertEqual(seq_of(self.P1), int(hashlib.sha256(self.P1.encode()).hexdigest()[:8], 16) % 3999999991 + 10)
        self.assertTrue(10 <= seq_of(self.P1) <= 4000000000)
        # the catch-all entries keep their numbers
        self.assertIn("ipv6 prefix-list ANY-V6 seq 5 permit ::/0 le 128\n", out)
        self.assertIn("ip prefix-list ANY-V4 seq 5 permit 0.0.0.0/0 le 32\n", out)

    def test_seq_unchanged_when_another_prefix_comes_or_goes(self):
        one = self.lines(self.with_prefixes([self.P2]), "SITE-V6")
        two = self.lines(self.with_prefixes([self.P2, self.P1]), "SITE-V6")
        three = self.lines(self.with_prefixes([self.P3, self.P2, self.P1]), "SITE-V6")
        self.assertEqual(len(one), 1)
        self.assertEqual(set(one) < set(two) < set(three), True, (one, two, three))
        # the same entries whatever the order of the VRFs, and after a removal
        self.assertEqual(three, self.lines(self.with_prefixes([self.P1, self.P2, self.P3]), "SITE-V6"))
        self.assertEqual(two, self.lines(self.with_prefixes([self.P1, self.P2]), "SITE-V6"))
        # IPv4 the same way
        v4a = self.lines(self.with_prefixes([self.P1], p4=["10.60.1.0/24"]), "SITE-V4")
        v4b = self.lines(self.with_prefixes([self.P1, self.P2], p4=["10.60.1.0/24", "10.60.2.0/24"]), "SITE-V4")
        self.assertTrue(set(v4a) < set(v4b), (v4a, v4b))

    def test_sequences_are_distinct(self):
        out = self.with_prefixes([self.P1, self.P2, self.P3], p4=["10.60.1.0/24", "10.60.2.0/24", "10.60.3.0/24"])
        for name in ("SITE-V6", "SITE-V4"):
            seqs = [int(ln.split()[4]) for ln in self.lines(out, name)]
            self.assertEqual(len(seqs), 3)
            self.assertEqual(len(set(seqs)), 3, seqs)

    def test_collision_takes_the_next_free_number(self):
        # every prefix hashing alike (a hash filter that answers 0x64 = 100): the next free numbers, in prefix order
        e = env()
        e.filters["hash"] = lambda data, hashtype="sha1": "00000064" + "0" * 56
        with open(GW) as f:
            text = f.read()
        v = gw_vars(v6=True, families=(4, 6), v4filter=True)
        v["cg_vrfs"][0]["prefixes6"] = [self.P2, self.P1]
        v["cg_vrfs"][1]["prefixes6"] = [self.P3]
        out = e.from_string(text).render(**v)
        self.assertEqual(self.lines(out, "SITE-V6"),
                         [f"ipv6 prefix-list SITE-V6 seq 110 permit {self.P1} le 128",
                          f"ipv6 prefix-list SITE-V6 seq 111 permit {self.P2} le 128",
                          f"ipv6 prefix-list SITE-V6 seq 112 permit {self.P3} le 128"])
        self.assertEqual(self.lines(out, "SITE-V4"),
                         ["ip prefix-list SITE-V4 seq 110 permit 10.60.1.0/24 le 32",
                          "ip prefix-list SITE-V4 seq 111 permit 10.61.1.0/24 le 32"])

    def test_otherwise_identical_to_the_release_before(self):
        """With the release's inputs only the SITE-V4/SITE-V6 permit lines (and comments) differ from what it rendered."""
        try:
            p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE_SEQ}:{os.path.relpath(GW, ROOT)}"],
                               capture_output=True, text=True, timeout=30)
            old = p.stdout if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            old = None
        if old is None:
            self.skipTest(f"{BASE_SEQ} not in this checkout")
        # network ::/0: an IPv6 VRF announces the unreachable default to its site while the fabric has no IPv6 egress
        allowed = [r"^!", r"^ipv6 prefix-list SITE-V6 seq \d+ permit ", r"^ip prefix-list SITE-V4 seq \d+ permit ",
                   r"^  network ::/0$"]
        for variables in (gw_vars(), gw_vars(v4filter=True), gw_vars(v6=True, families=(4, 6)),
                          gw_vars(v6=True, families=(4, 6), v4filter=True, egress6=True),
                          gw_vars(v6=True, families=(4, 6), upstream=("192.0.2.30", "203.0.113.30"))):
            new, before = render(GW, variables), render_text(old, variables)
            bad = [ln for ln in changed_lines(before, new) if not any(re.search(p, ln) for p in allowed)]
            self.assertEqual(bad, [], "lines changed outside the site-import filter entries")
            # the same entries, sequence numbers apart
            strip = lambda t: sorted(re.sub(r" seq \d+ ", " seq N ", ln) for ln in t.splitlines() if " prefix-list SITE-V" in ln)
            self.assertEqual(strip(new), strip(before))

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_ansible_template_module_agrees_on_both_lists(self):
        tmp = tempfile.mkdtemp(prefix="shc-frr-render-")
        dest = os.path.join(tmp, "frr.conf")
        variables = gw_vars(v6=True, families=(4, 6), v4filter=True)
        run_play([{"name": "render", "ansible.builtin.template": {"src": GW, "dest": dest}}, write_out("'ok'")],
                 variables)
        with open(dest) as f:
            self.assertEqual(f.read(), render(GW, variables))


class GatewayEgressStandby(unittest.TestCase):
    def test_standby_entry_with_ipv6_egress(self):
        out = render(GW, gw_vars(v6=True, families=(4, 6), egress6=True))
        save("gw-dual-stack-core-egress6.conf", out)
        self.assertIn("bgp community-list standard EGRESS-STANDBY seq 5 permit 65010:998\n"
                      "route-map GW-T5 permit 5\n match community EGRESS-STANDBY\n set local-preference 50\n"
                      " set community 65010:999 additive\nexit\nroute-map GW-T5 permit 10\n"
                      " set community 65010:999 additive\nexit\n", out)


class Root(unittest.TestCase):
    def setUp(self):
        self.base = base_text(RT)

    def test_ipv4_core_byte_identical(self):
        for name, fam in (("root-ipv4-core.conf", [4]), ("root-ipv4-core-nofamilies.conf", None)):
            new = render(RT, root_vars(families=fam))
            save(name, new)
            if self.base is None:
                self.skipTest(f"{BASE} not in this checkout")
            old = render_text(self.base, root_vars(families=fam))
            self.assertEqual(new, old, name)

    def test_dual_stack_core(self):
        out = render(RT, root_vars(v6=True, families=[4]))
        save("root-dual-stack-core.conf", out)
        self.assertIn("interface br-core\n ip router isis CORE\n ipv6 router isis CORE\n isis priority 127\n", out)
        self.assertIn("interface lo\n ip router isis CORE\n ipv6 router isis CORE\n isis passive\n", out)
        self.assertIn(" address-family ipv4 vpn\n  neighbor PE activate\n  neighbor PE route-reflector-client\n"
                      " exit-address-family\n !\n address-family ipv6 vpn\n  neighbor PE activate\n"
                      "  neighbor PE route-reflector-client\n exit-address-family\nexit\n", out)
        self.assertIn(" segment-routing prefix 10.255.0.1/32 index 1\n"
                      " segment-routing prefix 2001:db8:4005:ffff::1/128 index 1001\nexit\n", out)
        # the root VRFs stay IPv4 (their IPv6 side comes with IPv6 egress and circuits)
        self.assertNotIn("address-family ipv6 unicast", out)
        self.assertEqual(out.count("address-family ipv4 unicast"), 2)
        if self.base is not None:
            old = render_text(self.base, root_vars(v6=True, families=[4]))
            added = [ln for ln in difflib.ndiff(old.splitlines(), out.splitlines()) if ln.startswith("+ ")]
            removed = [ln for ln in difflib.ndiff(old.splitlines(), out.splitlines()) if ln.startswith("- ")]
            # only additions, and only these lines
            self.assertEqual(removed, [])
            self.assertTrue(set(a[2:] for a in added) <= {
                " ipv6 router isis CORE", " !", " address-family ipv6 vpn", "  neighbor PE activate",
                "  neighbor PE route-reflector-client", " exit-address-family",
                " segment-routing prefix 2001:db8:4005:ffff::1/128 index 1001"}, added)

    def test_vrf_without_ipv4_family(self):
        out = render(RT, root_vars(v6=True, families=[6]))
        self.assertNotIn("address-family ipv4 unicast", out)
        self.assertIn("router bgp 65010 vrf e10000\n", out)



class SiteDefaultWithoutEgress(unittest.TestCase):
    def test_ipv6_vrf_announces_the_unreachable_default_while_no_egress(self):
        new = render(GW, gw_vars(v6=True, families=(4, 6)))
        self.assertIn(" address-family ipv6 unicast\n  network ::/0\n", new)
        self.assertNotIn("network 0.0.0.0/0", new)

    def test_no_local_default_once_the_egress_offers_ipv6(self):
        new = render(GW, gw_vars(v6=True, families=(4, 6), egress6=True))
        self.assertNotIn("network ::/0", new)

    def test_no_local_default_in_an_ipv4_only_vrf(self):
        new = render(GW, gw_vars(v6=True, families=(4,)))
        self.assertNotIn("network ::/0", new)

if __name__ == "__main__":
    unittest.main()

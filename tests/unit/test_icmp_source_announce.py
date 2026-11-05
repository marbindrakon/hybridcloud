"""The border gateway nodes' ICMPv6 sources announced to the site (realization gateway.announceIcmpSource6, per site
gateway.announceIcmpSource6BySite) and the input filter that comes with it.

- Off (false, absent, or no IPv6 core): the gateway's FRR configuration, the apply configuration and the apply's
  calls and nftables state are byte-identical to the release before the switch (BASE, read from Git; those
  comparisons are skipped without it).
- On: only lines are added. Each IPv6 VRF announces this node's /128 (route-map GW-LOCAL); FROM-SITE refuses the
  whole core.icmpSource6 range from the site; an IPv4-only VRF gets nothing. A small evaluator of the rendered
  route-maps shows where the route can go: never into the network's VPN (so never to another site, a circuit's CE
  or the IPv6 egress upstreams, which are all behind it), to the site as Type-5 with the gateways' community, and
  never back in at the other node, even from a site that strips communities and with the site-import filter off.
- The apply loads table ip6 shc_gw_in6 (input hook, IPv6 to the node from a VRF: ICMPv6 and TCP that opens no
  connection pass, the rest is dropped) before the VRFs take the ICMP source, removes it with the key, and keeps it
  after a failed IPv6 step. The same ruleset is loaded into a real kernel (an unprivileged user and network
  namespace; skipped where that is not possible) and probed with packets: echo and the replies of a connection the
  node opened pass, a connection request and UDP to the node are dropped, forwarded traffic is untouched.

Documentation and private ranges only."""
import collections
import copy
import difflib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_frr_templates as T  # noqa: E402
import test_shc_fabric_apply as A  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

# the release before the switch
BASE = "74720c7a"
CG = os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudgateway_provision", "tasks")
NODE1_ICMP6 = "2001:db8:4005:1::1:2/128"
NODE2_ICMP6 = "2001:db8:4005:1::2:2/128"


def git_show(rel):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{rel}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def gw(announce=None, **kw):
    """the gateway template's variables (test_frr_templates.gw_vars), with this node's announced /128"""
    v = T.gw_vars(**kw)
    if announce is not None:
        v["cg_node_icmp6_announce"] = announce
    return v


def vrf_block(text, vrf):
    return text.split(f"router bgp 65010 vrf {vrf}\n")[1].split("\nexit\n")[0]


# ------------------------------------------------------------------------------------------- route-map evaluator
class Policy:
    """The route-maps, standard community lists and IPv6 prefix lists of a rendered FRR configuration, and FRR's
    evaluation of a route-map on an IPv6 route (first matching entry by sequence; all its matches must hold; an
    entry's sets apply on permit; no entry matches = deny)."""

    def __init__(self, text):
        self.maps, self.clists, self.plists = {}, {}, {}
        cur = None
        for ln in text.splitlines():
            m = re.match(r"^route-map (\S+) (permit|deny) (\d+)$", ln)
            if m:
                cur = {"seq": int(m[3]), "action": m[2], "match": [], "set": []}
                self.maps.setdefault(m[1], []).append(cur)
                continue
            if ln == "exit":
                cur = None
                continue
            if cur is not None and ln.startswith(" match "):
                cur["match"].append(ln[len(" match "):])
                continue
            if cur is not None and ln.startswith(" set "):
                cur["set"].append(ln[len(" set "):])
                continue
            m = re.match(r"^bgp community-list standard (\S+) seq \d+ (permit|deny) (\S+)$", ln)
            if m:
                self.clists.setdefault(m[1], []).append((m[2], m[3]))
                continue
            m = re.match(r"^ipv6 prefix-list (\S+) seq (\d+) (permit|deny) (\S+)(?: le (\d+))?$", ln)
            if m:
                self.plists.setdefault(m[1], []).append(
                    (int(m[2]), m[3], ipaddress.ip_network(m[4]), int(m[5]) if m[5] else None))

    def plist(self, name, prefix):
        for _, action, net, le in sorted(self.plists.get(name, []), key=lambda e: e[0]):
            longest = le if le is not None else net.prefixlen
            if prefix.subnet_of(net) and net.prefixlen <= prefix.prefixlen <= longest:
                return action == "permit"
        return False

    def matches(self, cond, route):
        m = re.match(r"^community (\S+)$", cond)
        if m:
            return any(action == "permit" and c in route["communities"] for action, c in self.clists[m[1]])
        m = re.match(r"^ipv6 address prefix-list (\S+)$", cond)
        if m:
            return self.plist(m[1], route["prefix"])
        if re.match(r"^ip address prefix-list \S+$", cond):
            return False  # an IPv4 prefix list never matches an IPv6 route
        raise AssertionError("match not modelled: " + cond)

    def apply(self, name, prefix, communities=()):
        """(permit, communities after the map, the deciding entry's sequence or None)"""
        route = {"prefix": ipaddress.ip_network(prefix), "communities": set(communities)}
        for e in sorted(self.maps[name], key=lambda e: e["seq"]):
            if all(self.matches(c, route) for c in e["match"]):
                if e["action"] == "deny":
                    return False, route["communities"], e["seq"]
                for s in e["set"]:
                    m = re.match(r"^community (\S+) additive$", s)
                    if m:
                        route["communities"].add(m[1])
                return True, route["communities"], e["seq"]
        return False, route["communities"], None


# ----------------------------------------------------------------------------------------------------- template
class TemplateOff(unittest.TestCase):
    """L6: the switch off renders exactly what the release before rendered."""

    CASES = (dict(), dict(v4filter=True), dict(v6=True, families=(4, 6)), dict(v6=True, families=(6,)),
             dict(v6=True, families=(4, 6), egress6=True, v4filter=True),
             dict(v6=True, families=(4, 6), upstream=("192.0.2.30", "203.0.113.30"), password="dummy"))

    def setUp(self):
        self.base = git_show(os.path.relpath(T.GW, ROOT))
        if self.base is None:
            self.skipTest(f"{BASE} not in this checkout")

    def test_byte_identical_to_the_release_before(self):
        for kw in self.CASES:
            for announce in (None, ""):
                v = gw(announce, **kw)
                if kw.get("v6"):
                    v["cg_vrfs"][0]["covering6"] = ["2001:db8:100::/56"]
                self.assertEqual(T.render(T.GW, v), T.render_text(self.base, v), (kw, announce))

    def test_no_ipv6_core_renders_nothing_of_it(self):
        # the job never sets the node's /128 without the IPv6 core; even if it did, the template renders nothing
        v = gw(NODE1_ICMP6, families=(4, 6))
        self.assertEqual(T.render(T.GW, v), T.render_text(self.base, v))
        self.assertNotIn("GW-ICMP6", T.render(T.GW, v))


class TemplateOn(unittest.TestCase):
    def setUp(self):
        self.off = T.render(T.GW, gw(None, v6=True, families=(4, 6)))
        self.on = T.render(T.GW, gw(NODE1_ICMP6, v6=True, families=(4, 6)))
        T.save("gw-dual-stack-core-icmp6-announced.conf", self.on)

    def test_only_lines_added(self):
        diff = list(difflib.ndiff(self.off.splitlines(), self.on.splitlines()))
        self.assertEqual([d for d in diff if d.startswith("- ")], [])
        added = [d[2:] for d in diff if d.startswith("+ ") and not d.startswith("+ ! ")]
        self.assertEqual(collections.Counter(added), collections.Counter([
            "ipv6 prefix-list GW-ICMP6 seq 5 permit 2001:db8:4005:1::/64 le 128", "!",
            "route-map FROM-SITE deny 15", " match ipv6 address prefix-list GW-ICMP6", "exit",
            f"  network {NODE1_ICMP6} route-map GW-LOCAL"]))

    def test_per_vrf(self):
        # the IPv6 VRF announces it in its IPv6 unicast family, after the default; the IPv4-only VRF gets nothing
        t51000 = vrf_block(self.on, "t51000")
        self.assertIn(" address-family ipv6 unicast\n  network ::/0 route-map GW-LOCAL\n"
                      f"  network {NODE1_ICMP6} route-map GW-LOCAL\n  label vpn export auto\n", t51000)
        self.assertIn("  route-map vpn export TO-VPN-10000\n", t51000)
        self.assertIn("  advertise ipv6 unicast route-map GW-T5\n", t51000)
        self.assertNotIn(NODE1_ICMP6, vrf_block(self.on, "t51003"))
        self.assertEqual(self.on.count(f"network {NODE1_ICMP6} "), 1)

    def test_after_the_covering_routes(self):
        v = gw(NODE1_ICMP6, v6=True, families=(4, 6), egress6=True)
        v["cg_vrfs"][0]["covering6"] = ["2001:db8:100::/56", "2001:db8:4005::/48"]
        out = T.render(T.GW, v)
        self.assertIn(" address-family ipv6 unicast\n  network 2001:db8:100::/56 route-map GW-LOCAL\n"
                      "  network 2001:db8:4005::/48 route-map GW-LOCAL\n"
                      f"  network {NODE1_ICMP6} route-map GW-LOCAL\n", out)

    def test_ipv4_only_vrfs(self):
        # no IPv6 VRF: no network line; the filter entry is the gateway's (stable when the first IPv6 VRF comes)
        out = T.render(T.GW, gw(NODE1_ICMP6, v6=True, families=(4,)))
        self.assertNotIn("network 2001:db8:4005:1::", out)
        self.assertIn("route-map FROM-SITE deny 15\n match ipv6 address prefix-list GW-ICMP6\nexit\n", out)

    def test_filter_order(self):
        self.assertEqual([ln for ln in T.section(self.on, "route-map FROM-SITE") if ln.startswith("route-map")],
                         ["route-map FROM-SITE deny 10", "route-map FROM-SITE deny 15", "route-map FROM-SITE permit 20",
                          "route-map FROM-SITE deny 30", "route-map FROM-SITE permit 40"])
        v = gw(NODE1_ICMP6, v6=True, families=(4, 6))
        v["fab_real"]["gateway"]["siteImportFilter"]["ipv6"] = False
        self.assertEqual([ln for ln in T.section(T.render(T.GW, v), "route-map FROM-SITE") if ln.startswith("route-map")],
                         ["route-map FROM-SITE deny 10", "route-map FROM-SITE deny 15", "route-map FROM-SITE permit 40"])

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_ansible_template_module_agrees(self):
        tmp = tempfile.mkdtemp(prefix="shc-frr-render-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dest = os.path.join(tmp, "frr.conf")
        v = gw(NODE1_ICMP6, v6=True, families=(4, 6))
        run_play([{"name": "render", "ansible.builtin.template": {"src": T.GW, "dest": dest}}, write_out("'ok'")], v)
        with open(dest) as f:
            self.assertEqual(f.read(), self.on)


class Leaks(unittest.TestCase):
    """Where the announced /128 can go, by the rendered policy (both nodes render the same maps)."""

    def node(self, icmp6, v4filter=False, v6filter=True):
        v = gw(icmp6, v6=True, families=(4, 6), v4filter=v4filter)
        v["fab_real"]["gateway"]["siteImportFilter"]["ipv6"] = v6filter
        return Policy(T.render(T.GW, v))

    def originate(self, p):
        """the network statement's route-map, then the VPN export and the Type-5 export"""
        ok, comm, _ = p.apply("GW-LOCAL", NODE1_ICMP6)
        self.assertTrue(ok)
        return comm

    def test_never_into_the_vpn(self):
        p = self.node(NODE1_ICMP6)
        comm = self.originate(p)
        self.assertEqual(comm, {"65010:997"})
        self.assertEqual(p.apply("TO-VPN-10000", NODE1_ICMP6, comm)[0::2], (False, 5))
        # the other route targets' maps deny it the same way (a VRF exports only through its own)
        self.assertEqual(p.apply("TO-VPN-10001", NODE1_ICMP6, comm)[0::2], (False, 5))
        # what the VPN carries is everything another site, a circuit's CE (root VRF) or the IPv6 egress upstream
        # (egress VRF) can learn from this gateway: the root's FRR imports only from the VPN and its template has
        # nothing of the announcement
        with open(T.RT) as f:
            self.assertNotIn("icmp6", f.read())

    def test_to_the_site_with_the_gateways_community(self):
        p = self.node(NODE1_ICMP6)
        ok, comm, seq = p.apply("GW-T5", NODE1_ICMP6, self.originate(p))
        self.assertTrue(ok)
        self.assertEqual((comm, seq), ({"65010:997", "65010:999"}, 10))

    def test_never_back_in_at_the_other_node(self):
        p1 = self.node(NODE1_ICMP6)
        _, sent, _ = p1.apply("GW-T5", NODE1_ICMP6, self.originate(p1))
        for v6filter in (True, False):
            for v4filter in (False, True):
                p2 = self.node(NODE2_ICMP6, v4filter=v4filter, v6filter=v6filter)
                # reflected back by the site as sent: the gateways' community
                self.assertEqual(p2.apply("FROM-SITE", NODE1_ICMP6, sent)[0::2], (False, 10))
                # by a site that strips communities: the range entry
                self.assertEqual(p2.apply("FROM-SITE", NODE1_ICMP6, ())[0::2], (False, 15), (v4filter, v6filter))
                # its own address, and the cloud edge routers' (node ordinals from 100), the same
                self.assertFalse(p2.apply("FROM-SITE", NODE2_ICMP6, ())[0])
                self.assertFalse(p2.apply("FROM-SITE", "2001:db8:4005:1::64:2/128", ())[0])
                # a placement's host route still enters (unchanged)
                self.assertTrue(p2.apply("FROM-SITE", "2001:db8:100:1::5/128", ())[0])

    def test_the_gap_the_range_entry_closes(self):
        """Without the range entry (the release before, site-import filter off) a stripped route would enter the
        other node's VRF, and from there its VPN (the community GW-LOCAL went with the others)."""
        base = git_show(os.path.relpath(T.GW, ROOT))
        if base is None:
            self.skipTest(f"{BASE} not in this checkout")
        v = gw(None, v6=True, families=(4, 6))
        v["fab_real"]["gateway"]["siteImportFilter"]["ipv6"] = False
        old = Policy(T.render_text(base, v))
        self.assertTrue(old.apply("FROM-SITE", NODE1_ICMP6, ())[0])
        self.assertTrue(old.apply("TO-VPN-10000", NODE1_ICMP6, ())[0])
        self.assertFalse(self.node(NODE2_ICMP6, v6filter=False).apply("FROM-SITE", NODE1_ICMP6, ())[0])


# -------------------------------------------------------------------------------------------------------- tasks
@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Switch(unittest.TestCase):
    """main.yml decides per gateway (site) whether its nodes announce; node_gateway.yml takes the node's /128."""

    def decide(self, gateway=None, site="site-osp", ipv6_core=True, icmp_pool="2001:db8:4005:1::/64"):
        t = copy.deepcopy(by_name(load(os.path.join(CG, "main.yml")), "Announce the nodes' ICMPv6 sources to the site or not"))
        real = {"core": {"icmpSource6": icmp_pool}}
        if gateway is not None:
            real["gateway"] = gateway
        return run_play([t, write_out("cg_icmp6_announce | bool")],
                        {"fab_real": real, "cg_site_name": site, "cg_ipv6_core": ipv6_core})

    def test_default_off(self):
        real = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]
        self.assertIs(real["gateway"]["announceIcmpSource6"], False)
        self.assertEqual(real["gateway"]["announceIcmpSource6BySite"], {})
        self.assertFalse(self.decide(real["gateway"]))
        self.assertFalse(self.decide(None))
        self.assertFalse(self.decide({}))

    def test_fleet_and_per_site(self):
        self.assertTrue(self.decide({"announceIcmpSource6": True}))
        self.assertTrue(self.decide({"announceIcmpSource6": False, "announceIcmpSource6BySite": {"site-osp": True}}))
        self.assertFalse(self.decide({"announceIcmpSource6": False, "announceIcmpSource6BySite": {"site-vm": True}}))
        self.assertFalse(self.decide({"announceIcmpSource6": True, "announceIcmpSource6BySite": {"site-osp": False}}))
        self.assertTrue(self.decide({"announceIcmpSource6": True, "announceIcmpSource6BySite": {"site-vm": False}}))
        self.assertTrue(self.decide({"announceIcmpSource6BySite": {"site-osp": "true"}}))

    def test_only_with_the_ipv6_core_and_the_pool(self):
        self.assertFalse(self.decide({"announceIcmpSource6": True}, ipv6_core=False))
        self.assertFalse(self.decide({"announceIcmpSource6": True}, icmp_pool=""))

    def node_value(self, announce):
        tasks = load(os.path.join(CG, "node_gateway.yml"))
        t = by_name(tasks, "{{ bgw_node.name }}: the ICMPv6 source this node announces to the site")
        core = dict(T.REAL["core"], loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
        check = F.pe_addresses({"core": core, "index": 2, "nodeOrdinal": 2})
        return run_play([t, write_out("cg_node_icmp6_announce")],
                        {"cg_icmp6_announce": announce, "fab_check_result": check})

    def test_node_value(self):
        self.assertEqual(self.node_value(True), NODE2_ICMP6)
        self.assertEqual(self.node_value(False), "")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ApplyConfig(unittest.TestCase):
    """the apply configuration (gw-<site>.json) gains icmp6Announced only while the node announces; otherwise the file
    is byte-identical to the release before's"""

    TASK = "{{ bgw_node.name }}: gateway instance config"

    def content(self, tasks, extra):
        t = by_name(tasks, self.TASK)
        t = {"name": "c", "vars": t.get("vars", {}), "ansible.builtin.set_fact": {"_c": t["ansible.builtin.copy"]["content"]}}
        core = dict(T.REAL["core"], loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
        v = {"bgw_node": {"name": "bgw-1"}, "fab_name": "site1", "cg_instance": {"type": "gateway"},
             "cg_node_addr": "192.0.2.4", "fab_spec": {"siteUnderlay": {"cidr": "192.0.2.0/24"}},
             "cg_pe": F.pe_addresses({"core": core, "index": 2}), "fab_real": {"core": core},
             "fab_check_result": F.pe_addresses({"core": core, "index": 2, "nodeOrdinal": 1}), "cg_ipv6_core": True}
        v.update(extra)
        return run_play([t, write_out("_c")], v)

    def test_off_byte_identical(self):
        base = git_show(os.path.relpath(os.path.join(CG, "node_gateway.yml"), ROOT))
        if base is None:
            self.skipTest(f"{BASE} not in this checkout")
        import yaml
        old = self.content(yaml.safe_load(base), {})
        new_tasks = load(os.path.join(CG, "node_gateway.yml"))
        self.assertEqual(self.content(new_tasks, {}), old)
        self.assertEqual(self.content(new_tasks, {"cg_node_icmp6_announce": ""}), old)
        self.assertNotIn("icmp6Announced", old)

    def test_on(self):
        c = json.loads(self.content(load(os.path.join(CG, "node_gateway.yml")), {"cg_node_icmp6_announce": NODE1_ICMP6}))
        self.assertIs(c["icmp6Announced"], True)
        self.assertEqual(c["icmp6"], NODE1_ICMP6)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Status(unittest.TestCase):
    def test_reported(self):
        t = by_name(load(os.path.join(CG, "main.yml")), "Report the gateway ready")
        common = {"cg_status": {"site": {}}, "cg_site_extra": {}, "cg_node_results": [], "cg_instance": {},
                  "fab_real": {"model": "linux-pe"}, "fab_name": "site1", "cg_vrfs": [],
                  "cg_pe": F.pe_addresses({"core": T.REAL["core"], "index": 2}),
                  "cg_root_pe": F.pe_addresses({"core": T.REAL["core"], "index": 1})}
        for extra, want in (({}, False), ({"cg_icmp6_announce": False}, False), ({"cg_icmp6_announce": True}, True)):
            out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")],
                           dict(common, **extra))
            self.assertIs(out["realization"]["icmpSource6Announced"], want)


# -------------------------------------------------------------------------------------------- apply (fake kernel)
GW_ON = dict(A.GW_CONF, icmp6Announced=True, **A.GW6)
TABLE = "ip6 shc_gw_in6"


class ApplyFilter(A.ApplyCase):
    def nft(self, ns="gw-iaas1"):
        return self.h.get_state()["ns"][ns]["nft"]

    def marker(self):
        return os.path.join(self.h.etc, "gw-iaas1.in6")

    def up(self, conf, fail=None):
        h = self.h
        h.conf("root", dict(A.ROOT_CONF, **A.ROOT6))
        h.ok(self, "root")
        h.conf("gw-iaas1", conf)
        return h.ok(self, "gw-iaas1", fail=fail)

    def test_loaded_before_the_icmp_sources(self):
        out, log = self.up(GW_ON)
        self.keep("gw-iaas1-in6-run1", log, out)
        self.assertIn(TABLE, self.nft())
        text = self.nft()[TABLE]
        self.assertEqual(text.splitlines(), [
            "table ip6 shc_gw_in6", "delete table ip6 shc_gw_in6", "table ip6 shc_gw_in6 {", "  chain input {",
            "    type filter hook input priority filter; policy accept;",
            '    iifname { "t51000", "t51002" } meta l4proto ipv6-icmp accept',
            '    iifname { "t51000", "t51002" } tcp flags & (syn | ack) != syn accept',
            '    iifname { "t51000", "t51002" } counter drop comment "only ICMPv6 and TCP that opens no connection to the node"',
            "  }", "}"])
        # no connection tracking anywhere in it (the namespace would then track all forwarded IPv6)
        self.assertNotRegex(text, r"\bct\b|notrack|fib")
        loaded = A.index(log, r"^W \[gw-iaas1\] nft -f -$")
        first = A.index(log, r"^W \[gw-iaas1\] ip addr add 2001:db8:4005:1::1:2/128 dev t51000 nodad$")
        self.assertLess(loaded, first)
        self.assertTrue(os.path.exists(self.marker()))
        self.assertTrue(out.strip().endswith("changed=yes"))
        out2, log2 = self.h.ok(self, "gw-iaas1")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in A.writes(log2) if A.is_v6(l)], [])

    def test_follows_the_vrfs(self):
        self.up(GW_ON)
        self.h.conf("gw-iaas1", dict(GW_ON, vrfs=A.GW_CONF["vrfs"][:1]))
        out, _ = self.h.ok(self, "gw-iaas1")
        self.assertTrue(out.strip().endswith("changed=yes"))
        self.assertIn('iifname { "t51000" } meta l4proto', self.nft()[TABLE])
        self.assertNotIn("t51002", self.nft()[TABLE])
        # no VRF at all: no table
        self.h.conf("gw-iaas1", dict(GW_ON, vrfs=[]))
        self.h.ok(self, "gw-iaas1")
        self.assertNotIn(TABLE, self.nft())
        self.assertFalse(os.path.exists(self.marker()))

    def test_removed_with_the_key(self):
        self.up(GW_ON)
        for conf in (dict(A.GW_CONF, icmp6Announced=False, **A.GW6), dict(A.GW_CONF, **A.GW6)):
            self.h.conf("gw-iaas1", dict(GW_ON))
            self.h.ok(self, "gw-iaas1")
            self.h.conf("gw-iaas1", conf)
            out, log = self.h.ok(self, "gw-iaas1")
            self.assertNotIn(TABLE, self.nft())
            self.assertFalse(os.path.exists(self.marker()))
            self.assertTrue(out.strip().endswith("changed=yes"), out)
            # the ICMP sources stay (they were there before the announcement)
            self.assertEqual(self.h.addrs6("gw-iaas1", "t51000"), ["2001:db8:4005:1::1:2/128"])
            out2, log2 = self.h.ok(self, "gw-iaas1")
            self.assertTrue(out2.strip().endswith("changed=no"), out2)
            self.assertEqual([l for l in log2 if "shc_gw_in6" in l or ("nft" in l and "delete" in l)], [])

    def test_ipv6_core_rolled_back(self):
        """the job writes no key without the IPv6 core: the table goes with the ICMP sources"""
        self.up(GW_ON)
        self.h.conf("gw-iaas1", dict(A.GW_CONF, **A.RollbackTest.EMPTY6))
        self.h.ok(self, "gw-iaas1")
        self.assertNotIn(TABLE, self.nft())
        self.assertEqual(self.h.addrs6("gw-iaas1", "t51000"), [])

    def test_down_forgets_the_marker(self):
        self.up(GW_ON)
        self.h.ok(self, "gw-iaas1", "down")
        self.assertFalse(os.path.exists(self.marker()))
        self.assertNotIn("gw-iaas1", self.h.get_state()["ns"])

    def test_refused_load_fails_closed(self):
        """the kernel refuses the table on a fresh namespace: the marker line, no ICMP source on the VRFs (nothing to
        announce: FRR's import check finds no route), IPv4 as always, exit 0"""
        with mock.patch.dict(os.environ, {"FAKE_FAIL_STDIN": r"table ip6 shc_gw_in6 \{"}):
            out, log = self.up(GW_ON)
        self.keep("gw-iaas1-in6-refused", log, out)
        self.assertIn("warning: the input filter of the VRFs failed; IPv6 core not applied\n", out)
        self.assertNotIn(TABLE, self.nft())
        self.assertEqual(self.h.addrs6("gw-iaas1", "t51000"), [])
        self.assertIn("vx51002", self.h.get_state()["ns"]["gw-iaas1"]["links"])

    def test_kept_after_an_earlier_ipv6_step_failed(self):
        """a failed IPv6 step before it (the namespace's forwarding refused) leaves the loaded filter as it was"""
        self.up(GW_ON)
        before = self.nft()[TABLE]
        st = self.h.get_state()
        st["ns"]["gw-iaas1"]["sysctl"]["net/ipv6/conf/all/forwarding"] = "0"
        self.h.set_state(st)
        rc, out, err, log = self.h.run("gw-iaas1", fail=r"^\[gw-iaas1\] sysctl -qw net/ipv6/conf/all/forwarding=1$")
        self.assertEqual(rc, 0, err)
        self.assertIn("IPv6 core not applied", out)
        self.assertEqual(self.nft()[TABLE], before)
        self.assertTrue(os.path.exists(self.marker()))


class ApplyOffAsBefore(A.ApplyCase):
    """L6 for the node: with the key absent or false, the release before's script and this one make the same calls in
    the same order and leave the same kernel state (nftables included), on a fresh blade and on a second run."""

    def test_same_calls_and_state(self):
        old = git_show(A.SCRIPT_REL)
        if old is None:
            self.skipTest(f"{BASE} not in this checkout")
        self.h.scripts["old"] = self.h.copy_script(old, "apply-old")
        for gw6 in (A.GW6, dict(A.GW6, icmp6Announced=False), {}):
            results = {}
            for which in ("old", "new"):
                self.h.set_state(A.blade())
                for f in os.listdir(self.h.etc):
                    os.unlink(os.path.join(self.h.etc, f))
                self.h.conf("root", dict(A.ROOT_CONF, **(A.ROOT6 if gw6 else {})))
                self.h.conf("gw-iaas1", dict(A.GW_CONF, **gw6))
                runs = [self.h.ok(self, "root", script=which)]
                for _ in range(2):
                    runs.append(self.h.ok(self, "gw-iaas1", script=which))
                results[which] = (runs, self.h.get_state(), sorted(os.listdir(self.h.etc)))
            self.assertEqual(results["old"][0], results["new"][0], gw6)
            self.assertEqual(results["old"][1], results["new"][1], gw6)
            self.assertEqual(results["old"][2], results["new"][2], gw6)
            self.assertNotIn(TABLE, results["new"][1]["ns"]["gw-iaas1"]["nft"])


# -------------------------------------------------------------------------------------------- apply (real kernel)
PROBE = textwrap.dedent(r'''
    """Runs as root of a new user and network namespace. A gateway VRF t51000 with the node's ICMP source on its device
    and a bridge br51000 (the site leg's bridge); a site host in VRF site behind it (veth vs/vg, the gateway's bridge
    MAC as its next hop, as an EVPN router MAC); a far host in VRF far behind another slave of t51000 (forwarded
    traffic). Probes from the site host, before and after the ruleset (argv[1]) is loaded."""
    import json, os, socket, struct, subprocess, sys, time

    ICMP6, SITE, FAR = "2001:db8:4005:1::1:2", "2001:db8:100:1::5", "2001:db8:200::5"

    def sh(*a):
        subprocess.run(a, check=True, capture_output=True)

    def mac(dev):
        return json.loads(subprocess.run(["ip", "-j", "link", "show", dev], capture_output=True, text=True,
                                         check=True).stdout)[0]["address"]

    def wsys(path, value):
        with open("/proc/sys/" + path, "w") as f:
            f.write(value)

    for k in ("all", "default"):
        wsys(f"net/ipv6/conf/{k}/accept_dad", "0")
        wsys(f"net/ipv6/conf/{k}/forwarding", "1")
    sh("ip", "link", "set", "lo", "up")
    for vrf, table in (("t51000", "51000"), ("site", "100"), ("far", "200")):
        sh("ip", "link", "add", vrf, "type", "vrf", "table", table)
        sh("ip", "link", "set", vrf, "up")
    sh("ip", "link", "add", "br51000", "type", "bridge")
    sh("ip", "link", "set", "br51000", "master", "t51000", "up")
    sh("ip", "link", "add", "vg", "type", "veth", "peer", "name", "vs")
    sh("ip", "link", "set", "vg", "master", "br51000", "up")
    sh("ip", "link", "set", "vs", "master", "site", "up")
    sh("ip", "link", "add", "xg", "type", "veth", "peer", "name", "xs")
    sh("ip", "link", "set", "xg", "master", "t51000", "up")
    sh("ip", "link", "set", "xs", "master", "far", "up")
    sh("ip", "addr", "add", ICMP6 + "/128", "dev", "t51000", "nodad")
    sh("ip", "addr", "add", SITE + "/64", "dev", "vs", "nodad")
    sh("ip", "addr", "add", FAR + "/64", "dev", "xs", "nodad")
    # the site host reaches the node's address and the far host through the gateway's bridge MAC
    for dst in (ICMP6 + "/128", "2001:db8:200::/64"):
        sh("ip", "-6", "route", "add", dst, "dev", "vs", "table", "100")
    for dst in (ICMP6, FAR):
        sh("ip", "-6", "neigh", "replace", dst, "lladdr", mac("br51000"), "dev", "vs", "nud", "permanent")
    # the gateway VRF: the site host on the bridge, the far host on xg
    sh("ip", "-6", "route", "add", SITE + "/128", "dev", "br51000", "table", "51000")
    sh("ip", "-6", "neigh", "replace", SITE, "lladdr", mac("vs"), "dev", "br51000", "nud", "permanent")
    sh("ip", "-6", "route", "add", FAR + "/128", "dev", "xg", "table", "51000")
    sh("ip", "-6", "neigh", "replace", FAR, "lladdr", mac("xs"), "dev", "xg", "nud", "permanent")
    # the far host returns to the site through the gateway
    sh("ip", "-6", "route", "add", "2001:db8:100:1::/64", "dev", "xs", "table", "200")
    sh("ip", "-6", "neigh", "replace", SITE, "lladdr", mac("xg"), "dev", "xs", "nud", "permanent")

    def sock(family, kind, vrf, proto=0):
        s = socket.socket(family, kind, proto)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, vrf.encode())
        return s

    def listener(vrf, addr, port):
        s = sock(socket.AF_INET6, socket.SOCK_STREAM, vrf)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, port))
        s.listen(8)
        return s

    # a listener of the node in the VRF (as bgpd's per-VRF socket), UDP of the node, a site and a far service
    node_tcp = listener("t51000", "::", 179)
    node_udp = sock(socket.AF_INET6, socket.SOCK_DGRAM, "t51000")
    node_udp.bind(("::", 3784))
    site_tcp = listener("site", SITE, 8080)
    far_tcp = listener("far", FAR, 8081)

    def connect(vrf, src, dst, port):
        s = sock(socket.AF_INET6, socket.SOCK_STREAM, vrf)
        s.settimeout(1.5)
        try:
            s.bind((src, 0))
            s.connect((dst, port))
            return "connected"
        except socket.timeout:
            return "timeout"
        except OSError as e:
            return "error %d" % e.errno
        finally:
            s.close()

    def echo():
        s = sock(socket.AF_INET6, socket.SOCK_RAW, "site", socket.IPPROTO_ICMPV6)
        s.bind((SITE, 0))
        s.settimeout(1.5)
        ident = os.getpid() & 0xFFFF
        s.sendto(struct.pack("!BBHHH", 128, 0, 0, ident, 1) + b"probe", (ICMP6, 0))
        end = time.time() + 1.5
        try:
            while time.time() < end:
                data, peer = s.recvfrom(2048)
                if data[0] == 129 and struct.unpack("!H", data[4:6])[0] == ident and peer[0] == ICMP6:
                    return "reply"
        except socket.timeout:
            pass
        finally:
            s.close()
        return "timeout"

    def udp(port):
        """a datagram to the node: did the node's socket get it (3784, open) or did the node refuse it (closed)"""
        s = sock(socket.AF_INET6, socket.SOCK_DGRAM, "site")
        s.settimeout(1.5)
        node_udp.settimeout(1.5)
        try:
            s.bind((SITE, 0))
            s.connect((ICMP6, port))
            s.send(b"probe")
            if port == 3784:
                try:
                    node_udp.recv(64)
                    return "delivered"
                except socket.timeout:
                    return "timeout"
            s.recv(64)
            return "answer"
        except socket.timeout:
            return "timeout"
        except ConnectionRefusedError:
            return "refused"
        finally:
            s.close()

    def probe():
        return {"echo": echo(), "tcp_to_node": connect("site", SITE, ICMP6, 179), "udp_to_node": udp(3784),
                "udp_closed": udp(3785),
                "tcp_from_node": connect("t51000", ICMP6, SITE, 8080), "forwarded": connect("site", SITE, FAR, 8081)}

    out = {"before": probe()}
    subprocess.run(["nft", "-f", sys.argv[1]], check=True, capture_output=True)
    out["after"] = probe()
    out["ruleset"] = subprocess.run(["nft", "list", "table", "ip6", "shc_gw_in6"], capture_output=True, text=True).stdout
    print(json.dumps(out))
''')


def can_probe():
    if not (shutil.which("unshare") and shutil.which("nft") and shutil.which("ip")):
        return False
    try:
        p = subprocess.run(["unshare", "-rn", "ip", "link", "add", "t1", "type", "vrf", "table", "1"],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


class RealKernel(A.ApplyCase):
    """The ruleset the apply loads, in a real kernel (VRF devices in an unprivileged user and network namespace)."""

    def test_probes(self):
        if not can_probe():
            self.skipTest("no unprivileged user and network namespace with VRF devices, nft and ip here")
        self.h.conf("root", dict(A.ROOT_CONF, **A.ROOT6))
        self.h.ok(self, "root")
        self.h.conf("gw-iaas1", GW_ON)
        self.h.ok(self, "gw-iaas1")
        rules = os.path.join(self.tmp, "in6.nft")
        with open(rules, "w") as f:
            f.write(self.h.get_state()["ns"]["gw-iaas1"]["nft"][TABLE])
        script = os.path.join(self.tmp, "probe.py")
        with open(script, "w") as f:
            f.write(PROBE)
        p = subprocess.run(["unshare", "-rn", sys.executable, "-I", script, rules], capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = json.loads(p.stdout)
        if self.evidence:
            with open(os.path.join(self.evidence, "real-kernel-probes.json"), "w") as f:
                json.dump(r, f, indent=1)
        # today (no filter): everything reaches the node
        self.assertEqual(r["before"], {"echo": "reply", "tcp_to_node": "connected", "udp_to_node": "delivered",
                                       "udp_closed": "refused", "tcp_from_node": "connected", "forwarded": "connected"})
        # with the filter: echo and the node's own connection pass, a connection request and UDP to the node are
        # dropped without an answer, forwarded traffic is untouched
        self.assertEqual(r["after"], {"echo": "reply", "tcp_to_node": "timeout", "udp_to_node": "timeout",
                                      "udp_closed": "timeout", "tcp_from_node": "connected", "forwarded": "connected"})
        m = re.search(r"counter packets (\d+)", r["ruleset"])
        self.assertTrue(m and int(m.group(1)) >= 2, r["ruleset"])


if __name__ == "__main__":
    unittest.main()

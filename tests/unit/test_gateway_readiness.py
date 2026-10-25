"""Readiness checks per address family (design/IPV6-PLAN.md §3.1 "Readiness", §11.5): the placement's route check
(networkplacement_provision gateway_routes.yml), the gateway and root waits for the IPv6 core and the per-family VPN
label check (cloudgateway_provision node_gateway.yml, hybridfabric_provision node_root.yml).

The checks are inline Python the tasks run over SSH (`python3 -c '<code>' <args>`). These tests render each task's
shell with Jinja2, take the code and the arguments out of it, and run the same code with the FRR and kernel output
replaced by fixtures:
- tests/fixtures/frr-s1-c9s/: real FRR 8.5.3 output from the offline 6VPE spike (design/ipv6/spike/results/c9s/
  run10-sb1-v6core-egicmp-plfilter/standby0/, dual-stack core): `show bgp vrf <vrf> ipv4|ipv6 unicast json` (bgp4/6)
  and `ip -j -4|-6 route show vrf <vrf>` (kernel4/6) on the gateway analog. B-64: VRF t51000 with the site's
  10.60.1.0/24 and 2602:81f:4005:1001::/64 (EVPN Type-5 from the site VTEP 10.65.0.11, the IPv6 route with the
  IPv4-mapped next hop ::ffff:10.65.0.11; site underlay 10.65.0.0/24) and VPN routes over the dual-stack core (IPv6
  next hops 2001:db8:6c0::1, ::3); B-128: plus the site's /128 host routes; B-v6only: the IPv6-only VRF t51002.
- the summary, IS-IS route and VPN detail JSON below is written in the shapes FRR 8.5.3's source prints
  (bgp_vty.c bgp_show_summary: peers{<ip>: {state}}, "summary established" lists only the sessions that are up and
  received the family; zebra_vty.c: {<prefix>: [{protocol, selected, installed}]}; bgp_route.c route_vty_out_detail:
  nhVrfName and remoteLabel per path), the spike kept no such JSON.
"""
import contextlib
import copy
import io
import json
import os
import re
import shlex
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
GW = os.path.join(ROLES, "cloudgateway_provision", "tasks", "node_gateway.yml")
RT = os.path.join(ROLES, "hybridfabric_provision", "tasks", "node_root.yml")
FIX = os.path.join(ROOT, "tests", "fixtures", "frr-s1-c9s")

T_ROUTES = "Read the gateway VRF's routes (waiting up to np_route_wait s for the site's)"
T_GW_WAIT = "{{ bgw_node.name }}: wait for the core (IS-IS, VPNv4 and VPNv6) and the upstream route reflectors"
T_GW_LABEL = "{{ bgw_node.name }}: every VRF's VPN label leads into it"
T_GW_READ = "{{ bgw_node.name }}: read the VRFs' routes"
T_GW_REC = "{{ bgw_node.name }}: record the node"
T_RT_WAIT = "{{ bgw_node.name }}: wait for BGP and IS-IS"
T_RT_LABEL = "{{ bgw_node.name }}: every root VRF's VPN label leads into it"
T_RT_REC = "{{ bgw_node.name }}: record the node"


def fixture(case, name):
    with open(os.path.join(FIX, case, name)) as f:
        return f.read()


def shell_of(path, name):
    t = by_name(load(path), name)
    sh = t["ansible.builtin.shell"]
    return sh["cmd"] if isinstance(sh, dict) else sh


def render(text, **variables):
    # Ansible's templating: trim_blocks on
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    env.filters["zip"] = lambda a, *b: list(zip(a, *b))
    return env.from_string(text).render(**variables)


def ansible_render(text, variables):
    """The same text templated by Ansible itself (a set_fact), to hold render() to Ansible's behaviour."""
    return run_play([{"ansible.builtin.set_fact": {"rendered": text}}, write_out("rendered")], variables)


def snippets(script):
    """[(code, args)] for every `python3 -c '<code>' <args>` of a rendered shell script."""
    out = []
    for m in re.finditer(r"python3 -c '\n([^']*?)\n' ([^\n]*)", script):
        out.append((m.group(1), shlex.split(m.group(2))))
    return out


def snippet(script):
    found = [s for s in snippets(script) if "def main(argv, run)" in s[0]]
    assert len(found) == 1, f"expected one check in the script, found {len(found)}"
    return found[0]


class Fake:
    """run(cmd) for the checks: answers from a {matcher: text} table, records the commands."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        line = " ".join(cmd)
        for key, text in self.answers.items():
            if key in line:
                return text
        raise AssertionError(f"unexpected command {line}")


def execute(code, args, answers):
    ns = {"__name__": "readiness_under_test"}
    exec(compile(code, "<inline check>", "exec"), ns)
    fake, buf = Fake(answers), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = ns["main"](args, fake)
    return rc, buf.getvalue(), fake.calls


# ---- placement: the routes per family ------------------------------------------------------------------------------

def route_vars(v4, v6=None, vrf="t51000", cidr="10.65.0.0/24"):
    v = {"np_gateway": {"metadata": {"name": "iaas1"}, "spec": {"siteUnderlay": {"cidr": cidr}}},
         "np_prefixes_v4": v4, "np_names": {"vrf": vrf}, "np_route_wait": 0}
    if v6 is not None:
        v["np_prefixes_v6"] = v6
    return v


class RouteCheckTest(unittest.TestCase):
    def check(self, variables, bgp4="{}", bgp6="{}", kernel6="[]"):
        code, args = snippet(render(shell_of(os.path.join(NP, "gateway_routes.yml"), T_ROUTES), **variables))
        rc, out, calls = execute(code, args, {"ipv4 unicast json": bgp4, "ipv6 unicast json": bgp6,
                                              "-6 route show vrf": kernel6})
        return rc, json.loads(out), calls, args

    def test_arguments(self):
        _, _, _, args = self.check(route_vars(["10.60.1.0/24"], ["2602:81f:4005:1001::/64"]),
                                   bgp4=fixture("B-64", "bgp4.json"), bgp6=fixture("B-64", "bgp6.json"),
                                   kernel6=fixture("B-64", "kernel6.json"))
        self.assertEqual(args, ["10.65.0.0/24", "10.60.1.0/24,2602:81f:4005:1001::/64", "gw-iaas1", "gw-iaas1", "t51000"])
        # np_prefixes_v6 not set (before subnets.yml knew it): IPv4 only
        _, _, _, args = self.check(route_vars(["10.60.1.0/24"]), bgp4=fixture("B-64", "bgp4.json"))
        self.assertEqual(args[1], "10.60.1.0/24")

    def test_ipv4_as_today(self):
        rc, out, calls, _ = self.check(route_vars(["10.60.1.0/24"]), bgp4=fixture("B-64", "bgp4.json"))
        self.assertEqual(rc, 0)
        # today's keys and values (10.60.2.0/24 is the other placement's, via the core loopback 10.255.0.3)
        self.assertEqual({k: out[k] for k in ("siteRoutes", "vpnRoutes", "missing")},
                         {"siteRoutes": 1, "vpnRoutes": 1, "missing": []})
        self.assertEqual(out["byFamily"], {"4": {"siteRoutes": 1, "vpnRoutes": 1, "missing": []}})
        # IPv4 reads BGP only, never the IPv6 table or the kernel
        self.assertEqual(calls, [["vtysh", "-N", "gw-iaas1", "-c", "show bgp vrf t51000 ipv4 unicast json"]])

    def test_ipv4_missing(self):
        rc, out, _, _ = self.check(route_vars(["10.60.9.0/24"]), bgp4=fixture("B-64", "bgp4.json"))
        self.assertEqual(rc, 1)
        self.assertEqual(out["missing"], ["10.60.9.0/24"])
        # a next hop outside the site underlay is VPN, not the site's
        rc, out, _, _ = self.check(route_vars(["10.60.1.0/24"], cidr="10.66.0.0/24"), bgp4=fixture("B-64", "bgp4.json"))
        self.assertEqual((rc, out["siteRoutes"], out["vpnRoutes"], out["missing"]), (1, 0, 2, ["10.60.1.0/24"]))

    def test_ipv6_mapped_next_hop_and_dual_stack_core(self):
        rc, out, _, _ = self.check(route_vars(["10.60.1.0/24"], ["2602:81f:4005:1001::/64"]),
                                   bgp4=fixture("B-64", "bgp4.json"), bgp6=fixture("B-64", "bgp6.json"),
                                   kernel6=fixture("B-64", "kernel6.json"))
        self.assertEqual(rc, 0, out)
        # IPv6: the site's /64 (via ::ffff:10.65.0.11); VPN: ::/0, 2001:db8:c0::/48 and the other placement's /64,
        # all via the core's IPv6 loopbacks
        self.assertEqual(out["byFamily"]["6"], {"siteRoutes": 1, "vpnRoutes": 3, "missing": [], "notInKernel": []})
        self.assertEqual((out["siteRoutes"], out["vpnRoutes"], out["missing"]), (2, 4, []))

    def test_ipv6_host_routes_inside_the_prefix(self):
        rc, out, _, _ = self.check(route_vars([], ["2602:81f:4005:1001::/64"]), bgp6=fixture("B-128", "bgp6.json"),
                                   kernel6=fixture("B-128", "kernel6.json"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(list(out["byFamily"]), ["6"])
        self.assertEqual(out["byFamily"]["6"]["siteRoutes"], 1)

    def test_ipv6_only_vrf(self):
        rc, out, calls, _ = self.check(route_vars([], ["2602:81f:4005:1101::/64"], vrf="t51002"),
                                       bgp6=fixture("B-v6only", "bgp6.json"), kernel6=fixture("B-v6only", "kernel6.json"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["missing"], [])
        self.assertFalse(any("ipv4" in " ".join(c) for c in calls))

    def test_ipv6_only_placement_never_passes_on_an_empty_table(self):
        empty = json.dumps({"vrfId": 7, "vrfName": "t51002", "routes": {}})
        kernel = json.dumps([{"type": "unreachable", "dst": "default", "dev": "lo", "metric": 4278198272}])
        rc, out, _, _ = self.check(route_vars([], ["2602:81f:4005:1101::/64"], vrf="t51002"), bgp6=empty, kernel6=kernel)
        self.assertEqual(rc, 1)
        self.assertEqual(out["missing"], ["2602:81f:4005:1101::/64"])
        self.assertEqual(out["byFamily"]["6"]["notInKernel"], ["2602:81f:4005:1101::/64"])

    def test_ipv6_in_bgp_but_not_in_the_kernel(self):
        # FRR holds the route as valid, zebra never installed it (the spike's IPv4-core failure)
        kernel = [r for r in json.loads(fixture("B-64", "kernel6.json")) if r.get("dst") != "2602:81f:4005:1001::/64"]
        rc, out, _, _ = self.check(route_vars([], ["2602:81f:4005:1001::/64"]), bgp6=fixture("B-64", "bgp6.json"),
                                   kernel6=json.dumps(kernel))
        self.assertEqual(rc, 1)
        self.assertEqual(out["byFamily"]["6"]["siteRoutes"], 1)
        self.assertEqual(out["byFamily"]["6"]["notInKernel"], ["2602:81f:4005:1001::/64"])

    def test_ipv6_plain_ipv4_next_hop_and_outside_the_underlay(self):
        bgp = json.loads(fixture("B-64", "bgp6.json"))
        path = bgp["routes"]["2602:81f:4005:1001::/64"][0]
        path["nexthops"][0]["ip"] = "10.65.0.11"
        rc, out, _, _ = self.check(route_vars([], ["2602:81f:4005:1001::/64"]), bgp6=json.dumps(bgp),
                                   kernel6=fixture("B-64", "kernel6.json"))
        self.assertEqual((rc, out["siteRoutes"]), (0, 1))
        path["nexthops"][0]["ip"] = "::ffff:10.66.0.11"
        rc, out, _, _ = self.check(route_vars([], ["2602:81f:4005:1001::/64"]), bgp6=json.dumps(bgp),
                                   kernel6=fixture("B-64", "kernel6.json"))
        self.assertEqual((rc, out["siteRoutes"], out["missing"]), (1, 0, ["2602:81f:4005:1001::/64"]))
        # a link-local next hop is never the site's
        path["nexthops"][0]["ip"] = "fe80::1"
        rc, out, _, _ = self.check(route_vars([], ["2602:81f:4005:1001::/64"]), bgp6=json.dumps(bgp),
                                   kernel6=fixture("B-64", "kernel6.json"))
        self.assertEqual(rc, 1)

    def test_missing_is_the_union_and_the_loop_waits_on_the_exit_code(self):
        rc, out, _, _ = self.check(route_vars(["10.60.1.0/24"], ["2602:81f:4005:1001::/64"]),
                                   bgp4=fixture("B-64", "bgp4.json"), bgp6=json.dumps({"routes": {}}), kernel6="[]")
        self.assertEqual(rc, 1)
        self.assertEqual(out["byFamily"]["4"]["missing"], [])
        self.assertEqual(out["missing"], ["2602:81f:4005:1001::/64"])
        # the wait loop ends on the check's exit code, not on a '"missing": []' substring (a family's own empty list
        # would end it early)
        sh = shell_of(os.path.join(NP, "gateway_routes.yml"), T_ROUTES)
        self.assertIn("out=$(check) && break", sh)
        self.assertNotIn('*\'"missing": []\'*', sh)

    def test_code_is_plain(self):
        # nothing in the code that Jinja2 or the shell's single quotes would change
        sh = shell_of(os.path.join(NP, "gateway_routes.yml"), T_ROUTES)
        code, _ = snippet(render(sh, **route_vars(["10.60.1.0/24"], [])))
        self.assertNotIn("'", code)
        self.assertIn(code, sh)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PrefixesV6Test(unittest.TestCase):
    def v6(self, result):
        tasks = ([{"ansible.builtin.set_fact": {"np_status": {}}}] + load(os.path.join(NP, "subnets.yml"))
                 + [write_out("{'v4': np_prefixes_v4, 'v6': np_prefixes_v6}")])
        return run_play(tasks, {"fab_ipam_result": result})

    def test_prefixes_and_rows(self):
        self.assertEqual(self.v6({"prefixes": ["10.60.4.0/24", "2001:db8:4000:1::/64"]}),
                         {"v4": ["10.60.4.0/24"], "v6": ["2001:db8:4000:1::/64"]})
        # a provider prefix only the IPAM's rows hold
        out = self.v6({"prefixes": ["10.60.8.0/29"], "subnets": [
            {"family": 6, "prefix": "2001:db8:1f16::/56", "source": "Provider"},
            {"family": 4, "prefix": "10.60.8.0/29", "source": "Network"}]})
        self.assertEqual(out, {"v4": ["10.60.8.0/29"], "v6": ["2001:db8:1f16::/56"]})
        self.assertEqual(self.v6({"prefixes": ["10.60.4.0/24"]})["v6"], [])


# ---- gateway and root: the IPv6 core --------------------------------------------------------------------------------

def summary(peers):
    return json.dumps({"routerId": "10.255.0.2", "as": 65010, "peers": {p: {"remoteAs": 65010, "state": s}
                                                                         for p, s in peers.items()}})


def isis(*prefixes, installed=True):
    out = {}
    for p in prefixes:
        r = {"prefix": p, "prefixLen": 128, "protocol": "isis", "selected": True, "distance": 115}
        if installed:
            r["installed"] = True
        out[p] = [r]
    return json.dumps(out)


GW_VARS = {"bgw_node": {"name": "bgw-1"}, "fab_name": "iaas1", "fab_spec": {"siteUnderlay": {}},
           "cg_root_pe": {"loopbackIp": "10.255.0.1", "loopbackIp6": "2602:81f:4005:ffff::1"},
           "cg_vrfs": [{"vrf": "t51000", "vni": 51000}]}


class GatewayWaitTest(unittest.TestCase):
    def script(self, loopback6):
        v = copy.deepcopy(GW_VARS)
        v["cg_pe"] = {"loopback6": loopback6}
        return render(shell_of(GW, T_GW_WAIT), **v)

    def test_ipv4_core_unchanged(self):
        sh = self.script("")
        self.assertNotIn("ipv6", sh)
        self.assertEqual([s for s in snippets(sh) if "def main" in s[0]], [])

    def run_check(self, established, plain, routes):
        code, args = snippet(self.script("2602:81f:4005:ffff::2/128"))
        self.assertEqual(args, ["gw-iaas1", "10.255.0.1", "2602:81f:4005:ffff::1"])
        return execute(code, args, {"summary established json": summary(established), "summary json": summary(plain),
                                    "show ipv6 route isis json": routes})

    def test_ipv6_core(self):
        sh = self.script("2602:81f:4005:ffff::2/128")
        self.assertIn("sysctl -n net.ipv6.conf.all.forwarding)\" = 1", sh)
        rc, out, calls = self.run_check({"10.255.0.1": "Established"}, {"10.255.0.1": "Established"},
                                        isis("2602:81f:4005:ffff::1/128"))
        self.assertEqual((rc, out.strip()), (0, "ipv6Vpn=Established"))
        self.assertTrue(all(c[:3] == ["vtysh", "-N", "gw-iaas1"] for c in calls))

    def test_session_up_but_vpnv6_not_negotiated(self):
        # the plain summary says Established although the route reflector has not activated VPNv6
        rc, out, _ = self.run_check({}, {"10.255.0.1": "Established"}, isis("2602:81f:4005:ffff::1/128"))
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=NoNeg"))
        rc, out, _ = self.run_check({}, {"10.255.0.1": "Active"}, isis("2602:81f:4005:ffff::1/128"))
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=Active"))

    def test_root_loopback_not_from_isis(self):
        est = {"10.255.0.1": "Established"}
        self.assertEqual(self.run_check(est, est, isis())[0], 1)
        self.assertEqual(self.run_check(est, est, isis("2602:81f:4005:ffff::1/128", installed=False))[0], 1)


ROOT_VARS = {"bgw_node": {"name": "bgw-1"}, "fab_spec": {"domainAsn": 65010},
             "fab_real": {"core": {"loopbackPool": "10.255.0.0/24", "loopbackPool6": "2602:81f:4005:ffff::/64"}},
             "hf_root": {"bridge": "br-core", "link": "10.255.1.1/24"}, "hf_node_egress": {}, "hf_node_vrfs": []}


class RootWaitTest(unittest.TestCase):
    def script(self, loopback6):
        v = copy.deepcopy(ROOT_VARS)
        v["hf_pe"] = {"loopback6": loopback6}
        return render(shell_of(RT, T_RT_WAIT), **v)

    def test_ipv4_core_unchanged(self):
        self.assertNotIn("ipv6", self.script(""))

    def run_check(self, pe4, plain6, neg6, routes):
        code, args = snippet(self.script("2602:81f:4005:ffff::1/128"))
        self.assertEqual(args, ["10.255.0.0/24", "2602:81f:4005:ffff::/64"])
        est = lambda ps: summary({p: "Established" for p in ps})
        return execute(code, args, {"ipv4 vpn summary established json": est(pe4),
                                    "ipv6 vpn summary established json": est(neg6),
                                    "ipv6 vpn summary json": summary(plain6),
                                    "show ipv6 route isis json": routes})

    def test_rollout_root_first(self):
        # the root has VPNv6, no gateway is rendered with it yet: nothing to wait for
        pes = ["10.255.0.2", "10.255.0.3"]
        rc, out, calls = self.run_check(pes, {p: "Established" for p in pes}, [], isis())
        self.assertEqual((rc, out.strip()), (0, "ipv6Vpn=NoNeg"))
        self.assertTrue(all(c[:2] == ["vtysh", "-c"] for c in calls))

    def test_partial_then_established(self):
        pes = ["10.255.0.2", "10.255.0.3"]
        up = {p: "Established" for p in pes}
        # .2 negotiated VPNv6: its IPv6 loopback (index 2) must be an IS-IS route; .3 not yet
        rc, out, _ = self.run_check(pes, up, ["10.255.0.2"], isis("2602:81f:4005:ffff::2/128"))
        self.assertEqual((rc, out.strip()), (0, "ipv6Vpn=Partial"))
        rc, out, _ = self.run_check(pes, up, ["10.255.0.2"], isis("2602:81f:4005:ffff::3/128"))
        self.assertEqual(rc, 1)
        rc, out, _ = self.run_check(pes, up, pes, isis("2602:81f:4005:ffff::2/128", "2602:81f:4005:ffff::3/128"))
        self.assertEqual((rc, out.strip()), (0, "ipv6Vpn=Established"))

    def test_pe_without_the_family(self):
        # a PE Established for VPNv4 that the VPNv6 summary doesn't list (the PE group lacks the family)
        rc, _, _ = self.run_check(["10.255.0.2"], {}, [], isis())
        self.assertEqual(rc, 1)

    def test_no_peers(self):
        rc, out, _ = self.run_check([], {}, [], isis())
        self.assertEqual((rc, out.strip()), (0, "ipv6Vpn=NoPeers"))


# ---- the VPN label per family ---------------------------------------------------------------------------------------

def vpn_detail(rds):
    """`show bgp ipv4|ipv6 vpn json detail` as FRR 8.5.3 prints it: {routes: {routeDistinguishers: {<rd>: {<prefix>:
    [path]}}}}. rds: {rd: [(prefix, kind, label, who)]}; kind "leak": a path leaked from VRF who (nhVrfName,
    remoteLabel); "evpn": the same for a route that came from EVPN (FRR prints no remoteLabel for it); "peer": a path
    received from peer who (peer.peerId, remoteLabel)."""
    out = {}
    for rd, entries in rds.items():
        table = out.setdefault(rd, {})
        for prefix, kind, label, who in entries:
            p = {"aspath": {"string": "Local"}, "origin": "IGP", "valid": True, "bestpath": {"overall": True},
                 "nexthops": [{"ip": "10.255.0.2", "afi": "ipv4", "used": True}]}
            if kind in ("leak", "evpn"):
                p.update({"nhVrfName": who, "nhVrfId": 4, "sourced": True, "local": True,
                          "peer": {"peerId": "0.0.0.0", "routerId": "10.255.0.2"}})
            else:
                p["peer"] = {"peerId": who, "routerId": who, "type": "internal"}
            if kind != "evpn":
                p["remoteLabel"] = label
            table.setdefault(prefix, []).append(p)
    return json.dumps({"vrfId": 0, "vrfName": "default", "routes": {"routeDistinguishers": out}})


MPLS = """16 dev t51000 proto bgp
17 dev t51001 proto bgp
16005 as to 16005 via inet 10.255.1.5 dev core0 proto isis
"""
GW_PE = {"loopbackIp": "10.255.0.2"}
LABEL_GW = {"bgw_node": {"name": "bgw-1"}, "fab_name": "iaas1", "cg_fabric": {"spec": {"domainAsn": 65010}}, "cg_pe": GW_PE}
LABEL_RT = {"bgw_node": {"name": "bgw-1"}, "fab_spec": {"domainAsn": 65010}}
DUAL = [{"vrf": "t51000", "vni": 51000, "families": [4, 6]}]
# the gateway's own tables: the site's routes came from EVPN, so no remoteLabel on them
OWN4 = vpn_detail({"65010:51000": [("10.60.0.0/24", "evpn", 0, "t51000")]})
OWN6 = vpn_detail({"65010:51000": [("2602:81f:4005:1001::/64", "evpn", 0, "t51000")]})
# the route reflector's: this gateway's paths (10.255.0.2), another gateway's under the same RD, another VRF's RD
RR4 = vpn_detail({"65010:51000": [("10.60.0.0/24", "peer", 16, "10.255.0.2"), ("10.60.1.0/24", "peer", 30, "10.255.0.3")],
                  "65010:51001": [("10.60.5.0/24", "peer", 17, "10.255.0.2")]})
RR6 = vpn_detail({"65010:51000": [("2602:81f:4005:1001::/64", "peer", 18, "10.255.0.2"),
                                  ("2602:81f:4005:1002::/64", "peer", 31, "10.255.0.3")]})


def label_answers(mpls, own4=OWN4, own6=OWN6, rr4=RR4, rr6=RR6):
    return {"-f mpls route": mpls, "-N gw-iaas1 -c show bgp ipv4 vpn json detail": own4,
            "-N gw-iaas1 -c show bgp ipv6 vpn json detail": own6,
            "vtysh -c show bgp ipv4 vpn json detail": rr4, "vtysh -c show bgp ipv6 vpn json detail": rr6}


class LabelCheckTest(unittest.TestCase):
    def code(self, path, name, variables):
        return snippet(render(shell_of(path, name), **variables))

    def gw(self, vrfs, asn=65010):
        v = copy.deepcopy(LABEL_GW)
        v["cg_fabric"]["spec"]["domainAsn"] = asn
        return self.code(GW, T_GW_LABEL, dict(v, cg_vrfs=vrfs))

    def test_same_code_on_gateway_and_root(self):
        g, gargs = self.gw([{"vrf": "t51000", "vni": 51000}, {"vrf": "t51001", "vni": 51001, "families": [4, 6]}])
        r, rargs = self.code(RT, T_RT_LABEL, dict(LABEL_RT, hf_node_vrfs=[{"vrf": "e100"}]))
        self.assertEqual(g, r)
        # <vrf>:<families>:<rd> with the template's RD (type 0 when the AS fits 16 bits)
        self.assertEqual(gargs, ["gw-iaas1", "gw-iaas1", "10.255.0.2", "t51000:4:65010:51000", "t51001:4,6:65010:51001"])
        self.assertEqual(rargs, ["", "", "", "e100:4"])
        self.assertNotIn("'", g)
        # a 4-byte AS: RD type 1, the loopback and the VNI's low 16 bits
        _, gargs = self.gw([{"vrf": "t70000", "vni": 70000}], asn=4200000001)
        self.assertEqual(gargs[3], "t70000:4:10.255.0.2:4464")

    def test_ipv4_only_as_today(self):
        code, args = self.gw([{"vrf": "t51000", "vni": 51000}, {"vrf": "t51001", "vni": 51001},
                              {"vrf": "t51002", "vni": 51002, "families": [4]}])
        rc, out, calls = execute(code, args, {"-f mpls route": MPLS})
        self.assertEqual((rc, out.split("\n")), (0, ["t51002 4", ""]))
        # IPv4 only: the MPLS table and nothing else
        self.assertEqual(calls, [["ip", "-n", "gw-iaas1", "-f", "mpls", "route"]])

    def test_labels_from_the_route_reflector(self):
        code, args = self.gw(DUAL)
        # IPv4's 16 is routed into the VRF, IPv6's 18 is not: only IPv6 is bound again
        rc, out, calls = execute(code, args, label_answers(MPLS))
        self.assertEqual(out, "t51000 6\n")
        self.assertIn(["vtysh", "-c", "show bgp ipv6 vpn json detail"], calls)
        self.assertIn(["vtysh", "-N", "gw-iaas1", "-c", "show bgp ipv6 vpn json detail"], calls)
        rc, out, _ = execute(code, args, label_answers(MPLS + "18 dev t51000 proto bgp\n"))
        self.assertEqual(out, "")
        # the other way round: IPv4's label went stale
        rc, out, _ = execute(code, args, label_answers("18 dev t51000 proto bgp\n17 dev t51000 proto bgp\n"))
        self.assertEqual(out, "t51000 4\n")

    def test_own_leaked_labels(self):
        # routes that did not come from EVPN show their label in the gateway's own table
        code, args = self.gw(DUAL)
        own4 = vpn_detail({"65010:51000": [("10.60.0.0/24", "leak", 16, "t51000")]})
        own6 = vpn_detail({"65010:51000": [("2602:81f:4005:1001::/64", "leak", 18, "t51000")]})
        empty = json.dumps({"routes": {"routeDistinguishers": {}}})
        rc, out, _ = execute(code, args, label_answers(MPLS, own4, own6, empty, empty))
        self.assertEqual(out, "t51000 6\n")

    def test_route_reflector_unreadable(self):
        code, args = self.gw(DUAL)
        answers = label_answers(MPLS + "18 dev t51000 proto bgp\n", rr4="not json", rr6="not json")
        # no label seen: the VRF needs one route per family, it has two
        rc, out, _ = execute(code, args, answers)
        self.assertEqual((rc, out), (0, ""))

    def test_family_without_exported_routes(self):
        # no IPv6 route exported yet: no label to read, the VRF needs a second label routed into it
        code, args = self.gw(DUAL)
        empty = json.dumps({"routes": {"routeDistinguishers": {}}})
        rc, out, _ = execute(code, args, label_answers(MPLS, rr6=empty, own6=empty))
        self.assertEqual(out, "t51000 6\n")
        rc, out, _ = execute(code, args, label_answers(MPLS + "18 dev t51000 proto bgp\n", rr6=empty, own6=empty))
        self.assertEqual(out, "")
        # neither family exported anything: two labels needed
        rc, out, _ = execute(code, args, label_answers(MPLS, empty, empty, empty, empty))
        self.assertEqual(out, "t51000 4\nt51000 6\n")

    def test_root_form(self):
        code, args = self.code(RT, T_RT_LABEL, dict(LABEL_RT, hf_node_vrfs=[{"vrf": "e100", "families": [4, 6]}]))
        v4 = vpn_detail({"10.255.0.1:100": [("0.0.0.0/0", "leak", 20, "e100"), ("10.60.0.0/24", "peer", 16, "10.255.0.2")]})
        v6 = vpn_detail({"10.255.0.1:100": [("::/0", "leak", 21, "e100")]})
        rc, out, calls = execute(code, args, {"-f mpls route": "20 dev e100 proto bgp\n21 dev e100 proto bgp\n",
                                              "show bgp ipv4 vpn json detail": v4, "show bgp ipv6 vpn json detail": v6})
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(calls[0], ["ip", "-f", "mpls", "route"])
        self.assertEqual([c[:2] for c in calls[1:]], [["vtysh", "-c"], ["vtysh", "-c"]])
        rc, out, _ = execute(code, args, {"-f mpls route": "20 dev e100 proto bgp\n",
                                          "show bgp ipv4 vpn json detail": v4, "show bgp ipv6 vpn json detail": v6})
        self.assertEqual(out, "e100 6\n")

    def test_rebind_under_the_family(self):
        for path, name in ((GW, T_GW_LABEL), (RT, T_RT_LABEL)):
            sh = shell_of(path, name)
            self.assertIn('-c "address-family ipv$f unicast"', sh)
            self.assertIn("-c 'no label vpn export auto' -c 'label vpn export auto'", sh)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AnsibleRenderTest(unittest.TestCase):
    """render() gives what Ansible gives (trim_blocks, filters) for every task the tests above run."""

    def same(self, path, name, variables):
        sh = shell_of(path, name)
        self.assertEqual(render(sh, **variables).rstrip("\n"), ansible_render(sh, variables).rstrip("\n"))

    def test_tasks(self):
        self.same(os.path.join(NP, "gateway_routes.yml"), T_ROUTES, route_vars(["10.60.1.0/24"], ["2001:db8::/64"]))
        for lo6 in ("", "2602:81f:4005:ffff::2/128"):
            self.same(GW, T_GW_WAIT, dict(copy.deepcopy(GW_VARS), cg_pe={"loopback6": lo6}))
            self.same(RT, T_RT_WAIT, dict(copy.deepcopy(ROOT_VARS), hf_pe={"loopback6": lo6}))
        self.same(GW, T_GW_LABEL, dict(LABEL_GW, cg_vrfs=[{"vrf": "t51000", "vni": 51000},
                                                          {"vrf": "t51001", "vni": 51001, "families": [4, 6]}]))
        self.same(RT, T_RT_LABEL, dict(LABEL_RT, hf_node_vrfs=[{"vrf": "e100", "families": []}]))


class ShellSyntaxTest(unittest.TestCase):
    """Every rendered script parses in bash (bash -n), the IPv6 core on and off."""

    def parse(self, path, name, variables):
        import subprocess
        sh = render(shell_of(path, name), **variables)
        proc = subprocess.run(["bash", "-n"], input=sh, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_scripts(self):
        self.parse(os.path.join(NP, "gateway_routes.yml"), T_ROUTES, route_vars(["10.60.1.0/24"], ["2001:db8::/64"]))
        for lo6 in ("", "2602:81f:4005:ffff::2/128"):
            self.parse(GW, T_GW_WAIT, dict(copy.deepcopy(GW_VARS), cg_pe={"loopback6": lo6}))
            self.parse(GW, T_GW_READ, dict(copy.deepcopy(GW_VARS), cg_pe={"loopback6": lo6}))
            self.parse(RT, T_RT_WAIT, dict(copy.deepcopy(ROOT_VARS), hf_pe={"loopback6": lo6}))
        for vrfs in ([], [{"vrf": "t51000", "vni": 51000}, {"vrf": "t51001", "vni": 51001, "families": [4, 6]}]):
            self.parse(GW, T_GW_LABEL, dict(LABEL_GW, cg_vrfs=vrfs))
            self.parse(RT, T_RT_LABEL, dict(LABEL_RT, hf_node_vrfs=[dict(v, vrf="e10%d" % i) for i, v in enumerate(vrfs)]))


class LabelScriptTest(unittest.TestCase):
    """The whole label task (wait, bind again under the family, check again) with ip, vtysh and sleep replaced."""

    SHIM_IP = '#!/bin/bash\ncat "$SHIM/mpls"\n'
    SHIM_SLEEP = "#!/bin/bash\nexit 0\n"
    SHIM_VTYSH = """#!/bin/bash
    case "$*" in
      *"configure terminal"*) echo "$*" >> "$SHIM/config"; [ -f "$SHIM/fix" ] && cat "$SHIM/fix" >> "$SHIM/mpls"; exit 0 ;;
      "-N gw-iaas1 -c show bgp ipv4 vpn json detail") cat "$SHIM/own4" ;;
      "-N gw-iaas1 -c show bgp ipv6 vpn json detail") cat "$SHIM/own6" ;;
      "-c show bgp ipv4 vpn json detail") cat "$SHIM/rr4" ;;
      "-c show bgp ipv6 vpn json detail") cat "$SHIM/rr6" ;;
      *) exit 1 ;;
    esac
    """

    def run_task(self, mpls, fix=None):
        import subprocess
        import tempfile
        import textwrap
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("ip", self.SHIM_IP), ("sleep", self.SHIM_SLEEP),
                               ("vtysh", textwrap.dedent(self.SHIM_VTYSH))):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
                os.chmod(os.path.join(d, name), 0o755)
            files = {"mpls": mpls, "own4": OWN4, "own6": OWN6, "rr4": RR4, "rr6": RR6}
            if fix is not None:
                files["fix"] = fix
            for name, body in files.items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            sh = render(shell_of(GW, T_GW_LABEL), **dict(LABEL_GW, cg_vrfs=DUAL))
            env = dict(os.environ, SHIM=d, PATH=d + os.pathsep + os.environ.get("PATH", ""))
            proc = subprocess.run(["bash", "-s"], input=sh, capture_output=True, text=True, env=env, timeout=60)
            config = ""
            if os.path.exists(os.path.join(d, "config")):
                with open(os.path.join(d, "config")) as f:
                    config = f.read()
            return proc, config

    def test_nothing_to_do(self):
        proc, config = self.run_task(MPLS + "18 dev t51000 proto bgp\n")
        self.assertEqual((proc.returncode, proc.stdout.strip(), config), (0, "rebound=", ""), proc.stderr)

    def test_rebind_only_ipv6(self):
        proc, config = self.run_task(MPLS, fix="18 dev t51000 proto bgp\n")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "rebound= t51000/ipv6"), proc.stderr)
        self.assertEqual(config.split("\n")[0], "-N gw-iaas1 -c configure terminal -c router bgp 65010 vrf t51000 "
                         "-c address-family ipv6 unicast -c no label vpn export auto -c label vpn export auto -c end")

    def test_still_missing(self):
        proc, _ = self.run_task(MPLS)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("VRF t51000: its IPv6 VPN label has no MPLS route into it, even after binding it again", proc.stderr)


# ---- the node records -----------------------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayRecordTest(unittest.TestCase):
    def record(self, loopback6, vrf_lines, verify_lines):
        task = by_name(load(GW), T_GW_REC)
        unchanged = {"changed": False}
        v = dict(copy.deepcopy(GW_VARS), cg_pe={"loopback6": loopback6, "loopbackIp": "10.255.0.2", "sid": 16002,
                                                "loopbackIp6": "2602:81f:4005:ffff::2" if loopback6 else ""},
                 cg_node_addr="10.231.0.2",
                 _cg_sessions={"stdout": json.dumps({"site": 2, "upstream": 0})}, _cg_tunnel={"stdout": ""},
                 _cg_vrf_routes={"stdout_lines": vrf_lines}, _cg_verify={"stdout_lines": verify_lines},
                 _cg_files=unchanged, _cg_conf=unchanged, _cg_apply=unchanged, _cg_daemons=unchanged,
                 _cg_frrconf=unchanged, _cg_state=unchanged)
        # inputs and outputs: facts, not extra vars (extra vars can't be overridden)
        start = {"ansible.builtin.set_fact": {"cg_node_results": [], "cg_changed_nodes": []}}
        return run_play([start, task, write_out("cg_node_results")], v)[0]

    def test_ipv4_shape(self):
        out = self.record("", ["t51000 4 2", "t51001 4 5"], [])
        self.assertEqual(out["vrfRoutes"], {"t51000": 2, "t51001": 5})
        self.assertEqual(out["ipv6Vpn"], "")
        self.assertTrue(out["message"].startswith("gateway instance gw-iaas1: VPNv4 to 10.255.0.1, IS-IS SR SID 16002"))

    def test_ipv6_shape(self):
        out = self.record("2602:81f:4005:ffff::2/128", ["t51000 4 2", "t51000 6 3"], ["ipv6Vpn=Established"])
        self.assertEqual(out["vrfRoutes"], {"t51000": {"4": 2, "6": 3}})
        self.assertEqual(out["ipv6Vpn"], "Established")
        self.assertIn("VPNv4 and VPNv6 to 10.255.0.1", out["message"])

    def test_read_task_families(self):
        sh = shell_of(GW, T_GW_READ)
        v = dict(copy.deepcopy(GW_VARS), cg_pe={"loopback6": ""})
        self.assertIn("for f in 4; do", render(sh, **v))
        v["cg_pe"]["loopback6"] = "2602:81f:4005:ffff::2/128"
        self.assertIn("for f in 4 6; do", render(sh, **v))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RootRecordTest(unittest.TestCase):
    def record(self, loopback6, verify_lines):
        task = by_name(load(RT), T_RT_REC)
        unchanged = {"changed": False}
        v = dict(copy.deepcopy(ROOT_VARS), hf_node_attach={},
                 hf_pe={"loopback6": loopback6, "loopbackIp": "10.255.0.1", "sid": 16001,
                        "loopbackIp6": "2602:81f:4005:ffff::1" if loopback6 else ""},
                 _hf_verify={"stdout_lines": verify_lines},
                 **{k: unchanged for k in ("_hf_files", "_hf_modules", "_hf_rootconf", "_hf_vrfgone", "_hf_unit",
                                           "_hf_apply", "_hf_daemons", "_hf_frrconf", "_hf_frr_state")})
        return run_play([task, write_out("hf_node_results")], v)[0]

    def test_records(self):
        out = self.record("", [])
        self.assertEqual(out["ipv6Vpn"], "")
        self.assertTrue(out["message"].startswith("root instance: VPNv4 route reflector, IS-IS SR (SID 16001), br-core"))
        out = self.record("2602:81f:4005:ffff::1/128", ["ipv6Vpn=Partial"])
        self.assertEqual(out["ipv6Vpn"], "Partial")
        self.assertIn("VPNv4 and VPNv6 route reflector", out["message"])


if __name__ == "__main__":
    unittest.main()

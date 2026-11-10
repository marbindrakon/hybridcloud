"""The cloud edge routers' ICMPv6 sources in the networks' source sets at the root (IPv6 egress filter).

A router of an AWS or Google Cloud site sends its Time Exceeded and Unreachable toward the fabric from its own /128 of
core.icmpSource6 (node ordinal 100 + its position, the gateway's provider-edge index). For a flow from the internet
those errors leave the network at the root through the network's egress veth, where the filter admits only the
network's src6. So:
- the gateway job reports the routers' addresses it configured (status.realization.edgeRouterIcmpSources6; [] on a
  site without them) and wakes the fabric when they change;
- the fabric job gives each network with IPv6 egress the addresses of the gateways its IPv6 placements from the
  network's space use (status.cloudGateway; a Provider-sourced or deleting placement adds nothing);
- egress-plan adds them to the network's src6 on every node, as /128s, only those inside core.icmpSource6;
- the apply puts them into set src6_<vpnTarget> of table inet shc_egress6, which the outbound chain reads on the
  forward and the output hook (both core transports).

Documentation and private ranges only."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_egress_ipv6 as E  # noqa: E402
import test_shc_fabric_apply as A  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HF = os.path.join(ROLES, "hybridfabric_provision", "tasks")
CG = os.path.join(ROLES, "cloudgateway_provision", "tasks")
POOL = "2001:db8:4005:1::/64"
R0, R1 = "2001:db8:4005:1::64:6", "2001:db8:4005:1::65:6"   # the routers of the gateway with index 6
CORE = dict(E.CORE6, icmpSource6=POOL)


def plan(edge, core=CORE):
    req = E.plan_request(core=core)
    req["networks"][0]["ipv6"]["edgeSources6"] = edge
    return F.egress_plan(req)


def src6(r, node, vrf="e10000"):
    return next(n for n in r["nodes"][node]["networks"] if n["vrf"] == vrf)["ipv6"]["src6"]


class Plan(unittest.TestCase):
    def test_the_routers_addresses_are_the_ones_the_gateway_computes(self):
        """the formula the gateway job uses (pe-addresses at node ordinal 100, 101), so the report holds these"""
        got = [F.pe_addresses({"core": dict(A.POOLS6, loopbackPool="10.255.0.0/24", linkSubnet="10.255.1.0/24"),
                               "index": 6, "nodeOrdinal": o})["icmp6"] for o in (100, 101)]
        self.assertEqual(got, [R0 + "/128", R1 + "/128"])

    def test_admitted_on_every_node(self):
        r = plan([R1 + "/128", R0])
        self.assertEqual(r["errors"], [])
        self.assertEqual(src6(r, "bgw-1"), [E.BLOCK, "2001:db8:4005:1::1:0/112", R0, R1])
        self.assertEqual(src6(r, "bgw-2"), [E.BLOCK, "2001:db8:4005:1::2:0/112", R0, R1])
        # only that network: the others are placed elsewhere
        self.assertEqual(src6(r, "bgw-1", "e10001"), ["2001:db8:4005:4300::/56", "2001:db8:4005:1::1:0/112"])

    def test_as_before_without_them(self):
        base = E.plan_request(core=CORE)
        self.assertEqual(plan([]), F.egress_plan(base))
        self.assertEqual(F.egress_plan(copy.deepcopy(base)), F.egress_plan(base))

    def test_only_inside_the_pool(self):
        """nothing but the platform's own ICMPv6 sources: an address outside core.icmpSource6, a prefix, IPv4 or
        garbage is left out; duplicates once"""
        r = plan([R0, R0, "2001:db8:4005:2::64:6", "2001:db8:4005:1::/112", "192.0.2.1", "nonsense", R1])
        self.assertEqual(src6(r, "bgw-1")[2:], [R0, R1])
        # no pool: none
        r = plan([R0, R1], core=E.CORE6)
        self.assertEqual(src6(r, "bgw-1"), [E.BLOCK])


class Apply(A.ApplyCase):
    def conf(self, extra=None):
        c = E.root_conf6()
        r = plan([R0, R1])
        net = next(n for n in r["nodes"]["bgw-1"]["networks"] if n["vrf"] == "e10000")
        c["egress"]["networks"][0]["ipv6"] = net["ipv6"]
        c.update(extra or {})
        return c

    def check(self, extra=None):
        h = self.h
        h.conf("root", self.conf(extra))
        out, log = h.ok(self, "root")
        nft = h.get_state()["ns"][""]["nft"]["inet shc_egress6"]
        # the routers' /128s in the set (outside the node's slice, so each stays a member of its own)
        self.assertIn("set src6_10000 { type ipv6_addr; flags interval; elements = { %s, 2001:db8:4005:1::1:0/112, %s, %s }; }"
                      % (E.BLOCK, R0, R1), nft)
        # one rule reads it, on both hooks the outbound chain is jumped to
        self.assertIn('oifname "xe10000" ip6 saddr != @src6_10000 counter drop comment "source outside the network\'s platform space"', nft)
        self.assertIn("chain out_forward { type filter hook forward priority filter; policy accept; meta nfproto ipv6 jump outbound; }", nft)
        self.assertIn("chain out_output { type filter hook output priority filter; policy accept; meta nfproto ipv6 jump outbound; }", nft)
        out2, _ = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        # gone again with the gateway's report
        h.conf("root", E.root_conf6() if not extra else dict(E.root_conf6(), **extra))
        h.ok(self, "root")
        self.assertNotIn(R0, h.get_state()["ns"][""]["nft"]["inet shc_egress6"])

    def test_mpls(self):
        self.check()

    def test_srv6(self):
        self.check(ROOT_SRV6)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricSelection(E.FabricSelection):
    def networks(self, nets, placements, v6on=True, gateways=None):
        t = by_name(load(os.path.join(HF, "main.yml")), "Set the networks with internet egress or platform reach")
        alloc = {"egress": {"HybridNetwork/acme/core": 0, "HybridNetwork/acme/t6": 1, "HybridNetwork/acme/v4": 2},
                 "vpnTarget": {"HybridNetwork/acme/core": 10000, "HybridNetwork/acme/t6": 10001, "HybridNetwork/acme/v4": 10002}}
        v = {"fab_name": "lab", "fab_ledger_result": {"ledger": {"alloc": alloc}},
             "_hf_networks": {"resources": nets}, "_hf_placements": {"resources": placements},
             "fab_real": {"egress": {"enabled": True, "ipv6": {"enabled": v6on}}}, "fab_st": {}}
        if gateways is not None:
            v["_hf_edge_gateways"] = {"resources": gateways}
        return run_play([t, write_out("hf_egress_networks")], v)

    def gw(self, name, edge, fabric="lab"):
        st = {"realization": {"edgeRouterIcmpSources6": edge}} if edge is not None else {}
        return {"metadata": {"namespace": "fabric", "name": name}, "spec": {"fabricRef": {"name": fabric}}, "status": st}

    def pl_on(self, name, network, subnets, gateway, deleting=False):
        p = self.pl(name, network, subnets, deleting)
        p["status"]["cloudGateway"] = gateway
        return p

    def test_edge_sources(self):
        nets = [self.net("core", ["10.60.0.0/16"], "Platform", E.BLOCK)]
        gws = [self.gw("aws-use2", [R1, R0]), self.gw("gcp-use4", ["2001:db8:4005:1::64:7"]), self.gw("iaas1", []),
               self.gw("other", ["2001:db8:4005:1::64:9"], fabric="elsewhere"), self.gw("new", None)]
        pls = [self.pl_on("core-aws", "core", [{"family": 6, "prefix": E.P64[0], "source": "Network"}], "aws-use2"),
               self.pl_on("core-aws2", "core", [{"family": 6, "prefix": E.P64[1]}], "aws-use2"),
               self.pl_on("core-gcp", "core", [{"family": 6, "prefix": "2001:db8:ffff:1::/64", "source": "Provider"}], "gcp-use4"),
               self.pl_on("core-metal", "core", [{"family": 6, "prefix": "2001:db8:4005:4203::/64"}], "iaas1"),
               self.pl_on("core-gone", "core", [{"family": 6, "prefix": "2001:db8:4005:4204::/64"}], "other", deleting=True),
               self.pl_on("core-new", "core", [{"family": 4, "prefix": "10.60.9.0/24"}], "new")]
        out = {e["network"]: e for e in self.networks(nets, pls, gateways=gws)}
        # only the routers of the gateway a Network-sourced IPv6 placement uses, each once, sorted
        self.assertEqual(out["acme/core"]["ipv6"]["edgeSources6"], [R0, R1])
        # no gateway list (IPv6 egress off, or none reports): the entry as before
        out = {e["network"]: e for e in self.networks(nets, pls)}
        self.assertNotIn("edgeSources6", out["acme/core"]["ipv6"])
        gws = [self.gw("aws-use2", []), self.gw("iaas1", [])]
        out = {e["network"]: e for e in self.networks(nets, pls, gateways=gws)}
        self.assertNotIn("edgeSources6", out["acme/core"]["ipv6"])

    def test_listing_only_with_ipv6_egress(self):
        t = by_name(load(os.path.join(HF, "main.yml")), "List the CloudGateways for their edge routers' ICMPv6 sources")
        self.assertEqual(t["when"], "(fab_real.egress.ipv6 | default({}, true)).enabled | default(false) | bool")
        self.assertEqual(t["kubernetes.core.k8s_info"]["kind"], "CloudGateway")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Gateway(unittest.TestCase):
    def results(self, addrs):
        return {"results": [{"item": {"name": f"r{i}", "instance": f"i-{i}"}, "stdout": '{"errors": [], "icmp6": "%s/128"}' % a}
                            for i, a in enumerate(addrs)]}

    def test_aws_report(self):
        tasks = load(os.path.join(CG, "site_aws_post.yml"))
        blk = by_name(tasks, "The IPv6 side of the routers' VRFs")["block"]
        t = by_name(blk, "Remember the routers' ICMPv6 sources for their names")
        res = self.results([R0, R1])
        res["results"].append({"item": {"name": "r9"}, "skipped": True})
        res["results"].append({"item": {"name": "r8"}, "stdout": '{"errors": ["does not fit"], "icmp6": ""}'})
        out = run_play([{"ansible.builtin.set_fact": {"cg_edge_icmp6": t["ansible.builtin.set_fact"]["cg_edge_icmp6"]}},
                        write_out("cg_edge_icmp6")], {"_cg_aws_icmp6": res})
        self.assertEqual(sorted(out), [R0, R1])

    def test_gcp_report(self):
        tasks = load(os.path.join(CG, "site_gcp_post.yml"))
        blk = by_name(tasks, "The routers' ICMPv6 sources")["block"]
        t = by_name(blk, "Remember the routers' ICMPv6 sources for their names")
        out = run_play([{"ansible.builtin.set_fact": {"cg_edge_icmp6": t["ansible.builtin.set_fact"]["cg_edge_icmp6"]}},
                        write_out("cg_edge_icmp6")], {"_cg_gcp_icmp6": self.results([R0, R1])})
        self.assertEqual(sorted(out), [R0, R1])

    def status(self, edge):
        import test_frr_templates as T
        t = by_name(load(os.path.join(CG, "main.yml")), "Report the gateway ready")
        v = {"cg_status": {"site": {}}, "cg_site_extra": {}, "cg_node_results": [], "cg_instance": {},
             "fab_real": {"model": "linux-pe"}, "fab_name": "site1", "cg_vrfs": [],
             "cg_pe": F.pe_addresses({"core": T.REAL["core"], "index": 2}),
             "cg_root_pe": F.pe_addresses({"core": T.REAL["core"], "index": 1})}
        if edge is not None:
            v["cg_edge_icmp6"] = edge
        return run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")], v)

    def test_status(self):
        self.assertEqual(self.status([R1, R0, R0])["realization"]["edgeRouterIcmpSources6"], [R0, R1])
        self.assertEqual(self.status([])["realization"]["edgeRouterIcmpSources6"], [])
        self.assertEqual(self.status(None)["realization"]["edgeRouterIcmpSources6"], [])

    def test_wakes_the_fabric_on_a_change(self):
        t = by_name(load(os.path.join(CG, "main.yml")), "Wake the fabric when the edge routers' ICMPv6 sources changed")
        self.assertEqual(t["vars"]["nudge_crs"],
                         [{"kind": "HybridFabric", "namespace": "{{ fab_namespace }}", "name": "{{ cg_fabric_name }}"}])
        cond = t["when"]
        for now, before, want in (([R0, R1], [], True), ([R1, R0], [R0, R1], False), ([], [R0], True),
                                  ([], None, False), ([R0], None, True)):
            fab_st = {"realization": {"edgeRouterIcmpSources6": before}} if before is not None else {}
            got = run_play([{"ansible.builtin.set_fact": {"_w": "{{ %s }}" % cond}}, write_out("_w | bool")],
                           {"cg_edge_icmp6": now, "fab_st": fab_st})
            self.assertIs(got, want, (now, before))


if __name__ == "__main__":
    unittest.main()

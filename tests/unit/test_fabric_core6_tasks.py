"""The core's IPv6 transport in the CloudGateway and HybridFabric jobs: the gateway's VRFs carry their placement's
address families and prefixes, the root VRFs IPv4 only, and the apply configurations (gw-<site>.json, root.json)
gain loopback6 / icmp6 / loopbackPool6 / icmpSource6 (empty strings on an IPv4-only core). The tasks run in
ansible-playbook (skipped when it is absent). Documentation and private ranges only."""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
CG = os.path.join(ROLES, "cloudgateway_provision", "tasks")
HF = os.path.join(ROLES, "hybridfabric_provision", "tasks")
REAL = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]
CORE6 = dict(REAL["core"], loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
NODES = [{"name": "bgw-1"}, {"name": "bgw-2"}]


def placement(name, vni, vt, subnets=None, prefixes=None, gateway="site1", release=False):
    st = {"cloudGateway": gateway, "realization": {"l3Vni": vni, "vpnTarget": vt, "gatewayRelease": release}}
    if subnets is not None:
        st["subnets"] = subnets
    if prefixes is not None:
        st["prefixes"] = prefixes
    return {"metadata": {"namespace": "entity-acme", "name": name}, "status": st}


def copy_as_fact(task, fact):
    """A copy task's content expression (and its vars) as a set_fact, so the play can return it."""
    t = {"name": task["name"], "vars": task.get("vars", {}),
         "ansible.builtin.set_fact": {fact: task["ansible.builtin.copy"]["content"]}}
    return t


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayVrfs(unittest.TestCase):
    def vrfs(self, placements):
        t = copy.deepcopy(by_name(load(os.path.join(CG, "main.yml")), "Set the gateway's VRFs"))
        return run_play([t, write_out("cg_vrfs")],
                        {"fab_name": "site1", "_cg_placements": {"resources": placements},
                         "cg_fabric": {"spec": {"overlayMtu": 9000}}, "fab_spec": {"transport": "none"},
                         "cg_instance": {}, "fab_st": {}})

    def test_families_and_prefixes(self):
        out = self.vrfs([
            placement("dual", 51001, 10001, subnets=[{"family": 4, "prefix": "10.60.1.0/24", "source": "Network"},
                                                     {"family": 6, "prefix": "2001:db8:100:1::/64", "source": "Network"}]),
            placement("v4", 51000, 10000, subnets=[{"family": 4, "prefix": "10.60.0.0/24", "source": "Network"}]),
            placement("v6only", 51002, 10000, subnets=[{"family": 6, "prefix": "2001:db8:100:2::/64", "source": "Network"}]),
            placement("old", 51003, 10002, prefixes=["10.60.3.0/24"]),
            placement("new", 51004, 10003),
            placement("other", 51005, 10004, subnets=[], gateway="site2"),
            placement("gone", 51006, 10005, subnets=[], release=True),
        ])
        self.assertEqual([v["vrf"] for v in out], ["t51000", "t51001", "t51002", "t51003", "t51004"])
        by = {v["vrf"]: v for v in out}
        self.assertEqual(by["t51000"], {"placement": "entity-acme/v4", "vrf": "t51000", "vni": 51000, "vpnTarget": 10000,
                                        "mtu": 9000, "families": [4], "prefixes4": ["10.60.0.0/24"], "prefixes6": []})
        self.assertEqual((by["t51001"]["families"], by["t51001"]["prefixes6"]), ([4, 6], ["2001:db8:100:1::/64"]))
        self.assertEqual((by["t51002"]["families"], by["t51002"]["prefixes4"]), ([6], []))
        # a status from before per-family subnets, and one without any prefix yet
        self.assertEqual((by["t51003"]["families"], by["t51003"]["prefixes4"]), ([4], ["10.60.3.0/24"]))
        self.assertEqual((by["t51004"]["families"], by["t51004"]["prefixes4"], by["t51004"]["prefixes6"]), ([4], [], []))

    def test_status_realization(self):
        t = by_name(load(os.path.join(CG, "main.yml")), "Report the gateway ready")
        expr = t["vars"]["fab_status"]
        vrfs = [{"placement": "entity-acme/dual", "vrf": "t51001", "vni": 51001, "vpnTarget": 10001, "mtu": 9000,
                 "families": [4, 6], "prefixes4": ["10.60.1.0/24"], "prefixes6": ["2001:db8:100:1::/64"]}]
        common = {"cg_status": {"site": {}}, "cg_site_extra": {}, "cg_node_results": [], "cg_instance": {},
                  "fab_real": {"model": "linux-pe"}, "fab_name": "site1", "cg_vrfs": vrfs}
        for core, lo6 in ((REAL["core"], ""), (CORE6, "2001:db8:4005:ffff::2")):
            out = run_play([{"ansible.builtin.set_fact": {"_s": expr}}, write_out("_s")],
                           dict(common, cg_pe=F.pe_addresses({"core": core, "index": 2}),
                                cg_root_pe=F.pe_addresses({"core": core, "index": 1})))
            r = out["realization"]
            self.assertEqual(r["loopback6"], lo6)
            self.assertEqual(r["vrfs"], [{"placement": "entity-acme/dual", "vrf": "t51001", "vni": 51001,
                                          "vpnTarget": 10001, "families": [4, 6]}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayConfig(unittest.TestCase):
    def tasks(self):
        return load(os.path.join(CG, "node_gateway.yml"))

    def config(self, core, node, ipv6_core=True):
        pe = F.pe_addresses({"core": core, "index": 3})
        ordinal = [n["name"] for n in NODES].index(node) + 1
        check = F.pe_addresses({"core": core, "index": 3, "nodeOrdinal": ordinal})
        t = copy_as_fact(by_name(self.tasks(), "{{ bgw_node.name }}: gateway instance config"), "_c")
        return json.loads(run_play([t, write_out("_c")],
                                   {"bgw_node": {"name": node}, "fab_name": "site1", "cg_instance": {"type": "gateway"},
                                    "cg_node_addr": "192.0.2.4", "fab_spec": {"siteUnderlay": {"cidr": "192.0.2.0/24"}},
                                    "cg_pe": pe, "fab_real": {"core": core}, "fab_check_result": check,
                                    "cg_ipv6_core": ipv6_core and core.get("loopbackPool6", "") != ""}))

    def test_ipv4_core(self):
        c = self.config(REAL["core"], "bgw-1")
        self.assertEqual(c, {"type": "gateway", "address": "192.0.2.4/24", "loopback6": "", "icmp6": "",
                             "loopbackPool6": "", "icmpSource6": ""})

    def test_dual_stack_core(self):
        c = self.config(CORE6, "bgw-2")
        self.assertEqual((c["loopback6"], c["icmp6"], c["loopbackPool6"], c["icmpSource6"]),
                         ("2001:db8:4005:ffff::3/128", "2001:db8:4005:1::2:3/128", "2001:db8:4005:ffff::/64",
                          "2001:db8:4005:1::/64"))

    def test_node_ordinal(self):
        t = by_name(self.tasks(), "{{ bgw_node.name }}: the gateway's ICMPv6 source on this node")
        expr = t["vars"]["fab_check"]["nodeOrdinal"]
        out = run_play([{"ansible.builtin.set_fact": {"_o": expr}}, write_out("_o")],
                       {"cg_fabric": {"spec": {"borderGateway": {"nodes": NODES}}}, "bgw_node": {"name": "bgw-2"}})
        self.assertEqual(int(out), 2)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayIPv6Gate(unittest.TestCase):
    """The gateway takes the IPv6 core only once the fabric reports it (HybridFabric status.realization.ipv6Core):
    before that, on a core with the IPv6 pools, it renders and applies what an IPv4-only core gets (the pools stay in
    the apply configuration for the stray removal)."""

    def run_gate(self, fabric, core=CORE6, node="bgw-2"):
        main = load(os.path.join(CG, "main.yml"))
        gate = copy.deepcopy(by_name(main, "Take the IPv6 core only once the fabric offers it"))
        tasks = load(os.path.join(CG, "node_gateway.yml"))
        icmp = by_name(tasks, "{{ bgw_node.name }}: the gateway's ICMPv6 source on this node")
        conf = copy_as_fact(by_name(tasks, "{{ bgw_node.name }}: gateway instance config"), "_c")
        status = by_name(main, "Report the gateway ready")["vars"]["fab_status"]
        ordinal = [n["name"] for n in NODES].index(node) + 1
        result = ("{'on': cg_ipv6_core, 'pe': cg_pe, 'root': cg_root_pe, 'icmpTask': _icmp_task, "
                  "'conf': _c | from_json, 'status': _s}")
        # cg_pe / cg_root_pe are facts in the job (extra vars would win over the gate's set_fact)
        facts = {"ansible.builtin.set_fact": {"cg_pe": "{{ _pe }}", "cg_root_pe": "{{ _root }}"}}
        return run_play([facts, gate,
                         {"ansible.builtin.set_fact": {"_icmp_task": "{{ (%s) }}" % ") and (".join(icmp["when"])}},
                         conf, {"ansible.builtin.set_fact": {"_s": status}}, write_out(result)],
                        {"cg_fabric": fabric, "fab_real": {"core": core, "model": "linux-pe"},
                         "_pe": F.pe_addresses({"core": core, "index": 3}),
                         "_root": F.pe_addresses({"core": core, "index": 1}),
                         # what the per-node ICMPv6 lookup returns when it runs
                         "fab_check_result": F.pe_addresses({"core": core, "index": 3, "nodeOrdinal": ordinal}),
                         "bgw_node": {"name": node}, "fab_name": "site1", "cg_instance": {"type": "gateway"},
                         "cg_node_addr": "192.0.2.4", "fab_spec": {"siteUnderlay": {"cidr": "192.0.2.0/24"}},
                         "cg_status": {"site": {}}, "cg_site_extra": {}, "cg_node_results": [], "cg_vrfs": []})

    def assert_ipv4_only(self, out):
        self.assertFalse(out["on"])
        for pe in (out["pe"], out["root"]):
            self.assertEqual((pe["loopback6"], pe["loopbackIp6"], pe["sid6"]), ("", "", 0))
        self.assertFalse(out["icmpTask"])
        c = out["conf"]
        self.assertEqual((c["loopback6"], c["icmp6"]), ("", ""))
        # the pools stay: an earlier configuration's addresses inside them are still removed
        self.assertEqual((c["loopbackPool6"], c["icmpSource6"]), ("2001:db8:4005:ffff::/64", "2001:db8:4005:1::/64"))
        self.assertEqual(out["status"]["realization"]["loopback6"], "")
        # the IPv4 side is untouched
        self.assertEqual((out["pe"]["loopbackIp"], out["pe"]["sid"]), ("10.255.0.3", 16003))

    def test_fabric_without_ipv6_core(self):
        for st in (None, {}, {"realization": {"routeReflector": "10.255.0.1"}},
                   {"realization": {"routeReflector6": "", "ipv6Core": False}}):
            with self.subTest(status=st):
                fabric = {"spec": {}} if st is None else {"spec": {}, "status": st}
                self.assert_ipv4_only(self.run_gate(fabric))

    def test_fabric_with_ipv6_core(self):
        out = self.run_gate({"spec": {}, "status": {"ready": True, "realization": {
            "routeReflector6": "2001:db8:4005:ffff::1", "ipv6Core": True}}})
        self.assertTrue(out["on"])
        self.assertEqual((out["pe"]["loopback6"], out["pe"]["sid6"]), ("2001:db8:4005:ffff::3/128", 17003))
        self.assertEqual(out["root"]["loopbackIp6"], "2001:db8:4005:ffff::1")
        self.assertTrue(out["icmpTask"])
        self.assertEqual((out["conf"]["loopback6"], out["conf"]["icmp6"]),
                         ("2001:db8:4005:ffff::3/128", "2001:db8:4005:1::2:3/128"))
        self.assertEqual(out["status"]["realization"]["loopback6"], "2001:db8:4005:ffff::3")

    def test_ipv4_only_core_ignores_the_fabric_flag(self):
        out = self.run_gate({"spec": {}, "status": {"realization": {"ipv6Core": True}}}, core=REAL["core"])
        self.assertFalse(out["on"])
        self.assertFalse(out["icmpTask"])
        self.assertEqual((out["conf"]["loopback6"], out["conf"]["loopbackPool6"]), ("", ""))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RootConfig(unittest.TestCase):
    def tasks(self):
        return load(os.path.join(HF, "node_root.yml"))

    def config(self, core, node):
        pe = F.pe_addresses({"core": core, "index": 1})
        check = F.pe_addresses({"core": core, "index": 1, "nodeOrdinal": [n["name"] for n in NODES].index(node) + 1})
        t = copy_as_fact(by_name(self.tasks(), "{{ bgw_node.name }}: root instance config"), "_c")
        return json.loads(run_play([t, write_out("_c")],
                                   {"bgw_node": {"name": node}, "hf_root": {"type": "root"}, "hf_node_egress": {},
                                    "hf_node_attach": {}, "hf_pe": pe, "fab_real": {"core": core},
                                    "fab_check_result": check}))

    def test_ipv4_core(self):
        self.assertEqual(self.config(REAL["core"], "bgw-1"),
                         {"type": "root", "loopback6": "", "icmp6": "", "loopbackPool6": "", "icmpSource6": ""})

    def test_dual_stack_core(self):
        c = self.config(CORE6, "bgw-1")
        self.assertEqual((c["loopback6"], c["icmp6"]), ("2001:db8:4005:ffff::1/128", "2001:db8:4005:1::1:1/128"))

    def test_node_ordinal(self):
        t = by_name(self.tasks(), "{{ bgw_node.name }}: the root's ICMPv6 source on this node")
        out = run_play([{"ansible.builtin.set_fact": {"_o": t["vars"]["fab_check"]["nodeOrdinal"]}}, write_out("_o")],
                       {"fab_spec": {"borderGateway": {"nodes": NODES}}, "bgw_node": {"name": "bgw-1"}})
        self.assertEqual(int(out), 1)

    def test_root_vrf_families(self):
        t = copy.deepcopy(by_name(load(os.path.join(HF, "main.yml")), "Keep the attachment plan"))
        plan = {"errors": [], "status": [], "skipped": [], "nodes": {},
                "vrfs": {"bgw-1": [{"vrf": "e10000", "vpnTarget": 10000}], "bgw-2": []}}
        out = run_play([t, write_out("hf_attach")], {"fab_check_result": plan, "fab_real": {"attachments": {}}, "fab_st": {}})
        self.assertEqual(out["vrfs"], {"bgw-1": [{"vrf": "e10000", "vpnTarget": 10000, "families": [4]}], "bgw-2": []})
        self.assertEqual(out["status"], [])

    def test_status_realization(self):
        t = by_name(load(os.path.join(HF, "main.yml")), "Report the fabric ready")
        common = {"fab_name": "lab", "hf_ledger": {"allocated": {}, "available": {}}, "hf_node_results": [],
                  "hf_egress_status": [], "hf_attach": {"status": [], "skipped": []}, "hf_ipam": {}}
        for core, rr6, on in ((REAL["core"], "", False), (CORE6, "2001:db8:4005:ffff::1", True)):
            out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")],
                           dict(common, fab_real=dict(REAL, core=core), hf_pe=F.pe_addresses({"core": core, "index": 1})))
            self.assertEqual((out["realization"]["routeReflector6"], out["realization"]["ipv6Core"]), (rr6, on))


if __name__ == "__main__":
    unittest.main()

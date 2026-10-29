"""The fabric orphan check's node read (fabric_orphan_check nodes.yml): the gw-* namespaces and the root with their
VRF devices, the root's circuit devices and BFD sessions, and the IPv6 leftovers shc-fabric-apply gives every VRF
(design/IPV6-PLAN.md §3.1 "Kernel"): an `unreachable` IPv6 default (metric 4278198272) in a table no VRF device of
the namespace uses, and an address from `core.icmpSource6` on a device that is no VRF. Those of a live VRF are owned.

The read is inline Python the task runs over SSH (a heredoc). These tests render the task's shell with Jinja2, take
the code out of it and run its main() with ip and vtysh answered from fixtures (iproute2 JSON shapes: `ip -j -d link
show type vrf` with linkinfo.info_data.table, `ip -j -6 route show table all type unreachable` with the table as text
and no table key for the main table, `ip -j -6 addr show` with addr_info). Documentation prefixes only."""
import json
import os
import re
import subprocess
import sys
import textwrap
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "fabric_orphan_check", "tasks")
NODES, MAIN = os.path.join(ROLE, "nodes.yml"), os.path.join(ROLE, "main.yml")
T_LIST = "List the gateway namespaces and their VRFs"
T_KEEP = "Keep the nodes"
T_COLLECT = "Collect the findings"

POOL = "2001:db8:4005:1::/64"
VARS = {"foc_parents": ["bond1"], "foc_pool": [3000, 3099], "fab_real": {"core": {"icmpSource6": POOL}}}


def vrf(name, table):
    return {"ifname": name, "linkinfo": {"info_kind": "vrf", "info_data": {"table": table}}}


def unreachable(table=None, metric=4278198272, dst="default"):
    r = {"type": "unreachable", "dst": dst, "metric": metric, "flags": [], "pref": "medium"}
    if table is not None:
        r["table"] = str(table)
    return r


def addrs(ifname, *locals_):
    return {"ifname": ifname, "addr_info": [{"family": "inet6", "local": a, "prefixlen": 128, "scope": "global"} for a in locals_]}


# bgw-1: gw-iaas1 holds t51000 (its default and ICMPv6 source owned) and leftovers of a VRF t51003 that went away
# (its table's default, its source on the bridge that stayed behind); gw-atuin has no VRF but a default left in table
# 51002; the root holds e10000 and the egress VRF with their defaults, the node's own unreachable default in the main
# table (another metric: not the apply script's) and an unreachable route that is no default
ANSWERS = {
    "ip netns list": "gw-iaas1 (id: 0)\ngw-atuin (id: 1)\n",
    "ip netns exec gw-iaas1 ip -j -d link show type vrf": json.dumps([vrf("t51000", 51000)]),
    "ip netns exec gw-iaas1 ip -j -6 route show table all type unreachable": json.dumps([unreachable(51000), unreachable(51003)]),
    "ip netns exec gw-iaas1 ip -j -6 addr show": json.dumps([
        addrs("lo", "::1", "2001:db8:4005:ffff::2"), addrs("t51000", "2001:db8:4005:1::1:2"),
        addrs("br51003", "2001:db8:4005:1::1:2")]),
    "ip netns exec gw-atuin ip -j -d link show type vrf": "[]",
    "ip netns exec gw-atuin ip -j -6 route show table all type unreachable": json.dumps([unreachable(51002)]),
    "ip netns exec gw-atuin ip -j -6 addr show": json.dumps([addrs("lo", "::1", "2001:db8:4005:ffff::5")]),
    "ip -j -d link show type vrf": json.dumps([vrf("e10000", 110000), vrf("egress", 9000)]),
    "ip -j -6 route show table all type unreachable": json.dumps([
        unreachable(110000), unreachable(9000), unreachable(metric=1024), unreachable(110001, dst="2001:db8:ffff::/48", metric=1024)]),
    "ip -j -6 addr show": json.dumps([addrs("lo", "::1", "2001:db8:4005:ffff::1"), addrs("e10000", "2001:db8:4005:1::1:1"),
                                      addrs("egress", "2001:db8:4005:1::1:1")]),
    "ip -j -d link show type vlan": json.dumps([{"ifname": "bond1.3001", "link": "bond1", "master": "e10001",
                                                 "linkinfo": {"info_kind": "vlan", "info_data": {"id": 3001}}}]),
    "vtysh -c show bfd peers json": json.dumps([{"peer": "10.255.128.3", "vrf": "e10001", "interface": "bond1.3001", "status": "up"}]),
}


class Fake:
    """run(cmd): the fixture for exactly that command line; records the calls."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        line = " ".join(cmd)
        if line not in self.answers:
            raise AssertionError(f"unexpected command {line}")
        return self.answers[line]


def shell(variables=VARS):
    task = by_name(load(NODES), T_LIST)
    sh = task["ansible.builtin.shell"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    env.filters["to_json"] = json.dumps
    return env.from_string(sh["cmd"] if isinstance(sh, dict) else sh).render(**variables)


def code(variables=VARS):
    m = re.search(r"python3 - <<'PY'\n(.*)\nPY\s*$", shell(variables), re.S)
    assert m, "no heredoc in the task"
    return textwrap.dedent(m.group(1))


def execute(answers, variables=VARS, pool=POOL):
    ns = {"__name__": "orphan_nodes_under_test"}
    exec(compile(code(variables), "<node read>", "exec"), ns)
    fake = Fake(answers)
    return ns["main"](fake, variables["foc_parents"], variables["foc_pool"][0], variables["foc_pool"][1], pool), fake.calls


class NodeReadTest(unittest.TestCase):
    def test_vrfs_devices_and_bfd_as_before(self):
        out, _ = execute(ANSWERS)
        self.assertEqual(out["netns"], {"gw-iaas1": ["t51000"], "gw-atuin": [], "root": ["e10000", "egress"]})
        self.assertEqual(out["rootDevices"], [{"name": "bond1.3001", "master": "e10001"}])
        self.assertEqual(out["rootBfd"], [{"peer": "10.255.128.3", "vrf": "e10001", "interface": "bond1.3001", "status": "up"}])

    def test_ipv6_leftovers_of_no_vrf(self):
        out, _ = execute(ANSWERS)
        self.assertEqual(out["ipv6Orphans"], [
            "IPv6 unreachable default in table 51003 (gw-iaas1) belongs to no VRF",
            "ICMPv6 source 2001:db8:4005:1::1:2 on br51003 (gw-iaas1) belongs to no VRF",
            "IPv6 unreachable default in table 51002 (gw-atuin) belongs to no VRF"])

    def test_owned_and_foreign_entries_are_not_findings(self):
        # a VRF's own default and source, the node's own default in the main table, an unreachable route that is no
        # default, the loopbacks outside the ICMPv6 pool
        a = dict(ANSWERS, **{"ip netns list": "gw-iaas1 (id: 0)\n",
                             "ip netns exec gw-iaas1 ip -j -6 route show table all type unreachable": json.dumps([unreachable(51000)]),
                             "ip netns exec gw-iaas1 ip -j -6 addr show": json.dumps([
                                 addrs("lo", "::1", "2001:db8:4005:ffff::2"), addrs("t51000", "2001:db8:4005:1::1:2")])})
        out, _ = execute(a)
        self.assertEqual(out["ipv6Orphans"], [])

    def test_covering_route_of_a_gone_vrf_is_a_finding(self):
        # a gateway's covering route (the apply script's metric, not the default) in a table no VRF uses
        a = dict(ANSWERS, **{"ip netns exec gw-atuin ip -j -6 route show table all type unreachable": json.dumps([
            unreachable(51002), unreachable(51002, dst="2001:db8:4005::/48"), unreachable(51004, dst="2001:db8:4005:4200::/56")])})
        out, _ = execute(a)
        self.assertIn("IPv6 unreachable 2001:db8:4005:4200::/56 in table 51004 (gw-atuin) belongs to no VRF", out["ipv6Orphans"])
        self.assertIn("IPv6 unreachable 2001:db8:4005::/48 in table 51002 (gw-atuin) belongs to no VRF", out["ipv6Orphans"])

    def test_without_the_icmp_pool_no_address_read(self):
        v = {"foc_parents": [], "foc_pool": [1, 0], "fab_real": {"core": {"icmpSource6": ""}}}
        out, calls = execute(ANSWERS, v, pool="")
        self.assertNotIn(["ip", "-j", "-6", "addr", "show"], calls)
        self.assertTrue(all("addr" not in c for c in calls), calls)
        self.assertEqual(out["ipv6Orphans"], ["IPv6 unreachable default in table 51003 (gw-iaas1) belongs to no VRF",
                                              "IPv6 unreachable default in table 51002 (gw-atuin) belongs to no VRF"])
        # the rendered call carries the empty pool
        self.assertIn('main(run, [], 1, 0,\n                          "")', shell(v))

    def test_unreadable_answers_are_empty(self):
        a = dict(ANSWERS, **{"vtysh -c show bfd peers json": "% Can't open configuration file",
                             "ip netns exec gw-atuin ip -j -6 route show table all type unreachable": ""})
        out, _ = execute(a)
        self.assertEqual(out["rootBfd"], [])
        self.assertNotIn("IPv6 unreachable default in table 51002 (gw-atuin) belongs to no VRF", out["ipv6Orphans"])

    def test_rendered_values_and_shell_syntax(self):
        sh = shell()
        self.assertIn('main(run, ["bond1"], 3000, 3099,\n                          "2001:db8:4005:1::/64")', sh)
        self.assertNotIn("'", code())
        proc = subprocess.run(["bash", "-n"], input=sh, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class KeepAndCollectTest(unittest.TestCase):
    """The node findings carry the node's name and join the report's findings (nodes.yml "Keep the nodes", main.yml
    "Collect the findings")."""

    def test_keep_and_collect(self):
        node = {"netns": {"gw-iaas1": ["t51000"], "root": []}, "rootDevices": [], "rootBfd": [],
                "ipv6Orphans": ["IPv6 unreachable default in table 51003 (gw-iaas1) belongs to no VRF"]}
        keep = by_name(load(NODES), T_KEEP)
        collect = by_name(load(MAIN), T_COLLECT)
        # the facts the tasks build on are set by a task (an extra var would win over set_fact)
        start = {"ansible.builtin.set_fact": {"foc_nodes": []}}
        out = run_play([start, keep, collect, write_out("{'nodes': foc_nodes, 'findings': foc_findings}")],
                       {"foc_fabric": {"metadata": {"name": "lab"}},
                        "_foc_node": {"results": [{"item": {"name": "bgw-1"}, "stdout": json.dumps(node)},
                                                  {"item": {"name": "bgw-2"}, "stdout": json.dumps(dict(node, ipv6Orphans=[]))}]},
                        "fab_check_result": {"findings": ["ledger lab: vni 51003 held by NetworkPlacement/entity-acme/gone, "
                                                          "which no longer exists"]}})
        self.assertEqual([n["ipv6Orphans"] for n in out["nodes"]],
                         [["node bgw-1: IPv6 unreachable default in table 51003 (gw-iaas1) belongs to no VRF"], []])
        self.assertEqual(out["nodes"][0]["netns"], node["netns"])
        self.assertEqual(out["findings"], ["ledger lab: vni 51003 held by NetworkPlacement/entity-acme/gone, which no longer exists",
                                           "node bgw-1: IPv6 unreachable default in table 51003 (gw-iaas1) belongs to no VRF"])
        # a node read from before this key (no ipv6Orphans) still keeps
        out = run_play([start, keep, write_out("foc_nodes")],
                       {"foc_fabric": {"metadata": {"name": "lab"}},
                        "_foc_node": {"results": [{"item": {"name": "bgw-1"}, "stdout": json.dumps({"netns": {}, "rootDevices": []})}]}})
        self.assertEqual(out[0]["ipv6Orphans"], [])


if __name__ == "__main__":
    unittest.main()

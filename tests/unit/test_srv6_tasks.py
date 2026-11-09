"""The SRv6 core transport in the HybridFabric and CloudGateway node tasks (hybridfabric_provision node_root.yml,
node_transport.yml, main.yml; cloudgateway_provision node_gateway.yml, main.yml).

On the MPLS core (realization core.transport mpls, the default) the tasks must do exactly what they did before the
transport existed: the rendered readiness scripts, the apply JSON (root.json, gw-<site>.json) and the FRR start and
reload decisions are compared with the same tasks at the base commit (BASE, read with git; those tests are skipped
when the commit is not in the repository).

On the SRv6 core the readiness checks are inline Python like the MPLS ones (`python3 -c '<code>' <args>`), run here
with the command output replaced by fixtures:
- tests/fixtures/srv6-x6-85-usid/: real kernel output from the offline SRv6 spike on FRR 8.5.3 with micro-SID
  locators (/48, block 32 / node 16 / function 16), el9 kernel: pe2-route6.txt = `ip -6 route show vrf t51001`
  of the plain PE (encapsulating routes, `encap seg6 mode encap`) followed by its SID route (`ip -6 route show table
  all | grep seg6local`, End.DT46 into table 51001), from the spike's A-pe2.txt; edge-route6.txt the same for the
  remote PE behind WireGuard (C-edge.txt, VRF t51005); pe1-sids.txt the gateway analog's SID route (B-pe1.txt, VRF
  t51000); pe2-sids-vrf-recreated.txt the SID route after VRF t51001 was deleted and created again under the same
  name (G.txt: a new function number, same locator; the spike printed it with repeated blanks squeezed). The
  spike's block is fd00:5e6::/32 (pe2's locator fd00:5e6:3::/48), outside 5f00::/16 by design of the spike.
- tests/fixtures/srv6-frr-renders/: stream B's FRR templates rendered for srv6 on FRR 8 by its own test
  (tests/unit/test_srv6_frr_templates.py on branch srv6-b at 7c04a3ca, SHC_RENDER_OUT): gw-srv6-frr8.conf,
  root-srv6-frr8.conf, and root-srv6-frr8-noexport.conf (its root_vars(v6=True, families=[6]): network VRFs that
  export nothing to the VPN, hence no per-VRF SID). The SID check reads the VRFs that must have a SID from them.
- tests/fixtures/srv6-replica-p2/: real outputs of the replica (FRR 8.5.3 on the SRv6 core with uSID, the
  integrated commit e0785d63; p-srv6/replica/out/p2-srv6-e0785d63/dfix/, copied as they are): per instance (root,
  gw-site-a/b/c) the VPN summaries, `show ipv6 route isis json`, `show isis neighbor json`, `ip -6 rule show`,
  `ip -6 route show table all`, `nft list table inet shc_core_guard`, the gateways' SID lists, EVPN summaries and
  `show vrf vni json`; and from that run's evidence/snap-post.txt gw-site-a's `show running-config` and its guard
  listed without counters (`nft -s`). summary(), zebra(), RULES and guard_table() build their variants from these.
- tests/fixtures/srv6-locator-lo/: real outputs of a provider edge whose locator is a prefix of lo (FRR 8.5.3-17 on
  the test VM, the anchor and the blackhole set by the apply's own helper): `show isis database detail`,
  `ip -6 addr show dev lo`, and `show ipv6 route connected json` (zebra lists the prefix with distance 255, metric
  8192, the blackhole as nexthop and no `selected`: why the check does not read zebra); own_locator() builds the
  first two for the instance's locator. And the replica's failed
  gateway (a zebra crash and watchfrr's restart, p10): its IS-IS database as recorded (own LSP without its locator).
- still hand-built (the replica recorded none): the root's running configuration (its PE listen range) and a gateway's
  `show bgp ipv4 vpn summary established json` (its IPv6 one stands in).
- before the replica, the FRR JSON and the rule and guard listings were written from FRR's and iproute2's source;
  the replica's outputs have the shapes assumed (see the record).
"""
import copy
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases, run_play, write_out  # noqa: E402
from test_gateway_readiness import render, shell_of, snippet  # noqa: E402

import yaml  # noqa: E402

BASE = "72f86002"
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
RT = os.path.join(ROLES, "hybridfabric_provision", "tasks", "node_root.yml")
GW = os.path.join(ROLES, "cloudgateway_provision", "tasks", "node_gateway.yml")
NT = os.path.join(ROLES, "hybridfabric_provision", "tasks", "node_transport.yml")
HF_MAIN = os.path.join(ROLES, "hybridfabric_provision", "tasks", "main.yml")
CG_MAIN = os.path.join(ROLES, "cloudgateway_provision", "tasks", "main.yml")
FIX = os.path.join(ROOT, "tests", "fixtures", "srv6-x6-85-usid")
P = "{{ bgw_node.name }}: "

T_RT_WAIT = P + "wait for BGP and IS-IS"
T_RT_LABEL = P + "every root VRF's VPN label leads into it"
T_RT_SID = P + "every root VRF's SID leads into it"
T_GW_WAIT = P + "wait for the core (IS-IS, VPNv4 and VPNv6) and the upstream route reflectors"
T_GW_LABEL = P + "every VRF's VPN label leads into it"
T_GW_SID = P + "every VRF's SID leads into it"


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def base_tasks(path):
    """The task file at the base commit, or None when git or the commit is not there."""
    rel = os.path.relpath(path, ROOT)
    try:
        out = subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (BASE, rel)], capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return yaml.safe_load(out.stdout) if out.returncode == 0 else None


def have_base():
    return base_tasks(RT) is not None


def base_shell(path, name):
    t = by_name(base_tasks(path), name)
    sh = t["ansible.builtin.shell"]
    return sh["cmd"] if isinstance(sh, dict) else sh


def copy_as_fact(task, fact):
    return {"name": task["name"], "vars": task.get("vars", {}),
            "ansible.builtin.set_fact": {fact: task["ansible.builtin.copy"]["content"]}}


# ---- the variables of the scripts ----------------------------------------------------------------------------------

BLOCK = "5f00:db8::/32"
POOL6 = "2001:db8:4005:ffff::/64"
CORE = {"loopbackPool": "10.255.0.0/24", "loopbackPool6": POOL6, "srv6": {"block": BLOCK}}
# node 1: the root (index 1) and a gateway (index 2); locator node field = (1 << 8) + index
RT_PE = {"loopback6": "2001:db8:4005:ffff::1/128", "loopbackIp6": "2001:db8:4005:ffff::1", "loopbackIp": "10.255.0.1",
         "sid": 16001, "locator": "5f00:db8:101::/48", "locatorNode": 257}
GW_PE = {"loopback6": "2001:db8:4005:ffff::2/128", "loopbackIp6": "2001:db8:4005:ffff::2", "loopbackIp": "10.255.0.2",
         "sid": 16002, "index": 2, "locator": "5f00:db8:102::/48", "locatorNode": 258}

ROOT_VARS = {"bgw_node": {"name": "bgw-1"}, "fab_spec": {"domainAsn": 65010}, "fab_name": "lab",
             "fab_real": {"core": CORE}, "hf_root": {"bridge": "br-core", "link": "10.255.1.1/24"},
             "hf_node_egress": {}, "hf_node_vrfs": [], "hf_pe": RT_PE}
GW_VARS = {"bgw_node": {"name": "bgw-1"}, "fab_name": "iaas1", "fab_spec": {"siteUnderlay": {}},
           "fab_real": {"core": CORE}, "cg_root_pe": RT_PE, "cg_pe": GW_PE,
           "cg_vrfs": [{"vrf": "t51000", "vni": 51000}], "cg_fabric": {"spec": {"domainAsn": 65010}}}


def srv6(v):
    return dict(copy.deepcopy(v), fab_node_transport="srv6")


# ---- mpls: as at the base commit ------------------------------------------------------------------------------------

def without_local4_check(script):
    """The mpls wait scripts without the IPv4 local-rule check both transports have now (tests/unit/
    test_fix_mpls_local4_readiness.py covers it): its comment and command lines, which follow the MPLS check."""
    out, skip = [], False
    for line in script.splitlines(True):
        if line.strip().startswith("# the IPv4 local rule after the l3mdev rule of the VRFs"):
            skip = True
        if not skip:
            out.append(line)
        elif line.strip().startswith('[ "$rc" = 0 ] || {'):
            skip = False
    return "".join(out)


@unittest.skipUnless(have_base(), "base commit %s not in the repository" % BASE)
class MplsScriptsUnchanged(unittest.TestCase):
    """Every readiness script renders on mpls as it did at the base commit, with and without fab_node_transport set,
    the IPv6 core on and off, and with the realization's SRv6 block present (the switch decides, not the block)."""

    def same(self, path, name, variables):
        old = render(base_shell(path, name), **variables)
        for v in (variables, dict(variables, fab_node_transport="mpls")):
            self.assertEqual(without_local4_check(render(shell_of(path, name), **v)), old, name)

    def test_root(self):
        for lo6 in ("", RT_PE["loopback6"]):
            v = dict(copy.deepcopy(ROOT_VARS), hf_pe=dict(RT_PE, loopback6=lo6))
            self.same(RT, T_RT_WAIT, v)
            for vrfs in ([], [{"vrf": "e100", "families": [4, 6]}, {"vrf": "e101"}]):
                self.same(RT, T_RT_LABEL, dict(v, hf_node_vrfs=vrfs))

    def test_gateway(self):
        for lo6 in ("", GW_PE["loopback6"]):
            v = dict(copy.deepcopy(GW_VARS), cg_pe=dict(GW_PE, loopback6=lo6))
            self.same(GW, T_GW_WAIT, v)
            self.same(GW, T_GW_LABEL, dict(v, cg_vrfs=[{"vrf": "t51000", "vni": 51000},
                                                       {"vrf": "t51001", "vni": 51001, "families": [4, 6]}]))
            self.same(GW, P + "read the VRFs' routes", v)

    def test_the_mpls_checks_run_only_on_mpls_and_the_sid_checks_only_on_srv6(self):
        for path, label, sid in ((RT, T_RT_LABEL, T_RT_SID), (GW, T_GW_LABEL, T_GW_SID)):
            tasks = load(path)
            self.assertEqual(by_name(tasks, label)["when"], "fab_node_transport | default('mpls') == 'mpls'")
            self.assertEqual(by_name(tasks, sid)["when"], "fab_node_transport == 'srv6'")
            old = by_name(base_tasks(path), label)
            new = dict(by_name(tasks, label))
            new.pop("when")
            self.assertEqual(new, old)


@unittest.skipUnless(HAVE_ANSIBLE and have_base(), "ansible-playbook or the base commit missing")
class MplsApplyConfigUnchanged(unittest.TestCase):
    """root.json and gw-<site>.json on mpls are byte for byte what the base commit's tasks write."""

    def contents(self, path, name, variables):
        out = []
        for tasks in (base_tasks(path), load(path)):
            t = copy_as_fact(by_name(tasks, name), "_c")
            out.append(run_play([t, write_out("_c")], variables))
        return out

    def test_root_json(self):
        pe = dict(RT_PE)
        pe.pop("locator")
        common = {"bgw_node": {"name": "bgw-1"}, "hf_root": {"type": "root", "bridge": "br-core"},
                  "hf_node_attach": {}, "hf_pe": pe,
                  "fab_check_result": {"icmp6": "2001:db8:4005:1::1:1/128"}}
        for core in (CORE, dict(CORE, icmpSource6="2001:db8:4005:1::/64"), {"loopbackPool": "10.255.0.0/24"}):
            for egress in ({}, {"vrf": "egress", "ipv6": {"address": "2001:db8::13/64"}}):
                for extra in ({}, {"fab_node_transport": "mpls"}):
                    v = dict(common, fab_real={"core": dict(core, transport="mpls")}, hf_node_egress=egress, **extra)
                    old, new = self.contents(RT, P + "root instance config", v)
                    self.assertEqual(new, old)
                    self.assertNotIn("transport", json.loads(new))

    def test_gateway_json(self):
        common = {"bgw_node": {"name": "bgw-1"}, "fab_name": "site1", "cg_instance": {"type": "gateway"},
                  "cg_node_addr": "192.0.2.4", "fab_spec": {"siteUnderlay": {"cidr": "192.0.2.0/24"}},
                  "cg_pe": GW_PE, "fab_check_result": {"icmp6": "2001:db8:4005:1::1:2/128"}}
        for core in (CORE, dict(CORE, icmpSource6="2001:db8:4005:1::/64")):
            for extra in ({}, {"fab_node_transport": "mpls"}, {"cg_node_icmp6_announce": "x", "cg_ipv6_core": True}):
                v = dict(common, fab_real={"core": core}, **extra)
                old, new = self.contents(GW, P + "gateway instance config", v)
                if "cg_node_icmp6_announce" in extra:
                    # the one intended difference: the file no longer carries icmp6Announced (the apply loads the
                    # VRFs' input filter with the IPv6 core whatever the announcement)
                    self.assertEqual(json.loads(new), {k: x for k, x in json.loads(old).items() if k != "icmp6Announced"})
                    self.assertNotIn("icmp6Announced", new)
                else:
                    self.assertEqual(new, old)
                self.assertNotIn("srv6", json.loads(new))


@unittest.skipUnless(HAVE_ANSIBLE and have_base(), "ansible-playbook or the base commit missing")
class MplsFrrDecisionsUnchanged(unittest.TestCase):
    """The FRR start (state) and reload (when) decisions are the base commit's for every combination."""

    def test_root(self):
        cases = []
        for active in ("active", "inactive"):
            for out in ("", "bridge-mac=changed", "changed=yes"):
                for conf in (True, False):
                    for due in ("reload=no", "reload=due"):
                        cases.append({"active": active, "out": out, "conf": conf, "due": due})
        res = []
        for tasks in (base_tasks(RT), load(RT)):
            s = by_name(tasks, P + "start FRR (enabled at boot)")["ansible.builtin.systemd"]["state"]
            w = by_name(tasks, P + "reload the FRR configuration")["when"]
            expr = "{{ {'state': (%s), 'reload': ((%s))} }}" % (
                s.strip()[2:-2].replace("omit", "'OMIT'"), ") and (".join(w))
            res.append(run_cases(
                [{"ansible.builtin.set_fact": {
                    "_hf_frr_active": {"stdout": "{{ case.active }}"}, "_hf_apply": {"stdout": "{{ case.out }}"},
                    "_hf_frrconf": {"changed": "{{ case.conf }}"}, "_hf_daemons": {"changed": False},
                    "_hf_frr_due": {"stdout": "{{ case.due }}"}}},
                 {"ansible.builtin.set_fact": {"_d": expr}}],
                cases, "_d", {"fab_node_transport": "mpls"}))
        self.assertEqual(res[1], res[0])
        self.assertIn("restarted", [r["state"] for r in res[0]])

    def test_gateway(self):
        cases = []
        for active in ("active", "inactive"):
            for daemons in (True, False):
                for unit in (True, False):
                    for conf in (True, False):
                        cases.append({"active": active, "daemons": daemons, "unit": unit, "conf": conf})
        res = []
        for tasks in (base_tasks(GW), load(GW)):
            s = by_name(tasks, P + "start the gateway instance (enabled at boot)")["ansible.builtin.systemd"]["state"]
            w = by_name(tasks, P + "reload the FRR configuration")["when"]
            expr = "{{ {'state': (%s), 'reload': ((%s))} }}" % (
                s.strip()[2:-2].replace("omit", "'OMIT'"), ") and (".join(w))
            res.append(run_cases(
                [{"ansible.builtin.set_fact": {
                    "_cg_active": {"stdout": "{{ case.active }}"}, "_cg_daemons": {"changed": "{{ case.daemons }}"},
                    "_cg_unit_changed": "{{ case.unit }}", "_cg_frrconf": {"changed": "{{ case.conf }}"},
                    "_cg_frr_due": {"stdout": "reload=no"}}},
                 {"ansible.builtin.set_fact": {"_d": expr}}],
                cases, "_d", {"fab_node_transport": "mpls"}))
        self.assertEqual(res[1], res[0])


# ---- the transport and FRR of a node --------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NodeTransport(unittest.TestCase):
    """node_transport.yml with the package query answered by a fixture."""

    def run_nt(self, rpm, core, expect_fail=False):
        tasks = copy.deepcopy(load(NT))
        q = tasks[0]
        q.pop("delegate_to")
        rpm_q = "rpm -q --qf '%{NAME} %{VERSION}\\n' frr frr10"
        self.assertIn(rpm_q, q["ansible.builtin.shell"])
        # the package query answered by the fixture, the rest of the step as it is
        q["ansible.builtin.shell"] = q["ansible.builtin.shell"].replace(rpm_q, "printf '%s'" % rpm.replace("\n", "\\n"))
        result = "{'t': fab_node_transport, 'major': fab_frr_major | int, 'version': fab_frr_version}"
        v = {"bgw_node": {"name": "bgw-1"}, "fab_real": {"core": core}, "nt_who": "HybridFabric lab"}
        try:
            return run_play(tasks + [write_out(result)], v)
        except AssertionError as e:
            if expect_fail:
                return str(e)
            raise

    def test_mpls_default(self):
        # a realization without the key, with an empty one, with mpls: mpls; FRR 8.5 found
        for core in ({}, {"transport": ""}, {"transport": "mpls"}, {"transport": None}):
            self.assertEqual(self.run_nt("frr 8.5.3\n", core), {"t": "mpls", "major": 8, "version": "8.5.3"})

    def test_mpls_takes_any_frr(self):
        # the MPLS core asks nothing of the FRR version
        self.assertEqual(self.run_nt("frr 8.4.4\n", {})["t"], "mpls")

    def test_frr10_package(self):
        self.assertEqual(self.run_nt("frr10 10.4.3\n", CORE | {"transport": "srv6"}),
                         {"t": "srv6", "major": 10, "version": "10.4.3"})
        # both installed: the newer counts
        self.assertEqual(self.run_nt("frr 8.5.3\nfrr10 10.4.3\n", {})["major"], 10)

    def test_no_frr(self):
        msg = self.run_nt("", {}, expect_fail=True)
        self.assertIn("no FRR package (frr or frr10) is installed", msg)

    def test_srv6_needs(self):
        msg = self.run_nt("frr 8.4.4\n", {"transport": "srv6"}, expect_fail=True)
        self.assertIn("HybridFabric lab on bgw-1: the core transport srv6 needs the SID block (realization "
                      "core.srv6.block), the dual-stack core (realization core.loopbackPool6), FRR 8.5 or later "
                      "(installed: 8.4.4)", msg)
        msg = self.run_nt("frr 8.5.3\n", {"transport": "srv6", "loopbackPool6": POOL6, "srv6": {"block": ""}},
                          expect_fail=True)
        self.assertIn("needs the SID block (realization core.srv6.block)\n", msg.replace('"', "\n"))
        self.assertEqual(self.run_nt("frr 8.5.3\n", dict(CORE, transport="srv6"))["t"], "srv6")

    def test_unknown_transport(self):
        msg = self.run_nt("frr 8.5.3\n", {"transport": "vxlan"}, expect_fail=True)
        self.assertIn("the core transport vxlan is not one of mpls, srv6", msg)


# ---- srv6: the apply configuration ----------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Srv6ApplyConfig(unittest.TestCase):
    def test_root_json(self):
        t = copy_as_fact(by_name(load(RT), P + "root instance config"), "_c")
        c = json.loads(run_play([t, write_out("_c")], {
            "bgw_node": {"name": "bgw-1"}, "hf_root": {"type": "root"}, "hf_node_egress": {}, "hf_node_attach": {},
            "hf_pe": RT_PE, "fab_real": {"core": dict(CORE, transport="srv6")}, "fab_check_result": {},
            "fab_node_transport": "srv6"}))
        self.assertEqual((c["transport"], c["srv6"]), ("srv6", {"block": BLOCK, "locator": "5f00:db8:101::/48"}))
        self.assertEqual(c["loopback6"], RT_PE["loopback6"])

    def test_gateway_json(self):
        t = copy_as_fact(by_name(load(GW), P + "gateway instance config"), "_c")
        c = json.loads(run_play([t, write_out("_c")], {
            "bgw_node": {"name": "bgw-1"}, "fab_name": "site1", "cg_instance": {"type": "gateway"},
            "cg_node_addr": "192.0.2.4", "fab_spec": {"siteUnderlay": {"cidr": "192.0.2.0/24"}}, "cg_pe": GW_PE,
            "fab_real": {"core": dict(CORE, transport="srv6")}, "fab_check_result": {}, "cg_ipv6_core": True,
            "fab_node_transport": "srv6"}))
        self.assertEqual((c["transport"], c["srv6"]), ("srv6", {"block": BLOCK, "locator": "5f00:db8:102::/48"}))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Srv6Locators(unittest.TestCase):
    """The per-node locator lookups run only on srv6 and land in hf_pe / cg_pe / cg_root_pe; a lookup error stops."""

    def test_conditions(self):
        for path, names in ((RT, ["the root's SRv6 locator on this node", "the root's SRv6 locator"]),
                            (GW, ["the root's SRv6 locator on this node", "keep the root's SRv6 locator",
                                  "the gateway's SRv6 locator on this node", "the gateway's SRv6 locator"])):
            for n in names:
                self.assertEqual(by_name(load(path), P + n)["when"], "fab_node_transport == 'srv6'", n)

    def test_node_ordinal_on_every_lookup(self):
        for path, names, spec in ((RT, ["the root's SRv6 locator on this node"], "fab_spec"),
                                  (GW, ["the root's SRv6 locator on this node",
                                        "the gateway's SRv6 locator on this node"], "cg_fabric.spec")):
            for n in names:
                fc = by_name(load(path), P + n)["vars"]["fab_check"]
                self.assertEqual(fc["action"], "pe-addresses")
                self.assertIn(spec + ".borderGateway.nodes", fc["nodeOrdinal"])

    def test_root_locator_fact_and_stop(self):
        tasks = load(RT)
        keep = by_name(tasks, P + "the root's SRv6 locator")
        stop = by_name(tasks, P + "stop on an SRv6 locator that does not fit")
        v = {"bgw_node": {"name": "bgw-2"}, "fab_name": "lab", "fab_node_transport": "srv6", "_pe": {"index": 1}}
        ok = {"errors": [], "locator": "5f00:db8:201::/48", "locatorNode": 513}
        out = run_play([{"ansible.builtin.set_fact": {"hf_pe": "{{ _pe }}"}}, stop, keep, write_out("hf_pe")],
                       dict(v, fab_check_result=ok))
        self.assertEqual(out, {"index": 1, "locator": "5f00:db8:201::/48", "locatorNode": 513})
        for bad, msg in (({"errors": ["node position 300 or provider-edge index 1 does not fit the SRv6 locator "
                                      "(1-255 each)"]}, "does not fit the SRv6 locator"),
                         ({"errors": [], "locator": ""}, "the realization gives no SRv6 locator for this node")):
            with self.assertRaises(AssertionError) as e:
                run_play([stop, write_out("1")], dict(v, fab_check_result=bad))
            self.assertIn(msg, str(e.exception))
        # mpls: nothing runs
        run_play([stop, keep, write_out("1")], dict(v, fab_node_transport="mpls", fab_check_result={"errors": ["x"]},
                                                    hf_pe={}))

    def test_no_early_failure_for_the_ipv6_core(self):
        # the gateway waits in main.yml instead (GatewayWaitsForTheIpv6Core)
        self.assertNotIn("needs the fabric's IPv6 core", open(GW).read())


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayWaitsForTheIpv6Core(unittest.TestCase):
    """On the SRv6 core a gateway waits (pending, as for a fabric that is not ready) until the fabric reports its
    IPv6 core; on mpls nothing changes."""

    def waiting(self, transport, status, before=()):
        main = load(CG_MAIN)
        tasks = [by_name(main, "Wait for the fabric to be ready"), by_name(main, "Wait for the fabric's IPv6 core (SRv6 core)")]
        core = {} if transport is None else {"transport": transport}
        facts = {"ansible.builtin.set_fact": {"cg_waiting": list(before)}}
        return run_play([facts] + tasks + [write_out("cg_waiting")],
                        {"cg_fabric_found": True, "cg_fabric_name": "lab", "fab_real": {"core": core},
                         "cg_fabric": {"spec": {}, "status": status}})

    def test_srv6(self):
        ready = {"ready": True, "status": "ready", "realization": {"ipv6Core": False}}
        self.assertEqual(self.waiting("srv6", ready),
                         ["HybridFabric lab (IPv6 core not reported yet; the SRv6 core needs it)"])
        self.assertEqual(self.waiting("srv6", dict(ready, realization={"ipv6Core": True})), [])
        self.assertEqual(self.waiting("srv6", {"ready": True, "status": "ready"}),
                         ["HybridFabric lab (IPv6 core not reported yet; the SRv6 core needs it)"])
        # a fabric not ready yet: one entry for it, not two
        self.assertEqual(self.waiting("srv6", {"status": "pending"}), ["HybridFabric lab (pending)"])
        # what else it waits for stays
        self.assertEqual(self.waiting("srv6", ready, before=["CloudInfrastructure site1: evpn capability"]),
                         ["CloudInfrastructure site1: evpn capability",
                          "HybridFabric lab (IPv6 core not reported yet; the SRv6 core needs it)"])

    def test_mpls_unchanged(self):
        for transport in (None, "mpls"):
            self.assertEqual(self.waiting(transport, {"ready": True, "status": "ready"}), [])


# ---- srv6: the readiness checks -------------------------------------------------------------------------------------

REPLICA = os.path.join(ROOT, "tests", "fixtures", "srv6-replica-p2")


def replica(name):
    with open(os.path.join(REPLICA, name)) as f:
        return f.read()


def summary(peers):
    """`show bgp ipv4|ipv6 vpn summary [established] json` in the shape FRR 8.5.3 prints on the SRv6 core (the
    replica's root, two IPv6 listen-range peers): its header and one of its peer entries per peer, with the state."""
    real = json.loads(replica("root__show_bgp_ipv4_vpn_summary_json.json"))
    entry = next(iter(real["peers"].values()))
    real["peers"] = {p: dict(entry, state=s) for p, s in peers.items()}
    real["displayedPeers"] = real["totalPeers"] = len(peers)
    return json.dumps(real, indent=2)


def zebra(protocol, *prefixes, installed=True):
    """`show ipv6 route <protocol> json` in the shape FRR 8.5.3 prints (an entry of the replica's
    `show ipv6 route isis json`, with prefix, protocol and installed set)."""
    real = json.loads(replica("gw-site-a__show_ipv6_route_isis_json.json"))
    entry = next(iter(real.values()))[0]
    out = {}
    for p in prefixes:
        r = dict(entry, prefix=p, prefixLen=int(p.split("/")[1]), protocol=protocol)
        if protocol != "isis":
            r["distance"] = 1
        if not installed:
            r.pop("installed")
        out[p] = [r]
    return json.dumps(out, indent=2)


LOCLO = os.path.join(ROOT, "tests", "fixtures", "srv6-locator-lo")
VM_LOC, VM_ANCHOR = "fd00:5e6:2::/48", "fd00:5e6:2::1/48"


def locfix(name):
    with open(os.path.join(LOCLO, name)) as f:
        return f.read()


def own_locator(locators, lsp=True):
    """The answers that show an instance's locator announced as a prefix of lo, from the test VM's real outputs: the
    anchor on lo (`ip -6 addr show dev lo`) and the own LSP (`show isis database detail`) carrying the locator
    (without lsp, or without locators: the own LSP without it)."""
    addr = locfix("pe1__ip6-addr-lo.txt")
    db = locfix("pe1__show_isis_database_detail.txt")
    line = "  IPv6 Reachability: %s (Metric: 10)\n" % VM_LOC
    assert line in db and VM_ANCHOR in addr
    if not locators:
        addr = addr.replace("    inet6 %s scope global nodad noprefixroute \n       valid_lft forever preferred_lft forever\n"
                            % VM_ANCHOR, "")
    for loc in locators:
        n = ipaddress.ip_network(loc)
        addr = addr.replace(VM_ANCHOR, "%s/%d" % (n.network_address + 1, n.prefixlen), 1)
    db = db.replace(line, "".join("  IPv6 Reachability: %s (Metric: 10)\n" % loc for loc in locators) if lsp else "")
    return {"ip -6 addr show dev lo": addr, "show isis database detail": db}


# `ip -6 rule show` as the replica's root printed it, the block replaced
RULES = replica("root__ip6-rule.txt").replace("5f00:b046::/32", "%s")
# `ip -4 rule show`: the kernel's default (a replica namespace, its F.txt), and the same rules with lookup local moved
# to 32765 as the apply leaves them on the SRv6 core (built from the default, not recorded)
RULES4_DEFAULT = open(os.path.join(ROOT, "tests", "fixtures", "srv6-replica-p4", "ip4-rule-default.txt")).read()
RULES4 = open(os.path.join(ROOT, "tests", "fixtures", "srv6-replica-p4", "ip4-rule-srv6.txt")).read()


def core_answers(est4, est6, plain=None, isis=(), own=(), rule=BLOCK, strict="1", seg6=None, guard=True,
                 running=""):
    """run() answers for the SRv6 core check (the first key a command contains wins)."""
    seg6 = seg6 or {}
    a = {"net.vrf.strict_mode": strict + "\n"}
    for dev in ("all", "default", "core0", "br-core"):
        a["net/ipv6/conf/%s/seg6_enabled" % dev] = seg6.get(dev, "0") + "\n"
    a.update({"ip -6 rule show": RULES % rule, "ip -4 rule show": RULES4,
              "ipv4 vpn summary established json": summary({p: "Established" for p in est4}),
              "ipv6 vpn summary established json": summary({p: "Established" for p in est6}),
              "show bgp ipv4 vpn summary json": summary(plain if plain is not None else {p: "Established" for p in est4}),
              "show bgp ipv6 vpn summary json": summary(plain if plain is not None else {p: "Established" for p in est6}),
              "show ipv6 route isis json": zebra("isis", *isis),
              "show running-config": running})
    a.update(own_locator(own))
    if guard is True:
        a["nft list table inet shc_core_guard"] = guard_table("core0")
    elif guard:
        a["nft list table inet shc_core_guard"] = guard
    return a


def guard_table(leg, block=BLOCK):
    """`nft list table inet shc_core_guard` as the replica's root printed it, with the core leg and the block
    replaced."""
    return replica("root__shc_core_guard.txt").replace('"br-core"', '"%s"' % leg).replace("5f00:b046::/32", block)


def sid_conf(*vrfs):
    """A rendered FRR configuration in the form of the templates: one BGP instance with the per-VRF SID per VRF."""
    return "".join("router bgp 65010 vrf %s\n bgp router-id 10.255.0.2\n sid vpn per-vrf export auto\n"
                   " address-family ipv4 unicast\n  rd vpn export 65010:1\n exit-address-family\nexit\n!\n" % v
                   for v in vrfs)


class Fake:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        line = " ".join(cmd)
        for key, text in self.answers.items():
            if key in line:
                return text
        raise subprocess.CalledProcessError(1, cmd)


def run_code(code, args, answers):
    import contextlib
    import io
    ns = {"__name__": "srv6_check_under_test"}
    exec(compile(code, "<inline check>", "exec"), ns)
    fake, out, err = Fake(answers), io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ns["main"](args, fake)
    return rc, out.getvalue(), err.getvalue(), fake.calls


class Srv6WaitScripts(unittest.TestCase):
    def test_gateway_script(self):
        sh = render(shell_of(GW, T_GW_WAIT), **srv6(GW_VARS))
        # no MPLS input, no session over the IPv4 loopback; IS-IS adjacency, the EVPN side and the VNIs as before
        self.assertNotIn("net.mpls", sh)
        self.assertNotIn("show bgp ipv4 vpn summary json' | python3", sh)
        self.assertIn("show isis neighbor' | grep -q ' Up '", sh)
        self.assertIn("show bgp l2vpn evpn summary json", sh)
        self.assertIn("show vrf vni json", sh)
        self.assertIn('sysctl -n net.ipv6.conf.all.forwarding)" = 1 ]', sh)
        _, args = snippet(sh)
        self.assertEqual(args, ["gw-iaas1", "gw-iaas1", "core0", BLOCK, GW_PE["loopbackIp6"], GW_PE["locator"],
                                POOL6, RT_PE["loopbackIp6"]])

    def test_root_script(self):
        sh = render(shell_of(RT, T_RT_WAIT), **srv6(ROOT_VARS))
        self.assertNotIn("platform_labels", sh)
        self.assertNotIn("bgp listen range 10.255.0.0/24", sh)
        self.assertIn("^router isis CORE$", sh)
        _, args = snippet(sh)
        self.assertEqual(args, ["", "", "br-core", BLOCK, RT_PE["loopbackIp6"], RT_PE["locator"], POOL6])

    def test_same_code_on_root_and_gateway(self):
        g, _ = snippet(render(shell_of(GW, T_GW_WAIT), **srv6(GW_VARS)))
        r, _ = snippet(render(shell_of(RT, T_RT_WAIT), **srv6(ROOT_VARS)))
        self.assertEqual(g, r)
        self.assertNotIn("'", g)
        g, _ = snippet(render(shell_of(GW, T_GW_SID), **srv6(GW_VARS)))
        r, _ = snippet(render(shell_of(RT, T_RT_SID), **srv6(dict(ROOT_VARS, hf_node_vrfs=[{"vrf": "e1", "table": 1}]))))
        self.assertEqual(g, r)
        self.assertNotIn("'", g)

    def test_bash_syntax(self):
        for path, name, v in ((GW, T_GW_WAIT, srv6(GW_VARS)), (RT, T_RT_WAIT, srv6(ROOT_VARS)),
                              (GW, T_GW_SID, srv6(GW_VARS)),
                              (RT, T_RT_SID, srv6(dict(ROOT_VARS, hf_node_vrfs=[{"vrf": "e100", "table": 100100}]))),
                              (RT, T_RT_SID, srv6(ROOT_VARS))):
            proc = subprocess.run(["bash", "-n"], input=render(shell_of(path, name), **v), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Srv6AnsibleRender(unittest.TestCase):
    """render() gives what Ansible gives for the SRv6 renders the tests above use."""

    def test_tasks(self):
        for path, name, v in ((GW, T_GW_WAIT, srv6(GW_VARS)), (RT, T_RT_WAIT, srv6(ROOT_VARS)),
                              (GW, T_GW_SID, srv6(GW_VARS)),
                              (RT, T_RT_SID, srv6(dict(ROOT_VARS, hf_node_vrfs=[{"vrf": "e100", "table": 100100}])))):
            sh = shell_of(path, name)
            got = run_play([{"ansible.builtin.set_fact": {"rendered": sh}}, write_out("rendered")], v)
            self.assertEqual(render(sh, **v).rstrip("\n"), got.rstrip("\n"), name)


class Srv6CoreCheck(unittest.TestCase):
    RR6, GW6 = RT_PE["loopbackIp6"], GW_PE["loopbackIp6"]

    def gw(self, answers):
        code, args = snippet(render(shell_of(GW, T_GW_WAIT), **srv6(GW_VARS)))
        return run_code(code, args, answers)

    def root(self, answers):
        code, args = snippet(render(shell_of(RT, T_RT_WAIT), **srv6(ROOT_VARS)))
        if answers.get("nft list table inet shc_core_guard") == guard_table("core0"):
            answers["nft list table inet shc_core_guard"] = guard_table("br-core")
        return run_code(code, args, answers)

    def gw_ok(self, **kw):
        a = dict(est4=[self.RR6], est6=[self.RR6], isis=[self.RR6 + "/128", "5f00:db8:101::/48"],
                 own=["5f00:db8:102::/48"])
        a.update(kw)
        return core_answers(**a)

    def test_gateway_ready(self):
        rc, out, err, calls = self.gw(self.gw_ok())
        self.assertEqual((rc, out.strip(), err), (0, "ipv6Vpn=Established", ""))
        # everything read inside the namespace
        self.assertTrue(all(c[:4] == ["ip", "netns", "exec", "gw-iaas1"] or c[:3] == ["vtysh", "-N", "gw-iaas1"]
                            for c in calls), calls)

    def test_gateway_vpn_families(self):
        rc, out, err, _ = self.gw(self.gw_ok(est4=[]))
        self.assertEqual(rc, 1)
        self.assertIn("VPNv4 %s: NoNeg" % self.RR6, err)
        self.assertEqual(out.strip(), "ipv6Vpn=Established")
        rc, out, err, _ = self.gw(self.gw_ok(est4=[], est6=[], plain={self.RR6: "Active"}))
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=Active"))
        rc, out, err, _ = self.gw(self.gw_ok(est4=[], est6=[], plain={}))
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=NotActivated"))

    def test_gateway_locator_on_lo(self):
        """the locator announced as a prefix of lo: the anchor and the own LSP each required; zebra is not read (FRR 8.5.3
        lists the connected prefix unselected, at distance 255, while IS-IS announces it: the VM's real output)"""
        loc = "5f00:db8:102::/48"
        conn = json.loads(locfix("pe1__show_ipv6_route_connected_json.json"))[VM_LOC][0]
        self.assertEqual((conn["protocol"], conn["distance"], "selected" in conn), ("connected", 255, False))
        for kw, msg in (({"own": []}, "lo does not hold the locator anchor 5f00:db8:102::1/48"),
                        ({}, "the own IS-IS LSP does not announce the locator %s" % loc)):
            with self.subTest(msg=msg):
                a = self.gw_ok(**kw)
                if "LSP" in msg:
                    a.update({k: v for k, v in own_locator([loc], lsp=False).items() if "isis" in k})
                rc, _, err, _ = self.gw(a)
                self.assertEqual(rc, 1)
                self.assertIn(msg, err)
        rc, _, err, calls = self.gw(self.gw_ok())
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(["ip", "netns", "exec", "gw-iaas1", "ip", "-6", "addr", "show", "dev", "lo"], calls)
        self.assertIn(["vtysh", "-N", "gw-iaas1", "-c", "show isis database detail"], calls)
        self.assertFalse([c for c in calls if "connected" in " ".join(c)], calls)

    def test_gateway_isis(self):
        # the root's locator not learned, then its loopback not installed
        rc, _, err, _ = self.gw(self.gw_ok(isis=[self.RR6 + "/128"]))
        self.assertEqual(rc, 1)
        self.assertIn("5f00:db8:101::/48 of %s is not an installed IS-IS route" % self.RR6, err)
        a = self.gw_ok()
        a["show ipv6 route isis json"] = zebra("isis", self.RR6 + "/128", installed=False)[:-1] + ", " + \
            zebra("isis", "5f00:db8:101::/48")[1:]
        rc, _, err, _ = self.gw(a)
        self.assertEqual(rc, 1)
        self.assertIn("%s/128 of %s is not an installed IS-IS route" % (self.RR6, self.RR6), err)

    def test_gateway_kernel_settings(self):
        for kw, msg in (({"strict": "0"}, "net.vrf.strict_mode is not 1"),
                        ({"seg6": {"core0": "1"}}, "net.ipv6.conf.core0.seg6_enabled is not 0 (routing headers accepted)"),
                        ({"seg6": {"default": "1"}}, "net.ipv6.conf.default.seg6_enabled is not 0"),
                        ({"seg6": {"all": "1"}}, "net.ipv6.conf.all.seg6_enabled is not 0"),
                        ({"guard": "table inet shc_core_guard {\n}\n"},
                         "the guard table inet shc_core_guard has no drop rule for 5f00:db8::/32 from outside core0 and lo"),
                        ({"guard": guard_table("core0", "5f00:db9::/32")}, "has no drop rule for 5f00:db8::/32"),
                        ({"guard": guard_table("core1")}, "has no drop rule for 5f00:db8::/32 from outside core0"),
                        ({"guard": guard_table("core0").replace(" drop ", " accept ")}, "has no drop rule"),
                        ({"guard": guard_table("core0").replace("iifname != ", "iifname ")}, "has no drop rule"),
                        ({"rule": "5f00:db9::/32"}, "no rule to 5f00:db8::/32 lookup main at preference 999"),
                        ({"guard": False}, "the guard table inet shc_core_guard is not loaded"),
                        ({"own": []}, "lo does not hold the locator anchor 5f00:db8:102::1/48")):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                rc, _, err, _ = self.gw(self.gw_ok(**kw))
                self.assertEqual(rc, 1)
                self.assertIn(msg, err)
        # the IPv4 lookup local rule: the kernel's default (0, ahead of the VRFs), both, none, or the l3mdev rule gone
        for rules4, msg in ((RULES4_DEFAULT, "the IPv4 rule lookup local is at 0, not after the l3mdev rule at 1000"),
                            (RULES4_DEFAULT.replace("1000:", "32765:\tfrom all lookup local\n1000:"),
                             "the IPv4 rule lookup local is at 0, 32765, not after the l3mdev rule at 1000"),
                            (RULES4.replace("32765:\tfrom all lookup local\n", ""),
                             "the IPv4 rule lookup local is at no preference, not after the l3mdev rule at 1000"),
                            (RULES4.replace("1000:\tfrom all lookup [l3mdev-table]\n", ""), "no IPv4 l3mdev rule")):
            with self.subTest(rules4=rules4):
                a = self.gw_ok()
                a["ip -4 rule show"] = rules4
                rc, _, err, calls = self.gw(a)
                self.assertEqual(rc, 1)
                self.assertIn(msg, err)
                self.assertIn(["ip", "netns", "exec", "gw-iaas1", "ip", "-4", "rule", "show"], calls)
        # the block as the kernel prints it equals a differently written block
        a = self.gw_ok()
        a["ip -6 rule show"] = RULES % "5f00:0db8:0::/32"
        self.assertEqual(self.gw(a)[0], 0)
        # the rule at another preference does not count
        a["ip -6 rule show"] = RULES.replace("999:", "1999:") % BLOCK
        self.assertEqual(self.gw(a)[0], 1)

    def root_running(self, pool=POOL6):
        return "router bgp 65010\n bgp listen range %s peer-group PE\n" % pool

    def test_root_without_gateways(self):
        rc, out, err, calls = self.root(core_answers([], [], own=["5f00:db8:101::/48"], running=self.root_running()))
        self.assertEqual((rc, out.split(), err), (0, ["ipv6Vpn=NoPeers", "pendingPeers="], ""))
        # the guard of the root names its core bridge
        rc, _, err, _ = self.root(core_answers([], [], own=["5f00:db8:101::/48"], running=self.root_running(),
                                               guard=guard_table("core1")))
        self.assertIn("has no drop rule for 5f00:db8::/32 from outside br-core and lo", err)
        self.assertTrue(all(c[0] != "ip" or c[:2] in (["ip", "-6"], ["ip", "-4"]) for c in calls))
        self.assertIn(["sysctl", "-n", "net/ipv6/conf/br-core/seg6_enabled"], calls)

    def test_root_own_locator_required(self):
        a = core_answers([], [], own=["5f00:db8:101::/48"], running=self.root_running())
        a.update({k: v for k, v in own_locator(["5f00:db8:101::/48"], lsp=False).items() if "isis" in k})
        rc, _, err, calls = self.root(a)
        self.assertEqual(rc, 1)
        self.assertIn("the own IS-IS LSP does not announce the locator 5f00:db8:101::/48", err)
        self.assertIn(["ip", "-6", "addr", "show", "dev", "lo"], calls)

    def test_root_listen_range(self):
        rc, _, err, _ = self.root(core_answers([], [], own=["5f00:db8:101::/48"],
                                               running=self.root_running("10.255.0.0/24")))
        self.assertEqual(rc, 1)
        self.assertIn("the PE group does not listen on %s" % POOL6, err)

    def test_root_with_gateways(self):
        gw2, gw3 = self.GW6, "2001:db8:4005:ffff::3"
        isis = [gw2 + "/128", "5f00:db8:102::/48", gw3 + "/128", "5f00:db8:103::/48"]
        base = dict(own=["5f00:db8:101::/48"], running=self.root_running())
        rc, out, err, _ = self.root(core_answers([gw2, gw3], [gw2, gw3], isis=isis, **base))
        self.assertEqual((rc, out.split(), err), (0, ["ipv6Vpn=Established", "pendingPeers="], ""))
        # gw3 has not negotiated VPNv6: reported (Partial, pendingPeers), not an error: its own job repairs it
        rc, out, err, _ = self.root(core_answers([gw2, gw3], [gw2], isis=isis, **base))
        self.assertEqual((rc, out.split()), (0, ["ipv6Vpn=Partial", "pendingPeers=" + gw3]))
        self.assertIn("peer not ready on the SRv6 core: VPNv6 %s: NoNeg" % gw3, err)
        # gw3's locator (node field 1 << 8 | 3) not learned: the same
        rc, out, err, _ = self.root(core_answers([gw2, gw3], [gw2, gw3], isis=isis[:3], **base))
        self.assertEqual((rc, out.split()), (0, ["ipv6Vpn=Partial", "pendingPeers=" + gw3]))
        self.assertIn("5f00:db8:103::/48 of %s is not an installed IS-IS route" % gw3, err)
        # neither gateway complete: NoNeg, still no error
        rc, out, _, _ = self.root(core_answers([gw2, gw3], [], isis=isis, **base))
        self.assertEqual((rc, out.split()), (0, ["ipv6Vpn=NoNeg", "pendingPeers=%s,%s" % (gw2, gw3)]))
        # the root's IPv4 local rule is checked in the root namespace too
        a = core_answers([gw2, gw3], [gw2, gw3], isis=isis, **base)
        a["ip -4 rule show"] = RULES4_DEFAULT
        rc, _, err, calls = self.root(a)
        self.assertEqual(rc, 1)
        self.assertIn("the IPv4 rule lookup local is at 0", err)
        self.assertIn(["ip", "-4", "rule", "show"], calls)
        # the root's own state stays required while gateways are pending
        rc, _, err, _ = self.root(core_answers([gw2, gw3], [gw2], isis=isis, seg6={"br-core": "1"}, **base))
        self.assertEqual(rc, 1)
        self.assertIn("net.ipv6.conf.br-core.seg6_enabled is not 0", err)
        # a session from outside the pool (not a provider edge) is not waited for
        rc, out, _, _ = self.root(core_answers([gw2, "10.255.0.9"], [gw2], isis=isis[:2], **base))
        self.assertEqual((rc, out.split()[0]), (0, "ipv6Vpn=Established"))

    def test_locator_on_the_second_node(self):
        # node 2: the root's locator 5f00:db8:201::/48, a gateway with index 2 has 5f00:db8:202::/48
        v = srv6(ROOT_VARS)
        v["hf_pe"] = dict(RT_PE, locator="5f00:db8:201::/48")
        code, args = snippet(render(shell_of(RT, T_RT_WAIT), **v))
        rc, out, err, _ = run_code(code, args, core_answers(
            [self.GW6], [self.GW6], isis=[self.GW6 + "/128", "5f00:db8:202::/48"], own=["5f00:db8:201::/48"],
            running=self.root_running(), guard=guard_table("br-core")))
        self.assertEqual((rc, err), (0, ""))


RBLOCK, RPOOL6 = "5f00:b046::/32", "2001:db8:4005:ffff::/64"


def replica_answers(inst, leg, own, running="", ipv4_established=None):
    """run() answers from what the replica recorded for one instance (tests/fixtures/srv6-replica-p2/<inst>__*):
    the VPN summaries, IS-IS routes, policy rules and guard listing as FRR 8.5.3, iproute2 and nft printed them on the
    SRv6 core, the sysctls as the apply sets them (0, strict mode 1). Not recorded there: the locator on lo (from the
    test VM's outputs, own_locator()), the root's running configuration (its PE
    listen range, given as text) and a gateway's `show bgp ipv4 vpn summary established json` (ipv4_established: the
    instance's IPv6 established summary stands in, the same header and peer entry shape)."""
    a = {"net.vrf.strict_mode": "1\n"}
    for dev in ("all", "default", leg):
        a["net/ipv6/conf/%s/seg6_enabled" % dev] = "0\n"
    est4 = inst + "__show_bgp_ipv4_vpn_summary_established_json.json"
    a.update({"ip -6 rule show": replica(inst + "__ip6-rule.txt"), "ip -4 rule show": RULES4,
              "ipv4 vpn summary established json": replica(ipv4_established or est4),
              "ipv6 vpn summary established json": replica(inst + "__show_bgp_ipv6_vpn_summary_established_json.json"),
              "show bgp ipv4 vpn summary json": replica(inst + "__show_bgp_ipv4_vpn_summary_json.json"),
              "show bgp ipv6 vpn summary json": replica(inst + "__show_bgp_ipv6_vpn_summary_json.json"),
              "show ipv6 route isis json": replica(inst + "__show_ipv6_route_isis_json.json"),
              "show running-config": running,
              "nft list table inet shc_core_guard": replica(inst + "__shc_core_guard.txt")})
    a.update(own_locator([own]))
    return a


class ReplicaReadiness(unittest.TestCase):
    """The SRv6 check against the replica's real outputs (FRR 8.5.3, uSID, one root and three gateways; gw-site-b's
    session to the root was down when they were taken: Idle, no IS-IS routes)."""

    def gw(self, site, lo6, loc, **over):
        v = srv6(dict(GW_VARS, fab_real={"core": dict(CORE, srv6={"block": RBLOCK})},
                      cg_pe=dict(GW_PE, loopbackIp6=lo6, locator=loc)))
        code, args = snippet(render(shell_of(GW, T_GW_WAIT), **v))
        inst = "gw-" + site
        a = replica_answers(inst, "core0", loc, ipv4_established=inst + "__show_bgp_ipv6_vpn_summary_established_json.json")
        a.update(over)
        return run_code(code, args, a)

    def test_gateway_whose_locator_is_not_announced(self):
        """the replica's failed gateway (its zebra crashed, watchfrr restarted the daemons): sessions up, routes in, but
        its own LSP without its locator, so no other node can reach its SIDs. The job then reported ready; the check
        must fail it. The IS-IS database as recorded there (an excerpt: the LSP headers and IPv6 reachability lines)."""
        failed = locfix("gw-site-a-failed__isis-db-excerpt.txt")
        self.assertIn("localhost.loca.00-00 *", failed)
        rc, out, err, _ = self.gw("site-a", "2001:db8:4005:ffff::2", "5f00:b046:102::/48",
                                  **{"show isis database detail": failed})
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=Established"))
        self.assertIn("the own IS-IS LSP does not announce the locator 5f00:b046:102::/48", err)
        # the root's locator in another LSP of the same database does not count for the root itself either
        code, _ = snippet(render(shell_of(GW, T_GW_WAIT), **srv6(GW_VARS)))
        ns = {"__name__": "x"}
        exec(compile(code, "<inline check>", "exec"), ns)
        self.assertFalse(ns["announced"](failed, ipaddress.ip_network("5f00:b046:101::/48")))
        self.assertFalse(ns["announced"](failed, ipaddress.ip_network("5f00:b046:102::/48")))
        db = locfix("pe1__show_isis_database_detail.txt")
        self.assertTrue(ns["announced"](db, ipaddress.ip_network(VM_LOC)))
        self.assertFalse(ns["announced"](db, ipaddress.ip_network("fd00:5e6:3::/48")))  # another node's LSP

    def test_gateways_up(self):
        for site, lo6, loc in (("site-a", "2001:db8:4005:ffff::2", "5f00:b046:102::/48"),
                               ("site-c", "2001:db8:4005:ffff::4", "5f00:b046:104::/48")):
            with self.subTest(site=site):
                rc, out, err, _ = self.gw(site, lo6, loc)
                self.assertEqual((rc, out.strip(), err), (0, "ipv6Vpn=Established", ""))

    def test_gateway_whose_session_is_down(self):
        rc, out, err, _ = self.gw("site-b", "2001:db8:4005:ffff::3", "5f00:b046:103::/48")
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=Idle"))
        self.assertIn("VPNv4 2001:db8:4005:ffff::1: Idle", err)
        self.assertIn("VPNv6 2001:db8:4005:ffff::1: Idle", err)
        self.assertIn("2001:db8:4005:ffff::1/128 of 2001:db8:4005:ffff::1 is not an installed IS-IS route", err)
        self.assertIn("5f00:b046:101::/48 of 2001:db8:4005:ffff::1 is not an installed IS-IS route", err)

    def test_root_with_one_gateway_down(self):
        # the root sees site-a (::2) and site-c (::4); site-b's session is not up, so not a peer: Established
        v = srv6(dict(ROOT_VARS, fab_real={"core": dict(CORE, srv6={"block": RBLOCK})},
                      hf_pe=dict(RT_PE, locator="5f00:b046:101::/48")))
        code, args = snippet(render(shell_of(RT, T_RT_WAIT), **v))
        a = replica_answers("root", "br-core", "5f00:b046:101::/48",
                            running="router bgp 65010\n bgp listen range %s peer-group PE\n" % RPOOL6)
        rc, out, err, _ = run_code(code, args, a)
        self.assertEqual((rc, out.split(), err), (0, ["ipv6Vpn=Established", "pendingPeers="], ""))
        # site-c's locator withdrawn: reported, not an error
        isis = json.loads(a["show ipv6 route isis json"])
        isis.pop("5f00:b046:104::/48")
        a["show ipv6 route isis json"] = json.dumps(isis)
        rc, out, err, _ = run_code(code, args, a)
        self.assertEqual((rc, out.split()), (0, ["ipv6Vpn=Partial", "pendingPeers=2001:db8:4005:ffff::4"]))

    def test_guard_listings(self):
        code, _ = snippet(render(shell_of(GW, T_GW_WAIT), **srv6(GW_VARS)))
        ns = {"__name__": "x"}
        exec(compile(code, "<inline check>", "exec"), ns)
        block = ipaddress.ip_network(RBLOCK)
        for name, leg in (("root__shc_core_guard.txt", "br-core"), ("gw-site-a__shc_core_guard.txt", "core0"),
                          ("gw-site-a__shc_core_guard-stateless.txt", "core0")):
            with self.subTest(listing=name):
                self.assertTrue(ns["guarded"](replica(name), block, leg))
                self.assertFalse(ns["guarded"](replica(name), block, "core9"))

    def test_sid_check(self):
        sh = render(shell_of(GW, T_GW_SID), **srv6(GW_VARS))
        code, _ = snippet(sh)
        for inst, loc, vrfs, conf in (
                ("gw-site-a", "5f00:b046:102::/48", ["t51000:51000", "t51003:51003"], sid_conf("t51000", "t51003")),
                ("gw-site-b", "5f00:b046:103::/48", ["t51001:51001"], sid_conf("t51001")),
                ("gw-site-c", "5f00:b046:104::/48", ["t51002:51002"], sid_conf("t51002")),
                ("root", "5f00:b046:101::/48", ["e10000:110000"], sid_conf("e10000"))):
            with self.subTest(inst=inst):
                answers = {"cat /c": conf, "route show table all": replica(inst + "__ip6-route-all.txt")}
                self.assertEqual(run_code(code, ["", loc, "/c"] + vrfs, answers)[:2], (0, ""))
                # the seg6local lines alone (the replica's SID lists) give the same
                if inst != "root":
                    answers["route show table all"] = replica(inst + "__sids.txt")
                    self.assertEqual(run_code(code, ["", loc, "/c"] + vrfs, answers)[:2], (0, ""))
        # a SID of another gateway's locator does not count
        answers = {"cat /c": sid_conf("t51000"), "route show table all": replica("gw-site-a__ip6-route-all.txt")}
        self.assertEqual(run_code(code, ["", "5f00:b046:104::/48", "/c", "t51000:51000"], answers)[1], "t51000 51000\n")

    def test_exported_from_real_running_config(self):
        # FRR's own printout of gw-site-a's configuration (" !" lines inside the VRF's BGP instance)
        self.assertEqual(exported_of(GW)(replica("gw-site-a__running-config.txt")), {"t51000"})


class Srv6SidCheck(unittest.TestCase):
    """The per-VRF SID check with the spike's real kernel output (locators of the spike's block fd00:5e6::/32)."""

    def check(self, netns, locator, vrfs, text, conf=None):
        sh = render(shell_of(GW, T_GW_SID), **srv6(dict(GW_VARS, cg_pe=dict(GW_PE, locator=locator))))
        code, _ = snippet(sh)
        args = [netns, locator, "/etc/frr/x.conf"] + vrfs
        if conf is None:
            conf = sid_conf(*[v.split(":")[0] for v in vrfs])
        rc, out, _, calls = run_code(code, args, {"cat /etc/frr/x.conf": conf, "route show table all": text})
        return rc, out, calls

    def test_plain_pe(self):
        rc, out, calls = self.check("gw-x", "fd00:5e6:3::/48", ["t51001:51001"], fixture("pe2-route6.txt"))
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(calls, [["cat", "/etc/frr/x.conf"], ["ip", "-n", "gw-x", "-6", "route", "show", "table", "all"]])

    def test_encapsulating_routes_are_no_sid(self):
        # pe2's VPN routes to the other PEs are seg6 encap routes, not End.DT46: they never count for a VRF
        text = "".join(l + "\n" for l in fixture("pe2-route6.txt").splitlines() if "seg6local" not in l)
        rc, out, _ = self.check("gw-x", "fd00:5e6:3::/48", ["t51001:51001"], text)
        self.assertEqual(out, "t51001 51001\n")

    def test_missing_vrf_and_table(self):
        rc, out, _ = self.check("", "fd00:5e6:3::/48", ["t51001:51001", "t51002:51002"], fixture("pe2-route6.txt"))
        self.assertEqual(out, "t51002 51002\n")

    def test_sid_from_another_locator(self):
        # the gateway analog's SID into 51000 is from fd00:5e6:2::/48: not this PE's
        rc, out, _ = self.check("", "fd00:5e6:3::/48", ["t51000:51000"], fixture("pe1-sids.txt"))
        self.assertEqual(out, "t51000 51000\n")
        rc, out, _ = self.check("", "fd00:5e6:2::/48", ["t51000:51000"], fixture("pe1-sids.txt"))
        self.assertEqual(out, "")

    def test_remote_pe_and_recreated_vrf(self):
        rc, out, _ = self.check("", "fd00:5e6:5::/48", ["t51005:51005"], fixture("edge-route6.txt"))
        self.assertEqual(out, "")
        # the VRF created again under the same name: a new function, the same locator
        rc, out, _ = self.check("", "fd00:5e6:3::/48", ["t51001:51001"], fixture("pe2-sids-vrf-recreated.txt"))
        self.assertEqual(out, "")

    def test_task_arguments(self):
        _, args = snippet(render(shell_of(GW, T_GW_SID), **srv6(dict(GW_VARS, cg_vrfs=[
            {"vrf": "t51000", "vni": 51000}, {"vrf": "t51001", "vni": 51001, "families": [6]}]))))
        self.assertEqual(args, ["gw-iaas1", GW_PE["locator"], "/etc/frr/gw-iaas1/frr.conf.shc", "t51000:51000",
                                "t51001:51001"])
        _, args = snippet(render(shell_of(RT, T_RT_SID), **srv6(dict(ROOT_VARS, hf_node_vrfs=[
            {"vrf": "e100", "table": 100100, "families": [4, 6]}, {"vrf": "e101", "table": 100101}]))))
        self.assertEqual(args, ["", RT_PE["locator"], "/etc/frr/frr.conf", "e100:100100", "e101:100101"])

    def test_vrfs_without_an_export_are_not_checked(self):
        # e101 exports nothing (no per-VRF SID in the configuration): no SID expected for it
        rc, out, _ = self.check("", "fd00:5e6:3::/48", ["t51001:51001", "e101:100101"], fixture("pe2-route6.txt"),
                                conf=sid_conf("t51001") + "router bgp 65010 vrf e101\n bgp router-id 10.255.0.1\nexit\n")
        self.assertEqual((rc, out), (0, ""))


def exported_of(path):
    code, _ = snippet(render(shell_of(path, T_GW_SID if path == GW else T_RT_SID),
                             **srv6(dict(ROOT_VARS, **GW_VARS) if path == GW else ROOT_VARS)))
    ns = {"__name__": "x"}
    exec(compile(code, "<inline check>", "exec"), ns)
    return ns["exported"]


class SidVrfsFromTheRenderedConfiguration(unittest.TestCase):
    """The VRFs that must have a SID, read from stream B's templates as rendered (tests/fixtures/srv6-frr-renders/)."""

    def render_text(self, name):
        with open(os.path.join(ROOT, "tests", "fixtures", "srv6-frr-renders", name)) as f:
            return f.read()

    def test_gateway_every_tenant_vrf(self):
        text = self.render_text("gw-srv6-frr8.conf")
        vrfs = {l.split()[1] for l in text.splitlines() if l.startswith("vrf ")}
        self.assertEqual(vrfs, {"t51000", "t51003"})
        # every tenant VRF exports: the derived set is today's list
        self.assertEqual(exported_of(GW)(text), vrfs)

    def test_root(self):
        f = exported_of(RT)
        # the network VRF exports, the egress VRF (its own AS, no VPN) does not
        self.assertEqual(f(self.render_text("root-srv6-frr8.conf")), {"e10000"})
        # network VRFs that export nothing to the VPN: none to check
        self.assertEqual(f(self.render_text("root-srv6-frr8-noexport.conf")), set())

    def test_same_function_on_both(self):
        text = self.render_text("gw-srv6-frr8.conf") + self.render_text("root-srv6-frr8.conf")
        self.assertEqual(exported_of(GW)(text), exported_of(RT)(text))



class Srv6SidScript(unittest.TestCase):
    """The whole SID task (settle, then fail naming the VRF) with ip and sleep replaced."""

    def run_task(self, kernel):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("ip", '#!/bin/bash\n/usr/bin/cat "$SHIM/kernel"\n'), ("sleep", "#!/bin/bash\nexit 0\n"),
                               ("cat", '#!/bin/bash\n[ "$1" = /etc/frr/gw-iaas1/frr.conf.shc ] || exit 1\n'
                                       '/usr/bin/cat "$SHIM/conf"\n')):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
                os.chmod(os.path.join(d, name), 0o755)
            with open(os.path.join(d, "kernel"), "w") as f:
                f.write(kernel)
            with open(os.path.join(d, "conf"), "w") as f:
                f.write(sid_conf("t51001", "t51002"))
            v = srv6(dict(GW_VARS, cg_pe=dict(GW_PE, locator="fd00:5e6:3::/48"),
                          cg_vrfs=[{"vrf": "t51001", "vni": 51001}, {"vrf": "t51002", "vni": 51002}]))
            sh = render(shell_of(GW, T_GW_SID), **v)
            env = dict(os.environ, SHIM=d, PATH=d + os.pathsep + os.environ.get("PATH", ""))
            return subprocess.run(["bash", "-s"], input=sh, capture_output=True, text=True, env=env, timeout=60)

    def test_all_there(self):
        k = fixture("pe2-route6.txt") + fixture("pe2-sids-vrf-recreated.txt").replace("vrftable 51001 dev t51001",
                                                                                       "vrftable 51002 dev t51002")
        proc = self.run_task(k)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "sids=ok"), proc.stderr)

    def test_missing_names_the_vrf(self):
        proc = self.run_task(fixture("pe2-route6.txt"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("VRF t51002: no SRv6 SID in the kernel (no End.DT46 route from locator fd00:5e6:3::/48 into its "
                      "table 51002)", proc.stderr)
        self.assertNotIn("t51001", proc.stderr)
        # no label is bound again: nothing but ip is called
        self.assertNotIn("label vpn", render(shell_of(GW, T_GW_SID), **srv6(GW_VARS)))


# ---- srv6: the records and the status -------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Srv6Records(unittest.TestCase):
    unchanged = {"changed": False}

    def root_record(self, transport, verify=("ipv6Vpn=Established", "pendingPeers=")):
        task = by_name(load(RT), P + "record the node")
        v = dict(copy.deepcopy(ROOT_VARS), hf_node_attach={}, _hf_verify={"stdout_lines": list(verify)},
                 **{k: self.unchanged for k in ("_hf_files", "_hf_modules", "_hf_rootconf", "_hf_vrfgone", "_hf_unit",
                                                "_hf_apply", "_hf_daemons", "_hf_frrconf", "_hf_frr_state")})
        if transport:
            v["fab_node_transport"] = transport
        return run_play([task, write_out("hf_node_results")], v)[0]

    def test_root(self):
        out = self.root_record("srv6")
        self.assertTrue(out["message"].startswith(
            "root instance: VPNv4 and VPNv6 route reflector, SRv6 (locator 5f00:db8:101::/48), br-core"), out["message"])
        self.assertEqual((out["transport"], out["locator"], out["ipv6Vpn"], out["pendingPeers"]),
                         ("srv6", "5f00:db8:101::/48", "Established", []))
        self.assertNotIn("not ready on the SRv6 core", out["message"])
        out = self.root_record("srv6", ["ipv6Vpn=Partial", "pendingPeers=2001:db8:4005:ffff::3"])
        self.assertEqual((out["ipv6Vpn"], out["pendingPeers"]), ("Partial", ["2001:db8:4005:ffff::3"]))
        self.assertTrue(out["message"].endswith(", not ready on the SRv6 core: 2001:db8:4005:ffff::3"), out["message"])
        for t in (None, "mpls"):
            out = self.root_record(t)
            self.assertTrue(out["message"].startswith("root instance: VPNv4 and VPNv6 route reflector, IS-IS SR (SID "
                                                      "16001), br-core"))
            self.assertNotIn("transport", out)
            self.assertNotIn("locator", out)

    def gw_record(self, transport):
        task = by_name(load(GW), P + "record the node")
        v = dict(copy.deepcopy(GW_VARS), cg_node_addr="10.231.0.2",
                 _cg_sessions={"stdout": json.dumps({"site": 2, "upstream": 0})}, _cg_tunnel={"stdout": ""},
                 _cg_vrf_routes={"stdout_lines": ["t51000 4 2", "t51000 6 1"]},
                 _cg_verify={"stdout_lines": ["ipv6Vpn=Established"]},
                 **{k: self.unchanged for k in ("_cg_files", "_cg_conf", "_cg_apply", "_cg_daemons", "_cg_frrconf",
                                                "_cg_state")})
        if transport:
            v["fab_node_transport"] = transport
        start = {"ansible.builtin.set_fact": {"cg_node_results": [], "cg_changed_nodes": []}}
        return run_play([start, task, write_out("cg_node_results")], v)[0]

    def test_gateway(self):
        out = self.gw_record("srv6")
        self.assertTrue(out["message"].startswith(
            "gateway instance gw-iaas1: VPNv4 and VPNv6 to route reflector 2001:db8:4005:ffff::1, SRv6 locator "
            "5f00:db8:102::/48, loopback 10.255.0.2"), out["message"])
        self.assertEqual((out["transport"], out["locator"], out["coreSessions"]), ("srv6", "5f00:db8:102::/48", 1))
        out = self.gw_record("mpls")
        self.assertTrue(out["message"].startswith("gateway instance gw-iaas1: VPNv4 and VPNv6 to 10.255.0.1, IS-IS SR "
                                                  "SID 16002, loopback 10.255.0.2"))
        self.assertNotIn("transport", out)

    def test_fabric_status(self):
        t = by_name(load(HF_MAIN), "Report the fabric ready")
        common = {"fab_name": "lab", "hf_ledger": {"allocated": {}, "available": {}},
                  "hf_egress_status": [], "hf_egress6_status": {"enabled": False},
                  "hf_attach": {"status": [], "skipped": []}, "hf_ipam": {}, "hf_pe": RT_PE}
        nodes = [{"name": "bgw-1", "transport": "srv6", "locator": "5f00:db8:101::/48"},
                 {"name": "bgw-2", "transport": "srv6", "locator": "5f00:db8:201::/48"}]
        out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")],
                       dict(common, fab_real={"model": "linux-pe", "egress": {},
                                              "core": dict(CORE, transport="srv6", linkSubnet="10.255.1.0/24")},
                            hf_node_results=nodes))
        r = out["realization"]
        self.assertEqual((r["coreTransport"], r["srv6Locators"]),
                         ("srv6", {"bgw-1": "5f00:db8:101::/48", "bgw-2": "5f00:db8:201::/48"}))
        out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")],
                       dict(common, fab_real={"model": "linux-pe", "egress": {},
                                              "core": dict(CORE, linkSubnet="10.255.1.0/24")},
                            hf_node_results=[{"name": "bgw-1"}]))
        self.assertEqual(out["realization"]["coreTransport"], "mpls")
        self.assertNotIn("srv6Locators", out["realization"])

    def test_gateway_status(self):
        t = by_name(load(CG_MAIN), "Report the gateway ready")
        common = {"cg_status": {"site": {}}, "cg_site_extra": {}, "cg_instance": {}, "fab_name": "site1",
                  "cg_vrfs": [], "cg_pe": GW_PE, "cg_root_pe": RT_PE}
        out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")],
                       dict(common, fab_real={"model": "linux-pe", "core": {"transport": "srv6"}},
                            cg_node_results=[{"name": "bgw-1", "locator": "5f00:db8:102::/48"}]))
        self.assertEqual((out["realization"]["coreTransport"], out["realization"]["srv6Locators"]),
                         ("srv6", {"bgw-1": "5f00:db8:102::/48"}))
        out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")],
                       dict(common, fab_real={"model": "linux-pe"}, cg_node_results=[{"name": "bgw-1"}]))
        self.assertEqual(out["realization"]["coreTransport"], "mpls")
        self.assertNotIn("srv6Locators", out["realization"])


CGU = os.path.join(ROLES, "hybridfabric_provision", "tasks", "core_guard_unit.yml")


class CoreGuardUnit(unittest.TestCase):
    """The SRv6 core guard between jobs: a timer per instance running `shc-fabric-apply <instance> guard`, installed
    and enabled only on srv6, stopped with a gateway instance and removed with the root."""

    def test_included_on_srv6_only(self):
        for path, inst in ((RT, "root"), (GW, "gw-{{ fab_name }}")):
            t = by_name(load(path), P + "keep the SRv6 core guard")
            self.assertTrue(t["ansible.builtin.include_tasks"].endswith("core_guard_unit.yml"))
            self.assertEqual((t["vars"]["cgu_instance"], t["when"]), (inst, "fab_node_transport | default('mpls') == 'srv6'"))
            # after the apply (the apply loads the guard first), before the wait
            names = [x.get("name", "") for x in load(path)]
            apply = [i for i, n in enumerate(names) if n.startswith(P + "apply the ")][0]
            self.assertLess(apply, names.index(t["name"]))
            # nothing else installs or enables it
            from play_helper import walk
            self.assertEqual(len([x for x in walk(load(path))
                                  if str(x.get("ansible.builtin.include_tasks", "")).endswith("core_guard_unit.yml")]), 1)
            self.assertNotIn("shc-core-guard@", open(path).read())

    def test_units(self):
        tasks = load(CGU)
        units = {i["name"]: i["content"] for i in tasks[0]["loop"]}
        self.assertIn("ExecStart=/usr/local/sbin/shc-fabric-apply %i guard\n", units["shc-core-guard@.service"])
        self.assertIn("Type=oneshot\n", units["shc-core-guard@.service"])
        self.assertIn("OnUnitActiveSec=15s\n", units["shc-core-guard@.timer"])
        self.assertIn("WantedBy=timers.target\n", units["shc-core-guard@.timer"])
        sd = tasks[1]["ansible.builtin.systemd"]
        self.assertEqual((sd["name"], sd["enabled"], sd["state"]), ("shc-core-guard@{{ cgu_instance }}.timer", True, "started"))

    def run_teardown(self, path, name, have_units, fab_name="site1"):
        t = copy.deepcopy(by_name(load(path), P + name))
        sh = t["ansible.builtin.shell"]
        with tempfile.TemporaryDirectory() as d:
            unit_dir = os.path.join(d, "etc/systemd/system")
            os.makedirs(unit_dir)
            if have_units:
                for u in ("shc-core-guard@.timer", "shc-core-guard@.service"):
                    open(os.path.join(unit_dir, u), "w").close()
            with open(os.path.join(d, "systemctl"), "w") as f:
                f.write('#!/bin/bash\necho "$*" >> "%s/log"\n'
                        'case "$1" in list-units) echo "shc-core-guard@gw-a.timer loaded active waiting x" ;; '
                        'is-enabled) exit 0 ;; esac\nexit 0\n' % d)
            os.chmod(os.path.join(d, "systemctl"), 0o755)
            sh = render(sh.replace("/etc/systemd/system/", unit_dir + "/"), fab_name=fab_name)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""))
            proc = subprocess.run(["bash", "-s"], input=sh, capture_output=True, text=True, env=env, timeout=30)
            log = open(os.path.join(d, "log")).read() if os.path.exists(os.path.join(d, "log")) else ""
            return proc, log, sorted(os.listdir(unit_dir))

    def test_gateway_teardown(self):
        path = os.path.join(ROLES, "cloudgateway_teardown", "tasks", "node_gateway_down.yml")
        proc, log, _ = self.run_teardown(path, "stop the instance's SRv6 core guard", True)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "stopped"), proc.stderr)
        self.assertIn("disable --now shc-core-guard@gw-site1.timer", log)
        # an mpls node: no unit template, nothing called
        proc, log, _ = self.run_teardown(path, "stop the instance's SRv6 core guard", False)
        self.assertEqual((proc.returncode, proc.stdout, log), (0, "", ""))
        # before the namespace goes
        names = [x.get("name", "") for x in load(path)]
        self.assertLess(names.index(P + "stop the instance's SRv6 core guard"),
                        names.index(P + "remove the namespace and the core leg"))

    def test_root_teardown(self):
        path = os.path.join(ROLES, "hybridfabric_teardown", "tasks", "node_root_down.yml")
        proc, log, left = self.run_teardown(path, "remove the SRv6 core guard units", True)
        self.assertEqual((proc.returncode, proc.stdout.strip(), left), (0, "removed", []), proc.stderr)
        self.assertIn("disable --now shc-core-guard@root.timer", log)
        self.assertIn("disable --now shc-core-guard@gw-a.timer", log)
        proc, log, _ = self.run_teardown(path, "remove the SRv6 core guard units", False)
        self.assertEqual((proc.returncode, proc.stdout, log), (0, "", ""))
        names = [x.get("name", "") for x in load(path)]
        self.assertLess(names.index(P + "remove the SRv6 core guard units"),
                        names.index(P + "remove the root instance (core bridge, loopback)"))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RootUnitStartReason(unittest.TestCase):
    """A root unit start that fails (its apply refused) fails the job with the apply's own lines, read from the
    unit's journal; a start that succeeds goes on as before."""

    STUB = """#!/bin/bash
    case "$1" in
      is-active) cat "$SHIM/state" ;;
      show) echo 4711 ;;
    esac
    """
    JOURNAL = """#!/bin/bash
    echo "$*" >> "$SHIM/jargs"
    cat "$SHIM/journal"
    """

    def run_steps(self, state, journal, unit_result):
        tasks = load(RT)
        check = copy.deepcopy(by_name(tasks, P + "the root instance runs, or why not"))
        stop = by_name(tasks, P + "stop when the root instance did not start")
        check.pop("delegate_to")
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("systemctl", self.STUB), ("journalctl", self.JOURNAL)):
                with open(os.path.join(d, name), "w") as f:
                    f.write(textwrap.dedent(body))
                os.chmod(os.path.join(d, name), 0o755)
            for name, body in (("state", state + "\n"), ("journal", journal)):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            check["environment"] = {"SHIM": d, "PATH": d + ":" + os.environ.get("PATH", "")}
            try:
                out = run_play([check, stop, write_out("'went on'")],
                               {"bgw_node": {"name": "bgw-1"}, "fab_name": "lab", "_hf_unit": unit_result})
                jargs = open(os.path.join(d, "jargs")).read() if os.path.exists(os.path.join(d, "jargs")) else ""
                return out, jargs
            except AssertionError as e:
                return str(e), ""

    def test_active_goes_on(self):
        out, jargs = self.run_steps("active", "", {"changed": False})
        self.assertEqual((out, jargs), ("went on", ""))

    def test_failed_start_names_the_apply_reason(self):
        journal = ("Starting shc fabric instance root...\n"
                   "transport srv6: net.ipv4.conf.all.rp_filter is 1; the root's network VRFs need it 0\n"
                   "shc-fabric-apply@root.service: Main process exited, code=exited, status=4/NOPERMISSION\n")
        out, _ = self.run_steps("failed", journal, {"failed": False, "msg": "Unable to start service shc-fabric-apply@root.service"})
        self.assertIn("HybridFabric lab on bgw-1: the root instance (shc-fabric-apply@root.service) did not start: "
                      "transport srv6: net.ipv4.conf.all.rp_filter is 1; the root's network VRFs need it 0", out)
        self.assertNotIn("went on", out)

    def test_failed_start_without_a_reason_line(self):
        # the mpls path, or a failure the apply did not explain: systemd's message, as before
        out, _ = self.run_steps("failed", "something else\n",
                                {"failed": False, "msg": "Unable to start service shc-fabric-apply@root.service"})
        self.assertIn("did not start: Unable to start service shc-fabric-apply@root.service", out)

    def test_the_start_itself_no_longer_fails_but_a_timeout_does(self):
        t = by_name(load(RT), P + "run the root instance at boot (and now)")
        self.assertIs(t["failed_when"], False)
        names = [x.get("name", "") for x in load(RT)]
        i = names.index(t["name"])
        self.assertEqual(names[i + 1:i + 3], [P + "the root instance runs, or why not",
                                              P + "stop when the root instance did not start"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class DirectApplyReason(unittest.TestCase):
    """An apply run directly on a node that refuses (exit 4, "transport srv6: …" on stderr) fails its step as before;
    the role's node-loop rescue puts the apply's own lines into fab_fail_msg, which the status message takes."""

    def rescue_task(self, path):
        main = load(path)
        loop = by_name(main, "Configure the nodes, one at a time" if path == HF_MAIN
                       else "Configure the gateway on the nodes, one at a time")
        names = [x.get("name") for x in loop["rescue"]]
        self.assertEqual(names, ["Keep the apply's own reason for the status",
                                 "Keep the lease while a step may still run on a node"])
        return copy.deepcopy(loop["rescue"][0])

    def run_failing(self, path, cmd, before=None, timeout=None):
        step = {"name": "bgw-1: apply the root instance", "ansible.builtin.shell": cmd, "args": {"executable": "/bin/bash"},
                "register": "_a", "failed_when": "_a.rc != 0 or 'IPv6 core not applied' in _a.stdout"}
        if timeout:
            step["timeout"] = timeout
        start = {"ansible.builtin.set_fact": {"fab_fail_msg": before}} if before is not None else {"ansible.builtin.debug": {"msg": "x"}}
        play = [start, {"block": [step], "rescue": [self.rescue_task(path)]},
                write_out("fab_fail_msg | default('UNSET')")]
        return run_play(play, {})

    def test_refusal_reaches_the_message(self):
        for path in (HF_MAIN, CG_MAIN):
            with self.subTest(path=os.path.basename(os.path.dirname(os.path.dirname(path)))):
                out = self.run_failing(path, "echo 'transport srv6: net.ipv4.conf.all.rp_filter is 1; the root VRFs need "
                                             "loose or no reverse path filtering' >&2; echo 'transport srv6: second' >&2; exit 4",
                                       before="")
                self.assertEqual(out, "bgw-1: apply the root instance: non-zero return code: transport srv6: "
                                      "net.ipv4.conf.all.rp_filter is 1; the root VRFs need loose or no reverse path "
                                      "filtering; transport srv6: second")

    def test_mpls_and_other_failures_unchanged(self):
        for path in (HF_MAIN, CG_MAIN):
            # no such line: fab_fail_msg is left as it was (the status message as today)
            self.assertEqual(self.run_failing(path, "echo 'some other error' >&2; exit 3", before=""), "")
            self.assertEqual(self.run_failing(path, "exit 2"), "UNSET")
            # the marker failure of an mpls apply (exit 0): no stderr line, unchanged
            self.assertEqual(self.run_failing(path, "echo 'warning: IPv6 core not applied'", before=""), "")
            # a step that timed out keeps the lease keeper's message
            self.assertEqual(self.run_failing(path, "echo 'transport srv6: x' >&2; sleep 5", before="", timeout=1), "")

    def test_the_apply_steps_are_as_before(self):
        # the steps' outcome is untouched: their failed_when is the base commit's
        if not have_base():
            self.skipTest("base commit missing")
        # (the gateway's second apply, "without the filter", is gone with the turn-off sequence: the VRFs' input
        # filter comes with the IPv6 core and never leaves with the announcement)
        for path, name in ((RT, P + "apply the root instance"),
                           (GW, P + "apply the gateway instance (namespace, site leg, core leg)")):
            self.assertEqual(by_name(load(path), name), by_name(base_tasks(path), name), name)


class TemplatesGetTheirVariables(unittest.TestCase):
    """Every task that renders an FRR template comes after the transport and FRR lookup (node_transport.yml)."""

    def test_order(self):
        for path, tpl in ((RT, "frr-root.conf.j2"), (GW, "frr-gateway.conf.j2")):
            names = [t.get("name", "") for t in load(path)]
            tasks = load(path)
            nt = names.index(P + "the core transport and FRR on this node")
            self.assertTrue(str(tasks[nt]["ansible.builtin.include_tasks"]).endswith("node_transport.yml"))
            for i, t in enumerate(tasks):
                if "ansible.builtin.template" in t:
                    self.assertGreater(i, nt, t["name"])
            self.assertIn(tpl, [t["ansible.builtin.template"]["src"] for t in tasks if "ansible.builtin.template" in t])


if __name__ == "__main__":
    unittest.main()

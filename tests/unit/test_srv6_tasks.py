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
- the FRR JSON (BGP summaries, `show ipv6 route isis|static json`) is written in the shapes FRR 8.5.3's source
  prints, as tests/unit/test_gateway_readiness.py does (the spike kept no JSON); `ip -6 rule show` in the form
  iproute2 prints a rule with a destination selector ("999:<tab>from all to <prefix> lookup main"), not taken from
  a real run either.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
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

@unittest.skipUnless(have_base(), "base commit %s not in the repository" % BASE)
class MplsScriptsUnchanged(unittest.TestCase):
    """Every readiness script renders on mpls as it did at the base commit, with and without fab_node_transport set,
    the IPv6 core on and off, and with the realization's SRv6 block present (the switch decides, not the block)."""

    def same(self, path, name, variables):
        old = render(base_shell(path, name), **variables)
        for v in (variables, dict(variables, fab_node_transport="mpls")):
            self.assertEqual(render(shell_of(path, name), **v), old, name)

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

    def test_gateway_needs_the_ipv6_core(self):
        stop = by_name(load(GW), P + "the SRv6 core needs the fabric's IPv6 core")
        v = {"bgw_node": {"name": "bgw-1"}, "fab_name": "site1", "cg_fabric_name": "lab", "fab_node_transport": "srv6",
             "cg_pe": GW_PE, "cg_root_pe": RT_PE}
        run_play([stop, write_out("1")], dict(v, cg_ipv6_core=True))
        with self.assertRaises(AssertionError) as e:
            run_play([stop, write_out("1")], dict(v, cg_ipv6_core=False))
        self.assertIn("does not report yet (status.realization.ipv6Core)", str(e.exception))
        run_play([stop, write_out("1")], dict(v, cg_ipv6_core=False, fab_node_transport="mpls"))


# ---- srv6: the readiness checks -------------------------------------------------------------------------------------

def summary(peers):
    return json.dumps({"routerId": "10.255.0.2", "as": 65010, "peers": {p: {"remoteAs": 65010, "state": s}
                                                                         for p, s in peers.items()}})


def zebra(protocol, *prefixes, installed=True):
    out = {}
    for p in prefixes:
        r = {"prefix": p, "protocol": protocol, "selected": True, "distance": 115 if protocol == "isis" else 1}
        if installed:
            r["installed"] = True
        out[p] = [r]
    return json.dumps(out)


RULES = ("0:\tfrom all lookup local\n999:\tfrom all to %s lookup main\n1000:\tfrom all lookup [l3mdev-table]\n"
         "32766:\tfrom all lookup main\n")


def core_answers(est4, est6, plain=None, isis=(), static=(), rule=BLOCK, strict="1", seg6=None, guard=True,
                 running=""):
    """run() answers for the SRv6 core check (the first key a command contains wins)."""
    seg6 = seg6 or {}
    a = {"net.vrf.strict_mode": strict + "\n"}
    for dev in ("all", "default", "core0", "br-core"):
        a["net/ipv6/conf/%s/seg6_enabled" % dev] = seg6.get(dev, "1") + "\n"
    a.update({"ip -6 rule show": RULES % rule,
              "ipv4 vpn summary established json": summary({p: "Established" for p in est4}),
              "ipv6 vpn summary established json": summary({p: "Established" for p in est6}),
              "show bgp ipv4 vpn summary json": summary(plain if plain is not None else {p: "Established" for p in est4}),
              "show bgp ipv6 vpn summary json": summary(plain if plain is not None else {p: "Established" for p in est6}),
              "show ipv6 route isis json": zebra("isis", *isis),
              "show ipv6 route static json": zebra("static", *static),
              "show running-config": running})
    if guard:
        a["nft list table inet shc_core_guard"] = "table inet shc_core_guard {\n}\n"
    return a


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
        return run_code(code, args, answers)

    def gw_ok(self, **kw):
        a = dict(est4=[self.RR6], est6=[self.RR6], isis=[self.RR6 + "/128", "5f00:db8:101::/48"],
                 static=["5f00:db8:102::/48"])
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
                        ({"seg6": {"core0": "0"}}, "seg6_enabled is not 1 on core0"),
                        ({"seg6": {"default": "0"}}, "seg6_enabled is not 1 on default"),
                        ({"rule": "5f00:db9::/32"}, "no rule to 5f00:db8::/32 lookup main at preference 999"),
                        ({"guard": False}, "the guard table inet shc_core_guard is not loaded"),
                        ({"static": []}, "the locator 5f00:db8:102::/48 has no installed discard route")):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                rc, _, err, _ = self.gw(self.gw_ok(**kw))
                self.assertEqual(rc, 1)
                self.assertIn(msg, err)
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
        rc, out, err, calls = self.root(core_answers([], [], static=["5f00:db8:101::/48"], running=self.root_running()))
        self.assertEqual((rc, out.strip(), err), (0, "ipv6Vpn=NoPeers", ""))
        self.assertTrue(all(c[0] != "ip" or c[:2] == ["ip", "-6"] for c in calls))
        self.assertIn(["sysctl", "-n", "net/ipv6/conf/br-core/seg6_enabled"], calls)

    def test_root_listen_range(self):
        rc, _, err, _ = self.root(core_answers([], [], static=["5f00:db8:101::/48"],
                                               running=self.root_running("10.255.0.0/24")))
        self.assertEqual(rc, 1)
        self.assertIn("the PE group does not listen on %s" % POOL6, err)

    def test_root_with_gateways(self):
        gw2, gw3 = self.GW6, "2001:db8:4005:ffff::3"
        isis = [gw2 + "/128", "5f00:db8:102::/48", gw3 + "/128", "5f00:db8:103::/48"]
        base = dict(static=["5f00:db8:101::/48"], running=self.root_running())
        rc, out, err, _ = self.root(core_answers([gw2, gw3], [gw2, gw3], isis=isis, **base))
        self.assertEqual((rc, out.strip(), err), (0, "ipv6Vpn=Established", ""))
        # gw3 has not negotiated VPNv6: Partial, and an error (both families are required on the SRv6 core)
        rc, out, err, _ = self.root(core_answers([gw2, gw3], [gw2], isis=isis, **base))
        self.assertEqual((rc, out.strip()), (1, "ipv6Vpn=Partial"))
        self.assertIn("VPNv6 %s: NoNeg" % gw3, err)
        # gw3's locator (node field 1 << 8 | 3) not learned
        rc, _, err, _ = self.root(core_answers([gw2, gw3], [gw2, gw3], isis=isis[:3], **base))
        self.assertEqual(rc, 1)
        self.assertIn("5f00:db8:103::/48 of %s is not an installed IS-IS route" % gw3, err)
        # a session from outside the pool (not a provider edge) is not waited for
        rc, out, _, _ = self.root(core_answers([gw2, "10.255.0.9"], [gw2], isis=isis[:2], **base))
        self.assertEqual((rc, out.strip()), (0, "ipv6Vpn=Established"))

    def test_locator_on_the_second_node(self):
        # node 2: the root's locator 5f00:db8:201::/48, a gateway with index 2 has 5f00:db8:202::/48
        v = srv6(ROOT_VARS)
        v["hf_pe"] = dict(RT_PE, locator="5f00:db8:201::/48")
        code, args = snippet(render(shell_of(RT, T_RT_WAIT), **v))
        rc, out, err, _ = run_code(code, args, core_answers(
            [self.GW6], [self.GW6], isis=[self.GW6 + "/128", "5f00:db8:202::/48"], static=["5f00:db8:201::/48"],
            running=self.root_running()))
        self.assertEqual((rc, err), (0, ""))


class Srv6SidCheck(unittest.TestCase):
    """The per-VRF SID check with the spike's real kernel output (locators of the spike's block fd00:5e6::/32)."""

    def check(self, netns, locator, vrfs, text):
        sh = render(shell_of(GW, T_GW_SID), **srv6(dict(GW_VARS, cg_pe=dict(GW_PE, locator=locator))))
        code, _ = snippet(sh)
        args = [netns, locator] + vrfs
        rc, out, _, calls = run_code(code, args, {"route show table all": text})
        return rc, out, calls

    def test_plain_pe(self):
        rc, out, calls = self.check("gw-x", "fd00:5e6:3::/48", ["t51001:51001"], fixture("pe2-route6.txt"))
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(calls, [["ip", "-n", "gw-x", "-6", "route", "show", "table", "all"]])

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
        self.assertEqual(args, ["gw-iaas1", GW_PE["locator"], "t51000:51000", "t51001:51001"])
        _, args = snippet(render(shell_of(RT, T_RT_SID), **srv6(dict(ROOT_VARS, hf_node_vrfs=[
            {"vrf": "e100", "table": 100100, "families": [4, 6]}, {"vrf": "e101", "table": 100101}]))))
        self.assertEqual(args, ["", RT_PE["locator"], "e100:100100", "e101:100101"])


class Srv6SidScript(unittest.TestCase):
    """The whole SID task (settle, then fail naming the VRF) with ip and sleep replaced."""

    def run_task(self, kernel):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("ip", '#!/bin/bash\ncat "$SHIM/kernel"\n'), ("sleep", "#!/bin/bash\nexit 0\n")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
                os.chmod(os.path.join(d, name), 0o755)
            with open(os.path.join(d, "kernel"), "w") as f:
                f.write(kernel)
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

    def root_record(self, transport):
        task = by_name(load(RT), P + "record the node")
        v = dict(copy.deepcopy(ROOT_VARS), hf_node_attach={}, _hf_verify={"stdout_lines": ["ipv6Vpn=Established"]},
                 **{k: self.unchanged for k in ("_hf_files", "_hf_modules", "_hf_rootconf", "_hf_vrfgone", "_hf_unit",
                                                "_hf_apply", "_hf_daemons", "_hf_frrconf", "_hf_frr_state")})
        if transport:
            v["fab_node_transport"] = transport
        return run_play([task, write_out("hf_node_results")], v)[0]

    def test_root(self):
        out = self.root_record("srv6")
        self.assertTrue(out["message"].startswith(
            "root instance: VPNv4 and VPNv6 route reflector, SRv6 (locator 5f00:db8:101::/48), br-core"), out["message"])
        self.assertEqual((out["transport"], out["locator"], out["ipv6Vpn"]), ("srv6", "5f00:db8:101::/48", "Established"))
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

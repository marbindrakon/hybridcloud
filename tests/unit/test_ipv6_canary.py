"""The translation canary (realization canaries.ipv6Translation, samples/platform/ipv6-canary): the fabric job's scrape
script on a border gateway node (a fake `run`: parsing, stale, unreachable, the gateway namespace and VRF in the
command), the fabric's choice of sites that report a canary, the fabric status with failedSince kept across runs, the
IPv6Translation condition and the alert after alertMinutes, the site job's lookup of the canary placement and the
probe's address (CloudVirt: a fake VirtualMachineInstance list; CloudOSO: the helper's answer), the OpenStack helper's
server-address action (a fake connection) and the shipped sample (the user data parses, its check script passes a
shell syntax check, its placeholders are the documented set). The plays run in ansible-playbook (skipped without it).
Documentation prefixes only."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import types
import unittest
from unittest import mock

import jinja2
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
try:
    import openstack  # noqa: F401
except ImportError:  # stub the SDK so the module imports; server-address never calls it directly
    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(ForbiddenException=Exception, NotFoundException=Exception)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)
import shc_openstack as S  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HF = os.path.join(ROLES, "hybridfabric_provision", "tasks")
CI = os.path.join(ROLES, "cloudinfrastructure_provision", "tasks")
SAMPLE = os.path.join(ROOT, "samples", "platform", "ipv6-canary")

NOW = 1_800_000_000.0
A1, A2 = "2001:db8:4005:4200::10", "2001:db8:4005:4201::10"
SRC = "2001:db8:ffff:1::1"


def metrics(ts, **checks):
    lines = ["# HELP shc_ipv6_canary_check 1 when the check passed", "# TYPE shc_ipv6_canary_check gauge"]
    lines += ['shc_ipv6_canary_check{check="%s"} %s' % (k, v) for k, v in checks.items()]
    if ts is not None:
        lines += ["# TYPE shc_ipv6_canary_run_timestamp_seconds gauge", "shc_ipv6_canary_run_timestamp_seconds %d" % ts]
    return "\n".join(lines) + "\n"


ALL_OK = dict(address=1, resolver=1, pref64=1, name4=1, literal4=1, name6=1, mtu=1, deny=1)


class ScrapeScript(unittest.TestCase):
    """canary_scrape.yml: the shell parses; the python reads every target from its gateway namespace and VRF."""

    TARGETS = [{"site": "site-a", "gateway": "gw-a", "netns": "gw-gw-a", "placement": "acme/p-a", "vrf": "t51001",
                "address": A1, "error": ""},
               {"site": "site-b", "gateway": "gw-b", "netns": "gw-gw-b", "placement": "acme/p-b", "vrf": "t51002",
                "address": A2, "error": ""},
               {"site": "site-c", "gateway": "gw-c", "netns": "gw-gw-c", "placement": "acme/p-c", "vrf": "t51003",
                "address": "", "error": "no VirtualMachineInstance canary"}]

    def render(self, port=9464):
        t = by_name(load(os.path.join(HF, "canary_scrape.yml")), "{{ hf_canary_bgw.name }}: read the canary metrics through the gateways")
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["to_json"] = json.dumps
        return env.from_string(t["ansible.builtin.shell"]["cmd"]).render(
            hf_canary_read=self.TARGETS, hf_canary_cfg={"port": port})

    def execute(self, answers, port=9464):
        sh = self.render(port)
        subprocess.run(["bash", "-n"], input=sh, text=True, check=True)
        code = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\nPY\s*$", sh, re.S).group(1))
        ns = {"__name__": "canary_under_test"}
        exec(compile(code, "<canary>", "exec"), ns)
        calls = []

        def run(cmd):
            calls.append(cmd)
            key = " ".join(cmd)
            for k, v in answers.items():
                if key.startswith(k) or k in key:
                    return v
            return (1, "", "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ns["main"](run, now=NOW)
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("canary=")][-1]
        return rc, json.loads(line.split("=", 1)[1]), calls

    def src(self, addr=SRC):
        return (0, json.dumps([{"ifname": "t", "addr_info": [{"family": "inet6", "local": addr, "prefixlen": 128}]}]), "")

    def test_healthy_failed_and_the_command(self):
        answers = {
            "ip -n gw-gw-a -j -6 addr show dev t51001": self.src(),
            "ip -n gw-gw-b -j -6 addr show dev t51002": (0, "[]", ""),
            "ip netns exec gw-gw-a": (0, metrics(NOW - 30, **ALL_OK), ""),
            "ip netns exec gw-gw-b": (0, metrics(NOW - 30, **dict(ALL_OK, deny=0, mtu="0")), ""),
        }
        rc, rec, calls = self.execute(answers)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(rec), ["site-a", "site-b"])  # a target without an address is not read
        a, b = rec["site-a"], rec["site-b"]
        self.assertEqual((a["healthy"], a["stale"], a["error"], a["address"], a["retry"]), (True, False, "", A1, False))
        self.assertEqual(a["checks"], ALL_OK)
        self.assertEqual(a["at"], "2027-01-15T07:59:30Z")
        self.assertEqual((b["healthy"], b["stale"]), (False, False))
        self.assertEqual((b["checks"]["deny"], b["checks"]["mtu"], b["checks"]["name4"]), (0, 0, 1))
        curl = [c for c in calls if "curl" in c]
        self.assertEqual(curl[0], ["ip", "netns", "exec", "gw-gw-a", "ip", "vrf", "exec", "t51001", "curl", "-6", "-s",
                                   "-f", "--max-time", "5", "--interface", SRC, "http://[%s]:9464/metrics" % A1])
        # no global address on the VRF: the kernel chooses the source
        self.assertNotIn("--interface", curl[1])
        self.assertEqual(curl[1][-1], "http://[%s]:9464/metrics" % A2)

    def test_stale_unreachable_and_malformed(self):
        answers = {
            "ip netns exec gw-gw-a": (0, metrics(NOW - 400, **ALL_OK), ""),
            "ip netns exec gw-gw-b": (7, "", "curl: (7) Failed to connect\n"),
        }
        _, rec, _ = self.execute(answers)
        self.assertEqual((rec["site-a"]["stale"], rec["site-a"]["healthy"]), (True, False))
        self.assertIn("400 s old", rec["site-a"]["error"])
        self.assertEqual((rec["site-b"]["stale"], rec["site-b"]["healthy"], rec["site-b"]["checks"]), (True, False, {}))
        self.assertIn("unreachable from gw-gw-b VRF t51002 (curl exit 7: curl: (7) Failed to connect)", rec["site-b"]["error"])
        # a probe that did not answer is for another node to try; a stale one answered
        self.assertEqual((rec["site-a"]["retry"], rec["site-b"]["retry"]), (False, True))
        # no timestamp; a page without checks
        answers = {"ip netns exec gw-gw-a": (0, metrics(None, **ALL_OK), ""),
                   "ip netns exec gw-gw-b": (0, metrics(NOW - 5), "")}
        _, rec, _ = self.execute(answers)
        self.assertEqual((rec["site-a"]["stale"], rec["site-a"]["error"]), (True, "the metrics carry no run timestamp"))
        self.assertEqual((rec["site-b"]["stale"], rec["site-b"]["healthy"], rec["site-b"]["error"]),
                         (False, False, "the metrics carry no checks"))

    def test_an_address_that_is_not_ipv6_is_never_requested(self):
        old = self.TARGETS
        try:
            self.TARGETS = [dict(old[0], address="198.51.100.7")]
            _, rec, calls = self.execute({})
        finally:
            self.TARGETS = old
        self.assertIn("not an IPv6 address", rec["site-a"]["error"])
        self.assertFalse(rec["site-a"]["retry"])  # no other node would do better
        self.assertEqual([c for c in calls if "curl" in c], [])

    def test_port(self):
        _, _, calls = self.execute({"ip netns exec": (0, metrics(NOW, **ALL_OK), "")}, port=19464)
        self.assertTrue(all(c[-1].endswith(":19464/metrics") for c in calls if "curl" in c))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricTargets(unittest.TestCase):
    def targets(self, gws, sites):
        t = by_name(load(os.path.join(HF, "canary.yml")), "The sites that report a translation canary")
        return run_play([t, write_out("hf_canary_targets")],
                        {"fab_name": "lab", "_hf_canary_objs": {"results": [{"resources": gws}, {"resources": sites}]}})

    def gw(self, name, site, fabric="lab", deleting=False):
        md = {"name": name, "namespace": "platform"}
        if deleting:
            md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        return {"metadata": md, "spec": {"fabricRef": {"name": fabric}, "cloudRef": {"name": site}}}

    def site(self, name, canary):
        st = {"realization": {"ipv6Canary": canary}} if canary is not None else {}
        return {"metadata": {"name": name}, "status": st}

    def test_no_canary_configured(self):
        out = run_play([{"name": "the file", "ansible.builtin.include_tasks": os.path.join(HF, "canary.yml")},
                        write_out("{'targets': hf_canary_targets, 'scrape': hf_canary_scrape, 'error': hf_canary_error}")],
                       {"fab_name": "lab", "fab_real": {"canaries": {"ipv6Translation": {"network": ""}}},
                        "fab_spec": {"borderGateway": {"nodes": [{"name": "bgw-1"}]}}})
        self.assertEqual(out, {"targets": [], "scrape": {}, "error": ""})

    def test_scrape_on_the_nodes(self):
        """canary_scrape.yml in ansible-playbook, delegated to localhost (as localhost and 127.0.0.1): the gateway
        namespace does not exist there, so the site reads unreachable from both; the first node's reading is kept,
        noting that no node reached the probe, and the site is left failed."""
        targets = [{"site": "site-a", "gateway": "gw-a", "netns": "gw-shc-test-none", "placement": "p/a", "vrf": "t51001",
                    "address": A1, "error": ""}]
        out = run_play([{"name": "initial facts", "ansible.builtin.set_fact": {"hf_canary_node": "", "hf_canary_scrape": {}}},
                        {"name": "the nodes", "ansible.builtin.include_tasks": os.path.join(HF, "canary_scrape.yml"),
                         "loop": [{"name": "localhost"}, {"name": "127.0.0.1"}],
                         "loop_control": {"loop_var": "hf_canary_bgw"}},
                        write_out("{'node': hf_canary_node, 'scrape': hf_canary_scrape, 'retry': hf_canary_retry, "
                                  "'tried': hf_canary_tried}")],
                       {"hf_canary_targets": targets, "hf_canary_cfg": {"port": 9464},
                        "ansible_python_interpreter": sys.executable})
        self.assertEqual(out["node"], "localhost")
        r = out["scrape"]["site-a"]
        self.assertEqual((r["healthy"], r["stale"], r["address"], r["node"]), (False, True, A1, "localhost"))
        self.assertNotIn("retry", r)
        self.assertIn("unreachable from gw-shc-test-none VRF t51001", r["error"])
        self.assertEqual(r["note"], "unreachable from every border gateway node that read it (localhost, 127.0.0.1)")
        self.assertEqual(out["retry"], ["site-a"])
        self.assertEqual([t["node"] for t in out["tried"]["site-a"]], ["localhost", "127.0.0.1"])

    def test_selection(self):
        gws = [self.gw("gw-a", "site-a"), self.gw("gw-b", "site-b"), self.gw("gw-c", "site-c"),
               self.gw("gw-x", "site-x", fabric="other"), self.gw("gw-d", "site-d", deleting=True),
               self.gw("gw-e", "site-e")]
        sites = [self.site("site-a", {"placement": "p/a", "vrf": "t51001", "address": A1}),
                 self.site("site-b", {"placement": "p/b", "error": "no server canary"}),
                 self.site("site-c", None),
                 self.site("site-x", {"placement": "p/x", "vrf": "t1", "address": A2}),
                 self.site("site-d", {"placement": "p/d", "vrf": "t1", "address": A2}),
                 self.site("site-e", {"placement": "p/e", "address": A2})]
        out = {t["site"]: t for t in self.targets(gws, sites)}
        self.assertEqual(sorted(out), ["site-a", "site-b", "site-e"])
        self.assertEqual(out["site-a"], {"site": "site-a", "gateway": "gw-a", "netns": "gw-gw-a", "placement": "p/a",
                                         "vrf": "t51001", "address": A1, "error": ""})
        self.assertEqual((out["site-b"]["address"], out["site-b"]["error"]), ("", "no server canary"))
        # an address without a VRF is not read
        self.assertEqual(out["site-e"]["address"], "")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NodeRetry(unittest.TestCase):
    """canary.yml's node loop over canary_scrape.yml, with the node's script replaced by a stub that answers per node
    (None: the script fails) for the sites the node was asked, then the fabric status: a probe the first node cannot
    reach is read again from the other nodes, in node order; the site fails only when no node reaches it."""

    TARGETS = [{"site": "site-a", "gateway": "gw-a", "netns": "gw-gw-a", "placement": "p/a", "vrf": "t51001",
                "address": A1, "error": ""},
               {"site": "site-b", "gateway": "gw-b", "netns": "gw-gw-b", "placement": "p/b", "vrf": "t51002",
                "address": A2, "error": ""}]

    def ok(self, address=A1, at="2027-01-15T07:59:30Z"):
        return {"address": address, "at": at, "checks": dict(ALL_OK), "stale": False, "healthy": True, "error": "",
                "retry": False}

    def unreachable(self, address=A1, vrf="t51001", netns="gw-gw-a", code=28):
        return {"address": address, "at": "", "checks": {}, "stale": True, "healthy": False, "retry": True,
                "error": "unreachable from %s VRF %s (curl exit %d)" % (netns, vrf, code)}

    def run_nodes(self, answers):
        tasks = load(os.path.join(HF, "canary_scrape.yml"))
        sh = by_name(tasks, "{{ hf_canary_bgw.name }}: read the canary metrics through the gateways")
        sh["ansible.builtin.shell"] = {"cmd": (
            "{% set a = hf_canary_answers[hf_canary_bgw.name] %}"
            "{% if a is none %}exit 1{% else %}printf '%s\\n' {{ ('canary=' ~ (a | dict2items | selectattr('key', 'in', "
            "hf_canary_read | selectattr('address') | map(attribute='site') | list) | items2dict | to_json)) | quote }}"
            "{% endif %}"), "executable": "/bin/bash"}
        sh["delegate_to"] = "localhost"
        canary = load(os.path.join(HF, "canary.yml"))
        play = [by_name(canary, n) for n in ("The translation canary's realization",
                                             "Read the canaries from a border gateway node",
                                             "Note that no border gateway node read the canaries")]
        play.append(by_name(load(os.path.join(HF, "main.yml")), "Set the translation canary status"))
        return run_play(play + [write_out("{'node': hf_canary_node, 'scrape': hf_canary_scrape, 'error': hf_canary_error, "
                                          "'retry': hf_canary_retry, 'status': hf_canary_status, "
                                          "'conditions': hf_canary_conditions}")],
                        {"fab_real": {"canaries": {"ipv6Translation": {"network": "acme/canary", "alertMinutes": 5}}},
                         "fab_spec": {"borderGateway": {"nodes": [{"name": n} for n in answers]}},
                         "fab_st": {}, "hf_canary_targets": self.TARGETS, "hf_canary_answers": answers},
                        files={"canary_scrape.yml": tasks})

    def test_first_node_cannot_reach_the_second_reads(self):
        out = self.run_nodes({"bgw-1": {"site-a": self.unreachable(), "site-b": self.ok(A2)},
                              # asked only for site-a: its site-b answer must not replace bgw-1's reading
                              "bgw-2": {"site-a": self.ok(), "site-b": self.ok(A2, at="2027-01-15T07:00:00Z")}})
        self.assertEqual((out["node"], out["error"], out["retry"]), ("bgw-1", "", []))
        a, b = out["status"]["site-a"], out["status"]["site-b"]
        self.assertEqual((a["healthy"], a["stale"], a["error"], a["node"], a["failedSince"]), (True, False, "", "bgw-2", ""))
        self.assertEqual(a["note"], "bgw-1: unreachable from gw-gw-a VRF t51001 (curl exit 28); read from bgw-2")
        self.assertNotIn("retry", a)
        self.assertEqual((b["node"], b["note"], b["at"]), ("bgw-1", "", "2027-01-15T07:59:30Z"))
        self.assertEqual((out["conditions"][0]["status"], out["conditions"][0]["reason"]), ("True", "Ready"))

    def test_the_third_node_reads_after_two_could_not(self):
        out = self.run_nodes({"bgw-1": {"site-a": self.unreachable(), "site-b": self.ok(A2)},
                              "bgw-2": {"site-a": self.unreachable(code=7)},
                              "bgw-3": {"site-a": self.ok()}})
        a = out["status"]["site-a"]
        self.assertEqual((a["healthy"], a["node"]), (True, "bgw-3"))
        self.assertEqual(a["note"], "bgw-1: unreachable from gw-gw-a VRF t51001 (curl exit 28); "
                                    "bgw-2: unreachable from gw-gw-a VRF t51001 (curl exit 7); read from bgw-3")

    def test_no_node_reaches_the_probe(self):
        out = self.run_nodes({"bgw-1": {"site-a": self.unreachable(), "site-b": self.ok(A2)},
                              "bgw-2": {"site-a": self.unreachable(code=7), "site-b": self.ok(A2)}})
        a = out["status"]["site-a"]
        # the first node's reading, failed, with every node that tried
        self.assertEqual((a["healthy"], a["stale"], a["node"]), (False, True, "bgw-1"))
        self.assertEqual(a["error"], "unreachable from gw-gw-a VRF t51001 (curl exit 28)")
        self.assertEqual(a["note"], "unreachable from every border gateway node that read it (bgw-1, bgw-2)")
        self.assertTrue(a["failedSince"])
        self.assertEqual(out["retry"], ["site-a"])
        self.assertEqual(out["status"]["site-b"]["healthy"], True)
        c = out["conditions"][0]
        self.assertEqual((c["status"], c["reason"]), ("False", "CanaryFailed"))
        self.assertIn("site-a (unreachable from gw-gw-a VRF t51001 (curl exit 28)", c["message"])

    def test_a_node_whose_script_fails(self):
        """As before: a node whose script fails reads nothing and the next node reads every site; once every site is
        read, no further node is asked (bgw-3's answers would replace the readings)."""
        out = self.run_nodes({"bgw-1": None, "bgw-2": {"site-a": self.ok(), "site-b": self.ok(A2)},
                              "bgw-3": {"site-a": self.unreachable(), "site-b": self.unreachable(A2)}})
        self.assertEqual((out["node"], out["error"], out["retry"]), ("bgw-2", "", []))
        for site in ("site-a", "site-b"):
            r = out["status"][site]
            self.assertEqual((r["healthy"], r["node"], r["note"]), (True, "bgw-2", ""))
        # a node that fails after the first read leaves the unreached site to the next node
        out = self.run_nodes({"bgw-1": {"site-a": self.unreachable(), "site-b": self.ok(A2)}, "bgw-2": None,
                              "bgw-3": {"site-a": self.ok()}})
        a = out["status"]["site-a"]
        self.assertEqual((a["healthy"], a["node"]), (True, "bgw-3"))
        self.assertEqual(a["note"], "bgw-1: unreachable from gw-gw-a VRF t51001 (curl exit 28); read from bgw-3")
        # no node's script runs: nothing read, as before
        out = self.run_nodes({"bgw-1": None, "bgw-2": None})
        self.assertEqual((out["node"], out["error"]), ("", "no border gateway node could read the canaries"))
        self.assertEqual(out["status"]["site-a"]["error"], "no border gateway node could read the canaries")
        self.assertEqual((out["status"]["site-a"]["node"], out["status"]["site-a"]["note"]), ("", ""))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricStatus(unittest.TestCase):
    TARGETS = [{"site": "site-a", "placement": "p/a", "vrf": "t51001", "address": A1, "error": ""},
               {"site": "site-b", "placement": "p/b", "vrf": "t51002", "address": A2, "error": ""}]

    def ok(self, address=A1):
        return {"address": address, "at": "2027-01-15T07:59:30Z", "checks": dict(ALL_OK), "stale": False,
                "healthy": True, "error": ""}

    def bad(self, address=A2, **checks):
        return {"address": address, "at": "2027-01-15T07:59:30Z", "checks": dict(ALL_OK, **checks), "stale": False,
                "healthy": False, "error": ""}

    def status(self, scrape, prev=None, targets=None, minutes=5, network="acme/canary", error="", now=None):
        t = by_name(load(os.path.join(HF, "main.yml")), "Set the translation canary status")
        return run_play([t, write_out("{'status': hf_canary_status, 'alert': hf_canary_alert_sites, 'patch': hf_canary_patch, "
                                      "'conditions': hf_canary_conditions, 'summary': hf_canary_summary}")],
                        {"hf_canary_cfg": {"network": network, "alertMinutes": minutes},
                         "hf_canary_targets": self.TARGETS if targets is None else targets,
                         "hf_canary_scrape": scrape, "hf_canary_error": error,
                         **({"hf_canary_now": now} if now else {}),
                         "fab_st": {"realization": {"translation": {"canaries": prev or {}}}}})

    def test_healthy(self):
        out = self.status({"site-a": self.ok(), "site-b": self.ok(A2)})
        self.assertEqual(out["status"]["site-a"], dict(self.ok(), placement="p/a", failedSince="", node="", note=""))
        # the node that read it and the note are kept as read
        out = self.status({"site-a": dict(self.ok(), node="bgw-2", note="bgw-1: unreachable; read from bgw-2"),
                           "site-b": self.ok(A2)})
        self.assertEqual((out["status"]["site-a"]["node"], out["status"]["site-a"]["note"]),
                         ("bgw-2", "bgw-1: unreachable; read from bgw-2"))
        self.assertEqual(out["conditions"][0]["status"], "True")
        self.assertEqual(out["alert"], [])
        self.assertEqual(out["conditions"], [{"type": "IPv6Translation", "status": "True", "reason": "Ready",
                                              "message": "the translation canary passes every check on site-a, site-b"}])

    def test_failed_keeps_since_and_alerts_after_the_minutes(self):
        # the run's time pinned: without hf_canary_now the fallback lookup is evaluated per use and can cross a second
        out = self.status({"site-a": self.ok(), "site-b": self.bad(deny=0, resolver=0)}, now="2027-01-15T08:00:00Z")
        b = out["status"]["site-b"]
        self.assertEqual(b["failedSince"], "2027-01-15T08:00:00Z")
        self.assertEqual(out["alert"], [])
        c = out["conditions"][0]
        self.assertEqual((c["type"], c["status"], c["reason"]), ("IPv6Translation", "False", "CanaryFailed"))
        self.assertIn("site-b (failed deny, resolver, since %s)" % b["failedSince"], c["message"])
        self.assertNotIn("site-a", c["message"])
        prev = {"site-b": dict(b, failedSince="2026-01-01T00:00:00Z")}
        out2 = self.status({"site-a": self.ok(), "site-b": self.bad(deny=0)}, prev=prev)
        self.assertEqual(out2["status"]["site-b"]["failedSince"], "2026-01-01T00:00:00Z")
        self.assertEqual(out2["alert"], ["site-b"])
        self.assertEqual(out2["summary"], "site-b (failed deny, since 2026-01-01T00:00:00Z)")
        # recovered: since cleared, no alert, condition True
        out3 = self.status({"site-a": self.ok(), "site-b": self.ok(A2)}, prev=prev)
        self.assertEqual((out3["status"]["site-b"]["failedSince"], out3["alert"]), ("", []))
        self.assertEqual(out3["conditions"][0]["status"], "True")

    def test_the_runs_time_is_used(self):
        prev = {"site-b": dict(self.bad(), failedSince="2026-05-31T23:56:00Z")}
        out = self.status({"site-a": self.bad(A1, name4=0), "site-b": self.bad(mtu=0)}, prev=prev, now="2026-06-01T00:00:00Z")
        self.assertEqual(out["status"]["site-a"]["failedSince"], "2026-06-01T00:00:00Z")
        self.assertEqual(out["alert"], [])  # site-b: 4 of 5 minutes
        out = self.status({"site-a": self.bad(A1, name4=0), "site-b": self.bad(mtu=0)}, prev=prev, now="2026-06-01T00:01:00Z")
        self.assertEqual(out["alert"], ["site-b"])

    def test_alert_at_once_with_zero_minutes(self):
        out = self.status({"site-a": self.ok(), "site-b": self.bad(mtu=0)}, minutes=0)
        self.assertEqual(out["alert"], ["site-b"])

    def test_not_read_and_no_address(self):
        targets = self.TARGETS + [{"site": "site-c", "placement": "p/c", "vrf": "", "address": "",
                                   "error": "no VirtualMachineInstance canary in the site namespaces ns-1"}]
        out = self.status({}, targets=targets, error="no border gateway node could read the canaries")
        for site in ("site-a", "site-b"):
            r = out["status"][site]
            self.assertEqual((r["healthy"], r["stale"], r["error"]), (False, True, "no border gateway node could read the canaries"))
            # written empty, so a merge-patch clears an earlier read's node and note
            self.assertEqual((r["node"], r["note"]), ("", ""))
        self.assertEqual(out["status"]["site-c"]["error"], "no VirtualMachineInstance canary in the site namespaces ns-1")
        self.assertEqual(out["status"]["site-c"]["address"], "")
        self.assertIn("site-c (no VirtualMachineInstance canary", out["conditions"][0]["message"])

    def test_off_and_nothing_reported(self):
        out = self.status({"site-a": self.ok()}, network="")
        self.assertEqual((out["status"], out["conditions"], out["alert"], out["patch"]), ({}, [], [], {}))
        out = self.status({}, targets=[])
        self.assertEqual((out["status"], out["conditions"]), ({}, []))

    def test_patch_removes_sites_and_checks_that_went_away(self):
        prev = {"site-a": dict(self.ok(), checks=dict(ALL_OK, gateway_ptr=1), failedSince=""),
                "site-gone": dict(self.ok(), failedSince="")}
        out = self.status({"site-a": self.ok(), "site-b": self.ok(A2)}, prev=prev)
        self.assertIsNone(out["patch"]["site-gone"])
        self.assertIsNone(out["patch"]["site-a"]["checks"]["gateway_ptr"])
        self.assertEqual(out["patch"]["site-a"]["checks"]["deny"], 1)
        self.assertNotIn("gateway_ptr", out["status"]["site-a"]["checks"])

    def test_event_and_write(self):
        tasks = load(os.path.join(HF, "main.yml"))
        ev = by_name(tasks, "Report the translation canary failure as an event")
        self.assertEqual(ev["kubernetes.core.k8s"]["definition"]["reason"], "IPv6TranslationCanaryFailed")
        self.assertEqual(ev["kubernetes.core.k8s"]["definition"]["type"], "Warning")
        self.assertEqual(ev["kubernetes.core.k8s"]["definition"]["involvedObject"]["kind"], "HybridFabric")
        self.assertFalse(ev["failed_when"])
        self.assertEqual(ev["when"], "hf_canary_alert_sites | length > 0")
        fin = by_name(tasks, "Report the fabric ready")["vars"]
        self.assertIn("+ hf_canary_conditions | default([])", fin["fab_conditions"])
        self.assertIn("'translation': {'canaries': hf_canary_patch | default({}),", fin["fab_status"])
        # with the read's time and pacing, which the operator uses to re-read and to mark a too-old reading stale
        self.assertIn("'readAt': hf_canary_now | default('', true)", fin["fab_status"])
        # the canary is read inside the node block, while the SSH key is there
        block = by_name(tasks, "Configure the nodes, one at a time")
        self.assertEqual([t["name"] for t in block["block"]],
                         ["Configure the root instance on each node", "Read the translation canaries"])
        self.assertEqual(by_name(block["block"], "Read the translation canaries")["ansible.builtin.include_tasks"], "canary.yml")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SiteLookup(unittest.TestCase):
    """ipv6_canary.yml: the placement of the canary network on this site and the probe's address."""

    def tasks(self, *names):
        tasks = load(os.path.join(CI, "ipv6_canary.yml"))
        return [by_name(tasks, n) for n in names]

    def pl(self, name, site, network="ipv6-canary", kind="CloudVirt", deleting=False, vrf="t51001", namespaces=None,
           site_status=None):
        md = {"namespace": "entity-platform", "name": name}
        if deleting:
            md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        st = {"site": site, "realization": {"vrf": vrf}}
        if namespaces is not None:
            st["namespaces"] = namespaces
        if site_status is not None:
            st["realization"]["site"] = site_status
        return {"metadata": md, "spec": {"network": network, "backend": {"kind": kind, "name": "env"}}, "status": st}

    def vmi(self, ns, addrs):
        return {"metadata": {"namespace": ns, "name": "canary"},
                "status": {"interfaces": [{"name": "default", "ipAddresses": addrs}]}}

    BASE = {"ci_canary_network": "entity-platform/ipv6-canary", "ci_canary_workload": "canary",
            "cloudinfrastructure_cr_name": "site-a"}
    # the facts the tasks overwrite start as set_fact (extra vars would take precedence over set_fact)
    INIT = {"name": "initial facts", "ansible.builtin.set_fact": {"ci_canary": None, "ci_canary_base": {"placement": ""},
                                                                  "ci_canary_namespaces": [], "ci_canary_placement": {}}}

    def find(self, placements, kind="CloudVirt", vmis=None):
        ts = self.tasks("Find the canary network's placement on this site",
                        "Name the placement, its gateway VRF and its site namespaces")
        if kind == "CloudVirt" and vmis is not None:
            ts += self.tasks("The probe's address (CloudVirt)")
        return run_play([self.INIT] + ts + [write_out("{'placement': ci_canary_placement, 'base': ci_canary_base, "
                                        "'namespaces': ci_canary_namespaces, 'canary': ci_canary | default(none)}")],
                        dict(self.BASE, ci_canary_kind=kind,
                             _ci_canary_placements={"resources": placements},
                             _ci_canary_vmis={"results": vmis or []}))

    def test_placement_choice(self):
        pls = [self.pl("z-other-site", "site-b"), self.pl("y-other-net", "site-a", network="other"),
               self.pl("x-gone", "site-a", deleting=True), self.pl("w-oso", "site-a", kind="CloudOSO"),
               self.pl("v-mine", "site-a", namespaces=["ns-probes"])]
        out = self.find(pls)
        self.assertEqual(out["placement"]["metadata"]["name"], "v-mine")
        self.assertEqual(out["base"], {"placement": "entity-platform/v-mine", "vrf": "t51001"})
        self.assertEqual(out["namespaces"], ["ns-probes"])
        self.assertEqual(self.find(pls[:4])["placement"], {})

    def test_cloudvirt_address(self):
        pls = [self.pl("p", "site-a", namespaces=["ns-1", "ns-2"])]
        vmis = [{"item": "ns-1", "resources": []},
                {"item": "ns-2", "resources": [self.vmi("ns-2", ["fe80::1", "fd99::5", A1 + "/64", "2001:db8::99"])]}]
        out = self.find(pls, vmis=vmis)
        self.assertEqual(out["canary"], {"placement": "entity-platform/p", "vrf": "t51001", "address": A1,
                                         "workload": "ns-2/canary"})

    def test_cloudvirt_errors(self):
        pls = [self.pl("p", "site-a", namespaces=["ns-1"])]
        out = self.find(pls, vmis=[{"item": "ns-1", "resources": []}])
        self.assertEqual(out["canary"]["error"], "no VirtualMachineInstance canary in the site namespaces ns-1")
        self.assertNotIn("address", out["canary"])
        out = self.find(pls, vmis=[{"item": "ns-1", "resources": [self.vmi("ns-1", ["fe80::1", "fd00::1"])]}])
        self.assertEqual(out["canary"], {"placement": "entity-platform/p", "vrf": "t51001", "workload": "ns-1/canary",
                                         "error": "VirtualMachineInstance ns-1/canary reports no global IPv6 address"})
        out = self.find(pls, vmis=[{"item": "ns-1", "failed": True, "msg": "forbidden"}])
        self.assertIn("(not readable: ns-1)", out["canary"]["error"])

    def test_cloudoso_address(self):
        t = self.tasks("The probe's address (CloudOSO)")[0]
        t = {k: v for k, v in t.items() if k != "when"}
        pl = self.pl("p", "site-a", kind="CloudOSO", site_status={"networkId": "net-1", "sharedTo": "proj-1"})
        base = {"placement": "entity-platform/p", "vrf": "t51001"}

        def run(result):
            return run_play([t, write_out("ci_canary")], dict(self.BASE, ci_canary_base=base, ci_canary_placement=pl,
                                                              os_result=result))
        self.assertEqual(run({"found": True, "serverId": "srv-1", "address": A1}),
                         dict(base, address=A1, workload="srv-1"))
        self.assertEqual(run({"found": False})["error"], "no server canary in the environment project proj-1")
        self.assertIn("(srv-1) has no global IPv6 address on the placement network net-1",
                      run({"found": True, "serverId": "srv-1", "address": ""})["error"])

    def test_realization_and_previous(self):
        t = self.tasks("The translation canary's realization")[0]
        defaults = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))
        cfg = defaults["fabric_realization_defaults"]["canaries"]["ipv6Translation"]
        self.assertEqual(cfg, {"network": "", "workload": "", "port": 9464, "alertMinutes": 5})
        out = run_play([t, write_out("{'n': ci_canary_network, 'w': ci_canary_workload, 'prev': ci_canary_prev, "
                                     "'kind': ci_canary_kind}")],
                       {"fabric_realization_defaults": defaults["fabric_realization_defaults"], "ci_cr_type": "openstack",
                        "fabric_realization": {"canaries": {"ipv6Translation": {"network": "ns/net", "workload": "canary"}}},
                        "cloudinfrastructure_cr_lookup": {"resources": [{"status": {"realization": {"ipv6Canary": {"placement": "ns/p"}}}}]}})
        self.assertEqual(out, {"n": "ns/net", "w": "canary", "prev": {"placement": "ns/p"}, "kind": "CloudOSO"})

    def test_no_canary_configured_writes_nothing(self):
        """The whole file with the defaults: nothing is looked up and nothing is written."""
        tasks = load(os.path.join(CI, "ipv6_canary.yml"))
        rec = by_name(tasks, "Record the translation canary of this site")
        rec["ansible.builtin.debug"] = {"msg": "{{ rec_status }}"}  # the status module stands in as a debug
        rec["vars"] = dict(rec.get("vars") or {}, rec_status=rec.pop("hybridsovereign.shc.cr_status")["status"])
        out = run_play(tasks + [write_out("{'canary': ci_canary, 'write': _ci_canary_write is skipped}")],
                       {"role_path": os.path.join(ROLES, "cloudinfrastructure_provision"), "ci_cr_type": "openshift",
                        "cloudinfrastructure_cr_name": "site-a", "cloudinfrastructure_cr_namespace": "platform",
                        "ci_cr_spec": {}, "cloudinfrastructure_cr_lookup": {"resources": [{"status": {}}]}})
        self.assertEqual(out, {"canary": None, "write": True})

    def test_a_failed_lookup_is_recorded_not_raised(self):
        """The whole file with a canary configured and the services API unreachable: the error is recorded."""
        tasks = load(os.path.join(CI, "ipv6_canary.yml"))
        rec = by_name(tasks, "Record the translation canary of this site")
        rec["ansible.builtin.debug"] = {"msg": "{{ rec_status }}"}
        rec["vars"] = dict(rec.get("vars") or {}, rec_status=rec.pop("hybridsovereign.shc.cr_status")["status"])
        out = run_play(tasks + [write_out("{'canary': ci_canary, 'write': _ci_canary_write is skipped}")],
                       {"role_path": os.path.join(ROLES, "cloudinfrastructure_provision"), "ci_cr_type": "openshift",
                        "cloudinfrastructure_cr_name": "site-a", "cloudinfrastructure_cr_namespace": "platform",
                        "services_api_host": "https://127.0.0.1:9", "services_bearer_token": "x",
                        "fabric_realization": {"canaries": {"ipv6Translation": {"network": "entity-platform/ipv6-canary",
                                                                                "workload": "canary"}}},
                        "ci_cr_spec": {}, "cloudinfrastructure_cr_lookup": {"resources": [{"status": {}}]}})
        self.assertEqual(out["canary"]["placement"], "")
        self.assertTrue(out["canary"]["error"])
        self.assertFalse(out["write"])
        # a malformed realization is an error too
        tasks = load(os.path.join(CI, "ipv6_canary.yml"))
        rec = by_name(tasks, "Record the translation canary of this site")
        rec["ansible.builtin.debug"] = {"msg": "x"}
        rec.pop("hybridsovereign.shc.cr_status")
        out = run_play(tasks + [write_out("ci_canary")],
                       {"role_path": os.path.join(ROLES, "cloudinfrastructure_provision"), "ci_cr_type": "openstack",
                        "cloudinfrastructure_cr_name": "site-a", "cloudinfrastructure_cr_namespace": "platform",
                        "fabric_realization": {"canaries": {"ipv6Translation": {"network": "no-slash", "workload": "canary"}}},
                        "ci_cr_spec": {}, "cloudinfrastructure_cr_lookup": {"resources": [{"status": {}}]}})
        self.assertIn('network must be "<namespace>/<name>"', out["error"])

    def test_wiring(self):
        main = load(os.path.join(CI, "main.yml"))
        inc = by_name(main, "Find the translation canary's probe on this site")
        self.assertEqual(inc["ansible.builtin.include_tasks"], "ipv6_canary.yml")
        rec = self.tasks("Record the translation canary of this site")[0]
        self.assertTrue(rec["hybridsovereign.shc.cr_status"]["partial"])
        self.assertFalse(rec["failed_when"])
        self.assertEqual(rec["when"], "_rec != _prev")
        blk = self.tasks("Find the translation canary on this site")[0]
        self.assertEqual([t["name"] for t in blk["rescue"]], ["Record why the translation canary was not found"])


class FakeCompute:
    def __init__(self, servers):
        self.list = servers
        self.queries = []

    def servers(self, **f):
        self.queries.append(f)
        # Nova's name filter is a regular expression (a substring matches)
        return [s for s in self.list if s.project_id == f.get("project_id") and f.get("name", "") in s.name]


class FakeNetwork:
    def __init__(self, ports):
        self.list = ports

    def ports(self, **f):
        return [p for p in self.list if all(getattr(p, k) == v for k, v in f.items())]


def obj(**kw):
    return types.SimpleNamespace(**kw)


class ServerAddress(unittest.TestCase):
    def call(self, servers, ports, **over):
        conn = obj(compute=FakeCompute(servers), network=FakeNetwork(ports))
        acct = mock.Mock()
        acct.own_scope.return_value = conn
        req = {"action": "server-address", "clouds_yaml": "/dev/null", "name": "canary", "projectId": "proj-1",
               "networkId": "net-1"}
        req.update(over)
        with mock.patch.object(S, "Account", return_value=acct):
            return S.ACTIONS["server-address"](req), conn

    def test_found(self):
        servers = [obj(id="srv-2", name="canary", project_id="proj-1", status="ACTIVE"),
                   obj(id="srv-9", name="canary-copy", project_id="proj-1", status="ACTIVE"),
                   obj(id="srv-1", name="canary", project_id="proj-other", status="ACTIVE")]
        ports = [obj(device_id="srv-2", network_id="net-1", fixed_ips=[
                     {"ip_address": "198.51.100.5"}, {"ip_address": "fe80::5"}, {"ip_address": "fd00::5"},
                     {"ip_address": A1}]),
                 obj(device_id="srv-2", network_id="net-2", fixed_ips=[{"ip_address": A2}])]
        out, conn = self.call(servers, ports)
        self.assertEqual(out, {"found": True, "name": "canary", "projectId": "proj-1", "serverId": "srv-2", "count": 1,
                               "status": "ACTIVE", "addresses": ["fe80::5", "fd00::5", A1], "address": A1})
        self.assertTrue(conn.compute.queries[0]["all_projects"])

    def test_not_found_and_no_global_address(self):
        out, _ = self.call([], [])
        self.assertEqual((out["found"], out["address"]), (False, ""))
        servers = [obj(id="srv-2", name="canary", project_id="proj-1", status="ACTIVE")]
        out, _ = self.call(servers, [obj(device_id="srv-2", network_id="net-1", fixed_ips=[{"ip_address": "fe80::5"}])])
        self.assertEqual((out["found"], out["address"], out["addresses"]), (True, "", ["fe80::5"]))

    def test_global6(self):
        self.assertTrue(S._global6(A1))
        for a in ("fe80::1", "fd12::1", "fc00::1", "ff02::1", "::1", "::", "198.51.100.1"):
            self.assertFalse(S._global6(a), a)

    def test_read_only_call(self):
        task = load(os.path.join(ROOT, "eda", "common", "tasks", "openstack_call.yml"))[0]
        self.assertIn("'server-address'", task["changed_when"])


class Sample(unittest.TestCase):
    PLACEHOLDER = re.compile(r"__[A-Z0-9]+(?:_[A-Z0-9]+)*__")

    def text(self, name):
        with open(os.path.join(SAMPLE, name)) as f:
            return f.read()

    def user_data(self):
        text = self.text("probe-user-data.yaml")
        self.assertTrue(text.startswith("#cloud-config\n"))
        return yaml.safe_load(text)

    def test_user_data(self):
        ud = self.user_data()
        self.assertEqual(sorted(ud["packages"]), sorted(["clatd", "python3", "bind-utils", "iputils", "curl", "qemu-guest-agent"]))
        files = {f["path"]: f["content"] for f in ud["write_files"]}
        self.assertEqual(files["/etc/clatd.conf"], "plat-prefix=__PREF64__\n")
        self.assertIn("ExecStart=/usr/bin/python3 -m http.server __PORT__ --bind :: --directory /var/lib/shc-ipv6-canary",
                      files["/etc/systemd/system/shc-ipv6-canary-http.service"])
        self.assertIn("OnUnitActiveSec=60s", files["/etc/systemd/system/shc-ipv6-canary.timer"])
        script = files["/usr/local/bin/shc-ipv6-canary"]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        self.assertNotIn("set -u", script)
        names = re.findall(r"^\s*result (\w+) check_\1$", script, re.M)
        self.assertEqual(names, ["address", "resolver", "pref64", "name4", "literal4", "name6", "mtu", "deny"])
        self.assertIn("ping -6 -M do -c 2 -W 3 -s 1352", script)
        self.assertIn('mv -f "$tmp" "$DIR/metrics"', script)

    def test_check_script_output_parses_as_the_fabric_reads_it(self):
        """The script, with its network commands replaced by stubs, writes metrics the scrape script parses."""
        page = self.run_check_script("2001:db8::54")
        checks = dict(re.findall(r'^shc_ipv6_canary_check\{check="(\w+)"\} ([01])$', page, re.M))
        self.assertEqual(checks, {k: "1" for k in ALL_OK})
        self.assertRegex(page, r"(?m)^shc_ipv6_canary_run_timestamp_seconds \d+$")
        # the resolver in use: an information metric, not a check (the fabric reads shc_ipv6_canary_check lines only)
        self.assertIn('shc_ipv6_canary_resolver_info{server="2001:db8::54",translation="1"} 1', page.splitlines())

    def test_the_resolver_in_use_after_the_guest_left_the_translation_resolvers(self):
        page = self.run_check_script("2001:db8:ffff::3331%2")
        self.assertIn('shc_ipv6_canary_resolver_info{server="2001:db8:ffff::3331",translation="0"} 1', page.splitlines())

    def run_check_script(self, current_resolver):
        """The check script with ip, dig, curl, ping and resolvectl stubbed; returns the metrics page it wrote."""
        files = {f["path"]: f["content"] for f in self.user_data()["write_files"]}
        script = files["/usr/local/bin/shc-ipv6-canary"].replace("DIR=/var/lib/shc-ipv6-canary", 'DIR="$CANARY_DIR"')
        import tempfile
        tmp = tempfile.mkdtemp(prefix="shc-canary-")
        try:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            stubs = {"ip": "echo '2: eth0    inet6 2001:db8:4005:4200::10/64 scope global dynamic'",
                     "dig": 'case "$*" in *AAAA*) echo 2001:db8:66::c000:aa;; *) echo 192.0.0.170;; esac',
                     "curl": 'case "$*" in *2001:db8:66::c633:6407*) exit 7;; *-w*) echo 200;; esac; exit 0',
                     "ping": "exit 0",
                     "resolvectl": "printf 'Link 2 (eth0)\\nCurrent DNS Server: %s\\n' '" + current_resolver + "'"}
            for name, body in stubs.items():
                with open(os.path.join(bindir, name), "w") as f:
                    f.write("#!/bin/bash\n" + body + "\n")
                os.chmod(os.path.join(bindir, name), 0o755)
            script = (script.replace("__PREF64__", "2001:db8:66::/96").replace("__RESOLVERS__", "2001:db8::53, 2001:db8::54")
                      .replace("__DENIED_IPV4__", "198.51.100.7"))
            path = os.path.join(tmp, "probe")
            with open(path, "w") as f:
                f.write(script)
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], CANARY_DIR=os.path.join(tmp, "out"))
            subprocess.run(["bash", path], env=env, check=True, timeout=60)
            with open(os.path.join(tmp, "out", "metrics")) as f:
                page = f.read()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        return page

    NM = "\n".join([
        'case "$*" in',
        '  "-g RUNNING general") echo running ;;',
        '  "-g GENERAL.CON-UUID device show enp1s0") echo 5f2b0c9e-0000-4000-8000-000000000001 ;;',
        '  "-g ipv4.dns,ipv6.dns connection show 5f2b0c9e-0000-4000-8000-000000000001") printf "\\n%s\\n" "$DNS" ;;',
        "esac"])

    def run_ignore_auto_dns(self, nmcli, networkctl="exit 1", network_file=None):
        """The user data's ignore-auto-dns script with ip, sleep, nmcli and networkctl stubbed; returns its output, the
        stubs' calls and the drop-ins it wrote under a stand-in for /etc/systemd/network."""
        script = {f["path"]: f["content"] for f in self.user_data()["write_files"]}["/usr/local/sbin/ignore-auto-dns"]
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="shc-dns-")
        try:
            bindir, etc, log = os.path.join(tmp, "bin"), os.path.join(tmp, "etc-network"), os.path.join(tmp, "calls")
            netfile = os.path.join(tmp, "10-netplan-enp1s0.network")
            os.makedirs(bindir)
            stubs = {"ip": "echo 'default via fe80::1 dev enp1s0 proto ra metric 100 expires 1798sec pref medium'",
                     "sleep": "exit 0", "nmcli": nmcli, "networkctl": networkctl}
            for name, body in stubs.items():
                with open(os.path.join(bindir, name), "w") as f:
                    f.write('#!/bin/bash\necho "%s $*" >> "$CALLS"\n%s\n' % (name, body))
                os.chmod(os.path.join(bindir, name), 0o755)
            if network_file is not None:
                with open(netfile, "w") as f:
                    f.write(network_file)
            path = os.path.join(tmp, "ignore-auto-dns")
            with open(path, "w") as f:
                f.write(script.replace("/etc/systemd/network", etc))
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], CALLS=log, NETFILE=netfile)
            out = subprocess.run(["bash", path], env=env, check=True, timeout=60, capture_output=True, text=True).stdout
            calls = []
            if os.path.exists(log):
                with open(log) as f:
                    calls = f.read().splitlines()
            drops = {}
            for root, _, names in os.walk(etc):
                for n in names:
                    with open(os.path.join(root, n)) as f:
                        drops[os.path.relpath(os.path.join(root, n), etc)] = f.read()
            return out, calls, drops
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ignore_auto_dns_networkmanager(self):
        """NetworkManager: the connection on the IPv6 default route's device ignores the resolvers DHCP hands out (both
        families: the canary's placement is IPv6-only) and is re-applied; one without resolvers of its own is left."""
        _, calls, drops = self.run_ignore_auto_dns("DNS='2001:db8::53,2001:db8::54'\n" + self.NM)
        self.assertIn("nmcli connection modify 5f2b0c9e-0000-4000-8000-000000000001 ipv6.ignore-auto-dns yes "
                      "ipv4.ignore-auto-dns yes", calls)
        self.assertEqual(calls[-1], "nmcli device reapply enp1s0")
        self.assertEqual(drops, {})
        out, calls, _ = self.run_ignore_auto_dns("DNS=''\n" + self.NM)
        self.assertFalse([c for c in calls if " modify " in c or "reapply" in c])
        self.assertIn("enp1s0: no resolvers configured: nothing changed", out)

    def test_ignore_auto_dns_networkd(self):
        """systemd-networkd (NetworkManager not running): a drop-in for the device's .network file turns the DHCP and
        router advertisement resolvers off, then networkd reloads and reconfigures the device."""
        networkctl = 'case "$1" in status) echo "       Network File: $NETFILE" ;; esac'
        _, calls, drops = self.run_ignore_auto_dns("exit 0", networkctl, "[Network]\nDNS=2001:db8::53\n")
        self.assertEqual(drops, {"10-netplan-enp1s0.network.d/90-ignore-auto-dns.conf":
                                 "[DHCPv6]\nUseDNS=false\n\n[IPv6AcceptRA]\nUseDNS=false\n\n[DHCPv4]\nUseDNS=false\n"})
        self.assertEqual(calls[-2:], ["networkctl reload", "networkctl reconfigure enp1s0"])
        # a .network file without resolvers of its own: nothing written
        _, _, drops = self.run_ignore_auto_dns("exit 0", networkctl, "[Network]\nDHCP=ipv6\n")
        self.assertEqual(drops, {})

    def test_ignore_auto_dns_unit(self):
        ud = self.user_data()
        files = {f["path"]: f["content"] for f in ud["write_files"]}
        unit = files["/etc/systemd/system/ignore-auto-dns.service"]
        for line in ("ExecStart=/usr/local/sbin/ignore-auto-dns", "After=network-online.target", "WantedBy=multi-user.target"):
            self.assertIn(line, unit.splitlines())
        subprocess.run(["bash", "-n"], input=files["/usr/local/sbin/ignore-auto-dns"], text=True, check=True)
        # enabled (and run) right after the units are loaded, before clatd and the checks start
        self.assertEqual(ud["runcmd"][:2], [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "ignore-auto-dns.service"]])

    def test_vm(self):
        vm = yaml.safe_load(self.text("vm-cloudvirt.yaml"))
        spec = vm["spec"]["template"]["spec"]
        self.assertEqual(spec["domain"]["devices"]["interfaces"][0]["binding"], {"name": "l2bridge"})
        ci = next(v for v in spec["volumes"] if v["name"] == "cloudinit")["cloudInitNoCloud"]
        self.assertEqual(ci["secretRef"], {"name": "__WORKLOAD__-user-data"})
        nd = yaml.safe_load(ci["networkData"])
        eth = nd["ethernets"]["primary"]
        self.assertEqual((nd["version"], eth["dhcp4"], eth["dhcp6"], eth["accept-ra"]), (2, False, True, True))
        self.assertEqual(eth["match"], {"macaddress": "__MAC__"})
        self.assertEqual(eth["nameservers"]["addresses"], ["__RESOLVERS__"])

    def test_objects(self):
        docs = [d for d in yaml.safe_load_all(self.text("canary-objects.yaml")) if d]
        net = next(d for d in docs if d["kind"] == "HybridNetwork")
        self.assertEqual(net["spec"]["platformPrefixes"], [{"family": 6}])
        self.assertNotIn("prefixes", net["spec"])
        self.assertTrue(net["spec"]["egress"]["internet"])
        for p in (d for d in docs if d["kind"] == "NetworkPlacement"):
            self.assertNotIn("ipv4", p["spec"])
            self.assertEqual(p["spec"]["ipv6"], {"prefixLength": 64})

    def test_placeholders_are_documented(self):
        readme = self.text("README.md")
        table = readme[readme.index("## Placeholders"):readme.index("## The checks")]
        documented = set(self.PLACEHOLDER.findall(table))
        used = set()
        for name in ("probe-user-data.yaml", "vm-cloudvirt.yaml", "server-cloudoso.md"):
            used |= set(self.PLACEHOLDER.findall(self.text(name)))
        self.assertEqual(used, documented)
        self.assertEqual(documented, {"__PREF64__", "__RESOLVERS__", "__IPV4_ONLY_NAME__", "__IPV4_LITERAL__", "__IPV6_NAME__",
                                      "__IPV6_PING_TARGET__", "__DENIED_IPV4__", "__PORT__", "__WORKLOAD__", "__NAMESPACE__",
                                      "__MAC__", "__PLACEMENT_NETWORK__", "__IMAGE__", "__FLAVOR__", "__SECURITY_GROUP__",
                                      "__SCRAPE_SOURCE__"})


if __name__ == "__main__":
    unittest.main()

"""The fabric health job's tasks (eda/rulebooks/roles/fabric_health) run in ansible-playbook on localhost: what is read
(paused and deleted gateways left out), the lease gate, how a node's read or its failure is kept, the pool rows, the
judge's request, the wakes; that every node command is bounded on the node; and that the job changes nothing on a
node (no task delegated to a node but the read)."""
import base64
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda/rulebooks/roles/fabric_health")
FAB = os.path.join(ROLE, "tasks/fabric.yml")
NODE = os.path.join(ROLE, "tasks/node.yml")
LEASE = os.path.join(ROLE, "tasks/lease.yml")


def gw(name, site, deleting=False, cidr="172.18.103.0/24", status=None):
    g = {"metadata": {"name": name, "namespace": "sovereign-cloud", "uid": "u-" + name},
         "spec": {"fabricRef": {"name": "lab"}, "cloudRef": {"name": site}, "siteUnderlay": {"cidr": cidr}},
         "status": status or {"ready": True, "status": "ready", "phase": "Ready", "nodes": [{"name": "bgw-1"}],
                              "delivery": {"eventId": "e", "completedEventId": "e"}}}
    if deleting:
        g["metadata"]["deletionTimestamp"] = "2026-11-10T00:00:00Z"
    return g


FABRIC = {"metadata": {"name": "lab", "namespace": "sovereign-cloud", "uid": "u-lab"},
          "spec": {"borderGateway": {"nodes": [{"name": "bgw-1", "address": "192.0.2.1"}]}},
          "status": {"ready": True, "status": "ready", "delivery": {"eventId": "e1", "completedEventId": "e1"},
                     "realization": {"egress": {"ipv6": {"enabled": True},
                                                "networks": [{"network": "t1/n1", "ipv6": True},
                                                             {"network": "t2/n2", "ipv6": False}]},
                                     "ipam": {"pools": [{"name": "p6", "blocksFree": 3}]}}}}
GATEWAYS = [gw("atuin", "atuin"), gw("iaas1", "iaas1"), gw("old", "atuin", deleting=True),
            gw("new", "atuin", status={"phase": "Creating"}),
            gw("busy", "atuin", status={"phase": "Ready", "nodes": [{"name": "bgw-1"}],
                                        "delivery": {"eventId": "e2", "completedEventId": "e1"}}),
            {"metadata": {"name": "other"}, "spec": {"fabricRef": {"name": "lab2"}, "cloudRef": {"name": "x"}}}]
REAL = {"core": {"transport": "srv6"}, "health": {"daemonRestartWindowMinutes": 30, "nodeReadTimeoutSeconds": 60},
        "egress": {"ipv6": {"enabled": True, "sessionDownAlertMinutes": 5}}, "nodes": {"sshUser": "root"}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RoleTest(unittest.TestCase):
    def facts(self, fabric=FABRIC):
        t = by_name(load(FAB), "{{ fh_fabric.metadata.name }}: this fabric's objects")
        return t, {"fh_fabric": fabric, "fh_gateways": GATEWAYS, "fab_real": REAL, "fh_paused_sites": ["iaas1"]}

    def test_what_is_read(self):
        t, v = self.facts()
        inst = by_name(load(FAB), "{{ fh_name }}: the instances to read on each node")
        inp = by_name(load(FAB), "{{ fh_name }}: what to read on each node")
        got = run_play([t, inst, inp, write_out("[fh_my_gateways | map(attribute='metadata.name') | list, fh_reader_input, fh_ready]")],
                       dict(v, role_path=ROLE))
        self.assertEqual(got[0], ["atuin", "iaas1", "old", "new", "busy"])
        self.assertEqual(got[1]["instances"], [{"name": "root", "kind": "root"},
                                               {"name": "gw-atuin", "kind": "gateway", "siteCidr": "172.18.103.0/24",
                                                "sitePeers": []}])
        self.assertEqual(got[1]["transport"], "srv6")
        self.assertEqual(int(got[1]["windowSeconds"]), 1800)
        self.assertTrue(got[2])
        # without the post-reload verification's helper in this release no source is passed
        helper = os.path.join(ROOT, "eda/common/files/shc_frr_pending.py")
        self.assertEqual(bool(got[1]["frrPendingHelper"]), os.path.exists(helper))

    def test_lease_gate(self):
        t, v = self.facts()
        keep = by_name(load(FAB), "{{ fh_name }}: keep the lease as found")
        cases = [({"held": True, "holder": "job 1", "stamp": "a"}, FABRIC["status"]["delivery"], {"reason": "LeaseHeld"}),
                 ({"held": False, "holder": "", "stamp": "b"}, {"eventId": "e2", "completedEventId": "e1"},
                  {"reason": "JobInFlight"}),
                 ({"held": False, "holder": "", "stamp": "b"}, {"eventId": "e2", "completedEventId": "e2"}, None)]
        for lease, delivery, want in cases:
            fab = copy.deepcopy(FABRIC)
            fab["status"]["delivery"] = delivery
            _, v = self.facts(fab)
            got = run_play([t, keep, write_out("fh_skip_reason")], dict(v, fh_lease=lease))
            self.assertEqual(got, (want or {}).get("reason", ""))

    def test_lease_read(self):
        t = by_name(load(LEASE), "{{ fh_name }}: the lease")
        import datetime
        fresh = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        for spec, held in (({"holderIdentity": "HybridFabric/lab job 9", "renewTime": fresh,
                             "leaseDurationSeconds": 600}, True),
                           ({"holderIdentity": "HybridFabric/lab job 9", "renewTime": "2026-01-01T00:00:00.000000Z",
                             "leaseDurationSeconds": 600}, False),
                           ({"holderIdentity": None, "renewTime": fresh}, False)):
            got = run_play([t, write_out("fh_lease")], {"fh_name": "lab", "_fh_lease": {"resources": [{"spec": spec}]}})
            self.assertEqual(str(got["held"]).lower() == "true", held, spec)
            self.assertIn(fresh if spec["renewTime"] == fresh else "2026-01-01", got["stamp"])

    def test_node_read_kept(self):
        keep = by_name(load(NODE), "{{ bgw_node.name }}: keep the read")
        cases = [({"rc": 0, "stdout": "noise\n" + json.dumps({"bootedAt": 5})}, {"name": "bgw-1", "reachable": True,
                                                                                 "read": {"bootedAt": 5}}),
                 ({"unreachable": True, "msg": "x"}, {"name": "bgw-1", "reachable": False,
                                                      "error": "the node did not answer over SSH"}),
                 ({"rc": 124, "stdout": ""}, {"name": "bgw-1", "reachable": True, "error": "the read ran out of time"}),
                 ({"rc": 1, "stdout": "Traceback"}, {"name": "bgw-1", "reachable": True,
                                                     "error": "the read failed (exit 1)"})]
        for res, want in cases:
            init = {"name": "init", "ansible.builtin.set_fact": {"fh_nodes": []}}  # (an extra var would win over set_fact)
            got = run_play([init, keep, write_out("fh_nodes")], {"bgw_node": {"name": "bgw-1"}, "_fh_read": res})
            self.assertEqual(got, [want])

    def test_node_read_is_bounded_and_the_only_node_task(self):
        read = by_name(load(NODE), "{{ bgw_node.name }}: read the node")
        argv = read["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:3], ["timeout", "-k", "5"])
        self.assertEqual(argv[4:6], ["python3", "-"])
        self.assertTrue(read["ignore_unreachable"])
        delegated = [t["name"] for f in ("main.yml", "fabric.yml", "node.yml", "lease.yml", "events.yml")
                     for t in walk(load(os.path.join(ROLE, "tasks", f))) if "delegate_to" in t]
        self.assertEqual(delegated, ["{{ bgw_node.name }}: read the node"])

    def test_a_lease_taken_and_released_during_the_read_discards_it(self):
        t = by_name(load(FAB), "{{ fh_name }}: discard a read a job overlapped")
        for before, after, discarded in (("|a|a", "|a|a", False), ("|a|a", "|b|c", True), ("|a|a", "", True)):
            init = {"name": "init", "ansible.builtin.set_fact": {"fh_skip_reason": "", "fh_nodes": [{"name": "x"}]}}
            got = run_play([init, t, write_out("fh_skip_reason")],
                           {"fh_lease": {"holder": "", "stamp": after}, "fh_lease_before": {"stamp": before},
                            "fh_fabric_enabled": True})
            self.assertEqual(got == "LeaseHeld", discarded, (before, after))

    def test_pool_rows(self):
        cmp_ = by_name(load(FAB), "{{ fh_name }}: compare the pool rows")
        old = {"realization": {"ipam": {"pools": [{"name": "p6", "blocksFree": 3, "blocksHeldDown": 2}]}}}
        for new, changed, freed in (([{"name": "p6", "blocksFree": 3, "blocksHeldDown": 2}], False, False),
                                    ([{"name": "p6", "blocksFree": 5, "blocksHeldDown": 0}], True, True),
                                    ([{"name": "p6", "blocksFree": 2, "blocksHeldDown": 2}], True, False)):
            got = run_play([cmp_, write_out("[fh_pools_changed, fh_pools_freed]")],
                           {"fh_status": old, "fab_ipam_result": {"pools": new}})
            self.assertEqual([str(x).lower() == "true" for x in got], [changed, freed])

    def test_judge_request_and_wakes(self):
        t, v = self.facts()
        judge = by_name(load(FAB), "{{ fh_name }}: judge")
        req = {"name": "request", "ansible.builtin.set_fact": {"req": judge["ansible.builtin.command"]["stdin"]},
               "vars": judge["vars"]}
        nodes = [{"name": "bgw-1", "reachable": False, "error": "x"}]
        got = run_play([t, req, write_out("req")], dict(v, fh_nodes=nodes, fh_skip_reason=""))
        got = json.loads(got) if isinstance(got, str) else got
        self.assertEqual([(g["name"], g["paused"], g["deleting"], g["judged"]) for g in got["gateways"]],
                         [("atuin", False, False, True), ("iaas1", True, False, True), ("old", False, True, True),
                          ("new", False, False, False), ("busy", False, False, False)])
        self.assertTrue(got["enabled"])
        self.assertTrue(got["egress6"]["enabled"])
        self.assertEqual(got["nodes"], nodes)
        self.assertIsNone(got["skipped"])
        self.assertEqual(got["expectedNodes"], ["bgw-1"])
        wake = by_name(load(FAB), "{{ fh_name }}: wake the jobs that repair what was found")
        answer = {"fabric": {"wake": True}, "gateways": [{"name": "atuin", "namespace": "ns", "wake": True},
                                                         {"name": "iaas1", "namespace": "ns", "wake": False}],
                  "wakeNetworks": True}
        got = run_play([t, {"name": "w", "ansible.builtin.set_fact": {"w": wake["vars"]["nudge_crs"]}}, write_out("w")],
                       dict(v, fh_answer=answer))
        self.assertEqual(got, [{"kind": "HybridFabric", "namespace": "sovereign-cloud", "name": "lab"},
                               {"kind": "CloudGateway", "namespace": "ns", "name": "atuin"},
                               {"kind": "HybridNetwork", "namespace": "t1", "name": "n1"}])

    def test_reader_input_round_trip(self):
        """the base64 argument the job passes is what the reader decodes"""
        inp = {"instances": [{"name": "root", "kind": "root"}], "transport": "mpls", "windowSeconds": "3600"}
        got = run_play([write_out("inp | to_json | b64encode")], {"inp": inp})
        self.assertEqual(json.loads(base64.b64decode(got)), inp)


def fake_read(name):
    unit = {"active": "active", "sub": "running", "result": "success", "restarts": 0, "enabled": "enabled",
            "loaded": True}
    return {"name": name, "reachable": True,
            "read": {"bootedAt": 1800000000, "failedUnits": [], "instances": {"root": {
                "start": dict(unit, unit="shc-fabric-apply@root.service", lines=[], incomplete=[]),
                "units": {"shc-fabric-apply@root.service": unit, "frr.service": unit},
                "routing": {"unit": "frr.service", "daemons": {"zebra": "Up"}, "crashes": []},
                "kernelDrift": [], "frr": {"reloadDue": False, "recorded": True, "pending": None, "notTaken": []}}}}}


def skip_and_read_path(reader_reads=True):
    """fabric.yml from the fabric's objects to the judgement, run for real; only what reaches outside is stubbed:
    the realization, the lease reads (fake_lease_before / fake_lease_after), the SSH connect and disconnect, and the
    node read (a fake reader result per node; reader_reads False: the read step runs but brings nothing)."""
    block = load(FAB)[0]["block"]
    names = [t.get("name", "") for t in block]
    start = names.index("{{ fh_fabric.metadata.name }}: this fabric's objects")
    end = names.index("{{ fh_name }}: the judgement")
    out = [{"name": "the realization", "ansible.builtin.set_fact": {"fab_real": "{{ fake_real }}"}}]

    def stub(t):
        t = dict(t)
        inc = str(t.get("ansible.builtin.include_tasks", ""))
        if inc == "lease.yml":
            which = "fake_lease_before" if "again" not in t["name"] else "fake_lease_after"
            t.pop("ansible.builtin.include_tasks")
            t["ansible.builtin.set_fact"] = {"fh_lease": "{{ %s }}" % which}
        elif inc.endswith("bgw_connect.yml") or inc.endswith("bgw_disconnect.yml"):
            t.pop("ansible.builtin.include_tasks")
            t.pop("vars", None)
            t["ansible.builtin.debug"] = {"msg": "ssh stub"}
        elif inc == "node.yml":
            t.pop("ansible.builtin.include_tasks")
            t["ansible.builtin.set_fact"] = {
                "fh_nodes": "{{ fh_nodes + [fake_reads[bgw_node.name]] }}" if reader_reads else "{{ fh_nodes }}",
                "fh_reads_done": "{{ (fh_reads_done | default(0) | int) + 1 }}"}
        for k in ("block", "always", "rescue"):
            if k in t:
                t[k] = [stub(x) for x in t[k]]
        return t
    out += [stub(t) for t in block[start:end + 1]]
    return out


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SkipAndRead(unittest.TestCase):
    """The real skip decision and read path of fabric.yml on the ansible-core version that runs it (the job's
    execution environment runs 2.16 without native types: a None from an expression arrives as "")."""

    def run_path(self, held=False, in_flight=False, after=None, reader_reads=True):
        fab = copy.deepcopy(FABRIC)
        fab["spec"]["borderGateway"]["nodes"] = [{"name": "bgw-1", "address": "192.0.2.1"},
                                                 {"name": "bgw-2", "address": "192.0.2.2"}]
        if in_flight:
            fab["status"]["delivery"] = {"eventId": "e2", "completedEventId": "e1"}
        lease = {"holder": "job 1" if held else "", "held": held, "stamp": "a"}
        tasks = skip_and_read_path(reader_reads) + [write_out(
            "{'answer': fh_answer, 'skip': fh_skip_reason, 'reads': fh_reads_done | default(0) | int}")]
        return run_play(tasks, {"fh_fabric": fab, "fh_gateways": [], "fh_paused_sites": [], "role_path": ROLE,
                                "fake_real": REAL, "fake_lease_before": lease,
                                "fake_lease_after": after or lease,
                                "fake_reads": {n: fake_read(n) for n in ("bgw-1", "bgw-2")}})

    def test_a_free_lease_reads_every_node_and_records_the_check(self):
        got = self.run_path()
        self.assertEqual((got["skip"], got["reads"]), ("", 2))
        h = got["answer"]["fabric"]["status"]["health"]
        self.assertTrue(h["checkedAt"])
        self.assertEqual([n["name"] for n in h["nodes"]], ["bgw-1", "bgw-2"])
        self.assertIsNone(h["skipped"])

    def test_a_held_lease_reads_nothing_and_records_no_check(self):
        for kw, why in (({"held": True}, "LeaseHeld"), ({"in_flight": True}, "JobInFlight")):
            got = self.run_path(**kw)
            self.assertEqual((got["skip"], got["reads"]), (why, 0))
            h = got["answer"]["fabric"]["status"]["health"]
            self.assertEqual(h["skipped"]["reason"], why)
            self.assertNotIn("checkedAt", h)
            self.assertNotIn("conditions", got["answer"]["fabric"]["status"])

    def test_a_lease_taken_during_the_read_discards_it(self):
        got = self.run_path(after={"holder": "", "held": False, "stamp": "b"})
        self.assertEqual(got["skip"], "LeaseHeld")
        self.assertNotIn("checkedAt", got["answer"]["fabric"]["status"]["health"])

    def test_a_read_that_brought_nothing_fails_loudly(self):
        with self.assertRaises(AssertionError) as e:
            self.run_path(reader_reads=False)
        self.assertIn("has no reason to skip", str(e.exception))


if __name__ == "__main__":
    unittest.main()

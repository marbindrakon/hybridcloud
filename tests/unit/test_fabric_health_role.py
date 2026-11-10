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
         "status": status or {"ready": True, "status": "ready"}}
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
        inp = by_name(load(FAB), "{{ fh_name }}: what to read on each node")
        got = run_play([t, inp, write_out("[fh_my_gateways | map(attribute='metadata.name') | list, fh_reader_input, fh_ready]")],
                       dict(v, role_path=ROLE))
        self.assertEqual(got[0], ["atuin", "iaas1", "old"])
        self.assertEqual(got[1]["instances"], [{"name": "root", "kind": "root"},
                                               {"name": "gw-atuin", "kind": "gateway", "siteCidr": "172.18.103.0/24"}])
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
            got = run_play([t, keep, write_out("fh_skipped")], dict(v, fh_lease=lease))
            self.assertEqual(got, want)

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
                 ({"rc": 124, "stdout": ""}, {"name": "bgw-1", "reachable": False, "error": "the read ran out of time"}),
                 ({"rc": 1, "stdout": "Traceback"}, {"name": "bgw-1", "reachable": False,
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
        got = run_play([t, req, write_out("req")], dict(v, fh_nodes=nodes, fh_skipped=None))
        got = json.loads(got) if isinstance(got, str) else got
        self.assertEqual([(g["name"], g["paused"], g["deleting"]) for g in got["gateways"]],
                         [("atuin", False, False), ("iaas1", True, False), ("old", False, True)])
        self.assertTrue(got["egress6"]["enabled"])
        self.assertEqual(got["nodes"], nodes)
        self.assertIsNone(got["skipped"])
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


if __name__ == "__main__":
    unittest.main()

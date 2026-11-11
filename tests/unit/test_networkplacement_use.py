"""The placement job's handling of spec.use on a CloudOSO (networkplacement_provision): the request always carries
both uses with their defaults (booleans in either ansible-core), status.use per use (ready, off, removing with reason
InUse and up to ten users named), the site record (networkName, the policy ids, routerExternalForProject), and the
final write of a placement whose use waits to be removed: Ready stays True, condition Degraded/InUse, status.status
pending (the operator then runs it again on its reconcile period; a ready placement is skipped otherwise), and the
Degraded condition cleared once the use is gone. The CRD's CEL rules for spec.use are checked as text here (the
envtest harness, tests/cel-ipam, runs them on an API server). The tasks run in ansible-playbook (skipped when
absent)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as ST  # noqa: E402

TASKS = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks")
BACKEND = os.path.join(TASKS, "backend_cloudoso.yml")
MAIN = os.path.join(TASKS, "main.yml")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-networkplacement.yaml")


def users(n):
    return [f"floating IP 192.0.2.{i}" for i in range(1, n + 1)]


def os_result(vm="ready", ra="off", vm_users=(), ra_users=(), prune=()):
    return {"use": {"virtualMachines": {"state": vm, "policyId": "rbac-1" if vm != "off" else "", "inUse": list(vm_users)},
                    "routerAttachments": {"state": ra, "policyId": "rbac-2" if ra != "off" else "",
                                          "inUse": list(ra_users)}},
            "shareId": "rbac-1" if vm != "off" else "", "externalId": "rbac-2" if ra != "off" else "",
            "routerExternalForProject": ra != "off", "pruneInUse": list(prune),
            "networkId": "net-1", "routerId": "rtr-1", "subnets": {}, "addressMode": "", "created": [], "changed": []}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Request(unittest.TestCase):
    def test_use_is_sent_with_its_defaults_as_booleans(self):
        task = by_name(load(BACKEND), "The placement's network, router and share")
        use = task["vars"]["os_request"]["use"]
        steps = [{"ansible.builtin.set_fact": {"fab_spec": "{{ case }}"}},
                 {"ansible.builtin.set_fact": {"_use": use}}]
        res = run_cases(steps, [{}, {"use": {}}, {"use": {"routerAttachments": True}},
                                {"use": {"virtualMachines": False, "routerAttachments": True}},
                                {"use": None}], "_use")
        self.assertEqual(res, [{"virtualMachines": True, "routerAttachments": False},
                               {"virtualMachines": True, "routerAttachments": False},
                               {"virtualMachines": True, "routerAttachments": True},
                               {"virtualMachines": False, "routerAttachments": True},
                               {"virtualMachines": True, "routerAttachments": False}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class UseStatus(unittest.TestCase):
    def run_status(self, results):
        tasks = load(BACKEND)
        steps = [{"ansible.builtin.set_fact": {"os_result": "{{ case }}", "np_status": {"entity": "acme"},
                                               "np_backend": {"status": {"projectId": "proj-tenant"}},
                                               "fab_real": {"openstack": {"platformProject": "platform"}},
                                               "np_names": {"osNetwork": "hn51001"}}},
                 by_name(tasks, "Record the site side"),
                 by_name(tasks, "The environment's uses of the network")]
        return run_cases(steps, results, "{'status': np_status, 'removing': np_use_removing, 'note': np_use_note, "
                                         "'site': np_backend_status}")

    def test_cases(self):
        res = self.run_status([
            os_result(),
            os_result(vm="off", ra="ready"),
            os_result(vm="removing", ra="ready", vm_users=["port of server vm-1"]),
            os_result(ra="removing", ra_users=users(12)),
            os_result(ra="ready", prune=["project proj-b: floating IP 192.0.2.17"]),
        ])
        ready = {"state": "ready", "reason": "", "message": ""}
        off = {"state": "off", "reason": "", "message": ""}
        self.assertEqual(res[0]["status"]["use"], {"virtualMachines": ready, "routerAttachments": off})
        self.assertEqual((res[0]["removing"], res[0]["note"]), ([], ""))
        self.assertEqual(res[0]["status"]["entity"], "acme")
        self.assertEqual(res[1]["status"]["use"], {"virtualMachines": off, "routerAttachments": ready})
        vm = res[2]["status"]["use"]["virtualMachines"]
        self.assertEqual(vm, {"state": "removing", "reason": "InUse",
                              "message": "Turning this off waits until the environment no longer uses it: "
                                         "port of server vm-1"})
        self.assertEqual(res[2]["removing"], ["virtualMachines"])
        self.assertEqual(res[2]["note"], "; use for virtual machines turned off, waiting: port of server vm-1")
        ra = res[3]["status"]["use"]["routerAttachments"]
        self.assertEqual(ra["message"], "Turning this off waits until the environment no longer uses it: "
                         + ", ".join(users(10)) + " and 2 more")
        self.assertEqual(res[3]["removing"], ["routerAttachments"])
        self.assertEqual(res[4]["note"], "; external access of other projects kept while used: "
                                         "project proj-b: floating IP 192.0.2.17")

    def test_site_record(self):
        res = self.run_status([os_result(ra="ready"), os_result(vm="off")])
        site = res[0]["site"]
        self.assertEqual(site["networkName"], "hn51001")
        self.assertEqual(site["rbac"], {"shared": "rbac-1", "external": "rbac-2"})
        self.assertIs(site["routerExternalForProject"], True)
        self.assertEqual(site["sharedTo"], "proj-tenant")
        self.assertEqual(res[1]["site"]["rbac"], {"shared": "", "external": ""})
        self.assertIs(res[1]["site"]["routerExternalForProject"], False)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FinalWrite(unittest.TestCase):
    """The vars of "Report the placement ready" as the job hands them to fabric_finish.yml."""

    def render(self, cases):
        task = by_name(load(MAIN), "Report the placement ready")
        v = task["vars"]
        steps = [{"ansible.builtin.set_fact": {"np_final": {"entity": "acme", "use": "{{ case.use }}"},
                                               "np_status": {"use": "{{ case.use }}"},
                                               "np_use_removing": "{{ case.removing }}",
                                               "np_backend_kind": "{{ case.kind }}",
                                               "fab_st": "{{ case.before }}"}},
                 {"ansible.builtin.set_fact": {"_status": v["fab_status"], "_conds": v["fab_conditions"]},
                  "vars": {"_np_degraded_before": v["_np_degraded_before"]}}]
        return run_cases(steps, cases, "{'status': _status, 'conds': _conds | map('dict2items') | map('rejectattr', "
                                       "'key', 'equalto', 'lastTransitionTime') | map('items2dict') | list}")

    def test_cases(self):
        msg = "Turning this off waits until the environment no longer uses it: floating IP 192.0.2.15"
        removing = {"virtualMachines": {"state": "ready", "reason": "", "message": ""},
                    "routerAttachments": {"state": "removing", "reason": "InUse", "message": msg}}
        steady = {"virtualMachines": {"state": "ready", "reason": "", "message": ""},
                  "routerAttachments": {"state": "off", "reason": "", "message": ""}}
        degraded = {"conditions": [{"type": "Degraded", "status": "True", "reason": "InUse"}]}
        res = self.render([
            {"use": removing, "removing": ["routerAttachments"], "kind": "CloudOSO", "before": {}},
            {"use": steady, "removing": [], "kind": "CloudOSO", "before": degraded},
            {"use": steady, "removing": [], "kind": "CloudOSO", "before": {}},
            {"use": {}, "removing": [], "kind": "CloudVirt", "before": degraded},
        ])
        self.assertEqual(res[0]["status"]["status"], "pending")
        self.assertEqual(res[0]["conds"], [{"type": "Degraded", "status": "True", "reason": "InUse", "message": msg}])
        self.assertNotIn("status", res[1]["status"])
        self.assertEqual(res[1]["conds"], [{"type": "Degraded", "status": "False", "reason": "Ready", "message": ""}])
        self.assertEqual(res[2]["conds"], [])
        self.assertNotIn("status", res[2]["status"])
        self.assertEqual(res[3]["conds"], [])

    def test_previous_wait_is_recognised(self):
        task = by_name(load(MAIN), "Start with nothing to wait for")
        expr = task["ansible.builtin.set_fact"]["np_prev_use_wait"]
        steps = [{"ansible.builtin.set_fact": {"fab_st": "{{ case }}", "fab_entity": "acme"}},
                 {"ansible.builtin.set_fact": {"_w": expr}}]
        res = run_cases(steps, [
            {},
            {"ready": True, "use": {"virtualMachines": {"state": "ready"}, "routerAttachments": {"state": "off"}}},
            {"ready": True, "use": {"virtualMachines": {"state": "ready"}, "routerAttachments": {"state": "removing"}}},
            {"ready": False, "use": {"routerAttachments": {"state": "removing"}}},
        ], "_w | bool")
        self.assertEqual(res, [False, False, True, False])


class PhaseOfAWaitingUse(unittest.TestCase):
    """What the status writer makes of the final write: Ready stays True, phase Degraded, ready not re-run-skipped."""

    def test_ready_kept_and_phase_degraded(self):
        obj = {"metadata": {"name": "p", "generation": 2, "resourceVersion": "7"},
               "status": {"ready": True, "status": "ready", "observedGeneration": 2,
                          "conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": "ok"}]}}
        body = {"status": "pending", "ready": True, "message": "m", "observedGeneration": 2,
                "conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": "m"},
                               {"type": "Degraded", "status": "True", "reason": "InUse", "message": "waits"}]}
        patch, info = ST.compute_status_patch(obj, body, {"action": "pending"})
        st = patch["status"]
        self.assertEqual(info["phase"], "Degraded")
        self.assertTrue(st["ready"])
        ready = next(c for c in st["conditions"] if c["type"] == "Ready")
        self.assertEqual(ready["status"], "True")
        # the operator re-publishes a placement whose status.status is not ready
        self.assertEqual(st["status"], "pending")


class CelRulesAsText(unittest.TestCase):
    def test_spec_use_rules_and_defaults(self):
        import yaml
        with open(CRD) as f:
            crd = yaml.safe_load(f)
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
        rules = {r["message"]: " ".join(r["rule"].split()) for r in spec["x-kubernetes-validations"]}
        self.assertEqual(rules["spec.use needs virtualMachines, routerAttachments or both"],
                         "!has(self.use) || self.use.virtualMachines || self.use.routerAttachments")
        self.assertEqual(rules["spec.use is offered only on CloudOSO; other environments use the defaults"],
                         "!has(self.use) || self.backend.kind == 'CloudOSO' || "
                         "(self.use.virtualMachines && !self.use.routerAttachments)")
        use = spec["properties"]["use"]["properties"]
        self.assertEqual((use["virtualMachines"]["default"], use["routerAttachments"]["default"]), (True, False))
        status = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
        self.assertEqual(sorted(status["use"]["properties"]), ["routerAttachments", "virtualMachines"])


if __name__ == "__main__":
    unittest.main()

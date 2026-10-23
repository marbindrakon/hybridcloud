"""The delete request's status write keeps a validation refusal visible to the teardown (R2A-F1, PAC-O1, GA2-F11).

mark_phase.yml writes phase Deleting at the delete request and, for an object that was ready, Ready=False/Deleting
(PAC-O1). k8s_status replaces the stored condition of the same type. The first task of the environment and network
teardowns (eda/common/tasks/teardown_unrealized.yml) reads the live object after that write and deletes an object
refused at validation without a site cleanup only when its Ready reason is a validation reason. These tests render
mark_phase's status write, apply it with the k8s_status merge rule and evaluate the teardown's decision on the result.
"""
import copy
import os
import unittest

from play_helper import HAVE_ANSIBLE, ROOT, load, run_play, write_out

MARK_PHASE = os.path.join(ROOT, "operator", "roles", "_common", "tasks", "mark_phase.yml")
UNREALIZED = os.path.join(ROOT, "eda", "common", "tasks", "teardown_unrealized.yml")


def k8s_status_merge(stored, written):
    """operator_sdk.util.k8s_status: top-level status keys are merged; a condition replaces the stored one of its type."""
    out = copy.deepcopy(stored or {})
    for key, value in written.items():
        if key != "conditions":
            out[key] = value
    if "conditions" in written:
        conds = list(out.get("conditions") or [])
        for c in written["conditions"]:
            conds = [o for o in conds if o.get("type") != c["type"]] + [c]
        out["conditions"] = conds
    return out


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class DeleteRequestKeepsTheRefusal(unittest.TestCase):
    def written(self, status):
        tasks = load(MARK_PHASE)
        record = tasks[-1]
        render = {"name": "render", "vars": record["vars"],
                  "ansible.builtin.set_fact": {"_st": record["operator_sdk.util.k8s_status"]["status"]}}
        return run_play(tasks[:-1] + [render, write_out("_st")],
                        {"mark_phase_cr": {"status": status}, "mark_phase_deleting": True})

    def nothing_realized(self, kind, status):
        decide = load(UNREALIZED)[0]
        cr = {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": kind,
              "metadata": {"name": "web", "namespace": "entity-uxr-corp"}, "status": status}
        return run_play([decide, write_out("teardown_nothing_realized | bool")], {"shc_live_cr": cr})

    def after_delete_request(self, status):
        return k8s_status_merge(status, self.written(status))

    def test_refused_object_still_deleted_without_site_cleanup(self):
        refused = {"phase": "Failed", "ready": False, "message": "Site iaas1 is not allowed for this tenant",
                   "conditions": [{"type": "Ready", "status": "False", "reason": "SiteNotAllowed",
                                   "message": "Site iaas1 is not allowed for this tenant"}]}
        self.assertIs(self.nothing_realized("CloudVirt", refused), True)
        after = self.after_delete_request(refused)
        self.assertEqual(after["phase"], "Deleting")
        self.assertEqual([c["reason"] for c in after["conditions"] if c["type"] == "Ready"], ["SiteNotAllowed"])
        self.assertIs(self.nothing_realized("CloudVirt", after), True)

    def test_refusal_in_status_reason_without_conditions_is_kept(self):
        refused = {"phase": "Failed", "ready": False, "reason": "NameInUse"}
        after = self.after_delete_request(refused)
        self.assertNotIn("conditions", after)
        self.assertIs(self.nothing_realized("NetworkPlacement", after), True)

    def test_ready_object_stops_being_ready(self):
        ready = {"phase": "Ready", "ready": True, "namespaces": [{"name": "web"}],
                 "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]}
        after = self.after_delete_request(ready)
        self.assertIs(after["ready"], False)
        self.assertEqual([(c["status"], c["reason"]) for c in after["conditions"] if c["type"] == "Ready"],
                         [("False", "Deleting")])
        self.assertIs(self.nothing_realized("CloudVirt", after), False)

    def test_object_without_ready_condition_gets_one(self):
        after = self.after_delete_request({"phase": "Creating"})
        self.assertEqual([(c["status"], c["reason"]) for c in after["conditions"]], [("False", "Deleting")])
        self.assertIs(after["ready"], False)


if __name__ == "__main__":
    unittest.main()

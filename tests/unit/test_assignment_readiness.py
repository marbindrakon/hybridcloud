"""Assignment readiness (eda/rulebooks/roles/assignment_provision): every linked object must be Ready.

P2AC-F2 (GF2-F2): the job computed all_projects_ready but did not use it, so a not-Ready app Project never held an
Assignment, and it wrote WaitingForDependencies where the reasons registry has LinkedNotReady. Now the Assignment is
Ready only when its Team, every Project and its OpenShift cluster are Ready; until then it is Waiting with
LinkedNotReady (WaitingForCluster while no cluster is named) and the ACM step does not run. The expressions run in
ansible-playbook (skipped when absent); the wiring checks always run.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as S  # noqa: E402
import shc_status_reasons as R  # noqa: E402

PROVISION = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_provision", "tasks", "main.yml")
STEPS = ("Build project readiness list", "Compute project readiness", "Determine overall readiness",
         "Determine Assignment readiness")


def tasks():
    return load(PROVISION)


def status_body():
    return by_name(tasks(), "Update Assignment status on services cluster")["vars"]["cr_status_body"]


class Wiring(unittest.TestCase):
    def test_acm_step_needs_the_overall_readiness(self):
        self.assertEqual(by_name(tasks(), "Provision via ACM Policy on central cluster")["when"], "assignment_ready | bool")
        ready = by_name(tasks(), "Determine Assignment readiness")["ansible.builtin.set_fact"]["assignment_ready"]
        for part in ("team_is_ready", "all_projects_ready", "openshift_is_ready"):
            self.assertIn(part, ready)

    def test_reasons_are_registered_waiting_reasons(self):
        reason = next(c for c in status_body()["conditions"] if c["type"] == "Ready")["reason"]
        for name in ("LinkedNotReady", "WaitingForCluster"):
            self.assertIn(name, reason)
            self.assertEqual(R.REASONS[name]["class"], R.WAITING)
        self.assertNotIn("WaitingForDependencies", reason)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Readiness(unittest.TestCase):
    def run_case(self, team=True, projects=None, openshift="ocp1", cluster=True):
        projects = projects if projects is not None else {}
        steps = [by_name(tasks(), n) for n in STEPS]
        cond = next(c for c in status_body()["conditions"] if c["type"] == "Ready")
        render = {"name": "render", "ansible.builtin.set_fact": {
            "_status": status_body()["status"], "_reason": cond["reason"], "_cond_status": cond["status"]}}
        lookups = {"results": [{"item": name, "resources": [{"status": {"ready": ok}}]}
                               for name, ok in projects.items()]}
        return run_play(steps + [render, write_out(
            "{'ready': assignment_ready | bool, 'status': _status | trim, 'reason': _reason | trim, "
            "'cond': _cond_status | trim}")],
            {"assignment_projects": list(projects), "project_lookups": lookups, "team_is_ready": team,
             "openshift_is_ready": cluster, "assignment_openshift": openshift})

    def phase(self, got):
        obj = {"metadata": {"name": "a", "generation": 1}, "status": {"phase": "Creating"}}
        patch, _ = S.compute_status_patch(obj, {"status": got["status"], "ready": got["ready"], "conditions": [
            {"type": "Ready", "status": got["cond"], "reason": got["reason"], "message": "m"}]},
            {"action": got["status"]})
        return patch["status"]["phase"], patch["status"]["summary"]

    def test_project_not_ready_holds_the_assignment(self):
        got = self.run_case(projects={"p1": True, "p2": False})
        self.assertEqual((got["ready"], got["status"], got["reason"]), (False, "pending", "LinkedNotReady"))
        self.assertEqual(self.phase(got), ("Waiting", R.REASONS["LinkedNotReady"]["summary"]))

    def test_team_not_ready(self):
        got = self.run_case(team=False, projects={"p1": True})
        self.assertEqual((got["ready"], got["reason"]), (False, "LinkedNotReady"))

    def test_cluster_not_ready(self):
        got = self.run_case(cluster=False)
        self.assertEqual((got["ready"], got["reason"]), (False, "LinkedNotReady"))

    def test_no_cluster_named(self):
        got = self.run_case(openshift="", cluster=False, projects={"p1": False})
        self.assertEqual((got["ready"], got["reason"]), (False, "WaitingForCluster"))

    def test_all_ready(self):
        got = self.run_case(projects={"p1": True, "p2": True})
        self.assertEqual((got["ready"], got["status"], got["reason"], got["cond"]), (True, "ready", "EDASucceeded", "True"))
        self.assertEqual(self.phase(got)[0], "Ready")
        self.assertTrue(self.run_case(projects={})["ready"])


if __name__ == "__main__":
    unittest.main()

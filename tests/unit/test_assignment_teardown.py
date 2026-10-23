"""Assignment teardown (eda/rulebooks/roles/assignment_teardown): the ACM steps run only for a provisioned Assignment.

P2AC-F3: an Assignment that names a cluster it never reached owns no ACM policy. Only what the provision job
records after it applied the policy (status.assignmentProvisioned, status.acmPolicyName) sends the teardown to the
ACM steps; spec.openshift and status.clusterName (both set while the Assignment still waits for its cluster) do
not. A provisioned Assignment whose ACM creator credential Secret is missing stops with CredentialsMissing and a
message naming the Secret (a platform-team step), not with a generic failure. The expressions run in
ansible-playbook (skipped when absent); the wiring checks always run.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

TEARDOWN = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_teardown", "tasks", "main.yml")
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as S  # noqa: E402
import shc_status_reasons as R  # noqa: E402

DERIVE = "Derive ACM policy name from Assignment CR name (unique per cluster)"
ACM_BLOCK = "Teardown ACM resources when provisioned"
SKIP_BLOCK = "Skip ACM teardown if Assignment was never provisioned"


def tasks():
    return load(TEARDOWN)


class Wiring(unittest.TestCase):
    def test_predicate_reads_only_what_the_provision_job_records(self):
        derive = by_name(tasks(), DERIVE)
        pred = derive["vars"]["_asg_provisioned"]
        self.assertIn("assignmentProvisioned", pred)
        self.assertIn("acmPolicyName", pred)
        self.assertNotIn("openshift", pred)
        self.assertNotIn("clusterName", pred)

    def test_provision_job_records_the_fields(self):
        prov = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_provision", "tasks")
        body = by_name(load(os.path.join(prov, "main.yml")), "Update Assignment status on services cluster")
        st = body["vars"]["cr_status_body"]
        self.assertIn("assignment_provisioned", st["assignmentProvisioned"])
        self.assertIn("assignment_acm_policy_name", st["acmPolicyName"])
        with open(os.path.join(prov, "provision_acm.yml")) as f:
            acm = f.read()
        self.assertIn("assignment_provisioned: true", acm)

    def test_missing_secret_is_named_before_any_credential_use(self):
        block = by_name(tasks(), ACM_BLOCK)["block"]
        names = [t.get("name") for t in block]
        stop = names.index("Stop when the ACM creator credential is missing")
        self.assertLess(stop, names.index("Decode ACM central SA credentials for delete"))
        self.assertEqual(names[stop - 1], "Record that the ACM creator credential is missing")
        rescue = next(t for t in tasks() if "rescue" in t)["rescue"][0]
        self.assertIn("assignment_teardown_reason", rescue["vars"]["cr_status_body"]["reason"])

    def test_credentials_missing_is_a_platform_reason(self):
        self.assertEqual(R.REASONS["CredentialsMissing"]["actor"], "P")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Predicate(unittest.TestCase):
    def policy_name(self, spec, status):
        derive = by_name(tasks(), DERIVE)
        cr = {"metadata": {"name": "app-on-ocp1"}, "spec": spec, "status": status}
        return run_play([derive, write_out("delete_acm_policy_name | trim")],
                        {"assignment_cr_name": "app-on-ocp1", "assignment_cr_lookup": {"resources": [cr]}})

    def test_named_cluster_never_reached_skips_acm(self):
        # the provision job's write while it waits for the cluster: clusterName known, nothing provisioned
        self.assertEqual(self.policy_name({"team": "t", "openshift": "ocp1"},
                                          {"openshift": "ocp1", "clusterName": "ocp1", "openshiftReady": False,
                                           "assignmentProvisioned": False, "acmPolicyName": ""}), "")
        self.assertEqual(self.policy_name({"team": "t", "openshift": "ocp1"}, None), "")
        self.assertEqual(self.policy_name({"team": "t"}, {}), "")

    def test_provisioned_assignment_runs_acm(self):
        self.assertEqual(self.policy_name({"team": "t", "openshift": "ocp1"},
                                          {"assignmentProvisioned": True, "acmPolicyName": "app-on-ocp1"}),
                         "app-on-ocp1")
        self.assertEqual(self.policy_name({"team": "t", "openshift": "ocp1"},
                                          {"assignmentProvisioned": "True"}), "app-on-ocp1")
        # recorded by an earlier provision version: the policy name alone
        self.assertEqual(self.policy_name({"team": "t", "openshift": "ocp1"},
                                          {"acmPolicyName": "app-on-ocp1"}), "app-on-ocp1")


PROVISION = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_provision", "tasks", "main.yml")
CARRY = "Carry the ACM record forward from the stored status"


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RecordCarriesForward(unittest.TestCase):
    """R3A-F1: a provision run that does not reach the ACM steps keeps the ACM record the stored status holds."""

    def rerun_status(self, stored):
        # the provision job without its ACM steps (Team, a Project or the cluster not Ready): the record it writes
        prov = load(PROVISION)
        carry = by_name(prov, CARRY)
        st = by_name(prov, "Update Assignment status on services cluster")["vars"]["cr_status_body"]
        render = {"name": "render the record", "ansible.builtin.set_fact": {
            "_rec": {"assignmentProvisioned": st["assignmentProvisioned"], "acmPolicyName": st["acmPolicyName"]}}}
        cr = {"metadata": {"name": "app-on-ocp1"}, "status": stored}
        return run_play([carry, render, write_out("_rec")], {"assignment_cr_lookup": {"resources": [cr]}})

    def delete_policy_name(self, status):
        derive = by_name(tasks(), DERIVE)
        cr = {"metadata": {"name": "app-on-ocp1"}, "spec": {"team": "t", "openshift": "ocp1"}, "status": status}
        return run_play([derive, write_out("delete_acm_policy_name | trim")],
                        {"assignment_cr_name": "app-on-ocp1", "assignment_cr_lookup": {"resources": [cr]}})

    def test_not_ready_rerun_after_provisioning_keeps_the_teardown_path(self):
        stored = {"ready": True, "clusterName": "ocp1", "assignmentProvisioned": True, "acmPolicyName": "app-on-ocp1"}
        rec = self.rerun_status(stored)
        self.assertEqual(str(rec["assignmentProvisioned"]).lower(), "true")
        self.assertEqual(rec["acmPolicyName"], "app-on-ocp1")
        after = dict(stored, ready=False, assignmentProvisioned=rec["assignmentProvisioned"],
                     acmPolicyName=rec["acmPolicyName"])
        self.assertEqual(self.delete_policy_name(after), "app-on-ocp1")

    def test_never_provisioned_rerun_records_nothing(self):
        for stored in ({"clusterName": "ocp1", "assignmentProvisioned": False, "acmPolicyName": ""}, None):
            rec = self.rerun_status(stored)
            self.assertEqual(str(rec["assignmentProvisioned"]).lower(), "false")
            self.assertEqual(rec["acmPolicyName"], "")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class MissingSecret(unittest.TestCase):
    def test_teardown_stops_with_credentials_missing_and_names_the_secret(self):
        all_tasks = tasks()
        block = by_name(all_tasks, ACM_BLOCK)["block"]
        record = by_name(block, "Record that the ACM creator credential is missing")
        stop = by_name(block, "Stop when the ACM creator credential is missing")
        rescue_body = next(t for t in all_tasks if "rescue" in t)["rescue"][0]["vars"]["cr_status_body"]
        harness = [{"block": [record, stop, write_out("{'reached': 'after the stop'}")],
                    "rescue": [{"name": "render the rescue body", "ansible.builtin.set_fact": {
                        "_reason": rescue_body["reason"], "_message": rescue_body["message"]}},
                        write_out("{'reason': _reason, 'message': _message}")]}]
        got = run_play(harness, {"acm_sa_secret": {"resources": []},
                                 "acm_central_sa_secret_name": "assignment-acm-creator-sa",
                                 "assignment_operator_namespace": "sovereign-cloud",
                                 "delete_acm_policy_name": "app-on-ocp1"})
        self.assertEqual(got["reason"], "CredentialsMissing")
        self.assertIn("Secret assignment-acm-creator-sa", got["message"])
        self.assertIn("namespace sovereign-cloud", got["message"])
        self.assertIn("Retry now", got["message"])

        # through the status library on the deleting object: Ready=False/CredentialsMissing, platform summary
        obj = {"metadata": {"name": "app-on-ocp1", "generation": 1, "deletionTimestamp": "2026-10-22T12:00:00Z"},
               "status": {"phase": "Deleting"}}
        patch, info = S.compute_status_patch(obj, {"status": "failed", "reason": got["reason"], "ready": False,
                                                   "message": got["message"]}, {"action": "failed"})
        ready = S.find_condition(patch["status"]["conditions"], "Ready")
        self.assertEqual((ready["status"], ready["reason"]), ("False", "CredentialsMissing"))
        self.assertEqual(patch["status"]["phase"], "Deleting")
        self.assertEqual(patch["status"]["retry"]["attempts"], 1)

    def test_secret_present_continues(self):
        block = by_name(tasks(), ACM_BLOCK)["block"]
        record = by_name(block, "Record that the ACM creator credential is missing")
        stop = by_name(block, "Stop when the ACM creator credential is missing")
        got = run_play([record, stop, write_out("{'reason': assignment_teardown_reason | default('TeardownFailed')}")],
                       {"acm_sa_secret": {"resources": [{"metadata": {"name": "assignment-acm-creator-sa"}}]},
                        "acm_central_sa_secret_name": "assignment-acm-creator-sa",
                        "assignment_operator_namespace": "sovereign-cloud", "delete_acm_policy_name": "x"})
        self.assertEqual(got["reason"], "TeardownFailed")


if __name__ == "__main__":
    unittest.main()

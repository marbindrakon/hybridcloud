"""Assignment teardown (eda/rulebooks/roles/assignment_teardown): the ACM steps run only for a provisioned Assignment.

P2AC-F3: an Assignment that names a cluster it never reached owns no ACM policy. Only what the provision job
records once an ACM object of the Assignment exists (status.assignmentProvisioned, status.acmPolicyName) sends
the teardown to the ACM steps; spec.openshift and status.clusterName (both set while the Assignment still waits
for its cluster) do not. A provisioned Assignment whose ACM creator credential Secret is missing stops with
CredentialsMissing and a message naming the Secret (a platform-team step), not with a generic failure. The
expressions run in ansible-playbook (skipped when absent); the wiring checks always run.

R3U-F1: a later provision run that skips the ACM step (a linked object not Ready) keeps what an earlier run recorded,
so the teardown of a provisioned Assignment still takes the ACM path.

R3A-F2: the record is set as soon as an ACM POST returned 201/409, and a job that fails after that (a later POST,
the compliance wait) writes it with the failure.
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
        with open(os.path.join(prov, "apply_acm_resource.yml")) as f:
            acm = f.read()
        self.assertIn("assignment_provisioned: true", acm)

    def test_record_is_set_as_soon_as_an_acm_object_exists(self):
        # R3A-F2: right after a POST returned 201/409, before the update and verify calls of that object
        prov = os.path.join(ROOT, "eda", "rulebooks", "roles", "assignment_provision", "tasks")
        apply = load(os.path.join(prov, "apply_acm_resource.yml"))
        names = [t["name"].split(" — ")[0] for t in apply]
        rec = names.index("Record the ACM policy of this Assignment")
        self.assertEqual(names[rec - 1], "Fail when ACM POST returned an unexpected status")
        self.assertEqual(rec, 2)
        acm = load(os.path.join(prov, "provision_acm.yml"))
        acm_names = [t["name"] for t in acm]
        self.assertLess(acm_names.index("Apply ACM Policy to central cluster"),
                        acm_names.index("Wait for ACM Policy to report compliance on target spoke"))
        self.assertNotIn("Set provisioned status facts", acm_names)

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
KEEP = "Keep what an earlier run put on a cluster"


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RecordSurvivesLaterRuns(unittest.TestCase):
    """R3U-F1: a provision run that skips the ACM step keeps what an earlier run recorded; only the teardown clears it."""

    def provision_record(self, stored, acm_ran):
        prov = load(PROVISION)
        body = by_name(prov, "Update Assignment status on services cluster")["vars"]["cr_status_body"]
        render = {"name": "render", "ansible.builtin.set_fact": {
            "_prov": body["assignmentProvisioned"], "_policy": body["acmPolicyName"]}}
        extra = {"assignment_cr_lookup": {"resources": [{"metadata": {"name": "app-on-ocp1"}, "status": stored}]}}
        if acm_ran:
            # what provision_acm.yml sets once it applied the policy
            extra.update({"assignment_provisioned": True, "assignment_acm_policy_name": "app-on-ocp1"})
        got = run_play([by_name(prov, KEEP), render, write_out(
            "{'assignmentProvisioned': _prov | bool, 'acmPolicyName': _policy | trim}")], extra)
        return got

    def test_not_ready_run_after_ready_run_keeps_the_record_and_teardown_takes_acm(self):
        first = self.provision_record(None, acm_ran=True)
        self.assertEqual(first, {"assignmentProvisioned": True, "acmPolicyName": "app-on-ocp1"})
        # the next run finds the cluster (or Team, or a Project) not Ready: no ACM step this time
        stored = {"ready": True, "clusterName": "ocp1", **first}
        second = self.provision_record(stored, acm_ran=False)
        self.assertEqual(second, {"assignmentProvisioned": True, "acmPolicyName": "app-on-ocp1"})
        third = self.provision_record({**stored, **second, "ready": False}, acm_ran=False)
        self.assertEqual(third, second)
        # the delete then still removes the ACM policy
        self.assertEqual(Predicate().policy_name({"team": "t", "openshift": "ocp1"}, {**stored, **third}),
                         "app-on-ocp1")

    def test_string_record_from_an_earlier_write_is_kept(self):
        got = self.provision_record({"assignmentProvisioned": "True", "acmPolicyName": "app-on-ocp1"}, acm_ran=False)
        self.assertEqual(got, {"assignmentProvisioned": True, "acmPolicyName": "app-on-ocp1"})

    def test_never_provisioned_stays_unrecorded(self):
        for stored in (None, {}, {"assignmentProvisioned": False, "acmPolicyName": "", "clusterName": "ocp1"}):
            self.assertEqual(self.provision_record(stored, acm_ran=False),
                             {"assignmentProvisioned": False, "acmPolicyName": ""})

    def test_keep_runs_before_the_status_write(self):
        names = [t.get("name") for t in load(PROVISION)[0]["block"]]
        self.assertLess(names.index(KEEP), names.index("Update Assignment status on services cluster"))
        self.assertLess(names.index("Read Assignment CR from services cluster"), names.index(KEEP))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FailureKeepsRecord(unittest.TestCase):
    """R3A-F2: a provision job that fails after it created an ACM object writes the record with the failure."""

    def rescue_body(self, facts):
        prov = load(PROVISION)
        rescue = next(t for t in prov if "rescue" in t)["rescue"][0]
        vars_ = dict(rescue["vars"])
        body = vars_.pop("cr_status_body")
        set_facts = [{"name": "fact", "ansible.builtin.set_fact": facts}] if facts else []
        harness = [{"block": set_facts + [{"name": "fail late", "ansible.builtin.fail": {"msg": "compliance wait"}}],
                    "rescue": [{"name": "render", "vars": vars_, "ansible.builtin.set_fact": {"_body": body}},
                               write_out("_body")]}]
        return run_play(harness, {})

    def test_record_goes_with_the_failure_once_set(self):
        got = self.rescue_body({"assignment_provisioned": True, "assignment_acm_policy_name": "app-on-ocp1"})
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["conditions"][0]["reason"], "EDAFailed")
        self.assertEqual(got["message"], "compliance wait")
        self.assertIs(got["assignmentProvisioned"], True)
        self.assertEqual(got["acmPolicyName"], "app-on-ocp1")
        derive = by_name(tasks(), DERIVE)
        cr = {"metadata": {"name": "app-on-ocp1"}, "spec": {"openshift": "ocp1"},
              "status": {"assignmentProvisioned": got["assignmentProvisioned"], "acmPolicyName": got["acmPolicyName"]}}
        self.assertEqual(run_play([derive, write_out("delete_acm_policy_name | trim")],
                                  {"assignment_cr_name": "app-on-ocp1", "assignment_cr_lookup": {"resources": [cr]}}),
                         "app-on-ocp1")

    def test_stored_record_goes_with_the_failure(self):
        got = self.rescue_body({"assignment_stored_provisioned": True,
                                "assignment_stored_acm_policy_name": "app-on-ocp1"})
        self.assertIs(got["assignmentProvisioned"], True)
        self.assertEqual(got["acmPolicyName"], "app-on-ocp1")

    def test_no_record_is_written_when_none_is_known(self):
        got = self.rescue_body({})
        self.assertEqual(got["status"], "failed")
        self.assertNotIn("assignmentProvisioned", got)
        self.assertNotIn("acmPolicyName", got)


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

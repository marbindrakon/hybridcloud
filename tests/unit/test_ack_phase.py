"""Phase in the namespace operator's acknowledgement write (operator/roles/_common/tasks/ack_phase.yml).

OD42 (P2AC-F1): HybridNetwork, CloudVirt, NetworkPlacement, Team, Project and Assignment wrote their first status
("Mark <Kind> as reconciling") without a phase; phase Creating followed 1-2 s later. The acknowledgement now carries
the phase itself: Creating for a new object, Updating for a spec change (or an object without a stored phase), the
stored phase (with its start time) for a re-check. The publish step is told the recorded phase, so mark_phase.yml
does not write Creating a second time. The decision runs in ansible-playbook (skipped when absent); the wiring checks
always run.
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

COMMON = os.path.join(ROOT, "operator", "roles", "_common", "tasks")
KINDS = {"hybridnetwork": "HybridNetwork", "cloudvirt": "CloudVirt", "networkplacement": "NetworkPlacement",
         "team": "Team", "project": "Project", "assignment": "Assignment"}
NOW_SINCE = "2026-10-22T09:00:00Z"


def role_block(kind):
    tasks = load(os.path.join(ROOT, "operator", "namespace", "roles", kind, "tasks", "main.yml"))
    return next(t for t in tasks if "block" in t)["block"]


class Wiring(unittest.TestCase):
    def test_first_status_write_carries_the_phase(self):
        for kind, Kind in KINDS.items():
            with self.subTest(kind=kind):
                block = role_block(kind)
                names = [t.get("name") for t in block]
                first = next(i for i, t in enumerate(block) if "operator_sdk.util.k8s_status" in t)
                self.assertEqual(names[first], "Mark %s as reconciling" % Kind)
                status = block[first]["operator_sdk.util.k8s_status"]["status"]
                self.assertEqual(status["phase"], "{{ ack_phase.phase }}")
                self.assertEqual(status["phaseSince"], "{{ ack_phase.phaseSince }}")
                self.assertEqual(status["summary"], "{{ ack_phase.summary }}")
                self.assertEqual(status["status"], "reconciling")
                ack = names.index("Decide the phase of the acknowledgement")
                self.assertLess(ack, first)
                inc = block[ack]["ansible.builtin.include_role"]
                self.assertEqual((inc["name"], inc["tasks_from"]), ("_common", "ack_phase"))
                self.assertEqual(block[ack]["vars"]["ack_phase_cr"], "{{ _hybridsovereign_redhat_%s }}" % kind)
                self.assertEqual(block[ack]["when"], block[first]["when"])

    def test_publish_is_told_the_recorded_phase(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                pub = by_name(role_block(kind), "Publish operator event to AMQ Streams")
                self.assertIn("ack_phase", pub["vars"]["operator_event_phase_recorded"])
                self.assertIn("reconcile_keeps_ready", pub["vars"]["operator_event_phase_recorded"])

    def test_summaries_match_the_status_library(self):
        with open(os.path.join(COMMON, "ack_phase.yml")) as f:
            text = f.read()
        for phase, summary in R.PHASE_SUMMARIES.items():
            self.assertIn("'%s': '%s'" % (phase, summary), text)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Decision(unittest.TestCase):
    def decide(self, status, generation=1):
        cr = {"metadata": {"name": "n", "generation": generation}, "spec": {}, "status": status}
        tasks = load(os.path.join(COMMON, "ack_phase.yml"))
        return run_play(tasks + [write_out("ack_phase")], {"ack_phase_cr": cr})

    def test_new_object_is_creating(self):
        for status in (None, {}, {"conditions": [{"type": "Ready", "status": "False", "reason": "AwaitingEDA"}]},
                       {"phase": "Creating", "phaseSince": NOW_SINCE, "summary": "Being created."}):
            with self.subTest(status=status):
                got = self.decide(status)
                self.assertEqual(got["phase"], "Creating")
                self.assertEqual(got["summary"], "Being created.")
                self.assertTrue(got["phaseSince"])
        # an operator restart before the job: the start time stays
        self.assertEqual(self.decide({"phase": "Creating", "phaseSince": NOW_SINCE})["phaseSince"], NOW_SINCE)

    def test_spec_change_is_updating(self):
        for cur in ("Ready", "Waiting", "Failed", "Degraded"):
            with self.subTest(cur=cur):
                got = self.decide({"phase": cur, "phaseSince": NOW_SINCE, "observedGeneration": 1,
                                   "summary": "x"}, generation=2)
                self.assertEqual(got["phase"], "Updating")
                self.assertEqual(got["summary"], "Applying the latest change.")
                self.assertNotEqual(got["phaseSince"], NOW_SINCE)

    def test_handled_object_without_a_phase_is_updating(self):
        self.assertEqual(self.decide({"observedGeneration": 2, "ready": False}, generation=2)["phase"], "Updating")

    def test_recheck_keeps_the_stored_phase(self):
        st = {"phase": "Waiting", "phaseSince": NOW_SINCE, "observedGeneration": 1,
              "summary": "Waiting for a cluster: name the OpenShift cluster to deploy onto."}
        got = self.decide(st)
        self.assertEqual(got, {"phase": "Waiting", "phaseSince": NOW_SINCE, "summary": st["summary"]})

    def test_no_second_creating_write_and_the_job_keeps_the_start(self):
        got = self.decide(None)
        mark = load(os.path.join(COMMON, "mark_phase.yml"))[0]
        decided = run_play([mark, write_out("_mark_phase_value | trim")],
                           {"mark_phase_cr": {"status": {}}, "mark_phase_deleting": False,
                            "mark_phase_recorded": got["phase"]})
        self.assertEqual(decided, "Keep")
        # the job's first write (status library) derives Creating too and keeps phaseSince
        obj = {"metadata": {"name": "n", "generation": 1},
               "status": {"status": "reconciling", "ready": False, "phase": "Creating",
                          "phaseSince": got["phaseSince"], "summary": "Being created.",
                          "conditions": [{"type": "Ready", "status": "False", "reason": "AwaitingEDA"}]}}
        patch, _ = S.compute_status_patch(obj, {"status": "reconciling", "ready": False}, {"action": "reconciling"})
        self.assertEqual((patch["status"]["phase"], patch["status"]["phaseSince"]), ("Creating", got["phaseSince"]))


if __name__ == "__main__":
    unittest.main()

"""Unit tests of the status library (stdlib unittest).

Run: python3 -m unittest discover -s tests/unit -v
Optional baseline replay: SHC_BASELINE_OBJECTS=<dir with <plural>.json lists> (skipped when unset).
"""
import glob
import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)

import shc_status as S  # noqa: E402
import shc_status_reasons as R  # noqa: E402

NOW = "2026-10-21T12:00:00Z"
EARLY = "2026-10-20T08:00:00Z"


def obj(status=None, generation=1, rv="100", deleting=False, name="x"):
    meta = {"name": name, "namespace": "entity-t", "generation": generation, "resourceVersion": rv, "uid": "u-1"}
    if deleting:
        meta["deletionTimestamp"] = EARLY
    return {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "HybridNetwork", "metadata": meta,
            "status": status or {}}


class MergeConditions(unittest.TestCase):
    def test_keeps_transition_time_when_status_unchanged(self):
        old = [{"type": "Ready", "status": "True", "reason": "Ready", "message": "", "lastTransitionTime": EARLY}]
        out = S.merge_conditions(old, [{"type": "Ready", "status": True, "reason": "Ready", "lastTransitionTime": NOW}], NOW)
        self.assertEqual(out[0]["lastTransitionTime"], EARLY)
        self.assertEqual(out[0]["status"], "True")

    def test_status_change_resets_transition_time(self):
        old = [{"type": "Ready", "status": "True", "reason": "Ready", "lastTransitionTime": EARLY}]
        out = S.merge_conditions(old, [{"type": "Ready", "status": "False", "reason": "InvalidSpec"}], NOW)
        self.assertEqual(out[0]["lastTransitionTime"], NOW)

    def test_other_types_kept_legacy_dropped(self):
        old = [{"type": "Running", "status": "True"}, {"type": "Published", "status": "True", "reason": "Published"},
               {"type": "Failure", "status": "False"}, {"type": "Successful", "status": "True"}]
        out = S.merge_conditions(old, [{"type": "Ready", "status": "True"}], NOW)
        self.assertEqual([c["type"] for c in out], ["Published", "Ready"])

    def test_empty_reason_filled(self):
        out = S.merge_conditions([], [{"type": "Ready", "status": "False", "reason": ""}], NOW)
        self.assertTrue(out[0]["reason"])
        self.assertEqual(out[0]["message"], "")


class Synthesize(unittest.TestCase):
    def test_legacy_bodies(self):
        self.assertEqual(S.synthesize_ready({"status": "ready", "message": "ok"}, [])["status"], "True")
        failed = S.synthesize_ready({"status": "failed", "reason": "TeardownFailed"}, [])
        self.assertEqual((failed["status"], failed["reason"]), ("False", "TeardownFailed"))
        self.assertEqual(S.synthesize_ready({"status": "failed"}, [])["reason"], "Failed")
        self.assertEqual(S.synthesize_ready({"status": "pending"}, [])["reason"], "Waiting")
        self.assertEqual(S.synthesize_ready({"status": "reconciling"}, [])["reason"], "InProgress")

    def test_reconciling_never_flips_ready(self):
        old = [{"type": "Ready", "status": "True", "reason": "Ready"}]
        self.assertIsNone(S.synthesize_ready({"status": "reconciling"}, old))

    def test_body_ready_condition_wins(self):
        self.assertIsNone(S.synthesize_ready({"status": "ready", "conditions": [{"type": "Ready", "status": "True"}]}, []))


class PhaseCases(unittest.TestCase):
    def test_fixture_cases(self):
        path = os.path.join(ROOT, "tests", "fixtures", "status-phase-cases.json")
        with open(path) as fh:
            cases = json.load(fh)
        self.assertGreaterEqual(len(cases), 24)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(S.derive_phase(case["object"]), case["expect"]["phase"])
                self.assertIn(S.derive_phase(case["object"]), S.PHASES)


class Patch(unittest.TestCase):
    def test_superseded(self):
        o = obj({"delivery": {"eventId": "e2"}})
        patch, info = S.compute_status_patch(o, {"status": "ready"}, {"event_id": "e1"}, NOW)
        self.assertIsNone(patch)
        self.assertTrue(info["superseded"])

    def test_ack_then_complete(self):
        o = obj({"delivery": {"eventId": "e1"}, "conditions": [{"type": "Ready", "status": "True", "reason": "Ready",
                                                                  "lastTransitionTime": EARLY}],
                 "observedGeneration": 1, "phase": "Ready", "phaseSince": EARLY})
        patch, info = S.compute_status_patch(o, {"status": "reconciling", "ready": False, "message": "x"},
                                             {"event_id": "e1", "job": {"name": "j", "jobId": "7", "url": "u"}}, NOW)
        st = patch["status"]
        self.assertEqual(patch["metadata"]["resourceVersion"], "100")
        self.assertEqual(st["delivery"], {"acknowledgedEventId": "e1", "jobId": "7", "jobStartedAt": NOW})
        self.assertEqual(st["edaJobs"][0]["status"], "running")
        ready = S.find_condition(st["conditions"], "Ready")
        self.assertEqual((ready["status"], ready["lastTransitionTime"]), ("True", EARLY))
        self.assertEqual(S.find_condition(st["conditions"], "Reconciling")["reason"], "JobRunning")
        self.assertEqual((st["phase"], st["phaseSince"]), ("Ready", EARLY))
        # second reconciling write for the same event keeps jobStartedAt
        o["status"]["delivery"]["acknowledgedEventId"] = "e1"
        patch2, _ = S.compute_status_patch(o, {"status": "reconciling"}, {"event_id": "e1"}, NOW)
        self.assertNotIn("delivery", patch2["status"])
        done, _ = S.compute_status_patch(o, {"status": "ready", "ready": True}, {"event_id": "e1"}, NOW)
        self.assertEqual(done["status"]["delivery"], {"completedEventId": "e1", "completedAt": NOW})

    # review be-controller-status c2 F2: a re-run of a Ready object does not flip status.ready either
    READY = {"ready": True, "status": "ready", "message": "Network is ready.", "observedGeneration": 1,
             "conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "lastTransitionTime": EARLY}],
             "phase": "Ready", "phaseSince": EARLY, "delivery": {"eventId": "e2", "completedEventId": "e1"}}
    ACK = {"status": "reconciling", "ready": False, "message": "Applying the change."}

    def test_rerun_of_ready_object_keeps_legacy_ready(self):
        o = obj(json.loads(json.dumps(self.READY)))
        patch, info = S.compute_status_patch(o, dict(self.ACK), {"event_id": "e2"}, NOW)
        st = patch["status"]
        for key in ("ready", "status", "reason", "message"):
            self.assertNotIn(key, st)
        self.assertEqual(st["delivery"]["acknowledgedEventId"], "e2")
        self.assertEqual(S.find_condition(st["conditions"], "Ready")["status"], "True")
        rec = S.find_condition(st["conditions"], "Reconciling")
        self.assertEqual((rec["status"], rec["reason"]), ("True", "JobRunning"))
        self.assertEqual(info["phase"], "Ready")
        merged = S._merge_patch(o["status"], st)
        self.assertEqual((merged["ready"], merged["status"], merged["message"]), (True, "ready", "Network is ready."))

    def test_rerun_progress_message_goes_on_reconciling(self):
        o = obj(json.loads(json.dumps(self.READY)))
        patch, _ = S.compute_status_patch(o, {"status": "reconciling", "ready": False, "message": "Step 2 of 3."},
                                          {"event_id": "e2"}, NOW)
        self.assertEqual(S.find_condition(patch["status"]["conditions"], "Reconciling")["message"], "Step 2 of 3.")

    def test_spec_change_or_not_ready_still_shows_reconciling(self):
        stale = obj(json.loads(json.dumps(self.READY)), generation=2)
        not_ready = json.loads(json.dumps(self.READY))
        not_ready["conditions"][0].update(status="False", reason="AwaitingEDA")
        for o in (stale, obj(not_ready)):
            patch, _ = S.compute_status_patch(o, dict(self.ACK), {"event_id": "e2"}, NOW)
            self.assertEqual((patch["status"]["ready"], patch["status"]["status"]), (False, "reconciling"))

    def test_body_ready_condition_is_not_overridden(self):
        o = obj(json.loads(json.dumps(self.READY)))
        body = dict(self.ACK, conditions=[{"type": "Ready", "status": "False", "reason": "ProvisionFailed"}])
        patch, _ = S.compute_status_patch(o, body, {"event_id": "e2"}, NOW)
        self.assertIs(patch["status"]["ready"], False)
        self.assertEqual(S.find_condition(patch["status"]["conditions"], "Ready")["status"], "False")

    def test_keeps_ready(self):
        self.assertTrue(S.keeps_ready(obj(json.loads(json.dumps(self.READY)))))
        self.assertFalse(S.keeps_ready(obj(json.loads(json.dumps(self.READY)), generation=2)))
        self.assertFalse(S.keeps_ready(obj({"ready": True, "observedGeneration": 1})))  # no Ready condition
        self.assertFalse(S.keeps_ready(obj(json.loads(json.dumps(self.READY)), generation=0)))

    def test_partial_leaves_delivery_and_jobs(self):
        patch, _ = S.compute_status_patch(obj({}), {"waitingFor": "gw"}, {"event_id": "e1", "partial": True,
                                                                          "job": {"name": "j"}}, NOW)
        self.assertNotIn("delivery", patch["status"])
        self.assertNotIn("edaJobs", patch["status"])

    def test_null_passthrough(self):
        patch, _ = S.compute_status_patch(obj({"dns": {"dnsConfigSecret": "s", "zone": "z"}}),
                                          {"status": "ready", "dns": {"dnsConfigSecret": None}}, {}, NOW)
        self.assertIn("dnsConfigSecret", patch["status"]["dns"])
        self.assertIsNone(patch["status"]["dns"]["dnsConfigSecret"])
        self.assertIn('"dnsConfigSecret": null', json.dumps(patch))

    def test_failed_body_gets_ready_condition(self):
        patch, info = S.compute_status_patch(obj({"observedGeneration": 1}), {"status": "failed", "message": "boom"}, {}, NOW)
        ready = S.find_condition(patch["status"]["conditions"], "Ready")
        self.assertEqual((ready["status"], ready["reason"]), ("False", "Failed"))
        self.assertEqual(info["phase"], "Failed")
        self.assertNotIn("boom", patch["status"]["summary"])

    def test_teardown_give_up(self):
        o = obj({"retry": {"attempts": 9, "failingSince": EARLY}}, deleting=True)
        patch, info = S.compute_status_patch(o, {"status": "failed", "reason": "TeardownFailed"}, {"give_up_attempts": 10}, NOW)
        st = patch["status"]
        self.assertEqual(st["retry"], {"attempts": 10, "failingSince": EARLY, "lastFailureTime": NOW})
        self.assertEqual(S.find_condition(st["conditions"], "Stalled")["reason"], "TeardownFailed")
        self.assertEqual(info["phase"], "Deleting")

    def test_successful_teardown_after_give_up_clears_stalled(self):
        # GA2-F12: a teardown that succeeds after Stalled=True/TeardownFailed clears Stalled and the retry count
        stalled = {"type": "Stalled", "status": "True", "reason": "TeardownFailed", "message": "x",
                   "lastTransitionTime": EARLY}
        o = obj({"retry": {"attempts": 10, "failingSince": EARLY}, "conditions": [stalled]}, deleting=True)
        body = {"status": "ready", "ready": False, "deletionComplete": True, "message": "Deleted"}
        patch, info = S.compute_status_patch(o, body, {}, NOW)
        st = patch["status"]
        self.assertEqual(S.find_condition(st["conditions"], "Stalled")["status"], "False")
        self.assertIsNone(st["retry"])
        self.assertEqual(info["phase"], "Deleting")

    def test_finished_teardown_is_not_ready(self):
        # GA2-F7: the object awaiting its finalizer shows Ready=False/Deleted, ready false, phase Deleting
        ready = {"type": "Ready", "status": "True", "reason": "Ready", "message": "", "lastTransitionTime": EARLY}
        o = obj({"ready": True, "conditions": [ready]}, deleting=True)
        body = {"status": "ready", "ready": False, "deletionComplete": True, "message": "Deleted"}
        patch, info = S.compute_status_patch(o, body, {}, NOW)
        st = patch["status"]
        cond = S.find_condition(st["conditions"], "Ready")
        self.assertEqual((cond["status"], cond["reason"]), ("False", "Deleted"))
        self.assertIs(st["ready"], False)
        self.assertEqual(info["phase"], "Deleting")
        self.assertEqual(st["phase"], "Deleting")
        self.assertEqual(st["summary"], "Deleted; being removed.")

    def test_finished_teardown_overrides_a_ready_condition_in_the_body(self):
        o = obj({}, deleting=True)
        body = {"status": "ready", "deletionComplete": True,
                "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]}
        patch, _ = S.compute_status_patch(o, body, {}, NOW)
        self.assertEqual(S.find_condition(patch["status"]["conditions"], "Ready")["reason"], "Deleted")

    def test_teardown_wait_keeps_stalled(self):
        stalled = {"type": "Stalled", "status": "True", "reason": "TeardownFailed", "message": "x"}
        o = obj({"conditions": [stalled]}, deleting=True)
        patch, _ = S.compute_status_patch(o, {"status": "pending"}, {}, NOW)
        self.assertEqual(S.find_condition(patch["status"]["conditions"], "Stalled")["status"], "True")

    def test_pending_never_counts(self):
        o = obj({"retry": {"attempts": 3}}, deleting=True)
        patch, _ = S.compute_status_patch(o, {"status": "pending"}, {}, NOW)
        self.assertNotIn("retry", patch["status"])

    def test_not_deleting_failure_does_not_count(self):
        patch, _ = S.compute_status_patch(obj({}), {"status": "failed"}, {}, NOW)
        self.assertNotIn("retry", patch["status"])

    def test_legacy_conditions_removed_on_any_write(self):
        o = obj({"conditions": [{"type": "Running", "status": "True"}, {"type": "Successful", "status": "True"}]})
        patch, _ = S.compute_status_patch(o, {"waitingFor": "x"}, {"partial": True}, NOW)
        self.assertEqual([c["type"] for c in patch["status"]["conditions"]], [])

    def test_backfill_patch(self):
        o = obj({"ready": True, "observedGeneration": 1,
                 "conditions": [{"type": "Ready", "status": "True", "reason": "EDASucceeded"},
                                {"type": "Running", "status": "True"}]})
        patch = S.phase_backfill_patch(o, NOW)
        self.assertEqual(patch["status"]["phase"], "Ready")
        self.assertEqual([c["type"] for c in patch["status"]["conditions"]], ["Ready"])
        o["status"].update(patch["status"])
        self.assertIsNone(S.phase_backfill_patch(o, NOW))


class Registry(unittest.TestCase):
    def test_entries(self):
        for reason, entry in R.REASONS.items():
            with self.subTest(reason=reason):
                self.assertIn(entry["actor"], ("A", "T", "P"))
                self.assertIn(entry["class"], (R.READY, R.PROGRESS, R.WAITING, R.FAILED, R.BLOCKED))
                self.assertLessEqual(len(entry["summary"]), S.SUMMARY_MAX)
                self.assertIsNone(S.BANNED_SUMMARY.search(entry["summary"]), entry["summary"])
        for phase, text in R.PHASE_SUMMARIES.items():
            self.assertIn(phase, S.PHASES)
            self.assertIsNone(S.BANNED_SUMMARY.search(text))

    def test_reasons_used_in_code_are_registered(self):
        import re
        pat = re.compile(r"reason['\"]?:\s*['\"]?([A-Z][A-Za-z]+)")
        missing = set()
        for base in ("eda", "operator"):
            for path in glob.glob(os.path.join(ROOT, base, "**", "*.yml"), recursive=True):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for m in pat.finditer(fh.read()):
                        if R.lookup(m.group(1)) is None:
                            missing.add(m.group(1))
        self.assertEqual(sorted(missing), [])


@unittest.skipUnless(os.environ.get("SHC_BASELINE_OBJECTS"), "SHC_BASELINE_OBJECTS not set")
class BaselineReplay(unittest.TestCase):
    def test_replay(self):
        files = glob.glob(os.path.join(os.environ["SHC_BASELINE_OBJECTS"], "*.json"))
        count = 0
        for path in files:
            with open(path) as fh:
                data = json.load(fh)
            for item in data.get("items", []):
                for body, opts in (({}, {"partial": True}),
                                   ({"status": "ready", "ready": True, "message": "ok"}, {}),
                                   ({"status": "failed", "ready": False, "message": "x"}, {}),
                                   ({"status": "reconciling", "ready": False}, {"event_id": "e"})):
                    with self.subTest(obj="%s/%s" % (item["kind"], item["metadata"]["name"]), body=body):
                        patch, info = S.compute_status_patch(item, body, opts, NOW)
                        if patch is None:
                            self.assertTrue(info["superseded"])
                            continue
                        st = patch["status"]
                        for cond in st.get("conditions", []):
                            self.assertNotIn(cond["type"], S.LEGACY_TYPES)
                            self.assertTrue(cond["reason"])
                        self.assertIn(st["phase"], S.PHASES)
                        self.assertLessEqual(len(st["summary"]), S.SUMMARY_MAX)
                        self.assertIsNone(S.BANNED_SUMMARY.search(st["summary"]))
                        if body.get("status") == "reconciling" and S._cond_true(
                                (item.get("status") or {}).get("conditions"), "Ready"):
                            self.assertEqual(S.find_condition(st["conditions"], "Ready")["status"], "True")
                count += 1
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()

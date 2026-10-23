"""Status text of a delete stays put (PAC-F3, PAC-O1, P2AC-O3).

- PAC-F3: the finalizers write "Awaiting EDA delete" once per delete (not after the teardown finished, not once the
  delete event was published), so a requeue never replaces the teardown's message, and the designed waits ("Deletion
  waits for ...", "Waiting for EDA to complete deletion") never go through the failure write in the rescue.
- PAC-O1: the delete request's phase write also sets status.ready false and Ready=False/Deleting, so READY is never
  True next to STATUS Deleting (mark_phase.yml; the Entity's own first write likewise).
- P2AC-O3: a delete that waits for dependents records DeletionBlocked=True/WaitingForDependents, so the status
  library derives the same summary as the wait write; the wait's end clears it.
"""
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as S  # noqa: E402

COMMON = os.path.join(ROOT, "operator", "roles", "_common", "tasks")
WAITS = "(Deletion waits for |Waiting for EDA to complete deletion)"


def finalizer_files():
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "operator", "*", "roles", "*", "tasks", "main.yml"))):
        with open(path) as f:
            if "Awaiting EDA delete" in f.read():
                out.append(path)
    return out


def mark_task(tasks):
    return next(t for t in walk(tasks) if t.get("name", "").endswith("as awaiting EDA delete"))


class FinalizerWiring(unittest.TestCase):
    def test_every_finalizer_marks_once_per_delete(self):
        files = finalizer_files()
        self.assertGreaterEqual(len(files), 26)
        for path in files:
            when = mark_task(load(path)).get("when")
            self.assertIsInstance(when, list, path)
            text = " ".join(when)
            self.assertIn("deletionComplete", text, path)
            self.assertIn("DeleteRequested$", text, path)

    def test_no_rescue_marks_a_wait_as_failed(self):
        for path in finalizer_files():
            for top in load(path):
                for t in (top.get("rescue") or []) if isinstance(top, dict) else []:
                    if "operator_sdk.util.k8s_status" in t and t["name"].endswith("delete as failed"):
                        if "entity_finalizer" in path:
                            # the Entity's tier wait is guarded; its EDA wait runs outside the block (no rescue)
                            self.assertIn("Entity teardown waits for ", str(t.get("when", "")), path)
                            final = by_name(load(path), "Block finalizer until EDA completes deletion")
                            self.assertIn(final, load(path))
                        else:
                            self.assertIn(WAITS, str(t.get("when", "")), path)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class MarkOnce(unittest.TestCase):
    def marks(self, status):
        path = os.path.join(ROOT, "operator", "namespace", "roles", "team_delete", "tasks", "main.yml")
        cond = mark_task(load(path))["when"]
        return run_play([{"name": "decide", "ansible.builtin.set_fact": {"_go": True}, "when": cond},
                         write_out("_go | default(false)")],
                        {"_hybridsovereign_redhat_team": {"status": status}})

    def test_first_pass_marks(self):
        self.assertIs(self.marks(None), True)
        self.assertIs(self.marks({"delivery": {"reason": "TeamCreateRequested"}}), True)

    def test_requeue_after_publish_and_after_teardown_keeps_the_message(self):
        self.assertIs(self.marks({"delivery": {"reason": "TeamDeleteRequested", "eventId": "e"}}), False)
        self.assertIs(self.marks({"deletionComplete": True, "message": "Team t deleted by EDA"}), False)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class DeleteRequestNotReady(unittest.TestCase):
    def status_written(self, deleting):
        tasks = load(os.path.join(COMMON, "mark_phase.yml"))
        record = tasks[1]
        render = {"name": "render", "vars": record["vars"],
                  "ansible.builtin.set_fact": {"_st": record["operator_sdk.util.k8s_status"]["status"]}}
        return run_play([tasks[0], render, write_out("_st")],
                        {"mark_phase_cr": {"status": {"phase": "Ready", "ready": True}} if deleting else {},
                         "mark_phase_deleting": deleting})

    def test_deleting_write_drops_ready(self):
        st = self.status_written(True)
        self.assertEqual(st["phase"], "Deleting")
        self.assertIs(st["ready"], False)
        self.assertEqual([(c["type"], c["status"], c["reason"]) for c in st["conditions"]], [("Ready", "False", "Deleting")])

    def test_creating_write_is_unchanged(self):
        st = self.status_written(False)
        self.assertEqual(st["phase"], "Creating")
        self.assertNotIn("ready", st)
        self.assertNotIn("conditions", st)

    def test_entity_first_delete_write_drops_ready(self):
        path = os.path.join(ROOT, "operator", "primary", "roles", "entity_finalizer", "tasks", "main.yml")
        mark = mark_task(load(path))
        render = {"name": "render", "ansible.builtin.set_fact": {"_st": mark["operator_sdk.util.k8s_status"]["status"]}}
        got = run_play([render, write_out("_st")], {"entity_delete_transition_time": "2026-10-23T10:00:00.000000Z",
                                                    "_entity_was_deleting": False, "_entity_phase_since": "x"})
        self.assertEqual(got["conditions"][0]["reason"], "Deleting")
        self.assertIs(got["ready"], False)
        again = run_play([render, write_out("_st")], {"entity_delete_transition_time": "t",
                                                      "_entity_was_deleting": True, "_entity_phase_since": "x"})
        self.assertNotIn("conditions", again)  # a requeue keeps what later writes gave


class DependentsWait(unittest.TestCase):
    def test_wait_records_the_condition_the_library_reads(self):
        tasks = load(os.path.join(COMMON, "block_on_dependents.yml"))
        st = by_name(tasks, "Record what the delete is waiting for")["operator_sdk.util.k8s_status"]["status"]
        cond = st["conditions"][0]
        self.assertEqual((cond["type"], cond["status"], cond["reason"]), ("DeletionBlocked", "True", "WaitingForDependents"))
        # the summary the library derives for that state equals the one the wait writes (no alternation)
        obj = {"metadata": {"name": "n", "deletionTimestamp": "2026-10-23T10:00:00Z", "generation": 1},
               "status": {"phase": "Deleting", "summary": st["summary"],
                          "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}, dict(cond)]}}
        self.assertIsNone(S.phase_backfill_patch(obj))
        self.assertEqual(S.phase_fields(obj, obj["status"])["summary"], st["summary"])

    def test_end_of_the_wait_clears_it(self):
        tasks = load(os.path.join(COMMON, "block_on_dependents.yml"))
        clear = by_name(tasks, "Record that the delete no longer waits")
        cond = clear["operator_sdk.util.k8s_status"]["status"]["conditions"][0]
        self.assertEqual((cond["type"], cond["status"]), ("DeletionBlocked", "False"))
        self.assertIn("_dependents | length == 0", clear["when"])
        names = [t["name"] for t in tasks]
        self.assertLess(names.index("Record that the delete no longer waits"), names.index("Requeue until the dependents are gone"))
        obj = {"metadata": {"name": "n", "deletionTimestamp": "2026-10-23T10:00:00Z"},
               "status": {"phase": "Deleting", "conditions": [dict(cond, message="")]}}
        self.assertEqual(S.phase_fields(obj, obj["status"])["summary"], "Being deleted.")


if __name__ == "__main__":
    unittest.main()

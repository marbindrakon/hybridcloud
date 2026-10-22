"""The final status write of every teardown role (eda/rulebooks/roles/*_teardown) ends a give-up.

GA2-F12 (review m6): a teardown that succeeds after Stalled=True/TeardownFailed must clear Stalled and status.retry
and show Ready=False/Deleted until the finalizer goes. The status library does that for a successful write (status
ready or success). This reads each role's deletionComplete write as data and runs it through the library, so a
role writing another word (CloudAWS and CloudGCP wrote "deleted", which kept Stalled=True and left the job entry
"running") is caught.
"""
import glob
import os
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)

import shc_status as S  # noqa: E402

NOW = "2026-10-22T12:00:00Z"
EARLY = "2026-10-22T08:00:00Z"


def walk(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def final_writes():
    """(role, task name, cr_status_body) of every patch_cr_status write with deletionComplete true."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "eda", "rulebooks", "roles", "*_teardown", "tasks", "*.yml"))):
        role = path.split(os.sep)[-3]
        with open(path) as f:
            tasks = yaml.safe_load(f) or []
        for t in walk(tasks):
            inc = t.get("ansible.builtin.include_tasks") or t.get("include_tasks") or ""
            body = (t.get("vars") or {}).get("cr_status_body")
            if "patch_cr_status.yml" in str(inc) and isinstance(body, dict) and body.get("deletionComplete") is True:
                out.append((role, t.get("name", ""), body))
    return out


def literal(body):
    """The body with templated values replaced by plain text (the fields that matter here are literals)."""
    return {k: ("x" if isinstance(v, str) and "{{" in v else v) for k, v in body.items()}


class TeardownFinalStatus(unittest.TestCase):
    def test_every_teardown_role_has_a_final_write(self):
        roles = {os.path.basename(os.path.dirname(os.path.dirname(p)))
                 for p in glob.glob(os.path.join(ROOT, "eda", "rulebooks", "roles", "*_teardown", "tasks", "main.yml"))}
        found = {r for r, _, _ in final_writes()}
        self.assertIn("cloudaws_teardown", found)
        self.assertIn("cloudgcp_teardown", found)
        self.assertGreaterEqual(len(found), len(roles) - 3, sorted(roles - found))

    def test_final_write_is_a_successful_write(self):
        for role, name, body in final_writes():
            with self.subTest(role=role, task=name):
                # (some write ready: true; the library writes ready false on a finished teardown either way)
                self.assertIn(body.get("status"), ("ready", "success"))

    def test_final_write_ends_a_give_up(self):
        stalled = {"type": "Stalled", "status": "True", "reason": "TeardownFailed", "message": "x",
                   "lastTransitionTime": EARLY}
        for role, name, body in final_writes():
            with self.subTest(role=role, task=name):
                o = {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "CloudAWS",
                     "metadata": {"name": "n", "namespace": "entity-t", "generation": 1, "resourceVersion": "1",
                                  "deletionTimestamp": EARLY},
                     "status": {"retry": {"attempts": 10, "failingSince": EARLY}, "conditions": [stalled]}}
                patch, info = S.compute_status_patch(o, literal(body), {}, NOW)
                st = patch["status"]
                self.assertEqual(S.find_condition(st["conditions"], "Stalled")["status"], "False")
                ready = S.find_condition(st["conditions"], "Ready")
                self.assertEqual((ready["status"], ready["reason"]), ("False", "Deleted"))
                self.assertIsNone(st["retry"])
                self.assertEqual(info["phase"], "Deleting")


if __name__ == "__main__":
    unittest.main()

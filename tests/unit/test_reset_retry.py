"""Retry now on a deleting object resets the teardown retry count (operator/roles/_common/tasks/publish_operator_event.yml).

P2AC-F4: the reset wrote `status.retry: null` through operator_sdk.util.k8s_status, whose no-change check
(object_contains) calls .items() on every mapping it compares, so a stored retry mapping against None raised
AttributeError ("MODULE FAILURE" in status.message). `retry: {}` would pass that check but is a no-op in the merge
patch the module sends. The reset now nulls each counter field the status library writes. This test replays the
module's comparison and the RFC 7386 merge the API server applies (a copy of the upstream logic, kept small).
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT, by_name, load  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as S  # noqa: E402

PUBLISH = os.path.join(ROOT, "operator", "roles", "_common", "tasks", "publish_operator_event.yml")


def object_contains(obj, subset):
    """operator_sdk.util k8s_status K8sStatus.object_contains (replace_lists false)."""
    def dict_is_subset(o, s):
        return all(mapping.get(type(o.get(k)), mapping["default"])(o.get(k), v) for (k, v) in s.items())

    def list_is_subset(o, s):
        return all(item in o for item in s)

    mapping = {dict: dict_is_subset, list: list_is_subset, tuple: list_is_subset, "default": lambda o, s: o == s}
    return dict_is_subset(obj, subset)


def merge_patch(target, patch):
    """RFC 7386."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def k8s_status_patch(old_status, new_status):
    """The status the object has after k8s_status (patch mode); conditions are left out of this replay."""
    if object_contains(old_status, new_status):
        return old_status
    return merge_patch(old_status, new_status)


def reset_status():
    task = by_name(load(PUBLISH), "Reset the teardown retry count")
    st = copy.deepcopy(task["operator_sdk.util.k8s_status"]["status"])
    st.pop("conditions", None)
    return st


def stalled_status():
    """status.retry as the status library leaves it after three failed teardown jobs."""
    obj = {"metadata": {"name": "x", "deletionTimestamp": "2026-10-22T12:00:00Z"}, "status": {}}
    for _ in range(3):
        body = {"status": "failed", "ready": False}
        S.count_teardown_failure(obj, body, "failed", 10, "2026-10-22T12:00:00Z")
        obj["status"]["retry"] = body["retry"]
    return obj["status"]


class ResetRetry(unittest.TestCase):
    def test_replay_reproduces_the_old_crash(self):
        with self.assertRaises(AttributeError):
            k8s_status_patch(stalled_status(), {"retry": None})

    def test_empty_mapping_would_not_reset(self):
        self.assertEqual(k8s_status_patch(stalled_status(), {"retry": {}})["retry"]["attempts"], 3)

    def test_reset_clears_every_counter_field(self):
        before = stalled_status()
        reset = reset_status()
        self.assertEqual(set(reset["retry"]), set(before["retry"]))
        self.assertTrue(all(v is None for v in reset["retry"].values()))
        after = k8s_status_patch(before, reset)
        self.assertEqual(after["retry"], {})

    def test_next_failure_counts_from_one(self):
        after = k8s_status_patch(stalled_status(), reset_status())
        obj = {"metadata": {"name": "x", "deletionTimestamp": "2026-10-22T12:00:00Z"}, "status": after}
        body = {"status": "failed"}
        S.count_teardown_failure(obj, body, "failed", 10, "2026-10-22T13:00:00Z")
        self.assertEqual(body["retry"]["attempts"], 1)
        self.assertEqual(body["retry"]["failingSince"], "2026-10-22T13:00:00Z")

    def test_reset_on_an_object_without_a_count(self):
        after = k8s_status_patch({"phase": "Deleting"}, reset_status())
        self.assertEqual(after.get("retry"), {})
        self.assertEqual(after["phase"], "Deleting")

    def test_no_other_status_write_nulls_a_mapping(self):
        # the same crash class: a k8s_status write in the shared operator tasks that sets a mapping field to null
        common = os.path.join(ROOT, "operator", "roles", "_common", "tasks")
        for name in sorted(os.listdir(common)):
            if not name.endswith(".yml"):
                continue
            from play_helper import walk
            for t in walk(load(os.path.join(common, name)) or []):
                st = (t.get("operator_sdk.util.k8s_status") or {}).get("status")
                if isinstance(st, dict):
                    for k in ("retry", "delivery"):
                        self.assertIsNot(st.get(k, "absent"), None, "%s: %s sets status.%s to null" % (name, t.get("name"), k))


if __name__ == "__main__":
    unittest.main()

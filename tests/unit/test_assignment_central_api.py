"""The Assignment teardown reaches the hub the way the provision role does (H2AC-F1).

A fact set from the bare CENTRAL_API_SERVER environment variable was empty in the job and every ACM
delete went to a relative URL, so a provisioned Assignment could never finish its teardown.
"""
import os
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")


def _load(path):
    with open(path) as f:
        return yaml.safe_load(f)


class AssignmentCentralApiTest(unittest.TestCase):
    def test_teardown_default_equals_provision_default(self):
        prov = _load(os.path.join(ROLES, "assignment_provision", "defaults", "main.yml"))
        tear = _load(os.path.join(ROLES, "assignment_teardown", "defaults", "main.yml"))
        self.assertIn("central_api_server", tear)
        self.assertEqual(tear["central_api_server"], prov["central_api_server"])
        self.assertIn("CENTRAL_API_SERVER", tear["central_api_server"])
        self.assertIn(" or ", tear["central_api_server"], "the default needs a fallback for an unset variable")

    def test_teardown_sets_no_central_api_server_fact(self):
        main = os.path.join(ROLES, "assignment_teardown", "tasks", "main.yml")
        with open(main) as f:
            text = f.read()
        self.assertNotIn("central_api_server: \"{{ lookup('env', 'CENTRAL_API_SERVER') }}\"", text)
        for task in _walk(_load(main)):
            facts = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
            self.assertNotIn("central_api_server", facts, "a fact would override the role default")


def _walk(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            if key in t:
                yield from _walk(t[key])


if __name__ == "__main__":
    unittest.main()

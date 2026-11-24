"""The two shared pre-checks of the names rules, as the jobs run them.

eda/common/tasks/name_check.yml: the rule for names, before a job makes anything.
eda/common/tasks/environment_name_unique.yml: an environment's name is unique within its tenant across the four
environment kinds; the older object keeps it; a refused or fully deleted one does not hold it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

TASKS = os.path.join(ROOT, "eda", "common", "tasks")
UNIQUE = os.path.join(TASKS, "environment_name_unique.yml")
ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudoso_provision")


def env(kind, created, reason="", deleted=False, complete=False):
    o = {"kind": kind, "metadata": {"name": "prod", "namespace": "entity-acme", "creationTimestamp": created},
         "status": {"conditions": [{"type": "Ready", "status": "False", "reason": reason}] if reason else []}}
    if deleted:
        o["metadata"]["deletionTimestamp"] = "2030-01-01T00:00:00Z"
        o["status"]["deletionComplete"] = complete
    return o


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Unique(unittest.TestCase):
    def decide(self, cases):
        tasks = load(UNIQUE)
        steps = [by_name(tasks, "Decide whether an older environment holds the name"), by_name(tasks, "Keep the answer")]
        steps[0] = dict(steps[0])
        steps[0]["vars"] = dict(steps[0]["vars"], _enu_others="{{ {'results': [{'resources': case.others}]} }}",
                                _enu_self="{{ {'resources': [case.me | default({})]} }}",
                                enu_kind="{{ case.kind }}", enu_created="{{ case.created }}")
        steps[1] = dict(steps[1])
        return run_cases(steps, cases, "{'taken': enu_taken | bool, 'holder': enu_holder_kind}")

    def test_cases(self):
        mine = {"kind": "CloudOSO", "created": "2030-01-02T00:00:00Z"}
        cases = [
            dict(mine, others=[]),
            dict(mine, others=[env("CloudVirt", "2030-01-01T00:00:00Z")]),                       # older: holds it
            dict(mine, others=[env("CloudVirt", "2030-01-03T00:00:00Z")]),                       # younger: does not
            dict(mine, others=[env("CloudVirt", "2030-01-01T00:00:00Z", "EnvironmentNameTaken")]),  # itself refused
            dict(mine, others=[env("CloudAWS", "2030-01-01T00:00:00Z", deleted=True, complete=True)]),
            dict(mine, others=[env("CloudAWS", "2030-01-01T00:00:00Z", deleted=True, complete=False)]),
            dict(mine, others=[env("CloudAWS", "2030-01-02T00:00:00Z")]),                        # same second: kind decides
            dict(mine, others=[env("CloudVirt", "2030-01-02T00:00:00Z")]),
            dict(mine, others=[env("CloudGCP", "2030-01-01T12:00:00Z"), env("CloudVirt", "2030-01-01T00:00:00Z")]),
        ]
        self.assertEqual(self.decide(cases), [
            {"taken": False, "holder": ""},
            {"taken": True, "holder": "CloudVirt"},
            {"taken": False, "holder": ""},
            {"taken": False, "holder": ""},
            {"taken": False, "holder": ""},
            {"taken": True, "holder": "CloudAWS"},
            {"taken": True, "holder": "CloudAWS"},
            {"taken": False, "holder": ""},
            {"taken": True, "holder": "CloudVirt"},
        ])

    def test_a_refused_environment_stays_refused_while_another_holds_the_name(self):
        """After the upgrade: an older CloudVirt the release before refused (a CloudOSO of its name existed) does not
        take the name from the working CloudOSO; it gets the name once no other environment holds it."""
        refused_me = {"kind": "CloudVirt", "created": "2030-01-01T00:00:00Z",
                      "me": env("CloudVirt", "2030-01-01T00:00:00Z", "EnvironmentNameTaken")}
        working_me = {"kind": "CloudVirt", "created": "2030-01-01T00:00:00Z",
                      "me": env("CloudVirt", "2030-01-01T00:00:00Z", "Ready")}
        cases = [
            dict(refused_me, others=[env("CloudOSO", "2030-01-02T00:00:00Z", "Ready")]),          # younger, working
            dict(refused_me, others=[env("CloudOSO", "2030-01-02T00:00:00Z", "EnvironmentNameTaken")]),
            dict(refused_me, others=[env("CloudOSO", "2030-01-02T00:00:00Z", deleted=True, complete=True)]),
            dict(refused_me, others=[]),
            dict(working_me, others=[env("CloudOSO", "2030-01-02T00:00:00Z", "Ready")]),          # older keeps it
            # the older of two environments that worked before the upgrade keeps working, the younger is refused
            {"kind": "CloudAWS", "created": "2030-01-02T00:00:00Z", "me": env("CloudAWS", "2030-01-02T00:00:00Z", "Ready"),
             "others": [env("CloudOSO", "2030-01-01T00:00:00Z", "Ready")]},
            {"kind": "CloudOSO", "created": "2030-01-01T00:00:00Z", "me": env("CloudOSO", "2030-01-01T00:00:00Z", "Ready"),
             "others": [env("CloudAWS", "2030-01-02T00:00:00Z", "EnvironmentNameTaken")]},
        ]
        self.assertEqual(self.decide(cases), [
            {"taken": True, "holder": "CloudOSO"},
            {"taken": False, "holder": ""},
            {"taken": False, "holder": ""},
            {"taken": False, "holder": ""},
            {"taken": False, "holder": ""},
            {"taken": True, "holder": "CloudOSO"},
            {"taken": False, "holder": ""},
        ])

    def test_it_reads_its_own_state(self):
        t = by_name(load(UNIQUE), "Read this environment's own state")
        self.assertEqual((t["kubernetes.core.k8s_info"]["kind"], t["kubernetes.core.k8s_info"]["name"]),
                         ("{{ enu_kind }}", "{{ enu_name }}"))

    def test_it_asks_for_the_three_other_kinds_in_the_tenants_namespace(self):
        t = by_name(load(UNIQUE), "List the tenant's environments of the other kinds")
        self.assertEqual(t["kubernetes.core.k8s_info"]["namespace"], "{{ enu_namespace }}")
        self.assertEqual(t["kubernetes.core.k8s_info"]["name"], "{{ enu_name }}")
        self.assertIn("difference([enu_kind])", t["loop"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Rule(unittest.TestCase):
    def test_the_job_gets_the_sentence_or_nothing(self):
        steps = [{"ansible.builtin.include_tasks": os.path.join(TASKS, "name_check.yml"),
                  "vars": {"name_check_items": "{{ case }}", "role_path": ROLE}}]
        got = run_cases(steps, [
            [{"value": "acme", "what": "tenant"}, {"value": "prod", "what": "environment"}],
            [{"value": "acme", "what": "tenant"}, {"value": "web.prod", "what": "environment"}],
            [{"value": "g" * 22, "what": "gateway", "limit": 21}],
        ], "name_check_message")
        self.assertEqual(got[0], "")
        self.assertIn("environment", got[1])
        self.assertIn("web.prod", got[1])
        self.assertIn("21", got[2])


if __name__ == "__main__":
    unittest.main()

"""Stored phase of the Entity kind at the create and the delete request (operator/primary/roles/entity and
entity_finalizer).

OD42 (PAC-F2): the first status write of a new Entity carries phase Creating (before the namespace operator is
deployed); the first write of the finalizer at the delete request carries phase Deleting with the summary, and a
requeue keeps the start time and the summary a later write gave. A finished teardown (deletionComplete) releases the
finalizer without another status write. The expressions run in ansible-playbook (skipped when absent); the wiring
checks always run.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTITY = os.path.join(ROOT, "operator", "primary", "roles", "entity", "tasks", "main.yml")
FINALIZER = os.path.join(ROOT, "operator", "primary", "roles", "entity_finalizer", "tasks", "main.yml")
COMMON = os.path.join(ROOT, "operator", "roles", "_common", "tasks")


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def block_tasks(path):
    return next(t for t in load(path) if "block" in t)["block"]


def by_name(tasks, name):
    return next(t for t in tasks if t.get("name") == name)


def run_play(tasks, extra_vars):
    """Run tasks on localhost and return the JSON the play's last task wrote to {{ out }}."""
    tmp = tempfile.mkdtemp(prefix="shc-ep-test-")
    try:
        out = os.path.join(tmp, "out.json")
        with open(os.path.join(tmp, "tasks.yml"), "w") as f:
            yaml.safe_dump(tasks, f)
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent('''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: tasks.yml
                '''))
        with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump(dict(extra_vars, out=out), f)
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                              cwd=tmp, env=env, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-2000:])
        with open(out) as f:
            return json.load(f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_out(expr):
    return {"ansible.builtin.copy": {"dest": "{{ out }}", "content": "{{ %s | to_json }}" % expr}}


class EntityCreateWiring(unittest.TestCase):
    def test_creating_is_written_before_the_namespace_operator_deployment(self):
        tasks = block_tasks(ENTITY)
        names = [t.get("name") for t in tasks]
        first_status = next(i for i, t in enumerate(tasks) if "operator_sdk.util.k8s_status" in t)
        self.assertEqual(names[first_status], "Record phase Creating on a new Entity")
        self.assertLess(first_status, names.index("Deploy per-entity namespace operator"))
        record = tasks[first_status]
        status = record["operator_sdk.util.k8s_status"]["status"]
        self.assertEqual(status["phase"], "Creating")
        self.assertEqual(status["summary"], "Being created.")
        self.assertIn("phaseSince", status)
        self.assertFalse(status["ready"])
        ready = next(c for c in status["conditions"] if c["type"] == "Ready")
        self.assertEqual((ready["status"], ready["reason"]), ("False", "AwaitingEDA"))
        self.assertEqual(record["when"], "entity_is_new | bool")

    def test_publish_is_told_the_recorded_phase(self):
        publish = by_name(block_tasks(ENTITY), "Publish operator event to AMQ Streams")
        self.assertIn("operator_event_phase_recorded", publish["vars"])
        self.assertIn("entity_is_new", publish["vars"]["operator_event_phase_recorded"])
        with open(os.path.join(COMMON, "publish_operator_event.yml")) as f:
            mark = by_name(yaml.safe_load(f), "Record the phase of a new or deleting object")
        self.assertIn("operator_event_phase_recorded", mark["vars"]["mark_phase_recorded"])


class FinalizerWiring(unittest.TestCase):
    def test_finished_teardown_exits_before_any_write(self):
        tasks = load(FINALIZER)
        self.assertEqual(tasks[0].get("ansible.builtin.meta"), "end_host")
        self.assertIn("deletionComplete", tasks[0]["when"])
        self.assertIn("block", tasks[1])
        # no second (dead) exit after the block
        self.assertEqual(sum(1 for t in tasks if t.get("ansible.builtin.meta") == "end_host"), 1)

    def test_first_write_at_the_delete_request_is_deleting(self):
        tasks = block_tasks(FINALIZER)
        first_status = next(t for t in tasks if "operator_sdk.util.k8s_status" in t)
        self.assertEqual(first_status["name"], "Mark Entity as awaiting EDA delete")
        self.assertIn("'phase': 'Deleting'", first_status["operator_sdk.util.k8s_status"]["status"])

    def test_cascade_done_is_written_once(self):
        done = by_name(block_tasks(FINALIZER), "Record that the cascade is done")
        self.assertIn("_entity_was_deleting", done["when"])
        self.assertIn("_entity_was_blocked", done["when"])


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class EntityNewDecision(unittest.TestCase):
    def is_new(self, status):
        facts = by_name(block_tasks(ENTITY), "Set event action and reason")
        task = {"name": "decide", "ansible.builtin.set_fact": {
            "entity_is_new": facts["ansible.builtin.set_fact"]["entity_is_new"]}}
        cr = {"metadata": {"name": "t"}, "spec": {}, "status": status}
        return run_play([task, write_out("entity_is_new | bool")], {"_hybridsovereign_redhat_entity": cr})

    def test_new_entity(self):
        self.assertTrue(self.is_new(None))
        self.assertTrue(self.is_new({"conditions": [{"type": "Ready", "status": "False", "reason": "AwaitingEDA"}]}))

    def test_existing_entity(self):
        self.assertFalse(self.is_new({"phase": "Creating"}))
        self.assertFalse(self.is_new({"phase": "Ready", "observedGeneration": 1, "ready": True}))
        self.assertFalse(self.is_new({"observedGeneration": 2, "ready": True}))  # from before stored phases


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class FinalizerFirstWrite(unittest.TestCase):
    NOW = "2026-10-22T21:00:00.000000Z"

    def first_write(self, status):
        tasks = block_tasks(FINALIZER)
        decide = by_name(tasks, "Decide the phase start")
        mark = by_name(tasks, "Mark Entity as awaiting EDA delete")
        render = {"name": "render", "ansible.builtin.set_fact": {
            "_rendered": mark["operator_sdk.util.k8s_status"]["status"]}}
        cr = {"metadata": {"name": "t"}, "spec": {}, "status": status}
        return run_play([decide, render, write_out("{'status': _rendered, 'blocked': _entity_was_blocked | bool}")],
                        {"_hybridsovereign_redhat_entity": cr, "entity_delete_transition_time": self.NOW})

    def test_delete_request_of_a_ready_entity(self):
        got = self.first_write({"phase": "Ready", "ready": True, "summary": "Ready."})
        st = got["status"]
        self.assertEqual(st["phase"], "Deleting")
        self.assertEqual(st["phaseSince"], self.NOW)
        self.assertEqual(st["summary"], "Being deleted.")
        self.assertFalse(st["ready"])
        self.assertFalse(got["blocked"])

    def test_requeue_keeps_start_and_summary(self):
        got = self.first_write({"phase": "Deleting", "phaseSince": "2026-10-22T20:00:00Z",
                                "summary": "Deletion waits: some objects have deletion protection turned on.",
                                "conditions": [{"type": "DeletionBlocked", "status": "True",
                                                "reason": "ProtectedChildren"}]})
        st = got["status"]
        self.assertEqual(st["phase"], "Deleting")
        self.assertEqual(st["phaseSince"], "2026-10-22T20:00:00Z")
        self.assertNotIn("summary", st)
        self.assertTrue(got["blocked"])


if __name__ == "__main__":
    unittest.main()

"""Stored phase at the delete request and on a new object's first write (operator/roles/_common/tasks/mark_phase.yml).

OD42 (GA2-F3, GA2-F10): a new object is Creating from the operator's first status write; an object being deleted
is Deleting from the delete request, also while the delete waits for its dependents. An edit or a re-run of an
existing object does not change the phase (the jobs write it). The decision task runs in ansible-playbook (skipped
when absent); the wiring checks always run.
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
COMMON = os.path.join(ROOT, "operator", "roles", "_common", "tasks")


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class DecideTest(unittest.TestCase):
    def decide(self, status, deleting=False):
        with open(os.path.join(COMMON, "mark_phase.yml")) as f:
            decide_task = yaml.safe_load(f)[0]
        tmp = tempfile.mkdtemp(prefix="shc-mp-test-")
        try:
            with open(os.path.join(tmp, "decide.yml"), "w") as f:
                yaml.safe_dump([decide_task], f)
            play = textwrap.dedent('''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: decide.yml
                    - ansible.builtin.copy:
                        dest: "{{ out }}"
                        content: "{{ {'phase': _mark_phase_value | trim} | to_json }}"
                ''')
            with open(os.path.join(tmp, "play.yml"), "w") as f:
                f.write(play)
            with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
                f.write("[defaults]\n")
            out = os.path.join(tmp, "out.json")
            with open(os.path.join(tmp, "vars.json"), "w") as f:
                json.dump({"mark_phase_cr": {"status": status}, "mark_phase_deleting": deleting, "out": out}, f)
            env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                       ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                       ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                       ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
            proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                                  cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
            with open(out) as f:
                return json.load(f)["phase"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_new_object_is_creating(self):
        self.assertEqual(self.decide(None), "Creating")
        self.assertEqual(self.decide({"conditions": [{"type": "Ready", "status": "False", "reason": "AwaitingEDA"}]}),
                         "Creating")

    def test_existing_object_keeps_its_phase(self):
        self.assertEqual(self.decide({"phase": "Ready", "observedGeneration": 1, "ready": True}), "Keep")
        self.assertEqual(self.decide({"phase": "Waiting"}), "Keep")
        # an object from before stored phases (handled, no phase): not a create
        self.assertEqual(self.decide({"observedGeneration": 2, "ready": True}), "Keep")
        self.assertEqual(self.decide({"realized": True}), "Keep")

    def test_delete_request_is_deleting_once(self):
        self.assertEqual(self.decide({"phase": "Ready", "ready": True}, deleting=True), "Deleting")
        self.assertEqual(self.decide({}, deleting=True), "Deleting")
        self.assertEqual(self.decide({"phase": "Deleting"}, deleting=True), "Keep")


class WiringTest(unittest.TestCase):
    def test_publish_records_the_phase_before_deciding(self):
        with open(os.path.join(COMMON, "publish_operator_event.yml")) as f:
            text = f.read()
        self.assertIn("include_tasks: mark_phase.yml", text)
        self.assertLess(text.index("include_tasks: mark_phase.yml"), text.index("- name: Decide whether to publish"))

    def test_dependents_wait_is_deleting(self):
        with open(os.path.join(COMMON, "block_on_dependents.yml")) as f:
            tasks = yaml.safe_load(f)
        record = next(t for t in tasks if t.get("name") == "Record what the delete is waiting for")
        status = record["operator_sdk.util.k8s_status"]["status"]
        self.assertEqual(status["phase"], "Deleting")
        self.assertIn("phaseSince", status)


if __name__ == "__main__":
    unittest.main()

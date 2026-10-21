"""Retry now (operator/roles/_common/tasks/reconcile_request.yml): which annotation values are a new request.

A timestamp is a request while it differs from status.lastHandledReconcileRequest. The legacy literal "true"
(older dashboards, runbooks) is a request every time; publish_operator_event removes it after publishing.
Skipped when ansible-playbook is absent.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TASK = os.path.join(ROOT, "operator", "roles", "_common", "tasks", "reconcile_request.yml")
ANN = "ansible.sdk.operatorframework.io/reconcileNow"


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class ReconcileRequestTest(unittest.TestCase):
    def decide(self, annotation, handled):
        tmp = tempfile.mkdtemp(prefix="shc-rr-test-")
        try:
            cr = {"metadata": {"annotations": {} if annotation is None else {ANN: annotation}},
                  "status": {} if handled is None else {"lastHandledReconcileRequest": handled}}
            play = textwrap.dedent('''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: "{{ task_file }}"
                    - ansible.builtin.copy:
                        dest: "{{ out }}"
                        content: "{{ {'now': reconcile_now | bool, 'legacy': reconcile_request_legacy | bool} | to_json }}"
                ''')
            with open(os.path.join(tmp, "play.yml"), "w") as f:
                f.write(play)
            with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
                f.write("[defaults]\n")
            out = os.path.join(tmp, "out.json")
            with open(os.path.join(tmp, "vars.json"), "w") as f:
                json.dump({"task_file": TASK, "reconcile_request_cr": cr, "out": out}, f)
            env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                       ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                       ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                       ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
            proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                                  cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
            with open(out) as f:
                return json.load(f)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_annotation(self):
        self.assertEqual(self.decide(None, None), {"now": False, "legacy": False})

    def test_new_timestamp(self):
        self.assertEqual(self.decide("2026-10-21T16:00:00Z", "2026-10-21T15:00:00Z"), {"now": True, "legacy": False})

    def test_handled_timestamp(self):
        self.assertEqual(self.decide("2026-10-21T16:00:00Z", "2026-10-21T16:00:00Z"), {"now": False, "legacy": False})

    def test_legacy_true_is_always_a_request(self):
        self.assertEqual(self.decide("true", None), {"now": True, "legacy": True})
        self.assertEqual(self.decide("true", "true"), {"now": True, "legacy": True})
        self.assertEqual(self.decide("True", "True"), {"now": True, "legacy": True})


if __name__ == "__main__":
    unittest.main()

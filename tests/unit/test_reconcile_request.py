"""Retry now (operator/roles/_common/tasks/reconcile_request.yml): which annotation values are a new request.

A timestamp is a request while it differs from status.lastHandledReconcileRequest. The legacy literal "true"
(older dashboards, runbooks) is a request every time; publish_operator_event removes it after publishing.
reconcile_keeps_ready: a run on a Ready object whose spec was already processed keeps Ready, and every
operator role then writes Reconciling=True/Queued instead of Ready=False/AwaitingEDA (UX-062).
Skipped when ansible-playbook is absent (the role scan always runs).
"""
import glob
import re
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
        cr = {"metadata": {"annotations": {} if annotation is None else {ANN: annotation}},
              "status": {} if handled is None else {"lastHandledReconcileRequest": handled}}
        out = self.run_task(cr)
        return {"now": out["now"], "legacy": out["legacy"]}

    def keeps_ready(self, status, generation=2):
        cr = {"metadata": {"generation": generation, "annotations": {ANN: "true"}}, "status": status}
        return self.run_task(cr)["keeps"]

    def run_task(self, cr):
        tmp = tempfile.mkdtemp(prefix="shc-rr-test-")
        try:
            play = textwrap.dedent('''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: "{{ task_file }}"
                    - ansible.builtin.copy:
                        dest: "{{ out }}"
                        content: "{{ {'now': reconcile_now | bool, 'legacy': reconcile_request_legacy | bool, 'keeps': reconcile_keeps_ready | bool} | to_json }}"
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

    def test_rerun_of_ready_object_keeps_ready(self):
        ready = {"type": "Ready", "status": "True", "reason": "Ready"}
        self.assertTrue(self.keeps_ready({"ready": True, "observedGeneration": 2, "conditions": [ready]}))
        # a Ready condition decides over status.ready
        self.assertTrue(self.keeps_ready({"ready": False, "observedGeneration": 2, "conditions": [ready]}))
        # no Ready condition: status.ready
        self.assertTrue(self.keeps_ready({"ready": True, "observedGeneration": 3}))

    def test_create_spec_change_or_not_ready_flips(self):
        ready = {"type": "Ready", "status": "True", "reason": "Ready"}
        not_ready = {"type": "Ready", "status": "False", "reason": "ProvisionFailed"}
        self.assertFalse(self.keeps_ready({}))
        self.assertFalse(self.keeps_ready({"ready": True, "observedGeneration": 1, "conditions": [ready]}))
        self.assertFalse(self.keeps_ready({"ready": True, "observedGeneration": 2, "conditions": [not_ready]}))
        self.assertFalse(self.keeps_ready({"ready": False, "observedGeneration": 2}))
        self.assertFalse(self.keeps_ready({"ready": True}, generation=None))


MARK = re.compile(r"^(\s*)- name: Mark (\w+) as reconciling$")


class RerunKeepsReadyRolesTest(unittest.TestCase):
    """Every operator role that marks its CR as reconciling guards the Ready=False write (UX-062)."""

    def test_roles_guard_the_ready_flip(self):
        files = sorted(glob.glob(os.path.join(ROOT, "operator", "*", "roles", "*", "tasks", "main.yml")))
        seen = 0
        for path in files:
            with open(path) as f:
                text = f.read()
            if "tasks_from: reconcile_request" not in text:
                continue
            lines = text.split("\n")
            for i, line in enumerate(lines):
                m = MARK.match(line)
                if not m:
                    continue
                seen += 1
                ind, kind = m.group(1), m.group(2)
                j = i + 1
                while j < len(lines) and lines[j].strip() and not lines[j].startswith(ind + "- "):
                    j += 1
                block = "\n".join(lines[i:j])
                rel = os.path.relpath(path, ROOT)
                self.assertIn("when: not (reconcile_keeps_ready | default(false) | bool)", block, rel)
                queued = text.split("- name: Mark %s as queued for a re-run" % kind, 1)
                self.assertEqual(len(queued), 2, rel)
                qblock = queued[1].split("\n" + ind + "- ", 1)[0]
                self.assertIn("type: Reconciling", qblock, rel)
                self.assertIn("reason: Queued", qblock, rel)
                self.assertNotIn("ready:", qblock, rel)
                self.assertIn("when: reconcile_keeps_ready | default(false) | bool", qblock, rel)
                self.assertLess(text.index("tasks_from: reconcile_request"), text.index(line), rel)
        self.assertGreaterEqual(seen, 28)


if __name__ == "__main__":
    unittest.main()

"""Runs eda/common/tasks/acknowledge_event.yml with stubbed modules (no cluster).

The object read (hybridsovereign.shc.cr_status), the namespace read (kubernetes.core.k8s_info), the services
connection and the status writer are stubbed; each case says what the API returns and checks whether the job
continues, with which input, and whether it writes the acknowledgement. Skipped when ansible-playbook is absent.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ACK = os.path.join(ROOT, "eda", "common", "tasks", "acknowledge_event.yml")

STUB_MODULE = textwrap.dedent('''\
    #!/usr/bin/python
    # WANT_JSON
    import json, os
    with open(os.environ["SHC_ACK_FIXTURE"]) as f:
        fx = json.load(f)
    out = dict(fx["%s"], changed=False)
    print(json.dumps(out))
    ''')

PLAYBOOK = textwrap.dedent('''\
    - hosts: localhost
      connection: local
      gather_facts: false
      tasks:
        - ansible.builtin.include_tasks: tasks/acknowledge_event.yml
        - ansible.builtin.copy:
            dest: "{{ out_dir }}/result.json"
            content: "{{ {'released': shc_object_released, 'spec': shc_live_spec, 'cr': shc_live_cr} | to_json }}"
    ''')

RESOLVE = textwrap.dedent('''\
    - ansible.builtin.set_fact:
        services_api_host: https://api.example.test:6443
        services_bearer_token: stub
    ''')

PATCH = textwrap.dedent('''\
    - ansible.builtin.copy:
        dest: "{{ out_dir }}/patched.json"
        content: "{{ cr_status_body | to_json }}"
    ''')

LIVE = {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "Rbac",
        "metadata": {"name": "r1", "namespace": "entity-uxr-corp", "uid": "u-1"},
        "spec": {"config": "default", "name": "r1-live"}}


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class AcknowledgeEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shc-ack-test-")
        os.makedirs(os.path.join(self.tmp, "tasks"))
        shutil.copy(ACK, os.path.join(self.tmp, "tasks", "acknowledge_event.yml"))
        for name, body in (("resolve_services_cluster_connection.yml", RESOLVE), ("patch_cr_status.yml", PATCH)):
            with open(os.path.join(self.tmp, "tasks", name), "w") as f:
                f.write(body)
        for coll, key in ((("hybridsovereign", "shc"), "cr_status"), (("kubernetes", "core"), "k8s_info")):
            mdir = os.path.join(self.tmp, "collections", "ansible_collections", *coll, "plugins", "modules")
            os.makedirs(mdir)
            with open(os.path.join(mdir, key + ".py"), "w") as f:
                f.write(STUB_MODULE % key)
            with open(os.path.join(self.tmp, "collections", "ansible_collections", *coll, "galaxy.yml"), "w") as f:
                f.write("namespace: %s\nname: %s\nversion: 0.0.1\n" % coll)
        with open(os.path.join(self.tmp, "play.yml"), "w") as f:
            f.write(PLAYBOOK)
        with open(os.path.join(self.tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ack(self, payload, resource, namespaces):
        fixture = {"cr_status": {"resource": resource}, "k8s_info": {"resources": namespaces}}
        with open(os.path.join(self.tmp, "fixture.json"), "w") as f:
            json.dump(fixture, f)
        with open(os.path.join(self.tmp, "vars.json"), "w") as f:
            json.dump({"event_payload": payload, "out_dir": self.tmp}, f)
        env = dict(os.environ, SHC_ACK_FIXTURE=os.path.join(self.tmp, "fixture.json"),
                   ANSIBLE_COLLECTIONS_PATH=os.path.join(self.tmp, "collections"),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_CONFIG=os.path.join(self.tmp, "ansible.cfg"),
                   ANSIBLE_REMOTE_TMP=os.path.join(self.tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(self.tmp, "local-tmp"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                              cwd=self.tmp, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        out = {}
        for name in ("result", "patched"):
            path = os.path.join(self.tmp, name + ".json")
            out[name] = None
            if os.path.exists(path):
                with open(path) as f:
                    out[name] = json.load(f)
        return out

    def payload(self, reason="RbacDeleteRequested", note=None, uid="u-1"):
        return {"eventId": "e-1", "reason": reason,
                "note": json.dumps({"config": "default", "name": "r1-note"}) if note is None else note,
                "regarding": {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "Rbac",
                              "name": "r1", "namespace": "entity-uxr-corp", "uid": uid}}

    def test_live_object_is_the_input_and_is_acknowledged(self):
        out = self.run_ack(self.payload(reason="RbacCreateRequested"), LIVE, [])
        self.assertFalse(out["result"]["released"])
        self.assertEqual(out["result"]["spec"]["name"], "r1-live")
        self.assertEqual(out["patched"]["status"], "reconciling")
        self.assertNotIn("reason", out["patched"])  # the Reconciling condition carries JobRunning

    def test_live_delete_uses_live_spec_not_note(self):
        out = self.run_ack(self.payload(), LIVE, [{"status": {"phase": "Active"}}])
        self.assertFalse(out["result"]["released"])
        self.assertEqual(out["result"]["spec"]["name"], "r1-live")

    def test_other_uid_ends_the_job(self):
        out = self.run_ack(self.payload(uid="u-other"), LIVE, [])
        self.assertIsNone(out["result"])
        self.assertIsNone(out["patched"])

    def test_gone_object_in_active_namespace_ends_the_job(self):
        out = self.run_ack(self.payload(), None, [{"status": {"phase": "Active"}}])
        self.assertIsNone(out["result"])

    def test_gone_object_non_delete_event_ends_the_job(self):
        out = self.run_ack(self.payload(reason="RbacCreateRequested"), None, [{"status": {"phase": "Terminating"}}])
        self.assertIsNone(out["result"])

    def test_released_object_in_terminating_namespace_runs_from_note(self):
        out = self.run_ack(self.payload(), None, [{"status": {"phase": "Terminating"}}])
        self.assertTrue(out["result"]["released"])
        self.assertEqual(out["result"]["spec"], {"config": "default", "name": "r1-note"})
        self.assertEqual(out["result"]["cr"], {})
        self.assertIsNone(out["patched"])

    def test_released_object_with_namespace_gone_runs_from_note(self):
        out = self.run_ack(self.payload(), None, [])
        self.assertTrue(out["result"]["released"])

    def test_released_object_without_usable_note_ends_the_job(self):
        for note in ("", "not json", "[]"):
            out = self.run_ack(self.payload(note=note), None, [{"status": {"phase": "Terminating"}}])
            self.assertIsNone(out["result"], note)


if __name__ == "__main__":
    unittest.main()

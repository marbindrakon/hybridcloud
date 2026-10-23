"""Which integration connection an object uses (resolve_integration_config.yml, EDA and operator copies).

The connection list (kubernetes.core.k8s_info) is stubbed; each case says which RbacConfigs exist, what
the object names in its spec and records in status.config, and checks the connection the task resolves,
where it came from, and whether the list was read. Both copies of the task file must behave the same.
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
COPIES = {
    "eda": os.path.join(ROOT, "eda", "common", "tasks", "resolve_integration_config.yml"),
    "operator": os.path.join(ROOT, "operator", "roles", "_common", "tasks", "resolve_integration_config.yml"),
}

STUB_MODULE = textwrap.dedent('''\
    #!/usr/bin/python
    # WANT_JSON
    import json, os, sys
    with open(os.environ["SHC_RIC_FIXTURE"]) as f:
        fx = json.load(f)
    with open(os.environ["SHC_RIC_CALLS"], "a") as f:
        f.write("list\\n")
    print(json.dumps({"resources": fx["resources"], "changed": False}))
    ''')

PLAYBOOK = textwrap.dedent('''\
    - hosts: localhost
      connection: local
      gather_facts: false
      tasks:
        - ansible.builtin.include_tasks: "{{ task_file }}"
          vars:
            resolve_config_kind: RbacConfig
            resolve_config_name: "{{ spec_name }}"
            resolve_config_recorded: "{{ recorded }}"
            resolve_config_namespace: integrations
            resolve_config_required: "{{ required }}"
            resolve_config_teardown: "{{ teardown }}"
        - ansible.builtin.copy:
            dest: "{{ out_dir }}/result.json"
            content: "{{ {'name': resolved_config_name, 'source': resolved_config_source} | to_json }}"
    ''')


def cfg(name, default=False, deleting=False):
    meta = {"name": name, "namespace": "integrations"}
    if default:
        meta["labels"] = {"hybridsovereign.redhat/default": "true"}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-21T00:00:00Z"
    return {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "RbacConfig", "metadata": meta}


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class ResolveIntegrationConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shc-ric-test-")
        mdir = os.path.join(self.tmp, "collections", "ansible_collections", "kubernetes", "core", "plugins", "modules")
        os.makedirs(mdir)
        with open(os.path.join(mdir, "k8s_info.py"), "w") as f:
            f.write(STUB_MODULE)
        with open(os.path.join(self.tmp, "collections", "ansible_collections", "kubernetes", "core", "galaxy.yml"), "w") as f:
            f.write("namespace: kubernetes\nname: core\nversion: 0.0.1\n")
        with open(os.path.join(self.tmp, "play.yml"), "w") as f:
            f.write(PLAYBOOK)
        with open(os.path.join(self.tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def resolve(self, copy, configs, spec="", recorded="", teardown=False, required=True):
        calls = os.path.join(self.tmp, "calls.txt")
        result = os.path.join(self.tmp, "result.json")
        for path in (calls, result):
            if os.path.exists(path):
                os.remove(path)
        with open(os.path.join(self.tmp, "fixture.json"), "w") as f:
            json.dump({"resources": configs}, f)
        with open(os.path.join(self.tmp, "vars.json"), "w") as f:
            json.dump({"task_file": COPIES[copy], "spec_name": spec, "recorded": recorded, "teardown": teardown,
                       "required": required, "out_dir": self.tmp,
                       "services_api_host": "https://api.example.test:6443", "services_bearer_token": "stub"}, f)
        env = dict(os.environ, SHC_RIC_FIXTURE=os.path.join(self.tmp, "fixture.json"), SHC_RIC_CALLS=calls,
                   ANSIBLE_COLLECTIONS_PATH=os.path.join(self.tmp, "collections"),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_CONFIG=os.path.join(self.tmp, "ansible.cfg"),
                   ANSIBLE_REMOTE_TMP=os.path.join(self.tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(self.tmp, "local-tmp"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                              cwd=self.tmp, env=env, capture_output=True, text=True, timeout=300)
        listed = os.path.exists(calls)
        if proc.returncode != 0:
            return {"failed": True, "listed": listed, "output": proc.stdout[-3000:] + proc.stderr[-2000:]}
        with open(result) as f:
            out = json.load(f)
        out.update(failed=False, listed=listed)
        return out

    def check(self, configs, expect, **kw):
        for copy in COPIES:
            with self.subTest(copy=copy):
                out = self.resolve(copy, configs, **kw)
                self.assertFalse(out["failed"], out.get("output"))
                got = {k: out[k] for k in expect}
                self.assertEqual(got, expect)

    # --- provision and reconcile ---
    def test_named_connection_wins_without_a_list(self):
        self.check([cfg("a"), cfg("b")], {"name": "b", "source": "spec", "listed": False}, spec="b", recorded="a")

    def test_only_connection_is_the_default(self):
        self.check([cfg("a")], {"name": "a", "source": "default", "listed": True})

    def test_labelled_connection_is_the_default(self):
        self.check([cfg("a"), cfg("b", default=True)], {"name": "b", "source": "default"})

    def test_two_unlabelled_without_record_fails_with_no_default(self):
        for copy in COPIES:
            with self.subTest(copy=copy):
                out = self.resolve(copy, [cfg("a"), cfg("b")])
                self.assertTrue(out["failed"])
                self.assertIn("NoDefaultConfig", out["output"])

    def test_two_unlabelled_with_status_config_keeps_the_recorded_one(self):
        # A second connection was added later without a default label: the object stays where it is.
        self.check([cfg("a"), cfg("b")], {"name": "a", "source": "recorded", "listed": True}, recorded="a")

    def test_relabelled_default_does_not_move_a_recorded_object(self):
        self.check([cfg("a"), cfg("b", default=True)], {"name": "a", "source": "recorded"}, recorded="a")

    def test_recorded_connection_gone_falls_back_to_the_default(self):
        self.check([cfg("b")], {"name": "b", "source": "default"}, recorded="a")

    def test_recorded_connection_being_deleted_falls_back_to_the_default(self):
        self.check([cfg("a", deleting=True), cfg("b")], {"name": "b", "source": "default"}, recorded="a")

    # --- teardown ---
    def test_teardown_uses_the_recorded_connection_first(self):
        self.check([cfg("a"), cfg("b", default=True)], {"name": "a", "source": "recorded", "listed": False},
                   spec="b", recorded="a", teardown=True, required=False)

    def test_teardown_keeps_a_recorded_connection_that_is_gone(self):
        # Never clean up in another connection than the one the object was realized in.
        self.check([cfg("b", default=True)], {"name": "a", "source": "recorded", "listed": False},
                   recorded="a", teardown=True, required=False)

    def test_teardown_without_record_uses_the_spec(self):
        self.check([cfg("a"), cfg("b")], {"name": "b", "source": "spec", "listed": False},
                   spec="b", teardown=True, required=False)

    def test_teardown_without_record_or_spec_uses_the_default(self):
        self.check([cfg("a"), cfg("b", default=True)], {"name": "b", "source": "default"},
                   teardown=True, required=False)

    def test_teardown_with_no_default_resolves_nothing(self):
        self.check([cfg("a"), cfg("b")], {"name": "", "source": "none", "listed": True},
                   teardown=True, required=False)


if __name__ == "__main__":
    unittest.main()

"""The job's entry that every status write records (eda/common/tasks/patch_cr_status.yml), for every kind.

Runs the task file with the status module stubbed (it records the job entry it is given) and checks: the entry's
name is the kind's singular and the action (entity-ready, not entitie-ready); the entry carries no address of the
automation platform, whatever the job's settings; jobId is the controller's job id, else the activation's id, else
the entry's name. The status library drops an address an older caller still sends (tests/unit/test_shc_status.py).
Skipped when ansible-playbook is absent.
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
TASK = os.path.join(ROOT, "eda", "common", "tasks", "patch_cr_status.yml")
BASES = os.path.join(ROOT, "operator", "config", "crd", "bases")

STUB = textwrap.dedent('''\
    #!/usr/bin/python
    # WANT_JSON
    import json, os, sys
    with open(sys.argv[1]) as f:
        args = json.load(f)
    with open(os.environ["SHC_JOB_OUT"], "a") as f:
        f.write(json.dumps({"plural": args.get("plural"), "job": args.get("job")}) + "\\n")
    print(json.dumps({"changed": False, "http": 200, "superseded": False, "resource": {}}))
    ''')

PLAY = textwrap.dedent('''\
    - hosts: localhost
      connection: local
      gather_facts: false
      tasks:
        - ansible.builtin.include_tasks: tasks/patch_cr_status.yml
          loop: "{{ plurals }}"
          loop_control:
            loop_var: cr_plural
          vars:
            cr_api_version: hybridsovereign.redhat/v1alpha1
            cr_name: web
            cr_namespace: entity-acme
            cr_status_body: {status: ready, message: Ready.}
            services_api_host: https://api.example.test:6443
            services_bearer_token: stub
    ''')


def kinds():
    out = {}
    for f in sorted(os.listdir(BASES)):
        with open(os.path.join(BASES, f)) as fh:
            n = yaml.safe_load(fh)["spec"]["names"]
        out[n["plural"]] = n["singular"]
    return out


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class JobEntry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shc-job-entry-")
        os.makedirs(os.path.join(cls.tmp, "tasks"))
        shutil.copy(TASK, os.path.join(cls.tmp, "tasks", "patch_cr_status.yml"))
        with open(os.path.join(cls.tmp, "tasks", "nudge_cr.yml"), "w") as f:
            f.write("- ansible.builtin.debug: {msg: nudge}\n")
        mdir = os.path.join(cls.tmp, "collections", "ansible_collections", "hybridsovereign", "shc", "plugins",
                            "modules")
        os.makedirs(mdir)
        with open(os.path.join(mdir, "cr_status.py"), "w") as f:
            f.write(STUB)
        with open(os.path.join(cls.tmp, "collections", "ansible_collections", "hybridsovereign", "shc",
                               "galaxy.yml"), "w") as f:
            f.write("namespace: hybridsovereign\nname: shc\nversion: 0.0.1\n")
        with open(os.path.join(cls.tmp, "play.yml"), "w") as f:
            f.write(PLAY)
        with open(os.path.join(cls.tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_task(self, extra):
        out = os.path.join(self.tmp, "jobs.jsonl")
        if os.path.exists(out):
            os.remove(out)
        with open(os.path.join(self.tmp, "vars.json"), "w") as f:
            json.dump(dict(extra, plurals=sorted(kinds())), f)
        env = dict(os.environ, SHC_JOB_OUT=out, ANSIBLE_COLLECTIONS_PATH=os.path.join(self.tmp, "collections"),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_CONFIG=os.path.join(self.tmp, "ansible.cfg"),
                   ANSIBLE_REMOTE_TMP=os.path.join(self.tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(self.tmp, "local-tmp"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                              cwd=self.tmp, env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        with open(out) as f:
            return {r["plural"]: r["job"] for r in map(json.loads, f)}

    def test_every_kind_names_its_job_by_its_singular(self):
        jobs = self.run_task({"tower_job_id": "4242"})
        self.assertEqual(set(jobs), set(kinds()))
        for plural, singular in kinds().items():
            self.assertEqual(jobs[plural]["name"], singular + "-ready", plural)
        self.assertEqual(jobs["entities"]["name"], "entity-ready")

    def test_no_address_whatever_the_settings(self):
        settings = [{"tower_job_id": "4242", "aap_ui_url": "https://controller.example.org/",
                     "eda_dashboard_host": "https://eda.example.org"},
                    {"ansible_eda": {"rulebook_activation_id": 9}, "eda_dashboard_host": "https://eda.example.org"},
                    {}]
        for extra in settings:
            for plural, job in self.run_task(extra).items():
                self.assertEqual(sorted(job), ["jobId", "name"], (plural, extra))
                self.assertNotIn("example.org", json.dumps(job))

    def test_job_id(self):
        cases = [({"tower_job_id": "4242"}, "4242"),
                 ({"tower_job_id": "4242", "aap_ui_url": "https://controller.example.org"}, "4242"),
                 ({"awx_job_id": 77}, "77"),
                 ({"ansible_eda": {"rulebook_activation_id": 9}}, "9"),
                 ({"ansible_eda": {"rulebook_activation_id": "manual"}}, "vault-ready"),
                 ({}, "vault-ready")]
        for extra, want in cases:
            self.assertEqual(self.run_task(extra)["vaults"]["jobId"].strip(), want, extra)


if __name__ == "__main__":
    unittest.main()

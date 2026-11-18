"""An access group (Rbac) that becomes usable wakes the environments that name it (gate part 2, G-2): the real
rbac_provision/tasks/wake_dependents.yml and common/tasks/nudge_cr.yml in ansible-playbook, with the real
kubernetes.core modules against a Kubernetes API over HTTP (fake_k8s_api.py). A CloudOSO or CloudVirt whose
spec.toolRbac names the Rbac gets the reconcile annotation (its Waiting group is then granted at once); one that
names another access group, or that is being deleted, does not; a steady access group wakes nothing. Names are
examples."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from fake_k8s_api import FakeK8s  # noqa: E402
from play_helper import HAVE_ANSIBLE, ROOT  # noqa: E402
from test_services_keycloak_url import have_k8s_collection  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "rbac_provision")
NS = "entity-acme"


def obj(kind, name, spec=None, namespace=NS, deleting=False):
    md = {"name": name, "namespace": namespace}
    if deleting:
        md["deletionTimestamp"] = "2026-11-17T18:00:00Z"
    return {"kind": kind, "metadata": md, "spec": spec or {}}


def run(fake, was_ready):
    tmp = tempfile.mkdtemp(prefix="shc-rbac-wake-")
    try:
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent(f'''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: {ROLE}/tasks/wake_dependents.yml
                '''))
        before = {"status": {"group": "/acme/developers",
                             "conditions": [{"type": "Ready", "status": "True" if was_ready else "False"}]}}
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump({"services_api_host": fake.url, "services_bearer_token": "token-of-the-job",
                       "services_validate_certs": False, "role_path": ROLE, "rbac_cr_name": "developers",
                       "rbac_cr_namespace": NS, "entity_name": "acme", "fabric_platform_namespace": "sovereign-cloud",
                       "rbac_group_path_display": "/acme/developers", "shc_live_cr": before}, f)
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "lt"),
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "rt"), TMPDIR=tmp)
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp, env=env,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_ANSIBLE and have_k8s_collection(), "ansible-playbook / kubernetes.core not installed")
class WakeEnvironments(unittest.TestCase):
    def fake(self):
        f = FakeK8s(objects=[
            obj("Entity", "acme", namespace="sovereign-cloud"),
            obj("CloudOSO", "web", {"toolRbac": {"editRbac": ["developers"], "viewRbac": ["auditors"]}}),
            obj("CloudOSO", "db", {"toolRbac": {"viewRbac": ["auditors"]}}),
            obj("CloudOSO", "old", {"toolRbac": {"editRbac": ["developers"]}}, deleting=True),
            obj("CloudVirt", "vms", {"toolRbac": {"viewRbac": ["developers"]}}),
            obj("Project", "p1", {"projectDeveloperRbac": ["developers"]}),
        ])
        self.addCleanup(f.stop)
        return f

    def test_an_access_group_that_became_usable_wakes_the_environments_that_name_it(self):
        fake = self.fake()
        rc, out = run(fake, was_ready=False)
        self.assertEqual(rc, 0, out[-3000:])
        woken = sorted((p["kind"], p["name"]) for p in fake.patches)
        self.assertEqual(woken, [("CloudOSO", "web"), ("CloudVirt", "vms"), ("Project", "p1")])
        for p in fake.patches:
            self.assertEqual(p["annotations"]["ansible.sdk.operatorframework.io/reconcileNow"], "true")

    def test_a_steady_access_group_wakes_nothing(self):
        fake = self.fake()
        rc, out = run(fake, was_ready=True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(fake.patches, [])


if __name__ == "__main__":
    unittest.main()

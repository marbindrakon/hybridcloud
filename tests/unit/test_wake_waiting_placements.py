"""GA2-F4: a network or environment that became ready wakes the placements waiting for it
(eda/common/tasks/wake_waiting_placements.yml). The selection template runs in ansible-playbook (skipped when
absent); the wiring checks always run."""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TASK = os.path.join(ROOT, "eda", "common", "tasks", "wake_waiting_placements.yml")


def placement(name, network="net1", backend=("CloudVirt", "vms"), status="pending", reason="WaitingForDependencies",
              waiting=(), deleting=False):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-10-22T10:00:00Z"
    return {"metadata": meta, "spec": {"network": network, "backend": {"kind": backend[0], "name": backend[1]}},
            "status": {"status": status, "waitingFor": list(waiting),
                       "conditions": [{"type": "Reconciling", "status": "False", "reason": "AsExpected"},
                                      {"type": "Ready", "status": "False", "reason": reason}]}}


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class SelectTest(unittest.TestCase):
    def select(self, kind, name, placements):
        with open(TASK) as f:
            template = yaml.safe_load(f)[1]["vars"]["nudge_crs"]
        tmp = tempfile.mkdtemp(prefix="shc-wp-test-")
        try:
            play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                     "tasks": [{"ansible.builtin.set_fact": {"_sel": template}},
                               {"ansible.builtin.copy": {"dest": "{{ out }}", "content": "{{ _sel | to_json }}"}}]}]
            with open(os.path.join(tmp, "play.yml"), "w") as f:
                yaml.safe_dump(play, f)
            with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
                f.write("[defaults]\n")
            out = os.path.join(tmp, "out.json")
            with open(os.path.join(tmp, "vars.json"), "w") as f:
                json.dump({"wp_kind": kind, "wp_name": name, "wp_namespace": "entity-a",
                           "_wp_placements": {"resources": placements}, "out": out}, f)
            env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                       ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                       ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                       ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
            proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                                  cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
            with open(out) as f:
                return sorted(p["name"] for p in json.load(f))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_network_wakes_its_waiting_placements(self):
        ps = [placement("p1"), placement("p2", network="other"), placement("p3", status="ready", reason="Ready"),
              placement("p4", deleting=True), placement("p5", reason="WaitingForGateway"),
              placement("p6", reason="WaitingForGateway", waiting=["HybridNetwork net1 (reconciling)"])]
        self.assertEqual(self.select("HybridNetwork", "net1", ps), ["p1", "p6"])

    def test_environment_wakes_placements_on_it(self):
        ps = [placement("p1"), placement("p2", backend=("CloudVirt", "other")), placement("p3", backend=("CloudOSO", "vms"))]
        self.assertEqual(self.select("CloudVirt", "vms", ps), ["p1"])
        self.assertEqual(self.select("CloudOSO", "vms", ps), ["p3"])


class WiringTest(unittest.TestCase):
    def test_ready_networks_and_environments_wake(self):
        with open(os.path.join(ROOT, "eda", "common", "tasks", "fabric_finish.yml")) as f:
            self.assertIn("wake_waiting_placements.yml", f.read())
        with open(os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudoso_provision", "tasks", "main.yml")) as f:
            self.assertIn("wake_waiting_placements.yml", f.read())


if __name__ == "__main__":
    unittest.main()

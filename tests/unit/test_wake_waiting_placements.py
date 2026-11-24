"""GA2-F4: a network or environment that became ready wakes the placements waiting for it
(eda/common/tasks/wake_waiting_placements.yml). The selection template runs in ansible-playbook (skipped when
absent); the wiring checks always run."""
import json
import os
import shutil
import subprocess
import sys
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
    def select(self, kind, name, placements, serves=None):
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
                           "_wp_placements": {"resources": placements}, "out": out,
                           **({"wp_compare": True, "wp_serves": serves} if serves is not None else {})}, f)
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


    def test_an_environment_wakes_its_ready_placements_that_record_other_namespaces(self):
        def ready(name, namespaces, **kw):
            p = placement(name, status="ready", reason="Ready", **kw)
            p["status"]["namespaces"] = namespaces
            return p
        ps = [placement("waiting"),
              ready("same", ["acme--prod--vms"]),
              ready("old-name", ["acme-prod-vms"]),
              ready("none-yet", []),
              ready("two", ["acme--prod--b", "acme--prod--a"], network="net2"),
              ready("left", ["acme--prod--x"], network="net3"),
              ready("no-namespaces", [], network="net3"),
              ready("elsewhere", ["acme-other-vms"], backend=("CloudVirt", "other")),
              ready("going", ["acme-prod-vms"], deleting=True)]
        older = placement("no-key", status="ready", reason="Ready", network="net4")   # records no namespaces at all
        ps.append(older)
        failed = placement("failed", status="failed", reason="ProvisionFailed")
        failed["status"]["namespaces"] = ["acme-prod-vms"]
        ps.append(failed)
        # net5: a network of the environment that no placement names changes nothing
        serves = {"net1": ["acme--prod--vms"], "net2": ["acme--prod--a", "acme--prod--b"], "net5": ["acme--prod--z"]}
        # a caller that says nothing about namespaces (a network, another kind of environment) wakes the waiting only
        self.assertEqual(self.select("CloudVirt", "vms", ps), ["waiting"])
        self.assertEqual(self.select("CloudVirt", "vms", ps, serves=serves), ["left", "none-yet", "old-name", "waiting"])
        # an environment with no namespace on any network: the placements that still record one
        self.assertEqual(self.select("CloudVirt", "vms", ps, serves={}), ["left", "old-name", "same", "two", "waiting"])

    def serves(self, cases):
        sys.path.insert(0, os.path.dirname(__file__))
        from play_helper import by_name, load, run_cases
        tasks = load(os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudvirt_provision", "tasks", "main.yml"))
        expr = by_name(tasks, "Report the environment ready")["vars"]["fab_serves"]
        step = {"ansible.builtin.set_fact": {"serves": expr}, "vars": {"cv_ns_status": "{{ case.now }}"}}
        return run_cases([step], cases, "serves")

    def test_the_environment_says_which_namespaces_it_has_on_each_network(self):
        vms = {"name": "vms", "namespace": "acme--prod--vms", "network": "core", "ready": True}
        more = {"name": "more", "namespace": "acme--prod--more", "network": "core", "ready": False}
        edge = {"name": "edge", "namespace": "acme--prod--edge", "network": "edge", "ready": True}
        plain = {"name": "default", "namespace": "acme--prod--default", "network": "", "ready": True}
        self.assertEqual(self.serves([{"now": [vms, plain]}, {"now": [plain]}, {"now": [vms, edge, more, plain]}]),
                         [{"core": ["acme--prod--vms"]}, {},
                          {"core": ["acme--prod--vms", "acme--prod--more"], "edge": ["acme--prod--edge"]}])

    def test_the_wiring_passes_the_namespaces_on(self):
        with open(os.path.join(ROOT, "eda", "common", "tasks", "fabric_finish.yml")) as f:
            finish = yaml.safe_load(f)
        wake = [t for t in finish if "wake_waiting_placements.yml" in str(t.get("ansible.builtin.include_tasks", ""))][0]
        self.assertEqual(wake["vars"]["wp_compare"], "{{ fab_serves is defined }}")
        self.assertIn("fab_serves", wake["vars"]["wp_serves"])
        with open(TASK) as f:
            self.assertIn("wp_serves", f.read())


class WiringTest(unittest.TestCase):
    def test_ready_networks_and_environments_wake(self):
        with open(os.path.join(ROOT, "eda", "common", "tasks", "fabric_finish.yml")) as f:
            self.assertIn("wake_waiting_placements.yml", f.read())
        with open(os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudoso_provision", "tasks", "main.yml")) as f:
            self.assertIn("wake_waiting_placements.yml", f.read())


if __name__ == "__main__":
    unittest.main()

"""CloudVirt status.virtualMachines (PGT2-F2): the VMs per site namespace, for the environment delete preview.

eda/common/tasks/cloudvirt_vm_status.yml summarizes the VirtualMachines of the environment's site namespaces (count,
sorted names capped, truncated flags). cloudvirt_provision fills status.virtualMachines with its ready write; the
site probe (cloudinfrastructure_provision, openshift sites) refreshes it with a partial status write that carries no
event, so it never supersedes or ends anything. A namespace that could not be read leaves the stored list alone.
"""
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

COMMON = os.path.join(ROOT, "eda", "common", "tasks", "cloudvirt_vm_status.yml")
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
CV_MAIN = os.path.join(ROLES, "cloudvirt_provision", "tasks", "main.yml")
CI_TASKS = os.path.join(ROLES, "cloudinfrastructure_provision", "tasks")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-cloudvirt.yaml")
INCLUDE = "{{ role_path }}/../../../common/tasks/cloudvirt_vm_status.yml"


def vm(name):
    return {"metadata": {"name": name}}


class Wiring(unittest.TestCase):
    def test_provision_fills_the_field_on_ready(self):
        tasks = load(CV_MAIN)
        inc = [t for t in walk(tasks) if t.get("ansible.builtin.include_tasks") == INCLUDE]
        self.assertEqual(len(inc), 1)
        self.assertIn("cv_kept", inc[0]["vars"]["cvs_namespaces"])
        self.assertIn("cv_namespaces", inc[0]["vars"]["cvs_namespaces"])
        ready = by_name(tasks, "Report the environment ready")
        self.assertIn("'virtualMachines': cv_vm_status", ready["vars"]["fab_status"])
        names = [t.get("name") for t in walk(tasks)]
        self.assertLess(names.index("Keep the removed namespaces that still hold VMs"),
                        names.index("The VirtualMachines in the environment's namespaces"))

    def test_probe_refreshes_openshift_sites_only_and_never_fails(self):
        main = load(os.path.join(CI_TASKS, "main.yml"))
        step = by_name(main, "Refresh the VirtualMachine lists of the environments on this site")
        self.assertEqual(step["when"], "ci_cr_type == 'openshift'")
        names = [t.get("name") for t in main[0]["block"]]
        self.assertLess(names.index("Wake the ones pending on it"), names.index(step["name"]))
        vms = load(os.path.join(CI_TASKS, "cloudvirt_vms.yml"))
        block = by_name(vms, "Refresh their VirtualMachine lists")
        self.assertTrue(block["rescue"])
        self.assertTrue(any(t.get("ansible.builtin.file", {}).get("state") == "absent" for t in block["always"]))

    def test_probe_write_is_partial_without_event(self):
        write = by_name(load(os.path.join(CI_TASKS, "cloudvirt_vm_write.yml")),
                        "Record the VirtualMachines of {{ civ_cv.metadata.namespace ~ '/' ~ civ_cv.metadata.name }}")
        mod = write["hybridsovereign.shc.cr_status"]
        self.assertTrue(mod["partial"])
        self.assertNotIn("event_id", mod)
        self.assertEqual(list(mod["status"].keys()), ["virtualMachines"])
        self.assertEqual(write["when"], "cv_vm_status | default({}) | length > 0")

    def test_crd_field(self):
        crd = load(CRD)
        st = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
        vms = st["virtualMachines"]["properties"]
        self.assertEqual(set(vms), {"observedAt", "total", "truncated", "namespaces"})
        self.assertEqual(set(vms["namespaces"]["items"]["properties"]),
                         {"name", "namespace", "count", "names", "truncated"})


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Summary(unittest.TestCase):
    def summarize(self, results, cap=3):
        step = load(COMMON)[1]
        return run_play([step, write_out("cv_vm_status")], {"_cvs_vms": {"results": results}, "cvs_names_cap": cap})

    def test_counts_names_and_cap(self):
        got = self.summarize([
            {"item": {"namespace": "acme-vms-a", "name": "a"}, "resources": [vm("z"), vm("b"), vm("m"), vm("c")]},
            {"item": {"namespace": "acme-vms-b", "name": "b"}, "resources": [vm("x")]},
            {"item": {"namespace": "acme-vms-old", "name": ""}, "resources": []},
        ])
        self.assertEqual(got["total"], 5)
        self.assertIs(got["truncated"], True)
        self.assertRegex(got["observedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        a, b, old = got["namespaces"]
        self.assertEqual(a, {"name": "a", "namespace": "acme-vms-a", "count": 4, "names": ["b", "c", "m"],
                             "truncated": True})
        self.assertEqual(b, {"name": "b", "namespace": "acme-vms-b", "count": 1, "names": ["x"], "truncated": False})
        self.assertEqual(old, {"name": "", "namespace": "acme-vms-old", "count": 0, "names": [], "truncated": False})

    def test_unreadable_namespace_keeps_the_stored_list(self):
        got = self.summarize([
            {"item": {"namespace": "acme-vms-a", "name": "a"}, "resources": [vm("x")]},
            {"item": {"namespace": "acme-vms-b", "name": "b"}, "failed": False, "msg": "Forbidden"},
        ])
        self.assertEqual(got, {})

    def test_no_namespaces(self):
        got = self.summarize([])
        self.assertEqual((got["total"], got["truncated"], got["namespaces"]), (0, False, []))


if __name__ == "__main__":
    unittest.main()

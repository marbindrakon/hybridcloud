"""The placement's status.ipv6 (networkplacement_provision main.yml "Describe the placement's IPv6", design/IPV6-PLAN.md
§2.3): written when the placement holds an IPv6 subnet; addressing from the backend kind's placement.ipv6.addressMode
(SLAAC for slaac or dhcpv6-stateless, else DHCPv6), internet unavailable/NotOffered while egress.ipv6.enabled is
off (the ready case is the egress realization's). Every key written is one the NetworkPlacement CRD declares, in
both copies the repository keeps. Runs the task in ansible-playbook with the shipped realization defaults."""
import copy
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

MAIN = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks", "main.yml")
DEFAULTS = os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")
CRDS = [os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-networkplacement.yaml"),
        os.path.join(ROOT, "operator", "primary", "helm", "templates", "crds.yaml")]
TASK = "Describe the placement's IPv6"
MESSAGE = "IPv6 internet access is not offered on this fabric yet"


def real(**changes):
    r = copy.deepcopy(load(DEFAULTS)["fabric_realization_defaults"])
    for path, value in changes.items():
        d = r
        keys = path.split(".")
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
    return r


def status_schema(path):
    """The status.properties of the NetworkPlacement CRD in a file (several documents in the generated copy, whose
    chart directives are dropped)."""
    with open(path) as f:
        lines = f.read().split("\n")
    # the generated copy opens with a chart comment and an installCRDs guard: YAML starts at the first apiVersion
    start = next(i for i, ln in enumerate(lines) if ln.startswith("apiVersion:"))
    text = "\n".join(ln for ln in lines[start:] if not ln.startswith("{{"))
    for doc in yaml.safe_load_all(text):
        if isinstance(doc, dict) and (doc.get("metadata") or {}).get("name") == "networkplacements.hybridsovereign.redhat":
            return doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
    raise AssertionError(f"no NetworkPlacement CRD in {path}")


class StatusIPv6Test(unittest.TestCase):
    def run_task(self, kind="CloudOSO", prefixes6=("2001:db8:100:1::/64",), fab_real=None, status=None):
        task = by_name(load(MAIN), TASK)
        # np_status is set by a task, as in the job (an extra var would win over the task's set_fact)
        start = {"ansible.builtin.set_fact": {"np_status": "{{ seed_status }}"}}
        variables = {"seed_status": status if status is not None else {"entity": "acme", "prefixes": ["10.60.1.0/24"] + list(prefixes6)},
                     "np_prefixes_v6": list(prefixes6), "np_backend_kind": kind, "fab_real": fab_real or real()}
        return run_play([start, task, write_out("np_status")], variables)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_openstack_stateful_dhcpv6_and_no_internet(self):
        st = self.run_task()
        # (translation is written as null: the placement has IPv4, so it gets none)
        self.assertEqual(st["ipv6"], {"addressing": "DHCPv6",
                                      "internet": {"state": "unavailable", "reason": "NotOffered", "message": MESSAGE},
                                      "translation": None})
        # the rest of the status is kept
        self.assertEqual(st["entity"], "acme")
        self.assertEqual(st["prefixes"], ["10.60.1.0/24", "2001:db8:100:1::/64"])

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_kind_without_a_mode_is_dhcpv6(self):
        r = real()
        r["placement"]["ipv6"]["addressMode"].pop("CloudVirt", None)
        self.assertEqual(self.run_task(kind="CloudVirt", fab_real=r)["ipv6"]["addressing"], "DHCPv6")

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_slaac_modes(self):
        for mode in ("slaac", "dhcpv6-stateless"):
            st = self.run_task(fab_real=real(**{"placement.ipv6.addressMode": {"CloudOSO": mode}}))
            self.assertEqual(st["ipv6"]["addressing"], "SLAAC", mode)
        st = self.run_task(fab_real=real(**{"placement.ipv6.addressMode": {"CloudOSO": "dhcpv6-stateful"}}))
        self.assertEqual(st["ipv6"]["addressing"], "DHCPv6")

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_with_ipv6_egress_the_internet_state_is_left_to_the_egress(self):
        st = self.run_task(fab_real=real(**{"egress.ipv6.enabled": True}))
        self.assertEqual(st["ipv6"], {"addressing": "DHCPv6", "translation": None})

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_no_ipv6_subnet_writes_nothing(self):
        st = self.run_task(prefixes6=(), status={"entity": "acme", "prefixes": ["10.60.1.0/24"]})
        self.assertNotIn("ipv6", st)

    def test_only_fields_the_crd_declares(self):
        for path in CRDS:
            ipv6 = status_schema(path)["ipv6"]["properties"]
            self.assertIn("addressing", ipv6, path)
            self.assertEqual(ipv6["addressing"]["type"], "string")
            internet = ipv6["internet"]["properties"]
            for key in ("state", "reason", "message"):
                self.assertEqual(internet[key]["type"], "string", (path, key))
            # the values the task writes are the ones the descriptions name
            self.assertIn("unavailable", internet["state"]["description"])
            self.assertIn("DHCPv6", ipv6["addressing"]["description"])
            self.assertIn("SLAAC", ipv6["addressing"]["description"])

    def test_task_shape(self):
        task = by_name(load(MAIN), TASK)
        self.assertEqual(task["when"], "np_prefixes_v6 | length > 0")
        # the internet row is built in the task's vars (NotOffered while the fabric offers no IPv6 egress, else the
        # network's family-6 row); the set_fact only adds it to the status
        self.assertIn("NotOffered", task["vars"]["_internet"])
        self.assertIn(MESSAGE, task["vars"]["_internet"])
        self.assertIn("_internet", task["ansible.builtin.set_fact"]["np_status"])


if __name__ == "__main__":
    unittest.main()

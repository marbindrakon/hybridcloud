"""The OpenShift Virtualization side of a placement (networkplacement_provision backend_cloudvirt.yml): the
ClusterUserDefinedNetwork definition is family-neutral (layer2.subnets = np_prefixes, both families), so a placement
with an IPv4 and an IPv6 subnet renders one dual-stack Layer2 EVPN network, an IPv6-only placement an IPv6 one, and the
VNIs and the MTU stay numbers. The definition expression runs in ansible-playbook (skipped when it is absent); the
wiring check always runs. Documentation and private ranges only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

NP = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks")
CUDN_TASK = "ClusterUserDefinedNetwork {{ np_names.cudn }}"
V4, V6 = "10.60.1.0/24", "2001:db8:100:1::/64"

VARS = {"fab_namespace": "entity-example-corp", "fab_name": "example-net-vms",
        "np_names": {"cudn": "hn51001", "routeAdvertisement": "hn51001",
                     "labels": {"hybridsovereign.redhat/l3vni": "51001", "hybridsovereign.redhat/networkplacement": "example-net-vms"}},
        "np_ns_selector": {"hybridsovereign.redhat/network": "example-net"},
        "np_gateway": {"status": {"site": {"vtep": "vtep-a"}}},
        "np_real": {"l3Vni": 51001, "l2Vni": 151001, "mtu": 9000}, "np_asn": 65010}


def cudn_task():
    return by_name(load(os.path.join(NP, "backend_cloudvirt.yml")), CUDN_TASK)


class Wiring(unittest.TestCase):
    def test_subnets_are_the_placement_prefixes_of_both_families(self):
        definition = cudn_task()["kubernetes.core.k8s"]["definition"]
        self.assertIn("'subnets': np_prefixes,", definition)
        self.assertNotIn("np_prefixes_v4", definition)
        self.assertNotIn("np_prefixes_v6", definition)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Render(unittest.TestCase):
    def cudn(self, prefixes):
        t = {"name": CUDN_TASK, "ansible.builtin.set_fact": {"_cudn": cudn_task()["kubernetes.core.k8s"]["definition"]}}
        return run_play([t, write_out("_cudn")], dict(VARS, np_prefixes=prefixes))

    def test_dual_stack(self):
        c = self.cudn([V4, V6])
        self.assertEqual((c["apiVersion"], c["kind"]), ("k8s.ovn.org/v1", "ClusterUserDefinedNetwork"))
        self.assertEqual(c["metadata"]["name"], "hn51001")
        self.assertEqual(c["metadata"]["annotations"], {"hybridsovereign.redhat/networkplacement": "entity-example-corp/example-net-vms"})
        self.assertEqual(c["spec"]["namespaceSelector"], {"matchLabels": VARS["np_ns_selector"]})
        net = c["spec"]["network"]
        self.assertEqual((net["topology"], net["transport"]), ("Layer2", "EVPN"))
        self.assertEqual(net["evpn"], {"vtep": "vtep-a", "ipVRF": {"vni": 51001, "routeTarget": "65010:51001"},
                                       "macVRF": {"vni": 151001, "routeTarget": "65010:151001"}})
        self.assertEqual(net["layer2"], {"role": "Primary", "mtu": 9000, "subnets": [V4, V6], "ipam": {"lifecycle": "Persistent"}})
        # numbers stay numbers (the API refuses strings for vni and mtu)
        self.assertIs(type(net["evpn"]["ipVRF"]["vni"]), int)
        self.assertIs(type(net["layer2"]["mtu"]), int)

    def test_single_family(self):
        self.assertEqual(self.cudn([V4])["spec"]["network"]["layer2"]["subnets"], [V4])
        self.assertEqual(self.cudn([V6])["spec"]["network"]["layer2"]["subnets"], [V6])


if __name__ == "__main__":
    unittest.main()

"""The names of a placement's realized objects (eda/common/files/shc_fabric.py placement-names) and the fabric orphan
check's reading of the platform project: the OpenStack network and router are <tenant>_<placement> from the names
function (the two-tenant pair acme-corp/core-prod against acme/corp-core-prod gives two names), the numbered objects
keep their number and carry the names in an annotation, and the orphan check matches the platform project's
networks and routers by recorded id or by tag, never by parsing their names. Names are examples."""
import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402


def names(entity, ns, name, network="core", vni=51003):
    return F.placement_names({"namespace": ns, "name": name, "entity": entity, "network": network, "l3Vni": vni})


class PlacementNames(unittest.TestCase):
    def test_the_openstack_network_is_the_tenants_and_the_placements(self):
        n = names("acme-corp", "entity-acme-corp", "core-prod")
        self.assertEqual((n["osNetwork"], n["osRouter"]), ("acme-corp_core-prod", "acme-corp_core-prod"))
        self.assertEqual(n["tag"], "shc-placement=entity-acme-corp/core-prod")

    def test_two_tenants_never_share_a_name(self):
        a = names("acme-corp", "entity-acme-corp", "web")["osNetwork"]
        b = names("acme", "entity-acme", "corp-web")["osNetwork"]
        self.assertEqual((a, b), ("acme-corp_web", "acme_corp-web"))

    def test_numbers_stay_on_devices_and_site_objects(self):
        n = names("acme-corp", "entity-acme-corp", "core-prod", vni=51003)
        self.assertEqual((n["vrf"], n["cudn"], n["routeAdvertisement"]), ("t51003", "hn51003", "hn51003"))
        self.assertEqual(json.loads(n["annotations"][F.NAMES_ANNOTATION]),
                         {"tenant": "acme-corp", "network": "entity-acme-corp/core",
                          "placement": "entity-acme-corp/core-prod"})
        self.assertEqual(n["display"], "acme-corp / core / core-prod")

    def test_a_placement_without_a_tenant_has_no_name(self):
        with self.assertRaises(ValueError):
            names("", "entity-x", "core-prod")


def placement(ns, name, gw="gw1", vni=51003, net_id="", rtr_id=""):
    return {"kind": "NetworkPlacement", "metadata": {"namespace": ns, "name": name}, "spec": {"network": "core"},
            "status": {"cloudGateway": gw, "realization": {"l3Vni": vni, "site": {
                "networkId": net_id, "routerId": rtr_id, "sharedTo": "proj-t"}}}}


def report(placements, networks, routers):
    out = F.orphan_report({"crs": {"placements": placements, "gateways": [], "fabrics": []},
                           "sites": [{"gateway": "gw1", "site": "s1", "networks": networks, "routers": routers}]})
    return [f for f in out["findings"] if "platform project" in f or "shared to" in f]


class OrphanCheckOfThePlatformProject(unittest.TestCase):
    def test_matched_by_recorded_id_whatever_the_name(self):
        p = placement("entity-acme-corp", "core-prod", net_id="net-1", rtr_id="rtr-1")
        self.assertEqual(report([p], [{"id": "net-1", "name": "acme-corp_core-prod", "tags": [], "sharedTo": ["proj-t"]}],
                                [{"id": "rtr-1", "name": "hn51003", "tags": []}]), [])

    def test_matched_by_tag(self):
        p = placement("entity-acme-corp", "core-prod")
        tag = ["shc-placement=entity-acme-corp/core-prod"]
        self.assertEqual(report([p], [{"id": "net-9", "name": "anything", "tags": tag, "sharedTo": []}],
                                [{"id": "rtr-9", "name": "anything", "tags": tag}]), [])

    def test_a_number_in_the_name_no_longer_matches(self):
        # hn51003 with the placement's VNI but neither its id nor its tag: an orphan
        p = placement("entity-acme-corp", "core-prod", vni=51003, net_id="net-1", rtr_id="rtr-1")
        found = report([p], [{"id": "net-x", "name": "hn51003", "tags": [], "sharedTo": []}],
                       [{"id": "rtr-x", "name": "hn51003", "tags": []}])
        self.assertEqual(len(found), 2)
        self.assertTrue(all("has no placement" in f for f in found))

    def test_another_gateways_placement_does_not_own_it(self):
        p = placement("entity-acme-corp", "core-prod", gw="gw2", net_id="net-1")
        self.assertEqual(len(report([p], [{"id": "net-1", "name": "x", "tags": [], "sharedTo": []}], [])), 1)

    def test_an_extra_share_is_still_reported(self):
        p = placement("entity-acme-corp", "core-prod", net_id="net-1")
        found = report([p], [{"id": "net-1", "name": "x", "tags": [], "sharedTo": ["proj-t", "proj-other"]}], [])
        self.assertEqual(found, ["site s1: network x is also shared to proj-other"])


if __name__ == "__main__":
    unittest.main()

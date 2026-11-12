"""TenantProfile status.reservedRanges, status.managedClusterRanges and sites[].externalNetworks: the Entity job
(entity_provision tenant_profile_compute.yml) copies the reserved ranges of the tenant's fabric (HybridFabric
status.reservedRanges, rebuilt from prefix, purpose, rule and site only) and the ranges for clusters the platform
manages (the fabric's realization.service.attachableRanges while the service is offered); the site summary
(common/tasks/site_tenant_summary.yml) lists an OpenStack site's global external networks. Rendered with the API
reads replaced by fixtures, as the existing profile tests do. Documentation ranges and example names only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, load, run_play, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "entity_provision")
COMPUTE = os.path.join(ROLE, "tasks", "tenant_profile_compute.yml")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-tenantprofile.yaml")

ROWS = [{"prefix": "192.0.2.0/24", "purpose": "Networks the platform's management connects from", "rule": "Refused"},
        {"prefix": "198.51.100.0/26", "purpose": "Addresses inside the platform's network nodes", "rule": "SteppedAround"},
        {"prefix": "100.64.0.0/16", "purpose": "Addresses the site uses itself", "rule": "SteppedAround", "site": "site-a",
         "source": "placement.siteReserved", "realizationKey": "placement.siteReserved.CloudOSO"},
        {"prefix": "203.0.113.0/24", "purpose": "x", "rule": "Bogus"},
        "not-a-row"]


def fabric(name="fab", rows=None, service=None, refs=None, deleting=False):
    md = {"name": name, "namespace": "platform-ns"}
    if deleting:
        md["deletionTimestamp"] = "2001-01-01T00:00:00Z"
    st = {"realization": {"service": service} if service is not None else {}}
    if rows is not None:
        st["reservedRanges"] = rows
    return {"metadata": md, "spec": {"entityRefs": [{"name": r} for r in refs or []]}, "status": st}


def site(name, typ="openstack", externals=None):
    st = {"capabilities": {"api": True, "evpn": True}, "status": "ready", "site": {}}
    if externals is not None:
        st["site"]["externalNetworks"] = externals
    return {"metadata": {"name": name}, "spec": {"type": typ}, "status": st}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Profile(unittest.TestCase):
    def render(self, fabrics=None, sites=(), fabrics_fail=False):
        tasks = []
        for t in load(COMPUTE):
            if t.get("name") == "List the platform's sites":
                tasks.append({"name": t["name"], "ansible.builtin.set_fact": {"_tp_sites": {"resources": list(sites)}}})
            elif t.get("name") == "List the platform's fabrics":
                tasks.append({"name": t["name"], "ansible.builtin.set_fact": {
                    "_tp_fabrics": {"failed": True} if fabrics_fail else {"resources": list(fabrics or [])}}})
            else:
                tasks.append(t)
        v = {"role_path": ROLE, "entity_cr_name": "acme", "entity_cr_namespace": "platform-ns",
             "entity_spec": {"displayName": "Acme"}, "entity_generation": 3}
        return run_play(tasks + [write_out("tenant_profile_status")], v)

    def test_the_ranges_of_the_tenants_fabric(self):
        svc = {"enabled": True, "attachableRanges": ["10.64.0.0/12"], "vrf": "service", "sources": ["192.0.2.0/24"]}
        out = self.render([fabric(rows=ROWS, service=svc)])
        self.assertEqual(out["reservedRanges"], [
            {"prefix": "192.0.2.0/24", "purpose": "Networks the platform's management connects from", "rule": "Refused"},
            {"prefix": "198.51.100.0/26", "purpose": "Addresses inside the platform's network nodes", "rule": "SteppedAround"},
            {"prefix": "100.64.0.0/16", "purpose": "Addresses the site uses itself", "rule": "SteppedAround", "site": "site-a"}])
        self.assertEqual(out["managedClusterRanges"], ["10.64.0.0/12"])
        # nothing but ranges and purposes leaves the fabric
        text = str(out)
        for word in ("realizationKey", "source", "service", "vrf", "fab"):
            self.assertNotIn(f"'{word}'", text)

    def test_absent_without_the_service_or_rows(self):
        out = self.render([fabric(rows=[], service={"enabled": False, "attachableRanges": ["10.64.0.0/12"]})])
        self.assertIsNone(out["reservedRanges"])
        self.assertIsNone(out["managedClusterRanges"])
        # a fabric before its first run with the new status: no keys
        out = self.render([fabric()])
        self.assertIsNone(out["reservedRanges"])
        self.assertIsNone(out["managedClusterRanges"])

    def test_absent_when_the_list_is_empty(self):
        # the list is required with the service (check-fabric); an empty one is never published as "any range"
        self.assertIsNone(self.render([fabric(rows=ROWS[:1], service={"enabled": True, "attachableRanges": []})])["managedClusterRanges"])

    def test_any_range_is_written_out(self):
        out = self.render([fabric(rows=ROWS[:1], service={"enabled": True, "attachableRanges": ["0.0.0.0/0"]})])
        self.assertEqual(out["managedClusterRanges"], ["0.0.0.0/0"])

    def test_only_the_tenants_fabric(self):
        other = fabric("other", rows=[{"prefix": "203.0.113.0/24", "purpose": "p", "rule": "Refused"}], refs=["someone"])
        out = self.render([other, fabric(rows=ROWS[:1]), fabric("gone", rows=ROWS[:2], deleting=True)])
        self.assertEqual([r["prefix"] for r in out["reservedRanges"]], ["192.0.2.0/24"])
        # several fabrics: the profile cannot tell (removed, as the IPv6 offer)
        self.assertIsNone(self.render([fabric("a", rows=ROWS[:1]), fabric("b", rows=ROWS[:1])])["reservedRanges"])

    def test_unreadable_fabrics_leave_the_keys_out(self):
        out = self.render(fabrics_fail=True)
        for k in ("reservedRanges", "managedClusterRanges", "ipv6Platform"):
            self.assertNotIn(k, out)

    def test_an_existing_profile_changes_only_by_the_new_keys(self):
        # an installation without the new fabric status: the profile body as before plus two nulls
        out = self.render([fabric()], sites=[site("site-a", externals=None)])
        self.assertEqual(set(out), {"displayName", "contacts", "sites", "observedEntityGeneration", "lastUpdatedTime",
                                    "ipv6Platform", "reservedRanges", "managedClusterRanges"})
        self.assertNotIn("externalNetworks", out["sites"][0])

    def test_external_networks_of_openstack_sites(self):
        ext = [{"name": "public-b", "id": "6b0c0a52-0000-4000-8000-00000000000b", "families": [6, "4"], "default": False,
                "shared": True, "subnets": ["x"], "rbacPolicy": "y"},
               {"name": "public-a", "id": "6b0c0a52-0000-4000-8000-00000000000a", "families": [4], "default": True},
               {"name": ""}, "junk"]
        out = self.render([], sites=[site("site-a", externals=ext), site("site-b", typ="openshift", externals=ext)])
        a = next(s for s in out["sites"] if s["name"] == "site-a")
        self.assertEqual(a["externalNetworks"], [
            {"name": "public-a", "id": "6b0c0a52-0000-4000-8000-00000000000a", "families": [4], "default": True},
            {"name": "public-b", "id": "6b0c0a52-0000-4000-8000-00000000000b", "families": [4, 6], "default": False}])
        b = next(s for s in out["sites"] if s["name"] == "site-b")
        self.assertNotIn("externalNetworks", b)


class CrdText(unittest.TestCase):
    def test_fields(self):
        doc = load(CRD)
        st = doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
        rr = st["reservedRanges"]["items"]
        self.assertEqual(sorted(rr["properties"]), ["prefix", "purpose", "rule", "site"])
        self.assertEqual(rr["properties"]["rule"]["enum"], ["Refused", "SteppedAround", "NotForManagedClusters"])
        self.assertIn("managedClusterRanges", st)
        xn = st["sites"]["items"]["properties"]["externalNetworks"]["items"]["properties"]
        self.assertEqual(sorted(xn), ["default", "families", "id", "name"])


if __name__ == "__main__":
    unittest.main()

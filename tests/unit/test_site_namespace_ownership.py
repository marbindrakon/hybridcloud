"""A virtualization environment's site namespaces and a placement's numbered site objects (eda/common/files/
shc_fabric.py cloudvirt-namespaces, namespace-ownership, site-object-ownership): the name <tenant>--<environment>--
<namespace> from the names function (the two-tenant pair acme-corp/web against acme/corp-web gives two names; the old
hyphen join gave one), a namespace the status records keeps its name, and the refuse-or-adopt decision enumerated:
what the site has of the name (nothing, this environment's, the tenant's other environment's, another tenant's, an
unlabelled one) x whether the status recorded it. Nothing is merged into a namespace that is not the environment's.
Names are examples."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402


def wanted(entity, cv, names=("vms",), recorded=None):
    return F.cloudvirt_namespaces({"entity": entity, "cloudvirt": cv, "namespaces": [{"name": n} for n in names],
                                   "recorded": recorded or []})["namespaces"]


def labels(entity, cv):
    return F.site_labels(entity, cv)


class Names(unittest.TestCase):
    def test_the_form(self):
        self.assertEqual(wanted("acme-corp", "prod")[0]["namespace"], "acme-corp--prod--vms")

    def test_two_tenants_get_two_namespaces(self):
        # the hyphen join gave acme-corp-web-vms for both
        a = wanted("acme-corp", "web")[0]["namespace"]
        b = wanted("acme", "corp-web")[0]["namespace"]
        self.assertEqual((a, b), ("acme-corp--web--vms", "acme--corp-web--vms"))

    def test_a_recorded_namespace_keeps_its_name(self):
        rec = [{"name": "vms", "namespace": "acme-corp-prod-vms", "ready": True}]
        out = wanted("acme-corp", "prod", names=("vms", "db"), recorded=rec)
        self.assertEqual([n["namespace"] for n in out], ["acme-corp-prod-vms", "acme-corp--prod--db"])

    def test_a_long_name_is_cut_with_a_derived_ending(self):
        ns = wanted("a" * 30, "b" * 30, names=("c" * 30,))[0]["namespace"]
        self.assertLessEqual(len(ns), 63)
        self.assertNotEqual(ns, wanted("a" * 30, "b" * 30, names=("c" * 29 + "d",))[0]["namespace"])


class Ownership(unittest.TestCase):
    """Every state of the decision for one namespace acme-corp--prod--vms of tenant acme-corp, environment prod."""
    ON_SITE = {
        "nothing": None,
        "ours": labels("acme-corp", "prod"),
        "tenant's other environment": labels("acme-corp", "test"),
        "another tenant's": labels("acme", "corp-prod"),
        "unlabelled": {},
        "the site's own": {"kubernetes.io/metadata.name": "acme-corp--prod--vms"},
    }
    EXPECTED = {"nothing": "", "ours": "", "tenant's other environment": "NamespaceTaken",
                "another tenant's": "NameTakenOnSite", "unlabelled": "NameTakenOnSite", "the site's own": "NameTakenOnSite"}

    def test_every_state(self):
        for recorded in (False, True):
            rec = [{"name": "vms", "namespace": "acme-corp--prod--vms"}] if recorded else []
            ns = wanted("acme-corp", "prod", recorded=rec)
            for site, have in self.ON_SITE.items():
                with self.subTest(site=site, recorded=recorded):
                    existing = {} if have is None else {"acme-corp--prod--vms": have}
                    out = F.namespace_ownership({"namespaces": ns, "existing": existing})
                    want = self.EXPECTED[site]
                    self.assertEqual((out["ok"], out["reason"]), (want == "", want))
                    if want:
                        self.assertEqual(out["refused"], ["acme-corp--prod--vms"])
                        self.assertNotIn("acme ", out["message"])      # another tenant is never named

    def test_one_foreign_namespace_stops_them_all(self):
        ns = wanted("acme-corp", "prod", names=("vms", "db"))
        out = F.namespace_ownership({"namespaces": ns, "existing": {"acme-corp--prod--db": {}}})
        self.assertEqual((out["ok"], out["refused"]), (False, ["acme-corp--prod--db"]))


class SiteObjects(unittest.TestCase):
    LABELS = F.placement_names({"namespace": "entity-acme-corp", "name": "core-prod", "entity": "acme-corp",
                                "network": "core", "l3Vni": 51003})["labels"]

    def decide(self, cudn, ra):
        return F.site_object_ownership({"labels": self.LABELS, "objects": [
            {"kind": "ClusterUserDefinedNetwork", "name": "hn51003", "existing": cudn},
            {"kind": "RouteAdvertisements", "name": "hn51003", "existing": ra}]})

    def test_every_state(self):
        other = dict(self.LABELS, **{"hybridsovereign.redhat/entity": "acme"})
        cases = [(None, None, True), (self.LABELS, self.LABELS, True), (self.LABELS, None, True),
                 ({}, None, False), (other, self.LABELS, False), (self.LABELS, {"x": "y"}, False)]
        for cudn, ra, ok in cases:
            with self.subTest(cudn=cudn, ra=ra):
                out = self.decide(cudn, ra)
                self.assertEqual(out["ok"], ok)
                if not ok:
                    self.assertEqual(out["reason"], "NameTakenOnSite")
                    self.assertIn("hn51003", out["message"])


if __name__ == "__main__":
    unittest.main()

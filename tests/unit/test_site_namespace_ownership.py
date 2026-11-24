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

    # recorded by this environment's status and without the platform's labels (someone with rights on the site's
    # Namespace objects removed them): the environment's, labelled again by the apply
    RECORDED = dict(EXPECTED, unlabelled="", **{"the site's own": ""})

    def test_every_state(self):
        for recorded in (False, True):
            rec = [{"name": "vms", "namespace": "acme-corp--prod--vms"}] if recorded else []
            ns = wanted("acme-corp", "prod", recorded=rec)
            for site, have in self.ON_SITE.items():
                with self.subTest(site=site, recorded=recorded):
                    existing = {} if have is None else {"acme-corp--prod--vms": have}
                    out = F.namespace_ownership({"namespaces": ns, "existing": existing,
                                                 "recorded": [r["namespace"] for r in rec]})
                    want = (self.RECORDED if recorded else self.EXPECTED)[site]
                    self.assertEqual((out["ok"], out["reason"]), (want == "", want))
                    if want:
                        self.assertEqual(out["refused"], ["acme-corp--prod--vms"])
                        self.assertNotIn("acme ", out["message"])      # another tenant is never named
                    else:
                        relabel = recorded and site in ("unlabelled", "the site's own")
                        self.assertEqual(out["relabelled"], ["acme-corp--prod--vms"] if relabel else [])

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


def cv_labels(entity, cv, name):
    """The labels the platform writes on a site namespace (the release before wrote the same keys)."""
    return dict(F.site_labels(entity, cv), **{F.CV_NAMESPACE_LABEL: name})


class RecordLost(unittest.TestCase):
    """A namespace the status does not record (status lost; an object restored without status; a pending write that
    recorded the new form before the site was read): the site's namespace that carries this environment's labels and
    names the spec's namespace is kept and recorded, never a second one made, and never removed as stale. Every
    state: what the site has (nothing; the release before's acme-corp-prod-vms with this environment's labels; the
    new form with them; both; the old form with another environment's or another tenant's labels; an unlabelled one
    of the new name) x what the status recorded (nothing, the old name, the new name)."""
    OLD, NEW = "acme-corp-prod-vms", "acme-corp--prod--vms"
    SITE = {
        "nothing": [],
        "old form, ours": [("acme-corp-prod-vms", cv_labels("acme-corp", "prod", "vms"))],
        "new form, ours": [("acme-corp--prod--vms", cv_labels("acme-corp", "prod", "vms"))],
        "both, ours": [("acme-corp-prod-vms", cv_labels("acme-corp", "prod", "vms")),
                       ("acme-corp--prod--vms", cv_labels("acme-corp", "prod", "vms"))],
        "old form, another environment's": [("acme-corp-prod-vms", cv_labels("acme-corp", "test", "vms"))],
        "old form, another tenant's": [("acme-corp-prod-vms", cv_labels("acme", "corp-prod", "vms"))],
        "new form, unlabelled": [("acme-corp--prod--vms", {})],
    }
    # (site, recorded) -> (namespace chosen, error expected)
    EXPECTED = {
        ("nothing", "none"): (NEW, False), ("nothing", "old"): (OLD, False), ("nothing", "new"): (NEW, False),
        ("old form, ours", "none"): (OLD, False), ("old form, ours", "old"): (OLD, False),
        ("old form, ours", "new"): (OLD, False),
        ("new form, ours", "none"): (NEW, False), ("new form, ours", "old"): (NEW, False),
        ("new form, ours", "new"): (NEW, False),
        ("both, ours", "none"): (NEW, True), ("both, ours", "old"): (OLD, False), ("both, ours", "new"): (NEW, False),
        ("old form, another environment's", "none"): (NEW, False),
        ("old form, another environment's", "old"): (OLD, False),
        ("old form, another environment's", "new"): (NEW, False),
        ("old form, another tenant's", "none"): (NEW, False), ("old form, another tenant's", "old"): (OLD, False),
        ("old form, another tenant's", "new"): (NEW, False),
        ("new form, unlabelled", "none"): (NEW, False), ("new form, unlabelled", "old"): (OLD, False),
        ("new form, unlabelled", "new"): (NEW, False),
    }

    def test_every_state(self):
        for site, objs in self.SITE.items():
            for recorded in ("none", "old", "new"):
                with self.subTest(site=site, recorded=recorded):
                    rec = {"none": [], "old": [{"name": "vms", "namespace": self.OLD}],
                           "new": [{"name": "vms", "namespace": self.NEW}]}[recorded]
                    out = F.cloudvirt_namespaces({"entity": "acme-corp", "cloudvirt": "prod",
                                                  "namespaces": [{"name": "vms"}], "recorded": rec,
                                                  "site": [{"name": n, "labels": lb} for n, lb in objs]})
                    ns, err = self.EXPECTED[(site, recorded)]
                    self.assertEqual(out["namespaces"][0]["namespace"], ns)
                    self.assertEqual(bool(out["errors"]), err)
                    # what the site holds with this environment's labels for a namespace of the spec: never stale
                    ours = [n for n, lb in objs if lb.get("hybridsovereign.redhat/cloudvirt") == "prod"
                            and lb.get("hybridsovereign.redhat/entity") == "acme-corp"]
                    self.assertEqual(out["claimed"], sorted(ours))
                    # the chosen namespace is then checked as before: a foreign one is refused, never merged into
                    have = dict(objs)
                    own = F.namespace_ownership({"namespaces": out["namespaces"],
                                                 "existing": {ns: have[ns]} if ns in have else {}})
                    foreign = ns in have and not (have[ns].get("hybridsovereign.redhat/cloudvirt") == "prod"
                                                  and have[ns].get("hybridsovereign.redhat/entity") == "acme-corp")
                    self.assertEqual(own["ok"], not foreign)

    def test_a_namespace_that_left_the_spec_is_not_claimed(self):
        out = F.cloudvirt_namespaces({"entity": "acme-corp", "cloudvirt": "prod", "namespaces": [{"name": "db"}],
                                      "site": [{"name": self.OLD, "labels": cv_labels("acme-corp", "prod", "vms")}]})
        self.assertEqual(out["claimed"], [])
        self.assertEqual(out["namespaces"][0]["namespace"], "acme-corp--prod--db")

    def test_the_job_never_removes_a_claimed_namespace(self):
        with open(os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudvirt_provision", "tasks", "main.yml")) as f:
            main = f.read()
        self.assertIn("| difference(cv_claimed) | list", main)
        # the site is read before the mapping that decides the names (the namespaces are applied after it)
        self.assertLess(main.index("List the environment's site namespaces"),
                        main.index("Map the namespaces to the site's namespaces"))
        self.assertLess(main.index("Map the namespaces to the site's namespaces"),
                        main.index("Ensure the site namespaces"))



class Readers(unittest.TestCase):
    """What others read in a virtualization environment's status.namespaces: only site namespaces it made (a run
    that only waits writes no name it has not made), and a placement reads those names instead of rebuilding them."""

    def test_a_waiting_run_records_only_names_it_made(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from play_helper import HAVE_ANSIBLE, by_name, load, run_cases
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        task = by_name(load(os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudvirt_provision", "tasks",
                                         "main.yml")), "Report what the environment waits for")
        probe = {"ansible.builtin.set_fact": {"_got": task["vars"]["fab_status"]},
                 "vars": {k: v for k, v in task["vars"].items() if k in ("_recorded", "_ns")}}
        status = [{"name": "vms", "namespace": "acme-corp-prod-vms", "ready": True, "message": "m"},
                  {"name": "db", "namespace": "acme-corp--prod--db", "ready": False, "message": "w"}]
        cases = [{"rec": [{"name": "vms", "namespace": "acme-corp-prod-vms"}]}, {"rec": []}]
        got = run_cases([probe], cases, "_got.namespaces",
                        extra_vars={"cv_ns_status": status, "cv_status": {"entity": "acme-corp"},
                                    "fab_st": "{{ {'namespaces': case.rec} }}"})
        self.assertEqual(got[0], [status[0], {"name": "db", "ready": False, "message": "w"}])
        self.assertEqual(got[1], [{"name": "vms", "ready": True, "message": "m"},
                                  {"name": "db", "ready": False, "message": "w"}])

    def test_the_placement_reads_the_recorded_names(self):
        with open(os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks", "main.yml")) as f:
            main = f.read()
        part = main[main.index("Map the CloudVirt's namespaces on this network"):]
        part = part[:part.index("- name:", 10)]
        self.assertIn("recorded: \"{{ (np_backend.status | default({}, true)).namespaces", part)
        # the function then keeps an earlier release's recorded name instead of the new form
        out = F.cloudvirt_namespaces({"entity": "acme-corp", "cloudvirt": "prod", "network": "core",
                                      "namespaces": [{"name": "vms", "network": "core"}],
                                      "recorded": [{"name": "vms", "namespace": "acme-corp-prod-vms"}]})
        self.assertEqual(out["namespaces"][0]["namespace"], "acme-corp-prod-vms")


if __name__ == "__main__":
    unittest.main()

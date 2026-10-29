"""A paused site (CloudInfrastructure spec.paused): the API field, the site job's early exit, the resolver's facts,
the dependants' SitePaused reason, the teardown hold, the orphan check's "not checked (paused)" line and the status
contract's Paused phase. YAML inspection plus the resolver's fact tasks in ansible-playbook (skipped without it)."""
import glob
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

MU = os.path.join(ROOT, "eda/rulebooks/collections/ansible_collections/hybridsovereign/shc/plugins/module_utils")
sys.path.insert(0, MU)
import shc_status as S  # noqa: E402
import shc_status_reasons as R  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROLES = os.path.join(ROOT, "eda/rulebooks/roles")
COMMON = os.path.join(ROOT, "eda/common/tasks")
CRD_BASE = os.path.join(ROOT, "operator/config/crd/bases/crd-cloudinfrastructure.yaml")
CRD_HELM = os.path.join(ROOT, "operator/primary/helm/templates/crds.yaml")


def _tasks(path):
    return walk(load(path))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class Api(unittest.TestCase):
    def test_spec_paused_in_both_crd_copies(self):
        for path in (CRD_BASE, CRD_HELM):
            with open(path) as fh:
                text = fh.read()
            self.assertIn("paused:", text, path)
        crd = yaml.safe_load(open(CRD_BASE))
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        self.assertEqual(spec["paused"]["type"], "boolean")
        self.assertIn("stops probing and realizing", spec["paused"]["description"])
        # API-only text: no plan phases, dates or lab names
        for word in ("OD1", "2026", "iaas1", "edge1", "rhoso", "cluster-power"):
            self.assertNotIn(word, spec["paused"]["description"])


class StatusContract(unittest.TestCase):
    def test_paused_phase_from_reason_and_legacy_status(self):
        site = {"metadata": {"name": "s", "generation": 3},
                "status": {"status": "paused", "ready": False, "observedGeneration": 3,
                           "conditions": [{"type": "Ready", "status": "False", "reason": "Paused", "message": "m"}]}}
        self.assertEqual(S.derive_phase(site), "Paused")
        held = {"metadata": {"name": "p", "generation": 1},
                "status": {"status": "pending", "ready": False, "realized": True, "observedGeneration": 1,
                           "conditions": [{"type": "Ready", "status": "False", "reason": "SitePaused", "message": "m"}]}}
        self.assertEqual(S.derive_phase(held), "Paused")
        legacy = {"metadata": {"name": "l", "generation": 1}, "status": {"status": "paused", "ready": False}}
        self.assertEqual(S.derive_phase(legacy), "Paused")
        # deletion and a stalled teardown still win
        self.assertEqual(S.derive_phase(dict(site, metadata={"name": "s", "deletionTimestamp": "2026-01-01T00:00:00Z"})), "Deleting")

    def test_registry_and_summaries(self):
        self.assertEqual(R.REASONS["Paused"]["class"], R.PAUSED)
        self.assertEqual(R.REASONS["SitePaused"]["class"], R.PAUSED)
        self.assertIn("Paused", S.PHASES)
        self.assertIn("Paused", R.PHASE_SUMMARIES)
        for text in (R.REASONS["Paused"]["summary"], R.REASONS["SitePaused"]["summary"], R.PHASE_SUMMARIES["Paused"]):
            self.assertIsNone(S.BANNED_SUMMARY.search(text), text)
            self.assertLessEqual(len(text), S.SUMMARY_MAX)
        self.assertEqual(S._LEGACY_READY["paused"], ("False", "Paused"))

    def test_shared_fixture_has_paused_cases(self):
        with open(os.path.join(ROOT, "tests/fixtures/status-phase-cases.json")) as fh:
            cases = json.load(fh)
        paused = [c for c in cases if c["expect"]["phase"] == "Paused"]
        self.assertGreaterEqual(len(paused), 2)
        for c in paused:
            self.assertEqual(S.derive_phase(c["object"]), "Paused", c["name"])

    def test_ui_copies_in_step(self):
        chart = os.path.join(ROOT, "charts/charts/shc-cluster-base/files")
        for f in ("shc_status.py", "shc_status_reasons.py"):
            with open(os.path.join(MU, f)) as a, open(os.path.join(chart, f)) as b:
                self.assertEqual(a.read(), b.read(), f + " (run scripts/sync-shc-status.sh)")
        gen = open(os.path.join(ROOT, "ui/packages/shared/src/status/reasons.generated.ts")).read()
        self.assertIn('"SitePaused"', gen)
        self.assertIn("'paused'", gen.splitlines()[4], "ReasonClass union")
        phase_ts = open(os.path.join(ROOT, "ui/packages/shared/src/status/phase.ts")).read()
        self.assertIn("'Paused'] as const", phase_ts)
        self.assertIn("if (legacy === 'paused' || rclass === 'paused') return 'Paused';", phase_ts)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class SiteJob(unittest.TestCase):
    def test_paused_block_comes_before_the_probe_and_ends_the_play(self):
        tasks = load(os.path.join(ROLES, "cloudinfrastructure_provision/tasks/main.yml"))
        block = tasks[0]["block"]
        names = [t.get("name", "") for t in block]
        i = names.index("A paused site is recorded as paused and left alone")
        self.assertLess(i, names.index("Set the unsupported-type failure reason"))
        paused = block[i]
        self.assertIn("paused", paused["when"])
        inner = paused["block"]
        mark = next(t for t in inner if t.get("name") == "Mark CloudInfrastructure paused")
        body = mark["vars"]["cr_status_body"]
        self.assertEqual(body["status"], "paused")
        self.assertFalse(body["ready"])
        ready = next(c for c in body["conditions"] if c["type"] == "Ready")
        self.assertEqual((ready["status"], ready["reason"]), ("False", "Paused"))
        # nothing about capabilities: the last probe's values stay
        self.assertNotIn("capabilities", body)
        self.assertTrue(any("end_play" in str(t.get("ansible.builtin.meta", "")) for t in inner))
        self.assertTrue(any("tenant_view.yml" in str(t.get("ansible.builtin.include_tasks", "")) for t in inner))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class Dependants(unittest.TestCase):
    def test_provision_roles_name_site_paused(self):
        for role, var in (("networkplacement_provision", "np_reason"), ("cloudgateway_provision", "cg_reason"),
                          ("cloudvirt_provision", "cv_reason"), ("cloudaws_provision", "ca_reason"),
                          ("cloudgcp_provision", "ca_reason")):
            text = open(os.path.join(ROLES, role, "tasks/main.yml")).read()
            self.assertIn("'SitePaused' if (ci_paused | default(false) | bool)", text, role)
            self.assertIn(var, text, role)
        text = open(os.path.join(ROLES, "cloudoso_provision/tasks/cloudref.yml")).read()
        self.assertIn("cloudoso_failure_reason: SitePaused", text)

    def test_teardown_roles_hold(self):
        for role in ("networkplacement_teardown", "cloudvirt_teardown", "cloudgateway_teardown", "cloudoso_teardown"):
            found = [t for t in _tasks(os.path.join(ROLES, role, "tasks/main.yml"))
                     if "site_paused_hold.yml" in str(t.get("ansible.builtin.include_tasks", ""))]
            self.assertEqual(len(found), 1, role)
            self.assertIn("ci_paused", str(found[0]["when"]), role)
            self.assertIn("sph_plural", found[0]["vars"], role)
        hold = load(os.path.join(COMMON, "site_paused_hold.yml"))
        body = hold[0]["vars"]["cr_status_body"]
        self.assertEqual(body["conditions"][0]["reason"], "SitePaused")
        self.assertTrue(body["deletionBlockedBy"][0].startswith("CloudInfrastructure "))
        self.assertEqual(hold[-1]["ansible.builtin.meta"], "end_play")

    def test_orphan_check_skips_a_paused_site_plainly(self):
        tasks = load(os.path.join(ROLES, "fabric_orphan_check/tasks/site.yml"))
        names = [t.get("name", "") for t in tasks]
        self.assertIn("Site {{ foc_gw.spec.cloudRef.name }} is paused: not checked", names)
        for t in tasks:
            if t.get("name", "").endswith("(OpenShift)\"") or "(OpenShift)" in t.get("name", "") or "(OpenStack)" in t.get("name", ""):
                self.assertIn("ci_paused", t["when"], t["name"])

    def test_gitops_lets_the_switch_be_flipped_live(self):
        vap = open(os.path.join(ROOT, "operator/primary/helm/templates/gitops-policies.yaml")).read()
        self.assertIn("name: pauseOnly", vap)
        self.assertIn("oldObject.kind == 'CloudInfrastructure'", vap)
        self.assertIn("object.spec.filter(k, k != 'paused').all(k, k in oldObject.spec && object.spec[k] == oldObject.spec[k])", vap)
        self.assertIn("variables.override || variables.pauseOnly", vap)
        app = open(os.path.join(ROOT, "bootstrap/helm/central/templates/hybridSovereignOperators/primary-operator-application.yaml")).read()
        self.assertIn("kind: CloudInfrastructure\n      jsonPointers:\n        - /spec/paused", app)

    def test_tenant_summary_unavailable_when_paused(self):
        text = open(os.path.join(COMMON, "site_tenant_summary.yml")).read()
        self.assertIn("and not (spec.paused | default(false) | bool)", text)


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class Resolver(unittest.TestCase):
    """The fact tasks of resolve_cloud_infrastructure.yml with a fake lookup result."""

    def _facts(self, obj):
        tasks = load(os.path.join(COMMON, "resolve_cloud_infrastructure.yml"))
        reset = by_name(tasks, "Reset CloudInfrastructure facts")
        setf = by_name(tasks, "Set CloudInfrastructure facts")
        err = by_name(tasks, "Record the resolution error")
        # ansible-core 2.19 renders an all-control template to None, 2.16 (the runner image) to ''
        out = run_play([reset, setf, err, write_out("{'usable': ci_usable, 'paused': ci_paused, 'error': ci_error | default('', true) | trim}")],
                       {"_ci_lookup": {"resources": [obj]}, "ci_ref_name": obj["metadata"]["name"]})
        return out

    def test_paused_site_is_unusable_with_a_plain_error(self):
        site = {"metadata": {"name": "site-a"}, "spec": {"type": "openshift", "paused": True},
                "status": {"status": "paused", "capabilities": {"api": True, "evpn": True}}}
        f = self._facts(site)
        self.assertTrue(f["paused"])
        self.assertFalse(f["usable"])
        self.assertEqual(f["error"], "CloudInfrastructure site-a is paused")

    def test_unpaused_site_is_usable(self):
        site = {"metadata": {"name": "site-a"}, "spec": {"type": "openshift"},
                "status": {"status": "ready", "capabilities": {"api": True, "evpn": True}}}
        f = self._facts(site)
        self.assertFalse(f["paused"])
        self.assertTrue(f["usable"])
        self.assertEqual(f["error"], "")


if __name__ == "__main__":
    unittest.main()

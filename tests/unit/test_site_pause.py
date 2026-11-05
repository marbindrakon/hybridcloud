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


OPERATOR_COMMON = os.path.join(ROOT, "operator/roles/_common/tasks")
HOLD_ROLES = {
    "operator/primary/roles/cloudinfrastructure/tasks/main.yml": "cloudinfrastructure",
    "operator/primary/roles/cloudgateway/tasks/main.yml": "cloudgateway",
    "operator/namespace/roles/networkplacement/tasks/main.yml": "networkplacement",
    "operator/namespace/roles/cloudvirt/tasks/main.yml": "cloudvirt",
    "operator/namespace/roles/cloudaws/tasks/main.yml": "cloudaws",
    "operator/namespace/roles/cloudgcp/tasks/main.yml": "cloudgcp",
    "operator/namespace/roles/cloudoso/tasks/main.yml": "cloudoso",
}


def _obj(reason, generation=2, observed=2, paused=None, deleting=False, status="pending"):
    meta = {"name": "o", "generation": generation}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-02T10:00:00Z"
    spec = {} if paused is None else {"paused": paused}
    return {"metadata": meta, "spec": spec,
            "status": {"status": status, "observedGeneration": observed,
                       "conditions": [{"type": "Ready", "status": "False", "reason": reason, "message": "m"}]}}


@unittest.skipIf(yaml is None, "PyYAML not installed")
class QuietWhilePausedWiring(unittest.TestCase):
    """A paused site's objects publish no event on their reconcile period (V11 stream H, issue 1)."""

    def test_every_held_kind_skips_before_the_in_flight_check(self):
        for path, kind in HOLD_ROLES.items():
            tasks = load(os.path.join(ROOT, path))
            names = [t.get("name", "") for t in tasks[0]["block"]]
            hold = [t for t in tasks[0]["block"]
                    if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") == "skip_while_site_paused"]
            self.assertEqual(len(hold), 1, path)
            i = names.index(hold[0]["name"])
            skip = next(n for n in names if n.startswith("Skip when already processed"))
            self.assertLess(names.index(skip), i, path)
            self.assertLess(i, names.index("Skip while the last event is in flight"), path)
            self.assertEqual(hold[0]["vars"]["paused_hold_cr"], "{{ _hybridsovereign_redhat_%s }}" % kind, path)
            self.assertEqual(hold[0]["vars"]["paused_hold_force"], "{{ reconcile_now | bool }}", path)

    def test_the_site_job_wakes_what_the_pause_held(self):
        tasks = load(os.path.join(ROLES, "cloudinfrastructure_provision/tasks/main.yml"))
        wakes = [t for t in walk(tasks) if t.get("ansible.builtin.include_tasks") == "wake_dependents.yml"]
        self.assertEqual(len(wakes), 2)
        ready, failed = wakes
        self.assertTrue(ready["vars"]["ciw_pending"])
        self.assertFalse(failed["vars"]["ciw_pending"])
        self.assertIn("not (ci_cr_spec.paused | default(false) | bool)", failed["when"])
        self.assertIn(failed, tasks[0]["rescue"])
        # the paused path ends before the wake
        names = [t.get("name", "") for t in tasks[0]["block"]]
        self.assertLess(names.index("A paused site is recorded as paused and left alone"), names.index(ready["name"]))
        wake = load(os.path.join(ROLES, "cloudinfrastructure_provision/tasks/wake_dependents.yml"))
        listing = by_name(wake, "List what may wait for this site")
        self.assertEqual(set(listing["loop"]), {"CloudGateway", "CloudAWS", "CloudVirt", "CloudGCP", "CloudOSO",
                                                "NetworkPlacement"})
        # a failed listing is retried, then named (ci_wake_error) and fails the job; never masked
        self.assertNotIn("failed_when", listing)
        self.assertEqual((listing["retries"], listing["until"]), (3, "_ci_dependents is not failed"))
        self.assertTrue(listing["ignore_errors"])
        self.assertIn("ci_wake_error", by_name(wake, "Name the kinds that could not be listed")["ansible.builtin.set_fact"])

    def test_the_site_job_fails_when_the_wake_could_not_list(self):
        tasks = load(os.path.join(ROLES, "cloudinfrastructure_provision/tasks/main.yml"))
        block, rescue = tasks[0]["block"], tasks[0]["rescue"]
        names = [t.get("name", "") for t in block]
        fail = "Fail the job: what waits for this site could not be listed"
        # after the site's own work: the wake and the DNS health ran first
        self.assertEqual(names[-1], fail)
        self.assertLess(names.index("Wake what waits for this site"), names.index(fail))
        inner = block[-1]["block"]
        self.assertEqual(inner[0]["ansible.builtin.set_fact"], {"ci_only_wake_failed": True})
        self.assertEqual(inner[-1]["ansible.builtin.fail"]["msg"], "{{ ci_wake_error }}")
        self.assertIn("ci_wake_error", block[-1]["when"])
        # the rescue leaves the ready site alone (no failed write, capabilities kept, no wake); failed_capabilities
        # stays its first task (tests/tenantprofile/site-summary.yml)
        self.assertEqual(rescue[0].get("ansible.builtin.include_tasks"), "failed_capabilities.yml")
        for name in ("Every capability of the failed site is off", "Mark CloudInfrastructure failed",
                     "Refresh the tenants' view of the failed site", "Wake what the pause held on the failed site"):
            when = by_name(rescue, name)["when"]
            self.assertIn("not (ci_only_wake_failed | default(false) | bool)",
                          when if isinstance(when, list) else [when], name)
        self.assertIn("ci_wake_error", by_name(rescue, "Re-raise")["ansible.builtin.fail"]["msg"])

    def test_namespace_operators_may_read_the_sites(self):
        """The hold reads the site's CloudInfrastructure from both operators: the primary one has every kind of the
        group; the namespace operators' shared ClusterRole needs get on cloudinfrastructures (get only)."""
        docs = [d for d in yaml.safe_load_all(open(os.path.join(ROOT, "operator/primary/helm/templates/clusterrole.yaml"))
                                                .read().replace("{{- include \"primary-operator.labels\" . | nindent 4 }}", "")
                                                .replace("{{ .Values.serviceAccount.name }}", "sa")
                                                .replace("{{ .Values.namespace }}", "ns")) if d]
        roles = {d["metadata"]["name"]: d["rules"] for d in docs if d["kind"] == "ClusterRole"}
        primary = [r for r in roles["hybridsovereign-primary-operator"] if "hybridsovereign.redhat" in r["apiGroups"]]
        self.assertTrue(any("*" in r["resources"] and "get" in r["verbs"] for r in primary))
        ns = [r for r in roles["hybridsovereign-namespace-operator"] if "cloudinfrastructures" in r["resources"]]
        self.assertEqual(len(ns), 1)
        self.assertEqual((ns[0]["apiGroups"], ns[0]["verbs"]), (["hybridsovereign.redhat"], ["get"]))

    def test_hold_comment_names_the_wake(self):
        with open(os.path.join(COMMON, "site_paused_hold.yml")) as fh:
            text = fh.read()
        self.assertIn("wake_dependents.yml", text)
        self.assertNotIn("The operator's requeue\n# retries", text)


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class QuietWhilePaused(unittest.TestCase):
    """The decisions in ansible-playbook: the operator's hold, the held deletion and the site job's wake."""

    @staticmethod
    def _site_files(site):
        """site_paused_now.yml with its CloudInfrastructure read replaced by the given answer (a site object, None = not
        found, "error" = the read failed); the decision tasks are the shipped ones."""
        tasks = load(os.path.join(OPERATOR_COMMON, "site_paused_now.yml"))
        read = by_name(tasks, "Read the site's CloudInfrastructure")
        answer = ({"failed": True, "msg": "forbidden"} if site == "error"
                  else {"resources": [] if site is None else [site]})
        stub = {"name": read["name"], "ansible.builtin.set_fact": {"_site_paused_ci": answer}, "when": read["when"]}
        return {"site_paused_now.yml": [stub if t is read else t for t in tasks]}

    def _hold(self, obj, force=False, site=None, out="_paused_hold | bool"):
        tasks = load(os.path.join(OPERATOR_COMMON, "skip_while_site_paused.yml"))
        decide = [t for t in tasks if "ansible.builtin.meta" not in t]
        return run_play(decide + [write_out(out)], {"paused_hold_cr": obj, "paused_hold_force": force},
                        files=self._site_files(site))

    def _site(self, paused):
        return {"metadata": {"name": "edge1"}, "spec": {"paused": paused}}

    def test_held_dependant_is_quiet(self):
        held = _obj("SitePaused")
        held["spec"]["cloudRef"] = {"name": "edge1"}
        out = self._hold(held, site=self._site(True),
                         out="{'hold': _paused_hold | bool, 'site': site_paused_site | default('')}")
        self.assertEqual(out, {"hold": True, "site": "edge1"})

    def test_a_held_dependant_resumes_without_the_wake(self):
        """The site is read at decision time: once it is resumed, or when it cannot be read, nothing holds the object
        (a lost wake costs one reconcilePeriod)."""
        held = _obj("SitePaused")
        held["spec"]["cloudRef"] = {"name": "edge1"}
        self.assertFalse(self._hold(held, site=self._site(False)), "resumed")
        self.assertFalse(self._hold(held, site=None), "site not found")
        self.assertFalse(self._hold(held, site="error"), "site read failed")
        no_site = _obj("SitePaused")
        self.assertFalse(self._hold(no_site, site=self._site(True)), "no site named")

    def test_the_site_of_each_kind(self):
        name = "{'hold': _paused_hold | bool, 'site': site_paused_site | default('')}"
        placement = _obj("SitePaused")
        placement["status"]["site"] = "edge1"
        oso = _obj("SitePaused", status="failed")
        oso["status"]["cloudInfrastructure"] = "edge1"
        gateway = _obj("SitePaused")
        gateway["spec"]["cloudRef"] = {"name": "edge1"}
        gateway["status"]["site"] = {"name": "edge1", "evpn": True}  # a gateway's status.site is a mapping
        for obj in (placement, oso, gateway):
            self.assertEqual(self._hold(obj, site=self._site(True), out=name), {"hold": True, "site": "edge1"})

    def test_paused_site_is_quiet(self):
        # a CloudInfrastructure's own spec.paused is current: no read
        out = self._hold(_obj("Paused", paused=True, status="paused"), site="error",
                         out="{'hold': _paused_hold | bool, 'site': site_paused_site | default('')}")
        self.assertEqual(out, {"hold": True, "site": ""})

    def test_spec_change_retry_now_and_delete_still_publish(self):
        paused = self._site(True)
        self.assertFalse(self._hold(_obj("SitePaused", generation=3, observed=2), site=paused), "spec change")
        self.assertFalse(self._hold(_obj("SitePaused"), force=True, site=paused), "Retry now / a wake")
        self.assertFalse(self._hold(_obj("SitePaused", deleting=True), site=paused), "delete")
        # resuming the site is a spec change; a Paused record without spec.paused never holds
        self.assertFalse(self._hold(_obj("Paused", generation=5, observed=4, paused=False, status="paused")))
        self.assertFalse(self._hold(_obj("Paused", paused=False, status="paused")))

    def test_other_waits_and_ready_objects_are_not_held(self):
        for reason in ("WaitingForDependencies", "WaitingForGateway", "Ready", "EDAFailed", ""):
            self.assertFalse(self._hold(_obj(reason)), reason)
        self.assertFalse(self._hold({}), "empty")
        self.assertFalse(self._hold(_obj("SitePaused", generation=0, observed=0)), "no generation")

    def _publish_skip(self, status, annotations=None, reason="NetworkPlacementDeleteRequested", reconcile_now=False,
                      site=None, out="_publish_skip | bool"):
        tasks = load(os.path.join(OPERATOR_COMMON, "publish_operator_event.yml"))
        decide = [by_name(tasks, n) for n in ("Tell whether the deletion reports a hold by a paused site",
                                              "Read whether the held deletion's site is paused now",
                                              "Decide whether to publish")]
        cr = {"metadata": {"name": "p", "annotations": annotations or {}, "deletionTimestamp": "2026-11-02T10:00:00Z"},
              "status": status}
        return run_play(decide + [write_out(out)],
                        {"operator_event_reason": reason, "_publish_cr": {"resources": [cr]},
                         "reconcile_now": reconcile_now},
                        files=self._site_files(self._site(True) if site is None else site))

    def _held_delete(self, event="e2", completed="e2", reason="SitePaused",
                     blocker="CloudInfrastructure edge1 (paused)"):
        return {"delivery": {"eventId": event, "reason": "NetworkPlacementDeleteRequested",
                             "publishedAt": "2026-11-02T10:00:01Z", "completedEventId": completed},
                "deletionBlockedBy": [blocker] if blocker else [],
                "conditions": [{"type": "Ready", "status": "False", "reason": reason, "message": "m"}]}

    def test_held_deletion_is_not_published_again(self):
        # the site named by the blocker is read, and still paused
        out = self._publish_skip(self._held_delete(), out="{'skip': _publish_skip | bool, 'site': site_paused_site}")
        self.assertEqual(out, {"skip": True, "site": "edge1"})

    def test_held_deletion_resumes_without_the_wake(self):
        """Once its site is no longer paused (or cannot be read), the first requeue publishes the held deletion."""
        self.assertFalse(self._publish_skip(self._held_delete(), site=self._site(False)), "resumed")
        self.assertFalse(self._publish_skip(self._held_delete(), site="error"), "site read failed")

    def test_held_deletion_resumes_on_a_wake(self):
        self.assertFalse(self._publish_skip(self._held_delete(), reconcile_now=True))
        self.assertFalse(self._publish_skip(self._held_delete(), annotations={"hybridsovereign.redhat/redeliver": "true"}))

    def test_other_finished_deletions_are_published_again(self):
        self.assertFalse(self._publish_skip(self._held_delete(reason="EDAFailed", blocker="")), "a failed teardown")
        self.assertFalse(self._publish_skip(self._held_delete(blocker="NetworkPlacement x")), "another blocker")
        # a create event never takes the delete branch
        self.assertFalse(self._publish_skip(self._held_delete(), reason="NetworkPlacementCreateRequested"))

    def _wake(self, results, pending=True, site="edge1"):
        tasks = load(os.path.join(ROLES, "cloudinfrastructure_provision/tasks/wake_dependents.yml"))
        template = by_name(tasks, "Wake the ones pending on it")["vars"]["nudge_crs"]
        out = run_play([{"ansible.builtin.set_fact": {"_sel": template}}, write_out("_sel")],
                       {"_ci_dependents": {"results": [{"item": k, "resources": v} for k, v in results.items()]},
                        "cloudinfrastructure_cr_name": site, "ciw_pending": pending})
        return sorted("%s/%s" % (o["kind"], o["name"]) for o in out)

    def _dep(self, name, reason, site=None, status="pending", deleting=False, status_site=None, oso_site=None):
        o = _obj(reason, status=status, deleting=deleting)
        o["metadata"].update({"name": name, "namespace": "entity-a"})
        if site:
            o["spec"]["cloudRef"] = {"name": site}
        if status_site:
            o["status"]["site"] = status_site
        if oso_site:
            o["status"]["cloudInfrastructure"] = oso_site
        return o

    def test_wake_after_a_ready_probe(self):
        results = {
            "CloudGateway": [self._dep("edge1", "SitePaused", site="edge1"),
                             self._dep("other", "SitePaused", site="iaas1"),
                             self._dep("ready", "Ready", site="edge1", status="ready")],
            "CloudVirt": [self._dep("waits", "WaitingForDependencies", site="edge1"),
                          self._dep("gone", "SitePaused", site="edge1", deleting=True)],
            "CloudAWS": [self._dep("leaving", "WaitingForDependencies", site="edge1", deleting=True)],
            "CloudGCP": [self._dep("g", "SitePaused", site="edge1"),
                         self._dep("gw", "WaitingForDependencies", site="edge1")],
            "CloudOSO": [self._dep("by-ref", "SitePaused", site="edge1", status="failed"),
                         self._dep("by-default", "SitePaused", status="failed", oso_site="edge1"),
                         self._dep("failed", "EDAFailed", site="edge1", status="failed")],
            "NetworkPlacement": [self._dep("core-edge1", "SitePaused", status_site="edge1"),
                                 self._dep("core-iaas1", "SitePaused", status_site="iaas1"),
                                 self._dep("waits", "WaitingForGateway", status_site="edge1")],
        }
        self.assertEqual(self._wake(results), [
            "CloudGCP/g", "CloudGateway/edge1", "CloudOSO/by-default", "CloudOSO/by-ref", "CloudVirt/gone",
            "CloudVirt/waits", "NetworkPlacement/core-edge1"])
        # after a failed probe only what the pause held
        self.assertEqual(self._wake(results, pending=False), [
            "CloudGCP/g", "CloudGateway/edge1", "CloudOSO/by-default", "CloudOSO/by-ref", "CloudVirt/gone",
            "NetworkPlacement/core-edge1"])

    def test_a_failed_listing_is_named_and_the_rest_still_woken(self):
        self.assertEqual(self._wake({"CloudGateway": [], "NetworkPlacement": []}), [])
        tasks = load(os.path.join(ROLES, "cloudinfrastructure_provision/tasks/wake_dependents.yml"))
        name = by_name(tasks, "Name the kinds that could not be listed")
        results = [{"item": "CloudGateway", "failed": True, "attempts": 4, "msg": "Failed to get client due to 429"},
                   {"item": "NetworkPlacement", "failed": False,
                    "resources": [self._dep("core-edge1", "SitePaused", status_site="edge1")]},
                   {"item": "CloudOSO", "failed": True, "msg": "forbidden"}]
        out = run_play([name, {"ansible.builtin.set_fact": {"_sel": by_name(tasks, "Wake the ones pending on it")[
            "vars"]["nudge_crs"]}}, write_out("{'error': ci_wake_error, 'woken': _sel}")],
            {"_ci_dependents": {"results": results}, "cloudinfrastructure_cr_name": "edge1", "ciw_pending": True})
        self.assertEqual(out["error"], "could not list CloudGateway, CloudOSO to wake what waits for site edge1: "
                                       "Failed to get client due to 429")
        self.assertEqual([o["name"] for o in out["woken"]], ["core-edge1"])
        # every kind listed: no error
        out = run_play([name, write_out("ci_wake_error")],
                       {"_ci_dependents": {"results": [results[1]]}, "cloudinfrastructure_cr_name": "edge1"})
        self.assertEqual(out, "")

    def test_cloudoso_held_write_carries_its_generation(self):
        """The CloudOSO holds through its failure write: with SitePaused it records the generation it holds (so the
        controller's hold engages, as for the other kinds), with any other failure it leaves the field alone."""
        tasks = load(os.path.join(ROLES, "cloudoso_provision/tasks/main.yml"))
        body = by_name(tasks[0]["rescue"], "Mark CloudOSO as failed on services cluster")["vars"]["cr_status_body"]
        expr = body["observedGeneration"]
        for reason, want in (("SitePaused", "4"), ("EDAFailed", ""), (None, "")):
            extra = {"cloudoso_generation": 4}
            if reason:
                extra["cloudoso_failure_reason"] = reason
            got = run_play([{"ansible.builtin.set_fact": {"_g": expr}}, write_out("_g | string")], extra)
            self.assertEqual(str(got).strip(), want, reason)
        # the status library keeps a generation and drops ''
        for value, kept in (("4", 4), ("", None)):
            patch, _ = S.compute_status_patch({"metadata": {"resourceVersion": "1", "generation": 4}, "status": {}},
                                              {"status": "failed", "observedGeneration": value})
            self.assertEqual(patch["status"].get("observedGeneration"), kept)


if __name__ == "__main__":
    unittest.main()

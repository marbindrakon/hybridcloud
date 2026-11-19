"""The API side of a cluster's installation: the CRD's install-time rule (its text pinned here; tests/cel-ipam/
cel_cases_install.py runs it on an API server), the install-time fields against the role's reads and the CRD's
provider blocks, status.install in the schema, the operator's rules (no periodic event for a stopped installation at
the generation it stopped at; the in-flight check reads the observation time), the provision template's time limit.
"""
import os
import re
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)

import shc_install as I  # noqa: E402

CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-platformopenshift.yaml")
ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
MESSAGE = ("The cluster is being installed or is installed. Sizes, flavors, release and image can be changed before "
           "the installation starts or after it has stopped; on AWS and Google Cloud the worker count can also be "
           "changed once the cluster is installed.")
MESSAGE_INSTALLING = ("The cluster is being installed. The worker count can be changed once the installation has "
                      "finished or after it has stopped.")
PROVIDERS = ("openstack", "aws", "gcp")
UI_INSTALL = os.path.join(ROOT, "ui", "packages", "shared", "src", "status", "install.ts")


def ui_list(name):
    """A string list exported by the pages' install.ts (e.g. INSTALL_TIME_FIELDS), as a set of dot paths."""
    with open(UI_INSTALL) as fh:
        text = fh.read()
    m = re.search(r"export const %s: readonly string\[\] = \[(.*?)\];" % name, text, re.S)
    assert m, name
    return set(re.findall(r"'([A-Za-z.]+)'", m.group(1)))


def unchanged_clause(p, f, root=True):
    s, o = ("self.spec", "oldSelf.spec") if root else ("self", "oldSelf")
    return ("(has({s}.{p}) && has({s}.{p}.{f})) == (has({o}.{p}) && has({o}.{p}.{f})) "
            "&& (!(has({s}.{p}) && has({s}.{p}.{f})) || {s}.{p}.{f} == {o}.{p}.{f})").format(s=s, o=o, p=p, f=f)


def compared_fields(rule_text, root=True):
    s = "self.spec" if root else "self"
    return set(re.findall(r"\b%s\.(openstack|aws|gcp)\.([A-Za-z]+) ==" % re.escape(s), rule_text))


def schema():
    with open(CRD) as fh:
        return yaml.safe_load(fh)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]


def role_reads():
    reads = {"openstack": set(), "aws": set(), "gcp": set()}
    for d in ("tasks", "templates"):
        for f in os.listdir(os.path.join(ROLE, d)):
            with open(os.path.join(ROLE, d, f)) as fh:
                text = fh.read()
            for p, field in re.findall(r"platform_spec\.(openstack|aws|gcp)\.([A-Za-z]+)", text):
                reads[p].add(field)
            for field in re.findall(r"_os\.([A-Za-z]+)", text):
                reads["openstack"].add(field)
    return reads


class Rule(unittest.TestCase):
    def rules(self):
        rules = schema().get("x-kubernetes-validations") or []
        found = [r for r in rules if "oldSelf.spec" in r["rule"]]
        self.assertEqual(len(found), 2)
        return found

    def test_messages_and_field_path(self):
        install, installing = self.rules()
        self.assertEqual(" ".join(install["message"].split()), MESSAGE)
        self.assertEqual(" ".join(installing["message"].split()), MESSAGE_INSTALLING)
        for r in (install, installing):
            # the refusal names the spec, not "<nil>"
            self.assertEqual(r["fieldPath"], ".spec")
            self.assertEqual(r["reason"], "FieldValueForbidden")

    OPEN = ("!(has(self.status) && has(self.status.clusterInstalled) && self.status.clusterInstalled == true) && "
            "(!has(self.status) || (has(self.status.install) && has(self.status.install.stage) && "
            "self.status.install.stage == 'Stopped') || ((!has(self.status.install) || !has(self.status.install.stage) "
            "|| self.status.install.stage == 'Preparing') && !(has(self.status.installStarted) && "
            "self.status.installStarted == true)))")

    def test_the_install_time_rule_compares_each_install_time_field(self):
        text = " ".join(self.rules()[0]["rule"].split())
        self.assertTrue(text.startswith("(" + self.OPEN + ") || ("), text[:300])
        want = {(p, f) for p in PROVIDERS for f in I.INSTALL_TIME_FIELDS[p]}
        self.assertEqual(compared_fields(text), want)
        for p, f in want:
            self.assertIn(unchanged_clause(p, f), text)

    def test_the_installing_rule_compares_the_fields_applied_to_an_installed_cluster(self):
        text = " ".join(self.rules()[1]["rule"].split())
        self.assertTrue(text.startswith("(" + self.OPEN + ") || ((has(self.status) && has(self.status.clusterInstalled)"
                                        " && self.status.clusterInstalled == true) || (has(self.status) && "
                                        "has(self.status.install) && has(self.status.install.stage) && "
                                        "self.status.install.stage == 'Installed')) || ("), text[:600])
        want = {(p, f) for p in PROVIDERS for f in I.INSTALLED_CHANGEABLE_FIELDS[p]}
        self.assertEqual(want, {("aws", "workerCount"), ("gcp", "workerCount")})
        self.assertEqual(compared_fields(text), want)

    def test_the_rule_runs_on_an_api_server(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tests", "cel-ipam", "cel_cases_install.py")))


class Fields(unittest.TestCase):
    """Each field of each provider block is of exactly one kind: install-time (refused while installing or installed),
    applied to an installed cluster (refused only while installing), or fixed from creation (its own rule)."""

    def spec_rules(self):
        return [" ".join(r["rule"].split()) for r in schema()["properties"]["spec"].get("x-kubernetes-validations")]

    def test_every_field_is_of_one_kind(self):
        props = schema()["properties"]["spec"]["properties"]
        for p in PROVIDERS:
            kinds = [set(I.INSTALL_TIME_FIELDS[p]), set(I.INSTALLED_CHANGEABLE_FIELDS[p]), set(I.IMMUTABLE_FIELDS[p])]
            self.assertEqual(set(props[p]["properties"]), set().union(*kinds), p)
            self.assertEqual(sum(len(k) for k in kinds), len(set().union(*kinds)), "%s: a field of two kinds" % p)

    def test_fixed_fields_have_their_own_rules(self):
        rules = self.spec_rules()
        props = schema()["properties"]["spec"]["properties"]
        for p in PROVIDERS:
            for f in I.IMMUTABLE_FIELDS[p]:
                if p == "openstack" and f in ("network", "externalNetwork"):
                    field_rules = [r["rule"] for r in props[p]["properties"][f].get("x-kubernetes-validations", [])]
                    self.assertIn("self == oldSelf", field_rules)
                    self.assertTrue(any("has(self.openstack.%s)" % f in r and "oldSelf" in r for r in rules), f)
                    continue
                self.assertIn(unchanged_clause(p, f, root=False), rules, "%s.%s" % (p, f))
        self.assertIn("has(self.type) == has(oldSelf.type) && (!has(self.type) || self.type == oldSelf.type)", rules)
        self.assertEqual(I.IMMUTABLE_TOP_FIELDS, ("type",))

    def test_the_worker_count_is_applied_to_an_installed_aws_and_google_cloud_cluster(self):
        """The release holds the worker pool with replicas from the worker count; its type (the worker flavor) is
        refused by the hub's admission once the pool exists, so it stays install-time."""
        with open(os.path.join(ROOT, "charts", "charts", "mce-cluster-build", "templates", "default-machinepool.yaml")) as fh:
            pool = fh.read()
        self.assertIn("replicas: {{ .Values.cluster.workerCount }}", pool)
        self.assertIn("type: {{ .Values.provider.aws.workerFlavor }}", pool)
        self.assertIn("type: {{ .Values.provider.gcp.workerFlavor }}", pool)
        self.assertNotIn("override-machinepool-platform", pool)
        for p in ("aws", "gcp"):
            self.assertIn("workerFlavor", I.INSTALL_TIME_FIELDS[p])
            self.assertEqual(I.INSTALLED_CHANGEABLE_FIELDS[p], ("workerCount",))
        self.assertEqual(I.INSTALLED_CHANGEABLE_FIELDS["openstack"], ())

    def test_every_field_the_role_reads_is_covered(self):
        props = schema()["properties"]["spec"]["properties"]
        for p, fields in role_reads().items():
            in_schema = fields & set(props[p]["properties"])
            self.assertTrue(in_schema, p)
            known = set(I.INSTALL_TIME_FIELDS[p]) | set(I.INSTALLED_CHANGEABLE_FIELDS[p]) | set(I.IMMUTABLE_FIELDS[p])
            self.assertLessEqual(in_schema, known, p)

    def test_fields_outside_the_blocks_stay_changeable(self):
        props = schema()["properties"]["spec"]["properties"]
        for f in ("toolRbac", "clusterViewerRbac", "deletionProtection"):
            self.assertIn(f, props)
            self.assertNotIn(f, I.IMMUTABLE_TOP_FIELDS)
            self.assertNotIn(f, I.INSTALLER_SPEC_KEYS)

    def test_the_pages_lock_the_same_fields(self):
        """One list: the pages' install-time and installed-changeable lists are the backend's."""
        self.assertEqual(ui_list("INSTALL_TIME_FIELDS"),
                         {"%s.%s" % (p, f) for p in PROVIDERS for f in I.INSTALL_TIME_FIELDS[p]})
        self.assertEqual(ui_list("INSTALLED_CHANGEABLE_FIELDS"),
                         {"%s.%s" % (p, f) for p in PROVIDERS for f in I.INSTALLED_CHANGEABLE_FIELDS[p]})


class StatusSchema(unittest.TestCase):
    def test_status_install(self):
        inst = schema()["properties"]["status"]["properties"]["install"]
        self.assertEqual(set(inst["properties"]),
                         {"stage", "attempt", "attemptsLimit", "run", "startedAt", "endedAt", "observedAt",
                          "lastFailure", "stoppedGeneration", "stoppedRequest", "stoppedSpec",
                          "stoppedSpecVersion", "stoppedAt", "namespaceWaitSince"})
        self.assertEqual(inst["properties"]["stage"]["enum"], list(I.STAGES))
        self.assertEqual(set(inst["properties"]["lastFailure"]["properties"]), {"attempt", "reason", "message", "time"})

    def test_descriptions(self):
        st = schema()["properties"]["status"]["properties"]
        self.assertIn("Stalled is True when the platform has", st["conditions"]["description"])
        self.assertIn("See status.install", st["provisionStatus"]["description"])
        self.assertIn("a new Retry now request (status.lastHandledReconcileRequest) or a change of spec.type or the "
                      "provider's block starts the installation again; a change of anything else starts nothing.",
                      " ".join(st["install"]["description"].split()))


class Operator(unittest.TestCase):
    def test_no_periodic_event_for_a_stopped_installation(self):
        with open(os.path.join(ROOT, "operator", "namespace", "roles", "platformopenshift", "tasks", "main.yml")) as fh:
            tasks = yaml.safe_load(fh)[0]["block"]
        names = [t["name"] for t in tasks]
        decide = tasks[names.index("Decide whether a stopped installation waits for a request")]
        skip = tasks[names.index("Skip a stopped installation nobody asked to start again")]
        self.assertLess(names.index("Read the Retry-now request"), names.index(decide["name"]))
        self.assertLess(names.index(skip["name"]), names.index("Publish operator event to AMQ Streams"))
        text = decide["ansible.builtin.set_fact"]["platformopenshift_stalled_waits"]
        self.assertIn("not (reconcile_now | bool)", text)
        self.assertIn("(platformopenshift_generation | int) <= stopped_gen", text)
        for word in ("stoppedGeneration", "stoppedSpec", "stoppedRequest", "completedEventId",
                     "(inst.stage | default('')) == 'Stopped'"):
            self.assertIn(word, text)

    def test_in_flight_reads_the_observation_time(self):
        with open(os.path.join(ROOT, "operator", "roles", "_common", "tasks", "skip_if_event_in_flight.yml")) as fh:
            text = fh.read()
        self.assertIn("in_flight_observed_kinds | default(['PlatformOpenshift'])", text)
        self.assertIn("in_flight_observed_stale_minutes | default(15)", text)
        self.assertNotIn("observed >= (d.jobStartedAt | string)", text, "the later of the two, not the check alone")
        self.assertIn("in ['Preparing', 'CleaningUp', 'Installing',", text)
        self.assertIn("observed > started) else started", text)


class Template(unittest.TestCase):
    def test_provision_template_time_limit_covers_the_watch(self):
        with open(os.path.join(ROOT, "aap-config", "controller", "job_templates.yml")) as fh:
            data = yaml.safe_load(fh)
        templates = data if isinstance(data, list) else next(v for v in data.values() if isinstance(v, list))
        tpl = next(t for t in templates if t.get("name") == "platformopenshift-provision")
        with open(os.path.join(ROLE, "defaults", "main.yml")) as fh:
            defaults = yaml.safe_load(fh)["platformopenshift_install_defaults"]
        self.assertEqual(tpl["timeout"], 150 * 60)
        # the watch window, the removal of a stopped build (up to about an hour of waits) and the work around it fit
        self.assertLess(defaults["watchWindowMinutes"] * 60, tpl["timeout"])
        self.assertEqual(set(defaults), {"observeIntervalSeconds", "watchWindowMinutes", "deadlineMinutes",
                                         "checkWriteMinutes", "pendingReleaseMinutes", "removalLimitMinutes",
                                         "namespaceWaitMinutes", "retryMarginMinutes"})
        self.assertEqual(defaults["retryMarginMinutes"], 1)
        self.assertEqual(defaults["namespaceWaitMinutes"], 30)
        self.assertEqual(defaults["removalLimitMinutes"], 60)
        self.assertEqual(defaults["pendingReleaseMinutes"], 10)
        self.assertEqual((defaults["watchWindowMinutes"], defaults["deadlineMinutes"]), (120, 360))


if __name__ == "__main__":
    unittest.main()


sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402


def _ago(minutes):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class OperatorRun(unittest.TestCase):
    """The operator's two rules, run (Ansible with a fake API read)."""

    IN_FLIGHT = os.path.join(ROOT, "operator", "roles", "_common", "tasks", "skip_if_event_in_flight.yml")

    def in_flight(self, status, kind="PlatformOpenshift"):
        obj = {"metadata": {"name": "demo", "namespace": "entity-example"}, "status": status}
        rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                            {"in_flight_kind": kind,
                             "ansible_operator_meta": {"name": "demo", "namespace": "entity-example"}},
                            k8s={"%s/entity-example/demo" % kind: obj},
                            tasks=[{"ansible.builtin.include_tasks": self.IN_FLIGHT}], result="'published'")
        self.assertEqual(rc, 0, out)
        return not any(t.get("result") == "published" for t in tr)

    def delivery(self, started):
        return {"eventId": "e1", "publishedAt": _ago(started + 1), "acknowledgedEventId": "e1",
                "jobStartedAt": _ago(started)}

    def test_fresh_observation_is_in_flight(self):
        self.assertTrue(self.in_flight({"delivery": self.delivery(60),
                                        "install": {"stage": "Installing", "observedAt": _ago(3)}}))

    def test_old_observation_is_replaced(self):
        # the job started 30 minutes ago (well inside the 150 minutes) but stopped observing 20 minutes ago
        self.assertFalse(self.in_flight({"delivery": self.delivery(30),
                                         "install": {"stage": "Installing", "observedAt": _ago(20)}}))

    def test_no_observation_by_this_job_uses_the_job_time(self):
        self.assertTrue(self.in_flight({"delivery": self.delivery(30)}))
        # no running stage recorded: the job time applies
        self.assertTrue(self.in_flight({"delivery": self.delivery(30), "install": {"observedAt": _ago(40)}}))
        self.assertFalse(self.in_flight({"delivery": self.delivery(160)}))

    def test_a_dead_job_is_replaced_whenever_it_died(self):
        """In every stage a job works in, the event is in flight only while the later of the check time and the job's
        start is younger than 15 minutes: a job that died before its first check (the check time an earlier job's) is
        replaced like one that died later; a living job writes at least every five minutes."""
        for stage in ("Preparing", "CleaningUp", "Installing", "Finishing"):
            # died 20 minutes after it started, before writing a check time (the stored one is the previous job's)
            self.assertFalse(self.in_flight({"delivery": self.delivery(20),
                                             "install": {"stage": stage, "observedAt": _ago(25)}}), stage)
            # living: its own check 3 minutes ago, started an hour ago
            self.assertTrue(self.in_flight({"delivery": self.delivery(60),
                                            "install": {"stage": stage, "observedAt": _ago(3)}}), stage)
            # just started (no check of its own yet)
            self.assertTrue(self.in_flight({"delivery": self.delivery(2),
                                            "install": {"stage": stage, "observedAt": _ago(40)}}), stage)
            # its last check 20 minutes ago: dead
            self.assertFalse(self.in_flight({"delivery": self.delivery(30),
                                             "install": {"stage": stage, "observedAt": _ago(20)}}), stage)
        # a stage no job works in keeps the job time
        for stage in ("Stopped", "Installed"):
            self.assertTrue(self.in_flight({"delivery": self.delivery(30),
                                            "install": {"stage": stage, "observedAt": _ago(25)}}), stage)

    def test_other_kinds_unchanged(self):
        self.assertTrue(self.in_flight({"delivery": self.delivery(10), "install": {"observedAt": _ago(9)}},
                                       kind="CloudOSO"))

    SPEC = {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3,
                                               "controlPlaneFlavor": "example.xlarge", "trustBundle": "caf\u00e9 \"x\"\n"},
            "toolRbac": {"clusterAdminRbac": ["admins"]}}

    def decide_task(self, name):
        with open(os.path.join(ROOT, "operator", "namespace", "roles", "platformopenshift", "tasks", "main.yml")) as fh:
            tasks = yaml.safe_load(fh)[0]["block"]
        return next(t for t in tasks if t["name"] == name)

    def stalled_waits(self, status, generation=1, reconcile_now=False, spec=None):
        decide = self.decide_task("Decide whether a stopped installation waits for a request")
        rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                            {"_hybridsovereign_redhat_platformopenshift": {"metadata": {"generation": generation},
                                                                           "spec": spec or self.SPEC,
                                                                           "status": status},
                             "reconcile_now": reconcile_now, "platformopenshift_generation": generation},
                            tasks=[decide], result="platformopenshift_stalled_waits | bool")
        self.assertEqual(rc, 0, out)
        return tr[-1]["result"]

    def stopped(self, **install):
        inst = {"stage": "Stopped", "stoppedGeneration": 2, "stoppedSpec": I.spec_digest(self.SPEC),
                "stoppedSpecVersion": I.DIGEST_VERSION,
                "stoppedRequest": "req-1", "endedAt": _ago(30)}
        inst.update(install)
        return {"conditions": [{"type": "Stalled", "status": "True", "reason": "QuotaExceeded"}],
                "lastHandledReconcileRequest": "req-1", "install": inst,
                "delivery": {"eventId": "e1", "completedEventId": "e1", "publishedAt": _ago(40)}}

    def test_stalled_rule(self):
        self.assertTrue(self.stalled_waits(self.stopped(), generation=2))
        self.assertFalse(self.stalled_waits(self.stopped(), generation=3),
                         "any spec change publishes once (the job applies what needs no installation)")
        self.assertFalse(self.stalled_waits(self.stopped(), generation=2, reconcile_now=True),
                         "a Retry request publishes")
        running = {"conditions": [{"type": "Stalled", "status": "False", "reason": "AsExpected"}]}
        self.assertFalse(self.stalled_waits(running, generation=2))

    def test_the_operator_digest_is_the_modules(self):
        """The same spec gives the same digest in the operator (Jinja) and in the module (Python): a stopped cluster
        at the stop's spec waits; a change of the installer's part publishes even at the recorded generation."""
        self.assertTrue(self.stalled_waits(self.stopped(), generation=2))
        smaller = {"type": "openstack", "openstack": dict(self.SPEC["openstack"], workerCount=0)}
        self.assertFalse(self.stalled_waits(self.stopped(), generation=2, spec=smaller))

    def test_the_operators_field_list_is_the_modules(self):
        decide = self.decide_task("Decide whether a stopped installation waits for a request")
        self.assertEqual(decide["vars"]["platformopenshift_install_fields"], I.DIGEST_FIELDS)
        self.assertEqual(decide["vars"]["platformopenshift_install_fields_version"], I.DIGEST_VERSION)

    def test_a_new_schema_default_or_list_entry_starts_nothing(self):
        """After a product update: a field the schema now fills in (not listed) gives the same digest; a digest
        recorded under another list version is not compared (only a Retry publishes)."""
        more = {"type": "openstack", "openstack": dict(self.SPEC["openstack"], someNewField=7)}
        self.assertTrue(self.stalled_waits(self.stopped(), generation=2, spec=more))
        smaller = {"type": "openstack", "openstack": dict(self.SPEC["openstack"], workerCount=0)}
        older = self.stopped(stoppedSpecVersion="0" * 16)
        self.assertTrue(self.stalled_waits(older, generation=2, spec=smaller))
        older["lastHandledReconcileRequest"] = "req-9"
        self.assertFalse(self.stalled_waits(older, generation=2, spec=smaller), "a Retry still publishes")

    def test_an_open_request_or_a_lost_event_publishes(self):
        st = self.stopped()
        st["lastHandledReconcileRequest"] = "req-2"
        self.assertFalse(self.stalled_waits(st, generation=2), "a Retry handled by the operator that no job acted on")
        st = self.stopped()
        st["delivery"] = {"eventId": "e2", "completedEventId": "e1", "publishedAt": _ago(5),
                          "reason": "PlatformOpenshiftReconcileRequested"}
        self.assertFalse(self.stalled_waits(st, generation=2), "an event after the stop that no job completed")
        st["delivery"]["completedEventId"] = "e2"
        self.assertTrue(self.stalled_waits(st, generation=2))

    def test_stalled_outside_a_stop_never_silences_the_checks(self):
        st = self.stopped(stage="Installing")
        self.assertFalse(self.stalled_waits(st, generation=2))

    def test_a_running_installation_keeps_its_state_while_queued(self):
        keep = self.decide_task("Keep a running installation's state while a run is queued")
        for stage, want in (("Installing", True), ("CleaningUp", True), ("Finishing", True), ("Stopped", False),
                            ("Installed", False), ("", False)):
            rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                                {"_hybridsovereign_redhat_platformopenshift": {"status": {"install": {"stage": stage}}}},
                                tasks=[keep], result="reconcile_keeps_ready | default(false) | bool")
            self.assertEqual(rc, 0, out)
            self.assertEqual(tr[-1]["result"], want, stage)

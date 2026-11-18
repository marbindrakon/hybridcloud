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
           "the installation starts or after it has stopped.")
# provider fields that are not install-time because they are immutable anyway (their own rules)
IMMUTABLE = {"openstack": {"network", "externalNetwork"}, "aws": {"network"}, "gcp": {"network"}}


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
    def rule(self):
        rules = schema().get("x-kubernetes-validations") or []
        found = [r for r in rules if "oldSelf.spec" in r["rule"]]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_message(self):
        self.assertEqual(" ".join(self.rule()["message"].split()), MESSAGE)

    def test_the_rule_text(self):
        text = " ".join(self.rule()["rule"].split())
        for p in ("openstack", "aws", "gcp"):
            self.assertIn("has(self.spec.%s) == has(oldSelf.spec.%s)" % (p, p), text)
            self.assertIn("(!has(self.spec.%s) || self.spec.%s == oldSelf.spec.%s)" % (p, p, p), text)
        self.assertIn("self.spec.type == oldSelf.spec.type", text)
        # accepted: not installed, and stopped or not started (no status.install, or Preparing, and installStarted
        # not true)
        self.assertIn("self.status.clusterInstalled == true ? false", text)
        self.assertIn("self.status.install.stage == 'Stopped'", text)
        self.assertIn("self.status.install.stage == 'Preparing'", text)
        self.assertIn("!(has(self.status.installStarted) && self.status.installStarted == true)", text)

    def test_the_rule_runs_on_an_api_server(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tests", "cel-ipam", "cel_cases_install.py")))


class Fields(unittest.TestCase):
    def test_install_time_fields_are_the_provider_blocks(self):
        """The rule compares whole provider blocks: every field of a block is install-time or already immutable."""
        props = schema()["properties"]["spec"]["properties"]
        for p in ("openstack", "aws", "gcp"):
            block = set(props[p]["properties"])
            self.assertEqual(block, set(I.INSTALL_TIME_FIELDS[p]) | IMMUTABLE[p], p)
        self.assertEqual(I.INSTALL_TIME_TOP_FIELDS, ("type",))

    def test_every_field_the_role_reads_is_covered(self):
        props = schema()["properties"]["spec"]["properties"]
        for p, fields in role_reads().items():
            in_schema = fields & set(props[p]["properties"])
            self.assertTrue(in_schema, p)
            self.assertLessEqual(in_schema, set(I.INSTALL_TIME_FIELDS[p]) | IMMUTABLE[p], p)

    def test_fields_outside_the_blocks_stay_changeable(self):
        props = schema()["properties"]["spec"]["properties"]
        for f in ("toolRbac", "clusterViewerRbac", "deletionProtection"):
            self.assertIn(f, props)
            self.assertNotIn(f, I.INSTALL_TIME_TOP_FIELDS)


class StatusSchema(unittest.TestCase):
    def test_status_install(self):
        inst = schema()["properties"]["status"]["properties"]["install"]
        self.assertEqual(set(inst["properties"]),
                         {"stage", "attempt", "attemptsLimit", "run", "startedAt", "endedAt", "observedAt",
                          "lastFailure", "stoppedGeneration", "stoppedRequest"})
        self.assertEqual(inst["properties"]["stage"]["enum"], list(I.STAGES))
        self.assertEqual(set(inst["properties"]["lastFailure"]["properties"]), {"attempt", "reason", "message", "time"})

    def test_descriptions(self):
        st = schema()["properties"]["status"]["properties"]
        self.assertIn("Stalled is True when the platform has", st["conditions"]["description"])
        self.assertIn("See status.install", st["provisionStatus"]["description"])
        self.assertIn("a new request starts the installation again",
                      " ".join(st["lastHandledReconcileRequest"]["description"].split()))


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
        self.assertIn("stoppedGeneration", text)

    def test_in_flight_reads_the_observation_time(self):
        with open(os.path.join(ROOT, "operator", "roles", "_common", "tasks", "skip_if_event_in_flight.yml")) as fh:
            text = fh.read()
        self.assertIn("in_flight_observed_kinds | default(['PlatformOpenshift'])", text)
        self.assertIn("in_flight_observed_stale_minutes | default(15)", text)
        self.assertIn("observed >= (d.jobStartedAt | string)", text)


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
                                         "checkWriteMinutes"})
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
        self.assertTrue(self.in_flight({"delivery": self.delivery(60), "install": {"observedAt": _ago(3)}}))

    def test_old_observation_is_replaced(self):
        # the job started 30 minutes ago (well inside the 150 minutes) but stopped observing 20 minutes ago
        self.assertFalse(self.in_flight({"delivery": self.delivery(30), "install": {"observedAt": _ago(20)}}))

    def test_no_observation_by_this_job_uses_the_job_time(self):
        self.assertTrue(self.in_flight({"delivery": self.delivery(30)}))
        self.assertTrue(self.in_flight({"delivery": self.delivery(30), "install": {"observedAt": _ago(40)}}))
        self.assertFalse(self.in_flight({"delivery": self.delivery(160)}))

    def test_other_kinds_unchanged(self):
        self.assertTrue(self.in_flight({"delivery": self.delivery(10), "install": {"observedAt": _ago(9)}},
                                       kind="CloudOSO"))

    def stalled_waits(self, status, generation=1, reconcile_now=False):
        with open(os.path.join(ROOT, "operator", "namespace", "roles", "platformopenshift", "tasks", "main.yml")) as fh:
            tasks = yaml.safe_load(fh)[0]["block"]
        decide = next(t for t in tasks if t["name"] == "Decide whether a stopped installation waits for a request")
        rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                            {"_hybridsovereign_redhat_platformopenshift": {"metadata": {"generation": generation},
                                                                           "status": status},
                             "reconcile_now": reconcile_now, "platformopenshift_generation": generation},
                            tasks=[decide], result="platformopenshift_stalled_waits | bool")
        self.assertEqual(rc, 0, out)
        return tr[-1]["result"]

    def test_stalled_rule(self):
        stalled = {"conditions": [{"type": "Stalled", "status": "True", "reason": "QuotaExceeded"}],
                   "install": {"stage": "Stopped", "stoppedGeneration": 2}}
        self.assertTrue(self.stalled_waits(stalled, generation=2))
        self.assertFalse(self.stalled_waits(stalled, generation=3), "a spec change publishes")
        self.assertFalse(self.stalled_waits(stalled, generation=2, reconcile_now=True), "a Retry request publishes")
        running = {"conditions": [{"type": "Stalled", "status": "False", "reason": "AsExpected"}]}
        self.assertFalse(self.stalled_waits(running, generation=2))

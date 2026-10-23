"""Redrive verdicts are per event (review m7).

status.delivery.state (NoAcknowledgement, Stalled, NoCompletion) is written with the event it is about
(status.delivery.stateEventId). A newer event, an acknowledgement after NoAcknowledgement or a finished job clears
it: the redrive pass clears a stale verdict (also one written before stateEventId existed) and decides again, the
tenancy pass counts a verdict only for the current event, and the operators clear it when they record a new event.
"""
import datetime as dt
import os
import sys
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files")
sys.path.insert(0, FILES)
sys.path.insert(0, os.path.dirname(__file__))

try:
    import kubernetes  # noqa: F401
except ImportError:  # stub the client so the pure functions import
    k8s = types.ModuleType("kubernetes")
    k8s.client = types.SimpleNamespace()
    k8s.config = types.SimpleNamespace(ConfigException=Exception)
    rest = types.ModuleType("kubernetes.client.rest")
    rest.ApiException = type("ApiException", (Exception,), {})
    sys.modules.update({"kubernetes": k8s, "kubernetes.client": k8s.client, "kubernetes.client.rest": rest})

import shc_tenancy as T  # noqa: E402
from play_helper import by_name, load  # noqa: E402

R = T.eda_redrive
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)
TCFG = dict(T.DEFAULTS["tenancy"])
RCFG = {"redriveKinds": ["Team"], "ackTimeoutMinutes": 10, "jobTimeoutMinutes": 30}


class Api:
    def __init__(self):
        self.status = []
        self.annotations = []

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.annotations.append(body["metadata"]["annotations"])

    def patch_namespaced_custom_object_status(self, group, version, ns, plural, name, body):
        self.status.append(body["status"]["delivery"])


def team(delivery, ready=False, running=False):
    conds = [{"type": "Ready", "status": "True" if ready else "False", "reason": "Ready" if ready else "InProgress"}]
    if running:
        conds.append({"type": "Reconciling", "status": "True", "reason": "JobRunning"})
    return {"metadata": {"namespace": "entity-a", "name": "t1", "generation": 1,
                         "creationTimestamp": "2026-10-21T08:00:00Z"},
            "status": {"ready": ready, "observedGeneration": 1, "conditions": conds, "delivery": delivery}}


def run(obj, cfg=RCFG):
    api = Api()
    counts, problems = R.run_redrive(api, [{"kind": "Team", "plural": "teams", "version": "v1alpha1", "items": [obj]}],
                                     cfg, NOW, False)
    return api, counts, problems


CLEAR = {"state": None, "stateSince": None, "stateEventId": None}


class Redrive(unittest.TestCase):
    def test_new_event_clears_an_earlier_events_no_completion(self):
        # e1's job never wrote its end (NoCompletion); Retry now published e2 two minutes ago
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:58:00Z", "acknowledgedEventId": "e1",
             "jobStartedAt": "2026-10-21T10:00:00Z", "completedEventId": "e0",
             "state": "NoCompletion", "stateSince": "2026-10-21T10:15:00Z", "stateEventId": "e1"}
        obj = team(d)
        api, counts, problems = run(obj)
        self.assertEqual(api.status, [CLEAR])
        self.assertEqual((counts["noCompletion"], counts["stalled"], problems), (0, 0, []))
        # the same run's tenancy pass sees the cleared state
        cond, stalled = T.delivery_condition([("Team", obj)], TCFG, NOW, RCFG)
        self.assertEqual((cond["status"], stalled), ("False", []))

    def test_new_event_while_its_job_runs_clears_stalled(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:50:00Z", "acknowledgedEventId": "e2",
             "jobStartedAt": "2026-10-21T11:51:00Z", "state": "Stalled", "stateEventId": "e1"}
        api, counts, _ = run(team(d, running=True))
        self.assertEqual(api.status, [CLEAR])
        self.assertEqual(counts["stalled"], 0)

    def test_acknowledgement_clears_no_acknowledgement_of_the_same_event(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "acknowledgedEventId": "e2",
             "jobStartedAt": "2026-10-21T11:55:00Z", "state": "NoAcknowledgement", "stateEventId": "e2"}
        obj = team(d, running=True)
        api, counts, _ = run(obj)
        self.assertEqual(api.status, [CLEAR])
        self.assertEqual(counts["noAcknowledgement"], 0)

    def test_verdict_names_its_event(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "acknowledgedEventId": "e2",
             "jobId": "7", "jobStartedAt": "2026-10-21T11:00:00Z"}
        api, counts, problems = run(team(d, running=True))
        self.assertEqual(counts["stalled"], 1)
        self.assertEqual(api.status, [{"state": "Stalled", "stateSince": "2026-10-21T12:00:00Z", "stateEventId": "e2"}])
        # a second pass writes nothing new
        d.update(api.status[0])
        api2, _, _ = run(team(d, running=True))
        self.assertEqual(api2.status, [])

    def test_legacy_verdict_is_decided_again(self):
        # written before stateEventId existed: cleared, then (still stalled) written again with its event
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "acknowledgedEventId": "e2",
             "jobStartedAt": "2026-10-21T11:00:00Z", "state": "Stalled", "stateSince": "2026-10-21T11:40:00Z"}
        obj = team(d, running=True)
        api, counts, _ = run(obj)
        self.assertEqual(api.status, [CLEAR, {"state": "Stalled", "stateSince": "2026-10-21T12:00:00Z",
                                              "stateEventId": "e2"}])
        self.assertEqual(counts["stalled"], 1)
        self.assertEqual(obj["status"]["delivery"]["stateEventId"], "e2")

    def test_finished_event_clears_its_verdict(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "acknowledgedEventId": "e2",
             "completedEventId": "e2", "state": "NoCompletion", "stateEventId": "e2"}
        api, _, _ = run(team(d))
        self.assertEqual(api.status, [CLEAR])


class Tenancy(unittest.TestCase):
    def raw(self, delivery):
        return ("CloudVirt", {"metadata": {"name": "x", "generation": 1, "creationTimestamp": "2026-10-21T10:00:00Z"},
                              "status": {"ready": False, "delivery": delivery}})

    def test_verdict_about_an_earlier_event_does_not_count(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:58:00Z", "state": "NoCompletion", "stateEventId": "e1"}
        cond, stalled = T.delivery_condition([self.raw(d)], TCFG, NOW)
        self.assertEqual((cond["status"], stalled), ("False", []))

    def test_verdict_about_the_current_event_counts(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:58:00Z", "state": "NoCompletion", "stateEventId": "e2"}
        cond, _ = T.delivery_condition([self.raw(d)], TCFG, NOW)
        self.assertEqual(cond["status"], "True")

    def test_no_acknowledgement_after_the_acknowledgement_does_not_count(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "acknowledgedEventId": "e2",
             "state": "NoAcknowledgement", "stateEventId": "e2"}
        cond, _ = T.delivery_condition([self.raw(d)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")


class Wiring(unittest.TestCase):
    def test_operator_clears_the_verdict_with_a_new_event(self):
        pub = load(os.path.join(ROOT, "operator", "roles", "_common", "tasks", "publish_operator_event.yml"))
        rec = by_name(pub, "Record the event id before publishing")["operator_sdk.util.k8s_status"]["status"]["delivery"]
        self.assertEqual({k: rec[k] for k in ("state", "stateSince", "stateEventId")}, CLEAR)

    def test_crds_declare_the_delivery_fields(self):
        import glob
        for path in glob.glob(os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-*.yaml")):
            crd = load(path)
            st = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]
            if "conditions" not in st.get("properties", {}) or not st.get("x-kubernetes-preserve-unknown-fields"):
                continue  # a platform-written kind without delivery (TenantProfile)
            props = st["properties"]["delivery"]["properties"]
            for key in ("eventId", "acknowledgedEventId", "completedEventId", "state", "stateSince", "stateEventId"):
                self.assertEqual(props[key]["type"], "string", (path, key))
            self.assertTrue(st["properties"]["delivery"]["x-kubernetes-preserve-unknown-fields"], path)


if __name__ == "__main__":
    unittest.main()

"""Unit tests of the shc-tenancy CronJob logic (no cluster; kubernetes client stubbed when absent)."""
import datetime as dt
import os
import sys
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files")
sys.path.insert(0, FILES)

try:
    import kubernetes  # noqa: F401
    import yaml  # noqa: F401
except ImportError:  # stub the client so the pure functions import
    k8s = types.ModuleType("kubernetes")
    k8s.client = types.SimpleNamespace()
    k8s.config = types.SimpleNamespace(ConfigException=Exception)
    rest = types.ModuleType("kubernetes.client.rest")
    rest.ApiException = type("ApiException", (Exception,), {})
    sys.modules.update({"kubernetes": k8s, "kubernetes.client": k8s.client, "kubernetes.client.rest": rest})
    if "yaml" not in sys.modules:
        try:
            import yaml  # noqa: F401
        except ImportError:
            sys.modules["yaml"] = types.SimpleNamespace(safe_load=lambda f: {})

import shc_tenancy as T  # noqa: E402

NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.timezone.utc)
TCFG = dict(T.DEFAULTS["tenancy"])
NS = types.SimpleNamespace


def deploy(available=1, since_min=None):
    conds = []
    if since_min is not None:
        conds = [NS(type="Available", status="False", last_transition_time=NOW - dt.timedelta(minutes=since_min))]
    return NS(status=NS(available_replicas=available, conditions=conds))


def pod(restarts=0, reason=None, finished_min=None):
    term = None
    if reason:
        term = NS(reason=reason, finished_at=NOW - dt.timedelta(minutes=finished_min))
    return NS(status=NS(container_statuses=[NS(restart_count=restarts, last_state=NS(terminated=term))]))


class Controller(unittest.TestCase):
    def test_available(self):
        self.assertEqual(T.controller_condition(deploy(), [pod()], TCFG, NOW)["status"], "True")

    def test_missing(self):
        self.assertEqual(T.controller_condition(None, [], TCFG, NOW)["reason"], "ControllerUnavailable")

    def test_unavailable_grace(self):
        self.assertEqual(T.controller_condition(deploy(0, 1), [], TCFG, NOW)["status"], "True")
        self.assertEqual(T.controller_condition(deploy(0, 5), [], TCFG, NOW)["reason"], "ControllerUnavailable")

    def test_oom_and_restarts(self):
        self.assertEqual(T.controller_condition(deploy(), [pod(1, "OOMKilled", 3)], TCFG, NOW)["reason"],
                         "ControllerOutOfMemory")
        self.assertEqual(T.controller_condition(deploy(), [pod(4, "Error", 3)], TCFG, NOW)["reason"],
                         "ControllerRestarting")
        self.assertEqual(T.controller_condition(deploy(), [pod(4, "Error", 60)], TCFG, NOW)["status"], "True")


def cr(name, ready=True, generation=1, observed=1, delivery=None, created="2026-10-21T10:00:00Z"):
    st = {"observedGeneration": observed, "delivery": delivery or {}}
    if ready:
        st["conditions"] = [{"type": "Ready", "status": "True", "reason": "Ready"}]
        st["ready"] = True
    return ("HybridNetwork", {"metadata": {"name": name, "generation": generation, "creationTimestamp": created},
                              "status": st})


class Delivery(unittest.TestCase):
    def test_ready_tenant(self):
        cond, stalled = T.delivery_condition([cr("a"), cr("b")], TCFG, NOW)
        self.assertEqual((cond["status"], stalled), ("False", []))

    def test_unacknowledged_event(self):
        d = {"eventId": "e", "publishedAt": "2026-10-21T11:00:00Z"}
        cond, stalled = T.delivery_condition([cr("a", ready=False, delivery=d)], TCFG, NOW)
        self.assertEqual((cond["status"], cond["reason"], stalled), ("True", "NoJobStarted", ["HybridNetwork/a"]))
        self.assertNotIn("notified", cond["message"])

    def test_recent_event_not_stalled(self):
        d = {"eventId": "e", "publishedAt": "2026-10-21T11:55:00Z"}
        cond, _ = T.delivery_condition([cr("a", ready=False, delivery=d)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_change_never_published(self):
        cond, _ = T.delivery_condition([cr("a", ready=False, generation=2, observed=1)], TCFG, NOW)
        self.assertEqual(cond["status"], "True")

    def test_redrive_state(self):
        cond, _ = T.delivery_condition([cr("a", ready=False, delivery={"state": "Stalled"})], TCFG, NOW)
        self.assertEqual(cond["status"], "True")

    # review be-controller-status r2 F2: tenant input errors and finished jobs are not delivery problems
    @staticmethod
    def raw(kind, status, generation=1):
        return (kind, {"metadata": {"name": "x", "generation": generation, "creationTimestamp": "2026-10-21T10:00:00Z"},
                       "status": status})

    def test_refused_at_validation_without_observed_generation(self):
        st = {"status": "failed", "ready": False,
              "conditions": [{"type": "Ready", "status": "False", "reason": "ProvisionFailed"}],
              "delivery": {"eventId": "e", "publishedAt": "2026-10-21T10:00:00Z", "acknowledgedEventId": "e",
                           "completedEventId": "e", "completedAt": "2026-10-21T10:02:00Z"}}
        cond, stalled = T.delivery_condition([self.raw("PlatformOpenshift", st)], TCFG, NOW)
        self.assertEqual((cond["status"], stalled), ("False", []))

    def test_completed_event_with_failed_validation_reason(self):
        st = {"conditions": [{"type": "Ready", "status": "False", "reason": "SiteNotAllowed"}],
              "delivery": {"eventId": "e", "publishedAt": "2026-10-21T10:00:00Z", "completedEventId": "e"}}
        cond, _ = T.delivery_condition([self.raw("CloudVirt", st, generation=3)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_refused_by_the_operator_never_published(self):
        st = {"status": "failed", "conditions": [{"type": "Ready", "status": "False", "reason": "OperatorFailed"}]}
        cond, _ = T.delivery_condition([self.raw("VaultKV", st)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_operator_dependency_wait(self):
        st = {"conditions": [{"type": "Ready", "status": "False", "reason": "WaitingForDependencies"}]}
        cond, _ = T.delivery_condition([self.raw("AAPOrg", st)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_finished_job_with_leftover_redrive_state(self):
        st = {"status": "failed", "delivery": {"eventId": "e", "publishedAt": "2026-10-21T10:00:00Z",
                                               "completedEventId": "e", "state": "Stalled"}}
        cond, _ = T.delivery_condition([self.raw("CloudVirt", st)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_running_job_is_not_undelivered(self):
        st = {"status": "reconciling", "delivery": {"eventId": "e", "publishedAt": "2026-10-21T10:00:00Z",
                                                    "acknowledgedEventId": "e"}}
        cond, _ = T.delivery_condition([self.raw("CloudVirt", st, generation=2)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")

    def test_new_event_after_a_failed_one_still_counts(self):
        st = {"status": "failed", "conditions": [{"type": "Ready", "status": "False", "reason": "ProvisionFailed"}],
              "delivery": {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "completedEventId": "e1"}}
        cond, _ = T.delivery_condition([self.raw("PlatformOpenshift", st, generation=2)], TCFG, NOW)
        self.assertEqual(cond["status"], "True")

    def test_publish_failing(self):
        st = {"delivery": {"eventId": "e", "publishedAt": None, "publishFailingSince": "2026-10-21T11:00:00Z"}}
        cond, _ = T.delivery_condition([self.raw("CloudVirt", st)], TCFG, NOW)
        self.assertEqual(cond["status"], "True")
        st = {"delivery": {"eventId": "e", "publishedAt": None}}
        cond, _ = T.delivery_condition([self.raw("CloudVirt", st)], TCFG, NOW)
        self.assertEqual(cond["status"], "False")  # a publish in flight


class ReadyReRun(unittest.TestCase):
    """review be-controller-status c2 F1: a re-run keeps a Ready object Ready until its job writes, so the
    delivery passes must still check its current event (settled = ready and no event outstanding)."""

    @staticmethod
    def ready_team(delivery):
        return {"metadata": {"namespace": "entity-a", "name": "t1", "generation": 2,
                             "creationTimestamp": "2026-10-21T08:00:00Z"},
                "status": {"ready": True, "status": "ready", "observedGeneration": 2,
                           "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"},
                                          {"type": "Reconciling", "status": "True", "reason": "Queued"}],
                           "delivery": delivery}}

    def kinds(self, obj):
        return [{"kind": "Team", "plural": "teams", "version": "v1alpha1", "items": [obj]}]

    LOST = {"eventId": "e2", "publishedAt": "2026-10-21T11:00:00Z", "completedEventId": "e1"}

    def test_lost_event_is_redriven(self):
        api = FakeApi()
        counts, problems = T.eda_redrive.run_redrive(api, self.kinds(self.ready_team(dict(self.LOST))),
                                                     {"redriveKinds": ["Team"]}, NOW, False)
        self.assertEqual((counts["redriven"], counts["readyOutstanding"], counts["notReady"]), (1, 1, 0))
        self.assertEqual(api.annotations, [{T.RECONCILE_NOW: "true", T.eda_redrive.REDRIVES: "1"}])
        self.assertEqual(problems, [])

    def test_lost_event_is_reported_when_redrive_is_off(self):
        api = FakeApi()
        counts, problems = T.eda_redrive.run_redrive(api, self.kinds(self.ready_team(dict(self.LOST))),
                                                     {"redriveKinds": []}, NOW, False)
        self.assertEqual(counts["noAcknowledgement"], 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("NoAcknowledgement", problems[0])

    def test_lost_event_raises_delivery_stalled(self):
        cond, stalled = T.delivery_condition([("Team", self.ready_team(dict(self.LOST)))], TCFG, NOW)
        self.assertEqual((cond["status"], stalled), ("True", ["Team/t1"]))

    def test_stalled_job_on_ready_object(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T10:00:00Z", "acknowledgedEventId": "e2",
             "jobId": "7", "jobStartedAt": "2026-10-21T10:01:00Z"}
        counts, problems = T.eda_redrive.run_redrive(FakeApi(), self.kinds(self.ready_team(d)), {}, NOW, False)
        self.assertEqual(counts["stalled"], 1)
        d["state"] = "Stalled"
        cond, _ = T.delivery_condition([("Team", self.ready_team(d))], TCFG, NOW)
        self.assertEqual(cond["status"], "True")

    def test_completed_event_stays_quiet(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T10:00:00Z", "acknowledgedEventId": "e2",
             "completedEventId": "e2", "state": "NoAcknowledgement"}
        obj = self.ready_team(d)
        obj["metadata"]["annotations"] = {T.eda_redrive.REDRIVES: "2"}
        api = FakeApi()
        counts, problems = T.eda_redrive.run_redrive(api, self.kinds(obj), {"redriveKinds": ["Team"]}, NOW, False)
        self.assertEqual((counts["redriven"], counts["readyOutstanding"], problems), (0, 0, []))
        self.assertEqual(api.annotations, [{T.eda_redrive.REDRIVES: None}])  # settled: bookkeeping cleared
        cond, stalled = T.delivery_condition([("Team", obj)], TCFG, NOW)
        self.assertEqual((cond["status"], stalled), ("False", []))

    def test_recent_rerun_is_not_yet_a_problem(self):
        d = {"eventId": "e2", "publishedAt": "2026-10-21T11:58:00Z", "completedEventId": "e1"}
        counts, problems = T.eda_redrive.run_redrive(FakeApi(), self.kinds(self.ready_team(d)),
                                                     {"redriveKinds": ["Team"]}, NOW, False)
        self.assertEqual((counts["redriven"], counts["noAcknowledgement"], problems), (0, 0, []))
        cond, _ = T.delivery_condition([("Team", self.ready_team(d))], TCFG, NOW)
        self.assertEqual(cond["status"], "False")


class Config(unittest.TestCase):
    def test_flat_config_is_redrive(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("ackTimeoutMinutes: 7\nredriveKinds: [Team]\n")
        try:
            cfg = T.load_config(fh.name)
        finally:
            os.unlink(fh.name)
        if cfg["redrive"].get("ackTimeoutMinutes") is None:
            self.skipTest("PyYAML not available")
        self.assertEqual(cfg["redrive"]["ackTimeoutMinutes"], 7)
        self.assertFalse(cfg["phase"]["write"])



class FakeApi:
    def __init__(self):
        self.annotations = []

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.annotations.append(body["metadata"]["annotations"])

    def patch_namespaced_custom_object_status(self, *args):
        pass


class MachineWakeTest(unittest.TestCase):
    """Re-drive and re-check write the legacy literal "true": operators of every generation act on it."""

    def test_redrive_writes_true(self):
        api = FakeApi()
        cr = {"metadata": {"namespace": "entity-a", "name": "n1", "generation": 1},
              "status": {"ready": False, "delivery": {"eventId": "e1", "publishedAt": "2026-10-21T11:00:00Z"}}}
        kinds = [{"kind": "HybridNetwork", "plural": "hybridnetworks", "version": "v1alpha1", "items": [cr]}]
        counts, _ = T.eda_redrive.run_redrive(api, kinds, {"redriveKinds": ["HybridNetwork"]}, NOW, False)
        self.assertEqual(counts["redriven"], 1)
        self.assertEqual(api.annotations[0][T.RECONCILE_NOW], "true")

    def test_recheck_writes_true(self):
        api = FakeApi()
        cr = {"metadata": {"namespace": "entity-a", "name": "t1", "generation": 1},
              "status": {"ready": False, "observedGeneration": 1,
                         "conditions": [{"type": "Ready", "status": "False", "reason": "ProvisionFailed"}]}}
        kinds = [{"kind": "Team", "plural": "teams", "version": "v1alpha1", "items": [cr]}]
        counts, _ = T.run_failed_recheck(api, kinds, T.DEFAULTS, NOW, False)
        self.assertEqual(counts["rechecked"], 1)
        self.assertEqual(api.annotations[0][T.RECONCILE_NOW], "true")


class NoCompletionTest(unittest.TestCase):
    """GF2-F2: a job that acknowledged its event and never wrote its final status is reported, not left Creating."""

    def cr(self, started_min, running=True, completed=False):
        at = (NOW - dt.timedelta(minutes=started_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        delivery = {"eventId": "e1", "publishedAt": at, "acknowledgedEventId": "e1", "jobId": "7454", "jobStartedAt": at}
        if completed:
            delivery["completedEventId"] = "e1"
        return {"metadata": {"namespace": "entity-a", "name": "d1", "generation": 1,
                             "creationTimestamp": "2026-10-21T10:00:00Z"},
                "status": {"ready": False, "phase": "Creating", "delivery": delivery,
                           "conditions": [{"type": "Reconciling", "status": "True" if running else "False",
                                           "reason": "JobRunning" if running else "AsExpected"}]}}

    def redrive(self, cr, kind="Rbac", cfg=None):
        api = FakeApi()
        kinds = [{"kind": kind, "plural": kind.lower() + "s", "version": "v1alpha1", "items": [cr]}]
        return T.eda_redrive.run_redrive(api, kinds, cfg or {}, NOW, False)

    def test_overdue_job_is_reported(self):
        counts, problems = self.redrive(self.cr(12))
        self.assertEqual(counts["noCompletion"], 1)
        self.assertIn("NoCompletion", problems[0])

    def test_recent_or_finished_job_is_quiet(self):
        for cr in (self.cr(5), self.cr(12, completed=True), self.cr(12, running=False)):
            counts, problems = self.redrive(cr)
            self.assertEqual((counts["noCompletion"], problems), (0, []))

    def test_long_job_kinds_use_their_window(self):
        cfg = {"kinds": {"CloudOSO": {"jobTimeoutMinutes": 150}, "CloudAWS": {"jobFinishedMinutes": 20}}}
        self.assertEqual(self.redrive(self.cr(40), "CloudOSO", cfg)[0]["noCompletion"], 0)
        self.assertEqual(self.redrive(self.cr(15), "CloudAWS", cfg)[0]["noCompletion"], 0)
        self.assertEqual(self.redrive(self.cr(22), "CloudAWS", cfg)[0]["noCompletion"], 1)
        # past the job timeout the Stalled verdict decides
        counts, _ = self.redrive(self.cr(31), "CloudAWS", cfg)
        self.assertEqual((counts["noCompletion"], counts["stalled"]), (0, 1))

    def test_delivery_stalled_counts_it(self):
        cond, stalled = T.delivery_condition([("Rbac", self.cr(12))], TCFG, NOW, {})
        self.assertEqual((cond["status"], stalled), ("True", ["Rbac/d1"]))
        cond, _ = T.delivery_condition([("Rbac", self.cr(5))], TCFG, NOW, {})
        self.assertEqual(cond["status"], "False")


class FinalizerReleaseTest(unittest.TestCase):
    """GA2-F12: a finished teardown whose finalizer is still set gets a machine wake; a long one is reported."""

    def cr(self, completed_min, complete=True, finalizers=("hybridsovereign.redhat/assignment-cleanup",)):
        at = (NOW - dt.timedelta(minutes=completed_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"metadata": {"namespace": "entity-a", "name": "a1", "deletionTimestamp": "2026-10-21T10:00:00Z",
                             "finalizers": list(finalizers)},
                "status": {"deletionComplete": complete, "delivery": {"completedAt": at},
                           "conditions": [{"type": "Stalled", "status": "True", "reason": "TeardownFailed"}]}}

    def run_pass(self, cr):
        api = FakeApi()
        kinds = [{"kind": "Assignment", "plural": "assignments", "version": "v1alpha1", "items": [cr]}]
        counts, actions, problems = T.run_finalizer_release(api, kinds, T.DEFAULTS, NOW, False)
        return api, counts, problems

    def test_recent_finish_waits(self):
        api, counts, _ = self.run_pass(self.cr(1))
        self.assertEqual((counts["finalizerWakes"], api.annotations), (0, []))

    def test_finished_teardown_is_woken(self):
        api, counts, problems = self.run_pass(self.cr(5))
        self.assertEqual(counts["finalizerWakes"], 1)
        self.assertEqual(api.annotations[0][T.RECONCILE_NOW], "true")
        self.assertEqual(problems, [])

    def test_long_stuck_finalizer_is_a_problem(self):
        _, counts, problems = self.run_pass(self.cr(45))
        self.assertEqual(counts["finalizerStuck"], 1)
        self.assertEqual(len(problems), 1)

    def test_unfinished_or_released_is_left_alone(self):
        for cr in (self.cr(45, complete=False), self.cr(45, finalizers=())):
            api, counts, _ = self.run_pass(cr)
            self.assertEqual((counts["finalizerWakes"], api.annotations), (0, []))


class StatusKindsTest(unittest.TestCase):
    def test_skip_tier_kinds_are_left_out(self):
        kinds = [{"kind": "TenantProfile", "labels": {"hybridsovereign.redhat/teardown-tier": "skip"}, "items": []},
                 {"kind": "Team", "labels": {"hybridsovereign.redhat/teardown-tier": "5"}, "items": []},
                 {"kind": "Entity", "labels": {}, "items": []},
                 {"kind": "CloudInfrastructure", "items": []}]
        self.assertEqual([k["kind"] for k in T.status_kinds(kinds)], ["Team", "Entity", "CloudInfrastructure"])


if __name__ == "__main__":
    unittest.main()

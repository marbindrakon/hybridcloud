"""The observation of a cluster installation and the decisions around it (module_utils/shc_install): what one look at
the cluster object, the hub's ClusterDeployment and its current ClusterProvision concludes; every ending of the
watching job; the check before the release; the guarded removal of a stopped build (an installed cluster is never
removed, per provider); the in-flight freshness; the status.install blocks the role writes.

Documentation ranges and example names only.
"""
import datetime
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)

import shc_install as I  # noqa: E402
import shc_status as S  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
QUOTA_LOG = ("level=error msg=error(MissingQuota): Cores is not available because the required number of resources "
             "(72) is more than the limit of 64")
SETTINGS = {"windowMinutes": 120, "deadlineMinutes": 360, "writeEveryMinutes": 5, "provider": "openstack",
            "tenantValues": {"controlPlane": "example.xlarge", "compute": "example.large"}}


def t(minutes_ago):
    return I.fmt_time(NOW - datetime.timedelta(minutes=minutes_ago))


SPEC = {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3, "controlPlaneFlavor": "example.xlarge"}}


def po(install=None, uid="uid-1", event="ev-1", generation=1, deleting=False, conditions=None, spec=None, **status):
    st = dict(status)
    st["delivery"] = {"eventId": event, "acknowledgedEventId": event, "jobStartedAt": t(10)}
    if install is not None:
        st["install"] = install
    if conditions is not None:
        st["conditions"] = conditions
    meta = {"name": "demo", "namespace": "entity-example", "uid": uid, "generation": generation,
            "resourceVersion": "7"}
    if deleting:
        meta["deletionTimestamp"] = t(1)
    return {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "PlatformOpenshift", "metadata": meta,
            "spec": spec if spec is not None else SPEC, "status": st}


def cond(ctype, status, reason="", message=""):
    return {"type": ctype, "status": status, "reason": reason, "message": message}


def cd(installed=False, provisioned=None, stopped=False, failed=None, restarts=0, limit=3, launch_error=False):
    conds = []
    if provisioned is not None:
        conds.append(cond("Provisioned", "True" if provisioned else "False",
                          "Provisioned" if provisioned else ("ProvisionStopped" if stopped else "Provisioning")))
    if stopped:
        conds.append(cond("ProvisionStopped", "True", "InstallAttemptsLimitReached", "Install attempts limit reached"))
    if failed:
        conds.append(cond("ProvisionFailed", "True", failed, "Cluster install failed but no known errors found in logs"))
    if launch_error:
        conds.append(cond("InstallLaunchError", "True", "PodFailedToStart", "x"))
    return {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterDeployment",
            "metadata": {"name": "shc-oso-example-demo-abc12", "namespace": "shc-oso-example-demo-abc12"},
            "spec": {"installed": installed, "installAttemptsLimit": limit},
            "status": {"installRestarts": restarts, "conditions": conds,
                       "provisionRef": {"name": "shc-oso-example-demo-abc12-0-x"}}}


def prov(attempt=0, stage="provisioning", reason=None, log=None, stuck=False):
    conds = []
    if reason is not None:
        conds.append(cond("ClusterProvisionFailed", "True", reason, "Cluster install failed but no known errors found"))
    if stuck:
        conds.append(cond("InstallPodStuck", "True", "PodInPendingPhase", "x"))
    spec = {"attempt": attempt, "stage": stage}
    if log is not None:
        spec["installLog"] = log
    return {"kind": "ClusterProvision", "spec": spec, "status": {"conditions": conds}}


JOB = {"uid": "uid-1", "eventId": "ev-1", "startedAt": t(10)}
RUNNING = {"stage": "Installing", "run": 1, "startedAt": t(8), "attempt": 1, "attemptsLimit": 3,
           "observedAt": t(1)}


def observe(obj, cd_=None, prov_=None, job=JOB, settings=SETTINGS, **kw):
    return I.observe(obj, job, cd_, prov_, NOW, settings, **kw)


def ready(res):
    return [c for c in res["conditions"] if c["type"] == "Ready"][0]


def stalled(res):
    found = [c for c in res["conditions"] if c["type"] == "Stalled"]
    return found[0] if found else None


class Endings(unittest.TestCase):
    """Every way the watching job ends, and that only "installed" leads on to a Ready write."""

    def test_object_gone(self):
        res = observe(None, cd(), prov())
        self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "gone", False))

    def test_another_uid(self):
        res = observe(po(uid="uid-2"), cd(), prov())
        self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "replaced", False))

    def test_being_deleted(self):
        res = observe(po(RUNNING, deleting=True), cd(), prov())
        self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "deleting", False))

    def test_newer_event(self):
        res = observe(po(RUNNING, event="ev-2"), cd(), prov())
        self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "superseded", False))

    def test_installed(self):
        res = observe(po(RUNNING), cd(installed=True, provisioned=True), prov(stage="complete"))
        self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "installed", True))
        self.assertEqual(res["install"]["stage"], "Finishing")
        self.assertEqual(ready(res)["status"], "False")
        self.assertEqual(ready(res)["reason"], "InstallFinishing")

    def test_installed_by_provisioned_condition_only(self):
        res = observe(po(RUNNING), cd(provisioned=True), prov(stage="complete"))
        self.assertEqual(res["outcome"], "installed")

    def test_stopped(self):
        res = observe(po(RUNNING, lastHandledReconcileRequest="2026-11-18T10:00:00Z", generation=4),
                      cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3),
                      prov(attempt=2, stage="failed", reason="UnknownError", log=QUOTA_LOG))
        self.assertEqual((res["end"], res["outcome"], res["write"], res["action"]), (True, "stopped", True, "failed"))
        self.assertEqual(res["install"]["stage"], "Stopped")
        self.assertEqual(res["install"]["endedAt"], I.fmt_time(NOW))
        self.assertEqual(res["install"]["stoppedGeneration"], 4)
        self.assertEqual(res["install"]["stoppedRequest"], "2026-11-18T10:00:00Z")
        self.assertEqual(res["install"]["stoppedSpec"], I.spec_digest(SPEC))
        msg = ("Attempt 3 of 3 failed: the site reports too little quota: Cores (72 needed, limit 64). "
               "The installer stopped after 3 attempts.")
        self.assertEqual(ready(res), cond("Ready", "False", "QuotaExceeded", msg))
        self.assertEqual(stalled(res), cond("Stalled", "True", "QuotaExceeded", msg))

    def test_stop_texts_say_what_the_hub_knows(self):
        """The attempts made, not the limit; a stop for another reason says only that it stopped; the times are the
        hub's, not the check's."""
        hub = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=1)
        hub["status"]["conditions"][-2]["reason"] = "SomethingElse"
        hub["status"]["conditions"][-2]["lastTransitionTime"] = t(7)
        p = prov(attempt=1, stage="failed", reason="UnknownError", log=QUOTA_LOG)
        p["status"]["conditions"][0]["lastTransitionTime"] = t(9)
        res = observe(po(RUNNING), hub, p)
        self.assertTrue(stalled(res)["message"].endswith("). The installer stopped."), stalled(res)["message"])
        self.assertTrue(stalled(res)["message"].startswith("Attempt 2 of 3 failed"))
        self.assertEqual(res["install"]["lastFailure"]["time"], t(9))
        self.assertEqual(res["install"]["endedAt"], t(7))
        two = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=1)
        res = observe(po(RUNNING), two, p)
        self.assertTrue(stalled(res)["message"].endswith("The installer stopped after 2 attempts."))

    def test_overdue_is_failed_and_keeps_being_checked(self):
        """Past the deadline: Failed (InstallOverdue) without Stalled, so the operator's periodic runs go on checking
        it; nothing of a stop is recorded."""
        res = observe(po(dict(RUNNING, startedAt=t(400))), cd(provisioned=False), prov(attempt=1))
        self.assertEqual((res["end"], res["outcome"], res["action"], res["write"]), (True, "overdue", "failed", True))
        self.assertEqual(ready(res)["reason"], "InstallOverdue")
        self.assertEqual(ready(res)["message"], "Attempt 2 of 3 has not finished; the installation has run for more "
                                                "than 360 minutes.")
        self.assertIsNone(stalled(res))
        self.assertEqual(res["install"]["stage"], "Installing", "Hive may still be trying: the stage stays")
        for k in ("stoppedGeneration", "stoppedRequest", "stoppedSpec", "endedAt"):
            self.assertNotIn(k, res["install"])
        patch, info = S.compute_status_patch(po(RUNNING), {"status": res["status"], "install": res["install"],
                                                           "conditions": res["conditions"]},
                                             {"action": res["action"], "event_id": "ev-1"})
        self.assertEqual(info["phase"], "Failed")
        self.assertEqual(patch["status"]["delivery"]["completedEventId"], "ev-1", "the next periodic run publishes")
        self.assertIn("The platform keeps checking it", patch["status"]["summary"])

    def test_overdue_then_installed_or_stopped(self):
        late = po(dict(RUNNING, startedAt=t(500)), conditions=[cond("Ready", "False", "InstallOverdue", "m")])
        res = observe(late, cd(installed=True, provisioned=True), prov(stage="complete", attempt=1))
        self.assertEqual((res["outcome"], res["install"]["stage"]), ("installed", "Finishing"))
        res = observe(late, cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3),
                      prov(attempt=2, stage="failed", reason="UnknownError", log=QUOTA_LOG))
        self.assertEqual((res["outcome"], stalled(res)["reason"]), ("stopped", "QuotaExceeded"))

    def test_overdue_helpers(self):
        self.assertTrue(I.overdue({"install": dict(RUNNING, startedAt=t(400))}, NOW, 360))
        self.assertFalse(I.overdue({"install": dict(RUNNING, startedAt=t(100))}, NOW, 360))
        self.assertFalse(I.overdue({"install": dict(STOPPED)}, NOW, 360))
        self.assertFalse(I.overdue({}, NOW, 360))

    def test_a_build_being_removed_concludes_nothing(self):
        """A ClusterDeployment with a deletion time (a removal under way): no stop, no write, the watch goes on."""
        hub = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3)
        hub["metadata"]["deletionTimestamp"] = t(1)
        res = observe(po({"stage": "Installing", "run": 2, "startedAt": t(2)}), hub,
                      prov(attempt=2, stage="failed", reason="UnknownError", log=QUOTA_LOG))
        self.assertEqual((res["end"], res["outcome"], res["write"]), (False, None, False))
        res = observe(po(RUNNING), hub, None, job=dict(JOB, startedAt=t(200)))
        self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "window", False))

    def test_stalled_never_stands_while_an_installation_runs(self):
        stale = [cond("Stalled", "True", "QuotaExceeded", "old")]
        for hub, p in ((cd(provisioned=False), prov()), (None, None),
                       (cd(provisioned=False, failed="UnknownError", restarts=1),
                        prov(attempt=0, stage="failed", reason="UnknownError", log=QUOTA_LOG))):
            res = observe(po(RUNNING, conditions=stale), hub, p)
            self.assertEqual(stalled(res)["status"], "False", hub)
        res = observe(po(dict(RUNNING, startedAt=t(400)), conditions=stale), cd(provisioned=False), prov())
        self.assertEqual(stalled(res)["status"], "False", "overdue")

    def test_the_first_stop_write_records_the_newest_request(self):
        """The write that first records a stop records the newest Retry request present then as handled at the stop
        (also one made while the page still said Installing): only a request made after this write asks for a new
        installation. A stop already recorded is not recorded again."""
        hub = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3)
        hub["status"]["conditions"][-2]["lastTransitionTime"] = t(15)
        obj = po(RUNNING, lastHandledReconcileRequest="2026-11-18T11:58:00Z")
        obj["status"]["delivery"].update(reason="PlatformOpenshiftReconcileRequested", publishedAt=t(2))
        res = observe(obj, hub, prov(attempt=2, stage="failed", reason="UnknownError", log=QUOTA_LOG))
        self.assertEqual(res["install"]["stoppedRequest"], "2026-11-18T11:58:00Z")
        stopped = po(res["install"], lastHandledReconcileRequest="2026-11-18T11:58:00Z")
        stopped["status"]["delivery"].update(reason="PlatformOpenshiftReconcileRequested", publishedAt=t(2))
        self.assertFalse(I.asked_since_stop(stopped)[0], "the request before the stop's write does not ask")
        # a request after the stop's write asks
        later = po(res["install"], lastHandledReconcileRequest="2026-11-18T12:00:30Z")
        self.assertTrue(I.asked_since_stop(later)[0])
        # an event published after the stop was written asks too (a request the operator does not record)
        ev = {"reason": "PlatformOpenshiftReconcileRequested", "publishedAt": I.fmt_time(NOW + datetime.timedelta(minutes=1))}
        self.assertTrue(I.asked_since_stop(stopped, event=ev)[0])
        # a second observation of the same stop records nothing again
        again = I.observe(po(res["install"], lastHandledReconcileRequest="2026-11-18T12:00:30Z"), JOB, hub, None,
                          NOW + datetime.timedelta(minutes=2), SETTINGS)
        self.assertEqual((again["outcome"], again["write"]), ("stopped", False))
        self.assertEqual(again["install"]["stoppedRequest"], "2026-11-18T11:58:00Z")

    def test_malformed_status_or_hub_data_does_not_raise(self):
        for lf in ({"attempt": 1}, {"attempt": 1, "reason": "QuotaExceeded"}, "text", None):
            observe(po(dict(RUNNING, lastFailure=lf)), cd(provisioned=False, stopped=True, restarts=3), prov())
            observe(po(dict(RUNNING, lastFailure=lf)), cd(provisioned=False, failed="UnknownError", restarts=3),
                    prov(attempt=2, stage="failed"))
        nulls = prov()
        nulls["spec"] = None
        observe(po(RUNNING), cd(provisioned=False), nulls)
        observe(po(RUNNING), cd(provisioned=False), None, provisions=[nulls, prov(attempt=1)])

    def test_a_failed_release_without_a_build_is_a_stop(self):
        res = observe(po({"stage": "Installing", "run": 2, "startedAt": t(3)}), None, None,
                      settings=dict(SETTINGS, releaseFailed=True))
        self.assertEqual((res["end"], res["outcome"], res["action"]), (True, "stopped", "failed"))
        self.assertEqual(res["install"]["stage"], "Stopped")
        self.assertEqual(stalled(res)["reason"], "InstallNotStarted")
        self.assertEqual(res["install"]["stoppedSpec"], I.spec_digest(SPEC))
        stopped = po(res["install"])
        self.assertTrue(I.may_remove_build(stopped, None, True, release_failed=True, event={"x": 1})[0] is False)
        changed = po(res["install"], spec={"type": "openstack", "openstack": {"environment": "env", "workerCount": 0}})
        self.assertTrue(I.may_remove_build(changed, None, True, release_failed=True)[0])

    def test_window_reached(self):
        res = observe(po(RUNNING), cd(provisioned=False), prov(), job=dict(JOB, startedAt=t(121)))
        self.assertEqual((res["end"], res["outcome"], res["write"], res["action"]),
                         (True, "window", True, "installing"))
        self.assertEqual(ready(res)["reason"], "Installing")

    def test_go_on(self):
        res = observe(po(RUNNING), cd(provisioned=False), prov())
        self.assertEqual((res["end"], res["outcome"]), (False, None))

    def test_unknown_object_or_hub_goes_on_without_a_write(self):
        for kw in ({"obj_error": True}, {"hub_error": True}):
            res = observe(po(RUNNING), None, None, **kw)
            self.assertEqual((res["end"], res["write"]), (False, False), kw)
            res = observe(po(RUNNING), None, None, job=dict(JOB, startedAt=t(200)), **kw)
            self.assertEqual((res["end"], res["outcome"], res["write"]), (True, "window", False), kw)

    def test_hub_error_never_stops_or_fails(self):
        res = observe(po(dict(RUNNING, startedAt=t(500))), None, None, hub_error=True)
        self.assertNotIn(res["outcome"], ("stopped", "overdue"))

    def test_no_ending_but_installed_writes_ready_true(self):
        cases = [observe(None), observe(po(uid="x")), observe(po(RUNNING, deleting=True)),
                 observe(po(RUNNING, event="ev-9")),
                 observe(po(RUNNING), cd(provisioned=False, stopped=True, failed="UnknownError"), None),
                 observe(po(dict(RUNNING, startedAt=t(400))), cd(), prov()),
                 observe(po(RUNNING), cd(), prov(), job=dict(JOB, startedAt=t(500))),
                 observe(po(RUNNING), cd(installed=True), prov(stage="complete"))]
        for res in cases:
            for c in res["conditions"]:
                self.assertFalse(c["type"] == "Ready" and c["status"] == "True", res)


class Stages(unittest.TestCase):
    def test_no_build_yet(self):
        res = observe(po({"stage": "Preparing", "run": 1}), None, None)
        self.assertEqual(res["install"]["stage"], "Installing")
        self.assertFalse(res["end"])

    def test_attempt_1_running(self):
        res = observe(po(RUNNING), cd(provisioned=False), prov(attempt=0))
        self.assertEqual((res["install"]["attempt"], res["install"]["attemptsLimit"]), (1, 3))
        self.assertEqual(ready(res), cond("Ready", "False", "Installing", "Installing, attempt 1 of 3."))
        self.assertNotIn("lastFailure", res["install"])

    def test_attempt_1_failed_quota_attempt_2_not_started(self):
        res = observe(po(RUNNING), cd(provisioned=False, failed="UnknownError", restarts=1),
                      prov(attempt=0, stage="failed", reason="UnknownError", log=QUOTA_LOG))
        lf = res["install"]["lastFailure"]
        self.assertEqual((lf["attempt"], lf["reason"]), (1, "QuotaExceeded"))
        self.assertEqual(lf["message"], "Attempt 1 of 3 failed: the site reports too little quota: Cores (72 needed, "
                                        "limit 64).")
        self.assertEqual(ready(res)["reason"], "InstallRetrying")
        self.assertFalse(res["end"])
        self.assertIsNone(stalled(res))

    def test_last_failure_kept_while_the_next_attempt_runs(self):
        lf = {"attempt": 1, "reason": "QuotaExceeded", "message": "Attempt 1 of 3 failed: x.", "time": t(3)}
        res = observe(po(dict(RUNNING, lastFailure=lf)), cd(provisioned=False, failed="UnknownError", restarts=1),
                      prov(attempt=1))
        self.assertEqual(res["install"]["attempt"], 2)
        self.assertEqual(res["install"]["lastFailure"], lf)
        self.assertEqual(ready(res)["reason"], "InstallRetrying")

    def test_failure_seen_only_on_the_deployment(self):
        res = observe(po(RUNNING), cd(provisioned=False, failed="AWSVPCLimitExceeded", restarts=1), prov(attempt=1),
                      settings=dict(SETTINGS, provider="aws"))
        self.assertEqual(res["install"]["lastFailure"]["attempt"], 1)
        self.assertEqual(res["install"]["lastFailure"]["reason"], "QuotaExceeded")

    def test_unknown_failure_on_the_deployment_only_is_not_called_no_log(self):
        res = observe(po(RUNNING), cd(provisioned=False, failed="UnknownError", restarts=1), prov(attempt=1))
        self.assertEqual(res["install"]["lastFailure"]["reason"], "InstallFailed")

    def test_stopped_on_unknown_cause(self):
        res = observe(po(RUNNING), cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3),
                      prov(attempt=2, stage="failed", reason="UnknownError", log="level=fatal msg=odd thing at 192.0.2.4"))
        self.assertEqual(ready(res)["reason"], "InstallFailed")
        self.assertNotIn("192.0.2", ready(res)["message"])

    def test_stopped_on_refused_setting(self):
        log = 'compute[0].platform.openstack.type: Not found: "example.large"'
        res = observe(po(RUNNING), cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3),
                      prov(attempt=2, stage="failed", reason="UnknownError", log=log))
        self.assertEqual(ready(res)["reason"], "InstallSettingRefused")
        self.assertIn('worker flavor "example.large"', ready(res)["message"])

    def test_stopped_on_aws_and_gcp_quota_codes(self):
        for provider, code in (("aws", "AWSEC2QuotaExceeded"), ("gcp", "GCPComputeQuotaExceeded")):
            res = observe(po(RUNNING), cd(provisioned=False, stopped=True, failed=code, restarts=3),
                          prov(attempt=2, stage="failed", reason=code, log="level=error msg=x"),
                          settings=dict(SETTINGS, provider=provider))
            self.assertEqual(stalled(res)["reason"], "QuotaExceeded", provider)

    def test_pod_not_starting(self):
        res = observe(po(RUNNING), cd(provisioned=False, launch_error=True), prov(attempt=0, stuck=True))
        self.assertEqual(ready(res)["reason"], "InstallNotStarted")
        self.assertFalse(res["end"])
        self.assertIsNone(stalled(res), "the platform keeps checking: not stalled")

    def test_installed_clears_stalled_and_last_failure(self):
        lf = {"attempt": 1, "reason": "QuotaExceeded", "message": "m", "time": t(3)}
        res = observe(po(dict(RUNNING, lastFailure=lf), conditions=[cond("Stalled", "True", "QuotaExceeded")]),
                      cd(installed=True, provisioned=True), prov(stage="complete", attempt=1))
        # sent as null: the merge patch removes the stored failure (an omitted key would keep it)
        self.assertIn("lastFailure", res["install"])
        self.assertIsNone(res["install"]["lastFailure"])
        self.assertEqual(res["install"]["endedAt"], I.fmt_time(NOW))
        self.assertEqual(stalled(res)["status"], "False")


class Writes(unittest.TestCase):
    def test_no_write_when_nothing_changed_and_the_check_time_is_recent(self):
        first = observe(po(RUNNING), cd(provisioned=False), prov())
        obj = po(first["install"], conditions=first["conditions"])
        again = I.observe(obj, JOB, cd(provisioned=False), prov(), NOW + datetime.timedelta(seconds=30), SETTINGS)
        self.assertFalse(again["write"])

    def test_check_time_written_every_five_minutes(self):
        first = observe(po(RUNNING), cd(provisioned=False), prov())
        obj = po(first["install"], conditions=first["conditions"])
        later = I.observe(obj, JOB, cd(provisioned=False), prov(), NOW + datetime.timedelta(minutes=5), SETTINGS)
        self.assertTrue(later["write"])

    def test_a_change_is_written_at_once(self):
        first = observe(po(RUNNING), cd(provisioned=False), prov())
        obj = po(first["install"], conditions=first["conditions"])
        res = I.observe(obj, JOB, cd(provisioned=False, failed="UnknownError", restarts=1),
                        prov(stage="failed", reason="UnknownError", log=QUOTA_LOG),
                        NOW + datetime.timedelta(seconds=30), SETTINGS)
        self.assertTrue(res["write"])

    def test_the_status_library_reads_the_stop_as_failed(self):
        res = observe(po(RUNNING), cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3),
                      prov(attempt=2, stage="failed", reason="UnknownError", log=QUOTA_LOG))
        obj = po(RUNNING)
        patch, info = S.compute_status_patch(obj, {"status": res["status"], "install": res["install"],
                                                   "conditions": res["conditions"]},
                                             {"action": res["action"], "event_id": "ev-1"})
        self.assertEqual(info["phase"], "Failed")
        self.assertEqual(patch["status"]["summary"], "The environment's quota is too small for this cluster. Make the "
                                                     "cluster smaller or have the quota raised, then retry.")
        self.assertEqual(patch["status"]["delivery"]["completedEventId"], "ev-1")

    def test_the_still_installing_write_completes_the_event_and_keeps_the_phase(self):
        res = observe(po(RUNNING), cd(provisioned=False), prov(), job=dict(JOB, startedAt=t(121)))
        obj = po(RUNNING, conditions=[cond("Ready", "False", "Installing", "Installing, attempt 1 of 3."),
                                      cond("Reconciling", "True", "JobRunning")], phase="Creating", phaseSince=t(9))
        patch, info = S.compute_status_patch(obj, {"status": res["status"], "install": res["install"],
                                                   "conditions": res["conditions"]},
                                             {"action": res["action"], "event_id": "ev-1"})
        self.assertEqual(info["phase"], "Creating")
        self.assertEqual(patch["status"]["phaseSince"], t(9))
        self.assertEqual(patch["status"]["delivery"]["completedEventId"], "ev-1")
        self.assertEqual(patch["status"]["summary"], "The cluster is being installed.")


class SameObject(unittest.TestCase):
    """The check right before the release."""

    def test_cases(self):
        self.assertEqual(I.same_object(po(), "uid-1", "ev-1"), (True, ""))
        self.assertFalse(I.same_object(None, "uid-1", "ev-1")[0])
        self.assertFalse(I.same_object(po(uid="uid-2"), "uid-1", "ev-1")[0])
        self.assertFalse(I.same_object(po(deleting=True), "uid-1", "ev-1")[0])
        self.assertFalse(I.same_object(po(event="ev-2"), "uid-1", "ev-1")[0])

    def test_the_spec_read_is_the_spec_installed(self):
        """The check before the release compares the generation the job read its spec at."""
        self.assertTrue(I.same_object(po(generation=4), "uid-1", "ev-1", 4)[0])
        ok, why = I.same_object(po(generation=5), "uid-1", "ev-1", 4)
        self.assertFalse(ok)
        self.assertIn("spec changed", why)
        self.assertTrue(I.same_object(po(generation=5), "uid-1", "ev-1")[0], "the removal's checks do not compare it")


STOPPED = {"stage": "Stopped", "run": 1, "startedAt": t(60), "endedAt": t(30), "stoppedGeneration": 2,
           "stoppedRequest": "2026-11-18T09:00:00Z", "stoppedSpec": I.spec_digest(SPEC), "stoppedSpecVersion": I.DIGEST_VERSION, "attempt": 3,
           "attemptsLimit": 3}
SMALLER = {"type": "openstack", "openstack": dict(SPEC["openstack"], workerCount=0)}


class Removal(unittest.TestCase):
    """The guarded removal of a stopped build: all four conditions, read fresh; unknown counts as installed."""

    STOPPED_CD = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3)

    def asked(self, **kw):
        """Stopped, and the installer's part of the spec changed since (the worker count)."""
        return po(STOPPED, generation=3, lastHandledReconcileRequest="2026-11-18T09:00:00Z", spec=SMALLER, **kw)

    def test_allowed_on_a_change_of_the_installation_settings(self):
        self.assertTrue(I.may_remove_build(self.asked(), self.STOPPED_CD, True)[0])

    def test_a_change_outside_the_installation_settings_does_not_ask(self):
        """Access groups, viewers, deletion protection: a newer generation with the same installer settings."""
        for extra in ({"toolRbac": {"clusterAdminRbac": ["admins"]}}, {"clusterViewerRbac": ["readers"]},
                      {"deletionProtection": True}):
            obj = po(STOPPED, generation=3, lastHandledReconcileRequest="2026-11-18T09:00:00Z",
                     spec=dict(SPEC, **extra))
            ok, why = I.may_remove_build(obj, self.STOPPED_CD, True)
            self.assertFalse(ok, extra)
            self.assertIn("nobody asked", why)

    def test_a_stop_recorded_without_the_digest_compares_the_generation(self):
        install = {k: v for k, v in STOPPED.items() if k != "stoppedSpec"}
        self.assertTrue(I.asked_since_stop(po(install, generation=3,
                                              lastHandledReconcileRequest="2026-11-18T09:00:00Z"))[0])
        self.assertFalse(I.asked_since_stop(po(install, generation=2,
                                               lastHandledReconcileRequest="2026-11-18T09:00:00Z"))[0])

    def test_allowed_on_a_newer_retry_request(self):
        obj = po(STOPPED, generation=2, lastHandledReconcileRequest="2026-11-18T11:59:00Z")
        self.assertTrue(I.may_remove_build(obj, self.STOPPED_CD, True)[0])

    def test_allowed_on_a_retry_event_published_after_the_stop(self):
        obj = po(STOPPED, generation=2, lastHandledReconcileRequest="2026-11-18T09:00:00Z")
        ev = {"reason": "PlatformOpenshiftReconcileRequested", "publishedAt": t(1)}
        self.assertTrue(I.may_remove_build(obj, self.STOPPED_CD, True, event=ev)[0])
        ev_old = {"reason": "PlatformOpenshiftReconcileRequested", "publishedAt": t(40)}
        self.assertFalse(I.may_remove_build(obj, self.STOPPED_CD, True, event=ev_old)[0])

    def test_nobody_asked(self):
        obj = po(STOPPED, generation=2, lastHandledReconcileRequest="2026-11-18T09:00:00Z")
        ok, why = I.may_remove_build(obj, self.STOPPED_CD, True)
        self.assertFalse(ok)
        self.assertIn("nobody asked", why)

    def test_hub_unreadable_means_no_removal(self):
        self.assertFalse(I.may_remove_build(self.asked(), None, False)[0])
        self.assertFalse(I.may_remove_build(self.asked(), self.STOPPED_CD, False)[0])

    def test_object_unreadable_means_no_removal(self):
        self.assertFalse(I.may_remove_build(None, self.STOPPED_CD, True)[0])

    def test_hive_still_trying_is_never_removed(self):
        for hub in (cd(provisioned=False, failed="UnknownError", restarts=1), cd(provisioned=False)):
            self.assertFalse(I.may_remove_build(self.asked(), hub, True)[0])

    def test_no_deployment_left(self):
        """No ClusterDeployment: whatever release of the build is left (failed, pending, deployed, none) is no live
        build; a Retry removes it and installs (a fresh installation)."""
        self.assertTrue(I.may_remove_build(self.asked(), None, True, release_failed=True, release_exists=True)[0])
        self.assertTrue(I.may_remove_build(self.asked(), None, True, release_exists=False)[0])
        self.assertTrue(I.may_remove_build(self.asked(), None, True, release_failed=False, release_exists=True)[0])

    def test_object_without_a_recorded_stop(self):
        obj = po({"stage": "Installing", "run": 1}, generation=3)
        self.assertFalse(I.may_remove_build(obj, self.STOPPED_CD, True)[0])
        self.assertFalse(I.may_remove_build(po(None, generation=3), self.STOPPED_CD, True)[0])

    def test_every_combination(self):
        """All 2^4 combinations of the four conditions (plus unreadable hub): only all four true removes."""
        for hub_installed in (False, True):
            for hub_stopped in (False, True):
                for obj_installed in (False, True):
                    for asked in (False, True):
                        for readable in (False, True):
                            hub = cd(installed=hub_installed, provisioned=hub_installed or None, stopped=hub_stopped,
                                     failed="UnknownError")
                            extra = {}
                            if obj_installed:
                                extra["clusterInstalled"] = True
                            obj = po(dict(STOPPED), generation=3, spec=SMALLER if asked else SPEC,
                                     lastHandledReconcileRequest="2026-11-18T09:00:00Z", **extra)
                            ok = I.may_remove_build(obj, hub, readable)[0]
                            want = readable and not hub_installed and hub_stopped and not obj_installed and asked
                            self.assertEqual(ok, want, (hub_installed, hub_stopped, obj_installed, asked, readable))

    def test_installed_anywhere_is_never_removed_per_provider(self):
        specs = {"openstack": SMALLER,
                 "aws": {"type": "aws", "aws": {"environment": "env", "workerCount": 0}},
                 "gcp": {"type": "gcp", "gcp": {"environment": "env", "workerCount": 0}}}
        for provider, spec in specs.items():
            with self.subTest(provider):
                stop = dict(STOPPED, stoppedSpec=I.spec_digest(dict(spec, **{provider: dict(spec[provider],
                                                                                              workerCount=2)})))
                asked = po(stop, generation=3, spec=spec, lastHandledReconcileRequest="2026-11-18T09:00:00Z")
                self.assertTrue(I.may_remove_build(asked, self.STOPPED_CD, True)[0], "asked and stopped: removed")
                # the hub says installed
                hub = cd(installed=True, provisioned=True, stopped=True)
                self.assertFalse(I.may_remove_build(asked, hub, True)[0])
                self.assertFalse(I.may_remove_build(asked, hub, True, recheck=True)[0])
                # the object says installed
                inst = po(stop, generation=3, spec=spec, clusterInstalled=True)
                self.assertFalse(I.may_remove_build(inst, self.STOPPED_CD, True)[0])
                self.assertFalse(I.may_remove_build(inst, None, True, release_failed=True)[0])
                for stage in ("Finishing", "Installed"):
                    obj = po(dict(stop, stage=stage), generation=3, spec=spec)
                    self.assertFalse(I.may_remove_build(obj, self.STOPPED_CD, True)[0])
                    self.assertFalse(I.may_remove_build(obj, None, True, recheck=True)[0])


class Recheck(unittest.TestCase):
    """The fresh read right before the first delete, and a removal under way that a later run continues."""

    STOPPED_CD = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3)

    def test_recheck(self):
        cleaning = po(dict(STOPPED, stage="CleaningUp", run=2), generation=3)
        self.assertTrue(I.may_remove_build(cleaning, self.STOPPED_CD, True, recheck=True)[0])
        self.assertFalse(I.may_remove_build(cleaning, cd(installed=True, provisioned=True), True, recheck=True)[0])
        self.assertFalse(I.may_remove_build(cleaning, self.STOPPED_CD, False, recheck=True)[0])
        self.assertFalse(I.may_remove_build(cleaning, cd(provisioned=False), True, recheck=True)[0])
        installed = po(dict(STOPPED, stage="CleaningUp"), clusterInstalled=True)
        self.assertFalse(I.may_remove_build(installed, self.STOPPED_CD, True, recheck=True)[0])
        running = po(dict(STOPPED, stage="Installing"))
        self.assertFalse(I.may_remove_build(running, self.STOPPED_CD, True, recheck=True)[0])

    def test_a_removal_under_way_is_continued(self):
        """Stage CleaningUp (an earlier job decided and recorded it): a later job continues the removal through the
        same guard, whether the ClusterDeployment is stopped, being deleted or gone; never a running or installed
        build."""
        cleaning = po({"stage": "CleaningUp", "run": 2, "observedAt": t(2)}, generation=3)
        deleting = cd(provisioned=False, stopped=True, failed="UnknownError", restarts=3)
        deleting["metadata"]["deletionTimestamp"] = t(1)
        for hub in (self.STOPPED_CD, None):
            ok, why = I.may_remove_build(cleaning, hub, True, release_exists=True)
            self.assertTrue(ok, hub)
            self.assertIn("goes on", why)
        # a build Hive is deprovisioning: nothing more is removed now; the run after it is gone continues
        ok, why = I.may_remove_build(cleaning, deleting, True, release_exists=True)
        self.assertFalse(ok)
        self.assertIn("next run continues", why)
        self.assertFalse(I.may_remove_build(cleaning, cd(provisioned=False), True)[0])
        self.assertFalse(I.may_remove_build(cleaning, cd(installed=True, provisioned=True), True)[0])
        self.assertFalse(I.may_remove_build(cleaning, self.STOPPED_CD, False)[0])


class NetworkPath(unittest.TestCase):
    """"Could not reach the cluster" only for that cause, and only on a hybrid network."""

    def test_api_unreachable_on_a_network(self):
        got = I.classify("openstack", "KubeAPIWaitTimeout", "level=fatal msg=x", network_path=True)
        self.assertIn("could not reach the cluster's API", got["text"])
        self.assertEqual(got["reason"], "InstallFailed")

    def test_other_causes_on_a_network_are_not_called_unreachable(self):
        quota = I.classify("openstack", "UnknownError", QUOTA_LOG, network_path=True)
        self.assertEqual(quota["reason"], "QuotaExceeded")
        self.assertNotIn("reach", quota["text"])
        other = I.classify("openstack", "BootstrapFailed", "level=fatal msg=x", network_path=True)
        self.assertNotIn("reach", other["text"])

    def test_not_on_a_network(self):
        got = I.classify("openstack", "KubeAPIWaitTimeout", "level=fatal msg=x", network_path=False)
        self.assertEqual(got["text"], "the installer reported KubeAPIWaitTimeout")

    def test_through_the_observation(self):
        res = observe(po(RUNNING), cd(provisioned=False, stopped=True, failed="KubeAPIWaitTimeout", restarts=3),
                      prov(attempt=2, stage="failed", reason="KubeAPIWaitTimeout",
                           log="level=fatal msg=waiting for Kubernetes API: context deadline exceeded"),
                      settings=dict(SETTINGS, networkPath=True))
        self.assertIn("could not reach the cluster's API", stalled(res)["message"])


class InstallBlocks(unittest.TestCase):
    def test_preparing_only_for_a_new_cluster(self):
        self.assertEqual(I.install_preparing({}, NOW), {"stage": "Preparing", "run": 1})
        self.assertIsNone(I.install_preparing({"install": {"stage": "Installing"}}, NOW))
        self.assertIsNone(I.install_preparing({"clusterInstalled": True}, NOW))
        self.assertIsNone(I.install_preparing({"installStarted": True}, NOW))

    def test_starting_keeps_a_running_installation(self):
        for stage in ("Installing", "Finishing", "Installed", "Stopped"):
            self.assertIsNone(I.install_starting({"install": {"stage": stage, "startedAt": t(30)}}, NOW))
        self.assertIsNone(I.install_starting({"clusterInstalled": True}, NOW))
        got = I.install_starting({"install": {"stage": "CleaningUp", "run": 2}}, NOW)
        self.assertEqual((got["stage"], got["run"], got["startedAt"]), ("Installing", 2, I.fmt_time(NOW)))
        self.assertEqual(I.install_starting({}, NOW)["run"], 1)

    def test_cleaning_starts_the_next_run(self):
        got = I.install_cleaning({"install": dict(STOPPED)}, NOW)
        self.assertEqual((got["stage"], got["run"]), ("CleaningUp", 2))
        for k in ("lastFailure", "endedAt", "stoppedGeneration", "stoppedRequest"):
            self.assertIsNone(got[k])
        # the new run starts with the removal: its time bound counts from here
        self.assertEqual(got["startedAt"], I.fmt_time(NOW))

    def test_check_time_only_on_a_running_installation(self):
        for stage in ("Preparing", "CleaningUp", "Installing", "Finishing"):
            self.assertEqual(I.install_check_time({"install": {"stage": stage}}, NOW), {"observedAt": I.fmt_time(NOW)})
        for status in ({"install": {"stage": "Installed"}}, {"install": {"stage": "Stopped"}}, {}, {"clusterInstalled": True}):
            self.assertIsNone(I.install_check_time(status, NOW))

    def test_a_cluster_from_before_installations_were_recorded_keeps_its_record(self):
        """install_starting leaves a record without status.install alone when its installation started (the watch
        writes it from the hub); a new object gets its first installation."""
        self.assertIsNone(I.install_starting({"installStarted": True}, NOW))
        self.assertEqual(I.install_starting({}, NOW)["stage"], "Installing")

    def test_installed(self):
        got = I.install_installed({"install": {"stage": "Finishing", "run": 2, "endedAt": t(4)}}, NOW)
        self.assertEqual((got["stage"], got["endedAt"]), ("Installed", t(4)))
        self.assertNotIn("run", got)
        self.assertIsNone(got["stoppedSpec"])

    def test_an_older_installed_cluster_gets_no_invented_end(self):
        """A cluster installed before installations were recorded: its end is not known, none is written."""
        self.assertNotIn("endedAt", I.install_installed({"clusterInstalled": True}, NOW))

    def test_the_end_of_an_installed_cluster(self):
        """endedAt: as the watch recorded it, else the hub's install time; never written empty (an end already
        stored is kept by the merge)."""
        hub = cd(installed=True, provisioned=True)
        hub["status"]["installedTimestamp"] = t(7)
        self.assertEqual(I.install_installed({"install": {"stage": "Finishing"}}, NOW, hub)["endedAt"], t(7))
        self.assertEqual(I.install_installed({"install": {"stage": "Finishing", "endedAt": t(9)}}, NOW, hub)["endedAt"],
                         t(9))
        self.assertNotIn("endedAt", I.install_installed({"install": {"stage": "Finishing"}}, NOW))

    def test_stopped_seen(self):
        self.assertEqual(I.install_stopped_seen(po(STOPPED, generation=5)),
                         {"stoppedGeneration": 5, "stoppedSpec": I.spec_digest(SPEC),
                          "stoppedSpecVersion": I.DIGEST_VERSION})


class HubWins(unittest.TestCase):
    """A recorded stop against what the hub holds: one outcome per hub state (the role's table)."""

    def test_hub_states(self):
        deleting = cd(provisioned=False, stopped=True)
        deleting["metadata"]["deletionTimestamp"] = t(1)
        cases = [(None, False, "unreadable"), (None, True, "none"), (deleting, True, "deleting"),
                 (cd(installed=True, provisioned=True), True, "installed"),
                 (cd(provisioned=True), True, "installed"),
                 (cd(provisioned=False, stopped=True, failed="UnknownError"), True, "stopped"),
                 (cd(provisioned=False, failed="UnknownError", restarts=1), True, "running"), (cd(), True, "running")]
        for hub, ok, want in cases:
            self.assertEqual(I.hub_state(hub, ok), want, want)
        self.assertEqual(set(I.HUB_STATES), {w for _, _, w in cases})

    def test_a_running_or_installed_build_is_never_removed_from_a_recorded_stop(self):
        asked = po(STOPPED, generation=3, spec=SMALLER, lastHandledReconcileRequest="2026-11-18T11:59:00Z")
        for hub in (cd(provisioned=False), cd(installed=True, provisioned=True)):
            self.assertFalse(I.may_remove_build(asked, hub, True)[0])

    def test_the_hubs_state_replaces_the_record(self):
        got = I.install_resumed({})
        self.assertEqual(got["stage"], "Installing")
        for k in ("endedAt", "attempt", "lastFailure", "stoppedGeneration", "stoppedRequest", "stoppedSpec",
                  "stoppedSpecVersion"):
            self.assertIn(k, got, k)
            self.assertIsNone(got[k], k)
        self.assertNotIn("run", got, "no new run is counted")

    def test_a_resumed_attempt_does_not_read_as_failed(self):
        """After a resume the stop's last failure is gone: a running attempt reads "Installing", not "failed ...
        trying again"."""
        lf = {"attempt": None, "reason": "InstallNotStarted", "message": "The installation could not be started: x.",
              "time": t(9)}
        stopped = dict(STOPPED, lastFailure=lf)
        resumed = dict(stopped)
        resumed.update(I.install_resumed({}))
        resumed = {k: v for k, v in resumed.items() if v is not None}   # the merge patch removes the nulls
        res = observe(po(resumed), cd(provisioned=False), prov(attempt=0))
        self.assertEqual(ready(res), cond("Ready", "False", "Installing", "Installing, attempt 1 of 3."))
        self.assertNotIn("lastFailure", res["install"])

    def test_a_young_pending_release_is_left_alone(self):
        """A Retry removes a release without a ClusterDeployment only when it failed, or has been pending longer than
        the installation allows; a younger pending one is an older job's release being made."""
        asked = po(STOPPED, generation=3, spec=SMALLER, lastHandledReconcileRequest="2026-11-18T11:59:00Z")
        self.assertTrue(I.release_young("pending-install", t(3), NOW, 10))
        self.assertFalse(I.release_young("pending-upgrade", "", NOW, 10), "an age that cannot be read counts as old")
        self.assertFalse(I.release_young("pending-upgrade", "not a time", NOW, 10))
        self.assertFalse(I.release_young("pending-install", t(11), NOW, 10))
        self.assertFalse(I.release_young("failed", t(1), NOW, 10))
        self.assertFalse(I.release_young("deployed", t(1), NOW, 10))
        self.assertFalse(I.may_remove_build(asked, None, True, release_exists=True, release_young=True)[0])
        self.assertTrue(I.may_remove_build(asked, None, True, release_exists=True, release_young=False)[0])
        self.assertTrue(I.release_young("pending-install", "2026-11-18T11:58:00.123456789Z", NOW, 10))


class NotStartedTexts(unittest.TestCase):
    def test_the_message_says_what_to_do_once_stopped(self):
        hub = cd(provisioned=False, stopped=True, launch_error=True)
        res = observe(po(RUNNING), hub, None)
        self.assertEqual(ready(res)["reason"], "InstallNotStarted")
        self.assertTrue(ready(res)["message"].endswith(I.NOT_STARTED_ADVICE.strip()), ready(res)["message"])
        # while Hive still tries: the platform keeps checking, no retry advice
        res = observe(po(RUNNING), cd(provisioned=False, launch_error=True), prov(attempt=0, stuck=True))
        self.assertIn("keeps checking", ready(res)["message"])
        self.assertNotIn("Retry", ready(res)["message"])
        # the summary is true in both states
        import shc_status_reasons as R
        self.assertNotIn("Retry", R.REASONS["InstallNotStarted"]["summary"])
        self.assertNotIn("looking at it", R.REASONS["InstallNotStarted"]["summary"])

    def test_a_failed_release_is_not_called_an_attempt(self):
        res = observe(po({"stage": "Installing", "run": 2, "startedAt": t(3)}), None, None,
                      settings=dict(SETTINGS, releaseFailed=True))
        self.assertTrue(stalled(res)["message"].startswith("The installation could not be started: the platform's "
                                                           "release of the build on the hub failed."))
        self.assertNotIn("Attempt", stalled(res)["message"])
        self.assertIn("Retry it once", stalled(res)["message"])


class DigestVersion(unittest.TestCase):
    """The version covers the listed fields with their API defaults: adding a default to a listed field (or a field to
    the list) fails this test until DIGEST_VERSION (and the operator's copy) is updated."""

    def test_the_version_is_derived_from_the_crd_base(self):
        import hashlib
        import json
        import yaml
        with open(os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-platformopenshift.yaml")) as fh:
            props = yaml.safe_load(fh)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"][
                "properties"]
        listed = {p: {f: props[p]["properties"][f].get("default") for f in fields}
                  for p, fields in I.DIGEST_FIELDS.items()}
        text = json.dumps(listed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertEqual(I.DIGEST_VERSION, hashlib.sha256(text.encode("ascii")).hexdigest()[:16])


class Digest(unittest.TestCase):
    def test_a_new_schema_default_or_an_unlisted_field_changes_nothing(self):
        """The digest covers the listed fields' values only: a field a later schema adds (with a default) to a block
        gives the same digest, so no stopped cluster reinstalls after a product update."""
        base = I.spec_digest(SPEC)
        more = {"type": "openstack", "openstack": dict(SPEC["openstack"], someNewField="default-value")}
        self.assertEqual(I.spec_digest(more), base)
        stopped = po(dict(STOPPED), generation=3, spec=more, lastHandledReconcileRequest="2026-11-18T09:00:00Z")
        self.assertFalse(I.asked_since_stop(stopped)[0])

    def test_a_digest_of_another_list_version_is_not_compared(self):
        """A product update that adds a field to the list: the recorded digest's version differs; a spec difference
        then does not ask (only a Retry), and the next run renews the record."""
        old = dict(STOPPED, stoppedSpecVersion="0" * 16, stoppedSpec="f" * 64)
        obj = po(old, generation=2, spec=SMALLER, lastHandledReconcileRequest="2026-11-18T09:00:00Z")
        asked, why = I.asked_since_stop(obj)
        self.assertFalse(asked)
        self.assertIn("another version", why)
        renewed = I.install_stopped_seen(obj)
        self.assertEqual((renewed["stoppedSpec"], renewed["stoppedSpecVersion"]),
                         (I.spec_digest(SMALLER), I.DIGEST_VERSION))
        # a Retry still asks
        obj["status"]["lastHandledReconcileRequest"] = "2026-11-18T11:59:00Z"
        self.assertTrue(I.asked_since_stop(obj)[0])
        # a new entry in the list gives another version
        import copy as _c
        fields = _c.deepcopy(I.DIGEST_FIELDS)
        fields["openstack"] = sorted(fields["openstack"] + ["someNewField"])
        self.assertNotEqual(I._sha(fields)[:16], I.DIGEST_VERSION)

    def test_the_installers_part_only(self):
        base = I.spec_digest(SPEC)
        self.assertEqual(base, I.spec_digest(dict(SPEC, toolRbac={"clusterAdminRbac": ["a"]}, deletionProtection=True,
                                                   clusterViewerRbac=["r"])))
        self.assertNotEqual(base, I.spec_digest(SMALLER))
        self.assertNotEqual(base, I.spec_digest(dict(SPEC, type="aws")))
        # key order does not matter
        self.assertEqual(base, I.spec_digest({"openstack": dict(reversed(list(SPEC["openstack"].items()))),
                                              "type": "openstack"}))
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        self.assertEqual(I.spec_digest(None), I.spec_digest({}))


if __name__ == "__main__":
    unittest.main()


class CapturedQuotaStop(unittest.TestCase):
    """A quota-stopped OpenStack installation as the hub showed it (tests/fixtures/install-causes/openstack-quota-stop:
    the ClusterDeployment and its provisions at each change, names replaced by example names). On that Hive the
    failure condition's message is the whole install log, attempts count from 0, and the stop comes minutes after the
    third failure, with the provision reference cleared."""

    QUOTA = ("the site reports too little quota: Cores (180 needed, limit 128), RAM (MB) (491520 needed, limit "
             "262144)")

    def steps(self):
        import json
        with open(os.path.join(ROOT, "tests", "fixtures", "install-causes", "openstack-quota-stop", "steps.json")) as fh:
            return json.load(fh)

    def replay(self, steps):
        install, conditions, out = dict(RUNNING, startedAt=t(5)), [], []
        install.pop("attempt")
        for i, step in enumerate(steps):
            cd_ = step["clusterDeployment"]
            provs = step["provisions"]
            ref = (((cd_ or {}).get("status") or {}).get("provisionRef") or {}).get("name")
            current = next((p for p in provs if p["metadata"]["name"] == ref), None)
            when = NOW + datetime.timedelta(minutes=i)
            res = I.observe(po(install, conditions=conditions), JOB, cd_, current, when, SETTINGS, provisions=provs)
            install, conditions = res["install"], res["conditions"]
            out.append(res)
        return out

    def test_the_sequence(self):
        got = self.replay(self.steps())
        reasons = [ready(r)["reason"] for r in got]
        attempts = [r["install"].get("attempt") for r in got]
        self.assertEqual(reasons[0], "Installing")
        self.assertEqual(reasons[-1], "QuotaExceeded")
        self.assertIn("InstallRetrying", reasons)
        self.assertEqual(attempts[-1], 3)
        self.assertEqual([r["outcome"] for r in got[:-1]], [None] * (len(got) - 1))
        self.assertEqual(got[-1]["outcome"], "stopped")
        self.assertEqual(stalled(got[-1])["message"],
                         "Attempt 3 of 3 failed: %s. The installer stopped after 3 attempts." % self.QUOTA)
        # the first failure is on the page while the installer tries again
        first = next(r for r in got if ready(r)["reason"] == "InstallRetrying")
        self.assertEqual(first["install"]["lastFailure"]["message"], "Attempt 1 of 3 failed: %s." % self.QUOTA)
        self.assertTrue(ready(first)["message"].endswith("The installer is trying again."))

    def test_all_attempts_failed_not_yet_stopped(self):
        steps = self.steps()
        between = steps[-2]   # the third attempt failed, Hive has not stopped yet
        self.assertEqual([p["spec"]["stage"] for p in between["provisions"]], ["failed"] * 3)
        got = self.replay(steps)[-2]
        self.assertIsNone(got["outcome"])
        self.assertEqual(ready(got)["reason"], "InstallRetrying")
        self.assertEqual(got["install"]["attempt"], 3)
        self.assertEqual(got["install"]["lastFailure"]["attempt"], 3)
        self.assertIn("No attempts are left", ready(got)["message"])
        self.assertIsNone(stalled(got))

    def test_a_job_that_first_looks_after_the_stop(self):
        last = self.steps()[-1]
        res = I.observe(po(dict(RUNNING)), JOB, last["clusterDeployment"], None, NOW, SETTINGS,
                        provisions=last["provisions"])
        self.assertEqual(res["outcome"], "stopped")
        self.assertEqual(stalled(res)["reason"], "QuotaExceeded")
        self.assertIn("Cores (180 needed, limit 128)", stalled(res)["message"])

    def test_no_log_text_in_any_written_field(self):
        for res in self.replay(self.steps()):
            text = repr(res["install"]) + repr(res["conditions"])
            for word in ("level=", "msg=", "quay.io", "clouds.yaml", "sha256", "MissingQuota"):
                self.assertNotIn(word, text)

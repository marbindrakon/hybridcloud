"""The event activation check (charts/charts/shc-cluster-base/files/eda_activations.py).

- its decisions as tables (status, restart policy, enabled, the record: grace, hourly cap, given up, disabled by the
  check);
- run_check against a fake event-automation API and a fake Kubernetes API: each finding of the review (a failure in
  the middle of a repair, slow starters, other restart policies, an empty list, a daily count, a refused enable),
  and a test for each mutation that once survived;
- an enumeration of the repair logic: every combination of status x enabled x policy x state record x where a read,
  write or the run itself fails, against the invariants (no more enables than the cap in any hour; no disable
  without a durable record first; never left disabled by the check without that record; a person's disable is never
  undone; exit non-zero exactly when a person is needed or the run was incomplete; no credential in any output);
- main() end to end over HTTP (through an injected plain-http transport: the script itself takes https only).

Standard library only, so it also runs in the job image (python3 -m unittest tests/unit/test_eda_activations.py)."""
import contextlib
import copy
import http.server
import io
import itertools
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files"))

import eda_activations as E  # noqa: E402

NOW = 1_800_000_000
TOKEN = "s3cret-token-value"
E.SECRETS.add(TOKEN)
CFG = dict(E.DEFAULTS, url="https://eda.example.com", automationNamespace="automation", stateNamespace="ops",
           pollSeconds=0, stopTimeoutSeconds=30, startTimeoutSeconds=60)


def act(aid=1, name="project-create", status="running", enabled=True, message="", policy="always"):
    return {"id": aid, "name": name, "status": status, "is_enabled": enabled, "restart_policy": policy,
            "restart_count": 3, "status_message": message}


def inst(iid, status="running", message=""):
    return {"id": iid, "status": status, "status_message": message}


class Clock:
    """A clock that moves on 5 s each time it is read, so every wait in the check ends."""

    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 5
        return self.t


class Decide(unittest.TestCase):
    """decide(activation, latest instance, record, now, cfg) -> verdict."""

    OLD = NOW - 7200
    TABLE = [
        (act(), inst(10), None, E.OK),
        (act(), None, None, E.OK),
        (act(enabled=False, status="stopped"), None, None, E.DISABLED),
        (act(status="deleting"), None, None, E.DELETING),
        (act(status="workers offline"), None, None, E.WORKERS_OFFLINE),
        (act(status="paused"), None, None, E.UNKNOWN),
        # another restart policy: not watched, whatever its state
        (act(status="completed", policy="never"), inst(3, "completed"), {"notRunningSince": OLD}, E.NOT_WATCHED),
        (act(status="failed", policy="on-failure"), inst(3, "failed"), {"notRunningSince": OLD}, E.NOT_WATCHED),
        # first seen not running: it waits for the grace time (the platform's own restart comes first)
        (act(status="failed", message="Activation has reached the maximum number of restarts"), inst(10, "failed"),
         None, E.WAITING),
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="completed"), inst(10, "completed"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="unresponsive"), inst(10, "unresponsive"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="stopped"), None, {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="error"), None, {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="running"), inst(920, "failed"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="running"), inst(920, "failed"), None, E.WAITING),
        (act(status="starting"), None, {"notRunningSince": NOW - 300}, E.WAITING),
        (act(status="pending"), None, {"notRunningSince": NOW - 700}, E.REPAIR),
        (act(status="failed", message="Activation 9 failed to start. Reason: Image quay.example/de:1 pull failed. "
                                      "The image url or the credentials may be incorrect."),
         inst(12, "failed"), {"notRunningSince": NOW - 300}, E.CANNOT_RUN),
        (act(status="failed", message="Activation has reached the maximum number of restarts"),
         inst(12, "failed", "Image example/de:1 pull failed. The image url or the credentials may be incorrect."),
         {"notRunningSince": NOW - 300}, E.CANNOT_RUN),
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 300, "repairs": [NOW - 3000, NOW - 2000]},
         E.REPAIR),
        (act(status="failed"), inst(10, "failed"),
         {"notRunningSince": NOW - 300, "repairs": [NOW - 3000, NOW - 2000, NOW - 1000]}, E.LIMIT),
        (act(status="failed"), inst(10, "failed"),
         {"notRunningSince": NOW - 300, "repairs": [NOW - 7000, NOW - 6000, NOW - 5000]}, E.REPAIR),
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 9000, "gaveUp": True, "repairs": []},
         E.LIMIT),
        (act(status="stopped", enabled=False), None, {"disabledByCheck": True, "repairs": [NOW - 100]}, E.ENABLE),
        (act(status="stopped", enabled=False), None,
         {"disabledByCheck": True, "repairs": [NOW - 300, NOW - 200, NOW - 100]}, E.LEFT_DISABLED),
        # a refused enable (400) set gaveUp: left disabled and reported, not enabled again
        (act(status="error", enabled=False), None, {"disabledByCheck": True, "gaveUp": True, "repairs": [NOW - 9000]},
         E.LEFT_DISABLED),
    ]

    def test_table(self):
        for i, (a, ins, rec, want) in enumerate(self.TABLE):
            with self.subTest(i=i, status=a["status"]):
                got, why, _ = E.decide(a, ins, rec, NOW, CFG)
                self.assertEqual(got, want, why)

    def test_first_sight_is_recorded(self):
        _, _, rec = E.decide(act(status="failed"), inst(1, "failed"), None, NOW, CFG)
        self.assertEqual(rec["notRunningSince"], NOW)

    def test_given_up_clears_only_when_running_with_no_recent_repair(self):
        _, _, rec = E.decide(act(), inst(1), {"gaveUp": True, "repairs": [NOW - 100]}, NOW, CFG)
        self.assertTrue(rec["gaveUp"])
        _, _, rec = E.decide(act(), inst(1), {"gaveUp": True, "repairs": [NOW - 4000]}, NOW, CFG)
        self.assertFalse(rec["gaveUp"])

    def test_repairs_kept_for_a_day(self):
        _, _, rec = E.decide(act(), inst(1), {"repairs": [NOW - 90000, NOW - 80000, NOW - 100]}, NOW, CFG)
        self.assertEqual(rec["repairs"], [NOW - 80000, NOW - 100])

    def test_a_person_disabling_clears_the_record(self):
        _, _, rec = E.decide(act(enabled=False, status="stopped"), None, {"gaveUp": True, "notRunningSince": NOW},
                             NOW, CFG)
        self.assertFalse(rec["gaveUp"])
        self.assertIsNone(rec["notRunningSince"])

    def test_cap_is_a_value(self):
        cfg = dict(CFG, maxRepairsPerHour=1)
        got, _, _ = E.decide(act(status="failed"), None, {"notRunningSince": NOW - 300, "repairs": [NOW - 10]}, NOW, cfg)
        self.assertEqual(got, E.LIMIT)

    def test_mark_before_acting_restarts_the_grace(self):
        rec = E.mark_before_acting({"repairs": [NOW - 10], "notRunningSince": NOW - 9000}, E.REPAIR, NOW)
        self.assertEqual((rec["repairs"], rec["notRunningSince"], rec["disabledByCheck"]),
                         ([NOW - 10, NOW], NOW, True))


class Jobs(unittest.TestCase):
    def jobs(self, *specs):
        return E.parse_jobs([{"metadata": {"name": n}, "status": {"active": a}} for n, a in specs])

    def test_parse(self):
        got = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1), ("other-job", 1),
                        ("activation-job-5-6", 0))
        self.assertEqual(sorted(got), [5, 71])

    def test_two_running_jobs(self):
        j = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1))[71]
        self.assertIn("2 activation Jobs", E.job_findings(act(71), j))
        self.assertEqual(E.stale_jobs(act(71), inst(1035), j), ["activation-job-71-968"])

    def test_one_running_one_finished(self):
        j = self.jobs(("activation-job-71-968", 0), ("activation-job-71-1035", 1))[71]
        self.assertIsNone(E.job_findings(act(71), j))

    def test_stale_only_when_certain(self):
        j = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1))[71]
        self.assertEqual(E.stale_jobs(act(71), inst(1035, "failed"), j), [])
        self.assertEqual(E.stale_jobs(act(71, status="failed"), inst(1035), j), [])
        self.assertEqual(E.stale_jobs(act(71), inst(1100), j), [])
        self.assertEqual(E.stale_jobs(act(71), None, j), [])


class Killed(BaseException):
    """The pod ends mid-run (deadline, eviction): nothing after it runs."""


def boom(what, status=None):
    # an API that echoes the credential in its error: the check must never show it
    return E.ApiError("%s: answered 502 (upstream said: Authorization: Bearer %s)" % (what, TOKEN), status=status)


class FakeEda:
    """The event-automation API as the check uses it. A disable takes `stop_polls` list reads to reach stopped; an
    enable takes `start_polls` to reach running, unless the activation is in `never_runs` (it fails) or `invalid`
    (400). An enable while the status blocks it answers 409; an enable of an enabled activation changes nothing,
    as the platform does. `fail` and `kill` inject a failure at one point: list (the first read), reread (the second),
    wait (every read after), detail, disable, enable."""

    def __init__(self, items, instances=None, never_runs=(), invalid=(), stop_polls=1, start_polls=1, log=None,
                 fail=None, kill=None):
        self.items = {a["id"]: dict(a) for a in items}
        self.instances = dict(instances or {})
        self.never_runs, self.invalid = set(never_runs), set(invalid)
        self.stop_polls, self.start_polls = stop_polls, start_polls
        self.pending = {}
        self.log = log if log is not None else []
        self.fail, self.kill = fail, kill
        self.list_reads = 0

    @property
    def calls(self):
        return [e for e in self.log if e[0] in ("disable", "enable")]

    def _tick(self):
        # the platform finishes stopping a disabled activation
        for aid, a in self.items.items():
            if not a["is_enabled"] and a["status"] in E.ENABLE_BLOCKED - {"deleting"} and aid not in self.pending:
                a["status"] = "stopped"
        for aid, (left, final) in list(self.pending.items()):
            if left <= 1:
                self.items[aid]["status"] = final
                if final == "running":
                    nid = max([i["id"] for i in self.instances.get(aid, [])] + [0]) + 1
                    self.instances.setdefault(aid, []).append(inst(nid))
                del self.pending[aid]
            else:
                self.pending[aid] = (left - 1, final)

    def activations(self):
        self.list_reads += 1
        n = self.list_reads
        point = "list" if n == 1 else "reread" if n == 2 else "wait"
        if self.kill == point:
            raise Killed()
        if self.fail == point:
            raise boom("GET /api/eda/v1/activations/")
        self._tick()
        return [dict(a) for a in self.items.values()]

    def detail(self, aid):
        if self.fail == "detail":
            raise boom("GET /api/eda/v1/activations/%d/" % aid)
        return dict(self.items[aid], instances=list(self.instances.get(aid, [])))

    def disable(self, aid):
        if self.fail == "disable":
            raise boom("POST disable", status=502)
        self.log.append(("disable", aid))
        a = self.items[aid]
        a["is_enabled"] = False
        if a["status"] in ("running", "starting"):
            a["status"] = "stopping"
            self.pending[aid] = (self.stop_polls, "stopped")
        else:
            a["status"] = "stopped"
        if self.kill == "disable":
            raise Killed()

    def enable(self, aid):
        if self.fail == "enable":
            raise boom("POST enable", status=409)
        a = self.items[aid]
        if a["is_enabled"]:
            self.log.append(("enable", aid))
            return
        if a["status"] in E.ENABLE_BLOCKED:
            raise E.ApiError("POST enable: answered 409 (Activation not enabled due to current activation status)",
                             status=409)
        if aid in self.invalid:
            raise E.ApiError("POST enable: answered 400 (the decision environment does not exist)", status=400)
        self.log.append(("enable", aid))
        a["is_enabled"] = True
        a["status"] = "pending"
        self.pending[aid] = (self.start_polls, "failed" if aid in self.never_runs else "running")


class FakeKube:
    """The Kubernetes API: the state ConfigMap (committed = what a later run would read) and the Jobs list.
    fail_writes: which writes of this run fail (1 = the first)."""

    def __init__(self, jobs=(), state=None, unreadable=False, log=None, fail_writes=()):
        self.job_items = [{"metadata": {"name": n}, "status": {"active": a}} for n, a in jobs]
        self.committed = copy.deepcopy(state)
        self.unreadable = unreadable
        self.log = log if log is not None else []
        self.fail_writes = set(fail_writes)
        self.writes = 0
        self.versions = []
        self.deleted = []

    def jobs(self, ns):
        return self.job_items

    def delete_job(self, ns, name):
        self.deleted.append(name)

    def read_state(self, ns, name):
        if self.unreadable:
            raise boom("GET configmaps", status=403)
        return (copy.deepcopy(self.committed), "v0") if self.committed is not None else ({}, None)

    def write_state(self, ns, name, state, version):
        self.writes += 1
        self.versions.append(version)
        if self.writes in self.fail_writes:
            raise boom("PUT configmaps/%s" % name, status=500)
        self.committed = copy.deepcopy(state)
        self.log.append(("write", copy.deepcopy(state)))
        return "v%d" % self.writes


def run(eda, kube, cfg=None, dry_run=False, now=NOW):
    lines = []
    eda.list_reads = 0
    kube.writes = 0
    counts, report = E.run_check(eda, kube, dict(CFG, **(cfg or {})), now, dry_run, clock=Clock(),
                                 sleep=lambda s: None, out=lines.append)
    return counts, report, lines


def later(eda, kube, minutes, cfg=None):
    return run(eda, kube, cfg, now=NOW + 60 * minutes)


class Check(unittest.TestCase):
    def test_all_running(self):
        eda = FakeEda([act(1), act(2, "b")], {1: [inst(5)], 2: [inst(6)]})
        kube = FakeKube([("activation-job-1-5", 1), ("activation-job-2-6", 1)])
        counts, report, _ = run(eda, kube)
        self.assertEqual(report, [])
        self.assertEqual(counts["running"], 2)
        self.assertEqual(eda.calls, [])
        self.assertEqual(sorted(kube.committed), ["1", "2"])

    def test_waits_then_repairs_in_one_batch(self):
        items = [act(1, "a", "failed", message="Activation has reached the maximum number of restarts"),
                 act(2, "b", "completed"), act(3, "c")]
        eda = FakeEda(items, {1: [inst(5, "failed")], 2: [inst(6, "completed")], 3: [inst(7)]})
        kube = FakeKube()
        counts, report, _ = run(eda, kube)
        self.assertEqual((counts["waiting"], eda.calls, report), (2, [], []))
        counts, report, lines = later(eda, kube, 5)
        self.assertEqual(report, [])
        self.assertEqual(eda.calls, [("disable", 1), ("disable", 2), ("enable", 1), ("enable", 2)])
        self.assertEqual(counts["repaired"], 2)
        self.assertTrue(any(l == "repaired activation b (id 2): running again" for l in lines))
        st = kube.committed
        self.assertEqual(st["1"]["repairs"], [NOW + 300])
        self.assertFalse(st["1"]["disabledByCheck"])

    def test_record_is_durable_before_the_disable(self):
        # kills the mutation "disabledByCheck not set at the disable"
        log = []
        eda = FakeEda([act(4, "x", "failed")], {4: [inst(2, "failed")]}, log=log)
        kube = FakeKube(state={"4": {"notRunningSince": NOW - 900}}, log=log)
        run(eda, kube)
        first = log.index(("disable", 4))
        writes = [e for e in log[:first] if e[0] == "write"]
        self.assertTrue(writes, log)
        self.assertTrue(writes[-1][1]["4"]["disabledByCheck"])
        self.assertIn(NOW, writes[-1][1]["4"]["repairs"])

    def test_a_list_failure_during_the_wait_still_enables_and_reports(self):
        # review W1: a 502 while waiting no longer leaves the activations disabled and the run silent
        eda = FakeEda([act(4, "x", "failed")], {4: [inst(2, "failed")]}, fail="wait")
        kube = FakeKube(state={"4": {"notRunningSince": NOW - 900}})
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [("disable", 4), ("enable", 4)])
        self.assertTrue(eda.items[4]["is_enabled"])
        self.assertTrue(report)
        self.assertNotIn(TOKEN, "\n".join(report))

    def test_killed_after_the_disable_is_enabled_by_the_next_run(self):
        eda = FakeEda([act(4, "x", "failed")], {4: [inst(2, "failed")]}, kill="disable")
        kube = FakeKube(state={"4": {"notRunningSince": NOW - 900}})
        with self.assertRaises(Killed):
            run(eda, kube)
        self.assertFalse(eda.items[4]["is_enabled"])
        self.assertTrue(kube.committed["4"]["disabledByCheck"])
        eda.kill = None
        later(eda, kube, 5)
        self.assertTrue(eda.items[4]["is_enabled"])
        self.assertEqual(eda.calls[-1], ("enable", 4))

    def test_state_write_failing_before_means_nothing_is_touched(self):
        eda = FakeEda([act(4, "x", "failed")], {4: [inst(2, "failed")]})
        kube = FakeKube(state={"4": {"notRunningSince": NOW - 900}}, fail_writes={1})
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [])
        self.assertIn("nothing was repaired", report[0])
        self.assertEqual(kube.writes, 1)

    def test_cap_held_when_the_final_write_fails(self):
        # review W1: with cap 3, at most 3 enables in the hour even if every run's final write fails
        eda = FakeEda([act(79, "broken", "failed")], {79: [inst(1, "failed")]}, never_runs={79})
        kube = FakeKube(state={"79": {"notRunningSince": NOW - 900}}, fail_writes={2})
        for i in range(8):
            later(eda, kube, 5 * i)
        self.assertEqual(len([c for c in eda.calls if c[0] == "enable"]), 3)

    def test_slow_starter_is_not_pushed_back_on_the_next_run(self):
        # review W2: after a repair the grace starts again from the repair
        eda = FakeEda([act(5, "slow", "failed")], {5: [inst(2, "failed")]}, start_polls=10_000)
        kube = FakeKube(state={"5": {"notRunningSince": NOW - 900}})
        run(eda, kube)
        self.assertEqual(kube.committed["5"]["notRunningSince"], NOW)
        later(eda, kube, 5)
        self.assertEqual(len(eda.calls), 2)   # still pending: waits, no second disable

    def test_reread_skips_one_that_came_back(self):
        eda = FakeEda([act(6, "x", "failed")], {6: [inst(2, "failed")]})
        kube = FakeKube(state={"6": {"notRunningSince": NOW - 900}})
        orig = eda.activations

        def back():
            items = orig()
            if eda.list_reads == 2:
                for a in items:
                    a["status"] = "running"
            return items
        eda.activations = back
        counts, report, lines = run(eda, kube)
        self.assertEqual(eda.calls, [])
        self.assertTrue(any(l.startswith("skip activation x (id 6)") for l in lines))

    def test_other_restart_policies_are_not_watched(self):
        # review W3
        eda = FakeEda([act(7, "one-shot", "completed", policy="never"), act(8, "b")], {7: [inst(1, "completed")]})
        counts, report, lines = run(eda, FakeKube(state={"7": {"notRunningSince": NOW - 9000}}))
        self.assertEqual((eda.calls, report), ([], []))
        self.assertEqual([l for l in lines if l.startswith("not watched")],
                         ["not watched (restart policy other than always): one-shot"])

    def test_an_empty_list_is_reported_and_prunes_nothing(self):
        # review W4
        kube = FakeKube(state={"4": {"disabledByCheck": True, "repairs": [NOW - 10]}})
        counts, report, _ = run(FakeEda([]), kube)
        self.assertIn("the activations list is empty", report[0])
        self.assertEqual(kube.writes, 0)
        counts, report, _ = run(FakeEda([]), kube, {"allowNoActivations": True})
        self.assertEqual(report, [])

    def test_a_disabled_by_check_record_survives_an_unreadable_detail(self):
        kube = FakeKube(state={"4": {"disabledByCheck": True, "repairs": [NOW - 10]},
                               "5": {"disabledByCheck": True, "repairs": [NOW - 10]}})
        eda = FakeEda([act(4, "x", "stopped", enabled=False), act(5, "y", "failed")], fail="detail")
        counts, report, _ = run(eda, kube)
        # 4 is disabled (no detail needed): enabled again; 5's detail failed: its record is kept as it was
        self.assertTrue(eda.items[4]["is_enabled"])
        self.assertFalse(kube.committed["4"]["disabledByCheck"])
        self.assertTrue(kube.committed["5"]["disabledByCheck"])
        self.assertTrue(report)

    def test_repaired_too_often_in_a_day(self):
        # review W8: each repair works, but it dies every 25 minutes
        eda = FakeEda([act(9, "flaky", "failed")], {9: [inst(1, "failed")]})
        kube = FakeKube(state={"9": {"notRunningSince": NOW - 900}})
        reports = []
        for i in range(8):
            eda.items[9]["status"] = "failed"
            eda.items[9]["is_enabled"] = True
            reports.append(later(eda, kube, 25 * i)[1])
        self.assertEqual(len([c for c in eda.calls if c[0] == "enable"]), 8)
        self.assertEqual(reports[:6], [[]] * 6)
        self.assertIn("RepairedTooOften: 7 repairs in the last day", reports[6][0])

    def test_refused_enable_gives_up(self):
        # review W10: after a 400 it stays disabled, reported, and is not enabled 3 times an hour for ever
        eda = FakeEda([act(8, "x", "failed")], {8: [inst(3, "failed")]}, invalid={8})
        kube = FakeKube(state={"8": {"notRunningSince": NOW - 900}})
        counts, report, _ = run(eda, kube)
        self.assertIn("CannotRun: the platform refuses to enable it", report[0])
        self.assertTrue(kube.committed["8"]["gaveUp"])
        self.assertTrue(kube.committed["8"]["disabledByCheck"])
        for i in range(1, 6):
            counts, report, _ = later(eda, kube, 5 * i)
            self.assertIn("LeftDisabled", report[0])
        self.assertEqual([c for c in eda.calls if c[0] == "enable"], [])

    def test_not_running_after_repair_then_the_cap(self):
        eda = FakeEda([act(79, "broken", "failed")], {79: [inst(1, "failed")]}, never_runs={79})
        kube = FakeKube(state={"79": {"notRunningSince": NOW - 900}})
        reports = []
        for i in range(8):
            reports.append(later(eda, kube, 5 * i)[1])
        self.assertEqual(len([c for c in eda.calls if c[0] == "enable"]), 3)
        self.assertTrue(all(r for r in reports))
        self.assertIn("RepairLimitReached", reports[-1][0])
        self.assertTrue(kube.committed["79"]["gaveUp"])

    def test_cannot_run_is_reported_not_repaired(self):
        msg = "Activation 79 failed to start. Reason: Image example/de:9 pull failed. The image url or the " \
              "credentials may be incorrect."
        eda = FakeEda([act(79, "uses-missing-image", "failed", message=msg)], {79: [inst(3, "failed")]})
        counts, report, _ = run(eda, FakeKube(state={"79": {"notRunningSince": NOW - 900}}))
        self.assertEqual(eda.calls, [])
        self.assertIn("CannotRun", report[0])

    def test_workers_offline_and_unknown_status_are_reported(self):
        eda = FakeEda([act(1, "a", "workers offline"), act(2, "b", "paused")])
        counts, report, _ = run(eda, FakeKube())
        self.assertEqual(eda.calls, [])
        self.assertEqual(sorted(r.split(":")[1].strip() for r in report), ["UnknownStatus", "WorkersOffline"])

    def test_two_jobs_reported_and_removed_only_when_switched_on(self):
        items = [act(71, "project-delete")]
        jobs = [("activation-job-71-968", 1), ("activation-job-71-1035", 1)]
        kube = FakeKube(jobs)
        counts, report, _ = run(FakeEda(items, {71: [inst(968, "failed"), inst(1035)]}), kube)
        self.assertIn("TwoJobs", report[0])
        self.assertIn("(older: activation-job-71-968)", report[0])
        self.assertEqual(kube.deleted, [])
        kube = FakeKube(jobs)
        counts, report, _ = run(FakeEda(items, {71: [inst(968, "failed"), inst(1035)]}), kube,
                                {"removeStaleJobs": True})
        self.assertEqual((kube.deleted, report), (["activation-job-71-968"], []))

    def test_api_does_not_answer(self):
        with self.assertRaises(E.ApiError) as cm:
            run(FakeEda([act()], fail="list"), FakeKube())
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_unreadable_state_repairs_nothing_even_without_grace(self):
        # kills the mutation "repair although the count is unreadable" (no grace hides the guard here)
        eda = FakeEda([act(1, "a", "failed")], {1: [inst(1, "failed")]})
        kube = FakeKube(unreadable=True)
        counts, report, _ = run(eda, kube, {"notRunningMinutes": 0})
        self.assertEqual(eda.calls, [])
        self.assertIn("cannot be read, so nothing is repaired", report[0])
        self.assertTrue(any("not done: the repair count is unknown" in r for r in report))
        self.assertEqual(kube.writes, 0)

    def test_dry_run_changes_nothing(self):
        eda = FakeEda([act(1, "a", "failed")], {1: [inst(1, "failed")]})
        kube = FakeKube(state={"1": {"notRunningSince": NOW - 900}})
        run(eda, kube, dry_run=True)
        self.assertEqual((eda.calls, kube.writes), ([], 0))

    def test_deleted_activations_leave_the_state(self):
        kube = FakeKube(state={"1": {"repairs": [NOW]}, "99": {"repairs": [NOW]}})
        run(FakeEda([act(1)], {1: [inst(1)]}), kube)
        self.assertEqual(sorted(kube.committed), ["1"])

    def test_writes_carry_the_version_read(self):
        kube = FakeKube(state={"4": {"notRunningSince": NOW - 900}})
        run(FakeEda([act(4, "x", "failed")], {4: [inst(2, "failed")]}), kube)
        self.assertEqual(kube.versions, ["v0", "v1"])


# ---- the enumeration --------------------------------------------------------------------------------------------

STATUSES = ["running", "pending", "starting", "stopping", "stopped", "completed", "failed", "unresponsive", "error",
            "deleted"]
RECORDS = {
    "none": None,
    "below": {"notRunningSince": NOW - 7200, "repairs": [NOW - 1000]},
    "atCap": {"notRunningSince": NOW - 7200, "repairs": [NOW - 3000, NOW - 2000, NOW - 1000]},
    "gaveUp": {"notRunningSince": NOW - 7200, "repairs": [], "gaveUp": True},
    "disabledByCheck": {"notRunningSince": NOW - 7200, "repairs": [NOW - 1000], "disabledByCheck": True},
}
FAILURES = ["none", "list", "detail", "reread", "write-before", "disable", "wait", "enable", "write-after",
            "kill-disable", "kill-wait"]


def expected(status, enabled, policy, record):
    """(action, needs a person) for activation 1, from the rules of the check as the docs state them."""
    if status == "deleted":
        return "none", False
    if not enabled:
        return ("enable", False) if record == "disabledByCheck" else ("none", False)
    if policy != "always" or status == "running" or record == "none":
        return "none", False
    if record in ("gaveUp", "atCap"):
        return "none", True
    return "repair", False


class Enumeration(unittest.TestCase):
    def test_every_combination(self):
        cap = CFG["maxRepairsPerHour"]
        failures = []
        cases = 0
        for status, enabled, policy, record, fail in itertools.product(
                STATUSES, (True, False), ("always", "on-failure"), RECORDS, FAILURES):
            cases += 1
            action, person = expected(status, enabled, policy, record)
            log = []
            items = [act(2, "steady")]
            if status != "deleted":
                items.insert(0, act(1, "subject", status, enabled, policy=policy))
            instances = {1: [inst(10, "running" if status == "running" else status)], 2: [inst(20)]}
            state = {"2": {"repairs": []}}
            if RECORDS[record] is not None:
                state["1"] = copy.deepcopy(RECORDS[record])
            eda_fail = fail if fail in ("list", "detail", "reread", "disable", "wait", "enable") else None
            kill = fail[5:] if fail.startswith("kill-") else None
            if fail == "write-before":
                fail_writes = {1} if action != "none" else set()
            elif fail == "write-after":
                fail_writes = {2} if action != "none" else {1}
            else:
                fail_writes = set()
            eda = FakeEda(items, instances, log=log, fail=eda_fail, kill=kill)
            kube = FakeKube(state=state, log=log, fail_writes=fail_writes)
            lines, killed, raised = [], False, False
            report = []
            try:
                _, report = E.run_check(eda, kube, CFG, NOW, False, clock=Clock(), sleep=lambda s: None,
                                        out=lines.append)
            except Killed:
                killed = True
            except E.ApiError as e:
                raised = True
                report = [str(e)]
            nonzero = killed or raised or bool(report)
            name = (status, enabled, policy, record, fail)

            def bad(why):
                failures.append("%s: %s (log %s, report %s)" % (name, why, log, report))

            enables = [e for e in log if e == ("enable", 1)]
            prior = len(E.in_window((RECORDS[record] or {}).get("repairs"), NOW, E.HOUR))
            if len(enables) + prior > cap:
                bad("more enables than the cap in the hour")
            last_write = state
            for e in log:
                if e[0] == "write":
                    last_write = e[1]
                elif e[0] == "disable":
                    rec = last_write.get(str(e[1])) or {}
                    if not (rec.get("disabledByCheck") and NOW in (rec.get("repairs") or [])):
                        bad("a disable without a durable record first")
            subject = eda.items.get(1)
            committed = (kube.committed or {}).get("1") or {}
            if subject is not None and not subject["is_enabled"]:
                if ("disable", 1) in log and not committed.get("disabledByCheck"):
                    bad("left disabled by the check without the record")
                if record == "disabledByCheck" and not committed.get("disabledByCheck"):
                    bad("the record of an activation still disabled was lost")
            if not enabled and record != "disabledByCheck" and enables:
                bad("a person's disable was undone")
            if action != "none" and fail == "none" and not (subject or {}).get("is_enabled"):
                bad("the repair did not finish")
            # exit status: non-zero exactly when a person is needed or the run was incomplete
            if fail in ("list", "detail", "write-after"):
                want = True
            elif fail in ("reread", "write-before", "wait", "enable", "kill-wait"):
                want = person or action != "none"
            elif fail in ("disable", "kill-disable"):
                want = person or action == "repair"
            else:
                want = person
            if nonzero != want:
                bad("exit %s, expected %s" % ("non-zero" if nonzero else "zero", "non-zero" if want else "zero"))
            if TOKEN in "\n".join(lines + report):
                bad("the credential is in the output")
        self.assertEqual(cases, 10 * 2 * 2 * 5 * 11)
        self.assertEqual(failures[:10], [], "%d of %d cases break an invariant" % (len(failures), cases))
        self.maxDiff = None


# ---- main() over HTTP ---------------------------------------------------------------------------------------------

class Server:
    """A local HTTP server answering with handler(method, path, body) -> (code, json or None[, headers])."""

    def __init__(self, handler):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                outer.seen.append((self.command, self.path, self.headers.get("Authorization")))
                res = handler(self.command, self.path, body)
                code, doc = res[0], res[1]
                extra = res[2] if len(res) > 2 else {}
                raw = b"" if doc is None else json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.seen = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eda-activations-")
        cred = os.path.join(self.tmp, "cred")
        os.mkdir(cred)
        with open(os.path.join(cred, "token"), "w") as f:
            f.write(TOKEN)
        self.cred = cred
        self.fake = FakeEda([act(1, "a", "failed"), act(2, "b")], {1: [inst(4, "failed")], 2: [inst(5)]})
        self.state = {"1": {"notRunningSince": 1}}
        self.paged = False
        self.eda_answer = None
        self.put_versions = []

        def eda(method, path, body):
            if self.eda_answer:
                return self.eda_answer
            if method == "GET" and path.startswith("/api/eda/v1/activations/?"):
                items = self.fake.activations()
                if self.paged:
                    if "page=2" in path:
                        return 200, {"count": 2, "next": None, "results": items[1:]}
                    return 200, {"count": 2, "next": "/api/eda/v1/activations/?page_size=200&page=2",
                                 "results": items[:1]}
                return 200, {"count": 2, "next": None, "results": items}
            parts = path.strip("/").split("/")
            aid = int(parts[4])
            if method == "GET":
                return 200, self.fake.detail(aid)
            try:
                getattr(self.fake, parts[5])(aid)
            except E.ApiError as e:
                return e.status, {"errors": str(e)}
            return 204, None

        def kube(method, path, body):
            if path.startswith("/apis/batch/v1/namespaces/automation/jobs"):
                return 200, {"items": [{"metadata": {"name": "activation-job-2-5"}, "status": {"active": 1}}]}
            if path == "/api/v1/namespaces/ops/configmaps/eda-activations-state" and method == "GET":
                return 200, {"metadata": {"resourceVersion": "3"}, "data": {"state.json": json.dumps(self.state)}}
            if method == "PUT":
                self.state = json.loads(body["data"]["state.json"])
                self.put_versions.append(body["metadata"]["resourceVersion"])
                meta = dict(body["metadata"], resourceVersion=str(4 + len(self.put_versions)))
                return 200, dict(body, metadata=meta)
            return 404, {"reason": "NotFound"}

        self.eda, self.kube = Server(eda), Server(kube)
        self.addCleanup(self.eda.close)
        self.addCleanup(self.kube.close)

    def config(self, **kw):
        path = os.path.join(self.tmp, "config.json")
        cfg = {"url": self.eda.url, "automationNamespace": "automation", "stateNamespace": "ops", "pollSeconds": 0,
               "credentialDir": self.cred, "kubeURL": self.kube.url,
               "kubeTokenFile": os.path.join(self.tmp, "none"), "kubeCAFile": os.path.join(self.tmp, "none")}
        cfg.update(kw)
        with open(path, "w") as f:
            json.dump(cfg, f)
        return path

    def capture(self, argv, transport="default"):
        buf = io.StringIO()
        t = E.make_transport() if transport == "default" else transport
        with contextlib.redirect_stdout(buf):
            rc = E.main(argv, transport=t)
        return rc, buf.getvalue()

    def test_repairs_over_http(self):
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 0, out)
        self.assertIn("repaired activation a (id 1): running again", out)
        self.assertEqual(len(self.state["1"]["repairs"]), 1)
        self.assertEqual(self.put_versions, ["3", "5"])
        self.assertEqual({a for _, _, a in self.eda.seen}, {"Bearer " + TOKEN})
        self.assertNotIn(TOKEN, out)

    def test_exit_1_when_something_is_reported(self):
        # kills the mutation "check mode exits 0 despite a report"
        self.fake.items[1]["status_message"] = "Image example/x:1 pull failed."
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 1)
        self.assertIn("PROBLEM activation a (id 1): CannotRun", out)

    def test_pagination(self):
        # kills the mutation "pagination ignored": the activation on page 2 is judged too
        self.paged = True
        self.fake.items = {1: act(1, "a"), 2: act(2, "b", "failed")}
        self.fake.instances = {1: [inst(4)], 2: [inst(5, "failed")]}
        self.state = {"2": {"notRunningSince": 1}}
        rc, out = self.capture(["--config", self.config()])
        self.assertIn("repaired activation b (id 2)", out)

    def test_next_link_to_another_origin_is_refused(self):
        self.eda_answer = (200, {"count": 1, "next": "https://elsewhere.example/steal", "results": [act(1)]})
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 1)
        self.assertIn("a link to another origin was refused: elsewhere.example", out)

    def test_redirect_is_not_followed(self):
        self.eda_answer = (302, None, {"Location": "https://elsewhere.example/x"})
        rc, out = self.capture(["--config", self.config(), "--mode", "api"])
        self.assertEqual(rc, 1)
        self.assertIn("answered 302", out)

    def test_http_is_refused_without_a_test_transport(self):
        with self.assertRaises(SystemExit) as cm:
            self.capture(["--config", self.config(), "--mode", "api"], transport=None)
        self.assertIn("must use https", str(cm.exception))

    def test_error_text_never_shows_the_credential(self):
        # kills the mutation "credential in the error-status text": an API that echoes the header
        self.eda_answer = (500, {"detail": "bad header Authorization: Bearer %s" % TOKEN})
        for mode in ("api", "check"):
            rc, out = self.capture(["--config", self.config(), "--mode", mode])
            self.assertEqual(rc, 1)
            self.assertIn("answered 500", out)
            self.assertNotIn(TOKEN, out)

    def test_api_mode(self):
        rc, out = self.capture(["--config", self.config(), "--mode", "api"])
        self.assertEqual(rc, 0)
        rc, out = self.capture(["--config", self.config(url="http://127.0.0.1:1"), "--mode", "api"])
        self.assertEqual(rc, 1)
        self.assertIn("does not answer", out)

    def test_check_mode_reports_an_unanswering_api(self):
        rc, out = self.capture(["--config", self.config(url="http://127.0.0.1:1")])
        self.assertEqual(rc, 1)
        self.assertIn("not checked", out)

    def test_basic_credential(self):
        os.remove(os.path.join(self.cred, "token"))
        for k, v in (("username", "checker"), ("password", "pw-value")):
            with open(os.path.join(self.cred, k), "w") as f:
                f.write(v)
        self.eda_answer = (500, {"detail": "pw-value"})
        rc, out = self.capture(["--config", self.config(), "--mode", "api"])
        self.assertTrue(self.eda.seen[-1][2].startswith("Basic "))
        self.assertNotIn("pw-value", out)

    def test_required_settings(self):
        with self.assertRaises(SystemExit) as cm:
            E.main(["--config", self.config(automationNamespace="")], transport=E.make_transport())
        self.assertIn("automationNamespace", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

"""The event activation check (charts/charts/shc-cluster-base/files/eda_activations.py).

- its decisions as tables (status, newest instance, restart policy, enabled, the record: grace, hourly cap, a
  refused restart), and the re-read just before a restart;
- run_check against a fake event-automation API and a fake Kubernetes API: each state, the order of the count and
  the calls, a person's disable between the reads, a write conflict, refused and failed restarts, the daily count;
- an enumeration of the restart logic: two activations per case, every combination of status (including running
  with a failed newest instance, and deleted) x enabled x policy x record x where a read, write or the run fails
  (including a person disabling between the reads and a pod killed during the restart), then a second run on the
  stored state; checked against the invariants (only GETs and restart; never a restart of a disabled or unwatched
  activation; never more restarts than the cap in any hour; never a restart without its count stored first; exit
  non-zero exactly when a person is needed or the run was incomplete; no credential in any output);
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
           pollSeconds=0, startTimeoutSeconds=60)
OLD = NOW - 7200


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
    """decide(activation, newest instance, record, now, cfg) -> verdict."""

    TABLE = [
        (act(), inst(10), None, E.OK),
        (act(), None, None, E.OK),
        (act(enabled=False, status="failed"), inst(10, "failed"), {"notRunningSince": OLD}, E.DISABLED),
        (act(status="deleting"), None, None, E.DELETING),
        (act(status="workers offline"), None, None, E.WORKERS_OFFLINE),
        (act(status="paused"), None, None, E.UNKNOWN),
        (act(status="completed", policy="never"), inst(3, "completed"), {"notRunningSince": OLD}, E.NOT_WATCHED),
        (act(status="failed", policy="on-failure"), inst(3, "failed"), {"notRunningSince": OLD}, E.NOT_WATCHED),
        # first seen not running: it waits for the grace time (the platform's own restart comes first)
        (act(status="failed", message="Activation has reached the maximum number of restarts"), inst(10, "failed"),
         None, E.WAITING),
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 300}, E.RESTART),
        (act(status="completed"), inst(10, "completed"), {"notRunningSince": NOW - 300}, E.RESTART),
        (act(status="unresponsive"), inst(10, "unresponsive"), {"notRunningSince": NOW - 300}, E.RESTART),
        (act(status="stopped"), None, {"notRunningSince": NOW - 300}, E.RESTART),
        (act(status="error"), None, {"notRunningSince": NOW - 300}, E.RESTART),
        # reads running, its newest instance does not: the platform lost track of it
        (act(status="running"), inst(920, "failed"), {"notRunningSince": NOW - 300}, E.RESTART),
        (act(status="running"), inst(920, "failed"), None, E.WAITING),
        (act(status="starting"), None, {"notRunningSince": NOW - 300}, E.WAITING),
        (act(status="pending"), None, {"notRunningSince": NOW - 700}, E.RESTART),
        (act(status="failed", message="Activation 9 failed to start. Reason: Image quay.example/de:1 pull failed. "
                                      "The image url or the credentials may be incorrect."),
         inst(12, "failed"), {"notRunningSince": NOW - 300}, E.CANNOT_RUN),
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 300, "repairs": [NOW - 3000, NOW - 2000]},
         E.RESTART),
        (act(status="failed"), inst(10, "failed"),
         {"notRunningSince": NOW - 300, "repairs": [NOW - 3000, NOW - 2000, NOW - 1000]}, E.LIMIT),
        # at the cap it is restarted again once the hour allows
        (act(status="failed"), inst(10, "failed"),
         {"notRunningSince": NOW - 300, "repairs": [NOW - 7000, NOW - 6000, NOW - 5000]}, E.RESTART),
        # a restart the platform refused: left alone, reported
        (act(status="error"), None, {"notRunningSince": OLD, "gaveUp": True, "repairs": []}, E.GAVE_UP),
    ]

    def test_table(self):
        for i, (a, ins, rec, want) in enumerate(self.TABLE):
            with self.subTest(i=i, status=a["status"]):
                got, why, _ = E.decide(a, ins, rec, NOW, CFG)
                self.assertEqual(got, want, why)

    def test_refused_clears_when_it_runs_or_is_disabled(self):
        _, _, rec = E.decide(act(), inst(1), {"gaveUp": True}, NOW, CFG)
        self.assertFalse(rec["gaveUp"])
        _, _, rec = E.decide(act(enabled=False, status="stopped"), None, {"gaveUp": True}, NOW, CFG)
        self.assertFalse(rec["gaveUp"])

    def test_repairs_kept_for_a_day(self):
        _, _, rec = E.decide(act(), inst(1), {"repairs": [NOW - 90000, NOW - 80000, NOW - 100]}, NOW, CFG)
        self.assertEqual(rec["repairs"], [NOW - 80000, NOW - 100])

    def test_cap_is_a_value(self):
        cfg = dict(CFG, maxRepairsPerHour=1)
        got, _, _ = E.decide(act(status="failed"), None, {"notRunningSince": NOW - 300, "repairs": [NOW - 10]}, NOW, cfg)
        self.assertEqual(got, E.LIMIT)


class StillDue(unittest.TestCase):
    """still_due(first read, fresh read, fresh newest instance): the re-read just before the restart."""

    TABLE = [
        (act(status="failed"), act(status="failed"), inst(1, "failed"), True),
        (act(status="failed"), act(status="failed", enabled=False), None, False),       # a person disabled it
        (act(status="failed"), act(status="failed", policy="never"), None, False),
        (act(status="failed"), act(status="running"), inst(2), False),                 # back by itself
        (act(status="failed"), act(status="pending"), None, False),                     # the platform is on it
        (act(status="pending"), act(status="pending"), None, True),                     # stuck in pending
        (act(status="running"), act(status="running"), inst(1, "failed"), True),        # still lost track
        (act(status="running"), act(status="running"), inst(2), False),
        (act(status="failed"), act(status="deleting"), None, False),
        (act(status="failed"), None, None, False),
    ]

    def test_table(self):
        for i, (first, fresh, ins, want) in enumerate(self.TABLE):
            with self.subTest(i=i):
                self.assertEqual(E.still_due(first, fresh, ins), want)


class Jobs(unittest.TestCase):
    def jobs(self, *specs):
        return E.parse_jobs([{"metadata": {"name": n}, "status": {"active": a}} for n, a in specs])

    def test_two_running_jobs(self):
        j = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1), ("other", 1))[71]
        self.assertIn("2 activation Jobs", E.job_findings(act(71), j))
        self.assertEqual(E.stale_jobs(act(71), inst(1035), j), ["activation-job-71-968"])

    def test_stale_only_when_certain(self):
        j = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1))[71]
        self.assertEqual(E.stale_jobs(act(71), inst(1035, "failed"), j), [])
        self.assertEqual(E.stale_jobs(act(71, status="failed"), inst(1035), j), [])
        self.assertEqual(E.stale_jobs(act(71), inst(1100), j), [])


class Killed(BaseException):
    """The pod ends mid-run (deadline, eviction): nothing after it runs."""


def boom(what, status=502):
    # an API that echoes the credential in its error: the check must never show it
    return E.ApiError("%s: answered %d (upstream said: Authorization: Bearer %s)" % (what, status, TOKEN), status=status)


class FakeEda:
    """The event-automation API as the check uses it: list, detail, restart, nothing else. A restart of an enabled
    activation makes it run (unless in `never_runs`); of a disabled one it answers 403, as the platform does; one in
    `invalid` answers 400. `fail`/`kill` inject a failure: list (the first list read), wait (any later one), detail
    (a first read), reread (a read just before a restart), restart, refused (403), disable-between (a person
    disables the first subject between the reads)."""

    def __init__(self, items, instances=None, never_runs=(), invalid=(), log=None, fail=None, kill=None):
        self.items = {a["id"]: dict(a) for a in items}
        self.instances = {k: list(v) for k, v in (instances or {}).items()}
        self.never_runs, self.invalid = set(never_runs), set(invalid)
        self.log = log if log is not None else []
        self.fail, self.kill = fail, kill
        self.list_reads = 0
        self.restarting = False

    @property
    def calls(self):
        return [e[:2] for e in self.log if e[0] == "restart"]

    def activations(self):
        self.list_reads += 1
        self.log.append(("GET", "list"))
        if self.fail == ("list" if self.list_reads == 1 else "wait"):
            raise boom("GET /api/eda/v1/activations/")
        return [dict(a) for a in self.items.values()]

    def detail(self, aid):
        self.log.append(("GET", aid))
        point = "reread" if self.restarting else "detail"
        if self.fail == point:
            raise boom("GET /api/eda/v1/activations/%d/" % aid)
        if self.fail == "disable-between" and self.restarting and aid == 1:
            self.items[1]["is_enabled"] = False
        return dict(self.items[aid], instances=list(self.instances.get(aid, [])))

    def restart(self, aid):
        a = self.items[aid]
        self.log.append(("restart", aid, bool(a["is_enabled"]), a.get("restart_policy")))
        if self.kill == "restart":
            raise Killed()
        if self.fail == "restart":
            raise boom("POST restart", status=500)
        if self.fail == "refused" or not a["is_enabled"]:
            raise boom("POST restart", status=403)
        if aid in self.invalid:
            raise E.ApiError("POST restart: answered 400 (the decision environment does not exist)", status=400)
        a["status"] = "failed" if aid in self.never_runs else "running"
        nid = max([i["id"] for i in self.instances.get(aid, [])] + [0]) + 1
        self.instances.setdefault(aid, []).append(inst(nid, a["status"]))

    def __getattr__(self, name):
        raise AssertionError("the check called %s, which it must never call" % name)


class FakeKube:
    """The state ConfigMap (committed = what a later run reads) and the Jobs list. fail_writes: which writes of a run
    fail (1 = the first); conflict_writes: which answer 409."""

    def __init__(self, jobs=(), state=None, unreadable=False, log=None, fail_writes=(), conflict_writes=()):
        self.job_items = [{"metadata": {"name": n}, "status": {"active": a}} for n, a in jobs]
        self.committed = copy.deepcopy(state)
        self.unreadable = unreadable
        self.log = log if log is not None else []
        self.fail_writes, self.conflict_writes = set(fail_writes), set(conflict_writes)
        self.writes = 0
        self.versions = []
        self.deleted = []

    def jobs(self, ns):
        return self.job_items

    def delete_job(self, ns, name):
        self.deleted.append(name)

    def read_state(self, ns, name):
        self.log.append(("read-state",))
        if self.unreadable:
            raise boom("GET configmaps", status=403)
        return (copy.deepcopy(self.committed), "v0") if self.committed is not None else ({}, None)

    def write_state(self, ns, name, state, version):
        self.writes += 1
        self.versions.append(version)
        if self.writes in self.conflict_writes:
            raise boom("PUT configmaps/%s" % name, status=409)
        if self.writes in self.fail_writes:
            raise boom("PUT configmaps/%s" % name, status=500)
        self.committed = copy.deepcopy(state)
        self.log.append(("write", copy.deepcopy(state)))
        return "v%d" % self.writes


def run(eda, kube, cfg=None, dry_run=False, now=NOW):
    lines = []
    eda.list_reads = 0
    eda.restarting = False
    kube.writes = 0
    orig = E.restart_due

    def phase(*a, **k):
        eda.restarting = True
        return orig(*a, **k)
    E.restart_due = phase
    try:
        counts, report = E.run_check(eda, kube, dict(CFG, **(cfg or {})), now, dry_run, clock=Clock(),
                                     sleep=lambda s: None, out=lines.append)
    finally:
        E.restart_due = orig
    return counts, report, lines


def later(eda, kube, minutes, cfg=None):
    return run(eda, kube, cfg, now=NOW + 60 * minutes)


class Check(unittest.TestCase):
    def test_all_running(self):
        eda = FakeEda([act(1), act(2, "b")], {1: [inst(5)], 2: [inst(6)]})
        kube = FakeKube([("activation-job-1-5", 1), ("activation-job-2-6", 1)])
        counts, report, _ = run(eda, kube)
        self.assertEqual((report, counts["running"], eda.calls), ([], 2, []))

    def test_state_is_read_before_the_list(self):
        log = []
        run(FakeEda([act()], {1: [inst(1)]}, log=log), FakeKube(log=log))
        self.assertEqual(log[0], ("read-state",))

    def test_waits_then_restarts_in_one_batch(self):
        items = [act(1, "a", "failed", message="Activation has reached the maximum number of restarts"),
                 act(2, "b", "completed"), act(3, "c")]
        eda = FakeEda(items, {1: [inst(5, "failed")], 2: [inst(6, "completed")], 3: [inst(7)]})
        kube = FakeKube()
        counts, report, _ = run(eda, kube)
        self.assertEqual((counts["waiting"], eda.calls, report), (2, [], []))
        counts, report, lines = later(eda, kube, 5)
        self.assertEqual(report, [])
        self.assertEqual(eda.calls, [("restart", 1), ("restart", 2)])
        self.assertEqual(counts["restarted"], 2)
        self.assertIn("restarted activation b (id 2): running again", lines)
        self.assertEqual(kube.committed["1"]["repairs"], [NOW + 300])

    def test_every_restart_of_a_batch_is_stored_before_the_first_call(self):
        log = []
        eda = FakeEda([act(4, "x", "failed"), act(5, "y", "error"), act(6, "z", "stopped")], log=log)
        kube = FakeKube(state={k: {"notRunningSince": OLD} for k in ("4", "5", "6")}, log=log)
        run(eda, kube)
        first = next(i for i, e in enumerate(log) if e[0] == "restart")
        stored = [e[1] for e in log[:first] if e[0] == "write"][-1]
        for k in ("4", "5", "6"):
            self.assertIn(NOW, stored[k]["repairs"], k)

    def test_status_running_latest_instance_failed(self):
        eda = FakeEda([act(73, "project-create")], {73: [inst(920, "failed")]})
        kube = FakeKube(state={"73": {"notRunningSince": NOW - 600}})
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [("restart", 73)])
        self.assertEqual((counts["restarted"], report), (1, []))

    def test_a_person_disabling_between_the_reads_stops_the_restart(self):
        eda = FakeEda([act(1, "x", "failed"), act(2, "y", "failed")], fail="disable-between")
        kube = FakeKube(state={"1": {"notRunningSince": OLD}, "2": {"notRunningSince": OLD}})
        counts, report, lines = run(eda, kube)
        self.assertEqual(eda.calls, [("restart", 2)])
        self.assertIn("skip activation x (id 1): it changed since it was read (disabled, failed)", lines)
        self.assertEqual(kube.committed["1"]["repairs"], [])   # the count taken back

    def test_state_write_failing_before_means_nothing_is_restarted(self):
        eda = FakeEda([act(4, "x", "failed")])
        kube = FakeKube(state={"4": {"notRunningSince": OLD}}, fail_writes={1})
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [])
        self.assertIn("nothing was restarted", report[0])

    def test_a_write_conflict_stops_the_run(self):
        eda = FakeEda([act(4, "x", "failed")])
        kube = FakeKube(state={"4": {"notRunningSince": OLD}}, conflict_writes={1})
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [])
        self.assertIn("another run of this check", report[0])
        kube = FakeKube(state={"4": {"repairs": []}}, conflict_writes={1})
        counts, report, _ = run(FakeEda([act(4, "x")], {4: [inst(1)]}), kube)
        self.assertIn("another run of this check", report[0])

    def test_refused_restart_gives_up_and_keeps_reporting(self):
        eda = FakeEda([act(8, "x", "failed")], invalid={8})
        kube = FakeKube(state={"8": {"notRunningSince": OLD}})
        counts, report, _ = run(eda, kube)
        self.assertIn("RestartRefused", report[0])
        self.assertTrue(kube.committed["8"]["gaveUp"])
        for i in range(1, 4):
            counts, report, _ = later(eda, kube, 70 * i)
            self.assertIn("RestartRefused", report[0])
        self.assertEqual(len(eda.calls), 1)

    def test_other_restart_failure_counts_as_an_attempt(self):
        eda = FakeEda([act(8, "x", "failed")], fail="restart")
        kube = FakeKube(state={"8": {"notRunningSince": OLD}})
        counts, report, _ = run(eda, kube)
        self.assertIn("the restart failed (counted as an attempt)", report[0])
        self.assertEqual(len(kube.committed["8"]["repairs"]), 1)
        self.assertFalse(kube.committed["8"]["gaveUp"])

    def test_the_cap_then_again_when_the_hour_allows(self):
        eda = FakeEda([act(79, "broken", "failed")], never_runs={79})
        kube = FakeKube(state={"79": {"notRunningSince": OLD}})
        reports = [later(eda, kube, 5 * i)[1] for i in range(12)]
        self.assertEqual(len(eda.calls), 3)
        self.assertIn("RepairLimitReached", reports[-1][0])
        later(eda, kube, 65)
        self.assertEqual(len(eda.calls), 4)

    def test_restarted_too_often_in_a_day_holds_for_the_window(self):
        eda = FakeEda([act(9, "flaky", "failed")])
        kube = FakeKube(state={"9": {"notRunningSince": OLD}})
        reports = []
        quick = {"notRunningMinutes": 0}
        for i in range(8):
            eda.items[9]["status"] = "failed"
            reports.append(later(eda, kube, 25 * i, quick)[1])
        self.assertEqual(reports[:6], [[]] * 6)
        self.assertIn("RepairedTooOften: 7 restarts in the last day", reports[6][0])
        # it runs now; the report holds while more than 6 restarts are within the last day (until 24 h after the
        # second one, at minute 25 + 1440)
        counts, report, _ = later(eda, kube, 1440 - 10, quick)
        self.assertIn("RepairedTooOften: 8 restarts", report[0])
        counts, report, _ = later(eda, kube, 25 + 1440 + 5, quick)
        self.assertEqual(report, [])

    def test_unwatched_activations_are_untouched_and_their_jobs_not_reported(self):
        eda = FakeEda([act(7, "one-shot", "completed", policy="never"), act(8, "b")], {8: [inst(1)]})
        kube = FakeKube([("activation-job-7-1", 1), ("activation-job-7-2", 1)],
                        state={"7": {"notRunningSince": OLD}})
        counts, report, lines = run(eda, kube)
        self.assertEqual((eda.calls, report), ([], []))
        self.assertIn("not watched (restart policy other than always): one-shot", lines)

    def test_a_disabled_activation_is_never_touched_nor_reported(self):
        eda = FakeEda([act(7, "off", "failed", enabled=False)])
        counts, report, _ = run(eda, FakeKube(state={"7": {"notRunningSince": OLD, "gaveUp": True}}))
        self.assertEqual((eda.calls, report), ([], []))
        self.assertEqual([e for e in eda.log if e[0] == "GET" and e[1] != "list"], [])

    def test_an_empty_list_never_prunes_the_counts(self):
        kube = FakeKube(state={"4": {"repairs": [NOW - 10]}})
        counts, report, _ = run(FakeEda([]), kube)
        self.assertIn("the activations list is empty", report[0])
        counts, report, _ = run(FakeEda([]), kube, {"allowNoActivations": True})
        self.assertEqual(report, [])
        self.assertEqual((kube.writes, kube.committed), (0, {"4": {"repairs": [NOW - 10]}}))

    def test_unreadable_state_restarts_nothing_even_without_grace(self):
        eda = FakeEda([act(1, "a", "failed")])
        counts, report, _ = run(eda, FakeKube(unreadable=True), {"notRunningMinutes": 0})
        self.assertEqual(eda.calls, [])
        self.assertIn("cannot be read, so nothing is restarted", report[0])
        self.assertTrue(any("not restarted: the restart count is unknown" in r for r in report))

    def test_a_wait_failure_is_reported(self):
        eda = FakeEda([act(4, "x", "failed")], fail="wait")
        counts, report, _ = run(eda, FakeKube(state={"4": {"notRunningSince": OLD}}))
        self.assertEqual(eda.calls, [("restart", 4)])
        self.assertTrue(report)
        self.assertNotIn(TOKEN, "\n".join(report))

    def test_cannot_run_workers_offline_unknown_are_reported_not_restarted(self):
        eda = FakeEda([act(1, "a", "failed", message="Image example/x:1 pull failed."), act(2, "b", "workers offline"),
                       act(3, "c", "paused")])
        counts, report, _ = run(eda, FakeKube(state={"1": {"notRunningSince": OLD}}))
        self.assertEqual(eda.calls, [])
        self.assertEqual(sorted(r.split(":")[1].strip() for r in report),
                         ["CannotRun", "UnknownStatus", "WorkersOffline"])

    def test_two_jobs_reported_and_removed_only_when_switched_on(self):
        items = [act(71, "project-delete")]
        jobs = [("activation-job-71-968", 1), ("activation-job-71-1035", 1)]
        kube = FakeKube(jobs)
        counts, report, _ = run(FakeEda(items, {71: [inst(968, "failed"), inst(1035)]}), kube)
        self.assertIn("TwoJobs", report[0])
        self.assertEqual(kube.deleted, [])
        kube = FakeKube(jobs)
        counts, report, _ = run(FakeEda(items, {71: [inst(968, "failed"), inst(1035)]}), kube,
                                {"removeStaleJobs": True})
        self.assertEqual((kube.deleted, report), (["activation-job-71-968"], []))

    def test_dry_run_changes_nothing(self):
        eda = FakeEda([act(1, "a", "failed")])
        kube = FakeKube(state={"1": {"notRunningSince": OLD}})
        run(eda, kube, dry_run=True)
        self.assertEqual((eda.calls, kube.writes), ([], 0))

    def test_writes_carry_the_version_read(self):
        kube = FakeKube(state={"4": {"notRunningSince": OLD}})
        run(FakeEda([act(4, "x", "failed")]), kube)
        self.assertEqual(kube.versions, ["v0", "v1"])


# ---- the enumeration --------------------------------------------------------------------------------------------

# running-lost: status running while the newest instance failed
STATUSES = ["running", "running-lost", "pending", "starting", "stopping", "stopped", "completed", "failed",
            "unresponsive", "error", "deleted"]
RECORDS = {
    "none": None,
    "below": {"notRunningSince": OLD, "repairs": [NOW - 1000]},
    "atCap": {"notRunningSince": OLD, "repairs": [NOW - 3000, NOW - 2000, NOW - 1000]},
    "gaveUp": {"notRunningSince": OLD, "repairs": [], "gaveUp": True},
}
FAILURES = ["none", "state-read", "list", "detail", "write-before", "conflict-before", "reread", "disable-between",
            "restart", "refused", "kill-restart", "wait", "write-after"]
SUBJECTS = list(itertools.product(STATUSES, (True, False), ("always", "on-failure"), RECORDS))


def expected(status, enabled, policy, record):
    """(restart due, needs a person) for one subject, from the rules the docs state."""
    if status == "deleted" or not enabled or policy != "always" or status == "running" or record == "none":
        return False, False
    if record in ("gaveUp", "atCap"):
        return False, True
    return True, False


def subject(aid, status, enabled, policy):
    if status == "deleted":
        return None, []
    st = "running" if status == "running-lost" else status
    newest = "failed" if status == "running-lost" else st
    return act(aid, "subject-%d" % aid, st, enabled, policy=policy), [inst(10 * aid, newest)]


class Enumeration(unittest.TestCase):
    def test_every_combination(self):
        cap = CFG["maxRepairsPerHour"]
        failures, cases = [], 0
        for i, (one, fail) in enumerate(itertools.product(SUBJECTS, FAILURES)):
            three = SUBJECTS[(i * 7 + 3) % len(SUBJECTS)]
            cases += 1
            log = []
            items, instances, state = [act(2, "steady")], {2: [inst(20)]}, {"2": {"repairs": []}}
            for aid, (status, enabled, policy, record) in ((1, one), (3, three)):
                a, ins = subject(aid, status, enabled, policy)
                if a:
                    items.append(a)
                    instances[aid] = ins
                if RECORDS[record] is not None:
                    state[str(aid)] = copy.deepcopy(RECORDS[record])
            due = {aid: expected(*s) for aid, s in ((1, one), (3, three))}
            acting = any(d for d, _ in due.values())
            person = any(p for _, p in due.values())
            eda_fail = fail if fail in ("list", "detail", "reread", "disable-between", "restart", "refused",
                                        "wait") else None
            kube = FakeKube(state=state, log=log, unreadable=fail == "state-read",
                            fail_writes={1} if fail == "write-before" and acting else
                            ({2} if acting else {1}) if fail == "write-after" else set(),
                            conflict_writes={1} if fail == "conflict-before" and acting else set())
            eda = FakeEda(items, instances, log=log, fail=eda_fail, kill="restart" if fail == "kill-restart" else None)
            name = (one, three, fail)
            outcome = {}

            def bad(why):
                failures.append("%s: %s (log %s, report %s)" % (name, why, log, outcome.get("report")))

            lines, nonzero = [], False
            try:
                counts, report, lines = run(eda, kube)
                outcome["report"] = report
                nonzero = bool(report)
            except Killed:
                nonzero = True
            except E.ApiError as e:
                outcome["report"] = [str(e)]
                nonzero = True
            if TOKEN in "\n".join(lines + outcome.get("report", [])):
                bad("the credential is in the output")
            # exit status of the first run: non-zero exactly when a person is needed or the run was incomplete
            if fail in ("state-read", "list", "detail", "write-after"):
                want = True
            elif fail in ("write-before", "conflict-before", "reread", "restart", "refused", "kill-restart", "wait"):
                want = person or acting
            elif fail == "disable-between":
                # a skipped restart is not a fault; the other subject's restart works
                want = person
            else:
                want = person
            if nonzero != want:
                bad("exit %s, expected %s" % ("non-zero" if nonzero else "zero", "non-zero" if want else "zero"))
            if fail == "none" and acting:
                for aid, (d, _) in due.items():
                    if d and ("restart", aid) not in eda.calls:
                        bad("activation %d was due and not restarted" % aid)
            # a second run on the stored state, five minutes later, nothing failing
            eda.fail = eda.kill = None
            kube.unreadable = False
            kube.fail_writes, kube.conflict_writes = set(), set()
            try:
                _, report2, lines2 = later(eda, kube, 5)
                if TOKEN in "\n".join(lines2 + report2):
                    bad("the credential is in the output of the second run")
            except Exception as e:  # noqa: BLE001
                bad("the second run raised %r" % e)
            # invariants over both runs
            stored = copy.deepcopy(state)
            for e in log:
                if e[0] == "write":
                    stored = e[1]
                elif e[0] == "restart":
                    aid, enabled_then, policy = e[1], e[2], e[3]
                    if not enabled_then:
                        bad("restart of a disabled activation %d" % aid)
                    if policy != "always":
                        bad("restart of an unwatched activation %d" % aid)
                    if not any(t >= NOW for t in (stored.get(str(aid)) or {}).get("repairs") or []):
                        bad("restart of %d without its count stored first" % aid)
                elif e[0] not in ("GET", "read-state"):
                    bad("a call other than GET and restart: %s" % (e,))
            for aid in (1, 3):
                prior = len(E.in_window((state.get(str(aid)) or {}).get("repairs"), NOW, E.HOUR))
                if prior + len([c for c in eda.calls if c == ("restart", aid)]) > cap:
                    bad("more restarts of %d than the cap in the hour" % aid)
        self.assertEqual(cases, len(SUBJECTS) * len(FAILURES))
        self.assertEqual(failures[:8], [], "%d of %d cases break an invariant" % (len(failures), cases))


# ---- main() over HTTP ---------------------------------------------------------------------------------------------

class Server:
    """A local HTTP server answering with handler(method, path, body) -> (code, json, bytes or None[, headers])."""

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
                raw = doc if isinstance(doc, bytes) else b"" if doc is None else json.dumps(doc).encode()
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
            if method == "POST" and parts[5] == "restart":
                try:
                    self.fake.restart(aid)
                except E.ApiError as e:
                    return e.status, {"detail": str(e)}
                return 204, None
            return 405, {"detail": "not allowed"}

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

    def test_restarts_over_http(self):
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 0, out)
        self.assertIn("restarted activation a (id 1): running again", out)
        self.assertEqual(self.put_versions, ["3", "5"])
        self.assertEqual({a for _, _, a in self.eda.seen}, {"Bearer " + TOKEN})
        self.assertEqual({(m, p.rsplit("/", 2)[-2]) for m, p, _ in self.eda.seen if m != "GET"}, {("POST", "restart")})
        self.assertNotIn(TOKEN, out)

    def test_exit_1_when_something_is_reported(self):
        self.fake.items[1]["status_message"] = "Image example/x:1 pull failed."
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 1)
        self.assertIn("PROBLEM activation a (id 1): CannotRun", out)

    def test_pagination(self):
        self.paged = True
        self.fake.items = {1: act(1, "a"), 2: act(2, "b", "failed")}
        self.fake.instances = {1: [inst(4)], 2: [inst(5, "failed")]}
        self.state = {"2": {"notRunningSince": 1}}
        rc, out = self.capture(["--config", self.config()])
        self.assertIn("restarted activation b (id 2)", out)

    def test_a_body_that_is_not_json_is_reported(self):
        self.eda_answer = (200, b"<html>maintenance</html>")
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 1)
        self.assertIn("with a body that is not JSON", out)

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

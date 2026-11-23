"""The event activation check (charts/charts/shc-cluster-base/files/eda_activations.py): its decisions as tables, one
run against a fake event-automation API and a fake Kubernetes API (each state: running, a failed or completed
activation, one whose status reads running while its latest instance does not, one that cannot run, the repair
limit, the workers offline, an unknown status, two Jobs for one activation, an API that does not answer, a repair
count that cannot be read), and main() end to end over HTTP. Standard library only, so it also runs in the job
image (python3 -m unittest tests/unit/test_eda_activations.py)."""
import http.server
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
CFG = dict(E.DEFAULTS, url="http://eda.invalid", automationNamespace="automation", stateNamespace="ops",
           pollSeconds=0, stopTimeoutSeconds=30, startTimeoutSeconds=60)


def act(aid=1, name="project-create", status="running", enabled=True, message=""):
    return {"id": aid, "name": name, "status": status, "is_enabled": enabled, "restart_policy": "always",
            "restart_count": 3, "status_message": message}


def inst(iid, status="running", message=""):
    return {"id": iid, "status": status, "status_message": message}


class Decide(unittest.TestCase):
    """decide(activation, latest instance, record, now, cfg) -> verdict."""

    TABLE = [
        # (activation, instance, record, verdict)
        (act(), inst(10), None, E.OK),
        (act(), None, None, E.OK),
        (act(enabled=False, status="stopped"), None, None, E.DISABLED),
        (act(status="deleting"), None, None, E.DELETING),
        (act(status="workers offline"), None, None, E.WORKERS_OFFLINE),
        (act(status="paused"), None, None, E.UNKNOWN),
        # first seen not running: it waits for the grace time (the platform's own restart comes first)
        (act(status="failed", message="Activation has reached the maximum number of restarts"), inst(10, "failed"),
         None, E.WAITING),
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="completed"), inst(10, "completed"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="unresponsive"), inst(10, "unresponsive"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="stopped"), None, {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="error"), None, {"notRunningSince": NOW - 300}, E.REPAIR),
        # reads running, its latest instance does not (the platform lost track of it)
        (act(status="running"), inst(920, "failed"), {"notRunningSince": NOW - 300}, E.REPAIR),
        (act(status="running"), inst(920, "failed"), None, E.WAITING),
        # starting for a while is still within its longer grace; past it, repaired
        (act(status="starting"), None, {"notRunningSince": NOW - 300}, E.WAITING),
        (act(status="pending"), None, {"notRunningSince": NOW - 700}, E.REPAIR),
        # an image that cannot be pulled, an invalid request: reported, never repaired
        (act(status="failed", message="Activation 9 failed to start. Reason: Image quay.example/de:1 pull failed. "
                                      "The image url or the credentials may be incorrect."),
         inst(12, "failed"), {"notRunningSince": NOW - 300}, E.CANNOT_RUN),
        (act(status="failed", message="Activation has reached the maximum number of restarts"),
         inst(12, "failed", "Image example/de:1 pull failed. The image url or the credentials may be incorrect."),
         {"notRunningSince": NOW - 300}, E.CANNOT_RUN),
        # the cap: three repairs within the hour, then reported and left alone
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 300, "repairs": [NOW - 3000, NOW - 2000]},
         E.REPAIR),
        (act(status="failed"), inst(10, "failed"),
         {"notRunningSince": NOW - 300, "repairs": [NOW - 3000, NOW - 2000, NOW - 1000]}, E.LIMIT),
        # repairs older than an hour do not count
        (act(status="failed"), inst(10, "failed"),
         {"notRunningSince": NOW - 300, "repairs": [NOW - 7000, NOW - 6000, NOW - 5000]}, E.REPAIR),
        # once given up it stays given up while it does not run, even after the hour
        (act(status="failed"), inst(10, "failed"), {"notRunningSince": NOW - 9000, "gaveUp": True, "repairs": []},
         E.LIMIT),
        # this check disabled it and the enable did not go through: enabled again (counted), never left silently
        (act(status="stopped", enabled=False), None, {"disabledByCheck": True, "repairs": [NOW - 100]}, E.ENABLE),
        (act(status="stopped", enabled=False), None,
         {"disabledByCheck": True, "repairs": [NOW - 300, NOW - 200, NOW - 100]}, E.LIMIT),
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
        self.assertEqual(rec["repairs"], [])

    def test_a_person_disabling_clears_the_record(self):
        _, _, rec = E.decide(act(enabled=False, status="stopped"), None, {"gaveUp": True, "notRunningSince": NOW},
                             NOW, CFG)
        self.assertFalse(rec["gaveUp"])
        self.assertIsNone(rec["notRunningSince"])

    def test_cap_is_a_value(self):
        cfg = dict(CFG, maxRepairsPerHour=1)
        got, _, _ = E.decide(act(status="failed"), None, {"notRunningSince": NOW - 300, "repairs": [NOW - 10]}, NOW, cfg)
        self.assertEqual(got, E.LIMIT)


class Jobs(unittest.TestCase):
    def jobs(self, *specs):
        return E.parse_jobs([{"metadata": {"name": n}, "status": {"active": a}} for n, a in specs])

    def test_parse(self):
        got = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1), ("other-job", 1),
                        ("activation-job-5-6", 0))
        self.assertEqual(sorted(got), [5, 71])
        self.assertEqual(sorted(got[71]), [(968, "activation-job-71-968", 1), (1035, "activation-job-71-1035", 1)])

    def test_two_running_jobs(self):
        j = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1))[71]
        self.assertIn("2 activation Jobs", E.job_findings(act(71), j))
        self.assertEqual(E.stale_jobs(act(71), inst(1035), j), ["activation-job-71-968"])

    def test_one_running_one_finished(self):
        j = self.jobs(("activation-job-71-968", 0), ("activation-job-71-1035", 1))[71]
        self.assertIsNone(E.job_findings(act(71), j))

    def test_stale_only_when_certain(self):
        j = self.jobs(("activation-job-71-968", 1), ("activation-job-71-1035", 1))[71]
        # the latest instance does not run, the activation does not run, or the latest has no running Job: nothing
        self.assertEqual(E.stale_jobs(act(71), inst(1035, "failed"), j), [])
        self.assertEqual(E.stale_jobs(act(71, status="failed"), inst(1035), j), [])
        self.assertEqual(E.stale_jobs(act(71), inst(1100), j), [])
        self.assertEqual(E.stale_jobs(act(71), None, j), [])


class FakeEda:
    """The event-automation API as the check uses it. A disable takes `stop_polls` list reads to reach stopped; an
    enable takes `start_polls` to reach running, unless the activation is in `never_runs` (it fails) or `invalid`
    (400). An enable while the status blocks it answers 409, as the platform does."""

    def __init__(self, items, instances=None, never_runs=(), invalid=(), stop_polls=1, start_polls=2, down=False):
        self.items = {a["id"]: dict(a) for a in items}
        self.instances = dict(instances or {})
        self.never_runs, self.invalid = set(never_runs), set(invalid)
        self.stop_polls, self.start_polls = stop_polls, start_polls
        self.pending = {}
        self.calls = []
        self.down = down

    def _tick(self):
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
        if self.down:
            raise E.ApiError("GET /api/eda/v1/activations/: no answer (connection refused)")
        self._tick()
        return [dict(a) for a in self.items.values()]

    def detail(self, aid):
        return dict(self.items[aid], instances=list(self.instances.get(aid, [])))

    def disable(self, aid):
        self.calls.append(("disable", aid))
        a = self.items[aid]
        a["is_enabled"] = False
        if a["status"] in ("running", "starting"):
            a["status"] = "stopping"
            self.pending[aid] = (self.stop_polls, "stopped")

    def enable(self, aid):
        self.calls.append(("enable", aid))
        a = self.items[aid]
        if a["status"] in E.ENABLE_BLOCKED:
            raise E.ApiError("POST enable: answered 409 (Activation not enabled due to current activation status)",
                             status=409)
        if aid in self.invalid:
            raise E.ApiError("POST enable: answered 400 (the decision environment does not exist)", status=400)
        a["is_enabled"] = True
        a["status"] = "pending"
        self.pending[aid] = (self.start_polls, "failed" if aid in self.never_runs else "running")


class FakeKube:
    def __init__(self, jobs=(), state=None, unreadable=False):
        self.job_items = [{"metadata": {"name": n}, "status": {"active": a}} for n, a in jobs]
        self.state = state
        self.unreadable = unreadable
        self.written = None
        self.deleted = []

    def jobs(self, ns):
        return self.job_items

    def delete_job(self, ns, name):
        self.deleted.append(name)

    def read_state(self, ns, name):
        if self.unreadable:
            raise E.ApiError("GET configmaps: answered 403", status=403)
        return (json.loads(json.dumps(self.state)), "7") if self.state is not None else ({}, None)

    def write_state(self, ns, name, state, version):
        self.written = (state, version)


class Clock:
    """A clock that moves on 5 s each time it is read, so every wait in the check ends."""

    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 5
        return self.t


def run(eda, kube, cfg=None, dry_run=False):
    lines = []
    counts, report = E.run_check(eda, kube, dict(CFG, **(cfg or {})), NOW, dry_run, clock=Clock(),
                                 sleep=lambda s: None, out=lines.append)
    return counts, report, lines


class Check(unittest.TestCase):
    def test_all_running(self):
        eda = FakeEda([act(1), act(2, "b")], {1: [inst(5)], 2: [inst(6)]})
        kube = FakeKube([("activation-job-1-5", 1), ("activation-job-2-6", 1)])
        counts, report, _ = run(eda, kube)
        self.assertEqual(report, [])
        self.assertEqual(counts["running"], 2)
        self.assertEqual(eda.calls, [])
        self.assertEqual(sorted(kube.written[0]), ["1", "2"])

    def test_waits_then_repairs_in_one_batch(self):
        items = [act(1, "a", "failed", message="Activation has reached the maximum number of restarts"),
                 act(2, "b", "completed"), act(3, "c")]
        eda = FakeEda(items, {1: [inst(5, "failed")], 2: [inst(6, "completed")], 3: [inst(7)]})
        kube = FakeKube()
        counts, report, lines = run(eda, kube)
        self.assertEqual((counts["waiting"], eda.calls, report), (2, [], []))
        # the next run, five minutes later: both repaired, disables before enables
        kube.state = kube.written[0]
        lines = []
        counts, report = E.run_check(eda, kube, CFG, NOW + 300, False, clock=Clock(), sleep=lambda s: None,
                                     out=lines.append)
        self.assertEqual(report, [])
        self.assertEqual(eda.calls, [("disable", 1), ("disable", 2), ("enable", 1), ("enable", 2)])
        self.assertEqual(counts["repaired"], 2)
        self.assertTrue(any(l.startswith("repair activation a (id 1)") and "repair 1 of at most 3" in l for l in lines))
        self.assertTrue(any(l == "repaired activation b (id 2): running again" for l in lines))
        st = kube.written[0]
        self.assertEqual(st["1"]["repairs"], [NOW + 300])
        self.assertIsNone(st["1"]["notRunningSince"])
        self.assertFalse(st["1"]["disabledByCheck"])

    def test_status_running_latest_instance_failed(self):
        eda = FakeEda([act(73, "project-create")], {73: [inst(920, "failed")]})
        kube = FakeKube(state={"73": {"notRunningSince": NOW - 600}})
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [("disable", 73), ("enable", 73)])
        self.assertEqual((counts["repaired"], report), (1, []))

    def test_not_running_after_repair_then_the_cap(self):
        eda = FakeEda([act(79, "broken", "failed")], {79: [inst(1, "failed")]}, never_runs={79})
        kube = FakeKube(state={"79": {"notRunningSince": NOW - 600}})
        now = NOW
        reports = []
        for _ in range(5):
            kube.state = kube.written[0] if kube.written else kube.state
            counts, report = E.run_check(eda, kube, CFG, now, False, clock=Clock(), sleep=lambda s: None,
                                         out=lambda s: None)
            reports.append(report)
            now += 300
        self.assertEqual([c for c in eda.calls if c[0] == "enable"], [("enable", 79)] * 3)
        for r in reports[:3]:
            self.assertEqual(len(r), 1)
            self.assertIn("NotRunningAfterRepair", r[0])
        for r in reports[3:]:
            self.assertIn("RepairLimitReached", r[0])
            self.assertIn("activation broken (id 79)", r[0])
        self.assertTrue(kube.written[0]["79"]["gaveUp"])

    def test_cannot_run_is_reported_not_repaired(self):
        msg = "Activation 79 failed to start. Reason: Image example/de:9 pull failed. The image url or the " \
              "credentials may be incorrect."
        eda = FakeEda([act(79, "uses-missing-image", "failed", message=msg)], {79: [inst(3, "failed")]})
        counts, report, _ = run(eda, FakeKube(state={"79": {"notRunningSince": NOW - 900}}))
        self.assertEqual(eda.calls, [])
        self.assertEqual(len(report), 1)
        self.assertIn("CannotRun", report[0])

    def test_enable_refused_as_invalid_is_not_left_silent(self):
        eda = FakeEda([act(8, "x", "failed")], {8: [inst(3, "failed")]}, invalid={8})
        kube = FakeKube(state={"8": {"notRunningSince": NOW - 900}})
        counts, report, _ = run(eda, kube)
        self.assertIn("CannotRun: the platform refuses to enable it", report[0])
        self.assertTrue(kube.written[0]["8"]["disabledByCheck"])
        # the next run sees it disabled: it is enabled again (no disable), and still reported
        kube.state = kube.written[0]
        counts, report = E.run_check(eda, kube, CFG, NOW + 300, False, clock=Clock(), sleep=lambda s: None,
                                     out=lambda s: None)
        self.assertEqual(eda.calls[-1], ("enable", 8))
        self.assertEqual(len(report), 1)

    def test_enable_conflict_when_it_never_stops(self):
        eda = FakeEda([act(4, "slow")], {4: [inst(2, "failed")]}, stop_polls=10_000)
        counts, report, _ = run(eda, FakeKube(state={"4": {"notRunningSince": NOW - 900}}))
        self.assertIn("the enable was refused (status stopping)", report[0])

    def test_workers_offline_and_unknown_status_are_reported(self):
        eda = FakeEda([act(1, "a", "workers offline"), act(2, "b", "paused")])
        counts, report, _ = run(eda, FakeKube())
        self.assertEqual(eda.calls, [])
        self.assertEqual(sorted(r.split(":")[1].strip() for r in report), ["UnknownStatus", "WorkersOffline"])

    def test_two_jobs_reported_and_removed_only_when_switched_on(self):
        items = [act(71, "project-delete")]
        jobs = [("activation-job-71-968", 1), ("activation-job-71-1035", 1)]
        eda = FakeEda(items, {71: [inst(968, "failed"), inst(1035)]})
        kube = FakeKube(jobs)
        counts, report, _ = run(eda, kube)
        self.assertEqual(counts["twoJobs"], 1)
        self.assertIn("TwoJobs", report[0])
        self.assertIn("(older: activation-job-71-968)", report[0])
        self.assertEqual(kube.deleted, [])
        kube = FakeKube(jobs)
        counts, report, lines = run(FakeEda(items, {71: [inst(968, "failed"), inst(1035)]}), kube,
                                    {"removeStaleJobs": True})
        self.assertEqual((kube.deleted, report), (["activation-job-71-968"], []))
        self.assertEqual(counts["staleJobsRemoved"], 1)

    def test_api_does_not_answer(self):
        with self.assertRaises(E.ApiError):
            run(FakeEda([], down=True), FakeKube())

    def test_unreadable_state_repairs_nothing(self):
        eda = FakeEda([act(1, "a", "failed")], {1: [inst(1, "failed")]})
        kube = FakeKube(unreadable=True)
        # even long after it was first seen: with no count there is no limit, so no repair
        counts, report, _ = run(eda, kube)
        self.assertEqual(eda.calls, [])
        self.assertIn("cannot be read, so nothing is repaired", report[0])
        self.assertIsNone(kube.written)

    def test_dry_run_changes_nothing(self):
        eda = FakeEda([act(1, "a", "failed")], {1: [inst(1, "failed")]})
        kube = FakeKube(state={"1": {"notRunningSince": NOW - 900}})
        run(eda, kube, dry_run=True)
        self.assertEqual((eda.calls, kube.written), ([], None))

    def test_deleted_activations_leave_the_state(self):
        kube = FakeKube(state={"1": {"repairs": [NOW]}, "99": {"repairs": [NOW]}})
        run(FakeEda([act(1)], {1: [inst(1)]}), kube)
        self.assertEqual(sorted(kube.written[0]), ["1"])


# ---- main() over HTTP ---------------------------------------------------------------------------------------------

class Server:
    """A local HTTP server answering with handler(method, path, body) -> (code, json or None)."""

    def __init__(self, handler):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                outer.seen.append((self.command, self.path, self.headers.get("Authorization")))
                code, doc = handler(self.command, self.path, body)
                raw = b"" if doc is None else json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
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
            f.write("s3cret-token-value")
        self.cred = cred
        self.fake = FakeEda([act(1, "a", "failed"), act(2, "b")], {1: [inst(4, "failed")], 2: [inst(5)]})
        self.state = {"1": {"notRunningSince": 1}}

        def eda(method, path, body):
            if method == "GET" and path.startswith("/api/eda/v1/activations/?"):
                return 200, {"count": 2, "next": None, "results": self.fake.activations()}
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
                self.put_version = body["metadata"]["resourceVersion"]
                return 200, body
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

    def capture(self, argv):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = E.main(argv)
        return rc, buf.getvalue()

    def test_repairs_over_http(self):
        rc, out = self.capture(["--config", self.config()])
        self.assertEqual(rc, 0, out)
        self.assertIn("repaired activation a (id 1): running again", out)
        self.assertEqual(len(self.state["1"]["repairs"]), 1)
        self.assertEqual(self.put_version, "3")
        auths = {a for _, _, a in self.eda.seen}
        self.assertEqual(auths, {"Bearer s3cret-token-value"})
        self.assertNotIn("s3cret", out)

    def test_api_mode(self):
        rc, out = self.capture(["--config", self.config(), "--mode", "api"])
        self.assertEqual(rc, 0)
        self.assertIn("answers", out)
        rc, out = self.capture(["--config", self.config(url="http://127.0.0.1:1"), "--mode", "api"])
        self.assertEqual(rc, 1)
        self.assertIn("does not answer", out)
        self.assertNotIn("s3cret", out)

    def test_check_mode_leaves_an_unanswering_api_to_the_api_mode(self):
        rc, out = self.capture(["--config", self.config(url="http://127.0.0.1:1")])
        self.assertEqual(rc, 0)
        self.assertIn("not checked", out)

    def test_basic_credential(self):
        os.remove(os.path.join(self.cred, "token"))
        for k, v in (("username", "checker"), ("password", "pw-value")):
            with open(os.path.join(self.cred, k), "w") as f:
                f.write(v)
        rc, out = self.capture(["--config", self.config(), "--mode", "api"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.eda.seen[-1][2].startswith("Basic "))
        self.assertNotIn("pw-value", out)

    def test_required_settings(self):
        with self.assertRaises(SystemExit) as cm:
            E.main(["--config", self.config(automationNamespace="")])
        self.assertIn("automationNamespace", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

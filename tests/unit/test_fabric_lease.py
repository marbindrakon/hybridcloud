"""The border gateway lease fabric-config-<fabric> is renewed by its holder, so it can be short.

- shc_ledger.py lease-renew: only the holder renews (renewTime, at the resourceVersion read); a lease taken over,
  released or deleted, or an API that does not answer within the renewal budget, stops the job before its next step.
  lease-release with delete (the fabric's teardown) removes the lease only while the job holds it.
- lease-acquire takes over a lease not renewed for the duration the lease itself carries: a dead holder's lease goes to
  the job waiting for it, a live holder's never does, and a lease written by the code before renewals (1800 s, never
  renewed) keeps its own duration. The old code meeting a renewed lease is covered with a copy of that code.
- Every holder renews before each node and around its long waits, and runs its node steps with the step timeout.
- The numbers: the realization's nodes.configLease and the helper's defaults agree, and they keep the longest time
  between two renewals below the duration, the wait above it, and a job's life below the operator's in-flight limit.
The API and the clock are fakes; the task-file checks that run ansible-playbook are skipped without it.
"""
import datetime
import importlib.util
import json
import os
import re
import sys
import textwrap
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

LEDGER = os.path.join(ROOT, "eda/common/files/shc_ledger.py")
ROLES = os.path.join(ROOT, "eda/rulebooks/roles")
NS = "sovereign-cloud"
NAME = "fabric-config-lab"
T0 = 1_800_000_000.0


def _read(path):
    with open(path) as f:
        return f.read()


def load_ledger():
    spec = importlib.util.spec_from_file_location("shc_ledger_lease", LEDGER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The lease code of the release before renewals (verbatim): what a job started before the rollout runs. It writes
# 1800 s and never renews; a waiter judges a lease by the duration the lease carries.
OLD_LEASE_CODE = '''
def lease_acquire(api, req):
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    dur, deadline = int(req.get("duration", 1800)), time.time() + int(req.get("wait", 1200))
    while True:
        code, lease = api.call("GET", lease_path(req["namespace"], name))
        spec = {"holderIdentity": holder, "leaseDurationSeconds": dur, "acquireTime": ts(now()), "renewTime": ts(now())}
        if code == 404:
            code, out = api.call("POST", lease_path(req["namespace"]), {
                "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                "metadata": {"name": name, "namespace": req["namespace"]}, "spec": spec})
            if code in (200, 201):
                return {"acquired": True, "lease": name}
            if code != 409:
                raise RuntimeError(f"creating lease {name}: HTTP {code} {out.get('message', '')}")
            continue
        if code != 200:
            raise RuntimeError(f"reading lease {name}: HTTP {code} {lease.get('message', '')}")
        s = lease.get("spec") or {}
        cur = s.get("holderIdentity") or ""
        renew = s.get("renewTime")
        expired = True
        if cur and renew:
            t = datetime.datetime.strptime(renew.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
            expired = (now() - t).total_seconds() > int(s.get("leaseDurationSeconds") or dur)
        if not cur or cur == holder or expired:
            lease["spec"] = spec
            code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
            if code == 200:
                return {"acquired": True, "lease": name, "tookOverFrom": cur if cur and cur != holder else ""}
            if code != 409:
                raise RuntimeError(f"taking lease {name}: HTTP {code} {out.get('message', '')}")
            continue
        if time.time() > deadline:
            raise RuntimeError(f"the border gateway configuration is busy: lease {name} held by {cur}")
        time.sleep(15)


def lease_release(api, req):
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    code, lease = api.call("GET", lease_path(req["namespace"], name))
    if code != 200 or (lease.get("spec") or {}).get("holderIdentity") != holder:
        return {"released": False}
    lease["spec"]["holderIdentity"] = None
    code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
    return {"released": code == 200}
'''


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self.text = json.dumps(body)

    def json(self):
        return json.loads(self.text)


class World:
    """A fake clock and a fake Lease API (resourceVersion checked on PUT and DELETE preconditions).

    time.sleep advances the clock and first runs the events scheduled up to the new time (other jobs' renewals,
    releases, takeovers). fail(n, exc) makes the next n requests raise exc; on_put(fn) runs fn just before the next PUT
    is applied (a concurrent writer). Every request is recorded in calls as (time, method)."""

    def __init__(self, mod):
        self.mod, self.t, self.rv, self.lease = mod, T0, 1, None
        self.events, self.calls, self.failures, self.before_put = [], [], [], []
        self.request_cost = 0.0
        for patcher in (mock.patch.object(mod.time, "time", lambda: self.t),
                        mock.patch.object(mod.time, "sleep", self.sleep),
                        mock.patch.object(mod.requests, "request", self.request)):
            patcher.start()

    def stop(self):
        mock.patch.stopall()

    def at(self, t, fn):
        self.events.append((t, fn))
        self.events.sort(key=lambda e: e[0])

    def sleep(self, seconds):
        end = self.t + seconds
        while self.events and self.events[0][0] <= end:
            t, fn = self.events.pop(0)
            self.t = max(self.t, t)
            fn()
        self.t = end

    def fail(self, n, exc):
        self.failures.extend([exc] * n)

    def on_put(self, fn):
        self.before_put.append(fn)

    def api(self):
        return self.mod.Api({"api": "https://api.example:6443", "token": "t"})

    def put_lease(self, holder, duration, renewed_ago, acquired_ago=None):
        """A lease as some job wrote it, renewed renewed_ago seconds before now."""
        stamp = lambda ago: self.mod.ts(datetime.datetime.fromtimestamp(self.t - ago, datetime.timezone.utc))  # noqa: E731
        self.rv += 1
        self.lease = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                      "metadata": {"name": NAME, "namespace": NS, "uid": "u1", "resourceVersion": str(self.rv)},
                      "spec": {"holderIdentity": holder, "leaseDurationSeconds": duration,
                               "acquireTime": stamp(renewed_ago if acquired_ago is None else acquired_ago),
                               "renewTime": stamp(renewed_ago)}}

    def holder(self):
        return ((self.lease or {}).get("spec") or {}).get("holderIdentity")

    def renew_time(self):
        return self.mod.parse_ts(self.lease["spec"]["renewTime"]).timestamp()

    def request(self, method, url, headers=None, data=None, verify=None, timeout=None):
        self.calls.append((self.t, method, timeout))
        if self.failures:
            exc = self.failures.pop(0)
            if isinstance(exc, (int, float)):           # a connect that times out after `timeout` seconds
                self.t += min(float(exc), float(timeout))
                raise requests.exceptions.ConnectTimeout("connect timed out")
            raise exc
        self.t += self.request_cost
        body = json.loads(data) if data else None
        named = url.rstrip("/").endswith("/leases/" + NAME)
        if method == "GET":
            return _Resp(200, json.loads(json.dumps(self.lease))) if self.lease else _Resp(404, {"message": "not found"})
        if method == "POST":
            if self.lease:
                return _Resp(409, {"message": "exists"})
            self.rv += 1
            body["metadata"].update({"uid": "u%d" % self.rv, "resourceVersion": str(self.rv)})
            self.lease = body
            return _Resp(201, body)
        if method == "PUT" and named:
            while self.before_put:
                self.before_put.pop(0)()
            if not self.lease:
                return _Resp(404, {"message": "not found"})
            if body["metadata"].get("resourceVersion") != self.lease["metadata"]["resourceVersion"]:
                return _Resp(409, {"message": "the object has been modified"})
            self.rv += 1
            body["metadata"]["resourceVersion"] = str(self.rv)
            self.lease = body
            return _Resp(200, body)
        if method == "DELETE" and named:
            if not self.lease:
                return _Resp(404, {"message": "not found"})
            pre = (body or {}).get("preconditions") or {}
            if pre.get("resourceVersion") not in (None, self.lease["metadata"]["resourceVersion"]):
                return _Resp(409, {"message": "precondition failed"})
            self.lease = None
            return _Resp(200, {"status": "Success"})
        return _Resp(400, {"message": "unexpected %s %s" % (method, url)})


class _Case(unittest.TestCase):
    def setUp(self):
        self.mod = load_ledger()
        self.w = World(self.mod)
        self.addCleanup(self.w.stop)

    def req(self, holder, **kw):
        return dict({"fabric": "lab", "namespace": NS, "holder": holder}, **kw)

    def acquire(self, holder, **kw):
        return self.mod.lease_acquire(self.w.api(), self.req(holder, **kw))

    def renew(self, holder, step="the root instance on bgw-2"):
        return self.mod.lease_renew(self.w.api(), self.req(holder, step=step))

    def renewing(self, gaps, holder="A"):
        """holder takes the lease now, renews after each gap in turn (its node steps), then releases it."""
        self.acquire(holder)
        t, log = self.w.t, []
        for gap in gaps:
            t += gap
            self.w.at(t, lambda: log.append(self.renew(holder)))
        self.w.at(t + 1, lambda: log.append(self.mod.lease_release(self.w.api(), self.req(holder))))
        return log

    def old(self):
        """The lease functions of the release before renewals, on the same fake clock and API."""
        ns = {"time": self.mod.time, "datetime": datetime, "now": self.mod.now, "ts": self.mod.ts,
              "lease_path": self.mod.lease_path}
        exec(compile(OLD_LEASE_CODE, "old_shc_ledger_lease", "exec"), ns)  # noqa: S102 - the test's own copy
        return ns


class Renew(_Case):
    def test_the_holder_renews(self):
        self.w.put_lease("A", 600, renewed_ago=100, acquired_ago=300)
        before = json.loads(json.dumps(self.w.lease))
        out = self.renew("A")
        self.assertEqual(out, {"renewed": True, "lease": NAME, "sinceLastRenewal": 100, "lapsed": False,
                               "renewedAt": T0, "duration": 600})
        self.assertEqual(self.w.renew_time(), T0)
        spec, old = self.w.lease["spec"], before["spec"]
        self.assertEqual((spec["holderIdentity"], spec["leaseDurationSeconds"], spec["acquireTime"]),
                         (old["holderIdentity"], old["leaseDurationSeconds"], old["acquireTime"]))
        self.assertNotEqual(self.w.lease["metadata"]["resourceVersion"], before["metadata"]["resourceVersion"])
        self.assertEqual([c[1] for c in self.w.calls], ["GET", "PUT"])

    def test_another_holder_stops_the_job_and_says_why(self):
        self.w.put_lease("CloudGateway/edge1 job 31", 600, renewed_ago=5)
        with self.assertRaises(self.mod.LeaseLost) as cm:
            self.renew("HybridFabric/lab job 30")
        msg = str(cm.exception)
        self.assertIn("taken over by CloudGateway/edge1 job 31", msg)
        self.assertIn("Stopped before the root instance on bgw-2", msg)
        self.assertIn("two jobs never change the border gateway nodes at the same time", msg)
        self.assertEqual([c[1] for c in self.w.calls], ["GET"])
        self.assertEqual(self.w.holder(), "CloudGateway/edge1 job 31")

    def test_a_released_lease_is_not_taken_back(self):
        self.w.put_lease(None, 600, renewed_ago=5)
        with self.assertRaises(self.mod.LeaseLost) as cm:
            self.renew("A")
        self.assertIn("no longer held by this job", str(cm.exception))
        self.assertIsNone(self.w.holder())

    def test_a_deleted_lease_is_lost(self):
        with self.assertRaises(self.mod.LeaseLost) as cm:
            self.renew("A")
        self.assertIn("no longer exists", str(cm.exception))
        self.assertIsNone(self.w.lease)

    def test_expired_and_taken_over(self):
        self.acquire("A", duration=600)
        self.w.sleep(601)
        out = self.acquire("B", duration=600)
        self.assertEqual(out["tookOverFrom"], "A")
        with self.assertRaises(self.mod.LeaseLost) as cm:
            self.renew("A")
        self.assertIn("taken over by B", str(cm.exception))
        self.assertEqual(self.w.holder(), "B")

    def test_expired_but_still_held_is_renewed(self):
        # nobody waited: no other job held it in between, so the holder is still the only one on the nodes
        self.acquire("A", duration=600)
        self.w.sleep(700)
        out = self.renew("A")
        self.assertTrue(out["renewed"])
        self.assertTrue(out["lapsed"])
        self.assertEqual(self.w.holder(), "A")

    def test_a_conflict_with_its_own_earlier_write_is_retried(self):
        # the first PUT was applied but its answer lost: the repeat carries the old resourceVersion
        self.w.put_lease("A", 600, renewed_ago=50)

        def own_write_landed():
            self.w.rv += 1
            self.w.lease["metadata"]["resourceVersion"] = str(self.w.rv)
        self.w.on_put(own_write_landed)
        out = self.renew("A")
        self.assertTrue(out["renewed"])
        self.assertEqual([c[1] for c in self.w.calls], ["GET", "PUT", "GET", "PUT"])

    def test_a_takeover_between_read_and_write_wins(self):
        self.w.put_lease("A", 600, renewed_ago=50)
        self.w.on_put(lambda: self.w.put_lease("B", 600, renewed_ago=0))
        with self.assertRaises(self.mod.LeaseLost) as cm:
            self.renew("A")
        self.assertIn("taken over by B", str(cm.exception))
        self.assertEqual(self.w.holder(), "B")

    def test_a_connection_blip_is_retried(self):
        self.w.put_lease("A", 600, renewed_ago=50)
        self.w.fail(2, requests.exceptions.ConnectionError("Failed to resolve 'api.services'"))
        out = self.renew("A")
        self.assertTrue(out["renewed"])
        self.assertEqual([c[1] for c in self.w.calls], ["GET", "GET", "GET", "PUT"])

    def test_a_lasting_outage_fails_within_the_budget(self):
        self.w.put_lease("A", 600, renewed_ago=50)
        self.w.fail(100, requests.exceptions.ConnectionError("Connection refused"))
        with self.assertRaises(RuntimeError) as cm:
            self.renew("A", step="the wait for the core on bgw-1")
        self.assertNotIsInstance(cm.exception, self.mod.LeaseLost)
        self.assertIn("could not renew the border gateway configuration lease fabric-config-lab: the API did not "
                      "answer within 60 s", str(cm.exception))
        self.assertIn("Stopped before the wait for the core on bgw-1", str(cm.exception))
        self.assertLessEqual(self.w.t - T0, self.mod.RENEW_BUDGET)

    def test_connect_timeouts_stay_within_the_budget(self):
        # every attempt hangs for its whole connect timeout: the timeouts shrink to what is left of the budget
        self.w.put_lease("A", 600, renewed_ago=50)
        self.w.fail(100, 30)
        with self.assertRaises(RuntimeError):
            self.renew("A")
        self.assertLessEqual(self.w.t - T0, self.mod.RENEW_BUDGET)
        self.assertTrue(all(c[2] <= 30 for c in self.w.calls))
        self.assertTrue(all(c[0] + c[2] <= T0 + self.mod.RENEW_BUDGET + 1e-6 for c in self.w.calls))

    def test_the_renewal_budget_is_a_hard_limit(self):
        # requests applies its timeout to the connect and to each read separately; the interval timer ends a
        # renewal at its budget however the requests stall (real time here, the budget cut to 1 s)
        import time as real_time
        self.w.stop()
        self.w.put_lease("A", 600, renewed_ago=50)
        stalls = []

        def stall(method, url, **kw):
            stalls.append(kw.get("timeout"))
            t = real_time.monotonic()
            while real_time.monotonic() - t < 5:      # a read that keeps trickling: no single socket timeout fires
                real_time.sleep(0.05)
            return _Resp(200, {})
        with mock.patch.object(self.mod, "RENEW_BUDGET", 1), mock.patch.object(self.mod.requests, "request", stall):
            start = real_time.monotonic()
            with self.assertRaises(RuntimeError) as cm:
                self.mod.lease_renew(self.mod.Api({"api": "https://a", "token": "t"}), self.req("A", step="s"))
            took = real_time.monotonic() - start
        self.assertLess(took, 2.0)
        self.assertIn("did not answer within 1 s", str(cm.exception))
        self.assertEqual(self.mod.signal.getitimer(self.mod.signal.ITIMER_REAL), (0.0, 0.0))

    def test_a_read_timeout_is_not_repeated(self):
        self.w.put_lease("A", 600, renewed_ago=50)
        self.w.fail(1, requests.exceptions.ReadTimeout("read timed out"))
        with self.assertRaises(RuntimeError) as cm:
            self.renew("A")
        self.assertIn("could not renew", str(cm.exception))
        self.assertEqual(len(self.w.calls), 1)

    def test_an_api_error_stops_the_job(self):
        self.w.put_lease("A", 600, renewed_ago=50)
        with mock.patch.object(self.mod.requests, "request", lambda *a, **k: _Resp(403, {"message": "forbidden"})):
            with self.assertRaises(RuntimeError) as cm:
                self.renew("A")
        self.assertIn("could not renew the border gateway configuration lease fabric-config-lab: reading lease "
                      "fabric-config-lab: HTTP 403 forbidden. Stopped before", str(cm.exception))

    def test_main_reports_a_lost_lease_as_the_job_error(self):
        self.w.put_lease("B", 600, renewed_ago=5)
        req = dict(self.req("A", step="the root instance on bgw-2"), action="lease-renew",
                   api="https://api.example:6443", token="t")
        out = []
        with mock.patch.object(self.mod.sys, "stdin", mock.Mock(read=lambda: json.dumps(req))), \
                mock.patch("builtins.print", out.append):
            rc = self.mod.main()
        self.assertEqual(rc, 1)
        self.assertIn("taken over by B", json.loads(out[0])["error"])


class Takeover(_Case):
    """The new numbers on the fake clock: a waiting job takes over a dead holder's lease, never a live one's."""

    def test_a_waiter_takes_over_a_dead_holder_after_the_duration(self):
        self.acquire("A")                     # and A dies at once
        self.w.sleep(1)
        out = self.acquire("B")
        self.assertEqual(out["tookOverFrom"], "A")
        waited = self.w.t - T0
        self.assertEqual(out["notRenewedFor"], round(waited))
        self.assertGreater(waited, self.mod.LEASE_DURATION)
        self.assertLessEqual(waited, self.mod.LEASE_DURATION + self.mod.LEASE_POLL + 1)

    def test_a_waiter_outlasts_any_dead_holder(self):
        # the holder's last renewal came any time up to the moment the waiter started: the waiter still gets it
        for ago in (0, 1, 200, 599, 600, 601, 1000):
            with self.subTest(last_renewal_ago=ago):
                self.w.lease, self.w.calls = None, []
                self.w.put_lease("A", self.mod.LEASE_DURATION, renewed_ago=ago)
                start = self.w.t
                self.assertEqual(self.acquire("B")["tookOverFrom"], "A")
                self.assertLess(self.w.t - start, self.mod.LEASE_WAIT)

    def test_a_live_holder_keeps_the_lease_at_the_longest_gap(self):
        # the worst gaps between two renewals: a failing wait for the core with one hung attempt and slow renewals
        # (501 s), a write stretch with a hung step (346 s), and the recorded ones (201, 46, 25, 22 s)
        gaps = [46, 501, 22, 25, 346, 201, 22]
        log = self.renewing(gaps)
        self.w.sleep(1)
        out = self.acquire("B")
        self.assertTrue(all(r.get("renewed") for r in log[:-1]), log)
        self.assertEqual(log[-1], {"released": True})
        self.assertEqual(out["tookOverFrom"], "")
        self.assertGreaterEqual(self.w.t - T0, sum(gaps))

    def test_a_holder_that_stops_renewing_loses_the_lease_and_stops(self):
        # A renews once, then stalls past the duration; B, waiting, takes over; A's late renewal stops A
        d, results = self.mod.LEASE_DURATION, []

        def renew_a():
            try:
                results.append(self.renew("A"))
            except self.mod.LeaseLost as e:
                results.append(str(e))
        self.acquire("A")
        self.w.at(T0 + 46, renew_a)
        self.w.at(T0 + 46 + d + 30, renew_a)
        self.w.sleep(1)
        out = self.acquire("B")
        self.assertEqual(out["tookOverFrom"], "A")
        self.assertGreater(self.w.t - T0, 46 + d)
        self.assertLessEqual(self.w.t - T0, 46 + d + self.mod.LEASE_POLL + 1)
        self.w.sleep(60)
        self.assertTrue(results[0]["renewed"])
        self.assertIn("taken over by B", results[1])
        self.assertEqual(self.w.holder(), "B")

    def test_a_busy_lease_gives_up_after_the_wait_with_a_reason(self):
        self.renewing([300] * 10)
        with self.assertRaises(RuntimeError) as cm:
            self.acquire("B")
        self.assertIn("the border gateway configuration is busy: lease fabric-config-lab held by A", str(cm.exception))
        self.assertIn("gave up after waiting 1200 s", str(cm.exception))
        self.assertGreaterEqual(self.w.t - T0, self.mod.LEASE_WAIT)


class Limits(_Case):
    def test_the_defaults_are_accepted(self):
        self.assertEqual(self.mod.lease_limits({}), (600, 1200))
        self.acquire("A")
        self.assertEqual(self.w.lease["spec"]["leaseDurationSeconds"], 600)

    def test_a_duration_a_node_step_could_outlive_is_refused(self):
        # right after a renewal (up to the budget), the lease must outlast the longest work of one step on a node
        # (the step timeout or a systemd job it starts) plus the margin: max(180, 360) + 60 + 60 = 480
        with self.assertRaises(RuntimeError) as cm:
            self.acquire("A", duration=479)
        self.assertIn("at least 480", str(cm.exception))
        self.assertIsNone(self.w.lease)
        self.assertEqual(self.mod.lease_limits({"duration": 480, "wait": 496}), (480, 496))
        with self.assertRaises(RuntimeError) as cm:
            self.mod.lease_limits({"duration": 600, "stepTimeout": 500, "wait": 1200})
        self.assertIn("at least 620", str(cm.exception))

    def test_a_wait_that_ends_before_the_duration_is_refused(self):
        for wait in (900, 915):
            with self.subTest(wait=wait), self.assertRaises(RuntimeError) as cm:
                self.acquire("A", duration=900, wait=wait)
            self.assertIn("must be longer than its duration", str(cm.exception))

    def test_only_positive_whole_seconds_are_accepted(self):
        # 0, null, a fraction or text would turn the step timeout off (or quietly into the default): refused
        for key, value in (("stepTimeout", 0), ("stepTimeout", None), ("stepTimeout", "3m"), ("stepTimeout", 1.5),
                           ("stepTimeout", True), ("duration", 0), ("wait", 0), ("systemdJob", -1), ("duration", None)):
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError) as cm:
                self.mod.lease_limits({key: value})
            self.assertIn("must be a positive whole number of seconds", str(cm.exception))
            self.assertIn(self.mod.LEASE_KEYS[key][0], str(cm.exception))
        self.assertEqual(self.mod.lease_limits({"duration": "700", "wait": 1300}), (700, 1300))

    def test_acquire_and_renew_report_when_they_renewed(self):
        out = self.acquire("A")
        self.assertEqual((out["renewedAt"], out["duration"]), (T0, 600))
        self.w.sleep(100)
        out = self.renew("A")
        self.assertEqual((out["renewedAt"], out["duration"]), (T0 + 100, 600))


class Upgrade(_Case):
    """A rollout meets leases written by the other version."""

    def test_a_new_job_keeps_an_old_style_lease_for_its_1800_s(self):
        # the old code wrote 1800 s and never renews; the new defaults (600 s) must not shorten it
        self.w.put_lease("HybridFabric/lab job 27004", 1800, renewed_ago=60)
        with self.assertRaises(RuntimeError) as cm:
            self.acquire("B")
        self.assertIn("busy", str(cm.exception))
        self.assertEqual(self.w.holder(), "HybridFabric/lab job 27004")
        # a job that starts later takes it over once those 1800 s have passed
        self.w.put_lease("HybridFabric/lab job 27004", 1800, renewed_ago=700)
        start = self.w.t
        self.assertEqual(self.acquire("C")["tookOverFrom"], "HybridFabric/lab job 27004")
        self.assertGreater(self.w.t - start, 1800 - 700)
        self.assertLessEqual(self.w.t - start, 1800 - 700 + self.mod.LEASE_POLL + 1)

    def test_an_old_job_waits_for_a_renewing_new_holder(self):
        old = self.old()
        log = self.renewing([201, 201, 201, 201])
        out = old["lease_acquire"](self.w.api(), self.req("OldJob"))
        self.assertTrue(all(r.get("renewed") for r in log[:-1]))
        self.assertEqual(out["tookOverFrom"], "")
        self.assertGreaterEqual(self.w.t - T0, 4 * 201)
        self.assertEqual(self.w.lease["spec"]["leaseDurationSeconds"], 1800)

    def test_an_old_job_takes_over_a_dead_new_holder_after_the_new_duration(self):
        old = self.old()
        self.acquire("A")
        out = old["lease_acquire"](self.w.api(), self.req("OldJob"))
        self.assertEqual(out["tookOverFrom"], "A")
        self.assertLessEqual(self.w.t - T0, self.mod.LEASE_DURATION + 16)
        # the new holder, had it only stalled, stops at its next renewal
        with self.assertRaises(self.mod.LeaseLost):
            self.renew("A")

    def test_an_old_job_never_frees_a_new_holders_lease(self):
        old = self.old()
        self.acquire("A")
        self.assertEqual(old["lease_release"](self.w.api(), self.req("OldJob")), {"released": False})
        self.assertEqual(self.w.holder(), "A")

    def test_a_new_job_takes_a_lease_an_old_job_released(self):
        old = self.old()
        old["lease_acquire"](self.w.api(), self.req("OldJob"))
        old["lease_release"](self.w.api(), self.req("OldJob"))
        self.assertEqual(self.acquire("A")["tookOverFrom"], "")
        self.assertEqual(self.w.lease["spec"]["leaseDurationSeconds"], 600)


class Release(_Case):
    def test_delete_only_while_held(self):
        self.w.put_lease("B", 600, renewed_ago=5)
        out = self.mod.lease_release(self.w.api(), self.req("A", delete=True))
        self.assertEqual(out, {"released": False})
        self.assertEqual(self.w.holder(), "B")
        self.w.put_lease("A", 600, renewed_ago=5)
        out = self.mod.lease_release(self.w.api(), self.req("A", delete=True))
        self.assertEqual(out, {"released": True, "deleted": True})
        self.assertIsNone(self.w.lease)

    def test_delete_does_not_remove_a_lease_taken_over_meanwhile(self):
        self.w.put_lease("A", 600, renewed_ago=5)
        real = self.w.request

        def takeover_before_delete(method, url, **kw):
            if method == "DELETE":
                self.w.put_lease("B", 600, renewed_ago=0)
            return real(method, url, **kw)
        with mock.patch.object(self.mod.requests, "request", takeover_before_delete):
            out = self.mod.lease_release(self.w.api(), self.req("A", delete=True))
        self.assertFalse(out["released"])
        self.assertEqual(self.w.holder(), "B")


# ---- the holders' task files ----

HOLDERS = {
    # role: (the file the lease is taken in, the node loop's task name, its node file, the holder variable, the fabric)
    "hybridfabric_provision": ("main.yml", "Configure the root instance on each node", "node_root.yml",
                               "hf_holder", "fab_name"),
    "cloudgateway_provision": ("main.yml", "Configure the gateway instance on each node", "node_gateway.yml",
                               "cg_holder", "cg_fabric_name"),
    "cloudgateway_teardown": ("main.yml", "Remove the gateway instance from each node", "node_gateway_down.yml",
                              "cgt_holder", "cg_fabric_name"),
    "hybridfabric_teardown": ("main.yml", "Remove the root instance from each node", "node_root_down.yml",
                              "hft_holder", "fab_name"),
}
# the block around each holder's node loop, which carries the step limit
LOOP_BLOCKS = {"hybridfabric_provision": "Configure the nodes, one at a time",
               "cloudgateway_provision": "Configure the gateway on the nodes, one at a time",
               "cloudgateway_teardown": "One node at a time",
               "hybridfabric_teardown": "Remove the root instance, one node at a time"}
# the long waits inside a node: a renewal right before and right after each
LONG_WAITS = {
    "node_root.yml": "wait for BGP and IS-IS",
    "node_gateway.yml": "wait for the core (IS-IS, VPNv4 and VPNv6) and the upstream route reflectors",
}
# roles that reach the nodes outside the lease: reads only
READ_ONLY = {"attachmentcircuit_provision", "networkplacement_provision", "fabric_orphan_check"}
STEP_TIMEOUT = "{{ fab_lease_step_timeout }}"


def _yaml_files(base):
    for d, _, files in os.walk(base):
        for f in files:
            if f.endswith((".yml", ".yaml")):
                yield os.path.join(d, f)


def _tasks(path):
    """Every task of a task file (nothing for a file that is no task list)."""
    try:
        doc = load(path)
    except Exception:  # noqa: BLE001 - not a single YAML document: no task list
        return []
    return list(walk(doc)) if isinstance(doc, list) else []


def _renew(task):
    inc = task.get("ansible.builtin.include_tasks")
    return (isinstance(inc, str) and inc.endswith("common/tasks/fabric_ledger.yml")
            and ((task.get("vars") or {}).get("fab_ledger") or {}).get("action") == "lease-renew")


def _node_name(task):
    return re.sub(r"^\{\{ bgw_node\.name \}\}: ", "", task.get("name", ""))


class HolderWiring(unittest.TestCase):
    def test_the_holders_are_the_ones_that_take_the_lease(self):
        found = set()
        for path in _yaml_files(os.path.join(ROOT, "eda")):
            for t in _tasks(path):
                led = (t.get("vars") or {}).get("fab_ledger") or {}
                if isinstance(led, dict) and led.get("action") == "lease-acquire":
                    found.add(os.path.relpath(path, ROLES).split(os.sep)[0])
        self.assertEqual(found, set(HOLDERS))

    def test_each_node_loop_runs_within_the_lease(self):
        # the step limit is a keyword of the block around the loop (evaluated for every step on both ansible-core
        # 2.16 and later; an include's apply is templated once on later versions); the include and every task of the
        # block's rescue and always set their own timeout, so the renewals, the SSH cleanup and the release still run
        for role, (main, loop, node_file, _, _) in HOLDERS.items():
            with self.subTest(role=role):
                tasks = load(os.path.join(ROLES, role, "tasks", main))
                block = by_name(tasks, LOOP_BLOCKS[role])
                self.assertEqual(block["timeout"], STEP_TIMEOUT)
                inc = by_name(block["block"], loop)
                self.assertEqual(inc["ansible.builtin.include_tasks"], node_file)
                self.assertEqual(inc["timeout"], 0)
                self.assertEqual(inc["loop_control"]["loop_var"], "bgw_node")
                for t in (block.get("rescue") or []) + block["always"]:
                    self.assertEqual(t.get("timeout"), 0, t.get("name"))

    def test_a_timed_out_step_keeps_the_lease(self):
        keep = "{{ role_path }}/../../../common/tasks/fabric_lease_keep.yml"
        for role in HOLDERS:
            with self.subTest(role=role):
                tasks = load(os.path.join(ROLES, role, "tasks/main.yml"))
                block = by_name(tasks, LOOP_BLOCKS[role])
                self.assertIn("Remove the SSH key", [x.get("name") for x in block["always"]])
                if role == "hybridfabric_teardown":
                    # the release is in the role's rescue: the keep decision comes first there, without re-raising
                    rescue = [x for x in tasks[0]["rescue"]]
                    names = [x.get("name") for x in rescue]
                    self.assertEqual(rescue[0]["ansible.builtin.include_tasks"], keep)
                    self.assertIs(rescue[0]["vars"]["fab_lease_keep_raise"], False)
                    self.assertLess(0, names.index("Release the lease"))
                    self.assertNotIn("rescue", block)
                else:
                    self.assertEqual([x.get("ansible.builtin.include_tasks") for x in block["rescue"]], [keep])

    def test_the_renewals_set_their_own_timeout(self):
        # inside the node loops every task inherits the step limit; the renewals must still run when it is short
        for role, (_, _, node_file, _, _) in HOLDERS.items():
            tasks = load(os.path.join(ROLES, role, "tasks", node_file))
            for t in tasks:
                inc = str(t.get("ansible.builtin.include_tasks") or "")
                if inc.endswith(("fabric_ledger.yml", "fabric_lease_ensure.yml")):
                    with self.subTest(role=role, task=t["name"]):
                        self.assertEqual(t.get("timeout"), 0)
        for common in ("fabric_ledger.yml", "fabric_lease_keep.yml", "fabric_lease_ensure.yml", "bgw_disconnect.yml"):
            for t in load(os.path.join(ROOT, "eda/common/tasks", common)):
                with self.subTest(file=common, task=t.get("name")):
                    self.assertIn("timeout", t)

    def test_long_steps_renew_first_when_the_lease_is_short(self):
        # every systemd start, restart or reload (not a timer, not enable/disable alone) and every apply up comes right
        # after a renewal or a renew-if-short check, at any depth of blocks
        def lists(tasks):
            yield tasks
            for t in tasks:
                for key in ("block", "rescue", "always"):
                    if t.get(key):
                        yield from lists(t[key])
        for role, (_, _, node_file, _, _) in HOLDERS.items():
            for tasks in lists(load(os.path.join(ROLES, role, "tasks", node_file))):
                for i, t in enumerate(tasks):
                    sd = t.get("ansible.builtin.systemd") or {}
                    cmd = str(t.get("ansible.builtin.command") or "")
                    long_step = (sd.get("state") and not str(sd.get("name", "")).endswith(".timer")) or \
                        re.search(r"shc-fabric-apply .+ up$", cmd.strip())
                    if not long_step:
                        continue
                    with self.subTest(role=role, task=t["name"]):
                        self.assertGreater(i, 0, "a long step first in its block")
                        self.assertTrue(_renew(tasks[i - 1]) or str(tasks[i - 1].get("ansible.builtin.include_tasks", ""))
                                        .endswith("fabric_lease_ensure.yml"), tasks[i - 1].get("name"))

    def test_the_applies_end_on_the_node_before_the_step(self):
        found = []
        for role, node_file in (("hybridfabric_provision", "node_root.yml"), ("cloudgateway_provision", "node_gateway.yml")):
            for t in walk(load(os.path.join(ROLES, role, "tasks", node_file))):
                cmd = str(t.get("ansible.builtin.command") or "")
                if re.search(r"shc-fabric-apply .+ up$", cmd.strip()):
                    found.append(t["name"])
                    with self.subTest(task=t["name"]):
                        self.assertTrue(cmd.startswith("timeout -k 10 {{ [fab_lease_step_timeout | int - 15, 15] | max }} "))
        self.assertGreaterEqual(len(found), 2, found)

    def test_each_node_starts_with_a_renewal_by_its_holder(self):
        for role, (_, _, node_file, holder, fabric) in HOLDERS.items():
            with self.subTest(role=role):
                tasks = load(os.path.join(ROLES, role, "tasks", node_file))
                first = tasks[0]
                self.assertTrue(_renew(first), first)
                led = first["vars"]["fab_ledger"]
                self.assertEqual(led["holder"], "{{ %s }}" % holder)
                self.assertEqual(led["fabric"], "{{ %s }}" % fabric)
                self.assertEqual(led["namespace"], "{{ fab_namespace }}")
                self.assertIn("{{ bgw_node.name }}", led["step"])

    def test_the_long_waits_are_renewed_before_and_after(self):
        for role, (_, _, node_file, holder, _) in HOLDERS.items():
            tasks = load(os.path.join(ROLES, role, "tasks", node_file))
            names = [_node_name(t) for t in tasks]
            renews = [i for i, t in enumerate(tasks) if _renew(t)]
            wait = LONG_WAITS.get(node_file)
            with self.subTest(role=role):
                if wait is None:
                    self.assertEqual(renews, [0])
                    continue
                i = names.index(wait)
                self.assertEqual(renews, [0, i - 1, i + 1])
                self.assertTrue(all(tasks[j]["vars"]["fab_ledger"]["holder"] == "{{ %s }}" % holder for j in renews))
                self.assertGreater(int(tasks[i]["retries"]), 0)

    def test_the_lease_is_still_released_in_always(self):
        for role in ("hybridfabric_provision", "cloudgateway_provision", "cloudgateway_teardown"):
            with self.subTest(role=role):
                tasks = load(os.path.join(ROLES, role, "tasks/main.yml"))
                blocks = [t for t in walk(tasks) if t.get("always")
                          and any(_t.get("name") == HOLDERS[role][1] for _t in walk(t.get("block")))]
                self.assertEqual(len(blocks), 1)
                self.assertIn("Release the lease", [t.get("name") for t in blocks[0]["always"]])

    def test_the_fabric_teardown_deletes_only_a_lease_it_holds(self):
        tasks = load(os.path.join(ROLES, "hybridfabric_teardown/tasks/main.yml"))
        step = by_name(tasks, "Delete the configuration lease")
        self.assertEqual(step["vars"]["fab_ledger"]["action"], "lease-release")
        self.assertIs(step["vars"]["fab_ledger"]["delete"], True)
        for path in _yaml_files(os.path.join(ROOT, "eda")):
            for t in _tasks(path):
                k8s = t.get("kubernetes.core.k8s") or {}
                self.assertNotEqual(k8s.get("kind"), "Lease", path)

    def test_only_reads_reach_the_nodes_outside_the_lease(self):
        connect = set()
        for path in _yaml_files(ROLES):
            text = _read(path)
            if "common/tasks/bgw_connect.yml" in text:
                connect.add(os.path.relpath(path, ROLES).split(os.sep)[0])
        self.assertEqual(connect - set(HOLDERS), READ_ONLY)
        for role in READ_ONLY:
            for path in _yaml_files(os.path.join(ROLES, role)):
                for t in _tasks(path):
                    if t.get("delegate_to") and not str(t.get("delegate_to")).startswith("localhost"):
                        with self.subTest(path=os.path.relpath(path, ROOT), task=t.get("name")):
                            self.assertIs(t.get("changed_when"), False)
                            self.assertTrue(set(t) & {"ansible.builtin.shell", "ansible.builtin.command"})


class Numbers(unittest.TestCase):
    def setUp(self):
        self.mod = load_ledger()
        real = load(os.path.join(ROOT, "eda/common/vars/fabric_realization.yml"))["fabric_realization_defaults"]
        self.cfg = real["nodes"]["configLease"]
        self.limits = load(os.path.join(ROOT, "eda/common/vars/fabric_lease.yml"))

    def test_the_realization_and_the_helper_agree(self):
        self.assertEqual(self.cfg, {"durationSeconds": self.mod.LEASE_DURATION, "waitSeconds": self.mod.LEASE_WAIT,
                                    "stepTimeoutSeconds": self.mod.LEASE_STEP_TIMEOUT,
                                    "systemdJobSeconds": self.mod.LEASE_SYSTEMD_JOB})
        self.assertEqual(self.limits["fab_lease_margin"], self.mod.LEASE_MARGIN)
        self.mod.lease_limits({k: self.cfg[v[0]] for k, v in self.mod.LEASE_KEYS.items()})

    def test_a_long_step_right_after_a_renewal_never_needs_another(self):
        # renew-if-short asks for max(step, systemd job) + margin; a renewal (up to its budget) leaves duration - budget
        d, t, u = self.cfg["durationSeconds"], self.cfg["stepTimeoutSeconds"], self.cfg["systemdJobSeconds"]
        self.assertLessEqual(max(t, u) + self.mod.LEASE_MARGIN, d - self.mod.RENEW_BUDGET)

    def test_the_systemd_job_bound_covers_the_units(self):
        # TimeoutSec=2m: a restart's stop up to 2 x 2 min (the stop command, then SIGTERM before SIGKILL) and its start
        # 2 min; a gateway reload with its one retry 2 x 2 min + 5 s; the root unit's oneshot start 5 min
        unit = _read(os.path.join(ROOT, "eda/common/files/bgw/shc-gw@.service"))
        self.assertIn("TimeoutSec=2m", unit)
        root = _read(os.path.join(ROOT, "eda/common/files/bgw/shc-fabric-apply@.service"))
        self.assertIn("TimeoutStartSec=5min", root)
        self.assertGreaterEqual(self.cfg["systemdJobSeconds"], max(2 * 120 + 120, 2 * 120 + 5, 300))

    def test_the_wait_outlasts_the_duration(self):
        self.assertGreater(self.cfg["waitSeconds"], self.cfg["durationSeconds"] + self.mod.LEASE_POLL)

    def test_a_waiting_job_stays_within_the_operators_in_flight_limit(self):
        # the operator replaces a fabric or gateway job 30 min after its start (skip_if_event_in_flight.yml); a job
        # that waits the whole lease wait must still end before that: the recorded work outside the wait (before the
        # lease 252 s, holding it 225 s, after it 59 s) rounds to 600 s
        text = _read(os.path.join(ROOT, "operator/roles/_common/tasks/skip_if_event_in_flight.yml"))
        overrides = re.search(r"in_flight_job_timeout_minutes \| default\((\{[^}]*\})\)", text).group(1)
        self.assertNotIn("HybridFabric", overrides)
        self.assertNotIn("CloudGateway", overrides)
        limit = int(re.search(r"timeouts\.get\(in_flight_kind, (\d+)\)", text).group(1)) * 60
        for role in ("hybridfabric", "cloudgateway"):
            role_text = _read(os.path.join(ROOT, "operator/primary/roles", role, "tasks/main.yml"))
            self.assertNotIn("in_flight_job_timeout_minutes", role_text)
        self.assertLessEqual(self.cfg["waitSeconds"] + 600, limit)


FAKE_LEDGER = textwrap.dedent('''\
    import json, sys, time
    req = json.load(sys.stdin)
    req.pop("token", None)
    with open(__file__ + ".calls", "a") as f:
        f.write(req["action"] + "\\n")
    if req["action"] == "lease-renew" and req["holder"] == "lost":
        print(json.dumps({"error": "the border gateway configuration lease fabric-config-lab was taken over by B"}))
        sys.exit(1)
    out = {"echo": req}
    if req["action"] in ("lease-acquire", "lease-renew"):
        out.update(renewedAt=time.time(), duration=req.get("duration", 600) if req["action"] == "lease-acquire" else 600)
        if req["holder"] == "lapsed":
            out.update(lapsed=True, sinceLastRenewal=700)
    if req["action"] == "lease-release":
        out["released"] = True
    print(json.dumps(out))
    ''')


class _Ledger(unittest.TestCase):
    """fabric_ledger.yml and the lease task files in ansible-playbook, with the helper replaced by one that logs the
    actions it got (to <helper>.calls) and answers with the request."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="shc-lease-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "common/files"))
        os.makedirs(os.path.join(self.tmp, "r/o/l"))
        # the lease task files and vars as they are, the helper replaced
        shutil.copytree(os.path.join(ROOT, "eda/common/tasks"), os.path.join(self.tmp, "common/tasks"))
        shutil.copytree(os.path.join(ROOT, "eda/common/vars"), os.path.join(self.tmp, "common/vars"))
        self.helper = os.path.join(self.tmp, "common/files/shc_ledger.py")
        with open(self.helper, "w") as f:
            f.write(FAKE_LEDGER)
        self.real = load(os.path.join(ROOT, "eda/common/vars/fabric_realization.yml"))["fabric_realization_defaults"]

    def calls(self):
        return _read(self.helper + ".calls").split() if os.path.exists(self.helper + ".calls") else []

    def include(self, name, **vars_):
        t = {"name": name, "ansible.builtin.include_tasks": os.path.join(self.tmp, "common/tasks", name)}
        if vars_:
            t["vars"] = vars_
        return t

    def run_tasks(self, tasks, extra=None):
        v = {"role_path": os.path.join(self.tmp, "r/o/l"), "services_api_host": "https://api.example:6443",
             "services_bearer_token": "t", "fab_real": self.real, "fab_namespace": NS}
        v.update(extra or {})
        return run_play(tasks, v)


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class LedgerTask(_Ledger):
    def ledger(self, fab_ledger, fab_real=None, then="fab_ledger_result"):
        play = [{"name": "the ledger task", "block": [self.include("fabric_ledger.yml", fab_ledger=fab_ledger)],
                 "rescue": [{"ansible.builtin.set_fact": {"fab_ledger_result": {"failed": "{{ ansible_failed_result.msg }}"}}}]},
                write_out(then)]
        return self.run_tasks(play, None if fab_real is None else {"fab_real": fab_real})

    def test_an_acquire_carries_the_realizations_lease(self):
        out = self.ledger({"action": "lease-acquire", "namespace": NS, "fabric": "lab", "holder": "A"})
        self.assertEqual({k: out["echo"].get(k) for k in ("duration", "wait", "stepTimeout", "systemdJob")},
                         {"duration": 600, "wait": 1200, "stepTimeout": 180, "systemdJob": 360})

    def test_an_installations_value_is_passed_as_it_stands_and_explicit_keys_win(self):
        # passed raw, so the helper refuses what the step limit could not use (0, null, text), never a default
        real = {"nodes": {"configLease": {"durationSeconds": 900, "waitSeconds": 1500, "stepTimeoutSeconds": None,
                                          "systemdJobSeconds": "3m"}}}
        out = self.ledger({"action": "lease-acquire", "namespace": NS, "fabric": "lab", "holder": "A", "wait": 1600}, real)
        self.assertEqual({k: out["echo"].get(k, "absent") for k in ("duration", "wait", "stepTimeout", "systemdJob")},
                         {"duration": 900, "wait": 1600, "stepTimeout": None, "systemdJob": "3m"})

    def test_other_actions_carry_no_lease_numbers(self):
        out = self.ledger({"action": "lease-renew", "namespace": NS, "fabric": "lab", "holder": "A", "step": "s"})
        self.assertFalse({"duration", "wait", "stepTimeout", "systemdJob"} & set(out["echo"]))

    def test_a_lost_lease_fails_the_task_with_the_helpers_reason(self):
        out = self.ledger({"action": "lease-renew", "namespace": NS, "fabric": "lab", "holder": "lost", "step": "s"})
        self.assertEqual(out["failed"], "Fabric lease-renew failed: the border gateway configuration lease "
                                        "fabric-config-lab was taken over by B")

    def test_the_job_tracks_its_lease_from_each_renewal(self):
        import time
        before = time.time()
        out = self.ledger({"action": "lease-acquire", "namespace": NS, "fabric": "lab", "holder": "A"},
                          then="{'lease': fab_lease, 'left': fab_lease_left | int, 'step': fab_lease_step_timeout | int, "
                               "'need': fab_lease_need | int}")
        # 2.16 keeps a templated value inside a dict as text: every use converts it (| int, | float)
        self.assertEqual((out["lease"]["fabric"], out["lease"]["holder"], int(out["lease"]["duration"])), ("lab", "A", 600))
        self.assertGreaterEqual(float(out["lease"]["until"]), before + 600)
        self.assertGreater(out["left"], 590)
        self.assertEqual((out["step"], out["need"]), (180, 420))

    def test_a_kept_lease_is_not_released(self):
        play = [{"ansible.builtin.set_fact": {"fab_lease_keep": True}},
                self.include("fabric_ledger.yml", fab_ledger={"action": "lease-release", "namespace": NS,
                                                              "fabric": "lab", "holder": "A"}),
                write_out("fab_ledger_result")]
        self.assertEqual(self.run_tasks(play), {"released": False, "kept": True})
        self.assertEqual(self.calls(), [])

    def test_a_lapsed_renewal_is_shown(self):
        play = [self.include("fabric_ledger.yml", fab_ledger={"action": "lease-renew", "namespace": NS, "fabric": "lab",
                                                              "holder": "lapsed", "step": "s"}),
                write_out("_fab_lease_lapsed_note.msg")]
        self.assertIn("[WARNING] lease fabric-config-lab was renewed 700 s after its previous renewal, longer than its "
                      "600 s; nobody had taken it over", self.run_tasks(play))


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class StepLimit(_Ledger):
    """common/vars/fabric_lease.yml: every node step's timeout is the step timeout cut to what the lease has left less
    the margin, and a step with less than the minimum left does not start; a block keyword, evaluated per step."""

    def limits(self, left):
        play = [{"ansible.builtin.include_vars": os.path.join(self.tmp, "common/vars/fabric_lease.yml")},
                {"ansible.builtin.set_fact": {"fab_lease": {"fabric": "lab", "until": "{{ now().timestamp() + %d }}" % left}}},
                write_out("{'step': fab_lease_step_timeout, 'need': fab_lease_need | int}")]
        return self.run_tasks(play)

    def test_the_step_timeout_follows_the_lease(self):
        self.assertEqual(str(self.limits(600)["step"]), "180")
        self.assertIn(str(self.limits(200)["step"]), ("139", "140"))   # 200 - 60, the template runs a moment later
        self.assertIn(str(self.limits(95)["step"]), ("34", "35"))
        self.assertIn("too little for this step", str(self.limits(85)["step"]))
        self.assertEqual(self.limits(600)["need"], 420)

    def test_a_step_with_too_little_lease_does_not_start(self):
        marker = os.path.join(self.tmp, "ran")
        play = [{"ansible.builtin.include_vars": os.path.join(self.tmp, "common/vars/fabric_lease.yml")},
                {"ansible.builtin.set_fact": {"fab_lease": {"fabric": "lab", "until": "{{ now().timestamp() + 80 }}"}}},
                {"name": "a node loop", "block": [{"name": "the steps", "timeout": "{{ fab_lease_step_timeout }}",
                                                   "block": [{"name": "a node step",
                                                              "ansible.builtin.command": "touch %s" % marker}]}],
                 "rescue": [{"ansible.builtin.set_fact": {"_why": "{{ ansible_failed_result.msg }}"}}]},
                write_out("_why")]
        why = self.run_tasks(play)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("too little for this step", why)


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class FailureClasses(_Ledger):
    """A holder's node loop as the four holders have it (guarded steps, the keep-lease rescue, the always block that
    ends the SSH sessions and releases): which failures release the lease at once, and which keep it."""

    def loop(self, step, left=600):
        import subprocess
        keydir = os.path.join(self.tmp, "keys.bgw")
        os.makedirs(keydir)
        fake = os.path.join(self.tmp, "ssh")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\ntrap "exit 0" TERM\nwhile :; do sleep 1; done\n')
        os.chmod(fake, 0o755)
        # this job's ssh client of a step that timed out, still running: its command line names the key directory
        ssh = subprocess.Popen([fake, "-o", "UserKnownHostsFile=%s/known_hosts" % keydir])
        self.addCleanup(lambda: ssh.poll() is None and ssh.kill())
        release = {"action": "lease-release", "namespace": NS, "fabric": "lab", "holder": "A"}
        play = [{"ansible.builtin.include_vars": os.path.join(self.tmp, "common/vars/fabric_lease.yml")},
                {"ansible.builtin.set_fact": {"fab_lease": {"namespace": NS, "fabric": "lab", "holder": "A",
                                                            "until": "{{ now().timestamp() + %d }}" % left},
                                              "bgw_keydir": {"path": keydir}}},
                {"name": "the job", "block": [
                    {"name": "Configure the nodes, one at a time", "timeout": "{{ fab_lease_step_timeout }}",
                     "block": [dict(step, name="bgw-1: the step")],
                     "rescue": [dict(self.include("fabric_lease_keep.yml"), timeout=0)],
                     "always": [dict(self.include("bgw_disconnect.yml"), timeout=0),
                                dict(self.include("fabric_ledger.yml", fab_ledger=release), timeout=0)]}],
                 "rescue": [{"ansible.builtin.set_fact": {"_why": "{{ ansible_failed_result.msg }}"}}]},
                write_out("{'why': _why | default(''), 'kept': fab_lease_keep | default(false), "
                          "'result': fab_ledger_result | default({})}")]
        out = self.run_tasks(play, {"fab_real": dict(self.real, nodes=dict(self.real["nodes"],
                                                                     configLease=dict(self.real["nodes"]["configLease"],
                                                                                      stepTimeoutSeconds=2)))})
        ssh.wait(timeout=10)
        out["sshEnded"] = ssh.returncode is not None
        out["keydirGone"] = not os.path.exists(keydir)
        return out

    def test_a_timed_out_step_keeps_the_lease(self):
        out = self.loop({"ansible.builtin.command": "sleep 10"})
        self.assertTrue(out["kept"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(out["result"], {"released": False, "kept": True})
        self.assertIn("bgw-1: the step: ", out["why"])
        self.assertIn("The step may still be running there: the border gateway configuration lease "
                      "fabric-config-lab is kept until it runs out at ", out["why"])
        self.assertTrue(out["sshEnded"])
        self.assertTrue(out["keydirGone"])

    def test_a_timeout_is_recognised_as_each_ansible_core_reports_it(self):
        # 2.19 and later: result.timedout; 2.16: only the message (no timedout key)
        for msg in ("The ansible.builtin.shell action failed to execute in the expected time frame (5) and was terminated",):
            with self.subTest(msg=msg):
                out = self.loop({"ansible.builtin.fail": {"msg": msg}})
                self.assertTrue(out["kept"])
                self.assertIn("is kept until it runs out at ", out["why"])
        self.assertEqual(self.calls(), [])

    def test_a_timed_out_item_of_a_looped_step_keeps_the_lease(self):
        # a looped step reports only "One or more items failed"; the timed-out item is in the results (marked by
        # timedout on 2.19 and later, by the message on 2.16: this test runs the version installed, the next one fakes
        # the 2.16 rendering)
        out = self.loop({"ansible.builtin.command": "sleep {{ item }}", "loop": [0, 10],
                         "loop_control": {"label": "the item of {{ item }} s"}})
        self.assertTrue(out["kept"])
        self.assertEqual(self.calls(), [])
        self.assertIn("One or more items failed (item the item of 10 s: ", out["why"])
        self.assertIn("is kept until it runs out at ", out["why"])
        self.assertTrue(out["sshEnded"])

    def test_a_looped_timeout_in_the_2_16_rendering_keeps_the_lease(self):
        msg = "The ansible.builtin.copy action failed to execute in the expected time frame (2) and was terminated"
        out = self.loop({"ansible.builtin.fail": {"msg": "{{ item }}"}, "loop": [msg]})
        self.assertTrue(out["kept"])
        self.assertEqual(self.calls(), [])
        self.assertIn("One or more items failed (item %s: %s)" % (msg, msg), out["why"])

    def test_a_looped_step_with_an_ordinary_error_releases_at_once(self):
        out = self.loop({"ansible.builtin.command": "{{ item }}", "loop": ["true", "false"]})
        self.assertFalse(out["kept"])
        self.assertEqual(self.calls(), ["lease-release"])
        self.assertNotIn("kept until", out["why"])

    def test_a_step_that_returned_an_error_releases_at_once(self):
        out = self.loop({"ansible.builtin.command": "false"})
        self.assertFalse(out["kept"])
        self.assertEqual(self.calls(), ["lease-release"])
        self.assertTrue(out["result"]["released"])
        self.assertNotIn("kept until", out["why"])
        self.assertTrue(out["sshEnded"])

    def test_a_step_the_lease_was_too_short_for_releases_at_once(self):
        out = self.loop({"ansible.builtin.command": "true"}, left=80)
        self.assertFalse(out["kept"])
        self.assertEqual(self.calls(), ["lease-release"])
        self.assertIn("too little for this step", out["why"])

    def test_the_fabric_teardown_keeps_the_lease_from_its_role_rescue(self):
        # the HybridFabric teardown releases in its role's rescue: the keep decision runs there first, without
        # re-raising, and the status message carries its reason
        release = {"action": "lease-release", "namespace": NS, "fabric": "lab", "holder": "A"}
        for step, kept in (({"ansible.builtin.command": "sleep 10"}, True), ({"ansible.builtin.command": "false"}, False)):
            with self.subTest(kept=kept):
                if os.path.exists(self.helper + ".calls"):
                    os.remove(self.helper + ".calls")
                play = [{"ansible.builtin.include_vars": os.path.join(self.tmp, "common/vars/fabric_lease.yml")},
                        {"ansible.builtin.set_fact": {"fab_lease": {"namespace": NS, "fabric": "lab", "holder": "A",
                                                                    "until": "{{ now().timestamp() + 600 }}"}}},
                        {"name": "the teardown", "block": [
                            {"name": "Remove the root instance, one node at a time", "timeout": "{{ fab_lease_step_timeout }}",
                             "block": [dict(step, name="bgw-1: the step")]}],
                         "rescue": [dict(self.include("fabric_lease_keep.yml"), vars={"fab_lease_keep_raise": False}),
                                    self.include("fabric_ledger.yml", fab_ledger=release),
                                    {"ansible.builtin.set_fact": {"_status": "{{ fab_lease_keep_msg | default("
                                                                  "ansible_failed_result.msg, true) }}"}}]},
                        write_out("{'kept': fab_lease_keep | default(false), 'status': _status}")]
                real = dict(self.real, nodes=dict(self.real["nodes"], configLease=dict(
                    self.real["nodes"]["configLease"], stepTimeoutSeconds=2)))
                out = self.run_tasks(play, {"fab_real": real})
                self.assertEqual(out["kept"], kept)
                self.assertEqual(self.calls(), [] if kept else ["lease-release"])
                self.assertEqual("is kept until it runs out at " in out["status"], kept)
                self.assertIn("bgw-1: the step: ", out["status"])

    def test_a_job_that_succeeds_releases(self):
        out = self.loop({"ansible.builtin.command": "true"})
        self.assertEqual((out["kept"], out["why"]), (False, ""))
        self.assertEqual(self.calls(), ["lease-release"])


if __name__ == "__main__":
    unittest.main()

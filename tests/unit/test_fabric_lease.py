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
        self.assertEqual(out, {"renewed": True, "lease": NAME, "sinceLastRenewal": 100, "lapsed": False})
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

    def test_a_duration_a_live_holder_could_outlast_is_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            self.acquire("A", duration=300, stepTimeout=180)
        self.assertIn("at least 360", str(cm.exception))
        self.assertIsNone(self.w.lease)
        self.assertEqual(self.mod.lease_limits({"duration": 360, "stepTimeout": 180, "wait": 400}), (360, 400))

    def test_a_wait_that_ends_before_the_duration_is_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            self.acquire("A", duration=900, wait=900)
        self.assertIn("must be longer than its duration", str(cm.exception))


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
# the long waits inside a node: a renewal right before and right after each
LONG_WAITS = {
    "node_root.yml": "wait for BGP and IS-IS",
    "node_gateway.yml": "wait for the core (IS-IS, VPNv4 and VPNv6) and the upstream route reflectors",
}
# roles that reach the nodes outside the lease: reads only
READ_ONLY = {"attachmentcircuit_provision", "networkplacement_provision", "fabric_orphan_check"}
STEP_TIMEOUT = "{{ fab_real.nodes.configLease.stepTimeoutSeconds | int }}"


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

    def test_each_node_loop_runs_with_the_step_timeout(self):
        for role, (main, loop, node_file, _, _) in HOLDERS.items():
            with self.subTest(role=role):
                task = by_name(load(os.path.join(ROLES, role, "tasks", main)), loop)
                inc = task["ansible.builtin.include_tasks"]
                self.assertEqual(inc["file"], node_file)
                self.assertEqual(inc["apply"], {"timeout": STEP_TIMEOUT})
                self.assertEqual(task["loop_control"]["loop_var"], "bgw_node")

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

    def test_the_realization_and_the_helper_agree(self):
        self.assertEqual(self.cfg, {"durationSeconds": self.mod.LEASE_DURATION, "waitSeconds": self.mod.LEASE_WAIT,
                                    "stepTimeoutSeconds": self.mod.LEASE_STEP_TIMEOUT})

    def test_the_longest_gap_between_renewals_stays_below_the_duration(self):
        d, t, b = self.cfg["durationSeconds"], self.cfg["stepTimeoutSeconds"], self.mod.RENEW_BUDGET
        # a stretch of writes between two renewals (46 s recorded) with one step hanging until its timeout
        self.assertLess(self.mod.LEASE_SEGMENT + t + 2 * b, d)
        self.assertGreaterEqual(self.mod.LEASE_SEGMENT, 46)
        # a long wait between two renewals: its retry schedule, one hung attempt, two slow renewals
        for role, node_file in (("hybridfabric_provision", "node_root.yml"), ("cloudgateway_provision", "node_gateway.yml")):
            with self.subTest(wait=node_file):
                tasks = load(os.path.join(ROLES, role, "tasks", node_file))
                wait = next(x for x in tasks if _node_name(x) == LONG_WAITS[node_file])
                schedule = int(wait["retries"]) * int(wait["delay"])
                self.assertLess(schedule + t + 2 * b, d)
        # a single step: the longest internal wait a step does (the apply script waits up to 90 s for an interface)
        self.assertGreaterEqual(t, 2 * 90)

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


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class LedgerTask(unittest.TestCase):
    """fabric_ledger.yml with the helper replaced by one that answers with the request it got."""

    FAKE = textwrap.dedent('''\
        import json, sys
        req = json.load(sys.stdin)
        req.pop("token", None)
        if req["action"] == "lease-renew" and req["holder"] == "lost":
            print(json.dumps({"error": "the border gateway configuration lease fabric-config-lab was taken over by B"}))
            sys.exit(1)
        print(json.dumps({"echo": req}))
        ''')

    def _run(self, fab_ledger, fab_real=None):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="shc-lease-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "common/files"))
        os.makedirs(os.path.join(tmp, "r/o/l"))
        with open(os.path.join(tmp, "common/files/shc_ledger.py"), "w") as f:
            f.write(self.FAKE)
        tasks = load(os.path.join(ROOT, "eda/common/tasks/fabric_ledger.yml"))
        extra = {"role_path": os.path.join(tmp, "r/o/l"), "fab_ledger": fab_ledger,
                 "services_api_host": "https://api.example:6443", "services_bearer_token": "t"}
        if fab_real is not None:
            extra["fab_real"] = fab_real
        play = [{"name": "the ledger task", "block": tasks,
                 "rescue": [{"ansible.builtin.set_fact": {"fab_ledger_result": {"failed": "{{ ansible_failed_result.msg }}"}}}]},
                write_out("fab_ledger_result")]
        return run_play(play, extra)

    def test_an_acquire_carries_the_realizations_lease(self):
        real = load(os.path.join(ROOT, "eda/common/vars/fabric_realization.yml"))["fabric_realization_defaults"]
        out = self._run({"action": "lease-acquire", "namespace": NS, "fabric": "lab", "holder": "A"}, real)
        self.assertEqual({k: out["echo"].get(k) for k in ("duration", "wait", "stepTimeout")},
                         {"duration": 600, "wait": 1200, "stepTimeout": 180})

    def test_an_installations_value_is_passed_and_explicit_keys_win(self):
        real = {"nodes": {"configLease": {"durationSeconds": 900, "waitSeconds": 1500, "stepTimeoutSeconds": 300}}}
        out = self._run({"action": "lease-acquire", "namespace": NS, "fabric": "lab", "holder": "A", "wait": 1600}, real)
        self.assertEqual((out["echo"]["duration"], out["echo"]["wait"], out["echo"]["stepTimeout"]), (900, 1600, 300))

    def test_other_actions_carry_no_lease_numbers(self):
        real = load(os.path.join(ROOT, "eda/common/vars/fabric_realization.yml"))["fabric_realization_defaults"]
        out = self._run({"action": "lease-renew", "namespace": NS, "fabric": "lab", "holder": "A", "step": "s"}, real)
        self.assertFalse({"duration", "wait", "stepTimeout"} & set(out["echo"]))
        out = self._run({"action": "lease-acquire", "namespace": NS, "fabric": "lab", "holder": "A"})
        self.assertFalse({"duration", "wait", "stepTimeout"} & set(out["echo"]))

    def test_a_lost_lease_fails_the_task_with_the_helpers_reason(self):
        out = self._run({"action": "lease-renew", "namespace": NS, "fabric": "lab", "holder": "lost", "step": "s"})
        self.assertEqual(out["failed"], "Fabric lease-renew failed: the border gateway configuration lease "
                                        "fabric-config-lab was taken over by B")


if __name__ == "__main__":
    unittest.main()

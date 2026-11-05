"""The border gateway lease fabric-config-<fabric> is renewed by its holder.

- shc_ledger.py lease-renew: only the holder renews (renewTime, at the resourceVersion read); a lease taken over,
  released or deleted, or an API that does not answer within the renewal budget, stops the job before its next step.
  lease-release with delete (the fabric's teardown) removes the lease only while the job holds it.
The API and the clock are fakes.
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


if __name__ == "__main__":
    unittest.main()

"""The fabric's status is not starved by wakes, and an API blip does not strand the border gateway lease (V11).

- A wake from another job (nudge_cr's legacy reconcileNow "true") does not supersede the HybridFabric job in flight
  (operator roles/hybridfabric); Retry now (a timestamp) still does. The job runs the wakes that came during it once
  it has written its status (hybridfabric_provision deferred_wakes.yml).
- shc_ledger.py (ledger and lease) and the status module cr_status send a request again when the API could not be
  reached (name resolution, connection refused), so a blip neither leaves the lease held nor the status unwritten.
The decisions run in ansible-playbook (skipped without it); the retries run against stubs.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

OPERATOR_COMMON = os.path.join(ROOT, "operator/roles/_common/tasks")
FABRIC_ROLE = os.path.join(ROOT, "operator/primary/roles/hybridfabric/tasks/main.yml")
FABRIC_JOB = os.path.join(ROOT, "eda/rulebooks/roles/hybridfabric_provision/tasks")
ANN = "ansible.sdk.operatorframework.io/reconcileNow"


class Wiring(unittest.TestCase):
    def test_fabric_wakes_do_not_force_past_the_job_in_flight(self):
        tasks = load(FABRIC_ROLE)
        step = by_name(tasks, "Skip while the last event is in flight")
        self.assertEqual(step["vars"]["in_flight_force"],
                         "{{ reconcile_now | bool and not (reconcile_request_legacy | bool) }}")

    def test_other_kinds_keep_forcing(self):
        # only the fabric defers wakes (its jobs take minutes and many kinds wake it)
        for role in ("cloudgateway", "cloudinfrastructure"):
            step = by_name(load(os.path.join(ROOT, "operator/primary/roles", role, "tasks/main.yml")),
                           "Skip while the last event is in flight")
            self.assertEqual(step["vars"]["in_flight_force"], "{{ reconcile_now | bool }}", role)

    def test_job_runs_the_deferred_wakes_after_its_status(self):
        main = load(os.path.join(FABRIC_JOB, "main.yml"))
        block = [t.get("name", "") for t in main[0]["block"]]
        self.assertEqual(block[-1], "Run the wakes that came during this job")
        self.assertLess(block.index("Report the fabric ready"), len(block) - 1)
        rescue = [t.get("name", "") for t in main[0]["rescue"]]
        self.assertLess(rescue.index("Mark the HybridFabric failed"), rescue.index("Run the wakes that came during the failed job"))
        self.assertLess(rescue.index("Run the wakes that came during the failed job"), rescue.index("Re-raise"))
        wakes = [t for t in walk(main) if t.get("ansible.builtin.include_tasks") == "deferred_wakes.yml"]
        self.assertEqual(len(wakes), 2)
        listing = by_name(load(os.path.join(FABRIC_JOB, "deferred_wakes.yml")), "Read the fabric's pending wake")
        self.assertFalse(listing["failed_when"])

    def test_lease_release_stays_in_an_always_block(self):
        main = load(os.path.join(FABRIC_JOB, "main.yml"))
        configure = by_name(main, "Configure the nodes, one at a time")
        self.assertIn("Release the lease", [t.get("name") for t in configure["always"]])


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class Decisions(unittest.TestCase):
    def _skip(self, annotation, delivery):
        """reconcile_request.yml, the fabric role's force expression and the in-flight decision."""
        rr = load(os.path.join(OPERATOR_COMMON, "reconcile_request.yml"))
        inflight = by_name(load(os.path.join(OPERATOR_COMMON, "skip_if_event_in_flight.yml")),
                           "Decide whether the last event is still in flight")
        force = by_name(load(FABRIC_ROLE), "Skip while the last event is in flight")["vars"]["in_flight_force"]
        cr = {"metadata": {"name": "lab", "generation": 1, "annotations": {ANN: annotation} if annotation else {}},
              "status": {"observedGeneration": 1, "lastHandledReconcileRequest": "2026-11-02T10:00:00Z",
                         "delivery": delivery}}
        return run_play(rr + [{"ansible.builtin.set_fact": {"in_flight_force": force}}, inflight,
                              write_out("{'skip': _event_in_flight | bool, 'force': in_flight_force | bool}")],
                        {"reconcile_request_cr": cr, "in_flight_kind": "HybridFabric",
                         "_in_flight_cr": {"resources": [cr]}})

    def _running(self):
        from datetime import datetime, timedelta, timezone
        started = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"eventId": "e1", "publishedAt": started, "acknowledgedEventId": "e1", "jobStartedAt": started,
                "reason": "HybridFabricReconcileRequested"}

    def test_a_wake_waits_for_the_job_in_flight(self):
        self.assertEqual(self._skip("true", self._running()), {"skip": True, "force": False})

    def test_retry_now_still_supersedes(self):
        self.assertEqual(self._skip("2026-11-02T17:00:00Z", self._running()), {"skip": False, "force": True})

    def test_a_wake_publishes_once_the_job_ended(self):
        done = dict(self._running(), completedEventId="e1")
        self.assertEqual(self._skip("true", done), {"skip": False, "force": False})

    def test_a_wake_replaces_a_stalled_job(self):
        stalled = dict(self._running(), jobStartedAt="2020-01-01T00:00:00Z", publishedAt="2020-01-01T00:00:00Z")
        self.assertEqual(self._skip("true", stalled), {"skip": False, "force": False})

    def _deferred(self, annotations):
        step = by_name(load(os.path.join(FABRIC_JOB, "deferred_wakes.yml")), "Run the wakes that came during this job")
        return run_play([{"ansible.builtin.set_fact": {"_due": "{{ %s }}" % step["when"].strip()}}, write_out("_due | bool")],
                        {"_hf_deferred": {"resources": [{"metadata": {"name": "lab", "annotations": annotations}}]}})

    def test_job_end_runs_a_pending_wake_only(self):
        self.assertTrue(self._deferred({ANN: "true"}))
        self.assertTrue(self._deferred({ANN: "True"}))
        self.assertFalse(self._deferred({ANN: "2026-11-02T17:00:00Z"}))
        self.assertFalse(self._deferred({}))
        self.assertFalse(self._deferred(None))


class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text

    def json(self):
        import json
        return json.loads(self.text)


class LedgerRetry(unittest.TestCase):
    def setUp(self):
        import requests
        self.requests = requests
        spec = importlib.util.spec_from_file_location("shc_ledger_retry", os.path.join(ROOT, "eda/common/files/shc_ledger.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.sleeps = []
        for patcher in (mock.patch.object(self.mod.time, "sleep", self.sleeps.append),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake(self, fake):
        patcher = mock.patch.object(self.mod.requests, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _api(self, failures, exc=None):
        calls = []

        def fake(method, url, **kw):
            calls.append(method)
            if len(calls) <= failures:
                raise (exc or self.requests.exceptions.ConnectionError("Failed to resolve 'api.services'"))
            return _Resp(200, '{"spec": {"holderIdentity": "HybridFabric/lab job 1"}}')
        self._fake(fake)
        return self.mod.Api({"api": "https://api.example:6443", "token": "t"}), calls

    def test_a_blip_is_retried(self):
        api, calls = self._api(2)
        code, body = api.call("GET", "/x")
        self.assertEqual(code, 200)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [1, 2])

    def test_a_lasting_outage_still_fails(self):
        api, calls = self._api(100)
        with self.assertRaises(self.requests.exceptions.ConnectionError):
            api.call("GET", "/x")
        self.assertEqual(len(calls), len(self.mod.Api.RETRY_PAUSES) + 1)
        self.assertLessEqual(sum(self.mod.Api.RETRY_PAUSES), 60)

    def test_a_read_timeout_is_not_repeated(self):
        api, calls = self._api(1, self.requests.exceptions.ReadTimeout("slow"))
        with self.assertRaises(self.requests.exceptions.ReadTimeout):
            api.call("GET", "/x")
        self.assertEqual(len(calls), 1)

    def test_lease_release_survives_a_blip(self):
        calls = []

        def fake(method, url, **kw):
            calls.append(method)
            if len(calls) == 1:
                raise self.requests.exceptions.ConnectionError("Failed to resolve 'api.services'")
            if method == "GET":
                return _Resp(200, '{"metadata": {"name": "fabric-config-lab"}, "spec": {"holderIdentity": "h"}}')
            return _Resp(200, '{}')
        self._fake(fake)
        out = self.mod.lease_release(self.mod.Api({"api": "https://a", "token": "t"}),
                                     {"fabric": "lab", "holder": "h", "namespace": "sovereign-cloud"})
        self.assertEqual(out, {"released": True})
        self.assertEqual(calls, ["GET", "GET", "PUT"])


class StatusModuleRetry(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "eda/rulebooks/collections"))
        try:
            from ansible_collections.hybridsovereign.shc.plugins.modules import cr_status
        except ImportError as e:  # pragma: no cover
            self.skipTest("ansible not importable: %s" % e)
        self.m = cr_status
        self.sleeps = []
        patcher = mock.patch.object(cr_status.time, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake(self, fake):
        patcher = mock.patch.object(self.m, "open_url", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    class _Module:
        params = {"api_key": "t", "validate_certs": False, "ca_path": None, "timeout": 30}

        class Failed(Exception):
            pass

        def fail_json(self, **kw):
            raise self.Failed(kw)

    def _open(self, failures):
        calls = []

        class R:
            status = 200

            def read(self):
                return b'{"ok": true}'

        def fake(url, **kw):
            calls.append(url)
            if len(calls) <= failures:
                raise self.m.URLError(OSError(-2, "Name or service not known"))
            return R()
        self._fake(fake)
        return calls

    def test_a_blip_is_retried(self):
        calls = self._open(2)
        code, body = self.m._request(self._Module(), "GET", "https://api/x")
        self.assertEqual((code, body), (200, {"ok": True}))
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [1, 2])

    def test_a_lasting_outage_fails_with_a_message(self):
        self._open(100)
        mod = self._Module()
        with self.assertRaises(mod.Failed) as ctx:
            self.m._request(mod, "PATCH", "https://api/x")
        self.assertIn("could not be reached", ctx.exception.args[0]["msg"])
        self.assertFalse(ctx.exception.args[0]["superseded"])

    def test_http_errors_are_returned_at_once(self):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            raise self.m.HTTPError(url, 409, "Conflict", {}, None)
        self._fake(fake)
        code, body = self.m._request(self._Module(), "PATCH", "https://api/x")
        self.assertEqual(code, 409)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

"""The writer of the admin dashboard's platform-health banner (charts/charts/shc-cluster-base/files/
platform_health_report.py), against a fake monitoring API (Alertmanager's /api/v2/alerts) and a fake Kubernetes API
(the check Jobs and their pods, and the writer's own ConfigMap shc-platform-health-<cluster>, key report.json, with
a JSON merge patch):

- no alert, one, several (order, prefix, silenced and inhibited left out), a check with findings, a check whose
  failed run was followed by a success, an alert without a check;
- the monitoring unreachable or answering an error, and the write refused: exit 1 and nothing written;
- an over-long finding, many findings, many faults (each line cut, the report bounded and the faults left out
  counted), web addresses, host names, IP addresses and a crash report kept out of the object, the token never in any
  output;
- another cluster's report untouched and never read; the writer's ConfigMap absent: nothing created, the run fails
  with what to set (the services cluster's chart makes the ConfigMaps);
- the writer on another cluster: the services cluster's address and token from the Secret's files, a missing one
  named by its setting;
- alertsDelivered with the receiver check off, on and quiet, on and firing;
- the key's shape against the banner's example object.

Standard library only, so it also runs in the job image."""
import contextlib
import copy
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files"))

import platform_health_report as R  # noqa: E402

TOKEN = "s3cret-report-token"
REMOTE_TOKEN = "s3cret-remote-token"
NOW = 1_900_000_000
CM_DIR = "/api/v1/namespaces/platform/configmaps/"
CM = CM_DIR + "shc-platform-health-services"
CHECKS = "eda-activations|eda-api-check|identity-write-probe-.+"
CFG = dict(R.DEFAULTS, cluster="services", opsNamespace="shc-ops", checkCronJobs=CHECKS, targetMode="local",
           targetNamespace="platform", targetConfigMap="shc-platform-health-services")

# the banner's object as the stream rules give it (one key)
EXAMPLE = {"schemaVersion": 1, "cluster": "hub", "writtenAt": "2030-01-01T00:00:00Z", "intervalSeconds": 300,
           "faults": [{"id": "ShcEventActivationsNotRunning", "severity": "warning", "since": "2030-01-01T00:00:00Z",
                       "object": "shc-ops/eda-activations", "summary": "<the alert's summary>",
                       "details": ["<one line per finding of the check, e.g. the activation's name and state>"]}],
           "alertsDelivered": None}


def alert(alertname, severity="warning", since="2030-01-01T00:00:00.123Z", summary=None, state="active", **labels):
    return {"labels": dict(labels, alertname=alertname, severity=severity),
            "annotations": {"summary": summary if summary is not None else "%s summary" % alertname,
                            "description": "a description with oc commands"},
            "startsAt": since, "endsAt": "2030-01-01T01:00:00Z", "fingerprint": alertname,
            "status": {"state": state, "silencedBy": [], "inhibitedBy": []}}


def job(name, cronjob, created, state):
    cond = {"failed": [{"type": "Failed", "status": "True"}], "succeeded": [{"type": "Complete", "status": "True"}],
            "running": []}[state]
    return {"metadata": {"name": name, "creationTimestamp": created,
                         "ownerReferences": [{"kind": "CronJob", "name": cronjob}]},
            "status": {"conditions": cond}}


def pod(jobname, message, created="2030-01-01T00:00:00Z"):
    return {"metadata": {"name": jobname + "-x", "labels": {"job-name": jobname}, "creationTimestamp": created},
            "status": {"containerStatuses": [{"state": {"terminated": {"exitCode": 1, "message": message}}}]}}


class Fake:
    """Alertmanager and the Kubernetes API in one: alerts, Jobs and pods in shc-ops, ConfigMaps in platform.

    cms: {cluster: ConfigMap} for the ConfigMaps shc-platform-health-<cluster> (the services cluster's chart makes
    them; a writer may only patch its own). A create is refused, as the writer's Role has no create."""

    def __init__(self, alerts=(), jobs=(), pods=(), cms=None):
        self.alerts = list(alerts)
        self.jobs = list(jobs)
        self.pods = list(pods)
        self.cms = {"shc-platform-health-" + k: copy.deepcopy(v) for k, v in (cms or {}).items()}
        self.alerts_answer = None
        self.patch_answer = None
        self.pods_answer = None
        self.calls = []

    @property
    def cm(self):
        return self.cms.get("shc-platform-health-services")

    def handle(self, method, path, body, auth):
        self.calls.append((method, path, body, auth))
        if path.startswith("/api/v2/alerts"):
            return self.alerts_answer or (200, self.alerts)
        if path == "/apis/batch/v1/namespaces/shc-ops/jobs" and method == "GET":
            return 200, {"items": self.jobs}
        if path.startswith("/api/v1/namespaces/shc-ops/pods?labelSelector=") and method == "GET":
            if self.pods_answer:
                return self.pods_answer
            sel = path.split("labelSelector=", 1)[1].replace("%3D", "=")
            want = sel.split("=", 1)[1]
            return 200, {"items": [p for p in self.pods if p["metadata"]["labels"]["job-name"] == want]}
        if path.startswith(CM_DIR) and method == "PATCH":
            if self.patch_answer:
                return self.patch_answer
            cm = self.cms.get(path[len(CM_DIR):])
            if cm is None:
                return 404, {"reason": "NotFound"}
            cm.setdefault("data", {}).update(body["data"])
            return 200, cm
        if path.rstrip("/") + "/" == CM_DIR and method == "POST":
            return 403, {"reason": "Forbidden"}
        return 404, {"reason": "NotFound"}

    def writes(self):
        return [c for c in self.calls if c[0] in ("PATCH", "POST")]


class FakeHttp:
    """The script's Http with the fake behind it (no socket)."""

    def __init__(self, fake, token=TOKEN):
        self.fake, self.token = fake, token

    def call(self, method, path, body=None, ok=(200,), content_type="application/json"):
        code, doc = self.fake.handle(method, path, copy.deepcopy(body), "Bearer " + self.token)
        if code not in ok:
            raise R.ApiError("the API: %s %s answered %d" % (method, path.split("?")[0], code), status=code)
        return doc


def run(fake, cfg=CFG):
    h = FakeHttp(fake)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code, doc = R.run(cfg, h, h, h, NOW)
    return code, doc, out.getvalue()


def written(fake, cluster="services"):
    return json.loads(fake.cms["shc-platform-health-" + cluster]["data"]["report.json"])


class Report(unittest.TestCase):
    def test_no_alert(self):
        fake = Fake(cms={"services": {"data": {}}})
        code, doc, _ = run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(written(fake), {"schemaVersion": 1, "cluster": "services", "writtenAt": "2030-03-17T17:46:40Z",
                                         "intervalSeconds": 300, "faults": [], "alertsDelivered": None})

    def test_one_alert(self):
        fake = Fake([alert("ShcVolumeFilling", namespace="aap", persistentvolumeclaim="db-repo",
                           summary="Volume aap/db-repo is 85 % used")], cms={"services": {"data": {}}})
        code, _, _ = run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(written(fake)["faults"], [{
            "id": "ShcVolumeFilling", "severity": "warning", "since": "2030-01-01T00:00:00Z",
            "object": "aap/db-repo", "summary": "Volume aap/db-repo is 85 % used", "details": []}])

    def test_several_alerts_ordered_and_filtered(self):
        silenced = alert("ShcCertificateNotReady", namespace="a", name="c", state="suppressed")
        silenced["status"]["silencedBy"] = ["s1"]
        inhibited = alert("ShcVolumeFilling", namespace="b", persistentvolumeclaim="p")
        inhibited["status"]["inhibitedBy"] = ["f1"]
        fake = Fake([alert("ShcBackupStale", since="2030-01-02T00:00:00Z", namespace="shc-ops", cronjob="etcd-backup"),
                     alert("KubePodCrashLooping", namespace="x", pod="y"),
                     alert("ShcAggregatedAPIUnavailable", "critical", since="2030-01-03T00:00:00Z",
                           name="v1.example.io"),
                     alert("ShcEtcdLeaderChanges", since="2030-01-01T00:00:00Z"),
                     alert("ShcVolumeAlmostFull", "critical", since="2030-01-01T00:00:00+02:00", namespace="q",
                           persistentvolumeclaim="r"),
                     silenced, inhibited], cms={"services": {"data": {}}})
        run(fake)
        got = [(f["id"], f["severity"], f["since"], f["object"]) for f in written(fake)["faults"]]
        self.assertEqual(got, [
            ("ShcVolumeAlmostFull", "critical", "2029-12-31T22:00:00Z", "q/r"),
            ("ShcAggregatedAPIUnavailable", "critical", "2030-01-03T00:00:00Z", "v1.example.io"),
            ("ShcEtcdLeaderChanges", "warning", "2030-01-01T00:00:00Z", ""),
            ("ShcBackupStale", "warning", "2030-01-02T00:00:00Z", "shc-ops/etcd-backup"),
        ])
        # a backup CronJob in the operations namespace is not a check: no Jobs read
        self.assertFalse([c for c in fake.calls if "/jobs" in c[1]])

    def test_check_with_findings(self):
        msg = "act-17 (acme-web): RepairLimitReached: 3 restarts within the hour\nact-4 (acme-db): CannotRun: pull failed\n"
        fake = Fake([alert("ShcEventActivationsNotRunning", "critical", namespace="shc-ops", cronjob="eda-activations")],
                    jobs=[job("eda-activations-1", "eda-activations", "2030-01-01T00:00:00Z", "failed"),
                          job("eda-activations-3", "eda-activations", "2030-01-01T00:10:00Z", "failed"),
                          job("eda-activations-2", "eda-activations", "2030-01-01T00:05:00Z", "succeeded"),
                          job("eda-api-check-9", "eda-api-check", "2030-01-01T00:20:00Z", "failed")],
                    pods=[pod("eda-activations-1", "old finding"), pod("eda-activations-3", msg),
                          pod("eda-api-check-9", "api")],
                    cms={"services": {"data": {}}})
        code, _, _ = run(fake)
        self.assertEqual(code, 0)
        f = written(fake)["faults"][0]
        self.assertEqual((f["object"], f["details"]), ("shc-ops/eda-activations", [
            "act-17 (acme-web): RepairLimitReached: 3 restarts within the hour", "act-4 (acme-db): CannotRun: pull failed"]))

    def test_a_success_after_the_failure_means_no_findings(self):
        fake = Fake([alert("ShcIdentityWriteCheckFailed", "critical", namespace="shc-ops",
                           cronjob="identity-write-probe-services")],
                    jobs=[job("identity-write-probe-services-1", "identity-write-probe-services", "2030-01-01T00:00:00Z",
                              "failed"),
                          job("identity-write-probe-services-2", "identity-write-probe-services", "2030-01-01T00:05:00Z",
                              "succeeded"),
                          job("identity-write-probe-services-3", "identity-write-probe-services", "2030-01-01T00:10:00Z",
                              "running")],
                    pods=[pod("identity-write-probe-services-1", "stale")], cms={"services": {"data": {}}})
        run(fake)
        self.assertEqual(written(fake)["faults"][0]["details"], [])

    def test_an_alert_without_a_check(self):
        for labels in ({"namespace": "other", "cronjob": "eda-activations"},       # not the operations namespace
                       {"namespace": "shc-ops", "cronjob": "etcd-backup"},          # not a check
                       {"namespace": "shc-ops", "cronjob": "eda-activations-x"},    # the name must match whole
                       {"namespace": "shc-ops"}):
            with self.subTest(labels=labels):
                fake = Fake([alert("ShcBackupJobFailed", **labels)],
                            jobs=[job("j", labels.get("cronjob", ""), "2030-01-01T00:00:00Z", "failed")],
                            pods=[pod("j", "secret-ish raw text")], cms={"services": {"data": {}}})
                run(fake)
                self.assertEqual(written(fake)["faults"][0]["details"], [])
                self.assertFalse([c for c in fake.calls if "/jobs" in c[1] or "/pods" in c[1]])

    def test_monitoring_unreachable_writes_nothing(self):
        for answer in ((503, {"error": "down"}), (403, {"reason": "Forbidden"}), (200, {"not": "a list"}),
                       (200, ["not an alert"])):
            with self.subTest(answer=answer):
                fake = Fake(cms={"services": {"data": {"report.json": "old"}}})
                fake.alerts_answer = answer
                code, doc, out = run(fake)
                self.assertEqual((code, doc), (1, None))
                self.assertEqual(fake.writes(), [])
                self.assertEqual(fake.cm["data"]["report.json"], "old")
                self.assertIn("not written: the alerts could not be read", out)

    def test_write_refused(self):
        for answer in ((403, {"reason": "Forbidden"}), (422, {"reason": "Invalid"}), (500, {})):
            with self.subTest(answer=answer):
                fake = Fake([alert("ShcVolumeFilling", namespace="a", persistentvolumeclaim="b")], cms={"services": {"data": {}}})
                fake.patch_answer = answer
                code, doc, out = run(fake)
                self.assertEqual((code, doc), (1, None))
                self.assertIn("not written", out)
                self.assertEqual(fake.cm, {"data": {}})

    def test_its_configmap_absent_nothing_is_created(self):
        # the writer may not create: the services cluster's chart makes one ConfigMap per reporting cluster
        fake = Fake([alert("ShcVolumeFilling", namespace="a", persistentvolumeclaim="b")],
                    cms={"hub": {"data": {}}})
        code, doc, out = run(fake)
        self.assertEqual((code, doc), (1, None))
        self.assertEqual([c[0] for c in fake.writes()], ["PATCH"])
        self.assertEqual(set(fake.cms), {"shc-platform-health-hub"})
        self.assertIn("ConfigMap shc-platform-health-services does not exist in platform on the services cluster", out)
        self.assertIn("ops.platformHealth.report.writers", out)

    def test_over_long_and_many_findings(self):
        long = "act-1: " + "x" * 2000
        msg = "\n".join([long] + ["finding %d" % i for i in range(40)])
        fake = Fake([alert("ShcEventActivationsNotRunning", "critical", namespace="shc-ops", cronjob="eda-activations",
                           summary="s" * 1000)],
                    jobs=[job("j1", "eda-activations", "2030-01-01T00:00:00Z", "failed")], pods=[pod("j1", msg)],
                    cms={"services": {"data": {}}})
        run(fake)
        f = written(fake)["faults"][0]
        self.assertEqual(len(f["details"]), CFG["maxDetails"])
        self.assertEqual(len(f["details"][0]), CFG["maxLineChars"])
        self.assertTrue(f["details"][0].endswith("…"))
        self.assertEqual(f["details"][-1], "and 22 more findings")
        self.assertEqual(len(f["summary"]), CFG["maxLineChars"])

    def test_the_key_is_bounded(self):
        alerts = [alert("ShcVolumeFilling", namespace="ns%03d" % i, persistentvolumeclaim="p" * 200,
                        summary="y" * 400) for i in range(300)]
        alerts.append(alert("ShcEventActivationsNotRunning", "critical", namespace="shc-ops", cronjob="eda-activations"))
        fake = Fake(alerts, jobs=[job("j1", "eda-activations", "2030-01-01T00:00:00Z", "failed")],
                    pods=[pod("j1", "\n".join("z" * 290 for _ in range(30)))], cms={"services": {"data": {}}})
        run(fake)
        raw = fake.cm["data"]["report.json"]
        self.assertLessEqual(len(raw.encode()), CFG["maxKeyBytes"])
        self.assertLess(len(raw.encode()), 64 * 1024)
        doc = json.loads(raw)
        # critical first, so it stays
        self.assertEqual(doc["faults"][0]["id"], "ShcEventActivationsNotRunning")
        self.assertLess(len(doc["faults"]), 301)
        # the faults left out are counted in one last fault
        last = doc["faults"][-1]
        self.assertEqual(last["id"], R.LEFT_OUT)
        self.assertRegex(last["summary"], r"^\d+ more faults on services are not shown$")
        self.assertEqual(int(last["summary"].split()[0]) + len(doc["faults"]) - 1, 301)
        # under the bound, nothing is left out and there is no such fault
        small = Fake([alert("ShcVolumeFilling", namespace="a", persistentvolumeclaim="b")], cms={"services": {"data": {}}})
        run(small)
        self.assertNotIn(R.LEFT_OUT, [f["id"] for f in written(small)["faults"]])

    def test_addresses_crash_reports_and_tokens_stay_out(self):
        R.SECRETS.add(TOKEN)
        msg = ("the write check could not run (realm platform at https://sso.example.com/realms/x): no answer\n"
               "GET /api: no answer from 192.0.2.10:8443 and [2001:db8::5]:443 at 12:30:45\n"
               "keycloak.ops.svc:8443 refused; alertmanager-main.openshift-monitoring.svc down; api.example.com x\n"
               "token %s leaked" % TOKEN)
        fake = Fake([alert("ShcIdentityWriteCheckFailed", "critical", namespace="shc-ops",
                           cronjob="identity-write-probe-services", summary="see http://10.0.0.1/x")],
                    jobs=[job("j1", "identity-write-probe-services", "2030-01-01T00:00:00Z", "failed")],
                    pods=[pod("j1", msg)], cms={"services": {"data": {}}})
        _, _, out = run(fake)
        raw = fake.cm["data"]["report.json"]
        for bad in ("sso.example.com", "192.0.2.10", "2001:db8", "10.0.0.1", TOKEN, "http", "keycloak.ops",
                    "openshift-monitoring.svc", "api.example.com"):
            self.assertNotIn(bad, raw)
        self.assertNotIn(TOKEN, out)
        f = json.loads(raw)["faults"][0]
        self.assertEqual(f["details"][1], "GET /api: no answer from [address] and [address] at 12:30:45")
        self.assertEqual(f["details"][2], "[address] refused; [address] down; [address] x")
        self.assertEqual(f["summary"], "see [address]")
        # field paths, file names and versions are not host names
        for keep in ("status.phase is Failed", "see file.py", "version 4.22.1", "activation acme-events: failed"):
            self.assertEqual(R.clean_line(keep, 300), keep)
        # a crash: the pod's log fallback is never copied
        fake = Fake([alert("ShcEventActivationsNotRunning", namespace="shc-ops", cronjob="eda-activations")],
                    jobs=[job("j1", "eda-activations", "2030-01-01T00:00:00Z", "failed")],
                    pods=[pod("j1", 'Traceback (most recent call last):\n  File "/x.py", line 1\nKeyError: url')],
                    cms={"services": {"data": {}}})
        run(fake)
        self.assertEqual(written(fake)["faults"][0]["details"], [R.CRASHED])

    def test_unreadable_findings_are_written_and_fail_the_run(self):
        fake = Fake([alert("ShcEventActivationsNotRunning", namespace="shc-ops", cronjob="eda-activations")],
                    jobs=[job("j1", "eda-activations", "2030-01-01T00:00:00Z", "failed")], cms={"services": {"data": {}}})
        fake.pods_answer = (403, {"reason": "Forbidden"})
        code, _, out = run(fake)
        self.assertEqual(code, 1)
        details = written(fake)["faults"][0]["details"]
        self.assertEqual(len(details), 1)
        self.assertTrue(details[0].startswith("the check's findings could not be read: "))
        self.assertIn("findings not read", out)

    def test_another_clusters_report_is_untouched(self):
        hub = json.dumps(dict(EXAMPLE, cluster="hub"))
        fake = Fake([alert("ShcVolumeFilling", namespace="a", persistentvolumeclaim="b")],
                    cms={"hub": {"data": {"report.json": hub}}, "services": {"data": {"other": "kept"}}})
        code, _, _ = run(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.cms["shc-platform-health-hub"], {"data": {"report.json": hub}})
        self.assertEqual(set(fake.cm["data"]), {"other", "report.json"})
        # one merge patch of the report key of its own ConfigMap; no ConfigMap is ever read
        (method, path, body, _), = fake.writes()
        self.assertEqual((method, path, list(body["data"])), ("PATCH", CM, ["report.json"]))
        self.assertFalse([c for c in fake.calls if c[0] == "GET" and "configmaps" in c[1]])

    def test_alerts_delivered(self):
        receiver = alert("AlertmanagerReceiversNotConfigured", namespace="openshift-monitoring")
        for check, alerts, want in ((False, [receiver], None), (True, [], True), (True, [receiver], False)):
            with self.subTest(check=check, firing=bool(alerts)):
                fake = Fake(alerts, cms={"services": {"data": {}}})
                run(fake, dict(CFG, receiverCheck=check))
                doc = written(fake)
                self.assertIs(doc["alertsDelivered"], want)
                self.assertEqual(doc["faults"], [])      # not a platform (Shc) alert

    def test_shape_matches_the_banner_example(self):
        fake = Fake([alert("ShcEventActivationsNotRunning", namespace="shc-ops", cronjob="eda-activations")],
                    jobs=[job("j1", "eda-activations", "2030-01-01T00:00:00Z", "failed")],
                    pods=[pod("j1", "act-1 (acme-web): CannotRun")], cms={"hub": {"data": {}}})
        run(fake, dict(CFG, cluster="hub", targetConfigMap="shc-platform-health-hub"))
        doc = written(fake, "hub")

        def shape(x):
            if isinstance(x, dict):
                return {k: shape(v) for k, v in x.items()}
            if isinstance(x, list):
                return [shape(x[0])] if x else []
            return type(x).__name__
        self.assertEqual(shape(doc), dict(shape(EXAMPLE), alertsDelivered="NoneType"))
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertRegex(doc["writtenAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class Main(unittest.TestCase):
    """main() end to end over HTTP, through an injected plain-http transport (the script takes https only)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="platform-health-report-")
        with open(os.path.join(self.tmp, "token"), "w") as f:
            f.write(TOKEN)
        self.local = Fake([alert("ShcVolumeFilling", namespace="a", persistentvolumeclaim="b")],
                          cms={"services": {"data": {}}})
        self.remote = Fake(cms={"services": {"data": {"report.json": "theirs"}}, "hub": {}})
        self.servers = [self.serve(self.local), self.serve(self.remote)]

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for n in files:
                os.remove(os.path.join(root, n))
            for n in dirs:
                os.rmdir(os.path.join(root, n))
        os.rmdir(self.tmp)

    def serve(self, fake):
        class H(http.server.BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                code, doc = fake.handle(self.command, self.path, body, self.headers.get("Authorization"))
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = _do

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def url(self, i):
        return "http://127.0.0.1:%d" % self.servers[i].server_address[1]

    def config(self, **kw):
        cfg = {"cluster": "hub", "opsNamespace": "shc-ops", "checkCronJobs": CHECKS, "targetMode": "remote",
               "targetNamespace": "platform", "targetConfigMap": "shc-platform-health-hub", "targetDir": os.path.join(self.tmp, "sc"), "targetServerKey": "server",
               "targetTokenKey": "token", "kubeURL": self.url(0), "alertmanagerURL": self.url(0),
               "kubeTokenFile": os.path.join(self.tmp, "token"), "kubeCAFile": "/nonexistent",
               "alertmanagerCAFile": "/nonexistent"}
        cfg.update(kw)
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        return path

    def secret(self, **files):
        d = os.path.join(self.tmp, "sc")
        os.makedirs(d, exist_ok=True)
        for k, v in files.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v)

    def transport(self, req, timeout):
        import urllib.request
        opener = urllib.request.build_opener(R._NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def main(self, path):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(["--config", path], transport=self.transport)
        return code, out.getvalue()

    def test_another_cluster_writes_to_the_services_cluster(self):
        self.secret(server=self.url(1) + "\n", token=REMOTE_TOKEN)
        code, out = self.main(self.config())
        self.assertEqual(code, 0)
        doc = written(self.remote, "hub")
        self.assertEqual(doc["faults"][0]["object"], "a/b")
        self.assertEqual(self.remote.cm["data"]["report.json"], "theirs")
        # the monitoring with this cluster's token, the write with the services cluster's
        self.assertEqual({c[3] for c in self.local.calls}, {"Bearer " + TOKEN})
        self.assertEqual({c[3] for c in self.remote.calls}, {"Bearer " + REMOTE_TOKEN})
        self.assertEqual(self.local.writes(), [])
        self.assertNotIn(REMOTE_TOKEN, out)
        self.assertNotIn(self.url(1), self.remote.cms["shc-platform-health-hub"]["data"]["report.json"])

    def test_on_the_services_cluster_it_writes_locally(self):
        code, _ = self.main(self.config(targetMode="local", cluster="services",
                                        targetConfigMap="shc-platform-health-services"))
        self.assertEqual(code, 0)
        self.assertIn("report.json", self.local.cm["data"])
        self.assertEqual(self.remote.calls, [])

    def test_a_missing_secret_names_the_setting(self):
        for files, setting in (({}, "serverKey"), ({"server": self.url(1)}, "tokenKey")):
            with self.subTest(setting=setting):
                self.secret(**files)
                with self.assertRaises(SystemExit) as e:
                    self.main(self.config())
                self.assertIn("ops.platformHealth.report.servicesCluster.secret", str(e.exception.code))
                self.assertIn("ops.platformHealth.report.servicesCluster." + setting, str(e.exception.code))
                self.assertEqual(self.local.calls + self.remote.calls, [])
        self.secret(server=self.url(1), token=REMOTE_TOKEN)
        with self.assertRaises(SystemExit) as e:
            self.main(self.config(targetCAKey="ca.crt"))
        self.assertIn("servicesCluster.caKey", str(e.exception.code))

    def test_monitoring_down_over_http(self):
        self.local.alerts_answer = (503, {})
        self.secret(server=self.url(1), token=REMOTE_TOKEN)
        code, out = self.main(self.config())
        self.assertEqual(code, 1)
        self.assertEqual(self.remote.writes(), [])
        self.assertNotIn(TOKEN, out)

    def test_monitoring_not_answering(self):
        self.secret(server=self.url(1), token=REMOTE_TOKEN)
        code, out = self.main(self.config(alertmanagerURL="http://127.0.0.1:9"))
        self.assertEqual(code, 1)
        self.assertIn("no answer", out)
        self.assertEqual(self.remote.writes(), [])

    def test_https_only_without_a_test_transport(self):
        with self.assertRaises(SystemExit):
            R.Http("http://example.com", "t", "", 5)

    def test_settings(self):
        for kw, word in (({"cluster": "Hub"}, "cluster"), ({"cluster": ""}, "cluster"),
                         ({"targetMode": "elsewhere"}, "targetMode"), ({"targetNamespace": ""}, "targetNamespace"),
                         ({"intervalSeconds": "300"}, "intervalSeconds"), ({"receiverCheck": "yes"}, "receiverCheck")):
            with self.subTest(kw=kw):
                with self.assertRaises(SystemExit) as e:
                    self.main(self.config(**kw))
                self.assertIn(word, str(e.exception.code))


if __name__ == "__main__":
    unittest.main()

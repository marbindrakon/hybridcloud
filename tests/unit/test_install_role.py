"""The provision role's installation steps, run with Ansible against a fake API (the services cluster and the hub in
one HTTP server; the module hybridsovereign.shc.install_observe is the real one): every ending of the watch, the check
right before the release, the guarded removal of a stopped build for each provider (an installed cluster is never
removed), and that only an installed cluster is ever written Ready (the two wrong endings of the old two-hour poll).

Example names and documentation ranges only.
"""
import copy
import datetime
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402

BUILD = "shc-oso-example-demo-0a1b2c3d"
NS = "entity-example"
QUOTA_LOG = ("level=fatal msg=failed to generate asset \"Platform Quota Check\": error(MissingQuota): Cores is not "
             "available because the required number of resources (72) is more than the limit of 64")


def ago(minutes):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class FakeApi:
    """GET of the cluster object, the ClusterDeployment and the provisions; merge PATCH of the object's status
    (recorded). cd_reads: the ClusterDeployment is gone after this many reads of it (a deletion Hive finishes);
    obj_reads: the object is gone after this many reads of it (a delete); hub objects of the removal's waits
    (ManagedCluster, uninstall jobs, namespace) are gone (404)."""

    def __init__(self, obj, cd=None, provisions=(), cd_reads=None, obj_reads=None):
        self.obj = copy.deepcopy(obj)
        self.cd = copy.deepcopy(cd)
        self.provisions = [copy.deepcopy(p) for p in provisions]
        self.patches = []
        self.gets = []
        self.cd_reads = cd_reads
        self.obj_reads = obj_reads
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                fake.gets.append(path)
                obj_path = "/apis/hybridsovereign.redhat/v1alpha1/namespaces/%s/platformopenshifts/demo" % NS
                hive = "/apis/hive.openshift.io/v1/namespaces/%s/" % BUILD
                if path == obj_path:
                    if fake.obj_reads is not None:
                        if fake.obj_reads <= 0:
                            fake.obj = None
                        fake.obj_reads -= 1
                    return self.send(200, fake.obj) if fake.obj else self.send(404, {})
                if path == hive + "clusterdeployments/" + BUILD:
                    if fake.cd_reads is not None:
                        if fake.cd_reads <= 0:
                            fake.cd = None
                        fake.cd_reads -= 1
                    return self.send(200, fake.cd) if fake.cd else self.send(404, {})
                if path == hive + "clusterprovisions":
                    return self.send(200, {"items": fake.provisions})
                return self.send(404, {})

            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.patches.append(body)
                st = dict(fake.obj.get("status") or {})

                def merge(a, b):
                    out = dict(a or {})
                    for k, v in b.items():
                        if v is None:
                            out.pop(k, None)
                        elif isinstance(v, dict):
                            out[k] = merge(out.get(k), v)
                        else:
                            out[k] = v
                    return out
                fake.obj["status"] = merge(st, body.get("status") or {})
                fake.obj["metadata"]["resourceVersion"] = str(int(fake.obj["metadata"]["resourceVersion"]) + 1)
                return self.send(200, fake.obj)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def ready_true_written(self):
        for p in self.patches:
            for c in (p.get("status") or {}).get("conditions") or []:
                if c.get("type") == "Ready" and c.get("status") == "True":
                    return True
        return False


SPEC = {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3}}
SMALLER = {"type": "openstack", "openstack": {"environment": "env", "workerCount": 0}}


def po(install=None, uid="uid-1", event="ev-1", generation=1, deleting=False, spec=None, **status):
    st = dict(status)
    st["delivery"] = {"eventId": event, "acknowledgedEventId": event, "jobStartedAt": ago(1)}
    if install is not None:
        st["install"] = install
    meta = {"name": "demo", "namespace": NS, "uid": uid, "generation": generation, "resourceVersion": "7"}
    if deleting:
        meta["deletionTimestamp"] = ago(0)
    return {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "PlatformOpenshift", "metadata": meta,
            "spec": copy.deepcopy(spec if spec is not None else SPEC), "status": st}


def cd(installed=False, stopped=False, failed=None, restarts=0):
    conds = [{"type": "Provisioned", "status": "True" if installed else "False",
              "reason": "Provisioned" if installed else ("ProvisionStopped" if stopped else "Provisioning")}]
    if stopped:
        conds.append({"type": "ProvisionStopped", "status": "True", "reason": "InstallAttemptsLimitReached"})
    if failed:
        conds.append({"type": "ProvisionFailed", "status": "True", "reason": failed, "message": "x"})
    return {"metadata": {"name": BUILD, "namespace": BUILD}, "spec": {"installed": installed, "installAttemptsLimit": 3},
            "status": {"installRestarts": restarts, "conditions": conds}}


def failed_provision(attempt):
    return {"metadata": {"name": "%s-%d-abcde" % (BUILD, attempt)},
            "spec": {"attempt": attempt, "stage": "failed", "installLog": QUOTA_LOG,
                     "clusterDeploymentRef": {"name": BUILD}},
            "status": {"conditions": [{"type": "ClusterProvisionFailed", "status": "True", "reason": "UnknownError",
                                       "message": QUOTA_LOG}]}}


RUNNING = {"stage": "Installing", "run": 1, "startedAt": ago(5), "attempt": 1, "attemptsLimit": 3,
           "observedAt": ago(1)}
sys.path.insert(0, os.path.join(H.EDA, "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                                "plugins", "module_utils"))
import shc_install as I  # noqa: E402

STOPPED = {"stage": "Stopped", "run": 1, "startedAt": ago(30), "endedAt": ago(20), "stoppedGeneration": 1,
           "stoppedRequest": "", "stoppedSpec": I.spec_digest(SPEC), "stoppedSpecVersion": I.DIGEST_VERSION,
           "attempt": 3, "attemptsLimit": 3,
           "lastFailure": {"attempt": 3, "reason": "QuotaExceeded", "message": "m", "time": ago(20)}}


def base_vars(api, provider="openstack", **kw):
    v = {"services_api_host": api.url, "services_bearer_token": "token-not-in-data", "services_validate_certs": False,
         "central_api_host": api.url, "central_bearer_token": "token-not-in-data",
         "ansible_operator_meta": {"name": "demo", "namespace": NS},
         "event_payload": {"eventId": "ev-1", "regarding": {"name": "demo", "namespace": NS, "uid": "uid-1"}},
         "po_uid": "uid-1", "po_job_started_at": ago(0), "platform_type": provider,
         "cb_cluster_name": BUILD, "cb_app_name": BUILD,
         # the role's defaults with a one-second interval (a task list run here does not load the role's defaults)
         "platformopenshift_install": {"observeIntervalSeconds": 1, "watchWindowMinutes": 120, "deadlineMinutes": 360,
                                       "checkWriteMinutes": 5},
         # the removal's waits: a few one-second rounds (extra vars win over the include's values)
         "rsw_retries": 3, "rsw_delay": 1, "platformopenshift_generation": 1}
    v.update(kw)
    return v


AFTER = {"name": "after", "ansible.builtin.shell": "cat >> {{ trace_file }}",
         "args": {"stdin": "{{ {'after': true} | to_json }}"}, "changed_when": False}


def run_tasks(api, tasks, provider="openstack", **kw):
    return H.run("platformopenshift_provision", "main.yml", base_vars(api, provider, **kw), tasks=tasks)


def after_reached(trace):
    return any(t.get("after") for t in trace)


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class WatchEndings(unittest.TestCase):
    """install_observe.yml: every ending; only installed goes on (to the steps that write Ready)."""

    WATCH = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/install_observe.yml"}, AFTER]

    def watch(self, obj, hub=None, provs=(), **kw):
        api = FakeApi(obj, hub, provs)
        try:
            rc, tr, out = run_tasks(api, self.WATCH, **kw)
        finally:
            api.close()
        return api, rc, tr, out

    def test_object_gone(self):
        api, rc, tr, out = self.watch(None, cd())
        self.assertEqual(rc, 0, out)
        self.assertFalse(after_reached(tr))
        self.assertEqual(api.patches, [])

    def test_another_uid(self):
        api, rc, tr, out = self.watch(po(RUNNING, uid="uid-2"), cd())
        self.assertEqual((rc, after_reached(tr), api.patches), (0, False, []), out)

    def test_being_deleted(self):
        api, rc, tr, out = self.watch(po(RUNNING, deleting=True), cd())
        self.assertEqual((rc, after_reached(tr), api.patches), (0, False, []), out)

    def test_newer_event(self):
        api, rc, tr, out = self.watch(po(RUNNING, event="ev-2"), cd())
        self.assertEqual((rc, after_reached(tr), api.patches), (0, False, []), out)

    def test_installed_goes_on(self):
        api, rc, tr, out = self.watch(po(RUNNING), cd(installed=True))
        self.assertEqual(rc, 0, out)
        self.assertTrue(after_reached(tr))
        self.assertEqual(api.obj["status"]["install"]["stage"], "Finishing")
        self.assertFalse(api.ready_true_written())

    def test_stopped_fails_the_job_after_writing_the_stop(self):
        api, rc, tr, out = self.watch(po(RUNNING), cd(stopped=True, failed="UnknownError", restarts=3),
                                      [failed_provision(0), failed_provision(1), failed_provision(2)])
        self.assertNotEqual(rc, 0)
        self.assertFalse(after_reached(tr))
        st = api.obj["status"]
        self.assertEqual(st["install"]["stage"], "Stopped")
        self.assertEqual(st["phase"], "Failed")
        stalled = [c for c in st["conditions"] if c["type"] == "Stalled"][0]
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "QuotaExceeded"))
        self.assertIn("Cores (72 needed, limit 64)", stalled["message"])
        self.assertNotIn("level=", json.dumps(st))
        self.assertFalse(api.ready_true_written())
        self.assertNotIn("token-not-in-data", H.LAST_OUTPUT)

    def test_the_job_entry_names_the_action(self):
        api, rc, tr, out = self.watch(po(RUNNING), cd(stopped=True, failed="UnknownError", restarts=3),
                                      [failed_provision(2)], tower_job_id="4242",
                                      aap_ui_url="https://controller.example.org/")
        jobs = api.obj["status"]["edaJobs"]
        self.assertEqual((jobs[0]["name"], jobs[0]["jobId"], jobs[0]["status"]),
                         ("platformopenshift-failed", "4242", "failed"))
        self.assertEqual(jobs[0]["url"], "https://controller.example.org/execution/jobs/playbook/4242/output")

    def test_overdue(self):
        """Past the deadline: written failed (InstallOverdue, no Stalled), the job ends without failing and the
        operator's periodic runs check it again."""
        api, rc, tr, out = self.watch(po(dict(RUNNING, startedAt=ago(400))), cd())
        self.assertEqual(rc, 0, out)
        self.assertFalse(after_reached(tr))
        st = api.obj["status"]
        self.assertEqual([c for c in st["conditions"] if c["type"] == "Ready"][0]["reason"], "InstallOverdue")
        self.assertFalse([c for c in st["conditions"] if c["type"] == "Stalled" and c["status"] == "True"])
        self.assertEqual(st["phase"], "Failed")

    def test_window_reached_writes_still_installing_and_ends(self):
        api, rc, tr, out = self.watch(po(RUNNING), cd(), po_job_started_at=ago(121))
        self.assertEqual(rc, 0, out)
        self.assertFalse(after_reached(tr))
        st = api.obj["status"]
        self.assertEqual(st["delivery"]["completedEventId"], "ev-1")
        self.assertEqual(st["phase"], "Creating")
        self.assertFalse(api.ready_true_written())

    def test_a_missing_setting_fails_with_its_name(self):
        api = FakeApi(po(RUNNING), cd())
        try:
            rc, tr, out = run_tasks(api, self.WATCH, platformopenshift_install={"observeIntervalSeconds": 1})
        finally:
            api.close()
        self.assertNotEqual(rc, 0)
        self.assertIn("watchWindowMinutes", out)


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class SameObjectBeforeRelease(unittest.TestCase):
    STEP = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/install_same_object.yml"}, AFTER]

    def check(self, obj):
        api = FakeApi(obj)
        try:
            rc, tr, out = run_tasks(api, self.STEP)
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        return after_reached(tr)

    def test_cases(self):
        self.assertTrue(self.check(po()))
        self.assertFalse(self.check(po(deleting=True)), "a deleting object gets no release")
        self.assertFalse(self.check(None))
        self.assertFalse(self.check(po(uid="uid-2")))
        self.assertFalse(self.check(po(event="ev-2")))


PROVIDER_SPECS = {"openstack": (SPEC, SMALLER),
                  "aws": ({"type": "aws", "aws": {"environment": "env", "workerCount": 2, "publish": "External"}},
                          {"type": "aws", "aws": {"environment": "env", "workerCount": 0, "publish": "External"}}),
                  "gcp": ({"type": "gcp", "gcp": {"environment": "env", "workerCount": 2}},
                          {"type": "gcp", "gcp": {"environment": "env", "workerCount": 0}})}


def stopped_for(provider):
    """A stop recorded with the provider's spec, and an object asking for a new installation (a smaller spec)."""
    first, smaller = PROVIDER_SPECS[provider]
    return dict(STOPPED, stoppedSpec=I.spec_digest(first)), smaller


def kinds_deleted(trace):
    return [t["k8s"]["kind"] for t in trace if "k8s" in t and t["k8s"]["state"] == "absent"]


def creates(trace):
    return [t["create"] for t in trace if "create" in t]


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class Removal(unittest.TestCase):
    """remove_stopped_build.yml: removes only a stopped build someone asked to install again; each provider's own
    spec decides (its digest at the stop)."""

    STEP = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/remove_stopped_build.yml"}, AFTER]

    def remove(self, obj, hub, provider="openstack", provisions=(), **api_kw):
        api = FakeApi(obj, hub, provisions, **api_kw)
        try:
            rc, tr, out = run_tasks(api, self.STEP, provider=provider, rsb_obj=obj or {})
        finally:
            api.close()
        return api, rc, tr, out, kinds_deleted(tr)

    def test_installed_is_never_removed_per_provider(self):
        for provider in PROVIDER_SPECS:
            with self.subTest(provider):
                stop, smaller = stopped_for(provider)
                # the hub says installed (even with a stop recorded and a change asked for): nothing removed, the hub
                # wins (the record takes its state) and the job goes on to the installed path
                api, rc, tr, out, deletes = self.remove(po(stop, generation=2, spec=smaller),
                                                        cd(installed=True, stopped=True), provider)
                self.assertEqual((rc, deletes, after_reached(tr)), (0, [], True), out)
                self.assertEqual(H.patches(tr)[0]["body"]["install"]["stage"], "Installing")
                # the object says installed
                api, rc, tr, out, deletes = self.remove(po(stop, generation=2, spec=smaller, clusterInstalled=True),
                                                        cd(stopped=True), provider)
                self.assertEqual((rc, deletes, after_reached(tr)), (0, [], False), out)
                # a running installation: nothing removed, the job goes on watching it
                api, rc, tr, out, deletes = self.remove(po(RUNNING, generation=2, spec=smaller),
                                                        cd(failed="UnknownError", restarts=1), provider)
                self.assertEqual((rc, deletes, after_reached(tr)), (0, [], True), out)

    def test_stopped_without_a_request_is_kept_and_nothing_is_built(self):
        api, rc, tr, out, deletes = self.remove(po(STOPPED, generation=2,
                                                   spec=dict(SPEC, toolRbac={"clusterAdminRbac": ["admins"]})),
                                                cd(stopped=True))
        self.assertEqual((rc, deletes, after_reached(tr)), (0, [], False), out)
        body = H.patches(tr)[0]["body"]
        self.assertEqual(body["status"], "failed", "the stop is kept (Ready again from Stalled), the event completed")
        self.assertEqual(body["install"], {"stoppedGeneration": 2, "stoppedSpec": I.spec_digest(SPEC),
                                           "stoppedSpecVersion": I.DIGEST_VERSION})

    def test_stopped_and_asked_is_removed_per_provider(self):
        for provider in PROVIDER_SPECS:
            with self.subTest(provider):
                stop, smaller = stopped_for(provider)
                api, rc, tr, out, deletes = self.remove(po(stop, generation=2, spec=smaller), cd(stopped=True),
                                                        provider, cd_reads=2)
                self.assertEqual(rc, 0, out)
                self.assertEqual(deletes, ["ManagedCluster", "ClusterDeployment"], out)
                body = H.patches(tr)[0]["body"]
                self.assertEqual((body["install"]["stage"], body["install"]["run"]), ("CleaningUp", 2))
                self.assertIsNone(body["install"]["lastFailure"])
                stalled = [c for c in body["conditions"] if c["type"] == "Stalled"][0]
                self.assertEqual(stalled["status"], "False")
                self.assertEqual(body["reason"], "InstallCleanup")
                order = [("patch" if "patch" in t else "delete" if "k8s" in t else None) for t in tr]
                self.assertLess(order.index("patch"), order.index("delete"))
                self.assertEqual(creates(tr), ["/api/v1/namespaces/" + BUILD])
                self.assertTrue(after_reached(tr))

    def test_hub_unreadable_removes_nothing(self):
        stop, smaller = stopped_for("openstack")
        obj = po(stop, generation=2, spec=smaller)
        api = FakeApi(obj, cd(stopped=True))
        try:
            v = base_vars(api, central_api_host="http://127.0.0.1:9", rsb_obj=obj)
            rc, tr, out = H.run("platformopenshift_provision", "main.yml", v, tasks=self.STEP)
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual(kinds_deleted(tr), [])
        self.assertFalse(after_reached(tr), "a stopped cluster that may not be removed is never built")

    def test_nothing_left_on_the_hub_starts_fresh(self):
        """A stopped installation someone asked to start again whose build is gone from the hub: no delete is needed,
        but the new installation still starts from the next run (CleaningUp, run 2), never from the stopped block."""
        stop, smaller = stopped_for("openstack")
        api, rc, tr, out, deletes = self.remove(po(stop, generation=2, spec=smaller), None)
        self.assertEqual(rc, 0, out)
        body = H.patches(tr)[0]["body"]
        self.assertEqual((body["install"]["stage"], body["install"]["run"]), ("CleaningUp", 2))
        self.assertTrue(after_reached(tr))
        self.assertEqual(I.install_starting({"install": {"stage": "CleaningUp", "run": 2}},
                                            I.parse_time(ago(0)))["stage"], "Installing")

    def test_recheck_blocks_the_delete_when_the_build_became_installed(self):
        """The fresh read right before the first delete (the build installed between the two reads): no delete."""
        stop, smaller = stopped_for("openstack")
        obj = po(stop, generation=2, spec=smaller)
        api = FlippingApi(obj, cd(stopped=True), cd(installed=True))
        try:
            rc, tr, out = run_tasks(api, self.STEP, rsb_obj=obj)
        finally:
            api.close()
        self.assertEqual(kinds_deleted(tr), [])
        self.assertNotEqual(rc, 0)


class FlippingApi(FakeApi):
    """The ClusterDeployment changes after its first read."""

    def __init__(self, obj, cd_, cd_after):
        super().__init__(obj, cd_)
        self._after = copy.deepcopy(cd_after)
        fake = self
        handler = self.server.RequestHandlerClass
        orig = handler.do_GET

        def do_GET(self):
            orig(self)
            if "clusterdeployments" in self.path:
                fake.cd = fake._after
        handler.do_GET = do_GET


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class RemovalTakeover(unittest.TestCase):
    """A second job during a removal, and a delete during a removal: the removal looks at its object in its waits and
    ends like the watch; a later job continues it; nobody writes Stopped again or builds on a deleting build."""

    REMOVE = {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/remove_stopped_build.yml"}
    WATCH = {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/install_observe.yml"}

    def cleaning(self, **kw):
        return po({"stage": "CleaningUp", "run": 2, "observedAt": ago(2)}, generation=2, spec=SMALLER,
                  lastHandledReconcileRequest="req-2",
                  conditions=[{"type": "Stalled", "status": "False", "reason": "AsExpected", "message": ""}], **kw)

    def deleting_cd(self):
        hub = cd(stopped=True, failed="UnknownError", restarts=3)
        hub["metadata"]["deletionTimestamp"] = ago(1)
        return hub

    def test_second_job_continues_the_removal(self):
        """Job 2 (a second Retry, or the operator's event) finds CleaningUp and the ClusterDeployment being deleted:
        it continues the removal (no second CleaningUp write, no run counted twice), then installs."""
        obj = self.cleaning()
        api = FakeApi(obj, self.deleting_cd(), [failed_provision(2)], cd_reads=3)
        try:
            rc, tr, out = run_tasks(api, [self.REMOVE, AFTER], rsb_obj=obj)
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr), [], "the removal under way is continued as recorded")
        self.assertEqual(kinds_deleted(tr), ["ManagedCluster", "ClusterDeployment"])
        self.assertEqual(creates(tr), ["/api/v1/namespaces/" + BUILD])
        self.assertTrue(after_reached(tr))

    def test_the_watch_never_concludes_a_stop_from_a_deleting_build(self):
        """The reviewer's sequence: removal step and watch on a build that is being deleted (it stays there past the
        removal's waits): the job fails before it builds and nothing writes Stopped or records the request."""
        obj = self.cleaning()
        api = FakeApi(obj, self.deleting_cd(), [failed_provision(2)])
        try:
            rc, tr, out = run_tasks(api, [self.REMOVE, self.WATCH, AFTER], rsb_obj=obj)
            # the watch alone on the deleting build: no conclusion, no write
            api2 = FakeApi(po(dict(RUNNING, startedAt=ago(2)), generation=2), self.deleting_cd(),
                           [failed_provision(2)])
            try:
                rc2, tr2, out2 = run_tasks(api2, [self.WATCH, AFTER], po_job_started_at=ago(121))
            finally:
                api2.close()
        finally:
            api.close()
        self.assertNotEqual(rc, 0)
        self.assertEqual(api.obj["status"]["install"]["stage"], "CleaningUp")
        self.assertNotIn("stoppedRequest", api.obj["status"]["install"])
        self.assertFalse(after_reached(tr))
        # the window ends with nothing concluded: only the hand-over (the event completed, nothing else written)
        self.assertEqual((rc2, after_reached(tr2)), (0, False), out2)
        self.assertEqual([sorted(set(p["status"]) - {"delivery", "edaJobs", "conditions", "phase", "phaseSince",
                                                      "summary"}) for p in api2.patches], [[]])
        self.assertEqual(api2.obj["status"]["delivery"]["completedEventId"], "ev-1")
        self.assertEqual(api2.obj["status"]["install"]["stage"], "Installing")

    def test_a_delete_during_the_removal_ends_the_job(self):
        """The object goes after the recheck (deleted while the build is removed): the next wait ends the job; the
        build's namespace is not created again."""
        stop, smaller = stopped_for("openstack")
        obj = po(stop, generation=2, spec=smaller)
        # reads of the object: decision, recheck, then the first wait finds it gone
        api = FakeApi(obj, cd(stopped=True), obj_reads=2)
        try:
            rc, tr, out = run_tasks(api, [self.REMOVE, AFTER], rsb_obj=obj)
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual(creates(tr), [])
        self.assertFalse(after_reached(tr))
        self.assertEqual(kinds_deleted(tr), ["ManagedCluster"], "the job ended in the first wait")

    def test_a_removal_under_way_with_an_unreadable_hub_hands_over(self):
        """One failed hub read in the decision of a job that continues a removal: nothing is removed, and the event
        is completed (the operator's next pass runs again; no 150 minutes in flight)."""
        obj = self.cleaning()
        api = FakeApi(obj, self.deleting_cd())
        try:
            v = base_vars(api, central_api_host="http://127.0.0.1:9", rsb_obj=obj)
            rc, tr, out = H.run("platformopenshift_provision", "main.yml", v, tasks=[self.REMOVE, AFTER])
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual((kinds_deleted(tr), after_reached(tr)), ([], False))
        self.assertEqual(api.obj["status"]["delivery"]["completedEventId"], "ev-1")
        self.assertEqual(api.obj["status"]["install"]["stage"], "CleaningUp")

    def test_a_newer_request_during_the_removal_ends_the_job(self):
        stop, smaller = stopped_for("openstack")
        obj = po(stop, generation=2, spec=smaller)
        api = FakeApi(obj, cd(stopped=True))
        handler = api.server.RequestHandlerClass
        orig = handler.do_GET
        reads = {"n": 0}

        def do_GET(self):
            if "platformopenshifts" in self.path:
                reads["n"] += 1
                if reads["n"] == 3:
                    api.obj["status"]["delivery"]["eventId"] = "ev-2"
            orig(self)
        handler.do_GET = do_GET
        try:
            rc, tr, out = run_tasks(api, [self.REMOVE, AFTER], rsb_obj=obj)
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual((creates(tr), after_reached(tr)), ([], False))


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class StoppedRuns(unittest.TestCase):
    """main.yml's early steps on a stopped and on an overdue installation."""

    @staticmethod
    def main_tasks(*names):
        import yaml
        with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks", "main.yml")) as fh:
            block = yaml.safe_load(fh)[0]["block"]

        def rooted(task):
            # the role's own task files by role path (the task list runs from another directory)
            task = dict(task)
            inc = task.get("ansible.builtin.include_tasks")
            if inc and "/" not in inc:
                task["ansible.builtin.include_tasks"] = "{{ role_path }}/tasks/" + inc
            if "block" in task:
                task["block"] = [rooted(t) for t in task["block"]]
            return task
        return [rooted(t) for t in block if t.get("name") in names]

    def run_main(self, api, obj, names, **kw):
        tasks = self.main_tasks(*names) + [AFTER]
        return run_tasks(api, tasks, platformopenshift_cr={"resources": [obj]}, platformopenshift_cr_name="demo",
                         platform_spec=obj["spec"], **kw)

    STOPPED_STEPS = ("Read what the hub holds of a stopped installation's build",
                     "End the job, the object is not this job's any more",
                     "The hub's build runs or is installed",
                     "The hub's build is being removed or cannot be read",
                     "Decide whether a stopped installation is asked to start again",
                     "A stopped installation nobody asked to start again")

    def stopped_run(self, hub, asked, **kw):
        """One run of main.yml's first steps on a recorded stop; asked: a Retry is open."""
        obj = po(dict(STOPPED), generation=1, buildName=BUILD,
                 lastHandledReconcileRequest="2026-11-18T12:00:00Z" if asked else "",
                 conditions=[{"type": "Stalled", "status": "True", "reason": "QuotaExceeded", "message": "m."}])
        api = FakeApi(obj, hub)
        try:
            rc, tr, out = self.run_main(api, obj, self.STOPPED_STEPS, **kw)
        finally:
            api.close()
        return api, rc, tr, out

    def test_a_recorded_stop_against_each_hub_state(self):
        """The record's table: one outcome per hub state, asked or not; no dead end, nothing removed here."""
        deleting = cd(stopped=True)
        deleting["metadata"]["deletionTimestamp"] = ago(1)
        rows = [  # hub, unreadable, (outcome not asked, outcome asked)
            (None, False, ("keep", "go")),                 # no ClusterDeployment: a Retry goes on to install fresh
            (cd(stopped=True), False, ("keep", "go")),     # stopped: a Retry goes on to remove and install
            (cd(), False, ("resume", "resume")),           # running: the hub wins, the watch goes on
            (cd(installed=True), False, ("resume", "resume")),   # installed: the installed path, then Ready
            (deleting, False, ("keep", "keep")),           # being deleted: the event completed, the next pass tries
            (cd(stopped=True), True, ("keep", "keep")),    # unreadable: the same
        ]
        for hub, unreadable, outcomes in rows:
            for asked, want in zip((False, True), outcomes):
                with self.subTest(hub=I.hub_state(hub, not unreadable), asked=asked):
                    kw = {"central_api_host": "http://127.0.0.1:9"} if unreadable else {}
                    api, rc, tr, out = self.stopped_run(hub, asked, **kw)
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(kinds_deleted(tr), [])
                    bodies = [p["body"] for p in H.patches(tr)]
                    if want == "go":
                        self.assertEqual((bodies, after_reached(tr)), ([], True))
                    elif want == "resume":
                        self.assertTrue(after_reached(tr), "the provider's steps go on")
                        self.assertEqual(bodies[0]["install"]["stage"], "Installing")
                        self.assertIsNone(bodies[0]["install"]["stoppedSpec"])
                        self.assertNotIn("run", bodies[0]["install"])
                        self.assertEqual([c for c in bodies[0]["conditions"] if c["type"] == "Stalled"][0]["status"],
                                         "False")
                    else:
                        self.assertFalse(after_reached(tr))
                        self.assertEqual(bodies[0]["status"], "failed", "the stop kept, the event completed")

    def test_an_access_group_change_on_a_stopped_cluster_starts_nothing(self):
        for extra in ({"toolRbac": {"clusterAdminRbac": ["admins"]}}, {"clusterViewerRbac": ["readers"]},
                      {"deletionProtection": True}):
            with self.subTest(extra):
                obj = po(STOPPED, generation=2, spec=dict(SPEC, **extra),
                         conditions=[{"type": "Stalled", "status": "True", "reason": "QuotaExceeded",
                                      "message": "Attempt 3 of 3 failed: m."}])
                api = FakeApi(obj, cd(stopped=True))
                try:
                    rc, tr, out = self.run_main(api, obj, self.STOPPED_STEPS)
                finally:
                    api.close()
                self.assertEqual(rc, 0, out)
                self.assertFalse(after_reached(tr), "nothing is built or removed")
                self.assertEqual(kinds_deleted(tr), [])
                body = H.patches(tr)[0]["body"]
                self.assertEqual((body["status"], body["reason"]), ("failed", "QuotaExceeded"))
                self.assertEqual(body["conditions"][0]["message"], "Attempt 3 of 3 failed: m.")
                self.assertEqual(body["install"], {"stoppedGeneration": 2, "stoppedSpec": I.spec_digest(obj["spec"]),
                                                   "stoppedSpecVersion": I.DIGEST_VERSION})

    def test_a_retry_or_a_change_of_the_installation_settings_goes_on(self):
        for obj in (po(STOPPED, generation=2, spec=SMALLER),
                    po(STOPPED, generation=1, lastHandledReconcileRequest="2026-11-18T12:00:00Z")):
            api = FakeApi(obj, cd(stopped=True))
            try:
                rc, tr, out = self.run_main(api, obj, self.STOPPED_STEPS)
            finally:
                api.close()
            self.assertEqual((rc, H.patches(tr), after_reached(tr)), (0, [], True), out)

    OVERDUE_STEP = ("Check an installation past the deadline once",)

    def overdue(self, hub, provisions=()):
        obj = po(dict(RUNNING, startedAt=ago(400)), buildName=BUILD)
        api = FakeApi(obj, hub, provisions)
        try:
            rc, tr, out = self.run_main(api, obj, self.OVERDUE_STEP)
        finally:
            api.close()
        return api, rc, tr, out

    def test_overdue_one_observation_then_the_run_ends(self):
        api, rc, tr, out = self.overdue(cd(failed=None, restarts=0))
        self.assertEqual(rc, 0, out)
        self.assertFalse(after_reached(tr))
        st = api.obj["status"]
        ready = [c for c in st["conditions"] if c["type"] == "Ready"][0]
        self.assertEqual((ready["reason"], st["phase"]), ("InstallOverdue", "Failed"))
        self.assertFalse([c for c in st["conditions"] if c["type"] == "Stalled" and c["status"] == "True"])
        self.assertEqual(st["delivery"]["completedEventId"], "ev-1", "the next periodic run publishes")
        self.assertEqual(len(api.patches), 1, "one observation")

    def test_overdue_installed_goes_on_to_the_providers_steps(self):
        api, rc, tr, out = self.overdue(cd(installed=True))
        self.assertEqual(rc, 0, out)
        self.assertTrue(after_reached(tr))
        self.assertEqual(api.obj["status"]["install"]["stage"], "Finishing")

    def test_overdue_with_an_unreadable_hub_hands_over(self):
        """The single check of an overdue cluster that cannot read the hub completes its event all the same."""
        obj = po(dict(RUNNING, startedAt=ago(400)), buildName=BUILD)
        api = FakeApi(obj, cd())
        try:
            rc, tr, out = self.run_main(api, obj, self.OVERDUE_STEP, central_api_host="http://127.0.0.1:9")
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertFalse(after_reached(tr))
        self.assertEqual(api.obj["status"]["delivery"]["completedEventId"], "ev-1")
        self.assertEqual(api.obj["status"]["install"], obj["status"]["install"], "what the page shows is unchanged")

    def test_overdue_stopped_is_a_stop(self):
        api, rc, tr, out = self.overdue(cd(stopped=True, failed="UnknownError", restarts=3), [failed_provision(2)])
        self.assertNotEqual(rc, 0)
        st = api.obj["status"]
        self.assertEqual(st["install"]["stage"], "Stopped")
        self.assertEqual([c for c in st["conditions"] if c["type"] == "Stalled"][0]["reason"], "QuotaExceeded")

    def test_a_running_installation_inside_the_deadline_is_left_to_the_providers_steps(self):
        obj = po(RUNNING, buildName=BUILD)
        api = FakeApi(obj, cd())
        try:
            rc, tr, out = self.run_main(api, obj, self.OVERDUE_STEP)
        finally:
            api.close()
        self.assertEqual((rc, api.patches, after_reached(tr)), (0, [], True), out)


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class ReleaseAndRescue(unittest.TestCase):
    def test_a_changed_spec_ends_the_job_before_the_release_and_asks_for_a_new_run(self):
        api = FakeApi(po(generation=3))
        try:
            rc, tr, out = run_tasks(api, [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/install_same_object.yml",
                                           "vars": {"iso_generation": 2}}, AFTER])
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertFalse(after_reached(tr))
        nudges = [t["nudge"] for t in tr if "nudge" in t]
        self.assertEqual(nudges, [[{"kind": "PlatformOpenshift", "namespace": NS, "name": "demo"}]])

    def test_the_same_spec_goes_on_without_a_nudge(self):
        api = FakeApi(po(generation=3))
        try:
            rc, tr, out = run_tasks(api, [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/install_same_object.yml",
                                           "vars": {"iso_generation": 3}}, AFTER])
        finally:
            api.close()
        self.assertEqual((rc, after_reached(tr)), (0, True), out)
        self.assertEqual([t for t in tr if "nudge" in t], [])

    def test_an_object_that_cannot_be_read_gets_no_release(self):
        """No release on an unknown object, and no quiet ending: the job fails (its rescue writes once the object can
        be read, which completes the event)."""
        api = FakeApi(po())
        try:
            rc, tr, out = run_tasks(api, [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/install_same_object.yml"},
                                          AFTER], services_api_host="http://127.0.0.1:9")
        finally:
            api.close()
        self.assertNotEqual(rc, 0)
        self.assertIn("could not be read; nothing was released or removed", out)
        self.assertFalse(after_reached(tr))

    def test_a_failed_release_without_a_build_is_a_stop(self):
        obj = po({"stage": "Installing", "run": 2, "startedAt": ago(2), "observedAt": ago(2)})
        api = FakeApi(obj, None)
        try:
            rc, tr, out = run_tasks(api, [{"ansible.builtin.include_tasks":
                                           "{{ role_path }}/tasks/install_release_failed.yml"}, AFTER],
                                    helm_deploy_result={"rc": 1})
        finally:
            api.close()
        self.assertNotEqual(rc, 0)
        st = api.obj["status"]
        self.assertEqual(st["install"]["stage"], "Stopped")
        self.assertEqual([c for c in st["conditions"] if c["type"] == "Stalled"][0]["reason"], "InstallNotStarted")

    def test_a_failed_release_beside_a_build_fails_without_a_stop(self):
        obj = po(RUNNING)
        api = FakeApi(obj, cd())
        try:
            rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                                base_vars(api, helm_deploy_result={"rc": 1}),
                                tasks=[{"ansible.builtin.include_tasks":
                                        "{{ role_path }}/tasks/install_release_failed.yml"}, AFTER],
                                k8s={"ClusterDeployment/%s/%s" % (BUILD, BUILD): cd()})
        finally:
            api.close()
        self.assertNotEqual(rc, 0)
        self.assertEqual(api.patches, [])

    def test_the_rescue_writes_no_text_of_another_system(self):
        """A step that fails with what the hub answered: the status names the step, not the answer; a fail task of
        the role keeps its own sentence."""
        import yaml
        with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks", "main.yml")) as fh:
            rescue = yaml.safe_load(fh)[0]["rescue"][0]
        rescue = dict(rescue, when=["services_api_host is defined"])
        for failing, want in (
                ({"name": "Read the hub's build", "ansible.builtin.slurp": {"src": "/nonexistent/api.hub.example.org"}},
                 'The step "Read the hub\'s build" failed; the platform team can see why in the job.'),
                ({"name": "Refuse", "ansible.builtin.fail": {"msg": "The platform's own sentence."}},
                 "The platform's own sentence.")):
            api = FakeApi(po(RUNNING))
            try:
                rc, tr, out = run_tasks(api, [{"block": [failing], "rescue": [rescue]}],
                                        platformopenshift_cr_name="demo", platformopenshift_cr_namespace=NS)
            finally:
                api.close()
            body = H.patches(tr)[0]["body"]
            self.assertEqual(body["message"], want)
            self.assertNotIn("example.org", json.dumps(body))


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class DefaultFlavors(unittest.TestCase):
    """No site's flavor name is a fallback: where an installation is about to start, a role that will have nodes and
    whose flavor neither the spec nor the installation's defaults name fails the job with the setting's name; an
    installed or installing cluster and a cluster without workers never fail on it."""

    def run_facts(self, spec, defaults, status=None):
        import yaml
        tdir = os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks")
        with open(os.path.join(tdir, "openstack.yml")) as fh:
            facts = [t for t in yaml.safe_load(fh) if t.get("name") == "Set OpenStack spec facts"]
        with open(os.path.join(tdir, "openstack_clusterbuild.yml")) as fh:
            check = [t for t in yaml.safe_load(fh) if t.get("name") == "Fail without the installation's default flavors"]
        with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "defaults", "main.yml")) as fh:
            role_defaults = yaml.safe_load(fh)
        self.assertEqual((len(facts), len(check)), (1, 1))
        v = {k: role_defaults[k] for k in ("default_control_plane_flavor", "default_worker_flavor",
                                           "default_control_plane_count", "default_worker_count")}
        v.update(platform_spec=spec, platformopenshift_defaults=defaults,
                 po_cr_current={"resources": [{"status": status or {}}]},
                 ansible_operator_meta={"name": "demo", "namespace": NS})
        return H.run("platformopenshift_provision", "main.yml", v, tasks=facts + check + [AFTER])

    def test_no_fallback(self):
        rc, tr, out = self.run_facts({"type": "openstack", "openstack": {"environment": "env"}}, {})
        self.assertNotEqual(rc, 0)
        self.assertIn("Set platformopenshift_defaults.controlPlaneFlavor", out)
        rc, tr, out = self.run_facts({"type": "openstack", "openstack": {"environment": "env",
                                                                         "controlPlaneFlavor": "example.xlarge"}},
                                     {"workerFlavor": ""})
        self.assertIn("Set platformopenshift_defaults.workerFlavor", out)
        rc, tr, out = self.run_facts({"type": "openstack", "openstack": {"environment": "env"}},
                                     {"controlPlaneFlavor": "example.xlarge", "workerFlavor": "example.large"})
        self.assertEqual((rc, after_reached(tr)), (0, True), out)

    def test_only_where_an_installation_starts_and_a_role_has_nodes(self):
        none = {"type": "openstack", "openstack": {"environment": "env"}}
        for status in ({"clusterInstalled": True}, {"install": {"stage": "Installed"}},
                       {"install": {"stage": "Installing"}}, {"install": {"stage": "Finishing"}}):
            rc, tr, out = self.run_facts(none, {}, status)
            self.assertEqual((rc, after_reached(tr)), (0, True), status)
        # no workers: the worker default is not needed
        rc, tr, out = self.run_facts({"type": "openstack", "openstack": {"environment": "env", "workerCount": 0}},
                                     {"controlPlaneFlavor": "example.xlarge"})
        self.assertEqual((rc, after_reached(tr)), (0, True), out)

    def test_the_defaults_name_no_flavor(self):
        with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "defaults", "main.yml")) as fh:
            text = fh.read()
        for line in text.splitlines():
            if line.startswith(("default_control_plane_flavor", "default_worker_flavor")):
                self.assertIn("default('', true)", line)


class ServicesAnswerNotInStatus(unittest.TestCase):
    """The failures of reading the AWS and Google Cloud environments carry a fixed sentence; what the services API
    answered goes to the job's log only."""

    def test_fail_texts(self):
        import yaml
        for f, raw in (("aws.yml", "cloudaws_raw"), ("gcp.yml", "cloudgcp_raw")):
            with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks", f)) as fh:
                tasks = yaml.safe_load(fh)
            fails = [t for t in tasks if "ansible.builtin.fail" in t and raw in json.dumps(t)]
            self.assertEqual(len(fails), 1, f)
            msg = fails[0]["ansible.builtin.fail"]["msg"]
            self.assertNotIn(".msg", msg, f)
            self.assertNotIn(".json", msg, f)
            self.assertNotIn("message", msg, f)


class Wiring(unittest.TestCase):
    """Each provider's task files call the removal, the check before the release (with the spec's generation), the
    release and the watch in this order; the watch comes before the steps after the install."""

    TASKS = os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks")

    def names(self, f):
        import yaml
        with open(os.path.join(self.TASKS, f)) as fh:
            return yaml.safe_load(fh)

    def index(self, tasks, pred, what):
        for i, t in enumerate(tasks):
            if pred(t):
                return i
        self.fail("%s not found" % what)

    def include(self, name):
        return lambda t: t.get("ansible.builtin.include_tasks") == name

    def deploy_order(self, f):
        tasks = self.names(f)
        rm = self.index(tasks, self.include("remove_stopped_build.yml"), "removal")
        same = self.index(tasks, self.include("install_same_object.yml"), "check before the release")
        helm = self.index(tasks, lambda t: t.get("name") == "Deploy mce-cluster-build chart", "release")
        failed = self.index(tasks, self.include("install_release_failed.yml"), "failed release")
        self.assertLess(rm, same, f)
        self.assertEqual(same + 1, helm, "%s: the check is right before the release" % f)
        self.assertEqual(failed, helm + 1, f)
        self.assertEqual(tasks[same]["vars"]["iso_generation"], "{{ platformopenshift_generation }}", f)
        self.assertIs(tasks[helm]["failed_when"], False, f)
        return tasks, helm

    def test_openstack(self):
        tasks, helm = self.deploy_order("openstack_clusterbuild.yml")
        watch = self.index(tasks, self.include("install_observe.yml"), "watch")
        monitor = self.index(tasks, self.include("openstack_clusterbuild_monitor.yml"), "post-install")
        self.assertLess(helm, watch)
        self.assertLess(watch, monitor)
        rm = self.index(tasks, self.include("remove_stopped_build.yml"), "removal")
        self.assertEqual(tasks[rm]["vars"]["rsb_obj"], "{{ po_cr_current.resources[0] }}")

    def aws_like(self, f):
        tasks = self.names(f)
        deploy = self.index(tasks, self.include("aws_clusterbuild_deploy.yml"), "deploy")
        post = self.index(tasks, self.include("aws_clusterbuild_postinstall.yml"), "post-install")
        self.assertLess(deploy, post, f)
        dtasks, helm = self.deploy_order("aws_clusterbuild_deploy.yml")
        rm = self.index(dtasks, self.include("remove_stopped_build.yml"), "removal")
        self.assertEqual(dtasks[rm]["vars"]["rsb_obj"], "{{ platformopenshift_cr.resources[0] }}")
        ptasks = self.names("aws_clusterbuild_postinstall.yml")
        self.assertEqual(ptasks[0].get("ansible.builtin.include_tasks"), "install_observe.yml",
                         "the watch is the first post-install step")

    def test_aws(self):
        self.aws_like("aws_clusterbuild.yml")

    def test_gcp(self):
        self.aws_like("gcp_clusterbuild.yml")

    def test_removal_waits_read_the_object(self):
        tasks = self.names("remove_stopped_build.yml")
        block = next(t for t in tasks if t.get("name") == "Remove the stopped build before a new installation")["block"]
        waits = [t for t in block if t.get("ansible.builtin.include_tasks") == "remove_stopped_build_wait.yml"]
        self.assertEqual([w["vars"]["rsw_for"] for w in waits],
                         ["managedcluster", "clusterdeployment", "uninstall", "namespace"])
        names = [t.get("name") for t in block]
        self.assertLess(names.index("Read the object once more before the release is uninstalled"),
                        names.index("Uninstall mce-cluster-build Helm release after Hive deprovision"))
        self.assertEqual(names.index("Read the object once more before the namespace is claimed again") + 1,
                         names.index("Claim the build's namespace again"))
        self.assertNotIn("kubernetes.core.k8s_info", json.dumps(block), "every wait reads the object (the module)")


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class NoReadyWithoutInstall(unittest.TestCase):
    """The old poll's wrong endings: an OpenStack cluster on an external network written Ready without an install, and
    every provider's final write; the guard before each Ready write fails the job instead."""

    def test_guards(self):
        import yaml
        for f in ("openstack.yml", "aws.yml", "gcp.yml"):
            with self.subTest(f):
                with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks", f)) as fh:
                    tasks = yaml.safe_load(fh)
                names = [t.get("name") for t in tasks]
                guard = names.index("Never write Ready for a cluster that is not installed")
                self.assertEqual(names[guard + 1], "Update PlatformOpenshift status")
                rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                                    {"ansible_operator_meta": {"name": "demo", "namespace": NS},
                                     "cb_app_name": BUILD, "cb_cluster_installed": False},
                                    tasks=[tasks[guard], AFTER])
                self.assertNotEqual(rc, 0)
                self.assertFalse(after_reached(tr))

    def test_no_catch_all_could_not_reach(self):
        with open(os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks",
                               "openstack_clusterbuild.yml")) as fh:
            text = fh.read()
        self.assertNotIn("The platform could not reach the cluster", text)
        self.assertNotIn("cd_info is failed", text)


if __name__ == "__main__":
    unittest.main()

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
    (recorded)."""

    def __init__(self, obj, cd=None, provisions=()):
        self.obj = copy.deepcopy(obj)
        self.cd = copy.deepcopy(cd)
        self.provisions = [copy.deepcopy(p) for p in provisions]
        self.patches = []
        self.gets = []
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
                    return self.send(200, fake.obj) if fake.obj else self.send(404, {})
                if path == hive + "clusterdeployments/" + BUILD:
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

    def ready_true_written(self):
        for p in self.patches:
            for c in (p.get("status") or {}).get("conditions") or []:
                if c.get("type") == "Ready" and c.get("status") == "True":
                    return True
        return False


def po(install=None, uid="uid-1", event="ev-1", generation=1, deleting=False, **status):
    st = dict(status)
    st["delivery"] = {"eventId": event, "acknowledgedEventId": event, "jobStartedAt": ago(1)}
    if install is not None:
        st["install"] = install
    meta = {"name": "demo", "namespace": NS, "uid": uid, "generation": generation, "resourceVersion": "7"}
    if deleting:
        meta["deletionTimestamp"] = ago(0)
    return {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "PlatformOpenshift", "metadata": meta,
            "status": st}


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
STOPPED = {"stage": "Stopped", "run": 1, "startedAt": ago(30), "endedAt": ago(20), "stoppedGeneration": 1,
           "stoppedRequest": "", "attempt": 3, "attemptsLimit": 3,
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
                                       "checkWriteMinutes": 5}}
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

    def test_overdue(self):
        api, rc, tr, out = self.watch(po(dict(RUNNING, startedAt=ago(400))), cd())
        self.assertNotEqual(rc, 0)
        self.assertEqual([c for c in api.obj["status"]["conditions"] if c["type"] == "Stalled"][0]["reason"],
                         "InstallOverdue")

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


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class Removal(unittest.TestCase):
    """remove_stopped_build.yml, for each provider: removes only a stopped build someone asked to install again."""

    STEP = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/remove_stopped_build.yml"}, AFTER]

    def remove(self, obj, hub, provider):
        api = FakeApi(obj, hub)
        try:
            rc, tr, out = run_tasks(api, self.STEP, provider=provider,
                                    rsb_status=(obj or {}).get("status") or {})
        finally:
            api.close()
        deletes = [t["k8s"] for t in tr if "k8s" in t and t["k8s"]["state"] == "absent"]
        return api, rc, tr, out, deletes

    def test_installed_is_never_removed_per_provider(self):
        for provider in ("openstack", "aws", "gcp"):
            with self.subTest(provider):
                # the hub says installed (even with a stop recorded and a request)
                api, rc, tr, out, deletes = self.remove(po(STOPPED, generation=2), cd(installed=True, stopped=True),
                                                        provider)
                self.assertEqual((rc, deletes), (0, []), out)
                self.assertTrue(after_reached(tr))
                # the object says installed
                api, rc, tr, out, deletes = self.remove(po(STOPPED, generation=2, clusterInstalled=True),
                                                        cd(stopped=True), provider)
                self.assertEqual((rc, deletes), (0, []), out)
                # a running installation
                api, rc, tr, out, deletes = self.remove(po(RUNNING, generation=2), cd(failed="UnknownError",
                                                                                       restarts=1), provider)
                self.assertEqual((rc, deletes), (0, []), out)

    def test_stopped_without_a_request_is_kept(self):
        api, rc, tr, out, deletes = self.remove(po(STOPPED, generation=1), cd(stopped=True), "openstack")
        self.assertEqual((rc, deletes, api.patches), (0, [], []), out)

    def test_stopped_and_asked_is_removed(self):
        for provider in ("openstack", "aws", "gcp"):
            with self.subTest(provider):
                api, rc, tr, out, deletes = self.remove(po(STOPPED, generation=2), cd(stopped=True), provider)
                kinds = [d["kind"] for d in deletes]
                self.assertEqual(kinds, ["ManagedCluster", "ClusterDeployment"], out)
                # the status write goes through patch_cr_status.yml (recorded by the harness)
                body = H.patches(tr)[0]["body"]
                self.assertEqual((body["install"]["stage"], body["install"]["run"]), ("CleaningUp", 2))
                self.assertIsNone(body["install"]["lastFailure"])
                stalled = [c for c in body["conditions"] if c["type"] == "Stalled"][0]
                self.assertEqual(stalled["status"], "False")
                self.assertEqual(body["reason"], "InstallCleanup")
                # the decision, the CleaningUp write, then the fresh read right before the first delete
                order = [("patch" if "patch" in t else "delete" if "k8s" in t else None) for t in tr]
                self.assertLess(order.index("patch"), order.index("delete"))
                self.assertEqual(sum("clusterdeployments" in g for g in api.gets), 2, "decided, then read again")

    def test_hub_unreadable_removes_nothing(self):
        obj = po(STOPPED, generation=2)
        api = FakeApi(obj, cd(stopped=True))
        try:
            v = base_vars(api, central_api_host="http://127.0.0.1:9", rsb_status=obj["status"])
            rc, tr, out = H.run("platformopenshift_provision", "main.yml", v, tasks=self.STEP)
        finally:
            api.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual([t for t in tr if "k8s" in t and t["k8s"]["state"] == "absent"], [])


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

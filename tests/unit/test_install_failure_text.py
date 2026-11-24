"""Nothing a cloud, the hub, the installer or a helper script returned reaches a tenant-visible field of the cluster
kind: the rescues of the provision and teardown roles write the platform's own sentence.

1. The decision (module_utils shc_install.failure_message) as a table.
2. Each shared call task fed a hostile answer (an address with a scheme, a project id, a token-like string, an id,
   markup), run with Ansible inside the rescue of each cluster role: none of it reaches the status body; the step's
   sentence does, and the answer is in the job's log.
3. A walk of every task file the two cluster roles run (their includes followed, the shared ones too): no fail or
   assert message interpolates a value outside the allow-list below (each entry says why it is the tenant's own or
   the platform's fixed text); templated task names carry only such values or a character that keeps the rescue from
   using the name; every step phrase given to the shared call tasks is plain words.

Example names and documentation values only.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402

sys.path.insert(0, os.path.join(H.EDA, "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                                "plugins", "module_utils"))
import shc_install as I  # noqa: E402

ROLES = os.path.join(H.EDA, "rulebooks", "roles")
NS = "entity-example"
HOSTILE = ("https://iam.googleapis.example/v1/projects/example-proj-4711/serviceAccounts/"
           "sa-1@example-proj-4711.iam.gserviceaccount.example <b onmouseover=alert(1)>x</b> "
           "ya29.a0AfH6SMBexampleexampleexampleexampleexample 0f8fad5b-d9cb-469f-a165-70867728950e "
           "arn:aws:iam::123456789012:role/example")
LEAKS = ("googleapis", "example-proj-4711", "onmouseover", "<b", "ya29", "0f8fad5b", "arn:aws", "123456789012",
         "serviceAccounts")


def sentence(step):
    return I.STEP_FAILED % step


# ---- 1. the decision ---------------------------------------------------------------------------------------------
class Decision(unittest.TestCase):
    def test_table(self):
        cases = [
            # action, msg, task name, refused -> expected
            ("ansible.builtin.fail", "The platform's own sentence.", "Refuse", False, "The platform's own sentence."),
            ("ansible.builtin.assert", "Set platformopenshift_defaults.install.deadlineMinutes (missing).", "x", False,
             "Set platformopenshift_defaults.install.deadlineMinutes (missing)."),
            ("", "PlatformOpenshift demo: the ranges overlap.", "Refuse the cluster", True,
             "PlatformOpenshift demo: the ranges overlap."),
            ("ansible.builtin.uri", "Status code was 403: " + HOSTILE, "Read the hub's build", False,
             sentence("Read the hub's build")),
            ("ansible.builtin.command", "non-zero return code", "Run the helper", False, sentence("Run the helper")),
            # a fail task that carried another system's text (a message the walk would also refuse)
            ("ansible.builtin.fail", "GCP wif-up failed: " + HOSTILE, "Fail", False, sentence("Fail")),
            # a templated task name with a value in it is never used
            ("ansible.builtin.uri", "x", "Read the OpenStack account from Vault oso/sites/site-a/account", False,
             I.NO_STEP_FAILED % "provisioning"),
            ("ansible.builtin.uri", "x", "GCP shc_gcp_cluster.py wif-up --project example-proj-4711", False,
             I.NO_STEP_FAILED % "provisioning"),
            ("ansible.builtin.uri", "x", "Read {{ path }}", False, I.NO_STEP_FAILED % "provisioning"),
            ("ansible.builtin.uri", "x", "", False, I.NO_STEP_FAILED % "provisioning"),
            ("ansible.builtin.fail", "", "Fail", False, sentence("Fail")),
            ("ansible.builtin.fail", "x" * 1001, "Fail", False, sentence("Fail")),
        ]
        for action, msg, step, refused, want in cases:
            with self.subTest(action=action, msg=msg[:40], step=step):
                self.assertEqual(I.failure_message(action, msg, step, refused), want)

    def test_hostile_pieces_alone(self):
        for piece in ("see https://example.org/x", "<script>", "a@b.example", "arn:aws:iam::1:role/x",
                      "projects/example-proj/zones/z", "0f8fad5b-d9cb-469f-a165-70867728950e",
                      "Bearer abc", "A" * 40, "{\"error\": 1}", "C:\\path",
                      # a cloud's quota sentence with the project's number, and one with a host of the cloud
                      "Quota 'CPUS_ALL_REGIONS' exceeded. Limit: 32.0 globally. project 123456789012",
                      "Quota exceeded for quota metric compute.googleapis.com/cpus in region us-east1"):
            with self.subTest(piece=piece):
                self.assertEqual(I.failure_message("ansible.builtin.fail", "Failed: " + piece, "Fail"), sentence("Fail"))

    def test_the_clusters_own_names_are_not_read_by_the_second_line(self):
        """A build name whose digest is all digits, or a name with a date: the platform's own sentence stays."""
        for own, msg in ((["shc-oso-example-demo-12345678"], "The release of the cluster's build shc-oso-example-demo-12345678 "
                                                             "on the hub failed (exit 1); the job's log has the release tool's output."),
                         (["backup-20261119"], "PlatformOpenshift backup-20261119: the cluster's domain is unknown")):
            self.assertEqual(I.failure_message("ansible.builtin.fail", msg, "Fail", own=own), msg)
            self.assertEqual(I.failure_message("ansible.builtin.fail", msg, "Fail"), sentence("Fail"), "without own")
        # another system's number beside the cluster's own name is still refused
        self.assertEqual(I.failure_message("ansible.builtin.fail", "backup-20261119 in project 123456789012", "Fail",
                                           own=["backup-20261119"]), sentence("Fail"))

    def test_the_deletion_wording(self):
        self.assertEqual(I.failure_message("x", "y", "", what="deletion"),
                         "A step of the cluster's deletion failed; the platform team can see why in the job's log.")


# ---- 2. the shared call tasks with a hostile answer --------------------------------------------------------------
def rescue_of(role):
    with open(os.path.join(ROLES, role, "tasks", "main.yml")) as fh:
        tasks = yaml.safe_load(fh)
    block = next(t for t in tasks if "rescue" in t)
    task = dict(block["rescue"][0])
    task["when"] = ["services_api_host is defined"]
    return task


class _Answer(object):
    """A local HTTP server that answers every request with 403 and the hostile text (central's answer)."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                data = json.dumps({"kind": "Status", "message": HOSTILE}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class HostileAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shc-hostile-")
        cls.helper = os.path.join(cls.tmp, "hostile_helper.py")
        with open(cls.helper, "w") as fh:
            fh.write("import json, sys\nsys.stdin.read() if not sys.stdin.isatty() else None\n"
                     "print(json.dumps({'ok': False, 'error': %r}))\nsys.exit(1)\n" % HOSTILE)
        # the call tasks run common/files/<helper>: the same file by a path relative to that directory
        cls.rel = os.path.relpath(cls.helper, os.path.join(H.EDA, "common", "files"))
        cls.answer = _Answer()

    @classmethod
    def tearDownClass(cls):
        cls.answer.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_in_rescue(self, call, role="platformopenshift_provision", real=(), **kw):
        v = {"services_api_host": "http://127.0.0.1:9", "services_bearer_token": "token-not-in-data",
             "central_api_host": self.answer.url, "central_bearer_token": "token-not-in-data",
             "platformopenshift_cr_name": "demo", "platformopenshift_cr_namespace": NS,
             "ansible_operator_meta": {"name": "demo", "namespace": NS}, "os_account_clouds_yaml": "/nonexistent",
             "os_account_cacert": ""}
        v.update(kw)
        rc, trace, out = H.run(role, "main.yml", v, tasks=[{"block": [call], "rescue": [rescue_of(role)]}], real=real)
        bodies = [p["body"] for p in H.patches(trace)]
        self.assertEqual(len(bodies), 1, out)
        return bodies[0], H.LAST_OUTPUT

    def check(self, body, step, what="message"):
        text = json.dumps(body)
        for leak in LEAKS:
            self.assertNotIn(leak, text)
        want = sentence(step)
        self.assertEqual(body["message"], want)
        if what == "deletion":
            self.assertEqual(body["deletion"]["message"], want)

    def test_gcp_call(self):
        step = "Prepare the cluster's identity on Google Cloud"
        body, log = self.run_in_rescue({"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/gcp_call.yml",
                                        "vars": {"gcp_helper": self.rel, "gcp_args": ["wif-up"], "shc_step": step}})
        self.check(body, step)
        self.assertIn("example-proj-4711", log, "the answer goes to the job's log")

    def test_aws_call(self):
        step = "Prepare the cluster's identity on AWS"
        body, log = self.run_in_rescue({"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/aws_call.yml",
                                        "vars": {"aws_helper": self.rel, "aws_config": {}, "aws_args": ["sts-up"],
                                                 "shc_step": step}})
        self.check(body, step)
        self.assertIn("example-proj-4711", log)

    def test_openstack_call(self):
        step = "Reserve the cluster's floating addresses"
        body, log = self.run_in_rescue(
            {"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/openstack_call.yml",
             "vars": {"os_helper": self.rel, "os_request": {"action": "floating-ips"}, "shc_step": step}},
            real=("openstack_call.yml",))
        self.check(body, step)
        self.assertIn("example-proj-4711", log)

    def test_po_call(self):
        step = "Work out the cluster's build name"
        body, log = self.run_in_rescue({"ansible.builtin.include_tasks": "{{ role_path }}/tasks/po_call.yml",
                                        "vars": {"po_request": {"action": HOSTILE}, "shc_step": step}})
        self.check(body, step)

    def test_fabric_check(self):
        step = "Check the cluster's address ranges"
        body, log = self.run_in_rescue(
            {"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/fabric_check.yml",
             "vars": {"fab_check": {"action": HOSTILE}, "shc_step": step}}, real=("fabric_check.yml",))
        self.check(body, step)

    def test_central_create(self):
        step = "Claim the build's namespace on the hub"
        body, log = self.run_in_rescue(
            {"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/central_create.yml",
             "vars": {"cc_path": "/api/v1/namespaces", "shc_step": step,
                      "cc_body": {"apiVersion": "v1", "kind": "Namespace",
                                  "metadata": {"name": "shc-oso-example-demo-0a1b2c3d"}}}},
            real=("central_create.yml",))
        self.check(body, step)
        self.assertIn("example-proj-4711", log)

    def test_the_role_default_step(self):
        """A call without words of its own fails with the role's default step (the role defaults set it)."""
        with open(os.path.join(ROLES, "platformopenshift_provision", "defaults", "main.yml")) as fh:
            step = yaml.safe_load(fh)["shc_step"]
        body, log = self.run_in_rescue({"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/gcp_call.yml",
                                        "vars": {"gcp_helper": self.rel, "gcp_args": ["hive-creds"]}},
                                       shc_step=step)
        self.check(body, step)

    def test_a_task_that_fails_with_the_answer(self):
        """Any other task whose failure carries what was answered: the general sentence with the step's name."""
        body, log = self.run_in_rescue({"name": "Read the hub's build", "ansible.builtin.command":
                                        {"argv": ["python3", self.helper]}})
        self.check(body, "Read the hub's build")

    def test_a_fail_task_that_would_copy_the_answer(self):
        """The second line: a fail message that carries another system's text is not written."""
        body, log = self.run_in_rescue({"name": "Fail on the answer", "ansible.builtin.fail": {"msg": HOSTILE}})
        self.check(body, "Fail on the answer")

    def test_the_teardown_rescue(self):
        step = "Remove the cluster's identity on Google Cloud"
        body, log = self.run_in_rescue(
            {"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/gcp_call.yml",
             "vars": {"gcp_helper": self.rel, "gcp_args": ["wif-down"], "shc_step": step}},
            role="platformopenshift_teardown")
        self.check(body, step, what="deletion")


# ---- 3. the walk ----------------------------------------------------------------------------------------------------
# What the walk allows and why: tests/unit/failure_text_walk.py (ALLOWED, RECORD_FIELDS, SOURCES, DECISION_RESULTS).
import failure_text_walk as FW  # noqa: E402

# Values a task name of the cluster roles may carry besides FW.ALLOWED: when such a name fails, the rescue either
# never uses it (the value always brings a character outside plain words) or the value is the platform's fixed word.
NAME_VALUES = {
    "vault_kv_path": "a Vault path: always has a slash",
    "os_account_vault_path": "a Vault path: always has a slash",
    "aws_helper": "a helper's file name: has a dot and an underscore",
    "gcp_helper": "a helper's file name: has a dot and an underscore",
    "aws_args": "follows the helper's file name in the same name",
    "gcp_args": "follows the helper's file name in the same name",
    "po_request": "its action: a fixed word of the calling task",
    "fab_check": "its action: a fixed word of the calling task",
    "os_request": "its action: a fixed word of the calling task",
    "cc_body": "the kind and name of an object the job creates: the build's namespace",
    "rsw_for": "a fixed word of the calling task",
    "ra_state": "present or absent",
    "po_stopped_hub": "a fixed word of the installation module",
    "ih_why": "a fixed sentence of the calling task",
}

# The reviewers' shapes of a leak (ten, then four more), each added to a copy of a task file: the walk must name each.
MUTATIONS = {
    "fail_via_task_vars": """
- name: Fail with what the site said
  vars:
    _m: "{{ os_result.error | default('') }}"
  ansible.builtin.fail:
    msg: "The floating addresses could not be reserved: {{ _m }}"
  when: false
""",
    "ps_message_from_result": """
- name: Refuse with the site's text
  ansible.builtin.include_tasks: preflight_stop.yml
  vars:
    ps_reason: QuotaExceeded
    ps_message: "PlatformOpenshift {{ ansible_operator_meta.name }}: {{ os_result.error }}"
  when: false
""",
    "status_patch_direct": """
- name: Say what the site answered
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/patch_cr_status.yml"
  vars:
    cr_api_version: hybridsovereign.redhat/v1alpha1
    cr_plural: platformopenshifts
    cr_name: "{{ ansible_operator_meta.name }}"
    cr_namespace: "{{ ansible_operator_meta.namespace }}"
    cr_status_partial: true
    cr_status_body:
      message: "{{ os_result.error }}"
  when: false
""",
    "fail_loop_item": """
- name: Fail with each line
  ansible.builtin.fail:
    msg: "The site said {{ item }}"
  loop: "{{ _os_call.stderr_lines | default([]) }}"
  when: false
""",
    "fail_freeform": """
- name: Fail freeform
  ansible.builtin.fail: "msg={{ os_result.error }}"
  when: false
""",
    "set_fact_then_fail": """
- name: Keep the text
  ansible.builtin.set_fact:
    cb_image_set: "{{ os_result.error }}"
- name: Fail with it
  ansible.builtin.fail:
    msg: "No image set {{ cb_image_set }}"
  when: false
""",
    "fail_direct_result": """
- name: Fail with the answer
  ansible.builtin.fail:
    msg: "The site said {{ os_result.error }}"
  when: false
""",
    "lookup_vars": """
- name: Fail with lookup
  ansible.builtin.fail:
    msg: "The site said {{ lookup('vars', 'os_result').error }}"
  when: false
""",
    "lastfailure_message_path": """
- name: Fail with the hub's text
  ansible.builtin.fail:
    msg: "Stopped: {{ io_obs.install.lastFailure.message }}"
  when: false
""",
    "rescue_failed_result": """
- name: Try
  block:
    - name: Do it
      ansible.builtin.command: "true"
  rescue:
    - name: Keep why
      ansible.builtin.set_fact:
        x_error: "{{ ansible_failed_result.msg }}"
    - name: Write it
      ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/patch_cr_status.yml"
      vars:
        cr_status_partial: true
        cr_status_body:
          conditions: [{type: X, status: "False", reason: Failed, message: "{{ x_error }}"}]
""",    "cr_status_module": """
- name: Write the status directly
  hybridsovereign.shc.cr_status:
    host: "{{ services_api_host }}"
    api_key: "{{ services_bearer_token }}"
    api_version: hybridsovereign.redhat/v1alpha1
    plural: platformopenshifts
    name: "{{ ansible_operator_meta.name }}"
    namespace: "{{ ansible_operator_meta.namespace }}"
    status:
      message: "{{ os_result.error }}"
  when: false
""",
    "uri_patch": """
- name: Patch the status by hand
  ansible.builtin.uri:
    url: "{{ services_api_host }}/apis/hybridsovereign.redhat/v1alpha1/namespaces/x/platformopenshifts/y/status"
    method: PATCH
    body: {status: {message: "{{ os_result.error }}"}}
  when: false
""",
    "record_field_from_result": """
- name: Record the site's answer
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/patch_cr_status.yml"
  vars:
    cr_status_partial: true
    cr_status_body:
      description: "{{ os_result.error }}"
      clusterVersion: "{{ os_result.stderr }}"
  when: false
""",
    "another_key_of_a_helper_root": """
- name: Refuse with the helper's error
  ansible.builtin.include_tasks: preflight_stop.yml
  vars:
    ps_reason: InvalidSpec
    ps_message: "{{ po_result.error }} {{ cl_owner.detail }}"
  when: false
""",
}


def scratch_tree():
    """A copy of the task files the walk reads (both cluster roles, the role they borrow from, common tasks)."""
    tmp = tempfile.mkdtemp(prefix="shc-walk-")
    for rel in ("eda/common/tasks", "eda/rulebooks/roles/platformopenshift_provision",
                "eda/rulebooks/roles/platformopenshift_teardown", "eda/rulebooks/roles/networkplacement_provision"):
        shutil.copytree(os.path.join(H.ROOT, rel), os.path.join(tmp, rel))
    return tmp


class Walk(unittest.TestCase):
    def test_every_written_value(self):
        w = FW.Walk()
        self.assertGreater(len(w.files), 60)
        self.assertGreater(len(w.sinks()), 150)
        self.assertEqual(w.problems(), [])

    def test_each_shape_of_a_leak_is_named(self):
        tmp = scratch_tree()
        try:
            target = os.path.join(tmp, "eda/rulebooks/roles/platformopenshift_provision/tasks/cluster_fips.yml")
            with open(target) as fh:
                orig = fh.read()
            self.assertEqual(FW.Walk(tmp).problems(), [], "the copy is clean")
            caught = []
            for name, text in MUTATIONS.items():
                with open(target, "w") as fh:
                    fh.write(orig + text)
                found = FW.Walk(tmp).problems()
                with self.subTest(name):
                    self.assertTrue(found, name)
                    self.assertTrue(any("cluster_fips.yml" in f for f in found), (name, found))
                if found:
                    caught.append(name)
            self.assertEqual(len(caught), len(MUTATIONS), caught)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_templated_task_names(self):
        """The rescue uses a failed task's (templated) name only when it is plain words: a templated name carries only
        allowed values, or has a character outside plain words in its own text, so it is never used."""
        found = []
        plain = re.compile(r"[A-Za-z ,'()-]*")
        w = FW.Walk()
        for f, t, skip, tv in w.tasks:
            name = t.get("name")
            if skip or not isinstance(name, str) or "{{" not in name:
                continue
            if not plain.fullmatch(re.sub(r"\{\{.*?\}\}", "", name)):
                continue
            paths, _ = FW.uses(name)
            bad = [p for p in paths if not FW.path_allowed(p) and re.split(r"[.\[]", p, 1)[0] not in NAME_VALUES
                   and re.split(r"[.\[]", p, 1)[0] not in (t.get("vars") or {})]
            if bad:
                found.append("%s: %r" % (os.path.relpath(f, H.EDA), name))
        self.assertEqual(found, [])

    def test_step_phrases_are_plain_words(self):
        phrases = []
        for role in FW.CLUSTER_ROLES:
            with open(os.path.join(ROLES, role, "defaults", "main.yml")) as fh:
                d = yaml.safe_load(fh)
            phrases.append(d["shc_step"])
            self.assertIn(d["shc_step_what"], ("provisioning", "deletion"))
        for f, t, skip, tv in FW.Walk().tasks:
            v = (t.get("vars") or {}).get("shc_step")
            if v is not None:
                phrases.append(v)
        self.assertGreater(len(phrases), 30)
        for p in phrases:
            self.assertRegex(p, I.STEP_NAME.pattern + r"\Z")
            self.assertNotIn("{{", p)

    def test_the_shared_calls_fail_with_the_step_when_it_is_set(self):
        """Each shared call task has its step-sentence failure, and its old failure only without a step."""
        common = os.path.join(H.EDA, "common", "tasks")
        for f in ("aws_call.yml", "gcp_call.yml", "openstack_call.yml", "fabric_check.yml", "central_create.yml",
                  "openstack_account.yml"):
            with open(os.path.join(common, f)) as fh:
                tasks = yaml.safe_load(fh)
            step_fails = [t for t in tasks if "ansible.builtin.fail" in t
                          and "shc_step_failed" in t["ansible.builtin.fail"]["msg"]]
            self.assertEqual(len(step_fails), 1, f)
            self.assertTrue(any("shc_step is defined" in w for w in FW._when(step_fails[0])), f)
            for t in tasks:
                for k in ("ansible.builtin.fail", "ansible.builtin.assert"):
                    if k in t and "shc_step_failed" not in json.dumps(t[k]):
                        self.assertTrue(any(FW.SKIP_WHEN in w for w in FW._when(t)), "%s: %s" % (f, t.get("name")))

    def test_both_rescues_use_the_decision(self):
        for role in FW.CLUSTER_ROLES:
            text = json.dumps(rescue_of(role))
            self.assertIn("shc_failure_message", text, role)
            self.assertNotIn("ansible_failed_result.msg", text, role)

    def test_no_lab_default_for_the_cluster_issuer(self):
        """The issuer of a cloud cluster's tokens is the installation's setting: no default, the job names it."""
        for f in ("aws_clusterbuild_facts.yml", "gcp_clusterbuild_facts.yml"):
            with open(os.path.join(ROLES, "platformopenshift_provision", "tasks", f)) as fh:
                text = fh.read()
            self.assertNotRegex(text, r"clusterIssuer[^\n]*default\('[^']+'\)", f)
            self.assertIn("Require the cluster issuer settings", text, f)
        with open(os.path.join(ROLES, "platformopenshift_teardown", "tasks", "main.yml")) as fh:
            text = fh.read()
        self.assertNotRegex(text, r"clusterIssuer[^\n]*default\('[^']+'\)")
        # a delete never needs the issuer setting: the static issuer is found by its exact name and label
        self.assertNotIn("clusterIssuer", text)
        self.assertEqual(text.count("ansible.builtin.include_tasks: unpublish_issuer.yml"), 2)


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class TeardownWithoutSettings(unittest.TestCase):
    """The delete's steps that once needed an installation setting, run with fakes."""

    def run_file(self, f, k8s, **kw):
        v = {"central_api_host": "https://hub.example.invalid", "central_bearer_token": "token-not-in-data",
             "services_api_host": "https://svc.example.invalid", "services_bearer_token": "token-not-in-data"}
        v.update(kw)
        return H.run("platformopenshift_teardown", f, v, k8s=k8s, result="'end'",
                     fakes={"aws_call.yml": H.TRACE % "{'aws': aws_args}"})

    def test_the_issuer_is_found_by_its_exact_name_and_label_in_any_namespace(self):
        b = "shc-aws-example-demo-0a1b2c3d"
        cms = [{"metadata": {"name": "oidc-static-" + b, "namespace": "issuers-a"}},
               {"metadata": {"name": "oidc-static-" + b + "-east", "namespace": "issuers-a"}}]
        rc, tr, out = self.run_file("unpublish_issuer.yml", {"ConfigMap//": cms}, po_build_name=b)
        self.assertEqual(rc, 0, out)
        deletes = [t["k8s"] for t in tr if "k8s" in t]
        self.assertEqual(deletes, [{"state": "absent", "kind": "ConfigMap", "name": "oidc-static-" + b,
                                    "namespace": "issuers-a"}])
        self.assertEqual(tr[-1], {"result": "end"})
        # none: said, and the delete goes on
        rc, tr, out = self.run_file("unpublish_issuer.yml", {"ConfigMap//": []}, po_build_name=b)
        self.assertEqual((rc, [t for t in tr if "k8s" in t], tr[-1]), (0, [], {"result": "end"}), out)
        self.assertIn("nothing to unpublish", out)

    def test_public_subnets_stay_while_another_external_cluster_uses_the_placement(self):
        def po_(name, placement, publish):
            return {"metadata": {"name": name}, "status": {"placement": placement, "publish": publish}}
        common = dict(po_placement="tenant-a/p1", platformopenshift_cr_name="c1", platformopenshift_cr_namespace="tenant-a",
                      po_aws_account="123", po_aws_region="us-east-2", aws_real={})
        placement = {"NetworkPlacement/tenant-a/p1": [{"metadata": {"name": "p1"},
                                                       "status": {"realization": {"site": {"vpc": "vpc-0a1b"}}}}]}
        shared = [po_("c1", "tenant-a/p1", "External"), po_("c2", "tenant-a/p1", "External")]
        rc, tr, out = self.run_file("public_subnets_down.yml", dict(placement, **{"PlatformOpenshift/tenant-a/": shared}),
                                    **common)
        self.assertEqual(rc, 0, out)
        self.assertEqual([t for t in tr if "aws" in t], [], "another External cluster uses them")
        self.assertIn("c2 still uses them", out)
        for others in ([po_("c1", "tenant-a/p1", "External")],
                       [po_("c1", "tenant-a/p1", "External"), po_("c3", "tenant-a/p1", "Internal"),
                        po_("c4", "tenant-a/p2", "External"), {"metadata": {"name": "c5"}}]):
            rc, tr, out = self.run_file("public_subnets_down.yml",
                                        dict(placement, **{"PlatformOpenshift/tenant-a/": others}), **common)
            self.assertEqual(rc, 0, out)
            # the VPC the placement records, never one found by its tag
            self.assertEqual([t["aws"] for t in tr if "aws" in t],
                             [["place-public", "--placement", "tenant-a/p1", "--state", "down", "--vpc-id", "vpc-0a1b"]])

    def test_public_subnets_go_only_from_the_vpc_the_placement_records(self):
        common = dict(po_placement="tenant-a/p1", platformopenshift_cr_name="c1", platformopenshift_cr_namespace="tenant-a",
                      po_aws_account="123", po_aws_region="us-east-2", aws_real={})
        alone = {"PlatformOpenshift/tenant-a/": [{"metadata": {"name": "c1"},
                                                  "status": {"placement": "tenant-a/p1", "publish": "External"}}]}
        # the placement is gone: its VPC went with it, nothing to remove, the delete goes on
        rc, tr, out = self.run_file("public_subnets_down.yml", dict(alone), **common)
        self.assertEqual((rc, [t for t in tr if "aws" in t], tr[-1]), (0, [], {"result": "end"}), out)
        self.assertIn("no longer exists", out)
        # the placement records no VPC: the step stops and nothing is deleted
        for site in ({}, {"vpc": ""}):
            k8s = dict(alone, **{"NetworkPlacement/tenant-a/p1": [{"metadata": {"name": "p1"},
                                                                   "status": {"realization": {"site": site}}}]})
            rc, tr, out = self.run_file("public_subnets_down.yml", k8s, **common)
            self.assertNotEqual(rc, 0, out)
            self.assertEqual([t for t in tr if "aws" in t], [])
            self.assertIn("Remove the cluster's public subnets on AWS", out)


# ---- 4. refusals: the tenant's own sentence, the same in the message and the condition ---------------------------------
sys.path.insert(0, os.path.join(ROLES, "platformopenshift_provision", "files"))
import po_preflight as P  # noqa: E402

LONG = "c" * 40   # a 40-character cluster name without a hyphen
IMAGE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"   # an image the tenant named by its id


def refusals():
    """Every kind of refusal sentence, built as the role and its helpers build it, with realistic values."""
    out = []
    pre = "PlatformOpenshift %s: " % LONG
    out.append(("FlavorNotFound", pre + "the environment has no flavor m1.nosuch; choose a flavor the environment offers"))
    out.append(("ImageNotFound", pre + "the environment has no image %s; choose an image the environment offers" % IMAGE_ID))
    out.append(("ImageNotFound", pre + "the environment has no image rhcos_4_18_custom_image_built_for_tenant_a; choose an "
                                       "image the environment offers"))
    out.append(("NetworkNameNotUnique", pre + "the environment sees 2 networks named shc-n1 (1 of them its own) and the "
                                              "installer takes the network by name; rename or remove the other network "
                                              "of that name, or ask the platform team about a network shared with the "
                                              "environment"))
    out.append(("ExternalNetworkNotListed", pre + "the environment does not see network ext-net as an external network"))
    for r in (P.external({"choice": "lab-internal", "site": "site-hidden-1", "networks": [{"name": "ext-a"}]}),
              P.external({"choice": "", "site": "site-hidden-1", "networks": [{"name": "ext-a"}]}),
              P.fips_check({"network": "ns/n1", "addresses": ["192.0.2.15"],
                            "service": {"networks": [{"network": "other/n9", "prefixes": ["192.0.2.0/28"]}]}})):
        out.append((r["reason"], pre + r["message"]))
    owner = P.hub_owner({"namespace": {"metadata": {"labels": {P.OWNER_LABEL: "uid-of-another-object-0001"}}},
                         "uid": "uid-of-this-object-0002", "recorded": "shc-oso-example-" + LONG[:20],
                         "buildName": "shc-oso-example-" + LONG[:20], "claim": True})
    out.append((owner["reason"], pre + owner["message"]))
    return out


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class RefusalTexts(unittest.TestCase):
    def test_no_platform_identifier_in_a_refusal(self):
        for reason, msg in refusals():
            with self.subTest(reason):
                for word in ("site-hidden-1", "uid-of", "192.0.2.0/28", "other/n9"):
                    self.assertNotIn(word, msg)

    def test_every_refusal_through_the_rescue(self):
        """Through preflight_stop.yml and the real rescue: reason, message and condition carry the sentence."""
        tasks = [{"block": [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/preflight_stop.yml"}],
                  "rescue": [rescue_of("platformopenshift_provision")]}]
        for reason, msg in refusals():
            with self.subTest(reason=reason, msg=msg[:60]):
                rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                                    {"services_api_host": "http://127.0.0.1:9", "services_bearer_token": "t",
                                     "platformopenshift_cr_name": LONG, "platformopenshift_cr_namespace": NS,
                                     "ansible_operator_meta": {"name": LONG, "namespace": NS},
                                     "ps_reason": reason, "ps_message": msg}, tasks=tasks)
                body = H.patches(tr)[0]["body"]
                self.assertEqual(body["reason"], reason, out)
                self.assertEqual(body["message"], msg)
                self.assertEqual([c["message"] for c in body["conditions"]], [msg])

    def test_a_failure_after_a_refusal_was_noted_is_not_the_refusal(self):
        """A refusal noted, then another step fails with a module's text (a failed status write): the general
        sentence, ProvisionFailed, no condition."""
        tasks = [{"block": [
            {"ansible.builtin.set_fact": {"po_fail_reason": "WaitingForPlacement",
                                          "po_preflight_condition": {"type": "Preflight", "status": "False",
                                                                     "reason": "WaitingForPlacement", "message": "waits"}}},
            {"name": "Record what the cluster waits for", "ansible.builtin.fail":
             {"msg": "status write failed (500): " + HOSTILE}}],
            "rescue": [rescue_of("platformopenshift_provision")]}]
        rc, tr, out = H.run("platformopenshift_provision", "main.yml",
                            {"services_api_host": "http://127.0.0.1:9", "services_bearer_token": "t",
                             "platformopenshift_cr_name": "demo", "platformopenshift_cr_namespace": NS}, tasks=tasks)
        body = H.patches(tr)[0]["body"]
        self.assertEqual(body["reason"], "ProvisionFailed")
        self.assertEqual(body["message"], sentence("Record what the cluster waits for"))
        self.assertEqual(body["conditions"], [])


if __name__ == "__main__":
    unittest.main()

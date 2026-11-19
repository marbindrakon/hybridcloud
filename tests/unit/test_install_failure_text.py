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
                      "Bearer abc", "A" * 40, "{\"error\": 1}", "C:\\path"):
            with self.subTest(piece=piece):
                self.assertEqual(I.failure_message("ansible.builtin.fail", "Failed: " + piece, "Fail"), sentence("Fail"))

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
# Values a fail or assert message of the cluster roles may carry, and why each is the tenant's own or the platform's
# fixed text. A key "root" allows the variable wherever it is used; "root.path" allows only that use of it (every
# use of the root must be that path).
ALLOWED = {
    "platformopenshift_cr_name": "the tenant's object name",
    "platformopenshift_cr_namespace": "the tenant's namespace",
    "ansible_operator_meta": "the tenant's object name and namespace",
    "bn_name": "the tenant's object name",
    "cb_app_name": "the build name, already shown in status.buildName",
    "cb_cluster_name": "the build name, already shown in status.buildName",
    "po_build_name": "the build name, already shown in status.buildName",
    "ocp_oidc_client_id": "the build name and a fixed suffix",
    "aws_env_name": "the environment name the tenant set in the spec",
    "gcp_env_name": "the environment name the tenant set in the spec",
    "oso_env_name": "the environment name the tenant set in the spec",
    "po_oso_env": "the environment name the tenant set in the spec",
    "platform_spec": "the tenant's spec (the region the tenant typed)",
    "aws_region": "the environment's region, shown in the tenant's environment status",
    "gcp_region": "the environment's region, shown in the tenant's environment status",
    "cb_placement_ref": "the tenant's network placement name",
    "po_network": "the tenant's network name",
    "po_platform_prefixes": "the address ranges of the tenant's own cluster network",
    "cb_image_set": "the release the tenant chose, or the name of the installation's default release",
    "item": "a setting name from a literal list in the same task",
    "_ocb_missing": "a setting name from a literal list in the same file",
    "teardown_step": "a fixed step word of the teardown",
    "shc_step": "plain words set in the task files and role defaults (checked below)",
    "shc_step_what": "a fixed word set in the role defaults",
    "ps_message": "a refusal: the platform's sentence with values the tenant set (preflight_stop.yml)",
    "po_preflight_condition.message": "the credential refusal's fixed sentence (openstack_cluster_credential.yml)",
    "io_obs.install.lastFailure": "only its reason is printed: a catalogue reason word from the installation module",
    "io_obs.outcome": "a fixed outcome word of the installation module",
    "rsb_recheck.why": "a fixed sentence of the installation module (may_remove_build)",
    "cloudaws_raw.status": "an HTTP status number",
    "cloudgcp_raw.status": "an HTTP status number",
    "helm_deploy_result.rc": "an exit code",
    "cloudoso_cr.resources[0].status.dnsProvider": "compared with a fixed word only, never printed",
    "fabric_realization_models": "the platform's fixed list of model words (only reached outside the cluster roles)",
}
# Values a task name of the cluster roles may carry besides ALLOWED: when such a name fails, the rescue either never
# uses it (the value always brings a character outside plain words) or the value is the platform's fixed word.
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
    "cr_plural": "the kind's plural",
    "cr_name": "the tenant's object name",
}
SKIP_WHEN = "shc_step is not defined"   # a task the cluster roles never run (both set shc_step in their defaults)
COMMON_INCLUDE = re.compile(r"\{\{ role_path \}\}")


def _includes(task):
    for k in ("ansible.builtin.include_tasks", "include_tasks", "ansible.builtin.import_tasks", "import_tasks"):
        if k in task:
            ref = task[k]
            return ref.get("file") if isinstance(ref, dict) else ref
    return None


def _when(task):
    w = task.get("when")
    if w is None:
        return []
    return [str(x) for x in (w if isinstance(w, list) else [w])]


def walk(role):
    """Every task file the role's main.yml reaches, with its tasks flattened: [(file, task)]."""
    role_dir = os.path.join(ROLES, role)
    seen, out = set(), []

    def resolve(cur, ref):
        if ref is None or "{{" in ref.replace("{{ role_path }}", ""):
            return None
        ref = ref.replace("{{ role_path }}", role_dir)
        if os.path.isabs(ref):
            return os.path.normpath(ref)
        for base in (os.path.join(role_dir, "tasks"), os.path.dirname(cur)):
            p = os.path.normpath(os.path.join(base, ref))
            if os.path.exists(p):
                return p
        return None

    def tasks_of(tasks, cur, inherited_skip):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            skip = inherited_skip or any(SKIP_WHEN in w for w in _when(t))
            for k in ("block", "rescue", "always"):
                if k in t:
                    tasks_of(t[k], cur, skip)
            out.append((cur, t, skip))
            ref = _includes(t)
            if ref is not None:
                p = resolve(cur, ref)
                if p and p not in seen:
                    seen.add(p)
                    with open(p) as fh:
                        tasks_of(yaml.safe_load(fh), p, skip)

    main = os.path.join(role_dir, "tasks", "main.yml")
    seen.add(main)
    with open(main) as fh:
        tasks_of(yaml.safe_load(fh), main, False)
    return seen, out


def messages(task):
    for k in ("ansible.builtin.fail", "fail"):
        if k in task and isinstance(task[k], dict) and "msg" in task[k]:
            yield str(task[k]["msg"])
    for k in ("ansible.builtin.assert", "assert"):
        if k in task and isinstance(task[k], dict):
            for mk in ("fail_msg", "msg"):
                if mk in task[k]:
                    yield str(task[k][mk])


def roots(template):
    """The variables a template reads (its own loop and set names, and Jinja's globals, left out)."""
    import jinja2
    from jinja2 import nodes
    ast = jinja2.Environment().parse(template)
    loaded = {n.name for n in ast.find_all(nodes.Name) if n.ctx == "load"}
    stored = {n.name for n in ast.find_all(nodes.Name) if n.ctx in ("store", "param")}
    return loaded - stored - {"loop", "lookup", "query", "range", "dict", "namespace", "true", "false", "none"}


def disallowed(template, local=()):
    bad = []
    for r in sorted(roots(template)):
        if r in ALLOWED or r in local:
            continue
        paths = [k for k in ALLOWED if k.startswith(r + ".") or k.startswith(r + "[")]
        if paths:
            uses = re.findall(r"\b%s\b[\w.\[\]0-9]*" % re.escape(r), template)
            if uses and all(any(u.startswith(p) for p in paths) for u in uses):
                continue
        bad.append(r)
    return bad


class Walk(unittest.TestCase):
    def test_every_fail_and_assert_message(self):
        found = []
        for role in ("platformopenshift_provision", "platformopenshift_teardown"):
            files, tasks = walk(role)
            self.assertGreater(len(files), 10, role)
            for f, t, skip in tasks:
                if skip:
                    continue
                local = set((t.get("vars") or {}).keys())
                for m in messages(t):
                    bad = disallowed(m, local)
                    if bad:
                        found.append("%s: %r interpolates %s" % (os.path.relpath(f, H.EDA), t.get("name"), bad))
        self.assertEqual(found, [])

    def test_templated_task_names(self):
        """The rescue uses a failed task's (templated) name only when it is plain words: a templated name carries only
        allowed values, or has a character outside plain words in its own text, so it is never used."""
        found = []
        plain = re.compile(r"[A-Za-z ,'()-]*")
        for role in ("platformopenshift_provision", "platformopenshift_teardown"):
            _, tasks = walk(role)
            for f, t, skip in tasks:
                name = t.get("name")
                if not isinstance(name, str) or "{{" not in name:
                    continue
                literal = re.sub(r"\{\{.*?\}\}", "", name)
                if not plain.fullmatch(literal):
                    continue
                bad = [b for b in disallowed(name, set((t.get("vars") or {}).keys()) - {"shc_step"})
                       if b not in NAME_VALUES]
                if bad:
                    found.append("%s: %r" % (os.path.relpath(f, H.EDA), name))
        self.assertEqual(found, [])

    def test_step_phrases_are_plain_words(self):
        phrases = []
        for role in ("platformopenshift_provision", "platformopenshift_teardown"):
            with open(os.path.join(ROLES, role, "defaults", "main.yml")) as fh:
                d = yaml.safe_load(fh)
            phrases.append(d["shc_step"])
            self.assertIn(d["shc_step_what"], ("provisioning", "deletion"))
            _, tasks = walk(role)
            for f, t, skip in tasks:
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
            self.assertTrue(any("shc_step is defined" in w for w in _when(step_fails[0])), f)
            for t in tasks:
                for m in messages(t):
                    if "shc_step_failed" not in m:
                        self.assertTrue(any(SKIP_WHEN in w for w in _when(t)), "%s: %s" % (f, t.get("name")))

    def test_both_rescues_use_the_decision(self):
        for role in ("platformopenshift_provision", "platformopenshift_teardown"):
            text = json.dumps(rescue_of(role))
            self.assertIn("shc_failure_message", text, role)
            self.assertNotIn("ansible_failed_result.msg", text, role)


if __name__ == "__main__":
    unittest.main()

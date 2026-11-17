"""The tenant sign-in directory's address (eda/common/tasks/services_keycloak_url.yml), run in ansible-playbook with
the real kubernetes.core.k8s_info against a Kubernetes API over HTTP (fake_k8s_api.py) that applies the label
selector itself: one Route with the label app=keycloak gives its host; two are a plain failure naming both; none
falls back to the job's KEYCLOAK_EXTERNAL_URL, and without it the job fails with a message that names the setting
(or, not required, leaves the address empty). The live roles that need the address use this file, and none of them
or their defaults carries a Keycloak address of its own. Names are examples."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from fake_k8s_api import FakeK8s, route  # noqa: E402
from play_helper import HAVE_ANSIBLE, ROOT  # noqa: E402

TASK = os.path.join(ROOT, "eda", "common", "tasks", "services_keycloak_url.yml")
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
# every live task file that looks the sign-in directory up (common tasks and the rulebook roles)
CALLERS = ["eda/common/tasks/keycloak_client_check.yml", "eda/common/tasks/services_signin_groups.yml",
           "eda/rulebooks/roles/aapconfig_provision/tasks/main.yml",
           "eda/rulebooks/roles/cloudvirt_provision/tasks/access.yml",
           "eda/rulebooks/roles/cloudvirt_teardown/tasks/main.yml",
           "eda/rulebooks/roles/platformopenshift_provision/tasks/keycloak_admin_session.yml",
           "eda/rulebooks/roles/quayconfig_provision/tasks/main.yml",
           "eda/rulebooks/roles/rbac_provision/tasks/main.yml", "eda/rulebooks/roles/rbac_teardown/tasks/main.yml",
           "eda/rulebooks/roles/rbacconfig_provision/tasks/main.yml",
           "eda/rulebooks/roles/rbacconfig_teardown/tasks/main.yml",
           "eda/rulebooks/roles/vault_provision/tasks/configure_oidc.yml",
           "eda/rulebooks/roles/vault_teardown/tasks/main.yml"]


def run(fake, extra=None, env=None):
    tmp = tempfile.mkdtemp(prefix="shc-skc-")
    try:
        out = os.path.join(tmp, "out.json")
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent(f'''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: {TASK}
                    - ansible.builtin.copy:
                        dest: {out}
                        content: "{{{{ {{'url': skc_url, 'source': skc_source}} | to_json }}}}"
                '''))
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump(dict({"services_api_host": fake.url, "services_bearer_token": "token-of-the-job"},
                           **(extra or {})), f)
        with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")
        e = {k: v for k, v in os.environ.items() if k != "KEYCLOAK_EXTERNAL_URL"}
        e.update(ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"), ANSIBLE_LOCALHOST_WARNING="false",
                 ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "lt"),
                 ANSIBLE_REMOTE_TMP=os.path.join(tmp, "rt"), HOME=os.environ.get("HOME", tmp), TMPDIR=tmp,
                 ANSIBLE_NOCOLOR="1")
        e.update(env or {})
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp, env=e,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        res = json.load(open(out)) if os.path.exists(out) else None
        return p.returncode, res, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def have_k8s_collection():
    try:
        import kubernetes  # noqa: F401
    except ImportError:
        return False
    p = subprocess.run(["ansible-doc", "-t", "module", "kubernetes.core.k8s_info", "--json"], capture_output=True,
                       text=True)
    return p.returncode == 0


@unittest.skipUnless(HAVE_ANSIBLE and have_k8s_collection(), "ansible-playbook / kubernetes.core not installed")
class Address(unittest.TestCase):
    def fake(self, *routes):
        f = FakeK8s(routes)
        self.addCleanup(f.stop)
        return f

    def test_the_labelled_route(self):
        fake = self.fake(route("kc-auth", "auth.example.org", {"app": "keycloak"}),
                         route("kc-legacy", "kc.apps.example.org"),
                         route("other-ns", "x.example.org", {"app": "keycloak"}, namespace="elsewhere"))
        rc, res, out = run(fake)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(res, {"url": "https://auth.example.org", "source": "route"})
        lists = fake.route_lists()
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["path"], "/apis/route.openshift.io/v1/namespaces/services-rhbk/routes")
        self.assertEqual(lists[0]["query"].get("labelSelector"), ["app=keycloak"])
        self.assertEqual(lists[0]["auth"], "Bearer token-of-the-job")

    def test_another_namespace(self):
        fake = self.fake(route("kc", "id.example.org", {"app": "keycloak"}, namespace="sso"))
        rc, res, out = run(fake, {"skc_namespace": "sso"})
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(res["url"], "https://id.example.org")

    def test_two_labelled_routes_are_a_plain_failure(self):
        fake = self.fake(route("kc-auth", "auth.example.org", {"app": "keycloak"}),
                         route("kc-legacy", "kc.apps.example.org", {"app": "keycloak"}))
        for required in (True, False):
            rc, res, out = run(fake, {"skc_required": required}, env={"KEYCLOAK_EXTERNAL_URL": "https://s.example.org"})
            self.assertNotEqual(rc, 0)
            self.assertIsNone(res)
            text = " ".join(out.split())
            self.assertIn("2 Routes in namespace services-rhbk on the services cluster carry the label app=keycloak "
                          "(kc-auth for auth.example.org, kc-legacy for kc.apps.example.org)", text)
            self.assertIn("only one Route may carry it", text)

    def test_no_route_the_setting(self):
        fake = self.fake(route("kc-legacy", "kc.apps.example.org"))
        rc, res, out = run(fake, env={"KEYCLOAK_EXTERNAL_URL": "https://sso.example.org/ "})
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(res, {"url": "https://sso.example.org", "source": "setting"})

    def test_no_route_no_setting_names_the_setting(self):
        fake = self.fake()
        rc, res, out = run(fake)
        self.assertNotEqual(rc, 0)
        self.assertIsNone(res)
        text = " ".join(out.split())
        self.assertIn("The sign-in directory's address is not known: no Route in namespace services-rhbk on the "
                      "services cluster carries the label app=keycloak, and the job's environment does not set "
                      "KEYCLOAK_EXTERNAL_URL.", text)

    def test_not_required(self):
        rc, res, out = run(self.fake(), {"skc_required": False})
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(res, {"url": "", "source": "none"})


class Callers(unittest.TestCase):
    """Every live lookup goes through the shared file; no live role or default carries an address of its own."""

    def text(self, rel):
        with open(os.path.join(ROOT, rel)) as f:
            return f.read()

    def test_every_lookup_uses_the_shared_file(self):
        for rel in CALLERS:
            with self.subTest(rel):
                t = self.text(rel)
                self.assertIn("services_keycloak_url.yml", t)
                self.assertNotIn("app=keycloak", t)
                self.assertNotIn("namespace: services-rhbk", t)
        live = sorted(glob.glob(os.path.join(ROOT, "eda", "common", "tasks", "*.yml"))
                      + glob.glob(os.path.join(ROLES, "*", "tasks", "*.yml")))
        found = [os.path.relpath(p, ROOT) for p in live if "app=keycloak" in open(p).read()]
        self.assertEqual(found, ["eda/common/tasks/services_keycloak_url.yml"])

    def test_no_keycloak_address_in_live_roles_or_defaults(self):
        pat = re.compile(r"rhbk-services\.apps|https://rhbk-services|plugin_rbac_keycloak_url_default")
        hits = []
        for p in sorted(glob.glob(os.path.join(ROLES, "*", "*", "*.yml"))
                        + glob.glob(os.path.join(ROOT, "eda", "common", "*", "*.yml"))):
            for n, line in enumerate(open(p), 1):
                if pat.search(line):
                    hits.append(f"{os.path.relpath(p, ROOT)}:{n}")
        self.assertEqual(hits, [])

    def test_the_include_paths(self):
        for rel in CALLERS:
            t = yaml.safe_load(self.text(rel))
            refs = re.findall(r"""include_tasks: ['"]?([^'"\n]*services_keycloak_url\.yml)""", self.text(rel))
            self.assertTrue(refs, rel)
            for ref in refs:
                if rel.startswith("eda/common/"):
                    self.assertEqual(ref, "services_keycloak_url.yml", rel)
                else:
                    self.assertEqual(ref, "{{ role_path }}/../../../common/tasks/services_keycloak_url.yml", rel)
            self.assertTrue(t)


if __name__ == "__main__":
    unittest.main()


GLOBAL_CHECK = os.path.join(ROOT, "tests", "global_tests", "roles", "check_keycloak_oidc", "tasks", "main.yml")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GlobalCheck(unittest.TestCase):
    """tests/global_tests check_keycloak_oidc reads the directory's address from KEYCLOAK_EXTERNAL_URL (no host name
    pattern of its own) and passes only when the discovery document's issuer is that address and realm."""

    def serve(self, issuer_host=None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                me = f"http://{issuer_host or self.headers['Host']}"
                body = json.dumps({"issuer": f"{me}/realms/sovereign-tenants"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", seen

    def check(self, env):
        tmp = tempfile.mkdtemp(prefix="shc-gkc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "out.json")
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent(f'''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: {GLOBAL_CHECK}
                    - ansible.builtin.copy:
                        dest: {out}
                        content: "{{{{ keycloak_oidc_check_passed | bool | to_json }}}}"
                '''))
        e = {k: v for k, v in os.environ.items() if k not in ("KEYCLOAK_EXTERNAL_URL", "KEYCLOAK_REALM")}
        e.update(ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_NOCOLOR="1",
                 ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "lt"), ANSIBLE_REMOTE_TMP=os.path.join(tmp, "rt"), **env)
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml"], cwd=tmp, env=e, capture_output=True,
                           text=True, timeout=300, stdin=subprocess.DEVNULL)
        return p.returncode, (json.load(open(out)) if os.path.exists(out) else None), p.stdout + p.stderr

    def test_the_setting_and_the_issuer(self):
        url, seen = self.serve()
        rc, passed, out = self.check({"KEYCLOAK_EXTERNAL_URL": url + "/"})
        self.assertEqual((rc, passed), (0, True), out[-2000:])
        self.assertEqual(seen, ["/realms/sovereign-tenants/.well-known/openid-configuration"])
        other, _ = self.serve(issuer_host="old-name.example.org")
        rc, passed, out = self.check({"KEYCLOAK_EXTERNAL_URL": other})
        self.assertEqual((rc, passed), (0, False), out[-2000:])

    def test_without_the_setting(self):
        rc, passed, out = self.check({})
        self.assertNotEqual(rc, 0)
        self.assertIn("Set KEYCLOAK_EXTERNAL_URL to the tenant sign-in directory's public address", out)


# the role callers: (task file, the step after the include, the variable it sets, whether the job needs an address)
ROLE_CALLERS = [
    ("aapconfig_provision/tasks/main.yml", "Override keycloak_internal_url with discovered Route",
     ["keycloak_internal_url", "keycloak_external_url"], False),
    ("quayconfig_provision/tasks/main.yml", "Override keycloak_internal_url with discovered Route",
     ["keycloak_internal_url"], False),
    ("platformopenshift_provision/tasks/keycloak_admin_session.yml", "Set the Keycloak URL", ["kc_base_url"], True),
    ("rbac_provision/tasks/main.yml", "Set Keycloak URL", ["svc_keycloak_url"], True),
    ("rbac_teardown/tasks/main.yml", "Set Keycloak URL", ["svc_keycloak_url"], True),
    ("rbacconfig_provision/tasks/main.yml", "Set Keycloak URL", ["plugin_rbac_keycloak_url"], True),
    ("rbacconfig_teardown/tasks/main.yml", "Set Keycloak URL", ["plugin_rbac_keycloak_url"], True),
    ("vault_provision/tasks/configure_oidc.yml", "Set Keycloak external URL", ["kc_external_url"], True),
    ("vault_teardown/tasks/main.yml", "Set Keycloak URL", ["kc_external_url_fin"], True),
    ("cloudvirt_provision/tasks/access.yml", "Keycloak URL", ["cv_kc"], True),
]
BEFORE = {"keycloak_internal_url": "http://kc-internal.example.org:8080",
          "keycloak_external_url": "https://kc-from-env.example.org"}


def run_caller(fake, rel, follow, names, env=None):
    """The caller's own two steps (the include of the shared file, the step that uses its answer), run with the
    role's role_path, the real k8s_info and the fake API; returns (rc, {name: value}, output)."""
    from play_helper import by_name, load
    path = os.path.join(ROLES, rel)
    tasks = load(path)
    steps = [by_name(tasks, "The sign-in directory's address"), by_name(tasks, follow)]
    tmp = tempfile.mkdtemp(prefix="shc-skc-caller-")
    try:
        out = os.path.join(tmp, "out.json")
        with open(os.path.join(tmp, "steps.yml"), "w") as f:
            yaml.safe_dump(steps, f)
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent(f'''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.set_fact:
                        keycloak_internal_url: "{BEFORE['keycloak_internal_url']}"
                        keycloak_external_url: "{BEFORE['keycloak_external_url']}"
                    - ansible.builtin.include_tasks: steps.yml
                    - ansible.builtin.copy:
                        dest: {out}
                        content: "{{{{ skc_test_names | map('extract', vars) | list | to_json }}}}"
                '''))
        role_path = os.path.dirname(os.path.dirname(path))
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump({"services_api_host": fake.url, "services_bearer_token": "token-of-the-job",
                       "role_path": role_path, "skc_test_names": names}, f)
        e = {k: v for k, v in os.environ.items() if k != "KEYCLOAK_EXTERNAL_URL"}
        e.update(ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_NOCOLOR="1",
                 ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "lt"), ANSIBLE_REMOTE_TMP=os.path.join(tmp, "rt"), TMPDIR=tmp)
        e.update(env or {})
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp, env=e,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        res = dict(zip(names, json.load(open(out)))) if os.path.exists(out) else None
        return p.returncode, res, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_ANSIBLE and have_k8s_collection(), "ansible-playbook / kubernetes.core not installed")
class CallerSteps(unittest.TestCase):
    """Each role's own steps around the shared lookup (A3): the labelled Route wins over the setting; without a Route
    the AAP and Quay jobs keep their environment's URLs, every other job takes the setting or fails plainly."""

    def fake(self, *routes):
        f = FakeK8s(routes)
        self.addCleanup(f.stop)
        return f

    def test_the_route_wins_over_the_setting(self):
        fake = self.fake(route("kc-auth", "auth.example.org", {"app": "keycloak"}))
        for rel, follow, names, _ in ROLE_CALLERS:
            with self.subTest(rel):
                rc, res, out = run_caller(fake, rel, follow, names,
                                          env={"KEYCLOAK_EXTERNAL_URL": "https://setting.example.org"})
                self.assertEqual(rc, 0, out[-2000:])
                self.assertEqual(res, {n: "https://auth.example.org" for n in names})

    def test_without_a_route(self):
        fake = self.fake(route("kc-legacy", "kc.apps.example.org"))
        for rel, follow, names, required in ROLE_CALLERS:
            with self.subTest(rel):
                rc, res, out = run_caller(fake, rel, follow, names,
                                          env={"KEYCLOAK_EXTERNAL_URL": "https://setting.example.org"})
                self.assertEqual(rc, 0, out[-2000:])
                if required:
                    self.assertEqual(res, {n: "https://setting.example.org" for n in names})
                else:   # AAP and Quay keep what their environment gave them
                    self.assertEqual(res, {n: BEFORE[n] for n in names})
                rc, res, out = run_caller(fake, rel, follow, names)
                if required:
                    self.assertNotEqual(rc, 0)
                    self.assertIn("The sign-in directory's address is not known", " ".join(out.split()))
                else:
                    self.assertEqual(rc, 0, out[-2000:])
                    self.assertEqual(res, {n: BEFORE[n] for n in names})

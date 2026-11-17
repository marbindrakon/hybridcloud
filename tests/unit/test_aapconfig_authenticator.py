"""The AAP sign-in authenticator of an AAPConfig converges to the Keycloak's current address
(eda/rulebooks/roles/aapconfig_provision/tasks/main.yml): the role's own authenticator tasks run in ansible-playbook
(at verbosity 3) against an AAP gateway authenticator API over HTTP (a fake: slug unique, the configuration replaced
whole on PATCH, SECRET returned masked as "$encrypted$" and kept when a PATCH sends the mask). An authenticator made
with an old Keycloak address gets one PATCH with the new endpoint, client and groups claim; a second run makes no
PATCH; the client secret never reaches the gateway again nor the job's output. Names are examples."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "aapconfig_provision", "tasks", "main.yml")
STEPS = ["The authenticator's wanted settings", "Configure OIDC authenticator via AAP gateway",
         "Handle existing authenticator — fetch by slug", "What differs in the existing authenticator",
         "Converge the existing authenticator (endpoint, client, groups claim)", "Read the authenticator's callback"]
SECRET = "kc-client-secret-value-not-in-output"
MASK = "$encrypted$"


class Gateway:
    def __init__(self):
        self.auths = {}
        self.log = []
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _auth_ok(self):
                return self.headers.get("Authorization") == "Basic " + base64.b64encode(b"admin:aap-pw").decode()

            def _view(self, a):
                cfg = dict(a["configuration"], SECRET=MASK, CALLBACK_URL=f"https://aap.example.org/cb/{a['slug']}/")
                return dict(a, configuration=cfg)

            def do_POST(self):
                body = self._body()
                gw.log.append(("POST", self.path, body))
                if not self._auth_ok():
                    return self._send(401, {})
                if any(a["slug"] == body["slug"] for a in gw.auths.values()):
                    return self._send(400, {"slug": ["authenticator with this slug already exists."]})
                aid = len(gw.auths) + 1
                gw.auths[aid] = dict(body, id=aid)
                return self._send(201, self._view(gw.auths[aid]))

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                gw.log.append(("GET", self.path, None))
                slug = urllib.parse.parse_qs(u.query).get("slug", [""])[0]
                res = [self._view(a) for a in gw.auths.values() if a["slug"] == slug]
                return self._send(200, {"count": len(res), "results": res})

            def do_PATCH(self):
                body = self._body()
                gw.log.append(("PATCH", self.path, body))
                aid = int(self.path.rstrip("/").split("/")[-1])
                a = gw.auths[aid]
                cfg = {k: v for k, v in body["configuration"].items() if k != "CALLBACK_URL"}
                if cfg.get("SECRET") == MASK:      # the mask keeps the stored secret
                    cfg["SECRET"] = a["configuration"]["SECRET"]
                a["configuration"] = cfg          # replaced whole
                return self._send(200, self._view(a))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run(gw, keycloak_url):
    tasks = load(ROLE)
    steps = [by_name(tasks, n) for n in STEPS]
    tmp = tempfile.mkdtemp(prefix="shc-aapauth-")
    try:
        with open(os.path.join(tmp, "tasks.yml"), "w") as f:
            yaml.safe_dump(steps, f)
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write("- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
                    "    - ansible.builtin.include_tasks: tasks.yml\n")
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump({"aap_gateway_internal_url": gw.url, "aap_username": "admin", "aap_password": "aap-pw",
                       "aapconfig_cr_name": "aap-sovereign-services", "keycloak_external_url": keycloak_url,
                       "keycloak_realm": "tenants", "aap_oidc_client_id": "aap-aap-sovereign-services",
                       "aap_oidc_groups_claim": "groups", "_kc_client_secret": {"json": {"value": SECRET}}}, f)
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "lt"),
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "rt"))
        p = subprocess.run(["ansible-playbook", "-vvv", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp,
                           env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Authenticator(unittest.TestCase):
    def setUp(self):
        self.gw = Gateway()
        self.addCleanup(self.gw.stop)

    def patches(self, since=0):
        return [e for e in self.gw.log[since:] if e[0] == "PATCH"]

    def test_a_changed_keycloak_address_converges(self):
        rc, out = run(self.gw, "https://kc.apps.example.org")        # made at the old address
        self.assertEqual(rc, 0, out[-3000:])
        a = self.gw.auths[1]
        self.assertEqual(a["configuration"]["OIDC_ENDPOINT"], "https://kc.apps.example.org/realms/tenants")
        self.assertEqual(a["configuration"]["SECRET"], SECRET)
        self.assertEqual(self.patches(), [])
        n = len(self.gw.log)
        rc, out = run(self.gw, "https://auth.example.org/")          # the Keycloak's new name
        self.assertEqual(rc, 0, out[-3000:])
        patches = self.patches(n)
        self.assertEqual(len(patches), 1)
        sent = patches[0][2]["configuration"]
        self.assertEqual((sent["OIDC_ENDPOINT"], sent["KEY"], sent["GROUPS_CLAIM"]),
                         ("https://auth.example.org/realms/tenants", "aap-aap-sovereign-services", "groups"))
        self.assertEqual(sent["SECRET"], MASK)
        self.assertEqual(a["configuration"]["OIDC_ENDPOINT"], "https://auth.example.org/realms/tenants")
        self.assertEqual(a["configuration"]["SECRET"], SECRET)       # kept by the mask
        self.assertNotIn(SECRET, out)
        n = len(self.gw.log)
        rc, out = run(self.gw, "https://auth.example.org")           # steady: nothing to change
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(self.patches(n), [])
        self.assertNotIn(SECRET, out)
        # only the POST of the first run carried the secret
        carried = [e[0] for e in self.gw.log if e[2] and SECRET in json.dumps(e[2])]
        self.assertEqual(carried, ["POST", "POST", "POST"])          # the create (each run's attempt answers 400)

    def test_a_changed_client_or_claim_converges(self):
        run(self.gw, "https://auth.example.org")
        self.gw.auths[1]["configuration"]["GROUPS_CLAIM"] = "Group"
        n = len(self.gw.log)
        rc, out = run(self.gw, "https://auth.example.org")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(len(self.patches(n)), 1)
        self.assertEqual(self.gw.auths[1]["configuration"]["GROUPS_CLAIM"], "groups")


if __name__ == "__main__":
    unittest.main()

"""The identity write probe (charts/charts/shc-cluster-base/files/identity_write_probe.py) against a fake Keycloak
over HTTP: a write that is taken; a write refused the way a read-only database answers (500); a write answered but
not kept; a refused sign-in; no answer at all; a custom attribute; the session ended afterwards; the credential and
tokens never in the output. Standard library only, so it also runs in the job image."""
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files"))

import identity_write_probe as P  # noqa: E402

PASSWORD = "pw-never-shown"
SECRET = "client-secret-never-shown"
TOKEN = "access-token-never-shown"


class FakeKeycloak:
    def __init__(self, write="ok"):
        self.write = write   # ok | readonly | lost
        self.user = {"id": "u1", "username": "write-check", "email": "write-check@example.com",
                     "firstName": "Write", "lastName": "Check", "attributes": {}}
        self.logged_out = False
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, code, doc=None):
                raw = b"" if doc is None else json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.seen.append(("POST", self.path))
                if self.path == "/realms/platform/protocol/openid-connect/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    if form.get("password") != PASSWORD or form.get("client_secret") != SECRET \
                            or form.get("grant_type") != "password":
                        return self._send(401, {"error": "invalid_grant", "error_description": "Invalid user credentials"})
                    return self._send(200, {"access_token": TOKEN, "refresh_token": "refresh-never-shown"})
                if self.path == "/realms/platform/protocol/openid-connect/logout":
                    outer.logged_out = True
                    return self._send(204)
                if self.path == "/realms/platform/account/":
                    if self.headers.get("Authorization") != "Bearer " + TOKEN:
                        return self._send(401)
                    rep = json.loads(raw)
                    if "userProfileMetadata" in rep:
                        return self._send(400, {"errorMessage": "unexpected field"})
                    if outer.write == "readonly":
                        return self._send(500, {"error": "unknown_error"})
                    if outer.write == "ok":
                        outer.user = rep
                    return self._send(204)
                self._send(404)

            def do_GET(self):
                outer.seen.append(("GET", self.path))
                if self.path == "/realms/platform/account/?userProfileMetadata=false":
                    if self.headers.get("Authorization") != "Bearer " + TOKEN:
                        return self._send(401)
                    return self._send(200, dict(outer.user, userProfileMetadata={"attributes": []}))
                self._send(404)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Probe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="identity-probe-")
        self.cred = os.path.join(self.tmp, "cred")
        os.mkdir(self.cred)
        for k, v in (("username", "write-check"), ("password", PASSWORD), ("clientSecret", SECRET)):
            with open(os.path.join(self.cred, k), "w") as f:
                f.write(v + "\n")

    def run_probe(self, kc, **cfg):
        path = os.path.join(self.tmp, "config.json")
        c = {"url": kc.url, "realm": "platform", "clientId": "write-check", "credentialDir": self.cred}
        c.update(cfg)
        with open(path, "w") as f:
            json.dump(c, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = P.main(["--config", path])
        out = buf.getvalue()
        for secret in (PASSWORD, SECRET, TOKEN, "refresh-never-shown"):
            self.assertNotIn(secret, out)
        return rc, out

    def keycloak(self, **kw):
        kc = FakeKeycloak(**kw)
        self.addCleanup(kc.close)
        return kc

    def test_write_taken(self):
        kc = self.keycloak()
        rc, out = self.run_probe(kc)
        self.assertEqual(rc, 0, out)
        self.assertIn("takes writes (realm platform at", out)
        self.assertTrue(kc.user["lastName"].startswith("write check "))
        self.assertEqual(kc.user["email"], "write-check@example.com")
        self.assertTrue(kc.logged_out)

    def test_read_only_database(self):
        kc = self.keycloak(write="readonly")
        rc, out = self.run_probe(kc)
        self.assertEqual(rc, 1)
        self.assertIn("refuses a write", out)
        self.assertIn("the write of lastName was refused: 500 (unknown_error)", out)
        self.assertTrue(kc.logged_out)

    def test_write_not_kept(self):
        rc, out = self.run_probe(self.keycloak(write="lost"))
        self.assertEqual(rc, 1)
        self.assertIn("does not read back", out)

    def test_sign_in_refused(self):
        with open(os.path.join(self.cred, "password"), "w") as f:
            f.write("wrong")
        rc, out = self.run_probe(self.keycloak())
        self.assertEqual(rc, 2)
        self.assertIn("could not run", out)
        self.assertIn("401 (invalid_grant; Invalid user credentials)", out)

    def test_no_answer(self):
        kc = self.keycloak()
        rc, out = self.run_probe(kc, url="http://127.0.0.1:1")
        self.assertEqual(rc, 2)
        self.assertIn("no answer", out)

    def test_custom_attribute(self):
        kc = self.keycloak()
        rc, out = self.run_probe(kc, attribute="writeCheck")
        self.assertEqual(rc, 0, out)
        self.assertTrue(kc.user["attributes"]["writeCheck"][0].startswith("write check "))
        self.assertEqual(kc.user["lastName"], "Check")

    def test_required_settings(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_probe(self.keycloak(), realm="")
        self.assertIn("realm", str(cm.exception))

    def test_public_client_needs_no_secret(self):
        os.remove(os.path.join(self.cred, "clientSecret"))
        rc, out = self.run_probe(self.keycloak())
        # the fake requires the secret: the sign-in is refused, and the probe says so rather than failing to start
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

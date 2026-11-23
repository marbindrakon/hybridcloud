"""The identity write probe (charts/charts/shc-cluster-base/files/identity_write_probe.py) against a fake Keycloak
over HTTP (through an injected plain-http transport; the script itself takes https only): a client-credentials
token for the service account of a dedicated realm, its own user read and written through the Admin API. A write
that is taken; refused the way a read-only database answers (500); answered but not kept; a refused token; no
answer; a custom attribute; a token without a subject; a redirect not followed; the client secret and the token
never in the output, even when the server echoes them. Standard library only, so it also runs in the job image."""
import base64
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

SECRET = "client-secret-never-shown"
USER_ID = "6f1c7c4e-0000-4000-8000-000000000001"


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return "%s.%s.signature-never-shown" % (enc({"alg": "RS256"}), enc(claims))


TOKEN = jwt({"sub": USER_ID, "azp": "write-check"})


class FakeKeycloak:
    def __init__(self, write="ok", token=TOKEN, echo=False, redirect=False):
        self.write, self.token, self.echo, self.redirect = write, token, echo, redirect
        self.refuse = False
        self.html = False
        self.user = {"id": USER_ID, "username": "service-account-write-check", "attributes": {}}
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, code, doc=None, headers=None):
                raw = b"" if doc is None else json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                return self.headers.get("Authorization") == "Bearer " + outer.token

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.seen.append(("POST", self.path))
                if outer.redirect:
                    return self._send(302, None, {"Location": "https://elsewhere.example/token"})
                if self.path == "/realms/probe/protocol/openid-connect/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    if outer.refuse or form.get("client_secret") != SECRET \
                            or form.get("grant_type") != "client_credentials":
                        doc = {"error": "unauthorized_client", "error_description": "Invalid client credentials"}
                        if outer.echo:
                            doc["error_description"] += " " + form.get("client_secret", "")
                        return self._send(401, doc)
                    return self._send(200, {"access_token": outer.token, "expires_in": 300})
                self._send(404)

            def do_GET(self):
                outer.seen.append(("GET", self.path))
                if self.path == "/admin/realms/probe/users/%s?userProfileMetadata=false" % USER_ID:
                    if not self.authorized():
                        return self._send(401)
                    if outer.html:
                        raw = b"<html>maintenance</html>"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        return self.wfile.write(raw)
                    return self._send(200, dict(outer.user, userProfileMetadata={"attributes": []}))
                self._send(404)

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.seen.append(("PUT", self.path))
                if self.path != "/admin/realms/probe/users/%s" % USER_ID or not self.authorized():
                    return self._send(401)
                rep = json.loads(raw)
                if "userProfileMetadata" in rep:
                    return self._send(400, {"errorMessage": "unexpected field"})
                if outer.write == "readonly":
                    doc = {"error": "unknown_error"}
                    if outer.echo:
                        doc["error"] += " " + outer.token
                    return self._send(500, doc)
                if outer.write == "ok":
                    outer.user = rep
                return self._send(204)

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
        with open(os.path.join(self.cred, "clientSecret"), "w") as f:
            f.write(SECRET + "\n")

    def run_probe(self, kc, transport="default", **cfg):
        path = os.path.join(self.tmp, "config.json")
        c = {"url": kc.url, "realm": "probe", "clientId": "write-check", "credentialDir": self.cred}
        c.update(cfg)
        with open(path, "w") as f:
            json.dump(c, f)
        buf = io.StringIO()
        t = P.make_transport() if transport == "default" else transport
        with contextlib.redirect_stdout(buf):
            rc = P.main(["--config", path], transport=t)
        out = buf.getvalue()
        for secret in (SECRET, TOKEN, "signature-never-shown"):
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
        self.assertIn("takes writes (realm probe at", out)
        self.assertTrue(kc.user["lastName"].startswith("write check "))
        self.assertEqual(kc.user["username"], "service-account-write-check")
        # its own user only, through the Admin API, after one token request
        self.assertEqual([m for m, _ in kc.seen], ["POST", "GET", "PUT", "GET"])

    def test_read_only_database(self):
        rc, out = self.run_probe(self.keycloak(write="readonly", echo=True))
        self.assertEqual(rc, 1)
        self.assertIn("refuses a write", out)
        self.assertIn("the write of lastName was refused: 500 (unknown_error ***)", out)

    def test_write_not_kept(self):
        rc, out = self.run_probe(self.keycloak(write="lost"))
        self.assertEqual(rc, 1)
        self.assertIn("does not read back", out)

    def test_token_refused(self):
        with open(os.path.join(self.cred, "clientSecret"), "w") as f:
            f.write("wrong")
        rc, out = self.run_probe(self.keycloak(echo=True))
        self.assertEqual(rc, 2)
        self.assertIn("could not run", out)
        self.assertIn("401 (unauthorized_client; Invalid client credentials", out)

    def test_secret_echoed_by_the_server_is_not_shown(self):
        kc = self.keycloak(echo=True)
        kc.refuse = True   # the right secret is sent; the server refuses and echoes it
        rc, out = self.run_probe(kc)
        self.assertEqual(rc, 2)
        self.assertIn("Invalid client credentials ***", out)

    def test_a_body_that_is_not_json_is_reported(self):
        kc = self.keycloak()
        kc.html = True
        rc, out = self.run_probe(kc)
        self.assertEqual(rc, 2)
        self.assertIn("reading its own user answered with a body that is not a JSON object", out)

    def test_token_without_subject(self):
        rc, out = self.run_probe(self.keycloak(token=jwt({"azp": "write-check"})))
        self.assertEqual(rc, 2)
        self.assertIn("names no user", out)

    def test_no_answer(self):
        kc = self.keycloak()
        rc, out = self.run_probe(kc, url="http://127.0.0.1:1")
        self.assertEqual(rc, 2)
        self.assertIn("no answer", out)

    def test_redirect_is_not_followed(self):
        rc, out = self.run_probe(self.keycloak(redirect=True))
        self.assertEqual(rc, 2)
        self.assertIn("answered 302", out)

    def test_http_is_refused_without_a_test_transport(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_probe(self.keycloak(), transport=None)
        self.assertIn("must use https", str(cm.exception))

    def test_custom_attribute(self):
        kc = self.keycloak()
        rc, out = self.run_probe(kc, attribute="writeCheck")
        self.assertEqual(rc, 0, out)
        self.assertTrue(kc.user["attributes"]["writeCheck"][0].startswith("write check "))

    def test_required_settings(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_probe(self.keycloak(), realm="")
        self.assertIn("realm", str(cm.exception))
        os.remove(os.path.join(self.cred, "clientSecret"))
        with self.assertRaises(SystemExit) as cm:
            self.run_probe(self.keycloak())
        self.assertIn("clientSecret", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

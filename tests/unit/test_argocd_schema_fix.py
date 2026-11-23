"""The Argo CD schema compatibility shim of the init chart (bootstrap/helm/init: templates/argocd-schema-fix.yaml,
files/schema-fix-server.py).

The server: a peer that connects and then stays silent must not block anybody else. A second client is answered at
once while the silent connection is open, and the silent connection is closed by the handshake timeout. As a
control, the same test against the server shape the chart used before (one thread, TLS wrapped around the listening
socket, so the handshake happens in accept) shows the second client waiting.

The chart: argocdSchemaFix.enabled false renders none of the shim's objects (and its namespace gets no pull secret);
on, two replicas spread over nodes, a disruption budget, HTTPS startup, readiness and liveness probes, resources, and
the ConfigMap carries exactly the server file."""
import http.server
import importlib.util
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

INIT = os.path.join(ROOT, "bootstrap", "helm", "init")
SERVER_FILE = os.path.join(INIT, "files", "schema-fix-server.py")
OPENSSL = shutil.which("openssl")


def load_server_module():
    spec = importlib.util.spec_from_file_location("schema_fix_server", SERVER_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get(port, path="/healthz", timeout=3.0):
    """One HTTPS GET to the server on localhost; the body, or an exception after timeout."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw) as s:
            s.sendall(("GET %s HTTP/1.0\r\nHost: x\r\n\r\n" % path).encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    if not head.startswith(b"HTTP/1.0 200"):
        raise AssertionError(head[:80])
    return body


@unittest.skipUnless(OPENSSL, "openssl not installed")
class SilentPeer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shc-schemafix-")
        cls.key = os.path.join(cls.tmp, "server.key")
        cls.crt = os.path.join(cls.tmp, "server.crt")
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cls.key, "-out", cls.crt,
                        "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
        cls.mod = load_server_module()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def start(self, httpd):
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def silent_peer(self, port):
        s = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(s.close)
        time.sleep(0.2)  # the server has accepted it
        return s

    def test_a_silent_peer_does_not_block_a_second_client(self):
        shims = self.mod.Shims({"io.example~1v1~1Thing": {"type": "object"}})
        httpd = self.mod.make_server(("127.0.0.1", 0), self.crt, self.key, shims, handshake_timeout=1.0,
                                     read_timeout=1.0)
        port = self.start(httpd)
        silent = self.silent_peer(port)
        started = time.monotonic()
        self.assertEqual(get(port, timeout=3.0), b"ok")
        self.assertLess(time.monotonic() - started, 1.0, "the second client waited for the silent one")
        self.assertIn(b"io.example~1v1~1Thing", get(port, "/openapi/v2"))
        self.assertIn(b"schemafix.sovereign-bootstrap.io/v1", get(port, "/apis/schemafix.sovereign-bootstrap.io/v1"))
        # the silent connection is closed by the handshake timeout
        silent.settimeout(3.0)
        self.assertEqual(silent.recv(1), b"")

    def test_a_peer_silent_after_the_handshake_is_closed_by_the_read_timeout(self):
        httpd = self.mod.make_server(("127.0.0.1", 0), self.crt, self.key, self.mod.Shims(), handshake_timeout=1.0,
                                     read_timeout=0.5)
        port = self.start(httpd)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        tls = ctx.wrap_socket(raw)
        self.addCleanup(tls.close)
        self.assertEqual(get(port), b"ok")
        self.assertEqual(tls.recv(1), b"")  # closed by the server within the read timeout

    def test_past_the_connection_cap_a_connection_is_closed_not_queued(self):
        httpd = self.mod.make_server(("127.0.0.1", 0), self.crt, self.key, self.mod.Shims(), handshake_timeout=2.0,
                                     read_timeout=2.0, max_connections=1)
        port = self.start(httpd)
        self.silent_peer(port)
        with self.assertRaises((OSError, ssl.SSLError)):
            get(port, timeout=1.5)

    def test_control_the_former_server_shape_is_blocked_by_a_silent_peer(self):
        """The shape of the chart's former server: HTTPServer, TLS around the listening socket."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.crt, self.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        port = self.start(httpd)
        silent = self.silent_peer(port)
        with self.assertRaises((OSError, ssl.SSLError)):
            get(port, timeout=1.5)
        silent.close()  # lets the blocked accept end, so the server can shut down


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Chart(unittest.TestCase):
    BASE = ["--set", "bootstrap.operator=true", "--set", "bootstrap.secrets=true",
            "--set", "clusters.services.bearerToken=example-token"]

    def render(self, *sets):
        args = ["helm", "template", "t", INIT] + self.BASE
        for s in sets:
            args += ["--set", s]
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]

    @staticmethod
    def shim_objects(docs):
        return [d for d in docs if d["metadata"].get("namespace") == "argocd-schema-fix"
                or d["metadata"].get("name") in ("argocd-schema-fix", "schema-fix-openapi-reader",
                                                 "v1.schemafix.sovereign-bootstrap.io")]

    def test_on_by_default_with_every_object(self):
        docs = self.shim_objects(self.render())
        # set: offline (no cluster to look up) the pull secrets' template renders the Namespace a second time
        self.assertEqual(sorted(set(d["kind"] for d in docs)),
                         sorted(["Namespace", "ServiceAccount", "ClusterRole", "ClusterRoleBinding", "ConfigMap",
                                 "Deployment", "PodDisruptionBudget", "Service", "APIService", "Secret"]))

    def test_off_renders_none_of_its_objects(self):
        docs = self.render("argocdSchemaFix.enabled=false")
        self.assertEqual(self.shim_objects(docs), [])
        # the other pull secrets stay
        self.assertTrue([d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "quay-pull-secret"])

    def test_the_server_is_hardened(self):
        docs = self.render()
        dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "schema-fix-server")
        self.assertEqual(dep["spec"]["replicas"], 2)
        self.assertEqual(dep["spec"]["strategy"]["rollingUpdate"], {"maxSurge": 1, "maxUnavailable": 0})
        pod = dep["spec"]["template"]["spec"]
        spread = pod["topologySpreadConstraints"][0]
        self.assertEqual(spread["topologyKey"], "kubernetes.io/hostname")
        self.assertEqual(spread["labelSelector"]["matchLabels"], {"app": "schema-fix-server"})
        c = pod["containers"][0]
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(c[probe]["httpGet"], {"path": "/healthz", "port": 6443, "scheme": "HTTPS"}, probe)
        self.assertEqual(c["startupProbe"]["periodSeconds"] * c["startupProbe"]["failureThreshold"], 600)
        self.assertEqual(c["resources"], {"requests": {"cpu": "50m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}})
        env = {e["name"]: e["value"] for e in c["env"]}
        self.assertEqual(env, {"SCHEMA_FIX_HANDSHAKE_TIMEOUT": "10", "SCHEMA_FIX_READ_TIMEOUT": "30"})
        pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")
        self.assertEqual(pdb["spec"], {"minAvailable": 1, "selector": {"matchLabels": {"app": "schema-fix-server"}}})

    def test_one_replica_has_no_disruption_budget(self):
        docs = self.render("argocdSchemaFix.replicas=1", "argocdSchemaFix.handshakeTimeoutSeconds=5")
        self.assertEqual([d for d in docs if d["kind"] == "PodDisruptionBudget"], [])
        dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "schema-fix-server")
        self.assertEqual(dep["spec"]["replicas"], 1)
        env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["SCHEMA_FIX_HANDSHAKE_TIMEOUT"], "5")

    def test_the_configmap_carries_the_server_file(self):
        cm = next(d for d in self.render() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "schema-fix-server")
        with open(SERVER_FILE) as f:
            self.assertEqual(cm["data"]["server.py"].rstrip("\n"), f.read().rstrip("\n"))
        self.assertIn("exec python3 /config/server.py", cm["data"]["serve.sh"])

    def test_the_values_file_documents_the_defaults(self):
        with open(os.path.join(INIT, "values.yaml")) as f:
            sf = yaml.safe_load(f)["argocdSchemaFix"]
        self.assertIs(sf["enabled"], True)
        self.assertEqual(sf["replicas"], 2)
        self.assertEqual((sf["handshakeTimeoutSeconds"], sf["readTimeoutSeconds"]), (10, 30))


if __name__ == "__main__":
    unittest.main()

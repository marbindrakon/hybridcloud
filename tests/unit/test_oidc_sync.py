"""The oidc-issuer chart's sync script (charts/charts/oidc-issuer/files/oidc-sync.sh) run with a fake `oc` and `curl`:
every issuer fetched; an acm cluster whose ManagedCluster is not available keeps its last documents and the run
succeeds; the same failure while the ManagedCluster is available is a fault (the documents are kept, the run fails and
the termination message names the issuer); the local cluster failing is a fault; an unavailable cluster never
published is reported, not a fault; oidc-documents that cannot be read stops the run before anything is applied.
Hosts and names are made up."""
import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "charts", "charts", "oidc-issuer", "files", "oidc-sync.sh")
BASE = "https://issuer.example.test"
ANN = "shc.signal9.gg/oidc-fetched-"

# The fake oc: state in $FAKE/state.json; prints what the real one would or exits 1.
FAKE_OC = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    fake = os.environ["FAKE"]
    st = json.load(open(os.path.join(fake, "state.json")))
    a = sys.argv[1:]
    server = next((x.split("=", 1)[1] for x in a if x.startswith("--server=")), None)
    a = [x for x in a if not x.startswith(("--server=", "--token=", "--certificate-authority="))]
    def out(s):
        sys.stdout.write(s); sys.exit(0)
    if a[:2] == ["get", "--raw"]:
        cluster = server.rsplit("/", 1)[1] if server else "local"
        if cluster in st["down"]:
            sys.stderr.write("No agent available\n"); sys.exit(1)
        if a[2] == "/openid/v1/jwks":
            out(json.dumps({"keys": [{"kid": cluster}]}))
        out(json.dumps({"issuer": "https://kubernetes.default.svc", "jwks_uri": "x"}))
    if a[:3] == ["get", "configmap", "oidc-documents"]:
        if st.get("cm_unreadable"):
            sys.exit(1)
        out(json.dumps(st["cm"]))
    if a[:2] == ["get", "configmap"] and "-l" in a:
        out(json.dumps({"items": []}))
    if a[:2] == ["get", "secret"]:
        ns = a[a.index("-n") + 1]
        out(st["tokens"].get(ns, ""))
    if a[:2] == ["get", "managedcluster"]:
        mc = st["mc"].get(a[2])
        if mc is None:
            sys.exit(1)
        out(json.dumps({"status": {"conditions": [{"type": "ManagedClusterConditionAvailable", "status": mc[0],
                                                   "reason": mc[1], "lastTransitionTime": mc[2]}]}}))
    if a[:2] == ["create", "configmap"]:
        data = {}
        for x in a:
            if x.startswith("--from-file="):
                k, p = x[len("--from-file="):].split("=", 1)
                data[k] = open(p).read()
        out(json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "oidc-documents"}, "data": data}))
    if a[:1] == ["apply"]:
        open(os.path.join(fake, "applied.json"), "w").write(sys.stdin.read()); sys.exit(0)
    sys.stderr.write("fake oc: unexpected %r\n" % a); sys.exit(2)
''').lstrip()


def b64(s):
    return base64.b64encode(s.encode()).decode()


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "needs bash and jq")
class OidcSync(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.bin = os.path.join(self.d, "bin")
        os.makedirs(self.bin)
        for name, body in (("oc", FAKE_OC), ("curl", "#!/bin/sh\nexit 1\n")):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.work = os.path.join(self.d, "work")
        os.makedirs(self.work)
        self.state = {
            "down": [], "tokens": {"siteA": b64("tok-a"), "siteB": b64("tok-b")},
            "mc": {"siteA": ["True", "ManagedClusterAvailable", "2026-01-01T00:00:00Z"],
                   "siteB": ["True", "ManagedClusterAvailable", "2026-01-01T00:00:00Z"]},
            "cm": {"metadata": {"annotations": {ANN + "siteB": "2026-01-02T03:04:05Z"}},
                   "data": {"siteB__keys.json": '{"keys":[{"kid":"old-b"}]}',
                            "siteB__openid-configuration": '{"issuer":"%s/siteB"}' % BASE}},
        }

    def run_sync(self):
        with open(os.path.join(self.d, "state.json"), "w") as f:
            json.dump(self.state, f)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE=self.d, NAMESPACE="oidc",
                   BASE_URL=BASE, MSA="sync", PROXY_URL="https://proxy.example.test", STATIC_LABEL="static",
                   STATIC_NAME_LABEL="static-name", CLUSTERS="hub local\nsiteA acm\nsiteB acm\n",
                   SA_CA="/dev/null", WORKDIR=self.work, TERMINATION_LOG=os.path.join(self.d, "termination"))
        p = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=60)
        applied = None
        if os.path.exists(os.path.join(self.d, "applied.json")):
            with open(os.path.join(self.d, "applied.json")) as f:
                applied = json.load(f)
        term = None
        if os.path.exists(os.path.join(self.d, "termination")):
            with open(os.path.join(self.d, "termination")) as f:
                term = f.read()
        return p, applied, term

    def test_all_fetched(self):
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("published: hub siteA siteB", p.stdout)
        self.assertEqual(json.loads(applied["data"]["siteB__keys.json"])["keys"][0]["kid"], "siteB")
        self.assertEqual(json.loads(applied["data"]["siteA__openid-configuration"])["issuer"], BASE + "/siteA")
        self.assertEqual(set(applied["metadata"]["annotations"]), {ANN + "hub", ANN + "siteA", ANN + "siteB"})
        self.assertIsNone(term)

    def test_unavailable_site_keeps_documents_and_succeeds(self):
        self.state["down"] = ["siteB"]
        self.state["mc"]["siteB"] = ["Unknown", "ManagedClusterLeaseUpdateStopped", "2026-01-03T00:00:00Z"]
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("siteB: keeping the documents fetched 2026-01-02T03:04:05Z (unreachable: ManagedCluster siteB "
                      "Available=Unknown (ManagedClusterLeaseUpdateStopped) since 2026-01-03T00:00:00Z", p.stdout)
        self.assertEqual(json.loads(applied["data"]["siteB__keys.json"])["keys"][0]["kid"], "old-b")
        # the fetch time of the kept documents is carried over
        self.assertEqual(applied["metadata"]["annotations"][ANN + "siteB"], "2026-01-02T03:04:05Z")
        self.assertIsNone(term)

    def test_failure_while_available_is_a_fault(self):
        self.state["down"] = ["siteB"]
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(applied["data"]["siteB__keys.json"])["keys"][0]["kid"], "old-b")
        self.assertIn("siteB: fetch through the cluster proxy failed while ManagedCluster siteB is available (fault)", term)

    def test_unreadable_managedcluster_is_a_fault(self):
        self.state["down"] = ["siteB"]
        del self.state["mc"]["siteB"]
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 1)
        self.assertIn("ManagedCluster siteB cannot be read", term)

    def test_local_failure_is_a_fault(self):
        self.state["down"] = ["local"]
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 1)
        self.assertIn("hub: fetch from this cluster's API failed (fault)", term)
        self.assertIn("published: siteA siteB", p.stdout)

    def test_unavailable_never_published_is_not_a_fault(self):
        self.state["down"] = ["siteA"]
        self.state["tokens"]["siteA"] = ""
        self.state["mc"]["siteA"] = ["Unknown", "ManagedClusterLeaseUpdateStopped", "2026-01-03T00:00:00Z"]
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("siteA: nothing published yet (unreachable: ManagedCluster siteA Available=Unknown", p.stdout)
        self.assertIn("no ManagedServiceAccount token", p.stdout)
        self.assertNotIn("siteA__keys.json", applied["data"])

    def test_documents_unreadable_applies_nothing(self):
        self.state["cm_unreadable"] = True
        p, applied, term = self.run_sync()
        self.assertEqual(p.returncode, 1)
        self.assertIsNone(applied)
        self.assertIn("oidc-documents: cannot be read", term)


if __name__ == "__main__":
    unittest.main()

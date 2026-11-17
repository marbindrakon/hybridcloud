"""Quay's sign-in server in the config bundle follows a change of the chart's oidc values (bootstrap/helm/charts/quay).

The settings the db-init Sync hook merges into the config bundle are in a tracked ConfigMap (not only in the hook's
environment): a changed oidc.server changes that object, so the Application goes OutOfSync and its sync runs the
hook again. Judged on the rendered chart (helm template) and by running the hook's own merge script, extracted from
the rendered Job, with a stand-in for `oc` that holds the bundle Secret: an old server is replaced, every other key
of the bundle (database, secret key, storage keys) stays, a second run patches nothing, no secret is printed.
Names are examples."""
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

CHART = os.path.join(ROOT, "bootstrap", "helm", "charts", "quay")
VALUES = {"namespace": "reg", "registry": {"name": "reg1", "configBundleSecretName": "reg1-config-bundle",
                                          "components": {"postgres": {"managed": False}}},
          "crunchy": {"enabled": True, "name": "pg"},
          "oidc": {"enabled": True, "clientId": "quay-client", "server": "https://kc.apps.example.org/realms/tenants",
                   "clientSecret": {"vaultKey": "v", "property": "p"}},
          "rgw": {"enabled": True, "hostname": "s3.example.org", "bucket": "b", "port": 443, "isSecure": True,
                  "storagePath": "/r", "credentials": {"vaultKey": "v", "accessKeyProperty": "a", "secretKeyProperty": "s"}}}
SECRETS = ("oidc-client-secret-value", "rgw-access-value", "rgw-secret-value", "quay-secret-key-value")


def render(server):
    v = json.loads(json.dumps(VALUES))
    v["oidc"]["server"] = server
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(v, f)
    try:
        p = subprocess.run(["helm", "template", "q", CHART, "-f", f.name], capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(f.name)
    assert p.returncode == 0, p.stderr
    return [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]


def obj(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


FAKE_OC = textwrap.dedent('''\
    #!/usr/bin/env python3
    import base64, json, os, sys
    store = os.environ["FAKE_SECRET"]
    a = sys.argv[1:]
    data = json.load(open(store))
    if a[:2] == ["get", "secret"]:
        sys.stdout.write(data["config.yaml"])        # jsonpath of .data.config.yaml: base64
    elif a[:2] == ["patch", "secret"]:
        body = json.loads(a[a.index("-p") + 1])
        data.update(body["data"])
        json.dump(data, open(store, "w"))
        open(store + ".patches", "a").write("patch\\n")
    else:
        sys.exit("unexpected oc call: %s" % a)
    ''')


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class BundleSettings(unittest.TestCase):
    def test_the_settings_are_a_tracked_object_that_follows_the_values(self):
        old, new = render("https://kc.apps.example.org/realms/tenants"), render("https://auth.example.org/realms/tenants/")
        cm_old, cm_new = obj(old, "ConfigMap", "reg1-bundle-settings"), obj(new, "ConfigMap", "reg1-bundle-settings")
        self.assertNotIn("argocd.argoproj.io/hook", cm_new["metadata"].get("annotations", {}))
        s_old, s_new = json.loads(cm_old["data"]["settings.json"]), json.loads(cm_new["data"]["settings.json"])
        self.assertEqual(s_old["RHBK_LOGIN_CONFIG"]["OIDC_SERVER"], "https://kc.apps.example.org/realms/tenants/")
        self.assertEqual(s_new["RHBK_LOGIN_CONFIG"]["OIDC_SERVER"], "https://auth.example.org/realms/tenants/")
        self.assertNotEqual(cm_old, cm_new)                 # the change Argo sees
        for k in ("CLIENT_SECRET", "access_key", "secret_key"):
            self.assertNotIn(k, cm_new["data"]["settings.json"])
        job = obj(new, "Job", "quay-db-init")
        self.assertEqual(job["metadata"]["annotations"]["argocd.argoproj.io/hook"], "Sync")
        self.assertLess(int(cm_new["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
                        int(job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]))
        env = {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["SHC_BUNDLE_SETTINGS"]["valueFrom"],
                         {"configMapKeyRef": {"name": "reg1-bundle-settings", "key": "settings.json"}})

    def test_the_hooks_merge_replaces_an_old_server_and_keeps_the_rest(self):
        docs = render("https://auth.example.org/realms/tenants")
        settings = obj(docs, "ConfigMap", "reg1-bundle-settings")["data"]["settings.json"]
        cmd = obj(docs, "Job", "quay-db-init")["spec"]["template"]["spec"]["containers"][0]["command"][2]
        script = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", cmd, re.S).group(1)
        script = textwrap.dedent(script)
        tmp = tempfile.mkdtemp(prefix="shc-quay-merge-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bundle = {"SERVER_HOSTNAME": "reg.example.org", "SECRET_KEY": "quay-secret-key-value",
                  "DB_URI": "postgresql://quay@pg/quay", "AUTHENTICATION_TYPE": "OIDC",
                  "RHBK_LOGIN_CONFIG": {"CLIENT_ID": "quay-client", "CLIENT_SECRET": "oidc-client-secret-value",
                                        "OIDC_SERVER": "https://kc.apps.example.org/realms/tenants/",
                                        "SERVICE_NAME": "Keycloak", "LOGIN_SCOPES": ["openid", "email", "profile"],
                                        "PREFERRED_GROUP_CLAIM_NAME": "groups"}}
        store = os.path.join(tmp, "secret.json")
        json.dump({"config.yaml": base64.b64encode(yaml.safe_dump(bundle).encode()).decode()}, open(store, "w"))
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        oc = os.path.join(bindir, "oc")
        open(oc, "w").write(FAKE_OC)
        os.chmod(oc, os.stat(oc).st_mode | stat.S_IEXEC)
        open(os.path.join(tmp, "merge.py"), "w").write(script)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], FAKE_SECRET=store, NS="reg",
                   BUNDLE_SECRET="reg1-config-bundle", SHC_BUNDLE_SETTINGS=settings,
                   OIDC_CLIENT_SECRET="oidc-client-secret-value", OIDC_CONFIG_KEY="RHBK_LOGIN_CONFIG",
                   RGW_ACCESS_KEY="rgw-access-value", RGW_SECRET_KEY="rgw-secret-value")

        def run():
            p = subprocess.run([sys.executable, os.path.join(tmp, "merge.py")], env=env, capture_output=True,
                               text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            for s in SECRETS:
                self.assertNotIn(s, p.stdout + p.stderr)
            return p.stdout

        out = run()
        self.assertIn("configBundle updated:", out)
        self.assertIn("RHBK_LOGIN_CONFIG", out)
        now = yaml.safe_load(base64.b64decode(json.load(open(store))["config.yaml"]))
        self.assertEqual(now["RHBK_LOGIN_CONFIG"]["OIDC_SERVER"], "https://auth.example.org/realms/tenants/")
        self.assertEqual(now["RHBK_LOGIN_CONFIG"]["CLIENT_SECRET"], "oidc-client-secret-value")
        for k in ("SERVER_HOSTNAME", "SECRET_KEY", "DB_URI"):
            self.assertEqual(now[k], bundle[k])               # the database and the secret key are not touched
        self.assertEqual(now["DISTRIBUTED_STORAGE_CONFIG"]["default"][1]["access_key"], "rgw-access-value")
        self.assertIn("already has the shc settings", run())  # steady: no patch
        self.assertEqual(open(store + ".patches").read().count("patch"), 1)


if __name__ == "__main__":
    unittest.main()

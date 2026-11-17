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


CENTRAL = os.path.join(ROOT, "bootstrap", "helm", "central")
LAB = os.path.join(CENTRAL, "values.yaml")


def lab_render(chart):
    with open(LAB) as f:
        vals = yaml.safe_load(f)["quayServices"]["values"]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(vals, f)
    try:
        p = subprocess.run(["helm", "template", "quay-services", chart, "-f", f.name], capture_output=True, text=True,
                           timeout=120)
    finally:
        os.unlink(f.name)
    assert p.returncode == 0, p.stderr
    return vals, [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]


def application():
    p = subprocess.run(["helm", "template", "central", CENTRAL], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return next(d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict) and d.get("kind") == "Application"
                and d["metadata"]["name"] == "quay-services")


def pointer_get(doc, ptr):
    cur = doc
    for part in ptr.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def argo_sync_result(app, target, live):
    """What the Secret holds after a sync, by Argo CD's documented rules (an emulation, not Argo): with
    RespectIgnoreDifferences the value of an ignored JSON pointer is taken from the live object, which needs the
    pointer in the manifest; with ApplyOutOfSyncOnly an object that compares equal (ignored fields left out) is not
    applied. An applied Secret: stringData is written over data, as the API server does."""
    opts = app["spec"]["syncPolicy"]["syncOptions"]
    rules = [r for r in app["spec"].get("ignoreDifferences", []) if r.get("kind") == "Secret"
             and r.get("name") == target["metadata"]["name"]]
    tgt = json.loads(json.dumps(target))
    ptrs = [p for r in rules for p in r.get("jsonPointers", [])]
    if "RespectIgnoreDifferences=true" in opts:
        for ptr in ptrs:
            if pointer_get(tgt, ptr) is not None:
                parts = ptr.strip("/").split("/")
                pointer_get(tgt, "/" + "/".join(parts[:-1]))[parts[-1]] = pointer_get(live, ptr)

    def effective(o):
        data = dict(o.get("data") or {})
        for k, v in (o.get("stringData") or {}).items():
            data[k] = base64.b64encode(v.encode()).decode()
        return data

    def compared(o):
        d = effective(o)
        for ptr in ptrs:
            parts = ptr.strip("/").split("/")
            if parts[0] == "data":
                d.pop(parts[1].replace("~1", "/"), None)
        return d

    if "ApplyOutOfSyncOnly=true" in opts and compared(tgt) == compared(live):
        return dict(live)
    return dict(live, data=effective(tgt))


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class FirstSync(unittest.TestCase):
    """The first sync of the new chart on a registry with data (F1, F3): the live bundle keeps every key, and the hook
    then changes only the sign-in keys that differ, inside the login block too."""

    def live_bundle(self, vals):
        base = yaml.safe_load(vals["registry"]["configBundle"])
        live = dict(base)
        live["SUPER_USERS"] = base["SUPER_USERS"] + ["home-admin-1", "home-admin-2"]     # added live later
        live["DB_URI"] = "postgresql://quay@pg/quay"
        live["SECRET_KEY"] = "quay-secret-key-value"
        live["DISTRIBUTED_STORAGE_CONFIG"] = {"default": ["RadosGWStorage", {"hostname": vals["rgw"]["hostname"],
                                              "port": 443, "is_secure": True, "bucket_name": vals["rgw"]["bucket"],
                                              "storage_path": vals["rgw"].get("storagePath", "/datastorage/registry"),
                                              "access_key": "rgw-access-value", "secret_key": "rgw-secret-value"}]}
        live["DISTRIBUTED_STORAGE_DEFAULT_LOCATIONS"] = []
        live["DISTRIBUTED_STORAGE_PREFERENCE"] = ["default"]
        live["AUTHENTICATION_TYPE"] = "OIDC"
        live[vals["oidc"]["configKey"] if "configKey" in vals["oidc"] else "RHBK_LOGIN_CONFIG"] = {
            "SERVICE_NAME": "Keycloak", "CLIENT_ID": vals["oidc"]["clientId"],
            "CLIENT_SECRET": "oidc-client-secret-value",
            "OIDC_SERVER": "https://kc-old.apps.example.org/realms/sovereign-tenants/",
            "LOGIN_SCOPES": ["openid", "email", "profile"], "PREFERRED_GROUP_CLAIM_NAME": "groups",
            "VERIFIED_EMAIL_CLAIM_NAME": "email"}                                              # added by hand
        live["LIVE_ONLY_SETTING"] = "kept"
        return live

    def secret_obj(self, docs):
        return next(d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("config-bundle"))

    def test_the_old_chart_wrote_the_base_config_over_the_live_bundle(self):
        """Why: the old manifest (stringData) has no /data/config.yaml, so the ignore rule could not keep the live
        value. Rendered from the chart before this change (git show of 95212b40)."""
        old = tempfile.mkdtemp(prefix="shc-quay-old-")
        self.addCleanup(shutil.rmtree, old, True)
        subprocess.run(f"git -C {ROOT} archive 95212b40 bootstrap/helm/charts/quay | tar -x -C {old}", shell=True,
                       check=True)
        vals, docs = lab_render(os.path.join(old, "bootstrap", "helm", "charts", "quay"))
        target = self.secret_obj(docs)
        live_cfg = self.live_bundle(vals)
        live = {"metadata": target["metadata"],
                "data": {"config.yaml": base64.b64encode(yaml.safe_dump(live_cfg).encode()).decode()}}
        app = application()
        app["spec"]["syncPolicy"]["syncOptions"] = [o for o in app["spec"]["syncPolicy"]["syncOptions"]
                                                    if o != "ApplyOutOfSyncOnly=true"]       # the old Application
        after = yaml.safe_load(base64.b64decode(argo_sync_result(app, target, live)["data"]["config.yaml"]))
        self.assertNotIn("DB_URI", after)
        self.assertEqual(after["SUPER_USERS"], ["sovereign_admin"])

    def test_the_live_bundle_survives_the_first_sync_and_the_hook_changes_only_the_server(self):
        vals, docs = lab_render(CHART)
        target = self.secret_obj(docs)
        self.assertNotIn("stringData", target)
        self.assertIn("config.yaml", target["data"])         # the ignored pointer exists in the manifest
        app = application()
        self.assertIn("RespectIgnoreDifferences=true", app["spec"]["syncPolicy"]["syncOptions"])
        self.assertIn("ApplyOutOfSyncOnly=true", app["spec"]["syncPolicy"]["syncOptions"])
        live_cfg = self.live_bundle(vals)
        live = {"metadata": target["metadata"],
                "data": {"config.yaml": base64.b64encode(yaml.safe_dump(live_cfg).encode()).decode()}}
        for opts in (None, "no-apply-out-of-sync-only"):     # each rule alone keeps the bundle
            a = json.loads(json.dumps(app))
            if opts:
                a["spec"]["syncPolicy"]["syncOptions"].remove("ApplyOutOfSyncOnly=true")
            after = argo_sync_result(a, target, live)
            self.assertEqual(after["data"], live["data"], opts)
        # then the hook (its own merge script) on that bundle
        settings = obj(docs, "ConfigMap", vals["registry"]["name"] + "-bundle-settings")["data"]["settings.json"]
        cmd = obj(docs, "Job", "quay-db-init")["spec"]["template"]["spec"]["containers"][0]["command"][2]
        script = textwrap.dedent(re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", cmd, re.S).group(1))
        tmp = tempfile.mkdtemp(prefix="shc-quay-first-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = os.path.join(tmp, "secret.json")
        json.dump(live["data"], open(store, "w"))
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        open(os.path.join(bindir, "oc"), "w").write(FAKE_OC)
        os.chmod(os.path.join(bindir, "oc"), 0o755)
        open(os.path.join(tmp, "merge.py"), "w").write(script)
        key = vals["oidc"].get("configKey", "RHBK_LOGIN_CONFIG")
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], FAKE_SECRET=store, NS="ns",
                   BUNDLE_SECRET="b", SHC_BUNDLE_SETTINGS=settings, OIDC_CLIENT_SECRET="oidc-client-secret-value",
                   OIDC_CONFIG_KEY=key, RGW_ACCESS_KEY="rgw-access-value", RGW_SECRET_KEY="rgw-secret-value")
        p = subprocess.run([sys.executable, os.path.join(tmp, "merge.py")], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for s in SECRETS:
            self.assertNotIn(s, p.stdout + p.stderr)
        now = yaml.safe_load(base64.b64decode(json.load(open(store))["config.yaml"]))
        self.assertEqual(set(now), set(live_cfg))             # every live key is still there
        diff = {k for k in live_cfg if now[k] != live_cfg[k]}
        self.assertEqual(diff, {key})                         # only the login block changed ...
        changed_in_block = {k for k in now[key] if now[key][k] != live_cfg[key].get(k)}
        self.assertEqual(changed_in_block, {"OIDC_SERVER"})   # ... and in it only the server
        self.assertEqual(now[key]["VERIFIED_EMAIL_CLAIM_NAME"], "email")    # the hand-added key kept (F3)
        self.assertEqual(now["SUPER_USERS"], live_cfg["SUPER_USERS"])


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class HookAccess(unittest.TestCase):
    def test_the_hook_reads_two_secrets_and_patches_one(self):
        docs = render("https://auth.example.org/realms/tenants")
        role = obj(docs, "Role", "quay-db-init")
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["pg-pguser-quay", "reg1-config-bundle"],
             "verbs": ["get"]},
            {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["reg1-config-bundle"], "verbs": ["patch"]}])

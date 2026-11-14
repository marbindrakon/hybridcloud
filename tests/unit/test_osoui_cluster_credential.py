"""An OpenStack cluster's own site user and credential, run as task files in ansible-playbook (po_harness.py) with the
real helper against a Keystone over HTTP (fake_keystone.py) and the real Vault task files against a Vault over HTTP
(fake_vault.py): common/tasks/openstack_cluster_credential.yml (the cluster build's preflight) and
platformopenshift_teardown cluster_identity_down.yml (the cluster's delete). Judged: what Keystone and Vault hold
afterwards, the status written, that a steady run touches no password, the refusals, and that no secret reaches the
job's output at -vvv, the trace (every status write, every helper answer) or a leftover file. Names are examples."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402
import osoui_roles as O  # noqa: E402
import osoui_fixtures  # noqa: E402

try:
    import openstack  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

BUILD = "shc-oso-acme-c1-0a1b2"
NS = "entity-acme"
PATH = f"central/cluster/oso/{BUILD}/build/provider-creds"
CRED_TASK = {"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/openstack_cluster_credential.yml"}


class Order(unittest.TestCase):
    """The credential is made once the build name is known to be this cluster's (the user is named after it), and
    before the network-name check that lists the networks as that credential sees them; the build uses its path; the
    teardown deletes the user after the floating IPs and before the Vault paths."""

    def names(self, *parts):
        from play_helper import load
        return [t.get("name") for t in load(os.path.join(O.ROOT, "eda", "rulebooks", "roles", *parts))]

    def test_preflight(self):
        n = self.names("platformopenshift_provision", "tasks", "openstack_preflight.yml")
        self.assertLess(n.index("Refuse a build name in use"), n.index("The cluster's own credential on the site"))
        self.assertLess(n.index("The cluster's own credential on the site"),
                        n.index("The network's name leads the installer to this network alone"))
        self.assertLess(n.index("The network's name leads the installer to this network alone"),
                        n.index("Wait (nothing refused the cluster)"))

    def test_build_and_teardown(self):
        with open(os.path.join(O.ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision", "tasks",
                               "openstack_clusterbuild.yml")) as f:
            text = f.read()
        self.assertIn('cb_provider_creds_vault: "cluster/oso/{{ cb_app_name }}/build/provider-creds"', text)
        self.assertNotIn("oso/projects/{{ oso_env_name }}/clouds-config", text)
        from play_helper import load
        block = load(os.path.join(O.ROOT, "eda", "rulebooks", "roles", "platformopenshift_teardown", "tasks",
                                  "main.yml"))[0]["block"]
        n = [t.get("name") for t in block]
        self.assertLess(n.index("Release the cluster's floating IPs"), n.index("Delete the cluster's user on the site"))
        self.assertLess(n.index("Delete the cluster's user on the site"), n.index("Delete the cluster's Vault paths"))


@unittest.skipUnless(__import__("shutil").which("helm"), "helm not installed")
class ChartCopy(unittest.TestCase):
    """The cluster build's values name the cluster's own path; the chart's ExternalSecret copies clouds.yaml from it
    to the hub, where Hive's ClusterDeployment uses it."""

    def test_the_external_secret_reads_the_clusters_path(self):
        import subprocess
        import tempfile
        import yaml
        with open(os.path.join(O.ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision", "templates",
                               "build-values.yaml.j2")) as f:
            self.assertIn('providerCredsVault: "{{ cb_provider_creds_vault }}"', f.read())
        values = {"cluster": {"name": BUILD, "clusterName": "c1", "baseDomain": "web.acme.example.org",
                              "clusterImageSet": "img", "controlPlaneCount": 3, "workerCount": 2, "apiVIP": "192.0.2.5",
                              "ingressVIP": "192.0.2.7", "pullSecretVault": "p", "sshKeyVault": "s"},
                  "provider": {"type": "OpenStack", "providerCredsVault": f"cluster/oso/{BUILD}/build/provider-creds",
                               "openstack": {"cloud": "openstack", "externalNetwork": "n", "controlPlaneFlavor": "f",
                                             "workerFlavor": "f", "computeFlavor": "f"}},
                  "secretStoreName": "vault-backend"}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(values, f)
        try:
            p = subprocess.run(["helm", "template", BUILD, os.path.join(O.ROOT, "charts", "charts", "mce-cluster-build"),
                                "-f", f.name], capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(f.name)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        docs = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)]
        es = next(d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == f"{BUILD}-provider-creds")
        self.assertEqual(es["spec"]["data"], [{"secretKey": "clouds.yaml", "remoteRef": {
            "key": f"cluster/oso/{BUILD}/build/provider-creds", "property": "clouds.yaml"}}])
        cd = next(d for d in docs if d.get("kind") == "ClusterDeployment")
        self.assertEqual(cd["spec"]["platform"]["openstack"]["credentialsSecretRef"]["name"], f"{BUILD}-provider-creds")


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class ClusterCredential(unittest.TestCase):
    manager = False

    def setUp(self):
        self.site = O.Site(manager=self.manager)
        self.addCleanup(self.site.stop)
        self.pid = self.site.ks.add_project("acme_web", self.site.tenants)
        env = mock.patch.dict(os.environ, dict(self.site.env(), ANSIBLE_VERBOSITY="3"))
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.assertEqual(self.site.ks.faults, [])

    def run_cred(self, real=(), expect_rc=0, **over):
        extra = {"fake_clouds_yaml": self.site.account, "cb_app_name": BUILD, "cb_oso_site": "site-a",
                 "cloudoso_cr": {"resources": [{"status": {"domainName": "tenants", "projectId": self.pid}}]},
                 "cb_oso_project_id": self.pid, "cb_fip_account": "cloudinfra/site-a", "oso_env_name": "web",
                 "ansible_operator_meta": {"name": "c1", "namespace": NS},
                 "po_cr_current": {"resources": [{"status": {}}]}}
        extra.update(over)
        tasks = [{"block": [CRED_TASK],
                  "rescue": [{"ansible.builtin.shell": "cat >> {{ trace_file }}",
                              "args": {"stdin": "{{ {'refused': {'reason': po_fail_reason, 'condition': po_preflight_condition}} | to_json }}"},
                              "changed_when": False}]}]
        rc, trace, out = H.run("platformopenshift_provision", "openstack.yml", extra, fakes=O.FAKES,
                               real=("openstack_cluster_credential.yml",) + tuple(real), tasks=tasks, dump_vars=True,
                               result="{'vault': cb_provider_creds_vault, 'cred': cb_credential | default({})}")
        self.last_rc = rc
        if expect_rc is not None:
            self.assertEqual(rc, expect_rc, out)
        self.hunt(trace)
        return trace

    def secrets(self):
        """Every secret of the run: each credential secret the site made (also deleted ones), each password the
        unit set (the fake records them), the site account's password, the job's Vault token."""
        ks = self.site.ks
        out = (ks.secrets_seen + [c["secret"] for c in ks.app_creds.values()]
               + [u["password"] for u in ks.users.values() if u["password"]]
               + [self.site.account_password, self.site.vault.token])
        self.assertGreaterEqual(len(out), 2)
        return out

    def hunt(self, trace):
        """No secret in the trace (every status write, every helper answer), the whole -vvv output, or any variable
        the play holds at its end: facts and registered results."""
        self.assertTrue(H.LAST_VARS)
        for where, text in (("trace", json.dumps(trace)), ("output", H.LAST_OUTPUT), ("variables", H.LAST_VARS)):
            for s in self.secrets():
                self.assertNotIn(s, text, f"a secret in the play's {where}")
        for f in os.listdir("/tmp"):
            self.assertFalse(f.startswith(f"eda-po-cred-{BUILD}"), f"left behind: /tmp/{f}")

    def user(self):
        return next(u for u in self.site.ks.users.values() if u["name"] == BUILD)

    def test_first_run_user_credential_vault_and_status(self):
        trace = self.run_cred()
        res = trace[-1]["result"]
        self.assertEqual(res["vault"], f"cluster/oso/{BUILD}/build/provider-creds")
        user = self.user()
        self.assertIn("Managed by the sovereign cloud platform", user["description"])
        self.assertEqual(self.site.ks.effective_roles(user["id"], "project", self.pid), {"member", "reader"})
        cred = [c for c in self.site.ks.app_creds.values() if c["user"] == user["id"]]
        self.assertEqual(len(cred), 1)
        self.assertEqual((cred[0]["name"], cred[0]["unrestricted"]), (BUILD, False))
        import yaml
        clouds = yaml.safe_load(self.site.vault.data[PATH]["clouds.yaml"])["clouds"]["openstack"]
        self.assertEqual(clouds["auth"]["application_credential_id"], cred[0]["id"])
        self.assertEqual(clouds["auth"]["application_credential_secret"], cred[0]["secret"])
        patch = [t for t in H.patches(trace)][-1]
        self.assertTrue(patch["partial"])
        body = patch["body"]
        self.assertEqual(body["credential"]["id"], cred[0]["id"])
        self.assertEqual((body["credential"]["user"], body["credential"]["userId"], body["credential"]["domain"],
                          body["credential"]["domainId"]), (BUILD, user["id"], "tenants", self.site.tenants))
        cond = body["conditions"][0]
        self.assertEqual((cond["type"], str(cond["status"]), cond["reason"]), ("ClusterCredential", "True", "OwnCredential"))
        self.assertEqual(cond["message"], f"The cluster uses its own credential on the site, owned by the site user {BUILD}.")
        self.assertEqual(res["cred"]["credential"]["kept"], False)
        if not self.manager:
            status = {"credential": body["credential"],
                      "conditions": [{k: v for k, v in cond.items() if k != "lastTransitionTime"}]}
            status["conditions"][0]["status"] = str(status["conditions"][0]["status"])
            osoui_fixtures.check(self, "PlatformOpenshift/credential", status,
                                 {cred[0]["id"]: "ac9d00112233445566778899aabbccdd", user["id"]: "0f3c11223344556677889900aabbccdd",
                                  self.site.tenants: "51c0aa7e1f2d4c3b9a8e7d6c5b4a3921"})

    def test_a_steady_run_touches_no_password_and_writes_no_vault(self):
        self.run_cred()
        n, v = len(self.site.ks.log), len(self.site.vault.log)
        trace = self.run_cred()
        self.assertTrue(trace[-1]["result"]["cred"]["credential"]["kept"])
        self.assertEqual([c for c in self.site.ks.log[n:] if c[0] in ("PATCH", "PUT", "DELETE")
                          or (c[0] == "POST" and c[1] != "/v3/auth/tokens")], [])
        self.assertEqual([c for c in self.site.vault.log[v:] if c[0] != "GET"], [])

    def test_the_site_not_answering_the_check_changes_nothing(self):
        """R1: a 5xx at the credential's sign-in is not "the credential is not valid": the job stops with a plain
        message, no password is set, the cluster's working credential stays on the site and in Vault."""
        self.run_cred()
        cred_ids = set(self.site.ks.app_creds)
        stored = dict(self.site.vault.data[PATH])
        for status in (503, 500):
            n = len(self.site.ks.log)
            self.fail_app_cred_sign_in(status)
            trace = self.run_cred(expect_rc=None)
            self.assertNotEqual(self.last_rc, 0)
            later = self.site.ks.log[n:]
            self.assertIn(("POST", "/v3/auth/tokens", status), later)
            self.assertEqual([c for c in later if c[0] in ("PATCH", "DELETE")
                              or (c[0] == "POST" and "application_credentials" in c[1])], [], later)
            self.assertEqual(set(self.site.ks.app_creds), cred_ids)
            self.assertEqual(self.site.vault.data[PATH], stored)
            self.assertEqual(H.patches(trace), [])
            self.assertIn("did not answer the check of the cluster's credential", H.LAST_OUTPUT)
            self.assertEqual([t for t in trace if "refused" in t and t["refused"].get("reason")], [])
        # the next run, the site answering again, keeps the credential
        trace = self.run_cred()
        self.assertTrue(trace[-1]["result"]["cred"]["credential"]["kept"])

    def fail_app_cred_sign_in(self, status):
        """The fake answers the next application credential sign-in with the status, once (the site account's
        password sign-ins pass)."""
        ks = self.site.ks
        orig = ks._auth

        def auth(body):
            methods = ((body.get("auth") or {}).get("identity") or {}).get("methods") or []
            if "application_credential" in methods:
                ks._auth = orig
                return ks._err(status, "Service Unavailable")
            return orig(body)
        ks._auth = auth

    def test_vault_not_answering_changes_nothing(self):
        """R2: the cluster's Vault path read failing (503) stops the job before the site is touched."""
        self.run_cred()
        cred_ids = set(self.site.ks.app_creds)
        n = len(self.site.ks.log)
        self.site.vault.fail_next.append(("GET", "/v1/central/data/cluster/oso/", 503))
        trace = self.run_cred(expect_rc=None)
        self.assertNotEqual(self.last_rc, 0)
        self.assertEqual([c for c in self.site.ks.log[n:] if c[0] != "GET" and c[1] != "/v3/auth/tokens"], [])
        self.assertEqual(set(self.site.ks.app_creds), cred_ids)
        self.assertEqual(H.patches(trace), [])
        self.assertIn("could not be read from Vault (Vault answered HTTP 503", H.LAST_OUTPUT)

    def test_the_real_account_loader_keeps_no_secret_in_a_variable(self):
        """R2: the site account (common/tasks/openstack_account.yml) and the cluster's credential are read from Vault
        into private files only: no fact or registered result holds the account's password or the credential's
        secret (the hunt reads every variable of the play)."""
        with open(self.site.account) as f:
            self.site.vault.data["central/cloudinfra/site-a"] = {"clouds.yaml": f.read()}
        self.run_cred(real=("openstack_account.yml",))
        trace = self.run_cred(real=("openstack_account.yml",))
        self.assertTrue(trace[-1]["result"]["cred"]["credential"]["kept"])
        self.assertIn('"_cc_vault"', H.LAST_VARS)
        self.assertIn('"_os_account_read"', H.LAST_VARS)

    def test_a_foreign_user_of_that_name_is_refused(self):
        self.site.ks.add_user(BUILD, self.site.tenants, password="someone-elses", description="theirs")
        trace = self.run_cred()
        ref = [t["refused"] for t in trace if "refused" in t][0]
        self.assertEqual(ref["reason"], "UserRefused")
        self.assertEqual((ref["condition"]["type"], ref["condition"]["reason"]), ("ClusterCredential", "UserRefused"))
        self.assertEqual(ref["condition"]["message"],
                         f"The site refused the cluster's user {BUILD}: a user named {BUILD} exists in domain "
                         "tenants and is not the platform's. The cluster cannot be built.")
        self.assertNotIn(PATH, self.site.vault.data)
        self.assertEqual(H.patches(trace), [])


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class ClusterCredentialManagerAccount(ClusterCredential):
    """The same with a site account that is a domain manager of the tenant domain only."""
    manager = True

    def test_a_role_the_manager_may_not_grant(self):
        real = {"openstack": {"clusterIdentity": {"rolesBySite": {"site-a": ["member", "load-balancer_member"]}}}}
        trace = self.run_cred(fabric_realization=real)
        ref = [t["refused"] for t in trace if "refused" in t][0]
        self.assertEqual(ref["reason"], "RoleRefused")
        self.assertIn("load-balancer_member", ref["condition"]["message"])


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class ClusterUserDown(unittest.TestCase):
    def setUp(self):
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        ks = self.site.ks
        self.pid = ks.add_project("acme_web", self.site.tenants)
        self.uid = ks.add_user(BUILD, self.site.tenants, description="u. Managed by the sovereign cloud "
                                                                              "platform; do not change.")
        ks.grant("user", self.uid, "project", self.pid, "member")
        ks.app_creds["c1"] = {"id": "c1", "user": self.uid, "project": self.pid, "name": BUILD,
                              "description": "", "secret": "cluster-secret-value", "roles": ["member"],
                              "unrestricted": False, "expires_at": None, "created_at": ""}
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)

    def run_down(self, status, hub_owned=None, hub_ns=None):
        po = {"metadata": {"name": "c1", "namespace": NS, "uid": "uid-c1"}, "status": status,
              "spec": {"type": "openstack", "openstack": {"environment": "web"}}}
        oso = {"metadata": {"name": "web", "namespace": NS},
               "status": {"domainName": "tenants", "projectId": self.pid, "accountVaultPath": "cloudinfra/site-a"}}
        k8s = {f"CloudOSO/{NS}/web": oso, f"Namespace//{NS}": {"metadata": {"name": NS, "labels": {
            "hybridsovereign.redhat/entity": "acme"}}}}
        if hub_ns is not None:
            k8s.update(hub_ns)
        extra = {"fake_clouds_yaml": self.site.account, "po_cr": {"resources": [po]}, "po_oso_env": "web",
                 "platformopenshift_cr_name": "c1", "platformopenshift_cr_namespace": NS,
                 "po_build_name": status.get("buildName", ""),
                 "services_api_host": "https://services.example.org", "services_bearer_token": "services-token-not-in-data",
                 "central_api_host": "https://central.example.org", "central_bearer_token": "central-token-not-in-data"}
        if hub_owned is not None:
            extra["po_hub_owned"] = hub_owned
        # po_vault_paths is a fact of the teardown (an extra var could not be changed by the file)
        tasks = [{"ansible.builtin.set_fact": {"po_vault_paths": ["x/one"]}},
                 {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/cluster_identity_down.yml"}]
        rc, trace, out = H.run("platformopenshift_teardown", "cluster_identity_down.yml", extra, k8s=k8s,
                               fakes=O.FAKES, tasks=tasks, result="{'paths': po_vault_paths}")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("cluster-secret-value", json.dumps(trace) + H.LAST_OUTPUT)
        return trace

    def test_the_user_and_its_credential_go_and_the_path_joins(self):
        trace = self.run_down({"buildName": BUILD, "credential": {"id": "c1", "user": BUILD, "userId": self.uid}})
        self.assertNotIn(self.uid, self.site.ks.users)
        self.assertEqual(self.site.ks.app_creds, {})
        self.assertEqual(sorted(trace[-1]["result"]["paths"]), [f"cluster/oso/{BUILD}/build/provider-creds", "x/one"])

    def test_a_cluster_refused_before_its_build_was_recorded(self):
        trace = self.run_down({"credential": {"user": BUILD, "userId": self.uid}})
        self.assertNotIn(self.uid, self.site.ks.users)
        paths = sorted(trace[-1]["result"]["paths"])
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].startswith("cluster/oso/shc-oso-acme-c1-") and paths[0].endswith("/build/provider-creds"),
                        paths)

    def test_no_credential_recorded_the_user_of_the_computed_name_goes(self):
        """R3: a cluster refused after its user was made (RoleRefused, PasswordRefused, CredentialRefused, a failed
        status write) recorded no credential: its delete still removes the user of its computed name."""
        trace = self.run_down({"buildName": BUILD}, hub_owned=True)
        self.assertNotIn(self.uid, self.site.ks.users)
        self.assertEqual(self.site.ks.app_creds, {})
        req = H.os_calls(trace, "cluster-identity-delete")[0]
        self.assertEqual((req["user_id"], req["user_name"]), ("", BUILD))
        # the recorded build's paths are the teardown's (resolve_platformopenshift_paths.yml), not this file's
        self.assertEqual(trace[-1]["result"]["paths"], ["x/one"])

    def derived(self):
        sys.path.insert(0, os.path.join(O.ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision", "files"))
        import po_preflight
        return po_preflight.build_name({"entity": "acme", "name": "c1"})["buildName"]

    def test_no_build_recorded_the_derived_names_user_goes(self):
        self.site.ks.users[self.uid]["name"] = self.derived()
        trace = self.run_down({})
        self.assertNotIn(self.uid, self.site.ks.users)
        self.assertEqual(H.os_calls(trace, "cluster-identity-delete")[0]["user_name"], self.derived())

    def test_a_build_another_cluster_owns_is_not_touched(self):
        """A cluster refused NamespaceTaken / BuildOwnerMismatch never made a user under that name: nothing is
        deleted by the name (the other cluster's user and Vault path stay)."""
        trace = self.run_down({"buildName": BUILD}, hub_owned=False)
        self.assertIn(self.uid, self.site.ks.users)
        self.assertEqual(H.os_calls(trace), [])
        self.assertEqual(trace[-1]["result"]["paths"], ["x/one"])
        self.site.ks.users[self.uid]["name"] = self.derived()
        taken = {f"Namespace//{self.derived()}": {"metadata": {"name": self.derived(), "labels": {
            "hybridsovereign.redhat/platformopenshift-uid": "uid-another"}}}}
        trace = self.run_down({}, hub_ns=taken)
        self.assertIn(self.uid, self.site.ks.users)
        self.assertEqual(H.os_calls(trace), [])
        self.assertEqual(trace[-1]["result"]["paths"], ["x/one"])

    def test_a_user_of_the_name_that_is_not_the_platforms_stays(self):
        self.site.ks.users[self.uid]["description"] = "someone's"
        trace = self.run_down({"buildName": BUILD}, hub_owned=True)
        self.assertIn(self.uid, self.site.ks.users)



@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class VaultDeleteQuiet(unittest.TestCase):
    """R21: the Vault delete task carries the job's token in its request: at -vvv it is not in the output."""

    def test_the_token_is_not_in_the_output(self):
        import fake_vault
        v = fake_vault.Vault(token="vault-token-for-the-hunt").start()
        self.addCleanup(v.stop)
        v.data["central/oso/projects/web/clouds-config"] = {"clouds.yaml": "x"}
        env = mock.patch.dict(os.environ, {"VAULT_ADDR": v.url, "VAULT_TOKEN": v.token, "ANSIBLE_VERBOSITY": "3"})
        env.start()
        self.addCleanup(env.stop)
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/vault_kv_delete.yml",
                  "vars": {"vault_kv_delete_paths": ["oso/projects/web/clouds-config"]}}]
        rc, _, out = H.run("cloudoso_provision", "x.yml", {}, tasks=tasks, dump_vars=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(v.data, {})
        self.assertNotIn(v.token, H.LAST_OUTPUT)
        self.assertNotIn(v.token, H.LAST_VARS)


if __name__ == "__main__":
    unittest.main()

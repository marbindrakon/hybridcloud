"""Who gets what on a tenant's OpenShift cluster, and who can sign in to it.

The real platformopenshift_provision tasks (cluster_signin_groups.yml, cluster_signin_limit.yml with
files/cluster_signin.py, the bindings of cluster_rbac.yml) and the real common tasks they use
(services_signin_groups.yml, obtain_keycloak_realm_token.yml, names_call.yml, vault_kv_read.yml) run in
ansible-playbook (po_harness.py) against an identity provider over HTTPS (fake_keycloak_flows.py) and a Vault over HTTP
(fake_vault.py). The realm is shared by two tenants that both have access groups named admins and devs; the cluster
is the first tenant's. What is judged: the groups each binding names, the groups claim of each person, and who the
identity provider lets sign in to the cluster's client (by the rules read in its source, see the fake's docstring).
Names are examples."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

if HAVE_CRYPTO:
    import fake_keycloak_flows  # noqa: E402
import fake_vault  # noqa: E402

ROLE = os.path.join(H.ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
HELPER = os.path.join(ROLE, "files", "cluster_signin.py")
NS = "entity-acme-corp"
CLIENT = "shc-oso-web-acme-corp-1a2b3c4d-keycloak-oidc"
CLUSTER_TOOL = {"clusterAdminRbac": ["admins"], "clusterDeveloperRbac": ["devs"], "clusterViewerRbac": ["auditors"]}
OLD_MAPPER = {"id": "m-old", "name": "groups", "protocol": "openid-connect",
              "protocolMapper": "oidc-group-membership-mapper",
              "config": {"full.path": "false", "claim.name": "groups", "id.token.claim": "true",
                         "access.token.claim": "true", "userinfo.token.claim": "true"}}

# every person of the realm: (directory groups)
PEOPLE = {
    "alice": ["/acme-corp/admins"],             # the cluster's tenant, its admin group
    "dave": ["/acme-corp/devs"],                 # the cluster's tenant, its developer group
    "olga": ["/acme-corp/others"],               # the cluster's tenant, a group the cluster does not name
    "gina": ["/globex/admins"],                  # another tenant, a group of the same name
    "gus": ["/globex/devs"],                     # another tenant, a group of the same name
    "nora": [],                                  # in no group
}


def load(path):
    with open(path) as f:
        return H.yaml.safe_load(f)


def text(path):
    with open(path) as f:
        return f.read()


def bindings(templates):
    """ClusterRoleBinding name -> (complianceType, its subjects' group names)."""
    out = {}
    for t in templates:
        o = t["objectDefinition"]
        out[o["metadata"]["name"].rsplit("-cluster-", 1)[1]] = (
            t["complianceType"], [s["name"] for s in o.get("subjects", [])])
    return out


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_CRYPTO, "ansible-playbook / cryptography not installed")
class TenantCluster(unittest.TestCase):
    def setUp(self):
        self.kc = fake_keycloak_flows.Keycloak().start()
        self.addCleanup(self.kc.stop)
        self.vault = fake_vault.Vault().start()
        self.addCleanup(self.vault.stop)
        self.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"] = self.kc.vault_entry()
        self.kc.add_client(CLIENT, mappers=[OLD_MAPPER])
        # the services cluster's client holds both tenants' roles of the same names (made by the Entity jobs)
        self.kc.add_client("openshift-services")
        for g in ("acme-corp/admins", "acme-corp/devs", "acme-corp/others", "acme-corp/auditors",
                  "globex/admins", "globex/devs"):
            self.kc.add_group(g)
        for name, groups in PEOPLE.items():
            self.kc.add_user(name, *groups)
        env = mock.patch.dict(os.environ, {"VAULT_ADDR": self.vault.url, "VAULT_TOKEN": self.vault.token,
                                           "PYTHONDONTWRITEBYTECODE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.rbacs = [{"metadata": {"name": n}, "status": {"group": "/acme-corp/" + n}}
                      for n in ("admins", "devs", "others")] + [{"metadata": {"name": "auditors"}, "status": {}}]

    def run_cluster(self, tool=None, ok=True, action="ensure"):
        rbac_tasks = load(os.path.join(ROLE, "tasks", "cluster_rbac.yml"))
        build = [t for t in rbac_tasks if t["name"] in ("Build the subjects of each binding",
                                                        "Assemble RBAC object templates")]
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/keycloak_admin_session.yml"},
                 {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/cluster_signin_groups.yml"},
                 {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/cluster_signin_limit.yml",
                  "vars": {"csl_action": action}}] + build
        extra = {"services_api_host": "https://services.example.org", "services_bearer_token": "services-token",
                 "keycloak_realm": "sovereign-tenants", "ocp_oidc_client_id": CLIENT, "cluster_cr_name": "web",
                 "cluster_name": "shc-oso-web-acme-corp-1a2b3c4d", "cluster_namespace": NS,
                 "resolved_entity": "acme-corp", "shc_step": "Configure the cluster's sign-in"}
        k8s = {"Route/services-rhbk/": [{"spec": {"host": self.kc.host}}], f"Rbac/{NS}/": self.rbacs,
               f"PlatformOpenshift/{NS}/web": [{"metadata": {"name": "web", "namespace": NS},
                                                "spec": {"toolRbac": CLUSTER_TOOL if tool is None else tool}}]}
        rc, trace, out = H.run("platformopenshift_provision", "x.yml", extra, k8s=k8s, tasks=tasks,
                               result="{'templates': rbac_object_templates, 'csl': csl_result, "
                                      "'faults': csg_faults, 'tiers': csg_tiers}")
        if ok:
            self.assertEqual(rc, 0, out)
        for secret in self.kc.automation.values():
            self.assertNotIn(secret[1], H.LAST_OUTPUT)
        for tok in self.kc.tokens:
            self.assertNotIn(tok, H.LAST_OUTPUT)
        return rc, (trace[-1]["result"] if trace and "result" in trace[-1] else None), out

    def who_signs_in(self):
        return {p: self.kc.signs_in(p, CLIENT) for p in PEOPLE}

    def test_two_tenants_same_group_names_a_cluster_of_one(self):
        """The enumeration: each person x (in which binding, what the cluster sees, may sign in)."""
        # before: the realm's own browser flow and the old mapper (group names without the tenant)
        self.assertEqual(self.kc.groups_claim("gina", CLIENT), ["admins"])
        self.assertTrue(all(self.who_signs_in().values()))
        _, res, _ = self.run_cluster()
        self.assertEqual(bindings(res["templates"]), {
            "admin": ("mustonlyhave", ["acme-corp.admins"]),
            "operator": ("mustnothave", []),
            "developer": ("mustonlyhave", ["acme-corp.devs"]),
            # auditors has no directory group yet: no binding, and said in the job's log
            "viewer": ("mustnothave", [])})
        self.assertEqual([f["group"] for f in res["faults"]], ["acme-corp.auditors"])
        table = {p: (self.kc.signs_in(p, CLIENT), self.kc.groups_claim(p, CLIENT)) for p in PEOPLE}
        self.assertEqual(table, {
            "alice": (True, ["acme-corp.admins", "sign-in"]),
            "dave": (True, ["acme-corp.devs", "sign-in"]),
            "olga": (False, []),
            "gina": (False, []),
            "gus": (False, []),
            "nora": (False, [])})
        # what a refused person reads on the identity provider's error page
        self.assertIn("not in one of its access groups", self.kc.deny_message(CLIENT))
        # the client roles are the tenant's, owned by this cluster, mapped to the tenant's groups only
        role = self.kc.role(CLIENT, "acme-corp.admins")
        self.assertEqual(role["attributes"], {"entity": ["acme-corp"], "owner": ["PlatformOpenshift/web"]})
        cid = self.kc.clients[CLIENT]["id"]
        self.assertEqual(self.kc.groups["globex/admins"]["mappings"].get(cid, []), [])
        self.assertEqual(sorted(self.kc.clients[CLIENT]["roles"]), ["acme-corp.admins", "acme-corp.devs", "sign-in"])

    def test_a_second_run_changes_nothing(self):
        self.run_cluster()
        flows, execs = dict(self.kc.flows), dict(self.kc.executions)
        _, res, _ = self.run_cluster()
        self.assertEqual(res["csl"]["changed"], [])
        self.assertEqual((self.kc.flows, self.kc.executions), (flows, execs))

    def test_an_access_group_taken_off_the_cluster(self):
        self.run_cluster()
        _, res, _ = self.run_cluster(tool={"clusterAdminRbac": ["admins"]})
        self.assertEqual(bindings(res["templates"])["developer"], ("mustnothave", []))
        self.assertIsNone(self.kc.role(CLIENT, "acme-corp.devs"))
        self.assertEqual(self.who_signs_in(), {"alice": True, "dave": False, "olga": False, "gina": False,
                                               "gus": False, "nora": False})

    def test_no_access_group_nobody_signs_in(self):
        _, res, _ = self.run_cluster(tool={})
        self.assertEqual({k: v[0] for k, v in bindings(res["templates"]).items()},
                         dict.fromkeys(["admin", "operator", "developer", "viewer"], "mustnothave"))
        self.assertFalse(any(self.who_signs_in().values()))

    def test_a_flow_of_the_name_that_is_not_ours_is_refused(self):
        foreign = self.kc.add_flow(CLIENT, "someone's own flow")
        rc, _, out = self.run_cluster(ok=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("already holds a sign-in flow named " + CLIENT, out)
        self.assertEqual(self.kc.flows[foreign]["description"], "someone's own flow")
        self.assertEqual(self.kc.clients[CLIENT]["authenticationFlowBindingOverrides"], {})

    def test_a_changed_flow_is_replaced_before_the_old_one_goes(self):
        self.run_cluster()
        bound = self.kc.clients[CLIENT]["authenticationFlowBindingOverrides"]["browser"]
        # someone switched the check off
        for e in self.kc.executions.values():
            if e.get("provider") == "deny-access-authenticator":
                e["requirement"] = "DISABLED"
        self.assertTrue(self.kc.signs_in("gina", CLIENT))
        _, res, _ = self.run_cluster()
        now = self.kc.clients[CLIENT]["authenticationFlowBindingOverrides"]["browser"]
        self.assertNotEqual(now, bound)
        self.assertEqual(self.kc.flows[now]["alias"], CLIENT + "-b")
        self.assertNotIn(bound, self.kc.flows)
        self.assertFalse(self.kc.signs_in("gina", CLIENT))
        self.assertTrue(self.kc.signs_in("alice", CLIENT))

    def test_the_flows_token_is_needed_and_used_only_for_flows(self):
        del self.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"]["flowsClientId"]
        rc, _, out = self.run_cluster(ok=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("automation client for sign-in flows is not published", out)
        # nothing on the client says anyone may sign in yet, and the realm still lets everyone through: the job
        # stopped before the cluster is given the identity provider (keycloak_oidc.yml orders the steps)
        self.assertEqual(self.kc.clients[CLIENT]["authenticationFlowBindingOverrides"], {})

    def test_teardown_removes_only_the_clusters_flows(self):
        self.run_cluster()
        other = self.kc.add_flow(CLIENT + "-b", "a flow of the same alias that is not this cluster's")
        rc, res, out = self.run_cluster(action="remove")
        self.assertEqual(res["csl"]["removed"], [CLIENT])
        self.assertIsNone(self.kc.flow(CLIENT))
        self.assertIn(other, self.kc.flows)


class Wiring(unittest.TestCase):
    def test_keycloak_oidc_limits_sign_in_before_the_cluster_gets_the_identity_provider(self):
        tasks = load(os.path.join(ROLE, "tasks", "keycloak_oidc.yml"))
        names = [t["name"] for t in tasks]
        self.assertLess(names.index("The cluster's access groups as client roles of its sign-in client"),
                        names.index("Only the cluster's people can sign in to its client"))
        self.assertLess(names.index("Only the cluster's people can sign in to its client"),
                        names.index("Build ACM Policy for OpenShift OAuth configuration"))
        self.assertNotIn("Add groups mapper to OIDC client (if not present)", names)
        body = text(os.path.join(ROLE, "tasks", "keycloak_oidc.yml"))
        self.assertNotIn('"full.path": "false"', body)

    def test_no_binding_cuts_the_tenant_off(self):
        body = text(os.path.join(ROLE, "tasks", "cluster_rbac.yml"))
        self.assertNotIn("regex_replace', '^.*/'", body)
        self.assertNotIn("complianceType': 'musthave'", body)

    def test_the_request_with_tokens_is_not_logged(self):
        tasks = load(os.path.join(ROLE, "tasks", "cluster_signin_limit.yml"))
        call = [t for t in tasks if "ansible.builtin.command" in t][0]
        self.assertTrue(call["no_log"])

    def test_teardown_removes_the_flow_after_the_client(self):
        tasks = load(os.path.join(H.ROOT, "eda", "rulebooks", "roles", "platformopenshift_teardown",
                                  "tasks", "main.yml"))
        names = [t.get("name") for t in tasks[0]["block"]]
        self.assertLess(names.index("Delete the cluster's OIDC client"), names.index("Remove the cluster's sign-in flow"))


if __name__ == "__main__":
    unittest.main()

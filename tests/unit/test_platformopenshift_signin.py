"""Who gets what on a tenant's OpenShift cluster.

The real platformopenshift_provision tasks (keycloak_oidc.yml up to the OAuth policy it builds, with
cluster_signin_groups.yml and cluster_groups_claim.yml / files/cluster_signin.py, and the bindings of cluster_rbac.yml)
and the real common tasks they use (services_signin_groups.yml, obtain_keycloak_realm_token.yml, names_call.yml,
vault_kv_read.yml) run in ansible-playbook (po_harness.py) against an identity provider over HTTPS
(fake_keycloak_signin.py) and a Vault over HTTP (fake_vault.py). The realm is shared by two tenants that both have
access groups named admins, devs and operators; the cluster is the first tenant's. What is judged, for every person
and every way to finish a sign-in: the groups claim of the cluster's token, the bindings that name one of those
groups, whether the person may create a project, and whether the person holds a project an operator asked for earlier
(the cluster's project template binds it to groups, never to the person). Names are examples.

The same tests run on the execution image (ansible-core 2.16, where a nested number renders as a string):
podman run --rm --entrypoint python3 --userns=keep-id -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v $PWD:/w:ro,z -w /w
<the automation's execution image> -m unittest tests.unit.test_platformopenshift_signin"""
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
    import fake_keycloak_signin  # noqa: E402
import fake_vault  # noqa: E402

ROLE = os.path.join(H.ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
TEARDOWN = os.path.join(H.ROOT, "eda", "rulebooks", "roles", "platformopenshift_teardown")
NS = "entity-acme-corp"
BUILD = "shc-oso-web-acme-corp-1a2b3c4d"
CLIENT = BUILD + "-keycloak-oidc"
MARK = NS + "/web"
CLUSTER_TOOL = {"clusterAdminRbac": ["admins"], "clusterOperatorRbac": ["operators"],
                "clusterDeveloperRbac": ["devs"], "clusterViewerRbac": ["auditors"]}
OLD_MAPPER = {"id": "m-old", "name": "groups", "protocol": "openid-connect",
              "protocolMapper": "oidc-group-membership-mapper",
              "config": {"full.path": "false", "claim.name": "groups", "id.token.claim": "true",
                         "access.token.claim": "true", "userinfo.token.claim": "true"}}
REALM_ROLE_GROUPS = {"name": "groups", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-realm-role-mapper",
                     "config": {"claim.name": "groups", "multivalued": "true"}}
GROUP_PATHS = {"name": "directory groups", "protocol": "openid-connect",
               "protocolMapper": "oidc-group-membership-mapper",
               "config": {"claim.name": "groups", "full.path": "true"}}
EMAIL = {"name": "email", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-property-mapper",
         "config": {"claim.name": "email"}}

# every person of the realm: (directory groups, realm roles)
PEOPLE = {
    "alice": (["/acme-corp/admins"], []),        # the cluster's tenant, its admin group
    "opal": (["/acme-corp/operators"], []),      # the cluster's tenant, its operator group
    "dave": (["/acme-corp/devs"], []),           # the cluster's tenant, its developer group
    "olga": (["/acme-corp/others"], []),         # the cluster's tenant, a group the cluster does not name
    "gina": (["/globex/admins"], ["admins"]),    # another tenant, a group of the same name (and a realm role of it)
    "gus": (["/globex/devs"], []),               # another tenant, a group of the same name
    "otto": (["/globex/operators"], []),         # another tenant, a group of the same name
    "nora": ([], []),                            # in no group
}
SCOPES = ((), ("microprofile-jwt",), ("microprofile-jwt", "directory-groups"))


def load(path):
    with open(path) as f:
        return H.yaml.safe_load(f)


def text(path):
    with open(path) as f:
        return f.read()


def bindings(templates):
    """ClusterRoleBinding name -> (complianceType, its subjects' group names, its role)."""
    out = {}
    for t in templates:
        o = t["objectDefinition"]
        if o["kind"] != "ClusterRoleBinding":
            continue
        name = o["metadata"]["name"]
        key = name.rsplit("-cluster-", 1)[1] if "-cluster-" in name else name
        out[key] = (t["complianceType"], [s["name"] for s in o.get("subjects", [])],
                    (o.get("roleRef") or {}).get("name"))
    return out


def project_template(templates):
    """(the cluster's project template, the cluster's project configuration) of the policy."""
    tpl = [t for t in templates if t["objectDefinition"]["kind"] == "Template"]
    cfg = [t for t in templates if t["objectDefinition"]["kind"] == "Project"]
    assert len(tpl) == 1 and len(cfg) == 1, templates
    return tpl[0], cfg[0]


def project_admins(templates):
    """The groups a project made through the cluster's template binds admin to (none: no binding)."""
    tpl, _ = project_template(templates)
    rbs = [o for o in tpl["objectDefinition"]["objects"] if o["kind"] == "RoleBinding"]
    return sorted(s["name"] for rb in rbs if rb["roleRef"]["name"] == "admin" for s in rb["subjects"])


def oidc_tasks():
    """keycloak_oidc.yml up to the OAuth policy it builds (the ACM writes to the hub are not run here)."""
    tasks = load(os.path.join(ROLE, "tasks", "keycloak_oidc.yml"))
    names = [t.get("name") for t in tasks]
    tasks = tasks[:names.index("Build ACM Policy for OpenShift OAuth configuration") + 1]
    for t in tasks:     # the role's own includes, from the given tasks' place
        inc = t.get("ansible.builtin.include_tasks")
        if inc and "/" not in inc:
            t["ansible.builtin.include_tasks"] = "{{ role_path }}/tasks/" + inc
    return tasks


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_CRYPTO, "ansible-playbook / cryptography not installed")
class TenantCluster(unittest.TestCase):
    def setUp(self):
        self.kc = fake_keycloak_signin.Keycloak().start()
        self.addCleanup(self.kc.stop)
        self.vault = fake_vault.Vault().start()
        self.addCleanup(self.vault.stop)
        self.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"] = self.kc.vault_entry()
        # the realm's client scopes: profile and email by default, microprofile-jwt (a realm-role mapper of groups)
        # optional, and a scope someone added that puts the person's directory groups into groups
        self.kc.add_scope("profile")
        self.kc.add_scope("email", [EMAIL])
        self.kc.add_scope("microprofile-jwt", [REALM_ROLE_GROUPS])
        self.kc.add_scope("directory-groups", [GROUP_PATHS])
        self.kc.realm_default = ["profile", "email"]
        self.kc.realm_optional = ["microprofile-jwt", "directory-groups"]
        # the services cluster's client holds both tenants' roles of the same names (made by the Entity jobs)
        self.kc.add_client("openshift-services", optional_scopes=["microprofile-jwt"])
        for g in ("acme-corp/admins", "acme-corp/devs", "acme-corp/operators", "acme-corp/others",
                  "acme-corp/auditors", "globex/admins", "globex/devs", "globex/operators"):
            self.kc.add_group(g)
        for name, (groups, roles) in PEOPLE.items():
            self.kc.add_user(name, *groups, realm_roles=roles)
        env = mock.patch.dict(os.environ, {"VAULT_ADDR": self.vault.url, "VAULT_TOKEN": self.vault.token,
                                           "PYTHONDONTWRITEBYTECODE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.rbacs = [{"metadata": {"name": n}, "status": {"group": "/acme-corp/" + n}}
                      for n in ("admins", "devs", "operators", "others")] + [{"metadata": {"name": "auditors"},
                                                                               "status": {}}]

    def old_client(self, **fields):
        """The cluster's client as the release before made it: the group-membership mapper, the realm's scopes."""
        self.kc.add_client(CLIENT, mappers=[OLD_MAPPER], default_scopes=self.kc.realm_default,
                           optional_scopes=self.kc.realm_optional, **fields)

    def run_cluster(self, tool=None, ok=True, realization=None, recorded=None):
        """recorded: the cluster's status as read at the job's start (default: its build recorded, as every cluster
        that reaches its sign-in has)."""
        rbac_tasks = load(os.path.join(ROLE, "tasks", "cluster_rbac.yml"))
        build = [t for t in rbac_tasks if t["name"] in ("Build the subjects of each binding",
                                                        "Assemble RBAC object templates",
                                                        "Say that nobody can request a project on the cluster")]
        extra = {"services_api_host": "https://services.example.org", "services_bearer_token": "services-token",
                 "central_token": "central-token", "central_api_server": "https://hub.example.org",
                 "keycloak_realm": "sovereign-tenants", "platformopenshift_oidc_idp_name": "keycloak-sso",
                 "cluster_cr_name": "web", "cluster_name": BUILD, "cluster_namespace": NS,
                 "cluster_console_url": "https://console-openshift-console.apps.web.prod.acme-corp.example.org",
                 "cluster_api_url": "https://api.web.prod.acme-corp.example.org:6443",
                 "cluster_apps_url": "https://apps.web.prod.acme-corp.example.org",
                 "cluster_domain": "web.prod.acme-corp.example.org",
                 "resolved_entity": "acme-corp", "shc_step": "Configure the cluster's sign-in",
                 "platformopenshift_cr": {"resources": [{"metadata": {"name": "web", "namespace": NS},
                                                         "status": {"buildName": BUILD} if recorded is None
                                                         else recorded}]}}
        if realization is not None:
            extra["fabric_realization"] = realization
        k8s = {"Route/services-rhbk/": [{"spec": {"host": self.kc.host}}], f"Rbac/{NS}/": self.rbacs,
               "ConfigMap/openshift-config-managed/default-ingress-cert": [{"data": {"ca-bundle.crt": "ca"}}],
               f"PlatformOpenshift/{NS}/web": [{"metadata": {"name": "web", "namespace": NS},
                                                "spec": {"toolRbac": CLUSTER_TOOL if tool is None else tool}}]}
        rc, trace, out = H.run("platformopenshift_provision", "x.yml", extra, k8s=k8s, tasks=oidc_tasks() + build,
                               result="{'templates': rbac_object_templates, 'csl': csl_result, "
                                      "'faults': csg_faults, 'tiers': csg_tiers, 'policy': acm_oidc_policy}")
        if ok:
            self.assertEqual(rc, 0, out)
        for secret in [s[1] for s in self.kc.automation.values()] + list(self.kc.secrets.values()):
            self.assertNotIn(secret, H.LAST_OUTPUT)
        for tok in self.kc.tokens:
            self.assertNotIn(tok, H.LAST_OUTPUT)
        self.trace = trace
        return rc, (trace[-1]["result"] if trace and "result" in trace[-1] else None), out

    def outcome(self, res, person, path="browser", scope=(), projects=False):
        """(groups claim, bindings that name one of them, may create a project) of a person's token; with projects,
        also whether the person is admin of a project an operator made earlier through the cluster's template (or
        is the cluster's admin, who holds every project)."""
        claim = self.kc.token_claims(person, CLIENT, path, scope)
        if claim is None:
            return None
        b = bindings(res["templates"])
        bound = sorted(k for k, (ct, subjects, _) in b.items()
                       if ct == "mustonlyhave" and set(subjects) & set(claim) and k != "self-provisioners")
        sp = b["self-provisioners"][1]
        project = bool(set(sp) & set(claim)) or "admin" in bound
        if not projects:
            return claim, bound, project
        holds = bool(set(project_admins(res["templates"])) & set(claim)) or "admin" in bound
        return claim, bound, project, holds

    def test_two_tenants_same_group_names_a_cluster_of_one(self):
        """The enumeration: each person x each way to sign in x each scope asked for -> (claim, bindings, project)."""
        self.old_client(directAccessGrantsEnabled=True)
        # before: the group-membership mapper sends the last part of a group, whoever's tenant
        self.assertEqual(self.kc.token_claims("gina", CLIENT), ["admins"])
        _, res, _ = self.run_cluster()
        b = bindings(res["templates"])
        self.assertEqual(b, {
            "admin": ("mustonlyhave", ["acme-corp.admins"], "cluster-admin"),
            "operator": ("mustonlyhave", ["acme-corp.operators"], "self-provisioner"),
            "developer": ("mustonlyhave", ["acme-corp.devs"], "edit"),
            # auditors has no directory group yet: no binding, and said in the job's log
            "viewer": ("mustnothave", [], None),
            # every signed-in person no longer creates projects: only the operator level does
            "self-provisioners": ("mustonlyhave", ["acme-corp.operators"], "self-provisioner")})
        self.assertEqual([f["group"] for f in res["faults"]], ["acme-corp.auditors"])
        want = {
            "alice": (["acme-corp.admins"], ["admin"], True),
            "opal": (["acme-corp.operators"], ["operator"], True),
            "dave": (["acme-corp.devs"], ["developer"], False),
            "olga": ([], [], False),
            "gina": ([], [], False),
            "gus": ([], [], False),
            "otto": ([], [], False),
            "nora": ([], [], False)}
        for person in PEOPLE:
            for path in fake_keycloak_signin.PATHS:
                for scope in SCOPES:
                    got = self.outcome(res, person, path, scope)
                    if path == "password-grant":
                        # the cluster's client takes no password grant: no token at all
                        self.assertIsNone(got, (person, scope))
                        continue
                    self.assertEqual(got, want[person], (person, path, scope))
        # the client roles are the tenant's, owned by this cluster, mapped to the tenant's groups only
        role = self.kc.role(CLIENT, "acme-corp.admins")
        self.assertEqual(role["attributes"], {"entity": ["acme-corp"], "owner": ["PlatformOpenshift/web"]})
        cid = self.kc.clients[CLIENT]["id"]
        for g in ("globex/admins", "globex/devs", "globex/operators"):
            self.assertEqual(self.kc.groups[g]["mappings"].get(cid, []), [])
        self.assertEqual(sorted(self.kc.clients[CLIENT]["roles"]),
                         ["acme-corp.admins", "acme-corp.devs", "acme-corp.operators"])
        # the scopes that wrote groups are unlinked from this client only (another client keeps them); email stays
        names = {s["id"]: s["name"] for s in self.kc.scopes.values()}
        client = self.kc.clients[CLIENT]
        self.assertEqual(sorted(names[i] for i in client["defaultScopes"]), ["email", "profile"])
        self.assertEqual(client["optionalScopes"], [])
        self.assertEqual(self.kc.clients["openshift-services"]["optionalScopes"], [self.kc.scope_id("microprofile-jwt")])
        # the client is marked as the cluster's and takes the code flow only
        self.assertEqual(client["attributes"]["platformopenshift"], MARK)
        self.assertFalse(client["directAccessGrantsEnabled"])
        self.assertFalse(client["implicitFlowEnabled"])

    def test_a_new_client_is_made_marked_with_the_claim_from_its_roles(self):
        self.run_cluster()
        # recorded as soon as it exists, before the cluster is given the identity provider
        self.assertEqual([p["body"] for p in H.patches(self.trace)], [{"oidcClientId": CLIENT}])
        client = self.kc.clients[CLIENT]
        self.assertEqual(client["attributes"]["platformopenshift"], MARK)
        self.assertEqual([m["protocolMapper"] for m in client["protocolMappers"]], ["oidc-usermodel-client-role-mapper"])
        self.assertEqual(client["optionalScopes"], [])
        self.assertEqual(self.kc.token_claims("gina", CLIENT, scope=("microprofile-jwt",)), [])
        self.assertEqual(self.kc.token_claims("alice", CLIENT), ["acme-corp.admins"])

    def test_a_second_run_changes_nothing(self):
        self.run_cluster()
        before = len([x for x in self.kc.log if x[0] in ("PUT", "POST", "DELETE")])
        _, res, _ = self.run_cluster()
        self.assertEqual(res["csl"]["changed"], [])
        writes = [x for x in self.kc.log if x[0] in ("PUT", "POST", "DELETE")][before:]
        # the common sign-in groups task posts its group mappings again on every run (the identity provider keeps a
        # mapping once); nothing of the cluster's client changes
        self.assertEqual([w for w in writes if not w[1].endswith("/protocol/openid-connect/token")
                          and "/role-mappings/" not in w[1]], [])

    def test_an_access_group_taken_off_the_cluster(self):
        self.run_cluster()
        _, res, _ = self.run_cluster(tool={"clusterAdminRbac": ["admins"]})
        b = bindings(res["templates"])
        self.assertEqual(b["developer"], ("mustnothave", [], None))
        self.assertEqual(b["self-provisioners"], ("mustonlyhave", [], "self-provisioner"))
        self.assertIsNone(self.kc.role(CLIENT, "acme-corp.devs"))
        self.assertEqual(self.kc.token_claims("dave", CLIENT), [])

    def test_no_access_group_no_group_no_binding_no_project(self):
        _, res, _ = self.run_cluster(tool={})
        b = bindings(res["templates"])
        self.assertEqual({k: v[0] for k, v in b.items() if k != "self-provisioners"},
                         dict.fromkeys(["admin", "operator", "developer", "viewer"], "mustnothave"))
        self.assertEqual(b["self-provisioners"][:2], ("mustonlyhave", []))
        for person in PEOPLE:
            self.assertEqual(self.kc.token_claims(person, CLIENT), [])

    def test_self_provisioners_keeps_openshift_from_restoring_it(self):
        _, res, _ = self.run_cluster()
        sp = [t["objectDefinition"] for t in res["templates"]
              if t["objectDefinition"]["metadata"]["name"] == "self-provisioners"][0]
        self.assertEqual(sp["metadata"]["annotations"], {"rbac.authorization.kubernetes.io/autoupdate": "false"})
        subjects = [s["name"] for s in sp["subjects"]]
        self.assertNotIn("system:authenticated:oauth", subjects)
        self.assertNotIn("system:authenticated", subjects)

    def test_a_client_of_the_name_marked_for_another_cluster_is_refused(self):
        self.old_client(attributes={"platformopenshift": "entity-globex/web"}, redirectUris=["https://x.example.org/"])
        rc, _, out = self.run_cluster(ok=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("that the platform made for another cluster", out)
        client = self.kc.clients[CLIENT]
        self.assertEqual(client["attributes"], {"platformopenshift": "entity-globex/web"})
        self.assertEqual(client["redirectUris"], ["https://x.example.org/"])
        self.assertEqual(client["roles"], {})
        self.assertEqual(client["protocolMappers"], [OLD_MAPPER])

    def test_an_unmarked_client_is_taken_only_when_this_cluster_recorded_it(self):
        """A client of the name without a mark (the release before made it): taken when the status records it or the
        build it is named after; otherwise refused and nothing changed."""
        for recorded, taken in (({"oidcClientId": CLIENT}, True), ({"buildName": BUILD}, True), ({}, False),
                                ({"buildName": "shc-oso-web-acme-corp-99999999"}, False)):
            with self.subTest(recorded=recorded):
                self.kc.clients.pop(CLIENT, None)
                self.old_client()
                rc, _, out = self.run_cluster(ok=taken, recorded=recorded)
                client = self.kc.clients[CLIENT]
                if taken:
                    self.assertEqual(client["attributes"]["platformopenshift"], MARK)
                else:
                    self.assertNotEqual(rc, 0)
                    self.assertIn("that this cluster did not record", out)
                    self.assertEqual(client["attributes"], {})
                    self.assertEqual(client["protocolMappers"], [OLD_MAPPER])
                    self.assertEqual(H.patches(self.trace), [])

    def test_a_project_belongs_to_the_groups_not_to_the_person_who_asked(self):
        """opal (operators) asks for a project; then opal leaves the group, or moves to the other tenant: nothing of
        the project is left to opal. The cluster's project template binds admin to the operator groups."""
        _, res, _ = self.run_cluster()
        tpl, cfg = project_template(res["templates"])
        self.assertEqual(tpl["complianceType"], "mustonlyhave")
        self.assertEqual(tpl["objectDefinition"]["metadata"], {"name": "project-request", "namespace": "openshift-config"})
        self.assertEqual(cfg["objectDefinition"]["spec"]["projectRequestTemplate"], {"name": "project-request"})
        self.assertEqual(project_admins(res["templates"]), ["acme-corp.operators"])
        self.assertNotIn("PROJECT_ADMIN_USER}", str(tpl["objectDefinition"]["objects"]))
        rbs = [o for o in tpl["objectDefinition"]["objects"] if o["kind"] == "RoleBinding"]
        self.assertEqual([s["kind"] for rb in rbs for s in rb["subjects"]], ["Group"])
        self.assertEqual(self.outcome(res, "opal", projects=True),
                         (["acme-corp.operators"], ["operator"], True, True))
        # alice is the cluster's admin (cluster-admin holds every project); dave, olga and the other tenant: nothing
        self.assertEqual(self.outcome(res, "alice", projects=True)[3], True)
        for person in ("dave", "olga", "gina", "gus", "otto", "nora"):
            self.assertEqual(self.outcome(res, person, projects=True)[2:], (False, False), person)
        for groups in ((), ("/globex/operators",), ("/acme-corp/others",)):
            with self.subTest(opal_now_in=groups):
                self.kc.add_user("opal", *groups)
                for path in ("browser", "reset-credentials", "brokered"):
                    for scope in SCOPES:
                        self.assertEqual(self.outcome(res, "opal", path, scope, projects=True),
                                         ([], [], False, False), (groups, path, scope))

    def test_an_operator_tier_without_a_group_nobody_requests_projects(self):
        tool = dict(CLUSTER_TOOL)
        del tool["clusterOperatorRbac"]
        _, res, _ = self.run_cluster(tool=tool)
        b = bindings(res["templates"])
        self.assertEqual(b["operator"], ("mustnothave", [], None))
        self.assertEqual(b["self-provisioners"], ("mustonlyhave", [], "self-provisioner"))
        tpl, cfg = project_template(res["templates"])
        # no binding to nobody, and none to the person who asked
        self.assertEqual([o["kind"] for o in tpl["objectDefinition"]["objects"]], ["Project"])
        self.assertEqual(project_admins(res["templates"]), [])
        self.assertIn("no operator access group", cfg["objectDefinition"]["spec"]["projectRequestMessage"])
        self.assertIn("nobody but its admins can request a project", H.LAST_OUTPUT)
        for person in PEOPLE:
            got = self.outcome(res, person, projects=True)
            self.assertEqual(got[2:], (True, True) if person == "alice" else (False, False), person)

    def test_a_realm_role_made_a_composite_of_the_clusters_role_gives_it(self):
        """What stays open, for the realm's administrators only: a realm role that holds this client's role (a
        composite) gives that role, and so the group, to everyone who has the realm role. The job does not make or
        undo composites; this test says what the identity provider does with one."""
        _, res, _ = self.run_cluster()
        self.assertEqual(self.outcome(res, "gina")[0], [])
        self.kc.composites["admins"] = [(CLIENT, "acme-corp.admins")]
        self.assertEqual(self.outcome(res, "gina")[:2], (["acme-corp.admins"], ["admin"]))

    def test_the_token_lifetime_is_a_setting_with_openshifts_default(self):
        def lifetime(res):
            oauth = res["policy"]["spec"]["policy-templates"][2]["objectDefinition"]["spec"]["object-templates"][0]
            return oauth["objectDefinition"]["spec"]["tokenConfig"]
        _, res, _ = self.run_cluster()
        self.assertEqual(lifetime(res), {"accessTokenMaxAgeSeconds": 86400})
        # an integer, also on ansible-core 2.16 (the OAuth field is an integer: a string is refused by the API)
        self.assertIs(type(lifetime(res)["accessTokenMaxAgeSeconds"]), int)
        _, res, _ = self.run_cluster(realization={"tenantCluster": {"oauthAccessTokenMaxAgeSeconds": 28800}})
        self.assertEqual(lifetime(res), {"accessTokenMaxAgeSeconds": 28800})
        self.assertIs(type(lifetime(res)["accessTokenMaxAgeSeconds"]), int)


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_CRYPTO, "ansible-playbook / cryptography not installed")
class TeardownClient(unittest.TestCase):
    """The teardown deletes the client of the recorded clientId when it is this cluster's (its mark, or none: a client
    of the release before) and leaves one marked for another cluster."""

    def setUp(self):
        self.kc = fake_keycloak_signin.Keycloak().start()
        self.addCleanup(self.kc.stop)
        self.vault = fake_vault.Vault().start()
        self.addCleanup(self.vault.stop)
        self.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"] = self.kc.vault_entry()
        env = mock.patch.dict(os.environ, {"VAULT_ADDR": self.vault.url, "VAULT_TOKEN": self.vault.token})
        env.start()
        self.addCleanup(env.stop)

    def run_teardown(self, recorded=CLIENT):
        block = load(os.path.join(TEARDOWN, "tasks", "main.yml"))[0]["block"]
        names = [t.get("name") for t in block]
        a = names.index("Say that no sign-in client is recorded")
        b = names.index("Say that a client of the name is another cluster's")
        tasks = [dict(t) for t in block[a:b + 1]]
        extra = {"keycloak_realm": "sovereign-tenants", "po_oidc_client_id": recorded,
                 "services_api_host": "https://services.example.org", "services_bearer_token": "services-token",
                 "platformopenshift_cr_name": "web", "platformopenshift_cr_namespace": NS}
        k8s = {"Route/services-rhbk/": [{"spec": {"host": self.kc.host}}]}
        rc, _, out = H.run("platformopenshift_teardown", "x.yml", extra, k8s=k8s, tasks=tasks)
        self.assertEqual(rc, 0, out)
        return out

    def test_ours_is_deleted(self):
        self.kc.add_client(CLIENT, attributes={"platformopenshift": MARK})
        self.run_teardown()
        self.assertNotIn(CLIENT, self.kc.clients)

    def test_one_of_the_release_before_is_deleted(self):
        self.kc.add_client(CLIENT)
        self.run_teardown()
        self.assertNotIn(CLIENT, self.kc.clients)

    def test_nothing_recorded_nothing_deleted(self):
        """A cluster whose status records no client deletes none, not even one of its build's name."""
        self.kc.add_client(CLIENT, attributes={"platformopenshift": MARK})
        out = self.run_teardown(recorded="")
        self.assertIn(CLIENT, self.kc.clients)
        self.assertIn("records no sign-in client", out)
        self.assertFalse([x for x in self.kc.log if x[0] == "DELETE"])

    def test_the_client_comes_from_the_status_alone(self):
        tasks = load(os.path.join(TEARDOWN, "tasks", "main.yml"))[0]["block"]
        facts = [t for t in tasks if t.get("name") == "Set platform type and recorded status facts"][0]
        expr = facts["ansible.builtin.set_fact"]["po_oidc_client_id"]
        self.assertIn("_st.oidcClientId", expr)
        self.assertNotIn("keycloak-oidc", expr)

    def test_one_marked_for_another_cluster_is_left(self):
        self.kc.add_client(CLIENT, attributes={"platformopenshift": "entity-globex/web"})
        out = self.run_teardown()
        self.assertIn(CLIENT, self.kc.clients)
        self.assertIn("is marked for another cluster", out)


class Wiring(unittest.TestCase):
    def test_the_cluster_gets_the_identity_provider_after_its_groups_claim(self):
        tasks = load(os.path.join(ROLE, "tasks", "keycloak_oidc.yml"))
        names = [t["name"] for t in tasks]
        self.assertLess(names.index("The cluster's access groups as client roles of its sign-in client"),
                        names.index("The cluster's groups claim holds its own roles only"))
        self.assertLess(names.index("The cluster's groups claim holds its own roles only"),
                        names.index("Build ACM Policy for OpenShift OAuth configuration"))
        body = text(os.path.join(ROLE, "tasks", "keycloak_oidc.yml"))
        self.assertNotIn('"full.path": "false"', body)

    def test_no_binding_cuts_the_tenant_off(self):
        body = text(os.path.join(ROLE, "tasks", "cluster_rbac.yml"))
        self.assertNotIn("regex_replace', '^.*/'", body)
        # musthave (a merge) only for the cluster's project configuration, never for a binding
        i = body.index("complianceType': 'musthave'")
        self.assertEqual(body.count("complianceType': 'musthave'"), 1)
        self.assertIn("'config.openshift.io/v1', 'kind': 'Project'", body[i:i + 200])

    def test_the_request_with_the_token_is_not_logged(self):
        tasks = load(os.path.join(ROLE, "tasks", "cluster_groups_claim.yml"))
        call = [t for t in tasks if "ansible.builtin.command" in t][0]
        self.assertTrue(call["no_log"])

    def test_no_sign_in_flow_and_no_realm_right(self):
        """The cluster's jobs change no authentication flow and need no realm-management right beyond the clients
        and membership automation clients (no flows purpose, no manage-realm)."""
        roots = [ROLE, TEARDOWN, os.path.join(H.ROOT, "docs", "platform", "cluster-installations.md")]
        for root in roots:
            paths = [root] if os.path.isfile(root) else [
                os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if not f.endswith(".pyc")]
            for p in paths:
                body = text(p)
                for word in ("authentication/flows", "authenticationFlowBindingOverrides", "flowsClient",
                             "manage-realm", "deny-access", "krt_purpose: flows"):
                    self.assertNotIn(word, body, p)
        self.assertFalse(os.path.exists(os.path.join(ROLE, "tasks", "cluster_flows_token.yml")))
        self.assertFalse(os.path.exists(os.path.join(ROLE, "tasks", "cluster_signin_limit.yml")))


if __name__ == "__main__":
    unittest.main()

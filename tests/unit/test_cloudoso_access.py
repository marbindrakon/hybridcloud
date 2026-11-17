"""The OpenStack environment's people and its migration, run as task files in ansible-playbook (po_harness.py) with
the real helper against a Keystone over HTTP (fake_keystone.py) and the real Vault task files against a Vault over
HTTP (fake_vault.py); the sign-in directory side is a trace (osoui_roles.FAKE_SSG). What is judged: what Keystone
holds afterwards, the order of the steps (Keystone group and grant before the client role; the client role before
the revoke and the group's deletion), status.access, AccessReady and status.endpoints, and that a second run writes
nothing. Names are examples."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402
import osoui_roles as O  # noqa: E402
import fake_keycloak  # noqa: E402
import osoui_fixtures  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

try:
    import openstack  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

NS = "entity-acme"
RBACS = [{"metadata": {"name": "developers"}, "status": {"group": "/acme/developers"}},
         {"metadata": {"name": "auditors"}, "status": {"group": "/acme/auditors"}},
         {"metadata": {"name": "newcomers"}, "status": {}}]
SIGN_IN = {"identityProvider": "platform-tenants", "protocol": "openid", "displayName": "Example Platform",
           "start": "IdentityProvider"}
START = ("https://identity.site-a.example.org/v3/auth/OS-FEDERATION/identity_providers/platform-tenants/protocols/"
         "openid/websso?origin=https%3A%2F%2Fdashboard.site-a.example.org%2Fauth%2Fwebsso%2F")


def site_obj(declared=True, confirmed=True, ttl=1440, source="Site"):
    spec = {"location": "On-premises", "displayName": "Site A", "openstack": {"projectDomain": "tenants",
            "dashboard": {"url": "https://dashboard.site-a.example.org"}}}
    status = {"site": {"endpoint": "https://identity.site-a.example.org", "region": "RegionOne",
                       "dashboard": "https://dashboard.site-a.example.org"}, "conditions": []}
    if declared:
        spec["openstack"]["signIn"] = dict(SIGN_IN)
        si = dict(SIGN_IN, groupDomain="tenants", startUrl=START, authorizationTTLSource=source)
        if ttl is not None:
            si["authorizationTTLMinutes"] = ttl
        status["site"]["signIn"] = si
        status["conditions"] = [{"type": "SignInConfigured", "status": "True" if confirmed else "False",
                                 "reason": "Confirmed" if confirmed else "IdentityProviderNotFound"}]
    return {"spec": spec, "status": status}


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class EnvironmentAccess(unittest.TestCase):
    def setUp(self):
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        self.pid = self.site.ks.add_project("acme_web", self.site.tenants)
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.assertEqual(self.site.ks.faults, [])

    def run_access(self, tool=None, site=None, recorded=None, ssg=None, fakes=None, k8s=None, rbacs=None):
        ci = site or site_obj()
        extra = {"fake_clouds_yaml": self.site.account, "oso_cloudinfra_name": "site-a", "ci_spec": ci["spec"],
                 "ci_obj": ci, "oso_spec": {"toolRbac": tool if tool is not None else
                                            {"editRbac": ["developers"], "viewRbac": ["auditors"]}},
                 "entity_name": "acme", "cloudoso_cr_name": "web", "cloudoso_cr_namespace": NS,
                 "cloudoso_cr_lookup": {"resources": [{"status": {"access": recorded or {}}}]},
                 "ep_project_domain": "tenants", "ep_vault_path": "cloudinfra/site-a",
                 "ep_os": {"project": {"id": self.pid}}, "services_api_host": "https://services.example.org",
                 "services_bearer_token": "services-token-not-in-data", "fake_ssg": ssg or {"all": [], "owned": {}},
                 "oso_project_name": "acme_web", "oso_project_id": self.pid, "oso_domain_name": "tenants",
                 "oso_domain_id": self.site.tenants, "oso_settings": {"regionName": ""}}
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/access.yml"},
                 {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/endpoints.yml"}]
        rc, trace, out = H.run("cloudoso_provision", "access.yml", extra,
                               k8s=dict({f"Rbac/{NS}/": RBACS if rbacs is None else rbacs}, **(k8s or {})),
                               fakes=O.FAKES if fakes is None else fakes, tasks=tasks,
                               result="{'access': oso_access, 'cond': oso_access_condition, 'ep': oso_endpoints}")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(self.site.account_password, json.dumps(trace) + out)
        return trace, trace[-1]["result"]

    def groups(self):
        return {g["name"]: g for g in self.site.ks.groups.values()}

    def fixture(self, name, res):
        """The status this case writes is the pages' shared example (osoui_fixtures.py)."""
        status = {"access": res["access"], "endpoints": res["ep"],
                  "conditions": [{k: (str(v).strip() if k in ("status", "message", "reason") else v)
                                  for k, v in res["cond"].items()}]}
        osoui_fixtures.check(self, "CloudOSO/" + name, status,
                             {self.pid: "8a1f0c2b9d4e4f00a1b2c3d4e5f60708", self.site.tenants: "51c0aa7e1f2d4c3b9a8e7d6c5b4a3921"})

    def roles_on_project(self, name, pid=None):
        gid = self.groups()[name]["id"]
        return {self.site.ks.roles[r]["name"] for (k, a, sk, sid, r) in self.site.ks.assignments
                if k == "group" and a == gid and sid == (pid or self.pid)}

    def steps(self, trace):
        out = []
        for t in trace:
            if "os" in t:
                out.append(f"{t['os']['action']}/{t['os'].get('state', '')}")
            elif "ssg" in t:
                out.append("client-roles")
        return out

    def test_two_access_groups_granted_in_order(self):
        trace, res = self.run_access()
        self.assertEqual(self.steps(trace), ["access-groups/present", "client-roles"])
        ssg = [t["ssg"] for t in trace if "ssg" in t][0]
        self.assertEqual(ssg["wanted"], ["acme.developers", "acme.auditors"])
        self.assertEqual((ssg["owner"], ssg["client"], ssg["realm"]), ("CloudOSO/web", "openstack-site-a", "sovereign-tenants"))
        self.assertEqual(self.roles_on_project("acme.developers"), {"member"})
        self.assertEqual(self.roles_on_project("acme.auditors"), {"reader"})
        acc = res["access"]
        self.assertEqual((acc["signInClient"], acc["groupDomain"]), ("openstack-site-a", "tenants"))
        self.assertEqual([(g["rbac"], g["group"], g["access"], g["role"], g["state"]) for g in acc["groups"]],
                         [("developers", "acme.developers", "Edit", "member", "Granted"),
                          ("auditors", "acme.auditors", "View", "reader", "Granted")])
        self.assertEqual((str(res["cond"]["status"]), res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("True", "GroupsGranted", "2 access groups can use this environment."))
        ep = res["ep"]
        self.assertEqual(ep["console"], START)
        self.assertEqual(ep["signIn"], {"method": "TenantSSO", "idpName": "Example Platform", "start": "IdentityProvider"})
        self.assertEqual(ep["signInGroups"], ["acme.developers", "acme.auditors"])
        self.assertEqual((ep["region"], ep["location"], ep["projectId"], ep["domainId"]),
                         ("RegionOne", "On-premises", self.pid, self.site.tenants))
        self.assertEqual(ep["apiCredentials"], {"state": "Offered", "validMinutesAfterSignIn": 1440, "source": "Site"})
        self.fixture("granted", res)

    def test_a_second_run_writes_nothing_on_the_site(self):
        _, res = self.run_access()
        n = len(self.site.ks.log)
        self.run_access(recorded=res["access"])
        writes = [c for c in self.site.ks.log[n:] if c[0] in ("POST", "PUT", "PATCH", "DELETE") and c[1] != "/v3/auth/tokens"]
        self.assertEqual(writes, [])

    def test_an_access_group_removed(self):
        _, first = self.run_access()
        trace, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"],
                                     ssg={"all": ["acme.developers", "acme.auditors"],
                                          "owned": {"acme.developers": ["CloudOSO/web"], "acme.auditors": ["CloudOSO/web"]}})
        # the revoke first, on its own; then the delete once the client role went
        self.assertEqual(self.steps(trace), ["access-groups/present", "client-roles", "access-groups/absent",
                                             "access-groups/absent"])
        revoke, delete = [t["os"] for t in trace if "os" in t and t["os"].get("state") == "absent"]
        self.assertEqual(revoke["groups"], [{"group": "acme.auditors", "delete": False}])
        self.assertEqual(delete["groups"], [{"group": "acme.auditors", "delete": True}])
        self.assertNotIn("acme.auditors", self.groups())
        self.assertEqual(self.roles_on_project("acme.developers"), {"member"})
        self.assertEqual(res["cond"]["message"].strip(), "1 access group can use this environment.")

    def test_a_group_another_environment_uses_keeps_its_client_role_and_group(self):
        _, first = self.run_access()
        other = self.site.ks.add_project("acme_db", self.site.tenants)
        self.site.ks.grant("group", self.groups()["acme.auditors"]["id"], "project", other, "reader")
        self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"],
                        ssg={"all": ["acme.developers", "acme.auditors"],
                             "owned": {"acme.auditors": ["CloudOSO/web", "CloudOSO/db"]}})
        self.assertIn("acme.auditors", self.groups())
        self.assertEqual(self.roles_on_project("acme.auditors"), set())
        self.assertEqual(self.roles_on_project("acme.auditors", other), {"reader"})

    def test_an_access_group_without_a_directory_group_waits(self):
        _, res = self.run_access(tool={"editRbac": ["developers", "newcomers"]})
        g = {x["rbac"]: x for x in res["access"]["groups"]}
        self.assertEqual((g["newcomers"]["state"], g["newcomers"]["message"]),
                         ("Waiting", "The access group has no group in the sign-in directory yet."))
        self.fixture("waiting", res)
        self.assertNotIn("acme.newcomers", self.groups())
        self.assertEqual((str(res["cond"]["status"]), res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("False", "WaitingForGroup", "Access group newcomers has no group in the sign-in directory yet."))

    def test_no_access_group(self):
        trace, res = self.run_access(tool={})
        self.assertEqual((str(res["cond"]["status"]), res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("False", "NoAccessGroups", "No access group can use this environment. Nobody can sign in to it."))
        self.fixture("noAccessGroups", res)
        self.assertEqual(res["access"]["groups"], [])

    def test_a_site_without_sign_in(self):
        trace, res = self.run_access(site=site_obj(declared=False))
        self.assertEqual(self.steps(trace), [])
        self.assertEqual(self.site.ks.groups, {})
        self.assertEqual((res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("SignInNotSetUp", "Sign-in with the platform identity is not set up on site site-a."))
        ep = res["ep"]
        self.assertEqual(ep["signIn"], {"method": "NotAvailable", "idpName": None, "start": None})
        # R12: a dashboard nobody can sign in to is not offered as the environment's console
        self.assertIsNone(ep["console"])
        self.fixture("noSignIn", res)
        self.assertEqual(ep["apiCredentials"], {"state": "NotOffered", "validMinutesAfterSignIn": None, "source": None})
        self.assertEqual((res["access"]["signInClient"], res["access"]["groupDomain"]), ("", ""))

    def test_a_declared_sign_in_the_probe_did_not_find(self):
        trace, res = self.run_access(site=site_obj(confirmed=False))
        self.assertEqual(self.steps(trace), [])
        self.assertEqual(res["cond"]["reason"], "SignInNotSetUp")
        self.assertIsNone(res["ep"]["console"])
        self.fixture("notConfirmed", res)

    def test_a_group_the_site_refuses(self):
        self.site.ks.add_group("acme.developers", self.site.tenants, description="the operator's")
        trace, res = self.run_access()
        ssg = [t["ssg"] for t in trace if "ssg" in t][0]
        self.assertEqual(ssg["wanted"], ["acme.auditors"])
        self.assertEqual((res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("GrantRefused", "The site refused the role for group acme.developers: a group named "
                                          "acme.developers exists in domain tenants and is not the platform's."))
        self.fixture("refused", res)

    def test_ttl_not_known_and_ttl_zero(self):
        _, res = self.run_access(site=site_obj(ttl=None, source="Unknown"))
        self.assertEqual(res["ep"]["apiCredentials"], {"state": "Unknown", "validMinutesAfterSignIn": None, "source": None})
        self.fixture("ttlUnknown", res)
        _, res = self.run_access(site=site_obj(ttl=0, source="Declared"))
        self.assertEqual(res["ep"]["apiCredentials"], {"state": "NotOffered", "validMinutesAfterSignIn": 0, "source": "Declared"})
        self.fixture("ttlZero", res)

    def test_start_at_the_dashboard(self):
        ci = site_obj()
        ci["spec"]["openstack"]["signIn"]["start"] = "Dashboard"
        ci["status"]["site"]["signIn"].update(start="Dashboard", startUrl="https://dashboard.site-a.example.org")
        _, res = self.run_access(site=ci)
        self.assertEqual(res["ep"]["signIn"], {"method": "TenantSSO", "idpName": "Example Platform", "start": "Dashboard"})
        self.assertEqual(res["ep"]["console"], "https://dashboard.site-a.example.org")
        self.fixture("startDashboard", res)

    def test_a_federation_group_domain(self):
        fed = self.site.ks.add_domain("platform-federated")
        self.site.ks.grant("user", self.site.account_user, "domain", fed, "admin")
        ci = site_obj()
        ci["spec"]["openstack"]["signIn"]["groupDomain"] = "platform-federated"
        ci["status"]["site"]["signIn"]["groupDomain"] = "platform-federated"
        _, res = self.run_access(site=ci)
        self.assertEqual(res["access"]["groupDomain"], "platform-federated")
        self.assertEqual(self.groups()["acme.developers"]["domain_id"], fed)

    def test_the_group_domain_is_the_sites_not_the_environments(self):
        """R15: without a declared group domain, the site's project domain (spec.openstack.projectDomain), never the
        domain the environment's record names."""
        fed = self.site.ks.add_domain("site-domain")
        self.site.ks.grant("user", self.site.account_user, "domain", fed, "admin")
        ci = site_obj()
        ci["spec"]["openstack"]["projectDomain"] = "site-domain"
        del ci["status"]["site"]["signIn"]["groupDomain"]
        _, res = self.run_access(site=ci)
        self.assertEqual(res["access"]["groupDomain"], "site-domain")
        self.assertEqual(self.groups()["acme.developers"]["domain_id"], fed)


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK and HAVE_CRYPTO, "ansible-playbook / openstacksdk / cryptography not installed")
class EnvironmentAccessRealDirectory(EnvironmentAccess):
    """access.yml with the real common/tasks/services_signin_groups.yml and obtain_keycloak_realm_token.yml against a
    sign-in directory admin API over HTTPS (fake_keycloak.py): the client roles, their owners, the last owner, the
    group mappings; and what the access step does when the directory or the site does not answer, refuses, or the
    site's sign-in is not confirmed or no longer declared (R4, R5, R7, R10). The inherited cases run here too, with
    the real directory side."""

    def setUp(self):
        super().setUp()
        self.kc = fake_keycloak.Keycloak().start()
        self.addCleanup(self.kc.stop)
        self.kc_client = self.kc.add_client("openstack-site-a")
        self.kc.add_group("/acme/developers")
        self.kc.add_group("/acme/auditors")
        self.site.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"] = self.kc.vault_entry()

    def run_access(self, tool=None, site=None, recorded=None, ssg=None, fakes=None, k8s=None, rbacs=None):
        real = {k: v for k, v in O.FAKES.items() if k != "services_signin_groups.yml"}
        return super().run_access(tool=tool, site=site, recorded=recorded, ssg=ssg, fakes=real, rbacs=rbacs,
                                  k8s={"Route/services-rhbk/": [{"spec": {"host": self.kc.host}}]})

    def owners(self, name):
        r = self.kc.role("openstack-site-a", name)
        return None if r is None else r["attributes"].get("owner")

    def other_env_owns(self, name):
        r = self.kc.role("openstack-site-a", name)
        r["attributes"]["owner"] = r["attributes"]["owner"] + ["CloudOSO/db"]

    def steps(self, trace):
        return [f"{t['os']['action']}/{t['os'].get('state', '')}" for t in trace if "os" in t]

    # the inherited cases that name the trace of the faked directory, or hand it its answer, are re-stated here
    def test_two_access_groups_granted_in_order(self):
        trace, res = self.run_access()
        self.assertEqual(self.steps(trace), ["access-groups/present"])
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertEqual(self.kc.role("openstack-site-a", "acme.developers")["attributes"]["entity"], ["acme"])
        self.assertEqual(self.kc.groups["acme/developers"]["mappings"][self.kc_client], ["acme.developers"])
        self.assertEqual(self.kc.groups["acme/auditors"]["mappings"][self.kc_client], ["acme.auditors"])
        # Keystone before the directory: the first directory write comes after the last grant
        self.assertEqual((str(res["cond"]["status"]), res["cond"]["reason"]), ("True", "GroupsGranted"))
        self.assertEqual(res["access"]["removing"], [])

    def test_an_access_group_removed(self):
        _, first = self.run_access()
        trace, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"])
        absent = [t["os"] for t in trace if "os" in t and t["os"].get("state") == "absent"][-1]
        self.assertEqual(absent["groups"], [{"group": "acme.auditors", "delete": True}])
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertNotIn("acme.auditors", self.groups())
        self.assertEqual(res["access"]["removing"], [])

    def test_a_group_another_environment_uses_keeps_its_client_role_and_group(self):
        """R10 (access.yml asking delete always): the client role another environment still owns stays, and the
        request says delete false (the helper's own check is not what keeps the group)."""
        _, first = self.run_access()
        self.other_env_owns("acme.auditors")
        trace, _ = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"])
        absent = [t["os"] for t in trace if "os" in t and t["os"].get("state") == "absent"][-1]
        self.assertEqual(absent["groups"], [{"group": "acme.auditors", "delete": False}])
        self.assertEqual(self.owners("acme.auditors"), ["CloudOSO/db"])
        self.assertIn("acme.auditors", self.groups())
        self.assertEqual(self.roles_on_project("acme.auditors"), set())

    def test_a_group_the_site_refuses(self):
        self.site.ks.add_group("acme.developers", self.site.tenants, description="the operator's")
        _, res = self.run_access()
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.developers"))
        self.assertEqual(self.owners("acme.auditors"), ["CloudOSO/web"])
        self.assertEqual((res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("GrantRefused", "The site refused the role for group acme.developers: a group named "
                                          "acme.developers exists in domain tenants and is not the platform's."))

    def test_the_directory_not_answering_keeps_the_environment_and_the_groups(self):
        """R4: the job goes on (rc 0, the status is written), AccessReady AccessNotChecked, the last states stay."""
        _, first = self.run_access()
        self.kc.down = True
        trace, res = self.run_access(recorded=first["access"])
        self.assertEqual((res["cond"]["status"], res["cond"]["reason"]), ("False", "AccessNotChecked"))
        self.assertIn("could not be checked this time", res["cond"]["message"])
        self.assertEqual([g["state"] for g in res["access"]["groups"]], ["Granted", "Granted"])
        self.assertTrue(all(g["message"].startswith("Could not be checked this time") for g in res["access"]["groups"]))
        self.fixture("notChecked", res)
        self.kc.down = False
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        _, res = self.run_access(recorded=res["access"])
        self.assertEqual(res["cond"]["reason"], "GroupsGranted")

    def test_a_new_environment_with_the_directory_down_still_gets_its_access_record(self):
        self.kc.down = True
        _, res = self.run_access()
        self.assertEqual(res["cond"]["reason"], "AccessNotChecked")
        self.assertEqual([g["state"] for g in res["access"]["groups"]], ["Waiting", "Waiting"])

    def test_a_role_of_another_entity_is_a_refusal_not_a_failed_job(self):
        self.kc.clients["openstack-site-a"]["roles"]["acme.developers"] = {
            "id": "r1", "name": "acme.developers", "description": "", "attributes": {"entity": ["globex"]}}
        _, res = self.run_access()
        self.assertEqual(res["cond"]["reason"], "GrantRefused")
        self.assertEqual(res["cond"]["message"].strip(),
                         "The sign-in directory refused group acme.developers: Client role acme.developers of "
                         "openstack-site-a exists but is not tagged entity=acme.")
        # V2: the other group goes on
        self.assertEqual([g["state"] for g in res["access"]["groups"]], ["Failed", "Granted"])
        self.assertEqual(self.owners("acme.auditors"), ["CloudOSO/web"])

    def test_a_5xx_on_one_grant_keeps_its_ownership(self):
        """R5: a group the site did not answer for keeps its state and this environment's ownership of its client
        role (a transient fault never deletes a client role people sign in with)."""
        _, first = self.run_access()
        gid = self.groups()["acme.developers"]["id"]
        self.site.ks.assignments = {a for a in self.site.ks.assignments if a[1] != gid}
        self.site.ks.fail_next.append(("PUT", f"/v3/projects/{self.pid}/groups/{gid}", 503, "Service Unavailable"))
        _, res = self.run_access(recorded=first["access"])
        dev = res["access"]["groups"][0]
        self.assertEqual((dev["state"], dev["message"]), ("Granted", "Could not be checked this time: Service Unavailable."))
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertEqual(res["cond"]["reason"], "AccessNotChecked")

    def test_a_refusal_of_a_group_that_held_its_roles_keeps_the_ownership(self):
        _, first = self.run_access()
        gid = self.groups()["acme.auditors"]["id"]
        self.site.ks.fail_next.append(("GET", "/v3/role_assignments", 403, "You are not authorized"))
        _, res = self.run_access(recorded=first["access"])
        self.assertEqual(res["access"]["groups"][0]["state"], "Failed")
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertTrue(gid)

    def test_removal_while_sign_in_is_not_confirmed(self):
        """R7(a): the tenant removes a group while the site's SignInConfigured is False: the group still goes."""
        _, first = self.run_access()
        trace, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"],
                                     site=site_obj(confirmed=False))
        self.assertEqual(self.steps(trace), ["access-groups/absent", "access-groups/absent"])
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertNotIn("acme.auditors", self.groups())
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertEqual(self.roles_on_project("acme.developers"), {"member"})
        self.assertEqual(res["cond"]["reason"], "SignInNotSetUp")
        self.assertEqual(res["access"]["removing"], [])

    def test_sign_in_no_longer_declared(self):
        """R7(b): the groups' roles are revoked, the environment leaves the client roles, then the record of the
        client and group domain goes; while the directory is down it stays and names what is still to go."""
        _, first = self.run_access()
        self.kc.down = True
        _, res = self.run_access(recorded=first["access"], site=site_obj(declared=False))
        self.assertEqual(res["access"]["removing"], ["acme.auditors", "acme.developers"])
        self.assertEqual((res["access"]["signInClient"], res["access"]["groupDomain"]), ("openstack-site-a", "tenants"))
        self.assertIn("is not finished", res["cond"]["message"])
        self.kc.down = False
        _, res = self.run_access(recorded=res["access"], site=site_obj(declared=False))
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.developers"))
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertEqual(self.groups(), {})
        self.assertEqual((res["access"]["signInClient"], res["access"]["groupDomain"], res["access"]["removing"]),
                         ("", "", []))
        self.assertEqual(res["ep"]["console"], None)

    # ---- second pass: V2, V7, V8 ----------------------------------------------------------------------------------
    def test_a_group_missing_from_the_directory_holds_no_other_removal(self):
        """V2: a new access group whose directory group is gone is that group's fault (Waiting, named); the removal
        of another group goes on in the same run."""
        _, first = self.run_access()
        rbacs = RBACS + [{"metadata": {"name": "newbies"}, "status": {"group": "/acme/newbies"}}]
        trace, res = self.run_access(tool={"editRbac": ["developers", "newbies"]}, recorded=first["access"], rbacs=rbacs)
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertNotIn("acme.auditors", self.groups())
        g = {x["rbac"]: x for x in res["access"]["groups"]}
        self.assertEqual((g["newbies"]["state"], g["newbies"]["message"]),
                         ("Waiting", "Its group /acme/newbies is not in the sign-in directory."))
        self.assertEqual(g["developers"]["state"], "Granted")
        self.assertEqual(res["access"]["removing"], [])
        self.assertEqual((res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("WaitingForGroup", "Access group newbies is not usable yet: Its group /acme/newbies is not "
                                             "in the sign-in directory."))
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.newbies"))

    def test_the_sites_client_missing_in_the_directory_still_revokes(self):
        """V2 (Q5): the directory cannot be used at all: the Keystone role of a group no longer wanted is revoked
        anyway (the group stays until its client role went); the next run finishes."""
        _, first = self.run_access()
        client = self.kc.clients.pop("openstack-site-a")
        _, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"])
        self.assertEqual(self.roles_on_project("acme.auditors"), set())
        self.assertIn("acme.auditors", self.groups())
        self.assertEqual(res["access"]["removing"], ["acme.auditors"])
        self.assertEqual(res["cond"]["reason"], "GrantRefused")
        self.kc.clients["openstack-site-a"] = client
        _, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=res["access"])
        self.assertNotIn("acme.auditors", self.groups())
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertEqual(res["access"]["removing"], [])

    def test_without_a_confirmed_sign_in_the_revoke_never_waits_for_the_directory(self):
        """W2 (Q5): sign-in not confirmed and the site's client missing in the directory: the removed group's role on
        the project is revoked in the same run; the directory part stays in removing until a later run."""
        _, first = self.run_access()
        client = self.kc.clients.pop("openstack-site-a")
        _, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"],
                                 site=site_obj(confirmed=False))
        self.assertEqual(self.roles_on_project("acme.auditors"), set())
        self.assertEqual(self.roles_on_project("acme.developers"), {"member"})
        self.assertIn("acme.auditors", self.groups())
        self.assertEqual(res["access"]["removing"], ["acme.auditors"])
        self.kc.clients["openstack-site-a"] = client
        _, res = self.run_access(tool={"editRbac": ["developers"]}, recorded=res["access"],
                                 site=site_obj(confirmed=False))
        self.assertNotIn("acme.auditors", self.groups())
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertEqual(res["access"]["removing"], [])

    def test_a_pending_removal_is_finished_at_the_next_run(self):
        """V7: the removed group is only in status.access.removing (the groups list holds the wanted ones): the next
        run still removes it."""
        _, first = self.run_access()
        self.kc.down = True
        _, second = self.run_access(tool={"editRbac": ["developers"]}, recorded=first["access"])
        self.assertEqual(second["access"]["removing"], ["acme.auditors"])
        self.assertEqual([g["group"] for g in second["access"]["groups"]], ["acme.developers"])
        self.kc.down = False
        _, third = self.run_access(tool={"editRbac": ["developers"]}, recorded=second["access"])
        self.assertNotIn("acme.auditors", self.groups())
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.auditors"))
        self.assertEqual(third["access"]["removing"], [])
        self.fixture("removing", second)

    def test_an_rbac_without_its_directory_group_for_a_run_keeps_what_it_had(self):
        """V8: while the Rbac exists and is wanted, its client role and its Keystone grant stay (Waiting); it leaves
        when the Rbac is gone."""
        _, first = self.run_access()
        no_group = [{"metadata": {"name": "developers"}, "status": {}}] + RBACS[1:]
        _, res = self.run_access(recorded=first["access"], rbacs=no_group)
        dev = res["access"]["groups"][0]
        self.assertEqual((dev["state"], dev["message"]), ("Waiting", "The access group has no group in the sign-in directory yet."))
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertEqual(self.roles_on_project("acme.developers"), {"member"})
        _, res = self.run_access(recorded=res["access"], rbacs=RBACS[1:])
        self.assertIsNone(self.kc.role("openstack-site-a", "acme.developers"))
        self.assertNotIn("acme.developers", self.groups())
        self.assertEqual(res["access"]["groups"][0]["message"], "The access group does not exist in this tenant.")



HAVE_AWS_COLLECTION = os.path.isdir(os.path.join(H.SYSTEM_COLLECTIONS, "ansible_collections", "amazon", "aws"))


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK and HAVE_AWS_COLLECTION,
                     "ansible-playbook / openstacksdk / amazon.aws (environmentprep.yml names a module of it) not here: "
                     "runs in the job's image")
class Migration(unittest.TestCase):
    """environmentprep.yml on a record of an earlier release: the site account's application credential and its
    Vault path go, the project stays, no credential is made, nothing secret reaches the record."""

    def setUp(self):
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)

    def run_prep(self, status):
        extra = {"fake_clouds_yaml": self.site.account, "entity_name": "acme", "cloudoso_cr_name": "web",
                 "cloudoso_cr_namespace": NS, "oso_cloudinfra_name": "site-a",
                 "cloudoso_cr_lookup": {"resources": [{"status": status}]},
                 "oso_settings": {"vaultPath": "cloudinfra/site-a", "projectDomain": "tenants", "baseDomain": "",
                                  "regionName": "", "dnsProvider": "none", "designateZoneId": "",
                                  "designateProjectId": "", "route53VaultPath": ""},
                 "services_api_host": "https://services.example.org", "services_bearer_token": "services-token-not-in-data"}
        rc, trace, out = H.run("cloudoso_provision", "environmentprep.yml", extra, fakes=O.FAKES,
                               result="{'record': oso_record, 'project': oso_project_id}")
        self.assertEqual(rc, 0, out)
        return trace, trace[-1]["result"]

    def test_the_legacy_credential_and_its_vault_path_go(self):
        ks = self.site.ks
        _, first = self.run_prep({})
        pid = first["project"]
        ks.app_creds["legacy-1"] = {"id": "legacy-1", "user": self.site.account_user, "project": pid,
                                    "name": "sovereign-acme_web", "description": "", "secret": "legacy-secret-value",
                                    "roles": ["member"], "unrestricted": False, "expires_at": None}
        self.site.vault.data["central/oso/projects/web/clouds-config"] = {"clouds.yaml": "legacy-secret-value"}
        trace, res = self.run_prep({"appCredentialId": "legacy-1", "projectId": pid})
        self.assertNotIn("legacy-1", ks.app_creds)
        self.assertNotIn("central/oso/projects/web/clouds-config", self.site.vault.data)
        self.assertEqual(res["project"], pid)
        self.assertIsNone(res["record"]["appCredentialId"])
        self.assertEqual([c for c in ks.log if c[0] == "POST" and "application_credentials" in c[1]], [])
        self.assertEqual([c for c in self.site.vault.log if c[0] == "POST"], [])
        self.assertNotIn("legacy-secret-value", json.dumps(trace))

    def test_a_record_without_a_legacy_credential_touches_no_vault_path(self):
        self.run_prep({})
        self.run_prep({"appCredentialId": ""})
        self.assertEqual([c for c in self.site.vault.log if c[0] == "DELETE"], [])


if __name__ == "__main__":
    unittest.main()

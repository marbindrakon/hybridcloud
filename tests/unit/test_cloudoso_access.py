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

    def run_access(self, tool=None, site=None, recorded=None, ssg=None):
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
        rc, trace, out = H.run("cloudoso_provision", "access.yml", extra, k8s={f"Rbac/{NS}/": RBACS},
                               fakes=O.FAKES, tasks=tasks,
                               result="{'access': oso_access, 'cond': oso_access_condition, 'ep': oso_endpoints}")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(self.site.account_password, json.dumps(trace) + out)
        return trace, trace[-1]["result"]

    def groups(self):
        return {g["name"]: g for g in self.site.ks.groups.values()}

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
        self.assertEqual(self.steps(trace), ["access-groups/present", "client-roles", "access-groups/absent"])
        absent = [t["os"] for t in trace if "os" in t and t["os"].get("state") == "absent"][0]
        self.assertEqual(absent["groups"], [{"group": "acme.auditors", "delete": True}])
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
        self.assertNotIn("acme.newcomers", self.groups())
        self.assertEqual((str(res["cond"]["status"]), res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("False", "WaitingForGroup", "Access group newcomers has no group in the sign-in directory yet."))

    def test_no_access_group(self):
        trace, res = self.run_access(tool={})
        self.assertEqual((str(res["cond"]["status"]), res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("False", "NoAccessGroups", "No access group can use this environment. Nobody can sign in to it."))
        self.assertEqual(res["access"]["groups"], [])

    def test_a_site_without_sign_in(self):
        trace, res = self.run_access(site=site_obj(declared=False))
        self.assertEqual(self.steps(trace), [])
        self.assertEqual(self.site.ks.groups, {})
        self.assertEqual((res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("SignInNotSetUp", "Sign-in with the platform identity is not set up on site site-a."))
        ep = res["ep"]
        self.assertEqual(ep["signIn"], {"method": "NotAvailable", "idpName": None, "start": None})
        self.assertEqual(ep["console"], "https://dashboard.site-a.example.org")
        self.assertEqual(ep["apiCredentials"], {"state": "NotOffered", "validMinutesAfterSignIn": None, "source": None})
        self.assertEqual((res["access"]["signInClient"], res["access"]["groupDomain"]), ("", ""))

    def test_a_declared_sign_in_the_probe_did_not_find(self):
        trace, res = self.run_access(site=site_obj(confirmed=False))
        self.assertEqual(self.steps(trace), [])
        self.assertEqual(res["cond"]["reason"], "SignInNotSetUp")

    def test_a_group_the_site_refuses(self):
        self.site.ks.add_group("acme.developers", self.site.tenants, description="the operator's")
        trace, res = self.run_access()
        ssg = [t["ssg"] for t in trace if "ssg" in t][0]
        self.assertEqual(ssg["wanted"], ["acme.auditors"])
        self.assertEqual((res["cond"]["reason"], res["cond"]["message"].strip()),
                         ("GrantRefused", "The site refused the role for group acme.developers: a group named "
                                          "acme.developers exists in domain tenants and is not the platform's."))

    def test_ttl_not_known_and_ttl_zero(self):
        _, res = self.run_access(site=site_obj(ttl=None, source="Unknown"))
        self.assertEqual(res["ep"]["apiCredentials"], {"state": "Unknown", "validMinutesAfterSignIn": None, "source": None})
        _, res = self.run_access(site=site_obj(ttl=0, source="Declared"))
        self.assertEqual(res["ep"]["apiCredentials"], {"state": "NotOffered", "validMinutesAfterSignIn": 0, "source": "Declared"})

    def test_a_federation_group_domain(self):
        fed = self.site.ks.add_domain("platform-federated")
        self.site.ks.grant("user", self.site.account_user, "domain", fed, "admin")
        ci = site_obj()
        ci["spec"]["openstack"]["signIn"]["groupDomain"] = "platform-federated"
        _, res = self.run_access(site=ci)
        self.assertEqual(res["access"]["groupDomain"], "platform-federated")
        self.assertEqual(self.groups()["acme.developers"]["domain_id"], fed)


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
                                    "roles": ["member"], "unrestricted": False, "expires_at": None, "created_at": ""}
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

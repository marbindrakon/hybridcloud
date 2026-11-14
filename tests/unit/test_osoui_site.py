"""An OpenStack site's sign-in for people (cloudinfrastructure_provision openstack_signin.yml) and what tenants see of
it (common/tasks/site_tenant_summary.yml, NetworkPlacement status.siteNetwork). The probe's sign-in reads are made by
the real helper (shc_openstack.sign_in_reads) against a Keystone over HTTP (fake_keystone.py) and handed to the task
file as the probe's answer; the sign-in directory check (common/tasks/keycloak_client_check.yml) answers from
fake_kcc. Names are examples."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eda", "common", "files"))
import po_harness as H  # noqa: E402
import osoui_roles as O  # noqa: E402
from play_helper import HAVE_ANSIBLE, by_name, load, run_cases, run_play, write_out  # noqa: E402

try:
    import openstack  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

FAKE_KCC = O.TRACE % "{'kcc': [kcc_realm, kcc_client_id]}" + """
- name: fake directory answer
  ansible.builtin.set_fact:
    kcc_result: "{{ fake_kcc | default({'checked': true, 'found': true, 'groupsMapper': true, 'note': ''}) }}"
"""
DASH = "https://dashboard.site-a.example.org"
DECL = {"identityProvider": "platform-tenants", "protocol": "openid", "displayName": "Example Platform",
        "start": "IdentityProvider"}
MAPPING = [{"local": [{"user": {"id": "{0}", "name": "{1}"}}],
            "remote": [{"type": "OIDC-sub"}, {"type": "OIDC-preferred_username"}]},
           {"local": [{"groups": "{0}", "domain": {"name": "tenants"}}], "remote": [{"type": "OIDC-groups"}]}]


@unittest.skipUnless(HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class SiteSignIn(unittest.TestCase):
    def setUp(self):
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)
        import shc_openstack as S
        self.S = S

    def declare(self, ttl=1440, rules=None):
        ks = self.site.ks
        fed = ks.add_domain("platform-federated")
        ks.idps["platform-tenants"] = {"id": "platform-tenants", "enabled": True, "domain_id": fed,
                                       "remote_ids": ["https://sso.example.org/realms/tenants"], "authorization_ttl": ttl}
        ks.protocols[("platform-tenants", "openid")] = {"id": "openid", "mapping_id": "m1"}
        ks.mappings["m1"] = {"id": "m1", "rules": rules or MAPPING}

    def reads(self, decl, clouds=None, dashboard=DASH):
        S = self.S
        acct = S.Account({"clouds_yaml": clouds or self.site.account})
        conn = acct.own_scope()
        notes = []
        out = S.sign_in_reads(S.Keystone(acct), conn.session.get_token(),
                              conn.session.auth.get_access(conn.session).user_id,
                              {"identity_provider": decl["identityProvider"], "protocol": decl["protocol"],
                               "group_domain": decl.get("groupDomain") or "tenants", "start": decl.get("start"),
                               "dashboard_url": dashboard}, notes)
        return out, notes

    def run_si(self, decl, reads=None, notes=(), kcc=None, dashboard=DASH):
        ci_os = {"projectDomain": "tenants", "dashboard": {"url": dashboard}} if dashboard else {"projectDomain": "tenants"}
        if decl:
            ci_os["signIn"] = decl
        probe = {"notes": list(notes)}
        if reads is not None:
            probe["sign_in"] = reads
        extra = {"ci_os": ci_os, "ci_signin": decl or {}, "ci_probe": probe, "cloudinfrastructure_cr_name": "site-a",
                 "services_api_host": "https://services.example.org", "services_bearer_token": "services-token-not-in-data"}
        if kcc is not None:
            extra["fake_kcc"] = kcc
        # the probe's facts as openstack.yml set them (extra vars could not be changed by the file)
        tasks = [{"ansible.builtin.set_fact": {
                     "ci_site_out": {"endpoint": "https://identity.site-a.example.org", "region": "RegionOne"},
                     "ci_capabilities_out": {"api": True, "evpn": False}, "ci_notes": []}},
                 {"ansible.builtin.include_tasks": "{{ role_path }}/tasks/openstack_signin.yml"}]
        rc, trace, out = H.run("cloudinfrastructure_provision", "openstack_signin.yml", extra, tasks=tasks,
                               fakes={"keycloak_client_check.yml": FAKE_KCC},
                               result="{'site': ci_site_out, 'caps': ci_capabilities_out, "
                                      "'cond': ci_extra_conditions[0], 'notes': ci_notes}")
        self.assertEqual(rc, 0, out)
        r = trace[-1]["result"]
        r["cond"]["status"] = str(r["cond"]["status"])
        return trace, r

    def cond(self, r):
        return (r["cond"]["status"], r["cond"]["reason"], r["cond"]["message"].strip())

    def test_not_declared(self):
        trace, r = self.run_si(None, dashboard="")
        self.assertEqual(self.cond(r), ("False", "NotDeclared", "No sign-in is declared for this site."))
        self.assertEqual((r["site"]["signIn"], r["site"]["dashboard"]), (None, None))
        self.assertEqual((r["caps"]["consoleSignIn"], r["caps"]["apiCredentials"]), (False, False))
        self.assertFalse([t for t in trace if "kcc" in t])

    def test_confirmed_by_an_administrator(self):
        self.declare()
        reads, notes = self.reads(DECL)
        trace, r = self.run_si(DECL, reads, notes)
        self.assertEqual(self.cond(r), ("True", "Confirmed",
                                        "Identity provider platform-tenants, its protocol and mapping were read from the site."))
        si = r["site"]["signIn"]
        self.assertEqual((si["authorizationTTLMinutes"], si["authorizationTTLSource"]), (1440, "Site"))
        self.assertEqual(si["startUrl"], reads["startUrl"]["url"])
        self.assertTrue(si["startUrl"].endswith("/websso?origin=https%3A%2F%2Fdashboard.site-a.example.org%2Fauth%2Fwebsso%2F"))
        self.assertEqual(sorted(si["confirmed"]), ["client", "groupDomain", "identityProvider", "mapping", "protocol",
                                                   "startUrl"])
        self.assertEqual(si["notConfirmed"], [])
        self.assertEqual((si["groupDomain"], si["groupDomainId"], si["userDomain"]),
                         ("tenants", self.site.tenants, "platform-federated"))
        self.assertIn({"role": "admin", "scope": "domain tenants"}, si["account"])
        self.assertEqual(r["site"]["dashboard"], DASH)
        self.assertEqual((r["caps"]["consoleSignIn"], r["caps"]["apiCredentials"]), (True, True))
        self.assertEqual([t["kcc"] for t in trace if "kcc" in t], [["sovereign-tenants", "openstack-site-a"]])

    def test_a_domain_manager_cannot_confirm_it(self):
        self.declare(ttl=None)
        mgr = O.Site(manager=True)
        self.addCleanup(mgr.stop)
        decl = dict(DECL, authorizationTTLMinutes=1440)
        # the manager account of a second fake: the same reads a domain manager gets (403 on the federation API)
        mgr.ks.idps.update(self.site.ks.idps)
        reads, _ = self.reads(decl, clouds=mgr.account)
        _, r = self.run_si(decl, reads)
        self.assertEqual(self.cond(r), ("True", "DeclaredNotConfirmed",
                                        "Declared by the platform team. The account may not read the site's federation settings."))
        si = r["site"]["signIn"]
        self.assertEqual((si["authorizationTTLMinutes"], si["authorizationTTLSource"]), (1440, "Declared"))
        self.assertIn("identityProvider", si["notConfirmed"])
        self.assertEqual((r["caps"]["consoleSignIn"], r["caps"]["apiCredentials"]), (True, True))

    def test_ttl_not_known_and_ttl_zero(self):
        self.declare(ttl=None)
        reads, _ = self.reads(DECL)
        _, r = self.run_si(DECL, reads)
        self.assertEqual((r["site"]["signIn"]["authorizationTTLMinutes"], r["site"]["signIn"]["authorizationTTLSource"]),
                         (None, "Unknown"))
        self.assertEqual((r["caps"]["consoleSignIn"], r["caps"]["apiCredentials"]), (True, False))
        self.site.ks.idps["platform-tenants"]["authorization_ttl"] = 0
        reads, _ = self.reads(DECL)
        _, r = self.run_si(DECL, reads)
        self.assertEqual((r["site"]["signIn"]["authorizationTTLMinutes"], r["site"]["signIn"]["authorizationTTLSource"]),
                         (0, "Site"))
        self.assertFalse(r["caps"]["apiCredentials"])

    def test_what_is_missing(self):
        reads, _ = self.reads(DECL)   # no provider on the site
        _, r = self.run_si(DECL, reads)
        self.assertEqual(self.cond(r), ("False", "IdentityProviderNotFound",
                                        "Identity provider platform-tenants does not exist on the site."))
        self.assertEqual(r["caps"]["consoleSignIn"], False)
        self.declare()
        decl = dict(DECL, groupDomain="nowhere")
        reads, _ = self.reads(decl)
        _, r = self.run_si(decl, reads)
        self.assertEqual(self.cond(r), ("False", "GroupDomainNotFound", "The group domain nowhere does not exist on the site."))
        reads, _ = self.reads(DECL)
        _, r = self.run_si(DECL, reads, kcc={"checked": True, "found": False, "groupsMapper": False, "note": ""})
        self.assertEqual(self.cond(r), ("False", "ClientNotFound",
                                        "The sign-in directory has no client openstack-site-a for this site."))

    def test_a_mapping_warning_does_not_switch_sign_in_off(self):
        self.declare(rules=[{"local": [{"user": {"name": "{0}"}}, {"group": {"name": "{1}", "domain": {"name": "x"}}}],
                             "remote": [{"type": "OIDC-preferred_username"}, {"type": "OIDC-groups"}]}])
        reads, _ = self.reads(DECL)
        _, r = self.run_si(DECL, reads)
        self.assertEqual(self.cond(r), ("True", "MappingWarning", "The site's mapping does not look groups up in "
                                                                  "tenants, has no stable user id."))
        self.assertTrue(r["caps"]["consoleSignIn"])

    def test_a_directory_that_could_not_be_asked(self):
        self.declare()
        reads, _ = self.reads(DECL)
        _, r = self.run_si(DECL, reads, kcc={"checked": False, "found": False, "groupsMapper": False,
                                             "note": "the sign-in directory could not be asked about client openstack-site-a"})
        self.assertEqual(r["cond"]["reason"], "Confirmed")
        self.assertIn("client", r["site"]["signIn"]["notConfirmed"])
        self.assertIn("the sign-in directory could not be asked about client openstack-site-a", r["notes"])


COMMON = os.path.join(O.ROOT, "eda", "common")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TenantView(unittest.TestCase):
    def test_capabilities_reach_the_tenant_profile(self):
        tasks = load(os.path.join(COMMON, "tasks", "site_tenant_summary.yml")) + [write_out("site_tenant_summaries")]
        sites = [{"metadata": {"name": "site-a"}, "spec": {"type": "openstack"},
                  "status": {"capabilities": {"api": True, "consoleSignIn": True, "apiCredentials": False}}},
                 {"metadata": {"name": "site-b"}, "spec": {"type": "openstack"},
                  "status": {"capabilities": {"api": True, "consoleSignIn": True, "apiCredentials": True}}}]
        out = run_play(tasks, {"sts_sites": sites})
        self.assertEqual([s["capabilities"] for s in out], [["ConsoleSignIn"], ["ConsoleSignIn", "ApiCredentials"]])


NP = os.path.join(O.ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks", "backend_cloudoso.yml")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementSiteNetwork(unittest.TestCase):
    def test_the_network_a_tenant_chooses(self):
        tasks = load(NP)
        steps = [{"ansible.builtin.set_fact": {
            "os_result": "{{ case }}", "np_status": {"entity": "acme"}, "np_names": {"osNetwork": "hn51001"},
            "np_backend": {"status": {"projectId": "proj-tenant"}}, "fab_spec": {},
            "fab_real": {"openstack": {"platformProject": "platform"}}}}]
        steps += [by_name(tasks, n) for n in ("Record the site side", "The network on the site, for the tenant")]
        case = {"networkId": "net-1", "routerId": "r-1", "subnets": {"2001:db8:1::/64": "sn-6", "192.0.2.0/24": "sn-4"},
                "gateways": {"192.0.2.0/24": "192.0.2.1", "2001:db8:1::/64": "2001:db8:1::1"}, "created": [],
                "changed": [], "shareId": "rb-1"}
        out = run_cases(steps, [case], "np_status.siteNetwork")
        self.assertEqual(out[0], {"name": "hn51001", "id": "net-1", "project": "platform", "subnets": [
            {"family": 4, "prefix": "192.0.2.0/24", "id": "sn-4", "gateway": "192.0.2.1"},
            {"family": 6, "prefix": "2001:db8:1::/64", "id": "sn-6", "gateway": "2001:db8:1::1"}]})


if __name__ == "__main__":
    unittest.main()

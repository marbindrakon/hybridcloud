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
        # as site_probe: an administrator's own token, a domain manager's scoped to the project domain
        access = conn.session.auth.get_access(conn.session)
        admin = "admin" in access.role_names
        ks = S.Keystone(acct)
        out = S.sign_in_reads(ks, S.identity_token(ks, conn.session.get_token(), admin, "tenants", notes),
                              access.user_id,
                              {"identity_provider": decl["identityProvider"], "protocol": decl["protocol"],
                               "group_domain": decl.get("groupDomain") or "tenants", "start": decl.get("start"),
                               "dashboard_url": dashboard, "project_domain": "tenants", "admin": admin}, notes)
        return out, notes

    def run_si(self, decl, reads=None, notes=(), kcc=None, dashboard=DASH, previous=None):
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
        if previous is not None:
            extra["cloudinfrastructure_cr_lookup"] = {"resources": [{"status": previous}]}
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

    def test_a_missing_provider_and_a_long_issuer(self):
        """An identity service can accept at most 64 characters as an identity provider's remote ID (Keystone
        2026.2): a provider that is not on the site, with a tenant issuer longer than that, gets the hint."""
        long_issuer = "https://sso-directory.apps.platform-services.example.org/realms/tenants"   # 71
        short_issuer = "https://sso.example.org/realms/tenants"
        self.assertGreater(len(long_issuer), 64)
        found = {"checked": True, "found": True, "groupsMapper": True, "note": ""}
        reads, _ = self.reads(DECL)   # no provider on the site
        _, r = self.run_si(DECL, reads, kcc=dict(found, issuer=long_issuer))
        self.assertEqual(self.cond(r), ("False", "IdentityProviderNotFound",
                                        "Identity provider platform-tenants does not exist on the site. The tenant "
                                        f"issuer of the platform, {long_issuer}, is 71 characters long: an identity "
                                        "service that accepts at most 64 characters as the remote ID of an identity "
                                        "provider (Keystone 2026.2 does) cannot register it."))
        for kcc in (dict(found, issuer=short_issuer), dict(found, issuer=""), found):
            _, r = self.run_si(DECL, reads, kcc=kcc)
            self.assertEqual(self.cond(r), ("False", "IdentityProviderNotFound",
                                            "Identity provider platform-tenants does not exist on the site."))
        self.declare()   # the provider exists: no hint, whatever the issuer's length
        reads, notes = self.reads(DECL)
        _, r = self.run_si(DECL, reads, notes, kcc=dict(found, issuer=long_issuer))
        self.assertEqual(r["cond"]["reason"], "Confirmed")
        self.assertNotIn("characters", r["cond"]["message"])

    def test_a_mapping_warning_does_not_switch_sign_in_off(self):
        self.declare(rules=[{"local": [{"user": {"name": "{0}"}}, {"group": {"name": "{1}", "domain": {"name": "x"}}}],
                             "remote": [{"type": "OIDC-preferred_username"}, {"type": "OIDC-groups"}]}])
        reads, _ = self.reads(DECL)
        _, r = self.run_si(DECL, reads)
        self.assertEqual(self.cond(r), ("True", "MappingWarning", "The site's mapping does not look groups up in "
                                                                  "tenants, has no stable user id."))
        self.assertTrue(r["caps"]["consoleSignIn"])

    def test_a_site_that_does_not_answer_keeps_the_last_result(self):
        """R6: a 503 on the identity provider (or the group domain) is not "does not exist": the last condition, the
        published sign-in and the capabilities stand, a note says why."""
        self.declare()
        reads, notes = self.reads(DECL)
        _, first = self.run_si(DECL, reads, notes)
        previous = {"conditions": [first["cond"]], "site": first["site"], "capabilities": first["caps"]}
        for path in ("/v3/OS-FEDERATION/identity_providers/platform-tenants", "/v3/domains"):
            self.site.ks.fail_next.append(("GET", path, 503, "Service Unavailable"))
            reads, notes = self.reads(DECL)
            self.assertTrue(reads["unreadable"])
            _, r = self.run_si(DECL, reads, notes, previous=previous)
            self.assertEqual(self.cond(r), self.cond(first))
            self.assertEqual(r["site"]["signIn"], first["site"]["signIn"])
            self.assertEqual((r["caps"]["consoleSignIn"], r["caps"]["apiCredentials"]), (True, True))
            self.assertTrue(any("could not be read this time" in n for n in r["notes"]), r["notes"])
        # a first probe that cannot read the site does not say the provider is missing
        self.site.ks.fail_next.append(("GET", "/v3/OS-FEDERATION/identity_providers/platform-tenants", 503, "x"))
        reads, notes = self.reads(DECL)
        _, r = self.run_si(DECL, reads, notes)
        self.assertEqual(r["cond"]["reason"], "DeclaredNotConfirmed")

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


@unittest.skipUnless(HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class DirectoryCheck(unittest.TestCase):
    """The real common/tasks/keycloak_client_check.yml against a sign-in directory over HTTPS (fake_keycloak.py) and
    the realm automation credentials in a Vault over HTTP: it finds the client and its groups mapper and gives the
    tenant issuer (the directory's address from the labelled Route, /realms/<realm>) that the site probe's hint
    reads; when the address is not known the note says why and the issuer is empty."""

    def setUp(self):
        import fake_keycloak
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        self.kc = fake_keycloak.Keycloak().start()
        self.addCleanup(self.kc.stop)
        self.kc.add_client("openstack-site-a")
        self.site.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"] = self.kc.vault_entry()
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)

    def check(self, routes):
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/keycloak_client_check.yml",
                  "vars": {"kcc_realm": "sovereign-tenants", "kcc_client_id": "openstack-site-a", "kcc_rbac_config": ""}}]
        rc, trace, out = H.run("cloudinfrastructure_provision", "openstack_signin.yml",
                               {"services_api_host": "https://services.example.org",
                                "services_bearer_token": "services-token-not-in-data"},
                               k8s={"Route/services-rhbk/": routes}, tasks=tasks, result="kcc_result")
        self.assertEqual(rc, 0, out)
        return trace[-1]["result"]

    def test_the_issuer_from_the_labelled_route(self):
        res = self.check([{"metadata": {"name": "kc"}, "spec": {"host": self.kc.host}}])
        self.assertEqual((res["checked"], res["found"], res["note"]), (True, True, ""))
        self.assertEqual(res["issuer"], f"https://{self.kc.host}/realms/sovereign-tenants")

    def test_two_labelled_routes(self):
        res = self.check([{"metadata": {"name": "kc-a"}, "spec": {"host": self.kc.host}},
                          {"metadata": {"name": "kc-b"}, "spec": {"host": "kc-b.example.org"}}])
        self.assertEqual((res["checked"], res["issuer"]), (False, ""))
        note = " ".join(res["note"].split())
        self.assertTrue(note.startswith("the sign-in directory could not be asked about client openstack-site-a: 2 "
                                        "Routes in namespace services-rhbk"), note)
        self.assertIn(f"kc-a for {self.kc.host}, kc-b for kc-b.example.org", note)


if __name__ == "__main__":
    unittest.main()

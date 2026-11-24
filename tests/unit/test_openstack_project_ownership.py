"""The refuse-or-adopt decision for the Keystone projects the platform makes (eda/common/files/shc_openstack.py:
provision for an environment's project, platform-project for the site's networks, the site probe's DNS project),
against the HTTP Keystone of fake_keystone.py. Every state is enumerated: what exists on the site (nothing, a project
of the wanted name without the platform's tag, one tagged for this owner, one tagged for another owner) x what the
status recorded (no id, the id of that project, the id of a project that is gone). A project the platform did not make
for this owner is refused (NameTakenOnSite): never given a role, never changed, never deleted. Names are examples
(tenant acme-corp, environment prod)."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
try:
    import openstack  # noqa: F401
    import requests  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover - the job images have both
    HAVE_SDK = False

import fake_keystone as F  # noqa: E402
import shc_names  # noqa: E402

TMP = tempfile.mkdtemp(prefix="shc-proj-test-")
_EMPTY = os.path.join(TMP, "no-clouds.yaml")
with open(_EMPTY, "w") as _f:
    _f.write("clouds: {}\n")
os.environ["OS_CLIENT_CONFIG_FILE"] = _EMPTY

if HAVE_SDK:
    import shc_openstack as S  # noqa: E402

OWNER = "CloudOSO/entity-acme-corp/prod"
OTHER = "CloudOSO/entity-acme/corp-prod"
NAME = shc_names.name("openstack-project", ["acme-corp", "prod"])["name"]
DESC = "Environment prod of tenant acme-corp. Managed by the sovereign cloud platform; do not change."

# what exists x what is recorded -> what the job does
ON_SITE = ("nothing", "unmarked", "ours", "another")
RECORDED = ("none", "that", "gone")


def expected(site, recorded):
    if site == "nothing":
        return "created"
    if site == "another":
        return "refused"
    if recorded == "that":
        return "recorded"
    if site == "ours":
        return "marked"
    return "refused"                       # unmarked, and not recorded (or recorded but gone)


@unittest.skipUnless(HAVE_SDK, "openstacksdk / requests not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.ks = F.Keystone(granularity=0.1).start()
        self.addCleanup(self.ks.stop)
        self.tenants = self.ks.add_domain("tenants")
        self.uid, pw, _ = self.ks.site_account()
        self.ks.grant("user", self.uid, "domain", self.tenants, "admin")
        self.clouds = self.ks.clouds_yaml(os.path.join(TMP, f"acct-{id(self)}.yaml"), "site-admin", pw,
                                          project="site-admin-project")

    def tearDown(self):
        self.assertEqual(self.ks.faults, [], "the fake Keystone itself failed")

    def arrange(self, site, recorded, name, owner, other):
        pid = None
        if site != "nothing":
            tags = {"unmarked": [], "ours": [S.owner_tag(owner)], "another": [S.owner_tag(other)]}[site]
            pid = self.ks.add_project(name, self.tenants, tags=tags)
            self.ks.projects[pid]["description"] = "someone's own text"
        rec = {"none": "", "that": pid or "", "gone": "0" * 32}[recorded]
        return pid, rec

    def writes(self):
        return [c for c in self.ks.log if c[0] in ("POST", "PUT", "PATCH", "DELETE") and c[1] != "/v3/auth/tokens"]

    def roles_on(self, pid):
        return {a for a in self.ks.assignments if a[2] == "project" and a[3] == pid}


class EnvironmentProject(Base):
    def run_case(self, site, recorded):
        pid, rec = self.arrange(site, recorded, NAME, OWNER, OTHER)
        before = dict(self.ks.projects[pid]) if pid else None
        req = {"clouds_yaml": self.clouds, "domain": "tenants", "project": NAME, "project_id": rec, "owner": OWNER,
               "description": DESC}
        want = expected(site, recorded if not (recorded == "that" and site == "nothing") else "none")
        if want == "refused":
            with self.assertRaises(S.Refusal) as cm:
                S.provision(req)
            self.assertEqual(cm.exception.reason, "NameTakenOnSite")
            self.assertIn(NAME, cm.exception.message)
            self.assertNotIn(pid, cm.exception.message)              # never a site id
            self.assertEqual(self.writes(), [])                      # nothing granted, changed or deleted
            self.assertEqual(self.ks.projects[pid], before)
            self.assertEqual(self.roles_on(pid), set())
            return
        out = S.provision(req)
        proj = self.ks.projects[out["project"]["id"]]
        self.assertEqual(out["project"]["found"], want)
        self.assertEqual(out["project"]["created"], want == "created")
        if pid:
            self.assertEqual(out["project"]["id"], pid)
        self.assertIn(S.owner_tag(OWNER), proj["tags"])
        self.assertEqual(proj["description"], DESC)                  # refreshed on each run
        self.assertTrue(self.roles_on(proj["id"]))                   # the site account's member role
        # a second run changes nothing
        n = len(self.writes())
        S.provision(dict(req, project_id=proj["id"]))
        self.assertEqual([c for c in self.writes()[n:] if c[0] != "PUT"], [])

    def test_every_state(self):
        for site in ON_SITE:
            for recorded in RECORDED:
                with self.subTest(site=site, recorded=recorded):
                    self.tearDown()
                    self.ks.stop()
                    self.setUp()
                    self.run_case(site, recorded)

    def test_the_old_project_keeps_its_name(self):
        # an environment keeps the project it was given: found by the recorded id, whatever its name now
        pid = self.ks.add_project("acme-corp_prod_old", self.tenants)
        out = S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": NAME, "project_id": pid,
                           "owner": OWNER, "description": DESC})
        self.assertEqual((out["project"]["id"], out["project"]["name"]), (pid, "acme-corp_prod_old"))

    def test_two_tenants_get_two_projects(self):
        # acme-corp + prod and acme + corp-prod: distinct names, each refused the other's project
        a = shc_names.name("openstack-project", ["acme-corp", "prod"])["name"]
        b = shc_names.name("openstack-project", ["acme", "corp-prod"])["name"]
        self.assertNotEqual(a, b)
        S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": a, "owner": OWNER})
        S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": b, "owner": OTHER})
        self.assertEqual(sorted(p["name"] for p in self.ks.projects.values() if p["domain_id"] == self.tenants),
                         sorted([a, b]))
        with self.assertRaises(S.Refusal):
            S.provision({"clouds_yaml": self.clouds, "domain": "tenants", "project": a, "owner": OTHER})

    def test_teardown_never_deletes_an_unmarked_project_by_name(self):
        pid = self.ks.add_project(NAME, self.tenants)
        req = {"clouds_yaml": self.clouds, "domain": "tenants", "project": NAME, "owner": OWNER}
        self.assertEqual(S.check_empty(req)["project_exists"], False)
        self.assertEqual(S.teardown(req)["project"], None)
        self.assertIn(pid, self.ks.projects)
        # ours by tag: the name fallback finds it (teardown then deletes it; the fake has no network service)
        self.ks.projects[pid]["tags"] = [S.owner_tag(OWNER)]
        conn_d = S.Account(req).domain("tenants")
        self.assertEqual(S.resolve_project(conn_d, self.tenants, req).id, pid)
        # another environment's tag: not found
        self.ks.projects[pid]["tags"] = [S.owner_tag(OTHER)]
        self.assertIsNone(S.resolve_project(conn_d, self.tenants, req))

    def test_the_tag_obeys_keystone(self):
        tag = S.owner_tag(OWNER)
        self.assertNotIn("/", tag)
        self.assertEqual(tag, "hybridsovereign.redhat:CloudOSO:entity-acme-corp:prod")
        with self.assertRaises(ValueError):
            S.owner_tag("a,b")
        with self.assertRaises(ValueError):
            S.owner_tag("")


class PlatformProject(Base):
    def req(self, rec="", state="present"):
        return {"clouds_yaml": self.clouds, "domain": "tenants", "name": "hybrid-fabric", "project_id": rec,
                "description": "Networks of the platform", "state": state}

    def test_every_state(self):
        mine, other = S.PLATFORM_PROJECT_OWNER, "someone-else"
        for site in ON_SITE:
            for recorded in RECORDED:
                with self.subTest(site=site, recorded=recorded):
                    self.tearDown()
                    self.ks.stop()
                    self.setUp()
                    pid, rec = self.arrange(site, recorded, "hybrid-fabric", mine, other)
                    want = expected(site, recorded if pid else "none")
                    if want == "refused":
                        with self.assertRaises(S.Refusal) as cm:
                            S.platform_project(self.req(rec))
                        self.assertEqual(cm.exception.reason, "NameTakenOnSite")
                        self.assertEqual(self.writes(), [])
                        # and the removal leaves it alone
                        self.assertFalse(S.platform_project(self.req(rec, "absent"))["deleted"])
                        self.assertIn(pid, self.ks.projects)
                        continue
                    out = S.platform_project(self.req(rec))
                    self.assertEqual(out["created"], want == "created")
                    self.assertIn(S.owner_tag(mine), self.ks.projects[out["id"]]["tags"])


class DnsProject(Base):
    def probe(self, rec, create=True):
        acct = S.Account({"clouds_yaml": self.clouds})
        conn = acct.own_scope()
        access = conn.session.auth.get_access(conn.session)
        return S.probe_dns_project(acct, conn, access.user_id, True,
                                   {"domain": "tenants", "name": "tenant-dns", "create": create, "project_id": rec,
                                    "description": "Tenant DNS"}, [])

    def test_every_state_when_the_platform_makes_it(self):
        for site in ON_SITE:
            for recorded in RECORDED:
                with self.subTest(site=site, recorded=recorded):
                    self.tearDown()
                    self.ks.stop()
                    self.setUp()
                    pid, rec = self.arrange(site, recorded, "tenant-dns", S.DNS_PROJECT_OWNER, "someone-else")
                    want = expected(site, recorded if pid else "none")
                    if want == "refused":
                        with self.assertRaises(S.Refusal):
                            self.probe(rec)
                        self.assertEqual(self.writes(), [])
                        continue
                    out = self.probe(rec)
                    self.assertTrue(out["found"])
                    self.assertEqual(out["created"], want == "created")
                    self.assertIn(S.owner_tag(S.DNS_PROJECT_OWNER), self.ks.projects[out["id"]]["tags"])

    def test_the_operators_own_project_is_used_as_it_is(self):
        pid = self.ks.add_project("tenant-dns", self.tenants)
        out = self.probe("", create=False)
        self.assertEqual(out["id"], pid)
        self.assertEqual(self.ks.projects[pid]["tags"], [])
        self.assertEqual([c for c in self.writes() if c[0] == "PATCH"], [])


if __name__ == "__main__":
    unittest.main()

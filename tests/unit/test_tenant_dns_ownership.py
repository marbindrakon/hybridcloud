"""What marks a DNS object as the platform's, and what the jobs do with one that is not: eda/common/files/shc_dns.py
ensure-zone / delete-zone / ensure-delegation / delete-delegation and shc_openstack.py dns-upsert / dns-delete, against
a fake Designate connection. Designate keeps no tags on zones or recordsets (designate/objects/zone.py, recordset.py:
the description, at most 160 characters, is the one free field), so the mark is the description: an environment's
zone and its delegation carry the description the job writes for that environment, a cluster's records carry
shc:<owner>. Every state of the zone decision is enumerated (what exists on the site x what status recorded); an
object of the name without the mark is refused (NameTakenOnSite) with nothing written, and never deleted. Names are
examples (example.test)."""
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
try:
    import openstack  # noqa: F401
    from openstack.exceptions import HttpException, NotFoundException
except ImportError:  # stub the SDK so the modules import; the actions run against fake connections
    class HttpException(Exception):
        pass

    class NotFoundException(HttpException):
        pass
    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(ForbiddenException=Exception, NotFoundException=NotFoundException,
                                            HttpException=HttpException)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)
import shc_dns as D  # noqa: E402
import shc_openstack as S  # noqa: E402

NAME = "prod.acme-corp.example.test."
DESC = "Environment zone of CloudOSO entity-acme-corp/prod (shared with its project)"
DELEG = "Delegation of CloudOSO entity-acme-corp/prod (site s1)"
OWNER = "PlatformOpenshift/entity-acme-corp/web"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeDNS:
    """Zones of the project (zones held by another project are listed in `elsewhere`: a create of that name is
    refused as Designate does), recordsets, shares. Every write is appended to conn.writes."""

    def __init__(self, conn):
        self.conn, self.zs, self.elsewhere, self.sets, self.shares, self.n = conn, [], set(), [], [], 0

    def _id(self, k):
        self.n += 1
        return f"{k}-{self.n}"

    def zones(self, name=None):
        return [z for z in self.zs if name is None or z.name == name]

    def get_zone(self, zid):
        z = next((z for z in self.zs if z.id == zid or z is zid), None)
        if z is None:
            raise NotFoundException("Could not find Zone")
        return z

    def create_zone(self, **kw):
        if kw["name"] in self.elsewhere or any(z.name == kw["name"] for z in self.zs):
            raise HttpException("409 Duplicate Zone")
        # Designate gives a zone and a recordset the project of the request that made it
        z = Obj(id=self._id("zone"), status="ACTIVE", attributes={}, project_id=self.conn.scope, **kw)
        self.zs.append(z)
        self.conn.writes.append(("create_zone", kw["name"]))
        return z

    def update_zone(self, z, **kw):
        z.__dict__.update(kw)
        self.conn.writes.append(("update_zone", z.name))
        return z

    def delete_zone(self, z, ignore_missing=True, delete_shares=False):
        self.zs.remove(z)
        self.conn.writes.append(("delete_zone", z.name))

    def zone_shares(self, z):
        return [s for s in self.shares if s.zone_id == z.id]

    def create_zone_share(self, z, target_project_id):
        s = Obj(id=self._id("share"), zone_id=z.id, target_project_id=target_project_id)
        self.shares.append(s)
        self.conn.writes.append(("share", target_project_id))
        return s

    def recordsets(self, zone, name=None, type=None):
        zid = zone.id if hasattr(zone, "id") else zone
        return [r for r in self.sets if r.zone_id == zid and (name is None or r.name == name)
                and (type is None or r.type == type)]

    def create_recordset(self, zone, **kw):
        rs = Obj(id=self._id("rs"), zone_id=zone.id, project_id=self.conn.scope, **kw)
        self.sets.append(rs)
        self.conn.writes.append(("create_recordset", kw["name"], kw["type"]))
        return rs

    def update_recordset(self, rs, **kw):
        rs.__dict__.update(kw)
        self.conn.writes.append(("update_recordset", rs.name, rs.type))
        return rs

    def delete_recordset(self, rs, zone, ignore_missing=True):
        self.sets.remove(rs)
        self.conn.writes.append(("delete_recordset", rs.name, rs.type))


class FakeConn:
    def __init__(self):
        self.writes = []
        self.scope = ""                  # the project the account is scoped to (Account.project)
        self.dns = FakeDNS(self)

    def at(self, project_id):
        self.scope = project_id
        return self


def call(mod, action, conn, **req):
    acct = mock.Mock()
    acct.project.side_effect = conn.at
    with mock.patch.object(mod, "Account", return_value=acct):
        fn = (D.ACTIONS if mod is D else S.ACTIONS)[action]
        return fn(dict(req, action=action))


def ns_set(conn, zone, name, desc, records=("ns1.example.test.",), project="p"):
    conn.dns.sets.append(Obj(id=f"ns-{len(conn.dns.sets)}", zone_id=zone.id, name=name, type="NS",
                             records=list(records), description=desc, ttl=3600, project_id=project))


class EnvironmentZone(unittest.TestCase):
    ON_SITE = ("nothing", "ours", "unmarked", "elsewhere")
    RECORDED = ("none", "that", "gone")

    @staticmethod
    def expected(site, recorded):
        if site == "nothing":
            return "created"
        if site == "elsewhere":
            return "refused"               # another project holds the name: never ours
        if recorded == "that":
            return "recorded"
        return "adopted" if site == "ours" else "refused"

    def test_every_state(self):
        for site in self.ON_SITE:
            for recorded in self.RECORDED:
                with self.subTest(site=site, recorded=recorded):
                    conn, zid = FakeConn(), ""
                    if site in ("ours", "unmarked"):
                        z = Obj(id="zone-old", name=NAME, status="ACTIVE", attributes={}, project_id="proj-dns",
                                description=DESC if site == "ours" else "someone's zone")
                        conn.dns.zs.append(z)
                    if site == "elsewhere":
                        conn.dns.elsewhere.add(NAME)
                    zid = {"none": "", "that": "zone-old" if site in ("ours", "unmarked") else "", "gone": "zone-gone"}[recorded]
                    want = self.expected(site, recorded if zid != "" or recorded != "that" else "none")
                    req = {"owner_project_id": "proj-dns", "name": NAME, "zone_id": zid, "email": "h@example.test",
                           "description": DESC, "share_with": "proj-env"}
                    if want == "refused":
                        with self.assertRaises(S.Refusal) as cm:
                            call(D, "ensure-zone", conn, **req)
                        self.assertEqual(cm.exception.reason, "NameTakenOnSite")
                        self.assertIn(NAME, cm.exception.message)
                        self.assertEqual(conn.writes, [])          # not reused, not shared
                        continue
                    out = call(D, "ensure-zone", conn, **req)
                    self.assertEqual(out["created"], want == "created")
                    z = conn.dns.get_zone(out["zone_id"])
                    self.assertEqual(z.description, DESC)          # the mark, refreshed
                    self.assertEqual([s.target_project_id for s in conn.dns.shares], ["proj-env"])

    def test_a_recorded_zone_keeps_working_after_a_text_change(self):
        conn = FakeConn()
        conn.dns.zs.append(Obj(id="zone-old", name=NAME, status="ACTIVE", attributes={}, description="older text",
                               project_id="p"))
        call(D, "ensure-zone", conn, owner_project_id="p", name=NAME, zone_id="zone-old", email="h@x", description=DESC)
        self.assertEqual(conn.dns.zs[0].description, DESC)

    def test_delete_by_name_needs_the_mark(self):
        conn = FakeConn()
        conn.dns.zs.append(Obj(id="zone-old", name=NAME, status="ACTIVE", attributes={}, description="someone's",
                               project_id="p"))
        out = call(D, "delete-zone", conn, owner_project_id="p", name=NAME, description=DESC)
        self.assertFalse(out["deleted"])
        self.assertEqual(conn.writes, [])
        conn.dns.zs[0].description = DESC
        self.assertTrue(call(D, "delete-zone", conn, owner_project_id="p", name=NAME, description=DESC)["deleted"])


class Delegation(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        self.glob = Obj(id="zone-global", name="example.test.", status="ACTIVE", attributes={}, description="")
        self.conn.dns.zs.append(self.glob)

    def ensure(self, desc=DELEG, ns=("ns9.example.test",)):
        return call(D, "ensure-delegation", self.conn, project_id="p", zone_id="zone-global", name=NAME,
                    nameservers=list(ns), description=desc)

    def test_every_state(self):
        # nothing -> created with the mark; ours -> updated; another's -> refused, untouched
        self.assertEqual(self.ensure()["state"], "created")
        self.assertEqual(self.conn.dns.sets[0].description, DELEG)
        self.assertEqual(self.ensure(ns=("ns8.example.test",))["state"], "updated")
        self.conn.dns.sets[0].description = "the tenant's own"
        n = len(self.conn.writes)
        with self.assertRaises(S.Refusal) as cm:
            self.ensure()
        self.assertEqual(cm.exception.reason, "NameTakenOnSite")
        self.assertEqual(self.conn.writes[n:], [])

    def test_a_delegation_without_a_description_is_refused(self):
        with self.assertRaises(ValueError):
            self.ensure(desc="")

    def test_delete_only_ours(self):
        ns_set(self.conn, self.glob, NAME, "the tenant's own")
        out = call(D, "delete-delegation", self.conn, project_id="p", zone_id="zone-global", name=NAME,
                   description=DELEG)
        self.assertEqual((out["deleted"], out["kept"]), (False, "not made by the platform for this environment"))
        out = call(D, "delete-delegation", self.conn, project_id="p", zone_id="zone-global", name=NAME)
        self.assertEqual(out["kept"], "no description to check it by")
        self.assertEqual(self.conn.writes, [])
        self.conn.dns.sets[0].description = DELEG
        self.assertTrue(call(D, "delete-delegation", self.conn, project_id="p", zone_id="zone-global", name=NAME,
                             description=DELEG)["deleted"])


class ClusterRecords(unittest.TestCase):
    API = "api.web." + NAME

    def setUp(self):
        self.conn = FakeConn()
        self.z = Obj(id="zone-env", name=NAME, status="ACTIVE", attributes={}, description=DESC)
        self.conn.dns.zs.append(self.z)

    def upsert(self, ip="192.0.2.10", owner=OWNER):
        return call(S, "dns-upsert", self.conn, dns_project_id="p", dns_zone_id="zone-env", owner=owner,
                    records=[{"name": self.API, "records": [ip]}, {"name": "*.apps.web." + NAME, "records": [ip]}])

    def test_every_state(self):
        # nothing -> created with the mark; ours -> updated; the tenant's own -> refused before any write
        self.assertEqual([r["state"] for r in self.upsert()["records"]], ["created", "created"])
        self.assertTrue(all(rs.description == "shc:" + OWNER for rs in self.conn.dns.sets))
        self.assertEqual([r["state"] for r in self.upsert("192.0.2.11")["records"]], ["updated", "updated"])
        self.conn.dns.sets[1].description = ""              # the tenant made *.apps.web itself
        n = len(self.conn.writes)
        with self.assertRaises(S.Refusal) as cm:
            self.upsert("192.0.2.12")
        self.assertEqual(cm.exception.reason, "NameTakenOnSite")
        self.assertEqual(self.conn.writes[n:], [])          # not even the api record
        # another cluster's mark is not ours either
        with self.assertRaises(S.Refusal):
            self.upsert(owner="PlatformOpenshift/entity-acme/corp-web")

    def test_delete_only_ours(self):
        self.upsert()
        self.conn.dns.sets[1].description = "mine, says the tenant"
        out = call(S, "dns-delete", self.conn, dns_project_id="p", dns_zone_id="zone-env", owner=OWNER,
                   records=[{"name": self.API}, {"name": "*.apps.web." + NAME}, {"name": "gone." + NAME}])
        self.assertEqual(out["deleted"], [self.API + " A"])
        self.assertEqual(out["kept"], ["*.apps.web." + NAME + " A"])
        self.assertEqual(out["missing"], ["gone." + NAME + " A"])
        self.assertEqual(len(self.conn.dns.sets), 1)

    def test_an_owner_is_required(self):
        with self.assertRaises(ValueError):
            self.upsert(owner="")


class MarkOfAnotherProject(unittest.TestCase):
    """The platform's description written by another project (a project the zone is shared with may create
    recordsets in it with any description: designate/common/policies/recordset.py) is not the platform's mark: the
    project that made the object (Designate sets it from the request, never changes it) must be the platform's."""

    def test_a_zone_of_another_project_with_our_description_is_refused(self):
        conn = FakeConn()
        conn.dns.zs.append(Obj(id="zone-t", name=NAME, status="ACTIVE", attributes={}, description=DESC,
                               project_id="proj-tenant"))
        with self.assertRaises(S.Refusal):
            call(D, "ensure-zone", conn, owner_project_id="proj-dns", name=NAME, zone_id="", email="h@x",
                 description=DESC)
        self.assertEqual(conn.writes, [])
        out = call(D, "delete-zone", conn, owner_project_id="proj-dns", name=NAME, description=DESC)
        self.assertFalse(out["deleted"])

    def test_a_delegation_a_tenant_wrote_with_our_description(self):
        conn = FakeConn()
        glob = Obj(id="zone-global", name="example.test.", status="ACTIVE", attributes={}, description="")
        conn.dns.zs.append(glob)
        ns_set(conn, glob, NAME, DELEG, project="proj-tenant")
        with self.assertRaises(S.Refusal):
            call(D, "ensure-delegation", conn, project_id="p", zone_id="zone-global", name=NAME,
                 nameservers=["ns9.example.test"], description=DELEG)
        out = call(D, "delete-delegation", conn, project_id="p", zone_id="zone-global", name=NAME, description=DELEG)
        self.assertFalse(out["deleted"])
        self.assertEqual(conn.writes, [])

    def test_a_cluster_record_a_tenant_wrote_with_our_mark(self):
        conn = FakeConn()
        z = Obj(id="zone-env", name=NAME, status="ACTIVE", attributes={}, description=DESC, project_id="p")
        conn.dns.zs.append(z)
        api = "api.web." + NAME
        conn.dns.sets.append(Obj(id="rs-t", zone_id=z.id, name=api, type="A", records=["192.0.2.66"], ttl=300,
                                 description="shc:" + OWNER, project_id="proj-tenant"))
        with self.assertRaises(S.Refusal):
            call(S, "dns-upsert", conn, dns_project_id="p", dns_zone_id="zone-env", owner=OWNER,
                 records=[{"name": api, "records": ["192.0.2.10"]}])
        out = call(S, "dns-delete", conn, dns_project_id="p", dns_zone_id="zone-env", owner=OWNER,
                   records=[{"name": api}])
        self.assertEqual(out["kept"], [api + " A"])
        self.assertEqual(conn.writes, [])


if __name__ == "__main__":
    unittest.main()

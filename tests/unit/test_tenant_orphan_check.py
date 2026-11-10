"""Unit tests of the tenant orphan check (charts/charts/shc-cluster-base/files/tenant_orphan_check.py) against fake
connections: a clean run, findings, a project the account holds no role on (Keystone 401 for the project scope: the
run completes the other checks, names the cloud, the account reference and the Keystone, and exits 2), a refused
domain token, and the termination message. Identifiers are made up."""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files")
sys.path.insert(0, FILES)

try:
    import openstack  # noqa: F401
    from keystoneauth1 import exceptions as ksa

    def unauthorized():
        return ksa.http.Unauthorized(http_status=401, message="The request you have made requires authentication.")
except ImportError:  # stub the SDK so the module imports
    class _SDKException(Exception):
        pass

    class _Unauthorized(_SDKException):
        http_status = 401

    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(SDKException=_SDKException)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)

    def unauthorized():
        return _Unauthorized("The request you have made requires authentication. (HTTP 401)")

import tenant_orphan_check as T  # noqa: E402

NS = types.SimpleNamespace
CFG = {"domain": "tenants", "cloudInfrastructure": "cloud-a", "ignoreProjects": ["dns"],
       "accountRef": "accounts/tenant-admin (ClusterSecretStore store)"}
CLOUD = {"auth": {"auth_url": "https://keystone.example.test:5000/v3", "username": "u", "password": "p"}}


def oso(name, pid, cloud="cloud-a", **st):
    return {"metadata": {"namespace": "entity-a", "name": name},
            "status": {"cloudInfrastructure": cloud, "projectId": pid, **st}}


class Conn:
    def __init__(self, projects=None, ips=None, fail=None):
        self._projects, self._ips, self._fail = projects or {}, ips or [], fail
        self.session = NS(auth=NS(get_access=lambda s: NS(domain_id="dom-1")))
        self.identity = NS(projects=self._list_projects)
        self.network = NS(ips=self._list_ips)

    def _list_projects(self, domain_id):
        if self._fail:
            raise self._fail
        return [NS(id=k, name=v) for k, v in self._projects.items()]

    def _list_ips(self, project_id):
        if self._fail:
            raise self._fail
        return self._ips


def connector(projects, project_conns=None, domain_fail=None):
    project_conns = project_conns or {}

    def connect(**scope):
        if "domain_name" in scope:
            return Conn(projects=projects, fail=domain_fail)
        return project_conns.get(scope["project_id"], Conn())
    return connect


ZONE = "tenants.example.test."


class ZoneConn:
    def __init__(self, recordsets):
        zone = NS(id="zone-1", name=ZONE)
        self.dns = NS(get_zone=lambda zid: zone, recordsets=lambda z: recordsets)


def rs(name, rtype, *records):
    return NS(name=name, type=rtype, records=list(records))


def gateway(name, names, namespace="sites"):
    return {"metadata": {"namespace": namespace, "name": name},
            "status": {"realization": {"reverseDns": {"dnsConfig": "dns", "names": [
                {"name": n, "address": a, "zone": ZONE} for n, a in names]}}}}


DCFG = dict(CFG, dnsZoneId="zone-1", dnsProjectId="pdns")


class PlatformNames(unittest.TestCase):
    """Check 4: the AAAA names the platform publishes for its gateways' ICMPv6 sources count as recorded."""

    def run_check(self, recordsets, gateways=(), placements=()):
        conns = {"pdns": ZoneConn(recordsets)}
        return T.check(DCFG, T.Account(DCFG, CLOUD), connector({}, conns), [], [],
                       [("CloudGateway", list(gateways)), ("NetworkPlacement", list(placements))])

    def test_recorded_gateway_names_are_not_findings(self):
        gws = [gateway("site-a", [("node-1.site-a.gateways.tenants.example.test.", "2001:db8::1:2"),
                                  ("node-2.site-a.gateways.tenants.example.test.", "2001:db8::2:2")])]
        sets = [rs(ZONE, "SOA", "x"), rs(ZONE, "NS", "ns."),
                rs("node-1.site-a.gateways.tenants.example.test.", "AAAA", "2001:db8::1:2"),
                rs("node-2.site-a.gateways.tenants.example.test.", "AAAA", "2001:DB8::2:2")]
        summary, findings, problems, rc = self.run_check(sets, gws)
        self.assertEqual((findings, problems, rc), ([], [], 0))

    def test_name_of_a_gateway_that_no_longer_exists_is_a_finding(self):
        sets = [rs("node-1.gone.gateways.tenants.example.test.", "AAAA", "2001:db8::1:9")]
        summary, findings, problems, rc = self.run_check(sets, [gateway("site-a", [])])
        self.assertEqual(rc, 1)
        self.assertIn("no PlatformOpenshift, CloudOSO, CloudGateway or NetworkPlacement recorded it", findings[0])

    def test_recorded_name_with_another_address_is_a_finding(self):
        gws = [gateway("site-a", [("node-1.site-a.gateways.tenants.example.test.", "2001:db8::1:2")])]
        sets = [rs("node-1.site-a.gateways.tenants.example.test.", "AAAA", "2001:db8::1:2", "2001:db8::1:7")]
        summary, findings, problems, rc = self.run_check(sets, gws)
        self.assertEqual(rc, 1)
        self.assertIn("CloudGateway sites/site-a recorded this name with 2001:db8::1:2 only", findings[0])

    def test_only_aaaa_counts(self):
        gws = [gateway("site-a", [("node-1.site-a.gateways.tenants.example.test.", "2001:db8::1:2")])]
        sets = [rs("node-1.site-a.gateways.tenants.example.test.", "A", "192.0.2.1")]
        self.assertEqual(self.run_check(sets, gws)[3], 1)

    def test_networkplacement_names_count(self):
        nps = [gateway("net-a", [("gw.net-a.tenants.example.test", "2001:db8:1::1")], namespace="entity-a")]
        sets = [rs("gw.net-a.tenants.example.test.", "AAAA", "2001:db8:1::1")]
        self.assertEqual(self.run_check(sets, placements=nps)[3], 0)


class OrphanCheck(unittest.TestCase):
    def setUp(self):
        self.acct = T.Account(CFG, CLOUD)

    def test_clean(self):
        summary, findings, problems, rc = T.check(
            CFG, self.acct, connector({"p1": "a_one", "pd": "dns"}), [oso("one", "p1")], [])
        self.assertEqual((findings, problems, rc), ([], [], 0))
        self.assertIn("2 projects, 1 CloudOSO", summary)

    def test_findings(self):
        conns = {"p1": Conn(ips=[NS(id="ip-x", floating_ip_address="192.0.2.9", port_id=None)])}
        summary, findings, problems, rc = T.check(
            CFG, self.acct, connector({"p1": "a_one", "p2": "a_two"}, conns),
            [oso("one", "p1"), oso("gone", "p9")], [])
        self.assertEqual(rc, 1)
        self.assertEqual(problems, [])
        self.assertEqual(len(findings), 3)  # gone project, project without CloudOSO, unattached IP

    def test_project_without_role_is_named_and_the_rest_is_checked(self):
        conns = {"p1": Conn(fail=unauthorized())}
        summary, findings, problems, rc = T.check(
            CFG, self.acct, connector({"p1": "a_one", "p2": "a_two"}, conns),
            [oso("one", "p1"), oso("elsewhere", "p7", cloud="cloud-b")], [])
        self.assertEqual(rc, 2)
        self.assertEqual(len(problems), 1)
        p = problems[0]
        for part in ("CloudOSO entity-a/one", "project a_one (p1)", "accounts/tenant-admin (ClusterSecretStore store)",
                     "cloud cloud-a", "Keystone keystone.example.test:5000", "HTTP 401", "no role on the project"):
            self.assertIn(part, p)
        self.assertNotIn("password", p)
        # the project without a CloudOSO is still found
        self.assertEqual(findings, ["project a_two (p2) in domain tenants has no CloudOSO"])
        self.assertIn("1 not checked", summary)
        self.assertIn("(1 on other clouds)", summary)

    def test_refused_domain_token(self):
        summary, findings, problems, rc = T.check(
            CFG, self.acct, connector({}, domain_fail=unauthorized()), [oso("one", "p1")], [])
        self.assertEqual(rc, 2)
        self.assertIn("domain tenants", problems[0])
        self.assertIn("expired or rotated", problems[0])
        self.assertIn("not checked", summary)

    def test_account_without_reference(self):
        self.assertIn("(account reference not configured)", str(T.Account({}, {"auth": {}})))

    def test_termination_message(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "termination-log")
            with contextlib.redirect_stdout(io.StringIO()):
                T.report("summary", [], [], path)
                self.assertFalse(os.path.exists(path))  # a clean run leaves no message
                T.report("summary", ["finding"], ["problem"], path)
            with open(path) as f:
                self.assertEqual(f.read().splitlines(), ["summary", "  not checked: problem", "  finding"])


if __name__ == "__main__":
    unittest.main()

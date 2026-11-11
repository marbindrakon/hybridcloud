"""cluster-fips of eda/common/files/shc_openstack.py against a fake Neutron: a cluster's API and ingress floating IPs
found by description first (a re-run after a failure at any step creates nothing twice), else created on the named
network; one on another network refused; duplicates of a description released; the release by recorded ids and by
description, a missing id counted as released, another project's floating IP never touched. Also provision without
the environment's floating IP pair. Addresses are documentation space only."""
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
try:
    import openstack  # noqa: F401
except ImportError:  # stub the SDK so the module imports
    class _NotFound(Exception):
        pass
    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(ForbiddenException=PermissionError, NotFoundException=_NotFound)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)
import shc_openstack as S  # noqa: E402

PROJECT, NET, OTHER_NET = "proj-env", "net-placed", "net-other"
DESCS = {"api": "shc-oso-acme-c1-api", "ingress": "shc-oso-acme-c1-ingress"}


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeNetwork:
    def __init__(self):
        self.fips, self.calls, self.n = [], [], 0
        self.fail_create_on = None

    def ips(self, **f):
        return [i for i in self.fips if all(getattr(i, k) == v for k, v in f.items())]

    def get_ip(self, fid):
        for i in self.fips:
            if i.id == fid:
                return i
        raise S.openstack.exceptions.NotFoundException(f"floating IP {fid} not found")

    def create_ip(self, **kw):
        if self.fail_create_on == kw.get("description"):
            raise RuntimeError("quota exceeded")
        self.n += 1
        ip = Obj(id=f"fip-{self.n}", floating_ip_address=f"192.0.2.{10 + self.n}", project_id=PROJECT, port_id=None,
                 floating_network_id=kw["floating_network_id"], description=kw.get("description", ""))
        self.fips.append(ip)
        self.calls.append(("create_ip", dict(kw)))
        return ip

    def delete_ip(self, fid, ignore_missing=True):
        self.fips = [i for i in self.fips if i.id != fid]
        self.calls.append(("delete_ip", fid))


class FakeConn:
    def __init__(self):
        self.network = FakeNetwork()


def call(conn, **req):
    acct = mock.Mock()
    acct.project.return_value = conn
    base = {"action": "cluster-fips", "clouds_yaml": "/dev/null", "project_id": PROJECT}
    with mock.patch.object(S, "Account", return_value=acct):
        out = S.ACTIONS["cluster-fips"]({**base, **req})
    acct.project.assert_called_with(PROJECT)
    return out


def present(conn, **over):
    return call(conn, **{"state": "present", "network_id": NET, "descriptions": DESCS, **over})


class Present(unittest.TestCase):
    def test_creates_both_on_the_network(self):
        conn = FakeConn()
        out = present(conn)
        self.assertEqual(out["network"], NET)
        self.assertEqual(out["api"], {"address": "192.0.2.11", "id": "fip-1", "created": True})
        self.assertEqual(out["ingress"], {"address": "192.0.2.12", "id": "fip-2", "created": True})
        self.assertEqual(conn.network.calls, [("create_ip", {"floating_network_id": NET, "description": DESCS["api"]}),
                                              ("create_ip", {"floating_network_id": NET, "description": DESCS["ingress"]})])
        self.assertEqual(out["released"], [])

    def test_rerun_finds_them_by_description(self):
        conn = FakeConn()
        first = present(conn)
        n = len(conn.network.calls)
        again = present(conn)
        self.assertEqual(conn.network.calls[n:], [])
        self.assertEqual((again["api"]["id"], again["ingress"]["id"]), (first["api"]["id"], first["ingress"]["id"]))
        self.assertFalse(again["api"]["created"] or again["ingress"]["created"])

    def test_failure_between_the_two_converges(self):
        conn = FakeConn()
        conn.network.fail_create_on = DESCS["ingress"]
        with self.assertRaises(RuntimeError):
            present(conn)
        conn.network.fail_create_on = None
        out = present(conn)
        self.assertFalse(out["api"]["created"])
        self.assertTrue(out["ingress"]["created"])
        self.assertEqual(len(conn.network.fips), 2)

    def test_one_on_another_network_is_refused(self):
        conn = FakeConn()
        conn.network.fips.append(Obj(id="fip-x", floating_ip_address="192.0.2.99", project_id=PROJECT, port_id=None,
                                     floating_network_id=OTHER_NET, description=DESCS["api"]))
        with self.assertRaises(RuntimeError) as cm:
            present(conn)
        self.assertIn(f"is on network {OTHER_NET}, not {NET}", str(cm.exception))
        self.assertEqual(conn.network.calls, [])

    def test_unassociated_duplicates_are_released(self):
        conn = FakeConn()
        for fid, port in (("fip-a", None), ("fip-b", "port-1"), ("fip-c", None)):
            conn.network.fips.append(Obj(id=fid, floating_ip_address="192.0.2.50", project_id=PROJECT, port_id=port,
                                         floating_network_id=NET, description=DESCS["api"]))
        out = present(conn)
        self.assertEqual(out["api"]["id"], "fip-b")  # the associated one stays
        self.assertEqual(out["released"], ["fip-a", "fip-c"])

    def test_both_descriptions_are_required(self):
        with self.assertRaises(ValueError):
            present(FakeConn(), descriptions={"api": DESCS["api"]})


class Absent(unittest.TestCase):
    def test_release_by_recorded_ids(self):
        conn = FakeConn()
        out = present(conn)
        rel = call(conn, state="absent", ids=[out["api"]["id"], out["ingress"]["id"]])
        self.assertEqual(rel, {"released": ["fip-1", "fip-2"], "missing": []})
        self.assertEqual(conn.network.fips, [])

    def test_missing_id_counts_as_released(self):
        conn = FakeConn()
        present(conn)
        rel = call(conn, state="absent", ids=["fip-1", "fip-gone", ""])
        self.assertEqual(rel, {"released": ["fip-1"], "missing": ["fip-gone"]})
        again = call(conn, state="absent", ids=["fip-1"])
        self.assertEqual(again, {"released": [], "missing": ["fip-1"]})

    def test_unrecorded_ones_go_by_description(self):
        # a run that created a floating IP and failed before recording it
        conn = FakeConn()
        present(conn)
        rel = call(conn, state="absent", ids=[], descriptions=DESCS)
        self.assertEqual(sorted(rel["released"]), ["fip-1", "fip-2"])

    def test_another_projects_floating_ip_is_never_touched(self):
        conn = FakeConn()
        conn.network.fips.append(Obj(id="fip-z", floating_ip_address="192.0.2.77", project_id="proj-z", port_id=None,
                                     floating_network_id=NET, description="x"))
        rel = call(conn, state="absent", ids=["fip-z"])
        self.assertEqual(rel, {"released": [], "missing": ["fip-z"]})
        self.assertEqual(len(conn.network.fips), 1)


class ProvisionWithoutPair(unittest.TestCase):
    def test_no_fip_descriptions_no_external_network(self):
        conn_d, conn_p = mock.MagicMock(), mock.MagicMock()
        conn_d.session.auth.get_access.return_value = Obj(user_id="u1", domain_id="d1", project_id=None)
        conn_d.identity.projects.return_value = [Obj(id="p1", name="env")]
        conn_d.identity.find_role.return_value = Obj(name="member")
        conn_p.identity.application_credentials.return_value = []
        conn_p.identity.create_application_credential.return_value = Obj(id="ac1", secret="s")
        acct = mock.Mock()
        acct.domain.return_value, acct.project.return_value = conn_d, conn_p
        acct.auth, acct.region = {"auth_url": "https://keystone.example:5000"}, "r1"
        with mock.patch.object(S, "Account", return_value=acct):
            out = S.provision({"action": "provision", "clouds_yaml": "/dev/null", "domain": "tenants", "project": "env",
                               "app_cred_name": "env-cred"})
        self.assertEqual(out["fips"], {})
        conn_p.network.find_network.assert_not_called()
        conn_p.network.create_ip.assert_not_called()


if __name__ == "__main__":
    unittest.main()

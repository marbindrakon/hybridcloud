"""cluster-fips of eda/common/files/shc_openstack.py against a fake Neutron: a cluster's API and ingress floating IPs
found by description first (a re-run after a failure at any step creates nothing twice), else created on the named
network; one on another network refused; duplicates of a description released; the release by recorded ids and by
description, a missing id counted as released, another project's floating IP never touched. Also provision without
the environment's floating IP pair. Addresses are documentation space only."""
import importlib.util
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


class NetworkNameCheck(unittest.TestCase):
    """Review R2 M3 and gate finding F1: the networks of a name as the installer finds them. The helper lists them
    with the environment's own credential, so the fake Neutron below answers what Neutron gave a project-scoped
    account at the gate (evidence s5/g1-internet-visibility.txt, s3/c-os-ra-on.txt): the cloud-wide external network
    `internet` (router:external True, shared False, owner the admin project) and `ext-net` (router:external True,
    shared True) with NO RBAC policy readable by the project, and a placed network exposed to the project by the
    platform's access_as_external policy (router:external True for the project; shared True only while use for
    virtual machines is on). Ids and names are examples."""

    ADMIN, PLATFORM, OTHER = "proj-admin", "proj-platform", "proj-other"

    def net(self, nid, name, owner, shared, external):
        return Obj(id=nid, name=name, project_id=owner, is_shared=shared, is_router_external=external)

    def check(self, listed, name, want, scoped=PROJECT):
        """listed: what Neutron returns to the environment's project for a name filter (its own filtering)."""
        conn = mock.MagicMock()
        conn.network.networks.side_effect = lambda **f: [n for n in listed if n.name == f.get("name")]
        conn.network.rbac_policies.return_value = []      # a project-scoped token reads no policy of another project
        conn.session.auth.get_access.return_value = Obj(user_id="u-app", domain_id=None, project_id=scoped)
        acct = mock.Mock()
        acct.as_configured.return_value = conn
        acct.project.return_value = conn
        with mock.patch.object(S, "Account", return_value=acct):
            out = S.network_name_check({"clouds_yaml": "/dev/null", "project_id": PROJECT, "name": name,
                                        "network_id": want})
        acct.as_configured.assert_called_once_with()
        return out

    def test_cloud_wide_external_not_shared(self):
        # F1: the site's default `internet` (external for every project through the admin project's policy)
        out = self.check([self.net("net-inet", "internet", self.ADMIN, False, True)], "internet", "net-inet")
        self.assertEqual([v["id"] for v in out["visible"]], ["net-inet"])
        self.assertTrue(out["unique"])
        self.assertTrue(out["external"])

    def test_cloud_wide_external_and_shared(self):
        out = self.check([self.net("net-ext", "ext-net", self.ADMIN, True, True)], "ext-net", "net-ext")
        self.assertTrue(out["unique"] and out["external"])

    def test_exposed_by_the_products_policy_only(self):
        # router attachments on, use for virtual machines off: external for this project, not shared
        out = self.check([self.net("net-placed", "hn51005", self.PLATFORM, False, True)], "hn51005", "net-placed")
        self.assertTrue(out["unique"] and out["external"])
        # both uses on (the gate's g5 placement): shared and external for the project
        out = self.check([self.net("net-placed", "hn51005", self.PLATFORM, True, True)], "hn51005", "net-placed")
        self.assertTrue(out["unique"] and out["external"])

    def test_visible_but_neither_external_nor_the_sites(self):
        # another project's network shared into this one under the same name: it is what the installer would find
        other = self.net("net-x", "internet", self.OTHER, True, False)
        out = self.check([other], "internet", "net-inet")
        self.assertEqual([v["id"] for v in out["visible"]], ["net-x"])
        self.assertFalse(out["unique"])
        self.assertFalse(out["external"])
        # the expected network seen only as shared (its external policy for the project not there): not usable
        out = self.check([self.net("net-placed", "hn51005", self.PLATFORM, True, False)], "hn51005", "net-placed")
        self.assertTrue(out["unique"])
        self.assertFalse(out["external"])

    def test_two_usable_networks_with_one_name(self):
        listed = [self.net("net-inet", "internet", self.ADMIN, False, True),
                  self.net("net-mine", "internet", PROJECT, False, False)]       # the tenant's own, same name
        out = self.check(listed, "internet", "net-inet")
        self.assertFalse(out["unique"])
        self.assertTrue(out["external"])
        self.assertEqual(sorted(v["id"] for v in out["visible"]), ["net-inet", "net-mine"])
        self.assertEqual([v["project_id"] for v in out["visible"] if v["id"] == "net-mine"], [PROJECT])

    def test_not_visible(self):
        out = self.check([], "hn51005", "net-placed")
        self.assertEqual((out["visible"], out["unique"], out["external"]), ([], False, False))

    def test_credential_of_another_project_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            self.check([self.net("net-inet", "internet", self.ADMIN, False, True)], "internet", "net-inet",
                       scoped="proj-elsewhere")
        self.assertIn("scoped to project proj-elsewhere", str(cm.exception))


class AccountAsConfigured(unittest.TestCase):
    """The environment's credential is an application credential: its auth type goes along (without it the SDK
    takes a password plugin and refuses application_credential_id)."""

    def test_auth_type_passed_only_when_written(self):
        import tempfile
        import yaml as Y
        for entry, want in (({"auth_type": "v3applicationcredential",
                              "auth": {"auth_url": "https://keystone.example.org/v3", "application_credential_id": "a",
                                       "application_credential_secret": "s"}}, "v3applicationcredential"),
                            ({"auth": {"auth_url": "https://keystone.example.org/v3", "username": "u",
                                       "password": "p", "project_id": "p1"}}, None)):
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
                Y.safe_dump({"clouds": {"openstack": dict(entry, region_name="r1")}}, fh)
            try:
                with mock.patch.object(S.openstack, "connect", create=True) as c:
                    S.Account({"clouds_yaml": fh.name}).as_configured()
                kw = c.call_args.kwargs
                self.assertEqual(kw.get("auth_type"), want)
                self.assertEqual(kw["auth"], entry["auth"])          # scope keys kept: the credential's own
            finally:
                os.unlink(fh.name)


class ProjectRouters(unittest.TestCase):
    """fix-routergw (gate F10): the cluster's router read through the helper, not a collection module the job's
    execution environment lacks. The site account scoped to the environment's project lists the routers of the name in
    that project only, in the shape files/po_preflight.py router-gateway judges."""

    def run_it(self, routers, **req):
        conn = mock.MagicMock()
        seen = []

        def listing(**f):
            seen.append(f)
            return [r for r in routers if all(getattr(r, k, None) == v for k, v in f.items())]
        conn.network.routers.side_effect = listing
        acct = mock.Mock()
        acct.project.return_value = conn
        with mock.patch.object(S, "Account", return_value=acct):
            out = S.ACTIONS["project-routers"]({"action": "project-routers", "clouds_yaml": "/dev/null",
                                                "project_id": PROJECT, "name": "c1-x7k2p-external-router", **req})
        acct.project.assert_called_once_with(PROJECT)
        self.assertEqual(seen, [{"name": "c1-x7k2p-external-router", "project_id": PROJECT}])
        conn.network.update_router.assert_not_called()
        return out

    @staticmethod
    def router(rid, project=PROJECT, name="c1-x7k2p-external-router", net=NET, addrs=("192.0.2.30",)):
        gw = {"network_id": net, "enable_snat": True,
              "external_fixed_ips": [{"subnet_id": "sn-1", "ip_address": a} for a in addrs]} if net else None
        return Obj(id=rid, name=name, project_id=project, external_gateway_info=gw)

    def test_the_projects_router_with_its_gateway(self):
        out = self.run_it([self.router("r-1", addrs=("2001:db8::30", "192.0.2.30")),
                           self.router("r-2", project="proj-other"), self.router("r-3", name="other-router")])
        self.assertEqual(out, {"routers": [{"id": "r-1", "name": "c1-x7k2p-external-router", "project_id": PROJECT,
                                            "external_gateway_info": {"network_id": NET, "external_fixed_ips": [
                                                {"subnet_id": "sn-1", "ip_address": "2001:db8::30"},
                                                {"subnet_id": "sn-1", "ip_address": "192.0.2.30"}]}}]})
        # the role's judge takes this shape unchanged
        spec = importlib.util.spec_from_file_location(
            "po_preflight", os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision", "files",
                                         "po_preflight.py"))
        P = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(P)
        judged = P.router_gateway({"routers": out["routers"], "name": "c1-x7k2p-external-router", "network_id": NET})
        self.assertEqual(judged, {"address": "192.0.2.30", "note": ""})

    def test_no_gateway_and_two_of_a_name(self):
        out = self.run_it([self.router("r-1", net=None)])
        self.assertEqual(out["routers"][0]["external_gateway_info"], {"network_id": "", "external_fixed_ips": []})
        out = self.run_it([self.router("r-1"), self.router("r-2")])
        self.assertEqual([r["id"] for r in out["routers"]], ["r-1", "r-2"])

    def test_needs_the_project_and_the_name(self):
        for req in ({"project_id": ""}, {"name": ""}, {"project_id": None}):
            with self.subTest(req=req), mock.patch.object(S, "Account") as acct:
                with self.assertRaises(ValueError):
                    S.project_routers({"clouds_yaml": "/dev/null", "project_id": PROJECT,
                                       "name": "c1-x7k2p-external-router", **req})
                acct.assert_not_called()

"""The tenant DNS controller (charts/charts/shc-cluster-base/files/tenant_dns_controller.py): forward-confirmed PTRs for
AAAA addresses inside the namespace's placement prefixes (written in the covering reverse zone of the DNS project with
the AAAA's ownership, not written outside the prefixes or without a reverse zone, deleted with the AAAA, swept when
orphaned) and its health record (the ConfigMap tenant-dns-status and the DNSReconcileFailed event). Runs the
controller's reconcile against a fake Designate connection and fake Kubernetes APIs; the SDKs are stubbed, so neither
openstacksdk nor the kubernetes client is needed. Documentation prefixes and names only."""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILE = os.path.join(ROOT, "charts", "charts", "shc-cluster-base", "files", "tenant_dns_controller.py")


# ── SDK stubs ──────────────────────────────────────────────────────────────────────────────────────────────────
class HttpException(Exception):
    status_code = 500


class ConflictException(HttpException):
    status_code = 409


class BadRequestException(HttpException):
    status_code = 400


class ApiException(Exception):
    def __init__(self, status=500, reason=""):
        super().__init__(f"{status} {reason}")
        self.status, self.reason = status, reason


class Obj:
    """Stands in for the kubernetes client's model classes: keeps its keyword arguments as attributes."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def stubs():
    os_mod = types.ModuleType("openstack")
    os_mod.exceptions = types.SimpleNamespace(HttpException=HttpException, ConflictException=ConflictException,
                                              BadRequestException=BadRequestException,
                                              raise_from_response=lambda r: None)
    os_mod.connect = None
    rs_mod = types.ModuleType("openstack.dns.v2.recordset")

    class Recordset(types.SimpleNamespace):
        @classmethod
        def existing(cls, connection=None, **d):
            return cls(**d)
    rs_mod.Recordset = Recordset
    k8s = types.ModuleType("kubernetes")
    k8s.client = types.SimpleNamespace(
        ApiException=ApiException, CoreV1Api=lambda: None, CustomObjectsApi=lambda: None,
        ApiClient=lambda: types.SimpleNamespace(sanitize_for_serialization=lambda o: o),
        CoreV1Event=Obj, V1ObjectMeta=Obj, V1ObjectReference=Obj, V1EventSource=Obj, V1ConfigMap=Obj)
    k8s.config = types.SimpleNamespace(ConfigException=Exception)
    k8s.watch = types.SimpleNamespace()
    return {"openstack": os_mod, "openstack.dns": types.ModuleType("openstack.dns"),
            "openstack.dns.v2": types.ModuleType("openstack.dns.v2"), "openstack.dns.v2.recordset": rs_mod,
            "kubernetes": k8s}


def load_controller():
    with mock.patch.dict(sys.modules, stubs()):
        spec = importlib.util.spec_from_file_location("tenant_dns_controller_under_test", FILE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


T = load_controller()


# ── fakes ──────────────────────────────────────────────────────────────────────────────────────────────────────
class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeDesignate:
    """The DNS project: its zones and recordsets; one GET /recordsets listing filtered by description."""

    def __init__(self, zones):
        self.zones_ = [types.SimpleNamespace(name=z, id=f"zone-{i}") for i, z in enumerate(zones)]
        self.rs = {}
        self.n = 0
        self.calls = []
        self.fail_zones = None

    def zone_id(self, name):
        return next(z.id for z in self.zones_ if z.name == name)

    def add(self, zone, name, rtype, records, description, ttl=300):
        self.n += 1
        rid = f"rs-{self.n}"
        self.rs[rid] = {"id": rid, "zone_id": self.zone_id(zone), "name": name, "type": rtype,
                        "records": list(records), "ttl": ttl, "description": description}
        return rid

    def find(self, name, rtype):
        return next((d for d in self.rs.values() if d["name"] == name and d["type"] == rtype), None)

    # the SDK surface the controller uses
    def zones(self):
        self.calls.append("zones")
        if self.fail_zones:
            raise self.fail_zones
        return list(self.zones_)

    def get(self, url, params=None):
        self.calls.append("list")
        pat = (params or {}).get("description", "")
        assert pat.endswith("*")
        items = [dict(d) for d in self.rs.values() if (d["description"] or "").startswith(pat[:-1])]
        return Response({"recordsets": items, "links": {}})

    def create_recordset(self, zone, name, type, records, ttl, description):  # noqa: A002 - the SDK's names
        self.calls.append(("create", name, type))
        assert name == zone.name or name.endswith("." + zone.name), (name, zone.name)
        if self.find(name, type):
            raise ConflictException()
        self.add(zone.name, name, type, records, description, ttl)

    def update_recordset(self, rs, records, ttl, description):
        self.calls.append(("update", rs.name, rs.type))
        self.rs[rs.id].update(records=list(records), ttl=ttl, description=description)

    def delete_recordset(self, rs, zone_id, ignore_missing=True):
        self.calls.append(("delete", rs.name, rs.type))
        self.rs.pop(rs.id, None)


class FakeCore:
    def __init__(self, namespaces):
        self.namespaces = namespaces  # {name: annotations}
        self.events = []
        self.configmaps = {}

    def list_namespace(self, label_selector=None):
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(metadata=types.SimpleNamespace(name=n, annotations=dict(a)))
            for n, a in sorted(self.namespaces.items())])

    def list_service_for_all_namespaces(self):
        return types.SimpleNamespace(items=[])

    def create_namespaced_event(self, namespace, ev):
        self.events.append(ev)

    def patch_namespaced_config_map(self, name, namespace, body):
        if (namespace, name) not in self.configmaps:
            raise ApiException(404, "Not Found")
        self.configmaps[(namespace, name)].update(body["data"])

    def create_namespaced_config_map(self, namespace, cm):
        self.configmaps[(namespace, cm.metadata.name)] = dict(cm.data)


class FakeCustom:
    def __init__(self):
        self.deps = []

    def list_cluster_custom_object(self, group, version, plural):
        if plural == "dnsendpoints":
            return {"items": self.deps}
        return {"items": []}

    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body):
        for o in self.deps:
            if o["metadata"]["namespace"] == namespace and o["metadata"]["name"] == name:
                o["metadata"].setdefault("annotations", {}).update(body["metadata"]["annotations"])

    def patch_namespaced_custom_object_status(self, *args):
        pass


DOMAIN = "tenants.example.org"
ZONE = f"env1.acme.{DOMAIN}."
NS = "acme-env1-vms"
NET1, NET2 = "2001:db8:4005:4200::/64", "2001:db8:4005:4201::/64"
A1, A2, OUT = "2001:db8:4005:4200::10", "2001:db8:4005:4201::20", "2001:db8:9999::30"
R48 = "5.0.0.4.8.b.d.0.1.0.0.2.ip6.arpa."   # 2001:db8:4005::/48
R32 = "8.b.d.0.1.0.0.2.ip6.arpa."           # 2001:db8::/32 (a shorter zone of the project)


def ptr_name(addr):
    import ipaddress
    return ipaddress.ip_address(addr).reverse_pointer + "."


def dep(name, targets, dns_name=None, rtype="AAAA", created="2026-01-01T00:00:00Z", ns=NS):
    return {"metadata": {"namespace": ns, "name": name, "uid": f"uid-{name}", "generation": 1,
                         "creationTimestamp": created},
            "spec": {"endpoints": [{"dnsName": dns_name or f"{name}.{ZONE}", "recordType": rtype,
                                    "targets": list(targets)}]}}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="tdns-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        clouds = os.path.join(tmp, "clouds.yaml")
        with open(clouds, "w") as f:
            yaml.safe_dump({"clouds": {"openstack": {"auth": {}}}}, f)
        self.c = T.Controller({"cluster": "site-a", "domain": DOMAIN, "namespace": "dns-ctl",
                               "healthFile": os.path.join(tmp, "healthy")}, clouds)
        self.core = FakeCore({NS: {T.ZONE_ANNOTATION: ZONE, T.PREFIXES_ANNOTATION: f"{NET1}, {NET2}"}})
        self.custom = FakeCustom()
        self.d = FakeDesignate([ZONE, R48, R32, f"other.{DOMAIN}."])
        self.c.core, self.c.custom = self.core, self.custom
        conn = types.SimpleNamespace(dns=self.d)
        self.c.dns = lambda: conn  # the fake connection also after a failed round dropped the cached one
        quiet = mock.patch.object(T, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def run_round(self):
        return self.c.reconcile_round()

    def status(self, name):
        o = next(o for o in self.custom.deps if o["metadata"]["name"] == name)
        return o["metadata"]["annotations"][T.STATUS_ANNOTATION]

    def desc(self, name, ns=NS):
        return self.c.description(f"{ns}/DNSEndpoint/{name}")


class Helpers(unittest.TestCase):
    def test_reverse_name_and_covering_zone(self):
        self.assertEqual(T.reverse_name(A1), "0.1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.2.4.5.0.0.4.8.b.d.0.1.0.0.2.ip6.arpa.")
        self.assertEqual(T.covering_zone(T.reverse_name(A1), [R32, R48, ZONE]), R48)
        self.assertEqual(T.covering_zone(T.reverse_name("2001:db8:1::1"), [R32, R48]), R32)
        self.assertIsNone(T.covering_zone(T.reverse_name("2001:db9::1"), [R32, R48]))
        # a label boundary, not a string suffix
        self.assertIsNone(T.covering_zone("x" + R48, [R48]))

    def test_ptr_is_never_a_tenant_type(self):
        tmp = tempfile.mkdtemp(prefix="tdns-test-")
        clouds = os.path.join(tmp, "clouds.yaml")
        with open(clouds, "w") as f:
            yaml.safe_dump({"clouds": {"openstack": {"auth": {}}}}, f)
        c = T.Controller({"cluster": "x", "domain": DOMAIN, "types": ["AAAA", "PTR"], "namespace": "n"}, clouds)
        self.assertEqual(c.types, {"AAAA"})


class Ptrs(Base):
    def test_in_prefix_aaaa_gets_its_ptr(self):
        self.custom.deps = [dep("web", [A1, A2])]
        self.assertTrue(self.run_round())
        aaaa = self.d.find(f"web.{ZONE}", "AAAA")
        self.assertEqual(sorted(aaaa["records"]), sorted([A1, A2]))
        for addr in (A1, A2):
            p = self.d.find(ptr_name(addr), "PTR")
            self.assertIsNotNone(p, addr)
            self.assertEqual(p["records"], [f"web.{ZONE}"])
            self.assertEqual(p["zone_id"], self.d.zone_id(R48))  # the longest covering zone
            self.assertEqual(p["description"], aaaa["description"])
            self.assertEqual(p["description"], self.desc("web"))
        st = self.status("web")
        self.assertTrue(st.startswith("ok: 3 record(s): "), st)
        self.assertIn(f"{A1} PTR", st)
        self.assertEqual(self.c.counts, {"refused": 0, "ptrs": 2})
        # one zone listing and one recordset listing per round
        self.assertEqual([x for x in self.d.calls if x in ("zones", "list")], ["zones", "list"])
        # a second round changes nothing
        self.d.calls.clear()
        self.run_round()
        self.assertEqual([x for x in self.d.calls if isinstance(x, tuple)], [])

    def test_outside_the_prefixes_no_ptr_but_the_aaaa(self):
        self.custom.deps = [dep("web", [A1, OUT])]
        self.run_round()
        self.assertEqual(sorted(self.d.find(f"web.{ZONE}", "AAAA")["records"]), sorted([A1, OUT]))
        self.assertIsNotNone(self.d.find(ptr_name(A1), "PTR"))
        self.assertIsNone(self.d.find(ptr_name(OUT), "PTR"))
        st = self.status("web")
        self.assertTrue(st.startswith("ok:"), st)
        self.assertIn(f"no PTR for {OUT}: outside the placement prefixes", st)
        events = [e for e in self.core.events if e.involved_object.name == "web"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].type, events[0].reason), ("Normal", "DNSRecordsPublished"))
        self.run_round()  # said once: the same status writes no second event
        self.assertEqual(len([e for e in self.core.events if e.involved_object.name == "web"]), 1)

    def test_no_platform_reverse_zone(self):
        self.d.zones_ = [z for z in self.d.zones_ if not z.name.endswith(".ip6.arpa.")]
        self.custom.deps = [dep("web", [A1])]
        self.run_round()
        self.assertIsNotNone(self.d.find(f"web.{ZONE}", "AAAA"))
        self.assertEqual([d for d in self.d.rs.values() if d["type"] == "PTR"], [])
        self.assertIn(f"no PTR for {A1}: no platform reverse zone", self.status("web"))

    def test_namespace_without_prefixes_has_no_reverse_names(self):
        self.core.namespaces[NS] = {T.ZONE_ANNOTATION: ZONE}
        self.custom.deps = [dep("web", [A1])]
        self.run_round()
        self.assertIsNotNone(self.d.find(f"web.{ZONE}", "AAAA"))
        self.assertIsNone(self.d.find(ptr_name(A1), "PTR"))
        self.assertEqual(self.status("web"), f"ok: 1 record(s): web.{ZONE.rstrip('.')} AAAA")

    def test_garbage_prefixes_are_ignored(self):
        self.core.namespaces[NS][T.PREFIXES_ANNOTATION] = f"not-a-prefix, 192.0.2.0/24, {NET1},,"
        self.custom.deps = [dep("web", [A1, A2])]
        with mock.patch.object(T, "log") as log:
            self.run_round()
            self.run_round()
        bad = [c.args[0] for c in log.call_args_list if "ignored" in c.args[0]]
        self.assertEqual(len(bad), 2, bad)  # each unusable entry logged once
        self.assertIsNotNone(self.d.find(ptr_name(A1), "PTR"))
        self.assertIsNone(self.d.find(ptr_name(A2), "PTR"))  # NET2 is no longer listed

    def test_ptr_goes_with_the_aaaa(self):
        self.custom.deps = [dep("web", [A1, A2])]
        self.run_round()
        # the address changed: the old PTR goes, the new one comes
        self.custom.deps = [dep("web", [A2, "2001:db8:4005:4200::11"])]
        self.run_round()
        self.assertIsNone(self.d.find(ptr_name(A1), "PTR"))
        self.assertIsNotNone(self.d.find(ptr_name(A2), "PTR"))
        self.assertIsNotNone(self.d.find(ptr_name("2001:db8:4005:4200::11"), "PTR"))
        # the annotation changed: NET1 left the namespace, its PTR goes, the AAAA stays
        self.core.namespaces[NS][T.PREFIXES_ANNOTATION] = NET2
        self.run_round()
        self.assertIsNone(self.d.find(ptr_name("2001:db8:4005:4200::11"), "PTR"))
        self.assertIsNotNone(self.d.find(ptr_name(A2), "PTR"))
        self.assertEqual(sorted(self.d.find(f"web.{ZONE}", "AAAA")["records"]),
                         sorted([A2, "2001:db8:4005:4200::11"]))
        # the object went: the AAAA and every PTR go
        self.custom.deps = []
        self.run_round()
        self.assertEqual(list(self.d.rs.values()), [])

    def test_the_sweep_handles_ptrs(self):
        mine = self.c.prefix + "owner=" + f"{NS}/DNSEndpoint/gone"
        other = "shc-tenant-dns cluster=site-b owner=x/DNSEndpoint/y"
        orphan = self.d.add(R48, ptr_name(A1), "PTR", [f"gone.{ZONE}"], mine)               # owner object gone
        stale = self.d.add(R48, ptr_name(A2), "PTR", [f"web.{ZONE}"], self.desc("web"))     # AAAA no longer has A2
        foreign = self.d.add(R48, ptr_name("2001:db8:4005:4200::99"), "PTR", [f"y.{ZONE}"], other)
        platform = self.d.add(R48, ptr_name("2001:db8:4005:4200::1"), "PTR", [f"gw.{ZONE}"], "shc:platform")
        self.custom.deps = [dep("web", ["2001:db8:4005:4200::12"])]
        self.run_round()
        self.assertNotIn(orphan, self.d.rs)
        self.assertNotIn(stale, self.d.rs)
        self.assertIn(foreign, self.d.rs)   # another cluster's
        self.assertIn(platform, self.d.rs)  # not the controller's
        self.assertIsNotNone(self.d.find(ptr_name("2001:db8:4005:4200::12"), "PTR"))

    def test_an_address_moves_to_another_object(self):
        self.custom.deps = [dep("old", [A1])]
        self.run_round()
        self.custom.deps = [dep("new", [A1])]
        self.run_round()
        p = self.d.find(ptr_name(A1), "PTR")
        self.assertEqual((p["records"], p["description"]), ([f"new.{ZONE}"], self.desc("new")))
        self.assertEqual(len([d for d in self.d.rs.values() if d["type"] == "PTR"]), 1)

    def test_one_reverse_name_per_address(self):
        self.custom.deps = [dep("a", [A1], created="2026-01-01T00:00:00Z"),
                            dep("b", [A1], created="2026-01-02T00:00:00Z")]
        self.run_round()
        self.assertEqual(self.d.find(ptr_name(A1), "PTR")["records"], [f"a.{ZONE}"])
        self.assertIn(f"no PTR for {A1}: its reverse name points to a.{ZONE.rstrip('.')}", self.status("b"))
        self.assertTrue(self.status("b").startswith("ok:"))

    def test_a_reverse_name_held_by_someone_else(self):
        self.d.add(R48, ptr_name(A1), "PTR", ["elsewhere.example."], "")
        self.custom.deps = [dep("web", [A1])]
        self.run_round()
        self.assertEqual(self.d.find(ptr_name(A1), "PTR")["records"], ["elsewhere.example."])
        self.assertIn(f"no PTR for {A1}: its reverse name is held by another owner", self.status("web"))
        self.assertIsNotNone(self.d.find(f"web.{ZONE}", "AAAA"))

    def test_no_ptr_before_the_aaaa_is_published(self):
        self.d.zones_ = [z for z in self.d.zones_ if z.name != ZONE]  # the environment zone is missing
        self.custom.deps = [dep("web", [A1])]
        self.run_round()
        self.assertEqual(list(self.d.rs.values()), [])
        self.assertTrue(self.status("web").startswith("refused:"))
        self.assertEqual(self.c.counts, {"refused": 1, "ptrs": 0})

    def test_a_tenant_cannot_ask_for_a_ptr(self):
        self.custom.deps = [dep("p", [f"web.{ZONE}"], dns_name=f"x.{ZONE}", rtype="PTR")]
        self.run_round()
        self.assertIn("record type PTR is not allowed", self.status("p"))
        self.assertEqual(list(self.d.rs.values()), [])


class Health(Base):
    KEY = ("dns-ctl", "tenant-dns-status")

    def test_success_writes_the_record(self):
        self.custom.deps = [dep("web", [A1]), dep("bad", ["192.0.2.1"])]  # an A target in an AAAA: refused
        with mock.patch.object(T, "rfc3339", return_value="2026-10-31T10:00:00Z"):
            self.assertTrue(self.run_round())
        self.assertEqual(self.core.configmaps[self.KEY], {"lastSuccess": "2026-10-31T10:00:00Z", "lastError": "",
                                                          "lastErrorAt": "", "refused": "1", "ptrs": "1"})
        self.assertEqual([e for e in self.core.events if e.reason == "DNSReconcileFailed"], [])

    def test_failures_keep_the_last_success_and_warn_once_per_error(self):
        with mock.patch.object(T, "rfc3339", return_value="2026-10-31T10:00:00Z"):
            self.run_round()
        self.d.fail_zones = HttpException("designate down")
        with mock.patch.object(T, "rfc3339", return_value="2026-10-31T10:05:00Z"):
            self.assertFalse(self.run_round())
            self.assertFalse(self.run_round())
        cm = self.core.configmaps[self.KEY]
        self.assertEqual(cm["lastSuccess"], "2026-10-31T10:00:00Z")
        self.assertEqual(cm["lastErrorAt"], "2026-10-31T10:05:00Z")
        self.assertEqual(cm["lastError"], "HttpException: designate down")
        warn = [e for e in self.core.events if e.reason == "DNSReconcileFailed"]
        self.assertEqual(len(warn), 1)
        self.assertEqual((warn[0].type, warn[0].involved_object.kind, warn[0].involved_object.name,
                          warn[0].metadata.namespace), ("Warning", "ConfigMap", "tenant-dns-status", "dns-ctl"))
        self.d.fail_zones = HttpException("other failure")
        self.run_round()
        self.assertEqual(len([e for e in self.core.events if e.reason == "DNSReconcileFailed"]), 2)
        # a success, then the first error again: warned again
        self.d.fail_zones = None
        with mock.patch.object(T, "rfc3339", return_value="2026-10-31T10:10:00Z"):
            self.run_round()
        self.assertEqual(self.core.configmaps[self.KEY]["lastSuccess"], "2026-10-31T10:10:00Z")
        self.assertEqual(self.core.configmaps[self.KEY]["lastError"], "HttpException: other failure")
        self.d.fail_zones = HttpException("designate down")
        self.run_round()
        self.assertEqual(len([e for e in self.core.events if e.reason == "DNSReconcileFailed"]), 3)

    def test_a_first_round_that_fails_creates_the_record(self):
        self.d.fail_zones = HttpException("designate down")
        with mock.patch.object(T, "rfc3339", return_value="2026-10-31T10:00:00Z"):
            self.run_round()
        self.assertEqual(self.core.configmaps[self.KEY],
                         {"lastSuccess": "", "lastError": "HttpException: designate down",
                          "lastErrorAt": "2026-10-31T10:00:00Z", "refused": "0", "ptrs": "0"})

    def test_the_health_write_never_raises(self):
        def broken(*a, **k):
            raise ApiException(403, "Forbidden")
        self.core.patch_namespaced_config_map = broken
        self.core.create_namespaced_event = broken
        self.d.fail_zones = HttpException("designate down")
        self.assertFalse(self.run_round())


if __name__ == "__main__":
    unittest.main()

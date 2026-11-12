"""Reverse DNS of the platform's IPv6 space (design/IPV6-PLAN.md §8.3): the nibble and zone helpers of
eda/common/files/shc_dns.py, its ensure-record / delete-record / ensure-ptr / delete-ptr / ptr-name actions against a
fake connection (ownership marker, refusal of another owner's recordset, idempotency, the covering zone), the IPv6
gateway in a placement's status.subnets (CloudVirt: the first host; CloudOSO: the site's gateway_ip, the
placement-network result's gateways), the placement and gateway name tasks and the shared writer
(reverse_dns_apply.yml / reverse_dns_name.yml) as plays against a fake helper that records every request, the site
job's reverse zones, and the teardown order (names before the subnets go back, PTR before AAAA). Documentation
prefixes and example.test only."""
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
try:
    import openstack  # noqa: F401
except ImportError:  # stub the SDK so the modules import; the actions run against fake connections
    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(ForbiddenException=Exception, NotFoundException=Exception,
                                            HttpException=Exception)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)
import shc_dns as D  # noqa: E402
import shc_openstack as S  # noqa: E402

COMMON = os.path.join(ROOT, "eda", "common")
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
NPD = os.path.join(ROLES, "networkplacement_teardown", "tasks")
CG = os.path.join(ROLES, "cloudgateway_provision", "tasks")
CGD = os.path.join(ROLES, "cloudgateway_teardown", "tasks")
CI = os.path.join(ROLES, "cloudinfrastructure_provision", "tasks")
DEFAULTS = os.path.join(COMMON, "vars", "fabric_realization.yml")

ZONE32 = "8.b.d.0.1.0.0.2.ip6.arpa"            # 2001:db8::/32
ZONE48 = "1.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa"    # 2001:db8:1::/48
DNSP = "proj-dns"
OWNER = "NetworkPlacement/acme/p1"
GW6 = "2001:db8:1:7::1"


# ---- pure helpers ----
class Helpers(unittest.TestCase):
    def test_ptr_name_is_the_full_nibble_form(self):
        n = D.ptr_name("2001:db8::1")
        self.assertEqual(n, "1." + "0." * 23 + "8.b.d.0.1.0.0.2.ip6.arpa.")
        self.assertEqual(len(n.split(".")), 32 + 3)
        self.assertEqual(D.ptr_address(n), ipaddress.IPv6Address("2001:db8::1"))
        with self.assertRaises(ValueError):
            D.ptr_name("192.0.2.1")
        with self.assertRaises(ValueError):
            D.ptr_address(ZONE32)

    def test_zone_network(self):
        self.assertEqual(D.zone_network(ZONE32), ipaddress.IPv6Network("2001:db8::/32"))
        self.assertEqual(D.zone_network(ZONE48 + "."), ipaddress.IPv6Network("2001:db8:1::/48"))
        self.assertEqual(D.zone_network("F.f.ip6.arpa"), ipaddress.IPv6Network("ff00::/8"))
        for bad in ("example.test", "ip6.arpa", "10.8.b.d.ip6.arpa", "g.ip6.arpa", "2.0.192.in-addr.arpa"):
            self.assertIsNone(D.zone_network(bad), bad)

    def test_covering_zone_is_the_longest(self):
        zones = [ZONE32 + ".", ZONE48 + ".", "example.test."]
        self.assertEqual(D.covering_zone("2001:db8:1:7::1", zones), ZONE48 + ".")
        self.assertEqual(D.covering_zone("2001:db8:2::1", zones), ZONE32 + ".")
        self.assertIsNone(D.covering_zone("2001:db9::1", zones))
        self.assertIsNone(D.covering_zone("192.0.2.1", zones))

    def test_owner_description(self):
        self.assertEqual(D.owner_description(OWNER), "shc:" + OWNER)
        long = D.owner_description("NetworkPlacement/" + "n" * 63 + "/" + "x" * 200)
        self.assertEqual(len(long), D.DESCRIPTION_MAX)
        self.assertNotEqual(long, D.owner_description("NetworkPlacement/" + "n" * 63 + "/" + "x" * 199))

    def test_canonical_records(self):
        self.assertEqual(D.canonical_records("AAAA", ["2001:DB8:0::1"]), ["2001:db8::1"])
        self.assertEqual(D.canonical_records("PTR", ["GW.example.test"]), ["gw.example.test."])
        with self.assertRaises(ValueError):
            D.canonical_records("AAAA", ["192.0.2.1"])
        with self.assertRaises(ValueError):
            D.canonical_records("AAAA", [])


# ---- the actions against a fake Designate ----
class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeDNS:
    def __init__(self, conn, zones):
        self.conn, self.zones_, self.sets, self.n = conn, zones, [], 0

    def zones(self, name=None):
        # Designate's name filter: exact or with a leading wildcard
        if name and name.startswith("*"):
            return [z for z in self.zones_ if z.name.endswith(name[1:])]
        return [z for z in self.zones_ if name is None or z.name == name]

    def recordsets(self, zone, name=None, type=None):
        return [r for r in self.sets if r.zone_id == zone.id and (name is None or r.name == name)
                and (type is None or r.type == type)]

    def create_recordset(self, zone, **kw):
        self.n += 1
        rs = Obj(id=f"rs-{self.n}", zone_id=zone.id, **kw)
        self.sets.append(rs)
        self.conn.calls.append(("create", kw["name"], kw["type"]))
        return rs

    def update_recordset(self, rs, **kw):
        rs.__dict__.update(kw)
        self.conn.calls.append(("update", rs.name, rs.type))
        return rs

    def delete_recordset(self, rs, zone, ignore_missing=True):
        self.sets.remove(rs)
        self.conn.calls.append(("delete", rs.name, rs.type))


class FakeConn:
    def __init__(self, zones):
        self.calls = []
        self.dns = FakeDNS(self, zones)


def zone(name, zid, project=DNSP):
    return Obj(id=zid, name=D.fqdn(name), project_id=project)


def designate():
    return FakeConn([zone("env1.acme.example.test", "z-env"), zone("example.test", "z-global"),
                     zone(ZONE32, "z-32"), zone(ZONE48, "z-48"),
                     # shared with the project from another one: never written
                     zone("9." + ZONE48, "z-shared", project="proj-other")])


def call(conn, action, **req):
    acct = mock.Mock()
    acct.project.return_value = conn
    with mock.patch.object(D, "Account", return_value=acct):
        out = D.ACTIONS[action](dict(req, action=action, project_id=req.get("project_id", DNSP)))
    acct.project.assert_called_with(req.get("project_id", DNSP))
    return out


class Records(unittest.TestCase):
    NAME = "net1.gateways.env1.acme.example.test"

    def ensure(self, conn, records=(GW6,), owner=OWNER, ttl=3600, name=None):
        return call(conn, "ensure-record", zone="env1.acme.example.test.", name=name or self.NAME, type="AAAA",
                    records=list(records), ttl=ttl, owner=owner)

    def test_create_then_unchanged_then_update(self):
        conn = designate()
        out = self.ensure(conn)
        self.assertEqual(out, {"name": self.NAME + ".", "type": "AAAA", "zone": "env1.acme.example.test.",
                               "records": [GW6], "changed": True, "id": "rs-1"})
        rs = conn.dns.sets[0]
        self.assertEqual((rs.description, rs.ttl, rs.zone_id), ("shc:" + OWNER, 3600, "z-env"))
        self.assertFalse(self.ensure(conn)["changed"])
        self.assertTrue(self.ensure(conn, records=["2001:db8:1:7::2"])["changed"])
        self.assertTrue(self.ensure(conn, records=["2001:db8:1:7::2"], ttl=300)["changed"])
        self.assertEqual(conn.calls, [("create", self.NAME + ".", "AAAA"), ("update", self.NAME + ".", "AAAA"),
                                      ("update", self.NAME + ".", "AAAA")])
        self.assertEqual(rs.records, ["2001:db8:1:7::2"])

    def test_another_owner_is_refused_and_untouched(self):
        conn = designate()
        self.ensure(conn, owner="NetworkPlacement/other/p9")
        with self.assertRaisesRegex(RuntimeError, "is held by another owner"):
            self.ensure(conn)
        out = call(conn, "delete-record", zone="env1.acme.example.test", name=self.NAME, type="AAAA", owner=OWNER)
        self.assertEqual(out, {"name": self.NAME + ".", "type": "AAAA", "changed": False, "id": "",
                               "kept": "held by another owner"})
        self.assertEqual(len(conn.dns.sets), 1)
        self.assertEqual([c[0] for c in conn.calls], ["create"])

    def test_delete_owned_and_missing(self):
        conn = designate()
        self.ensure(conn)
        out = call(conn, "delete-record", zone="env1.acme.example.test", name=self.NAME, type="AAAA", owner=OWNER)
        self.assertEqual(out, {"name": self.NAME + ".", "type": "AAAA", "changed": True, "id": "rs-1"})
        self.assertEqual(conn.dns.sets, [])
        again = call(conn, "delete-record", zone="env1.acme.example.test", name=self.NAME, type="AAAA", owner=OWNER)
        self.assertEqual((again["changed"], again["id"]), (False, ""))
        gone = call(conn, "delete-record", zone="missing.example.test", name="x.missing.example.test", type="AAAA",
                    owner=OWNER)
        self.assertFalse(gone["changed"])

    def test_zone_and_name_checks(self):
        conn = designate()
        with self.assertRaisesRegex(RuntimeError, "zone missing.example.test. not found in project proj-dns"):
            call(conn, "ensure-record", zone="missing.example.test", name="a.missing.example.test", type="AAAA",
                 records=[GW6], owner=OWNER)
        with self.assertRaisesRegex(RuntimeError, "is not inside the zone"):
            self.ensure(conn, name="net1.other.test")
        # a zone shared from another project is not the project's own
        with self.assertRaisesRegex(RuntimeError, "not found in project"):
            call(conn, "ensure-record", zone="9." + ZONE48, name="1.9." + ZONE48, type="PTR",
                 records=["x.example.test"], owner=OWNER)


class Ptrs(unittest.TestCase):
    TARGET = "net1.gateways.env1.acme.example.test"

    def test_longest_covering_zone_and_idempotency(self):
        conn = designate()
        out = call(conn, "ensure-ptr", address=GW6, target=self.TARGET, ttl=3600, owner=OWNER)
        self.assertEqual(out["zone"], ZONE48 + ".")
        self.assertEqual(out["name"], D.ptr_name(GW6))
        self.assertEqual(out["records"], [self.TARGET + "."])
        self.assertTrue(out["changed"])
        rs = conn.dns.sets[0]
        self.assertEqual((rs.type, rs.zone_id, rs.description), ("PTR", "z-48", "shc:" + OWNER))
        self.assertFalse(call(conn, "ensure-ptr", address=GW6, target=self.TARGET + ".", ttl=3600, owner=OWNER)["changed"])
        # outside the /48, inside the /32
        out = call(conn, "ensure-ptr", address="2001:db8:2::1", target=self.TARGET, ttl=3600, owner=OWNER)
        self.assertEqual(out["zone"], ZONE32 + ".")

    def test_no_covering_zone_is_refused(self):
        conn = designate()
        with self.assertRaisesRegex(RuntimeError, "^no platform reverse zone covers 2001:db9::1$"):
            call(conn, "ensure-ptr", address="2001:db9::1", target=self.TARGET, owner=OWNER)
        # the zone shared from another project does not count, the /48 covers it
        out = call(conn, "ensure-ptr", address="2001:db8:1:9000::1", target=self.TARGET, owner=OWNER)
        self.assertEqual(out["zone"], ZONE48 + ".")
        self.assertEqual(conn.calls, [("create", D.ptr_name("2001:db8:1:9000::1"), "PTR")])

    def test_another_owner_and_delete(self):
        conn = designate()
        call(conn, "ensure-ptr", address=GW6, target="vm.example.test", owner="tenant/acme/vm")
        with self.assertRaisesRegex(RuntimeError, "held by another owner"):
            call(conn, "ensure-ptr", address=GW6, target=self.TARGET, owner=OWNER)
        self.assertEqual(call(conn, "delete-ptr", address=GW6, owner=OWNER)["changed"], False)
        self.assertEqual(len(conn.dns.sets), 1)
        self.assertTrue(call(conn, "delete-ptr", address=GW6, owner="tenant/acme/vm")["changed"])
        self.assertEqual(conn.dns.sets, [])
        # no covering zone: nothing to delete
        self.assertEqual(call(conn, "delete-ptr", address="2001:db9::1", owner=OWNER)["changed"], False)

    def test_ptr_name_action_needs_no_account(self):
        with mock.patch.object(D, "Account", side_effect=AssertionError("no account")):
            self.assertEqual(D.ACTIONS["ptr-name"]({"address": GW6, "zones": [ZONE32, ZONE48]}),
                             {"name": D.ptr_name(GW6), "zone": ZONE48 + "."})
            self.assertEqual(D.ACTIONS["ptr-name"]({"address": "2001:db9::1", "zones": [ZONE32]})["zone"], "")


# ---- the placement's IPv6 gateway in status.subnets ----
class PlacementNetworkGateways(unittest.TestCase):
    def test_gateways_in_the_result(self):
        import test_shc_openstack_placement as P
        conn = P.FakeConn()
        create = conn.network.create_subnet

        def with_gateway(**kw):
            s = create(**kw)
            s.gateway_ip = "2001:db8:5e00:100::1" if kw["ip_version"] == 6 else "10.65.1.1"
            return s
        conn.network.create_subnet = with_gateway
        out = P.run(conn)
        self.assertEqual(out["gateways"], {P.V4: "10.65.1.1", P.V6: "2001:db8:5e00:100::1"})
        # a subnet without a gateway reports ""
        conn = P.FakeConn()
        self.assertEqual(P.run(conn)["gateways"], {P.V4: "", P.V6: ""})


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SubnetGateways(unittest.TestCase):
    def subnets(self, kind, prefixes, rows=None):
        tasks = load(os.path.join(NP, "subnets.yml"))
        res = {"prefixes": prefixes}
        if rows is not None:
            res["subnets"] = rows
        start = {"ansible.builtin.set_fact": {"np_status": {"entity": "acme"}}}
        return run_play([start] + tasks + [write_out("np_subnets")],
                        {"fab_ipam_result": res, "np_backend_kind": kind})

    def test_first_host_on_cloudvirt_and_cloudoso(self):
        for kind in ("CloudVirt", "CloudOSO"):
            rows = self.subnets(kind, ["10.60.1.0/24", "2001:db8:1:7::/64"])
            self.assertEqual(rows, [{"family": 4, "prefix": "10.60.1.0/24", "source": "Network", "gateway": "10.60.1.1"},
                                    {"family": 6, "prefix": "2001:db8:1:7::/64", "source": "Network", "gateway": GW6}],
                             kind)
        # a longer prefix whose network address ends in a group
        rows = self.subnets("CloudVirt", ["2001:db8:1:7::100/120"])
        self.assertEqual(rows[0]["gateway"], "2001:db8:1:7::101")

    def test_no_ipv6_gateway_elsewhere_or_for_a_provider(self):
        self.assertNotIn("gateway", self.subnets("CloudAWS", ["2001:db8:1:7::/64"])[0])
        rows = self.subnets("CloudOSO", [], rows=[{"family": 6, "prefix": "2001:db8:1:7::/64", "source": "Provider"}])
        self.assertNotIn("gateway", rows[0])

    def test_cloudoso_takes_the_site_gateway(self):
        task = by_name(load(os.path.join(NP, "backend_cloudoso.yml")), "The IPv6 gateways the site assigned")
        rows = [{"family": 4, "prefix": "10.60.1.0/24", "gateway": "10.60.1.1", "source": "Network"},
                {"family": 6, "prefix": "2001:db8:1:7::/64", "gateway": GW6, "source": "Network"}]
        start = {"ansible.builtin.set_fact": {"np_subnets": rows, "np_status": {"subnets": rows}}}
        out = run_play([start, task, write_out("{'rows': np_subnets, 'st': np_status.subnets}")],
                       {"os_result": {"gateways": {"10.60.1.0/24": "10.60.1.254", "2001:db8:1:7::/64": "2001:DB8:1:7::FE"}}})
        self.assertEqual(out["rows"][0]["gateway"], "10.60.1.1")     # IPv4 unchanged
        self.assertEqual(out["rows"][1]["gateway"], "2001:db8:1:7::fe")
        self.assertEqual(out["st"], out["rows"])
        # none reported: the first host stays
        out = run_play([start, task, write_out("np_subnets")], {"os_result": {"gateways": {"2001:db8:1:7::/64": ""}}})
        self.assertEqual(out[1]["gateway"], GW6)

    def test_crd_declares_the_gateway(self):
        crd = load(os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-networkplacement.yaml"))
        st = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
        self.assertEqual(st["subnets"]["items"]["properties"]["gateway"]["type"], "string")
        self.assertTrue(st["realization"]["x-kubernetes-preserve-unknown-fields"])


# ---- the writer as a play against a fake helper ----
FAKE_HELPER = textwrap.dedent('''\
    import json, os, sys, types
    here = os.path.dirname(os.path.abspath(__file__))
    req = json.load(sys.stdin)
    req.pop("clouds_yaml", None)
    req.pop("cacert", None)
    with open(os.path.join(here, "calls.jsonl"), "a") as f:
        f.write(json.dumps(req) + "\\n")
    fail = {}
    if os.path.exists(os.path.join(here, "fail.json")):
        fail = json.load(open(os.path.join(here, "fail.json")))
    a = req["action"]
    if a in fail:
        print(json.dumps({"error": fail[a]}))
        sys.exit(1)
    if a == "ptr-name":
        sys.path.insert(0, %r)
        try:
            import openstack
        except ImportError:
            sys.modules["openstack"] = types.SimpleNamespace(exceptions=None, connect=None)
        import shc_dns
        print(json.dumps(shc_dns.ptr_name_action(req)))
    elif a == "ensure-parent":
        print(json.dumps({"zone_id": "id-" + req["name"], "zone": req["name"] + ".", "status": "ACTIVE",
                          "created": True, "nameservers": ["ns1.example.test."], "attributes": req.get("attributes") or {}}))
    else:
        print(json.dumps({"changed": True, "id": "rs-1"}))
    ''') % FILES


def run_tree(tasks, extra_vars, fail=None, siblings=None):
    """Run tasks with role_path in a tree whose common/tasks are the shipped files and whose common/files/shc_dns.py
    is the fake helper. Returns (written result, the helper's requests in order). siblings: {name: path} task files
    copied next to the tasks (relative includes)."""
    tmp = tempfile.mkdtemp(prefix="shc-rdns-test-")
    try:
        # role_path/../../../common is <tmp>/common
        role = os.path.join(tmp, "a", "b", "c")
        os.makedirs(role)
        shutil.copytree(os.path.join(COMMON, "tasks"), os.path.join(tmp, "common", "tasks"))
        shutil.copytree(os.path.join(COMMON, "vars"), os.path.join(tmp, "common", "vars"))
        files = os.path.join(tmp, "common", "files")
        os.makedirs(files)
        with open(os.path.join(files, "shc_dns.py"), "w") as f:
            f.write(FAKE_HELPER)
        if fail:
            with open(os.path.join(files, "fail.json"), "w") as f:
                json.dump(fail, f)
        play = os.path.join(tmp, "play")
        os.makedirs(play)
        for name, path in (siblings or {}).items():
            shutil.copy(path, os.path.join(play, name))
        out = os.path.join(tmp, "out.json")
        with open(os.path.join(play, "tasks.yml"), "w") as f:
            yaml.safe_dump(tasks, f)
        with open(os.path.join(play, "play.yml"), "w") as f:
            f.write("- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
                    "    - ansible.builtin.include_tasks: tasks.yml\n")
        with open(os.path.join(play, "vars.json"), "w") as f:
            json.dump(dict(extra_vars, out=out, role_path=role), f)
        with open(os.path.join(play, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(play, "ansible.cfg"), ANSIBLE_LOCALHOST_WARNING="false",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=play, env=env,
                              capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-2000:])
        with open(out) as f:
            result = json.load(f)
        calls = []
        if os.path.exists(os.path.join(files, "calls.jsonl")):
            with open(os.path.join(files, "calls.jsonl")) as f:
                calls = [json.loads(ln) for ln in f if ln.strip()]
        return result, calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CFG = {"zones": [ZONE48], "gatewayLabel": "gateways", "ttl": 600, "staleMinutes": 30}
INIT = {"name": "initial facts", "ansible.builtin.set_fact": {"rdns_names": [], "rdns_notes": []}}


def writer(publish=(), remove=(), fail=None, cfg=CFG, owner=OWNER):
    return run_tree([INIT, {"ansible.builtin.include_tasks": "{{ role_path }}/../../../common/tasks/reverse_dns_apply.yml"},
                     write_out("{'names': rdns_names, 'notes': rdns_notes}")],
                    {"_rdns_publish": list(publish), "_rdns_remove": list(remove), "rdns_cfg": cfg, "rdns_owner": owner,
                     "dnscfg_project_id": DNSP, "os_account_clouds_yaml": "/dev/null", "os_account_cacert": ""},
                    fail=fail)


NAME = {"name": "net1.gateways.env1.acme.example.test", "address": GW6, "zone": "env1.acme.example.test"}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Writer(unittest.TestCase):
    def test_publish_aaaa_then_ptr(self):
        out, calls = writer(publish=[NAME])
        # the TTL reaches the helper as a number on ansible-core 2.20 and as its text on 2.16 (a number inside a
        # nested mapping of task vars stays a string there); shc_dns.py casts it, so the value is what counts
        for c in calls[1:]:
            c["ttl"] = int(c["ttl"])
        self.assertEqual([c["action"] for c in calls], ["ptr-name", "ensure-record", "ensure-ptr"])
        self.assertEqual(calls[0]["zones"], [ZONE48])
        self.assertEqual(calls[1], {"action": "ensure-record", "project_id": DNSP, "zone": "env1.acme.example.test.",
                                    "name": NAME["name"] + ".", "type": "AAAA", "records": [GW6], "ttl": 600,
                                    "owner": OWNER})
        self.assertEqual(calls[2], {"action": "ensure-ptr", "project_id": DNSP, "address": GW6,
                                    "target": NAME["name"] + ".", "ttl": 600, "owner": OWNER})
        self.assertEqual(out, {"names": [{"name": NAME["name"] + ".", "address": GW6,
                                          "zone": "env1.acme.example.test."}], "notes": []})

    def test_an_address_no_zone_covers_gets_neither_record(self):
        out, calls = writer(publish=[dict(NAME, address="2001:db9::1")])
        self.assertEqual([c["action"] for c in calls], ["ptr-name"])
        self.assertEqual(out["names"], [])
        self.assertEqual(out["notes"], ["no PTR for 2001:db9::1: no platform reverse zone covers it"])

    def test_errors_are_notes(self):
        out, calls = writer(publish=[NAME], fail={"ensure-ptr": "RuntimeError: x is held by another owner"})
        self.assertEqual([c["action"] for c in calls], ["ptr-name", "ensure-record", "ensure-ptr"])
        # the AAAA exists: it is recorded, so a later run or the teardown removes it
        self.assertEqual(len(out["names"]), 1)
        self.assertEqual(out["notes"], ["PTR of %s: RuntimeError: x is held by another owner" % GW6])
        out, calls = writer(publish=[NAME], fail={"ensure-record": "RuntimeError: zone not found"})
        self.assertEqual([c["action"] for c in calls], ["ptr-name", "ensure-record"])
        self.assertEqual(out["names"], [])
        self.assertEqual(out["notes"], ["AAAA %s.: RuntimeError: zone not found" % NAME["name"]])

    def test_remove_ptr_then_aaaa(self):
        out, calls = writer(remove=[NAME])
        self.assertEqual([c["action"] for c in calls], ["delete-ptr", "delete-record"])
        self.assertEqual(calls[0], {"action": "delete-ptr", "project_id": DNSP, "address": GW6, "owner": OWNER})
        self.assertEqual(calls[1], {"action": "delete-record", "project_id": DNSP, "zone": "env1.acme.example.test.",
                                    "name": NAME["name"] + ".", "type": "AAAA", "owner": OWNER})
        self.assertEqual(out, {"names": [], "notes": []})
        # a failed removal keeps the name for a retry, and the AAAA is still tried
        out, calls = writer(remove=[NAME], fail={"delete-ptr": "HttpException: 503"})
        self.assertEqual([c["action"] for c in calls], ["delete-ptr", "delete-record"])
        self.assertEqual(out["names"], [{"name": NAME["name"] + ".", "address": GW6, "zone": "env1.acme.example.test."}])
        self.assertEqual(out["notes"], ["PTR of %s: HttpException: 503" % GW6])

    def test_removals_run_before_publications(self):
        other = dict(NAME, address="2001:db8:1:8::1")
        _, calls = writer(publish=[NAME], remove=[other])
        self.assertEqual([c["action"] for c in calls], ["delete-ptr", "delete-record", "ptr-name", "ensure-record", "ensure-ptr"])
        self.assertEqual(calls[0]["address"], "2001:db8:1:8::1")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class WhatToWrite(unittest.TestCase):
    TASK = "What to publish and what to remove"

    def plan(self, want, prev, cfg=CFG):
        t = by_name(load(os.path.join(COMMON, "tasks", "reverse_dns.yml")), self.TASK)
        return run_play([t, write_out("{'publish': _rdns_publish, 'remove': _rdns_remove, 'names': rdns_names}")],
                        {"rdns_cfg": cfg, "rdns_want": want, "rdns_prev": prev})

    def test_plan(self):
        prev = [dict(NAME, name=NAME["name"] + "."), dict(NAME, address="2001:db8:1:8::1")]
        out = self.plan([NAME], prev)
        self.assertEqual(out["publish"], [NAME])
        self.assertEqual(out["remove"], [prev[1]])           # the same name with its old address goes
        self.assertEqual(out["names"], [])
        # without reverse zones nothing is published and every recorded name goes
        out = self.plan([NAME], prev, cfg={"zones": []})
        self.assertEqual((out["publish"], out["remove"]), ([], prev))

    def test_skipped_without_zones_and_names(self):
        tasks = load(os.path.join(COMMON, "tasks", "reverse_dns.yml"))
        out = run_play(tasks + [write_out("{'names': rdns_names, 'notes': rdns_notes}")],
                       {"rdns_cfg": {"zones": []}, "rdns_want": [NAME], "rdns_prev": [], "rdns_owner": OWNER,
                        "rdns_tag": "t", "role_path": "/nonexistent/a/b"})
        self.assertEqual(out, {"names": [], "notes": []})


# ---- the placement's gateway name ----
def real(**dns):
    r = load(DEFAULTS)["fabric_realization_defaults"]
    r["dns"]["reverse6"].update(dns)
    return r


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementName(unittest.TestCase):
    TASK = "The gateway name the placement wants"

    def want(self, backend_status, zones=(ZONE48,), subnets=None, st=None, network="Net.One"):
        t = by_name(load(os.path.join(NP, "gateway_name.yml")), self.TASK)
        return run_play([t, write_out("{'want': np_rdns_want, 'notes': np_rdns_pre_notes, 'prev': np_rdns_prev, "
                                      "'dnscfg': np_rdns_dnscfg}")],
                        {"fab_real": real(zones=list(zones)), "fab_st": st or {}, "fab_spec": {"network": network},
                         "np_backend": {"status": backend_status},
                         "np_subnets": subnets if subnets is not None else
                         [{"family": 4, "prefix": "10.60.1.0/24", "gateway": "10.60.1.1"},
                          {"family": 6, "prefix": "2001:db8:1:7::/64", "gateway": GW6, "source": "Network"}]})

    def test_cloudvirt_zone(self):
        out = self.want({"dns": {"zone": "env1.acme.example.test.", "owner": DNSP, "mode": "platform"}})
        self.assertEqual(out["want"], [{"name": "net-one.gateways.env1.acme.example.test", "address": GW6,
                                        "zone": "env1.acme.example.test"}])
        self.assertEqual(out["notes"], [])

    def test_cloudoso_zone_and_recorded_dnsconfig(self):
        out = self.want({"dns": {"zone": "env1.acme.example.test.", "ownerProjectId": DNSP, "dnsConfig": "tenant-dns"}})
        self.assertEqual(out["want"][0]["name"], "net-one.gateways.env1.acme.example.test")
        self.assertEqual(out["dnscfg"], "tenant-dns")
        st = {"realization": {"reverseDns": {"dnsConfig": "older", "names": [NAME]}}}
        out = self.want({"dns": {"zone": "env1.acme.example.test.", "dnsConfig": "tenant-dns"}}, st=st)
        self.assertEqual((out["dnscfg"], out["prev"]), ("older", [NAME]))

    def test_no_zone_no_gateway_no_reverse_zones(self):
        out = self.want({})
        self.assertEqual(out["want"], [])
        self.assertEqual(out["notes"], ["no gateway name: the environment has no DNS zone"])
        self.assertEqual(self.want({"dns": {"zone": "e.example.test"}}, zones=())["want"], [])
        self.assertEqual(self.want({}, zones=())["notes"], [])
        self.assertEqual(self.want({"dns": {"zone": "e.example.test"}},
                                   subnets=[{"family": 4, "prefix": "10.60.1.0/24", "gateway": "10.60.1.1"}])["want"], [])

    def test_wiring_and_record(self):
        main = load(os.path.join(NP, "main.yml"))
        names = [t.get("name") for t in walk(main)]
        self.assertLess(names.index("Check the gateway's routes"), names.index("The IPv6 gateway's name"))
        self.assertEqual(names.index("The IPv6 gateway's name") + 1, names.index("Set the final status"))
        self.assertIn("np_rdns_record", by_name(main, "Set the final status")["ansible.builtin.set_fact"]["np_final"])
        self.assertIn("np_rdns_note", by_name(main, "Report the placement ready")["vars"]["fab_message"])
        inc = by_name(load(os.path.join(NP, "gateway_name.yml")), "Publish the gateway name")
        self.assertEqual(inc["vars"]["rdns_owner"], "{{ np_owner }}")
        rec = by_name(load(os.path.join(NP, "gateway_name.yml")), "Record the gateway name")
        out = run_play([rec, write_out("{'note': np_rdns_note, 'rec': np_rdns_record}")],
                       {"np_rdns_pre_notes": [], "rdns_notes": ["PTR of x: y"], "rdns_names": [NAME],
                        "rdns_dnsconfig_name": "tenant-dns", "np_rdns_prev": []})
        self.assertEqual(out, {"note": "; reverse DNS: PTR of x: y",
                               "rec": {"reverseDns": {"dnsConfig": "tenant-dns", "names": [NAME]}}})
        out = run_play([rec, write_out("np_rdns_record")], {"np_rdns_pre_notes": [], "rdns_notes": [], "rdns_names": [],
                                                            "rdns_dnsconfig_name": "", "np_rdns_prev": []})
        self.assertEqual(out, {})


# ---- the namespace annotation ----
class NamespaceAnnotation(unittest.TestCase):
    def test_wiring(self):
        inc = by_name(load(os.path.join(NP, "backend_cloudvirt.yml")), "The placement's IPv6 prefixes on its namespaces")
        self.assertEqual(inc["ansible.builtin.include_tasks"], "namespace_prefixes.yml")
        self.assertEqual(inc["vars"]["npp_value"], "{{ np_prefixes_v6 | default([], true) | join(',') }}")
        tasks = load(os.path.join(NP, "namespace_prefixes.yml"))
        for t in walk(tasks):
            if "kubernetes.core.k8s" in t:
                self.assertEqual(t["kubernetes.core.k8s"]["state"], "patched")
                self.assertIn("hybridsovereign.redhat/ipv6-prefixes", t["kubernetes.core.k8s"]["definition"])
                self.assertTrue(t["ignore_errors"])
        td = by_name(load(os.path.join(NPD, "main.yml")), "Remove the placement's IPv6 prefixes from its namespaces")
        self.assertEqual(td["vars"]["npp_set"], [])
        self.assertTrue(td["ansible.builtin.include_tasks"].endswith("networkplacement_provision/tasks/namespace_prefixes.yml"))

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_only_this_placements_value_is_removed(self):
        tasks = load(os.path.join(NP, "namespace_prefixes.yml"))
        clear = by_name(tasks, "Remove this placement's IPv6 prefixes from them")
        probe = {"name": "the namespaces to clear", "ansible.builtin.set_fact": {"result_names": clear["loop"]}}

        def ns(name, value):
            return {"metadata": {"name": name, "annotations": {"hybridsovereign.redhat/ipv6-prefixes": value}
                                 if value is not None else {}}}
        reads = {"results": [{"item": "a", "resources": [ns("a", "2001:db8:1:7::/64")]},
                             {"item": "b", "resources": [ns("b", "2001:db8:1:9::/64")]},
                             {"item": "c", "resources": [ns("c", None)]},
                             {"item": "d", "resources": []}]}
        out = run_play([probe, write_out("result_names")],
                       {"_npp_read": reads, "npp_owned": ["2001:db8:1:7::/64"]})
        self.assertEqual(out, ["a"])


# ---- the gateway's node names ----
@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayNames(unittest.TestCase):
    def names(self, nodes, zones=(ZONE48,), usable=True, st=None):
        tasks = load(os.path.join(CG, "reverse_dns.yml"))
        ts = [by_name(tasks, "The names the gateway wants"), by_name(tasks, "Name every node's ICMPv6 source")]
        return run_play(ts + [write_out("{'want': cg_rdns_want, 'prev': cg_rdns_prev}")],
                        {"fab_real": real(zones=list(zones)), "fab_st": st or {}, "fab_name": "gw-site.a",
                         "cg_rdns_nodes": nodes, "dnscfg_usable": usable, "dnscfg_domain": "tenants.example.test"})

    def test_names(self):
        nodes = [{"node": "bgw-1.example.test", "address": "2001:db8:ffff:1::1:2"},
                 {"node": "BGW_2", "address": "2001:db8:ffff:2::1:2"}]
        out = self.names(nodes)
        self.assertEqual(out["want"], [
            {"name": "bgw-1.gw-site-a.gateways.tenants.example.test", "address": "2001:db8:ffff:1::1:2",
             "zone": "tenants.example.test"},
            {"name": "bgw-2.gw-site-a.gateways.tenants.example.test", "address": "2001:db8:ffff:2::1:2",
             "zone": "tenants.example.test"}])

    def test_silent_without_zones_or_sources(self):
        self.assertEqual(self.names([{"node": "n1", "address": "2001:db8::1"}], zones=())["want"], [])
        self.assertEqual(self.names([])["want"], [])
        tasks = load(os.path.join(CG, "reverse_dns.yml"))
        for name in ("Publish the names", "Record the names"):
            self.assertEqual(by_name(tasks, name)["when"], "cg_rdns_want | length > 0 or cg_rdns_prev | length > 0")

    def test_node_sources_and_wiring(self):
        node = load(os.path.join(CG, "node_gateway.yml"))
        names = [t.get("name") for t in walk(node)]
        i = names.index("{{ bgw_node.name }}: remember the ICMPv6 source for its name")
        self.assertLess(names.index("{{ bgw_node.name }}: stop on an ICMPv6 source that does not fit"), i)
        t = node[i]
        out = run_play([{"ansible.builtin.set_fact": {"cg_rdns_nodes": []}}, t, write_out("cg_rdns_nodes")],
                       {"bgw_node": {"name": "n1"}, "cg_ipv6_core": True,
                        "fab_real": {"core": {"icmpSource6": "2001:db8:ffff::/48"}},
                        "fab_check_result": {"icmp6": "2001:db8:ffff:1::1:2/128"}})
        self.assertEqual(out, [{"node": "n1", "address": "2001:db8:ffff:1::1:2"}])
        main = load(os.path.join(CG, "main.yml"))
        names = [t.get("name") for t in walk(main)]
        self.assertLess(names.index("Configure the gateway instance on each node"), names.index("Name the nodes' ICMPv6 sources"))
        self.assertLess(names.index("Name the nodes' ICMPv6 sources"), names.index("Report the gateway ready"))
        ready = by_name(main, "Report the gateway ready")["vars"]
        self.assertIn("cg_rdns_record", ready["fab_status"])
        self.assertIn("cg_rdns_note", ready["fab_message"])
        inc = by_name(load(os.path.join(CG, "reverse_dns.yml")), "Publish the names")
        self.assertEqual(inc["vars"]["rdns_owner"], "CloudGateway/{{ fab_name }}")

    def test_cloud_routers_sources_join_the_nodes(self):
        # the AWS and Google Cloud routers' ICMPv6 sources (site_*_post.yml) are named like the nodes' sources
        nodes = [{"node": "n1", "address": "2001:db8:ffff:1::1:2"}]
        reg = {"results": [
            {"item": {"name": "shc-edge-site-a", "instance": "i-1"}, "stdout": json.dumps({"icmp6": "2001:db8:ffff:1::64:2/128"})},
            {"item": {"name": "shc-edge-site-b", "instance": "i-2"}, "stdout": json.dumps({"errors": ["does not fit"]})},
            {"item": {"name": "shc-edge-site-c"}, "skipped": True}]}
        for f, var, extra in (("site_aws_post.yml", "_cg_aws_icmp6", {"aws_edge_cfg": {"ipv6": {"enabled": True}}}),
                              ("site_gcp_post.yml", "_cg_gcp_icmp6", {})):
            t = by_name(load(os.path.join(CG, f)), "Remember the routers' ICMPv6 sources for their names")
            # (an extra var can't be overridden by set_fact: the nodes so far come in through a set_fact)
            init = {"ansible.builtin.set_fact": {"cg_rdns_nodes": "{{ _nodes }}"}}
            out = run_play([init, t, write_out("cg_rdns_nodes")], dict({"_nodes": nodes, var: reg}, **extra))
            self.assertEqual(out, nodes + [{"node": "shc-edge-site-a", "address": "2001:db8:ffff:1::64:2"}], f)
            # named as the nodes are
            want = self.names(out)["want"]
            self.assertEqual(want[-1]["name"], "shc-edge-site-a.gw-site-a.gateways.tenants.example.test")
        # AWS: not while the routers' IPv6 side is only being removed
        t = by_name(load(os.path.join(CG, "site_aws_post.yml")), "Remember the routers' ICMPv6 sources for their names")
        init = {"ansible.builtin.set_fact": {"cg_rdns_nodes": "{{ _nodes }}"}}
        out = run_play([init, t, write_out("cg_rdns_nodes")], {"_nodes": nodes, "_cg_aws_icmp6": reg,
                                                                 "aws_edge_cfg": {"ipv6": {"enabled": False}}})
        self.assertEqual(out, nodes)
        # the routers are named after the nodes are known and before the names are published
        main = [t.get("name") for t in walk(load(os.path.join(CG, "main.yml")))]
        for post in ("Give the platform-run edge the nodes' keys and the site key (aws)",
                     "Give the platform-run edge the nodes' keys and the site key (gcp)"):
            self.assertLess(main.index("Configure the gateway instance on each node"), main.index(post))
            self.assertLess(main.index(post), main.index("Name the nodes' ICMPv6 sources"))


# ---- the site job's reverse zones ----
@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SiteReverseZones(unittest.TestCase):
    def run_zones(self, zones, here=True, prev=None, fail=None):
        tasks = load(os.path.join(CI, "reverse_zones.yml"))
        rec = by_name(tasks, "Record the reverse zones of this site")
        rec["vars"] = dict(rec.get("vars") or {}, rec_status=rec.pop("hybridsovereign.shc.cr_status")["status"])
        rec["ansible.builtin.set_fact"] = {"written": "{{ rec_status }}"}
        rec.pop("failed_when", None)
        init = {"ansible.builtin.set_fact": {"ci_notes": [], "written": None}}
        status = {"realization": {"dns": {"reverseZones": prev}}} if prev is not None else {}
        return run_tree([init] + tasks + [write_out("{'zones': ci_reverse_zones, 'written': written, 'notes': ci_notes}")],
                        {"fabric_realization": {"dns": {"reverse6": {"zones": zones}}},
                         "ci_dns_native": True, "dnscfg_usable": True, "dnscfg_domain": "tenants.example.test",
                         "dnscfg_zone_id": "z-global", "ci_dns_parent": {"zone_id": "z-global" if here else "z-local"},
                         "ci_probe": {"dns_project": {"id": DNSP}}, "ci_dns": {"zoneAttributes": {"scope": "public"}},
                         "cloudinfrastructure_cr_lookup": {"resources": [{"status": status}]},
                         "cloudinfrastructure_cr_name": "site-a", "cloudinfrastructure_cr_namespace": "platform",
                         "os_account_clouds_yaml": "/dev/null"},
                        fail=fail, siblings={"reverse_zone.yml": os.path.join(CI, "reverse_zone.yml")})

    def test_zones_on_the_global_site(self):
        (out, calls) = self.run_zones([ZONE48 + ".", "Not-A-Zone.example.test"])
        self.assertEqual(calls, [{"action": "ensure-parent", "owner_project_id": DNSP, "name": ZONE48,
                                  "email": "hostmaster@tenants.example.test",
                                  "description": "Reverse DNS of the sovereign cloud platform",
                                  "attributes": {"scope": "public"}}])
        rows = [{"name": ZONE48 + ".", "id": "id-" + ZONE48, "nameservers": ["ns1.example.test."]},
                {"name": "not-a-zone.example.test.",
                 "error": "not a reverse zone under ip6.arpa (one hexadecimal digit per label)"}]
        self.assertEqual(out["zones"], rows)
        self.assertEqual(out["written"], {"realization": {"dns": {"reverseZones": rows}}})
        self.assertIn("created reverse zone " + ZONE48 + ".", out["notes"])

    def test_error_recorded_and_unchanged_not_written(self):
        out, _ = self.run_zones([ZONE48], fail={"ensure-parent": "RuntimeError: held by another project"})
        self.assertEqual(out["zones"], [{"name": ZONE48 + ".", "error": "RuntimeError: held by another project"}])
        rows = [{"name": ZONE48 + ".", "id": "id-" + ZONE48, "nameservers": ["ns1.example.test."]}]
        out, _ = self.run_zones([ZONE48], prev=rows)
        self.assertIsNone(out["written"])

    def test_other_sites_and_no_zones(self):
        out, calls = self.run_zones([ZONE48], here=False)
        self.assertEqual((calls, out["written"]), ([], None))
        # a site that recorded zones before and no longer hosts them loses the record
        out, calls = self.run_zones([ZONE48], here=False, prev=[{"name": ZONE48 + ".", "id": "x"}])
        self.assertEqual(out["written"], {"realization": {"dns": {"reverseZones": None}}})
        out, calls = self.run_zones([])
        self.assertEqual((calls, out["written"]), ([], None))

    def test_wiring(self):
        tasks = load(os.path.join(CI, "openstack.yml"))
        names = [t.get("name") for t in tasks]
        self.assertEqual(names.index("Keep the parent zone result") + 1,
                         names.index("Ensure the platform's parent zone in the DNS project") + 2)
        self.assertEqual(names[names.index("Keep the parent zone result") + 1],
                         "Ensure the platform's reverse zones in the DNS project")
        rec = by_name(load(os.path.join(CI, "reverse_zones.yml")), "Record the reverse zones of this site")
        self.assertTrue(rec["hybridsovereign.shc.cr_status"]["partial"])
        self.assertFalse(rec["failed_when"])

    def test_realization_defaults(self):
        self.assertEqual(load(DEFAULTS)["fabric_realization_defaults"]["dns"],
                         {"reverse6": {"zones": [], "gatewayLabel": "gateways", "ttl": 3600, "staleMinutes": 30}})


# ---- teardown order ----
class TeardownOrder(unittest.TestCase):
    def test_placement_names_go_before_the_subnets(self):
        names = [t.get("name") for t in walk(load(os.path.join(NPD, "main.yml")))]
        rm = names.index("Remove the IPv6 gateway's names")
        self.assertLess(names.index("Release the gateway"), rm)
        self.assertLess(names.index("Remove the site side"), rm)
        self.assertLess(rm, names.index("Wait while a gateway name could not be removed"))
        self.assertLess(names.index("Wait while a gateway name could not be removed"), names.index("Release the VNIs"))
        self.assertLess(rm, names.index("Release the subnets"))
        self.assertLess(names.index("Remove the placement's IPv6 prefixes from its namespaces"),
                        names.index("Delete the RouteAdvertisements and the network"))
        t = by_name(load(os.path.join(NPD, "main.yml")), "Remove the IPv6 gateway's names")
        self.assertEqual(t["vars"]["rdns_want"], [])
        self.assertEqual(t["vars"]["rdns_owner"], "{{ npd_owner }}")
        # the wait is a "Deletion waits for" failure: the rescue reports it pending, not failed
        wait = by_name(load(os.path.join(NPD, "main.yml")), "Wait while a gateway name could not be removed")
        self.assertTrue(wait["ansible.builtin.fail"]["msg"].startswith("Deletion waits for "))

    def test_ptr_before_aaaa_in_the_writer(self):
        name = load(os.path.join(COMMON, "tasks", "reverse_dns_name.yml"))
        remove = by_name(name, "{{ rdns_item.name }}: remove")["block"]
        actions = [t["vars"]["os_request"]["action"] for t in remove if "vars" in t]
        self.assertEqual(actions, ["delete-ptr", "delete-record"])
        publish = by_name(name, "{{ rdns_item.name }}: publish")["block"]
        actions = [t["vars"]["os_request"]["action"] for t in publish if "vars" in t]
        self.assertEqual(actions, ["ptr-name", "ensure-record", "ensure-ptr"])
        apply = [t["name"] for t in load(os.path.join(COMMON, "tasks", "reverse_dns_apply.yml"))]
        self.assertEqual(apply, ["Remove the names no longer wanted", "Publish the wanted names"])

    def test_gateway_names_go_after_the_nodes(self):
        names = [t.get("name") for t in walk(load(os.path.join(CGD, "main.yml")))]
        self.assertLess(names.index("Remove the gateway instance from each node"),
                        names.index("Remove the names of the nodes' ICMPv6 sources"))
        self.assertLess(names.index("Remove the names of the nodes' ICMPv6 sources"), names.index("Mark deletion complete"))
        t = by_name(load(os.path.join(CGD, "main.yml")), "Remove the names of the nodes' ICMPv6 sources")
        self.assertEqual(t["vars"]["rdns_owner"], "CloudGateway/{{ fab_name }}")


if __name__ == "__main__":
    unittest.main()

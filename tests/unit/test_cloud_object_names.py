"""Names of the objects the platform makes on AWS and Google Cloud, and the refusal of an object of the wanted name
that the platform did not make.

Google Cloud: a placement's VPC is shc-<tenant>--<placement> from the names function, cut with a derived ending so
that every ending the helper appends still fits Google's 63 characters; an existing placement keeps the name its
status records (status.realization.site.vpc). The environment's Cloud DNS zone is named by the function too. A VPC,
zone or external VPN gateway of the wanted name without the platform's mark is refused (NameTakenOnSite), never
taken over and never deleted; external VPN gateways are deleted by name and label, never by a name's beginning.

Example names are made up: tenants acme-corp / acme, placements core-prod / corp-core-prod (the two-tenant pair).
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
NP = os.path.join(ROLES, "networkplacement_provision")
sys.path.insert(0, FILES)
import shc_names  # noqa: E402
import test_gcp_tenant_ipv6 as V  # noqa: E402
import shc_gcp_tenant as T  # noqa: E402

GCP_LIMIT = 63          # Compute Engine names: 1-63, ^[a-z]([-a-z0-9]*[a-z0-9])? (naming-resources)
GCP_NAME = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


def read(path):
    with open(path) as f:
        return f.read()


def helper_endings():
    """Every ending shc_gcp_tenant.py appends to a placement's VPC name n (n + "-x", "%s-t%d" % (n, i))."""
    src = read(os.path.join(FILES, "shc_gcp_tenant.py"))
    ends = set(re.findall(r'\bn \+ "(-[a-z0-9]+)"', src))
    ends |= {"-t%d" % i for i in range(2)} if '"%s-t%d" % (n, i)' in src else set()
    ends |= set(re.findall(r'"%s(-[a-z0-9]+)" % n', src))
    return ends


class GcpPlacementName(unittest.TestCase):
    def test_the_room_covers_every_ending_the_helper_appends(self):
        ends = helper_endings()
        self.assertIn("-default6", ends)
        self.assertIn("-fabric6", ends)
        reserve = shc_names.KINDS["gcp-placement"][3]
        self.assertEqual(reserve, max(len(e) for e in ends), sorted(ends))

    def test_every_name_and_ending_fits_google(self):
        for tenant, placement in [("acme", "p1"), ("acme-corp", "core-prod"), ("t" * 30, "p" * 30),
                                  ("a" + "-b" * 14 + "c", "x" * 30)]:
            vpc = shc_names.name("gcp-placement", [tenant, placement])["name"]
            for end in helper_endings() | {""}:
                self.assertLessEqual(len(vpc + end), GCP_LIMIT, (vpc, end))
                self.assertRegex(vpc + end, GCP_NAME)

    def test_the_two_tenant_pair_gets_two_names(self):
        a = shc_names.name("gcp-placement", ["acme-corp", "web"])["name"]
        b = shc_names.name("gcp-placement", ["acme", "corp-web"])["name"]
        self.assertEqual((a, b), ("shc-acme-corp--web", "shc-acme--corp-web"))

    def test_long_names_are_cut_with_a_derived_ending(self):
        a = shc_names.name("gcp-placement", ["t" * 30, "p" * 30])
        b = shc_names.name("gcp-placement", ["t" * 30, "p" * 29 + "q"])
        self.assertFalse(a["exact"])
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual(len(a["name"]), GCP_LIMIT - 9)

    def test_no_silent_cut_is_left(self):
        for path in (os.path.join(NP, "tasks", "backend_cloudgcp.yml"), os.path.join(FILES, "shc_gcp_tenant.py")):
            text = read(path)
            self.assertNotIn("truncate(", text, path)
            self.assertNotIn("[:55]", text, path)


class GcpDnsZoneName(unittest.TestCase):
    def test_zone_names(self):
        self.assertEqual(shc_names.name("gcp-dns-zone", ["acme-corp", "prod"])["name"], "shc-acme-corp--prod")
        self.assertNotEqual(shc_names.name("gcp-dns-zone", ["acme-corp", "web"])["name"],
                            shc_names.name("gcp-dns-zone", ["acme", "corp-web"])["name"])
        long = shc_names.name("gcp-dns-zone", ["t" * 30, "e" * 30])["name"]
        self.assertLessEqual(len(long), 63)        # Cloud DNS managed zone name: 1-63, letter first, [a-z0-9-]
        self.assertRegex(long, GCP_NAME)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GcpPlacementTasks(unittest.TestCase):
    """The backend's naming steps as the job runs them: the rule for names, then the VPC name from status or the
    names function, then the labels."""
    FIRST, LAST = "Check the names the placement's cloud objects carry", "The environment's project and the placement's names"

    def steps(self):
        tasks = load(os.path.join(NP, "tasks", "backend_cloudgcp.yml"))
        names = [t.get("name") for t in tasks]
        part = tasks[names.index(self.FIRST): names.index(self.LAST) + 1]
        return [{"block": part, "rescue": [{"ansible.builtin.set_fact": {"_refused": "{{ fab_fail_reason }}: {{ fab_fail_msg }}"}}]}]

    def run_names(self, cases):
        base = {"role_path": NP, "np_gateway": {"metadata": {"name": "gcp-a"}, "status": {}}, "np_asn": 65010,
                "np_backend": {"status": {"project": "p", "region": "r"}}, "gcp_real": {}}
        steps = [{"ansible.builtin.set_fact": {"fab_entity": "{{ case.tenant }}", "fab_name": "{{ case.placement }}",
                                               "np_backend_name": "{{ case.environment }}",
                                               "fab_spec": {"network": "{{ case.network }}"},
                                               "fab_st": "{{ case.status }}", "_refused": "",
                                               "np_gcp_vpc": "", "fab_fail_reason": "", "fab_fail_msg": ""}}] + self.steps()
        return run_cases(steps, cases, "{'vpc': np_gcp_vpc, 'labels': (np_gcp_cfg | default({})).labels | default({}),"
                                       " 'refused': _refused}", extra_vars=base)

    def test_states(self):
        ok = {"tenant": "acme-corp", "environment": "prod", "network": "core", "placement": "core-prod", "status": {}}
        got = self.run_names([
            ok,                                                                                  # new: the function's
            dict(ok, status={"realization": {"site": {"vpc": "shc-entity-acme-corp-core-prod"}}}),  # recorded: kept
            dict(ok, tenant="acme", placement="corp-core-prod"),                                 # the pair's other half
            dict(ok, placement="core.prod"),                                                     # made before the rule
            dict(ok, network="n" * 31),
        ])
        self.assertEqual(got[0]["vpc"], "shc-acme-corp--core-prod")
        self.assertEqual(got[0]["labels"], {"shc-entity": "acme-corp", "shc-environment": "prod", "shc-network": "core",
                                            "shc-placement": "core-prod"})
        self.assertEqual(got[0]["refused"], "")
        self.assertEqual(got[1]["vpc"], "shc-entity-acme-corp-core-prod")
        self.assertEqual(got[2]["vpc"], "shc-acme--corp-core-prod")
        self.assertTrue(got[3]["refused"].startswith("NameNotAllowed: NetworkPlacement core.prod: The placement name"))
        self.assertIn("NameNotAllowed: NetworkPlacement core-prod: The network name", got[4]["refused"])
        for g in got[:3]:
            for v in g["labels"].values():                     # Google: label values 0-63 of [a-z0-9_-]
                self.assertRegex(v, r"^[a-z0-9_-]{0,63}$")


# ---- refuse, do not adopt (Google Cloud placement and zone) -------------------------------------------------------

class FakeDns:
    """Cloud DNS managed zones of one project: GET by name, list, create, delete."""

    def __init__(self, zones=()):
        self.zones = {z["name"]: dict(z) for z in zones}
        self.calls = []

    def http(self, method, url, body=None, token=None, ok404=False, **_):
        path = url.split("/managedZones", 1)[1] if "/managedZones" in url else url
        self.calls.append((method, path))
        if path == "" and method == "POST":
            z = dict(body, nameServers=["ns-a.example.", "ns-b.example."])
            self.zones[body["name"]] = z
            return z
        name = path.strip("/").split("/")[0] if path else ""
        if "/rrsets" in path or "/changes" in path:
            return {}
        if method == "GET":
            if name in self.zones:
                return self.zones[name]
            if ok404:
                return None
            raise AssertionError("404 " + path)
        if method == "DELETE":
            del self.zones[name]
            return {}
        raise AssertionError((method, path))

    def list_all(self, url, key, tok, params=None):
        if "/rrsets" in url:
            return []
        return list(self.zones.values())


FQDN = "prod.acme-corp.tenants.example.com"
ZONE = "shc-acme-corp--prod"


def zone(name=ZONE, dns=FQDN + ".", labels=None):
    return {"name": name, "dnsName": dns, "visibility": "public", "labels": labels if labels is not None else
            {"shc-managed": "true", "shc-entity": "acme-corp"}, "nameServers": ["ns-a.example."]}


class ZoneOwnership(unittest.TestCase):
    """exists ours / exists unmarked / exists another tenant's / absent, found by the recorded name or by DNS name."""

    def run_zone(self, action, zones, name=ZONE):
        f = FakeDns(zones)
        t = T.Tenant.__new__(T.Tenant)
        t.project, t.tok, t.changed = "p", "tok", []
        t.labels = {"shc-managed": "true", "shc-entity": "acme-corp", "shc-environment": "prod"}
        buf = io.StringIO()
        with mock.patch.object(T.g, "http", f.http), mock.patch.object(T.g, "list_all", f.list_all), \
                contextlib.redirect_stdout(buf):
            try:
                out = getattr(t, action)(FQDN, name)
            except SystemExit:
                return {"refused": json.loads(buf.getvalue())}, f
        return out, f

    def test_up_states(self):
        out, f = self.run_zone("zone_up", [])                                   # absent: made, with our name
        self.assertEqual(out["zoneId"], ZONE)
        self.assertEqual(f.zones[ZONE]["labels"]["shc-entity"], "acme-corp")
        out, f = self.run_zone("zone_up", [zone()])                             # ours: used as it is
        self.assertEqual(out["zoneId"], ZONE)
        self.assertNotIn(("POST", ""), f.calls)
        out, f = self.run_zone("zone_up", [zone(name="shc-prod-acme-corp-tenants-example-com")], name=ZONE)
        self.assertEqual(out["zoneId"], "shc-prod-acme-corp-tenants-example-com")   # ours under an older name
        for zones in ([zone(labels={})], [zone(name="other", labels={})],             # unmarked, by name / DNS name
                      [zone(labels={"shc-managed": "true", "shc-entity": "acme"})],   # another tenant's
                      [zone(dns="elsewhere.example.com.")]):                          # our name, another DNS name
            out, f = self.run_zone("zone_up", zones)
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", zones)
            self.assertNotIn("acme.", out["refused"]["error"].replace("acme-corp", ""))  # never the other tenant
            self.assertEqual([c for c in f.calls if c[0] != "GET"], [], zones)          # untouched

    def test_up_needs_the_name(self):
        out, _ = self.run_zone("zone_up", [], name=None)
        self.assertIn("--zone-name", out["refused"]["error"])

    def test_down_states(self):
        out, f = self.run_zone("zone_down", [zone()])
        self.assertTrue(out["deleted"])
        self.assertEqual(f.zones, {})
        out, f = self.run_zone("zone_down", [])
        self.assertFalse(out["deleted"])
        for zones in ([zone(labels={})], [zone(labels={"shc-managed": "true", "shc-entity": "acme"})]):
            out, f = self.run_zone("zone_down", zones)
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
            self.assertEqual(len(f.zones), 1)


class PlacementOwnership(unittest.TestCase):
    """The placement's VPC and the site's external VPN gateway: exists ours / unmarked / another site's / absent."""

    def setUp(self):
        self.f = V.FakeCompute()
        self.psk = mock.patch.object(T.secrets, "token_urlsafe", lambda n: "psk")
        self.psk.start()

    def tearDown(self):
        self.psk.stop()

    def call(self, fn, *a, **kw):
        t = V.tenant(self.f)
        buf = io.StringIO()
        with V.fake(self.f), contextlib.redirect_stdout(buf), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHC_EDGE_VPN_TUNNELS_FILE", None)
            try:
                return getattr(t, fn)(*a, **kw)
            except SystemExit:
                return {"refused": json.loads(buf.getvalue())}

    def up(self, vpc="shc-acme--p1"):
        return self.call("place_up", "p1", V.VNI, V.V4, V.EDGE, "fabric", None, None, vpc=vpc)

    def test_vpc_states(self):
        out = self.up()                                                          # absent: made under the given name
        self.assertEqual(out["vpc"], "shc-acme--p1")
        self.assertTrue(self.f.networks["shc-acme--p1"]["description"].startswith(T.VPC_MARK))
        self.assertEqual(self.up()["vpc"], "shc-acme--p1")                       # ours: used again
        self.f.networks["shc-other"] = {"name": "shc-other", "mtu": 1370, "description": "made by hand",
                                        "selfLink": self.f.link("/global/networks/shc-other")}
        before = len(self.f.mutations())
        out = self.up("shc-other")                                               # unmarked: refused, untouched
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
        self.assertEqual(len(self.f.mutations()), before)
        out = self.call("place_down", "x", vpc="shc-other")                      # and never emptied
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
        self.assertIn("shc-other", self.f.networks)

    def test_external_gateway_states(self):
        ext = "shc-edge-" + V.EDGE["site"]
        self.up()
        self.assertEqual(self.f.externals[ext]["labels"], {"shc-managed": "true", "shc-edge-site": V.EDGE["site"]})
        for labels in ({}, {"shc-managed": "true", "shc-edge-site": "another-site"}):    # unmarked / another site's
            f = V.FakeCompute()
            f.externals[ext] = {"name": ext, "labels": labels, "interfaces": [{"id": 0, "ipAddress": "198.51.100.11"},
                                                                              {"id": 1, "ipAddress": "198.51.100.12"}],
                                "selfLink": f.link("/global/externalVpnGateways/" + ext)}
            self.f = f
            out = self.up()
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", labels)
            self.assertEqual(self.f.externals[ext]["labels"], labels)

    def test_delete_by_name_and_label_never_by_prefix(self):
        self.up()
        ext = "shc-edge-" + V.EDGE["site"]
        # a gateway the platform did not make, with the old prefix: left alone
        self.f.externals["shc-edge-by-hand"] = {"name": "shc-edge-by-hand", "labels": {},
                                                "selfLink": self.f.link("/global/externalVpnGateways/shc-edge-by-hand")}
        out = self.call("place_down", "p1", vpc="shc-acme--p1")
        self.assertTrue(out["deleted"])
        self.assertNotIn(ext, self.f.externals)                  # the one the tunnels used: ours, no tunnel left
        self.assertIn("shc-edge-by-hand", self.f.externals)

    def test_a_recorded_gateway_without_the_label_stays(self):
        self.up()
        ext = "shc-edge-" + V.EDGE["site"]
        self.f.externals[ext]["labels"] = {}
        self.call("place_down", "p1", vpc="shc-acme--p1")
        self.assertIn(ext, self.f.externals)

    def test_a_rerun_teardown_finds_the_recorded_gateway(self):
        """Tunnels already gone (an earlier run failed after them): the gateway the status records still goes."""
        self.up()
        ext = "shc-edge-" + V.EDGE["site"]
        for i in range(2):
            del self.f.tunnels["shc-acme--p1-t%d" % i]
        self.call("place_down", "p1", vpc="shc-acme--p1")
        self.assertIn(ext, self.f.externals)                     # nothing names it: kept
        self.call("place_down", "p1", vpc="shc-acme--p1", external_gateway=ext)
        self.assertNotIn(ext, self.f.externals)


# ---- AWS ----------------------------------------------------------------------------------------------------------

try:
    import botocore.exceptions
    import shc_aws_tenant as A
    HAVE_BOTO = True
except ImportError:  # pragma: no cover
    HAVE_BOTO = False


class AwsPlacementName(unittest.TestCase):
    def test_names(self):
        self.assertEqual(shc_names.name("aws-placement", ["acme-corp", "core-prod"])["name"], "acme-corp_core-prod")
        self.assertNotEqual(shc_names.name("aws-placement", ["acme-corp", "web"])["name"],
                            shc_names.name("aws-placement", ["acme", "corp-web"])["name"])
        # the longest ending the helper appends is "-public-<zone letter>"; EC2 tag values take 256
        name = shc_names.name("aws-placement", ["t" * 30, "p" * 30])["name"]
        self.assertLessEqual(len(name + "-public-a"), 256)
        self.assertEqual(shc_names.KINDS["aws-placement"][3], len("-public-a"))

    @unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
    def test_an_existing_vpc_keeps_its_name(self):
        old = {"Tags": [{"Key": "Name", "Value": "shc-acme-corp-core-prod"}]}
        self.assertEqual(A.vpc_name(old, "acme-corp_core-prod", "acme-corp/core-prod"), "shc-acme-corp-core-prod")
        self.assertEqual(A.vpc_name(None, "acme-corp_core-prod", "acme-corp/core-prod"), "acme-corp_core-prod")

    @unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
    def test_subnets_found_by_role_and_zone(self):
        mk = lambda i, role, az: {"SubnetId": i, "AvailabilityZone": az,
                                  "Tags": [{"Key": "Name", "Value": "x"}] + ([{"Key": "shc-role", "Value": role}] if role else [])}
        got = A.by_role([mk("a", "work", "z1a"), mk("b", "tgw", "z1a"), mk("c", None, "z1b")])
        self.assertEqual(sorted(got), [("tgw", "z1a"), ("work", "z1a")])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AwsPlacementTasks(unittest.TestCase):
    FIRST, LAST = "Check the names the placement's cloud objects carry", "The environment's account"

    def test_states(self):
        tasks = load(os.path.join(NP, "tasks", "backend_cloudaws.yml"))
        names = [t.get("name") for t in tasks]
        part = tasks[names.index(self.FIRST): names.index(self.LAST) + 1]
        steps = [{"ansible.builtin.set_fact": {"fab_entity": "{{ case.tenant }}", "fab_name": "{{ case.placement }}",
                                               "np_backend_name": "prod", "fab_spec": {"network": "core"},
                                               "_refused": "", "np_aws_name": ""}},
                 {"block": part, "rescue": [{"ansible.builtin.set_fact": {"_refused": "{{ fab_fail_reason }}"}}]}]
        got = run_cases(steps, [{"tenant": "acme-corp", "placement": "web"}, {"tenant": "acme", "placement": "corp-web"},
                                {"tenant": "acme", "placement": "Web"}],
                        "{'name': np_aws_name, 'tags': (np_aws_cfg | default({})).tags | default({}), 'refused': _refused}",
                        extra_vars={"role_path": NP, "np_backend": {"status": {}}, "aws_real": {"hub": {"region": "r"}},
                                    "aws_edge_cfg": {}})
        self.assertEqual([g["name"] for g in got[:2]], ["acme-corp_web", "acme_corp-web"])
        self.assertEqual(got[0]["tags"], {"shc-entity": "acme-corp", "shc-environment": "prod", "shc-network": "core"})
        self.assertEqual(got[2]["refused"], "NameNotAllowed")


class FakeRoute53:
    def __init__(self, zones):
        self.zones = {z["Id"]: z for z in zones}
        self.calls = []

    def list_tags_for_resource(self, ResourceType, ResourceId):
        z = self.zones["/hostedzone/" + ResourceId]
        return {"ResourceTagSet": {"Tags": [{"Key": k, "Value": v} for k, v in z.get("_tags", {}).items()]}}

    def get_hosted_zone(self, Id):
        key = Id if Id.startswith("/hostedzone/") else "/hostedzone/" + Id
        if key not in self.zones:
            raise botocore.exceptions.ClientError({"Error": {"Code": "NoSuchHostedZone", "Message": Id}}, "GetHostedZone")
        return {"HostedZone": self.zones[key], "DelegationSet": {"NameServers": ["ns-1.example."]}}

    def list_hosted_zones_by_name(self, DNSName, MaxItems):
        return {"HostedZones": [z for z in self.zones.values() if z["Name"] == DNSName]}

    def create_hosted_zone(self, Name, **kw):
        self.calls.append("create")
        z = {"Id": "/hostedzone/ZNEW", "Name": Name + ".", "Config": {"PrivateZone": False}}
        self.zones[z["Id"]] = z
        return {"HostedZone": z}

    def change_tags_for_resource(self, **kw):
        self.zones["/hostedzone/" + kw["ResourceId"]]["_tags"] = {t["Key"]: t["Value"] for t in kw["AddTags"]}

    def list_resource_record_sets(self, **kw):
        return {"ResourceRecordSets": []}

    def get_paginator(self, name):
        return mock.Mock(paginate=lambda **kw: [{"ResourceRecordSets": []}])

    def change_resource_record_sets(self, **kw):
        self.calls.append("change")

    def delete_hosted_zone(self, Id):
        self.calls.append("delete")
        del self.zones[Id]


AZONE = "prod.acme-corp.tenants.example.com"


def r53zone(zid, tags, name=AZONE + "."):
    return {"Id": "/hostedzone/" + zid, "Name": name, "Config": {"PrivateZone": False}, "_tags": tags}


OURS = {"shc-managed": "true", "shc-entity": "acme-corp"}


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class AwsZoneOwnership(unittest.TestCase):
    """exists ours / exists unmarked / exists another tenant's / absent; by the recorded id or by name."""

    def run_zone(self, action, zones, zone_id=None):
        t = A.Tenant.__new__(A.Tenant)
        t.r53, t.changed, t.cfg = FakeRoute53(zones), [], {"tags": {"shc-entity": "acme-corp"}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                return getattr(t, action)(AZONE, zone_id), t.r53
            except SystemExit:
                return {"refused": json.loads(buf.getvalue())}, t.r53

    def test_up(self):
        out, r = self.run_zone("zone_up", [])
        self.assertEqual((out["zoneId"], r.calls), ("ZNEW", ["create"]))
        out, r = self.run_zone("zone_up", [r53zone("Z1", OURS)], "Z1")
        self.assertEqual((out["zoneId"], r.calls), ("Z1", []))
        out, r = self.run_zone("zone_up", [r53zone("Z1", OURS)])
        self.assertEqual(out["zoneId"], "Z1")
        # ours beside an unmarked one of the same name (Route 53 allows several): ours is used
        out, r = self.run_zone("zone_up", [r53zone("Z0", {}), r53zone("Z1", OURS)])
        self.assertEqual(out["zoneId"], "Z1")
        for zones, zid in (([r53zone("Z1", {})], None), ([r53zone("Z1", {"shc-managed": "true", "shc-entity": "acme"})], None),
                           ([r53zone("Z1", {})], "Z1"), ([r53zone("Z1", OURS, name="other.example.com.")], "Z1")):
            out, r = self.run_zone("zone_up", zones, zid)
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", zones)
            self.assertEqual(r.calls, [])

    def test_down(self):
        out, r = self.run_zone("zone_down", [r53zone("Z1", OURS)], "Z1")
        self.assertEqual((out["deleted"], r.calls), (True, ["delete"]))
        out, r = self.run_zone("zone_down", [])
        self.assertFalse(out["deleted"])
        out, r = self.run_zone("zone_down", [r53zone("Z1", {})])
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
        self.assertEqual(r.calls, [])


# ---- the hub's objects named by a number: the names stay, tags carry tenant, network and placement -----------------

if HAVE_BOTO:
    import shc_aws_edge as E
    import test_aws_edge_ipv6 as EV

    class TaggingEC2(EV.FakeEC2):
        def create_tags(self, Resources, Tags):
            self.rec("create_tags", {"Resources": Resources, "Tags": Tags})
            for coll, key in ((self.route_tables, "TransitGatewayRouteTableId"),
                              (self.attachments, "TransitGatewayAttachmentId"), (self.peers, "TransitGatewayConnectPeerId")):
                for o in coll:
                    if o.get(key) == Resources[0]:
                        keys = {x["Key"] for x in Tags}
                        o["Tags"] = [t for t in o.get("Tags", []) if t["Key"] not in keys] + Tags

NAMES = {"shc-entity": "acme-corp", "shc-network": "core"}


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
@mock.patch.object(E.time, "sleep", lambda s: None) if HAVE_BOTO else (lambda c: c)
class HubNames(unittest.TestCase):
    def created(self, ec2, call):
        return [E.tagmap({"Tags": kw["TagSpecifications"][0]["Tags"]}) for n, kw in ec2.calls if n == call]

    def test_new_objects_carry_the_names(self):
        e, ec2, _ = EV.edge(TaggingEC2(), ipv6=False)
        e.net_up(51000, names=NAMES)
        for call in ("create_transit_gateway_route_table", "create_transit_gateway_connect",
                     "create_transit_gateway_connect_peer"):
            for t in self.created(ec2, call):
                self.assertTrue(t["Name"].startswith("shc-net-51000"), t)          # the name stays
                self.assertEqual({k: t[k] for k in NAMES}, NAMES, call)

    def test_existing_objects_gain_them_and_are_found_by_tags(self):
        e, ec2, ssm = EV.edge(TaggingEC2(), ipv6=False)
        e.net_up(51000)                                                            # made without names (older run)
        ec2.calls = []
        e2, _, _ = EV.edge(ec2, ipv6=False)
        e2.ssm = e.ssm
        e2.net_up(51000, names=NAMES)
        self.assertEqual([n for n, _ in ec2.calls if n.startswith("create_transit_gateway")], [])   # found, not remade
        tagged = [kw["Resources"][0] for n, kw in ec2.calls if n == "create_tags"]
        self.assertEqual(len(tagged), 4)                                           # table, connect attachment, 2 peers
        for rt in ec2.route_tables:
            self.assertEqual({k: E.tagmap(rt).get(k) for k in NAMES}, NAMES)

    def test_a_table_named_like_ours_without_our_tags_is_not_used(self):
        ec2 = TaggingEC2()
        ec2.route_tables.append({"TransitGatewayRouteTableId": "tgw-rtb-by-hand", "State": "available",
                                 "Tags": [{"Key": "Name", "Value": "shc-net-51000"}]})
        e, _, _ = EV.edge(ec2, ipv6=False)
        out = e.net_up(51000, names=NAMES)
        self.assertNotEqual(out["routeTable"], "tgw-rtb-by-hand")

    def test_the_tenants_attachment_names_its_placement(self):
        e, ec2, _ = EV.edge(TaggingEC2(), ipv6=False)
        e.net_up(51000, names=NAMES)
        ec2.attachments.append({"TransitGatewayAttachmentId": "tgw-attach-t", "ResourceType": "vpc", "State": "available",
                                "TransitGatewayId": "tgw-0", "ResourceOwnerId": "111111111111", "Tags": []})
        ec2.describe_transit_gateway_vpc_attachments = lambda **kw: {"TransitGatewayVpcAttachments": []}
        ec2.get_transit_gateway_route_table_propagations = lambda **kw: {"TransitGatewayRouteTablePropagations": []}
        e.net_attach(51000, "tgw-attach-t", names=dict(NAMES, **{"shc-environment": "prod",
                                                                   "shc-placement": "entity-acme-corp/core-prod"}))
        tags = [kw["Tags"] for n, kw in ec2.calls if n == "create_tags" and kw["Resources"] == ["tgw-attach-t"]][0]
        self.assertEqual(E.tagmap({"Tags": tags}), {"shc-edge-site": "site-x", "shc-vni": "51000", "shc-role": "tenant",
                                                    "shc-entity": "acme-corp", "shc-environment": "prod",
                                                    "shc-network": "core", "shc-placement": "entity-acme-corp/core-prod"})


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class AwsPlacementObjects(unittest.TestCase):
    """A placement's own objects: the Name the names function gave, every tag naming it, found again by tags."""

    def setUp(self):
        import test_aws_tenant_ipv6 as AT
        self.AT = AT
        for p in (mock.patch.object(A.time, "sleep", lambda s: None), mock.patch.object(A.shc_aws_edge, "Edge", AT.FakeEdge)):
            p.start()
            self.addCleanup(p.stop)
        self.ec2 = AT.FakeEc2()

    def up(self, name="acme_p1"):
        t = self.AT.tenant(self.ec2)
        t.cfg["tags"] = {"shc-entity": "acme", "shc-environment": "prod", "shc-network": "core"}
        return t.place_up("entity-acme/p1", 1001, list(self.AT.V4), False, None, name=name)

    def test_names_and_tags(self):
        self.up()
        want = {"shc-managed": "true", "shc-entity": "acme", "shc-environment": "prod", "shc-network": "core",
                "shc-placement": "entity-acme/p1"}
        vpc = list(self.ec2.vpcs.values())[0]
        self.assertEqual(A.tagmap(vpc)["Name"], "acme_p1")
        for o in [vpc] + list(self.ec2.subnets.values()):
            t = A.tagmap(o)
            self.assertEqual({k: t.get(k) for k in want}, want, t)
        self.assertEqual(sorted(A.tagmap(x)["Name"] for x in self.ec2.subnets.values()),
                         sorted("acme_p1-%s-%s" % (r, z[-1]) for r in ("work", "tgw") for z in self.AT.AZS))
        edge = self.AT.FakeEdge.instances[-1]
        self.assertEqual(edge.names, {"shc-entity": "acme", "shc-network": "core"})
        self.assertEqual(edge.attach_names["shc-placement"], "entity-acme/p1")
        self.assertEqual(edge.attach_names["shc-environment"], "prod")

    def test_a_renamed_vpc_keeps_its_objects(self):
        """Found by tags, not Names: a re-run with another Name (or a person renaming the VPC) makes nothing new."""
        self.up()
        before = (len(self.ec2.vpcs), len(self.ec2.subnets))
        for s in self.ec2.subnets.values():
            s["Tags"] = [t for t in s["Tags"] if t["Key"] != "Name"] + [{"Key": "Name", "Value": "renamed"}]
        self.up(name="something-else")
        self.assertEqual((len(self.ec2.vpcs), len(self.ec2.subnets)), before)
        self.assertEqual(A.tagmap(list(self.ec2.vpcs.values())[0])["Name"], "acme_p1")


if __name__ == "__main__":
    unittest.main()

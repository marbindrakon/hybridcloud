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


if __name__ == "__main__":
    unittest.main()

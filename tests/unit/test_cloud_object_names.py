"""Names of the objects the platform makes on AWS and Google Cloud, and the refusal of an object of the wanted name
that the platform did not make.

Google Cloud: a placement's VPC is shc-<tenant>--<placement> from the names function, cut with a derived ending so
that every ending the helper appends still fits Google's 63 characters; an existing placement keeps the name its
status records (status.realization.site.vpc). The environment's Cloud DNS zone is named by the function too.

What is ours on a tenant's project or account is what a status records (the VPC, the zone, the site's external VPN
gateway): labels, tags and descriptions there are written for people (the tenant can write them) and decide nothing.
An object of the wanted name that no status records is refused (NameTakenOnSite), never taken over and never
deleted, whatever it carries; a recorded one is used and deleted whatever someone wrote on it. What a run makes
reaches status in that run, also when the run fails later, so a deletion removes it. The decisions are enumerated:
(exists or not) x (what status records) x (what the object carries).

Example names are made up: tenants acme-corp / acme, placements core-prod / corp-core-prod (the two-tenant pair).
"""
import contextlib
import copy
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
                                               "np_gcp_vpc": "", "np_gcp_vpc_recorded": False,
                                               "fab_fail_reason": "", "fab_fail_msg": ""}}] + self.steps()
        return run_cases(steps, cases, "{'vpc': np_gcp_vpc, 'labels': (np_gcp_cfg | default({})).labels | default({}),"
                                       " 'refused': _refused, 'recorded': np_gcp_vpc_recorded | bool}", extra_vars=base)

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
        self.assertEqual([g["recorded"] for g in got[:3]], [False, True, False])   # passed on as --vpc-recorded
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
    """(absent | the recorded zone | a zone of the name or DNS name) x (recorded or not) x (labels: ours, none, another
    tenant's) -> made | used | refused; zone-down deletes only the recorded zone."""

    def run_zone(self, action, zones, name=ZONE, recorded=False):
        f = FakeDns(zones)
        t = T.Tenant.__new__(T.Tenant)
        t.project, t.tok, t.changed = "p", "tok", []
        t.labels = {"shc-managed": "true", "shc-entity": "acme-corp", "shc-environment": "prod"}
        buf = io.StringIO()
        T.MADE.clear()
        with mock.patch.object(T.g, "http", f.http), mock.patch.object(T.g, "list_all", f.list_all), \
                contextlib.redirect_stdout(buf):
            try:
                out = t.zone_up(FQDN, name, recorded) if action == "zone_up" else t.zone_down(FQDN, name)
            except SystemExit:
                return {"refused": json.loads(buf.getvalue())}, f
        return out, f

    LABELS = {"ours": None, "none": {}, "other": {"shc-managed": "true", "shc-entity": "acme"}}

    def test_up_states(self):
        for recorded in (False, True):
            out, f = self.run_zone("zone_up", [], recorded=recorded)       # absent: made with the name given
            self.assertEqual(out["zoneId"], ZONE)
            self.assertEqual(T.MADE, {"zoneId": ZONE})
            for label, labels in self.LABELS.items():
                # the zone of the name: recorded -> the environment's whatever its labels; else refused
                out, f = self.run_zone("zone_up", [zone(labels=labels)], recorded=recorded)
                if recorded:
                    self.assertEqual(out["zoneId"], ZONE, label)
                else:
                    self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", label)   # a forged mark too
                    self.assertNotIn("acme.", out["refused"]["error"].replace("acme-corp", ""))
                self.assertNotIn(("POST", ""), f.calls)
                # another zone of the DNS name (recorded one gone, or nothing recorded): refused, untouched
                out, f = self.run_zone("zone_up", [zone(name="other", labels=labels)], recorded=recorded)
                self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", (label, recorded))
                self.assertEqual([c for c in f.calls if c[0] != "GET"], [])
            out, f = self.run_zone("zone_up", [zone(dns="elsewhere.example.com.")], recorded=recorded)
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")    # the name holds another DNS name
        # an environment of the release before: its status records the older name
        old = "shc-prod-acme-corp-tenants-example-com"
        out, f = self.run_zone("zone_up", [zone(name=old, labels={})], name=old, recorded=True)
        self.assertEqual(out["zoneId"], old)

    def test_up_needs_the_name(self):
        out, _ = self.run_zone("zone_up", [], name=None)
        self.assertIn("--zone-name", out["refused"]["error"])

    def test_down_states(self):
        for label, labels in self.LABELS.items():                          # recorded: deleted whatever its labels
            out, f = self.run_zone("zone_down", [zone(labels=labels)])
            self.assertTrue(out["deleted"], label)
            self.assertEqual(f.zones, {})
        out, f = self.run_zone("zone_down", [])
        self.assertFalse(out["deleted"])
        out, f = self.run_zone("zone_down", [zone()], name=None)           # nothing recorded: nothing deleted
        self.assertFalse(out["deleted"])
        self.assertEqual(len(f.zones), 1)
        out, f = self.run_zone("zone_down", [zone(dns="elsewhere.example.com.")])
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
        self.assertEqual(len(f.zones), 1)


class PlacementOwnership(unittest.TestCase):
    """The placement's VPC and the site's external VPN gateway: (absent | exists) x (recorded or not) x (description
    or labels: the platform's, edited, forged)."""
    EXT = "shc-edge-" + V.EDGE["site"]

    def setUp(self):
        self.f = V.FakeCompute()
        self.psk = mock.patch.object(T.secrets, "token_urlsafe", lambda n: "psk")
        self.psk.start()

    def tearDown(self):
        self.psk.stop()

    def call(self, fn, *a, **kw):
        t = V.tenant(self.f)
        buf = io.StringIO()
        T.MADE.clear()
        with V.fake(self.f), contextlib.redirect_stdout(buf), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHC_EDGE_VPN_TUNNELS_FILE", None)
            try:
                return getattr(t, fn)(*a, **kw)
            except SystemExit:
                return {"refused": json.loads(buf.getvalue())}

    def up(self, vpc="shc-acme--p1", recorded=None, ext_recorded=None):
        recorded = vpc in self.f.networks if recorded is None else recorded
        ext_recorded = self.EXT in self.f.externals if ext_recorded is None else ext_recorded
        return self.call("place_up", "p1", V.VNI, V.V4, V.EDGE, "fabric", None, None, vpc=vpc,
                         vpc_recorded=recorded, ext_recorded=ext_recorded)

    def add_vpc(self, name, description):
        self.f.networks[name] = {"name": name, "mtu": 1370, "description": description,
                                 "selfLink": self.f.link("/global/networks/" + name)}

    def add_ext(self, labels):
        self.f.externals[self.EXT] = {"name": self.EXT, "labels": labels,
                                      "interfaces": [{"id": 0, "ipAddress": "198.51.100.11"},
                                                     {"id": 1, "ipAddress": "198.51.100.12"}],
                                      "selfLink": self.f.link("/global/externalVpnGateways/" + self.EXT)}

    def test_vpc_states(self):
        out = self.up(recorded=False)                                    # absent, nothing recorded: made
        self.assertEqual(out["vpc"], "shc-acme--p1")
        self.assertTrue(self.f.networks["shc-acme--p1"]["description"].startswith(T.VPC_MARK))
        self.assertEqual(T.MADE["vpc"], "shc-acme--p1")
        self.assertEqual(self.up(recorded=True)["vpc"], "shc-acme--p1")  # recorded: used again
        for description in ("made by hand", T.VPC_MARK + "of tenant acme (forged)", ""):
            # exists, nothing recorded: refused whatever its description says (a forged mark too), untouched
            self.f = V.FakeCompute()
            self.add_vpc("shc-acme--p1", description)
            before = len(self.f.mutations())
            out = self.up(recorded=False)
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", description)
            self.assertEqual(len(self.f.mutations()), before)
            # exists and recorded: the placement's whatever its description says (someone edited it)
            out = self.up(recorded=True)
            self.assertEqual(out["vpc"], "shc-acme--p1", description)

    def test_a_recorded_vpc_with_an_edited_description_is_deleted(self):
        self.up()
        self.f.networks["shc-acme--p1"]["description"] = "edited in the console"
        out = self.call("place_down", "p1", vpc="shc-acme--p1", external_gateway=self.EXT)
        self.assertTrue(out["deleted"])
        self.assertNotIn("shc-acme--p1", self.f.networks)
        self.assertNotIn(self.EXT, self.f.externals)

    def test_teardown_states(self):
        """The teardown: (recorded VPC there | recorded VPC gone | nothing recorded) x (a VPC of the older name)."""
        self.up(vpc="shc-p1", recorded=False)                            # a VPC of the older name, made by someone
        before = len(self.f.mutations())
        out = self.call("place_down", "p1", vpc=None, external_gateway=None)   # nothing recorded: nothing deleted
        self.assertFalse(out["deleted"])
        self.assertIn("recorded no VPC", out["note"])
        self.assertEqual(len(self.f.mutations()), before)
        self.assertIn("shc-p1", self.f.networks)
        # the recorded VPC is gone, its router and subnet's names are taken by objects of another VPC: left alone
        self.f = V.FakeCompute()
        self.add_vpc("elsewhere", "x")
        self.f.subnets["shc-acme--p1"] = {"name": "shc-acme--p1", "ipCidrRange": V.V4[0], "stackType": "IPV4_ONLY",
                                          "network": self.f.networks["elsewhere"]["selfLink"]}
        self.f.routers["shc-acme--p1"] = {"name": "shc-acme--p1", "network": self.f.networks["elsewhere"]["selfLink"]}
        before = len(self.f.mutations())
        out = self.call("place_down", "p1", vpc="shc-acme--p1")
        self.assertFalse(out["deleted"])
        self.assertIn("no longer exists", out["note"])
        self.assertEqual(out["left"], ["router shc-acme--p1", "subnet shc-acme--p1"])
        self.assertEqual(len(self.f.mutations()), before)

    def test_objects_of_the_vpcs_names_in_another_vpc(self):
        """A subnet named like the VPC that belongs to another VPC: place-up refuses, place-down leaves it alone."""
        self.add_vpc("elsewhere", "x")
        self.f.subnets["shc-acme--p1"] = {"name": "shc-acme--p1", "ipCidrRange": V.V4[0], "stackType": "IPV4_ONLY",
                                          "network": self.f.networks["elsewhere"]["selfLink"]}
        out = self.up(recorded=False)
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
        self.assertEqual(out["refused"]["made"], {"vpc": "shc-acme--p1"})   # the VPC it made reaches status
        out = self.call("place_down", "p1", vpc="shc-acme--p1")
        self.assertIn("shc-acme--p1", self.f.subnets)
        self.assertEqual(out["left"], ["subnet shc-acme--p1"])

    def test_external_gateway_states(self):
        self.up()
        self.assertEqual(self.f.externals[self.EXT]["labels"], {"shc-managed": "true", "shc-edge-site": V.EDGE["site"]})
        for labels in ({}, {"shc-managed": "true", "shc-edge-site": V.EDGE["site"]},     # none / forged
                       {"shc-managed": "true", "shc-edge-site": "another-site"}):
            for recorded in (False, True):
                self.f = V.FakeCompute()
                self.add_ext(labels)
                out = self.up(ext_recorded=recorded)
                if recorded:                                  # a placement's status records it: used as it is
                    self.assertEqual(out["externalGateway"], self.EXT, labels)
                else:                                         # nobody records it: refused whatever its labels
                    self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", labels)
                    self.assertEqual(out["refused"]["made"], {"vpc": "shc-acme--p1"})
                self.assertEqual(self.f.externals[self.EXT]["labels"], labels)

    def test_delete_only_the_recorded_gateway(self):
        self.up()
        self.f.externals["shc-edge-by-hand"] = {"name": "shc-edge-by-hand", "labels": {"shc-managed": "true"},
                                                "selfLink": self.f.link("/global/externalVpnGateways/shc-edge-by-hand")}
        self.call("place_down", "p1", vpc="shc-acme--p1")                 # nothing recorded: the gateway stays
        self.assertIn(self.EXT, self.f.externals)
        self.call("place_down", "p1", vpc="shc-acme--p1", external_gateway=self.EXT)
        self.assertNotIn(self.EXT, self.f.externals)                      # recorded, no tunnel left anywhere
        self.assertIn("shc-edge-by-hand", self.f.externals)               # never by a name's beginning

    def test_a_recorded_gateway_without_the_label_goes(self):
        self.up()
        self.f.externals[self.EXT]["labels"] = {}
        self.call("place_down", "p1", vpc="shc-acme--p1", external_gateway=self.EXT)
        self.assertNotIn(self.EXT, self.f.externals)

    def test_a_tunnel_in_another_region_keeps_the_gateway(self):
        self.up()
        self.f.tunnels["other-region-t0"] = {"name": "other-region-t0", "router": "x/regions/elsewhere/routers/o",
                                             "vpnGateway": "x/regions/elsewhere/vpnGateways/o",
                                             "peerExternalGateway": self.f.externals[self.EXT]["selfLink"]}
        self.call("place_down", "p1", vpc="shc-acme--p1", external_gateway=self.EXT)
        self.assertIn(self.EXT, self.f.externals)

    def test_a_failed_first_run_then_delete_removes_what_was_made(self):
        """place-up makes the VPC and fails later (here: an external gateway nobody records); the failure names what
        it made, the job records it, and the deletion removes it."""
        self.add_ext({})
        out = self.up(recorded=False, ext_recorded=False)
        self.assertEqual(out["refused"]["made"], {"vpc": "shc-acme--p1"})
        self.assertIn("shc-acme--p1", self.f.networks)
        self.call("place_down", "p1", vpc=out["refused"]["made"]["vpc"])
        self.assertNotIn("shc-acme--p1", self.f.networks)
        self.assertIn(self.EXT, self.f.externals)                          # not this placement's: untouched


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GcpPlacementRecords(unittest.TestCase):
    """The backend's record right after place-up: (ok | failed) x (made or reported) x (already recorded)."""

    def test_states(self):
        task = by_name(load(os.path.join(NP, "tasks", "backend_cloudgcp.yml")), "What the placement owns in the project now")
        ok = {"vpc": "shc-a--p", "externalGateway": "shc-edge-s"}
        cases = [{"err": "", "res": {"result": ok}, "site": {}},                                   # first run, ok
                 {"err": "boom", "res": {"made": {"vpc": "shc-a--p"}}, "site": {}},                # failed after the VPC
                 {"err": "boom", "res": {}, "site": {}},                                           # refused: nothing made
                 {"err": "", "res": {"result": ok}, "site": ok},                                   # already recorded
                 {"err": "", "res": {"result": ok}, "site": {"vpc": "shc-a--p"}}]                  # gateway first used
        steps = [{"ansible.builtin.set_fact": {"gcp_error": "{{ case.err }}", "gcp_result": "{{ case.res }}",
                                               "fab_st": {"realization": {"site": "{{ case.site }}"}}}}, task]
        got = run_cases(steps, cases, "np_gcp_own")
        self.assertEqual(got, [ok, {"vpc": "shc-a--p"}, {}, {}, {"externalGateway": "shc-edge-s"}])

    def test_the_gateway_recorded_by_another_placement_of_the_project(self):
        task = by_name(load(os.path.join(NP, "tasks", "backend_cloudgcp.yml")), "Is the site's external VPN gateway recorded")

        def pl(project, ext):
            return {"metadata": {"namespace": "n", "name": "x"},
                    "status": {"realization": {"site": {"project": project, "externalGateway": ext}}}}
        cases = [{"all": [], "mine": ""}, {"all": [pl("p", "shc-edge-s")], "mine": ""},
                 {"all": [pl("q", "shc-edge-s")], "mine": ""}, {"all": [pl("p", "shc-edge-t")], "mine": ""},
                 {"all": [], "mine": "shc-edge-s"}]
        steps = [{"ansible.builtin.set_fact": {"_np_gcp_all": {"resources": "{{ case.all }}"},
                                               "fab_st": {"realization": {"site": {"externalGateway": "{{ case.mine }}"}}}}},
                 task]
        got = run_cases(steps, cases, "np_gcp_ext_recorded | bool",
                        extra_vars={"np_gateway": {"metadata": {"name": "s"}}, "np_gcp_cfg": {"project": "p"}})
        self.assertEqual(got, [False, True, False, False, True])


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
    """(absent | the recorded zone | a zone of the name) x (recorded id or not) x (tags: ours, none, another
    tenant's) -> made | used | refused; zone-down deletes only the recorded zone."""
    TAGS = {"ours": OURS, "none": {}, "other": {"shc-managed": "true", "shc-entity": "acme"}}

    def run_zone(self, action, zones, zone_id=None):
        t = A.Tenant.__new__(A.Tenant)
        t.r53, t.changed, t.cfg = FakeRoute53(zones), [], {"tags": {"shc-entity": "acme-corp"}}
        buf = io.StringIO()
        A.MADE.clear()
        with contextlib.redirect_stdout(buf):
            try:
                return getattr(t, action)(AZONE, zone_id), t.r53
            except SystemExit:
                return {"refused": json.loads(buf.getvalue())}, t.r53

    def test_up(self):
        out, r = self.run_zone("zone_up", [])
        self.assertEqual((out["zoneId"], r.calls, A.MADE), ("ZNEW", ["create"], {"zoneId": "ZNEW"}))
        out, r = self.run_zone("zone_up", [], "ZGONE")                  # the recorded zone is gone: a new one
        self.assertEqual(out["zoneId"], "ZNEW")
        for label, tags in self.TAGS.items():
            out, r = self.run_zone("zone_up", [r53zone("Z1", tags)], "Z1")      # recorded: used whatever its tags
            self.assertEqual((out["zoneId"], r.calls), ("Z1", []), label)
            out, r = self.run_zone("zone_up", [r53zone("Z1", tags)])            # nothing recorded: refused
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", label)  # a forged mark too
            self.assertEqual(r.calls, [])
            out, r = self.run_zone("zone_up", [r53zone("Z1", tags)], "ZGONE")   # recorded gone, another of the name
            self.assertEqual(out["refused"]["reason"], "NameTakenOnSite", label)
        out, r = self.run_zone("zone_up", [r53zone("Z1", OURS, name="other.example.com.")], "Z1")
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")    # the recorded id is another zone now

    def test_down(self):
        for label, tags in self.TAGS.items():
            out, r = self.run_zone("zone_down", [r53zone("Z1", tags)], "Z1")    # recorded: deleted whatever its tags
            self.assertEqual((out["deleted"], r.calls), (True, ["delete"]), label)
            out, r = self.run_zone("zone_down", [r53zone("Z1", tags)])          # nothing recorded: nothing deleted
            self.assertEqual((out["deleted"], r.calls), (False, []), label)
        out, r = self.run_zone("zone_down", [], "Z1")
        self.assertFalse(out["deleted"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ZoneRecords(unittest.TestCase):
    """The environment jobs record the zone right after zone-up: (ok | failed after the create | refused) x (already
    recorded)."""

    def test_states(self):
        for cloud, res_key, err_key, field in (("aws", "aws_result", "aws_error", "route53ZoneId"),
                                               ("gcp", "gcp_result", "gcp_error", "dnsZoneId")):
            task = by_name(load(os.path.join(ROLES, "cloud%s_provision" % cloud, "tasks", "main.yml")),
                           "Record the zone the run made")
            body = dict(task["vars"])
            probe = {"ansible.builtin.set_fact": {"_got": "{{ _made if (%s) else '' }}" % task["when"]}, "vars": body}
            cases = [{"err": "", "res": {"result": {"zoneId": "Z1"}}, "st": {}},
                     {"err": "boom", "res": {"made": {"zoneId": "Z1"}}, "st": {}},
                     {"err": "boom", "res": {}, "st": {}},
                     {"err": "", "res": {"result": {"zoneId": "Z1"}}, "st": {field: "Z1"}}]
            steps = [{"ansible.builtin.set_fact": {err_key: "{{ case.err }}", res_key: "{{ case.res }}",
                                                   "fab_st": "{{ case.st }}"}}, probe]
            got = run_cases(steps, cases, "_got", extra_vars={"fab_name": "prod", "fab_namespace": "n",
                                                              "ca_account": "a", "ca_zone": "z",
                                                              "ci_spec": {"aws": {"region": "r"}, "gcp": {"region": "r"}}})
            self.assertEqual(got, ["Z1", "Z1", "", ""], cloud)
            self.assertEqual(body["cr_status_body"][field], "{{ _made }}")


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
class AwsShareStays(unittest.TestCase):
    """CloudAWS teardown: the account leaves the hub's transit gateway share only when no other environment (of
    this tenant or another, not being deleted) records the same account."""

    def test_states(self):
        tasks = load(os.path.join(ROLES, "cloudaws_teardown", "tasks", "main.yml"))[0]["block"]
        decide = by_name(tasks, "Whether another environment keeps the account in the share")
        share = by_name(tasks, "Stop sharing the hub's transit gateway with the account")
        probe = {"ansible.builtin.set_fact": {"_down": "{{ %s }}" % share["when"]}}
        me = {"metadata": {"name": "prod", "namespace": "entity-acme-corp"}, "status": {"awsAccountId": "111"}}
        env = lambda name, ns, acct, deleting=False: {
            "metadata": dict({"name": name, "namespace": ns}, **({"deletionTimestamp": "x"} if deleting else {})),
            "status": {"awsAccountId": acct}}
        cases = [[me],                                                       # alone: the share goes
                 [me, env("dev", "entity-acme-corp", "111")],                 # another environment of the tenant
                 [me, env("prod", "entity-acme", "111")],                     # another tenant, same name
                 [me, env("dev", "entity-acme-corp", "222")],                 # another account
                 [me, env("dev", "entity-acme-corp", "111", deleting=True)],  # the other is being deleted too
                 [me, {"metadata": {"name": "new", "namespace": "entity-acme-corp"}}]]   # no account recorded yet
        got = run_cases([{"ansible.builtin.set_fact": {"_cad_envs": {"resources": "{{ case }}"}}}, decide, probe],
                        cases, "_down | bool",
                        extra_vars={"fab_name": "prod", "fab_namespace": "entity-acme-corp", "cad_account": "111"})
        self.assertEqual(got, [True, False, False, True, True, True])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ZoneTeardownSaysWhatItLeft(unittest.TestCase):
    """CloudAWS and CloudGCP teardowns: the deletion message says what was deleted and what was left (the helper's
    deleted/note for the zone, the DNS helper's deleted/kept for the delegation), never "removed" for a zone or a
    delegation that was kept, not recorded or not found."""

    def test_states(self):
        for kind, where in (("aws", "account"), ("gcp", "project")):
            tasks = load(os.path.join(ROLES, "cloud%s_teardown" % kind, "tasks", "main.yml"))[0]["block"]
            done = by_name(tasks, "What the teardown deleted and left")
            zone = {"zone": "prod.acme-corp.example.com", "dnsConfig": "dns"}
            cases = [
                {"dns": zone, "z": {"deleted": True}, "d": {"deleted": True}},
                {"dns": zone, "z": {"deleted": False, "note": "no zone was recorded"}, "d": {"deleted": False}},
                {"dns": zone, "z": {"deleted": False}, "d": {"deleted": False, "kept": "not made by the platform"}},
                {"dns": zone, "z": {}, "d": {}},
                {"dns": {"zone": "prod.acme-corp.example.com"}, "z": {"deleted": True}, "d": {}},
                {"dns": {}, "z": {}, "d": {}},
            ]
            steps = [{"ansible.builtin.set_fact": {"cad_dns": "{{ case.dns }}", "cad_zone": "{{ case.z }}",
                                                   "cad_deleg": "{{ case.d }}"}}, done]
            got = run_cases(steps, cases, "cad_done", extra_vars={"cad_account_shared": False})
            z = "Zone prod.acme-corp.example.com"
            self.assertEqual(got, [
                z + " deleted; its delegation in the tenant zone deleted",
                z + " was not deleted: the environment recorded no zone id, so nothing was deleted in the %s; no "
                    "delegation of it was found in the tenant zone" % where,
                z + " was already gone; its delegation in the tenant zone was kept (not made by the platform)",
                z + " was not deleted: the environment recorded no %s; its delegation in the tenant zone was not "
                    "deleted: the platform's tenant DNS configuration was not found" % where,
                z + " deleted; no delegation in a tenant zone was recorded",
                "No DNS zone was recorded for the environment, so none was deleted",
            ], kind)
            for g in got[1:4]:
                self.assertNotIn("removed", g)
            if kind == "aws":   # the account kept in the hub's share for another environment
                got = run_cases(steps, cases[:1], "cad_done", extra_vars={"cad_account_shared": True})
                self.assertEqual(got[0], z + " deleted; its delegation in the tenant zone deleted; the account stays "
                                             "in the hub's transit gateway share, as another environment records it")


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

    def up(self, name="acme_p1", vpc_id="recorded"):
        t = self.AT.tenant(self.ec2)
        t.cfg["tags"] = {"shc-entity": "acme", "shc-environment": "prod", "shc-network": "core"}
        if vpc_id == "recorded":            # what the placement's status records after a run
            vpc_id = next(iter(self.ec2.vpcs), None)
        A.MADE.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                return t.place_up("entity-acme/p1", 1001, list(self.AT.V4), False, None, name=name, vpc_id=vpc_id)
            except SystemExit:
                return {"refused": json.loads(buf.getvalue().splitlines()[-1])}

    def test_vpc_states(self):
        """(absent | tagged for the placement) x (recorded or not) x (tags: ours, edited)."""
        out = self.up(vpc_id=None)                                        # absent, nothing recorded: made
        vid = out["vpcId"]
        self.assertEqual(A.MADE, {"vpcId": vid})
        self.assertEqual(self.up(vpc_id=vid)["vpcId"], vid)               # recorded: used
        out = self.up(vpc_id=None)                                        # tagged, nothing recorded: refused
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")
        self.assertEqual(len(self.ec2.vpcs), 1)
        v = self.ec2.vpcs[vid]                                            # someone edits its tags
        v["Tags"] = [t for t in v["Tags"] if t["Key"] not in ("shc-managed", "shc-entity")]
        self.assertEqual(self.up(vpc_id=vid)["vpcId"], vid)               # recorded: still the placement's
        out = self.up(vpc_id="vpc-gone")                                  # recorded one gone, a tagged one there
        self.assertEqual(out["refused"]["reason"], "NameTakenOnSite")

    def down(self, vpc_id):
        A.MADE.clear()
        return self.AT.tenant(self.ec2).place_down("entity-acme/p1", 1001, None, vpc_id=vpc_id)

    def public(self, state, vpc_id):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                return self.AT.tenant(self.ec2).place_public("entity-acme/p1", state, vpc_id=vpc_id)
            except SystemExit:
                return {"refused": json.loads(buf.getvalue().splitlines()[-1])}

    def forge(self, vid, new_id):
        """Another VPC in the account carrying the placement's tags (anyone in the account can write them)."""
        self.ec2.vpcs[new_id] = dict(copy.deepcopy(self.ec2.vpcs[vid]), VpcId=new_id)

    def public_fake(self):
        """The fake EC2 with an internet gateway and the public route table (place-public up and down)."""
        base = self.AT.FakeEc2

        class Ec2(base):
            def __init__(self):
                super().__init__()
                self.igws, self.ptables = {}, {}

            def h_create_internet_gateway(self, **kw):
                g = self._id("igw")
                self.igws[g] = {"InternetGatewayId": g, "Attachments": [], "Tags": self._tags(kw)}
                return {"InternetGateway": copy.deepcopy(self.igws[g])}

            def h_attach_internet_gateway(self, InternetGatewayId, VpcId, **kw):
                self.igws[InternetGatewayId]["Attachments"] = [{"VpcId": VpcId}]
                return {}

            def h_detach_internet_gateway(self, InternetGatewayId, VpcId, **kw):
                self.igws[InternetGatewayId]["Attachments"] = []
                return {}

            def h_delete_internet_gateway(self, InternetGatewayId, **kw):
                del self.igws[InternetGatewayId]
                return {}

            def h_describe_internet_gateways(self, Filters=None, **kw):
                vpc = {x["Name"]: x["Values"] for x in Filters or []}.get("attachment.vpc-id", [None])[0]
                return {"InternetGateways": [copy.deepcopy(g) for g in self.igws.values()
                                             if any(a["VpcId"] == vpc for a in g["Attachments"])]}

            def h_create_route_table(self, VpcId, **kw):
                r = self._id("rtb")
                self.ptables[r] = {"RouteTableId": r, "VpcId": VpcId, "Routes": [], "Associations": [],
                                   "Tags": self._tags(kw)}
                return {"RouteTable": copy.deepcopy(self.ptables[r])}

            def h_describe_route_tables(self, Filters=None, RouteTableIds=None, **kw):
                f = {x["Name"]: x["Values"] for x in Filters or []}
                if RouteTableIds:
                    return {"RouteTables": [copy.deepcopy(self.ptables[r]) for r in RouteTableIds]}
                if "tag:shc-role" in f:
                    return {"RouteTables": [copy.deepcopy(t) for t in self.ptables.values()
                                            if t["VpcId"] == f["vpc-id"][0]]}
                return super().h_describe_route_tables(Filters=Filters, **kw)

            def h_create_route(self, RouteTableId, **kw):
                if RouteTableId in self.ptables:
                    self.ptables[RouteTableId]["Routes"].append(dict(kw))
                    return {"Return": True}
                return super().h_create_route(RouteTableId=RouteTableId, **kw)

            def h_associate_route_table(self, RouteTableId, SubnetId, **kw):
                self.ptables[RouteTableId]["Associations"].append({"SubnetId": SubnetId})
                return {"AssociationId": "rtbassoc-" + SubnetId}

            def h_delete_route_table(self, RouteTableId, **kw):
                del self.ptables[RouteTableId]
                return {}
        return Ec2()

    def test_teardown_and_public_states(self):
        """place-down and place-public: (recorded VPC there | recorded VPC gone | nothing recorded) x (no other VPC |
        another VPC with the placement's tag). Only the recorded VPC is ever changed or deleted."""
        self.ec2 = self.public_fake()
        def mutations():
            return [n for n, _ in self.ec2.calls if n.startswith(self.AT.MUTATING)]
        # two VPCs with the placement's tag, one recorded: only the recorded one gets public subnets and goes
        vid = self.up(vpc_id=None)["vpcId"]
        self.forge(vid, "vpc-forged")
        out = self.public("up", vid)
        self.assertTrue(out["public"])
        self.assertEqual({s["VpcId"] for s in self.ec2.subnets.values()}, {vid})
        self.assertTrue(all(kw.get("VpcId", vid) == vid for n, kw in self.ec2.calls if n.startswith(self.AT.MUTATING)))
        self.assertEqual(self.public("down", vid)["public"], [])
        out = self.down(vid)
        self.assertTrue(out["deleted"])
        self.assertEqual(list(self.ec2.vpcs), ["vpc-forged"])
        self.assertEqual(out["left"], ["vpc vpc-forged"])
        # a tag on a VPC that no status records, nothing recorded: nothing changed or deleted, the VPC named
        before = len(mutations())
        out = self.down(None)
        self.assertFalse(out["deleted"])
        self.assertIn("recorded no VPC", out["note"])
        self.assertEqual(out["left"], ["vpc vpc-forged"])
        self.assertIn("no recorded VPC", self.public("up", None)["refused"]["error"])
        out = self.public("down", None)
        self.assertEqual((out["deleted"], out["left"]), (False, ["vpc vpc-forged"]))
        self.assertEqual(len(mutations()), before)
        self.assertIn("vpc-forged", self.ec2.vpcs)
        # a record whose VPC is gone (another VPC carries the tag): nothing deleted in the account, the note says so
        out = self.down(vid)
        self.assertFalse(out["deleted"])
        self.assertIn("no longer exists", out["note"])
        self.assertEqual(out["left"], ["vpc vpc-forged"])
        self.assertIn("no longer exists", self.public("up", vid)["refused"]["error"])
        self.assertFalse(self.public("down", vid)["deleted"])
        self.assertEqual(len(mutations()), before)

    def test_a_waiter_error_keeps_the_record(self):
        """A VPC made, then a waiter's time-out (not an AWS answer): the JSON line still names the VPC made."""
        import botocore.exceptions

        class Waiter:
            def wait(self, **kw):
                raise botocore.exceptions.WaiterError("VpcAvailable", "Max attempts exceeded", {})
        argv = ["x", "--config", "{}", "place-up", "--placement", "entity-acme/p1", "--vni", "1001",
                "--name", "acme_p1"] + ["--prefix=" + p for p in self.AT.V4]
        tenant = self.AT.tenant(self.ec2)
        tenant.cfg["tags"] = {}
        buf = io.StringIO()
        with mock.patch.object(A, "Tenant", lambda cfg: tenant), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.ec2, "get_waiter", lambda name: Waiter(), create=True), \
                contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            A.MADE.clear()
            A.main()
        line = json.loads(buf.getvalue().splitlines()[-1])
        self.assertFalse(line["ok"])
        self.assertIn("WaiterError", line["error"])
        self.assertEqual(line["made"], {"vpcId": next(iter(self.ec2.vpcs))})

    def test_teardown_by_the_recorded_vpc(self):
        vid = self.up(vpc_id=None)["vpcId"]
        other = self.AT.tenant(self.ec2)
        # another VPC carrying the placement's tag (forged): the teardown with the recorded id never touches it
        forged = dict(copy.deepcopy(self.ec2.vpcs[vid]), VpcId="vpc-forged")
        self.ec2.vpcs["vpc-forged"] = forged
        other.place_down("entity-acme/p1", 1001, None, vpc_id=vid)
        self.assertEqual(list(self.ec2.vpcs), ["vpc-forged"])

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


# ---- refuse, do not adopt: a cluster's and a gateway's identities ---------------------------------------------------

import shc_gcp_cluster as GC  # noqa: E402
import shc_gcp_edge as GE  # noqa: E402

BUILD = "shc-gcp-acme-web-41df0c0d"


class FakeIam:
    """Service accounts, custom roles and a pool of one project, as the IAM REST API returns them."""

    def __init__(self, sas=None, roles=None, pool=None):
        self.sas, self.roles, self.pool, self.calls = dict(sas or {}), dict(roles or {}), pool, []

    def http(self, method, url, body=None, token=None, ok404=False, **_):
        self.calls.append((method, url.split("/v1/", 1)[-1]))
        if "/serviceAccounts/" in url and method == "GET":
            return self.sas.get(url.rsplit("/", 1)[-1].split("@")[0])
        if url.endswith("/serviceAccounts") and method == "POST":
            self.sas[body["accountId"]] = dict(body["serviceAccount"])
            return {}
        if "/roles/" in url and method == "GET":
            return self.roles.get(url.rsplit("/", 1)[-1])
        if url.endswith("/roles") and method == "POST":
            self.roles[body["roleId"]] = dict(body["role"], name="projects/p/roles/" + body["roleId"])
            return {}
        if "workloadIdentityPools/" in url and method == "GET" and "/providers/" not in url:
            return self.pool
        raise AssertionError((method, url))


def cluster(fake):
    c = GC.Cluster.__new__(GC.Cluster)
    c.cluster, c.project, c.tok, c.changed = BUILD, "p", "tok", []
    c.pool_id = GC.pool_id(BUILD)
    c.pool = "https://iam.googleapis.com/v1/projects/p/locations/global/workloadIdentityPools/" + c.pool_id
    c.provider = c.pool + "/providers/oidc"
    return c


def refusal(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn()
        except SystemExit:
            return json.loads(buf.getvalue().splitlines()[-1])
    return None


class GcpClusterOwnership(unittest.TestCase):
    """exists ours / exists unmarked / exists another cluster's / absent, for each identity a cluster gets."""
    REF = {"namespace": "openshift-ingress-operator", "name": "cloud-credentials"}

    def sa_case(self, existing):
        aid = GC.sa_account_id(BUILD, self.REF["namespace"], self.REF["name"])
        f = FakeIam(sas={aid: existing} if existing is not None else {})
        with mock.patch.object(GC.g, "http", f.http), mock.patch.object(GC.time, "sleep", lambda s: None):
            return refusal(lambda: cluster(f).ensure_sa(aid, self.REF)), f, aid

    def test_service_account(self):
        r, f, aid = self.sa_case(None)                                                   # absent: made, marked
        self.assertIsNone(r)
        self.assertEqual(f.sas[aid]["description"], "shc-cluster=" + BUILD)
        r, f, _ = self.sa_case({"description": "shc-cluster=" + BUILD})                  # ours: used
        self.assertIsNone(r)
        self.assertNotIn("POST", [m for m, _ in f.calls])
        for other in ({"email": aid}, {"description": "made by hand"},
                      {"description": "shc-cluster=shc-gcp-acme-api-1234abcd"}):
            r, f, _ = self.sa_case(other)                                                # unmarked / another's
            self.assertEqual(r["reason"], "NameTakenOnSite", other)
            self.assertNotIn("POST", [m for m, _ in f.calls])
            self.assertNotIn("acme-api", r["error"])

    def role_case(self, existing):
        rid = GC.role_id(BUILD, self.REF["namespace"], self.REF["name"])
        f = FakeIam(roles={rid: existing} if existing is not None else {})
        with mock.patch.object(GC.g, "http", f.http):
            return refusal(lambda: cluster(f).custom_role_up(rid, ["compute.x.get"], BUILD + " a/b")), f

    def test_custom_role(self):
        rid = GC.role_id(BUILD, self.REF["namespace"], self.REF["name"])
        self.assertIsNone(self.role_case(None)[0])
        self.assertIsNone(self.role_case({"name": "projects/p/roles/" + rid, "title": BUILD + " a/b",
                                          "includedPermissions": ["compute.x.get"]})[0])
        for other in ({"name": "projects/p/roles/" + rid, "title": "by hand"},
                      {"name": "projects/p/roles/" + rid, "title": "by hand", "deleted": True},
                      {"name": "projects/p/roles/" + rid, "title": BUILD + "-other a/b"}):
            r, f = self.role_case(other)
            self.assertEqual(r["reason"], "NameTakenOnSite", other)
            self.assertEqual([m for m, _ in f.calls], ["GET"])                           # not undeleted, not patched

    def test_pool(self):
        for pool, refused in ((None, False), ({"state": "ACTIVE", "description": GC.pool_description(BUILD)}, False),
                              ({"state": "ACTIVE", "description": "by hand"}, True),
                              ({"state": "DELETED", "description": "OpenShift cluster other (SHC)"}, True)):
            f = FakeIam(pool=pool)
            with mock.patch.object(GC.g, "http", f.http):
                c = cluster(f)
                if not refused:
                    continue                                    # the create path waits on Google: covered live
                r = refusal(lambda: c.pool_up("https://issuer.example.com/" + BUILD))
            self.assertEqual(r["reason"], "NameTakenOnSite", pool)
            self.assertEqual([m for m, _ in f.calls], ["GET"])


class GcpEdgeAccount(unittest.TestCase):
    def case(self, existing):
        e = GE.Edge.__new__(GE.Edge)
        e.project, e.site, e.prefix, e.tok, e.changed = "hub", "gcp-a", "shc-edge-gcp-a", "tok", []
        e.sa_email = "shc-edge-gcp-a@hub.iam.gserviceaccount.com"
        f = FakeIam(sas={"shc-edge-gcp-a": existing} if existing is not None else {})
        with mock.patch.object(GE.g, "http", f.http), mock.patch.object(GE.time, "sleep", lambda s: None):
            return refusal(e.ensure_sa), f

    def test_states(self):
        r, f = self.case(None)
        self.assertIsNone(r)
        self.assertEqual(f.sas["shc-edge-gcp-a"]["displayName"], "SHC edge router gcp-a")
        self.assertIsNone(self.case({"displayName": "SHC edge router gcp-a"})[0])
        for other in ({"email": "shc-edge-gcp-a@hub"}, {"displayName": "SHC edge router gcp-b"}):
            r, f = self.case(other)
            self.assertEqual(r["reason"], "NameTakenOnSite")
            self.assertNotIn("POST", [m for m, _ in f.calls])


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class AwsRoleOwnership(unittest.TestCase):
    """The gateway's IAM role and a cluster's operator roles: ours / unmarked / another's / absent."""

    class Iam:
        class exceptions:
            NoSuchEntityException = type("NoSuchEntity", (Exception,), {})

        def __init__(self, role):
            self.role, self.calls = role, []

        def get_role(self, RoleName):
            if self.role is None:
                raise self.exceptions.NoSuchEntityException(RoleName)
            return {"Role": dict(self.role, RoleName=RoleName)}

        def __getattr__(self, name):
            def call(**kw):
                self.calls.append(name)
                if name == "get_instance_profile":
                    return {"InstanceProfile": {"Roles": [{"RoleName": "shc-edge-aws-a"}]}}
                if name == "create_role":
                    return {"Role": {"Arn": "arn"}}
                return {}
            return call

    def test_gateway_role(self):
        import shc_aws_edge as AE
        ours = {"Description": "SHC edge router aws-a", "Tags": [{"Key": "shc-managed", "Value": "true"}]}
        for role, refused in ((None, False), (ours, False), ({"Description": "by hand", "Tags": []}, True),
                              ({"Description": "SHC edge router aws-b", "Tags": ours["Tags"]}, True)):
            e = AE.Edge.__new__(AE.Edge)
            e.site, e.prefix, e.changed, e.iam = "aws-a", "shc-edge-aws-a", [], self.Iam(role)
            e.param, e.cfg = "/shc/edge/aws-a/config", {"account": "111111111111", "region": "r"}
            # only the role step of up(): run it through a slice that stops before the instance profile
            r = refusal(lambda: self.role_step(e))
            if refused:
                self.assertEqual(r["reason"], "NameTakenOnSite", role)
                self.assertNotIn("put_role_policy", e.iam.calls)
            else:
                self.assertIsNone(r, role)
                self.assertIn("put_role_policy", e.iam.calls)

    @staticmethod
    def role_step(e):
        """The role part of Edge.up as it is written (read from the source, run on its own)."""
        import inspect
        import textwrap
        import shc_aws_edge as AE
        src = inspect.getsource(AE.Edge.up)
        start = src.index("        # the routers' role")
        end = src.index("        # the routers' config")
        body = textwrap.dedent(src[start:end])
        exec(compile(body, "role_step", "exec"), dict(vars(AE), self=e, c=e.cfg, json=json))

    def test_cluster_role_outside_its_path(self):
        A2 = __import__("shc_aws_cluster")
        c = A2.Cluster.__new__(A2.Cluster)
        c.cluster, c.path, c.issuer, c.changed = "shc-aws-acme-web-41df0c0d", "/shc/shc-aws-acme-web-41df0c0d/", \
            "https://issuer.example.com/x", []
        c.provider_arn, c.account = "arn:aws:iam::1:oidc-provider/issuer.example.com/x", "1"
        cr = {"metadata": {"name": "x"}, "spec": {"secretRef": {"namespace": "ns", "name": "n"},
                                                  "serviceAccountNames": ["sa"], "providerSpec": {}}}
        c.iam = self.Iam({"Path": "/elsewhere/", "AssumeRolePolicyDocument": {}})
        r = refusal(lambda: c.role_up(cr))
        self.assertEqual(r["reason"], "NameTakenOnSite")
        self.assertEqual(c.iam.calls, [])


# ---- the environment jobs: the rule for names, then one name per tenant across the four kinds --------------------

class EnvironmentJobs(unittest.TestCase):
    ROLES = {"CloudAWS": "cloudaws_provision", "CloudGCP": "cloudgcp_provision"}

    def block(self, role):
        return load(os.path.join(ROLES, role, "tasks", "main.yml"))[0]["block"]

    def test_checks_come_first_and_in_order(self):
        for kind, role in self.ROLES.items():
            tasks = self.block(role)
            names = [t.get("name") for t in tasks]
            first = names.index("Check the environment's names")
            unique = names.index("Check that the tenant has no other environment of this name")
            self.assertEqual(names[:first], ["Read the %s" % kind], role)          # before anything else
            self.assertLess(first, unique)
            self.assertLess(unique, names.index("Resolve the site"))
            inc = tasks[unique]
            self.assertTrue(inc["ansible.builtin.include_tasks"].endswith("environment_name_unique.yml"))
            self.assertEqual(inc["vars"]["enu_kind"], kind)
            stop = by_name(tasks, "Refuse a name another of the tenant's environments holds")
            self.assertEqual(stop["ansible.builtin.set_fact"]["fab_fail_reason"], "EnvironmentNameTaken")

    def test_no_dashboard_host_and_no_slug(self):
        for role in list(self.ROLES.values()) + ["cloudaws_teardown", "cloudgcp_teardown"]:
            for dirpath, _, files in os.walk(os.path.join(ROLES, role)):
                for f in files:
                    text = read(os.path.join(dirpath, f))
                    self.assertNotIn("eda_dashboard_host", text, f)
                    self.assertNotIn("slug", text.lower(), f)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_a_name_made_before_the_rule_is_refused(self):
        for kind, role in self.ROLES.items():
            tasks = self.block(role)
            names = [t.get("name") for t in tasks]
            part = tasks[names.index("Check the environment's names"): names.index("Stop on a refused name") + 1]
            steps = [{"ansible.builtin.set_fact": {"fab_name": "{{ case }}", "_refused": ""}},
                     {"block": part, "rescue": [{"ansible.builtin.set_fact": {"_refused": "{{ fab_fail_reason }}: {{ fab_fail_msg }}"}}]}]
            got = run_cases(steps, ["prod", "Prod", "prod.eu", "p" * 31], "_refused",
                            extra_vars={"role_path": os.path.join(ROLES, role), "fab_entity": "acme-corp"})
            self.assertEqual(got[0], "")
            for g in got[1:]:
                self.assertTrue(g.startswith("NameNotAllowed: %s " % kind), g)


# ---- a gateway's name: at most 21, checked before anything is made on either cloud -------------------------------

class GatewayName(unittest.TestCase):
    def test_the_check_comes_first(self):
        for cloud in ("aws", "gcp"):
            tasks = load(os.path.join(ROLES, "cloudgateway_provision", "tasks", "site_%s.yml" % cloud))
            self.assertEqual(tasks[0]["name"], "Check the gateway's name")
            self.assertEqual(tasks[0]["vars"]["name_check_items"][0]["limit"], 21)
            self.assertEqual(tasks[0]["vars"]["name_check_items"][0]["what"], "gateway")

    def test_21_keeps_every_edge_id_inside_its_platform(self):
        site = "g" * 21
        self.assertLessEqual(len("shc-edge-" + site), 30)                    # Google service account id 6-30
        self.assertGreaterEqual(len("shc-edge-a"), 6)
        self.assertLessEqual(len("shc-edge-%s-config" % site), 255)          # Secret Manager secret id
        self.assertLessEqual(len("shc-edge-%s-z" % site), 63)                # Compute Engine names
        self.assertLessEqual(len("shc-edge-" + site), 64)                    # IAM role name, STS session name
        self.assertEqual(shc_names.GATEWAY_NAME_MAX, 21)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_refusal(self):
        for cloud in ("aws", "gcp"):
            tasks = load(os.path.join(ROLES, "cloudgateway_provision", "tasks", "site_%s.yml" % cloud))[:3]
            steps = [{"ansible.builtin.set_fact": {"fab_name": "{{ case }}", "_refused": ""}},
                     {"block": tasks, "rescue": [{"ansible.builtin.set_fact": {"_refused": "{{ fab_fail_reason }}"}}]}]
            got = run_cases(steps, ["g" * 21, "g" * 22, "Edge-1"], "_refused",
                            extra_vars={"role_path": os.path.join(ROLES, "cloudgateway_provision")})
            self.assertEqual(got, ["", "NameNotAllowed", "NameNotAllowed"], cloud)


if __name__ == "__main__":
    unittest.main()

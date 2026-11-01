"""IPv6-only placements on Google Cloud and translation by path (design/IPV6-PLAN.md §6.4, §7.1, §11.12).

shc_gcp_tenant.py against the fake Compute Engine of test_gcp_tenant_ipv6, extended with what an IPv6-only VPC needs:
IPV6_ONLY subnets (no IPv4 range), Cloud DNS server policies (a policy in use can't be deleted, a VPC with a policy
can't go), Cloud NAT on the Cloud Router and its natStatus. Checked: the call sequence of an IPv6-only placement with
the provider's NAT64 path (DNS64 policy <vpc>-dns64, Cloud NAT <vpc>-nat64, Google's IPv4 default route kept for the
NAT), the policy and NAT following the translation switch, the NAT's address as ipv4Source once Google reports it,
teardown order, and dual-stack and IPv4-only placements calling exactly what they called before (every call, GETs
and bodies included, pinned by digest).

The placement job's task files in ansible-playbook (skipped when it is absent): families.yml (IPv6-only on the cloud
kinds when the site is switched, refused when not), ipv6_status.yml (translation by path for every kind and source),
the GCP backend's translation request and report, the site capabilities with both cloud sites switched, and the
GCP edge's net-up for a network VRF whose placement has no IPv4 prefix.

Addresses as in test_gcp_tenant_ipv6 (Google's ranges as on the real service, the network's from 3fff::/20); the
NAT's address is from TEST-NET-3."""
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_gcp_tenant_ipv6 as V  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_gcp_api as G  # noqa: E402
import shc_gcp_tenant as T  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
CI = os.path.join(ROLES, "cloudinfrastructure_provision")
REAL = V.REAL
N, PLACEMENT, VNI = V.N, V.PLACEMENT, V.VNI
NAT_IP = "203.0.113.46"
V6T = dict(V.V6, translation=True)
TRANSLATION = {"pref64": "64:ff9b::/96", "resolvers": ["fd20:ce::254"], "nat": N + "-nat64", "dnsPolicy": N + "-dns64"}


class FakeCloud(V.FakeCompute):
    """The V9 fake plus IPV6_ONLY subnets, Cloud DNS policies and Cloud NAT status."""

    def __init__(self):
        super().__init__()
        self.policies = {}
        self.nat_ips = []

    def http(self, method, url, body=None, token=None, headers=None, timeout=60, raw=False, ok404=False):
        u = urllib.parse.urlparse(url)
        if u.netloc == "dns.googleapis.com":
            return self.dns(method, u.path, body, ok404)
        if method == "GET" and u.path.endswith("/getRouterStatus"):
            self.calls.append((method, u.path[len(urllib.parse.urlparse(V.BASE).path):], None))
            rt = self.routers.get(u.path.split("/")[-2]) or {}
            st = [{"name": x["name"], "autoAllocatedNatIps": list(self.nat_ips), "minExtraNatIpsNeeded": 0}
                  for x in rt.get("nats", [])]
            return {"result": dict({"bgpPeerStatus": [], "bestRoutesForRouter": []}, **({"natStatus": st} if st else {}))}
        return super().http(method, url, body, token, headers, timeout, raw, ok404)

    def dns(self, method, path, body, ok404):
        prefix = "/dns/v1/projects/%s/policies" % V.PROJECT
        assert path.startswith(prefix), path
        name = path[len(prefix):].strip("/") or None
        self.calls.append((method, "dns:policies" + ("/" + name if name else ""), copy.deepcopy(body)))
        if method == "GET":
            if name not in self.policies:
                if ok404:
                    return None
                raise G.GcpError("GET %s: HTTP 404" % path, 404)
            return copy.deepcopy(self.policies[name])
        if method == "POST":
            for p in self.policies.values():
                if [x["networkUrl"] for x in p.get("networks", [])] == [x["networkUrl"] for x in body["networks"]]:
                    self.err("a network has one server policy")
            self.policies[body["name"]] = dict(copy.deepcopy(body), kind="dns#policy",
                                               networks=[dict(x, kind="dns#policyNetwork") for x in body["networks"]])
            return copy.deepcopy(self.policies[body["name"]])
        if method == "PATCH":
            self.policies[name].update(copy.deepcopy(body))
            return {"policy": copy.deepcopy(self.policies[name])}
        if method == "DELETE":
            if self.policies[name].get("networks"):
                self.err("policy %s is still bound to networks" % name)
            del self.policies[name]
            return {}
        raise AssertionError(method)

    def post_subnetworks(self, name, b):
        if b.get("stackType") != "IPV6_ONLY":
            return super().post_subnetworks(name, b)
        if "ipCidrRange" in b or b.get("secondaryIpRanges"):
            self.err("an IPV6_ONLY subnet has no IPv4 range")
        if b.get("ipv6AccessType") != "EXTERNAL":
            self.err("only external IPv6 in this fake")
        s = dict(b, fingerprint="fp-" + self._id(), selfLink=self.link("/regions/%s/subnetworks/%s" % (V.REGION, b["name"])))
        self._make_v6(s)
        self.subnets[b["name"]] = s

    def delete_networks(self, name, _):
        link = self.networks[name]["selfLink"]
        if any(x["networkUrl"] == link for p in self.policies.values() for x in p.get("networks", [])):
            self.err("network %s has a DNS server policy" % name)
        return super().delete_networks(name, _)

    def names(self):
        out = []
        for m, p, b in self.mutations():
            if p.startswith("dns:"):
                out.append("%s %s" % (m, p[4:] + ("/" + b["name"] if m == "POST" else "")))
                continue
            p = p.replace("/regions/%s" % V.REGION, "").replace("/global", "")
            out.append("%s %s" % (m, p.lstrip("/") + ("/" + b["name"] if m == "POST" and b and "name" in b else "")))
        return out


@contextlib.contextmanager
def fake(f):
    with mock.patch.object(G, "http", f.http), mock.patch.object(G, "Identity", V.StubIdentity):
        yield


def failure(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), unittest.TestCase().assertRaises(SystemExit):
        fn()
    return json.loads(buf.getvalue())["error"]


class Base(unittest.TestCase):
    FAKE = FakeCloud

    def setUp(self):
        self.f = self.FAKE()
        self.tmp = tempfile.mkdtemp(prefix="shc-gcp6-test-")
        self.tfile = os.path.join(self.tmp, "tunnels.json")
        self.env = mock.patch.dict(os.environ, {"SHC_EDGE_VPN_TUNNELS_FILE": self.tfile})
        self.env.start()
        counter = iter(range(1000))
        self.psk = mock.patch.object(T.secrets, "token_urlsafe", lambda n: "secret-%d" % next(counter))
        self.psk.start()

    def tearDown(self):
        self.psk.stop()
        self.env.stop()
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def tenant(self, **cfg):
        with fake(self.f):
            return T.Tenant(dict({"project": V.PROJECT, "region": V.REGION, "labels": {"shc-entity": "acme"}}, **cfg))

    def up(self, ipv6=V6T, prefixes=(), known=None, egress="fabric", **cfg):
        t = self.tenant(**cfg)
        with fake(self.f):
            out = t.place_up(PLACEMENT, VNI, list(prefixes), V.EDGE, egress, known, copy.deepcopy(ipv6))
        with open(self.tfile) as fh:
            self.tunnels_file = json.load(fh)
        return out, t.changed

    def known(self):
        return {r: [self.f.tunnels["%s-t%d" % (N, i)]["gwIp"]] for i, r in enumerate(V.ROUTERS)}

    def down(self):
        t = self.tenant()
        with fake(self.f):
            return t.place_down(PLACEMENT), t.changed

    def status(self):
        t = self.tenant()
        with fake(self.f):
            return t.place_status(PLACEMENT)


# ---- the IPv6-only placement ------------------------------------------------------------------------------------

class IPv6OnlyPlacement(Base):
    def test_new_placement_with_the_providers_nat64(self):
        out, changed = self.up()
        self.assertEqual(self.f.names(), [
            "POST networks/" + N,
            "POST subnetworks/" + N,
            "POST firewalls/%s-fabric" % N,                 # (the IPv4 rule stays: harmless without IPv4)
            "POST firewalls/%s-iap" % N,
            "POST firewalls/%s-fabric6" % N,
            "POST routers/" + N,
            "POST vpnGateways/" + N,
            "POST externalVpnGateways/shc-edge-gcp-test",
            "POST vpnTunnels/%s-t0" % N,
            "PATCH routers/" + N,
            "POST vpnTunnels/%s-t1" % N,
            "PATCH routers/" + N,
            "POST policies/%s-dns64" % N,
            "PATCH routers/" + N])                          # the Cloud NAT
        m = self.f.mutations()
        self.assertEqual(m[1][2], {"name": N, "network": self.f.networks[N]["selfLink"], "stackType": "IPV6_ONLY",
                                   "ipv6AccessType": "EXTERNAL"})
        self.assertEqual(m[4][2]["sourceRanges"], sorted([V.NET6, V.ICMP6, V.EXT6[0]]))
        self.assertEqual((m[6][2]["stackType"], m[6][2]["gatewayIpVersion"]), ("IPV4_IPV6", "IPV4"))
        peer = m[11][2]["bgpPeers"][-1]
        self.assertEqual((peer["enableIpv6"], peer["advertiseMode"], peer["advertisedIpRanges"]),
                         (True, "CUSTOM", [{"range": V.EXT6[0]}]))
        self.assertEqual(m[12][2], {"name": N + "-dns64", "description": "DNS64 for the placement's IPv6-only subnet",
                                    "networks": [{"networkUrl": self.f.networks[N]["selfLink"]}],
                                    "dns64Config": {"scope": {"allQueries": True}},
                                    "enableInboundForwarding": False, "enableLogging": False})
        self.assertEqual(m[13][2], {"nats": [{"name": N + "-nat64", "type": "PUBLIC", "natIpAllocateOption": "AUTO_ONLY",
                                              "sourceSubnetworkIpRangesToNat64": "ALL_IPV6_SUBNETWORKS"}]})
        # the NAT patch keeps the tunnels' interfaces and peers
        self.assertEqual(len(self.f.routers[N]["bgpPeers"]), 2)
        # Google's IPv4 default route stays (Cloud NAT forwards the translated IPv4 by it), the ::/0 too (internet)
        self.assertEqual(sorted(r["destRange"] for r in self.f.routes.values()), ["0.0.0.0/0", "::/0"])
        self.assertEqual(out["subnet"], None)
        self.assertEqual(out["ipv6"]["subnet"], V.EXT6[0])
        self.assertTrue(out["ipv6"]["only"])
        self.assertEqual(out["ipv6"]["translation"], TRANSLATION)        # no address before Google allocates one
        self.assertIn("dns policy %s-dns64" % N, changed)
        self.assertIn("nat %s-nat64" % N, changed)
        # the routers' tunnels still carry the IPv4 sessions with Google's IPv6 next hops
        for r in V.ROUTERS:
            e, = self.tunnels_file[r]
            self.assertTrue(e["psk"] and e["inside"].endswith("/30") and e["inside6"].endswith("/126") and e["bgp6"])

    def test_rerun_changes_nothing_and_reports_the_nats_address(self):
        self.up()
        self.f.reset()
        self.f.nat_ips = ["203.0.113.47", NAT_IP]
        out, changed = self.up(known=self.known())
        self.assertEqual(self.f.mutations(), [])
        self.assertEqual(changed, [])
        self.assertEqual(out["ipv6"]["translation"], dict(TRANSLATION, ipv4Source=NAT_IP))   # the lowest one

    def test_translation_off_removes_policy_and_nat_and_on_puts_them_back(self):
        self.up()
        for off in (dict(V6T, translation=False), dict(V6T, internet=False), dict(V.V6)):
            self.f.reset()
            out, changed = self.up(off, known=self.known())
            names = self.f.names()
            self.assertIn("PATCH routers/" + N, names, off)
            self.assertEqual(names[-2:], ["PATCH policies/%s-dns64" % N, "DELETE policies/%s-dns64" % N], off)
            self.assertEqual(self.f.routers[N].get("nats"), [], off)
            self.assertEqual(self.f.policies, {}, off)
            self.assertNotIn("translation", out["ipv6"], off)
            self.assertTrue(out["ipv6"]["only"])
            self.assertIn("deleted nat %s-nat64" % N, changed)
            self.assertIn("deleted dns policy %s-dns64" % N, changed)
            # off again: nothing left to remove
            self.f.reset()
            self.up(off, known=self.known())
            self.assertEqual([x for x in self.f.names() if "routes" not in x], [], off)
            self.up(known=self.known())
            self.assertEqual(sorted(self.f.policies), [N + "-dns64"])
            self.assertEqual([x["name"] for x in self.f.routers[N]["nats"]], [N + "-nat64"])

    def test_policy_and_nat_kept_in_step(self):
        self.up()
        self.f.policies[N + "-dns64"]["dns64Config"] = {"scope": {"allQueries": False}}
        self.f.routers[N]["nats"][0]["sourceSubnetworkIpRangesToNat"] = "ALL_SUBNETWORKS_ALL_IP_RANGES"
        self.f.routers[N]["nats"].append({"name": "someone-else"})
        self.f.reset()
        _, changed = self.up(known=self.known())
        self.assertEqual(self.f.names(), ["PATCH policies/%s-dns64" % N, "PATCH routers/" + N])
        m = self.f.mutations()
        self.assertEqual(m[0][2]["dns64Config"], {"scope": {"allQueries": True}})
        self.assertEqual(m[1][2]["nats"], [{"name": "someone-else"}, T.Tenant.nat64_body(N)])
        self.assertIn("nat %s-nat64 in step" % N, changed)

    def test_the_ipv4_route_for_the_nat_is_put_back(self):
        self.up()
        google4 = [k for k, r in self.f.routes.items() if r["destRange"] == "0.0.0.0/0"]
        for k in google4:
            del self.f.routes[k]
        self.f.reset()
        self.up(known=self.known())
        self.assertEqual(self.f.names(), ["POST routes/%s-default4" % N])
        b = self.f.mutations()[0][2]
        self.assertEqual((b["destRange"], b["nextHopGateway"]), ("0.0.0.0/0", V.BASE + "/global/gateways/default-internet-gateway"))
        # and the teardown takes it before the VPC (the fake refuses a VPC with a route of the platform's left)
        self.f.reset()
        self.down()
        self.assertIn("DELETE routes/%s-default4" % N, self.f.names())
        self.assertEqual(self.f.networks, {})

    def test_the_fabric_egress_keeps_googles_ipv4_route_on_an_ipv6_only_vpc(self):
        self.up(dict(V6T, translation=False))
        self.assertIn("0.0.0.0/0", [r["destRange"] for r in self.f.routes.values()])

    def test_place_status_reports_the_nat_and_the_policy(self):
        self.up()
        self.f.nat_ips = [NAT_IP]
        st = self.status()
        self.assertIsNone(st["subnet"])
        self.assertEqual(st["ipv6"]["subnet"], V.EXT6[0])
        self.assertTrue(st["ipv6"]["only"])
        self.assertEqual(st["ipv6"]["nat64"], {"name": N + "-nat64", "inStep": True, "addresses": [NAT_IP]})
        self.assertEqual(st["ipv6"]["dns64"], {"name": N + "-dns64", "inStep": True})
        self.up(dict(V6T, translation=False), known=self.known())
        st = self.status()
        self.assertEqual((st["ipv6"]["nat64"], st["ipv6"]["dns64"]), (None, None))

    def test_teardown_in_order(self):
        self.up()
        self.f.reset()
        out, changed = self.down()
        self.assertTrue(out["deleted"])
        self.assertEqual(self.f.names(), [
            "DELETE vpnTunnels/%s-t0" % N, "DELETE vpnTunnels/%s-t1" % N,
            "DELETE routers/" + N,                                   # the Cloud NAT is part of it
            "DELETE vpnGateways/" + N, "DELETE firewalls/%s-fabric" % N, "DELETE firewalls/%s-iap" % N,
            "DELETE firewalls/%s-fabric6" % N,
            "PATCH policies/%s-dns64" % N, "DELETE policies/%s-dns64" % N,   # detached, then deleted
            "DELETE subnetworks/" + N, "DELETE networks/" + N, "DELETE externalVpnGateways/shc-edge-gcp-test"])
        self.assertEqual(self.f.mutations()[7][2], {"networks": []})
        self.assertIn("deleted nat %s-nat64" % N, changed)
        self.assertEqual((self.f.networks, self.f.routes, self.f.policies, self.f.routers), ({}, {}, {}, {}))

    def test_teardown_without_translation(self):
        self.up(dict(V6T, internet=False))
        self.f.reset()
        self.down()
        self.assertFalse([x for x in self.f.names() if "polic" in x])
        self.assertEqual(self.f.networks, {})

    def test_refusals(self):
        # neither family
        self.assertIn("neither an IPv4 prefix nor IPv6", failure(lambda: self.up(None)))
        # a subnet's stack type can't change: dual-stack stays dual-stack, IPv6-only stays IPv6-only
        self.up(V.V6, prefixes=V.V4)
        self.assertIn("want IPV6_ONLY", failure(lambda: self.up(known=self.known())))
        self.f = FakeCloud()
        self.up()
        self.assertIn("exists as None, want %s" % V.V4[0], failure(lambda: self.up(V.V6, prefixes=V.V4, known=self.known())))

    def test_the_request_keeps_translation_only_when_given(self):
        self.assertEqual(T.ipv6_request(dict(V.V6, translation="true"))["translation"], True)
        self.assertEqual(T.ipv6_request(dict(V.V6, translation=False))["translation"], False)
        self.assertNotIn("translation", T.ipv6_request(V.V6))


# ---- dual-stack and IPv4-only placements: every call as before ----------------------------------------------------

class UnchangedPlacements(Base):
    FAKE = V.FakeCompute

    # sha256 of every call (method, path, body; GETs included) of place-up, a re-run, place-status and place-down,
    # recorded with the module as it was before IPv6-only placements existed, on this fake and with these secrets
    PINNED = {
        "dual-stack": (76, "baebde4aabf39dbf8c74b7d6c2211e81c1d1ca1502eecc67b115bee0d9064f49"),
        "dual-stack internet off": (77, "6c1fc48336e2f9cbf8e2a536c63588997f9fa65b561c6f164238f00959f91280"),
        "ipv4-only": (67, "85a3c268f8063a122a48b42379f904fdaecf3dec1b2ab729a7de53938c6fa379"),
        "ipv4-only ipv6Edge": (67, "ce73b312eb80cdcfddd4089c3884e0f6e4bbbcc004f0fcec1a495f958d004250"),
        "dual-stack cloudNat": (75, "e9072d9cc3f9f7824884e6b5f444579947a89de34938ed099dfc00489e1bd79c"),
    }

    def calls(self, ipv6, egress="fabric", **cfg):
        self.f = V.FakeCompute()
        self.up(ipv6, prefixes=V.V4, egress=egress, **cfg)
        self.up(ipv6, prefixes=V.V4, known=self.known(), egress=egress, **cfg)
        self.status()
        self.down()
        return self.f.calls

    def test_every_call_as_before(self):
        cases = {"dual-stack": (V.V6, {}), "dual-stack internet off": (dict(V.V6, internet=False), {}),
                 "ipv4-only": (None, {}), "ipv4-only ipv6Edge": (None, {"ipv6Edge": True}),
                 "dual-stack cloudNat": (V.V6, {"egress": "cloudNat"})}
        for name, (ipv6, kw) in cases.items():
            for variant in ([ipv6, dict(ipv6, translation=True)] if ipv6 else [ipv6]):
                # (the counter of the stub secrets starts again per case)
                self.psk.stop()
                counter = iter(range(1000))
                self.psk = mock.patch.object(T.secrets, "token_urlsafe", lambda n, c=counter: "psk%d" % next(c))
                self.psk.start()
                calls = self.calls(variant, **kw)
                digest = hashlib.sha256(json.dumps(calls, sort_keys=True).encode()).hexdigest()
                self.assertEqual((len(calls), digest), self.PINNED[name], (name, variant))


# ---- the placement job's task files -------------------------------------------------------------------------------

SITE = V.SITE
NET_P6 = {"spec": {"prefixes": [], "platformPrefixes": [{"family": 6}], "egress": {"internet": True}},
          "status": {"ipv6": {"mode": "Platform", "prefixes": [V.NET6]}}}
PREF64_LAB = "2001:db8:64::/96"
DNS64_LAB = ["2001:db8:53::10", "2001:db8:53::11"]
BACKEND_TR = {"path": "Provider", "pref64": "64:ff9b::/96", "resolvers": ["fd20:ce::254"], "ipv4Source": NAT_IP}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FamiliesOnClouds(unittest.TestCase):
    def refuse(self, kind, spec, switched=True, caps_only=True):
        tasks = load(os.path.join(NP, "families.yml")) + [write_out("{'refuse': np_refuse, 'want': np_want}")]
        on = {"placement": {"ipFamiliesBySite": {SITE: [4, 6]}, "ipv6OnlyBySite": {SITE: switched},
                            "ipv6SourcesBySite": {SITE: ["Network", "Provider"]}}}
        caps = dict(V.CAPS, ipv6NetworkSource=True, ipv6Only=caps_only)
        return run_play(tasks, {"fab_spec": spec, "fab_name": "p1", "np_backend_kind": kind, "np_site": SITE,
                                "np_network": V.NET_P, "ci_capabilities": caps, "fab_real": V.merged(REAL, {**on})})

    def test_ipv6_only_offered_on_switched_cloud_sites(self):
        for kind, spec in (("CloudGCP", {"ipv6": {}}), ("CloudAWS", {"ipv6": {"source": "Provider"}}),
                           ("CloudAWS", {"ipv6": {"prefixLength": 60}})):
            out = self.refuse(kind, spec)
            self.assertEqual(out["refuse"], {}, (kind, spec))
            self.assertEqual([w["family"] for w in out["want"]], [6])
        self.assertEqual(self.refuse("CloudGCP", {"ipv6": {}})["want"], [{"family": 6, "source": "Provider"}])

    def test_still_refused_where_the_site_doesnt_offer_it(self):
        msg = {"reason": "FamilyUnavailable", "msg": "IPv6-only placements are not offered on this site (%s)" % SITE}
        for kind in ("CloudGCP", "CloudAWS"):
            self.assertEqual(self.refuse(kind, {"ipv6": {"source": "Provider"}}, switched=False)["refuse"], msg, kind)
            self.assertEqual(self.refuse(kind, {"ipv6": {"source": "Provider"}}, caps_only=False)["refuse"], msg, kind)
        # the other rules still come first: GCP source Network
        self.assertEqual(self.refuse("CloudGCP", {"ipv6": {"source": "Network"}})["refuse"]["msg"],
                         "this site offers IPv6 only from the cloud provider; use source Provider")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TranslationByPath(unittest.TestCase):
    TR = {SITE: {"pref64": PREF64_LAB, "dns64Resolvers": DNS64_LAB, "provider": True}}

    def describe(self, kind, source, v4=(), internet=True, egress6=True, cap=True, translation=None, backend=None,
                 mode="Platform", seed_translation="kept"):
        task = by_name(load(os.path.join(NP, "ipv6_status.yml")), "Describe the placement's IPv6")
        fab_real = V.merged(REAL, {"egress": {"ipv6": {"enabled": egress6}}})
        fab_real["translation"] = copy.deepcopy(self.TR if translation is None else translation)
        start = {"ansible.builtin.set_fact": {"np_status": "{{ seed_status }}"}}
        net = V.merged(NET_P6, {"spec": {"egress": {"internet": internet}}})
        variables = {"seed_status": {"entity": "acme", "ipv6": {"translation": seed_translation}},
                     "np_prefixes_v4": list(v4), "np_prefixes_v6": ["2001:db8:4005:4200::/64"],
                     "np_backend_kind": kind, "np_ipv6_source": source, "np_ipv6_mode": mode, "np_site": SITE,
                     "np_network": net, "fab_real": fab_real,
                     "ci_capabilities": {"ipv6": True, "ipv6Only": True, "ipv6Translation": cap}}
        tasks = [start]
        if backend is not None:
            tasks.append({"ansible.builtin.set_fact": {"np_backend_translation": backend}})
        out = run_play(tasks + [task, write_out("{'ipv6': np_status.ipv6, 'tr': np_translation}")], variables)
        return out["ipv6"], out["tr"]

    def test_source_network_on_every_kind_is_the_fabric_path(self):
        want = {"path": "Fabric", "pref64": PREF64_LAB, "resolvers": DNS64_LAB}
        for kind in ("CloudVirt", "CloudOSO", "CloudAWS"):
            ipv6, tr = self.describe(kind, "Network")
            self.assertEqual(ipv6["translation"], want, kind)
            self.assertEqual(tr, want, kind)
            # it still needs the fabric's IPv6 egress and the site's capability; never the backend's report
            for kw in ({"egress6": False}, {"cap": False}, {"internet": False}, {"v4": ["10.70.4.0/24"]},
                       {"translation": {SITE: {"provider": True}}}):
                ipv6, tr = self.describe(kind, "Network", backend=BACKEND_TR, **kw)
                self.assertIsNone(ipv6["translation"], (kind, kw))
                self.assertEqual(tr, {}, (kind, kw))

    def test_source_provider_is_pending_before_the_backend(self):
        for kind in ("CloudAWS", "CloudGCP"):
            ipv6, tr = self.describe(kind, "Provider")
            self.assertEqual(tr, {"path": "Provider", "pending": True}, kind)
            # status keeps what it held (merge patch): the key is not written
            self.assertNotIn("translation", ipv6, kind)
            # whatever the fabric's IPv6 egress and the site's capability say
            ipv6, tr = self.describe(kind, "Provider", egress6=False, cap=False)
            self.assertEqual(tr["path"], "Provider", kind)

    def test_source_provider_publishes_the_backends_report(self):
        for kind in ("CloudAWS", "CloudGCP"):
            ipv6, tr = self.describe(kind, "Provider", backend=BACKEND_TR)
            self.assertEqual(ipv6["translation"], BACKEND_TR, kind)
            self.assertEqual(tr, BACKEND_TR, kind)
            no_addr = {k: v for k, v in BACKEND_TR.items() if k != "ipv4Source"}
            ipv6, _ = self.describe(kind, "Provider", backend=dict(no_addr, ipv4Source=""))
            self.assertEqual(ipv6["translation"], no_addr, kind)
            # a report without a prefix is no report
            ipv6, tr = self.describe(kind, "Provider", backend={"path": "Provider"})
            self.assertEqual(tr, {"path": "Provider", "pending": True}, kind)

    def test_source_provider_not_qualifying_writes_null(self):
        cases = {"no internet": {"internet": False}, "dual-stack": {"v4": ["10.70.4.0/24"]},
                 "site without provider translation": {"translation": {SITE: {"pref64": PREF64_LAB}}},
                 "provider switched off": {"translation": {SITE: {"provider": False}}},
                 "no entry for the site": {"translation": {}}, "tenant prefixes": {"mode": "Tenant"}}
        for kind in ("CloudAWS", "CloudGCP"):
            for what, kw in cases.items():
                ipv6, tr = self.describe(kind, "Provider", backend=BACKEND_TR, **kw)
                self.assertIn("translation", ipv6, (kind, what))
                self.assertIsNone(ipv6["translation"], (kind, what))
                self.assertEqual(tr, {}, (kind, what))

    def test_the_job_starts_with_no_report(self):
        tasks = load(os.path.join(NP, "main.yml"))[0]["block"]
        names = [t.get("name") for t in tasks]
        i = names.index("No provider translation reported yet")
        self.assertEqual(tasks[i]["ansible.builtin.set_fact"], {"np_backend_translation": {}})
        self.assertLess(i, names.index("The placement's IPv6 in status"))
        self.assertLess(names.index("The placement's IPv6 in status"), names.index("The site side ({{ np_backend_kind }})"))


class TranslationSchema(unittest.TestCase):
    def test_the_crd_declares_path_and_ipv4_source(self):
        from test_placement_ipv6_status import CRDS, status_schema
        for path in CRDS:
            tr = status_schema(path)["ipv6"]["properties"]["translation"]["properties"]
            self.assertEqual(set(tr), {"path", "pref64", "resolvers", "ipv4Source"}, path)
            self.assertIn("Fabric", tr["path"]["description"])
            self.assertIn("Provider", tr["path"]["description"])
            self.assertEqual(tr["ipv4Source"]["type"], "string")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GcpBackendTasks(unittest.TestCase):
    BLOCK = os.path.join(NP, "backend_cloudgcp.yml")

    def request(self, v4, translation):
        task = by_name(load(self.BLOCK), "The placement's IPv6 here")
        variables = {"np_want": ([{"family": 4, "prefixLength": 24}] if v4 else []) + [{"family": 6, "source": "Provider"}],
                     "np_network": V.NET_P, "np_ipv6_mode": "Platform", "np_prefixes_v4": list(v4),
                     "fab_real": V.merged(REAL, {"core": {"icmpSource6": V.ICMP6}})}
        if translation is not None:
            variables["np_translation"] = translation
        return run_play([task, write_out("np_gcp_ipv6")], variables)

    def test_the_request_asks_for_translation_on_ipv6_only_placements(self):
        base = {"source": "Provider", "internet": True, "fabricSources": [V.NET6, V.ICMP6]}
        self.assertEqual(self.request([], {"path": "Provider", "pending": True}), dict(base, translation=True))
        for tr in ({}, None, {"path": "Fabric", "pref64": PREF64_LAB, "resolvers": []}):
            self.assertEqual(self.request([], tr), dict(base, translation=False), tr)
        # dual-stack: the request as before
        self.assertEqual(self.request(V.V4, {"path": "Provider", "pending": True}), base)

    def report(self, place):
        task = by_name(load(self.BLOCK), "The provider's translation")
        return run_play([task, write_out("np_backend_translation")], {"np_gcp_place": place})

    def test_the_report(self):
        tr = dict(TRANSLATION, ipv4Source=NAT_IP)
        self.assertEqual(self.report({"ipv6": {"subnet": V.EXT6[0], "translation": tr}}), BACKEND_TR)
        self.assertEqual(self.report({"ipv6": {"subnet": V.EXT6[0], "translation": TRANSLATION}}),
                         {k: v for k, v in BACKEND_TR.items() if k != "ipv4Source"})
        self.assertEqual(self.report({"ipv6": {"subnet": V.EXT6[0], "only": True}}), {})
        self.assertEqual(self.report({}), {})

    def test_the_report_comes_before_the_second_description(self):
        names = [t.get("name") for t in load(self.BLOCK)]
        self.assertLess(names.index("Keep the cloud side"), names.index("The provider's translation"))
        self.assertLess(names.index("The provider's translation"), names.index("The provider's IPv6 subnet in the network's address space"))

    def test_second_description_publishes_it(self):
        """The provider subnet recorded, then subnets.yml and ipv6_status.yml as the backend runs them."""
        keep = by_name(load(self.BLOCK), "Keep the rows with the allocated prefixes")
        start = {"ansible.builtin.set_fact": {
            "np_status": {"entity": "acme", "prefixes": []}, "np_prefixes": [],
            "np_backend_translation": BACKEND_TR,
            "fab_ipam_result": {"changed": True, "subnets": [{"family": 6, "prefix": V.EXT6[0], "source": "Provider"}]}}}
        tasks = ([start, keep] + load(os.path.join(NP, "subnets.yml")) + load(os.path.join(NP, "ipv6_status.yml"))
                 + [write_out("{'status': np_status, 'tr': np_translation}")])
        real = V.merged(REAL, {"translation": {SITE: {"provider": True}}})
        out = run_play(tasks, {"np_backend_kind": "CloudGCP", "np_network": NET_P6, "np_ipv6_source": "Provider",
                               "np_ipv6_mode": "Platform", "np_site": SITE, "ci_capabilities": V.CAPS, "fab_real": real})
        self.assertEqual(out["status"]["subnets"], [{"family": 6, "prefix": V.EXT6[0], "source": "Provider"}])
        self.assertEqual(out["status"]["ipv6"], {"addressing": "DHCPv6", "internet": {"state": "ready", "path": "Provider"},
                                                 "translation": BACKEND_TR})

    def test_site_status_records_the_ipv6_only_side(self):
        task = by_name(load(self.BLOCK), "Record the site side's IPv6")
        start = {"ansible.builtin.set_fact": {"np_backend_status": {"vpc": N}}}
        place = {"ipv6": {"subnet": V.EXT6[0], "nexthops": {}, "only": True, "translation": TRANSLATION}}
        out = run_play([start, task, write_out("np_backend_status")], {"np_gcp_ipv6": {"internet": True}, "np_gcp_place": place})
        self.assertEqual(out["ipv6"], {"source": "Provider", "subnet": V.EXT6[0], "internet": True, "nexthops": {},
                                       "only": True, "nat64": N + "-nat64", "dns64Policy": N + "-dns64"})

    def test_net_up_arguments_need_no_ipv4_prefix(self):
        task = by_name(load(self.BLOCK), "The routers' VRF and tunnels for this network")
        out = run_play([V.task_vars(task), write_out("_out")],
                       {"gcp_edge_cfg": {}, "np_l3vni": VNI, "_np_gcp_tunnels": {"path": "/tmp/x"},
                        "np_gcp_ipv6": {"source": "Provider"}, "np_gcp_network6": [V.NET6],
                        "np_gcp_place": {"subnet": None, "ipv6": {"subnet": V.EXT6[0], "only": True}}})["gcp_args"]
        self.assertEqual(out[:-1], ["net-up", "--vni", str(VNI), "--ipv6"])
        self.assertEqual(json.loads(out[-1]), {"prefixes": [V.EXT6[0]], "network": [V.NET6]})


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CloudSiteCapabilities(unittest.TestCase):
    """The site job with both cloud sites switched as the lab switches them: IPv6-only and translation (for source
    Network the fabric's entry, for source Provider just provider: true)."""

    REAL6 = {"placement": {"ipFamiliesBySite": {"aws-use2": [4, 6], "gcp-use4": [4, 6]},
                           "ipv6OnlyBySite": {"aws-use2": True, "gcp-use4": True},
                           "ipv6SourcesBySite": {"aws-use2": ["Network", "Provider"], "gcp-use4": ["Provider"]}},
             "translation": {"aws-use2": {"pref64": PREF64_LAB, "dns64Resolvers": DNS64_LAB, "provider": True},
                             "gcp-use4": {"provider": True}}}

    def caps(self, ctype, name, real):
        tasks = ([{"ansible.builtin.set_fact": {"ci_capabilities_out": {"api": True}}}]
                 + load(os.path.join(CI, "tasks", "ipv6_capabilities.yml")) + [write_out("ci_capabilities_out")])
        return run_play(tasks, {"role_path": CI, "ci_cr_type": ctype, "cloudinfrastructure_cr_name": name,
                                "fabric_realization": real})

    def test_both_cloud_sites_read_ipv6_only_and_translation(self):
        aws = self.caps("aws", "aws-use2", self.REAL6)
        gcp = self.caps("gcp", "gcp-use4", self.REAL6)
        for out in (aws, gcp):
            self.assertTrue(out["ipv6"] and out["ipv6Only"] and out["ipv6Translation"] and out["ipv6ProviderSource"], out)
        self.assertTrue(aws["ipv6NetworkSource"])
        self.assertFalse(gcp["ipv6NetworkSource"])

    def test_without_the_switches_they_read_false(self):
        real = copy.deepcopy(self.REAL6)
        real["placement"]["ipv6OnlyBySite"] = {}
        real["translation"] = {}
        for ctype, name in (("aws", "aws-use2"), ("gcp", "gcp-use4")):
            out = self.caps(ctype, name, real)
            self.assertFalse(out["ipv6Only"] or out["ipv6Translation"], out)
            self.assertTrue(out["ipv6"])


class GcpEdgeIPv6OnlyVrf(unittest.TestCase):
    """net-up never takes an IPv4 prefix of the placement: the VRF's IPv4 side is the BGP session on the tunnel's
    link-local /30 (it carries the IPv6 routes), so a VRF of an IPv6-only placement is the dual-stack one."""

    def test_net_up_and_the_routers_render(self):
        import test_gcp_edge_ipv6 as EV
        e = EV.FakeEdge(dict(EV.edge_cfg(), overlayMtu=1370), EV.site_conf())
        out = EV.net_up(e, EV.file_tunnels("s-a", "s-b"), ipv6=EV.six(), use_file=True)
        self.assertEqual(out["ipv6"], EV.six())
        vrf, = e.store["vrfs"]
        self.assertEqual(vrf["ipv6"], EV.six())
        # nothing of an IPv4 prefix in the VRF: only the tunnels' session addresses
        self.assertEqual(sorted(vrf), ["ipv6", "mtu", "name", "peerAsn", "vni", "vpn"])
        conf = dict(EV.agent_cfg(), vrfs=[dict(vrf)])
        frr = EV.render(conf)
        self.assertIn(" address-family ipv6 unicast\n  neighbor VPN activate", frr)
        self.assertIn("  advertise ipv6 unicast route-map t51003-evpn6", frr)
        self.assertIn("ipv6 prefix-list t51003-in6 seq 5 permit %s le 128" % EV.SUBNET6, frr)


if __name__ == "__main__":
    unittest.main()

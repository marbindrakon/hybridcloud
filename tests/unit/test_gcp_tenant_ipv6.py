"""IPv6 on a Google Cloud placement (design/IPV6-PLAN.md §6.3, §11.11): shc_gcp_tenant.py against a fake Compute
Engine REST API that records the calls and enforces the rules the spike found (a gateway's stack type is fixed at
creation, IPv6 on a BGP peer needs a dual-stack gateway, one family per firewall rule, a VPC can't go while a static
route of its own is left), and the placement and teardown task files in ansible-playbook (skipped when it is absent).

Source Provider only: the subnet dual-stack with an external /64 from Google (an IPv4-only subnet patched in place);
the HA VPN gateway dual-stack (re-created when it exists IPV4_ONLY, the tunnels with new secrets); the Cloud Router's
peers with enableIpv6 and the /64 as a custom advertisement; firewall <vpc>-fabric6; Google's ::/0 only while the
network wants internet; the tunnels file lists every tunnel with Google's next-hop pair. Teardown in reverse.

Addresses: Google's external ranges come from 2600:1900::/28 and the next hops from 2600:2d00:0:2::/63, as on the
real service; the network's IPv6 space is from the documentation prefix 3fff::/20 (RFC 9637)."""
import contextlib
import copy
import io
import ipaddress
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
NP = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks")
NPD = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_teardown", "tasks")
REAL = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]

sys.path.insert(0, FILES)
import shc_gcp_api as G  # noqa: E402
import shc_gcp_tenant as T  # noqa: E402

PROJECT, REGION = "tenant-p", "xx-east1"
BASE = "%s/projects/%s" % (G.COMPUTE, PROJECT)
PLACEMENT = "acme-p1"
N = "shc-" + PLACEMENT
VNI = 1001
V4 = ["10.70.4.0/24"]
NET6 = "3fff:0:4000::/48"          # the network's IPv6 space
ICMP6 = "3fff:0:ffff::/64"         # the fabric's ICMPv6 sources
EXT6 = ["2600:1900:4090:640::/64", "2600:1900:4090:641::/64"]
EDGE = {"site": "gcp-test", "asn": 65010, "routers": [{"name": "shc-edge-gcp-test-b", "publicIp": "198.51.100.12"},
                                                      {"name": "shc-edge-gcp-test-a", "publicIp": "198.51.100.11"}]}
ROUTERS = ["shc-edge-gcp-test-a", "shc-edge-gcp-test-b"]
V6 = {"source": "Provider", "internet": True, "fabricSources": [NET6, ICMP6]}


class FakeCompute:
    """Just enough Compute Engine for a placement. Every call is recorded as (method, path, body)."""

    def __init__(self):
        self.calls = []
        self.n = 0
        self.networks, self.subnets, self.routes, self.firewalls = {}, {}, {}, {}
        self.routers, self.gateways, self.tunnels, self.externals = {}, {}, {}, {}
        self.instances = []
        self.next_ext = list(EXT6)
        self.next_hop = 0xa5a9
        self.next_ip = 1

    # ---- plumbing
    def _id(self):
        self.n += 1
        return "%016x" % (0xabc0 + self.n)

    def link(self, path):
        return BASE + path

    def http(self, method, url, body=None, token=None, headers=None, timeout=60, raw=False, ok404=False):
        u = urllib.parse.urlparse(url)
        path = u.path[len(urllib.parse.urlparse(BASE).path):]
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((method, path, copy.deepcopy(body)))
        parts = path.strip("/").split("/")
        if parts[0] == "regions":
            parts = parts[2:]
        kind, name = parts[1] if parts[0] == "global" else parts[0], None
        if parts[0] == "global":
            name = parts[2] if len(parts) > 2 else None
        else:
            name = parts[1] if len(parts) > 1 else None
        if path.startswith("/aggregated/instances"):
            return {"items": {"zones/x": {"instances": copy.deepcopy(self.instances)}}}
        store = {"networks": self.networks, "subnetworks": self.subnets, "routes": self.routes,
                 "firewalls": self.firewalls, "routers": self.routers, "vpnGateways": self.gateways,
                 "vpnTunnels": self.tunnels, "externalVpnGateways": self.externals}[kind]
        if method == "GET" and name and len(parts) > (3 if parts[0] == "global" else 2):
            return {"result": {"bgpPeerStatus": [], "bestRoutesForRouter": []}}  # getRouterStatus
        if method == "GET" and name:
            if name not in store:
                if ok404:
                    return None
                raise G.GcpError("GET %s: HTTP 404" % path, 404)
            return copy.deepcopy(store[name])
        if method == "GET":
            items = list(store.values())
            flt = (q.get("filter") or [""])[0]
            if flt.startswith("network eq"):
                net = flt.split("/")[-1].rstrip('"')
                items = [x for x in items if x.get("network", "").endswith("/networks/" + net)]
            return {"items": copy.deepcopy(items)}
        handler = getattr(self, "%s_%s" % (method.lower(), kind))
        handler(name, copy.deepcopy(body))
        return {"status": "DONE", "selfLink": self.link("/operations/x"), "name": "op"}

    @staticmethod
    def err(msg):
        raise G.GcpError(msg, 400)

    # ---- networks and subnets
    def post_networks(self, _, b):
        self.networks[b["name"]] = dict(b, selfLink=self.link("/global/networks/" + b["name"]))
        rid = "default-route-" + self._id()
        self.routes[rid] = {"name": rid, "network": self.networks[b["name"]]["selfLink"], "destRange": "0.0.0.0/0",
                            "nextHopGateway": self.link("/global/gateways/default-internet-gateway"), "priority": 1000}

    def delete_networks(self, name, _):
        link = self.networks[name]["selfLink"]
        users = [k for k, v in list(self.subnets.items()) + list(self.firewalls.items()) + list(self.routers.items())
                 + list(self.gateways.items()) if v.get("network") == link]
        users += [k for k, v in self.routes.items() if v["network"] == link and not k.startswith("default-route-")]
        if users:
            self.err("network %s is in use by %s" % (name, users))
        self.routes = {k: v for k, v in self.routes.items() if v["network"] != link}
        del self.networks[name]

    def _system_v6_route(self, network):
        if not any(r["network"] == network and r["destRange"] == "::/0" for r in self.routes.values()):
            rid = "default-route-" + self._id()
            self.routes[rid] = {"name": rid, "network": network, "destRange": "::/0", "priority": 1000,
                                "nextHopGateway": self.link("/global/gateways/default-internet-gateway")}

    def _make_v6(self, s):
        s["externalIpv6Prefix"] = self.next_ext.pop(0)
        self._system_v6_route(s["network"])

    def post_subnetworks(self, _, b):
        s = dict(b, stackType=b.get("stackType", "IPV4_ONLY"), fingerprint="fp-" + self._id(),
                 selfLink=self.link("/regions/%s/subnetworks/%s" % (REGION, b["name"])))
        if s["stackType"] == "IPV4_IPV6":
            if b.get("ipv6AccessType") != "EXTERNAL":
                self.err("only external IPv6 in this fake")
            self._make_v6(s)
        self.subnets[b["name"]] = s

    def patch_subnetworks(self, name, b):
        s = self.subnets[name]
        if b.get("fingerprint") != s["fingerprint"]:
            self.err("subnet %s: fingerprint mismatch" % name)
        if s["stackType"] == "IPV4_ONLY" and b.get("stackType") == "IPV4_IPV6":
            s.update(stackType="IPV4_IPV6", ipv6AccessType=b["ipv6AccessType"])
            self._make_v6(s)
        s["fingerprint"] = "fp-" + self._id()

    def delete_subnetworks(self, name, _):
        del self.subnets[name]

    # ---- routes and firewalls
    def post_routes(self, _, b):
        self.routes[b["name"]] = b

    def delete_routes(self, name, _):
        del self.routes[name]

    def post_firewalls(self, _, b):
        fams = {ipaddress.ip_network(r).version for r in b["sourceRanges"]}
        if len(fams) != 1:
            self.err("firewall %s: a rule holds one family" % b["name"])
        self.firewalls[b["name"]] = b

    def patch_firewalls(self, name, b):
        if len({ipaddress.ip_network(r).version for r in b["sourceRanges"]}) != 1:
            self.err("one family per rule")
        self.firewalls[name].update(b)

    def delete_firewalls(self, name, _):
        del self.firewalls[name]

    # ---- the Cloud Router, the HA VPN gateway and its tunnels
    def post_routers(self, _, b):
        self.routers[b["name"]] = dict(b, interfaces=[], bgpPeers=[])

    def patch_routers(self, name, b):
        rt = self.routers[name]
        ifs = {x["name"]: x for x in b.get("interfaces", rt["interfaces"])}
        for p in b.get("bgpPeers", []):
            if p.get("enableIpv6"):
                t = self.tunnels[ifs[p["interfaceName"]]["linkedVpnTunnel"].split("/")[-1]]
                gw = self.gateways[t["vpnGateway"].split("/")[-1]]
                if gw["stackType"] != "IPV4_IPV6":
                    self.err("peer %s: IPv6 needs a dual-stack gateway" % p["name"])
                if not p.get("ipv6NexthopAddress"):
                    p["ipv6NexthopAddress"] = "2600:2d00:0:2::%x" % self.next_hop
                    p["peerIpv6NexthopAddress"] = "2600:2d00:0:2::%x" % (self.next_hop + 1)
                    self.next_hop += 4
            if p.get("advertisedIpRanges"):
                p["advertisedIpRanges"] = [dict(r, description="") for r in p["advertisedIpRanges"]]
        rt.update(b)

    def delete_routers(self, name, _):
        del self.routers[name]

    def post_vpnGateways(self, _, b):
        ips = []
        for _i in range(2):
            ips.append("34.157.0.%d" % self.next_ip)
            self.next_ip += 1
        self.gateways[b["name"]] = dict(b, stackType=b.get("stackType", "IPV4_ONLY"),
                                        vpnInterfaces=[{"id": i, "ipAddress": ip} for i, ip in enumerate(ips)],
                                        selfLink=self.link("/regions/%s/vpnGateways/%s" % (REGION, b["name"])))

    def delete_vpnGateways(self, name, _):
        if any(t["vpnGateway"].endswith("/" + name) for t in self.tunnels.values()):
            self.err("gateway %s is in use by tunnels" % name)
        del self.gateways[name]

    def post_vpnTunnels(self, _, b):
        gw = self.gateways[b["vpnGateway"].split("/")[-1]]
        self.tunnels[b["name"]] = dict(b, peerIp=self.externals[b["peerExternalGateway"].split("/")[-1]]["interfaces"][b["peerExternalGatewayInterface"]]["ipAddress"],
                                       gwIp=gw["vpnInterfaces"][b["vpnGatewayInterface"]]["ipAddress"],
                                       selfLink=self.link("/regions/%s/vpnTunnels/%s" % (REGION, b["name"])))

    def delete_vpnTunnels(self, name, _):
        del self.tunnels[name]

    def post_externalVpnGateways(self, _, b):
        self.externals[b["name"]] = dict(b, selfLink=self.link("/global/externalVpnGateways/" + b["name"]))

    def delete_externalVpnGateways(self, name, _):
        del self.externals[name]

    # ---- reading the calls
    def mutations(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]

    def names(self):
        """Mutations as 'METHOD kind[/name]' (the region left out)."""
        out = []
        for m, p, b in self.mutations():
            p = p.replace("/regions/%s" % REGION, "").replace("/global", "")
            out.append("%s %s" % (m, p.lstrip("/") + ("/" + b["name"] if m == "POST" and b and "name" in b else "")))
        return out

    def reset(self):
        self.calls = []


class StubIdentity:
    def project(self, project):
        return "token"


@contextlib.contextmanager
def fake(f):
    with mock.patch.object(G, "http", f.http), mock.patch.object(G, "Identity", StubIdentity):
        yield


def tenant(f, **cfg):
    with fake(f):
        return T.Tenant(dict({"project": PROJECT, "region": REGION, "labels": {"shc-entity": "acme"}}, **cfg))


class GcpTenantIpv6Test(unittest.TestCase):
    def setUp(self):
        self.f = FakeCompute()
        self.tmp = tempfile.mkdtemp(prefix="shc-gcp-test-")
        self.tfile = os.path.join(self.tmp, "tunnels.json")
        self.env = mock.patch.dict(os.environ, {"SHC_EDGE_VPN_TUNNELS_FILE": self.tfile})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def up(self, ipv6=None, known=None, egress="fabric", **cfg):
        t = tenant(self.f, **cfg)
        with fake(self.f):
            out = t.place_up(PLACEMENT, VNI, V4, EDGE, egress, known, ipv6)
        with open(self.tfile) as fh:
            self.tunnels_file = json.load(fh)
        return out, t.changed

    def known(self):
        """What the routers hold after net-up stored the tunnels file: every tunnel's peer."""
        return {r: [self.f.tunnels["%s-t%d" % (N, i)]["gwIp"]] for i, r in enumerate(ROUTERS)}

    def down(self):
        t = tenant(self.f)
        with fake(self.f):
            return t.place_down(PLACEMENT), t.changed

    # ---- a new dual-stack placement
    def test_new_dual_stack_placement(self):
        out, _ = self.up(V6)
        self.assertEqual(self.f.names(), [
            "POST networks/" + N,
            "POST subnetworks/" + N,
            self.f.names()[2],                       # the VPC's IPv4 default route (fabric egress), checked below
            "POST firewalls/%s-fabric" % N,
            "POST firewalls/%s-iap" % N,
            "POST firewalls/%s-fabric6" % N,
            "POST routers/" + N,
            "POST vpnGateways/" + N,
            "POST externalVpnGateways/shc-edge-gcp-test",
            "POST vpnTunnels/%s-t0" % N,
            "PATCH routers/" + N,
            "POST vpnTunnels/%s-t1" % N,
            "PATCH routers/" + N])
        m = self.f.mutations()
        self.assertEqual(m[1][2]["stackType"], "IPV4_IPV6")
        self.assertEqual(m[1][2]["ipv6AccessType"], "EXTERNAL")
        self.assertTrue(m[2][0] == "DELETE" and m[2][1].startswith("/global/routes/default-route-"))
        self.assertEqual(m[5][2]["sourceRanges"], sorted([NET6, ICMP6, EXT6[0]]))
        self.assertEqual(m[5][2]["allowed"], [{"IPProtocol": "all"}])
        self.assertEqual(m[5][2]["direction"], "INGRESS")
        self.assertEqual((m[7][2]["stackType"], m[7][2]["gatewayIpVersion"]), ("IPV4_IPV6", "IPV4"))
        peer = m[12][2]["bgpPeers"][-1]
        self.assertEqual((peer["name"], peer["enableIpv6"], peer["advertiseMode"], peer["advertisedGroups"],
                          peer["advertisedIpRanges"]),
                         ("peer-t1", True, "CUSTOM", ["ALL_SUBNETS"], [{"range": EXT6[0]}]))
        self.assertNotIn("ipv6NexthopAddress", peer)               # Google assigns the pair; no pool of ours
        # Google's own ::/0 stays (internet on); the IPv4 default went (fabric egress)
        self.assertEqual([r["destRange"] for r in self.f.routes.values()], ["::/0"])
        hops = {"%s-t0" % N: {"cloud": "2600:2d00:0:2::a5a9", "router": "2600:2d00:0:2::a5aa"},
                "%s-t1" % N: {"cloud": "2600:2d00:0:2::a5ad", "router": "2600:2d00:0:2::a5ae"}}
        self.assertEqual(out["ipv6"], {"subnet": EXT6[0], "nexthops": hops})
        # the tunnels file: every tunnel, with Google's pair (the router's side /126, Google's side as the BGP next hop)
        self.assertEqual(sorted(self.tunnels_file), ROUTERS)
        for i, r in enumerate(ROUTERS):
            e, = self.tunnels_file[r]
            self.assertEqual(sorted(e), ["bgp", "bgp6", "ifId", "inside", "inside6", "peerIp", "psk"])
            self.assertTrue(e["psk"])
            self.assertEqual(e["inside6"], hops["%s-t%d" % (N, i)]["router"] + "/126")
            self.assertEqual(e["bgp6"], [hops["%s-t%d" % (N, i)]["cloud"]])
            self.assertEqual(e["ifId"], VNI)
        # stdout never carries a secret
        self.assertNotIn("psk", json.dumps(out))
        self.assertEqual(out["tunnels"][ROUTERS[0]][0]["inside6"], "2600:2d00:0:2::a5aa/126")

    def test_rerun_changes_nothing_and_lists_every_tunnel(self):
        self.up(V6)
        self.f.reset()
        out, changed = self.up(V6, known=self.known())
        self.assertEqual(self.f.mutations(), [])
        self.assertEqual(changed, [])
        for r in ROUTERS:
            e, = self.tunnels_file[r]
            self.assertIsNone(e["psk"])                               # the routers keep theirs
            self.assertTrue(e["inside6"].endswith("/126") and e["bgp6"])
        self.assertEqual(out["ipv6"]["subnet"], EXT6[0])

    # ---- IPv4-only placements
    IPV4_SEQUENCE = None

    def ipv4_names(self):
        return ["POST networks/" + N, "POST subnetworks/" + N, self.f.names()[2],
                "POST firewalls/%s-fabric" % N, "POST firewalls/%s-iap" % N, "POST routers/" + N,
                "POST vpnGateways/" + N, "POST externalVpnGateways/shc-edge-gcp-test",
                "POST vpnTunnels/%s-t0" % N, "PATCH routers/" + N, "POST vpnTunnels/%s-t1" % N, "PATCH routers/" + N]

    def test_ipv4_only_placement_as_before(self):
        out, _ = self.up()
        self.assertEqual(self.f.names(), self.ipv4_names())
        m = self.f.mutations()
        self.assertNotIn("stackType", m[1][2])
        self.assertEqual(m[6][2], {"name": N, "network": self.f.networks[N]["selfLink"],
                                   "labels": {"shc-managed": "true", "shc-entity": "acme", "shc-placement": PLACEMENT,
                                              "shc-vni": str(VNI)}})
        self.assertEqual(m[11][2]["bgpPeers"][-1], {"name": "peer-t1", "interfaceName": "if-t1",
                                                    "peerIpAddress": "169.254.31.78", "peerAsn": 65010})
        self.assertNotIn("ipv6", out)
        for r in ROUTERS:
            self.assertEqual(sorted(self.tunnels_file[r][0]), ["bgp", "ifId", "inside", "peerIp", "psk"])
        self.assertEqual(sorted(out["tunnels"][ROUTERS[0]][0]), ["bgp", "ifId", "inside", "peerIp"])

    def test_ipv4_only_placement_on_an_ipv6_edge_gets_a_dual_stack_gateway(self):
        out, _ = self.up(ipv6Edge=True)
        self.assertEqual(self.f.names(), self.ipv4_names())
        self.assertEqual(self.f.gateways[N]["stackType"], "IPV4_IPV6")
        self.assertEqual(self.f.subnets[N]["stackType"], "IPV4_ONLY")
        self.assertFalse(any(p.get("enableIpv6") for p in self.f.routers[N]["bgpPeers"]))
        self.assertNotIn("ipv6", out)
        # the realization switch arrives as a string from a template too
        self.f = FakeCompute()
        self.up(ipv6Edge="False")
        self.assertEqual(self.f.gateways[N]["stackType"], "IPV4_ONLY")

    # ---- an existing IPv4-only placement takes IPv6
    def test_existing_ipv4_subnet_is_patched_in_place(self):
        self.up(ipv6Edge=True)
        self.f.reset()
        fp = self.f.subnets[N]["fingerprint"]
        out, _ = self.up(V6, known=self.known(), ipv6Edge=True)
        self.assertEqual(self.f.names(), ["PATCH subnetworks/" + N, "POST firewalls/%s-fabric6" % N,
                                          "PATCH routers/" + N])
        m = self.f.mutations()
        self.assertEqual(m[0][2], {"stackType": "IPV4_IPV6", "ipv6AccessType": "EXTERNAL", "fingerprint": fp})
        self.assertEqual([p["enableIpv6"] for p in m[2][2]["bgpPeers"]], [True, True])   # both peers, one patch
        self.assertEqual(self.f.subnets[N]["ipCidrRange"], V4[0])
        self.assertEqual(out["ipv6"]["subnet"], EXT6[0])
        for r in ROUTERS:
            e, = self.tunnels_file[r]
            self.assertIsNone(e["psk"])                               # no tunnel churn on a dual-stack gateway
            self.assertTrue(e["inside6"])

    def test_ipv4_only_gateway_is_recreated(self):
        self.up()
        old_ips = [x["ipAddress"] for x in self.f.gateways[N]["vpnInterfaces"]]
        ext_before = copy.deepcopy(self.f.externals)
        self.f.reset()
        out, changed = self.up(V6, known=self.known())
        self.assertEqual(self.f.names(), [
            "PATCH subnetworks/" + N, "POST firewalls/%s-fabric6" % N,
            "PATCH routers/" + N,                                       # the tunnels' interfaces and peers go first
            "DELETE vpnTunnels/%s-t0" % N, "DELETE vpnTunnels/%s-t1" % N,
            "DELETE vpnGateways/" + N, "POST vpnGateways/" + N,
            "POST vpnTunnels/%s-t0" % N, "PATCH routers/" + N, "POST vpnTunnels/%s-t1" % N, "PATCH routers/" + N])
        self.assertEqual(self.f.mutations()[2][2], {"interfaces": [], "bgpPeers": []})
        self.assertEqual(self.f.mutations()[6][2]["stackType"], "IPV4_IPV6")
        self.assertEqual(self.f.externals, ext_before)                # the routers' external gateway is untouched
        self.assertIn("ha vpn gateway %s recreated dual-stack (tunnels with new secrets)" % N, changed)
        new_ips = out["haVpnGateway"]["interfaces"]
        self.assertNotEqual(new_ips, old_ips)
        for i, r in enumerate(ROUTERS):
            e, = self.tunnels_file[r]
            self.assertTrue(e["psk"])                                 # new secrets for the new tunnels
            self.assertEqual(e["peerIp"], new_ips[i])
            self.assertTrue(e["inside6"] and e["bgp6"])
        # a second run keeps the new gateway
        self.f.reset()
        self.up(V6, known=self.known())
        self.assertEqual(self.f.mutations(), [])

    def test_ipv4_only_gateway_stays_for_an_ipv4_placement(self):
        self.up()
        self.f.reset()
        self.up(known=self.known(), ipv6Edge=True)
        self.assertEqual(self.f.mutations(), [])
        self.assertEqual(self.f.gateways[N]["stackType"], "IPV4_ONLY")

    # ---- kept in step
    def test_peer_advertisement_and_firewall_kept_in_step(self):
        self.up(V6)
        rt = self.f.routers[N]
        rt["bgpPeers"][0]["advertiseMode"] = "DEFAULT"
        rt["bgpPeers"][0]["advertisedIpRanges"] = []
        rt["bgpPeers"][1]["advertisedIpRanges"] = [{"range": "2600:1900:4090:999::/64"}]
        hops = copy.deepcopy([(p["ipv6NexthopAddress"], p["peerIpv6NexthopAddress"]) for p in rt["bgpPeers"]])
        self.f.reset()
        more = dict(V6, fabricSources=[NET6, ICMP6, "3fff:0:5000::/48"])
        self.up(more, known=self.known())
        self.assertEqual(self.f.names(), ["PATCH firewalls/%s-fabric6" % N, "PATCH routers/" + N])
        m = self.f.mutations()
        self.assertEqual(m[0][2], {"sourceRanges": sorted([NET6, ICMP6, "3fff:0:5000::/48", EXT6[0]])})
        for p in m[1][2]["bgpPeers"]:
            self.assertEqual((p["advertiseMode"], p["advertisedGroups"], p["advertisedIpRanges"]),
                             ("CUSTOM", ["ALL_SUBNETS"], [{"range": EXT6[0]}]))
        # Google's next hops are kept
        self.assertEqual([(p["ipv6NexthopAddress"], p["peerIpv6NexthopAddress"]) for p in self.f.routers[N]["bgpPeers"]],
                         hops)

    def test_default_route_follows_internet(self):
        def v6routes():
            return sorted(k for k, r in self.f.routes.items() if r["destRange"] == "::/0")
        self.up(dict(V6, internet=False))
        self.assertEqual(v6routes(), [])                              # Google's own went at once
        self.f.reset()
        _, changed = self.up(dict(V6, internet=True), known=self.known())
        self.assertEqual(self.f.names(), ["POST routes/%s-default6" % N])
        b = self.f.mutations()[0][2]
        self.assertEqual((b["destRange"], b["nextHopGateway"], b["network"]),
                         ("::/0", BASE + "/global/gateways/default-internet-gateway", self.f.networks[N]["selfLink"]))
        self.assertIn("ipv6 default internet route", changed)
        self.f.reset()
        self.up(dict(V6, internet=True), known=self.known())
        self.assertEqual(self.f.mutations(), [])
        self.up(dict(V6, internet=False), known=self.known())
        self.assertEqual(self.f.names(), ["DELETE routes/%s-default6" % N])
        self.assertEqual(v6routes(), [])

    def test_the_ipv4_default_route_is_left_to_the_ipv4_egress(self):
        self.up(V6, egress="cloudNat")
        self.assertEqual(sorted(r["destRange"] for r in self.f.routes.values()), ["0.0.0.0/0", "::/0"])

    # ---- status and refusals
    def test_place_status_reports_ipv6(self):
        self.up(V6)
        t = tenant(self.f)
        with fake(self.f):
            st = t.place_status(PLACEMENT)
        self.assertEqual(st["ipv6"]["subnet"], EXT6[0])
        self.assertEqual(sorted(st["ipv6"]["nexthops"]), ["%s-t0" % N, "%s-t1" % N])
        self.f = FakeCompute()
        self.up()
        t = tenant(self.f)
        with fake(self.f):
            self.assertNotIn("ipv6", t.place_status(PLACEMENT))

    def test_refusals_in_the_module(self):
        for bad in ({"source": "Network"}, {"source": "Provider", "fabricSources": ["10.0.0.0/8"]}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                T.ipv6_request(bad)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["ok"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            T.ipv6_request({"source": "Network"})
        self.assertEqual(json.loads(buf.getvalue())["reason"], "FamilyUnavailable")
        self.assertEqual(T.ipv6_request({"internet": "true", "fabricSources": ["3fff:0:4000:0::/48", NET6]}),
                         {"source": "Provider", "internet": True, "fabricSources": [NET6]})
        self.assertIsNone(T.ipv6_request({}))

    def test_a_peer_without_next_hops_stops_the_run(self):
        self.up(V6)
        for p in self.f.routers[N]["bgpPeers"]:
            p.pop("peerIpv6NexthopAddress")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            self.up(V6, known=self.known())
        self.assertIn("no IPv6 next hops", json.loads(buf.getvalue())["error"])

    # ---- teardown
    def test_teardown_in_reverse(self):
        self.up(dict(V6, internet=False))
        self.up(V6, known=self.known())                                # puts <vpc>-default6 back
        self.f.reset()
        out, _ = self.down()
        self.assertTrue(out["deleted"])
        self.assertEqual(self.f.names(), [
            "DELETE vpnTunnels/%s-t0" % N, "DELETE vpnTunnels/%s-t1" % N, "DELETE routers/" + N,
            "DELETE vpnGateways/" + N, "DELETE firewalls/%s-fabric" % N, "DELETE firewalls/%s-iap" % N,
            "DELETE firewalls/%s-fabric6" % N, "DELETE subnetworks/" + N, "DELETE routes/%s-default6" % N,
            "DELETE networks/" + N, "DELETE externalVpnGateways/shc-edge-gcp-test"])
        self.assertEqual((self.f.networks, self.f.routes, self.f.firewalls), ({}, {}, {}))

    def test_teardown_with_googles_route_only(self):
        self.up(V6)
        self.f.reset()
        self.down()
        self.assertNotIn("DELETE routes/%s-default6" % N, self.f.names())
        self.assertEqual(self.f.routes, {})                           # Google's own went with the VPC

    def test_teardown_waits_for_instances(self):
        self.up(V6)
        self.f.instances = [{"name": "vm1", "networkInterfaces": [{"network": self.f.networks[N]["selfLink"]}]}]
        self.f.reset()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            self.down()
        self.assertTrue(json.loads(buf.getvalue())["waiting"])
        self.assertEqual(self.f.mutations(), [])

    def test_cli_passes_ipv6(self):
        seen = {}

        class Stub:
            changed = []

            def __init__(self, cfg):
                pass

            def __getattr__(self, name):
                return lambda *a: {}

            def place_up(self, *a):
                seen["args"] = a
                return {}

        argv = ["shc_gcp_tenant.py", "--config", "{}", "place-up", "--placement", PLACEMENT, "--vni", str(VNI),
                "--prefixes", json.dumps(V4), "--edge", json.dumps(EDGE), "--known-peers", "{}", "--ipv6", json.dumps(V6)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(T, "Tenant", Stub), \
                contextlib.redirect_stdout(io.StringIO()):
            T.main()
        self.assertEqual(seen["args"][-1], V6)
        self.assertEqual(seen["args"][-2], {})
        with mock.patch.object(sys, "argv", argv[:-2]), mock.patch.object(T, "Tenant", Stub), \
                contextlib.redirect_stdout(io.StringIO()):
            T.main()
        self.assertIsNone(seen["args"][-1])


def merged(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


SITE = "gcp-test"
CAPS = {"ipv6": True, "ipv6Only": True, "ipv6NetworkSource": False, "ipv6ProviderSource": True, "ipv6Translation": True}
NET_P = {"spec": {"prefixes": ["10.70.0.0/16"], "platformPrefixes": [{"family": 6}], "egress": {"internet": True}},
         "status": {"ipv6": {"mode": "Platform", "prefixes": [NET6]}}}
NET_T = {"spec": {"prefixes": ["10.70.0.0/16", "fd5c:8::/48"]}, "status": {"ipv6": {"mode": "Tenant", "prefixes": ["fd5c:8::/48"]}}}


def task_vars(task):
    """A task's vars evaluated into one fact (an include's vars can't be read back otherwise)."""
    return {"vars": task["vars"], "ansible.builtin.set_fact": {"_out": {k: "{{ %s }}" % k for k in task["vars"]
                                                                          if not k.startswith("_")}}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GcpPlacementTasksTest(unittest.TestCase):
    def families(self, spec, network=NET_P, caps=CAPS, out="np_refuse"):
        tasks = load(os.path.join(NP, "families.yml")) + [write_out(out)]
        on = {"placement": {"ipFamiliesBySite": {SITE: [4, 6]}, "ipv6OnlyBySite": {SITE: True},
                            "ipv6SourcesBySite": {SITE: ["Provider"]}}}
        return run_play(tasks, {"fab_spec": spec, "fab_name": "p1", "np_backend_kind": "CloudGCP", "np_site": SITE,
                                "np_network": network, "ci_capabilities": caps, "fab_real": merged(REAL, on)})

    def test_refusals(self):
        tenant_msg = {"reason": "FamilyUnavailable", "msg": "this site can't carry tenant-provided IPv6 prefixes"}
        # the mode first: with or without a source, never the default source's refusal
        for v6 in ({}, {"source": "Provider"}, {"source": "Network"}):
            self.assertEqual(self.families({"ipv4": {"prefixLength": 24}, "ipv6": v6}, network=NET_T), tenant_msg, v6)
        self.assertEqual(self.families({"ipv4": {"prefixLength": 24}, "ipv6": {"source": "Network"}}),
                         {"reason": "FamilyUnavailable",
                          "msg": "this site offers IPv6 only from the cloud provider; use source Provider"})
        # IPv6-only: offered on a switched site (test_gcp_ipv6_only), refused where the site doesn't offer it
        self.assertEqual(self.families({"ipv6": {}}), {})
        self.assertEqual(self.families({"ipv6": {}}, caps=dict(CAPS, ipv6Only=False)),
                         {"reason": "FamilyUnavailable",
                          "msg": "IPv6-only placements are not offered on this site (%s)" % SITE})
        self.assertEqual(self.families({"ipv4": {"prefixLength": 24}, "ipv6": {}}), {})

    def test_the_default_source_is_provider(self):
        want = self.families({"ipv4": {"prefixLength": 24}, "ipv6": {}}, out="np_want")
        self.assertEqual(want, [{"prefixLength": 24, "family": 4}, {"family": 6, "source": "Provider"}])

    def ipv6_here(self, want6, network=NET_P, icmp6=ICMP6):
        task = by_name(load(os.path.join(NP, "backend_cloudgcp.yml")), "The placement's IPv6 here")
        want = [{"family": 4, "prefixLength": 24}] + ([want6] if want6 else [])
        return run_play([task, write_out("{'v6': np_gcp_ipv6, 'net6': np_gcp_network6}")],
                        {"np_want": want, "np_network": network, "np_ipv6_mode": "Platform",
                         "fab_real": merged(REAL, {"core": {"icmpSource6": icmp6}})})

    def test_the_ipv6_request(self):
        out = self.ipv6_here({"family": 6, "source": "Provider"})
        self.assertEqual(out, {"v6": {"source": "Provider", "internet": True, "fabricSources": [NET6, ICMP6]},
                               "net6": [NET6]})
        off = merged(NET_P, {"spec": {"egress": {"internet": False}}})
        self.assertFalse(self.ipv6_here({"family": 6, "source": "Provider"}, network=off)["v6"]["internet"])
        self.assertEqual(self.ipv6_here({"family": 6, "source": "Provider"}, icmp6="")["v6"]["fabricSources"], [NET6])
        self.assertEqual(self.ipv6_here(None)["v6"], {})

    def call_args(self, name, v6, place=None):
        task = by_name(load(os.path.join(NP, "backend_cloudgcp.yml")), name)
        return run_play([task_vars(task), write_out("_out")],
                        {"np_gcp_cfg": {}, "gcp_edge_cfg": {}, "np_gcp_placement": PLACEMENT, "np_l3vni": VNI,
                         "np_prefixes_v4": V4, "np_gcp_edge": EDGE, "np_gcp_egress": "fabric", "np_gcp_known": {},
                         "_np_gcp_tunnels": {"path": "/tmp/x"}, "np_gcp_ipv6": v6, "np_gcp_network6": [NET6],
                         "np_gcp_place": place or {}})["gcp_args"]

    def test_place_up_and_net_up_arguments(self):
        up = "The placement's VPC and its HA VPN to the site's routers"
        net = "The routers' VRF and tunnels for this network"
        def parsed(args):
            return [json.loads(a) if i in (6, 8, 12) else a for i, a in enumerate(args)]
        base = ["place-up", "--placement", PLACEMENT, "--vni", str(VNI), "--prefixes", V4, "--edge", EDGE,
                "--egress", "fabric", "--known-peers", {}]
        self.assertEqual(parsed(self.call_args(up, {})), base)    # IPv4 only: as before
        self.assertEqual(self.call_args(net, {}), ["net-up", "--vni", str(VNI)])
        v6 = {"source": "Provider", "internet": True, "fabricSources": [NET6, ICMP6]}
        args = self.call_args(up, v6)
        self.assertEqual(parsed(args[:-2]), base)
        self.assertEqual(args[-2], "--ipv6")
        self.assertEqual(json.loads(args[-1]), v6)
        args = self.call_args(net, v6, {"ipv6": {"subnet": EXT6[0], "nexthops": {}}})
        self.assertEqual(args[:-1], ["net-up", "--vni", str(VNI), "--ipv6"])
        self.assertEqual(json.loads(args[-1]), {"prefixes": [EXT6[0]], "network": [NET6]})

    def provider_status(self, internet):
        """The provider subnet recorded (ipam-external's rows), then subnets.yml and ipv6_status.yml as the backend
        runs them."""
        block = load(os.path.join(NP, "backend_cloudgcp.yml"))
        keep = by_name(block, "Keep the rows with the allocated prefixes")
        start = {"ansible.builtin.set_fact": {
            "np_status": {"entity": "acme", "prefixes": V4},
            "np_prefixes": V4,
            "fab_ipam_result": {"changed": True, "subnets": [{"family": 4, "prefix": V4[0], "source": "Network"},
                                                             {"family": 6, "prefix": EXT6[0], "source": "Provider"}]}}}
        net = merged(NET_P, {"spec": {"egress": {"internet": internet}}})
        tasks = ([start, keep] + load(os.path.join(NP, "subnets.yml")) + load(os.path.join(NP, "ipv6_status.yml"))
                 + [write_out("{'status': np_status, 'v6': np_prefixes_v6, 'v4': np_prefixes_v4}")])
        return run_play(tasks, {"np_backend_kind": "CloudGCP", "np_network": net, "np_ipv6_source": "Provider",
                                "np_ipv6_mode": "Platform", "np_site": SITE, "ci_capabilities": CAPS,
                                "fab_real": REAL})

    def test_provider_subnet_in_status(self):
        out = self.provider_status(True)
        st = out["status"]
        self.assertEqual(st["prefixes"], V4)
        self.assertEqual(st["subnets"], [{"family": 4, "prefix": V4[0], "source": "Network", "gateway": "10.70.4.1"},
                                         {"family": 6, "prefix": EXT6[0], "source": "Provider"}])
        self.assertEqual(out["v6"], [EXT6[0]])                       # the gateway's route check looks for it
        self.assertEqual(st["ipv6"], {"addressing": "DHCPv6", "internet": {"state": "ready", "path": "Provider"},
                                      "translation": None})
        self.assertIsNone(self.provider_status(False)["status"]["ipv6"]["internet"])

    def test_the_ledger_entry(self):
        task = by_name(load(os.path.join(NP, "backend_cloudgcp.yml")), "Record the provider-assigned subnet")
        out = run_play([task_vars(task), write_out("_out")],
                       {"np_fabric": {"metadata": {"name": "fab"}}, "fab_namespace": "acme", "fab_spec": {"network": "n1"},
                        "np_owner": "NetworkPlacement/acme/p1", "np_gcp_place": {"ipv6": {"subnet": EXT6[0]}}})
        self.assertEqual(out["fab_ipam"], {"action": "ipam-external", "fabric": "fab", "network": "HybridNetwork/acme/n1",
                                           "owner": "NetworkPlacement/acme/p1",
                                           "external": [{"family": 6, "prefix": EXT6[0]}]})

    def test_site_status_records_ipv6(self):
        task = by_name(load(os.path.join(NP, "backend_cloudgcp.yml")), "Record the site side's IPv6")
        hops = {"%s-t0" % N: {"cloud": "2600:2d00:0:2::a5a9", "router": "2600:2d00:0:2::a5aa"}}
        start = {"ansible.builtin.set_fact": {"np_backend_status": {"vpc": N}}}   # extra vars can't be overridden
        out = run_play([start, task, write_out("np_backend_status")],
                       {"np_gcp_ipv6": {"internet": False},
                        "np_gcp_place": {"ipv6": {"subnet": EXT6[0], "nexthops": hops}}})
        self.assertEqual(out, {"vpc": N, "ipv6": {"source": "Provider", "subnet": EXT6[0], "internet": False,
                                                  "nexthops": hops}})

    def test_the_edge_switch_reaches_the_tenant_config(self):
        task = by_name(load(os.path.join(NP, "backend_cloudgcp.yml")), "The environment's project and the placement's names")
        tasks = [task, write_out("np_gcp_cfg.ipv6Edge")]
        common = {"np_backend": {"status": {"project": PROJECT, "region": REGION}}, "fab_entity": "acme",
                  "fab_spec": {"network": "n1"}, "fab_namespace": "acme", "fab_name": "p1",
                  "np_gateway": {"metadata": {"name": SITE}, "status": {}}, "np_asn": 65010}
        self.assertIs(run_play(tasks, dict(common, gcp_real={"edge": {"ipv6": {"enabled": True}}})), True)
        self.assertIs(run_play(tasks, dict(common, gcp_real={"edge": {}})), False)


if __name__ == "__main__":
    unittest.main()

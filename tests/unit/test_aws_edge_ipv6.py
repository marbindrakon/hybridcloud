"""IPv6 on the AWS edge (design/IPV6-PLAN.md §6.1-§6.2): Connect peers with an IPv6 /125 next to the IPv4 /29 (pool
allocation, the one-time re-create of a peer without one), the routers' per-network IPv6 configuration (the gateway
job's vrfs6 settings composed with the peers' addresses), the router agent's FRR and kernel rendering per IPv6 source
and mode (the IPv4-only render unchanged), its state report, and the gateway job's tasks that build the per-network
settings. The AWS APIs are fakes: nothing reaches the network. Documentation and private ranges only."""
import copy
import io
import ipaddress
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
try:
    import boto3  # noqa: F401
    import botocore.exceptions  # noqa: F401
except ImportError:  # stub the SDK so the module imports; every call below goes to the fakes
    _b3 = types.ModuleType("boto3")
    _b3.client = _b3.Session = None
    _bc = types.ModuleType("botocore")
    _bce = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, response=None, op=""):
            super().__init__(str(response))
            self.response = response or {"Error": {"Code": ""}}

    _bce.ClientError = ClientError
    _bc.exceptions = _bce
    sys.modules.setdefault("boto3", _b3)
    sys.modules.setdefault("botocore", _bc)
    sys.modules.setdefault("botocore.exceptions", _bce)
import shc_aws_edge as E  # noqa: E402
import shc_edge_agent as A  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
CG = os.path.join(ROLES, "cloudgateway_provision")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
COMMON_TASKS = os.path.join(ROOT, "eda", "common", "tasks")
REAL = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]

POOL6 = "fd00:db8:5c::/64"
ACCOUNT = "111111111111"


def cfg(ipv6=True, pool6=POOL6):
    c = {"account": ACCOUNT, "region": "region-1", "site": "site-x", "vpcName": "hub", "tgwName": "tgw",
         "tgwCidr": "203.0.113.0/24", "edgeSubnets": ["edge-a", "edge-b"], "transportSubnets": ["tr-a", "tr-b"],
         "instanceType": "t.small", "ami": {"owner": "1", "name": "x"}, "asn": 64512,
         "tunnel": {"cidr": "192.0.2.0/24", "mtu": 1420, "listenPort": 51820},
         "wireguardSources": ["198.51.100.1/32"], "insideCidrPool": "169.254.100.0/24"}
    if ipv6 is not None:
        c["ipv6"] = {"enabled": ipv6, "insideCidrPool6": pool6}
    return c


def tags_of(spec):
    return {t["Key"]: t["Value"] for s in spec or [] for t in s["Tags"]}


def match(obj, filters):
    tm = {t["Key"]: t["Value"] for t in obj.get("Tags", [])}
    for f in filters or []:
        n, vals = f["Name"], f["Values"]
        if n.startswith("tag:"):
            if tm.get(n[4:]) not in vals:
                return False
        elif n == "state" and obj.get("State") not in vals:
            return False
        elif n == "transit-gateway-id" and obj.get("TransitGatewayId", "tgw-0") not in vals:
            return False
        elif n == "transit-gateway-attachment-id" and obj.get("TransitGatewayAttachmentId") not in vals:
            return False
    return True


class FakeEC2:
    """The EC2 calls net-up makes, against an in-memory transit gateway; every call is recorded."""

    def __init__(self, routers=2):
        self.calls = []
        self.tgw = {"TransitGatewayId": "tgw-0", "State": "available", "Options": {"AmazonSideAsn": 64513}}
        self.attachments = [{"TransitGatewayAttachmentId": "tgw-attach-tr", "ResourceType": "vpc", "State": "available",
                             "TransitGatewayId": "tgw-0", "ResourceOwnerId": ACCOUNT,
                             "Tags": [{"Key": "shc-edge-site", "Value": "site-x"}, {"Key": "shc-role", "Value": "transport"}]}]
        self.route_tables, self.assoc, self.props, self.peers = [], {}, {}, []
        self.instances = [{"InstanceId": "i-%s" % x, "PrivateIpAddress": "198.51.100.%d" % (10 + n),
                           "State": {"Name": "running"}, "InstanceType": "t.small",
                           "Tags": [{"Key": "Name", "Value": "shc-edge-site-x-%s" % x}]}
                          for n, x in enumerate("ab"[:routers])]
        self.n = 0

    def _id(self, p):
        self.n += 1
        return "%s-%d" % (p, self.n)

    def __getattr__(self, name):
        raise AssertionError("unexpected EC2 call " + name)

    def rec(self, name, kw):
        self.calls.append((name, copy.deepcopy(kw)))

    def describe_transit_gateways(self, **kw):
        return {"TransitGateways": [self.tgw]}

    def describe_transit_gateway_attachments(self, Filters=None, TransitGatewayAttachmentIds=None):
        if TransitGatewayAttachmentIds:
            return {"TransitGatewayAttachments": [a for a in self.attachments
                                                  if a["TransitGatewayAttachmentId"] in TransitGatewayAttachmentIds]}
        return {"TransitGatewayAttachments": [a for a in self.attachments if match(a, Filters)]}

    def describe_transit_gateway_route_tables(self, Filters=None, TransitGatewayRouteTableIds=None):
        if TransitGatewayRouteTableIds:
            return {"TransitGatewayRouteTables": [r for r in self.route_tables
                                                  if r["TransitGatewayRouteTableId"] in TransitGatewayRouteTableIds]}
        return {"TransitGatewayRouteTables": [r for r in self.route_tables if match(r, Filters)]}

    def create_transit_gateway_route_table(self, **kw):
        self.rec("create_transit_gateway_route_table", kw)
        rt = {"TransitGatewayRouteTableId": self._id("tgw-rtb"), "State": "available",
              "Tags": [{"Key": k, "Value": v} for k, v in tags_of(kw["TagSpecifications"]).items()]}
        self.route_tables.append(rt)
        return {"TransitGatewayRouteTable": rt}

    def create_transit_gateway_connect(self, **kw):
        self.rec("create_transit_gateway_connect", kw)
        a = {"TransitGatewayAttachmentId": self._id("tgw-attach-c"), "ResourceType": "connect", "State": "available",
             "TransitGatewayId": "tgw-0", "ResourceOwnerId": ACCOUNT,
             "Tags": [{"Key": k, "Value": v} for k, v in tags_of(kw["TagSpecifications"]).items()]}
        self.attachments.append(a)
        return {"TransitGatewayConnect": a}

    def get_transit_gateway_route_table_associations(self, TransitGatewayRouteTableId):
        return {"Associations": [{"TransitGatewayAttachmentId": a, "State": "associated"}
                                 for a in self.assoc.get(TransitGatewayRouteTableId, [])]}

    def associate_transit_gateway_route_table(self, TransitGatewayRouteTableId, TransitGatewayAttachmentId):
        self.assoc.setdefault(TransitGatewayRouteTableId, []).append(TransitGatewayAttachmentId)

    def get_transit_gateway_route_table_propagations(self, TransitGatewayRouteTableId):
        return {"TransitGatewayRouteTablePropagations": [{"TransitGatewayAttachmentId": a, "State": "enabled"}
                                                         for a in self.props.get(TransitGatewayRouteTableId, [])]}

    def enable_transit_gateway_route_table_propagation(self, TransitGatewayRouteTableId, TransitGatewayAttachmentId):
        self.props.setdefault(TransitGatewayRouteTableId, []).append(TransitGatewayAttachmentId)

    def describe_transit_gateway_connect_peers(self, Filters=None, TransitGatewayConnectPeerIds=None):
        ps = self.peers
        if TransitGatewayConnectPeerIds:
            ps = [p for p in ps if p["TransitGatewayConnectPeerId"] in TransitGatewayConnectPeerIds]
        return {"TransitGatewayConnectPeers": [copy.deepcopy(p) for p in ps if match(p, Filters)]}

    def add_peer(self, attachment, peer_address, blocks, tags=None, state="available"):
        v4 = ipaddress.ip_network(blocks[0])
        p = {"TransitGatewayConnectPeerId": self._id("tgw-connect-peer"), "TransitGatewayAttachmentId": attachment,
             "State": state, "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()],
             "ConnectPeerConfiguration": {
                 "TransitGatewayAddress": "203.0.113.%d" % (100 + len(self.peers)), "PeerAddress": peer_address,
                 "InsideCidrBlocks": list(blocks), "Protocol": "gre",
                 "BgpConfigurations": [{"TransitGatewayAddress": str(v4[2]), "PeerAddress": str(v4[1])},
                                       {"TransitGatewayAddress": str(v4[3]), "PeerAddress": str(v4[1])}]}}
        self.peers.append(p)
        return p

    def create_transit_gateway_connect_peer(self, **kw):
        self.rec("create_transit_gateway_connect_peer", kw)
        p = self.add_peer(kw["TransitGatewayAttachmentId"], kw["PeerAddress"], kw["InsideCidrBlocks"],
                          tags_of(kw.get("TagSpecifications")), state="pending")
        return {"TransitGatewayConnectPeer": copy.deepcopy(p)}

    def delete_transit_gateway_connect_peer(self, TransitGatewayConnectPeerId):
        self.rec("delete_transit_gateway_connect_peer", {"TransitGatewayConnectPeerId": TransitGatewayConnectPeerId})
        for p in self.peers:
            if p["TransitGatewayConnectPeerId"] == TransitGatewayConnectPeerId:
                p["State"] = "deleted"

    def delete_transit_gateway_connect(self, TransitGatewayAttachmentId):
        self.rec("delete_transit_gateway_connect", {"TransitGatewayAttachmentId": TransitGatewayAttachmentId})
        for a in self.attachments:
            if a["TransitGatewayAttachmentId"] == TransitGatewayAttachmentId:
                a["State"] = "deleted"

    def delete_transit_gateway_route_table(self, TransitGatewayRouteTableId):
        self.route_tables = [r for r in self.route_tables if r["TransitGatewayRouteTableId"] != TransitGatewayRouteTableId]

    def describe_addresses(self, **kw):
        return {"Addresses": []}

    def describe_instances(self, **kw):
        return {"Reservations": [{"Instances": copy.deepcopy(self.instances)}]}

    def describe_vpcs(self, **kw):
        return {"Vpcs": []}

    def search_transit_gateway_routes(self, **kw):
        return {"Routes": []}


class FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self):
        self.params, self.puts = {}, 0

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.params:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, Name, Value, **kw):
        self.params[Name] = Value
        self.puts += 1

    def add_tags_to_resource(self, **kw):
        pass

    def get_paginator(self, name):
        ssm = self

        class P:
            def paginate(self, Path):
                return [{"Parameters": [{"Name": k, "Value": v} for k, v in ssm.params.items() if k.startswith(Path + "/")]}]
        return P()


class FakeSession:
    def __init__(self, ec2, ssm):
        self.c = {"ec2": ec2, "ssm": ssm, "iam": object(), "ram": object()}

    def client(self, name):
        return self.c[name]


def edge(ec2=None, ipv6=True, nodes=None):
    ec2 = ec2 or FakeEC2()
    ssm = FakeSSM()
    with mock.patch.object(E, "session", lambda c: FakeSession(ec2, ssm)):
        e = E.Edge(cfg(ipv6))
    conf = {"site": "site-x", "asn": 64512, "tunnel": {"cidr": "192.0.2.0/24", "mtu": 1420, "listenPort": 51820},
            "nodes": nodes or {"i-a": {"name": "shc-edge-site-x-a", "address": "192.0.2.10"},
                               "i-b": {"name": "shc-edge-site-x-b", "address": "192.0.2.11"}},
            "gateways": [], "vrfs": []}
    ssm.params[e.param] = json.dumps(conf)
    return e, ec2, ssm


def peer_creates(ec2):
    return [kw for n, kw in ec2.calls if n == "create_transit_gateway_connect_peer"]


@mock.patch.object(E.time, "sleep", lambda s: None)
class ConnectPeers(unittest.TestCase):
    def test_new_peers_get_a_29_and_a_125(self):
        e, ec2, _ = edge()
        r = e.net_up(51000)
        creates = peer_creates(ec2)
        self.assertEqual([c["InsideCidrBlocks"] for c in creates],
                         [["169.254.100.0/29", "fd00:db8:5c::/125"], ["169.254.100.8/29", "fd00:db8:5c::8/125"]])
        self.assertEqual([c["PeerAddress"] for c in creates], ["198.51.100.10", "198.51.100.11"])
        self.assertEqual(creates[0]["BgpOptions"], {"PeerAsn": 64512})
        self.assertNotIn("shc-ipv6-recreated", tags_of(creates[0]["TagSpecifications"]))
        vrf = e.read_config()["vrfs"][0]
        self.assertEqual(vrf["ipv6"]["inside6"], {
            "i-a": {"inside": "fd00:db8:5c::1/125", "tgw": ["fd00:db8:5c::2", "fd00:db8:5c::3"]},
            "i-b": {"inside": "fd00:db8:5c::9/125", "tgw": ["fd00:db8:5c::a", "fd00:db8:5c::b"]}})
        # no settings from the gateway yet: not enabled; the IPv4 side as before
        self.assertFalse(vrf["ipv6"]["enabled"])
        self.assertEqual(vrf["connect"]["i-a"]["inside"], "169.254.100.1/29")
        self.assertEqual(r["peers"]["i-a"]["inside6"], "fd00:db8:5c::1/125")

    def test_pool_skips_the_125s_other_peers_hold(self):
        ec2 = FakeEC2()
        ec2.add_peer("tgw-attach-other", "198.51.100.99", ["169.254.100.8/29", "fd00:db8:5c::/125"])
        ec2.add_peer("tgw-attach-other", "198.51.100.98", ["169.254.100.16/29", "fd00:db8:5c::10/125"])
        e, ec2, _ = edge(ec2)
        e.net_up(51000)
        self.assertEqual([c["InsideCidrBlocks"] for c in peer_creates(ec2)],
                         [["169.254.100.0/29", "fd00:db8:5c::8/125"], ["169.254.100.24/29", "fd00:db8:5c::18/125"]])

    def test_free_blocks_and_exhaustion(self):
        pool = ipaddress.ip_network("fd00:db8:5c::/124")
        taken = [ipaddress.ip_network("fd00:db8:5c::/125"), ipaddress.ip_network("169.254.100.0/29")]
        self.assertEqual([str(b) for b in E.free_blocks(pool, 125, taken)], ["fd00:db8:5c::8/125"])
        ec2 = FakeEC2()
        ec2.add_peer("tgw-attach-other", "198.51.100.99", ["169.254.100.8/29", "fd00:db8:5c::/125"])
        ec2.add_peer("tgw-attach-other", "198.51.100.98", ["169.254.100.16/29", "fd00:db8:5c::8/125"])
        ssm = FakeSSM()
        with mock.patch.object(E, "session", lambda c: FakeSession(ec2, ssm)):
            e = E.Edge(cfg(pool6="fd00:db8:5c::/124"))
        ssm.params[e.param] = json.dumps({"nodes": {"i-a": {"name": "shc-edge-site-x-a", "address": "192.0.2.10"}},
                                          "vrfs": []})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, self.assertRaises(SystemExit):
            e.net_up(51000)
        self.assertIn("has no free /125 left", out.getvalue())

    def test_ipv4_only_edge_unchanged(self):
        e, ec2, _ = edge(ipv6=False)
        e.net_up(51000)
        self.assertEqual([c["InsideCidrBlocks"] for c in peer_creates(ec2)], [["169.254.100.0/29"], ["169.254.100.8/29"]])
        vrf = e.read_config()["vrfs"][0]
        self.assertNotIn("ipv6", vrf)
        self.assertNotIn("ipv6", e.read_config())
        # no ipv6 block in the config at all is the same as switched off
        e2, ec2b, _ = edge(ipv6=None)
        e2.net_up(51000)
        self.assertEqual([c["InsideCidrBlocks"] for c in peer_creates(ec2b)], [["169.254.100.0/29"], ["169.254.100.8/29"]])

    def test_peer_without_125_is_recreated_once(self):
        ec2 = FakeEC2()
        e, ec2, _ = edge(ec2, ipv6=False)
        e.net_up(51000)  # IPv4-only peers, as before V8
        old = {p["ConnectPeerConfiguration"]["PeerAddress"]: p for p in ec2.peers}
        ec2.calls.clear()
        e6, _, ssm6 = edge(ec2)
        ssm6.params[e6.param] = e.ssm.params[e.param]
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            e6.net_up(51000)
        deletes = [kw["TransitGatewayConnectPeerId"] for n, kw in ec2.calls if n == "delete_transit_gateway_connect_peer"]
        self.assertEqual(sorted(deletes), sorted(p["TransitGatewayConnectPeerId"] for p in old.values()))
        creates = peer_creates(ec2)
        # the same /29 and peer address, a new /125, the re-create tag
        self.assertEqual([(c["PeerAddress"], c["InsideCidrBlocks"]) for c in creates],
                         [("198.51.100.10", ["169.254.100.0/29", "fd00:db8:5c::/125"]),
                          ("198.51.100.11", ["169.254.100.8/29", "fd00:db8:5c::8/125"])])
        self.assertEqual(tags_of(creates[0]["TagSpecifications"])["shc-ipv6-recreated"], "true")
        self.assertIn("re-creating connect peer", err.getvalue())
        self.assertTrue(any("re-creating connect peer" in c for c in e6.changed))
        self.assertEqual(set(e6.read_config()["vrfs"][0]["ipv6"]["inside6"]), {"i-a", "i-b"})
        # a second run: nothing more to do
        ec2.calls.clear()
        e6.changed = []
        e6.net_up(51000)
        self.assertEqual([n for n, _ in ec2.calls if "connect_peer" in n], [])

    def test_recreated_peer_without_125_is_not_recreated_again(self):
        ec2 = FakeEC2(routers=1)
        e, ec2, _ = edge(ec2, nodes={"i-a": {"name": "shc-edge-site-x-a", "address": "192.0.2.10"}})
        e.net_up(51000)
        p = ec2.peers[0]
        p["ConnectPeerConfiguration"]["InsideCidrBlocks"] = ["169.254.100.0/29"]
        p["Tags"].append({"Key": "shc-ipv6-recreated", "Value": "true"})
        ec2.calls.clear()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            e.net_up(51000)
        self.assertEqual([n for n, _ in ec2.calls if "connect_peer" in n], [])
        self.assertIn("not again", err.getvalue())
        self.assertNotIn("ipv6", e.read_config()["vrfs"][0])

    def test_pool_validation(self):
        for bad in ("10.0.0.0/24", "2001:db8::/64", "fd00:db8:5c::/126", "not-a-network", ""):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out, self.assertRaises(SystemExit):
                E.ipv6_settings(cfg(pool6=bad))
            self.assertIn("insideCidrPool6", out.getvalue())
        self.assertEqual(E.ipv6_settings(cfg(ipv6=False, pool6="")), (False, None))
        self.assertEqual(E.ipv6_settings(cfg())[1], ipaddress.ip_network(POOL6))

    def test_inside6_entry(self):
        self.assertEqual(E.inside6_entry("fd00:db8:5c::10/125"),
                         {"inside": "fd00:db8:5c::11/125", "tgw": ["fd00:db8:5c::12", "fd00:db8:5c::13"]})


NETS6 = {"icmp6": {"i-a": "2001:db8:4005:1::64:5/128", "i-b": "2001:db8:4005:1::65:5/128", "i-gone": "2001:db8:4005:1::66:5/128"},
         "networks": [{"vni": 51000, "prefixes6": ["2001:db8:100:10::/60"], "network6": ["2001:db8:100::/48"],
                       "source": "Network", "internet": True}]}


@mock.patch.object(E.time, "sleep", lambda s: None)
class RouterConfig(unittest.TestCase):
    def test_vrfs6_composes_with_the_peers(self):
        e, ec2, ssm = edge()
        e.net_up(51000)
        r = e.set_vrfs6(NETS6)
        self.assertEqual(r["changedVnis"], [51000])
        six = e.read_config()["vrfs"][0]["ipv6"]
        self.assertEqual(six, {
            "enabled": True, "prefixes6": ["2001:db8:100:10::/60"], "network6": ["2001:db8:100::/48"],
            "source": "Network", "internet": True,
            "inside6": {"i-a": {"inside": "fd00:db8:5c::1/125", "tgw": ["fd00:db8:5c::2", "fd00:db8:5c::3"]},
                        "i-b": {"inside": "fd00:db8:5c::9/125", "tgw": ["fd00:db8:5c::a", "fd00:db8:5c::b"]}},
            # only the site's routers
            "icmp6": {"i-a": "2001:db8:4005:1::64:5/128", "i-b": "2001:db8:4005:1::65:5/128"}})
        puts = ssm.puts
        e.changed = []
        r = e.set_vrfs6(copy.deepcopy(NETS6))
        self.assertEqual((r["changedVnis"], e.changed, ssm.puts), ([], [], puts))
        # net-up keeps the gateway's settings (a re-run of the placement)
        e.net_up(51000)
        self.assertTrue(e.read_config()["vrfs"][0]["ipv6"]["enabled"])
        # a network the gateway no longer lists loses its IPv6 (the peers' /125s stay recorded)
        r = e.set_vrfs6({"icmp6": NETS6["icmp6"], "networks": []})
        six = e.read_config()["vrfs"][0]["ipv6"]
        self.assertEqual((r["changedVnis"], six["enabled"], six["prefixes6"]), ([51000], False, []))
        # status shows the settings
        st = e.status()
        self.assertEqual(st["vrfs"][0]["ipv6"]["enabled"], False)
        self.assertIn("t51000", st["ipv6"])

    def test_settings_before_the_vrf_exists(self):
        e, ec2, _ = edge()
        e.set_vrfs6(NETS6)  # the gateway ran before the placement's net-up
        self.assertEqual(e.read_config()["vrfs"], [])
        e.net_up(51000)
        self.assertTrue(e.read_config()["vrfs"][0]["ipv6"]["enabled"])
        e.net_down(51000)
        conf = e.read_config()
        self.assertEqual((conf["vrfs"], conf["ipv6"]["networks"]), ([], {}))

    def test_net_up_with_ipv6(self):
        e, ec2, _ = edge()
        e.net_up(51000, ipv6={"prefixes6": ["2001:db8:100:10::/60"], "network6": ["2001:db8:100::/48"],
                              "source": "Provider"})
        six = e.read_config()["vrfs"][0]["ipv6"]
        self.assertEqual((six["enabled"], six["source"], six["internet"]), (True, "Provider", False))

    def test_vrfs6_refuses_bad_input(self):
        e, ec2, _ = edge()
        for bad in ({"networks": [{"vni": 1, "prefixes6": ["10.0.0.0/24"]}]},
                    {"networks": [{"vni": 1, "prefixes6": ["2001:db8::/64"], "source": "Elsewhere"}]},
                    {"networks": [{"vni": 1, "prefixes6": ["2001:db8::1/64"]}]},
                    {"icmp6": {"i-a": "2001:db8::/64"}}, {"icmp6": {"i-a": "nonsense"}}):
            with mock.patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(SystemExit):
                e.set_vrfs6(bad)

    def test_vrfs6_with_ipv6_off_clears(self):
        e, ec2, ssm = edge()
        e.net_up(51000)
        e.set_vrfs6(NETS6)
        off, _, _ = edge(ec2, ipv6=False)
        off.ssm.params[off.param] = ssm.params[e.param]
        r = off.set_vrfs6(NETS6)
        conf = off.read_config()
        self.assertEqual((r["changedVnis"], "ipv6" in conf, "ipv6" in conf["vrfs"][0]), ([51000], False, False))

    def test_user_data_forwarding(self):
        e, _, _ = edge()
        with mock.patch.object(E, "open", lambda *a, **k: io.BytesIO(b"agent"), create=True):
            self.assertIn("net.ipv6.conf.all.forwarding = 1", e.user_data())
        e4, _, _ = edge(ipv6=False)
        with mock.patch.object(E, "open", lambda *a, **k: io.BytesIO(b"agent"), create=True):
            self.assertNotIn("ipv6", e4.user_data())

    def test_status_summary(self):
        conf = {"vrfs": [{"name": "t51000", "vni": 51000, "ipv6": {
            "enabled": True, "source": "Network", "internet": True, "prefixes6": ["2001:db8:100:10::/60"],
            "inside6": {"i-a": {"inside": "fd00:db8:5c::1/125", "tgw": []}}, "icmp6": {"i-a": "2001:db8:4005:1::64:5/128"}}},
            {"name": "t51001", "vni": 51001}]}
        reports = {"i-a": {"state": {
            "bgp6": {"t51000": {"169.254.100.10": {"af6": "negotiated"}, "169.254.100.11": {"af6": "NoNeg"}}},
            "routes6": {"t51000": {"2001:db8:100:10::/60": ["bgp"], "::/0": ["bgp"], "2001:db8:100::/48": ["bgp"],
                                   "fd00:db8:5c::/125": ["connected"]}}}}}
        self.assertEqual(E.ipv6_status(conf, reports), {"t51000": {
            "enabled": True, "source": "Network", "internet": True, "routers": {"i-a": {
                "inside6": "fd00:db8:5c::1/125", "icmp6": "2001:db8:4005:1::64:5/128", "sessions6": "1/2",
                "routes6": {"bgp": 3, "connected": 1}, "default6": True, "missing6": []}}}})


# ---- the router agent

NODES = {"i-a": {"name": "shc-edge-site-x-a", "address": "192.0.2.10"},
         "i-b": {"name": "shc-edge-site-x-b", "address": "192.0.2.11"}}
CONNECT = {"i-a": {"local": "198.51.100.10", "remote": "198.51.100.200", "inside": "169.254.100.1/29",
                   "bgp": ["169.254.100.2", "169.254.100.3"]}}


def agent_cfg(ipv6=None, test=True):
    v = {"name": "t51000", "vni": 51000, "mtu": 1370, "tgwAsn": 64513, "connect": copy.deepcopy(CONNECT)}
    if test:
        v["testAddress"] = {"i-a": "198.18.0.1"}
    if ipv6 is not None:
        v["ipv6"] = ipv6
    return {"site": "site-x", "asn": 64512, "bgpPassword": "pw",
            "tunnel": {"cidr": "192.0.2.0/24", "mtu": 1420, "listenPort": 51820}, "nodes": copy.deepcopy(NODES),
            "gateways": [{"name": "gw1", "publicKey": "k1", "address": "192.0.2.1"},
                         {"name": "gw2", "publicKey": "k2", "address": "192.0.2.2"}],
            "vrfs": [v, {"name": "t51001", "vni": 51001, "mtu": 1370, "tgwAsn": 64513, "connect": {}}]}


def six(source="Network", internet=False, prefixes6=("2001:db8:100:10::/60",), network6=("2001:db8:100::/48",),
        icmp=True, enabled=True):
    return {"enabled": enabled, "prefixes6": list(prefixes6), "network6": list(network6), "source": source,
            "internet": internet, "inside6": {"i-a": {"inside": "fd00:db8:5c::1/125", "tgw": ["fd00:db8:5c::2", "fd00:db8:5c::3"]}},
            "icmp6": {"i-a": "2001:db8:4005:1::64:5/128"} if icmp else {}}


# the render of the agent before IPv6 for this configuration, byte for byte
IPV4_RENDER = (
    "frr defaults traditional\nhostname shc-edge-site-x-a\nservice integrated-vtysh-config\n!\nvrf t51000\n vni 51000\n"
    "exit-vrf\n!\nroute-map t51000-tgw-in permit 10\nexit\nroute-map t51000-tgw-out permit 10\nexit\n!\n"
    "ip prefix-list t51000-test seq 5 permit 198.18.0.1/32\nroute-map t51000-connected permit 10\n"
    " match ip address prefix-list t51000-test\nexit\n!\nvrf t51001\n vni 51001\nexit-vrf\n!\n"
    "route-map t51001-tgw-in permit 10\nexit\nroute-map t51001-tgw-out permit 10\nexit\n!\nrouter bgp 64512\n"
    " bgp router-id 192.0.2.10\n no bgp default ipv4-unicast\n neighbor GW peer-group\n neighbor GW remote-as 64512\n"
    " neighbor GW bfd\n neighbor GW timers connect 10\n neighbor GW password pw\n neighbor 192.0.2.1 peer-group GW\n"
    " neighbor 192.0.2.2 peer-group GW\n address-family l2vpn evpn\n  neighbor GW activate\n  advertise-all-vni\n"
    " exit-address-family\nexit\n!\nrouter bgp 64512 vrf t51000\n bgp router-id 192.0.2.10\n"
    " no bgp default ipv4-unicast\n neighbor TGW peer-group\n neighbor TGW remote-as 64513\n"
    " neighbor TGW ebgp-multihop 2\n neighbor TGW timers connect 10\n neighbor 169.254.100.2 peer-group TGW\n"
    " neighbor 169.254.100.3 peer-group TGW\n address-family ipv4 unicast\n  neighbor TGW activate\n"
    "  neighbor TGW route-map t51000-tgw-in in\n  neighbor TGW route-map t51000-tgw-out out\n"
    "  redistribute connected route-map t51000-connected\n exit-address-family\n address-family l2vpn evpn\n"
    "  advertise ipv4 unicast\n exit-address-family\nexit\n!\nrouter bgp 64512 vrf t51001\n bgp router-id 192.0.2.10\n"
    " no bgp default ipv4-unicast\n address-family ipv4 unicast\n exit-address-family\n address-family l2vpn evpn\n"
    "  advertise ipv4 unicast\n exit-address-family\nexit\n!\n")


def render(c, me="i-a"):
    return A.frr_conf(c, me, c["nodes"][me])


def lines(text, prefix):
    return [x for x in text.splitlines() if x.startswith(prefix)]


class AgentRender(unittest.TestCase):
    def test_ipv4_render_unchanged(self):
        self.assertEqual(render(agent_cfg()), IPV4_RENDER)
        # an ipv6 block that is off, or names another router, renders exactly the same
        self.assertEqual(render(agent_cfg(six(enabled=False))), IPV4_RENDER)
        c = agent_cfg(six())
        c["vrfs"][0]["ipv6"]["inside6"] = {"i-b": c["vrfs"][0]["ipv6"]["inside6"]["i-a"]}
        self.assertEqual(render(c), IPV4_RENDER)

    def test_network_source_with_internet(self):
        out = render(agent_cfg(six(internet=True)))
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-in6"),
                         ["ipv6 prefix-list t51000-in6 seq 5 permit 2001:db8:100:10::/60 le 128"])
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-out6"),
                         ["ipv6 prefix-list t51000-out6 seq 5 deny 2001:db8:100:10::/60 le 128",
                          "ipv6 prefix-list t51000-out6 seq 10 permit 2001:db8:100::/48 le 128",
                          "ipv6 prefix-list t51000-out6 seq 15 permit ::/0"])
        self.assertIn(" address-family ipv6 unicast\n  neighbor TGW activate\n"
                      "  neighbor TGW route-map t51000-tgw-in6 in\n  neighbor TGW route-map t51000-tgw-out6 out\n"
                      " exit-address-family\n", out)
        self.assertIn("  advertise ipv4 unicast\n  advertise ipv6 unicast route-map t51000-evpn6\n", out)
        self.assertIn("route-map t51000-evpn6 permit 10\n match ipv6 address prefix-list t51000-in6\nexit\n", out)
        # the ICMPv6 source on the fabric-side routes, the transit gateway's routes as they are
        self.assertIn("vrf t51000\n vni 51000\n ipv6 protocol bgp route-map t51000-src6\nexit-vrf\n", out)
        self.assertIn("route-map t51000-src6 permit 10\n match ipv6 address prefix-list t51000-in6\nexit\n"
                      "route-map t51000-src6 permit 20\n set src 2001:db8:4005:1::64:5\nexit\n", out)
        # the IPv4 side as before, the IPv4-only VRF untouched
        self.assertIn("  neighbor TGW route-map t51000-tgw-in in\n  neighbor TGW route-map t51000-tgw-out out\n", out)
        self.assertNotIn("t51001-in6", out)
        # the router without a /125 renders IPv4 only
        self.assertNotIn("ipv6", render(agent_cfg(six(internet=True)), "i-b"))

    def test_network_source_without_internet(self):
        out = render(agent_cfg(six(internet=False)))
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-out6"),
                         ["ipv6 prefix-list t51000-out6 seq 5 deny 2001:db8:100:10::/60 le 128",
                          "ipv6 prefix-list t51000-out6 seq 10 permit 2001:db8:100::/48 le 128"])
        self.assertNotIn("permit ::/0", out)

    def test_provider_source_never_sends_a_default(self):
        out = render(agent_cfg(six(source="Provider", internet=True, prefixes6=("2001:db8:aaaa:bb00::/56",))))
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-in6"),
                         ["ipv6 prefix-list t51000-in6 seq 5 permit 2001:db8:aaaa:bb00::/56 le 128"])
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-out6"),
                         ["ipv6 prefix-list t51000-out6 seq 5 deny 2001:db8:aaaa:bb00::/56 le 128",
                          "ipv6 prefix-list t51000-out6 seq 10 permit 2001:db8:100::/48 le 128"])
        self.assertNotIn("::/0\n", out.replace("::/0 le 128", ""))
        self.assertIn("advertise ipv6 unicast route-map t51000-evpn6", out)

    def test_tenant_prefix_network_no_default_either_way(self):
        # mode Tenant: the gateway job never sets internet; a ULA network outside fd00::/16
        out = render(agent_cfg(six(internet=False, prefixes6=("fd5c:8:0:10::/60",), network6=("fd5c:8::/48",))))
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-in6"),
                         ["ipv6 prefix-list t51000-in6 seq 5 permit fd5c:8:0:10::/60 le 128"])
        self.assertNotIn("::/0", out.replace("::/0 le 128", "x"))

    def test_nothing_to_announce_denies_all(self):
        out = render(agent_cfg(six(network6=())))
        self.assertEqual(lines(out, "ipv6 prefix-list t51000-out6"),
                         ["ipv6 prefix-list t51000-out6 seq 5 deny 2001:db8:100:10::/60 le 128",
                          "ipv6 prefix-list t51000-out6 seq 10 deny ::/0 le 128"])

    def test_without_icmp_source(self):
        out = render(agent_cfg(six(icmp=False)))
        self.assertNotIn("src6", out)
        self.assertIn("address-family ipv6 unicast", out)


class FakeHost:
    """The commands apply() runs, against a router with nothing configured yet."""

    def __init__(self, links=(), addrs6=None):
        self.cmds, self.links, self.addrs6 = [], set(links), addrs6 or {}

    def sh(self, cmd, check=True, inp=None):
        self.cmds.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        c = cmd if isinstance(cmd, list) else cmd.split()
        if c[:4] == ["ip", "-j", "-6", "addr"]:
            dev = c[c.index("dev") + 1]
            return json.dumps([{"addr_info": [{"family": "inet6", "local": a.split("/")[0], "prefixlen": int(a.split("/")[1]),
                                               "scope": "global"} for a in self.addrs6.get(dev, [])]}])
        if c[:3] == ["ip", "link", "add"]:
            self.links.add(c[3])
        if "default" in cmd and "awk" in cmd:
            return "eth0\n"
        if "addr show dev eth0" in cmd:
            return "198.51.100.10/24\n"
        return ""

    def link_exists(self, name):
        return name in self.links


def apply_cmds(c, host):
    with mock.patch.object(A, "sh", host.sh), mock.patch.object(A, "link_exists", host.link_exists), \
            mock.patch.object(A, "frr_conf", lambda *a: ""), mock.patch.object(A.os.path, "exists", lambda p: True), \
            mock.patch.object(A, "open", lambda *a, **k: io.StringIO(""), create=True):
        A.apply(c, "i-a")
    return host.cmds


class AgentApply(unittest.TestCase):
    def test_ipv6_vrf(self):
        host = FakeHost(addrs6={"gre51000": ["fd00:db8:5c::9/125"]})
        cmds = apply_cmds(agent_cfg(six(), test=False), host)
        self.assertIn("sysctl -qw net.ipv6.conf.all.forwarding=1 net.ipv6.conf.default.forwarding=1", cmds)
        self.assertIn("ip -6 addr del fd00:db8:5c::9/125 dev gre51000", cmds)
        self.assertIn("ip -6 addr replace fd00:db8:5c::1/125 dev gre51000 nodad", cmds)
        self.assertIn("ip link add lo51000 type dummy", cmds)
        self.assertIn("ip -6 addr replace 2001:db8:4005:1::64:5/128 dev lo51000 nodad", cmds)
        self.assertIn("ip -6 route replace unreachable default metric 4278198272 table 51000", cmds)
        # the IPv4 inside address as before
        self.assertIn("ip addr replace 169.254.100.1/29 dev gre51000", cmds)
        self.assertFalse([x for x in cmds if "51001" in x and "-6" in x])

    def test_ipv4_only_runs_no_ipv6_command(self):
        cmds = apply_cmds(agent_cfg(), FakeHost())
        self.assertEqual([x for x in cmds if "-6" in x.split() or "ipv6" in x], [])
        self.assertIn("ip addr replace 198.18.0.1/32 dev lo51000", cmds)

    def test_ipv6_switched_off_removes_it(self):
        host = FakeHost(links={"lo51000", "gre51000"}, addrs6={"gre51000": ["fd00:db8:5c::1/125"],
                                                               "lo51000": ["2001:db8:4005:1::64:5/128"]})
        cmds = apply_cmds(agent_cfg(six(enabled=False), test=True), host)
        self.assertIn("ip -6 addr del fd00:db8:5c::1/125 dev gre51000", cmds)
        self.assertIn("ip -6 addr del 2001:db8:4005:1::64:5/128 dev lo51000", cmds)
        self.assertIn("ip -6 route del unreachable default metric 4278198272 table 51000", cmds)
        self.assertNotIn("sysctl -qw net.ipv6.conf.all.forwarding=1 net.ipv6.conf.default.forwarding=1", cmds)


class AgentState(unittest.TestCase):
    def test_bgp6_peers(self):
        j = {"peers": {"169.254.100.2": {"state": "Established", "pfxRcd": 1, "pfxSnt": 2},
                       "169.254.100.3": {"state": "Established"},
                       "169.254.100.4": {"state": "Active"}}}
        self.assertEqual(A.bgp6_peers(j), {
            "169.254.100.2": {"state": "Established", "af6": "negotiated", "pfxRcd": 1, "pfxSnt": 2},
            "169.254.100.3": {"state": "Established", "af6": "NoNeg", "pfxRcd": None, "pfxSnt": None},
            "169.254.100.4": {"state": "Active", "af6": "Active", "pfxRcd": None, "pfxSnt": None}})
        self.assertEqual(A.bgp6_peers({"ipv6Unicast": j})["169.254.100.2"]["af6"], "negotiated")

    def vtysh(self, outputs):
        def sh(cmd, check=True, inp=None):
            c = cmd if isinstance(cmd, str) else " ".join(cmd)
            if c.startswith("wg show"):
                return "pubkey 1700000000\n"
            for k, v in outputs.items():
                if c.endswith('-c ' + k) or c.endswith(k):
                    return json.dumps(v)
            return "{}"
        return sh

    def test_state_reports_ipv6(self):
        out = {
            "show bgp vrf t51000 summary json": {
                "ipv4Unicast": {"peers": {"169.254.100.2": {"state": "Established", "pfxRcd": 1, "pfxSnt": 3}}},
                "ipv6Unicast": {"peers": {"169.254.100.2": {"state": "Established", "pfxRcd": 7, "pfxSnt": 9}}}},
            "show bgp vrf t51000 ipv6 unicast summary json": {
                "peers": {"169.254.100.2": {"state": "Established", "pfxRcd": 1, "pfxSnt": 2},
                          "169.254.100.3": {"state": "Established"}}},
            "show ipv6 route vrf t51000 json": {"2001:db8:100:10::/60": [{"protocol": "bgp"}],
                                                "::/0": [{"protocol": "bgp"}],
                                                "fd00:db8:5c::/125": [{"protocol": "connected"}]},
            "show ip route vrf t51000 json": {"10.0.0.0/24": [{"protocol": "bgp"}]}}
        with mock.patch.object(A, "sh", self.vtysh(out)):
            st = A.state(agent_cfg(six()), "i-a")
        # the IPv4 counts are not overwritten by the IPv6 family's
        self.assertEqual(st["bgp"]["t51000"]["169.254.100.2"], {"state": "Established", "pfxRcd": 1, "pfxSnt": 3})
        self.assertEqual({k: v["af6"] for k, v in st["bgp6"]["t51000"].items()},
                         {"169.254.100.2": "negotiated", "169.254.100.3": "NoNeg"})
        self.assertEqual(st["routes6"]["t51000"], {"2001:db8:100:10::/60": ["bgp"], "::/0": ["bgp"],
                                                   "fd00:db8:5c::/125": ["connected"]})
        with mock.patch.object(A, "sh", self.vtysh(out)):
            st4 = A.state(agent_cfg(), "i-a")
        self.assertEqual(sorted(st4), ["bgp", "routes", "wg"])


# ---- the gateway job

def router(name, iid):
    return {"name": name, "instance": iid, "publicIp": "203.0.113.%d" % (10 + len(name)), "tunnelAddress": "192.0.2.10",
            "publicKey": "k"}


def placement(name, vni, subnets):
    return {"metadata": {"namespace": "entity-acme", "name": name}, "spec": {"network": "net-" + name},
            "status": {"subnets": subnets, "cloudGateway": "site-x", "realization": {"l3Vni": vni, "vpnTarget": 10000}}}


def network(name, mode, internet, prefixes):
    return {"metadata": {"namespace": "entity-acme", "name": "net-" + name}, "spec": {"egress": {"internet": internet}},
            "status": {"ipv6": {"mode": mode, "prefixes": prefixes}}}


def vrf(name, vni, p6):
    return {"placement": "entity-acme/" + name, "network": "entity-acme/net-" + name, "vrf": "t%d" % vni, "vni": vni,
            "prefixes6": p6, "families": [4, 6] if p6 else [4]}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayJob(unittest.TestCase):
    def post_tasks(self):
        block = by_name(load(os.path.join(CG, "tasks", "site_aws_post.yml")), "The IPv6 side of the routers' VRFs")
        return [copy.deepcopy(by_name(block["block"], n)) for n in
                ("The routers' ICMPv6 sources", "The networks' IPv6 settings for the routers")]

    def test_per_network_settings_and_icmp6(self):
        core = dict(REAL["core"], loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
        out = run_play(self.post_tasks() + [write_out("{'vrfs6': cg_aws_vrfs6, 'errors': cg_aws_icmp6_errors}")], {
            "role_path": CG, "fab_name": "site-x", "fab_real": {"core": core}, "cg_pe": {"index": 5},
            "cg_ipv6_core": True,
            # out of order on purpose: the position is by name
            "cg_aws_routers": [router("shc-edge-site-x-b", "i-b"), router("shc-edge-site-x-a", "i-a")],
            "cg_vrfs": [vrf("v4", 51000, []), vrf("plat", 51001, ["2001:db8:100:10::/60"]),
                        vrf("own", 51002, ["fd5c:8:0:10::/60"]), vrf("prov", 51003, ["2001:db8:aaaa:bb00::/56"]),
                        vrf("quiet", 51004, ["2001:db8:200:10::/60"])],
            "_cg_placements": {"resources": [
                placement("v4", 51000, [{"family": 4, "prefix": "10.60.0.0/24", "source": "Network"}]),
                placement("plat", 51001, [{"family": 4, "prefix": "10.60.1.0/24", "source": "Network"},
                                          {"family": 6, "prefix": "2001:db8:100:10::/60", "source": "Network"}]),
                placement("own", 51002, [{"family": 6, "prefix": "fd5c:8:0:10::/60", "source": "Network"}]),
                placement("prov", 51003, [{"family": 6, "prefix": "2001:db8:aaaa:bb00::/56", "source": "Provider"}]),
                placement("quiet", 51004, [{"family": 6, "prefix": "2001:db8:200:10::/60"}])]},
            "_cg_networks": {"resources": [
                network("plat", "Platform", True, ["2001:db8:100::/48"]),
                network("own", "Tenant", True, ["fd5c:8::/48"]),
                network("prov", "Platform", True, ["2001:db8:300::/48"]),
                network("quiet", "Platform", False, ["2001:db8:200::/48"])]}})
        self.assertEqual(out["errors"], [])
        # node ordinal 100 + the router's position by name, the gateway's provider-edge index 5
        self.assertEqual(out["vrfs6"]["icmp6"], {"i-a": "2001:db8:4005:1::64:5/128", "i-b": "2001:db8:4005:1::65:5/128"})
        base = int(ipaddress.ip_network("2001:db8:4005:1::/64").network_address)
        self.assertEqual(out["vrfs6"]["icmp6"]["i-a"], "%s/128" % ipaddress.IPv6Address(base + (100 << 16) + 5))
        self.assertEqual(out["vrfs6"]["networks"], [
            {"vni": 51001, "prefixes6": ["2001:db8:100:10::/60"], "network6": ["2001:db8:100::/48"],
             "source": "Network", "internet": True},
            # mode Tenant: never internet
            {"vni": 51002, "prefixes6": ["fd5c:8:0:10::/60"], "network6": ["fd5c:8::/48"], "source": "Network",
             "internet": False},
            {"vni": 51003, "prefixes6": ["2001:db8:aaaa:bb00::/56"], "network6": ["2001:db8:300::/48"],
             "source": "Provider", "internet": True},
            # no source on the row: Network
            {"vni": 51004, "prefixes6": ["2001:db8:200:10::/60"], "network6": ["2001:db8:200::/48"],
             "source": "Network", "internet": False}])
        # the edge script takes it as it is
        for n in out["vrfs6"]["networks"]:
            E.network6_entry(n)

    def test_without_the_ipv6_core_no_icmp6(self):
        out = run_play(self.post_tasks() + [write_out("cg_aws_vrfs6")], {
            "role_path": CG, "fab_name": "site-x", "fab_real": {"core": dict(REAL["core"], icmpSource6="2001:db8:4005:1::/64")},
            "cg_pe": {"index": 5}, "cg_ipv6_core": False, "cg_aws_routers": [router("shc-edge-site-x-a", "i-a")],
            "cg_vrfs": [], "_cg_placements": {"resources": []}, "_cg_networks": {"resources": []}})
        self.assertEqual(out, {"icmp6": {}, "networks": []})

    def test_realization_defaults(self):
        t = by_name(load(os.path.join(COMMON_TASKS, "aws_realization.yml")), "The installation's AWS realization")
        out = run_play([copy.deepcopy(t), write_out("aws_real")],
                       {"aws_realization": {"hub": {"account": ACCOUNT}, "edge": {"tgwName": "tgw"}}})
        self.assertEqual(out["edge"], {"tgwName": "tgw", "ipv6": {"enabled": False, "insideCidrPool6": ""}})
        out = run_play([copy.deepcopy(t), write_out("aws_real")], {"aws_realization": {
            "hub": {}, "edge": {"ipv6": {"enabled": True, "insideCidrPool6": POOL6}}}})
        self.assertEqual(out["edge"]["ipv6"], {"enabled": True, "insideCidrPool6": POOL6})
        out = run_play([copy.deepcopy(t), write_out("aws_real")], {"aws_realization": {"hub": {}}})
        self.assertNotIn("edge", out)

    def test_placement_waits_for_the_gateway_to_carry_ipv6(self):
        t = copy.deepcopy(by_name(load(os.path.join(NP, "main.yml")), "Does the gateway carry the placement"))

        def carries(families, p6):
            gw = {"status": {"realization": {"vrfs": [{"placement": "entity-acme/p1", "vni": 51001, "families": families}]}}}
            return run_play([t, write_out("np_gw_carries")], {
                "_np_gw_again": {"resources": [gw]}, "fab_namespace": "entity-acme", "fab_name": "p1",
                "np_l3vni": 51001, "np_prefixes_v6": p6})
        self.assertTrue(carries([4], []))
        self.assertFalse(carries([4], ["2001:db8:aaaa:bb00::/56"]))
        self.assertTrue(carries([4, 6], ["2001:db8:aaaa:bb00::/56"]))


if __name__ == "__main__":
    unittest.main()

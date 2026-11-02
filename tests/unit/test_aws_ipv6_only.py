"""IPv6-only AWS placements (design/IPV6-PLAN.md §6.4): shc_aws_tenant.py against a fake EC2 that checks every call
against the EC2 API model botocore ships (boto3 is never called for real), the hub's NAT egress VPC serving NAT64
(shc_aws_edge.py against a fake hub, model-checked the same way), the edge routers' placeholder deny
(shc_edge_agent.py), and the placement, teardown and realization task files in ansible-playbook (skipped when it is
absent).

The VPC of an IPv6-only placement keeps a placeholder IPv4 CIDR (a /26 of the realization's tenant.placeholderV4Pool
chosen by the VNI) for its attachment subnets; its workload subnets are IPv6-only. Source Network: a DHCP option set
with the translation's resolvers. Source Provider with translation: DNS64 on the workload subnets, 64:ff9b::/96 to the
transit gateway and from there to the network's NAT egress VPC. Dual-stack and IPv4 placements make exactly the calls
they made before (pinned by digest).

Addresses are documentation and shared-address ranges only."""
import contextlib
import copy
import hashlib
import io
import ipaddress
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
NP = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks")
NPD = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_teardown", "tasks")
COMMON_TASKS = os.path.join(ROOT, "eda", "common", "tasks")
sys.path.insert(0, FILES)

try:
    import botocore.exceptions
    import botocore.session
    from botocore import xform_name
    import shc_aws_tenant as T
    import shc_aws_edge as E
    import test_aws_tenant_ipv6 as V8
    HAVE_BOTO = True
except ImportError:  # pragma: no cover - the runner image has boto3
    HAVE_BOTO = False
import shc_edge_agent as A  # noqa: E402

POOL4 = "100.127.0.0/16"            # the placeholder pool in these tests
NET6 = "3fff:0:4000::/48"
BLOCK = "3fff:0:4000:10::/60"
AMAZON = "3fff:a:1:100::/56"
EGRESS6 = "3fff:e:1:100::/56"       # the fake hub's Amazon /56 for the egress VPC
TGW = "tgw-0test"
PLACEMENT = "acme/p1"
VNI = 51003
EIP = "192.0.2.77"
RESOLVERS = ["fd00:5c:53::10", "fd00:5c:53::11"]
NAT64 = "64:ff9b::/96"

MUTATING = ("create_", "modify_", "associate_", "provision_", "replace_", "delete_", "disassociate_", "deprovision_",
            "attach_", "detach_", "allocate_", "release_")


def digest(calls):
    return hashlib.sha256(json.dumps(calls, sort_keys=True, default=str).encode()).hexdigest()


def refused(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), unittest.TestCase().assertRaises(SystemExit):
        fn()
    return json.loads(out.getvalue().splitlines()[-1])


# ---- the tenant side ------------------------------------------------------------------------------------------------

if HAVE_BOTO:
    class FakeEc2(V8.FakeEc2):
        """V8's fake EC2 plus IPv6-only subnets (no IPv4), the subnets' DNS attributes and DHCP option sets."""

        def __init__(self, amazon=AMAZON):
            super().__init__(amazon)
            self.dhcp = {}

        def h_create_vpc(self, CidrBlock, **kw):
            out = super().h_create_vpc(CidrBlock, **kw)
            v = out["Vpc"]["VpcId"]
            self.vpcs[v]["DhcpOptionsId"] = "dopt-default"
            out["Vpc"]["DhcpOptionsId"] = "dopt-default"
            return out

        def h_create_subnet(self, VpcId, CidrBlock=None, **kw):
            if kw.get("Ipv6Native"):
                if CidrBlock:
                    raise V8.client_error("InvalidParameterCombination", "an IPv6-native subnet takes no IPv4")
                if not kw.get("Ipv6CidrBlock"):
                    raise V8.client_error("MissingParameter", "an IPv6-native subnet needs its /64")
            elif not CidrBlock:
                raise V8.client_error("MissingParameter", "CidrBlock")
            s = self._id("subnet")
            row = {"SubnetId": s, "VpcId": VpcId, "CidrBlock": CidrBlock, "AvailabilityZone": kw.get("AvailabilityZone"),
                   "Tags": self._tags(kw), "AssignIpv6AddressOnCreation": False, "Ipv6CidrBlockAssociationSet": [],
                   "Ipv6Native": bool(kw.get("Ipv6Native")), "EnableDns64": False,
                   "PrivateDnsNameOptionsOnLaunch": {"HostnameType": "ip-name", "EnableResourceNameDnsAAAARecord": False}}
            if not CidrBlock:
                del row["CidrBlock"]
            self.subnets[s] = row
            if kw.get("Ipv6CidrBlock"):
                self._sub6(s, kw["Ipv6CidrBlock"])
            return {"Subnet": copy.deepcopy(row)}

        def h_modify_subnet_attribute(self, SubnetId, **kw):
            s = self.subnets[SubnetId]
            if len(kw) != 1:
                raise V8.client_error("InvalidParameterCombination", "one attribute at a time")
            if "AssignIpv6AddressOnCreation" in kw:
                s["AssignIpv6AddressOnCreation"] = kw["AssignIpv6AddressOnCreation"]["Value"]
            if "PrivateDnsHostnameTypeOnLaunch" in kw:
                s["PrivateDnsNameOptionsOnLaunch"]["HostnameType"] = kw["PrivateDnsHostnameTypeOnLaunch"]
            if "EnableResourceNameDnsAAAARecordOnLaunch" in kw:
                s["PrivateDnsNameOptionsOnLaunch"]["EnableResourceNameDnsAAAARecord"] = \
                    kw["EnableResourceNameDnsAAAARecordOnLaunch"]["Value"]
            if "EnableDns64" in kw:
                s["EnableDns64"] = kw["EnableDns64"]["Value"]
            return {}

        def h_create_transit_gateway_vpc_attachment(self, TransitGatewayId, VpcId, SubnetIds, **kw):
            if not all(self.subnets[s].get("CidrBlock") for s in SubnetIds):
                raise V8.client_error("InsufficientFreeAddressesInSubnet", "the attachment needs IPv4")
            return super().h_create_transit_gateway_vpc_attachment(TransitGatewayId, VpcId, SubnetIds, **kw)

        def h_describe_dhcp_options(self, Filters=None, **kw):
            out = list(self.dhcp.values())
            for f in Filters or []:
                key = f["Name"].split(":", 1)[1]
                out = [d for d in out if T.tagmap(d).get(key) in f["Values"]]
            return {"DhcpOptions": copy.deepcopy(out)}

        def h_create_dhcp_options(self, DhcpConfigurations, **kw):
            d = self._id("dopt")
            self.dhcp[d] = {"DhcpOptionsId": d, "Tags": self._tags(kw), "DhcpConfigurations": [
                {"Key": c["Key"], "Values": [{"Value": v} for v in c["Values"]]} for c in DhcpConfigurations]}
            return {"DhcpOptions": copy.deepcopy(self.dhcp[d])}

        def h_associate_dhcp_options(self, DhcpOptionsId, VpcId, **kw):
            if DhcpOptionsId != "default" and DhcpOptionsId not in self.dhcp:
                raise V8.client_error("InvalidDhcpOptionID.NotFound", DhcpOptionsId)
            self.vpcs[VpcId]["DhcpOptionsId"] = "dopt-default" if DhcpOptionsId == "default" else DhcpOptionsId
            return {}

        def h_delete_dhcp_options(self, DhcpOptionsId, **kw):
            if any(v.get("DhcpOptionsId") == DhcpOptionsId for v in self.vpcs.values()):
                raise V8.client_error("DependencyViolation", "the option set is associated")
            del self.dhcp[DhcpOptionsId]
            return {}


class FakeEdge:
    """The hub side: records net_up's arguments exactly as given (a dual-stack call passes no nat64)."""
    instances = []

    def __init__(self, cfg):
        self.changed = []
        FakeEdge.instances.append(self)

    def tgw(self):
        return {"TransitGatewayId": TGW}

    def net_up(self, vni, **kw):
        FakeEdge.calls.append(["net_up", vni, kw])
        eg = {"publicIp": EIP, "natGateway": "nat-1"} if kw.get("egress") else None
        return {"routeTable": "tgw-rtb-net", "connectAttachment": "tgw-attach-connect", "egress": eg}

    def net_attach(self, vni, att):
        FakeEdge.calls.append(["net_attach", vni, att])
        return {}

    def attachments(self, tgw_id, **f):
        return []

    def net_down(self, vni):
        FakeEdge.calls.append(["net_down", vni])


def tenant(ec2):
    t = T.Tenant.__new__(T.Tenant)
    t.cfg = {"account": V8.ACCOUNT, "region": V8.REGION, "azs": list(V8.AZS),
             "tags": {"shc-entity": "acme", "shc-network": "core"}, "edge": {}}
    t.ec2, t.r53, t.ram = ec2, None, None
    t.changed = []
    return t


def only6(source="Network", internet=True, resolvers=None, nat64=None, pool=POOL4):
    d = {"source": source, "internet": internet, "networkPrefixes6": [NET6]}
    if source == "Network":
        d["prefix"] = BLOCK
        d["resolvers"] = list(RESOLVERS if resolvers is None else resolvers)
    else:
        d["nat64"] = internet if nat64 is None else nat64
    if pool:
        d["placeholderV4Pool"] = pool
    return d


def dual_stack_scenarios(module, ec2_cls):
    """The V8 dual-stack and IPv4 runs (up, re-run, change, down) against a fresh fake; every EC2 call and every hub
    call in order. Run with the module at hand and pinned by digest below."""
    out = {}
    for label, runs in (
            ("ipv4", [("up", None), ("up", None), ("down", None)]),
            ("network", [("up", V8.ipv6()), ("up", V8.ipv6()), ("up", V8.ipv6(internet=False)),
                         ("down", {"source": "Network", "prefix": V8.BLOCK})]),
            ("provider", [("up", V8.ipv6(source="Provider")), ("up", V8.ipv6(source="Provider", internet=False)),
                          ("down", {"source": "Provider", "prefix": V8.AMAZON})])):
        ec2 = ec2_cls()
        FakeEdge.calls = []
        with mock.patch.object(module.time, "sleep", lambda s: None), \
                mock.patch.object(module.shc_aws_edge, "Edge", FakeEdge):
            for what, v6 in runs:
                t = module.Tenant.__new__(module.Tenant)
                t.cfg = {"account": V8.ACCOUNT, "region": V8.REGION, "azs": list(V8.AZS),
                         "tags": {"shc-entity": "acme", "shc-network": "core"}, "edge": {}}
                t.ec2, t.r53, t.ram, t.changed = ec2, None, None, []
                if what == "up":
                    t.place_up(PLACEMENT, 1001, list(V8.V4), True, v6)
                else:
                    t.place_down(PLACEMENT, 1001, v6)
        out[label] = {"ec2": [[n, kw] for n, kw in ec2.calls], "hub": copy.deepcopy(FakeEdge.calls)}
    return out


# the scenarios' calls as the module made them before IPv6-only existed (sha256 of the canonical JSON)
DUAL_STACK_DIGESTS = {
    "ipv4": "b33bb9d63af45b6e3c37a1bc13a9c9ae697b1f68a29218eb44794466f2d89868",
    "network": "fd6c1fcde2b0c49a6287261335bd8f87831620aceadb594ff6740e6f06c9ff3a",
    "provider": "f854fd92a5b1687f10ba3fbc3eaae0d5a12fa76ff09e86c629740a93d057a1b4",
}


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class PlaceholderTest(unittest.TestCase):
    def test_a_26_by_vni_stable_and_inside_the_pool(self):
        a = T.placeholder_v4(POOL4, VNI, V8.AZS)
        self.assertEqual(a, T.placeholder_v4(POOL4, VNI, V8.AZS))         # the same on every run
        n = ipaddress.ip_network(a)
        self.assertEqual(n.prefixlen, 26)
        self.assertTrue(n.subnet_of(ipaddress.ip_network(POOL4)))
        self.assertEqual(a, "100.127.%d.%d/26" % ((VNI % 1024) // 4, (VNI % 4) * 64))
        self.assertNotEqual(a, T.placeholder_v4(POOL4, VNI + 1, V8.AZS))  # neighbours differ
        self.assertEqual(T.placeholder_v4(POOL4, VNI + 1024, V8.AZS), a)  # wraps around the pool's 1024 /26s
        self.assertEqual(T.placeholder_v4("198.51.100.0/26", VNI, V8.AZS), "198.51.100.0/26")  # a pool of one

    def test_attachment_28s_per_az_and_the_az_limit(self):
        self.assertEqual(T.layout_only("100.127.4.64/26", V8.AZS),
                         [{"az": V8.AZS[0], "tgw": "100.127.4.64/28"}, {"az": V8.AZS[1], "tgw": "100.127.4.80/28"}])
        four = [V8.REGION + x for x in "abcd"]
        self.assertEqual(len(T.layout_only("100.127.4.64/26", four)), 4)
        self.assertIn("attachment /28s", refused(lambda: T.placeholder_v4(POOL4, VNI, four + [V8.REGION + "e"]))["error"])

    def test_request_keys(self):
        # a dual-stack request is as before; the IPv6-only keys come back canonical
        self.assertEqual(T.ipv6_want(V8.ipv6(source="Provider")),
                         {"source": "Provider", "internet": True, "networkPrefixes6": [NET6]})
        w = T.ipv6_want(dict(only6(), placeholderV4Pool="100.127.0.0/16", resolvers=["FD00:5C:53::10"]))
        self.assertEqual((w["placeholderV4Pool"], w["resolvers"]), (POOL4, ["fd00:5c:53::10"]))
        self.assertTrue(T.ipv6_want(only6("Provider"))["nat64"])
        for bad in ("100.127.0.0/27", "3fff::/48", "not-a-net"):
            self.assertIn("placeholderV4Pool", refused(lambda: T.ipv6_want(dict(only6(), placeholderV4Pool=bad)))["error"])
        self.assertIn("resolvers", refused(lambda: T.ipv6_want(dict(only6(), resolvers=["x"])))["error"])


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class TenantIpv6OnlyTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(T.time, "sleep", lambda s: None), mock.patch.object(T.shc_aws_edge, "Edge", FakeEdge)):
            p.start()
            self.addCleanup(p.stop)
        FakeEdge.calls = []
        self.ec2 = FakeEc2()

    def up(self, v6, egress=True):
        t = tenant(self.ec2)
        return t.place_up(PLACEMENT, VNI, [], egress, v6), t.changed

    def down(self, v6):
        t = tenant(self.ec2)
        return t.place_down(PLACEMENT, VNI, v6), t.changed

    def routes6(self, vpc):
        return {r["DestinationIpv6CidrBlock"]: {k: v for k, v in r.items() if k.endswith("Id")}
                for r in self.ec2.tables[vpc]["Routes"]
                if r.get("DestinationIpv6CidrBlock") and r["Origin"] == "CreateRoute"}

    def routes4(self, vpc):
        return [r["DestinationCidrBlock"] for r in self.ec2.tables[vpc]["Routes"]
                if r.get("DestinationCidrBlock") and r["Origin"] == "CreateRoute"]

    def work(self):
        return [s for s in self.ec2.subnets.values() if T.tagmap(s)["shc-role"] == "work"]

    def test_refused_without_the_placeholder_pool(self):
        out = refused(lambda: self.up(only6(pool="")))
        self.assertEqual(out["reason"], "FamilyUnavailable")
        self.assertIn("no placeholder IPv4 for IPv6-only VPCs", out["error"])
        self.assertEqual(self.ec2.calls, [])

    # ---- source Network
    def test_network_source(self):
        r, _ = self.up(only6())
        vpc = r["vpcId"]
        placeholder = T.placeholder_v4(POOL4, VNI, V8.AZS)
        self.assertEqual(self.ec2.names(), [
            "create_ipam", "modify_ipam", "create_ipam_pool", "provision_ipam_pool_cidr", "create_vpc",
            "modify_vpc_attribute", "create_dhcp_options", "associate_dhcp_options",
            "create_subnet", "modify_subnet_attribute", "modify_subnet_attribute", "modify_subnet_attribute",
            "create_subnet",
            "create_subnet", "modify_subnet_attribute", "modify_subnet_attribute", "modify_subnet_attribute",
            "create_subnet",
            "create_transit_gateway_vpc_attachment", "create_route", "create_route"])
        create_vpc = next(kw for n, kw in self.ec2.calls if n == "create_vpc")
        self.assertEqual(create_vpc["CidrBlock"], placeholder)
        self.assertEqual(T.tagmap(self.ec2.vpcs[vpc])["shc-ipv6-only"], "true")
        # the workload subnets: IPv6-only, resource-name hostnames with AAAA, no DNS64 (the fabric's DNS64 instead)
        for s in self.work():
            self.assertNotIn("CidrBlock", s)
            self.assertTrue(s["Ipv6Native"] and s["AssignIpv6AddressOnCreation"])
            self.assertEqual(s["PrivateDnsNameOptionsOnLaunch"],
                             {"HostnameType": "resource-name", "EnableResourceNameDnsAAAARecord": True})
            self.assertFalse(s["EnableDns64"])
        self.assertEqual(r["subnets"], [{"id": s["SubnetId"], "az": s["AvailabilityZone"],
                                         "ipv6": s["Ipv6CidrBlockAssociationSet"][0]["Ipv6CidrBlock"]} for s in self.work()])
        # the attachment subnets: a placeholder /28 and a /64 each; the attachment with IPv6
        self.assertEqual([(s["cidr"], s["ipv6"]) for s in r["attachmentSubnets"]],
                         [(x["tgw"], y["tgw"]) for x, y in zip(T.layout_only(placeholder, V8.AZS), T.layout6(BLOCK, V8.AZS))])
        att = next(kw for n, kw in self.ec2.calls if n == "create_transit_gateway_vpc_attachment")
        self.assertEqual(att["Options"], {"Ipv6Support": "enable"})
        # the option set: the translation's resolvers, on the VPC
        d = next(iter(self.ec2.dhcp.values()))
        self.assertEqual(T.dhcp_servers(d), RESOLVERS)
        self.assertEqual(T.tagmap(d)["Name"], "shc-acme-p1")
        self.assertEqual(self.ec2.vpcs[vpc]["DhcpOptionsId"], d["DhcpOptionsId"])
        # routes as dual-stack (the network's space and ::/0 to the transit gateway), no IPv4 default, no 64:ff9b
        self.assertEqual(self.routes6(vpc), {NET6: {"TransitGatewayId": TGW}, "::/0": {"TransitGatewayId": TGW}})
        self.assertEqual(self.routes4(vpc), [])
        self.assertEqual(FakeEdge.calls[0], ["net_up", VNI, {"egress": True}])
        self.assertEqual(r["placeholderV4"], placeholder)
        self.assertEqual(r["prefixes"], [])
        self.assertEqual({k: r["ipv6"][k] for k in ("only", "dns64", "dhcpOptions", "translation", "path")},
                         {"only": True, "dns64": False, "dhcpOptions": d["DhcpOptionsId"], "translation": None,
                          "path": "Fabric"})

    def test_network_source_without_translation_uses_the_vpc_resolver(self):
        self.up(only6(resolvers=[]))
        self.assertEqual(T.dhcp_servers(next(iter(self.ec2.dhcp.values()))), ["AmazonProvidedDNS"])

    def test_new_resolvers_replace_the_option_set(self):
        r, _ = self.up(only6())
        old = next(iter(self.ec2.dhcp))
        _, changed = self.up(only6(resolvers=[]))
        new = next(iter(self.ec2.dhcp))
        self.assertNotEqual(old, new)
        self.assertEqual(len(self.ec2.dhcp), 1)
        self.assertEqual(self.ec2.vpcs[r["vpcId"]]["DhcpOptionsId"], new)
        names = [n for n, _ in self.ec2.mutations()][-3:]
        self.assertEqual(names, ["create_dhcp_options", "associate_dhcp_options", "delete_dhcp_options"])
        self.assertIn("deleted dhcp options " + old, changed)

    def test_rerun_keeps_everything(self):
        r1, _ = self.up(only6())
        before = len(self.ec2.calls)
        r2, changed = self.up(only6())
        self.assertEqual(changed, [])
        self.assertEqual([n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)], [])
        self.assertEqual((r2["vpcId"], r2["placeholderV4"], r2["ipv6"]["dhcpOptions"]),
                         (r1["vpcId"], r1["placeholderV4"], r1["ipv6"]["dhcpOptions"]))

    def test_a_vpc_with_another_cidr_is_refused(self):
        self.up(only6())
        t = tenant(self.ec2)
        self.assertIn("has CIDR", refused(lambda: t.place_up(PLACEMENT, VNI + 1, [], True, only6()))["error"])

    def test_no_public_subnets(self):
        self.up(only6())
        t = tenant(self.ec2)
        self.assertIn("IPv6-only", refused(lambda: t.place_public(PLACEMENT, "up"))["error"])

    # ---- source Provider
    def test_provider_source_with_translation(self):
        r, _ = self.up(only6("Provider"))
        vpc = r["vpcId"]
        self.assertEqual(self.ec2.names(), [
            "create_vpc", "modify_vpc_attribute",
            "create_subnet", "modify_subnet_attribute", "modify_subnet_attribute", "modify_subnet_attribute",
            "modify_subnet_attribute", "create_subnet",
            "create_subnet", "modify_subnet_attribute", "modify_subnet_attribute", "modify_subnet_attribute",
            "modify_subnet_attribute", "create_subnet",
            "create_transit_gateway_vpc_attachment", "create_egress_only_internet_gateway",
            "create_route", "create_route", "create_route"])
        self.assertTrue(all(s["EnableDns64"] for s in self.work()))
        eigw = next(iter(self.ec2.eigws))
        self.assertEqual(self.routes6(vpc), {NET6: {"TransitGatewayId": TGW}, "::/0": {"EgressOnlyInternetGatewayId": eigw},
                                             NAT64: {"TransitGatewayId": TGW}})
        self.assertEqual(self.ec2.dhcp, {})                    # the VPC's own resolver: the default option set
        self.assertEqual(FakeEdge.calls[0], ["net_up", VNI, {"egress": True, "nat64": [AMAZON]}])
        self.assertEqual(r["ipv6"]["translation"], {"path": "Provider", "pref64": NAT64,
                                                    "resolvers": ["fd00:ec2::253"], "ipv4Source": EIP})
        self.assertTrue(r["ipv6"]["dns64"])
        self.assertEqual(self.routes4(vpc), [])

    def test_provider_translation_needs_the_egress_vpc(self):
        r, _ = self.up(only6("Provider"), egress=False)
        self.assertNotIn(NAT64, self.routes6(r["vpcId"]))
        self.assertFalse(any(s["EnableDns64"] for s in self.work()))
        self.assertEqual(FakeEdge.calls[0], ["net_up", VNI, {"egress": False, "nat64": []}])
        self.assertIsNone(r["ipv6"]["translation"])

    def test_provider_internet_off_and_on(self):
        r, _ = self.up(only6("Provider"))
        before = len(self.ec2.calls)
        r2, changed = self.up(only6("Provider", internet=False), egress=False)
        names = [n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)]
        self.assertEqual(names, ["modify_subnet_attribute", "modify_subnet_attribute", "delete_route", "delete_route",
                                 "delete_egress_only_internet_gateway"])
        self.assertEqual(self.routes6(r["vpcId"]), {NET6: {"TransitGatewayId": TGW}})
        self.assertFalse(any(s["EnableDns64"] for s in self.work()))
        self.assertIn("deleted route " + NAT64, changed)
        self.assertEqual(FakeEdge.calls[-2], ["net_up", VNI, {"egress": False, "nat64": []}])
        self.assertIsNone(r2["ipv6"]["translation"])
        r3, _ = self.up(only6("Provider"))
        self.assertIn(NAT64, self.routes6(r["vpcId"]))
        self.assertTrue(all(s["EnableDns64"] for s in self.work()))
        self.assertEqual(r3["ipv6"]["translation"]["ipv4Source"], EIP)

    def test_provider_translation_off_with_internet_on(self):
        r, _ = self.up(only6("Provider"))
        self.up(only6("Provider", nat64=False))
        self.assertEqual(set(self.routes6(r["vpcId"])), {NET6, "::/0"})
        self.assertEqual(FakeEdge.calls[-2], ["net_up", VNI, {"egress": True, "nat64": []}])

    # ---- teardown
    def test_teardown_network_source_in_reverse(self):
        r, _ = self.up(only6())
        before = len(self.ec2.calls)
        out, _ = self.down({"source": "Network", "prefix": BLOCK, "only": True})
        names = [n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)]
        order = ["delete_route", "delete_transit_gateway_vpc_attachment", "delete_subnet", "associate_dhcp_options",
                 "delete_dhcp_options", "disassociate_vpc_cidr_block", "delete_vpc", "delete_ipam_pool", "delete_ipam"]
        self.assertEqual([n for i, n in enumerate(names) if i == 0 or names[i - 1] != n], order)
        assoc = next(kw for n, kw in self.ec2.calls[before:] if n == "associate_dhcp_options")
        self.assertEqual(assoc["DhcpOptionsId"], "default")
        self.assertEqual((self.ec2.vpcs, self.ec2.subnets, self.ec2.dhcp, self.ec2.pools), ({}, {}, {}, {}))
        self.assertEqual(FakeEdge.calls[-1], ["net_down", VNI])

    def test_teardown_provider_source(self):
        self.up(only6("Provider"))
        before = len(self.ec2.calls)
        self.down({"source": "Provider", "prefix": AMAZON, "only": True})
        names = [n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)]
        self.assertEqual(names.count("delete_route"), 3)      # the network's space, ::/0 and 64:ff9b::/96
        self.assertLess(names.index("delete_egress_only_internet_gateway"), names.index("delete_vpc"))
        self.assertEqual(self.ec2.vpcs, {})

    def test_the_networks_egress_vpc_does_not_keep_the_hub_side(self):
        # the NAT egress VPC's attachment carries the network's VNI tag too; it is not another tenant VPC
        self.up(only6("Provider"))
        egress = [{"ResourceType": "vpc", "TransitGatewayAttachmentId": "tgw-attach-egress",
                   "Tags": [{"Key": "shc-vni", "Value": str(VNI)}, {"Key": "shc-role", "Value": "egress"}]}]
        with mock.patch.object(FakeEdge, "attachments", lambda self, tgw, **f: egress):
            out, _ = self.down({"source": "Provider", "only": True})
        self.assertFalse(out["networkKept"])
        self.assertEqual(FakeEdge.calls[-1], ["net_down", VNI])
        tenant_att = [dict(egress[0], Tags=[{"Key": "shc-vni", "Value": str(VNI)}, {"Key": "shc-role", "Value": "tenant"}])]
        with mock.patch.object(FakeEdge, "attachments", lambda self, tgw, **f: tenant_att):
            out, _ = self.down({"source": "Provider", "only": True})
        self.assertTrue(out["networkKept"])

    def test_teardown_finds_an_option_set_without_its_vpc(self):
        t = tenant(self.ec2)
        self.ec2.dhcp["dopt-x"] = {"DhcpOptionsId": "dopt-x", "Tags": [{"Key": "shc-placement", "Value": PLACEMENT}],
                                   "DhcpConfigurations": []}
        t.place_down(PLACEMENT, VNI, {"source": "Network", "only": True})
        self.assertEqual(self.ec2.dhcp, {})

    def test_cli_place_up_without_prefix(self):
        seen = {}

        class Stub:
            check = share_down = None

            def __init__(self, cfg):
                self.changed = []

            def place_up(self, *a):
                seen["up"] = a
                return {}
        argv = ["x", "--config", "{}", "place-up", "--placement", PLACEMENT, "--vni", str(VNI), "--egress",
                "--ipv6", json.dumps(only6())]
        with mock.patch.object(T, "Tenant", Stub), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            T.main()
        self.assertEqual(seen["up"], (PLACEMENT, VNI, [], True, only6()))


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class DualStackUnchangedTest(unittest.TestCase):
    def test_calls_byte_identical(self):
        got = dual_stack_scenarios(T, FakeEc2)
        for label, want in DUAL_STACK_DIGESTS.items():
            self.assertEqual(digest(got[label]), want, label)
        # and in words: no IPv6-only object or attribute appears
        names = {n for s in got.values() for n, _ in s["ec2"]}
        self.assertFalse(names & {"create_dhcp_options", "associate_dhcp_options", "describe_dhcp_options"})
        self.assertFalse(any("Ipv6Native" in kw or "EnableDns64" in kw for s in got.values() for _, kw in s["ec2"]))
        self.assertFalse(any("nat64" in c[2] for s in got.values() for c in s["hub"] if c[0] == "net_up"))


# ---- the hub: NAT64 in the network's NAT egress VPC -------------------------------------------------------------------

if HAVE_BOTO:
    class FakeHub:
        """Just enough hub EC2 for a network's NAT egress VPC; calls recorded and checked against the API model."""
        _model = None

        def __init__(self):
            if FakeHub._model is None:
                m = botocore.session.get_session().get_service_model("ec2")
                FakeHub._model = {xform_name(op): m.operation_model(op) for op in m.operation_names}
            self.calls, self.n = [], 0
            self.vpcs, self.subnets, self.igws, self.tables, self.nats, self.eips = {}, {}, {}, {}, {}, {}
            self.atts = {"tgw-attach-connect": {"TransitGatewayAttachmentId": "tgw-attach-connect", "ResourceType": "connect",
                                                "State": "available", "Tags": []}}
            self.tgw_rts = {"tgw-rtb-net": {"TransitGatewayRouteTableId": "tgw-rtb-net", "State": "available",
                                            "Tags": [{"Key": "Name", "Value": "shc-net-%d" % VNI}]}}
            self.assoc, self.static = {}, {}
            self.amazon = [EGRESS6]

        def _id(self, kind):
            self.n += 1
            return "%s-%04d" % (kind, self.n)

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name == "get_waiter":
                return lambda waiter: mock.Mock()
            op = self._model.get(name)
            handler = getattr(self, "h_" + name, None)
            if op is None or handler is None:
                raise AssertionError("unexpected EC2 call " + name)

            def call(**kw):
                # botocore's alias: an EC2 member named Filter is passed as Filters (describe_nat_gateways)
                given = {("Filter" if k == "Filters" and "Filter" in op.input_shape.members else k) for k in kw}
                unknown = given - set(op.input_shape.members)
                if unknown:
                    raise AssertionError("%s: not in the EC2 API: %s" % (name, sorted(unknown)))
                missing = set(op.input_shape.required_members) - given
                if missing:
                    raise AssertionError("%s: missing %s" % (name, sorted(missing)))
                self.calls.append([name, copy.deepcopy(kw)])
                return handler(**kw)
            return call

        def names(self, since=0):
            return [n for n, _ in self.calls[since:] if n.startswith(MUTATING)]

        @staticmethod
        def _tags(kw):
            return [t for s in kw.get("TagSpecifications") or [] for t in s["Tags"]]

        @staticmethod
        def _f(Filters):
            return {f["Name"]: f["Values"] for f in Filters or []}

        def h_describe_transit_gateways(self, **kw):
            return {"TransitGateways": [{"TransitGatewayId": TGW, "State": "available"}]}

        def h_describe_vpcs(self, Filters=None, VpcIds=None, **kw):
            out = list(self.vpcs.values())
            if VpcIds:
                out = [v for v in out if v["VpcId"] in VpcIds]
            f = self._f(Filters)
            if "tag:Name" in f:
                out = [v for v in out if E.tagmap(v).get("Name") in f["tag:Name"]]
            return {"Vpcs": copy.deepcopy(out)}

        def h_create_vpc(self, CidrBlock, **kw):
            v = self._id("vpc")
            self.vpcs[v] = {"VpcId": v, "CidrBlock": CidrBlock, "Tags": self._tags(kw), "Ipv6CidrBlockAssociationSet": []}
            self.tables["main-" + v] = {"RouteTableId": "rtb-main-" + v, "VpcId": v, "Tags": [],
                                        "Associations": [{"Main": True}], "Routes": []}
            return {"Vpc": copy.deepcopy(self.vpcs[v])}

        def h_associate_vpc_cidr_block(self, VpcId, **kw):
            self.vpcs[VpcId]["Ipv6CidrBlockAssociationSet"].append(
                {"AssociationId": "assoc-" + VpcId, "Ipv6CidrBlock": self.amazon.pop(0), "Ipv6Pool": "Amazon",
                 "Ipv6CidrBlockState": {"State": "associated"}})
            return {}

        def h_delete_vpc(self, VpcId, **kw):
            del self.vpcs[VpcId]
            return {}

        def h_describe_subnets(self, Filters=None, **kw):
            vpc = self._f(Filters)["vpc-id"][0]
            return {"Subnets": [copy.deepcopy(s) for s in self.subnets.values() if s["VpcId"] == vpc]}

        def h_create_subnet(self, VpcId, CidrBlock, **kw):
            s = self._id("subnet")
            self.subnets[s] = {"SubnetId": s, "VpcId": VpcId, "CidrBlock": CidrBlock, "Tags": self._tags(kw),
                               "Ipv6CidrBlockAssociationSet": []}
            return {"Subnet": copy.deepcopy(self.subnets[s])}

        def h_associate_subnet_cidr_block(self, SubnetId, Ipv6CidrBlock, **kw):
            vpc = self.vpcs[self.subnets[SubnetId]["VpcId"]]
            if not any(ipaddress.ip_network(Ipv6CidrBlock).subnet_of(ipaddress.ip_network(a["Ipv6CidrBlock"]))
                       for a in vpc["Ipv6CidrBlockAssociationSet"]):
                raise V8.client_error("InvalidSubnet.Range", Ipv6CidrBlock)
            self.subnets[SubnetId]["Ipv6CidrBlockAssociationSet"].append(
                {"Ipv6CidrBlock": Ipv6CidrBlock, "Ipv6CidrBlockState": {"State": "associated"}})
            return {}

        def h_delete_subnet(self, SubnetId, **kw):
            del self.subnets[SubnetId]
            return {}

        def h_describe_internet_gateways(self, Filters=None, **kw):
            vpc = self._f(Filters)["attachment.vpc-id"][0]
            return {"InternetGateways": [copy.deepcopy(g) for g in self.igws.values() if g.get("VpcId") == vpc]}

        def h_create_internet_gateway(self, **kw):
            g = self._id("igw")
            self.igws[g] = {"InternetGatewayId": g, "Tags": self._tags(kw)}
            return {"InternetGateway": copy.deepcopy(self.igws[g])}

        def h_attach_internet_gateway(self, InternetGatewayId, VpcId, **kw):
            self.igws[InternetGatewayId]["VpcId"] = VpcId
            return {}

        def h_detach_internet_gateway(self, InternetGatewayId, VpcId, **kw):
            self.igws[InternetGatewayId].pop("VpcId")
            return {}

        def h_delete_internet_gateway(self, InternetGatewayId, **kw):
            del self.igws[InternetGatewayId]
            return {}

        def h_describe_route_tables(self, Filters=None, RouteTableIds=None, **kw):
            out = list(self.tables.values())
            if RouteTableIds:
                out = [t for t in out if t["RouteTableId"] in RouteTableIds]
            f = self._f(Filters)
            if "vpc-id" in f:
                out = [t for t in out if t["VpcId"] in f["vpc-id"]]
            return {"RouteTables": copy.deepcopy(out)}

        def h_create_route_table(self, VpcId, **kw):
            r = self._id("rtb")
            self.tables[r] = {"RouteTableId": r, "VpcId": VpcId, "Tags": self._tags(kw), "Associations": [],
                              "Routes": []}
            return {"RouteTable": copy.deepcopy(self.tables[r])}

        def h_associate_route_table(self, RouteTableId, SubnetId, **kw):
            self.tables[RouteTableId]["Associations"].append({"SubnetId": SubnetId})
            return {}

        def h_delete_route_table(self, RouteTableId, **kw):
            del self.tables[RouteTableId]
            return {}

        def _key(self, kw):
            return "DestinationIpv6CidrBlock" if "DestinationIpv6CidrBlock" in kw else "DestinationCidrBlock"

        def h_create_route(self, RouteTableId, **kw):
            k = self._key(kw)
            t = self.tables[RouteTableId]
            if any(r.get(k) == kw[k] for r in t["Routes"]):
                raise V8.client_error("RouteAlreadyExists", kw[k])
            t["Routes"].append(dict(kw, State="active", Origin="CreateRoute"))
            return {}

        def h_replace_route(self, RouteTableId, **kw):
            k = self._key(kw)
            t = self.tables[RouteTableId]
            t["Routes"] = [r for r in t["Routes"] if r.get(k) != kw[k]] + [dict(kw, State="active", Origin="CreateRoute")]
            return {}

        def h_delete_route(self, RouteTableId, **kw):
            k = self._key(kw)
            t = self.tables[RouteTableId]
            t["Routes"] = [r for r in t["Routes"] if r.get(k) != kw[k]]
            return {}

        def h_describe_nat_gateways(self, Filters=None, NatGatewayIds=None, **kw):
            out = list(self.nats.values())
            if NatGatewayIds:
                out = [n for n in out if n["NatGatewayId"] in NatGatewayIds]
            f = self._f(Filters)
            if "subnet-id" in f:
                out = [n for n in out if n["SubnetId"] in f["subnet-id"]]
            if "vpc-id" in f:
                out = [n for n in out if n["VpcId"] in f["vpc-id"]]
            return {"NatGateways": copy.deepcopy(out)}

        def h_describe_addresses(self, Filters=None, **kw):
            name = self._f(Filters).get("tag:Name", [None])[0]
            return {"Addresses": [copy.deepcopy(a) for a in self.eips.values() if E.tagmap(a).get("Name") == name]}

        def h_allocate_address(self, **kw):
            a = self._id("eipalloc")
            self.eips[a] = {"AllocationId": a, "PublicIp": EIP, "Tags": self._tags(kw)}
            return {"AllocationId": a, "PublicIp": EIP}

        def h_release_address(self, AllocationId, **kw):
            del self.eips[AllocationId]
            return {}

        def h_create_nat_gateway(self, SubnetId, AllocationId, **kw):
            n = self._id("nat")
            self.nats[n] = {"NatGatewayId": n, "SubnetId": SubnetId, "VpcId": self.subnets[SubnetId]["VpcId"],
                            "State": "available", "NatGatewayAddresses": [{"PublicIp": EIP}]}
            return {"NatGateway": copy.deepcopy(self.nats[n])}

        def h_delete_nat_gateway(self, NatGatewayId, **kw):
            del self.nats[NatGatewayId]
            return {}

        def h_describe_transit_gateway_attachments(self, Filters=None, TransitGatewayAttachmentIds=None, **kw):
            out = [a for a in self.atts.values() if a["State"] != "deleted"]
            if TransitGatewayAttachmentIds:
                out = [a for a in self.atts.values() if a["TransitGatewayAttachmentId"] in TransitGatewayAttachmentIds]
            for k, v in self._f(Filters).items():
                if k.startswith("tag:"):
                    out = [a for a in out if E.tagmap(a).get(k[4:]) in v]
            return {"TransitGatewayAttachments": copy.deepcopy(out)}

        def h_create_transit_gateway_vpc_attachment(self, TransitGatewayId, VpcId, SubnetIds, **kw):
            opts = kw.get("Options") or {}
            if opts.get("Ipv6Support") == "enable" and not all(self.subnets[s]["Ipv6CidrBlockAssociationSet"] for s in SubnetIds):
                raise V8.client_error("InvalidParameterValue", "IPv6 support needs IPv6 on every attachment subnet")
            a = self._id("tgw-attach")
            self.atts[a] = {"TransitGatewayAttachmentId": a, "ResourceType": "vpc", "State": "available", "VpcId": VpcId,
                            "Tags": self._tags(kw), "Options": {"Ipv6Support": opts.get("Ipv6Support", "disable")}}
            return {"TransitGatewayVpcAttachment": copy.deepcopy(self.atts[a])}

        def h_describe_transit_gateway_vpc_attachments(self, TransitGatewayAttachmentIds=None, **kw):
            return {"TransitGatewayVpcAttachments": [copy.deepcopy(self.atts[a]) for a in TransitGatewayAttachmentIds]}

        def h_modify_transit_gateway_vpc_attachment(self, TransitGatewayAttachmentId, Options=None, **kw):
            a = self.atts[TransitGatewayAttachmentId]
            if (Options or {}).get("Ipv6Support") == "enable":
                sub = [s for s in self.subnets.values() if s["VpcId"] == a["VpcId"] and E.tagmap(s)["Name"].endswith("-tgw")]
                if not all(s["Ipv6CidrBlockAssociationSet"] for s in sub):
                    raise V8.client_error("InvalidParameterValue", "IPv6 support needs IPv6 on the subnets")
            a["Options"].update(Options or {})
            return {}

        def h_delete_transit_gateway_vpc_attachment(self, TransitGatewayAttachmentId, **kw):
            self.atts[TransitGatewayAttachmentId]["State"] = "deleted"
            return {}

        def h_describe_transit_gateway_route_tables(self, Filters=None, **kw):
            out = list(self.tgw_rts.values())
            f = self._f(Filters)
            if "tag:Name" in f:
                out = [r for r in out if E.tagmap(r).get("Name") in f["tag:Name"]]
            return {"TransitGatewayRouteTables": copy.deepcopy(out)}

        def h_get_transit_gateway_route_table_associations(self, TransitGatewayRouteTableId, **kw):
            return {"Associations": [{"TransitGatewayAttachmentId": a, "State": "associated"}
                                     for a, rt in self.assoc.items() if rt == TransitGatewayRouteTableId]}

        def h_associate_transit_gateway_route_table(self, TransitGatewayRouteTableId, TransitGatewayAttachmentId, **kw):
            self.assoc[TransitGatewayAttachmentId] = TransitGatewayRouteTableId
            return {}

        def h_search_transit_gateway_routes(self, TransitGatewayRouteTableId, Filters, **kw):
            dest = self._f(Filters)["route-search.exact-match"][0]
            att = self.static.get((TransitGatewayRouteTableId, dest))
            return {"Routes": [{"DestinationCidrBlock": dest, "Type": "static",
                                "TransitGatewayAttachments": [{"TransitGatewayAttachmentId": att}]}] if att else []}

        def h_create_transit_gateway_route(self, DestinationCidrBlock, TransitGatewayRouteTableId, **kw):
            key = (TransitGatewayRouteTableId, DestinationCidrBlock)
            if key in self.static:
                raise V8.client_error("RouteAlreadyExists", DestinationCidrBlock)
            self.static[key] = kw["TransitGatewayAttachmentId"]
            return {}

        def h_replace_transit_gateway_route(self, DestinationCidrBlock, TransitGatewayRouteTableId, **kw):
            self.static[(TransitGatewayRouteTableId, DestinationCidrBlock)] = kw["TransitGatewayAttachmentId"]
            return {}

        def h_delete_transit_gateway_route(self, TransitGatewayRouteTableId, DestinationCidrBlock, **kw):
            del self.static[(TransitGatewayRouteTableId, DestinationCidrBlock)]
            return {}


def hub_edge(ec2, module=None):
    module = module or E
    e = module.Edge.__new__(module.Edge)
    e.cfg = {"account": V8.ACCOUNT, "region": "xx-test-1", "site": "site-x", "tgwName": "tgw",
             "egress": {"cidrPool": "10.250.16.0/20", "az": "xx-test-1a"}}
    e.v6, e.pool6, e.site, e.ec2, e.changed = False, None, "site-x", ec2, []
    e.prefix, e.param = "shc-edge-site-x", "/shc/edge/site-x/config"
    return e


def egress_scenario(module, nat64_runs=None):
    """egress-up twice, egress-down; the hub's calls in order. nat64_runs: the nat64 argument per egress-up (None:
    the call a dual-stack placement makes, without one)."""
    ec2 = FakeHub()
    with mock.patch.object(module.time, "sleep", lambda s: None):
        for nat64 in nat64_runs or [None, None]:
            e = hub_edge(ec2, module)
            if nat64 is None:
                e.egress_up(VNI, "tgw-rtb-net")
            else:
                e.egress_up(VNI, "tgw-rtb-net", nat64=nat64)
        hub_edge(ec2, module).egress_down(VNI)
    return ec2


# the hub's calls for a network's NAT egress VPC (up, re-run, down) before NAT64 existed (sha256)
EGRESS_DIGEST = "b331dbe391422fa85cfa57d958212c1a0bfad1235a4265abaa462fcd1fbb527b"


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class HubNat64Test(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(E.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def table(self, ec2, role):
        return next(t for t in ec2.tables.values() if E.tagmap(t).get("Name") == "shc-egress-%d-%s" % (VNI, role))

    def routes6(self, t):
        return {r["DestinationIpv6CidrBlock"]: {k: v for k, v in r.items() if k.endswith("Id") and k != "RouteTableId"}
                for r in t["Routes"] if r.get("DestinationIpv6CidrBlock")}

    def test_dual_stack_calls_unchanged(self):
        self.assertEqual(digest(egress_scenario(E).calls), EGRESS_DIGEST)

    def test_nat64_up(self):
        ec2 = FakeHub()
        out = hub_edge(ec2).egress_up(VNI, "tgw-rtb-net", nat64=[AMAZON])
        vpc = ec2.vpcs[out["vpc"]]
        self.assertEqual(vpc["Ipv6CidrBlockAssociationSet"][0]["Ipv6CidrBlock"], EGRESS6)
        six = {E.tagmap(s)["Name"]: s["Ipv6CidrBlockAssociationSet"][0]["Ipv6CidrBlock"] for s in ec2.subnets.values()}
        self.assertEqual(six, {"shc-egress-%d-tgw" % VNI: "3fff:e:1:100::/64", "shc-egress-%d-public" % VNI: "3fff:e:1:101::/64"})
        att = ec2.atts[out["attachment"]]
        self.assertEqual(att["Options"]["Ipv6Support"], "enable")
        self.assertEqual(self.routes6(self.table(ec2, "tgw")), {NAT64: {"NatGatewayId": out["natGateway"]}})
        self.assertEqual(self.routes6(self.table(ec2, "public")), {AMAZON: {"TransitGatewayId": TGW}})
        self.assertEqual(ec2.static[("tgw-rtb-net", NAT64)], out["attachment"])
        self.assertEqual(ec2.static[("tgw-rtb-net", "0.0.0.0/0")], out["attachment"])   # IPv4 as before
        self.assertEqual(out["nat64"], {"prefix": NAT64, "block": EGRESS6, "returns": [AMAZON]})
        self.assertEqual(out["publicIp"], EIP)
        # the order: IPv6 on the VPC and subnets before the attachment
        names = ec2.names()
        self.assertLess(names.index("associate_subnet_cidr_block"), names.index("create_transit_gateway_vpc_attachment"))
        # a re-run changes nothing
        before = len(ec2.calls)
        e = hub_edge(ec2)
        e.egress_up(VNI, "tgw-rtb-net", nat64=[AMAZON])
        self.assertEqual((ec2.names(before), e.changed), ([], []))

    def test_an_existing_egress_vpc_gains_nat64(self):
        ec2 = FakeHub()
        out = hub_edge(ec2).egress_up(VNI, "tgw-rtb-net")
        self.assertEqual(ec2.atts[out["attachment"]]["Options"]["Ipv6Support"], "disable")
        self.assertNotIn("nat64", out)
        before = len(ec2.calls)
        out = hub_edge(ec2).egress_up(VNI, "tgw-rtb-net", nat64=[AMAZON])
        names = ec2.names(before)
        self.assertEqual(names, ["associate_vpc_cidr_block", "associate_subnet_cidr_block", "associate_subnet_cidr_block",
                                 "modify_transit_gateway_vpc_attachment", "create_route", "create_route",
                                 "create_transit_gateway_route"])
        self.assertEqual(ec2.atts[out["attachment"]]["Options"]["Ipv6Support"], "enable")

    def test_nat64_removed_and_the_ipv4_side_kept(self):
        ec2 = FakeHub()
        hub_edge(ec2).egress_up(VNI, "tgw-rtb-net", nat64=[AMAZON])
        out = hub_edge(ec2).egress_up(VNI, "tgw-rtb-net", nat64=[])
        self.assertIsNone(out["nat64"])
        self.assertEqual(self.routes6(self.table(ec2, "tgw")), {})
        self.assertEqual(self.routes6(self.table(ec2, "public")), {})
        self.assertNotIn(("tgw-rtb-net", NAT64), ec2.static)
        self.assertIn(("tgw-rtb-net", "0.0.0.0/0"), ec2.static)

    def test_egress_down_removes_the_static_nat64_first(self):
        ec2 = FakeHub()
        hub_edge(ec2).egress_up(VNI, "tgw-rtb-net", nat64=[AMAZON])
        before = len(ec2.calls)
        hub_edge(ec2).egress_down(VNI)
        names = ec2.names(before)
        self.assertEqual(names[:3], ["delete_transit_gateway_route", "delete_transit_gateway_route",
                                     "delete_transit_gateway_vpc_attachment"])
        self.assertEqual((ec2.static, ec2.vpcs, ec2.nats), ({}, {}, {}))

    def test_routers_config_takes_the_pool(self):
        e = hub_edge(FakeHub())
        self.assertEqual(e.placeholder_conf({"vrfs": []}), {"vrfs": []})
        e.cfg["placeholderV4Pool"] = " 100.127.0.0/16 "
        self.assertEqual(e.placeholder_conf({"vrfs": []}), {"vrfs": [], "placeholderV4Pool": POOL4})
        e.cfg["placeholderV4Pool"] = ""
        self.assertEqual(e.placeholder_conf({"vrfs": [], "placeholderV4Pool": POOL4}), {"vrfs": []})
        e.cfg["placeholderV4Pool"] = "3fff::/48"
        self.assertIn("not an IPv4 network", refused(lambda: e.placeholder_conf({}))["error"])


# ---- the routers: the placeholder never enters a network VRF -----------------------------------------------------------

class RouterDenyTest(unittest.TestCase):
    def setUp(self):
        import test_aws_edge_ipv6 as V8E
        self.V8E = V8E

    def test_without_a_pool_the_render_is_unchanged(self):
        self.assertEqual(self.V8E.render(self.V8E.agent_cfg()), self.V8E.IPV4_RENDER)
        c = self.V8E.agent_cfg()
        c["placeholderV4Pool"] = ""
        self.assertEqual(self.V8E.render(c), self.V8E.IPV4_RENDER)

    def test_every_vrf_denies_the_pool_before_its_permit(self):
        for v6 in (None, self.V8E.six(internet=True)):
            c = self.V8E.agent_cfg(v6)
            want = self.V8E.render(c)
            for vrf in ("t51000", "t51001"):
                block = ["ip prefix-list %s-placeholder4 seq 5 permit %s le 32" % (vrf, POOL4),
                         "route-map %s-tgw-in deny 5" % vrf, " match ip address prefix-list %s-placeholder4" % vrf, "exit"]
                anchor = "route-map %s-tgw-in permit 10\n" % vrf
                want = want.replace(anchor, "\n".join(block) + "\n" + anchor)
            c["placeholderV4Pool"] = POOL4
            # exactly the deny block ahead of each VRF's permit, nothing else
            self.assertEqual(self.V8E.render(c), want)

    def test_a_host_pool_has_no_le(self):
        self.assertEqual(A.placeholder4("t1", "100.127.0.1/32")[0], "ip prefix-list t1-placeholder4 seq 5 permit 100.127.0.1/32")

    def test_ipv6_policy_untouched(self):
        c = self.V8E.agent_cfg(self.V8E.six(internet=True))
        before = [x for x in self.V8E.render(c).splitlines() if "6" in x.split(" ")[0] or "-in6" in x or "-out6" in x]
        c["placeholderV4Pool"] = POOL4
        after = [x for x in self.V8E.render(c).splitlines() if "6" in x.split(" ")[0] or "-in6" in x or "-out6" in x]
        self.assertEqual(before, after)


# ---- the task files --------------------------------------------------------------------------------------------------

def task_vars(task):
    return {"vars": task["vars"], "ansible.builtin.set_fact": {"_out": {k: "{{ %s }}" % k for k in task["vars"]
                                                                          if not k.startswith("_")}}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TasksTest(unittest.TestCase):
    def backend(self, names, extra, out):
        tasks = [by_name(load(os.path.join(NP, "backend_cloudaws.yml")), n) for n in names]
        return run_play(tasks + [write_out(out)], extra)

    def test_refused_without_a_placeholder_pool(self):
        names = ["Is the placement IPv6-only", "Refuse an IPv6-only placement without the site's placeholder IPv4"]
        out = self.backend(names, {"np_prefixes_v4": [], "aws_real": {"tenant": {"placeholderV4Pool": ""}},
                                   "fab_name": "p1"},
                           "{'reason': fab_fail_reason | default(''), 'msg': fab_fail_msg | default('')}")
        self.assertEqual(out, {"reason": "FamilyUnavailable",
                               "msg": "NetworkPlacement p1: this site has no placeholder IPv4 for IPv6-only VPCs"})
        out = self.backend(names, {"np_prefixes_v4": [], "aws_real": {"tenant": {"placeholderV4Pool": POOL4}},
                                   "fab_name": "p1"}, "fab_fail_reason | default('')")
        self.assertEqual(out, "")
        out = self.backend(names, {"np_prefixes_v4": ["10.60.4.0/24"], "aws_real": {"tenant": {"placeholderV4Pool": ""}},
                                   "fab_name": "p1"}, "fab_fail_reason | default('')")
        self.assertEqual(out, "")                              # dual-stack never needs one

    def only_side(self, source, translation, nat_egress=True, v4=()):
        names = ["Is the placement IPv6-only", "The network's NAT egress VPC for an IPv6-only placement",
                 "The placement's IPv6-only side here"]
        v6 = {"source": source, "internet": True, "networkPrefixes6": [NET6]}
        # facts the earlier tasks set (extra vars would win over the tasks' set_fact)
        start = {"ansible.builtin.set_fact": {"np_aws_nat_egress": nat_egress, "np_aws_ipv6": v6}}
        tasks = [start] + [by_name(load(os.path.join(NP, "backend_cloudaws.yml")), n) for n in names]
        return run_play(tasks + [write_out("{'egress': np_aws_nat_egress, 'ipv6': np_aws_ipv6}")],
                        {"np_prefixes_v4": list(v4), "aws_real": {"tenant": {"placeholderV4Pool": POOL4}},
                         "np_ipv6_source": source, "np_translation": translation})

    def test_network_source_hands_the_resolvers_and_builds_no_egress_vpc(self):
        out = self.only_side("Network", {"pref64": "3fff:66::/96", "resolvers": RESOLVERS})
        self.assertEqual(out["ipv6"], {"source": "Network", "internet": True, "networkPrefixes6": [NET6],
                                       "placeholderV4Pool": POOL4, "resolvers": RESOLVERS})
        self.assertFalse(out["egress"])
        self.assertEqual(self.only_side("Network", {})["ipv6"]["resolvers"], [])

    def test_provider_source_translates_with_the_egress_vpc(self):
        out = self.only_side("Provider", {"path": "Provider", "pending": True})
        self.assertEqual(out, {"egress": True, "ipv6": {"source": "Provider", "internet": True,
                                                        "networkPrefixes6": [NET6], "placeholderV4Pool": POOL4,
                                                        "nat64": True}})
        out = self.only_side("Provider", {})                   # not qualifying: no egress VPC, no NAT64
        self.assertEqual((out["egress"], out["ipv6"]["nat64"]), (False, False))
        out = self.only_side("Provider", {"path": "Provider", "pending": True}, nat_egress=False)
        self.assertEqual((out["egress"], out["ipv6"]["nat64"]), (False, False))

    def test_dual_stack_request_unchanged(self):
        out = self.only_side("Provider", {}, v4=["10.60.4.0/24"])
        self.assertEqual(out, {"egress": True, "ipv6": {"source": "Provider", "internet": True, "networkPrefixes6": [NET6]}})

    def test_place_up_without_a_prefix(self):
        task = by_name(load(os.path.join(NP, "backend_cloudaws.yml")), "The placement's VPC and its attachment to the network")
        args = run_play([task_vars(task), write_out("_out")],
                        {"np_aws_cfg": {}, "fab_namespace": "acme", "fab_name": "p1", "np_l3vni": VNI,
                         "np_prefixes_v4": [], "np_aws_nat_egress": True, "np_aws_ipv6": only6("Provider")})["aws_args"]
        self.assertEqual(args[:6], ["place-up", "--placement", "acme/p1", "--vni", str(VNI), "--egress"])
        self.assertEqual(json.loads(args[-1]), only6("Provider"))

    def test_translation_and_placeholder_recorded(self):
        result = {"account": "1", "vpcId": "vpc-1", "subnets": [], "attachment": "a", "routeTable": "r",
                  "connectAttachment": "c", "egress": {"publicIp": EIP}, "placeholderV4": "100.127.199.0/26",
                  "ipv6": {"source": "Provider", "translation": {"path": "Provider", "pref64": NAT64,
                                                                 "resolvers": ["fd00:ec2::253"], "ipv4Source": EIP}}}
        names = ["The site side's translation", "Record the site side", "Record the site side's IPv6",
                 "Record the placeholder IPv4"]
        out = self.backend(names, {"aws_result": {"result": result, "changed": []}, "np_aws_cfg": {"region": "r"}},
                           "{'tr': np_backend_translation, 'site': np_backend_status}")
        self.assertEqual(out["tr"], result["ipv6"]["translation"])
        self.assertEqual(out["site"]["placeholderV4"], "100.127.199.0/26")
        self.assertEqual(out["site"]["egress"], "nat-vpc " + EIP)
        result = dict(result, ipv6={"source": "Network"})
        result.pop("placeholderV4")
        out = self.backend(names, {"aws_result": {"result": result, "changed": []}, "np_aws_cfg": {"region": "r"}},
                           "{'tr': np_backend_translation, 'site': np_backend_status}")
        self.assertEqual(out["tr"], {})
        self.assertNotIn("placeholderV4", out["site"])

    def test_teardown_says_ipv6_only(self):
        task = by_name(load(os.path.join(NPD, "main.yml")),
                       "Remove the placement's VPC (and the network's hub side with its last placement)")

        def args(spec):
            return run_play([task_vars(task), write_out("_out")],
                            {"fab_spec": spec, "fab_st": {}, "_npd_aws_env": {"resources": []}, "aws_real": {},
                             "aws_edge_cfg": {}, "npd_real": {"l3Vni": VNI}, "fab_namespace": "acme",
                             "fab_name": "p1"})["aws_args"]
        self.assertEqual(json.loads(args({"ipv6": {"source": "Provider"}})[-1]), {"source": "Provider", "only": True})
        self.assertEqual(json.loads(args({"ipv4": {}, "ipv6": {}})[-1]), {"source": "Network"})

    def test_realization_default_and_edge_config(self):
        tasks = load(os.path.join(COMMON_TASKS, "aws_realization.yml"))
        real = by_name(tasks, "The installation's AWS realization")
        edge = by_name(tasks, "The edge site's settings")
        base = {"hub": {"account": "1", "region": "xx-test-1"}, "edge": {"tgwName": "tgw"}}
        out = run_play([copy.deepcopy(real), copy.deepcopy(edge), write_out("{'real': aws_real, 'edge': aws_edge_cfg}")],
                       {"aws_realization": base, "aws_edge_site": "s", "aws_edge_cidr": "192.0.2.0/24", "aws_edge_asn": 1})
        self.assertEqual(out["real"]["tenant"], {"placeholderV4Pool": ""})
        self.assertNotIn("placeholderV4Pool", out["edge"])
        out = run_play([copy.deepcopy(real), copy.deepcopy(edge), write_out("{'real': aws_real, 'edge': aws_edge_cfg}")],
                       {"aws_realization": dict(base, tenant={"azs": ["a"], "placeholderV4Pool": POOL4}),
                        "aws_edge_site": "s", "aws_edge_cidr": "192.0.2.0/24", "aws_edge_asn": 1})
        self.assertEqual(out["real"]["tenant"], {"azs": ["a"], "placeholderV4Pool": POOL4})
        self.assertEqual(out["edge"]["placeholderV4Pool"], POOL4)


if __name__ == "__main__":
    unittest.main()

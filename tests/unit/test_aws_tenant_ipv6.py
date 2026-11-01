"""IPv6 on an AWS placement (design/IPV6-PLAN.md §6.2, §11.10): shc_aws_tenant.py against a fake EC2 that records the
calls and checks every keyword against the EC2 API model botocore ships (boto3 is never called for real), and the
placement and teardown task files in ansible-playbook (skipped when it is absent).

Source Network: the account's IPAM (Free Tier, private GUA), a private-scope pool per placement holding exactly the
placement's block, the VPC's IPv6 block from the pool, a /64 per AZ for the workload and attachment subnets, the
attachment with IPv6 support, the network's IPv6 space and (internet) ::/0 to the transit gateway. Source Provider:
Amazon's /56, an egress-only internet gateway with ::/0 only with internet. Teardown in reverse.

Addresses: the IPAM refuses 2001:db8::/32 and fd00::/16 (both in the AWS site's reserved ranges), so the realized
blocks here are from the other documentation prefix, 3fff::/20 (RFC 9637), which it accepts; the fake Amazon /56 is
from it too."""
import contextlib
import copy
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
REAL = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]
CRDS = [os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-networkplacement.yaml"),
        os.path.join(ROOT, "operator", "primary", "helm", "templates", "crds.yaml")]

try:
    import botocore.exceptions
    import botocore.session
    from botocore import xform_name
    sys.path.insert(0, FILES)
    import shc_aws_tenant as T
    HAVE_BOTO = True
except ImportError:  # pragma: no cover - the runner image has boto3
    HAVE_BOTO = False

ACCOUNT = "111111111111"
REGION = "xx-test-1"
AZS = [REGION + "a", REGION + "b"]
NET6 = "3fff:0:4000::/48"           # the network's IPv6 space
BLOCK = "3fff:0:4000:10::/60"        # the placement's block from it (source Network)
AMAZON = "3fff:a:1:100::/56"         # what the fake hands out as Amazon's /56
V4 = ["10.60.4.0/24"]
TGW = "tgw-0test"
PLACEMENT = "acme/p1"
REFUSED = [ipaddress.ip_network("fd00::/16"), ipaddress.ip_network("2001:db8::/32")]

MUTATING = ("create_", "modify_", "associate_", "provision_", "replace_", "delete_", "disassociate_", "deprovision_")


def client_error(code, msg):
    return botocore.exceptions.ClientError({"Error": {"Code": code, "Message": msg}}, "op")


class FakeEc2:
    """Just enough EC2 for a placement. Every call is recorded and its keywords checked against the API model."""
    _model = None

    def __init__(self, amazon=AMAZON):
        if FakeEc2._model is None:
            m = botocore.session.get_session().get_service_model("ec2")
            FakeEc2._model = {xform_name(op): m.operation_model(op) for op in m.operation_names}
        self.calls = []
        self.n = 0
        self.amazon = [amazon, "3fff:a:1:200::/56"]
        self.ipams, self.pools, self.pool_cidrs, self.vpcs, self.subnets = {}, {}, {}, {}, {}
        self.atts, self.tables, self.eigws = {}, {}, {}

    def _id(self, kind):
        self.n += 1
        return "%s-%04d" % (kind, self.n)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name == "get_waiter":  # not an API operation; the fake's objects are ready at once
            return lambda waiter: mock.Mock()
        op = self._model.get(name)
        handler = getattr(self, "h_" + name, None)
        if op is None or handler is None:
            raise AssertionError("unexpected EC2 call " + name)

        def call(**kw):
            unknown = set(kw) - set(op.input_shape.members)
            if unknown:
                raise AssertionError("%s: not in the EC2 API: %s" % (name, sorted(unknown)))
            missing = set(op.input_shape.required_members) - set(kw)
            if missing:
                raise AssertionError("%s: missing %s" % (name, sorted(missing)))
            self.calls.append((name, copy.deepcopy(kw)))
            return handler(**kw)
        return call

    def mutations(self):
        return [(n, kw) for n, kw in self.calls if n.startswith(MUTATING)]

    def names(self):
        return [n for n, _ in self.mutations()]

    @staticmethod
    def _tags(kw):
        return [t for s in kw.get("TagSpecifications") or [] for t in s["Tags"]]

    # ---- the transit gateway is shared already
    def h_describe_transit_gateways(self, **kw):
        return {"TransitGateways": [{"TransitGatewayId": TGW}]}

    # ---- IPAM
    def h_create_ipam(self, **kw):
        i = self._id("ipam")
        self.ipams[i] = {"IpamId": i, "IpamArn": "arn:ipam/" + i, "IpamRegion": REGION, "State": "create-complete",
                         "PrivateDefaultScopeId": "ipam-scope-p" + i, "Tier": kw.get("Tier"),
                         "EnablePrivateGua": False, "Tags": self._tags(kw)}
        return {"Ipam": dict(self.ipams[i], State="create-in-progress")}

    def h_modify_ipam(self, IpamId, **kw):
        self.ipams[IpamId]["EnablePrivateGua"] = kw.get("EnablePrivateGua", False)
        return {"Ipam": self.ipams[IpamId]}

    def h_describe_ipams(self, IpamIds=None, **kw):
        return {"Ipams": [copy.deepcopy(i) for k, i in self.ipams.items() if not IpamIds or k in IpamIds]}

    def h_delete_ipam(self, IpamId, **kw):
        if any(p["IpamArn"] == self.ipams[IpamId]["IpamArn"] for p in self.pools.values()) and not kw.get("Cascade"):
            raise client_error("IncorrectState", "the IPAM has pools")
        del self.ipams[IpamId]
        return {}

    def h_create_ipam_pool(self, IpamScopeId, AddressFamily, **kw):
        ipam = next(i for i in self.ipams.values() if i["PrivateDefaultScopeId"] == IpamScopeId)
        p = self._id("ipam-pool")
        self.pools[p] = {"IpamPoolId": p, "IpamArn": ipam["IpamArn"], "IpamScopeId": IpamScopeId,
                         "AddressFamily": AddressFamily, "Locale": kw.get("Locale"), "State": "create-complete",
                         "Tags": self._tags(kw)}
        self.pool_cidrs[p] = []
        return {"IpamPool": dict(self.pools[p], State="create-in-progress")}

    def h_describe_ipam_pools(self, IpamPoolIds=None, **kw):
        return {"IpamPools": [copy.deepcopy(p) for k, p in self.pools.items() if not IpamPoolIds or k in IpamPoolIds]}

    def h_provision_ipam_pool_cidr(self, IpamPoolId, Cidr, **kw):
        if not self.ipams[next(i for i, x in self.ipams.items()
                               if x["IpamArn"] == self.pools[IpamPoolId]["IpamArn"])]["EnablePrivateGua"]:
            raise client_error("InvalidParameterValue", "private GUA is not enabled")
        n = ipaddress.ip_network(Cidr)
        refused = any(n.subnet_of(r) for r in REFUSED)
        self.pool_cidrs[IpamPoolId].append({"Cidr": Cidr, "State": "pending-provision", "_refused": refused})
        return {"IpamPoolCidr": {"Cidr": Cidr, "State": "pending-provision"}}

    def h_get_ipam_pool_cidrs(self, IpamPoolId, **kw):
        out = []
        for c in self.pool_cidrs[IpamPoolId]:
            row = {"Cidr": c["Cidr"], "State": c["State"]}
            if c["State"] == "failed-provision":
                row["FailureReason"] = {"Code": "cidr-not-available",
                                        "Message": "%s is not valid for use in pool" % c["Cidr"]}
            out.append(row)
            # the next look finds it provisioned (or refused)
            if c["State"] == "pending-provision":
                c["State"] = "failed-provision" if c["_refused"] else "provisioned"
        return {"IpamPoolCidrs": out}

    def h_delete_ipam_pool(self, IpamPoolId, **kw):
        if not kw.get("Cascade") and any(v["IpamPoolId"] == IpamPoolId for v in self.vpcs.values() if "IpamPoolId" in v):
            raise client_error("IncorrectState", "The CIDR has one or more allocations")
        del self.pools[IpamPoolId]
        return {}

    # ---- VPC
    def h_create_vpc(self, CidrBlock, **kw):
        v = self._id("vpc")
        self.vpcs[v] = {"VpcId": v, "CidrBlock": CidrBlock, "Tags": self._tags(kw),
                        "CidrBlockAssociationSet": [{"CidrBlock": CidrBlock, "CidrBlockState": {"State": "associated"}}],
                        "Ipv6CidrBlockAssociationSet": []}
        self.tables[v] = {"RouteTableId": "rtb-" + v, "Routes": [
            {"DestinationCidrBlock": CidrBlock, "GatewayId": "local", "Origin": "CreateRouteTable"}]}
        if kw.get("Ipv6IpamPoolId") or kw.get("AmazonProvidedIpv6CidrBlock"):
            self._v6(v, kw)
        return {"Vpc": copy.deepcopy(self.vpcs[v])}

    def _v6(self, v, kw):
        if kw.get("Ipv6IpamPoolId"):
            pool = kw["Ipv6IpamPoolId"]
            free = [c["Cidr"] for c in self.pool_cidrs[pool] if c["State"] == "provisioned"
                    and ipaddress.ip_network(c["Cidr"]).prefixlen == kw["Ipv6NetmaskLength"]]
            if not free:
                raise client_error("InvalidParameterValue", "no /%d in pool %s" % (kw["Ipv6NetmaskLength"], pool))
            cidr, src = free[0], "IPAM Managed"
            self.vpcs[v]["IpamPoolId"] = pool
        else:
            cidr, src = self.amazon.pop(0), "Amazon"
        self.vpcs[v]["Ipv6CidrBlockAssociationSet"].append(
            {"AssociationId": "vpc-cidr-assoc-" + v, "Ipv6CidrBlock": cidr, "Ipv6Pool": src,
             "Ipv6CidrBlockState": {"State": "associated"}})
        self.tables[v]["Routes"].append({"DestinationIpv6CidrBlock": cidr, "GatewayId": "local",
                                         "Origin": "CreateRouteTable"})

    def h_associate_vpc_cidr_block(self, VpcId, **kw):
        if kw.get("CidrBlock"):
            self.vpcs[VpcId]["CidrBlockAssociationSet"].append({"CidrBlock": kw["CidrBlock"],
                                                                "CidrBlockState": {"State": "associated"}})
        else:
            self._v6(VpcId, kw)
        return {}

    def h_disassociate_vpc_cidr_block(self, AssociationId, **kw):
        for v in self.vpcs.values():
            v["Ipv6CidrBlockAssociationSet"] = [a for a in v["Ipv6CidrBlockAssociationSet"]
                                                if a["AssociationId"] != AssociationId]
            v.pop("IpamPoolId", None)
        return {}

    def h_modify_vpc_attribute(self, VpcId, **kw):
        return {}

    def h_describe_vpcs(self, VpcIds=None, Filters=None, **kw):
        out = list(self.vpcs.values())
        if VpcIds:
            out = [v for v in out if v["VpcId"] in VpcIds]
        for f in Filters or []:
            key = f["Name"].split(":", 1)[1]
            out = [v for v in out if T.tagmap(v).get(key) in f["Values"]]
        return {"Vpcs": copy.deepcopy(out)}

    def h_delete_vpc(self, VpcId, **kw):
        if any(s["VpcId"] == VpcId for s in self.subnets.values()):
            raise client_error("DependencyViolation", "subnets left")
        del self.vpcs[VpcId]
        del self.tables[VpcId]
        return {}

    # ---- subnets
    def h_create_subnet(self, VpcId, CidrBlock, **kw):
        s = self._id("subnet")
        row = {"SubnetId": s, "VpcId": VpcId, "CidrBlock": CidrBlock, "AvailabilityZone": kw.get("AvailabilityZone"),
               "Tags": self._tags(kw), "AssignIpv6AddressOnCreation": False, "Ipv6CidrBlockAssociationSet": []}
        self.subnets[s] = row
        if kw.get("Ipv6CidrBlock"):
            self._sub6(s, kw["Ipv6CidrBlock"])
        return {"Subnet": copy.deepcopy(row)}

    def _sub6(self, s, cidr):
        vpc = self.vpcs[self.subnets[s]["VpcId"]]
        n = ipaddress.ip_network(cidr)
        if n.prefixlen != 64 or not any(n.subnet_of(ipaddress.ip_network(a["Ipv6CidrBlock"]))
                                        for a in vpc["Ipv6CidrBlockAssociationSet"]):
            raise client_error("InvalidSubnet.Range", "%s is not a /64 of the VPC's IPv6 block" % cidr)
        self.subnets[s]["Ipv6CidrBlockAssociationSet"].append(
            {"Ipv6CidrBlock": cidr, "Ipv6CidrBlockState": {"State": "associated"}})

    def h_associate_subnet_cidr_block(self, SubnetId, Ipv6CidrBlock, **kw):
        self._sub6(SubnetId, Ipv6CidrBlock)
        return {}

    def h_modify_subnet_attribute(self, SubnetId, **kw):
        if "AssignIpv6AddressOnCreation" in kw:
            self.subnets[SubnetId]["AssignIpv6AddressOnCreation"] = kw["AssignIpv6AddressOnCreation"]["Value"]
        return {}

    def h_describe_subnets(self, Filters=None, **kw):
        vpc = next(f["Values"][0] for f in Filters if f["Name"] == "vpc-id")
        return {"Subnets": [copy.deepcopy(s) for s in self.subnets.values() if s["VpcId"] == vpc]}

    def h_delete_subnet(self, SubnetId, **kw):
        del self.subnets[SubnetId]
        return {}

    # ---- the attachment
    def h_create_transit_gateway_vpc_attachment(self, TransitGatewayId, VpcId, SubnetIds, **kw):
        opts = kw.get("Options") or {}
        if opts.get("Ipv6Support") == "enable" and not all(self.subnets[s]["Ipv6CidrBlockAssociationSet"] for s in SubnetIds):
            raise client_error("InvalidParameterValue", "IPv6 support needs IPv6 on every attachment subnet")
        if not all(self.subnets[s]["CidrBlock"] for s in SubnetIds):
            raise client_error("InsufficientFreeAddressesInSubnet", "the attachment needs IPv4")
        a = self._id("tgw-attach")
        self.atts[a] = {"TransitGatewayAttachmentId": a, "VpcId": VpcId, "State": "available", "SubnetIds": SubnetIds,
                        "Options": {"Ipv6Support": opts.get("Ipv6Support", "disable")}}
        return {"TransitGatewayVpcAttachment": copy.deepcopy(self.atts[a])}

    def h_modify_transit_gateway_vpc_attachment(self, TransitGatewayAttachmentId, **kw):
        self.atts[TransitGatewayAttachmentId]["Options"].update(kw.get("Options") or {})
        return {}

    def h_describe_transit_gateway_vpc_attachments(self, Filters=None, TransitGatewayAttachmentIds=None, **kw):
        out = list(self.atts.values())
        if TransitGatewayAttachmentIds:
            out = [a for a in out if a["TransitGatewayAttachmentId"] in TransitGatewayAttachmentIds]
        for f in Filters or []:
            out = [a for a in out if a["VpcId"] in f["Values"]]
        return {"TransitGatewayVpcAttachments": copy.deepcopy(out)}

    def h_delete_transit_gateway_vpc_attachment(self, TransitGatewayAttachmentId, **kw):
        self.atts[TransitGatewayAttachmentId]["State"] = "deleted"
        return {}

    # ---- routes
    def h_describe_route_tables(self, Filters=None, RouteTableIds=None, **kw):
        f = {x["Name"]: x["Values"] for x in Filters or []}
        if "tag:Name" in f:
            return {"RouteTables": []}
        return {"RouteTables": [copy.deepcopy(self.tables[f["vpc-id"][0]])]}

    def _table(self, rt):
        return next(t for t in self.tables.values() if t["RouteTableId"] == rt)

    def h_create_route(self, RouteTableId, **kw):
        t = self._table(RouteTableId)
        dest = kw.get("DestinationIpv6CidrBlock") or kw.get("DestinationCidrBlock")
        key = "DestinationIpv6CidrBlock" if "DestinationIpv6CidrBlock" in kw else "DestinationCidrBlock"
        if any(r.get(key) == dest for r in t["Routes"]):
            raise client_error("RouteAlreadyExists", dest)
        if kw.get("EgressOnlyInternetGatewayId") and kw["EgressOnlyInternetGatewayId"] not in self.eigws:
            raise client_error("InvalidGatewayID.NotFound", kw["EgressOnlyInternetGatewayId"])
        t["Routes"].append(dict({k: v for k, v in kw.items() if k != "RouteTableId"}, Origin="CreateRoute"))
        return {"Return": True}

    def h_replace_route(self, RouteTableId, DestinationIpv6CidrBlock, **kw):
        t = self._table(RouteTableId)
        t["Routes"] = [r for r in t["Routes"] if r.get("DestinationIpv6CidrBlock") != DestinationIpv6CidrBlock]
        t["Routes"].append(dict(kw, DestinationIpv6CidrBlock=DestinationIpv6CidrBlock, Origin="CreateRoute"))
        return {}

    def h_delete_route(self, RouteTableId, DestinationIpv6CidrBlock=None, **kw):
        t = self._table(RouteTableId)
        t["Routes"] = [r for r in t["Routes"] if r.get("DestinationIpv6CidrBlock") != DestinationIpv6CidrBlock]
        return {}

    # ---- the egress-only internet gateway
    def h_create_egress_only_internet_gateway(self, VpcId, **kw):
        g = self._id("eigw")
        self.eigws[g] = {"EgressOnlyInternetGatewayId": g, "Attachments": [{"VpcId": VpcId, "State": "attached"}],
                         "Tags": self._tags(kw)}
        return {"EgressOnlyInternetGateway": copy.deepcopy(self.eigws[g])}

    def h_describe_egress_only_internet_gateways(self, **kw):
        return {"EgressOnlyInternetGateways": copy.deepcopy(list(self.eigws.values()))}

    def h_delete_egress_only_internet_gateway(self, EgressOnlyInternetGatewayId, **kw):
        for t in self.tables.values():
            if any(r.get("EgressOnlyInternetGatewayId") == EgressOnlyInternetGatewayId for r in t["Routes"]):
                raise client_error("DependencyViolation", "a route uses the gateway")
        del self.eigws[EgressOnlyInternetGatewayId]
        return {"ReturnCode": True}

    # ---- teardown reads
    def h_describe_instances(self, **kw):
        return {"Reservations": []}

    def h_describe_internet_gateways(self, **kw):
        return {"InternetGateways": []}

    def h_describe_security_groups(self, **kw):
        return {"SecurityGroups": [{"GroupId": "sg-default", "GroupName": "default"}]}


class FakeEdge:
    """The hub side (shc_aws_edge.Edge): the network's route table and Connect attachment exist."""
    instances = []

    def __init__(self, cfg):
        self.changed = []
        self.calls = []
        FakeEdge.instances.append(self)

    def tgw(self):
        return {"TransitGatewayId": TGW}

    def net_up(self, vni, egress=False):
        self.calls.append(("net_up", vni))
        return {"routeTable": "tgw-rtb-net", "connectAttachment": "tgw-attach-connect", "egress": None}

    def net_attach(self, vni, att):
        self.calls.append(("net_attach", vni, att))
        return {}

    def attachments(self, tgw_id, **f):
        return []

    def net_down(self, vni):
        self.calls.append(("net_down", vni))


def tenant(ec2):
    t = T.Tenant.__new__(T.Tenant)
    t.cfg = {"account": ACCOUNT, "region": REGION, "azs": list(AZS), "tags": {"shc-entity": "acme", "shc-network": "core"},
             "edge": {}}
    t.ec2, t.r53, t.ram = ec2, None, None
    t.changed = []
    return t


def ipv6(source="Network", internet=True, prefix=BLOCK, net6=(NET6,)):
    d = {"source": source, "internet": internet, "networkPrefixes6": list(net6)}
    if source == "Network":
        d["prefix"] = prefix
    return d


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class AwsTenantIpv6Test(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(T.time, "sleep", lambda s: None),
                   mock.patch.object(T.shc_aws_edge, "Edge", FakeEdge)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ec2 = FakeEc2()

    def up(self, v6, placement=PLACEMENT, egress=False):
        t = tenant(self.ec2)
        r = t.place_up(placement, 1001, list(V4), egress, v6)
        return r, t.changed

    def down(self, v6, placement=PLACEMENT):
        t = tenant(self.ec2)
        return t.place_down(placement, 1001, v6), t.changed

    def refused(self, fn):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            fn()
        return json.loads(out.getvalue().splitlines()[-1])

    def routes6(self, vpc):
        return {r["DestinationIpv6CidrBlock"]: {k: v for k, v in r.items() if k.endswith("Id")}
                for r in self.ec2.tables[vpc]["Routes"]
                if r.get("DestinationIpv6CidrBlock") and r["Origin"] == "CreateRoute"}

    # ---- the layout
    def test_layout6_work_from_the_bottom_attachment_from_the_top(self):
        self.assertEqual(T.layout6(BLOCK, AZS), [
            {"az": AZS[0], "work": "3fff:0:4000:10::/64", "tgw": "3fff:0:4000:1e::/64"},
            {"az": AZS[1], "work": "3fff:0:4000:11::/64", "tgw": "3fff:0:4000:1f::/64"}])
        three = T.layout6("3fff:0:4000:100::/56", AZS + [REGION + "c"])
        self.assertEqual([x["work"] for x in three],
                         ["3fff:0:4000:100::/64", "3fff:0:4000:101::/64", "3fff:0:4000:102::/64"])
        self.assertEqual([x["tgw"] for x in three],
                         ["3fff:0:4000:1fd::/64", "3fff:0:4000:1fe::/64", "3fff:0:4000:1ff::/64"])
        self.assertIn("/64s", self.refused(lambda: T.layout6("3fff:0:4000:10::/63", AZS + [REGION + "c"]))["error"])

    def test_request_shape_checked(self):
        self.assertIsNone(T.ipv6_want(None))
        self.assertIsNone(T.ipv6_want({}))
        self.assertIn("/44 to /60", self.refused(lambda: T.ipv6_want(ipv6(prefix="3fff:0:4000:10::/64")))["error"])
        self.assertIn("needs the placement's block", self.refused(lambda: T.ipv6_want({"source": "Network"}))["error"])
        self.assertIn("Network or Provider", self.refused(lambda: T.ipv6_want({"source": "Byoip"}))["error"])
        # Provider takes no prefix; the network's space comes back canonical
        self.assertEqual(T.ipv6_want({"source": "Provider", "networkPrefixes6": ["3FFF:0:4000::/48"]}),
                         {"source": "Provider", "internet": False, "networkPrefixes6": [NET6]})

    def test_an_ipv6_only_request_is_refused(self):
        t = tenant(self.ec2)
        out = self.refused(lambda: t.place_up(PLACEMENT, 1001, [], False, ipv6()))
        self.assertIn("needs an IPv4 prefix", out["error"])
        self.assertEqual(self.ec2.calls, [])

    # ---- source Network
    def test_network_source_with_internet(self):
        r, changed = self.up(ipv6())
        vpc = r["vpcId"]
        ipam = next(iter(self.ec2.ipams.values()))
        pool = next(iter(self.ec2.pools.values()))
        names = self.ec2.names()
        # the recorded sequence of changes
        self.assertEqual(names, [
            "create_ipam", "modify_ipam", "create_ipam_pool", "provision_ipam_pool_cidr", "create_vpc",
            "modify_vpc_attribute",
            "create_subnet", "modify_subnet_attribute", "create_subnet", "create_subnet", "modify_subnet_attribute",
            "create_subnet", "create_transit_gateway_vpc_attachment", "create_route", "create_route", "create_route"])
        calls = dict((n, kw) for n, kw in reversed(self.ec2.mutations()))
        self.assertEqual(calls["create_ipam"]["Tier"], "free")
        self.assertEqual(calls["create_ipam"]["OperatingRegions"], [{"RegionName": REGION}])
        self.assertEqual(calls["modify_ipam"], {"IpamId": ipam["IpamId"], "EnablePrivateGua": True})
        self.assertEqual((calls["create_ipam_pool"]["IpamScopeId"], calls["create_ipam_pool"]["AddressFamily"],
                          calls["create_ipam_pool"]["Locale"], calls["create_ipam_pool"]["AutoImport"]),
                         (ipam["PrivateDefaultScopeId"], "ipv6", REGION, False))
        self.assertEqual(calls["provision_ipam_pool_cidr"], {"IpamPoolId": pool["IpamPoolId"], "Cidr": BLOCK})
        self.assertEqual((calls["create_vpc"]["Ipv6IpamPoolId"], calls["create_vpc"]["Ipv6NetmaskLength"]),
                         (pool["IpamPoolId"], 60))
        self.assertNotIn("AmazonProvidedIpv6CidrBlock", calls["create_vpc"])
        self.assertEqual(calls["create_transit_gateway_vpc_attachment"]["Options"], {"Ipv6Support": "enable"})
        # the pool and the IPAM carry the platform's tags; the pool is the placement's
        self.assertEqual(T.tagmap(pool)["shc-placement"], PLACEMENT)
        self.assertEqual(T.tagmap(ipam)["shc-role"], "tenant-ipam")
        self.assertEqual(T.tagmap(ipam)["shc-managed"], "true")
        self.assertNotIn("shc-network", T.tagmap(ipam))          # one IPAM per account and region, not per network
        # subnets: IPv4 as before, a /64 each
        self.assertEqual([(s["cidr"], s["ipv6"]) for s in r["subnets"]],
                         [("10.60.4.0/26", "3fff:0:4000:10::/64"), ("10.60.4.64/26", "3fff:0:4000:11::/64")])
        self.assertEqual([(s["cidr"], s["ipv6"]) for s in r["attachmentSubnets"]],
                         [("10.60.4.224/28", "3fff:0:4000:1e::/64"), ("10.60.4.240/28", "3fff:0:4000:1f::/64")])
        work = [s for s in self.ec2.subnets.values() if T.tagmap(s)["shc-role"] == "work"]
        self.assertTrue(all(s["AssignIpv6AddressOnCreation"] for s in work))
        # routes: the network's space and ::/0 to the transit gateway; no egress-only gateway
        self.assertEqual(self.routes6(vpc), {NET6: {"TransitGatewayId": TGW}, "::/0": {"TransitGatewayId": TGW}})
        self.assertEqual(self.ec2.eigws, {})
        self.assertEqual(r["ipv6"], {"source": "Network", "prefix": BLOCK, "internet": True, "path": "Fabric",
                                     "pool": pool["IpamPoolId"], "ipam": ipam["IpamId"], "egressOnlyGateway": None,
                                     "routes": ["3fff:0:4000::/48", "::/0"], "subnets": T.layout6(BLOCK, AZS)})
        self.assertIn("ipam pool cidr " + BLOCK, changed)

    def test_network_source_without_internet(self):
        r, _ = self.up(ipv6(internet=False))
        self.assertEqual(self.routes6(r["vpcId"]), {NET6: {"TransitGatewayId": TGW}})
        self.assertFalse(r["ipv6"]["internet"])

    def test_network_space_equal_to_the_block_gets_no_route(self):
        # the VPC's own block is local: a route for it (or inside it) would be refused
        r, _ = self.up(ipv6(net6=(BLOCK, "3fff:0:5000::/48"), internet=False))
        self.assertEqual(self.routes6(r["vpcId"]), {"3fff:0:5000::/48": {"TransitGatewayId": TGW}})

    def test_rerun_keeps_vpc_block_and_routes(self):
        r1, _ = self.up(ipv6())
        before = len(self.ec2.calls)
        r2, changed = self.up(ipv6())
        self.assertEqual(changed, [])
        self.assertEqual([n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)], [])
        self.assertEqual((r2["vpcId"], r2["ipv6"]["prefix"], r2["ipv6"]["pool"]),
                         (r1["vpcId"], r1["ipv6"]["prefix"], r1["ipv6"]["pool"]))
        self.assertEqual(len(self.ec2.ipams), 1)
        self.assertEqual(len(self.ec2.pools), 1)

    def test_internet_turned_off_removes_the_default_route(self):
        r, _ = self.up(ipv6())
        _, changed = self.up(ipv6(internet=False))
        self.assertEqual(changed, ["deleted route ::/0"])
        self.assertEqual(self.routes6(r["vpcId"]), {NET6: {"TransitGatewayId": TGW}})

    def test_second_placement_shares_the_ipam(self):
        self.up(ipv6())
        self.up(ipv6(prefix="3fff:0:4000:20::/60"), placement="acme/p2")
        self.assertEqual(len(self.ec2.ipams), 1)
        self.assertEqual(sorted(T.tagmap(p)["shc-placement"] for p in self.ec2.pools.values()), ["acme/p1", "acme/p2"])
        self.assertEqual(self.ec2.names().count("create_ipam"), 1)

    def test_the_ipam_refuses_a_reserved_block(self):
        out = self.refused(lambda: self.up(ipv6(prefix="fd00:5::/60", net6=("fd00:5::/48",))))
        self.assertEqual(out["reason"], "PrefixInvalid")
        self.assertIn("not valid for use in pool", out["error"])
        self.assertEqual(self.ec2.vpcs, {})                    # nothing built on a refused block

    def test_an_existing_attachment_gets_ipv6_support(self):
        r, _ = self.up(None)                                    # an IPv4 placement
        self.assertIsNone(r["ipv6"])
        self.assertEqual([n for n in self.ec2.names() if "ipam" in n], [])
        att = r["attachment"]
        self.assertEqual(self.ec2.atts[att]["Options"]["Ipv6Support"], "disable")
        _, changed = self.up(ipv6(source="Provider", internet=False))
        self.assertEqual(self.ec2.atts[att]["Options"]["Ipv6Support"], "enable")
        self.assertIn("tgw attachment %s ipv6" % att, changed)
        self.assertTrue(all(s["Ipv6CidrBlockAssociationSet"] for s in self.ec2.subnets.values()))

    # ---- source Provider
    def test_provider_source_with_internet(self):
        r, _ = self.up(ipv6(source="Provider"))
        vpc = r["vpcId"]
        self.assertEqual(self.ec2.names(), [
            "create_vpc", "modify_vpc_attribute",
            "create_subnet", "modify_subnet_attribute", "create_subnet", "create_subnet", "modify_subnet_attribute",
            "create_subnet", "create_transit_gateway_vpc_attachment", "create_route",
            "create_egress_only_internet_gateway", "create_route", "create_route"])
        create_vpc = next(kw for n, kw in self.ec2.calls if n == "create_vpc")
        self.assertTrue(create_vpc["AmazonProvidedIpv6CidrBlock"])
        self.assertNotIn("Ipv6IpamPoolId", create_vpc)
        eigw = next(iter(self.ec2.eigws))
        self.assertEqual(self.routes6(vpc), {NET6: {"TransitGatewayId": TGW}, "::/0": {"EgressOnlyInternetGatewayId": eigw}})
        self.assertEqual((r["ipv6"]["source"], r["ipv6"]["prefix"], r["ipv6"]["path"], r["ipv6"]["egressOnlyGateway"],
                          r["ipv6"]["pool"], r["ipv6"]["ipam"]), ("Provider", AMAZON, "Provider", eigw, None, None))
        self.assertEqual([s["ipv6"] for s in r["subnets"]], ["3fff:a:1:100::/64", "3fff:a:1:101::/64"])
        self.assertEqual([s["ipv6"] for s in r["attachmentSubnets"]], ["3fff:a:1:1fe::/64", "3fff:a:1:1ff::/64"])
        self.assertEqual(self.ec2.ipams, {})

    def test_provider_source_without_internet(self):
        r, _ = self.up(ipv6(source="Provider", internet=False))
        self.assertEqual(self.routes6(r["vpcId"]), {NET6: {"TransitGatewayId": TGW}})
        self.assertEqual(self.ec2.eigws, {})
        self.assertIsNone(r["ipv6"]["egressOnlyGateway"])

    def test_provider_internet_follows_egress(self):
        r, _ = self.up(ipv6(source="Provider", internet=False))
        _, changed = self.up(ipv6(source="Provider"))
        self.assertEqual(len(self.ec2.eigws), 1)
        self.assertIn("route ::/0", changed)
        before = len(self.ec2.calls)
        r2, changed = self.up(ipv6(source="Provider"))
        self.assertEqual(changed, [])                          # the re-run keeps the /56 and the gateway
        self.assertEqual(r2["ipv6"]["prefix"], r["ipv6"]["prefix"])
        self.assertEqual([n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)], [])
        _, changed = self.up(ipv6(source="Provider", internet=False))
        self.assertEqual(changed[:1], ["deleted route ::/0"])
        self.assertEqual(self.ec2.eigws, {})
        self.assertEqual(self.ec2.names()[-2:], ["delete_route", "delete_egress_only_internet_gateway"])

    # ---- teardown
    def teardown_order(self, names):
        wanted = ["delete_route", "delete_egress_only_internet_gateway", "delete_transit_gateway_vpc_attachment",
                  "delete_subnet", "disassociate_vpc_cidr_block", "delete_vpc", "delete_ipam_pool", "delete_ipam"]
        seen = [n for n in wanted if n in names]
        self.assertEqual(seen, sorted(seen, key=lambda n: max(i for i, x in enumerate(names) if x == n)))
        # every one of a kind before the next kind starts
        for a, b in zip(seen, seen[1:]):
            self.assertLess(max(i for i, x in enumerate(names) if x == a), min(i for i, x in enumerate(names) if x == b),
                            (a, b))
        return seen

    def test_teardown_network_source_in_reverse(self):
        self.up(ipv6())
        before = len(self.ec2.calls)
        r, _ = self.down({"source": "Network", "prefix": BLOCK})
        names = [n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)]
        self.assertEqual(self.teardown_order(names), [
            "delete_route", "delete_transit_gateway_vpc_attachment", "delete_subnet", "disassociate_vpc_cidr_block",
            "delete_vpc", "delete_ipam_pool", "delete_ipam"])
        self.assertEqual(names.count("delete_route"), 2)       # the network's space and ::/0; 0.0.0.0/0 goes with the VPC
        self.assertTrue(next(kw for n, kw in self.ec2.calls if n == "delete_ipam_pool")["Cascade"])
        self.assertEqual((self.ec2.vpcs, self.ec2.subnets, self.ec2.pools, self.ec2.ipams), ({}, {}, {}, {}))
        self.assertEqual(r["ipv6"], {"pool": r["ipv6"]["pool"], "ipamDeleted": True})
        self.assertFalse(r["networkKept"])

    def test_teardown_provider_source(self):
        self.up(ipv6(source="Provider"))
        before = len(self.ec2.calls)
        r, _ = self.down({"source": "Provider"})
        names = [n for n, _ in self.ec2.calls[before:] if n.startswith(MUTATING)]
        self.assertEqual(self.teardown_order(names), [
            "delete_route", "delete_egress_only_internet_gateway", "delete_transit_gateway_vpc_attachment",
            "delete_subnet", "disassociate_vpc_cidr_block", "delete_vpc"])
        self.assertEqual(self.ec2.eigws, {})
        self.assertEqual(r["ipv6"], {"pool": None, "ipamDeleted": False})

    def test_teardown_keeps_the_ipam_while_another_pool_is_in_it(self):
        self.up(ipv6())
        self.up(ipv6(prefix="3fff:0:4000:20::/60"), placement="acme/p2")
        r, _ = self.down({"source": "Network"})
        self.assertFalse(r["ipv6"]["ipamDeleted"])
        self.assertEqual(len(self.ec2.ipams), 1)
        self.assertEqual([T.tagmap(p)["shc-placement"] for p in self.ec2.pools.values()], ["acme/p2"])
        r, _ = self.down({"source": "Network"}, placement="acme/p2")
        self.assertTrue(r["ipv6"]["ipamDeleted"])
        self.assertEqual(self.ec2.ipams, {})

    def test_teardown_finds_a_pool_without_a_vpc(self):
        # a run that stopped after the pool: the teardown with spec.ipv6 still removes it
        t = tenant(self.ec2)
        ipam = t.ensure_ipam()
        t.ensure_pool(ipam, PLACEMENT, BLOCK, "shc-acme-p1", {"shc-placement": PLACEMENT})
        r, _ = self.down({"source": "Network"})
        self.assertIsNotNone(r["ipv6"]["pool"])
        self.assertEqual((self.ec2.pools, self.ec2.ipams), ({}, {}))

    def test_teardown_of_an_ipv4_placement_reads_no_ipam(self):
        self.up(None)
        before = len(self.ec2.calls)
        r, _ = self.down(None)
        self.assertIsNone(r["ipv6"])
        self.assertEqual([n for n, _ in self.ec2.calls[before:] if "ipam" in n or "egress_only" in n], [])

    # ---- the command line
    def test_cli_passes_ipv6(self):
        seen = {}

        class Stub:
            check = share_down = None

            def __init__(self, cfg):
                self.changed = []

            def place_up(self, *a):
                seen["up"] = a
                return {}

            def place_down(self, *a):
                seen["down"] = a
                return {}
        argv = ["x", "--config", "{}", "place-up", "--placement", PLACEMENT, "--vni", "7", "--prefix", V4[0],
                "--ipv6", json.dumps(ipv6())]
        with mock.patch.object(T, "Tenant", Stub), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            T.main()
            sys.argv[3] = "place-down"
            T.main()
        self.assertEqual(seen["up"], (PLACEMENT, 7, V4, False, ipv6()))
        self.assertEqual(seen["down"], (PLACEMENT, 7, ipv6()))


# ---- the task files ---------------------------------------------------------------------------------------------

def merged(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


SITE = "aws-test"
CAPS = {"ipv6": True, "ipv6Only": True, "ipv6NetworkSource": True, "ipv6ProviderSource": True, "ipv6Translation": False}
NET_P = {"spec": {"prefixes": ["10.60.0.0/16"], "platformPrefixes": [{"family": 6}], "egress": {"internet": True}},
         "status": {"ipv6": {"mode": "Platform", "prefixes": [NET6]}}}
NET_T = {"spec": {"prefixes": ["10.60.0.0/16", "fd5c:8::/48"]}, "status": {"ipv6": {"mode": "Tenant", "prefixes": ["fd5c:8::/48"]}}}


def task_vars(task):
    """A task's vars evaluated into one fact (an include's vars can't be read back otherwise)."""
    return {"vars": task["vars"], "ansible.builtin.set_fact": {"_out": {k: "{{ %s }}" % k for k in task["vars"]
                                                                          if not k.startswith("_")}}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AwsPlacementTasksTest(unittest.TestCase):
    def families(self, spec, network=NET_P, real=None, caps=CAPS):
        tasks = load(os.path.join(NP, "families.yml")) + [write_out("np_refuse")]
        on = merged({"placement": {"ipFamiliesBySite": {SITE: [4, 6]}, "ipv6OnlyBySite": {SITE: True}}}, real)
        return run_play(tasks, {"fab_spec": spec, "fab_name": "p1", "np_backend_kind": "CloudAWS", "np_site": SITE,
                                "np_network": network, "ci_capabilities": caps, "fab_real": merged(REAL, on)})

    def test_ipv6_only_refused_on_aws_whatever_the_switches(self):
        out = self.families({"ipv6": {"prefixLength": 60}})
        self.assertEqual(out, {"reason": "FamilyUnavailable",
                               "msg": "IPv6-only placements are not offered on this site (%s)" % SITE})
        self.assertEqual(self.families({"ipv4": {"prefixLength": 24}, "ipv6": {}}), {})

    def test_provider_on_tenant_prefixes(self):
        out = self.families({"ipv4": {"prefixLength": 24}, "ipv6": {"source": "Provider"}}, network=NET_T)
        self.assertEqual(out["reason"], "PrefixInvalid")

    def refuse_msg(self, kind, invalid):
        task = by_name(load(os.path.join(NP, "main.yml")), "Refuse a pinned prefix the network can't give")
        return run_play([task, write_out("{'reason': fab_fail_reason, 'msg': fab_fail_msg}")],
                        {"fab_name": "p1", "np_backend_kind": kind, "fab_real": REAL,
                         "fab_ipam_result": {"invalid": invalid}})

    def test_tenant_prefix_inside_the_clouds_range(self):
        sys.path.insert(0, FILES)
        import shc_ipam
        state = {"prefixes": ["10.60.0.0/16", "fd00:5::/48"], "alloc": {}}
        reserved = REAL["placement"]["siteReserved"]["CloudAWS"]
        sized = shc_ipam.plan_allocation(state, "o", [{"family": 6, "prefixLength": 60}], reserved)["invalid"]
        pinned = shc_ipam.plan_allocation(state, "o", [{"family": 6, "prefix": "fd00:5:0:10::/60"}], reserved)["invalid"]
        for invalid in (sized, pinned):
            out = self.refuse_msg("CloudAWS", invalid)
            self.assertEqual(out["reason"], "PrefixInvalid")
            self.assertTrue(out["msg"].startswith("NetworkPlacement p1: "), out)
            self.assertTrue(out["msg"].endswith("fd00::/16: the cloud reserves this range"), out)
        # elsewhere the message stays the IPAM's
        out = self.refuse_msg("CloudVirt", "fd97::/64 overlaps the site's own range fd97::/64")
        self.assertEqual(out["msg"], "NetworkPlacement p1: fd97::/64 overlaps the site's own range fd97::/64")
        out = self.refuse_msg("CloudAWS", "3fff:1::/60 lies outside the network's IPv6 address space (3fff::/48)")
        self.assertNotIn("reserves", out["msg"])

    def ipv6_here(self, want6, network=NET_P, mode="Platform", subnets=None):
        task = by_name(load(os.path.join(NP, "backend_cloudaws.yml")), "The placement's IPv6 here")
        want = [{"family": 4, "prefixLength": 24}] + ([want6] if want6 else [])
        rows = subnets if subnets is not None else [{"family": 4, "prefix": "10.60.4.0/24", "source": "Network"},
                                                    {"family": 6, "prefix": BLOCK, "source": "Network"}]
        return run_play([task, write_out("np_aws_ipv6")],
                        {"np_want": want, "np_subnets": rows, "np_network": network, "np_ipv6_mode": mode})

    def test_the_ipv6_request(self):
        self.assertEqual(self.ipv6_here({"family": 6, "source": "Network", "prefixLength": 60}),
                         {"source": "Network", "prefix": BLOCK, "internet": True, "networkPrefixes6": [NET6]})
        off = merged(NET_P, {"spec": {"egress": {"internet": False}}})
        self.assertFalse(self.ipv6_here({"family": 6, "source": "Network", "prefixLength": 60}, network=off)["internet"])
        # tenant prefixes: carried by the fabric, never to the internet
        out = self.ipv6_here({"family": 6, "source": "Network", "prefixLength": 60},
                             network=merged(NET_T, {"spec": {"egress": {"internet": True}}}), mode="Tenant",
                             subnets=[{"family": 6, "prefix": "fd5c:8:0:10::/60", "source": "Network"}])
        self.assertEqual(out, {"source": "Network", "prefix": "fd5c:8:0:10::/60", "internet": False,
                               "networkPrefixes6": ["fd5c:8::/48"]})
        # Provider: no block of the network's; the first run has no provider row yet, a later one has it
        self.assertEqual(self.ipv6_here({"family": 6, "source": "Provider"},
                                        subnets=[{"family": 4, "prefix": "10.60.4.0/24", "source": "Network"}]),
                         {"source": "Provider", "internet": True, "networkPrefixes6": [NET6]})
        self.assertEqual(self.ipv6_here({"family": 6, "source": "Provider"},
                                        subnets=[{"family": 6, "prefix": AMAZON, "source": "Provider"}]),
                         {"source": "Provider", "internet": True, "networkPrefixes6": [NET6]})
        self.assertEqual(self.ipv6_here(None), {})

    def place_args(self, v6):
        task = by_name(load(os.path.join(NP, "backend_cloudaws.yml")), "The placement's VPC and its attachment to the network")
        return run_play([task_vars(task), write_out("_out")],
                        {"np_aws_cfg": {}, "fab_namespace": "acme", "fab_name": "p1", "np_l3vni": 1001,
                         "np_prefixes_v4": V4, "np_aws_nat_egress": False, "np_aws_ipv6": v6})["aws_args"]

    def test_place_up_arguments(self):
        self.assertEqual(self.place_args({}), ["place-up", "--placement", "acme/p1", "--vni", "1001", "--prefix=" + V4[0]])
        args = self.place_args({"source": "Provider", "internet": True, "networkPrefixes6": [NET6]})
        self.assertEqual(args[-2], "--ipv6")
        self.assertEqual(json.loads(args[-1]), {"source": "Provider", "internet": True, "networkPrefixes6": [NET6]})

    def provider_status(self, internet, egress6=False):
        """The provider block recorded (ipam-external's rows), then subnets.yml and ipv6_status.yml as the backend
        runs them."""
        block = load(os.path.join(NP, "backend_cloudaws.yml"))
        keep = by_name(block, "Keep the rows with the allocated prefixes")
        start = {"ansible.builtin.set_fact": {
            "np_status": {"entity": "acme", "prefixes": V4},
            "np_prefixes": V4,
            "fab_ipam_result": {"changed": True, "subnets": [{"family": 4, "prefix": V4[0], "source": "Network"},
                                                             {"family": 6, "prefix": AMAZON, "source": "Provider"}]}}}
        net = merged(NET_P, {"spec": {"egress": {"internet": internet}}})
        tasks = ([start, keep] + load(os.path.join(NP, "subnets.yml")) + load(os.path.join(NP, "ipv6_status.yml"))
                 + [write_out("{'status': np_status, 'v6': np_prefixes_v6, 'v4': np_prefixes_v4}")])
        return run_play(tasks, {"np_backend_kind": "CloudAWS", "np_network": net, "np_ipv6_source": "Provider",
                                "np_ipv6_mode": "Platform", "np_site": SITE, "ci_capabilities": CAPS,
                                "fab_real": merged(REAL, {"egress": {"ipv6": {"enabled": egress6}}})})

    def test_provider_block_in_status(self):
        out = self.provider_status(True)
        st = out["status"]
        self.assertEqual(st["prefixes"], V4)                       # allocated prefixes only
        self.assertEqual(st["subnets"], [{"family": 4, "prefix": V4[0], "source": "Network", "gateway": "10.60.4.1"},
                                         {"family": 6, "prefix": AMAZON, "source": "Provider"}])
        self.assertEqual(out["v6"], [AMAZON])                      # the gateway's route check looks for it
        self.assertEqual(out["v4"], V4)
        # the provider's path, ready whatever the fabric's IPv6 egress says
        self.assertEqual(st["ipv6"], {"addressing": "DHCPv6", "internet": {"state": "ready", "path": "Provider"},
                                      "translation": None})
        st = self.provider_status(False)["status"]
        self.assertIsNone(st["ipv6"]["internet"])                  # no internet: none (written as null)

    def network_status(self, egress6, row=None):
        task = by_name(load(os.path.join(NP, "ipv6_status.yml")), "Describe the placement's IPv6")
        net = merged(NET_P, {"status": {"egress": {"families": [row] if row else []}}})
        start = {"ansible.builtin.set_fact": {"np_status": {}}}
        return run_play([start, task, write_out("np_status")],
                        {"np_backend_kind": "CloudAWS", "np_network": net, "np_ipv6_source": "Network",
                         "np_prefixes_v4": V4, "np_prefixes_v6": [BLOCK], "np_site": SITE,
                         "fab_real": merged(REAL, {"egress": {"ipv6": {"enabled": egress6}}})})["ipv6"]["internet"]

    def test_network_source_status_path_fabric(self):
        self.assertEqual(self.network_status(False)["path"], "Fabric")
        self.assertEqual(self.network_status(False)["reason"], "NotOffered")
        self.assertEqual(self.network_status(True, {"family": 6, "state": "ready", "mode": "Routed"}),
                         {"state": "ready", "path": "Fabric"})

    def teardown_args(self, spec, site=None):
        task = by_name(load(os.path.join(NPD, "main.yml")),
                       "Remove the placement's VPC (and the network's hub side with its last placement)")
        st = {"realization": {"site": site}} if site else {}
        return run_play([task_vars(task), write_out("_out")],
                        {"fab_spec": spec, "fab_st": st, "_npd_aws_env": {"resources": []}, "aws_real": {},
                         "aws_edge_cfg": {}, "npd_real": {"l3Vni": 1001}, "fab_namespace": "acme",
                         "fab_name": "p1"})["aws_args"]

    def test_teardown_passes_ipv6(self):
        base = ["place-down", "--placement", "acme/p1", "--vni", "1001"]
        self.assertEqual(self.teardown_args({"ipv4": {"prefixLength": 24}}), base)
        args = self.teardown_args({"ipv4": {"prefixLength": 24}, "ipv6": {}})
        self.assertEqual(args[:-1], base + ["--ipv6"])
        self.assertEqual(json.loads(args[-1]), {"source": "Network"})
        args = self.teardown_args({"ipv4": {"prefixLength": 24}, "ipv6": {"source": "Provider"}},
                                  site={"ipv6": {"source": "Provider", "prefix": AMAZON}})
        self.assertEqual(json.loads(args[-1]), {"source": "Provider", "prefix": AMAZON})


class CrdFieldsTest(unittest.TestCase):
    """The fields the AWS side writes are declared (both CRD copies)."""

    def test_declared(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_placement_ipv6_status import status_schema
        for path in CRDS:
            st = status_schema(path)
            self.assertIn("source", st["subnets"]["items"]["properties"], path)
            self.assertIn("path", st["ipv6"]["properties"]["internet"]["properties"], path)
            self.assertIn("realization", st, path)


if __name__ == "__main__":
    unittest.main()

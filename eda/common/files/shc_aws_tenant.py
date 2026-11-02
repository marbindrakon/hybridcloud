#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §11): a tenant's AWS side. Runs with the job's AWS web identity (the management account's
automation role) and enters the tenant account through its `shc-automation` role with external ID shc-<account>, and
the hub through shc_aws_edge.py for the network's transit gateway side. Idempotent; prints one JSON line.

  shc_aws_tenant.py --config '<json>' check
  shc_aws_tenant.py --config '<json>' zone-up --zone FQDN           the environment's Route53 public zone
  shc_aws_tenant.py --config '<json>' zone-down --zone FQDN
  shc_aws_tenant.py --config '<json>' place-up --placement NS/NAME --vni N [--prefix CIDR ...] [--egress]
                                                [--ipv6 '<json>']        (no --prefix: IPv6-only, see below)
  shc_aws_tenant.py --config '<json>' place-down --placement NS/NAME --vni N [--ipv6 '<json>']
  shc_aws_tenant.py --config '<json>' place-public --placement NS/NAME --state up|down
      public /28s (one per AZ, below the attachment /28s) with an internet gateway, for internet-facing load
      balancers of a DMZ-like workload; the workload subnets keep leaving through the network
  shc_aws_tenant.py --config '<json>' share-down        stop sharing the hub's transit gateway with this account

config: {"account", "region", "memberRole", "entry", "azs": [..], "tags": {..},
         "edge": <shc_aws_edge.py config of the region's edge site>}

A placement is one VPC in the tenant account: its prefixes (the first is the VPC's primary CIDR), in each AZ a
workload subnet (the lower half of the prefix split per AZ) and a /28 for the transit gateway attachment (from the
top of the prefix), the attachment accepted into the network's route table (shc-net-<vni>, its Connect attachment
to the edge routers created on first use), and the VPC's default route to the transit gateway. Traffic leaves the
VPC only through the network (--egress: the network's internet egress is its NAT egress VPC at the hub). An account
outside the Organization is added to the hub's transit gateway share on first use (the invitation accepted here) and
removed again with share-down.

IPv6 (design/IPV6-PLAN.md §6.2), --ipv6 {"source": "Network" | "Provider", "prefix": CIDR (Network: the placement's
block, a /44 to /60 in steps of 4), "internet": bool, "networkPrefixes6": [CIDR]} (the network's IPv6 space):
  Network   the block from the network's space, placed in the VPC as space AWS never advertises: the account's IPAM in
            the region (Free Tier, one per account and region, private GUA enabled, tagged shc-role tenant-ipam), a
            private-scope pool per placement holding exactly the block, the VPC's IPv6 block from that pool; internet
            (when asked) is ::/0 to the transit gateway, through the network
  Provider  the Amazon-assigned /56; internet (when asked) is ::/0 to an egress-only internet gateway, never through
            the network
Both: a /64 per AZ for the workload subnet (from the bottom of the block) and the attachment subnet (from its top;
the attachment subnets keep their IPv4 /28s, an attachment needs IPv4), the attachment with IPv6 support, and the
network's IPv6 space (networkPrefixes6, less the VPC's own block) routed to the transit gateway. The IPv6 routes of
the VPC's main route table are reconciled to exactly that set. place-up's result carries ipv6 {source, prefix,
subnets, internet, path, pool, ipam, egressOnlyGateway, routes}. place-down with --ipv6 (or a VPC holding IPv6) removes
in reverse: the IPv6 routes, the egress-only gateway, the VPC's IPv6 block, the placement's pool (cascade: IPAM
releases a VPC's allocation minutes after the VPC is gone, so a plain deprovision would wait that long) and the
IPAM once it holds no other pool.

IPv6-only (design/IPV6-PLAN.md §6.4; place-up without --prefix): --ipv6 adds "placeholderV4Pool" (the site's never-
routed IPv4 range; without it an IPv6-only placement is refused), and optionally "resolvers" (source Network: the
translation resolvers for the VPC's DHCP option set) and "nat64" (source Provider: translation qualifies). The VPC
still needs an IPv4 CIDR and the attachment IPv4 subnets (AWS refuses an attachment on IPv6-only subnets): the VPC's
IPv4 CIDR is a /26 of the pool chosen by the VNI (stable on re-run), holding only the attachment subnets (a /28 + a
/64 per AZ); the workload subnets are IPv6-only (a /64 per AZ, resource-name hostnames with an AAAA record on launch).
No IPv4 default route. Source Network: no DNS64; a DHCP option set shc-<placement> with the resolvers given (else
AmazonProvidedDNS) on the VPC (the VPC holds only IPv6-only workload subnets, so an IPv6-only resolver list strands no
dual-stack guest); internet as for dual-stack. Source Provider with nat64: DNS64 on the workload subnets, the VPC's
64:ff9b::/96 to the transit gateway, and the network's NAT egress VPC (net-up --egress --nat64) translating it with
its NAT gateway (needs --egress); without nat64 none of that (removed when present). place-up's ipv6 then also carries
only, dns64, dhcpOptions and translation ({path: Provider, pref64, resolvers: [the VPC resolver], ipv4Source: the NAT
gateway's Elastic IP} or None), the result placeholderV4. place-down also removes the DHCP option set.
"""
import argparse
import ipaddress
import json
import os
import sys
import time

import boto3
import botocore.exceptions

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shc_aws_edge  # noqa: E402

TAGS = [{"Key": "shc-managed", "Value": "true"}]
# IPv6-only VPCs: the slice of the placeholder pool per placement (room for four AZs' attachment /28s), the NAT64
# prefix the NAT gateway translates, the VPC resolver's IPv6 address (DNS64 when the subnet asks for it)
PLACEHOLDER_PREFIXLEN = 26
NAT64_PREFIX = "64:ff9b::/96"
VPC_RESOLVER6 = "fd00:ec2::253"


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(1)


def session(cfg):
    sts = boto3.client("sts")
    if cfg.get("entry", "automation") == "org-access":
        kw = {"RoleArn": "arn:aws:iam::%s:role/OrganizationAccountAccessRole" % cfg["account"]}
    else:
        kw = {"RoleArn": "arn:aws:iam::%s:role/%s" % (cfg["account"], cfg.get("memberRole", "shc-automation")),
              "ExternalId": "shc-%s" % cfg["account"]}
    c = sts.assume_role(RoleSessionName="shc-tenant", **kw)["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=cfg["region"])


def tags(name, extra):
    return TAGS + [{"Key": "Name", "Value": name}] + [{"Key": k, "Value": str(v)} for k, v in extra.items()]


def tagspec(kind, name, extra):
    return [{"ResourceType": kind, "Tags": tags(name, extra)}]


def tagmap(o):
    return {t["Key"]: t["Value"] for t in o.get("Tags", [])}


def layout(prefixes, azs):
    """Workload subnets: the lower half of the primary prefix split per AZ; attachment /28s from its top."""
    p = ipaddress.ip_network(prefixes[0])
    if p.prefixlen > 24:
        fail("prefix %s: an AWS placement needs at least a /24" % p)
    lower, upper = list(p.subnets(prefixlen_diff=1))
    bits = max(1, (len(azs) - 1).bit_length())
    work = list(lower.subnets(prefixlen_diff=bits))[:len(azs)]
    tops = list(upper.subnets(new_prefix=28))
    tgw, public = tops[-len(azs):], tops[-2 * len(azs):-len(azs)]
    return [{"az": az, "work": str(work[i]), "tgw": str(tgw[i]), "public": str(public[i])} for i, az in enumerate(azs)]


def layout6(block, azs):
    """A placement's IPv6 /64s: per AZ the workload subnet from the bottom of the block (index 0, 1, ...) and the
    attachment subnet from its top (the last len(azs) /64s)."""
    b = ipaddress.ip_network(block)
    if b.version != 6 or not 44 <= b.prefixlen <= 64:
        fail("IPv6 block %s: an AWS placement takes a /44 to /64" % b)
    count = 1 << (64 - b.prefixlen)
    if count < 2 * len(azs):
        fail("IPv6 block %s has %d /64s, %d AZs need %d" % (b, count, len(azs), 2 * len(azs)))
    base = int(b.network_address)

    def nth(i):
        return str(ipaddress.IPv6Network((base + (i << 64), 64)))
    return [{"az": az, "work": nth(i), "tgw": nth(count - len(azs) + i)} for i, az in enumerate(azs)]


def placeholder_v4(pool, vni, azs):
    """An IPv6-only placement's VPC IPv4 CIDR: the /26 of the placeholder pool at index VNI modulo the pool's /26s
    (the same on every run). Two placements may share one: the CIDR never leaves its VPC."""
    p = ipaddress.ip_network(pool)
    if len(azs) > 1 << (28 - PLACEHOLDER_PREFIXLEN):
        fail("an IPv6-only placement's /%d holds %d attachment /28s, not %d AZs"
             % (PLACEHOLDER_PREFIXLEN, 1 << (28 - PLACEHOLDER_PREFIXLEN), len(azs)))
    count = 1 << (PLACEHOLDER_PREFIXLEN - p.prefixlen)
    base = int(p.network_address) + (int(vni) % count) * (1 << (32 - PLACEHOLDER_PREFIXLEN))
    return str(ipaddress.IPv4Network((base, PLACEHOLDER_PREFIXLEN)))


def layout_only(cidr, azs):
    """An IPv6-only VPC's IPv4: an attachment /28 per AZ from the bottom of its placeholder CIDR, nothing else."""
    tgw = list(ipaddress.ip_network(cidr).subnets(new_prefix=28))
    return [{"az": az, "tgw": str(tgw[i])} for i, az in enumerate(azs)]


def dhcp_servers(d):
    for x in d.get("DhcpConfigurations") or []:
        if x.get("Key") == "domain-name-servers":
            return [v.get("Value") for v in x.get("Values") or []]
    return []


def ipv6_want(ipv6):
    """The --ipv6 request, canonical; None without one."""
    if not ipv6:
        return None
    src = ipv6.get("source") or "Network"
    if src not in ("Network", "Provider"):
        fail("IPv6 source %s: Network or Provider" % src)
    out = {"source": src, "internet": bool(ipv6.get("internet")),
           "networkPrefixes6": [str(ipaddress.ip_network(p)) for p in ipv6.get("networkPrefixes6") or []]}
    if src == "Network":
        if not ipv6.get("prefix"):
            fail("IPv6 source Network needs the placement's block (prefix)")
        b = ipaddress.ip_network(ipv6["prefix"])
        if b.version != 6 or not 44 <= b.prefixlen <= 60 or b.prefixlen % 4:
            fail("IPv6 block %s: a VPC takes a /44 to /60 in steps of 4" % b)
        out["prefix"] = str(b)
    # IPv6-only (place-up without an IPv4 prefix); absent keys leave a dual-stack request as it was
    pool = str(ipv6.get("placeholderV4Pool") or "").strip()
    if pool:
        try:
            p = ipaddress.ip_network(pool)
        except ValueError:
            p = None
        if p is None or p.version != 4 or p.prefixlen > PLACEHOLDER_PREFIXLEN:
            fail("placeholderV4Pool %s: an IPv4 network of /%d or larger" % (pool, PLACEHOLDER_PREFIXLEN))
        out["placeholderV4Pool"] = str(p)
    if "resolvers" in ipv6:
        try:
            out["resolvers"] = [str(ipaddress.ip_address(str(x).strip())) for x in ipv6.get("resolvers") or []]
        except ValueError as e:
            fail("resolvers: %s" % e)
    if "nat64" in ipv6:
        out["nat64"] = bool(ipv6.get("nat64"))
    return out


def v6_assoc(vpc):
    """The VPC's live IPv6 block associations."""
    return [a for a in vpc.get("Ipv6CidrBlockAssociationSet") or []
            if a.get("Ipv6CidrBlockState", {}).get("State") in ("associated", "associating")]


GONE = ("delete-in-progress", "delete-complete", "delete-failed")


class Tenant:
    def __init__(self, cfg):
        self.cfg = cfg
        s = session(cfg)
        self.ec2, self.r53, self.ram = s.client("ec2"), s.client("route53"), s.client("ram")
        self.changed = []

    # ---- the environment's zone
    def find_zone(self, zone):
        name = zone.rstrip(".") + "."
        for z in self.r53.list_hosted_zones_by_name(DNSName=name, MaxItems="5")["HostedZones"]:
            if z["Name"] == name and not z["Config"].get("PrivateZone"):
                return z
        return None

    def zone_up(self, zone):
        z = self.find_zone(zone)
        if not z:
            self.changed.append("zone " + zone)
            z = self.r53.create_hosted_zone(Name=zone, CallerReference="shc-%s-%d" % (zone, int(time.time())),
                                            HostedZoneConfig={"Comment": "SHC environment zone", "PrivateZone": False})["HostedZone"]
            self.r53.change_tags_for_resource(ResourceType="hostedzone", ResourceId=z["Id"].split("/")[-1],
                                              AddTags=[{"Key": t["Key"], "Value": t["Value"]} for t in
                                                       tags(zone, self.cfg.get("tags", {}))])
        # names appear here all the time (clusters' API and ingress): keep negative answers short-lived in resolvers
        # (Route53's default SOA makes them 900 s; a lookup just before an installer writes its record then stalls it)
        soa = [r for r in self.r53.list_resource_record_sets(HostedZoneId=z["Id"], StartRecordName=z["Name"],
                                                             StartRecordType="SOA", MaxItems="1")["ResourceRecordSets"]
               if r["Type"] == "SOA" and r["Name"] == z["Name"]]
        if soa:
            f = soa[0]["ResourceRecords"][0]["Value"].split()
            if soa[0]["TTL"] != 60 or f[6] != "60":
                f[6] = "60"
                self.r53.change_resource_record_sets(HostedZoneId=z["Id"], ChangeBatch={"Changes": [{"Action": "UPSERT",
                    "ResourceRecordSet": {"Name": z["Name"], "Type": "SOA", "TTL": 60,
                                          "ResourceRecords": [{"Value": " ".join(f)}]}}]})
                self.changed.append("zone %s negative TTL 60 s" % zone)
        ns = self.r53.get_hosted_zone(Id=z["Id"])["DelegationSet"]["NameServers"]
        return {"zone": zone, "zoneId": z["Id"].split("/")[-1], "nameServers": sorted(ns)}

    def zone_down(self, zone):
        z = self.find_zone(zone)
        if not z:
            return {"zone": zone, "deleted": False}
        changes = []
        for page in self.r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=z["Id"]):
            for rr in page["ResourceRecordSets"]:
                if not (rr["Type"] in ("NS", "SOA") and rr["Name"] == z["Name"]):
                    changes.append({"Action": "DELETE", "ResourceRecordSet": rr})
        if changes:
            self.r53.change_resource_record_sets(HostedZoneId=z["Id"], ChangeBatch={"Changes": changes})
        self.r53.delete_hosted_zone(Id=z["Id"])
        self.changed.append("deleted zone " + zone)
        return {"zone": zone, "deleted": True}

    # ---- a placement's VPC
    def find_vpc(self, placement):
        v = self.ec2.describe_vpcs(Filters=[{"Name": "tag:shc-placement", "Values": [placement]}])["Vpcs"]
        return v[0] if v else None

    def tgw_visible(self, tgw_id):
        try:
            return bool(self.ec2.describe_transit_gateways(TransitGatewayIds=[tgw_id])["TransitGateways"])
        except botocore.exceptions.ClientError as e:
            if "InvalidTransitGatewayID" not in str(e):
                raise
            return False

    def share_up(self, edge, tgw_id):
        """An account outside the Organization: the hub shares its transit gateway with it, this account accepts."""
        arn = edge.share_add(self.cfg["account"])["share"]
        self.changed += edge.changed
        edge.changed = []
        for _ in range(30):
            for inv in self.ram.get_resource_share_invitations(resourceShareArns=[arn])["resourceShareInvitations"]:
                if inv["status"] == "PENDING":
                    self.ram.accept_resource_share_invitation(resourceShareInvitationArn=inv["resourceShareInvitationArn"])
                    self.changed.append("accepted transit gateway share")
            if self.tgw_visible(tgw_id):
                return
            time.sleep(10)
        fail("the hub's transit gateway never became visible in account %s" % self.cfg["account"])

    def share_down(self):
        edge = shc_aws_edge.Edge(self.cfg["edge"])
        r = edge.share_remove(self.cfg["account"])
        self.changed += edge.changed
        return r

    # ---- IPv6: the account's IPAM, a placement's pool, routes
    def wait_ipam(self, ipam_id):
        for _ in range(60):
            i = self.ec2.describe_ipams(IpamIds=[ipam_id])["Ipams"][0]
            if i["State"].endswith("-complete"):
                return i
            if i["State"].endswith("-failed"):
                fail("IPAM %s: %s" % (ipam_id, i["State"]))
            time.sleep(5)
        fail("IPAM %s never settled (%s)" % (ipam_id, i["State"]))

    def find_ipam(self):
        region = self.cfg["region"]
        mine = [i for i in self.ec2.describe_ipams()["Ipams"]
                if tagmap(i).get("shc-role") == "tenant-ipam" and tagmap(i).get("shc-managed") == "true"
                and i.get("IpamRegion", region) == region and i["State"] not in GONE]
        return mine[0] if mine else None

    def ensure_ipam(self):
        """The account's IPAM in the region: Free Tier, private GUA enabled (the network's own GUA space, which AWS
        never advertises, can then be provisioned into its private scope)."""
        ipam = self.find_ipam()
        if not ipam:
            extra = {"shc-role": "tenant-ipam"}
            if self.cfg.get("tags", {}).get("shc-entity"):
                extra["shc-entity"] = self.cfg["tags"]["shc-entity"]
            ipam = self.ec2.create_ipam(Description="SHC placements' IPv6", Tier="free",
                                        OperatingRegions=[{"RegionName": self.cfg["region"]}],
                                        TagSpecifications=tagspec("ipam", "shc-tenant-ipam", extra))["Ipam"]
            self.changed.append("ipam " + ipam["IpamId"])
        ipam = self.wait_ipam(ipam["IpamId"])
        if not ipam.get("EnablePrivateGua"):
            self.ec2.modify_ipam(IpamId=ipam["IpamId"], EnablePrivateGua=True)
            self.changed.append("ipam %s private GUA" % ipam["IpamId"])
            ipam = self.wait_ipam(ipam["IpamId"])
        return ipam

    def find_pool(self, placement):
        p = [x for x in self.ec2.describe_ipam_pools()["IpamPools"]
             if tagmap(x).get("shc-placement") == placement and x["State"] not in GONE]
        return p[0] if p else None

    def ensure_pool(self, ipam, placement, block, name, extra):
        """The placement's private-scope pool, holding exactly its block."""
        pool = self.find_pool(placement)
        if pool and pool.get("IpamArn") and pool["IpamArn"] != ipam["IpamArn"]:
            fail("IPAM pool %s of %s belongs to another IPAM" % (pool["IpamPoolId"], placement))
        if not pool:
            pool = self.ec2.create_ipam_pool(IpamScopeId=ipam["PrivateDefaultScopeId"], Locale=self.cfg["region"],
                                             AddressFamily="ipv6", AutoImport=False,
                                             Description="SHC placement " + placement,
                                             TagSpecifications=tagspec("ipam-pool", name, extra))["IpamPool"]
            self.changed.append("ipam pool " + name)
        pool_id = pool["IpamPoolId"]
        for _ in range(60):
            pool = self.ec2.describe_ipam_pools(IpamPoolIds=[pool_id])["IpamPools"][0]
            if pool["State"].endswith("-complete"):
                break
            if pool["State"].endswith("-failed"):
                fail("IPAM pool %s: %s %s" % (pool_id, pool["State"], pool.get("StateMessage", "")))
            time.sleep(5)
        else:
            fail("IPAM pool %s never settled" % pool_id)
        provisioned = False
        for _ in range(60):
            cidrs = self.ec2.get_ipam_pool_cidrs(IpamPoolId=pool_id)["IpamPoolCidrs"]
            other = [x["Cidr"] for x in cidrs if x["Cidr"] != block
                     and x["State"] not in ("deprovisioned", "failed-provision", "failed-deprovision")]
            if other:
                fail("IPAM pool %s of %s holds %s, not only %s" % (pool_id, placement, ", ".join(other), block))
            mine = [x for x in cidrs if x["Cidr"] == block]
            state = mine[-1]["State"] if mine else ""
            if state == "provisioned":
                return pool_id
            if state == "failed-provision" and provisioned:
                why = (mine[-1].get("FailureReason") or {}).get("Message", "failed-provision")
                fail("IPAM refused %s: %s" % (block, why), reason="PrefixInvalid")
            if not provisioned and state != "pending-provision":
                self.ec2.provision_ipam_pool_cidr(IpamPoolId=pool_id, Cidr=block)
                self.changed.append("ipam pool cidr " + block)
            provisioned = True
            time.sleep(5)
        fail("IPAM pool %s: %s never provisioned" % (pool_id, block))

    def vpc_block(self, vpc_id, want, pool_id=None):
        """The VPC's IPv6 block: the pool's (source Network, want = the block) or Amazon's /56 (want None)."""
        asked = False
        for _ in range(60):
            vpc = self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
            live = v6_assoc(vpc)
            if want:
                other = [a["Ipv6CidrBlock"] for a in live if a.get("Ipv6CidrBlock") and a["Ipv6CidrBlock"] != want]
                if other:
                    fail("VPC %s has IPv6 %s, want %s" % (vpc_id, ", ".join(other), want))
                mine = [a for a in live if a.get("Ipv6CidrBlock") == want or a.get("Ipv6Pool") == "IPAM Managed"]
            else:
                other = [a.get("Ipv6CidrBlock") for a in live if a.get("Ipv6Pool") not in ("Amazon", None)]
                if other:
                    fail("VPC %s has IPv6 %s that Amazon did not assign" % (vpc_id, ", ".join(map(str, other))))
                mine = live
            if mine and mine[0]["Ipv6CidrBlockState"]["State"] == "associated" and mine[0].get("Ipv6CidrBlock"):
                return mine[0]["Ipv6CidrBlock"]
            if not mine and not asked:
                plen = int(want.split("/")[1]) if want else None
                kw = ({"Ipv6IpamPoolId": pool_id, "Ipv6NetmaskLength": plen} if want
                      else {"AmazonProvidedIpv6CidrBlock": True})
                self.ec2.associate_vpc_cidr_block(VpcId=vpc_id, **kw)
                self.changed.append("vpc ipv6 " + (want or "Amazon /56"))
                asked = True
            time.sleep(3)
        fail("VPC %s: its IPv6 block never associated" % vpc_id)

    def find_eigw(self, vpc_id):
        return [g["EgressOnlyInternetGatewayId"] for g in
                self.ec2.describe_egress_only_internet_gateways()["EgressOnlyInternetGateways"]
                if any(a.get("VpcId") == vpc_id for a in g.get("Attachments") or [])]

    def main_table(self, vpc_id):
        return self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                       {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]

    def route_retry(self, fn, **kw):
        """A route to the transit gateway waits for the attachment to become available."""
        for _ in range(30):
            try:
                return fn(**kw)
            except botocore.exceptions.ClientError as e:
                if "InvalidTransitGatewayID" not in str(e) and "IncorrectState" not in str(e):
                    raise
                time.sleep(10)
        fail("route %s: the transit gateway attachment never became usable" % kw)

    def routes6(self, vpc_id, want):
        """The main route table's IPv6 routes become exactly want {destination: {TransitGatewayId | EgressOnlyInternetGatewayId: id}}."""
        main = self.main_table(vpc_id)
        rt = main["RouteTableId"]
        have = {r["DestinationIpv6CidrBlock"]: r for r in main["Routes"]
                if r.get("DestinationIpv6CidrBlock") and r.get("Origin") != "CreateRouteTable"}
        for dest in sorted(want):
            target = want[dest]
            r = have.get(dest)
            if r is None:
                self.route_retry(self.ec2.create_route, RouteTableId=rt, DestinationIpv6CidrBlock=dest, **target)
                self.changed.append("route %s" % dest)
            elif any(r.get(k) != v for k, v in target.items()):
                self.route_retry(self.ec2.replace_route, RouteTableId=rt, DestinationIpv6CidrBlock=dest, **target)
                self.changed.append("route %s replaced" % dest)
        for dest in sorted(set(have) - set(want)):
            self.ec2.delete_route(RouteTableId=rt, DestinationIpv6CidrBlock=dest)
            self.changed.append("deleted route " + dest)

    def eigw_down(self, vpc_id):
        for g in self.find_eigw(vpc_id):
            self.ec2.delete_egress_only_internet_gateway(EgressOnlyInternetGatewayId=g)
            self.changed.append("deleted egress-only internet gateway " + g)

    def pool_down(self, placement):
        """The placement's pool (cascade, see the module doc), then the IPAM when no other pool is left in it."""
        pool = self.find_pool(placement)
        out = {"pool": None, "ipamDeleted": False}
        if not pool:
            return out
        self.ec2.delete_ipam_pool(IpamPoolId=pool["IpamPoolId"], Cascade=True)
        self.changed.append("deleted ipam pool " + pool["IpamPoolId"])
        out["pool"] = pool["IpamPoolId"]
        ipam = self.find_ipam()
        if ipam and ipam["IpamArn"] == pool.get("IpamArn", ipam["IpamArn"]):
            left = [x for x in self.ec2.describe_ipam_pools()["IpamPools"]
                    if x.get("IpamArn") == ipam["IpamArn"] and x["State"] not in GONE
                    and x["IpamPoolId"] != pool["IpamPoolId"]]
            if not left:
                self.ec2.delete_ipam(IpamId=ipam["IpamId"], Cascade=True)
                self.changed.append("deleted ipam " + ipam["IpamId"])
                out["ipamDeleted"] = True
        return out

    # ---- IPv6-only: the VPC's DHCP option set, the workload subnets' DNS
    def find_dhcp(self, placement):
        return self.ec2.describe_dhcp_options(Filters=[{"Name": "tag:shc-placement", "Values": [placement]}])["DhcpOptions"]

    def dhcp_up(self, vpc_id, placement, name, extra, servers):
        """The VPC's option set shc-<placement> with these resolvers (option sets can't change: a new one replaces
        it, the old one goes)."""
        sets = self.find_dhcp(placement)
        mine = next((d for d in sets if dhcp_servers(d) == servers), None)
        if not mine:
            region = self.cfg["region"]
            conf = [{"Key": "domain-name-servers", "Values": list(servers)},
                    {"Key": "domain-name", "Values": ["ec2.internal" if region == "us-east-1" else region + ".compute.internal"]}]
            mine = self.ec2.create_dhcp_options(DhcpConfigurations=conf,
                                                TagSpecifications=tagspec("dhcp-options", name, extra))["DhcpOptions"]
            self.changed.append("dhcp options %s %s" % (name, ", ".join(servers)))
        if self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0].get("DhcpOptionsId") != mine["DhcpOptionsId"]:
            self.ec2.associate_dhcp_options(DhcpOptionsId=mine["DhcpOptionsId"], VpcId=vpc_id)
            self.changed.append("vpc %s dhcp options %s" % (vpc_id, mine["DhcpOptionsId"]))
        for d in sets:
            if d["DhcpOptionsId"] != mine["DhcpOptionsId"]:
                self.ec2.delete_dhcp_options(DhcpOptionsId=d["DhcpOptionsId"])
                self.changed.append("deleted dhcp options " + d["DhcpOptionsId"])
        return mine["DhcpOptionsId"]

    def dhcp_down(self, vpc_id, placement):
        """The placement's option sets: the VPC back on the default set, then deleted."""
        sets = self.find_dhcp(placement)
        if sets and vpc_id:
            cur = self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0].get("DhcpOptionsId")
            if cur in {d["DhcpOptionsId"] for d in sets}:
                self.ec2.associate_dhcp_options(DhcpOptionsId="default", VpcId=vpc_id)
        for d in sets:
            self.ec2.delete_dhcp_options(DhcpOptionsId=d["DhcpOptionsId"])
            self.changed.append("deleted dhcp options " + d["DhcpOptionsId"])

    def subnet_v6only(self, s, sn_name, dns64):
        """An IPv6-only workload subnet: resource-name hostnames (required there) with their AAAA record on launch,
        and DNS64 as asked (one attribute per call)."""
        sid = s["SubnetId"]
        dns = s.get("PrivateDnsNameOptionsOnLaunch") or {}
        if dns.get("HostnameType") != "resource-name":
            self.ec2.modify_subnet_attribute(SubnetId=sid, PrivateDnsHostnameTypeOnLaunch="resource-name")
            self.changed.append("subnet %s resource-name hostnames" % sn_name)
        if not dns.get("EnableResourceNameDnsAAAARecord"):
            self.ec2.modify_subnet_attribute(SubnetId=sid, EnableResourceNameDnsAAAARecordOnLaunch={"Value": True})
            self.changed.append("subnet %s AAAA records" % sn_name)
        if bool(s.get("EnableDns64")) != bool(dns64):
            self.ec2.modify_subnet_attribute(SubnetId=sid, EnableDns64={"Value": bool(dns64)})
            self.changed.append("subnet %s DNS64 %s" % (sn_name, "on" if dns64 else "off"))

    def place_up(self, placement, vni, prefixes, egress=False, ipv6=None):
        c = self.cfg
        v6 = ipv6_want(ipv6)
        only = not prefixes
        if only and not (v6 and v6.get("placeholderV4Pool")):
            fail("an AWS placement needs an IPv4 prefix: a VPC keeps an IPv4 CIDR and the attachment needs IPv4 "
                 "(this site has no placeholder IPv4 for IPv6-only VPCs)", reason="FamilyUnavailable")
        placeholder = placeholder_v4(v6["placeholderV4Pool"], vni, c["azs"]) if only else None
        cidrs = [placeholder] if only else prefixes
        provider = bool(v6) and v6["source"] == "Provider"
        # source Provider, IPv6-only: NAT64 in the network's egress VPC while translation qualifies (needs that VPC)
        nat64 = bool(only and provider and v6.get("nat64") and egress)
        edge = shc_aws_edge.Edge(c["edge"])
        tgw_id = edge.tgw()["TransitGatewayId"]
        if not self.tgw_visible(tgw_id):
            self.share_up(edge, tgw_id)
        extra = dict(c.get("tags", {}), **{"shc-placement": placement, "shc-vni": str(vni)})
        if only:
            extra["shc-ipv6-only"] = "true"
        name = "shc-" + placement.replace("/", "-")
        lay = layout_only(placeholder, c["azs"]) if only else layout(prefixes, c["azs"])
        pool_id = ipam_id = None
        if v6 and v6["source"] == "Network":
            layout6(v6["prefix"], c["azs"])  # refuse a block too small before anything is made
            ipam = self.ensure_ipam()
            ipam_id = ipam["IpamId"]
            pool_id = self.ensure_pool(ipam, placement, v6["prefix"], name, extra)
        vpc = self.find_vpc(placement)
        if vpc and vpc["CidrBlock"] != cidrs[0]:
            fail("VPC %s of %s has CIDR %s, want %s" % (vpc["VpcId"], placement, vpc["CidrBlock"], cidrs[0]))
        if not vpc:
            self.changed.append("vpc " + name)
            v6kw = {}
            if v6 and v6["source"] == "Network":
                v6kw = {"Ipv6IpamPoolId": pool_id, "Ipv6NetmaskLength": int(v6["prefix"].split("/")[1])}
            elif v6:
                v6kw = {"AmazonProvidedIpv6CidrBlock": True}
            vpc = self.ec2.create_vpc(CidrBlock=cidrs[0], TagSpecifications=tagspec("vpc", name, extra), **v6kw)["Vpc"]
            self.ec2.get_waiter("vpc_available").wait(VpcIds=[vpc["VpcId"]])
            self.ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsHostnames={"Value": True})
        vpc_id = vpc["VpcId"]
        have_cidrs = {a["CidrBlock"] for a in self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlockAssociationSet"]
                      if a["CidrBlockState"]["State"] in ("associated", "associating")}
        for p in cidrs[1:]:
            if p not in have_cidrs:
                self.changed.append("vpc cidr " + p)
                self.ec2.associate_vpc_cidr_block(VpcId=vpc_id, CidrBlock=p)
        block6, lay6 = None, {}
        if v6:
            block6 = self.vpc_block(vpc_id, v6.get("prefix"), pool_id)
            lay6 = {l["az"]: l for l in layout6(block6, c["azs"])}
        # source Network, IPv6-only: the guests' resolvers from the VPC's option set (Provider keeps Amazon's)
        dhcp_id = None
        if only and not provider:
            dhcp_id = self.dhcp_up(vpc_id, placement, name, extra, v6.get("resolvers") or ["AmazonProvidedDNS"])
        elif only:
            self.dhcp_down(vpc_id, placement)
        subnets = {tagmap(s).get("Name"): s for s in self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]}
        work, tgw_subnets = [], []
        for l in lay:
            for role, cidr in (("work", l.get("work")), ("tgw", l["tgw"])):
                sn_name = "%s-%s-%s" % (name, role, l["az"][-1])
                cidr6 = lay6[l["az"]][role] if v6 else None
                native = only and role == "work"
                s = subnets.get(sn_name)
                if not s and native:
                    self.changed.append("subnet " + sn_name)
                    s = self.ec2.create_subnet(VpcId=vpc_id, AvailabilityZone=l["az"], Ipv6Native=True,
                                               Ipv6CidrBlock=cidr6,
                                               TagSpecifications=tagspec("subnet", sn_name, dict(extra, **{"shc-role": role})))["Subnet"]
                elif not s:
                    self.changed.append("subnet " + sn_name)
                    s = self.ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=l["az"],
                                               TagSpecifications=tagspec("subnet", sn_name, dict(extra, **{"shc-role": role})),
                                               **({"Ipv6CidrBlock": cidr6} if cidr6 else {}))["Subnet"]
                if native and s.get("CidrBlock"):
                    fail("subnet %s has IPv4 %s; an IPv6-only placement's workload subnets have none"
                         % (s["SubnetId"], s["CidrBlock"]))
                if cidr6:
                    have6 = [a["Ipv6CidrBlock"] for a in s.get("Ipv6CidrBlockAssociationSet") or []
                             if a.get("Ipv6CidrBlockState", {}).get("State") in ("associated", "associating")]
                    if have6 and cidr6 not in have6:
                        fail("subnet %s has IPv6 %s, want %s" % (s["SubnetId"], ", ".join(have6), cidr6))
                    if not have6:
                        self.ec2.associate_subnet_cidr_block(SubnetId=s["SubnetId"], Ipv6CidrBlock=cidr6)
                        self.changed.append("subnet %s ipv6 %s" % (sn_name, cidr6))
                    if role == "work" and not s.get("AssignIpv6AddressOnCreation"):
                        self.ec2.modify_subnet_attribute(SubnetId=s["SubnetId"], AssignIpv6AddressOnCreation={"Value": True})
                        self.changed.append("subnet %s assigns IPv6" % sn_name)
                if native:
                    self.subnet_v6only(s, sn_name, dns64=nat64)
                row = {"id": s["SubnetId"], "az": l["az"]} if native else {"id": s["SubnetId"], "cidr": cidr, "az": l["az"]}
                if cidr6:
                    row["ipv6"] = cidr6
                (work if role == "work" else tgw_subnets).append(row)
        att = [a for a in self.ec2.describe_transit_gateway_vpc_attachments(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]}])["TransitGatewayVpcAttachments"]
            if a["State"] not in ("deleted", "deleting", "rejected", "failed")]
        if att:
            att_id = att[0]["TransitGatewayAttachmentId"]
            if v6 and (att[0].get("Options") or {}).get("Ipv6Support") != "enable":
                self.ec2.modify_transit_gateway_vpc_attachment(TransitGatewayAttachmentId=att_id,
                                                               Options={"Ipv6Support": "enable"})
                self.changed.append("tgw attachment %s ipv6" % att_id)
        else:
            self.changed.append("tgw attachment " + name)
            att_id = self.ec2.create_transit_gateway_vpc_attachment(
                TransitGatewayId=tgw_id, VpcId=vpc_id, SubnetIds=[s["id"] for s in tgw_subnets],
                TagSpecifications=tagspec("transit-gateway-attachment", name, extra),
                **({"Options": {"Ipv6Support": "enable"}} if v6 else {}))["TransitGatewayVpcAttachment"]["TransitGatewayAttachmentId"]
        # the hub: the network's route table, Connect attachment and router VRF; accept the attachment into it. An
        # IPv6-only provider-source VPC also tells the network's egress VPC whether to translate for its block
        if only and provider:
            net = edge.net_up(vni, egress=egress, nat64=[block6] if nat64 else [])
        else:
            net = edge.net_up(vni, egress=egress)
        for _ in range(30):  # the attachment shows up on the hub side shortly after creation
            try:
                edge.net_attach(vni, att_id)
                break
            except botocore.exceptions.ClientError as e:
                if "NotFound" not in str(e):
                    raise
                time.sleep(10)
        else:
            fail("attachment %s never reached the hub" % att_id)
        self.changed += edge.changed
        # the VPC's default route leaves through the network (an IPv6-only VPC has no IPv4 workload: none)
        if not only:
            main = self.main_table(vpc_id)
            if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in main["Routes"]):
                self.route_retry(self.ec2.create_route, RouteTableId=main["RouteTableId"], DestinationCidrBlock="0.0.0.0/0",
                                 TransitGatewayId=tgw_id)
                self.changed.append("default route")
        out6 = None
        if v6:
            # the network's IPv6 space through the network (not the VPC's own block: that route is local)
            mine = ipaddress.ip_network(block6)
            want = {p: {"TransitGatewayId": tgw_id} for p in v6["networkPrefixes6"]
                    if not ipaddress.ip_network(p).subnet_of(mine)}
            eigw = None
            if v6["internet"] and v6["source"] == "Provider":
                found = self.find_eigw(vpc_id)
                if found:
                    eigw = found[0]
                else:
                    eigw = self.ec2.create_egress_only_internet_gateway(
                        VpcId=vpc_id, TagSpecifications=tagspec("egress-only-internet-gateway", name, extra)
                    )["EgressOnlyInternetGateway"]["EgressOnlyInternetGatewayId"]
                    self.changed.append("egress-only internet gateway " + eigw)
                want["::/0"] = {"EgressOnlyInternetGatewayId": eigw}
            elif v6["internet"]:
                want["::/0"] = {"TransitGatewayId": tgw_id}
            if nat64:  # the translator's prefix to the network's egress VPC (over the transit gateway)
                want[NAT64_PREFIX] = {"TransitGatewayId": tgw_id}
            self.routes6(vpc_id, want)
            if not eigw:
                self.eigw_down(vpc_id)
            out6 = {"source": v6["source"], "prefix": block6, "subnets": list(lay6.values()), "internet": v6["internet"],
                    "path": "Provider" if v6["source"] == "Provider" else "Fabric",
                    "pool": pool_id, "ipam": ipam_id, "egressOnlyGateway": eigw, "routes": sorted(want)}
            if only:
                translation = None
                if nat64:
                    translation = {"path": "Provider", "pref64": NAT64_PREFIX, "resolvers": [VPC_RESOLVER6]}
                    ip4 = (net.get("egress") or {}).get("publicIp")
                    if ip4:
                        translation["ipv4Source"] = ip4
                out6.update({"only": True, "dns64": nat64, "dhcpOptions": dhcp_id, "translation": translation})
        out = {"placement": placement, "account": c["account"], "vpcId": vpc_id, "prefixes": prefixes,
               "subnets": work, "attachmentSubnets": tgw_subnets, "attachment": att_id,
               "routeTable": net["routeTable"], "connectAttachment": net["connectAttachment"],
               "egress": net.get("egress"), "ipv6": out6}
        if only:
            out["placeholderV4"] = placeholder
        return out

    def place_public(self, placement, state):
        vpc = self.find_vpc(placement)
        if not vpc:
            fail("placement %s has no VPC" % placement)
        vpc_id = vpc["VpcId"]
        name = "shc-" + placement.replace("/", "-")
        extra = dict(self.cfg.get("tags", {}), **{"shc-placement": placement, "shc-role": "public"})
        subnets = {tagmap(x).get("Name"): x for x in self.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]}
        rt_name = name + "-public"
        rts = [r for r in self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                                  {"Name": "tag:Name", "Values": [rt_name]}])["RouteTables"]]
        igws = self.ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]
        if state == "up" and tagmap(vpc).get("shc-ipv6-only") == "true":
            fail("placement %s is IPv6-only: it has no IPv4 for public subnets" % placement)
        if state == "down":
            for sn_name, s in subnets.items():
                if (sn_name or "").startswith(name + "-public-"):
                    for _ in range(30):  # load balancer ENIs go with the cluster's deprovision
                        try:
                            self.ec2.delete_subnet(SubnetId=s["SubnetId"])
                            self.changed.append("deleted subnet " + s["SubnetId"])
                            break
                        except botocore.exceptions.ClientError as e:
                            if "DependencyViolation" not in str(e):
                                raise
                            time.sleep(10)
            for r in rts:
                self.ec2.delete_route_table(RouteTableId=r["RouteTableId"])
            for g in igws:
                self.ec2.detach_internet_gateway(InternetGatewayId=g["InternetGatewayId"], VpcId=vpc_id)
                self.ec2.delete_internet_gateway(InternetGatewayId=g["InternetGatewayId"])
                self.changed.append("deleted internet gateway " + g["InternetGatewayId"])
            return {"placement": placement, "public": []}
        if igws:
            igw_id = igws[0]["InternetGatewayId"]
        else:
            igw_id = self.ec2.create_internet_gateway(TagSpecifications=tagspec("internet-gateway", name, extra))["InternetGateway"]["InternetGatewayId"]
            self.ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            self.changed.append("internet gateway " + igw_id)
        if rts:
            rt = rts[0]
        else:
            rt = self.ec2.create_route_table(VpcId=vpc_id, TagSpecifications=tagspec("route-table", rt_name, extra))["RouteTable"]
            self.changed.append("route table " + rt_name)
        if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in rt.get("Routes", [])):
            self.ec2.create_route(RouteTableId=rt["RouteTableId"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        lay = layout([vpc["CidrBlock"]], self.cfg["azs"])
        out = []
        assoc = {a.get("SubnetId") for a in self.ec2.describe_route_tables(RouteTableIds=[rt["RouteTableId"]])["RouteTables"][0]["Associations"]}
        for l in lay:
            sn = "%s-public-%s" % (name, l["az"][-1])
            s = subnets.get(sn)
            if not s:
                s = self.ec2.create_subnet(VpcId=vpc_id, CidrBlock=l["public"], AvailabilityZone=l["az"],
                                           TagSpecifications=tagspec("subnet", sn, extra))["Subnet"]
                self.changed.append("subnet " + sn)
            if s["SubnetId"] not in assoc:
                self.ec2.associate_route_table(RouteTableId=rt["RouteTableId"], SubnetId=s["SubnetId"])
            out.append({"id": s["SubnetId"], "cidr": l["public"], "az": l["az"]})
        return {"placement": placement, "public": out, "internetGateway": igw_id}

    def place_down(self, placement, vni, ipv6=None):
        c = self.cfg
        edge = shc_aws_edge.Edge(c["edge"])
        vpc = self.find_vpc(placement)
        with6 = bool(ipv6) or bool(vpc and v6_assoc(vpc))
        # IPv6-only (the request says so, or the VPC's tag): its DHCP option set goes too
        only = bool((ipv6 or {}).get("only")) or bool(vpc and tagmap(vpc).get("shc-ipv6-only") == "true")
        out6 = {"pool": None, "ipamDeleted": False}
        if vpc:
            vpc_id = vpc["VpcId"]
            left = [i["InstanceId"] for r in self.ec2.describe_instances(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
                for i in r["Instances"]]
            if left:
                fail("VPC %s still has instances %s; the tenant removes its workloads first" % (vpc_id, left),
                     reason="WorkloadsPresent")
            if with6:  # IPv6 in reverse: the routes, then the egress-only gateway
                self.routes6(vpc_id, {})
                self.eigw_down(vpc_id)
            for a in self.ec2.describe_transit_gateway_vpc_attachments(Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]}])["TransitGatewayVpcAttachments"]:
                if a["State"] not in ("deleted", "deleting"):
                    self.ec2.delete_transit_gateway_vpc_attachment(TransitGatewayAttachmentId=a["TransitGatewayAttachmentId"])
                    self.changed.append("deleted tgw attachment " + a["TransitGatewayAttachmentId"])
                for _ in range(60):
                    x = self.ec2.describe_transit_gateway_vpc_attachments(
                        TransitGatewayAttachmentIds=[a["TransitGatewayAttachmentId"]])["TransitGatewayVpcAttachments"]
                    if not x or x[0]["State"] == "deleted":
                        break
                    time.sleep(10)
            self.place_public(placement, "down")
            for s in self.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]:
                for _ in range(20):  # attachment ENIs linger briefly
                    try:
                        self.ec2.delete_subnet(SubnetId=s["SubnetId"])
                        break
                    except botocore.exceptions.ClientError as e:
                        if "DependencyViolation" not in str(e):
                            raise
                        time.sleep(10)
            if only:  # the VPC back on the default option set, ours deleted
                self.dhcp_down(vpc_id, placement)
            if with6:  # the VPC's IPv6 block, once no subnet holds a /64 of it
                for a in v6_assoc(self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]):
                    self.ec2.disassociate_vpc_cidr_block(AssociationId=a["AssociationId"])
                    self.changed.append("vpc ipv6 %s released" % a.get("Ipv6CidrBlock", ""))
            for sg in self.ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]:
                if sg["GroupName"] != "default":
                    self.ec2.delete_security_group(GroupId=sg["GroupId"])
            self.ec2.delete_vpc(VpcId=vpc_id)
            self.changed.append("deleted vpc " + vpc_id)
        elif only:  # a run that stopped before the VPC: an option set without one
            self.dhcp_down(None, placement)
        if with6:  # the placement's pool and, with its last pool, the IPAM
            out6 = self.pool_down(placement)
        # the network's hub side goes with its last tenant attachment
        tgw_id = edge.tgw()["TransitGatewayId"]
        # (the network's own NAT egress VPC carries the network's tag as well: it is not a tenant's)
        others = [a for a in edge.attachments(tgw_id, **{"shc-vni": str(vni)}) if a["ResourceType"] == "vpc"
                  and tagmap(a).get("shc-role") != "egress"]
        if not others:
            edge.net_down(vni)
            self.changed += edge.changed
        return {"placement": placement, "vni": vni, "networkKept": bool(others), "ipv6": out6 if with6 else None}

    def check(self):
        who = boto3.client("sts").get_caller_identity()["Arn"]
        me = self.ec2.describe_vpcs(MaxResults=5)["Vpcs"]
        return {"account": self.cfg["account"], "caller": who, "vpcs": len(me)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("command", choices=["check", "zone-up", "zone-down", "place-up", "place-down", "place-public",
                                        "share-down"])
    ap.add_argument("--zone")
    ap.add_argument("--placement")
    ap.add_argument("--vni", type=int)
    ap.add_argument("--prefix", action="append", default=[])
    ap.add_argument("--egress", action="store_true")
    ap.add_argument("--state", choices=["up", "down"])
    ap.add_argument("--ipv6", type=json.loads, default=None)
    a = ap.parse_args()
    t = Tenant(json.loads(a.config))
    if a.command.startswith("zone") and not a.zone:
        fail("--zone is required")
    if a.command in ("place-up", "place-down") and not (a.placement and a.vni):
        fail("--placement and --vni are required")
    if a.command == "place-public" and not (a.placement and a.state):
        fail("--placement and --state are required")
    r = {"check": t.check,
         "zone-up": lambda: t.zone_up(a.zone), "zone-down": lambda: t.zone_down(a.zone),
         # without --prefix: IPv6-only (place_up refuses it without the site's placeholder range)
         "place-up": lambda: t.place_up(a.placement, a.vni, a.prefix, a.egress, a.ipv6),
         "place-down": lambda: t.place_down(a.placement, a.vni, a.ipv6),
         "place-public": lambda: t.place_public(a.placement, a.state),
         "share-down": t.share_down}[a.command]()
    print(json.dumps({"ok": True, "changed": t.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

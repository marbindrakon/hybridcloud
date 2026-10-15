#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §11): a tenant's AWS side. Runs with the job's AWS web identity (the management account's
automation role) and enters the tenant account through its `shc-automation` role with external ID shc-<account>, and
the hub through shc_aws_edge.py for the network's transit gateway side. Idempotent; prints one JSON line.

  shc_aws_tenant.py --config '<json>' check
  shc_aws_tenant.py --config '<json>' zone-up --zone FQDN           the environment's Route53 public zone
  shc_aws_tenant.py --config '<json>' zone-down --zone FQDN
  shc_aws_tenant.py --config '<json>' place-up --placement NS/NAME --vni N --prefix CIDR [--prefix CIDR ...]
  shc_aws_tenant.py --config '<json>' place-down --placement NS/NAME --vni N

config: {"account", "region", "memberRole", "entry", "azs": [..], "tags": {..},
         "edge": <shc_aws_edge.py config of the region's edge site>}

A placement is one VPC in the tenant account: its prefixes (the first is the VPC's primary CIDR), in each AZ a
workload subnet (the lower half of the prefix split per AZ) and a /28 for the transit gateway attachment (from the
top of the prefix), the attachment accepted into the network's route table (shc-net-<vni>, its Connect attachment
to the edge routers created on first use), and the VPC's default route to the transit gateway. Traffic leaves the
VPC only through the network.
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
    tgw = list(upper.subnets(new_prefix=28))[-len(azs):]
    return [{"az": az, "work": str(work[i]), "tgw": str(tgw[i])} for i, az in enumerate(azs)]


class Tenant:
    def __init__(self, cfg):
        self.cfg = cfg
        s = session(cfg)
        self.ec2, self.r53 = s.client("ec2"), s.client("route53")
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

    def place_up(self, placement, vni, prefixes):
        c = self.cfg
        edge = shc_aws_edge.Edge(c["edge"])
        tgw_id = edge.tgw()["TransitGatewayId"]
        if not self.ec2.describe_transit_gateways(TransitGatewayIds=[tgw_id])["TransitGateways"]:
            fail("the hub's transit gateway is not shared with account %s" % c["account"])
        extra = dict(c.get("tags", {}), **{"shc-placement": placement, "shc-vni": str(vni)})
        name = "shc-" + placement.replace("/", "-")
        lay = layout(prefixes, c["azs"])
        vpc = self.find_vpc(placement)
        if vpc and vpc["CidrBlock"] != prefixes[0]:
            fail("VPC %s of %s has CIDR %s, want %s" % (vpc["VpcId"], placement, vpc["CidrBlock"], prefixes[0]))
        if not vpc:
            self.changed.append("vpc " + name)
            vpc = self.ec2.create_vpc(CidrBlock=prefixes[0], TagSpecifications=tagspec("vpc", name, extra))["Vpc"]
            self.ec2.get_waiter("vpc_available").wait(VpcIds=[vpc["VpcId"]])
            self.ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsHostnames={"Value": True})
        vpc_id = vpc["VpcId"]
        have_cidrs = {a["CidrBlock"] for a in self.ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]["CidrBlockAssociationSet"]
                      if a["CidrBlockState"]["State"] in ("associated", "associating")}
        for p in prefixes[1:]:
            if p not in have_cidrs:
                self.changed.append("vpc cidr " + p)
                self.ec2.associate_vpc_cidr_block(VpcId=vpc_id, CidrBlock=p)
        subnets = {tagmap(s).get("Name"): s for s in self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]}
        work, tgw_subnets = [], []
        for l in lay:
            for role, cidr in (("work", l["work"]), ("tgw", l["tgw"])):
                sn_name = "%s-%s-%s" % (name, role, l["az"][-1])
                s = subnets.get(sn_name)
                if not s:
                    self.changed.append("subnet " + sn_name)
                    s = self.ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=l["az"],
                                               TagSpecifications=tagspec("subnet", sn_name, dict(extra, **{"shc-role": role})))["Subnet"]
                (work if role == "work" else tgw_subnets).append({"id": s["SubnetId"], "cidr": cidr, "az": l["az"]})
        att = [a for a in self.ec2.describe_transit_gateway_vpc_attachments(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]}])["TransitGatewayVpcAttachments"]
            if a["State"] not in ("deleted", "deleting", "rejected", "failed")]
        if att:
            att_id = att[0]["TransitGatewayAttachmentId"]
        else:
            self.changed.append("tgw attachment " + name)
            att_id = self.ec2.create_transit_gateway_vpc_attachment(
                TransitGatewayId=tgw_id, VpcId=vpc_id, SubnetIds=[s["id"] for s in tgw_subnets],
                TagSpecifications=tagspec("transit-gateway-attachment", name, extra))["TransitGatewayVpcAttachment"]["TransitGatewayAttachmentId"]
        # the hub: the network's route table, Connect attachment and router VRF; accept the attachment into it
        net = edge.net_up(vni)
        for _ in range(30):  # the attachment shows up on the hub side shortly after creation
            try:
                acc = edge.net_attach(vni, att_id)
                break
            except botocore.exceptions.ClientError as e:
                if "NotFound" not in str(e):
                    raise
                time.sleep(10)
        else:
            fail("attachment %s never reached the hub" % att_id)
        self.changed += edge.changed
        # the VPC's default route leaves through the network
        main = self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                       {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
        if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in main["Routes"]):
            for _ in range(30):
                try:
                    self.ec2.create_route(RouteTableId=main["RouteTableId"], DestinationCidrBlock="0.0.0.0/0",
                                          TransitGatewayId=tgw_id)
                    self.changed.append("default route")
                    break
                except botocore.exceptions.ClientError as e:
                    if "InvalidTransitGatewayID" not in str(e) and "IncorrectState" not in str(e):
                        raise
                    time.sleep(10)
        return {"placement": placement, "account": c["account"], "vpcId": vpc_id, "prefixes": prefixes,
                "subnets": work, "attachmentSubnets": tgw_subnets, "attachment": att_id,
                "routeTable": net["routeTable"], "connectAttachment": net["connectAttachment"]}

    def place_down(self, placement, vni):
        c = self.cfg
        edge = shc_aws_edge.Edge(c["edge"])
        vpc = self.find_vpc(placement)
        if vpc:
            vpc_id = vpc["VpcId"]
            left = [i["InstanceId"] for r in self.ec2.describe_instances(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
                for i in r["Instances"]]
            if left:
                fail("VPC %s still has instances %s; the tenant removes its workloads first" % (vpc_id, left),
                     reason="WorkloadsPresent")
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
            for s in self.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]:
                for _ in range(20):  # attachment ENIs linger briefly
                    try:
                        self.ec2.delete_subnet(SubnetId=s["SubnetId"])
                        break
                    except botocore.exceptions.ClientError as e:
                        if "DependencyViolation" not in str(e):
                            raise
                        time.sleep(10)
            for sg in self.ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]:
                if sg["GroupName"] != "default":
                    self.ec2.delete_security_group(GroupId=sg["GroupId"])
            self.ec2.delete_vpc(VpcId=vpc_id)
            self.changed.append("deleted vpc " + vpc_id)
        # the network's hub side goes with its last tenant attachment
        tgw_id = edge.tgw()["TransitGatewayId"]
        others = [a for a in edge.attachments(tgw_id, **{"shc-vni": str(vni)}) if a["ResourceType"] == "vpc"]
        if not others:
            edge.net_down(vni)
            self.changed += edge.changed
        return {"placement": placement, "vni": vni, "networkKept": bool(others)}

    def check(self):
        who = boto3.client("sts").get_caller_identity()["Arn"]
        me = self.ec2.describe_vpcs(MaxResults=5)["Vpcs"]
        return {"account": self.cfg["account"], "caller": who, "vpcs": len(me)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("command", choices=["check", "zone-up", "zone-down", "place-up", "place-down"])
    ap.add_argument("--zone")
    ap.add_argument("--placement")
    ap.add_argument("--vni", type=int)
    ap.add_argument("--prefix", action="append", default=[])
    a = ap.parse_args()
    t = Tenant(json.loads(a.config))
    if a.command.startswith("zone") and not a.zone:
        fail("--zone is required")
    if a.command.startswith("place") and not (a.placement and a.vni):
        fail("--placement and --vni are required")
    r = {"check": t.check,
         "zone-up": lambda: t.zone_up(a.zone), "zone-down": lambda: t.zone_down(a.zone),
         "place-up": lambda: t.place_up(a.placement, a.vni, a.prefix or fail("--prefix is required")),
         "place-down": lambda: t.place_down(a.placement, a.vni)}[a.command]()
    print(json.dumps({"ok": True, "changed": t.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

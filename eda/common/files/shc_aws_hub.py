#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §9): the hub account's network (VPC, subnets, Transit Gateway, its route tables, the RAM
share). Runs with the job's AWS web identity (the management account's automation role) and enters the hub account
through its automation role with external ID shc-<account>. Idempotent (objects found by Name tag, tagged
shc-managed=true). Creates no attachments: attachments are billed per hour and are made only while testing; any it
finds are reported. Prints one JSON line.

  shc_aws_hub.py --config '<json>' [--dry-run]

config: {"account", "region", "memberRole",
         "vpc": {"name", "cidr", "subnets": [{"name", "az", "cidr"}]},
         "tgw": {"name", "asn", "routeTables": [names],
                 "share": {"name", "principals": [arn], "allowExternal": bool}}}
"""
import argparse
import json
import sys
import time

import boto3

TAGS = [{"Key": "shc-managed", "Value": "true"}]


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(1)


def tagspec(kind, name):
    return [{"ResourceType": kind, "Tags": TAGS + [{"Key": "Name", "Value": name}]}]


def by_name(items, name):
    for i in items:
        if {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name") == name:
            return i
    return None


def hub_session(cfg):
    sts = boto3.client("sts")
    c = sts.assume_role(RoleArn="arn:aws:iam::%s:role/%s" % (cfg["account"], cfg.get("memberRole", "shc-automation")),
                        RoleSessionName="shc-hub-network", ExternalId="shc-%s" % cfg["account"])["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=cfg["region"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(a.config)
    s = hub_session(cfg)
    ec2, ram = s.client("ec2"), s.client("ram")
    out = {"ok": True, "account": cfg["account"], "region": cfg["region"], "dryRun": a.dry_run, "changed": []}

    # VPC
    v = cfg["vpc"]
    vpc = by_name(ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [v["name"]]}])["Vpcs"], v["name"])
    if vpc and vpc["CidrBlock"] != v["cidr"]:
        fail("VPC %s exists with CIDR %s, want %s" % (v["name"], vpc["CidrBlock"], v["cidr"]))
    if not vpc:
        out["changed"].append("vpc " + v["name"])
        if a.dry_run:
            print(json.dumps(out)); return
        vpc = ec2.create_vpc(CidrBlock=v["cidr"], TagSpecifications=tagspec("vpc", v["name"]))["Vpc"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc["VpcId"]])
        ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsHostnames={"Value": True})
    vpc_id = vpc["VpcId"]
    out["vpc"] = {"id": vpc_id, "name": v["name"], "cidr": v["cidr"]}

    have = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    out["subnets"] = []
    for sn in v.get("subnets", []):
        cur = by_name(have, sn["name"])
        if cur and (cur["CidrBlock"] != sn["cidr"] or cur["AvailabilityZone"] != sn["az"]):
            fail("subnet %s exists as %s in %s" % (sn["name"], cur["CidrBlock"], cur["AvailabilityZone"]))
        if not cur:
            out["changed"].append("subnet " + sn["name"])
            if not a.dry_run:
                cur = ec2.create_subnet(VpcId=vpc_id, CidrBlock=sn["cidr"], AvailabilityZone=sn["az"],
                                        TagSpecifications=tagspec("subnet", sn["name"]))["Subnet"]
        if cur:
            out["subnets"].append({"name": sn["name"], "id": cur["SubnetId"], "az": sn["az"], "cidr": sn["cidr"]})

    # Transit Gateway (no attachments here)
    t = cfg["tgw"]
    tgws = [g for g in ec2.describe_transit_gateways(Filters=[{"Name": "tag:Name", "Values": [t["name"]]}])["TransitGateways"]
            if g["State"] not in ("deleting", "deleted")]
    tgw = tgws[0] if tgws else None
    if tgw and tgw["Options"]["AmazonSideAsn"] != int(t["asn"]):
        fail("TGW %s exists with ASN %s, want %s" % (t["name"], tgw["Options"]["AmazonSideAsn"], t["asn"]))
    if not tgw:
        out["changed"].append("tgw " + t["name"])
        if a.dry_run:
            print(json.dumps(out)); return
        tgw = ec2.create_transit_gateway(
            Description="SHC hub transit gateway (attachments only while testing)",
            Options={"AmazonSideAsn": int(t["asn"]), "AutoAcceptSharedAttachments": "disable",
                     "DefaultRouteTableAssociation": "disable", "DefaultRouteTablePropagation": "disable",
                     "DnsSupport": "enable", "VpnEcmpSupport": "enable", "MulticastSupport": "disable"},
            TagSpecifications=tagspec("transit-gateway", t["name"]))["TransitGateway"]
    tgw_id = tgw["TransitGatewayId"]
    for _ in range(60):  # a new TGW takes a few minutes
        tgw = ec2.describe_transit_gateways(TransitGatewayIds=[tgw_id])["TransitGateways"][0]
        if tgw["State"] == "available":
            break
        time.sleep(15)
    out["tgw"] = {"id": tgw_id, "arn": tgw["TransitGatewayArn"], "asn": tgw["Options"]["AmazonSideAsn"], "state": tgw["State"]}

    rts = ec2.describe_transit_gateway_route_tables(Filters=[{"Name": "transit-gateway-id", "Values": [tgw_id]}])["TransitGatewayRouteTables"]
    out["routeTables"] = []
    for name in t.get("routeTables", []):
        rt = by_name([r for r in rts if r["State"] not in ("deleting", "deleted")], name)
        if not rt:
            out["changed"].append("tgw route table " + name)
            if not a.dry_run:
                rt = ec2.create_transit_gateway_route_table(TransitGatewayId=tgw_id,
                                                            TagSpecifications=tagspec("transit-gateway-route-table", name))["TransitGatewayRouteTable"]
        if rt:
            out["routeTables"].append({"name": name, "id": rt["TransitGatewayRouteTableId"]})

    # RAM share of the TGW
    sh = t.get("share")
    if sh:
        shares = [x for x in ram.get_resource_shares(resourceOwner="SELF", name=sh["name"])["resourceShares"]
                  if x["status"] not in ("DELETING", "DELETED")]
        share = shares[0] if shares else None
        if not share:
            out["changed"].append("ram share " + sh["name"])
            if not a.dry_run:
                share = ram.create_resource_share(name=sh["name"], resourceArns=[tgw["TransitGatewayArn"]],
                                                  principals=sh.get("principals", []),
                                                  allowExternalPrincipals=bool(sh.get("allowExternal", False)),
                                                  tags=[{"key": "shc-managed", "value": "true"}])["resourceShare"]
        if share:
            arn = share["resourceShareArn"]
            have_p = {p["associatedEntity"] for p in ram.get_resource_share_associations(
                associationType="PRINCIPAL", resourceShareArns=[arn])["resourceShareAssociations"]
                if p["status"] in ("ASSOCIATED", "ASSOCIATING")}
            missing = [p for p in sh.get("principals", []) if p not in have_p]
            if missing:
                out["changed"].append("ram principals " + ",".join(missing))
                if not a.dry_run:
                    ram.associate_resource_share(resourceShareArn=arn, principals=missing)
            out["share"] = {"arn": arn, "principals": sorted(have_p | set(missing)),
                            "allowExternal": share.get("allowExternalPrincipals")}

    # attachments cost money per hour: report them, never create them here
    att = ec2.describe_transit_gateway_attachments(Filters=[{"Name": "transit-gateway-id", "Values": [tgw_id]}])["TransitGatewayAttachments"]
    out["attachments"] = [{"id": x["TransitGatewayAttachmentId"], "type": x["ResourceType"], "owner": x["ResourceOwnerId"],
                           "state": x["State"]} for x in att if x["State"] not in ("deleted", "deleting", "rejected", "failed")]
    print(json.dumps(out))


if __name__ == "__main__":
    main()

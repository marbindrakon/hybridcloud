#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §10): the AWS edge site in the hub account. Edge routers (EC2, one per edge subnet) are
EVPN VTEPs of the fabric: the border gateways dial them over WireGuard. Each network placed in AWS is one VRF on
them, handed to the transit gateway over one Connect attachment (GRE + BGP, a peer per router) whose route table
belongs to that network alone, so tenants stay apart even with overlapping prefixes. Everything here is billed only
while it exists, so the site and its networks are brought up for use and removed afterwards. Prints one JSON line.

  shc_aws_edge.py --config '<json>' <command> [args]
    status                        what exists, the routers' published keys and state
    up                            routers, their network, the transport attachment; writes the routers' config
    replace                       new routers (same addresses and Elastic IPs, new keys), networks re-peered
    down                          everything of the site (networks first)
    gateways --gateways '<json>'  the border gateway nodes [{name, publicKey, address}]; BGP password from env
                                  SHC_EDGE_BGP_PASSWORD (never argv)
    net-up --vni N [--test]       the network's route table, Connect attachment and peers; its VRF on the routers
    net-attach --vni N --attachment ID   accept a tenant VPC attachment into the network's route table
    net-down --vni N              the network's Connect attachment, peers, route table; its VRF on the routers

config: {"account", "region", "memberRole", "entry": "automation" | "org-access", "site", "vpcName", "tgwName",
         "tgwCidr", "edgeSubnets": [names], "transportSubnets": [names], "instanceType", "ami": {"owner", "name"},
         "asn", "tunnel": {"cidr", "mtu", "listenPort", "nodeAddresses": [..]}, "wireguardSources": [cidrs],
         "insideCidrPool", "overlayMtu"}
entry: automation = the job's identity assumes <memberRole> with external ID shc-<account> (AAP); org-access =
the caller's management-account credentials assume OrganizationAccountAccessRole (one-off use by the owner).
"""
import argparse
import base64
import gzip
import ipaddress
import json
import os
import sys
import time

import boto3
import botocore.exceptions

TAGS = [{"Key": "shc-managed", "Value": "true"}]
HERE = os.path.dirname(os.path.abspath(__file__))
# reserved by AWS for other uses inside 169.254.0.0/16 (Connect peer inside CIDRs must avoid them)
RESERVED_INSIDE = [ipaddress.ip_network(n) for n in ("169.254.0.0/29", "169.254.1.0/29", "169.254.2.0/29",
                                                    "169.254.3.0/29", "169.254.4.0/29", "169.254.5.0/29",
                                                    "169.254.169.248/29")]


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(1)


def tags(name, **more):
    return TAGS + [{"Key": "Name", "Value": name}] + [{"Key": k.replace("_", "-"), "Value": str(v)} for k, v in more.items()]


def tagspec(kind, name, **more):
    return [{"ResourceType": kind, "Tags": tags(name, **more)}]


def tagmap(o):
    return {t["Key"]: t["Value"] for t in o.get("Tags", o.get("tags", []))}


def session(cfg):
    sts = boto3.client("sts")
    if cfg.get("entry", "automation") == "org-access":
        kw = {"RoleArn": "arn:aws:iam::%s:role/OrganizationAccountAccessRole" % cfg["account"]}
    else:
        kw = {"RoleArn": "arn:aws:iam::%s:role/%s" % (cfg["account"], cfg.get("memberRole", "shc-automation")),
              "ExternalId": "shc-%s" % cfg["account"]}
    c = sts.assume_role(RoleSessionName="shc-edge-" + cfg["site"], **kw)["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                         aws_session_token=c["SessionToken"], region_name=cfg["region"])


class Edge:
    def __init__(self, cfg):
        # derived settings: router tunnel addresses from the tunnel subnet (host routerHostIndex onwards), the
        # overlay MTU of the site leg (tunnel MTU minus VXLAN)
        t = cfg.setdefault("tunnel", {})
        if not t.get("nodeAddresses"):
            net = ipaddress.ip_network(t["cidr"])
            t["nodeAddresses"] = [str(net[int(t.get("routerHostIndex", 10)) + i]) for i in range(len(cfg["edgeSubnets"]))]
        cfg.setdefault("overlayMtu", int(t.get("mtu", 1420)) - 50)
        self.cfg = cfg
        self.site = cfg["site"]
        s = session(cfg)
        self.ec2, self.iam, self.ssm = s.client("ec2"), s.client("iam"), s.client("ssm")
        self.changed = []
        self.prefix = "shc-edge-" + self.site
        self.param = "/shc/edge/%s/config" % self.site

    # ---- lookups
    def vpc(self):
        v = self.ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [self.cfg["vpcName"]]}])["Vpcs"]
        if not v:
            fail("hub VPC %s not found (run aws-hub-network first)" % self.cfg["vpcName"])
        return v[0]

    def subnets(self, vpc_id, names):
        have = {tagmap(s).get("Name"): s for s in self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]}
        missing = [n for n in names if n not in have]
        if missing:
            fail("hub subnets missing: %s" % ", ".join(missing))
        return [have[n] for n in names]

    def tgw(self):
        g = [x for x in self.ec2.describe_transit_gateways(
            Filters=[{"Name": "tag:Name", "Values": [self.cfg["tgwName"]]}])["TransitGateways"]
             if x["State"] not in ("deleting", "deleted")]
        if not g:
            fail("transit gateway %s not found" % self.cfg["tgwName"])
        return g[0]

    def attachments(self, tgw_id, **filters):
        f = [{"Name": "transit-gateway-id", "Values": [tgw_id]},
             {"Name": "state", "Values": ["pendingAcceptance", "pending", "available", "modifying", "initiating"]}]
        f += [{"Name": "tag:" + k, "Values": [v]} for k, v in filters.items()]
        return self.ec2.describe_transit_gateway_attachments(Filters=f)["TransitGatewayAttachments"]

    def instances(self):
        r = self.ec2.describe_instances(Filters=[{"Name": "tag:shc-edge-site", "Values": [self.site]},
                                                 {"Name": "instance-state-name",
                                                  "Values": ["pending", "running", "stopping", "stopped"]}])
        return sorted([i for res in r["Reservations"] for i in res["Instances"]], key=lambda i: tagmap(i)["Name"])

    def read_config(self):
        try:
            return json.loads(self.ssm.get_parameter(Name=self.param, WithDecryption=True)["Parameter"]["Value"])
        except self.ssm.exceptions.ParameterNotFound:
            return None

    def write_config(self, c):
        old = self.read_config()
        if old == c:
            return
        self.changed.append("config")
        self.ssm.put_parameter(Name=self.param, Type="SecureString", Overwrite=True, Value=json.dumps(c, sort_keys=True))
        if old is None:
            self.ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=self.param, Tags=tags(self.param))

    def node_reports(self):
        out = {}
        for page in self.ssm.get_paginator("get_parameters_by_path").paginate(Path="/shc/edge/%s/nodes" % self.site):
            for p in page["Parameters"]:
                out[p["Name"].rsplit("/", 1)[1]] = json.loads(p["Value"])
        return out

    # ---- site
    def up(self):
        c = self.cfg
        vpc = self.vpc()
        vpc_id = vpc["VpcId"]
        edge_subnets = self.subnets(vpc_id, c["edgeSubnets"])
        transport_subnets = self.subnets(vpc_id, c["transportSubnets"])
        tgw = self.tgw()
        tgw_id = tgw["TransitGatewayId"]

        # the transit gateway's CIDR block (Connect peer addresses; free)
        if c["tgwCidr"] not in tgw["Options"].get("TransitGatewayCidrBlocks", []):
            self.changed.append("tgw cidr " + c["tgwCidr"])
            self.ec2.modify_transit_gateway(TransitGatewayId=tgw_id, Options={"AddTransitGatewayCidrBlocks": [c["tgwCidr"]]})

        # internet gateway and the edge subnets' route table
        igws = self.ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]
        if igws:
            igw_id = igws[0]["InternetGatewayId"]
        else:
            self.changed.append("internet gateway")
            igw_id = self.ec2.create_internet_gateway(TagSpecifications=tagspec("internet-gateway", self.prefix))["InternetGateway"]["InternetGatewayId"]
            self.ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        rts = self.ec2.describe_route_tables(Filters=[{"Name": "tag:Name", "Values": [self.prefix + "-public"]},
                                                      {"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
        if rts:
            rt = rts[0]
        else:
            self.changed.append("route table " + self.prefix + "-public")
            rt = self.ec2.create_route_table(VpcId=vpc_id, TagSpecifications=tagspec("route-table", self.prefix + "-public"))["RouteTable"]
        rt_id = rt["RouteTableId"]
        if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in rt.get("Routes", [])):
            self.ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        for s in edge_subnets:
            cur = [a for t in self.ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": [s["SubnetId"]]}])["RouteTables"]
                   for a in t["Associations"] if a.get("SubnetId") == s["SubnetId"]]
            if not cur:
                self.ec2.associate_route_table(RouteTableId=rt_id, SubnetId=s["SubnetId"])
            elif cur[0]["RouteTableId"] != rt_id:
                self.ec2.replace_route_table_association(AssociationId=cur[0]["RouteTableAssociationId"], RouteTableId=rt_id)

        # security group: WireGuard from the border gateways' public sources, GRE from the transit gateway
        sgs = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [self.prefix]},
                                                         {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
        if sgs:
            sg_id = sgs[0]["GroupId"]
        else:
            self.changed.append("security group " + self.prefix)
            sg_id = self.ec2.create_security_group(GroupName=self.prefix, VpcId=vpc_id,
                                                   Description="SHC edge routers " + self.site,
                                                   TagSpecifications=tagspec("security-group", self.prefix))["GroupId"]
        want = [{"IpProtocol": "udp", "FromPort": c["tunnel"]["listenPort"], "ToPort": c["tunnel"]["listenPort"],
                 "IpRanges": [{"CidrIp": x} for x in c["wireguardSources"]]},
                {"IpProtocol": "47", "IpRanges": [{"CidrIp": c["tgwCidr"]}]},
                # the site's routers mesh with each other over the VPC
                {"IpProtocol": "udp", "FromPort": c["tunnel"]["listenPort"], "ToPort": c["tunnel"]["listenPort"],
                 "UserIdGroupPairs": [{"GroupId": sg_id}]}]
        for perm in want:
            try:
                self.ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

        # the routers' role: read the site's config, publish their own reports
        acct = c["account"]
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "ssm:GetParameter",
             "Resource": "arn:aws:ssm:%s:%s:parameter%s" % (c["region"], acct, self.param)},
            {"Effect": "Allow", "Action": "ssm:PutParameter",
             "Resource": "arn:aws:ssm:%s:%s:parameter/shc/edge/%s/nodes/*" % (c["region"], acct, self.site)},
            {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": "*",
             "Condition": {"StringEquals": {"kms:ViaService": "ssm.%s.amazonaws.com" % c["region"]}}}]}
        try:
            self.iam.get_role(RoleName=self.prefix)
        except self.iam.exceptions.NoSuchEntityException:
            self.changed.append("iam role " + self.prefix)
            self.iam.create_role(RoleName=self.prefix, Tags=tags(self.prefix), Description="SHC edge router " + self.site,
                                 AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                                     "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                     "Action": "sts:AssumeRole"}]}))
        self.iam.put_role_policy(RoleName=self.prefix, PolicyName="shc-edge", PolicyDocument=json.dumps(policy))
        try:
            ip = self.iam.get_instance_profile(InstanceProfileName=self.prefix)["InstanceProfile"]
        except self.iam.exceptions.NoSuchEntityException:
            ip = self.iam.create_instance_profile(InstanceProfileName=self.prefix, Tags=tags(self.prefix))["InstanceProfile"]
        if not any(r["RoleName"] == self.prefix for r in ip.get("Roles", [])):
            self.iam.add_role_to_instance_profile(InstanceProfileName=self.prefix, RoleName=self.prefix)

        # the routers' config: nodes are filled in as instances appear; gateways and VRFs are kept
        conf = self.read_config() or {}
        conf.update({"site": self.site, "asn": int(c["asn"]),
                     "tunnel": {k: c["tunnel"][k] for k in ("cidr", "mtu", "listenPort")}})
        conf.setdefault("nodes", {})
        conf.setdefault("gateways", [])
        conf.setdefault("vrfs", [])

        # the routers: one per edge subnet, each with an Elastic IP
        have = {tagmap(i)["Name"]: i for i in self.instances()}
        ami = None
        for idx, s in enumerate(edge_subnets):
            name = "%s-%s" % (self.prefix, "abcdefgh"[idx])
            inst = have.get(name)
            if not inst:
                if ami is None:
                    imgs = self.ec2.describe_images(Owners=[c["ami"]["owner"]], Filters=[
                        {"Name": "name", "Values": [c["ami"]["name"]]}, {"Name": "state", "Values": ["available"]},
                        {"Name": "architecture", "Values": [c["ami"].get("architecture", "arm64")]}])["Images"]
                    if not imgs:
                        fail("no AMI matches %s" % c["ami"])
                    ami = sorted(imgs, key=lambda i: i["CreationDate"])[-1]
                self.changed.append("instance " + name)
                # a fixed address per subnet: the Connect peers name it, so a replaced router keeps its peers
                fixed = str(ipaddress.ip_network(s["CidrBlock"])[int(c.get("edgeHostIndex", 10))])
                for attempt in range(12):  # a new instance profile takes a few seconds to be usable
                    try:
                        inst = self.ec2.run_instances(
                            ImageId=ami["ImageId"], InstanceType=c["instanceType"], MinCount=1, MaxCount=1,
                            SubnetId=s["SubnetId"], SecurityGroupIds=[sg_id], PrivateIpAddress=fixed,
                            IamInstanceProfile={"Name": self.prefix}, UserData=self.user_data(),
                            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                            BlockDeviceMappings=[{"DeviceName": ami["RootDeviceName"], "Ebs": {"VolumeSize": 10, "VolumeType": "gp3",
                                                                                     "DeleteOnTermination": True}}],
                            TagSpecifications=tagspec("instance", name, shc_edge_site=self.site)
                            + tagspec("volume", name, shc_edge_site=self.site))["Instances"][0]
                        break
                    except botocore.exceptions.ClientError as e:
                        # a new instance profile, or the replaced router's address, takes a little while to free up
                        if not any(x in str(e).lower() for x in ("instance profile", "address is in use")) or attempt == 11:
                            raise
                        time.sleep(10)
            have[name] = inst
            conf["nodes"].setdefault(inst["InstanceId"], {"name": name, "address": c["tunnel"]["nodeAddresses"][idx]})
        ids = [have["%s-%s" % (self.prefix, "abcdefgh"[i])]["InstanceId"] for i in range(len(edge_subnets))]
        conf["nodes"] = {k: v for k, v in conf["nodes"].items() if k in ids}
        self.write_config(conf)
        self.ec2.get_waiter("instance_running").wait(InstanceIds=ids)
        for iid in ids:
            addrs = self.ec2.describe_addresses(Filters=[{"Name": "instance-id", "Values": [iid]}])["Addresses"]
            if not addrs:
                # a replaced router takes its predecessor's Elastic IP (the border gateways' endpoint stays)
                free = [a for a in self.ec2.describe_addresses(Filters=[
                    {"Name": "tag:Name", "Values": [conf["nodes"][iid]["name"]]},
                    {"Name": "tag:shc-edge-site", "Values": [self.site]}])["Addresses"] if "AssociationId" not in a]
                if free:
                    a = free[0]
                    self.changed.append("elastic ip %s (kept) %s" % (a["PublicIp"], iid))
                else:
                    self.changed.append("elastic ip " + iid)
                    a = self.ec2.allocate_address(Domain="vpc", TagSpecifications=tagspec(
                        "elastic-ip", conf["nodes"][iid]["name"], shc_edge_site=self.site))
                self.ec2.associate_address(AllocationId=a["AllocationId"], InstanceId=iid)

        # the transport attachment (billed while it exists) and the route to the transit gateway's CIDR
        tr = self.attachments(tgw_id, **{"shc-edge-site": self.site, "shc-role": "transport"})
        if tr:
            tr_id = tr[0]["TransitGatewayAttachmentId"]
        else:
            self.changed.append("transport attachment")
            tr_id = self.ec2.create_transit_gateway_vpc_attachment(
                TransitGatewayId=tgw_id, VpcId=vpc_id, SubnetIds=[s["SubnetId"] for s in transport_subnets],
                Options={"DnsSupport": "enable"},
                TagSpecifications=tagspec("transit-gateway-attachment", self.prefix + "-transport",
                                          shc_edge_site=self.site, shc_role="transport"))["TransitGatewayVpcAttachment"]["TransitGatewayAttachmentId"]
        self.wait_attachment(tr_id)
        rt = self.ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
        if not any(r.get("DestinationCidrBlock") == c["tgwCidr"] for r in rt["Routes"]):
            self.ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock=c["tgwCidr"], TransitGatewayId=tgw_id)
        self.sync_nodes(wait=True)
        return self.status()

    def sync_nodes(self, wait=False, tries=24):
        """Put each router's published public key and VPC address into the site config (the mesh between them).
        With wait, give new routers a few minutes to publish their keys."""
        for i in range(tries if wait else 1):
            conf = self.read_config() or {}
            reports = self.node_reports()
            insts = {x["InstanceId"]: x for x in self.instances()}
            missing = [k for k in conf.get("nodes", {}) if not (reports.get(k) or {}).get("publicKey")]
            if not missing or i == (tries if wait else 1) - 1:
                break
            time.sleep(10)
        for k, n in conf.get("nodes", {}).items():
            if (reports.get(k) or {}).get("publicKey") and k in insts:
                n["publicKey"] = reports[k]["publicKey"]
                n["underlay"] = insts[k]["PrivateIpAddress"]
        self.write_config(conf)
        return missing

    def wait_attachment(self, att_id, states=("available",), tries=80):
        for _ in range(tries):
            a = self.ec2.describe_transit_gateway_attachments(TransitGatewayAttachmentIds=[att_id])["TransitGatewayAttachments"][0]
            if a["State"] in states:
                return a
            if a["State"] in ("failed", "rejected", "deleted"):
                fail("attachment %s is %s" % (att_id, a["State"]))
            time.sleep(15)
        fail("attachment %s not %s after waiting" % (att_id, "/".join(states)))

    def user_data(self):
        agent = base64.b64encode(gzip.compress(open(os.path.join(HERE, "shc_edge_agent.py"), "rb").read())).decode()
        return """#!/bin/bash
# shc edge router %(site)s: FRR + WireGuard; the agent pulls its config from Parameter Store every 30 s
set -euo pipefail
install -d -m 0700 /etc/shc-edge
echo %(site)s > /etc/shc-edge/site; echo %(region)s > /etc/shc-edge/region
dnf -y install frr wireguard-tools nftables
sed -i 's/^bgpd=no/bgpd=yes/; s/^bfdd=no/bfdd=yes/' /etc/frr/daemons
cat > /etc/sysctl.d/90-shc-edge.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.tcp_l3mdev_accept = 1
net.ipv4.udp_l3mdev_accept = 1
EOF
sysctl --system >/dev/null
cat > /etc/nftables/shc-edge.nft <<'EOF'
table inet shc_edge {
  chain forward {
    type filter hook forward priority mangle; policy accept;
    tcp flags syn tcp option maxseg size set rt mtu
  }
}
EOF
grep -q shc-edge.nft /etc/sysconfig/nftables.conf || echo 'include "/etc/nftables/shc-edge.nft"' >> /etc/sysconfig/nftables.conf
systemctl enable --now nftables
echo '%(agent)s' | base64 -d | gunzip > /usr/local/sbin/shc-edge-agent
chmod 0755 /usr/local/sbin/shc-edge-agent
cat > /etc/systemd/system/shc-edge-agent.service <<'EOF'
[Unit]
Description=SHC edge router agent (config from Parameter Store)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/shc-edge-agent
EOF
cat > /etc/systemd/system/shc-edge-agent.timer <<'EOF'
[Unit]
Description=SHC edge router agent every 30 s
[Timer]
OnBootSec=5
OnUnitActiveSec=30
AccuracySec=5
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now shc-edge-agent.timer
""" % {"site": self.site, "region": self.cfg["region"], "agent": agent}

    def down(self):
        conf = self.read_config() or {}
        for v in list(conf.get("vrfs", [])):
            self.net_down(int(v["vni"]))
        tgw = self.tgw()
        vpc_id = self.vpc()["VpcId"]
        for a in self.attachments(tgw["TransitGatewayId"], **{"shc-edge-site": self.site}):
            if a["ResourceType"] == "connect":
                self.net_down(int(tagmap(a).get("shc-vni", "0")))
        insts = self.instances()
        for i in insts:
            for a in self.ec2.describe_addresses(Filters=[{"Name": "instance-id", "Values": [i["InstanceId"]]}])["Addresses"]:
                self.ec2.disassociate_address(AssociationId=a["AssociationId"])
                self.ec2.release_address(AllocationId=a["AllocationId"])
                self.changed.append("released " + a["PublicIp"])
        for a in self.ec2.describe_addresses(Filters=[{"Name": "tag:shc-edge-site", "Values": [self.site]}])["Addresses"]:
            if "AssociationId" not in a:
                self.ec2.release_address(AllocationId=a["AllocationId"])
                self.changed.append("released " + a["PublicIp"])
        if insts:
            ids = [i["InstanceId"] for i in insts]
            self.ec2.terminate_instances(InstanceIds=ids)
            self.changed += ["terminated " + x for x in ids]
            self.ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
        for a in self.attachments(tgw["TransitGatewayId"], **{"shc-edge-site": self.site, "shc-role": "transport"}):
            self.ec2.delete_transit_gateway_vpc_attachment(TransitGatewayAttachmentId=a["TransitGatewayAttachmentId"])
            self.changed.append("deleted transport attachment " + a["TransitGatewayAttachmentId"])
            self.wait_attachment(a["TransitGatewayAttachmentId"], states=("deleted",))
        rts = self.ec2.describe_route_tables(Filters=[{"Name": "tag:Name", "Values": [self.prefix + "-public"]},
                                                      {"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
        for rt in rts:
            for a in rt.get("Associations", []):
                if not a.get("Main"):
                    self.ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
            self.ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
            self.changed.append("deleted route table " + rt["RouteTableId"])
        for igw in self.ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]:
            if tagmap(igw).get("Name") == self.prefix:
                self.ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
                self.ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
                self.changed.append("deleted internet gateway " + igw["InternetGatewayId"])
        for sg in self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [self.prefix]},
                                                             {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]:
            for attempt in range(20):  # ENIs of terminated instances linger briefly
                try:
                    self.ec2.delete_security_group(GroupId=sg["GroupId"])
                    self.changed.append("deleted security group " + sg["GroupId"])
                    break
                except botocore.exceptions.ClientError as e:
                    if e.response["Error"]["Code"] != "DependencyViolation" or attempt == 19:
                        raise
                    time.sleep(10)
        try:
            ip = self.iam.get_instance_profile(InstanceProfileName=self.prefix)["InstanceProfile"]
            for r in ip.get("Roles", []):
                self.iam.remove_role_from_instance_profile(InstanceProfileName=self.prefix, RoleName=r["RoleName"])
            self.iam.delete_instance_profile(InstanceProfileName=self.prefix)
            self.changed.append("deleted instance profile")
        except self.iam.exceptions.NoSuchEntityException:
            pass
        try:
            for p in self.iam.list_role_policies(RoleName=self.prefix)["PolicyNames"]:
                self.iam.delete_role_policy(RoleName=self.prefix, PolicyName=p)
            self.iam.delete_role(RoleName=self.prefix)
            self.changed.append("deleted iam role")
        except self.iam.exceptions.NoSuchEntityException:
            pass
        names = [self.param] + ["/shc/edge/%s/nodes/%s" % (self.site, k) for k in self.node_reports()]
        for n in names:
            try:
                self.ssm.delete_parameter(Name=n)
                self.changed.append("deleted " + n)
            except self.ssm.exceptions.ParameterNotFound:
                pass
        return self.status()

    def replace(self):
        """Replace the routers (a new agent or image): terminate them, then up (same addresses and Elastic IPs; new
        WireGuard keys, so the CloudGateway's peers need the new keys); then net-up per network."""
        insts = self.instances()
        if insts:
            ids = [i["InstanceId"] for i in insts]
            self.ec2.terminate_instances(InstanceIds=ids)
            self.changed += ["terminated " + x for x in ids]
            self.ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
        for k in list(self.node_reports()):
            self.ssm.delete_parameter(Name="/shc/edge/%s/nodes/%s" % (self.site, k))
        r = self.up()
        conf = self.read_config() or {}
        for v in conf.get("vrfs", []):
            self.net_up(int(v["vni"]), bool(v.get("testAddress")))
        return r

    def set_gateways(self, gateways):
        conf = self.read_config()
        if conf is None:
            fail("site %s has no config yet (run up first)" % self.site)
        conf["gateways"] = [{"name": g["name"], "publicKey": g["publicKey"], "address": g["address"]} for g in gateways]
        pw = os.environ.get("SHC_EDGE_BGP_PASSWORD", "")
        if pw:
            conf["bgpPassword"] = pw
        else:
            conf.pop("bgpPassword", None)
        self.write_config(conf)
        return self.status()

    # ---- networks
    def net_names(self, vni):
        return "shc-net-%d" % vni

    def net_up(self, vni, test=False):
        c = self.cfg
        tgw = self.tgw()
        tgw_id = tgw["TransitGatewayId"]
        name = self.net_names(vni)
        conf = self.read_config()
        if conf is None or not conf.get("nodes"):
            fail("site %s is not up" % self.site)
        tr = self.attachments(tgw_id, **{"shc-edge-site": self.site, "shc-role": "transport"})
        if not tr:
            fail("site %s has no transport attachment (run up first)" % self.site)
        # the network's route table
        rts = [r for r in self.ec2.describe_transit_gateway_route_tables(Filters=[
            {"Name": "transit-gateway-id", "Values": [tgw_id]}, {"Name": "tag:Name", "Values": [name]}])["TransitGatewayRouteTables"]
            if r["State"] not in ("deleting", "deleted")]
        if rts:
            rt_id = rts[0]["TransitGatewayRouteTableId"]
        else:
            self.changed.append("tgw route table " + name)
            rt_id = self.ec2.create_transit_gateway_route_table(TransitGatewayId=tgw_id, TagSpecifications=tagspec(
                "transit-gateway-route-table", name, shc_edge_site=self.site, shc_vni=vni))["TransitGatewayRouteTable"]["TransitGatewayRouteTableId"]
            for _ in range(40):
                if self.ec2.describe_transit_gateway_route_tables(TransitGatewayRouteTableIds=[rt_id])["TransitGatewayRouteTables"][0]["State"] == "available":
                    break
                time.sleep(5)
        # the Connect attachment (billed while it exists)
        ca = self.attachments(tgw_id, **{"shc-edge-site": self.site, "shc-vni": str(vni)})
        ca = [a for a in ca if a["ResourceType"] == "connect"]
        if ca:
            ca_id = ca[0]["TransitGatewayAttachmentId"]
        else:
            self.changed.append("connect attachment " + name)
            ca_id = self.ec2.create_transit_gateway_connect(
                TransportTransitGatewayAttachmentId=tr[0]["TransitGatewayAttachmentId"], Options={"Protocol": "gre"},
                TagSpecifications=tagspec("transit-gateway-attachment", name, shc_edge_site=self.site, shc_vni=vni)
            )["TransitGatewayConnect"]["TransitGatewayAttachmentId"]
        self.wait_attachment(ca_id)
        self.associate(rt_id, ca_id)
        # one Connect peer per router; inside /29s from the pool, clear of AWS's reserved ones and of other peers
        peers = [p for p in self.ec2.describe_transit_gateway_connect_peers(Filters=[
            {"Name": "transit-gateway-attachment-id", "Values": [ca_id]}])["TransitGatewayConnectPeers"]
            if p["State"] not in ("deleting", "deleted")]
        used = set()
        for p in self.ec2.describe_transit_gateway_connect_peers()["TransitGatewayConnectPeers"]:
            if p["State"] not in ("deleting", "deleted"):
                used |= {ipaddress.ip_network(x) for x in p["ConnectPeerConfiguration"]["InsideCidrBlocks"]}
        pool = (n for n in ipaddress.ip_network(c["insideCidrPool"]).subnets(new_prefix=29)
                if not any(n.overlaps(r) for r in RESERVED_INSIDE + list(used)))
        insts = {i["InstanceId"]: i for i in self.instances()}
        # peers of routers that are gone (their address isn't a current router's): remove them first
        current = {insts[i]["PrivateIpAddress"] for i in conf["nodes"] if i in insts}
        for p in [p for p in peers if p["ConnectPeerConfiguration"]["PeerAddress"] not in current]:
            self.ec2.delete_transit_gateway_connect_peer(TransitGatewayConnectPeerId=p["TransitGatewayConnectPeerId"])
            self.changed.append("deleted stale connect peer " + p["TransitGatewayConnectPeerId"])
        peers = [p for p in peers if p["ConnectPeerConfiguration"]["PeerAddress"] in current]
        pool = (n for n in ipaddress.ip_network(c["insideCidrPool"]).subnets(new_prefix=29)
                if not any(n.overlaps(r) for r in RESERVED_INSIDE + list(used)))
        by_peer = {p["ConnectPeerConfiguration"]["PeerAddress"]: p for p in peers}
        connect = {}
        for iid in conf["nodes"]:
            inst = insts.get(iid)
            if not inst:
                continue
            pa = inst["PrivateIpAddress"]
            p = by_peer.get(pa)
            if not p:
                self.changed.append("connect peer " + conf["nodes"][iid]["name"])
                p = self.ec2.create_transit_gateway_connect_peer(
                    TransitGatewayAttachmentId=ca_id, PeerAddress=pa, BgpOptions={"PeerAsn": int(c["asn"])},
                    InsideCidrBlocks=[str(next(pool))],
                    TagSpecifications=tagspec("transit-gateway-connect-peer", "%s-%s" % (name, conf["nodes"][iid]["name"]),
                                              shc_edge_site=self.site, shc_vni=vni))["TransitGatewayConnectPeer"]
            for _ in range(30):  # the peer's BGP addresses appear shortly after creation
                cpc = p["ConnectPeerConfiguration"]
                bgp = cpc.get("BgpConfigurations") or []
                if bgp:
                    break
                time.sleep(5)
                p = self.ec2.describe_transit_gateway_connect_peers(
                    TransitGatewayConnectPeerIds=[p["TransitGatewayConnectPeerId"]])["TransitGatewayConnectPeers"][0]
            if not bgp:
                fail("connect peer %s has no BGP addresses" % p["TransitGatewayConnectPeerId"])
            plen = ipaddress.ip_network(cpc["InsideCidrBlocks"][0]).prefixlen
            connect[iid] = {"local": pa, "remote": cpc["TransitGatewayAddress"],
                            "inside": "%s/%d" % (bgp[0]["PeerAddress"], plen),
                            "bgp": sorted({b["TransitGatewayAddress"] for b in bgp}), "peerId": p["TransitGatewayConnectPeerId"]}
        # the VRF on the routers
        vrf = {"name": "t%d" % vni, "vni": vni, "mtu": int(c["overlayMtu"]), "tgwAsn": int(tgw["Options"]["AmazonSideAsn"]),
               "connect": {k: {x: v[x] for x in ("local", "remote", "inside", "bgp")} for k, v in connect.items()}}
        if test:
            base = ipaddress.ip_address(c.get("testBase", "10.252.0.1"))
            vrf["testAddress"] = {iid: str(base + i) for i, iid in enumerate(sorted(conf["nodes"], key=lambda k: conf["nodes"][k]["name"]))}
        conf["vrfs"] = sorted([v for v in conf["vrfs"] if v["vni"] != vni] + [vrf], key=lambda v: v["vni"])
        self.write_config(conf)
        return {"vni": vni, "routeTable": rt_id, "connectAttachment": ca_id, "peers": connect}

    def association(self, att_id):
        """The route table an attachment is associated with (the route tables' own lists: DescribeTransitGateway-
        Attachments leaves Association empty for some attachment types)."""
        tgw_id = self.tgw()["TransitGatewayId"]
        for rt in self.ec2.describe_transit_gateway_route_tables(Filters=[
                {"Name": "transit-gateway-id", "Values": [tgw_id]}])["TransitGatewayRouteTables"]:
            if rt["State"] in ("deleting", "deleted"):
                continue
            for a in self.ec2.get_transit_gateway_route_table_associations(
                    TransitGatewayRouteTableId=rt["TransitGatewayRouteTableId"])["Associations"]:
                if a["TransitGatewayAttachmentId"] == att_id and a["State"] in ("associated", "associating"):
                    return rt["TransitGatewayRouteTableId"]
        return None

    def associate(self, rt_id, att_id):
        cur = self.association(att_id)
        if cur != rt_id:
            if cur:
                self.ec2.disassociate_transit_gateway_route_table(TransitGatewayRouteTableId=cur, TransitGatewayAttachmentId=att_id)
                self.changed.append("disassociated %s from %s" % (att_id, cur))
                for _ in range(30):
                    if self.association(att_id) is None:
                        break
                    time.sleep(5)
            self.ec2.associate_transit_gateway_route_table(TransitGatewayRouteTableId=rt_id, TransitGatewayAttachmentId=att_id)
            self.changed.append("associated %s" % att_id)
        props = self.ec2.get_transit_gateway_route_table_propagations(TransitGatewayRouteTableId=rt_id)["TransitGatewayRouteTablePropagations"]
        if not any(p["TransitGatewayAttachmentId"] == att_id and p["State"] in ("enabled", "enabling") for p in props):
            self.ec2.enable_transit_gateway_route_table_propagation(TransitGatewayRouteTableId=rt_id, TransitGatewayAttachmentId=att_id)
            self.changed.append("propagating %s" % att_id)

    def net_attach(self, vni, att_id):
        tgw_id = self.tgw()["TransitGatewayId"]
        rts = self.ec2.describe_transit_gateway_route_tables(Filters=[
            {"Name": "transit-gateway-id", "Values": [tgw_id]}, {"Name": "tag:Name", "Values": [self.net_names(vni)]},
            {"Name": "state", "Values": ["available"]}])["TransitGatewayRouteTables"]
        if not rts:
            fail("network %d is not up" % vni)
        a = self.ec2.describe_transit_gateway_attachments(TransitGatewayAttachmentIds=[att_id])["TransitGatewayAttachments"][0]
        if a["TransitGatewayId"] != tgw_id or a["ResourceType"] != "vpc":
            fail("%s is not a VPC attachment to %s" % (att_id, tgw_id))
        if a["State"] == "pendingAcceptance":
            self.ec2.accept_transit_gateway_vpc_attachment(TransitGatewayAttachmentId=att_id)
            self.changed.append("accepted " + att_id)
        self.wait_attachment(att_id)
        try:  # the hub's own tags on the tenant's attachment (lets down/status find it)
            self.ec2.create_tags(Resources=[att_id], Tags=[{"Key": "shc-edge-site", "Value": self.site},
                                                           {"Key": "shc-vni", "Value": str(vni)},
                                                           {"Key": "shc-role", "Value": "tenant"}])
        except botocore.exceptions.ClientError as e:
            print("tagging %s: %s" % (att_id, e), file=sys.stderr)
        self.associate(rts[0]["TransitGatewayRouteTableId"], att_id)
        return {"vni": vni, "attachment": att_id, "owner": a["ResourceOwnerId"]}

    def net_down(self, vni):
        tgw_id = self.tgw()["TransitGatewayId"]
        name = self.net_names(vni)
        conf = self.read_config()
        if conf and any(v["vni"] == vni for v in conf.get("vrfs", [])):
            conf["vrfs"] = [v for v in conf["vrfs"] if v["vni"] != vni]
            self.write_config(conf)
        rts = [r for r in self.ec2.describe_transit_gateway_route_tables(Filters=[
            {"Name": "transit-gateway-id", "Values": [tgw_id]}, {"Name": "tag:Name", "Values": [name]}])["TransitGatewayRouteTables"]
            if r["State"] not in ("deleting", "deleted")]
        for a in self.attachments(tgw_id, **{"shc-edge-site": self.site, "shc-vni": str(vni)}):
            if a["ResourceType"] == "connect":
                for p in self.ec2.describe_transit_gateway_connect_peers(Filters=[
                        {"Name": "transit-gateway-attachment-id", "Values": [a["TransitGatewayAttachmentId"]]}])["TransitGatewayConnectPeers"]:
                    if p["State"] not in ("deleting", "deleted"):
                        self.ec2.delete_transit_gateway_connect_peer(TransitGatewayConnectPeerId=p["TransitGatewayConnectPeerId"])
                        self.changed.append("deleted connect peer " + p["TransitGatewayConnectPeerId"])
                for _ in range(60):
                    left = [p for p in self.ec2.describe_transit_gateway_connect_peers(Filters=[
                        {"Name": "transit-gateway-attachment-id", "Values": [a["TransitGatewayAttachmentId"]]}])["TransitGatewayConnectPeers"]
                        if p["State"] != "deleted"]
                    if not left:
                        break
                    time.sleep(10)
                self.ec2.delete_transit_gateway_connect(TransitGatewayAttachmentId=a["TransitGatewayAttachmentId"])
                self.changed.append("deleted connect attachment " + a["TransitGatewayAttachmentId"])
                self.wait_attachment(a["TransitGatewayAttachmentId"], states=("deleted",))
            elif rts and self.association(a["TransitGatewayAttachmentId"]) == rts[0]["TransitGatewayRouteTableId"]:
                # a tenant's attachment: let go of it, the tenant's side deletes it
                rt_id = rts[0]["TransitGatewayRouteTableId"]
                try:
                    self.ec2.disable_transit_gateway_route_table_propagation(TransitGatewayRouteTableId=rt_id,
                                                                             TransitGatewayAttachmentId=a["TransitGatewayAttachmentId"])
                except botocore.exceptions.ClientError:
                    pass
                self.ec2.disassociate_transit_gateway_route_table(TransitGatewayRouteTableId=rt_id,
                                                                  TransitGatewayAttachmentId=a["TransitGatewayAttachmentId"])
                self.changed.append("released tenant attachment " + a["TransitGatewayAttachmentId"])
        for rt in rts:
            for _ in range(30):
                try:
                    self.ec2.delete_transit_gateway_route_table(TransitGatewayRouteTableId=rt["TransitGatewayRouteTableId"])
                    self.changed.append("deleted tgw route table " + rt["TransitGatewayRouteTableId"])
                    break
                except botocore.exceptions.ClientError as e:
                    if "IncorrectState" not in e.response["Error"]["Code"] and "DependencyViolation" not in e.response["Error"]["Code"]:
                        raise
                    time.sleep(10)
        return {"vni": vni}

    # ---- status
    def status(self):
        tgw = self.tgw()
        out = {"site": self.site, "instances": [], "attachments": [], "nodes": {}}
        for i in self.instances():
            addr = self.ec2.describe_addresses(Filters=[{"Name": "instance-id", "Values": [i["InstanceId"]]}])["Addresses"]
            out["instances"].append({"id": i["InstanceId"], "name": tagmap(i)["Name"], "state": i["State"]["Name"],
                                     "type": i["InstanceType"], "privateIp": i.get("PrivateIpAddress"),
                                     "publicIp": addr[0]["PublicIp"] if addr else None})
        for a in self.attachments(tgw["TransitGatewayId"]):
            out["attachments"].append({"id": a["TransitGatewayAttachmentId"], "type": a["ResourceType"],
                                       "owner": a["ResourceOwnerId"], "state": a["State"], "name": tagmap(a).get("Name"),
                                       "routeTable": self.association(a["TransitGatewayAttachmentId"])})
        conf = self.read_config() or {}
        out["vrfs"] = [{k: v[k] for k in ("name", "vni")} for v in conf.get("vrfs", [])]
        out["gateways"] = [g["name"] for g in conf.get("gateways", [])]
        out["tunnelAddresses"] = {k: v["address"] for k, v in conf.get("nodes", {}).items()}
        out["nodes"] = self.node_reports()
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("command", choices=["status", "up", "down", "replace", "gateways", "net-up", "net-attach", "net-down"])
    ap.add_argument("--gateways")
    ap.add_argument("--vni", type=int)
    ap.add_argument("--attachment")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    e = Edge(json.loads(a.config))
    if a.command in ("net-up", "net-attach", "net-down") and not a.vni:
        fail("--vni is required")
    r = {"status": e.status, "up": e.up, "down": e.down, "replace": e.replace,
         "gateways": lambda: e.set_gateways(json.loads(a.gateways or "[]")),
         "net-up": lambda: e.net_up(a.vni, a.test), "net-attach": lambda: e.net_attach(a.vni, a.attachment),
         "net-down": lambda: e.net_down(a.vni)}[a.command]()
    print(json.dumps({"ok": True, "changed": e.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.3): the hub project's network for the edge routers: the VPC (custom mode, MTU 1500
so WireGuard at 1420 leaves unfragmented), the edge subnet in the site's region and the firewall rules (WireGuard
from the border gateways' public sources, IPsec from the tenant VPCs' HA VPN gateways, the router mesh). Free while
no instance exists. Runs with the platform identity. Idempotent; prints one JSON line.

  shc_gcp_hub.py --config '<json>' [--dry-run]

config: {"project", "region", "vpcName", "subnetName", "subnetCidr", "networkTag", "wireguardSources": [cidr],
         "tunnel": {"listenPort"}}
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shc_gcp_api as g  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = json.loads(a.config)
    proj, region = c["project"], c["region"]
    base = "%s/projects/%s" % (g.COMPUTE, proj)
    tok = g.Identity().platform()
    out = {"ok": True, "project": proj, "region": region, "dryRun": a.dry_run, "changed": []}
    try:
        net = g.http("GET", "%s/global/networks/%s" % (base, c["vpcName"]), token=tok, ok404=True)
        if net and net.get("mtu") != 1500:
            g.fail("VPC %s exists with MTU %s, want 1500" % (c["vpcName"], net.get("mtu")))
        if not net:
            out["changed"].append("vpc " + c["vpcName"])
            if not a.dry_run:
                g.wait_compute(g.http("POST", "%s/global/networks" % base, {
                    "name": c["vpcName"], "autoCreateSubnetworks": False, "mtu": 1500,
                    "routingConfig": {"routingMode": "REGIONAL"},
                    "description": "SHC hub: edge router transit"}, token=tok), tok)
                net = g.http("GET", "%s/global/networks/%s" % (base, c["vpcName"]), token=tok)
        out["vpc"] = {"name": c["vpcName"], "id": (net or {}).get("id"), "selfLink": (net or {}).get("selfLink"), "mtu": 1500}
        sn = g.http("GET", "%s/regions/%s/subnetworks/%s" % (base, region, c["subnetName"]), token=tok, ok404=True)
        if sn and sn.get("ipCidrRange") != c["subnetCidr"]:
            g.fail("subnet %s exists as %s, want %s" % (c["subnetName"], sn.get("ipCidrRange"), c["subnetCidr"]))
        if not sn:
            out["changed"].append("subnet " + c["subnetName"])
            if not a.dry_run and net:
                g.wait_compute(g.http("POST", "%s/regions/%s/subnetworks" % (base, region), {
                    "name": c["subnetName"], "network": net["selfLink"], "ipCidrRange": c["subnetCidr"],
                    "description": "SHC edge routers"}, token=tok), tok)
                sn = g.http("GET", "%s/regions/%s/subnetworks/%s" % (base, region, c["subnetName"]), token=tok)
        out["subnet"] = {"name": c["subnetName"], "cidr": c["subnetCidr"], "selfLink": (sn or {}).get("selfLink")}
        tag, port = c.get("networkTag", "shc-edge"), str(c.get("tunnel", {}).get("listenPort", 51820))
        rules = {
            "shc-edge-wireguard": {"sourceRanges": c["wireguardSources"], "allowed": [{"IPProtocol": "udp", "ports": [port]}],
                                   "description": "WireGuard from the border gateways' public sources"},
            "shc-edge-ipsec": {"sourceRanges": ["0.0.0.0/0"], "allowed": [{"IPProtocol": "udp", "ports": ["500", "4500"]},
                                                                        {"IPProtocol": "esp"}],
                               "description": "HA VPN from tenant VPCs (IKE authenticates)"},
            "shc-edge-mesh": {"sourceTags": [tag], "allowed": [{"IPProtocol": "udp", "ports": [port]}, {"IPProtocol": "icmp"}],
                              "description": "Router mesh"}}
        out["firewall"] = []
        for name, want in rules.items():
            have = g.http("GET", "%s/global/firewalls/%s" % (base, name), token=tok, ok404=True)
            if not have:
                out["changed"].append("firewall " + name)
                if not a.dry_run and net:
                    body = dict(want, name=name, network=net["selfLink"], direction="INGRESS", targetTags=[tag])
                    g.wait_compute(g.http("POST", "%s/global/firewalls" % base, body, token=tok), tok)
            out["firewall"].append(name)
    except g.GcpError as e:
        g.fail(str(e), code=e.code)
    print(json.dumps(out))


if __name__ == "__main__":
    main()

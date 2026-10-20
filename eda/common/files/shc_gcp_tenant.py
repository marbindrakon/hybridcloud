#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.5): a tenant environment's resources in its Google Cloud project, entered from the
platform identity by impersonating the project's member account. Idempotent; prints one JSON line.

  shc_gcp_tenant.py --config '<json>' check                              the project is usable (label, APIs)
  shc_gcp_tenant.py --config '<json>' zone-up --zone FQDN                the environment's Cloud DNS public zone
  shc_gcp_tenant.py --config '<json>' zone-down --zone FQDN
  shc_gcp_tenant.py --config '<json>' place-up --placement NAME --vni N --prefixes '[cidr]' --edge '<json>' [--egress fabric|cloudNat] [--known-peers '<json>']
      the placement's VPC, subnet, Cloud Router, HA VPN gateway and tunnels to the site's routers; prints the
      routers' tunnel parameters (PSKs go to SHC_EDGE_VPN_TUNNELS_FILE, never to stdout)
  shc_gcp_tenant.py --config '<json>' place-down --placement NAME
  shc_gcp_tenant.py --config '<json>' place-status --placement NAME

config: {"project", "region", "labels": {..}, "vpcMtu", "linkLocalPool", "cloudRouterAsn"}
--edge: {"site", "asn", "routers": [{"name", "publicIp"}]}
"""
import argparse
import ipaddress
import json
import os
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shc_gcp_api as g  # noqa: E402

DNS = "https://dns.googleapis.com/dns/v1"
NEEDED_APIS = ["compute.googleapis.com", "dns.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
               "secretmanager.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com"]


def fail(msg, **extra):
    g.fail(msg, **extra)


def zone_name(fqdn):
    return "shc-" + fqdn.rstrip(".").replace(".", "-")[:55]


class Tenant:
    def __init__(self, cfg):
        self.cfg = cfg
        self.project, self.region = cfg["project"], cfg.get("region", "")
        self.base = "%s/projects/%s" % (g.COMPUTE, self.project)
        self.tok = g.Identity().project(self.project)
        self.changed = []
        self.labels = dict({"shc-managed": "true"}, **(cfg.get("labels") or {}))

    def get(self, path, ok404=True):
        return g.http("GET", self.base + path, token=self.tok, ok404=ok404)

    def op(self, method, path, body=None, timeout=600):
        return g.wait_compute(g.http(method, self.base + path, body, token=self.tok), self.tok, timeout)

    # ---- the project
    def check(self):
        p = g.http("GET", "%s/projects/%s" % (g.CRM, self.project), token=self.tok)
        if p.get("labels", {}).get("shc-managed") != "true":
            fail("project %s is not labelled shc-managed=true" % self.project, project=self.project)
        enabled = {s["config"]["name"] for s in g.list_all("%s/projects/%s/services" % (g.SERVICEUSAGE, self.project),
                                                           "services", self.tok, {"filter": "state:ENABLED", "pageSize": 200})}
        missing = [s for s in NEEDED_APIS if s not in enabled]
        if missing:
            self.changed += ["api " + s for s in g.enable_services(self.project, NEEDED_APIS, self.tok)]
        return {"project": self.project, "projectNumber": (p.get("name") or "").split("/")[-1], "state": p.get("state"),
                "principal": g.tokeninfo(self.tok).get("email", "?"), "apisEnabled": missing}

    # ---- the environment's zone
    def find_zone(self, fqdn):
        name = fqdn.rstrip(".") + "."
        for z in g.list_all("%s/projects/%s/managedZones" % (DNS, self.project), "managedZones", self.tok):
            if z["dnsName"] == name and z.get("visibility", "public") == "public":
                return z
        return None

    def zone_up(self, fqdn):
        z = self.find_zone(fqdn)
        if not z:
            self.changed.append("zone " + fqdn)
            z = g.http("POST", "%s/projects/%s/managedZones" % (DNS, self.project), {
                "name": zone_name(fqdn), "dnsName": fqdn.rstrip(".") + ".", "description": "SHC environment zone",
                "visibility": "public", "labels": self.labels}, token=self.tok)
        # keep negative answers short-lived in resolvers (names appear all the time: clusters' API and ingress)
        rrs = g.list_all("%s/projects/%s/managedZones/%s/rrsets" % (DNS, self.project, z["name"]), "rrsets", self.tok,
                         {"name": z["dnsName"], "type": "SOA"})
        if rrs:
            f = rrs[0]["rrdatas"][0].split()
            if rrs[0].get("ttl") != 60 or f[6] != "60":
                f[6] = "60"
                g.http("PATCH", "%s/projects/%s/managedZones/%s/rrsets/%s/SOA" % (DNS, self.project, z["name"], z["dnsName"]),
                       {"name": z["dnsName"], "type": "SOA", "ttl": 60, "rrdatas": [" ".join(f)]}, token=self.tok)
                self.changed.append("zone %s negative TTL 60 s" % fqdn)
        return {"zone": fqdn, "zoneId": z["name"], "nameServers": sorted(z["nameServers"])}

    def zone_down(self, fqdn):
        z = self.find_zone(fqdn)
        if not z:
            return {"zone": fqdn, "deleted": False}
        rrs = g.list_all("%s/projects/%s/managedZones/%s/rrsets" % (DNS, self.project, z["name"]), "rrsets", self.tok)
        dele = [r for r in rrs if not (r["type"] in ("NS", "SOA") and r["name"] == z["dnsName"])]
        if dele:
            g.http("POST", "%s/projects/%s/managedZones/%s/changes" % (DNS, self.project, z["name"]), {"deletions": dele}, token=self.tok)
        g.http("DELETE", "%s/projects/%s/managedZones/%s" % (DNS, self.project, z["name"]), token=self.tok)
        self.changed.append("deleted zone " + fqdn)
        return {"zone": fqdn, "deleted": True}

    # ---- a placement's VPC and its HA VPN to the site's routers
    def names(self, placement):
        return "shc-%s" % placement

    def place_status(self, placement):
        n = self.names(placement)
        net = self.get("/global/networks/" + n)
        out = {"placement": placement, "vpc": n if net else None}
        if net:
            out["subnet"] = (self.get("/regions/%s/subnetworks/%s" % (self.region, n)) or {}).get("ipCidrRange")
            out["tunnels"] = {}
            for i in range(2):
                t = self.get("/regions/%s/vpnTunnels/%s-t%d" % (self.region, n, i))
                if t:
                    out["tunnels"]["%s-t%d" % (n, i)] = {"status": t.get("status"), "detail": t.get("detailedStatus"), "peer": t.get("peerIp")}
            rs = g.http("GET", "%s/regions/%s/routers/%s/getRouterStatus" % (self.base, self.region, n), token=self.tok, ok404=True)
            out["bgp"] = [{"name": p["name"], "status": p.get("status"), "state": p.get("state"), "learned": p.get("numLearnedRoutes")}
                          for p in ((rs or {}).get("result") or {}).get("bgpPeerStatus", [])]
            out["routes"] = sorted({r["destRange"] for r in ((rs or {}).get("result") or {}).get("bestRoutesForRouter", [])})
        return out

    def place_up(self, placement, vni, prefixes, edge, egress="fabric", known=None):
        c = self.cfg
        n = self.names(placement)
        labels = dict(self.labels, **{"shc-placement": placement, "shc-vni": str(vni)})
        mtu = int(c.get("vpcMtu", 1370))
        net = self.get("/global/networks/" + n)
        if net and net.get("mtu") != mtu:
            fail("VPC %s exists with MTU %s, want %s" % (n, net.get("mtu"), mtu))
        if not net:
            self.changed.append("vpc " + n)
            self.op("POST", "/global/networks", {"name": n, "autoCreateSubnetworks": False, "mtu": mtu,
                                                  "routingConfig": {"routingMode": "REGIONAL"},
                                                  "description": "SHC placement %s (VNI %d)" % (placement, vni)})
            net = self.get("/global/networks/" + n)
        sn = self.get("/regions/%s/subnetworks/%s" % (self.region, n))
        if sn and sn.get("ipCidrRange") != prefixes[0]:
            fail("subnet %s exists as %s, want %s" % (n, sn.get("ipCidrRange"), prefixes[0]))
        if not sn:
            self.changed.append("subnet " + n)
            body = {"name": n, "network": net["selfLink"], "ipCidrRange": prefixes[0], "privateIpGoogleAccess": True,
                    "secondaryIpRanges": [{"rangeName": "extra%d" % i, "ipCidrRange": p} for i, p in enumerate(prefixes[1:])]}
            self.op("POST", "/regions/%s/subnetworks" % self.region, body)
            sn = self.get("/regions/%s/subnetworks/%s" % (self.region, n))
        # the VPC's own default route to the internet goes when the fabric is the egress
        for r in g.list_all(self.base + "/global/routes", "items", self.tok, {"filter": 'network eq ".*/%s"' % n}):
            if r.get("destRange") == "0.0.0.0/0" and r.get("nextHopGateway") and egress == "fabric":
                self.op("DELETE", "/global/routes/" + r["name"])
                self.changed.append("default internet route removed")
        # firewall: the fabric's address space and the platform's sources in, everything inside the VPC
        fw = {n + "-fabric": {"sourceRanges": c.get("fabricSources", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]),
                              "allowed": [{"IPProtocol": "all"}], "description": "From the hybrid fabric and the VPC"},
              n + "-iap": {"sourceRanges": ["35.235.240.0/20"], "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
                           "description": "SSH through IAP"}}
        for name, want in fw.items():
            if not self.get("/global/firewalls/" + name):
                self.changed.append("firewall " + name)
                self.op("POST", "/global/firewalls", dict(want, name=name, network=net["selfLink"], direction="INGRESS"))
        # Cloud Router and HA VPN gateway
        if not self.get("/regions/%s/routers/%s" % (self.region, n)):
            self.changed.append("router " + n)
            self.op("POST", "/regions/%s/routers" % self.region, {"name": n, "network": net["selfLink"],
                                                                   "bgp": {"asn": int(c.get("cloudRouterAsn", 65020))}})
        gw = self.get("/regions/%s/vpnGateways/%s" % (self.region, n))
        if not gw:
            self.changed.append("ha vpn gateway " + n)
            self.op("POST", "/regions/%s/vpnGateways" % self.region, {"name": n, "network": net["selfLink"], "labels": labels})
            gw = self.get("/regions/%s/vpnGateways/%s" % (self.region, n))
        # the site's routers as the external VPN gateway (one per site, shared by the project's placements)
        ext_name = "shc-edge-" + edge["site"]
        routers = sorted(edge["routers"], key=lambda r: r["name"])[:2]
        ext = self.get("/global/externalVpnGateways/" + ext_name)
        want_if = [{"id": i, "ipAddress": r["publicIp"]} for i, r in enumerate(routers)]
        if ext and [{"id": i["id"], "ipAddress": i["ipAddress"]} for i in ext.get("interfaces", [])] != want_if:
            fail("external VPN gateway %s has other addresses than the site's routers (replace it when no tunnel uses it)" % ext_name)
        if not ext:
            self.changed.append("external vpn gateway " + ext_name)
            self.op("POST", "/global/externalVpnGateways", {"name": ext_name, "redundancyType": "TWO_IPS_REDUNDANCY" if len(routers) == 2 else "SINGLE_IP_INTERNALLY_REDUNDANT",
                                                             "interfaces": want_if, "labels": self.labels})
            ext = self.get("/global/externalVpnGateways/" + ext_name)
        # link-local /30s for this network: a slice of the pool by VNI (two /30s per placement)
        pool = ipaddress.ip_network(c.get("linkLocalPool", "169.254.0.0/16"))
        slot = (vni % 8000) * 2
        subnets = list(pool.subnets(new_prefix=30))
        psk_file = os.environ.get("SHC_EDGE_VPN_TUNNELS_FILE")
        tunnels, secrets_out = {}, {}
        for i, r in enumerate(routers):
            ll = subnets[slot + i]
            cloud_ip, router_ip = str(ll[1]), str(ll[2])
            tname = "%s-t%d" % (n, i)
            t = self.get("/regions/%s/vpnTunnels/%s" % (self.region, tname))
            psk = None
            # a tunnel whose shared secret never reached the routers (the run that minted it failed before the
            # edge config was written) can't be repaired in place: GCE tunnels' secrets are immutable, so the
            # tunnel is recreated with a fresh secret. `known` = the peers the routers hold a secret for.
            if t and known is not None and gw["vpnInterfaces"][i]["ipAddress"] not in set(known.get(r["name"], [])):
                rt = self.get("/regions/%s/routers/%s" % (self.region, n))
                patch = {"interfaces": [x for x in rt.get("interfaces", []) if x["name"] != "if-t%d" % i],
                         "bgpPeers": [x for x in rt.get("bgpPeers", []) if x["name"] != "peer-t%d" % i]}
                if patch != {"interfaces": rt.get("interfaces", []), "bgpPeers": rt.get("bgpPeers", [])}:
                    self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n), patch)
                self.op("DELETE", "/regions/%s/vpnTunnels/%s" % (self.region, tname))
                self.changed.append("tunnel %s recreated (secret unknown to the routers)" % tname)
                t = None
            if not t:
                psk = secrets.token_urlsafe(30)
                self.changed.append("tunnel " + tname)
                self.op("POST", "/regions/%s/vpnTunnels" % self.region, {
                    "name": tname, "vpnGateway": gw["selfLink"], "vpnGatewayInterface": i,
                    "peerExternalGateway": ext["selfLink"], "peerExternalGatewayInterface": i,
                    "router": "%s/regions/%s/routers/%s" % (self.base, self.region, n), "ikeVersion": 2,
                    "sharedSecret": psk, "labels": labels})
                t = self.get("/regions/%s/vpnTunnels/%s" % (self.region, tname))
            # the router interface and BGP peer for this tunnel
            rt = self.get("/regions/%s/routers/%s" % (self.region, n))
            ifs = {x["name"]: x for x in rt.get("interfaces", [])}
            peers = {x["name"]: x for x in rt.get("bgpPeers", [])}
            patch = {}
            if "if-t%d" % i not in ifs:
                patch["interfaces"] = rt.get("interfaces", []) + [{"name": "if-t%d" % i, "linkedVpnTunnel": t["selfLink"],
                                                                     "ipRange": "%s/30" % cloud_ip}]
            if "peer-t%d" % i not in peers:
                patch["bgpPeers"] = rt.get("bgpPeers", []) + [{"name": "peer-t%d" % i, "interfaceName": "if-t%d" % i,
                                                                 "peerIpAddress": router_ip, "peerAsn": int(edge["asn"])}]
            if patch:
                self.changed.append("router %s %s" % (n, "+".join(patch)))
                self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n), patch)
            entry = {"ifId": int(vni), "peerIp": gw["vpnInterfaces"][i]["ipAddress"], "inside": "%s/30" % router_ip,
                     "bgp": [cloud_ip], "psk": psk}
            tunnels[r["name"]] = [entry]
        # the routers need the PSKs: new ones were generated here; existing tunnels' secrets are in the edge config
        # already (the file carries only what this run minted)
        if psk_file:
            old = os.umask(0o077)
            json.dump({k: [dict(e) for e in v] for k, v in tunnels.items()}, open(psk_file, "w"))
            os.umask(old)
        return {"placement": placement, "vpc": n, "subnet": sn["ipCidrRange"], "haVpnGateway": {"name": n, "interfaces": [x["ipAddress"] for x in gw["vpnInterfaces"]]},
                "externalGateway": ext_name, "routerAsn": int(c.get("cloudRouterAsn", 65020)), "egress": egress,
                "tunnels": {k: [{x: e[x] for x in ("ifId", "peerIp", "inside", "bgp")} for e in v] for k, v in tunnels.items()}}

    def place_down(self, placement):
        n = self.names(placement)
        # workload guard: any instance on the VPC refuses the teardown
        agg = g.http("GET", self.base + "/aggregated/instances?filter=" + urllib.parse.quote('status != "TERMINATED"'), token=self.tok)
        using = [i["name"] for scope in (agg.get("items") or {}).values() for i in scope.get("instances", [])
                 if any(nic.get("network", "").endswith("/networks/" + n) for nic in i.get("networkInterfaces", []))]
        if using:
            fail("Deletion waits for workloads: VPC %s still has instances %s" % (n, ", ".join(sorted(using))), waiting=True)
        for i in range(2):
            if self.get("/regions/%s/vpnTunnels/%s-t%d" % (self.region, n, i)):
                self.op("DELETE", "/regions/%s/vpnTunnels/%s-t%d" % (self.region, n, i))
                self.changed.append("deleted tunnel %s-t%d" % (n, i))
        if self.get("/regions/%s/routers/%s" % (self.region, n)):
            self.op("DELETE", "/regions/%s/routers/%s" % (self.region, n))
            self.changed.append("deleted router " + n)
        if self.get("/regions/%s/vpnGateways/%s" % (self.region, n)):
            self.op("DELETE", "/regions/%s/vpnGateways/%s" % (self.region, n))
            self.changed.append("deleted ha vpn gateway " + n)
        for f in g.list_all(self.base + "/global/firewalls", "items", self.tok, {"filter": 'network eq ".*/%s"' % n}):
            self.op("DELETE", "/global/firewalls/" + f["name"])
            self.changed.append("deleted firewall " + f["name"])
        if self.get("/regions/%s/subnetworks/%s" % (self.region, n)):
            self.op("DELETE", "/regions/%s/subnetworks/%s" % (self.region, n))
            self.changed.append("deleted subnet " + n)
        if self.get("/global/networks/" + n):
            self.op("DELETE", "/global/networks/" + n)
            self.changed.append("deleted vpc " + n)
        # the site's external VPN gateway goes with the project's last tunnel
        exts = g.list_all(self.base + "/global/externalVpnGateways", "items", self.tok)
        tunnels_left = g.list_all(self.base + "/regions/%s/vpnTunnels" % self.region, "items", self.tok)
        for e in exts:
            if e["name"].startswith("shc-edge-") and not any(t.get("peerExternalGateway", "").endswith("/" + e["name"]) for t in tunnels_left):
                self.op("DELETE", "/global/externalVpnGateways/" + e["name"])
                self.changed.append("deleted external vpn gateway " + e["name"])
        return {"placement": placement, "deleted": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("command", choices=["check", "zone-up", "zone-down", "place-up", "place-down", "place-status"])
    ap.add_argument("--zone")
    ap.add_argument("--placement")
    ap.add_argument("--vni", type=int)
    ap.add_argument("--prefixes")
    ap.add_argument("--edge")
    ap.add_argument("--egress", default="fabric")
    ap.add_argument("--known-peers", help="JSON {router: [peer ip]} of tunnels whose secret the routers hold (place-up)")
    a = ap.parse_args()
    if a.command.startswith("zone") and not a.zone:
        fail("--zone is required")
    if a.command.startswith("place") and not a.placement:
        fail("--placement is required")
    t = None
    try:
        t = Tenant(json.loads(a.config))
        r = {"check": t.check, "zone-up": lambda: t.zone_up(a.zone), "zone-down": lambda: t.zone_down(a.zone),
             "place-up": lambda: t.place_up(a.placement, a.vni, json.loads(a.prefixes or "[]"), json.loads(a.edge or "{}"), a.egress,
                                             json.loads(a.known_peers) if a.known_peers else None),
             "place-down": lambda: t.place_down(a.placement), "place-status": lambda: t.place_status(a.placement)}[a.command]()
    except g.GcpError as ex:
        fail(str(ex), code=ex.code, changed=t.changed if t else [])
    print(json.dumps({"ok": True, "changed": t.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.5): a tenant environment's resources in its Google Cloud project, entered from the
platform identity by impersonating the project's member account. Idempotent; prints one JSON line.

  shc_gcp_tenant.py --config '<json>' check                              the project is usable (label, APIs)
  shc_gcp_tenant.py --config '<json>' zone-up --zone FQDN                the environment's Cloud DNS public zone
  shc_gcp_tenant.py --config '<json>' zone-down --zone FQDN
  shc_gcp_tenant.py --config '<json>' place-up --placement NAME --vni N --prefixes '[cidr]' --edge '<json>' [--egress fabric|cloudNat] [--known-peers '<json>'] [--ipv6 '<json>']
      the placement's VPC, subnet, Cloud Router, HA VPN gateway and tunnels to the site's routers; prints the
      routers' tunnel parameters (PSKs go to SHC_EDGE_VPN_TUNNELS_FILE, never to stdout)
  shc_gcp_tenant.py --config '<json>' place-down --placement NAME
  shc_gcp_tenant.py --config '<json>' place-status --placement NAME

config: {"project", "region", "labels": {..}, "vpcMtu", "linkLocalPool", "cloudRouterAsn", "ipv6Edge"}
  ipv6Edge: the site's routers carry IPv6, so a new HA VPN gateway is created dual-stack (IPV4_IPV6, IPv4 outer
  addresses) even for an IPv4-only placement: a gateway's stack type can't change after creation
--edge: {"site", "asn", "routers": [{"name", "publicIp"}]}
--ipv6: {"source": "Provider", "internet": bool, "fabricSources": [IPv6 CIDR][, "translation": bool]}
  (design/IPV6-PLAN.md §6.3; absent = IPv4 only, nothing below happens):
  - the subnet is dual-stack with an external IPv6 range (stackType IPV4_IPV6, ipv6AccessType EXTERNAL: Google
    assigns a /64); an IPv4-only subnet is changed in place;
  - an HA VPN gateway that exists IPV4_ONLY is re-created dual-stack: the router's interfaces and BGP peers of its
    tunnels go, then the tunnels and the gateway; the tunnels come back with new shared secrets (the external VPN
    gateway of the site's routers stays);
  - the Cloud Router's peers exchange IPv6 on their IPv4 sessions (enableIpv6; Google assigns the next-hop pair from
    2600:2d00:0:2::/63, read back as ipv6NexthopAddress / peerIpv6NexthopAddress) and advertise ALL_SUBNETS plus the
    external /64 as a custom range (Google never advertises an external IPv6 range by default), kept in step;
  - firewall <vpc>-fabric6: IPv6 ingress from fabricSources and the subnet's own /64 (a rule holds one family);
    nothing from the internet (the implied deny ingress covers IPv6);
  - Google's IPv6 default route (::/0 to the default internet gateway) only while internet is true; when Google's own
    is gone it is put back as <vpc>-default6;
  - the tunnels file entries gain inside6 ("<peerIpv6NexthopAddress>/126", the router's side) and bgp6
    (["<ipv6NexthopAddress>"], Google's side); the result gains ipv6 {"subnet", "nexthops": {<tunnel>: {"cloud",
    "router"}}}.
IPv6-only placement (--prefixes [], design/IPV6-PLAN.md §6.4): the subnet is IPV6_ONLY with an external range (no
  IPv4 range at all; the HA VPN, peers, firewall and IPv6 default route as above, the routers keep the IPv4 BGP
  sessions that carry IPv6). Google's IPv4 default route stays whatever --egress says (no workload sends IPv4;
  Cloud NAT forwards translated IPv4 by the VPC's IPv4 routes). translation (read only here) asks for the
  provider's NAT64 path:
  - Cloud DNS server policy <vpc>-dns64 on the VPC (dns64Config.scope.allQueries: IPv6-only VMs get AAAA answers in
    64:ff9b::/96 for IPv4-only names; Google never applies it to dual-stack VMs);
  - Cloud NAT <vpc>-nat64 on the placement's Cloud Router (sourceSubnetworkIpRangesToNat64 ALL_IPV6_SUBNETWORKS,
    natIpAllocateOption AUTO_ONLY, no IPv4 source ranges);
  - when Google's IPv4 default route is gone, <vpc>-default4 (Cloud NAT needs a route to the internet gateway);
  without translation the policy and the NAT go. The result's ipv6 gains "only": true and, with translation,
  "translation": {"pref64": "64:ff9b::/96", "resolvers": [the metadata server, the VMs' resolver], "nat", "dnsPolicy"
  [, "ipv4Source": the NAT's address Google allocated, once it reports one]}.
Tunnels file (SHC_EDGE_VPN_TUNNELS_FILE): {<router name>: [{"ifId", "peerIp", "inside", "bgp", "psk"[, "inside6",
  "bgp6"]}]}, every tunnel of the placement; psk is null for a tunnel this run did not mint (the routers keep theirs).
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
# the provider's NAT64 path (Cloud DNS DNS64 + Cloud NAT): the well-known prefix, and the resolver Google hands an
# IPv6-only VM (the metadata server at its link-local IPv4 address: the guest gets a link-local IPv4 address for it;
# metadata itself also answers at fd20:ce::254)
NAT64_PREFIX = "64:ff9b::/96"
GCP_RESOLVERS6 = ["169.254.169.254"]
NEEDED_APIS = ["compute.googleapis.com", "dns.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
               "secretmanager.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com"]


def fail(msg, **extra):
    g.fail(msg, **extra)


def zone_name(fqdn):
    return "shc-" + fqdn.rstrip(".").replace(".", "-")[:55]


def truthy(v):
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def ipv6_request(ipv6):
    """The --ipv6 request in canonical form, or None for an IPv4-only placement."""
    if not ipv6:
        return None
    if (ipv6.get("source") or "Provider") != "Provider":
        fail("this site offers IPv6 only from the cloud provider; use source Provider", reason="FamilyUnavailable")
    srcs = []
    for p in ipv6.get("fabricSources") or []:
        try:
            n = ipaddress.ip_network(str(p).strip(), strict=False)
        except ValueError:
            fail("fabricSources: %s is not a prefix" % p)
        if n.version != 6:
            fail("fabricSources: %s is not IPv6" % p)
        srcs.append(str(n))
    out = {"source": "Provider", "internet": truthy(ipv6.get("internet", False)), "fabricSources": sorted(set(srcs))}
    if "translation" in ipv6:
        out["translation"] = truthy(ipv6.get("translation"))
    return out


def same_ranges(a, b):
    return sorted(str(ipaddress.ip_network(x, strict=False)) for x in a or []) == \
        sorted(str(ipaddress.ip_network(x, strict=False)) for x in b or [])


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

    @staticmethod
    def nexthops6(n, rt):
        """{<tunnel>: {"cloud": ipv6NexthopAddress, "router": peerIpv6NexthopAddress}} of the peers with IPv6 on."""
        out = {}
        for p in (rt or {}).get("bgpPeers", []):
            if p.get("enableIpv6") and p.get("name", "").startswith("peer-t") and p.get("ipv6NexthopAddress"):
                out["%s-%s" % (n, p["name"][len("peer-"):])] = {"cloud": p["ipv6NexthopAddress"],
                                                                "router": p.get("peerIpv6NexthopAddress")}
        return out

    @staticmethod
    def peer6_in_step(p, ext6):
        return (p.get("enableIpv6") is True and p.get("advertiseMode") == "CUSTOM"
                and list(p.get("advertisedGroups") or []) == ["ALL_SUBNETS"]
                and same_ranges([r.get("range") for r in p.get("advertisedIpRanges") or []], [ext6]))

    @staticmethod
    def peer6(p, ext6):
        """A BGP peer with IPv6 route exchange and the external /64 advertised (Google keeps the next hops it gave)."""
        return dict(p, enableIpv6=True, advertiseMode="CUSTOM", advertisedGroups=["ALL_SUBNETS"],
                    advertisedIpRanges=[{"range": ext6}])

    def recreate_gateway(self, n, net, labels):
        """An IPV4_ONLY HA VPN gateway can't take IPv6 (its stack type is fixed at creation): the router's interfaces
        and peers of its tunnels, the tunnels, then the gateway go; it comes back dual-stack, and the tunnels follow
        with new shared secrets."""
        rt = self.get("/regions/%s/routers/%s" % (self.region, n))
        if rt:
            mine_if = {"if-t%d" % i for i in range(2)}
            mine_peer = {"peer-t%d" % i for i in range(2)}
            patch = {"interfaces": [x for x in rt.get("interfaces", []) if x["name"] not in mine_if],
                     "bgpPeers": [x for x in rt.get("bgpPeers", []) if x["name"] not in mine_peer]}
            if patch != {"interfaces": rt.get("interfaces", []), "bgpPeers": rt.get("bgpPeers", [])}:
                self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n), patch)
        for i in range(2):
            if self.get("/regions/%s/vpnTunnels/%s-t%d" % (self.region, n, i)):
                self.op("DELETE", "/regions/%s/vpnTunnels/%s-t%d" % (self.region, n, i))
        self.op("DELETE", "/regions/%s/vpnGateways/%s" % (self.region, n))
        self.changed.append("ha vpn gateway %s recreated dual-stack (tunnels with new secrets)" % n)
        return self.create_gateway(n, net, labels, True)

    def create_gateway(self, n, net, labels, dual):
        body = {"name": n, "network": net["selfLink"], "labels": labels}
        if dual:
            body.update(stackType="IPV4_IPV6", gatewayIpVersion="IPV4")
        self.op("POST", "/regions/%s/vpnGateways" % self.region, body)
        return self.get("/regions/%s/vpnGateways/%s" % (self.region, n))

    @staticmethod
    def nat64_body(n):
        """The placement's Cloud NAT for NAT64: IPv6-only subnets only, addresses Google allocates, no IPv4 NAT."""
        return {"name": n + "-nat64", "type": "PUBLIC", "natIpAllocateOption": "AUTO_ONLY",
                "sourceSubnetworkIpRangesToNat64": "ALL_IPV6_SUBNETWORKS"}

    @staticmethod
    def nat64_in_step(x):
        return (x.get("natIpAllocateOption") == "AUTO_ONLY"
                and x.get("sourceSubnetworkIpRangesToNat64") == "ALL_IPV6_SUBNETWORKS"
                and not x.get("sourceSubnetworkIpRangesToNat") and not x.get("subnetworks")
                and x.get("type", "PUBLIC") == "PUBLIC")

    def policy_url(self, name=None):
        return "%s/projects/%s/policies%s" % (DNS, self.project, "/" + name if name else "")

    def get_policy(self, n):
        return g.http("GET", self.policy_url(n + "-dns64"), token=self.tok, ok404=True)

    @staticmethod
    def policy_in_step(pol, n):
        return (((pol.get("dns64Config") or {}).get("scope") or {}).get("allQueries") is True
                and [x.get("networkUrl", "").rsplit("/networks/", 1)[-1] for x in pol.get("networks") or []] == [n])

    def drop_policy(self, n):
        """The VPC's DNS64 server policy, detached from the network first (a policy in use can't be deleted)."""
        pol = self.get_policy(n)
        if pol:
            if pol.get("networks"):
                g.http("PATCH", self.policy_url(n + "-dns64"), {"networks": []}, token=self.tok)
            g.http("DELETE", self.policy_url(n + "-dns64"), token=self.tok)
            self.changed.append("deleted dns policy %s-dns64" % n)

    def nat64_address(self, n):
        """The address Google allocated to the placement's NAT64 (getRouterStatus natStatus), or None until it has."""
        rs = g.http("GET", "%s/regions/%s/routers/%s/getRouterStatus" % (self.base, self.region, n), token=self.tok, ok404=True)
        for st in ((rs or {}).get("result") or {}).get("natStatus", []):
            if st.get("name") == n + "-nat64":
                ips = sorted(st.get("autoAllocatedNatIps") or [], key=ipaddress.ip_address)
                return ips[0] if ips else None
        return None

    def place_status(self, placement):
        n = self.names(placement)
        net = self.get("/global/networks/" + n)
        out = {"placement": placement, "vpc": n if net else None}
        if net:
            sn = self.get("/regions/%s/subnetworks/%s" % (self.region, n)) or {}
            out["subnet"] = sn.get("ipCidrRange")
            if sn.get("stackType") in ("IPV4_IPV6", "IPV6_ONLY"):
                rt = self.get("/regions/%s/routers/%s" % (self.region, n)) or {}
                out["ipv6"] = {"subnet": sn.get("externalIpv6Prefix"), "nexthops": self.nexthops6(n, rt)}
            if sn.get("stackType") == "IPV6_ONLY":
                # the provider's NAT64 path: the Cloud NAT on the router and the VPC's DNS64 policy
                nat = next((x for x in rt.get("nats", []) if x.get("name") == n + "-nat64"), None)
                pol = self.get_policy(n)
                out["ipv6"]["only"] = True
                out["ipv6"]["nat64"] = {"name": nat["name"], "inStep": self.nat64_in_step(nat)} if nat else None
                out["ipv6"]["dns64"] = {"name": pol["name"], "inStep": self.policy_in_step(pol, n)} if pol else None
            out["tunnels"] = {}
            for i in range(2):
                t = self.get("/regions/%s/vpnTunnels/%s-t%d" % (self.region, n, i))
                if t:
                    out["tunnels"]["%s-t%d" % (n, i)] = {"status": t.get("status"), "detail": t.get("detailedStatus"), "peer": t.get("peerIp")}
            rs = g.http("GET", "%s/regions/%s/routers/%s/getRouterStatus" % (self.base, self.region, n), token=self.tok, ok404=True)
            out["bgp"] = [{"name": p["name"], "status": p.get("status"), "state": p.get("state"), "learned": p.get("numLearnedRoutes")}
                          for p in ((rs or {}).get("result") or {}).get("bgpPeerStatus", [])]
            out["routes"] = sorted({r["destRange"] for r in ((rs or {}).get("result") or {}).get("bestRoutesForRouter", [])})
            if (out.get("ipv6") or {}).get("nat64"):
                st = next((x for x in ((rs or {}).get("result") or {}).get("natStatus", []) if x.get("name") == n + "-nat64"), {})
                out["ipv6"]["nat64"]["addresses"] = sorted(st.get("autoAllocatedNatIps") or [])
        return out

    def place_up(self, placement, vni, prefixes, edge, egress="fabric", known=None, ipv6=None):
        c = self.cfg
        n = self.names(placement)
        v6 = ipv6_request(ipv6)
        # an IPv6-only placement: no IPv4 range anywhere in the VPC (an IPV6_ONLY subnet)
        only6 = not prefixes
        if only6 and not v6:
            fail("placement %s has neither an IPv4 prefix nor IPv6" % placement)
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
        if sn and only6 and (sn.get("stackType") != "IPV6_ONLY" or sn.get("ipCidrRange")):
            # a subnet's stack type can't change in place in either direction
            fail("subnet %s exists as %s %s, want IPV6_ONLY" % (n, sn.get("stackType", "IPV4_ONLY"), sn.get("ipCidrRange") or ""))
        if sn and not only6 and sn.get("ipCidrRange") != prefixes[0]:
            fail("subnet %s exists as %s, want %s" % (n, sn.get("ipCidrRange"), prefixes[0]))
        if not sn:
            self.changed.append("subnet " + n)
            if only6:
                body = {"name": n, "network": net["selfLink"], "stackType": "IPV6_ONLY", "ipv6AccessType": "EXTERNAL"}
            else:
                body = {"name": n, "network": net["selfLink"], "ipCidrRange": prefixes[0], "privateIpGoogleAccess": True,
                        "secondaryIpRanges": [{"rangeName": "extra%d" % i, "ipCidrRange": p} for i, p in enumerate(prefixes[1:])]}
                if v6:
                    body.update(stackType="IPV4_IPV6", ipv6AccessType="EXTERNAL")
            self.op("POST", "/regions/%s/subnetworks" % self.region, body)
            sn = self.get("/regions/%s/subnetworks/%s" % (self.region, n))
        ext6 = None
        if only6:
            if sn.get("ipv6AccessType") != "EXTERNAL":
                fail("subnet %s is IPV6_ONLY with IPv6 access %s, want EXTERNAL" % (n, sn.get("ipv6AccessType")))
        elif v6:
            # an IPv4-only subnet takes an external IPv6 range in place; the access type can't change afterwards
            if sn.get("stackType", "IPV4_ONLY") == "IPV4_ONLY":
                self.changed.append("subnet %s dual-stack" % n)
                self.op("PATCH", "/regions/%s/subnetworks/%s" % (self.region, n),
                        {"stackType": "IPV4_IPV6", "ipv6AccessType": "EXTERNAL", "fingerprint": sn.get("fingerprint")})
                sn = self.get("/regions/%s/subnetworks/%s" % (self.region, n))
            if sn.get("stackType") != "IPV4_IPV6" or sn.get("ipv6AccessType") != "EXTERNAL":
                fail("subnet %s is %s with IPv6 access %s, want IPV4_IPV6 with EXTERNAL"
                     % (n, sn.get("stackType"), sn.get("ipv6AccessType")))
        if v6:
            ext6 = sn.get("externalIpv6Prefix")
            if not ext6:
                fail("subnet %s has no external IPv6 range yet" % n)
            ext6 = str(ipaddress.ip_network(ext6))
        # the VPC's own default route to the internet goes when the fabric is the egress (an IPv6-only VPC keeps it:
        # it carries no workload's IPv4, and Cloud NAT routes translated IPv4 by it)
        for r in g.list_all(self.base + "/global/routes", "items", self.tok, {"filter": 'network eq ".*/%s"' % n}):
            if r.get("destRange") == "0.0.0.0/0" and r.get("nextHopGateway") and egress == "fabric" and not only6:
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
        if v6:
            # IPv6 in from the fabric (the network's IPv6 space, the fabric's ICMPv6 sources) and the subnet itself;
            # one family per rule, so it is a rule of its own, kept in step with the sources
            name, src6 = n + "-fabric6", sorted(set(v6["fabricSources"] + [ext6]))
            f6 = self.get("/global/firewalls/" + name)
            if not f6:
                self.changed.append("firewall " + name)
                self.op("POST", "/global/firewalls", {"sourceRanges": src6, "allowed": [{"IPProtocol": "all"}],
                                                      "description": "IPv6 from the hybrid fabric and the subnet",
                                                      "name": name, "network": net["selfLink"], "direction": "INGRESS"})
            elif not same_ranges(f6.get("sourceRanges"), src6):
                self.changed.append("firewall %s sources" % name)
                self.op("PATCH", "/global/firewalls/" + name, {"sourceRanges": src6})
        # Cloud Router and HA VPN gateway
        if not self.get("/regions/%s/routers/%s" % (self.region, n)):
            self.changed.append("router " + n)
            self.op("POST", "/regions/%s/routers" % self.region, {"name": n, "network": net["selfLink"],
                                                                   "bgp": {"asn": int(c.get("cloudRouterAsn", 65020))}})
        gw = self.get("/regions/%s/vpnGateways/%s" % (self.region, n))
        if not gw:
            self.changed.append("ha vpn gateway " + n)
            gw = self.create_gateway(n, net, labels, bool(v6) or truthy(c.get("ipv6Edge", False)))
        elif v6 and gw.get("stackType", "IPV4_ONLY") == "IPV4_ONLY":
            gw = self.recreate_gateway(n, net, labels)
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
                peer = {"name": "peer-t%d" % i, "interfaceName": "if-t%d" % i,
                        "peerIpAddress": router_ip, "peerAsn": int(edge["asn"])}
                patch["bgpPeers"] = rt.get("bgpPeers", []) + [self.peer6(peer, ext6) if v6 else peer]
            if patch:
                self.changed.append("router %s %s" % (n, "+".join(patch)))
                self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n), patch)
            entry = {"ifId": int(vni), "peerIp": gw["vpnInterfaces"][i]["ipAddress"], "inside": "%s/30" % router_ip,
                     "bgp": [cloud_ip], "psk": psk}
            tunnels[r["name"]] = [entry]
        out6 = None
        if v6:
            out6 = self.place_up6(n, net, v6, ext6, tunnels, routers)
        if only6:
            out6["only"] = True
            tr = self.place_nat64(n, net, bool(v6.get("translation")) and v6["internet"])
            if tr:
                out6["translation"] = tr
        # the routers need the PSKs: new ones were generated here; the file lists every tunnel of the placement, an
        # existing one with psk null (the routers keep the secret they hold)
        if psk_file:
            old = os.umask(0o077)
            json.dump({k: [dict(e) for e in v] for k, v in tunnels.items()}, open(psk_file, "w"))
            os.umask(old)
        shown = ("ifId", "peerIp", "inside", "bgp", "inside6", "bgp6")
        out = {"placement": placement, "vpc": n, "subnet": sn.get("ipCidrRange"), "haVpnGateway": {"name": n, "interfaces": [x["ipAddress"] for x in gw["vpnInterfaces"]]},
               "externalGateway": ext_name, "routerAsn": int(c.get("cloudRouterAsn", 65020)), "egress": egress,
               "tunnels": {k: [{x: e[x] for x in shown if x in e} for e in v] for k, v in tunnels.items()}}
        if out6 is not None:
            out["ipv6"] = out6
        return out

    def place_up6(self, n, net, v6, ext6, tunnels, routers):
        """The IPv6 side once the tunnels and peers exist: the peers' IPv6 route exchange and custom advertisement in
        step, Google's next-hop pair into the tunnel entries, Google's IPv6 default route following internet."""
        rt = self.get("/regions/%s/routers/%s" % (self.region, n))
        mine = {"peer-t%d" % i for i in range(len(routers))}
        peers = rt.get("bgpPeers", [])
        if any(p["name"] in mine and not self.peer6_in_step(p, ext6) for p in peers):
            self.changed.append("router %s peers IPv6" % n)
            self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n),
                    {"bgpPeers": [self.peer6(p, ext6) if p["name"] in mine else p for p in peers]})
            rt = self.get("/regions/%s/routers/%s" % (self.region, n))
        hops = self.nexthops6(n, rt)
        for i, r in enumerate(routers):
            h = hops.get("%s-t%d" % (n, i))
            if not h or not h.get("router"):
                fail("Cloud Router %s: peer-t%d has no IPv6 next hops from Google yet" % (n, i))
            for e in tunnels[r["name"]]:
                e["inside6"] = "%s/126" % h["router"]
                e["bgp6"] = [h["cloud"]]
        # Google's ::/0 to the default internet gateway: the placement's internet path, only while it wants internet
        default6 = [x for x in g.list_all(self.base + "/global/routes", "items", self.tok, {"filter": 'network eq ".*/%s"' % n})
                    if x.get("destRange") == "::/0" and x.get("nextHopGateway")]
        if v6["internet"] and not default6:
            self.changed.append("ipv6 default internet route")
            self.op("POST", "/global/routes", {"name": n + "-default6", "network": net["selfLink"], "destRange": "::/0",
                                               "nextHopGateway": self.base + "/global/gateways/default-internet-gateway",
                                               "priority": 1000, "description": "IPv6 internet through Google"})
        elif not v6["internet"]:
            for x in default6:
                self.op("DELETE", "/global/routes/" + x["name"])
                self.changed.append("ipv6 default internet route removed")
        return {"subnet": ext6, "nexthops": hops}

    def place_nat64(self, n, net, on):
        """The provider's NAT64 path of an IPv6-only placement: the VPC's DNS64 server policy, the Cloud NAT on the
        placement's Cloud Router and an IPv4 route to the internet gateway for it, in step while on; the policy and
        the NAT removed while off. Returns the translation the placement reports, or None while off."""
        pname = n + "-dns64"
        pol = self.get_policy(n)
        rt = self.get("/regions/%s/routers/%s" % (self.region, n))
        nats = rt.get("nats", [])
        mine = next((x for x in nats if x.get("name") == n + "-nat64"), None)
        if not on:
            if mine:
                self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n),
                        {"nats": [x for x in nats if x.get("name") != n + "-nat64"]})
                self.changed.append("deleted nat %s-nat64" % n)
            self.drop_policy(n)
            return None
        want_pol = {"name": pname, "description": "DNS64 for the placement's IPv6-only subnet",
                    "networks": [{"networkUrl": net["selfLink"]}], "dns64Config": {"scope": {"allQueries": True}},
                    "enableInboundForwarding": False, "enableLogging": False}
        if not pol:
            g.http("POST", self.policy_url(), want_pol, token=self.tok)
            self.changed.append("dns policy " + pname)
        elif not self.policy_in_step(pol, n):
            g.http("PATCH", self.policy_url(pname), {"networks": want_pol["networks"], "dns64Config": want_pol["dns64Config"]},
                   token=self.tok)
            self.changed.append("dns policy %s in step" % pname)
        if not mine or not self.nat64_in_step(mine):
            self.op("PATCH", "/regions/%s/routers/%s" % (self.region, n),
                    {"nats": [x for x in nats if x.get("name") != n + "-nat64"] + [self.nat64_body(n)]})
            self.changed.append("nat %s-nat64%s" % (n, " in step" if mine else ""))
        # Cloud NAT forwards the translated packet by the VPC's IPv4 routes: one to the internet gateway must exist
        v4default = [x for x in g.list_all(self.base + "/global/routes", "items", self.tok, {"filter": 'network eq ".*/%s"' % n})
                     if x.get("destRange") == "0.0.0.0/0" and x.get("nextHopGateway")]
        if not v4default:
            self.changed.append("ipv4 default internet route for NAT64")
            self.op("POST", "/global/routes", {"name": n + "-default4", "network": net["selfLink"], "destRange": "0.0.0.0/0",
                                               "nextHopGateway": self.base + "/global/gateways/default-internet-gateway",
                                               "priority": 1000, "description": "Translated IPv4 through Google"})
        tr = {"pref64": NAT64_PREFIX, "resolvers": list(GCP_RESOLVERS6), "nat": n + "-nat64", "dnsPolicy": pname}
        addr = self.nat64_address(n)
        if addr:
            tr["ipv4Source"] = addr
        return tr

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
        rt = self.get("/regions/%s/routers/%s" % (self.region, n))
        if rt:
            # the NAT64 (a Cloud NAT is part of its router) goes with it
            self.op("DELETE", "/regions/%s/routers/%s" % (self.region, n))
            self.changed += ["deleted nat " + x["name"] for x in rt.get("nats", []) if x.get("name") == n + "-nat64"]
            self.changed.append("deleted router " + n)
        if self.get("/regions/%s/vpnGateways/%s" % (self.region, n)):
            self.op("DELETE", "/regions/%s/vpnGateways/%s" % (self.region, n))
            self.changed.append("deleted ha vpn gateway " + n)
        for f in g.list_all(self.base + "/global/firewalls", "items", self.tok, {"filter": 'network eq ".*/%s"' % n}):
            self.op("DELETE", "/global/firewalls/" + f["name"])
            self.changed.append("deleted firewall " + f["name"])
        sn = self.get("/regions/%s/subnetworks/%s" % (self.region, n))
        if sn:
            if sn.get("stackType") == "IPV6_ONLY":
                # an IPv6-only VPC's DNS64 policy and the IPv4 route kept for its NAT64, while the subnet still says
                # what the VPC was (a run that fails here finds them again)
                self.drop_policy(n)
                if self.get("/global/routes/%s-default4" % n):
                    self.op("DELETE", "/global/routes/%s-default4" % n)
                    self.changed.append("deleted route %s-default4" % n)
            self.op("DELETE", "/regions/%s/subnetworks/%s" % (self.region, n))
            self.changed.append("deleted subnet " + n)
        if self.get("/global/networks/" + n):
            # the IPv6 default route the platform put back (Google's own go with the VPC)
            if self.get("/global/routes/%s-default6" % n):
                self.op("DELETE", "/global/routes/%s-default6" % n)
                self.changed.append("deleted route %s-default6" % n)
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
    ap.add_argument("--ipv6", help='JSON {"source": "Provider", "internet": bool, "fabricSources": [cidr], "translation": bool} (place-up)')
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
                                             json.loads(a.known_peers) if a.known_peers else None,
                                             json.loads(a.ipv6) if a.ipv6 else None),
             "place-down": lambda: t.place_down(a.placement), "place-status": lambda: t.place_status(a.placement)}[a.command]()
    except g.GcpError as ex:
        fail(str(ex), code=ex.code, changed=t.changed if t else [])
    print(json.dumps({"ok": True, "changed": t.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

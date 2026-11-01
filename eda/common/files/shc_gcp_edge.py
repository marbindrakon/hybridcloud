#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.3, §3.4): a Google Cloud site's edge, run by the platform. The routers (one per
zone of the hub's edge subnet, static external addresses, a WireGuard mesh between them) are EVPN VTEPs of the
fabric over WireGuard, dialled by the border gateway nodes; the tenant VPCs reach them with HA VPN + BGP, one VRF
per network. No inbound management: the routers pull their configuration from Secret Manager with their own
service account and publish their keys and state as guest attributes. Runs with the platform identity (the hub
project). Prints one JSON line.

  shc_gcp_edge.py --config '<json>' status|down|replace
  shc_gcp_edge.py --config '<json>' up [--icmp6 '{<node name>: "<a>/128"}']
  shc_gcp_edge.py --config '<json>' gateways --gateways '[{"name","publicKey","address"}]' [--icmp6 '{..}']
                                                                   (SHC_EDGE_BGP_PASSWORD)
  shc_gcp_edge.py --config '<json>' net-up --vni N [--test] [--ipv6 '{"prefixes": [..], "network": [..]}']
                                                                   tunnels from SHC_EDGE_VPN_TUNNELS_FILE (JSON, PSKs; or SHC_EDGE_VPN_TUNNELS)
  shc_gcp_edge.py --config '<json>' net-known --vni N           tunnel peers whose secret the routers hold
  shc_gcp_edge.py --config '<json>' net-down --vni N

config: {"project", "region", "zones": [..], "vpcName", "subnetName", "machineType", "image": {"project", "family"},
         "networkTag", "edgeHostIndex", "site", "asn",
         "tunnel": {"cidr", "mtu", "listenPort", "routerHostIndex"}, "vpn": {"cloudRouterAsn", "mtu"},
         "ipv6": {"enabled"}}
SHC_EDGE_VPN_TUNNELS: {<node name>: [{"ifId", "peerIp", "inside", "bgp": [..], "psk", "inside6"?, "bgp6"?: [..]}]}
  every tunnel of the placement is listed; psk null = the tunnel's secret is the one the routers hold already (an
  error for a tunnel they don't know). The other fields replace the stored ones.

IPv6 (design/IPV6-PLAN.md §6.1, §6.3; ipv6.enabled): there is no next-hop pool. Google assigns each Cloud Router
peer's IPv6 next hops (ipv6NexthopAddress, peerIpv6NexthopAddress) and the placement job hands them over with the
tunnels: inside6 "<peerIpv6NexthopAddress>/126" (the router's side, on its XFRM device) and bgp6
["<ipv6NexthopAddress>"] (Google's side). net-up --ipv6 gives a network VRF its IPv6 side: prefixes (the placement's
Google-assigned subnet /64s: what the routers accept from the Cloud Router and announce to the site's gateway as
Type-5) and network (the network's IPv6 space: what they announce to the Cloud Router); without it the VRF is
IPv4-only. The site's routers' ICMPv6 sources (the gateway job's, from the fabric's ICMPv6 pool) are kept at
ipv6.icmp6 of the routers' config. With ipv6.enabled off nothing IPv6 is kept: inside6/bgp6, --ipv6 and --icmp6
are ignored, and gateways/up remove an IPv6 side left from before, so the config is what it was before IPv6.
"""
import argparse
import base64
import gzip
import ipaddress
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import shc_gcp_api as g  # noqa: E402

IAM = "https://iam.googleapis.com/v1"
TUNNEL_FIELDS = ("ifId", "peerIp", "inside", "bgp")
SM = "https://secretmanager.googleapis.com/v1"
LETTERS = "abcdefgh"

# the routers' first-boot script (metadata startup-script; runs on every boot, installs once)
STARTUP = r"""#!/bin/bash
# shc edge router %(site)s: FRR + WireGuard + Libreswan; the agent pulls its config from Secret Manager every 30 s
set -euo pipefail
if [ ! -f /etc/shc-edge/installed ]; then
install -d -m 0700 /etc/shc-edge
echo %(site)s > /etc/shc-edge/site; echo %(project)s > /etc/shc-edge/project
dnf -y install frr wireguard-tools nftables libreswan
sed -i 's/^bgpd=no/bgpd=yes/; s/^bfdd=no/bfdd=yes/' /etc/frr/daemons
printf '%%s\n' 'net.ipv4.ip_forward = 1' 'net.ipv4.conf.all.rp_filter = 0' 'net.ipv4.conf.default.rp_filter = 0' \
  'net.ipv4.tcp_l3mdev_accept = 1' 'net.ipv4.udp_l3mdev_accept = 1' > /etc/sysctl.d/90-shc-edge.conf
%(sysctl6)ssysctl --system >/dev/null
# firewalld's forward chain drops conntrack-INVALID packets: with ECMP over two tunnels and two blades a reply often
# reaches the router that never saw the request (ICMP replies are then INVALID). Forwarding here is stateless; the
# VPC firewall and the IPsec/WireGuard peers are the fence.
systemctl disable --now firewalld >/dev/null 2>&1 || true
printf '%%s\n' 'table inet shc_edge {' '  chain forward {' '    type filter hook forward priority mangle; policy accept;' \
  '    tcp flags syn tcp option maxseg size set rt mtu' '  }' '}' > /etc/nftables/shc-edge.nft
grep -q shc-edge.nft /etc/sysconfig/nftables.conf || echo 'include "/etc/nftables/shc-edge.nft"' >> /etc/sysconfig/nftables.conf
systemctl enable --now nftables
touch /etc/shc-edge/installed
fi
echo '%(agent)s' | base64 -d | gunzip > /usr/local/sbin/shc-edge-agent
chmod 0755 /usr/local/sbin/shc-edge-agent
printf '%%s\n' '[Unit]' 'Description=SHC edge router agent (config from Secret Manager)' 'After=network-online.target' \
  'Wants=network-online.target' '[Service]' 'Type=oneshot' 'ExecStart=/usr/bin/python3 /usr/local/sbin/shc-edge-agent' \
  > /etc/systemd/system/shc-edge-agent.service
printf '%%s\n' '[Unit]' 'Description=SHC edge router agent every 30 s' '[Timer]' 'OnBootSec=5' 'OnUnitActiveSec=30' \
  'AccuracySec=5' '[Install]' 'WantedBy=timers.target' > /etc/systemd/system/shc-edge-agent.timer
systemctl daemon-reload
systemctl enable --now shc-edge-agent.timer
"""


def fail(msg, **extra):
    g.fail(msg, **extra)


def ipv6_enabled(cfg):
    return bool((cfg.get("ipv6") or {}).get("enabled"))


def v6_prefixes(xs, what):
    """Canonical IPv6 prefixes, sorted; never ::/0 (a default route is neither accepted from Google nor sent to it)."""
    out = set()
    for x in xs or []:
        try:
            n = ipaddress.ip_network(str(x).strip())
        except ValueError as e:
            fail("%s: %s" % (what, e))
        if n.version != 6 or n.prefixlen == 0:
            fail("%s: %s is not an IPv6 prefix other than ::/0" % (what, n))
        out.add(str(n))
    return sorted(out)


def network6_entry(ipv6, vni):
    """One VRF's IPv6 side as net-up --ipv6 gives it: {"prefixes", "network"}, normalized."""
    if not isinstance(ipv6, dict):
        fail("network %s: --ipv6 must be a JSON object with prefixes and network" % vni)
    return {"prefixes": v6_prefixes(ipv6.get("prefixes"), "network %s ipv6.prefixes" % vni),
            "network": v6_prefixes(ipv6.get("network"), "network %s ipv6.network" % vni)}


def icmp6_entries(icmp6, nodes):
    """The routers' ICMPv6 sources: {<node name>: "<a>/128"} for the site's routers; empty values are dropped."""
    out = {}
    for k, a in sorted((icmp6 or {}).items()):
        if not a or k not in nodes:
            continue
        try:
            n = ipaddress.ip_network(str(a).strip())
        except ValueError:
            n = None
        if n is None or n.version != 6 or n.prefixlen != 128:
            fail("icmp6 %s of %s is not an IPv6 /128" % (a, k))
        out[k] = str(n)
    return out


def merge_tunnels(stored, tunnels, v6):
    """The VRF's tunnels from the tunnels file (every tunnel of the placement, by router) over the stored ones (by
    router and peer address): the file's fields replace the stored ones, a null psk keeps the stored secret, and a
    null psk for a tunnel the routers don't hold is an error. inside6/bgp6 only with IPv6 on."""
    old = {k: {t["peerIp"]: t for t in ts} for k, ts in (stored or {}).items()}
    out = {}
    for k, ts in tunnels.items():
        rows = []
        for t in ts:
            e = {x: t[x] for x in TUNNEL_FIELDS}
            e["psk"] = t.get("psk") or (old.get(k, {}).get(t["peerIp"]) or {}).get("psk")
            if not e["psk"]:
                fail("tunnel ipsec%s of %s to %s has no shared secret (none in the tunnels file, none held by the "
                     "routers)" % (t["ifId"], k, t["peerIp"]))
            if v6 and t.get("inside6"):
                try:
                    i6 = ipaddress.ip_interface(str(t["inside6"]).strip())
                    b6 = [str(ipaddress.ip_address(str(b).strip())) for b in (
                        t.get("bgp6") if isinstance(t.get("bgp6"), list) else [t.get("bgp6")] if t.get("bgp6") else [])]
                except ValueError as ex:
                    fail("tunnel ipsec%s of %s: %s" % (t["ifId"], k, ex))
                if i6.version != 6 or "/" not in str(t["inside6"]) or any(":" not in b for b in b6):
                    fail("tunnel ipsec%s of %s: inside6 %s / bgp6 %s are not IPv6 (<address>/126, [<address>])"
                         % (t["ifId"], k, t["inside6"], t.get("bgp6")))
                e["inside6"], e["bgp6"] = str(i6), b6
            rows.append(e)
        out[k] = rows
    return out


def strip_ipv6(conf):
    """The routers' config without any IPv6 side (edge ipv6 off)."""
    conf.pop("ipv6", None)
    for v in conf.get("vrfs", []):
        v.pop("ipv6", None)
        for ts in (v.get("vpn") or {}).values():
            for t in ts:
                t.pop("inside6", None)
                t.pop("bgp6", None)
    return conf


class Edge:
    def __init__(self, cfg):
        t = cfg.setdefault("tunnel", {})
        if not t.get("nodeAddresses") and t.get("cidr"):
            net = ipaddress.ip_network(t["cidr"])
            t["nodeAddresses"] = [str(net[int(t.get("routerHostIndex", 10)) + i]) for i in range(len(cfg["zones"]))]
        cfg.setdefault("overlayMtu", int(t.get("mtu", 1420)) - 50)
        self.cfg = cfg
        self.v6 = ipv6_enabled(cfg)
        self.site = cfg["site"]
        self.project, self.region = cfg["project"], cfg["region"]
        self.base = "%s/projects/%s" % (g.COMPUTE, self.project)
        self.tok = g.Identity().platform()
        self.changed = []
        self.prefix = "shc-edge-" + self.site
        self.secret = "%s/projects/%s/secrets/%s-config" % (SM, self.project, self.prefix)
        self.sa_email = "%s@%s.iam.gserviceaccount.com" % (self.prefix, self.project)

    # ---- lookups
    def get(self, path, ok404=True):
        return g.http("GET", self.base + path, token=self.tok, ok404=ok404)

    def op(self, method, path, body=None, timeout=600):
        return g.wait_compute(g.http(method, self.base + path, body, token=self.tok), self.tok, timeout)

    def network(self):
        n = self.get("/global/networks/" + self.cfg["vpcName"])
        if not n:
            fail("hub VPC %s not found (run the site job first)" % self.cfg["vpcName"])
        return n

    def subnet(self):
        s = self.get("/regions/%s/subnetworks/%s" % (self.region, self.cfg["subnetName"]))
        if not s:
            fail("hub subnet %s not found" % self.cfg["subnetName"])
        return s

    def node_names(self):
        return ["%s-%s" % (self.prefix, LETTERS[i]) for i in range(len(self.cfg["zones"]))]

    def zone_of(self, name):
        return self.cfg["zones"][self.node_names().index(name)]

    def instances(self):
        out = {}
        for name in self.node_names():
            inst = self.get("/zones/%s/instances/%s" % (self.zone_of(name), name))
            if inst:
                out[name] = inst
        return out

    def read_config(self):
        try:
            r = g.http("GET", self.secret + "/versions/latest:access", token=self.tok)
        except g.GcpError as e:
            if e.code == 404 or e.status == "FAILED_PRECONDITION":
                return None
            raise
        return json.loads(base64.b64decode(r["payload"]["data"]))

    def write_config(self, c):
        old = self.read_config()
        if old == c:
            return
        self.changed.append("config")
        data = base64.b64encode(json.dumps(c, sort_keys=True).encode()).decode()
        g.http("POST", self.secret + ":addVersion", {"payload": {"data": data}}, token=self.tok)
        # keep only the newest version (versions are billed)
        vers = g.list_all(self.secret + "/versions", "versions", self.tok, {"filter": "state:ENABLED"})
        for v in sorted(vers, key=lambda x: int(x["name"].rsplit("/", 1)[1]))[:-1]:
            g.http("POST", "%s/%s:destroy" % (SM, v["name"]), {}, token=self.tok)

    def node_reports(self):
        out = {}
        for name in self.instances():
            try:
                r = g.http("GET", "%s/zones/%s/instances/%s/getGuestAttributes?queryPath=shc/report" % (
                    self.base, self.zone_of(name), name), token=self.tok, ok404=True)
            except g.GcpError:
                r = None
            items = ((r or {}).get("queryValue") or {}).get("items") or []
            if items:
                try:
                    out[name] = json.loads(items[0]["value"])
                except ValueError:
                    pass
        return out

    # ---- pieces of the site
    def ensure_sa(self):
        sa = g.http("GET", "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, self.sa_email), token=self.tok, ok404=True)
        if not sa:
            self.changed.append("service account " + self.prefix)
            g.http("POST", "%s/projects/%s/serviceAccounts" % (IAM, self.project), {
                "accountId": self.prefix, "serviceAccount": {"displayName": "SHC edge router " + self.site,
                                                             "description": "Reads the site's config secret"}}, token=self.tok)
            time.sleep(5)

    def ensure_secret(self):
        s = g.http("GET", self.secret, token=self.tok, ok404=True)
        if not s:
            self.changed.append("secret " + self.prefix + "-config")
            g.http("POST", "%s/projects/%s/secrets?secretId=%s-config" % (SM, self.project, self.prefix), {
                "replication": {"automatic": {}}, "labels": {"shc-managed": "true", "shc-edge-site": self.site}}, token=self.tok)
        pol = g.http("GET", self.secret + ":getIamPolicy", token=self.tok)
        member = "serviceAccount:" + self.sa_email
        b = [x for x in pol.get("bindings", []) if x["role"] == "roles/secretmanager.secretAccessor"]
        if not b or member not in b[0].get("members", []):
            if b:
                b[0]["members"].append(member)
            else:
                pol.setdefault("bindings", []).append({"role": "roles/secretmanager.secretAccessor", "members": [member]})
            g.http("POST", self.secret + ":setIamPolicy", {"policy": pol}, token=self.tok)
            self.changed.append("secret access for " + self.prefix)

    def ensure_address(self, name):
        a = self.get("/regions/%s/addresses/%s" % (self.region, name))
        if not a:
            self.changed.append("address " + name)
            self.op("POST", "/regions/%s/addresses" % self.region, {
                "name": name, "addressType": "EXTERNAL", "networkTier": "PREMIUM",
                "labels": {"shc-managed": "true", "shc-edge-site": self.site}, "description": "SHC edge router " + name})
            a = self.get("/regions/%s/addresses/%s" % (self.region, name))
        return a

    def startup_script(self):
        agent = base64.b64encode(gzip.compress(open(os.path.join(HERE, "shc_gcp_edge_agent.py"), "rb").read())).decode()
        # IPv6 forwarding and Packet Too Big without a rate limit (the agent sets them too, for routers made before)
        sysctl6 = ("printf '%s\\n' 'net.ipv6.conf.all.forwarding = 1' 'net.ipv6.conf.default.forwarding = 1' "
                   "'net.ipv6.icmp.ratelimit = 0' > /etc/sysctl.d/91-shc-edge-ipv6.conf\n") if self.v6 else ""
        return STARTUP % {"site": self.site, "project": self.project, "agent": agent, "sysctl6": sysctl6}

    def set_icmp6(self, conf, icmp6):
        """The site-level IPv6 side of the routers' config: the routers' ICMPv6 sources (icmp6 None: as stored).
        With IPv6 off, the whole IPv6 side goes."""
        if not self.v6:
            return strip_ipv6(conf)
        if icmp6 is not None:
            conf["ipv6"] = {"icmp6": icmp6_entries(icmp6, conf.get("nodes") or {})}
        return conf

    def up(self, icmp6=None):
        c = self.cfg
        self.network()
        subnet = self.subnet()
        self.ensure_sa()
        self.ensure_secret()
        conf = self.read_config() or {}
        conf.update({"site": self.site, "asn": int(c["asn"]), "vpnMtu": int((c.get("vpn") or {}).get("mtu", 1406)),
                     "tunnel": {k: c["tunnel"][k] for k in ("cidr", "mtu", "listenPort")}})
        conf.setdefault("nodes", {})
        conf.setdefault("gateways", [])
        conf.setdefault("vrfs", [])
        have = self.instances()
        subnet_net = ipaddress.ip_network(subnet["ipCidrRange"])
        for idx, name in enumerate(self.node_names()):
            zone = c["zones"][idx]
            addr = self.ensure_address(name)
            fixed = str(subnet_net[int(c.get("edgeHostIndex", 10)) + idx])
            inst = have.get(name)
            if not inst:
                self.changed.append("instance " + name)
                body = {
                    "name": name, "machineType": "zones/%s/machineTypes/%s" % (zone, c["machineType"]),
                    "canIpForward": True, "tags": {"items": [c.get("networkTag", "shc-edge")]},
                    "labels": {"shc-managed": "true", "shc-edge-site": self.site},
                    "disks": [{"boot": True, "autoDelete": True, "initializeParams": {
                        "sourceImage": "projects/%s/global/images/family/%s" % (c["image"]["project"], c["image"]["family"]),
                        "diskSizeGb": "20", "diskType": "zones/%s/diskTypes/pd-balanced" % zone}}],
                    "networkInterfaces": [{"subnetwork": subnet["selfLink"], "networkIP": fixed,
                                           "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT",
                                                              "natIP": addr["address"]}]}],
                    "serviceAccounts": [{"email": self.sa_email, "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}],
                    "metadata": {"items": [{"key": "startup-script", "value": self.startup_script()},
                                           {"key": "enable-guest-attributes", "value": "TRUE"}]}}
                for attempt in range(12):  # a new service account takes a few seconds to be usable
                    try:
                        self.op("POST", "/zones/%s/instances" % zone, body)
                        break
                    except g.GcpError as e:
                        if "service account" not in str(e).lower() or attempt == 11:
                            raise
                        time.sleep(10)
                inst = self.get("/zones/%s/instances/%s" % (zone, name))
            node = conf["nodes"].setdefault(name, {"name": name, "address": c["tunnel"]["nodeAddresses"][idx]})
            node["externalIp"] = addr["address"]
            node["underlay"] = inst["networkInterfaces"][0]["networkIP"]
        conf["nodes"] = {k: v for k, v in conf["nodes"].items() if k in self.node_names()}
        self.set_icmp6(conf, icmp6)
        self.write_config(conf)
        self.sync_nodes(wait=True)
        return self.status()

    def sync_nodes(self, wait=False, tries=30):
        """Put each router's published public key into the site config (the mesh between them). With wait, give new
        routers a few minutes to boot and publish."""
        missing = []
        for i in range(tries if wait else 1):
            conf = self.read_config() or {}
            reports = self.node_reports()
            missing = [k for k in conf.get("nodes", {}) if not (reports.get(k) or {}).get("publicKey")]
            if not missing or i == (tries if wait else 1) - 1:
                break
            time.sleep(10)
        for k, n in conf.get("nodes", {}).items():
            if (reports.get(k) or {}).get("publicKey"):
                n["publicKey"] = reports[k]["publicKey"]
        self.write_config(conf)
        return missing

    def delete_instances(self):
        ops = []
        for name in self.instances():
            self.changed.append("deleted instance " + name)
            ops.append(g.http("DELETE", "%s/zones/%s/instances/%s" % (self.base, self.zone_of(name), name), token=self.tok))
        for o in ops:
            g.wait_compute(o, self.tok)

    def down(self):
        self.delete_instances()
        for name in self.node_names():
            if self.get("/regions/%s/addresses/%s" % (self.region, name)):
                self.op("DELETE", "/regions/%s/addresses/%s" % (self.region, name))
                self.changed.append("released address " + name)
        if g.http("GET", self.secret, token=self.tok, ok404=True):
            g.http("DELETE", self.secret, token=self.tok)
            self.changed.append("deleted secret")
        if g.http("GET", "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, self.sa_email), token=self.tok, ok404=True):
            g.http("DELETE", "%s/projects/%s/serviceAccounts/%s" % (IAM, self.project, self.sa_email), token=self.tok)
            self.changed.append("deleted service account")
        return {"site": self.site, "instances": [], "nodes": {}}

    def replace(self):
        """Replace the routers (a new agent or image): same names, addresses and config; new WireGuard keys, so the
        CloudGateway's peers need the new keys."""
        self.delete_instances()
        conf = self.read_config() or {}
        for n in conf.get("nodes", {}).values():
            n.pop("publicKey", None)
        self.write_config(conf)
        return self.up()

    def set_gateways(self, gateways, icmp6=None):
        conf = self.read_config()
        if conf is None:
            fail("site %s has no config yet (run up first)" % self.site)
        conf["gateways"] = [{"name": x["name"], "publicKey": x["publicKey"], "address": x["address"]} for x in gateways]
        pw = os.environ.get("SHC_EDGE_BGP_PASSWORD", "")
        if pw:
            conf["bgpPassword"] = pw
        else:
            conf.pop("bgpPassword", None)
        self.set_icmp6(conf, icmp6)
        self.write_config(conf)
        return self.status()

    # ---- networks (the cloud side is the tenant VPC's HA VPN; here only the routers' VRF and tunnels)
    def net_up(self, vni, test=False, ipv6=None):
        conf = self.read_config()
        if conf is None or not conf.get("nodes"):
            fail("site %s is not up" % self.site)
        # the tenant side writes the tunnels (PSKs inside) to a file; the env var form is kept for tests. Never
        # pass the JSON through an Ansible environment: a whole-expression template turns it into a dict and
        # the environment then carries its Python repr (single quotes), which is not JSON.
        tf = os.environ.get("SHC_EDGE_VPN_TUNNELS_FILE")
        if tf:
            with open(tf) as f:
                tunnels = json.load(f)
        else:
            tunnels = json.loads(os.environ.get("SHC_EDGE_VPN_TUNNELS") or "{}")
        # a tunnel the tenant side didn't mint again (psk null) keeps the secret the config already holds
        stored = next((v.get("vpn", {}) for v in conf.get("vrfs", []) if v["vni"] == vni), {})
        tunnels = merge_tunnels(stored, tunnels, self.v6)
        vrf = {"name": "t%d" % vni, "vni": vni, "mtu": int(self.cfg["overlayMtu"]),
               "peerAsn": int((self.cfg.get("vpn") or {}).get("cloudRouterAsn", 65020)),
               "vpn": {k: v for k, v in tunnels.items() if k in conf["nodes"]}}
        if test:
            base = ipaddress.ip_address(self.cfg.get("testBase", "10.252.1.1"))
            vrf["testAddress"] = {n: str(base + i) for i, n in enumerate(sorted(conf["nodes"]))}
        if self.v6 and ipv6 is not None:
            vrf["ipv6"] = network6_entry(ipv6, vni)
        conf["vrfs"] = sorted([v for v in conf["vrfs"] if v["vni"] != vni] + [vrf], key=lambda v: v["vni"])
        self.write_config(conf)
        fields = TUNNEL_FIELDS + (("inside6", "bgp6") if self.v6 else ())
        out = {"vni": vni, "vrf": vrf["name"],
               "tunnels": {k: [{x: t[x] for x in fields if x in t} for t in v] for k, v in vrf["vpn"].items()}}
        if "ipv6" in vrf:
            out["ipv6"] = vrf["ipv6"]
        return out

    def net_known(self, vni):
        """The tunnel peers (Google's HA VPN interface addresses) whose shared secret the routers' config holds for
        this network; the tenant side re-mints the tunnels that are not in this list."""
        conf = self.read_config() or {}
        known = {}
        for v in conf.get("vrfs", []):
            if v["vni"] == vni:
                known = {k: sorted(t["peerIp"] for t in ts if t.get("psk")) for k, ts in v.get("vpn", {}).items()}
        return {"vni": vni, "known": known}

    def net_down(self, vni):
        conf = self.read_config()
        if conf and any(v["vni"] == vni for v in conf.get("vrfs", [])):
            conf["vrfs"] = [v for v in conf["vrfs"] if v["vni"] != vni]
            self.write_config(conf)
            self.changed.append("vrf t%d removed" % vni)
        return {"vni": vni}

    # ---- status
    def status(self):
        out = {"site": self.site, "project": self.project, "region": self.region, "instances": [], "nodes": {}}
        for name, inst in self.instances().items():
            nic = inst["networkInterfaces"][0]
            out["instances"].append({"id": inst.get("id"), "name": name, "zone": self.zone_of(name), "state": inst.get("status"),
                                     "type": inst["machineType"].rsplit("/", 1)[1], "privateIp": nic.get("networkIP"),
                                     "publicIp": ((nic.get("accessConfigs") or [{}])[0]).get("natIP")})
        conf = self.read_config() or {}
        out["vrfs"] = [dict({k: v[k] for k in ("name", "vni")}, **({"ipv6": v["ipv6"]} if v.get("ipv6") else {}))
                       for v in conf.get("vrfs", [])]
        out["gateways"] = [x["name"] for x in conf.get("gateways", [])]
        out["tunnelAddresses"] = {k: v["address"] for k, v in conf.get("nodes", {}).items()}
        out["nodes"] = self.node_reports()
        if self.v6 or conf.get("ipv6") or any(v.get("ipv6") for v in conf.get("vrfs", [])):
            out["ipv6"] = ipv6_status(conf, out["nodes"])
        return out


def ipv6_status(conf, reports):
    """Per IPv6 VRF and router what its report says: the tunnels' IPv6 addresses, the router's ICMPv6 source, the
    Cloud Router sessions that negotiated IPv6 unicast (of those reported), the IPv6 routes the router holds by
    protocol, whether it has a default route (it never should: Google's default is the provider path) and the
    placement's prefixes it has not learned from the Cloud Router yet."""
    icmp6 = (conf.get("ipv6") or {}).get("icmp6") or {}
    out = {}
    for v in conf.get("vrfs", []):
        six = v.get("ipv6") or {}
        if not six:
            continue
        row = {"prefixes": list(six.get("prefixes") or []), "network": list(six.get("network") or []), "routers": {}}
        for name in sorted(v.get("vpn") or {}):
            t6 = [t for t in v["vpn"][name] if t.get("inside6")]
            st = ((reports.get(name) or {}).get("state") or {})
            peers = (st.get("bgp6") or {}).get(v["name"]) or {}
            routes = (st.get("routes6") or {}).get(v["name"]) or {}
            bgp = {p for p, protos in routes.items() if "bgp" in protos}
            row["routers"][name] = {
                "inside6": {"ipsec%d" % t["ifId"]: t["inside6"] for t in t6},
                "icmp6": icmp6.get(name, ""),
                "sessions6": "%d/%d" % (sum(1 for p in peers.values() if p.get("af6") == "negotiated"), len(peers)),
                "routes6": {proto: sum(1 for ps in routes.values() if proto in ps)
                            for proto in sorted({x for ps in routes.values() for x in ps})},
                "default6": "::/0" in routes,
                "missing6": [p for p in six.get("prefixes") or [] if p not in bgp] if t6 else list(six.get("prefixes") or [])}
        out[v["name"]] = row
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("command", choices=["status", "up", "down", "replace", "gateways", "net-up", "net-known", "net-down"])
    ap.add_argument("--gateways")
    ap.add_argument("--icmp6", help="up, gateways: the routers' ICMPv6 sources {<node name>: <a>/128}")
    ap.add_argument("--ipv6", help="net-up: the VRF's IPv6 side {prefixes, network}")
    ap.add_argument("--vni", type=int)
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.command in ("net-up", "net-down") and not a.vni:
        fail("--vni is required")
    e = None
    try:
        e = Edge(json.loads(a.config))
        icmp6 = json.loads(a.icmp6) if a.icmp6 else None
        r = {"status": e.status, "up": lambda: e.up(icmp6), "down": e.down, "replace": e.replace,
             "gateways": lambda: e.set_gateways(json.loads(a.gateways or "[]"), icmp6),
             "net-up": lambda: e.net_up(a.vni, a.test, json.loads(a.ipv6) if a.ipv6 else None),
             "net-known": lambda: e.net_known(a.vni),
             "net-down": lambda: e.net_down(a.vni)}[a.command]()
    except g.GcpError as ex:
        fail(str(ex), code=ex.code, changed=e.changed if e else [])
    print(json.dumps({"ok": True, "changed": e.changed, "result": r}, default=str))


if __name__ == "__main__":
    main()

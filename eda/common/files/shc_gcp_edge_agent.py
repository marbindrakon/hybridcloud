#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.3, §3.4): the Google Cloud edge router's agent. Runs on the instance (systemd
timer), standard library only. Pulls the site's desired configuration from Secret Manager with the instance's own
service account (metadata server token), applies it, and publishes the node's WireGuard public key and its state as
a guest attribute, so the router needs no inbound management access and its private key never leaves it.

  secret  shc-edge-<site>-config (latest version)   JSON written by the automation (shc_gcp_edge.py)
  guest attribute shc/report                        JSON written here: publicKey, tunnelAddress, applied, state

config: {"site", "asn", "tunnel": {"cidr", "mtu", "listenPort"}, "bgpPassword"?,
         "nodes": {<instance name>: {"name", "address", "publicKey"?, "underlay"?, "externalIp"?}},
         "gateways": [{"name", "publicKey", "address"}],
         "vrfs": [{"name", "vni", "mtu", "peerAsn",
                   "vpn": {<instance name>: [{"ifId", "peerIp", "inside", "bgp": [..], "psk",
                                              "inside6"?: "<a>/126", "bgp6"?: [..]}]},
                   "testAddress"?: {<instance name>: "a.b.c.d"},
                   "ipv6"?: {"prefixes": [..], "network": [..]}}],
         "ipv6"?: {"icmp6": {<instance name>: "<a>/128"}}}
Per network VRF: an L3VNI bridge + VXLAN over the WireGuard tunnel (the fabric side), and one XFRM interface
(ipsec<ifId>, Libreswan ipsec-interface) per HA VPN tunnel inside the VRF with eBGP to the Cloud Router (the cloud
side).

IPv6 (design/IPV6-PLAN.md §6.1, §6.3), per VRF with an ipv6 block, on the tunnels of this router that carry inside6
(the Cloud Router peer has IPv6 on; Google assigned the next-hop pair, the router's side is inside6); a VRF without an
ipv6 block is configured exactly as before IPv6 existed:
- Libreswan: two conns per such tunnel, shc-<vni>-<ifId>-v4 (0.0.0.0/0) and -v6 (::/0), with identical IKE
  parameters on the same ipsec-interface: one IKE SA, two child SAs, both families on one XFRM device (Libreswan 4.15
  takes no mixed-family subnet list in one conn). A tunnel without inside6 keeps its single conn shc-<vni>-<ifId>.
- kernel: IPv6 forwarding and no ICMPv6 rate limit (Packet Too Big for every oversized packet); the XFRM device
  takes inside6 next to the IPv4 /30 (Libreswan re-creates the device when it restarts, so its VRF master, MTU and
  both addresses are asserted after Libreswan on every run); lo<vni> takes the router's ICMPv6 source /128 (icmp6,
  never announced); an unreachable IPv6 default in the VRF table, so a lookup that misses fails in the VRF instead
  of falling through to the main table.
- FRR, on the IPv4 sessions to the Cloud Router (MP-BGP): address-family ipv6 unicast with route-maps per family
  (RFC 8212). In (<vrf>-vpn6-in): the placement's prefixes only (ipv6.prefixes: the subnets' Google-assigned /64s),
  never a default. Out (<vrf>-vpn6-out): the network's IPv6 space (ipv6.network) learned from the fabric, never the
  placement's own prefixes and never ::/0 (Google's own default route is the provider path), next hop the router's
  inside6 address. A provider-assigned prefix of another cloud is outside ipv6.network, so it is never announced into
  this cloud. advertise ipv6 unicast in the VRF's EVPN family, limited to the placement's prefixes (Type-5 toward the
  site's gateway). zebra installs the VRF's BGP routes toward the fabric with the icmp6 /128 as their source (ipv6
  protocol bgp route-map <vrf>-src6), so ICMPv6 errors toward the fabric leave from it; routes toward the Cloud
  Router keep the XFRM device's address.
"""
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

STATE = "/etc/shc-edge"
KEY = STATE + "/wg.key"
# an unreachable IPv6 default at the highest metric: any route FRR installs wins over it
UNREACHABLE_METRIC = "4278198272"


def _local(name):
    try:
        return open(os.path.join(STATE, name)).read().strip()
    except OSError:  # not on a router (the unit tests import the module)
        return ""


SITE = _local("site")
PROJECT = _local("project")
MD = "http://metadata.google.internal/computeMetadata/v1/"
IPSEC_CONF = "/etc/ipsec.d/shc-edge.conf"
IPSEC_SECRETS = "/etc/ipsec.d/shc-edge.secrets"


def metadata(path, method="GET", data=None):
    req = urllib.request.Request(MD + path, data=data, method=method, headers={"Metadata-Flavor": "Google"})
    return urllib.request.urlopen(req, timeout=5).read().decode()


def token():
    return json.loads(metadata("instance/service-accounts/default/token"))["access_token"]


def secret_config():
    url = "https://secretmanager.googleapis.com/v1/projects/%s/secrets/shc-edge-%s-config/versions/latest:access" % (PROJECT, SITE)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    return json.loads(base64.b64decode(r["payload"]["data"]))


def publish(report):
    body = json.dumps(report).encode()
    req = urllib.request.Request(MD + "instance/guest-attributes/shc/report", data=body, method="PUT",
                                 headers={"Metadata-Flavor": "Google"})
    urllib.request.urlopen(req, timeout=5).read()


def sh(cmd, check=True, inp=None):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), input=inp, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("%s: %s" % (cmd, r.stderr.strip()))
    return r.stdout


def link_exists(name):
    return subprocess.run(["ip", "link", "show", name], capture_output=True).returncode == 0


def public_key():
    if not os.path.exists(KEY):
        old = os.umask(0o077)
        open(KEY, "w").write(sh(["wg", "genkey"]))
        os.umask(old)
    return sh(["wg", "pubkey"], inp=open(KEY).read()).strip()


def tunnels6(v, me):
    """This router's tunnels of the VRF that carry IPv6 (the VRF has an ipv6 block and the tunnel its inside6)."""
    if not v.get("ipv6"):
        return []
    return [t for t in v.get("vpn", {}).get(me, []) if t.get("inside6")]


def v6_of(v, me):
    """The VRF's ipv6 block when IPv6 is on for it on this router (at least one tunnel with inside6), else None."""
    return v["ipv6"] if tunnels6(v, me) else None


def icmp6_of(cfg, me):
    """This router's ICMPv6 source /128 (the site's ipv6.icmp6), or None."""
    return ((cfg.get("ipv6") or {}).get("icmp6") or {}).get(me) or None


def _pl6(name, seq, action, prefix):
    """One ipv6 prefix-list line: the prefix and every longer one inside it (exactly ::/0 for the default route)."""
    n = ipaddress.ip_network(prefix)
    more = "" if n.prefixlen in (0, 128) else " le 128"
    return "ipv6 prefix-list %s seq %d %s %s%s" % (name, seq, action, n, more)


def frr_policy6(v, six, icmp, nexthop):
    """The VRF's IPv6 prefix lists and route-maps (see the module docstring). nexthop: the router's inside6 address
    for the outbound route-map, None when the router holds more than one IPv6 tunnel in the VRF (FRR then takes each
    session's interface address, which is that tunnel's inside6)."""
    name = v["name"]
    own = list(six.get("prefixes") or [])
    out = [_pl6(name + "-in6", 5 * (i + 1), "permit", p) for i, p in enumerate(own)]
    if not own:
        out.append("ipv6 prefix-list %s-in6 seq 5 deny ::/0 le 128" % name)
    # toward Google: never the default route (the provider path is Google's own), never the placement's prefixes
    lines = [_pl6(name + "-out6", 5, "deny", "::/0")]
    seq = 5
    for p in own:
        seq += 5
        lines.append(_pl6(name + "-out6", seq, "deny", p))
    for p in six.get("network") or []:
        seq += 5
        lines.append(_pl6(name + "-out6", seq, "permit", p))
    out += lines
    out += ["route-map %s-vpn6-in permit 10" % name, " match ipv6 address prefix-list %s-in6" % name, "exit",
            "route-map %s-vpn6-out permit 10" % name, " match ipv6 address prefix-list %s-out6" % name]
    if nexthop:
        out.append(" set ipv6 next-hop global %s" % nexthop)
    out += ["exit", "route-map %s-evpn6 permit 10" % name, " match ipv6 address prefix-list %s-in6" % name, "exit"]
    if icmp:
        # zebra: the Cloud Router's routes as they are, every other BGP route with the ICMPv6 source as its source
        out += ["route-map %s-src6 permit 10" % name, " match ipv6 address prefix-list %s-in6" % name, "exit",
                "route-map %s-src6 permit 20" % name, " set src %s" % icmp.split("/")[0], "exit"]
    return out + ["!"]


def frr_conf(cfg, me, node):
    gws = cfg.get("gateways", [])
    pw = cfg.get("bgpPassword")
    out = ["frr defaults traditional", "hostname %s" % node["name"], "service integrated-vtysh-config", "!"]
    for v in cfg.get("vrfs", []):
        six = v6_of(v, me)
        icmp = icmp6_of(cfg, me) if six else None
        out += ["vrf %s" % v["name"], " vni %d" % v["vni"]]
        if icmp:
            out.append(" ipv6 protocol bgp route-map %s-src6" % v["name"])
        out += ["exit-vrf", "!"]
        # FRR 8.5 sends and accepts nothing on eBGP without policy (RFC 8212). The HA VPN belongs to this network's
        # VPC alone: take its routes, give it the network's
        out += ["route-map %s-vpn-in permit 10" % v["name"], "exit", "route-map %s-vpn-out permit 10" % v["name"],
                "exit", "!"]
        if six:
            t6 = tunnels6(v, me)
            nh = str(ipaddress.ip_interface(t6[0]["inside6"]).ip) if len(t6) == 1 else None
            out += frr_policy6(v, six, icmp, nh)
        if v.get("testAddress", {}).get(me):
            out += ["ip prefix-list %s-test seq 5 permit %s/32" % (v["name"], v["testAddress"][me]),
                    "route-map %s-connected permit 10" % v["name"], " match ip address prefix-list %s-test" % v["name"],
                    "exit", "!"]
    rid = node["address"]
    out += ["router bgp %d" % cfg["asn"], " bgp router-id %s" % rid, " no bgp default ipv4-unicast",
            " neighbor GW peer-group", " neighbor GW remote-as %d" % cfg["asn"], " neighbor GW bfd",
            " neighbor GW timers connect 10"]
    if pw:
        out.append(" neighbor GW password %s" % pw)
    out += [" neighbor %s peer-group GW" % g["address"] for g in gws]
    out += [" address-family l2vpn evpn", "  neighbor GW activate", "  advertise-all-vni", " exit-address-family",
            "exit", "!"]
    for v in cfg.get("vrfs", []):
        tunnels = v.get("vpn", {}).get(me, [])
        six = v6_of(v, me)
        out += ["router bgp %d vrf %s" % (cfg["asn"], v["name"]), " bgp router-id %s" % rid,
                " no bgp default ipv4-unicast"]
        if tunnels:
            out += [" neighbor VPN peer-group", " neighbor VPN remote-as %d" % v["peerAsn"],
                    " neighbor VPN timers 20 60", " neighbor VPN timers connect 10"]
            for t in tunnels:
                for b in t["bgp"]:
                    out += [" neighbor %s peer-group VPN" % b, " neighbor %s interface ipsec%d" % (b, t["ifId"])]
        out += [" address-family ipv4 unicast"]
        if tunnels:
            out += ["  neighbor VPN activate", "  neighbor VPN route-map %s-vpn-in in" % v["name"],
                    "  neighbor VPN route-map %s-vpn-out out" % v["name"]]
        if v.get("testAddress", {}).get(me):
            out.append("  redistribute connected route-map %s-connected" % v["name"])
        out += [" exit-address-family"]
        if six:
            # IPv6 NLRI on the IPv4 sessions (MP-BGP); the Cloud Router peer has IPv6 on
            out += [" address-family ipv6 unicast", "  neighbor VPN activate",
                    "  neighbor VPN route-map %s-vpn6-in in" % v["name"],
                    "  neighbor VPN route-map %s-vpn6-out out" % v["name"], " exit-address-family"]
        out += [" address-family l2vpn evpn", "  advertise ipv4 unicast"]
        if six:
            out.append("  advertise ipv6 unicast route-map %s-evpn6" % v["name"])
        out += [" exit-address-family", "exit", "!"]
    return "\n".join(out) + "\n"


def conn_names(v, me, t):
    """The Libreswan conns of one tunnel: (name, family subnet) pairs; two for a tunnel that carries IPv6."""
    base = "shc-%d-%d" % (v["vni"], t["ifId"])
    if v.get("ipv6") and t.get("inside6"):
        return [(base + "-v4", "0.0.0.0/0"), (base + "-v6", "::/0")]
    return [(base, "0.0.0.0/0")]


def wanted_conns(cfg, me):
    return [n for v in cfg.get("vrfs", []) for t in v.get("vpn", {}).get(me, []) for n, _ in conn_names(v, me, t)]


def ipsec_conf(cfg, me, node, local_ip, gateway):
    """Libreswan: one route-based (XFRM interface) IKEv2 tunnel per HA VPN gateway interface, PSK. Google Cloud
    sees the router at its external address (one-to-one NAT), so the router identifies itself by it. leftnexthop is
    the subnet gateway: Libreswan routes the encrypted packets through its table 50, and on GCE (a /32 on eth0) a
    link-scope host route to the peer never resolves ARP, so nothing leaves (P11 spike, 10-20). A tunnel that carries
    IPv6 has one conn per family with the same IKE parameters on the same ipsec-interface (one IKE SA, two child
    SAs); the secret is per tunnel (by the IDs) either way."""
    ext = node.get("externalIp", "")
    conns, secrets = [], []
    for v in cfg.get("vrfs", []):
        for t in v.get("vpn", {}).get(me, []):
            for name, sub in conn_names(v, me, t):
                conns += ["conn %s" % name,
                          "    type=tunnel", "    authby=secret", "    ikev2=insist",
                          "    left=%s" % local_ip, "    leftid=%s" % ext, "    leftnexthop=%s" % gateway,
                          "    right=%s" % t["peerIp"], "    rightid=%s" % t["peerIp"],
                          "    leftsubnet=%s" % sub, "    rightsubnet=%s" % sub,
                          # Google's recommended AEAD set: AES-GCM-16-256, PRF SHA2-512, DH group 20 (ECP-384), PFS
                          "    ike=aes_gcm256-sha2_512;dh20", "    esp=aes_gcm256;dh20", "    pfs=yes",
                          "    ikelifetime=36000s", "    salifetime=10800s",
                          "    dpddelay=10", "    dpdaction=restart",
                          "    encapsulation=yes", "    ipsec-interface=%d" % t["ifId"],
                          "    auto=start", ""]
            secrets.append('%s %s : PSK "%s"' % (ext, t["peerIp"], t["psk"]))
    return "\n".join(conns), "\n".join(secrets) + ("\n" if secrets else "")


def loaded_conns(status):
    """The shc conns Libreswan has loaded, from `ipsec auto --status` (4.x prefixes each line with a number)."""
    out = set()
    for line in status.splitlines():
        m = re.match(r'^(?:\d+ )?"(shc-[^"]+)"', line)
        if m:
            out.add(m.group(1))
    return out


def addrs6(dev, want):
    """Exactly these global IPv6 addresses on dev (nodad: an address the router alone holds)."""
    want = [str(ipaddress.ip_interface(w)) for w in want]
    have = []
    for a in json.loads(sh(["ip", "-j", "-6", "addr", "show", "dev", dev, "scope", "global"]) or "[]"):
        for i in a.get("addr_info", []):
            if i.get("family") == "inet6" and i.get("scope", "global") == "global":
                have.append(str(ipaddress.ip_interface("%s/%s" % (i["local"], i["prefixlen"]))))
    for h in have:
        if h not in want:
            sh(["ip", "-6", "addr", "del", h, "dev", dev])
    for w in want:
        if w not in have:
            sh(["ip", "-6", "addr", "replace", w, "dev", dev, "nodad"])


def xfrm_assert(cfg, me, v):
    """The VRF's XFRM devices as they must be: in the VRF, at the VPN MTU, up, with the tunnel's IPv4 /30 and (IPv6)
    its inside6. Libreswan creates the device on --add and re-creates it, bare, when it restarts."""
    vrf = v["name"]
    for tn in v.get("vpn", {}).get(me, []):
        dev = "ipsec%d" % tn["ifId"]
        if not link_exists(dev):  # Libreswan creates it on --add; make sure it exists for the VRF and FRR
            sh(["ip", "link", "add", dev, "type", "xfrm", "if_id", str(tn["ifId"])], check=False)
        sh(["ip", "link", "set", dev, "master", vrf, "mtu", str(int(cfg.get("vpnMtu", 1406))), "up"], check=False)
        sh(["ip", "addr", "replace", tn["inside"], "dev", dev], check=False)
        if "ipv6" in v:
            try:
                addrs6(dev, [tn["inside6"]] if tn.get("inside6") else [])
            except RuntimeError:
                if link_exists(dev):
                    raise


def write_if_changed(path, content, mode=0o640, owner=None):
    if os.path.exists(path) and open(path).read() == content:
        return False
    old = os.umask(0o027)
    open(path + ".new", "w").write(content)
    os.umask(old)
    os.chmod(path + ".new", mode)
    if owner:
        sh(["chown", owner, path + ".new"])
    os.replace(path + ".new", path)
    return True


def apply(cfg, me):
    node = cfg["nodes"][me]
    t = cfg["tunnel"]
    plen = ipaddress.ip_network(t["cidr"]).prefixlen
    eth = sh("ip -o route show default | awk '{print $5; exit}'").strip()
    gateway = sh("ip -o route show default | awk '{print $3; exit}'").strip()
    local_ip = sh("ip -4 -o addr show dev %s | awk '{print $4; exit}'" % eth).strip().split("/")[0]
    # WireGuard: the gateways dial in (no endpoints here); the site's other routers are dialled over the VPC
    if not link_exists("wg0"):
        sh("ip link add wg0 type wireguard")
    args = ["wg", "set", "wg0", "private-key", KEY, "listen-port", str(t["listenPort"])]
    mesh = [n for k, n in cfg["nodes"].items() if k != me and n.get("publicKey") and n.get("underlay")]
    want = {g["publicKey"] for g in cfg.get("gateways", [])} | {n["publicKey"] for n in mesh}
    for line in sh(["wg", "show", "wg0", "peers"]).split():
        if line not in want:
            args += ["peer", line, "remove"]
    for g in cfg.get("gateways", []):
        args += ["peer", g["publicKey"], "allowed-ips", g["address"] + "/32"]
    for n in mesh:
        args += ["peer", n["publicKey"], "endpoint", "%s:%d" % (n["underlay"], t["listenPort"]),
                 "allowed-ips", n["address"] + "/32", "persistent-keepalive", "25"]
    sh(args)
    sh("ip addr replace %s/%d dev wg0 && ip link set wg0 mtu %d up" % (node["address"], plen, t["mtu"]))
    if any(v6_of(v, me) for v in cfg.get("vrfs", [])):
        sh(["sysctl", "-qw", "net.ipv6.conf.all.forwarding=1", "net.ipv6.conf.default.forwarding=1",
            "net.ipv6.icmp.ratelimit=0"])  # Packet Too Big for every oversized packet, like the border gateways
    # one VRF per network: L3VNI bridge + VXLAN over the tunnel, the HA VPN's XFRM interfaces, a test loopback
    want_vrfs = {v["name"] for v in cfg.get("vrfs", [])}
    for line in sh("ip -o link show type vrf").splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if name.startswith("t") and name not in want_vrfs:
            n = name[1:]
            for dev in ("lo" + n, "vx" + n, "br" + n, name):
                if link_exists(dev):
                    sh(["ip", "link", "del", dev])
            # its IPv6 unreachable default sits on lo and outlives the VRF
            sh(["ip", "-6", "route", "del", "unreachable", "default", "metric", UNREACHABLE_METRIC, "table", n], check=False)
    # Libreswan first (it creates the XFRM interfaces), then the VRF membership
    conf, secrets = ipsec_conf(cfg, me, node, local_ip, gateway)
    changed = write_if_changed(IPSEC_CONF, conf + "\n")
    changed |= write_if_changed(IPSEC_SECRETS, secrets, mode=0o600)
    if subprocess.run(["systemctl", "is-active", "--quiet", "ipsec"]).returncode != 0:
        sh(["systemctl", "enable", "--now", "ipsec"])
        changed = True
    if changed:
        sh(["ipsec", "auto", "--rereadsecrets"], check=False)
        # connections no longer configured go first: a tunnel that changes between one conn and one per family
        # keeps its ipsec-interface, and the old conn's SAs must not hold the device while the new conns come up
        wanted = wanted_conns(cfg, me)
        for name in sorted(loaded_conns(sh(["ipsec", "auto", "--status"], check=False)) - set(wanted)):
            sh(["ipsec", "auto", "--delete", name], check=False)
        for name in wanted:
            sh(["ipsec", "auto", "--replace", name], check=False)
            sh(["ipsec", "auto", "--asynchronous", "--up", name], check=False)
    for v in cfg.get("vrfs", []):
        for tn in v.get("vpn", {}).get(me, []):
            sh(["ip", "route", "replace", tn["peerIp"], "via", gateway, "dev", eth, "table", "50"], check=False)
    for v in cfg.get("vrfs", []):
        n, vrf = str(v["vni"]), v["name"]
        if not link_exists(vrf):
            sh(["ip", "link", "add", vrf, "type", "vrf", "table", n])
        sh(["ip", "link", "set", vrf, "up"])
        if not link_exists("br" + n):
            sh(["ip", "link", "add", "br" + n, "type", "bridge"])
        sh(["ip", "link", "set", "br" + n, "master", vrf, "addrgenmode", "none", "up"])
        if not link_exists("vx" + n):
            sh(["ip", "link", "add", "vx" + n, "type", "vxlan", "id", n, "dstport", "4789", "local", node["address"],
                "nolearning"])
        sh(["ip", "link", "set", "vx" + n, "master", "br" + n, "mtu", str(v["mtu"]), "up"])
        xfrm_assert(cfg, me, v)
        ta = v.get("testAddress", {}).get(me)
        six = v6_of(v, me)
        icmp = icmp6_of(cfg, me) if six else None
        if ta or icmp:
            if not link_exists("lo" + n):
                sh(["ip", "link", "add", "lo" + n, "type", "dummy"])
            sh(["ip", "link", "set", "lo" + n, "master", vrf, "up"])
            if ta:
                sh(["ip", "addr", "replace", ta + "/32", "dev", "lo" + n])
            else:
                sh(["ip", "-4", "addr", "flush", "dev", "lo" + n, "scope", "global"])
        elif link_exists("lo" + n):
            sh(["ip", "link", "del", "lo" + n])
        if "ipv6" in v:
            if ta or icmp:
                addrs6("lo" + n, [icmp] if icmp else [])
            if six:
                sh(["ip", "-6", "route", "replace", "unreachable", "default", "metric", UNREACHABLE_METRIC, "table", n])
            else:
                sh(["ip", "-6", "route", "del", "unreachable", "default", "metric", UNREACHABLE_METRIC, "table", n],
                   check=False)
    # FRR: rewrite and reload only on change
    if write_if_changed("/etc/frr/frr.conf", frr_conf(cfg, me, node), owner="frr:frr"):
        if subprocess.run(["systemctl", "is-active", "--quiet", "frr"]).returncode == 0:
            sh(["systemctl", "reload", "frr"])
        else:
            sh(["systemctl", "enable", "--now", "frr"])
    # Libreswan brought its conns up asynchronously and may have re-created an XFRM device bare meanwhile: the
    # devices again, so the sessions find their addresses without waiting for the next run
    if changed:
        for v in cfg.get("vrfs", []):
            xfrm_assert(cfg, me, v)


def state(cfg, me):
    st = {"wg": [], "bgp": {}, "ipsec": {}}
    for line in sh(["wg", "show", "wg0", "latest-handshakes"], check=False).splitlines():
        k, ts = line.split()
        st["wg"].append({"publicKey": k, "latestHandshake": int(ts)})
    for line in sh(["ipsec", "trafficstatus"], check=False).splitlines():
        if '"shc-' in line:
            name = line.split('"')[1]
            st["ipsec"][name] = "established"
    for vrf in ["default"] + [v["name"] for v in cfg.get("vrfs", [])]:
        try:
            j = json.loads(sh(["vtysh", "-c", "show bgp vrf %s summary json" % vrf]) or "{}")
        except Exception:
            continue
        peers = {}
        for key, af in j.items():
            # the IPv6 family's sessions are reported apart (bgp6): the same addresses would overwrite the IPv4 counts
            if isinstance(af, dict) and key != "ipv6Unicast":
                for addr, p in (af.get("peers") or {}).items():
                    peers[addr] = {"state": p.get("state"), "pfxRcd": p.get("pfxRcd", p.get("prefixReceivedCount")),
                                   "pfxSnt": p.get("pfxSnt")}
        st["bgp"][vrf] = peers
    st["routes"] = {}
    for v in cfg.get("vrfs", []):
        try:
            j = json.loads(sh(["vtysh", "-c", "show ip route vrf %s json" % v["name"]]) or "{}")
        except Exception:
            continue
        st["routes"][v["name"]] = {p: sorted({r.get("protocol", "?") for r in rs}) for p, rs in j.items()
                                   if isinstance(rs, list)}
    six = [v for v in cfg.get("vrfs", []) if v6_of(v, me)]
    if six:
        st["bgp6"], st["routes6"] = {}, {}
    for v in six:
        try:
            st["bgp6"][v["name"]] = bgp6_peers(json.loads(
                sh(["vtysh", "-c", "show bgp vrf %s ipv6 unicast summary json" % v["name"]]) or "{}"))
        except Exception:
            pass
        try:
            j = json.loads(sh(["vtysh", "-c", "show ipv6 route vrf %s json" % v["name"]]) or "{}")
        except Exception:
            continue
        st["routes6"][v["name"]] = {p: sorted({r.get("protocol", "?") for r in rs}) for p, rs in j.items()
                                    if isinstance(rs, list)}
    return st


def bgp6_peers(j):
    """The Cloud Router sessions of a VRF's IPv6 unicast summary: per peer its state, whether the IPv6 family was
    negotiated on it (af6: negotiated, NoNeg when the session is up without it, else the session state) and the
    prefix counts."""
    af = j if "peers" in j else (j.get("ipv6Unicast") or {})
    out = {}
    for addr, p in (af.get("peers") or {}).items():
        state = p.get("state")
        rcd = p.get("pfxRcd", p.get("prefixReceivedCount"))
        if state == "Established" and isinstance(rcd, int):
            af6 = "negotiated"
        elif state in ("Established", "NoNeg"):
            af6 = "NoNeg"
        else:
            af6 = state
        out[addr] = {"state": state, "af6": af6, "pfxRcd": rcd, "pfxSnt": p.get("pfxSnt")}
    return out


def main():
    me = metadata("instance/name")
    pub = public_key()
    cfg = secret_config()
    report = {"publicKey": pub, "applied": None, "error": None, "instance": me}
    if cfg.get("nodes", {}).get(me):
        report["tunnelAddress"] = cfg["nodes"][me]["address"]
        try:
            apply(cfg, me)
            report["applied"] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
        except Exception as e:  # report it; the next run retries
            report["error"] = str(e)[:500]
        report["state"] = state(cfg, me)
    cache = STATE + "/last-report"
    stale = not os.path.exists(cache) or (datetime.datetime.now().timestamp() - os.path.getmtime(cache)) > 600
    cmp_body = json.dumps({k: v for k, v in report.items() if k != "state"}, sort_keys=True) + json.dumps(
        {k: v for k, v in report.get("state", {}).items() if k != "wg"}, sort_keys=True)
    if stale or open(cache).read() != cmp_body:
        publish(dict(report, time=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        open(cache, "w").write(cmp_body)
    if report["error"]:
        print(report["error"], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

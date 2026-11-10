#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §10): the AWS edge router's agent. Runs on the instance (systemd timer), standard library
only. Pulls the site's desired configuration from SSM Parameter Store, applies it, and publishes the node's WireGuard
public key and its state back, so the router needs no inbound management access and its private key never leaves it.

  /shc/edge/<site>/config            SecureString JSON written by the automation (shc_aws_edge.py)
  /shc/edge/<site>/nodes/<instance>  String JSON written here: publicKey, tunnelAddress, applied, state

config: {"site", "asn", "tunnel": {"cidr", "mtu", "listenPort"}, "bgpPassword"?, "placeholderV4Pool"?,
         "nodes": {<instance-id>: {"name", "address"}},
         "gateways": [{"name", "publicKey", "address"}],
         "vrfs": [{"name", "vni", "mtu", "tgwAsn", "connect": {<instance-id>: {"local", "remote", "inside", "bgp": [..]}},
                   "testAddress"?: {<instance-id>: "a.b.c.d"},
                   "ipv6"?: {"enabled", "prefixes6": [..], "network6": [..], "source": "Network" | "Provider",
                             "internet", "inside6": {<instance-id>: {"inside": "<a>/125", "tgw": [..]}},
                             "icmp6": {<instance-id>: "<a>/128"}}}]}

placeholderV4Pool (the IPv4 range IPv6-only VPCs take their never-routed IPv4 CIDR from): every network VRF's IPv4
route-map from the transit gateway denies it and everything inside it before its permit, so such a CIDR never
enters the VRF nor becomes an EVPN Type-5 route. Without the key the render is as before.

The transit gateway's IPv4 default (the static route toward the network's NAT egress VPC, present while the network
has internet access) is denied the same way in every network VRF: it serves the VPCs behind the transit gateway, not
the network's other sites, so it must not become a Type-5 route. It would also come and go with the network's
internet access, and on the site gateway (FRR 8.5) the withdrawal of a VTEP's last IPv4 Type-5 route drops that
VTEP's router MAC even while IPv6 Type-5 routes still use it, which blackholes the IPv6 traffic toward an IPv6-only
VPC.

IPv6 (design/IPV6-PLAN.md §6.1-§6.2), per VRF whose ipv6 block is enabled and names this router in inside6; a VRF
without an ipv6 block is configured exactly as before IPv6 existed:
- kernel: IPv6 forwarding; the GRE device takes the peer's /125 address (FRR's IPv6 next hop toward the transit
  gateway, and the source of the router's ICMPv6 errors toward the VPC); lo<vni> takes the router's ICMPv6 source
  /128 (icmp6, never announced); an unreachable IPv6 default in the VRF table, so a lookup that misses fails in the
  VRF instead of falling through to the main table.
- FRR, on the IPv4 sessions to the transit gateway (it has no IPv6 BGP peering): address-family ipv6 unicast with
  route-maps per family (RFC 8212). In from the transit gateway: the placement's prefixes (prefixes6: the VPC's IPv6
  blocks) only, never a default route. Out to it: the network's IPv6 space (network6) learned from the fabric, never
  the placement's own prefixes, and ::/0 only for source Network with internet (the fabric's IPv6 egress; internet is
  set only for a network whose IPv6 the platform assigned and that asked for internet). Source Provider: the network's
  space only (the VPC's own internet gateway carries its default route); a provider-assigned prefix of another cloud
  is outside network6 and is never announced into this cloud. advertise ipv6 unicast in the VRF's EVPN family, limited
  to prefixes6 (Type-5 toward the site's gateway). zebra installs the VRF's BGP routes toward the fabric with the
  icmp6 /128 as their source (ipv6 protocol bgp route-map <vrf>-src6), so ICMPv6 errors toward the fabric leave from
  it; routes toward the transit gateway keep the GRE address.
"""
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import subprocess
import sys
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
REGION = _local("region")


def imds(path, token=None):
    if token is None:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        token = urllib.request.urlopen(req, timeout=5).read().decode()
    req = urllib.request.Request("http://169.254.169.254/latest/" + path, headers={"X-aws-ec2-metadata-token": token})
    return urllib.request.urlopen(req, timeout=5).read().decode(), token


def ssm(target, body):
    """One SSM JSON API call, signed with SigV4 from the instance role."""
    role, tok = imds("meta-data/iam/security-credentials/")
    cred = json.loads(imds("meta-data/iam/security-credentials/" + role.strip(), tok)[0])
    host = "ssm.%s.amazonaws.com" % REGION
    payload = json.dumps(body).encode()
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": "application/x-amz-json-1.1", "host": host, "x-amz-date": amzdate,
               "x-amz-security-token": cred["Token"], "x-amz-target": "AmazonSSM." + target}
    signed = ";".join(sorted(headers))
    canon = "\n".join(["POST", "/", "", "".join("%s:%s\n" % (k, headers[k]) for k in sorted(headers)), signed,
                       hashlib.sha256(payload).hexdigest()])
    scope = "%s/%s/ssm/aws4_request" % (day, REGION)
    sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = ("AWS4" + cred["SecretAccessKey"]).encode()
    for part in (day, REGION, "ssm", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
        cred["AccessKeyId"], scope, signed, sig)
    req = urllib.request.Request("https://" + host + "/", data=payload, method="POST", headers=headers)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"ParameterNotFound" in e.read():
            return None
        raise


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


def v6_of(v, me):
    """The VRF's ipv6 block when IPv6 is on for it on this router (enabled, a /125 here and a Connect peer), else None."""
    six = v.get("ipv6") or {}
    if six.get("enabled") and (six.get("inside6") or {}).get(me) and (v.get("connect") or {}).get(me):
        return six
    return None


def _pl6(name, seq, action, prefix):
    """One ipv6 prefix-list line: the prefix and every longer one inside it (exactly ::/0 for the default route)."""
    n = ipaddress.ip_network(prefix)
    more = "" if n.prefixlen in (0, 128) else " le 128"
    return "ipv6 prefix-list %s seq %d %s %s%s" % (name, seq, action, n, more)


def frr_policy6(v, six, icmp):
    """The VRF's IPv6 prefix lists and route-maps (see the module docstring)."""
    name = v["name"]
    own = list(six.get("prefixes6") or [])
    out = [_pl6(name + "-in6", 5 * (i + 1), "permit", p) for i, p in enumerate(own)]
    if not own:
        out.append("ipv6 prefix-list %s-in6 seq 5 deny ::/0 le 128" % name)
    seq, permits = 0, 0
    lines = []
    for p in own:
        seq += 5
        lines.append(_pl6(name + "-out6", seq, "deny", p))
    for p in six.get("network6") or []:
        seq += 5
        permits += 1
        lines.append(_pl6(name + "-out6", seq, "permit", p))
    if six.get("source", "Network") == "Network" and six.get("internet"):
        seq += 5
        permits += 1
        lines.append(_pl6(name + "-out6", seq, "permit", "::/0"))
    if not permits:
        seq += 5
        lines.append("ipv6 prefix-list %s-out6 seq %d deny ::/0 le 128" % (name, seq))
    out += lines
    out += ["route-map %s-tgw-in6 permit 10" % name, " match ipv6 address prefix-list %s-in6" % name, "exit",
            "route-map %s-tgw-out6 permit 10" % name, " match ipv6 address prefix-list %s-out6" % name, "exit",
            "route-map %s-evpn6 permit 10" % name, " match ipv6 address prefix-list %s-in6" % name, "exit"]
    if icmp:
        # zebra: the transit gateway's routes as they are, every other BGP route with the ICMPv6 source as its source
        out += ["route-map %s-src6 permit 10" % name, " match ipv6 address prefix-list %s-in6" % name, "exit",
                "route-map %s-src6 permit 20" % name, " set src %s" % icmp.split("/")[0], "exit"]
    return out + ["!"]


def default4(name):
    """The deny of the transit gateway's IPv4 default ahead of a VRF's tgw-in permit (see the module docstring)."""
    return ["ip prefix-list %s-default4 seq 5 permit 0.0.0.0/0" % name,
            "route-map %s-tgw-in deny 4" % name, " match ip address prefix-list %s-default4" % name, "exit"]


def placeholder4(name, pool):
    """The deny of the placeholder IPv4 range ahead of a VRF's tgw-in permit (see the module docstring)."""
    n = ipaddress.ip_network(pool)
    more = "" if n.prefixlen == 32 else " le 32"
    return ["ip prefix-list %s-placeholder4 seq 5 permit %s%s" % (name, n, more),
            "route-map %s-tgw-in deny 5" % name, " match ip address prefix-list %s-placeholder4" % name, "exit"]


def frr_conf(cfg, me, node):
    gws = cfg.get("gateways", [])
    pw = cfg.get("bgpPassword")
    pool4 = cfg.get("placeholderV4Pool")
    out = ["frr defaults traditional", "hostname %s" % node["name"], "service integrated-vtysh-config", "!"]
    for v in cfg.get("vrfs", []):
        six = v6_of(v, me)
        icmp = (six.get("icmp6") or {}).get(me) if six else None
        out += ["vrf %s" % v["name"], " vni %d" % v["vni"]]
        if icmp:
            out.append(" ipv6 protocol bgp route-map %s-src6" % v["name"])
        out += ["exit-vrf", "!"]
        # FRR 8.5 sends and accepts nothing on eBGP without policy (RFC 8212, also under "traditional"). The transit
        # gateway's route table belongs to this network alone: take its routes (an IPv6-only VPC's placeholder IPv4
        # CIDR and the IPv4 default excepted), give it the network's
        out += default4(v["name"])
        if pool4:
            out += placeholder4(v["name"], pool4)
        out += ["route-map %s-tgw-in permit 10" % v["name"], "exit", "route-map %s-tgw-out permit 10" % v["name"],
                "exit", "!"]
        if six:
            out += frr_policy6(v, six, icmp)
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
        c = v.get("connect", {}).get(me)
        out += ["router bgp %d vrf %s" % (cfg["asn"], v["name"]), " bgp router-id %s" % rid,
                " no bgp default ipv4-unicast"]
        if c:
            out += [" neighbor TGW peer-group", " neighbor TGW remote-as %d" % v["tgwAsn"],
                    " neighbor TGW ebgp-multihop 2", " neighbor TGW timers connect 10"]
            out += [" neighbor %s peer-group TGW" % a for a in c["bgp"]]
        out += [" address-family ipv4 unicast"]
        if c:
            out += ["  neighbor TGW activate", "  neighbor TGW route-map %s-tgw-in in" % v["name"],
                    "  neighbor TGW route-map %s-tgw-out out" % v["name"]]
        if v.get("testAddress", {}).get(me):
            out.append("  redistribute connected route-map %s-connected" % v["name"])
        out += [" exit-address-family"]
        six = v6_of(v, me)
        if six:
            # IPv6 NLRI on the IPv4 sessions; FRR sends the GRE device's /125 address as the next hop
            out += [" address-family ipv6 unicast", "  neighbor TGW activate",
                    "  neighbor TGW route-map %s-tgw-in6 in" % v["name"],
                    "  neighbor TGW route-map %s-tgw-out6 out" % v["name"], " exit-address-family"]
        out += [" address-family l2vpn evpn", "  advertise ipv4 unicast"]
        if six:
            out.append("  advertise ipv6 unicast route-map %s-evpn6" % v["name"])
        out += [" exit-address-family", "exit", "!"]
    return "\n".join(out) + "\n"


def addrs6(dev, want):
    """Exactly these global IPv6 addresses on dev (nodad: a /125 or /128 the router alone holds)."""
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


def input_nft(vrfs, peers):
    """The router's input filter for its network VRFs (table inet shc_edge_in, replaced as a whole; "" without VRFs):
    what a cloud workload or a fabric host reaches of the router itself through a VRF. The router's addresses in a
    VRF (the cloud leg's, the test address, the ICMPv6 source) are local there, and FRR (and, with tcp_l3mdev_accept,
    any default-VRF listener) would answer. Passes: BGP with the cloud's peers on their own leg (peers: [(device,
    IPv4 address)], both directions), ICMP and ICMPv6, TCP segments with ACK or RST (replies to connections the router
    opened; stateless, so forwarded traffic is never tracked); everything else to the router from a VRF is dropped.
    The default VRF (the WireGuard tunnel, EVPN to the gateways, management) is not matched; forwarded traffic never
    meets the input hook."""
    if not vrfs:
        return ""
    m = "    iifname { %s }" % ", ".join('"%s"' % v for v in sorted(vrfs))
    out = ["table inet shc_edge_in", "delete table inet shc_edge_in", "table inet shc_edge_in {", "  chain input {",
           "    type filter hook input priority filter; policy accept;"]
    for dev, addr in sorted(set(peers)):
        for d in ("dport", "sport"):
            out.append('%s meta sdifname "%s" ip saddr %s tcp %s 179 accept' % (m, dev, addr, d))
    out += ["%s meta l4proto { icmp, ipv6-icmp } accept" % m, "%s tcp flags & (ack | rst) != 0 accept" % m,
            '%s counter drop comment "to the router from a network: only ICMP, TCP with ACK or RST and the cloud\'s BGP"' % m,
            "  }", "}"]
    return "\n".join(out) + "\n"


def input_filter(text):
    """load the input filter, or remove it when there is none"""
    if text:
        sh(["nft", "-f", "-"], inp=text)
    else:
        sh(["nft", "delete", "table", "inet", "shc_edge_in"], check=False)


def apply(cfg, me):
    node = cfg["nodes"][me]
    t = cfg["tunnel"]
    plen = ipaddress.ip_network(t["cidr"]).prefixlen
    eth = sh("ip -o route show default | awk '{print $5; exit}'").strip()
    local_ip = sh("ip -4 -o addr show dev %s | awk '{print $4; exit}'" % eth).strip().split("/")[0]
    # WireGuard: the gateways dial in (no endpoints here); the site's other routers are dialled over the VPC (their
    # routes reach the router through the gateways with the other router as next hop). Each peer may only use its
    # own tunnel address.
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
    # one VRF per network: L3VNI bridge + VXLAN over the tunnel, GRE to the transit gateway, a test loopback
    want_vrfs = {v["name"] for v in cfg.get("vrfs", [])}
    # the router's own input filter first (iifname and sdifname match by name, so before the devices exist)
    input_filter(input_nft(want_vrfs, [("gre%d" % v["vni"], a) for v in cfg.get("vrfs", [])
                                       for a in (v.get("connect", {}).get(me) or {}).get("bgp", [])]))
    for line in sh("ip -o link show type vrf").splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if name.startswith("t") and name not in want_vrfs:
            n = name[1:]
            for dev in ("gre" + n, "lo" + n, "vx" + n, "br" + n, name):
                if link_exists(dev):
                    sh(["ip", "link", "del", dev])
            # its IPv6 unreachable default sits on lo and outlives the VRF
            sh(["ip", "-6", "route", "del", "unreachable", "default", "metric", UNREACHABLE_METRIC, "table", n], check=False)
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
        c = v.get("connect", {}).get(me)
        if c:
            gre = "gre" + n
            if link_exists(gre):
                cur = sh(["ip", "-d", "link", "show", gre])
                if ("remote %s " % c["remote"]) not in cur or ("local %s " % local_ip) not in cur:
                    sh(["ip", "link", "del", gre])
            if not link_exists(gre):
                sh(["ip", "link", "add", gre, "type", "gre", "local", local_ip, "remote", c["remote"], "ttl", "255",
                    "dev", eth])
            sh(["ip", "link", "set", gre, "master", vrf, "mtu", "1476", "up"])
            sh(["ip", "addr", "replace", c["inside"], "dev", gre])
            if "ipv6" in v:
                six = v6_of(v, me)
                addrs6(gre, [six["inside6"][me]["inside"]] if six else [])
        elif link_exists("gre" + n):
            sh(["ip", "link", "del", "gre" + n])
        ta = v.get("testAddress", {}).get(me)
        six = v6_of(v, me)
        icmp = (six.get("icmp6") or {}).get(me) if six else None
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
    conf = frr_conf(cfg, me, node)
    path = "/etc/frr/frr.conf"
    if not os.path.exists(path) or open(path).read() != conf:
        old = os.umask(0o027)
        open(path + ".new", "w").write(conf)
        os.umask(old)
        sh(["chown", "frr:frr", path + ".new"])
        os.replace(path + ".new", path)
        if subprocess.run(["systemctl", "is-active", "--quiet", "frr"]).returncode == 0:
            sh(["systemctl", "reload", "frr"])
        else:
            sh(["systemctl", "enable", "--now", "frr"])


def state(cfg, me):
    st = {"wg": [], "bgp": {}}
    for line in sh(["wg", "show", "wg0", "latest-handshakes"], check=False).splitlines():
        k, ts = line.split()
        st["wg"].append({"publicKey": k, "latestHandshake": int(ts)})
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
    # each network VRF's routes (prefix -> protocol), so the automation can see what reached the router
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
        # where each selected IPv6 route leads (next hop and device): toward the transit gateway (gre<vni>) or back
        # into the fabric (br<vni>, the gateway's VTEP); kept compact, the report is one standard parameter
        st.setdefault("nh6", {})[v["name"]] = nexthops6(j)
    if six:
        st["fwd6"] = snmp6()
    return st


# the kernel's IPv6 forwarding and drop counters (/proc/net/snmp6), to tell where forwarded packets go
SNMP6_KEYS = ("Ip6InReceives", "Ip6OutForwDatagrams", "Ip6InNoRoutes", "Ip6OutNoRoutes", "Ip6InDiscards",
              "Ip6OutDiscards", "Ip6InHdrErrors", "Ip6InAddrErrors", "Icmp6OutTimeExcds", "Icmp6OutDestUnreachs",
              "Icmp6OutPktTooBigs")


def snmp6(path="/proc/net/snmp6"):
    out = {}
    try:
        for line in open(path):
            parts = line.split()
            if len(parts) == 2 and parts[0] in SNMP6_KEYS:
                out[parts[0]] = int(parts[1])
    except (OSError, ValueError):
        pass
    return out


def nexthops6(routes):
    """{prefix: ["<next hop>@<device>", ...]} of the selected route of each prefix in a 'show ipv6 route vrf .. json'."""
    out = {}
    for p, rs in routes.items():
        if not isinstance(rs, list):
            continue
        for r in rs:
            if not r.get("selected"):
                continue
            hops = []
            for nh in r.get("nexthops") or []:
                if nh.get("active") is False:
                    continue
                via = nh.get("ip") or ("unreachable" if nh.get("unreachable") else "direct")
                hops.append("%s@%s" % (via, nh.get("interfaceName", "-")))
            out[p] = sorted(hops)
    return out


def bgp6_peers(j):
    """The transit gateway sessions of a VRF's IPv6 unicast summary: per peer its state, whether the IPv6 family was
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
    me, _ = imds("meta-data/instance-id")
    pub = public_key()
    node_param = "/shc/edge/%s/nodes/%s" % (SITE, me)
    r = ssm("GetParameter", {"Name": "/shc/edge/%s/config" % SITE, "WithDecryption": True})
    cfg = json.loads(r["Parameter"]["Value"]) if r else {}
    report = {"publicKey": pub, "applied": None, "error": None}
    if cfg.get("nodes", {}).get(me):
        report["tunnelAddress"] = cfg["nodes"][me]["address"]
        try:
            apply(cfg, me)
            report["applied"] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
        except Exception as e:  # report it; the next run retries
            report["error"] = str(e)[:500]
        report["state"] = state(cfg, me)
    # publish only on change (state without timestamps), at least every 10 minutes
    cache = STATE + "/last-report"
    body = json.dumps(report, sort_keys=True)
    stale = not os.path.exists(cache) or (datetime.datetime.now().timestamp() - os.path.getmtime(cache)) > 600
    # change = anything but the handshake times (sessions, prefix counts, routes)
    cmp_body = json.dumps({k: v for k, v in report.items() if k != "state"}, sort_keys=True) + json.dumps(
        {k: v for k, v in report.get("state", {}).items() if k != "wg"}, sort_keys=True)
    if stale or not os.path.exists(cache) or open(cache).read() != cmp_body:
        ssm("PutParameter", {"Name": node_param, "Type": "String", "Overwrite": True,
                             "Value": json.dumps(dict(report, time=datetime.datetime.now(datetime.timezone.utc)
                                                      .strftime("%Y-%m-%dT%H:%M:%SZ")))})
        open(cache, "w").write(cmp_body)
    if report["error"]:
        print(report["error"], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

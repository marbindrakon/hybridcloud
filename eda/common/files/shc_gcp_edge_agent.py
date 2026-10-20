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
                   "vpn": {<instance name>: [{"ifId", "peerIp", "inside", "bgp": [..], "psk"}]},
                   "testAddress"?: {<instance name>: "a.b.c.d"}}]}
Per network VRF: an L3VNI bridge + VXLAN over the WireGuard tunnel (the fabric side), and one XFRM interface
(ipsec<ifId>, Libreswan ipsec-interface) per HA VPN tunnel inside the VRF with eBGP to the Cloud Router (the cloud
side).
"""
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

STATE = "/etc/shc-edge"
KEY = STATE + "/wg.key"
SITE = open(STATE + "/site").read().strip()
PROJECT = open(STATE + "/project").read().strip()
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


def frr_conf(cfg, me, node):
    gws = cfg.get("gateways", [])
    pw = cfg.get("bgpPassword")
    out = ["frr defaults traditional", "hostname %s" % node["name"], "service integrated-vtysh-config", "!"]
    for v in cfg.get("vrfs", []):
        out += ["vrf %s" % v["name"], " vni %d" % v["vni"], "exit-vrf", "!"]
        # FRR 8.5 sends and accepts nothing on eBGP without policy (RFC 8212). The HA VPN belongs to this network's
        # VPC alone: take its routes, give it the network's
        out += ["route-map %s-vpn-in permit 10" % v["name"], "exit", "route-map %s-vpn-out permit 10" % v["name"],
                "exit", "!"]
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
        out += [" exit-address-family", " address-family l2vpn evpn", "  advertise ipv4 unicast",
                " exit-address-family", "exit", "!"]
    return "\n".join(out) + "\n"


def ipsec_conf(cfg, me, node, local_ip, gateway):
    """Libreswan: one route-based (XFRM interface) IKEv2 tunnel per HA VPN gateway interface, PSK. Google Cloud
    sees the router at its external address (one-to-one NAT), so the router identifies itself by it. leftnexthop is
    the subnet gateway: Libreswan routes the encrypted packets through its table 50, and on GCE (a /32 on eth0) a
    link-scope host route to the peer never resolves ARP, so nothing leaves (P11 spike, 10-20)."""
    ext = node.get("externalIp", "")
    conns, secrets = [], []
    for v in cfg.get("vrfs", []):
        for t in v.get("vpn", {}).get(me, []):
            name = "shc-%d-%d" % (v["vni"], t["ifId"])
            conns += ["conn %s" % name,
                      "    type=tunnel", "    authby=secret", "    ikev2=insist",
                      "    left=%s" % local_ip, "    leftid=%s" % ext, "    leftnexthop=%s" % gateway,
                      "    right=%s" % t["peerIp"], "    rightid=%s" % t["peerIp"],
                      "    leftsubnet=0.0.0.0/0", "    rightsubnet=0.0.0.0/0",
                      # Google's recommended AEAD set: AES-GCM-16-256, PRF SHA2-512, DH group 20 (ECP-384), PFS
                      "    ike=aes_gcm256-sha2_512;dh20", "    esp=aes_gcm256;dh20", "    pfs=yes",
                      "    ikelifetime=36000s", "    salifetime=10800s",
                      "    dpddelay=10", "    dpdaction=restart",
                      "    encapsulation=yes", "    ipsec-interface=%d" % t["ifId"],
                      "    auto=start", ""]
            secrets.append('%s %s : PSK "%s"' % (ext, t["peerIp"], t["psk"]))
    return "\n".join(conns), "\n".join(secrets) + ("\n" if secrets else "")


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
    # one VRF per network: L3VNI bridge + VXLAN over the tunnel, the HA VPN's XFRM interfaces, a test loopback
    want_vrfs = {v["name"] for v in cfg.get("vrfs", [])}
    for line in sh("ip -o link show type vrf").splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if name.startswith("t") and name not in want_vrfs:
            n = name[1:]
            for dev in ("lo" + n, "vx" + n, "br" + n, name):
                if link_exists(dev):
                    sh(["ip", "link", "del", dev])
    # Libreswan first (it creates the XFRM interfaces), then the VRF membership
    conf, secrets = ipsec_conf(cfg, me, node, local_ip, gateway)
    changed = write_if_changed(IPSEC_CONF, conf + "\n")
    changed |= write_if_changed(IPSEC_SECRETS, secrets, mode=0o600)
    if subprocess.run(["systemctl", "is-active", "--quiet", "ipsec"]).returncode != 0:
        sh(["systemctl", "enable", "--now", "ipsec"])
        changed = True
    if changed:
        sh(["ipsec", "auto", "--rereadsecrets"], check=False)
        for v in cfg.get("vrfs", []):
            for tn in v.get("vpn", {}).get(me, []):
                sh(["ipsec", "auto", "--replace", "shc-%d-%d" % (v["vni"], tn["ifId"])], check=False)
                sh(["ipsec", "auto", "--asynchronous", "--up", "shc-%d-%d" % (v["vni"], tn["ifId"])], check=False)
        # connections no longer configured
        listed = [l.split('"')[1] for l in sh(["ipsec", "auto", "--status"], check=False).splitlines() if l.startswith('"shc-')]
        wanted = {"shc-%d-%d" % (v["vni"], tn["ifId"]) for v in cfg.get("vrfs", []) for tn in v.get("vpn", {}).get(me, [])}
        for name in set(listed) - wanted:
            sh(["ipsec", "auto", "--delete", name], check=False)
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
        for tn in v.get("vpn", {}).get(me, []):
            dev = "ipsec%d" % tn["ifId"]
            if not link_exists(dev):  # Libreswan creates it on --add; make sure it exists for the VRF and FRR
                sh(["ip", "link", "add", dev, "type", "xfrm", "if_id", str(tn["ifId"])], check=False)
            sh(["ip", "link", "set", dev, "master", vrf, "mtu", str(int(cfg.get("vpnMtu", 1406))), "up"], check=False)
            sh(["ip", "addr", "replace", tn["inside"], "dev", dev], check=False)
        ta = v.get("testAddress", {}).get(me)
        if ta:
            if not link_exists("lo" + n):
                sh(["ip", "link", "add", "lo" + n, "type", "dummy"])
            sh(["ip", "link", "set", "lo" + n, "master", vrf, "up"])
            sh(["ip", "addr", "replace", ta + "/32", "dev", "lo" + n])
        elif link_exists("lo" + n):
            sh(["ip", "link", "del", "lo" + n])
    # FRR: rewrite and reload only on change
    if write_if_changed("/etc/frr/frr.conf", frr_conf(cfg, me, node), owner="frr:frr"):
        if subprocess.run(["systemctl", "is-active", "--quiet", "frr"]).returncode == 0:
            sh(["systemctl", "reload", "frr"])
        else:
            sh(["systemctl", "enable", "--now", "frr"])


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
        for af in j.values():
            if isinstance(af, dict):
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
    return st


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

#!/usr/bin/env python3
"""shc (design/P8-PLAN.md §10): the AWS edge router's agent. Runs on the instance (systemd timer), standard library
only. Pulls the site's desired configuration from SSM Parameter Store, applies it, and publishes the node's WireGuard
public key and its state back, so the router needs no inbound management access and its private key never leaves it.

  /shc/edge/<site>/config            SecureString JSON written by the automation (shc_aws_edge.py)
  /shc/edge/<site>/nodes/<instance>  String JSON written here: publicKey, tunnelAddress, applied, state

config: {"site", "asn", "tunnel": {"cidr", "mtu", "listenPort"}, "bgpPassword"?,
         "nodes": {<instance-id>: {"name", "address"}},
         "gateways": [{"name", "publicKey", "address"}],
         "vrfs": [{"name", "vni", "mtu", "tgwAsn", "connect": {<instance-id>: {"local", "remote", "inside", "bgp": [..]}},
                   "testAddress"?: {<instance-id>: "a.b.c.d"}}]}
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
SITE = open(STATE + "/site").read().strip()
REGION = open(STATE + "/region").read().strip()


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


def frr_conf(cfg, me, node):
    gws = cfg.get("gateways", [])
    pw = cfg.get("bgpPassword")
    out = ["frr defaults traditional", "hostname %s" % node["name"], "service integrated-vtysh-config", "!"]
    for v in cfg.get("vrfs", []):
        out += ["vrf %s" % v["name"], " vni %d" % v["vni"], "exit-vrf", "!"]
        # FRR 8.5 sends and accepts nothing on eBGP without policy (RFC 8212, also under "traditional"). The transit
        # gateway's route table belongs to this network alone: take its routes, give it the network's
        out += ["route-map %s-tgw-in permit 10" % v["name"], "exit", "route-map %s-tgw-out permit 10" % v["name"],
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
        out += [" exit-address-family", " address-family l2vpn evpn", "  advertise ipv4 unicast",
                " exit-address-family", "exit", "!"]
    return "\n".join(out) + "\n"


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
    # one VRF per network: L3VNI bridge + VXLAN over the tunnel, GRE to the transit gateway, a test loopback
    want_vrfs = {v["name"] for v in cfg.get("vrfs", [])}
    for line in sh("ip -o link show type vrf").splitlines():
        name = line.split(":")[1].strip().split("@")[0]
        if name.startswith("t") and name not in want_vrfs:
            n = name[1:]
            for dev in ("gre" + n, "lo" + n, "vx" + n, "br" + n, name):
                if link_exists(dev):
                    sh(["ip", "link", "del", dev])
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
        elif link_exists("gre" + n):
            sh(["ip", "link", "del", "gre" + n])
        ta = v.get("testAddress", {}).get(me)
        if ta:
            if not link_exists("lo" + n):
                sh(["ip", "link", "add", "lo" + n, "type", "dummy"])
            sh(["ip", "link", "set", "lo" + n, "master", vrf, "up"])
            sh(["ip", "addr", "replace", ta + "/32", "dev", "lo" + n])
        elif link_exists("lo" + n):
            sh(["ip", "link", "del", "lo" + n])
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
        for af in j.values():
            if isinstance(af, dict):
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
    return st


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

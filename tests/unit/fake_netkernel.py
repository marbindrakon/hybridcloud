"""A small stateful stand-in for the Linux network tools that eda/common/files/bgw/shc-fabric-apply calls (ip,
sysctl, nft, wg, modprobe, vtysh, and cat for /sys and /proc), for test_shc_fabric_apply.py.

Every tool is a wrapper script `exec python3 fake_netkernel.py <tool> "$@"`. The kernel state (namespaces, links,
addresses, IPv6 routes, sysctls, nftables tables, WireGuard devices) is a JSON file ($FAKE_STATE); every call is
appended to $FAKE_LOG as one line "<W|R> [<namespace>] <tool> <args>" (W = it changes the state). `ip netns exec
<ns> <cmd>` runs <cmd> with $FAKE_NS=<ns>, so the wrapped tools act inside that namespace. $FAKE_FAIL (a regular
expression, optional) makes every call whose "[<namespace>] <tool> <args>" matches it fail (rc 2, no change), as a
host would refuse it (a missing sysctl, IPv6 disabled).

Modelled kernel behaviour the script depends on: a device takes its IPv6 settings from the namespace's
net.ipv6.conf.default when it is created or moved into a namespace; writing net.ipv6.conf.all.disable_ipv6 or
.forwarding rewrites default and every device; an up device with IPv6 enabled (and addrgenmode not none) has an
fe80::/64 link-local from its MAC; disable_ipv6=1 removes all IPv6 addresses; lo has ::1. sysctl keys follow procps:
when the first separator is "/", "/" separates (a device name may hold a dot), else ".".
Nothing here touches the real system."""
import fcntl
import ipaddress
import json
import os
import re
import sys

V6_ATTRS = ("disable_ipv6", "forwarding", "accept_ra", "autoconf")
NO_LL_KINDS = ("vrf", "wireguard", "loopback")


# ---------------------------------------------------------------------------------------------------------- state
def new_ns(defaults):
    ns = {"links": {}, "sysctl": {}, "routes6": [], "proxy": {}, "nft": {}, "wg": {}}
    for k in ("all", "default"):
        for a, v in defaults.items():
            ns["sysctl"][f"net/ipv6/conf/{k}/{a}"] = v
    return ns


# a new namespace on the blades: net.core.devconf_inherit_init_net=0, so the kernel's defaults
NETNS_DEFAULTS = {"disable_ipv6": "0", "forwarding": "0", "accept_ra": "1", "autoconf": "1"}


def mac_ll(mac):
    b = [int(x, 16) for x in mac.split(":")]
    b[0] ^= 2
    e = b[:3] + [0xFF, 0xFE] + b[3:]
    return str(ipaddress.IPv6Address("fe80::" + ":".join("%02x%02x" % (e[i], e[i + 1]) for i in range(0, 8, 2))))


class Kernel:
    def __init__(self, st):
        self.st = st

    def ns(self, name):
        return self.st["ns"][name]

    def next_index(self):
        self.st["ifindex"] = self.st.get("ifindex", 100) + 1
        return self.st["ifindex"]

    def new_mac(self):
        i = self.next_index()
        return "02:00:00:00:%02x:%02x" % (i // 256, i % 256)

    def add_link(self, nsname, name, kind, **kw):
        ns = self.ns(nsname)
        link = {"name": name, "kind": kind, "mtu": kw.get("mtu", 65536 if kind == "loopback" else 1500),
                "up": False, "master": None, "address": kw.get("address") or self.new_mac(),
                "index": self.next_index(), "addrgenmode": "eui64", "addrs": [], "parent": kw.get("parent"),
                "table": kw.get("table"), "vlan": kw.get("vlan"), "peer": kw.get("peer")}
        ns["links"][name] = link
        self.inherit6(nsname, name)
        return link

    def inherit6(self, nsname, dev):
        sc = self.ns(nsname)["sysctl"]
        for a in V6_ATTRS:
            sc[f"net/ipv6/conf/{dev}/{a}"] = sc.get(f"net/ipv6/conf/default/{a}", NETNS_DEFAULTS[a])

    def drop6(self, nsname, dev):
        sc = self.ns(nsname)["sysctl"]
        for k in [k for k in sc if k.startswith(f"net/ipv6/conf/{dev}/") or k.startswith(f"net/mpls/conf/{dev}/")
                  or k.startswith(f"net/ipv4/conf/{dev}/")]:
            del sc[k]

    def refresh6(self, nsname, dev):
        ns = self.ns(nsname)
        link = ns["links"].get(dev)
        if not link:
            return
        if ns["sysctl"].get(f"net/ipv6/conf/{dev}/disable_ipv6", "0") == "1":
            link["addrs"] = [a for a in link["addrs"] if a["family"] != 6]
            return
        if not link["up"]:
            return
        if link["kind"] == "loopback":
            want = "::1"
            plen = 128
        elif link["kind"] in NO_LL_KINDS or link["addrgenmode"] == "none":
            return
        else:
            want = mac_ll(link["address"])
            plen = 64
        if not any(a["local"] == want for a in link["addrs"]):
            link["addrs"].append({"family": 6, "local": want, "prefixlen": plen, "nodad": False})

    def del_link(self, nsname, dev):
        ns = self.ns(nsname)
        link = ns["links"].pop(dev)
        self.drop6(nsname, dev)
        for other in ns["links"].values():
            if other["master"] == dev:
                other["master"] = None
        if link["kind"] == "veth" and link["peer"]:
            pns, pname = link["peer"]
            if pname in self.ns(pns)["links"]:
                self.ns(pns)["links"][pname]["peer"] = None
                self.del_link(pns, pname)
        ns["wg"].pop(dev, None)

    def move_link(self, nsname, dev, target):
        link = self.ns(nsname)["links"].pop(dev)
        self.drop6(nsname, dev)
        link["up"] = False
        link["master"] = None
        link["addrs"] = []
        self.ns(target)["links"][dev] = link
        self.inherit6(target, dev)
        if link["kind"] == "veth" and link["peer"]:
            pns, pname = link["peer"]
            self.ns(pns)["links"][pname]["peer"] = [target, dev]
        wg = self.ns(nsname)["wg"].pop(dev, None)
        if wg is not None:
            self.ns(target)["wg"][dev] = wg


class Fail(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------------------------------------------------- ip
def link_line(link, ns, detail=False):
    name = link["name"] + (("@" + link["parent"]) if link["parent"] else "")
    if link["kind"] == "veth" and link["peer"]:
        pns, pname = link["peer"]
        name = link["name"] + ("@" + pname if pns == ns else "@if%d" % 99)
    flags = "<LOOPBACK,UP,LOWER_UP>" if link["kind"] == "loopback" else (
        "<BROADCAST,MULTICAST,UP,LOWER_UP>" if link["up"] else "<BROADCAST,MULTICAST>")
    s = "%d: %s: %s mtu %d qdisc noqueue" % (link["index"], name, flags, link["mtu"])
    if link["master"]:
        s += " master %s" % link["master"]
    s += " state %s mode DEFAULT group default" % ("UP" if link["up"] else "DOWN")
    if detail and link["kind"] == "vrf":
        s += "\n    link/ether %s\n    vrf table %s addrgenmode eui64" % (link["address"], link["table"])
    return s


def link_json(link, ns, detail=False):
    j = {"ifindex": link["index"], "ifname": link["name"], "mtu": link["mtu"],
         "operstate": "UP" if link["up"] else "DOWN", "address": link["address"]}
    if link["master"]:
        j["master"] = link["master"]
    if link["parent"]:
        j["link"] = link["parent"]
    if detail:
        info = {}
        if link["kind"] == "vlan":
            info = {"protocol": "802.1Q", "id": link["vlan"]}
        elif link["kind"] == "vrf":
            info = {"table": link["table"]}
        j["linkinfo"] = {"info_kind": link["kind"], "info_data": info}
    return j


def addr_text(a):
    if a["family"] == 4:
        return "    inet %s/%d scope global" % (a["local"], a["prefixlen"])
    scope = "link" if ipaddress.ip_address(a["local"]) in ipaddress.ip_network("fe80::/10") else "global"
    if a["local"] == "::1":
        scope = "host"
    return "    inet6 %s/%d scope %s%s " % (a["local"], a["prefixlen"], scope, " nodad" if a["nodad"] else "")


def parse_ifaddr(s):
    i = ipaddress.ip_interface(s)
    return i.version, str(i.ip), i.network.prefixlen


def ip_main(k, nsname, args, out):
    opts = set()
    while args and args[0].startswith("-"):
        opts.add(args.pop(0))
    fam = 6 if "-6" in opts else (4 if "-4" in opts else None)
    if not args:
        raise Fail("usage")
    obj, args = args[0], args[1:]
    ns = k.ns(nsname)
    links = ns["links"]

    def need(dev):
        if dev not in links:
            raise Fail('Device "%s" does not exist.' % dev)
        return links[dev]

    if obj == "netns":
        verb = args[0]
        if verb == "list":
            for n in sorted(k.st["ns"]):
                if n:
                    out.append("%s (id: 0)" % n)
            return False
        if verb == "add":
            if args[1] in k.st["ns"]:
                raise Fail('Cannot create namespace file "/run/netns/%s": File exists' % args[1])
            k.st["ns"][args[1]] = new_ns(NETNS_DEFAULTS)
            k.add_link(args[1], "lo", "loopback")
            return True
        if verb == "del":
            gone = k.st["ns"].pop(args[1])
            for l in list(gone["links"].values()):  # veth peers elsewhere go with it
                if l["kind"] == "veth" and l["peer"] and l["peer"][0] != args[1]:
                    pns, pname = l["peer"]
                    if pname in k.ns(pns)["links"]:
                        k.ns(pns)["links"][pname]["peer"] = None
                        k.del_link(pns, pname)
            return True
        raise Fail("netns verb " + verb)

    if obj == "link":
        verb = args[0] if args else "show"
        rest = args[1:]
        if verb == "show":
            sel = dict(zip(rest[0::2], rest[1::2])) if len(rest) % 2 == 0 else {}
            dev = None
            if len(rest) % 2 == 1:
                dev = rest[0]
                sel = dict(zip(rest[1::2], rest[2::2]))
            dev = sel.pop("dev", dev)
            if dev is not None:
                cand = [need(dev)]
            else:
                cand = sorted(links.values(), key=lambda l: l["index"])
            if "type" in sel:
                cand = [l for l in cand if l["kind"] == sel["type"]]
            if "master" in sel:
                cand = [l for l in cand if l["master"] == sel["master"]]
            if "-j" in opts:
                out.append(json.dumps([link_json(l, nsname, "-d" in opts) for l in cand]))
            else:
                for l in cand:
                    out.append(link_line(l, nsname, "-d" in opts))
            return False
        if verb == "add":
            return link_add(k, nsname, rest)
        if verb == "set":
            return link_set(k, nsname, rest)
        if verb in ("del", "delete"):
            need(rest[0])
            k.del_link(nsname, rest[0])
            return True
        raise Fail("link verb " + verb)

    if obj in ("addr", "address"):
        verb = args[0] if args else "show"
        rest = args[1:]
        if verb == "show":
            dev = rest[rest.index("dev") + 1] if "dev" in rest else None
            cand = [need(dev)] if dev else sorted(links.values(), key=lambda l: l["index"])
            if "-j" in opts:
                out.append(json.dumps([{"ifname": l["name"], "addr_info": [
                    {"family": "inet" if a["family"] == 4 else "inet6", "local": a["local"],
                     "prefixlen": a["prefixlen"]}
                    for a in l["addrs"] if fam is None or a["family"] == fam]} for l in cand]))
            elif "-br" in opts:
                for l in cand:
                    adds = [a for a in l["addrs"] if fam is None or a["family"] == fam]
                    if fam is not None and not adds:
                        continue
                    name = l["name"] + (("@" + l["parent"]) if l["parent"] else "")
                    state = "UNKNOWN" if l["kind"] == "loopback" else ("UP" if l["up"] else "DOWN")
                    out.append("%-16s %-14s %s " % (name, state, " ".join("%s/%d" % (a["local"], a["prefixlen"])
                                                                          for a in adds)))
            else:
                for l in cand:
                    out.append(link_line(l, nsname))
                    for a in l["addrs"]:
                        if fam is None or a["family"] == fam:
                            out.append(addr_text(a))
            return False
        if verb in ("add", "del", "replace"):
            addr = rest[0]
            dev = rest[rest.index("dev") + 1]
            l = need(dev)
            v, local, plen = parse_ifaddr(addr)
            have = [a for a in l["addrs"] if a["local"] == local and a["prefixlen"] == plen]
            if verb == "del":
                if not have:
                    raise Fail("RTNETLINK answers: Cannot assign requested address", 2)
                l["addrs"].remove(have[0])
                return True
            if v == 6 and ns["sysctl"].get(f"net/ipv6/conf/{dev}/disable_ipv6") == "1":
                raise Fail("RTNETLINK answers: Permission denied", 2)
            if have and verb == "add":
                raise Fail("RTNETLINK answers: File exists", 2)
            if have:
                l["addrs"].remove(have[0])
            l["addrs"].append({"family": v, "local": local, "prefixlen": plen, "nodad": "nodad" in rest[1:]})
            return True
        raise Fail("addr verb " + verb)

    if obj == "route":
        verb = args[0] if args else "show"
        rest = args[1:]
        if fam != 6:
            raise Fail("only IPv6 routes are modelled")
        if verb in ("show", "list"):
            sel = dict(zip(rest[0::2], rest[1::2]))
            table = sel.get("table", "main")
            routes = [r for r in ns["routes6"] if table == "all" or r["table"] == table]
            if "proto" in sel:
                routes = [r for r in routes if r.get("proto") == sel["proto"]]
            if "-j" in opts:
                out.append(json.dumps([{k2: v2 for k2, v2 in (("type", r["type"]), ("dst", r["dst"]),
                                                              ("gateway", r.get("via")), ("dev", r.get("dev")),
                                                              ("protocol", r.get("proto")),
                                                              ("metric", r["metric"])) if v2 is not None}
                                       for r in routes]))
            else:
                for r in routes:
                    s = ("unreachable " if r["type"] == "unreachable" else "") + r["dst"]
                    if r.get("via"):
                        s += " via " + r["via"]
                    s += " dev " + (r.get("dev") or "lo")
                    if r.get("proto"):
                        s += " proto " + r["proto"]
                    if table == "all" and r["table"] != "main":
                        s += " table " + r["table"]
                    out.append(s + " metric %s pref medium" % r["metric"])
            return False
        if verb == "replace":
            if rest[:2] != ["unreachable", "default"]:
                raise Fail("only unreachable defaults are modelled")
            sel = dict(zip(rest[2::2], rest[3::2]))
            r = {"type": "unreachable", "dst": "default", "metric": sel["metric"], "table": sel.get("table", "main")}
            ns["routes6"] = [x for x in ns["routes6"] if not (x["table"] == r["table"] and x["dst"] == "default"
                                                               and x["metric"] == r["metric"])]
            ns["routes6"].append(r)
            return True
        raise Fail("route verb " + verb)

    if obj == "neigh":
        verb = args[0]
        rest = args[1:]
        if verb == "show":
            dev = rest[rest.index("dev") + 1]
            for a in ns["proxy"].get(dev, []):
                out.append("%s dev %s proxy" % (a, dev))
            return False
        if verb in ("add", "del"):
            a, dev = rest[1], rest[rest.index("dev") + 1]
            lst = ns["proxy"].setdefault(dev, [])
            if verb == "add":
                lst.append(a)
            else:
                lst.remove(a)
            return True
        raise Fail("neigh verb " + verb)
    raise Fail("object " + obj)


def link_add(k, nsname, rest):
    kw = {}
    name = None
    i = 0
    while i < len(rest) and rest[i] != "type":
        t = rest[i]
        if t in ("link", "name", "address", "mtu"):
            kw[t] = rest[i + 1]
            i += 2
        else:
            name = t
            i += 1
    name = kw.pop("name", name)
    kind = rest[i + 1]
    extra = rest[i + 2:]
    if name in k.ns(nsname)["links"]:
        raise Fail("RTNETLINK answers: File exists", 2)
    mtu = int(kw["mtu"]) if "mtu" in kw else None
    common = {"address": kw.get("address"), "parent": kw.get("link")}
    if mtu:
        common["mtu"] = mtu
    if kind == "vrf":
        k.add_link(nsname, name, "vrf", table=extra[extra.index("table") + 1], **common)
    elif kind == "vlan":
        parent = k.ns(nsname)["links"][kw["link"]]
        common["mtu"] = mtu or parent["mtu"]
        common["address"] = parent["address"]
        k.add_link(nsname, name, "vlan", vlan=int(extra[extra.index("id") + 1]), **common)
    elif kind == "veth":
        pname = extra[extra.index("name") + 1]
        pns = extra[extra.index("netns") + 1] if "netns" in extra else nsname
        pmtu = int(extra[extra.index("mtu") + 1]) if "mtu" in extra else (mtu or 1500)
        k.add_link(nsname, name, "veth", peer=[pns, pname], **common)
        k.add_link(pns, pname, "veth", peer=[nsname, name], mtu=pmtu)
    elif kind in ("bridge", "vxlan", "wireguard", "macvlan", "dummy"):
        k.add_link(nsname, name, kind, **common)
        if kind == "wireguard":
            k.ns(nsname)["wg"][name] = {"key": None, "port": None, "peers": {}}
    else:
        raise Fail("link kind " + kind)
    return True


def link_set(k, nsname, rest):
    dev = rest[0]
    links = k.ns(nsname)["links"]
    if dev not in links:
        raise Fail('Cannot find device "%s"' % dev)
    l = links[dev]
    i = 1
    while i < len(rest):
        t = rest[i]
        if t == "up":
            l["up"] = True
            i += 1
        elif t == "down":
            l["up"] = False
            i += 1
        elif t == "mtu":
            l["mtu"] = int(rest[i + 1])
            i += 2
        elif t == "master":
            if rest[i + 1] not in links:
                raise Fail('Device "%s" does not exist.' % rest[i + 1])
            l["master"] = rest[i + 1]
            i += 2
        elif t == "address":
            l["address"] = rest[i + 1]
            i += 2
        elif t == "addrgenmode":
            l["addrgenmode"] = rest[i + 1]
            if rest[i + 1] == "none":
                l["addrs"] = [a for a in l["addrs"] if not (a["family"] == 6 and a["local"].startswith("fe80"))]
            i += 2
        elif t == "netns":
            k.move_link(nsname, dev, rest[i + 1])
            return True
        elif t == "type":
            return True  # bridge_slave options: not modelled
        else:
            raise Fail("link set " + t)
    k.refresh6(nsname, dev)
    return True


# --------------------------------------------------------------------------------------------------------- sysctl
def norm_key(key):
    for c in key:
        if c in "./":
            return key.split(c)
    return [key]


def sysctl_main(k, nsname, args, out, err):
    opts = ""
    while args and args[0].startswith("-"):
        opts += args.pop(0)[1:]
    sc = k.ns(nsname)["sysctl"]
    links = k.ns(nsname)["links"]
    rc = 0
    wrote = False
    for a in args:
        if "w" in opts or "=" in a:
            key, val = a.split("=", 1)
            p = norm_key(key)
            path = "/".join(p)
            if p[:3] in (["net", "ipv6", "conf"], ["net", "ipv4", "conf"], ["net", "mpls", "conf"]) and \
                    p[3] not in ("all", "default") and p[3] not in links:
                err.append("sysctl: cannot stat /proc/sys/%s: No such file or directory" % path)
                rc = 255
                continue
            sc[path] = val
            wrote = True
            if p[:3] == ["net", "ipv6", "conf"] and p[3] == "all" and p[4] in ("disable_ipv6", "forwarding"):
                sc["net/ipv6/conf/default/" + p[4]] = val
                for d in links:
                    sc[f"net/ipv6/conf/{d}/{p[4]}"] = val
            if p[:3] == ["net", "ipv6", "conf"] and p[4] == "disable_ipv6":
                for d in (links if p[3] == "all" else [p[3]] if p[3] in links else []):
                    k.refresh6(nsname, d)
            if "q" not in opts:
                out.append("%s = %s" % (".".join(p), val))
        else:
            path = "/".join(norm_key(a))
            if path not in sc:
                err.append("sysctl: cannot stat /proc/sys/%s: No such file or directory" % path)
                rc = 255
                continue
            out.append(sc[path] if "n" in opts else "%s = %s" % (a, sc[path]))
    return rc, wrote


# ------------------------------------------------------------------------------------------------------ nft, wg
def nft_main(k, nsname, args, out, stdin):
    ns = k.ns(nsname)
    if args[:2] == ["-f", "-"]:
        text = stdin
        heads = [l for l in text.splitlines() if l.rstrip().endswith("{") and l.startswith("table ")]
        key = " ".join(heads[0].split()[1:3])
        ns["nft"][key] = text
        return 0, True
    a = [x for x in args if x != "-s"]
    if a[:2] == ["list", "table"]:
        key = " ".join(a[2:4])
        if key not in ns["nft"]:
            return 1, False
        out.append(ns["nft"][key].rstrip("\n"))
        return 0, False
    if a[:2] == ["delete", "table"]:
        key = " ".join(a[2:4])
        if ns["nft"].pop(key, None) is None:
            return 1, False
        return 0, True
    raise Fail("nft " + " ".join(args))


def wg_main(k, nsname, args, out):
    ns = k.ns(nsname)
    if args == ["genkey"]:
        out.append("fake-wireguard-private-key-for-tests=")
        return False
    if args[0] == "set":
        w = ns["wg"][args[1]]
        rest = args[2:]
        i = 0
        while i < len(rest):
            t = rest[i]
            if t == "private-key":
                w["key"] = "set"
                i += 2
            elif t == "listen-port":
                w["port"] = rest[i + 1]
                i += 2
            elif t == "peer":
                pk = rest[i + 1]
                i += 2
                if i < len(rest) and rest[i] == "remove":
                    w["peers"].pop(pk, None)
                    i += 1
                    continue
                peer = w["peers"].setdefault(pk, {})
                while i < len(rest) and rest[i] in ("allowed-ips", "persistent-keepalive", "endpoint"):
                    peer[rest[i]] = rest[i + 1]
                    i += 2
            else:
                raise Fail("wg set " + t)
        return True
    if args[0] == "show":
        w = ns["wg"][args[1]]
        what = args[2]
        if what == "public-key":
            out.append("fake-public-key")
        elif what == "listen-port":
            out.append(str(w["port"]))
        elif what == "peers":
            out.extend(sorted(w["peers"]))
        elif what == "allowed-ips":
            out.extend("%s\t%s" % (p, v.get("allowed-ips", "")) for p, v in sorted(w["peers"].items()))
        elif what == "persistent-keepalive":
            out.extend("%s\t%s" % (p, v.get("persistent-keepalive", "off")) for p, v in sorted(w["peers"].items()))
        return False
    raise Fail("wg " + " ".join(args))


# ------------------------------------------------------------------------------------------------------------ cat
def cat_main(k, nsname, args, out, err):
    rc = 0
    for p in args:
        if p.startswith("/sys/class/net/"):
            dev, attr = p.split("/")[4:6]
            l = k.ns(nsname)["links"].get(dev)
            if not l:
                err.append("cat: %s: No such file or directory" % p)
                rc = 1
                continue
            out.append(str(l["address"] if attr == "address" else l[attr]))
        elif p.startswith("/proc/sys/"):
            key = p[len("/proc/sys/"):]
            sc = k.ns(nsname)["sysctl"]
            if key not in sc:
                err.append("cat: %s: No such file or directory" % p)
                rc = 1
                continue
            out.append(sc[key])
        else:
            raise Fail("cat of a real file: %s" % p)
    return rc


# ----------------------------------------------------------------------------------------------------------- main
def main():
    tool, args = sys.argv[1], sys.argv[2:]
    if tool == "cat" and not any(a.startswith(("/sys/", "/proc/")) for a in args):
        os.execv("/bin/cat", ["cat"] + args)
    nsname = os.environ.get("FAKE_NS", "")
    if tool == "ip" and args[:2] == ["netns", "exec"]:
        os.environ["FAKE_NS"] = args[2]
        os.execvp(args[3], args[3:])
    stdin = sys.stdin.read() if tool == "nft" and args[:2] == ["-f", "-"] else ""
    with open(os.environ["FAKE_STATE"] + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(os.environ["FAKE_STATE"]) as f:
            st = json.load(f)
        k = Kernel(st)
        out, err = [], []
        rc, wrote = 0, False
        try:
            if nsname not in st["ns"]:
                raise Fail('Cannot open network namespace "%s": No such file or directory' % nsname)
            if os.environ.get("FAKE_FAIL") and re.search(os.environ["FAKE_FAIL"], "[%s] %s %s" % (
                    nsname or "root", tool, " ".join(args))):
                raise Fail("%s: Operation not permitted (refused by the test)" % tool, 2)
            if tool == "ip":
                wrote = ip_main(k, nsname, list(args), out)
            elif tool == "sysctl":
                rc, wrote = sysctl_main(k, nsname, list(args), out, err)
            elif tool == "nft":
                rc, wrote = nft_main(k, nsname, list(args), out, stdin)
            elif tool == "wg":
                wrote = wg_main(k, nsname, list(args), out)
            elif tool == "cat":
                rc = cat_main(k, nsname, list(args), out, err)
            elif tool == "modprobe":
                wrote = True
            elif tool == "vtysh":
                pass
            else:
                raise Fail("unknown tool " + tool)
        except Fail as e:
            err.append(str(e))
            rc = e.code
            wrote = False
        if wrote:
            with open(os.environ["FAKE_STATE"] + ".tmp", "w") as f:
                json.dump(st, f, indent=1, sort_keys=True)
            os.replace(os.environ["FAKE_STATE"] + ".tmp", os.environ["FAKE_STATE"])
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write("%s [%s] %s %s%s\n" % ("W" if wrote else "R", nsname or "root", tool, " ".join(args),
                                         "" if rc == 0 else "  -> rc=%d" % rc))
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    if err:
        sys.stderr.write("\n".join(err) + "\n")
    sys.exit(rc)


if __name__ == "__main__":
    main()

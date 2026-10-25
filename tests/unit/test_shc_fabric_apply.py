"""shc-fabric-apply (eda/common/files/bgw/shc-fabric-apply), the border gateway's kernel side, run for real against
a fake kernel (fake_netkernel.py: ip, sysctl, nft, wg, modprobe, vtysh and cat for /sys and /proc are wrapper scripts
first in PATH; jq, python3 and the shell tools are the real ones). Covers the IPv6 core keys of the apply JSON
(loopback6, icmp6, loopbackPool6, icmpSource6): without them no IPv6 command at all; with them the namespace sysctls,
the core and site legs, the loopback, the VRFs' unreachable defaults and ICMP sources, in the order the kernel needs,
and a second run that reads changed=no. The addresses are documentation prefixes.

SHC_APPLY_BASELINE=<git revision> also compares a run of that revision's script with this one for an IPv4-only
JSON: the same calls, in the same order, and the same final state."""
import copy
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCRIPT_REL = "eda/common/files/bgw/shc-fabric-apply"
SCRIPT = os.path.join(ROOT, SCRIPT_REL)
FAKE = os.path.join(HERE, "fake_netkernel.py")
sys.path.insert(0, HERE)
import fake_netkernel as F  # noqa: E402

TOOLS = ("ip", "sysctl", "nft", "wg", "modprobe", "vtysh", "cat")
METRIC = "4278198272"

ROOT_CONF = {
    "type": "root", "bridge": "br-core", "mtu": "9100", "link": "10.255.1.1/24",
    "loopback": "10.255.0.1/32", "loopbackPool": "10.255.0.0/24",
    "egress": {
        "vrf": "egress", "table": 9000, "parent": "bond1.237", "mtu": 9000, "address": "172.18.237.13/24",
        "gateway": "172.18.237.1", "deny": ["10.0.0.0/8"], "allow": [], "platformSources": ["172.18.230.0/24"],
        "platformPorts": [6443, 443],
        "networks": [{"vrf": "e10000", "table": 110000, "zone": 1, "xe": "xe10000", "xo": "xo10000",
                      "xeAddr": "169.254.0.1/31", "xoAddr": "169.254.0.0/31", "nat": "172.18.237.64",
                      "platform": []}]},
    "attachments": {"vlanPool": [3000, 3099], "parents": ["bond1"], "platformSources": ["172.18.230.0/24"],
                    "circuits": []},
}
CIRCUIT = {"circuit": "c1", "device": "bond1.3001", "parent": "bond1", "vlan": 3001, "mtu": 1500, "vrf": "e10001",
           "table": 110001, "zone": 2, "address": "192.0.2.1/30", "link": "192.0.2.2", "nat": False}
GW_CONF = {
    "type": "gateway", "netns": "gw-iaas1", "address": "172.18.236.4/24", "trunk": "bond1", "vlan": 236,
    "coreBridge": "br-core", "coreMtu": 9100, "coreVeth": "pe2", "link": "10.255.1.2/24",
    "loopback": "10.255.0.2/32", "loopbackPool": "10.255.0.0/24",
    "vrfs": [{"name": "t51000", "vni": 51000, "mtu": 9000}, {"name": "t51002", "vni": 51002, "mtu": 9000}],
}
WG_CONF = {
    "type": "gateway", "netns": "gw-aws1", "address": "10.66.0.4/24", "coreBridge": "br-core", "coreMtu": 9100,
    "coreVeth": "pe6", "link": "10.255.1.6/24", "loopback": "10.255.0.6/32", "loopbackPool": "10.255.0.0/24",
    "wireguard": {"device": "wg0", "listenPort": 51820, "keepalive": 25,
                  "peers": [{"publicKey": "peer-a", "endpoint": "198.51.100.7:51820", "allowedIPs": ["10.66.0.0/24"]}]},
    "vrfs": [{"name": "t51003", "vni": 51003, "mtu": 1380}],
}
POOLS6 = {"loopbackPool6": "2001:db8:4005:ffff::/64", "icmpSource6": "2001:db8:4005:1::/64"}
ROOT6 = dict(POOLS6, loopback6="2001:db8:4005:ffff::1/128", icmp6="2001:db8:4005:1::1:1/128")
GW6 = dict(POOLS6, loopback6="2001:db8:4005:ffff::2/128", icmp6="2001:db8:4005:1::1:2/128")
WG6 = dict(POOLS6, loopback6="2001:db8:4005:ffff::6/128", icmp6="2001:db8:4005:1::1:6/128")


def v6_token(tok):
    if tok == "-6" or "ipv6" in tok or "inet6" in tok:
        return True
    try:
        return ipaddress.ip_interface(tok).version == 6
    except ValueError:
        return False


def is_v6(line):
    return any(v6_token(t) for t in line.split()[2:])


def writes(log):
    return [l for l in log if l.startswith("W ")]


def blade():
    """The root namespace of a blade before the fabric: management bond0.10, trunk bond1, egress parent bond1.237;
    IPv6 forwarding on, RA accepted (as V0 read them)."""
    st = {"ns": {"": F.new_ns({"disable_ipv6": "0", "forwarding": "1", "accept_ra": "1", "autoconf": "1"})}}
    k = F.Kernel(st)
    sc = st["ns"][""]["sysctl"]
    sc.update({"net/ipv4/ip_forward": "1", "net/mpls/platform_labels": "0"})
    lo = k.add_link("", "lo", "loopback", address="00:00:00:00:00:00")
    lo["up"] = True
    lo["addrs"].append({"family": 4, "local": "127.0.0.1", "prefixlen": 8, "nodad": False})
    for name, kind, mtu, mac, parent, vlan in (("bond0", "bond", 1500, "2c:41:38:eb:d2:64", None, None),
                                               ("bond0.10", "vlan", 1500, "2c:41:38:eb:d2:64", "bond0", 10),
                                               ("bond1", "bond", 9100, "00:02:c9:2f:3a:c0", None, None),
                                               ("bond1.237", "vlan", 9000, "00:02:c9:2f:3a:c0", "bond1", 237)):
        k.add_link("", name, kind, mtu=mtu, address=mac, parent=parent, vlan=vlan)["up"] = True
    k.ns("")["links"]["bond0.10"]["addrs"].append({"family": 4, "local": "172.18.0.61", "prefixlen": 24,
                                                   "nodad": False})
    for d in list(k.ns("")["links"]):
        k.refresh6("", d)
    return st


class Harness:
    def __init__(self, tmp, baseline=None):
        self.tmp = tmp
        self.bin = os.path.join(tmp, "bin")
        self.etc = os.path.join(tmp, "etc")
        os.makedirs(self.bin)
        os.makedirs(self.etc)
        for t in TOOLS:
            p = os.path.join(self.bin, t)
            with open(p, "w") as f:
                f.write('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, FAKE, t))
            os.chmod(p, 0o755)
        self.state = os.path.join(tmp, "state.json")
        self.log = os.path.join(tmp, "calls.log")
        open(self.log, "w").close()
        with open(SCRIPT) as f:
            self.scripts = {"new": self.copy_script(f.read(), "apply-new")}
        if baseline is not None:
            self.scripts["old"] = self.copy_script(baseline, "apply-old")
        self.set_state(blade())

    def copy_script(self, text, name):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text.replace("/etc/shc-fabric/", self.etc + "/"))
        return p

    def set_state(self, st):
        with open(self.state, "w") as f:
            json.dump(st, f)

    def get_state(self):
        with open(self.state) as f:
            return json.load(f)

    def conf(self, instance, data):
        with open(os.path.join(self.etc, instance + ".json"), "w") as f:
            json.dump(data, f, indent=1)

    def run(self, instance, act="up", script="new", fail=None):
        """fail: a regular expression; the fake kernel refuses the calls it matches (FAKE_FAIL)"""
        with open(self.log) as f:
            start = len(f.readlines())
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), FAKE_STATE=self.state,
                   FAKE_LOG=self.log)
        env.pop("FAKE_NS", None)
        env.pop("FAKE_FAIL", None)
        if fail:
            env["FAKE_FAIL"] = fail
        p = subprocess.run(["bash", self.scripts[script], instance, act], env=env, capture_output=True, text=True,
                           timeout=120)
        with open(self.log) as f:
            log = [l.rstrip("\n") for l in f.readlines()[start:]]
        return p.returncode, p.stdout, p.stderr, log

    def ok(self, case, instance, act="up", script="new", fail=None):
        rc, out, err, log = self.run(instance, act, script, fail)
        case.assertEqual(rc, 0, "%s %s failed: %s\n%s" % (instance, act, err, "\n".join(log[-15:])))
        return out, log

    # state readers
    def link(self, ns, dev):
        return self.get_state()["ns"][ns]["links"][dev]

    def addrs6(self, ns, dev, ll=False):
        return sorted("%s/%d" % (a["local"], a["prefixlen"]) for a in self.link(ns, dev)["addrs"]
                      if a["family"] == 6 and (ll or not a["local"].startswith("fe80:")))

    def has_ll(self, ns, dev):
        return any(a["family"] == 6 and a["local"].startswith("fe80:") for a in self.link(ns, dev)["addrs"])

    def sysctl(self, ns, key):
        return self.get_state()["ns"][ns]["sysctl"].get(key)

    def unreachable6(self, ns):
        return sorted(r["table"] for r in self.get_state()["ns"][ns]["routes6"]
                      if r["type"] == "unreachable" and r["dst"] == "default" and r["metric"] == METRIC)

    def add_addr(self, ns, dev, cidr):
        st = self.get_state()
        v, local, plen = F.parse_ifaddr(cidr)
        st["ns"][ns]["links"][dev]["addrs"].append({"family": v, "local": local, "prefixlen": plen, "nodad": True})
        self.set_state(st)


def index(log, pattern, start=0):
    rx = re.compile(pattern)
    for i, l in enumerate(log[start:], start):
        if rx.search(l):
            return i
    raise AssertionError("no call matches %r in:\n%s" % (pattern, "\n".join(log)))


class ApplyCase(unittest.TestCase):
    baseline = None

    def setUp(self):
        if not shutil.which("jq"):
            self.skipTest("jq not installed")
        self.tmp = tempfile.mkdtemp(prefix="shc-apply-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.h = Harness(self.tmp, self.baseline)
        self.evidence = os.environ.get("SHC_APPLY_EVIDENCE")

    def keep(self, name, log, out=""):
        """the call log of a run, for the record (SHC_APPLY_EVIDENCE=<dir>)"""
        if self.evidence:
            os.makedirs(self.evidence, exist_ok=True)
            with open(os.path.join(self.evidence, name + ".log"), "w") as f:
                f.write("\n".join(log) + "\n" + ("# stdout: " + out.strip().replace("\n", " | ") + "\n"))


class SyntaxTest(unittest.TestCase):
    def test_bash_n(self):
        subprocess.run(["bash", "-n", SCRIPT], check=True)

    def test_shellcheck_errors(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed")
        p = subprocess.run(["shellcheck", "-s", "bash", "-S", "error", SCRIPT], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)


class IPv4OnlyTest(ApplyCase):
    def test_no_ipv6_command_without_the_keys(self):
        h = self.h
        h.conf("root", ROOT_CONF)
        h.conf("gw-iaas1", GW_CONF)
        logs = []
        for n in range(2):
            out_r, log_r = h.ok(self, "root")
            out_g, log_g = h.ok(self, "gw-iaas1")
            logs += log_r + log_g
            self.keep("ipv4-only-run%d-root" % (n + 1), log_r, out_r)
            self.keep("ipv4-only-run%d-gw-iaas1" % (n + 1), log_g, out_g)
        self.assertEqual([l for l in logs if is_v6(l)], [])
        self.assertTrue(out_r.strip().endswith("changed=no"), out_r)
        self.assertTrue(out_g.strip().endswith("changed=no"), out_g)
        self.assertEqual(h.unreachable6(""), [])
        self.assertEqual(h.unreachable6("gw-iaas1"), [])
        # today's kernel state: IPv6 sysctls as the kernel made them
        self.assertEqual(h.sysctl("gw-iaas1", "net/ipv6/conf/all/forwarding"), "0")
        self.assertEqual(h.sysctl("gw-iaas1", "net/ipv6/conf/core0/accept_ra"), "1")
        self.assertTrue(h.has_ll("gw-iaas1", "bond1.236"))

    def test_down_without_the_keys(self):
        h = self.h
        h.conf("root", ROOT_CONF)
        h.conf("gw-iaas1", GW_CONF)
        h.ok(self, "root")
        h.ok(self, "gw-iaas1")
        _, log_g = h.ok(self, "gw-iaas1", "down")
        _, log_r = h.ok(self, "root", "down")
        self.assertEqual([l for l in log_g + log_r if is_v6(l)], [])
        self.assertNotIn("gw-iaas1", h.get_state()["ns"])
        self.assertNotIn("br-core", h.get_state()["ns"][""]["links"])


class BaselineTest(ApplyCase):
    """IPv4-only JSON: this script issues exactly the calls of the baseline revision (SHC_APPLY_BASELINE)."""

    @classmethod
    def setUpClass(cls):
        rev = os.environ.get("SHC_APPLY_BASELINE")
        if not rev:
            raise unittest.SkipTest("SHC_APPLY_BASELINE not set")
        cls.baseline = subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (rev, SCRIPT_REL)], check=True,
                                      capture_output=True, text=True).stdout

    def run_both(self, root_conf, gw_confs):
        results = {}
        for which in ("old", "new"):
            self.h.set_state(blade())
            for f in os.listdir(self.h.etc):  # the WireGuard key a first run generates
                os.unlink(os.path.join(self.h.etc, f))
            self.h.conf("root", root_conf)
            logs = []
            for n in range(2):
                logs.append(self.h.ok(self, "root", script=which))
                for name, c in gw_confs:
                    self.h.conf(name, c)
                    logs.append(self.h.ok(self, name, script=which))
            results[which] = (logs, self.h.get_state())
        return results

    def test_same_calls_as_baseline(self):
        root = copy.deepcopy(ROOT_CONF)
        root["attachments"]["circuits"] = [CIRCUIT]
        r = self.run_both(root, [("gw-iaas1", GW_CONF), ("gw-aws1", WG_CONF)])
        old_logs, old_state = r["old"]
        new_logs, new_state = r["new"]
        for (oo, ol), (no, nl) in zip(old_logs, new_logs):
            self.assertEqual(ol, nl)
            self.assertEqual(oo, no)
        self.assertEqual(old_state, new_state)
        self.keep("baseline-compare-new", [l for _, log in new_logs for l in log])


class GatewayIPv6Test(ApplyCase):
    def bring_up(self, gw_conf=GW_CONF, gw6=GW6, instance="gw-iaas1"):
        h = self.h
        h.conf("root", dict(ROOT_CONF, **ROOT6))
        h.ok(self, "root")
        h.conf(instance, dict(gw_conf, **gw6))
        return h.ok(self, instance)

    def test_fresh_namespace_order(self):
        h = self.h
        out, log = self.bring_up()
        self.keep("gw-iaas1-v6-fresh-run1", log, out)
        ns = "gw-iaas1"
        # namespace sysctls first, then the devices
        first_router = index(log, r"^W \[gw-iaas1\] sysctl -qw net/ipv6/conf/all/forwarding=1$")
        for k, v in (("forwarding", "1"), ("accept_ra", "0"), ("autoconf", "0"), ("disable_ipv6", "0")):
            for c in ("all", "default"):
                self.assertEqual(h.sysctl(ns, "net/ipv6/conf/%s/%s" % (c, k)), v, (c, k))
        # site leg: IPv6 off after the move into the namespace, before it comes up
        moved = index(log, r"^W \[root\] ip link set bond1\.236 netns gw-iaas1$")
        off = index(log, r"^W \[gw-iaas1\] sysctl -qw net/ipv6/conf/bond1\.236/disable_ipv6=1$")
        up = index(log, r"^W \[gw-iaas1\] ip link set bond1\.236 mtu \d+ up$")
        self.assertLess(first_router, moved)
        self.assertLess(moved, off)
        self.assertLess(off, up)
        self.assertEqual(h.sysctl(ns, "net/ipv6/conf/bond1.236/disable_ipv6"), "1")
        self.assertEqual(h.addrs6(ns, "bond1.236", ll=True), [])
        # core leg: created in the namespace (it takes the namespace's defaults set above, so nothing to write), its
        # IPv6 settings checked before it comes up, link-local only
        created = index(log, r"^W \[root\] ip link add pe2 mtu 9100 type veth peer name core0 netns gw-iaas1$")
        c_ra = index(log, r"^R \[gw-iaas1\] sysctl -n net/ipv6/conf/core0/accept_ra$")
        c_up = index(log, r"^W \[gw-iaas1\] ip link set core0 mtu 9100 up$")
        self.assertLess(created, c_ra)
        self.assertLess(c_ra, c_up)
        for k, v in (("disable_ipv6", "0"), ("accept_ra", "0"), ("autoconf", "0")):
            self.assertEqual(h.sysctl(ns, "net/ipv6/conf/core0/" + k), v, k)
        self.assertTrue(h.has_ll(ns, "core0"))
        self.assertEqual(h.addrs6(ns, "core0"), [])
        # loopback and VRFs
        self.assertEqual(h.addrs6(ns, "lo"), ["2001:db8:4005:ffff::2/128", "::1/128"])
        index(log, r"^W \[gw-iaas1\] ip addr add 2001:db8:4005:ffff::2/128 dev lo nodad$")
        self.assertEqual(h.unreachable6(ns), ["51000", "51002"])
        for vrf, vni in (("t51000", "51000"), ("t51002", "51002")):
            made = index(log, r"^W \[gw-iaas1\] ip link add %s type vrf table %s$" % (vrf, vni))
            route = index(log, r"^W \[gw-iaas1\] ip -6 route replace unreachable default metric %s table %s$"
                          % (METRIC, vni))
            icmp = index(log, r"^W \[gw-iaas1\] ip addr add 2001:db8:4005:1::1:2/128 dev %s nodad$" % vrf)
            self.assertLess(made, route)
            self.assertLess(made, icmp)
            self.assertEqual(h.addrs6(ns, vrf), ["2001:db8:4005:1::1:2/128"])
        self.assertTrue(out.strip().endswith("changed=yes"))
        # a second run changes nothing and writes nothing IPv6
        out2, log2 = h.ok(self, "gw-iaas1")
        self.keep("gw-iaas1-v6-fresh-run2", log2, out2)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if is_v6(l)], [])
        # the management interface and the root's other devices are never touched
        self.assertEqual([l for l in log + log2 if "bond0" in l], [])

    def test_upgrade_of_a_live_namespace(self):
        """today's namespace (no keys), with leftovers: a stale IPv6 loopback and ICMP source from the pools, a
        global address on core0; then the keys arrive"""
        h = self.h
        h.conf("root", ROOT_CONF)
        h.conf("gw-iaas1", GW_CONF)
        h.ok(self, "root")
        h.ok(self, "gw-iaas1")
        ns = "gw-iaas1"
        self.assertTrue(h.has_ll(ns, "bond1.236"))
        h.add_addr(ns, "lo", "2001:db8:4005:ffff::9/128")
        h.add_addr(ns, "lo", "2001:db8:77::1/128")  # outside the pool: not ours, stays
        h.add_addr(ns, "t51000", "2001:db8:4005:1::9:9/128")
        h.add_addr(ns, "core0", "2001:db8:99::5/64")
        h.conf("root", dict(ROOT_CONF, **ROOT6))
        # a non-canonical form of the loopback still ends up idempotent
        h.conf("gw-iaas1", dict(GW_CONF, **dict(GW6, loopback6="2001:DB8:4005:FFFF:0:0:0:2/128")))
        h.ok(self, "root")
        out, log = h.ok(self, "gw-iaas1")
        self.keep("gw-iaas1-v6-upgrade-run1", log, out)
        self.assertTrue(out.strip().endswith("changed=yes"))
        self.assertEqual(h.addrs6(ns, "lo"), ["2001:db8:4005:ffff::2/128", "2001:db8:77::1/128", "::1/128"])
        self.assertEqual(h.addrs6(ns, "t51000"), ["2001:db8:4005:1::1:2/128"])
        self.assertEqual(h.addrs6(ns, "core0"), [])
        self.assertTrue(h.has_ll(ns, "core0"))
        self.assertEqual(h.addrs6(ns, "bond1.236", ll=True), [])
        self.assertEqual(h.sysctl(ns, "net/ipv6/conf/core0/accept_ra"), "0")
        self.assertEqual(h.sysctl(ns, "net/ipv6/conf/all/forwarding"), "1")
        # disable_ipv6 was already 0: never rewritten (writing "all" would re-enable the site leg)
        self.assertEqual([l for l in log if "disable_ipv6=0" in l and "/all/" in l], [])
        out2, log2 = h.ok(self, "gw-iaas1")
        self.keep("gw-iaas1-v6-upgrade-run2", log2, out2)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if is_v6(l)], [])
        self.assertEqual(h.sysctl(ns, "net/ipv6/conf/bond1.236/disable_ipv6"), "1")

    def test_keys_removed_again(self):
        """IPv6 core turned off (the pools still written): the loopback and ICMP sources go, nothing else IPv6"""
        h = self.h
        self.bring_up()
        h.conf("gw-iaas1", dict(GW_CONF, **POOLS6))
        out, log = h.ok(self, "gw-iaas1")
        self.keep("gw-iaas1-v6-removed", log, out)
        ns = "gw-iaas1"
        self.assertEqual(h.addrs6(ns, "lo"), ["::1/128"])
        self.assertEqual(h.addrs6(ns, "t51000"), [])
        self.assertEqual(sorted(l.split("] ", 1)[1] for l in writes(log) if is_v6(l)), sorted([
            "ip addr del 2001:db8:4005:ffff::2/128 dev lo",
            "ip addr del 2001:db8:4005:1::1:2/128 dev t51000",
            "ip addr del 2001:db8:4005:1::1:2/128 dev t51002"]))
        out2, _ = h.ok(self, "gw-iaas1")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)

    def test_wireguard_site_leg(self):
        h = self.h
        out, log = self.bring_up(WG_CONF, WG6, "gw-aws1")
        self.keep("gw-aws1-wireguard-v6-run1", log, out)
        moved = index(log, r"^W \[root\] ip link set wg0 netns gw-aws1$")
        off = index(log, r"^W \[gw-aws1\] sysctl -qw net/ipv6/conf/wg0/disable_ipv6=1$")
        up = index(log, r"^W \[gw-aws1\] ip link set wg0 mtu 1420 up$")
        self.assertLess(moved, off)
        self.assertLess(off, up)
        self.assertEqual(h.unreachable6("gw-aws1"), ["51003"])
        out2, log2 = h.ok(self, "gw-aws1")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if is_v6(l)], [])


class RootIPv6Test(ApplyCase):
    def root_conf(self):
        c = dict(copy.deepcopy(ROOT_CONF), **ROOT6)
        c["attachments"]["circuits"] = [CIRCUIT]
        return c

    def test_root_up(self):
        h = self.h
        h.conf("root", self.root_conf())
        before_mgmt = {k: v for k, v in h.get_state()["ns"][""]["sysctl"].items() if "/bond0" in k}
        out, log = h.ok(self, "root")
        self.keep("root-v6-run1", log, out)
        # core bridge: IPv6 settings before it comes up, link-local only
        made = index(log, r"^W \[root\] ip link add br-core address \S+ type bridge$")
        ra = index(log, r"^W \[root\] sysctl -qw net/ipv6/conf/br-core/accept_ra=0$")
        up = index(log, r"^W \[root\] ip link set br-core mtu 9100 up$")
        self.assertLess(made, ra)
        self.assertLess(ra, up)
        for k, v in (("disable_ipv6", "0"), ("accept_ra", "0"), ("autoconf", "0")):
            self.assertEqual(h.sysctl("", "net/ipv6/conf/br-core/" + k), v, k)
        self.assertTrue(h.has_ll("", "br-core"))
        self.assertEqual(h.addrs6("", "br-core"), [])
        # egress0 never takes RAs or SLAAC: set before it comes up in the egress VRF
        e_made = index(log, r"^W \[root\] ip link add egress0 link bond1\.237 type macvlan mode bridge$")
        e_ra = index(log, r"^W \[root\] sysctl -qw net/ipv6/conf/egress0/accept_ra=0$")
        e_up = index(log, r"^W \[root\] ip link set egress0 master egress mtu 9000 up$")
        self.assertLess(e_made, e_ra)
        self.assertLess(e_ra, e_up)
        self.assertEqual(h.sysctl("", "net/ipv6/conf/egress0/autoconf"), "0")
        # loopback, VRF tables, ICMP sources
        self.assertEqual(h.addrs6("", "lo"), ["2001:db8:4005:ffff::1/128", "::1/128"])
        self.assertEqual(h.unreachable6(""), ["110000", "110001", "9000"])
        for vrf in ("egress", "e10000", "e10001"):
            self.assertEqual(h.addrs6("", vrf), ["2001:db8:4005:1::1:1/128"], vrf)
        # the circuit stays without IPv6, as before
        self.assertEqual(h.sysctl("", "net/ipv6/conf/bond1.3001/disable_ipv6"), "1")
        # the management interface is untouched; the namespace-wide values too (no forwarding/RA change here)
        self.assertEqual([l for l in log if "bond0" in l], [])
        self.assertEqual({k: v for k, v in h.get_state()["ns"][""]["sysctl"].items() if "/bond0" in k}, before_mgmt)
        self.assertEqual(h.sysctl("", "net/ipv6/conf/all/accept_ra"), "1")
        out2, log2 = h.ok(self, "root")
        self.keep("root-v6-run2", log2, out2)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if is_v6(l) and "bond1.3001/disable_ipv6" not in l], [])

    def test_root_down_removes_the_loopback(self):
        h = self.h
        h.conf("root", self.root_conf())
        h.ok(self, "root")
        _, log = h.ok(self, "root", "down")
        self.assertEqual(h.addrs6("", "lo"), ["::1/128"])
        index(log, r"^W \[root\] ip addr del 2001:db8:4005:ffff::1/128 dev lo$")

    def test_ra_precheck_leaves_ipv6_out(self):
        """an RA route on a host interface: IPv6 left out with the marker line, IPv4 exactly as without the keys,
        exit 0 (a boot keeps the gateways; the fabric job fails on the marker)"""
        h = self.h
        ra = {"type": "unicast", "dst": "default", "via": "fe80::1", "dev": "bond0.10", "proto": "ra",
              "metric": "1024", "table": "main"}

        def blade_with_ra():
            st = blade()
            st["ns"][""]["routes6"].append(dict(ra))
            return st

        h.set_state(blade_with_ra())
        h.conf("root", self.root_conf())
        rc, out, err, log = h.run("root")
        self.keep("root-v6-ra-precheck", log, out + " | stderr: " + err)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: interface bond0.10 relies on router advertisements; IPv6 core not applied\n", out)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.assertEqual([l for l in writes(log) if is_v6(l) and "bond1.3001/disable_ipv6" not in l], [])
        self.assertEqual(h.addrs6("", "lo"), ["::1/128"])
        self.assertEqual(h.unreachable6(""), [])
        # IPv4: the same writes as a run without the IPv6 keys
        v4_only = copy.deepcopy(self.root_conf())
        for k in ROOT6:
            v4_only.pop(k)
        h.set_state(blade_with_ra())
        h.conf("root", v4_only)
        _, log4 = h.ok(self, "root")
        self.assertEqual(writes(log), writes(log4))
        # a re-run with the keys: still nothing IPv6, nothing changed
        h.conf("root", self.root_conf())
        out2, log2 = h.ok(self, "root")
        self.assertIn("IPv6 core not applied", out2)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in writes(log2) if is_v6(l) and "bond1.3001/disable_ipv6" not in l], [])
        # without loopback6 the precheck never runs
        h.conf("root", dict(ROOT_CONF, **POOLS6))
        out3, _ = h.ok(self, "root")
        self.assertNotIn("IPv6 core not applied", out3)

    def test_ra_route_in_a_vrf_does_not_count(self):
        h = self.h
        h.conf("root", self.root_conf())
        h.ok(self, "root")
        st = h.get_state()
        st["ns"][""]["routes6"].append({"type": "unicast", "dst": "default", "via": "fe80::1", "dev": "xo10000",
                                        "proto": "ra", "metric": "1024", "table": "main"})
        h.set_state(st)
        h.ok(self, "root")



class RollbackTest(ApplyCase):
    """The IPv6 core rolled back: the jobs write the IPv6 keys as "" (pools included). The apply still removes the
    loopback and ICMP addresses from the pools of its last run (kept in <instance>.pools6), then forgets them; the
    sysctls and the unreachable defaults stay."""

    EMPTY6 = {"loopback6": "", "icmp6": "", "loopbackPool6": "", "icmpSource6": ""}

    def pools_file(self, instance):
        return os.path.join(self.h.etc, instance + ".pools6")

    def test_ipv4_only_writes_no_pools_file(self):
        h = self.h
        h.conf("root", ROOT_CONF)
        h.conf("gw-iaas1", dict(GW_CONF, **self.EMPTY6))
        h.ok(self, "root")
        h.ok(self, "gw-iaas1")
        self.assertFalse(os.path.exists(self.pools_file("root")))
        self.assertFalse(os.path.exists(self.pools_file("gw-iaas1")))

    def test_gateway_rollback(self):
        h = self.h
        h.conf("root", dict(ROOT_CONF, **ROOT6))
        h.ok(self, "root")
        h.conf("gw-iaas1", dict(GW_CONF, **GW6))
        h.ok(self, "gw-iaas1")
        with open(self.pools_file("gw-iaas1")) as f:
            self.assertEqual(f.read(), "2001:db8:4005:ffff::/64\n2001:db8:4005:1::/64\n")
        ns = "gw-iaas1"
        h.conf(ns, dict(GW_CONF, **self.EMPTY6))
        out, log = h.ok(self, ns)
        self.keep("gw-iaas1-v6-rollback-run1", log, out)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.assertEqual(h.addrs6(ns, "lo"), ["::1/128"])
        self.assertEqual(h.addrs6(ns, "t51000"), [])
        self.assertEqual(h.addrs6(ns, "t51002"), [])
        self.assertEqual(sorted(l.split("] ", 1)[1] for l in writes(log) if is_v6(l)), sorted([
            "ip addr del 2001:db8:4005:ffff::2/128 dev lo",
            "ip addr del 2001:db8:4005:1::1:2/128 dev t51000",
            "ip addr del 2001:db8:4005:1::1:2/128 dev t51002"]))
        # inert leftovers
        self.assertEqual(h.unreachable6(ns), ["51000", "51002"])
        self.assertEqual(h.sysctl(ns, "net/ipv6/conf/all/forwarding"), "1")
        self.assertFalse(os.path.exists(self.pools_file(ns)))
        # then a run as on an IPv4-only core: no IPv6 call at all
        out2, log2 = h.ok(self, ns)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in log2 if is_v6(l)], [])

    def test_root_rollback_and_down(self):
        h = self.h
        c = copy.deepcopy(ROOT_CONF)
        h.conf("root", dict(c, **ROOT6))
        h.ok(self, "root")
        h.conf("root", dict(c, **self.EMPTY6))
        out, log = h.ok(self, "root")
        self.keep("root-v6-rollback-run1", log, out)
        self.assertEqual(h.addrs6("", "lo"), ["::1/128"])
        for vrf in ("egress", "e10000"):
            self.assertEqual(h.addrs6("", vrf), [], vrf)
        self.assertEqual(h.unreachable6(""), ["110000", "9000"])
        self.assertFalse(os.path.exists(self.pools_file("root")))
        out2, log2 = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in log2 if is_v6(l)], [])
        # down forgets the pools too
        h.conf("root", dict(c, **ROOT6))
        h.ok(self, "root")
        self.assertTrue(os.path.exists(self.pools_file("root")))
        h.ok(self, "root", "down")
        self.assertFalse(os.path.exists(self.pools_file("root")))

    def test_pools_file_rewritten_only_on_change(self):
        h = self.h
        h.conf("root", dict(ROOT_CONF, **ROOT6))
        h.ok(self, "root")
        st = os.stat(self.pools_file("root"))
        os.utime(self.pools_file("root"), ns=(st.st_atime_ns, 1))
        h.ok(self, "root")
        self.assertEqual(os.stat(self.pools_file("root")).st_mtime_ns, 1)
        h.conf("root", dict(ROOT_CONF, **dict(ROOT6, icmp6="", icmpSource6="")))
        h.ok(self, "root")
        with open(self.pools_file("root")) as f:
            self.assertEqual(f.read(), "2001:db8:4005:ffff::/64\n")


def v4_writes(log):
    """the writes of a run that are not IPv6 ones (the attachment circuit's disable_ipv6=1 is IPv4-era: kept)"""
    return [l for l in writes(log) if not is_v6(l) or "bond1.3001/disable_ipv6" in l]


class IPv6StepFailureTest(ApplyCase):
    """An IPv6 command the host refuses never stops the IPv4 setup: the marker line on stdout, the IPv6 steps after it
    left out, every IPv4 write as in a run without the IPv6 keys, exit 0 (a boot keeps the gateways; the jobs fail
    on the marker)."""

    def root_conf(self, v6=True):
        c = copy.deepcopy(ROOT_CONF)
        c["attachments"]["circuits"] = [CIRCUIT]
        return dict(c, **ROOT6) if v6 else c

    def keyless_root(self):
        self.h.set_state(blade())
        self.h.conf("root", self.root_conf(False))
        return self.h.ok(self, "root")

    def failed_root(self, fail, name):
        h = self.h
        h.set_state(blade())
        h.conf("root", self.root_conf())
        rc, out, err, log = h.run("root", fail=fail)
        self.keep(name, log, out + " | stderr: " + err)
        self.assertEqual(rc, 0, err)
        return out, log

    def test_root_first_ipv6_step_refused(self):
        h = self.h
        out, log = self.failed_root(r"^\[root\] sysctl -qw net/ipv6/conf/br-core/accept_ra=0$", "root-v6-step-refused")
        self.assertIn("warning: IPv6 settings of the core bridge br-core failed; IPv6 core not applied\n", out)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.assertEqual([l for l in writes(log) if is_v6(l) and "bond1.3001/disable_ipv6" not in l], [])
        self.assertEqual(h.addrs6("", "lo"), ["::1/128"])
        self.assertEqual(h.unreachable6(""), [])
        # IPv4 brought up as always: core bridge up with its address, loopback, egress, circuit
        self.assertTrue(h.link("", "br-core")["up"])
        self.assertIn("egress0", h.get_state()["ns"][""]["links"])
        state = h.get_state()
        _, log4 = self.keyless_root()
        self.assertEqual(v4_writes(log), v4_writes(log4))
        self.assertEqual(writes(log), writes(log4))
        # IPv4 state equal to the keyless run's (the IPv6 settings the step did not reach untouched too)
        self.assertEqual(state, h.get_state())

    def test_root_later_ipv6_step_refused(self):
        h = self.h
        refuse = r"^\[root\] ip -6 route replace unreachable default .* table 110000$"
        out, log = self.failed_root(refuse, "root-v6-vrf-step-refused")
        self.assertIn("warning: IPv6 in VRF e10000 failed; IPv6 core not applied\n", out)
        # the steps before stay done (core bridge, loopback); none after it runs (e10000's ICMP source, the other
        # VRFs: e10001 and egress come after e10000)
        self.assertEqual(h.addrs6("", "lo"), ["2001:db8:4005:ffff::1/128", "::1/128"])
        self.assertEqual(h.sysctl("", "net/ipv6/conf/br-core/accept_ra"), "0")
        self.assertEqual(h.unreachable6(""), [])
        for vrf in ("e10000", "e10001", "egress"):
            self.assertEqual(h.addrs6("", vrf), [], vrf)
        self.assertEqual([l for l in log if ("e10001" in l or "table 9000" in l) and is_v6(l)], [])
        # a second run with the same refusal: the same marker, nothing changes
        rc, out2, err, _ = h.run("root", fail=refuse)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: IPv6 in VRF e10000 failed; IPv6 core not applied\n", out2)
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        _, log4 = self.keyless_root()
        self.assertEqual(v4_writes(log), v4_writes(log4))

    def gateway(self, gw, fail=None):
        """the root as with the keys, then the gateway (gw: its JSON), from a fresh blade"""
        h = self.h
        h.set_state(blade())
        h.conf("root", dict(ROOT_CONF, **ROOT6))
        h.ok(self, "root")
        h.conf("gw-iaas1", gw)
        rc, out, err, log = h.run("gw-iaas1", fail=fail)
        self.assertEqual(rc, 0, err)
        return out, log

    def test_gateway_first_ipv6_step_refused(self):
        h = self.h
        out, log = self.gateway(dict(GW_CONF, **GW6), r"^\[gw-iaas1\] sysctl -qw net/ipv6/conf/all/forwarding=1$")
        self.keep("gw-iaas1-v6-step-refused", log, out)
        self.assertIn("warning: the namespace's IPv6 settings failed; IPv6 core not applied\n", out)
        self.assertTrue(out.strip().endswith("changed=yes"), out)
        self.assertEqual([l for l in writes(log) if is_v6(l)], [])
        ns = "gw-iaas1"
        self.assertEqual(h.addrs6(ns, "lo"), ["::1/128"])
        self.assertEqual(h.unreachable6(ns), [])
        # the gateway's IPv4 side is complete: site leg, core leg, VRFs
        self.assertTrue(h.link(ns, "bond1.236")["up"])
        self.assertTrue(h.link(ns, "core0")["up"])
        self.assertIn("vx51002", h.get_state()["ns"][ns]["links"])
        state = h.get_state()["ns"][ns]
        _, log4 = self.gateway(GW_CONF)
        self.assertEqual(writes(log), writes(log4))
        self.assertEqual(state, h.get_state()["ns"][ns])

    def test_gateway_vrf_step_refused(self):
        h = self.h
        out, log = self.gateway(dict(GW_CONF, **GW6), r"^\[gw-iaas1\] ip addr add \S+ dev t51000 nodad$")
        self.keep("gw-iaas1-v6-vrf-step-refused", log, out)
        self.assertIn("warning: IPv6 in VRF t51000 failed; IPv6 core not applied\n", out)
        ns = "gw-iaas1"
        # done before: the namespace settings, the loopback, the site leg off, core0, t51000's unreachable default;
        # t51002 gets nothing IPv6 but all of its IPv4 devices
        self.assertEqual(h.addrs6(ns, "lo"), ["2001:db8:4005:ffff::2/128", "::1/128"])
        self.assertEqual(h.unreachable6(ns), ["51000"])
        self.assertEqual(h.addrs6(ns, "t51002"), [])
        self.assertIn("vx51002", h.get_state()["ns"][ns]["links"])
        _, log4 = self.gateway(GW_CONF)
        self.assertEqual(v4_writes(log), v4_writes(log4))


if __name__ == "__main__":
    unittest.main()

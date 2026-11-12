"""The root VRFs' IPv4 ICMP source (realization core.icmpSource4, default 192.0.0.8).

The root's "fragmentation needed" and "time exceeded" for a network's traffic used to leave from whichever address
the kernel took in the VRF: the network VRF's side of the egress link (egress.linkPool, 169.254.0.0/16) for the hop in
the network's VRF, the egress VRF's side for the hop there. OpenShift sites drop some of those (OVN-Kubernetes
reserves part of 169.254.0.0/17), so which networks lost IPv4 path MTU discovery depended on allocation order. Now:
- the apply holds the address as a /32 on every root VRF device (egress VRF, network and circuit VRFs), removes it
  without the key, changes nothing on a second run, on both core transports;
- for a route whose next hop lies outside the VRF (every route back to a network's hosts: over the core) the kernel
  prefers the VRF device's address whatever the order of the VRF's other devices; the egress VRF's routes back to the
  networks have their next hop inside that VRF (the egress veth), so FRR installs them with the address as their
  source (zebra route-map on the static routes, only the NAT addresses);
- check-fabric refuses an address that is no single IPv4 address, link-local, or inside the core's IPv4 ranges; the
  network job keeps tenant prefixes off it.
A real kernel (an unprivileged user and network namespace) shows the error source of a forwarded packet's time
exceeded and fragmentation needed for both kinds of route, with and without the address.

Documentation and private ranges only."""
import copy
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_egress_ipv6 as E  # noqa: E402
import test_frr_templates as T  # noqa: E402
import test_shc_fabric_apply as A  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6, root_full  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

HF = os.path.join(ROOT, "eda", "rulebooks", "roles", "hybridfabric_provision", "tasks")
SRC = "192.0.0.8"


class Realization(unittest.TestCase):
    def test_default(self):
        real = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]
        self.assertEqual(real["core"]["icmpSource4"], SRC)

    def test_check(self):
        core = {"loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24"}
        self.assertEqual(F.icmp4_check(dict(core, icmpSource4="")), [])
        self.assertEqual(F.icmp4_check(dict(core, icmpSource4=SRC)), [])
        self.assertEqual(F.icmp4_check(dict(core, icmpSource4="198.51.100.9/32")), [])
        for bad in ("169.254.0.3", "10.255.0.7", "10.255.1.9", "198.51.100.0/24", "2001:db8::1", "x"):
            self.assertEqual(len(F.icmp4_check(dict(core, icmpSource4=bad))), 1, bad)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_exclusive(self):
        task = by_name(load(os.path.join(ROOT, "eda/rulebooks/roles/hybridnetwork_provision/tasks/main.yml")),
                       "The platform's exclusive ranges")
        out = run_play([task, write_out("hn_exclusive")], {"fab_real": {"core": {"icmpSource4": SRC}}})
        self.assertEqual(out, [SRC + "/32"])
        self.assertEqual(run_play([task, write_out("hn_exclusive")], {"fab_real": {"core": {"icmpSource4": ""}}}), [])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RootConfig(unittest.TestCase):
    def content(self, core):
        t = by_name(load(os.path.join(HF, "node_root.yml")), "{{ bgw_node.name }}: root instance config")
        t = {"name": "c", "vars": t.get("vars", {}), "ansible.builtin.set_fact": {"_c": t["ansible.builtin.copy"]["content"]}}
        v = {"bgw_node": {"name": "bgw-1"}, "hf_root": {"type": "root"}, "hf_node_egress": {}, "hf_node_attach": {},
             "hf_pe": {"loopback6": ""}, "fab_check_result": {"icmp6": ""}, "fab_real": {"core": core}}
        return json.loads(run_play([t, write_out("_c")], v))

    def test_key(self):
        self.assertEqual(self.content({"icmpSource4": SRC})["icmp4"], SRC)
        self.assertEqual(self.content({"icmpSource4": SRC + "/32"})["icmp4"], SRC)
        self.assertNotIn("icmp4", self.content({"icmpSource4": ""}))
        self.assertNotIn("icmp4", self.content({}))


class Template(unittest.TestCase):
    def vars(self, src):
        v = E.root_vars(standby=False)
        v["fab_real"]["core"]["icmpSource4"] = src
        return v

    def test_routes_back_to_the_networks_take_it(self):
        out = T.render(T.RT, self.vars(SRC))
        nat = self.vars(SRC)["hf_node_egress"]["networks"][0]["nat"]
        egress = out.split("vrf egress\n")[1].split("exit-vrf")[0]
        self.assertTrue(egress.startswith(" ip protocol static route-map SHC-EGRESS-SRC4\n"), egress)
        # the NAT addresses only (the platform reach prefixes of an earlier release are gone)
        self.assertIn("ip prefix-list SHC-EGRESS-NETS4 seq 5 permit %s/32\n" % nat, out)
        self.assertNotIn("ip prefix-list SHC-EGRESS-NETS4 seq 10 ", out)
        # the hand-off address, not icmpSource4: the error crosses the network's VRF, which holds icmpSource4 as a
        # local address and would drop it as a martian source (RealKernelEgress)
        handoff = self.vars(SRC)["hf_node_egress"]["address"].split("/")[0]
        self.assertIn("route-map SHC-EGRESS-SRC4 permit 10\n match ip address prefix-list SHC-EGRESS-NETS4\n"
                      " set src %s\nexit\nroute-map SHC-EGRESS-SRC4 permit 20\nexit\n" % handoff, out)
        self.assertNotIn("set src %s" % SRC, out)
        # only in the egress VRF (a network's VRF takes the address from the VRF device, kernel)
        self.assertEqual(out.count("ip protocol static route-map"), 1)

    def test_without_the_key_or_networks(self):
        for v in (self.vars(""), dict(self.vars(SRC), hf_node_egress=dict(self.vars(SRC)["hf_node_egress"], networks=[]))):
            out = T.render(T.RT, v)
            self.assertNotIn("SHC-EGRESS-SRC4", out)
            self.assertNotIn("SHC-EGRESS-NETS4", out)


class Apply(A.ApplyCase):
    def addrs4(self, dev):
        link = self.h.get_state()["ns"][""]["links"].get(dev) or {}
        return sorted("%s/%s" % (a["local"], a["prefixlen"]) for a in link.get("addrs", []) if a.get("family") == 4)

    def check(self, conf, vrfs):
        h = self.h
        h.conf("root", dict(conf, icmp4=SRC))
        out, log = h.ok(self, "root")
        links = h.get_state()["ns"][""]["links"]
        self.assertEqual(sorted(n for n, l in links.items() if l["kind"] == "vrf"), sorted(vrfs))
        for v in vrfs:
            self.assertEqual(self.addrs4(v), [SRC + "/32"], v)
        # nowhere else
        st = h.get_state()["ns"][""]["links"]
        self.assertEqual(sorted(n for n, l in st.items() if any(a.get("local") == SRC for a in l.get("addrs", []))), sorted(vrfs))
        out2, _ = h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        # another address: replaced; then without the key: gone
        h.conf("root", dict(conf, icmp4="198.51.100.9"))
        h.ok(self, "root")
        for v in vrfs:
            self.assertEqual(self.addrs4(v), ["198.51.100.9/32"], v)
        h.conf("root", conf)
        out3, _ = h.ok(self, "root")
        self.assertTrue(out3.strip().endswith("changed=yes"), out3)
        for v in vrfs:
            self.assertEqual(self.addrs4(v), [], v)

    def test_mpls(self):
        c = copy.deepcopy(A.ROOT_CONF)
        c["attachments"]["circuits"] = [copy.deepcopy(A.CIRCUIT)]
        self.check(c, ["e10000", "e10001", "egress"])

    def test_srv6(self):
        c = root_full(**ROOT_SRV6)
        self.check(c, sorted({"egress"} | {n["vrf"] for n in c["egress"]["networks"]}
                             | {x["vrf"] for x in c["attachments"]["circuits"]}))


PROBE = textwrap.dedent(r'''
    """A network VRF e1 with a circuit ce1 (the smaller MTU) toward the CE in VRF cev, the egress veth's VRF side xe1
    (169.254.0.3/31) and the network's ingress slave cr0. The way back to the tenant (VRF ten) leaves by c0: outside
    the VRF (argv[1] "outside": as over the core) or inside it ("inside": as the egress VRF's way back over its veth).
    argv[2]: the address for the VRF device ("" none), argv[3]: "src" = also as the route's source. The tenant sends a
    TTL 1 datagram and a DF datagram of 1450 bytes to the CE; prints the type and the source of the errors it gets."""
    import socket, subprocess, sys, time
    where, addr, src = sys.argv[1], sys.argv[2], sys.argv[3]

    def sh(*a):
        subprocess.run(a, check=True, capture_output=True)

    def mac(dev):
        return subprocess.run(["ip", "-br", "link", "show", dev], capture_output=True, text=True).stdout.split()[2]

    sh("ip", "link", "set", "lo", "up")
    for k in ("net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=0", "net.ipv4.conf.default.rp_filter=0"):
        sh("sysctl", "-qw", k)
    for vrf, table in (("e1", "110001"), ("ten", "100"), ("cev", "200")):
        sh("ip", "link", "add", vrf, "type", "vrf", "table", table)
        sh("ip", "link", "set", vrf, "up")
    sh("ip", "addr", "add", "10.74.0.6/32", "dev", "ten")
    sh("ip", "link", "add", "tr", "type", "veth", "peer", "name", "cr0")
    sh("ip", "link", "set", "tr", "master", "ten", "up")
    sh("ip", "link", "set", "cr0", "master", "e1", "up")
    sh("ip", "addr", "add", "10.9.0.2/30", "dev", "tr")
    sh("ip", "addr", "add", "10.9.0.1/30", "dev", "cr0")
    sh("ip", "route", "add", "10.50.1.0/24", "via", "10.9.0.1", "dev", "tr", "table", "100")
    sh("ip", "link", "add", "c0", "type", "veth", "peer", "name", "t0")
    if where == "inside":
        sh("ip", "link", "set", "c0", "master", "e1")
    sh("ip", "addr", "add", "10.255.1.1/24", "dev", "c0")
    sh("ip", "link", "set", "c0", "up")
    sh("ip", "link", "set", "t0", "master", "ten", "up")
    sh("ip", "addr", "add", "10.255.1.2/24", "dev", "t0")
    if addr:
        sh("ip", "addr", "add", addr + "/32", "dev", "e1")
    sh("ip", "route", "add", "10.74.0.0/24", "via", "10.255.1.2", "dev", "c0", "onlink", "table", "110001",
       *(["src", addr] if src == "src" else []))
    sh("ip", "neigh", "replace", "10.255.1.2", "lladdr", mac("t0"), "dev", "c0", "nud", "permanent")
    sh("ip", "link", "add", "ce1", "type", "veth", "peer", "name", "cv")
    sh("ip", "link", "set", "ce1", "master", "e1", "mtu", "1400", "up")
    sh("ip", "addr", "add", "10.255.128.1/30", "dev", "ce1")
    sh("ip", "link", "set", "cv", "master", "cev", "up")
    sh("ip", "addr", "add", "10.255.128.2/30", "dev", "cv")
    sh("ip", "addr", "add", "10.50.1.1/32", "dev", "cev")
    sh("ip", "route", "add", "10.74.0.0/24", "via", "10.255.128.1", "dev", "cv", "table", "200")
    sh("ip", "route", "add", "10.50.1.1/32", "via", "10.255.128.2", "dev", "ce1", "table", "110001")
    sh("ip", "link", "add", "xe1", "type", "veth", "peer", "name", "xo1")
    sh("ip", "link", "set", "xe1", "master", "e1", "up")
    sh("ip", "addr", "add", "169.254.0.3/31", "dev", "xe1")
    time.sleep(1)

    def probe(ttl, size):
        r = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        r.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"ten")
        r.settimeout(1.5)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"ten")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        s.setsockopt(socket.IPPROTO_IP, 10, 2)  # IP_MTU_DISCOVER: IP_PMTUDISC_DO
        s.bind(("10.74.0.6", 0))
        s.sendto(b"x" * size, ("10.50.1.1", 33434))
        end = time.time() + 1.5
        while time.time() < end:
            try:
                d, p = r.recvfrom(2048)
            except socket.timeout:
                break
            t = d[(d[0] & 15) * 4]
            if t in (3, 11):
                return "%d %s" % (t, p[0])
        return "none"

    print(probe(1, 20) + "," + probe(64, 1450))
''')


class RealKernel(unittest.TestCase):
    def run_probe(self, where, addr, src=""):
        tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), "shc-icmp4-%d" % os.getpid())
        os.makedirs(tmp, exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(tmp, "probe.py")
        with open(script, "w") as f:
            f.write(PROBE)
        p = subprocess.run(["unshare", "-rn", sys.executable, "-I", script, where, addr, src], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return p.stdout.strip()

    def test_error_sources(self):
        import test_icmp_source_announce as G
        if not G.can_probe():
            self.skipTest("no unprivileged user and network namespace with VRF devices, nft and ip here")
        # the way back outside the VRF (a network's VRF, back over the core): today the first address of the VRF's
        # devices (here the ingress slave's); with the address on the VRF device, that address
        self.assertEqual(self.run_probe("outside", ""), "11 10.9.0.1,3 10.9.0.1")
        self.assertEqual(self.run_probe("outside", SRC), "11 %s,3 %s" % (SRC, SRC))
        # the way back inside the VRF (the egress VRF, back over its veth): the next hop device's own address, also
        # with the address on the VRF device; the route's source decides
        self.assertEqual(self.run_probe("inside", ""), "11 10.255.1.1,3 10.255.1.1")
        self.assertEqual(self.run_probe("inside", SRC), "11 10.255.1.1,3 10.255.1.1")
        self.assertEqual(self.run_probe("inside", SRC, "src"), "11 %s,3 %s" % (SRC, SRC))


EGRESS_PROBE = r'''"""argv: vrfaddr(on e1 VRF dev or "") egressvrfaddr("" or addr on egr VRF dev) src(route src in egress VRF or "")"""
import socket, subprocess, sys, time
e1addr, egaddr, src = sys.argv[1], sys.argv[2], sys.argv[3]
def sh(*a): subprocess.run(a, check=True, capture_output=True)
def mac(d): return subprocess.run(["ip", "-br", "link", "show", d], capture_output=True, text=True).stdout.split()[2]
sh("ip", "link", "set", "lo", "up")
for k in ("net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=0", "net.ipv4.conf.default.rp_filter=0"):
    sh("sysctl", "-qw", k)
for v, t in (("e1", "110001"), ("egr", "9000"), ("ten", "100"), ("inet", "300")):
    sh("ip", "link", "add", v, "type", "vrf", "table", t); sh("ip", "link", "set", v, "up")
sh("ip", "addr", "add", "10.74.0.6/32", "dev", "ten")
# into e1 from the tenant (a slave), back to it over a device outside the VRF (the core)
sh("ip", "link", "add", "tr", "type", "veth", "peer", "name", "cr0")
sh("ip", "link", "set", "tr", "master", "ten", "up"); sh("ip", "link", "set", "cr0", "master", "e1", "up")
sh("ip", "addr", "add", "10.9.0.2/30", "dev", "tr"); sh("ip", "addr", "add", "10.9.0.1/30", "dev", "cr0")
sh("ip", "route", "add", "default", "via", "10.9.0.1", "dev", "tr", "table", "100")
sh("ip", "link", "add", "c0", "type", "veth", "peer", "name", "t0")
sh("ip", "addr", "add", "10.255.1.1/24", "dev", "c0"); sh("ip", "link", "set", "c0", "up")
sh("ip", "link", "set", "t0", "master", "ten", "up"); sh("ip", "addr", "add", "10.255.1.2/24", "dev", "t0")
sh("ip", "route", "add", "10.74.0.0/24", "via", "10.255.1.2", "dev", "c0", "onlink", "table", "110001")
sh("ip", "neigh", "replace", "10.255.1.2", "lladdr", mac("t0"), "dev", "c0", "nud", "permanent")
# the egress veth: xe1 in e1, xo1 in the egress VRF
sh("ip", "link", "add", "xe1", "type", "veth", "peer", "name", "xo1")
sh("ip", "link", "set", "xe1", "master", "e1", "up"); sh("ip", "link", "set", "xo1", "master", "egr", "up")
sh("ip", "addr", "add", "169.254.0.3/31", "dev", "xe1"); sh("ip", "addr", "add", "169.254.0.2/31", "dev", "xo1")
sh("ip", "route", "add", "default", "via", "169.254.0.2", "dev", "xe1", "table", "110001")
# the hand-off: egress0 in the egress VRF toward the internet host 1.1.1.1 (VRF inet)
sh("ip", "link", "add", "egress0", "type", "veth", "peer", "name", "up0")
sh("ip", "link", "set", "egress0", "master", "egr", "up"); sh("ip", "link", "set", "up0", "master", "inet", "up")
sh("ip", "addr", "add", "172.18.237.13/24", "dev", "egress0"); sh("ip", "addr", "add", "172.18.237.1/24", "dev", "up0")
sh("ip", "addr", "add", "1.1.1.1/32", "dev", "inet")
sh("ip", "route", "add", "default", "via", "172.18.237.1", "dev", "egress0", "table", "9000")
if e1addr: sh("ip", "addr", "add", e1addr + "/32", "dev", "e1")
if egaddr: sh("ip", "addr", "add", egaddr + "/32", "dev", "egr")
# as on the node: the network's traffic is source-NATed where it leaves its VRF (xe1), the egress VRF routes the NAT
# address back over the veth
nat = subprocess.run(["nft", "-f", "-"], input="table ip shc_egress {\n chain zone_output {\n  type filter hook output priority raw; policy accept;\n  ct zone set oifname map { \"xe1\" : 1 }\n }\n chain zone_prerouting {\n  type filter hook prerouting priority raw; policy accept;\n  ct zone set iifname map { \"xe1\" : 1, \"e1\" : 1, \"cr0\" : 1 }\n }\n chain source_nat {\n  type nat hook postrouting priority srcnat; policy accept;\n  oifname \"xe1\" snat to 172.18.237.64\n }\n}\n", text=True, capture_output=True)
assert nat.returncode == 0, nat.stderr
sh("ip", "route", "add", "172.18.237.64/32", "via", "169.254.0.3", "dev", "xo1", "table", "9000", *(["src", src] if src else []))
time.sleep(1)
def martian():
    rows = open("/proc/net/stat/rt_cache").read().splitlines()
    i = rows[0].split().index("in_martian_src")
    return sum(int(r.split()[i], 16) for r in rows[1:])
def probe(ttl):
    r = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    r.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"ten"); r.settimeout(1.5)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"ten")
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
    s.bind(("10.74.0.6", 0)); s.sendto(b"x" * 20, ("1.1.1.1", 33434))
    end = time.time() + 1.5
    while time.time() < end:
        try: d, p = r.recvfrom(2048)
        except socket.timeout: break
        if d[(d[0] & 15) * 4] == 11: return p[0]
    return "none"
m0 = martian()
h1, h2 = probe(1), probe(2)
print("hop1=%s hop2=%s martian_src+%d" % (h1, h2, martian() - m0))
'''


class RealKernelEgress(unittest.TestCase):
    """The egress VRF's hop end to end, as on a node: the network's VRF e1 (icmpSource4 on its device, the way back to
    the tenant over a device outside it), the egress veth xe1/xo1 into the egress VRF, the hand-off egress0 toward
    1.1.1.1, the network's conntrack zone and source NAT on xe1, the egress VRF's route back to the NAT address over
    the veth. The tenant sends TTL 1 (the error from the network's VRF) and TTL 2 (from the egress VRF, translated
    back by conntrack and forwarded through the network's VRF); prints which source each answer came from and the
    growth of the kernel's martian source counter (in_martian_src, /proc/net/stat/rt_cache)."""

    def run_probe(self, *args):
        tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), "shc-icmp4e-%d" % os.getpid())
        os.makedirs(tmp, exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(tmp, "probe.py")
        with open(script, "w") as f:
            f.write(EGRESS_PROBE)
        p = subprocess.run(["unshare", "-rn", sys.executable, "-I", script] + list(args), capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return p.stdout.strip()

    def test_both_hops_reach_the_tenant(self):
        import test_icmp_source_announce as G
        if not G.can_probe() or not os.path.exists("/proc/net/stat/rt_cache"):
            self.skipTest("no unprivileged user and network namespace with VRF devices, nft and ip here")
        # before: the second hop from the egress veth's link-local side
        self.assertEqual(self.run_probe("", "", ""), "hop1=10.9.0.1 hop2=169.254.0.2 martian_src+0")
        # icmpSource4 also as the egress VRF's source: the network's VRF drops the error (a local source arriving)
        r = self.run_probe(SRC, SRC, SRC)
        self.assertTrue(r.startswith("hop1=%s hop2=none martian_src+" % SRC) and not r.endswith("+0"), r)
        # the release: icmpSource4 on both VRF devices, the egress VRF's routes back from the hand-off address
        self.assertEqual(self.run_probe(SRC, SRC, "172.18.237.13"),
                         "hop1=%s hop2=172.18.237.13 martian_src+0" % SRC)


if __name__ == "__main__":
    unittest.main()

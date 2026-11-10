"""The root instance's input filters for its VRFs (shc-fabric-apply, root): what a network reaches of the node itself.

A packet delivered to the node in a root VRF (the egress VRF, a network's VRF e<n>) that did not arrive on one of the
VRF's own legs (egress0, a network's egress veth xe<n>, a circuit's device: each has its own filter or session) came
from a network: from the core, or through a network's veth xo<n> into the egress VRF. Table ip shc_root_in (always,
with root VRFs): ICMP and TCP with ACK or RST pass, the rest is dropped. Table ip6 shc_root_in6 (with the dual-stack
core): link-local sources pass, then as the gateways' filter (ICMPv6 and TCP with ACK or RST pass, other TCP reset, UDP
port unreachable, the rest dropped). Both stateless, loaded before the VRFs and their addresses, replaced as a whole,
removed with the last root VRF (and the IPv6 one with the dual-stack core); an IPv6 step that failed leaves the IPv6
one as it was. An IPv4-only root gets no IPv6 table. Both core transports.

The rulesets are loaded into a real kernel (an unprivileged user and network namespace; skipped where that is not
possible) and probed: from a network (a slave of the VRF that is none of its legs) BGP on 179 is reset (IPv6) or
dropped (IPv4) although a listener is there, UDP refused (IPv6) or dropped (IPv4), echo answered; from a circuit's
device the same listener accepts the connection; answers stay in the VRF.

Documentation and private ranges only."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_shc_fabric_apply as A  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6, root_full  # noqa: E402

T4, T6 = "ip shc_root_in", "ip6 shc_root_in6"
CE = dict(A.CIRCUIT, device="ce1", address6="2001:db8:c::1/64", link6="2001:db8:c::2", sources6=["2001:db8:c00::/48"])


def root4(**kw):
    c = copy.deepcopy(A.ROOT_CONF)
    c.update(kw)
    return c


def root6(**kw):
    return dict(root4(**kw), **A.ROOT6)


def with_circuit(conf):
    c = copy.deepcopy(conf)
    c["attachments"]["circuits"] = [copy.deepcopy(CE)]
    return c


class Apply(A.ApplyCase):
    def nft(self):
        return self.h.get_state()["ns"][""]["nft"]

    def marker(self, fam):
        return os.path.join(self.h.etc, f"root.in{fam}")

    def up(self, conf, **kw):
        self.h.conf("root", conf)
        return self.h.ok(self, "root", **kw)

    def test_ipv4_only_root(self):
        """an IPv4-only root: the IPv4 table on its VRFs and legs, no IPv6 table and no IPv6 command for it"""
        out, log = self.up(root4())
        self.keep("root-in4", log, out)
        self.assertEqual(self.nft()[T4].splitlines(), [
            "table ip shc_root_in", "delete table ip shc_root_in", "table ip shc_root_in {", "  chain input {",
            "    type filter hook input priority filter; policy accept;",
            '    iifname { "e10000", "egress" } meta sdifname { "egress0", "xe10000" } accept comment "a leg of the VRF: its own filter and sessions"',
            '    iifname { "e10000", "egress" } meta l4proto icmp accept',
            '    iifname { "e10000", "egress" } tcp flags & (ack | rst) != 0 accept',
            '    iifname { "e10000", "egress" } counter drop comment "from a network to the node: only ICMP and TCP with ACK or RST"',
            "  }", "}"])
        self.assertNotIn(T6, self.nft())
        self.assertTrue(os.path.exists(self.marker(4)))
        self.assertFalse(os.path.exists(self.marker(6)))
        self.assertEqual([l for l in log if "shc_root_in6" in l], [])
        # no connection tracking, no answer sent on IPv4
        self.assertNotRegex(self.nft()[T4], r"\bct\b|notrack|reject")
        # loaded before the VRFs exist (iifname matches by name)
        self.assertLess(A.index(log, r"^W \[root\] nft -f -$"), A.index(log, r"^W \[root\] ip link add (egress|e10000) type vrf"))
        out2, log2 = self.h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)

    def test_dual_stack_root(self):
        out, log = self.up(root6())
        self.keep("root-in6", log, out)
        text = self.nft()[T6]
        self.assertEqual(text.splitlines(), [
            "table ip6 shc_root_in6", "delete table ip6 shc_root_in6", "table ip6 shc_root_in6 {", "  chain input {",
            "    type filter hook input priority filter; policy accept;",
            '    iifname { "e10000", "egress" } meta sdifname { "egress0", "xe10000" } accept comment "a leg of the VRF: its own filter and sessions"',
            '    iifname { "e10000", "egress" } ip6 saddr fe80::/10 accept comment "link-local: the veth sessions"',
            '    iifname { "e10000", "egress" } meta l4proto ipv6-icmp accept',
            '    iifname { "e10000", "egress" } tcp flags & (ack | rst) != 0 accept',
            '    iifname { "e10000", "egress" } tcp flags & (ack | rst) == 0 counter reject with tcp reset comment "TCP without ACK or RST: reset"',
            '    iifname { "e10000", "egress" } udp dport 0-65535 counter reject with icmpv6 type port-unreachable comment "UDP: port unreachable"',
            '    iifname { "e10000", "egress" } counter drop comment "other protocols and fragments without a transport header"',
            "  }", "}"])
        self.assertNotRegex(text, r"\bct\b|notrack|fib")
        self.assertIn(T4, self.nft())
        # before any root VRF takes the ICMP source
        loads = [i for i, l in enumerate(log) if l == "W [root] nft -f -"]
        first = A.index(log, r"^W \[root\] ip addr add 2001:db8:4005:1::1:1/128 dev (egress|e10000) nodad$")
        self.assertLess(loads[1], first)
        self.assertTrue(os.path.exists(self.marker(6)))
        out2, log2 = self.h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)

    def test_circuits_are_legs(self):
        self.up(with_circuit(root6()))
        for t in (T4, T6):
            self.assertIn('iifname { "e10000", "e10001", "egress" } meta sdifname { "egress0", "xe10000", "ce1" } accept',
                          self.nft()[t])

    def test_circuit_only_root(self):
        """no egress: the circuits' VRFs and devices only"""
        c = with_circuit(root6())
        del c["egress"]
        self.up(c)
        self.assertIn('iifname { "e10001" } meta sdifname { "ce1" } accept', self.nft()[T4])
        self.assertIn('iifname { "e10001" } meta sdifname { "ce1" } accept', self.nft()[T6])

    def test_follow_the_vrfs_and_go_with_the_last(self):
        self.up(with_circuit(root6()))
        c = root6()
        c["egress"]["networks"] = []
        self.up(c)
        self.assertIn('iifname { "egress" } meta sdifname { "egress0" } accept', self.nft()[T4])
        del c["egress"]
        out, log = self.up(c)
        self.assertNotIn(T4, self.nft())
        self.assertNotIn(T6, self.nft())
        self.assertFalse(os.path.exists(self.marker(4)) or os.path.exists(self.marker(6)))
        out2, log2 = self.h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)
        self.assertEqual([l for l in log2 if "shc_root_in" in l], [])

    def test_ipv6_core_rolled_back(self):
        """the IPv6 table goes with the dual-stack core; the IPv4 one stays"""
        self.up(root6())
        self.up(dict(root4(), **A.RollbackTest.EMPTY6))
        self.assertNotIn(T6, self.nft())
        self.assertIn(T4, self.nft())
        self.assertFalse(os.path.exists(self.marker(6)))

    def test_kept_after_an_earlier_ipv6_step_failed(self):
        """a failed IPv6 step before it (the core bridge's IPv6 settings refused) leaves the loaded table as it was"""
        self.up(root6())
        before = self.nft()[T6]
        st = self.h.get_state()
        st["ns"][""]["sysctl"]["net/ipv6/conf/br-core/accept_ra"] = "1"
        self.h.set_state(st)
        rc, out, err, log = self.h.run("root", fail=r"^\[root\] sysctl -qw net/ipv6/conf/br-core/accept_ra=0$")
        self.assertEqual(rc, 0, err)
        self.assertIn("IPv6 core not applied", out)
        self.assertEqual(self.nft()[T6], before)
        self.assertTrue(os.path.exists(self.marker(6)))

    def test_refused_ipv6_load(self):
        """mpls: the IPv6 core is left out (no ICMP source on the VRFs), IPv4 as always"""
        with mock.patch.dict(os.environ, {"FAKE_FAIL_STDIN": r"table ip6 shc_root_in6 \{"}):
            out, log = self.up(root6())
        self.assertIn("warning: the input filter of the root VRFs failed; IPv6 core not applied\n", out)
        self.assertNotIn(T6, self.nft())
        self.assertIn(T4, self.nft())
        self.assertEqual(self.h.addrs6("", "e10000"), [])

    def test_down(self):
        self.up(with_circuit(root6()))
        self.h.ok(self, "root", "down")
        self.assertNotIn(T4, self.nft())
        self.assertNotIn(T6, self.nft())
        self.assertFalse(os.path.exists(self.marker(4)) or os.path.exists(self.marker(6)))


class ApplySrv6(A.ApplyCase):
    """transport srv6: the same tables (the decapsulated packet shows the VRF as its input interface); a refused IPv6
    load fails the apply"""

    def test_tables(self):
        self.h.conf("root", root_full(**ROOT_SRV6))
        out, log = self.h.ok(self, "root")
        nft = self.h.get_state()["ns"][""]["nft"]
        self.assertIn(T4, nft)
        self.assertIn(T6, nft)
        self.assertIn("reject with tcp reset", nft[T6])
        out2, _ = self.h.ok(self, "root")
        self.assertTrue(out2.strip().endswith("changed=no"), out2)

    def test_refused_ipv6_load_fails(self):
        self.h.conf("root", root_full(**ROOT_SRV6))
        with mock.patch.dict(os.environ, {"FAKE_FAIL_STDIN": r"table ip6 shc_root_in6 \{"}):
            rc, out, err, log = self.h.run("root")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("transport srv6: the input filter of the root VRFs failed", err)


class Orphan(unittest.TestCase):
    """the fabric orphan check reads whether the root's tables are loaded and judges them against the root's VRFs and
    the fabric's IPv6 core"""

    def report(self, vrfs, rin, core6=True):
        import shc_fabric as F
        fabric = {"kind": "HybridFabric", "metadata": {"namespace": "sovereign-cloud", "name": "lab"},
                  "status": {"realization": {"ipv6Core": core6, "egress": {"networks": [{"vpnTarget": 10000}]}}}}
        node = {"fabric": "lab", "node": "bgw-1", "netns": {"root": vrfs}}
        if rin is not None:
            node["rootInputFilters"] = rin
        return F.orphan_report({"crs": {"fabrics": [fabric]}, "nodes": [node]})["findings"]

    def test_read(self):
        import test_fabric_orphan_nodes as O
        out, calls = O.execute(dict(O.ANSWERS, **{"nft list table ip shc_root_in": "table ip shc_root_in {\n}\n"}))
        self.assertEqual(out["rootInputFilters"], {"ipv4": True, "ipv6": False})
        self.assertIn(["nft", "list", "table", "ip6", "shc_root_in6"], calls)

    def test_judged(self):
        both = {"ipv4": True, "ipv6": True}
        self.assertEqual(self.report(["e10000", "egress"], both), [])
        self.assertEqual(self.report(["e10000", "egress"], {"ipv4": True, "ipv6": False}, core6=False), [])
        self.assertEqual(self.report([], {"ipv4": False, "ipv6": False}), [])
        # a node read from before the tables: not judged
        self.assertEqual(self.report(["e10000", "egress"], None), [])
        self.assertEqual(self.report([], both), [
            "node bgw-1: the root VRFs' IPv4 input filter (ip shc_root_in) is loaded, but the root has no VRF",
            "node bgw-1: the root VRFs' IPv6 input filter (ip6 shc_root_in6) is loaded, but the root has no VRF"])
        self.assertEqual(self.report(["e10000", "egress"], both, core6=False), [
            "node bgw-1: the root VRFs' IPv6 input filter (ip6 shc_root_in6) is loaded, but the fabric has no IPv6 core"])
        self.assertEqual(self.report(["e10000", "egress"], {"ipv4": False, "ipv6": False}), [
            "node bgw-1: the root has VRFs (e10000, egress), but their IPv4 input filter (ip shc_root_in) is not loaded",
            "node bgw-1: the root has VRFs (e10000, egress), but their IPv6 input filter (ip6 shc_root_in6) is not loaded"])


# -------------------------------------------------------------------------------------------- apply (real kernel)
PROBE = textwrap.dedent(r'''
    """Runs as root of a new user and network namespace. A root VRF e10000 with the node's ICMP source and the egress
    veth's IPv4 address (on xe10000), two slaves toward hosts in VRFs of their own: cr0 (a leg the configuration does
    not name: what arrives from the core) toward the tenant host, ce1 (a circuit's device) toward the CE. Listeners of
    the node in e10000 on TCP 179 (both families) and UDP 3784. A decoy route in the main table toward the tenant: an
    answer routed outside the VRF would leave there. Probes before and after the rulesets (argv[1], argv[2])."""
    import json, os, socket, struct, subprocess, sys, time

    ICMP6, XE4 = "2001:db8:4005:1::1:1", "169.254.0.1"
    TEN6, TEN4, CE6, CE4 = "2001:db8:a::2", "10.9.0.2", "2001:db8:c::2", "192.0.2.2"

    def sh(*a):
        subprocess.run(a, check=True, capture_output=True)

    def wsys(path, value):
        with open("/proc/sys/" + path, "w") as f:
            f.write(value)

    for k in ("all", "default"):
        wsys(f"net/ipv6/conf/{k}/accept_dad", "0")
    sh("ip", "link", "set", "lo", "up")
    for vrf, table in (("e10000", "110000"), ("ten", "100"), ("cev", "200")):
        sh("ip", "link", "add", vrf, "type", "vrf", "table", table)
        sh("ip", "link", "set", vrf, "up")
    sh("ip", "link", "add", "xe10000", "type", "dummy")
    sh("ip", "link", "set", "xe10000", "master", "e10000", "up")
    for a, b, va, vb in (("cr0", "tv", "e10000", "ten"), ("ce1", "cv", "e10000", "cev")):
        sh("ip", "link", "add", a, "type", "veth", "peer", "name", b)
        sh("ip", "link", "set", a, "master", va, "up")
        sh("ip", "link", "set", b, "master", vb, "up")
    sh("ip", "addr", "add", ICMP6 + "/128", "dev", "e10000", "nodad")
    sh("ip", "addr", "add", XE4 + "/31", "dev", "xe10000")
    for dev, a6, a4 in (("cr0", "2001:db8:a::1/64", "10.9.0.1/30"), ("tv", TEN6 + "/64", TEN4 + "/30"),
                        ("ce1", "2001:db8:c::1/64", "192.0.2.1/30"), ("cv", CE6 + "/64", CE4 + "/30")):
        sh("ip", "addr", "add", a6, "dev", dev, "nodad")
        sh("ip", "addr", "add", a4, "dev", dev)
    for table, gw6, gw4 in (("100", "2001:db8:a::1", "10.9.0.1"), ("200", "2001:db8:c::1", "192.0.2.1")):
        sh("ip", "-6", "route", "add", ICMP6 + "/128", "via", gw6, "table", table)
        sh("ip", "-4", "route", "add", XE4 + "/32", "via", gw4, "table", table)
    sh("ip", "link", "add", "dummy0", "type", "dummy")
    sh("ip", "link", "set", "dummy0", "addrgenmode", "none")   # no link-local: the device sends nothing of its own
    sh("ip", "link", "set", "dummy0", "up")
    sh("ip", "-6", "route", "add", "2001:db8:a::/64", "dev", "dummy0")
    sh("ip", "-4", "route", "add", "10.9.0.0/30", "dev", "dummy0")
    time.sleep(1)

    def decoy_tx():
        return json.loads(subprocess.run(["ip", "-s", "-j", "link", "show", "dummy0"], capture_output=True, text=True,
                                         check=True).stdout)[0]["stats64"]["tx"]["packets"]

    def sock(family, kind, vrf, proto=0):
        s = socket.socket(family, kind, proto)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, vrf.encode())
        return s

    listeners = []
    for fam, any_ in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        s = sock(fam, socket.SOCK_STREAM, "e10000")
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((any_, 179))
        s.listen(8)
        listeners.append(s)
    udps = []
    for fam, any_ in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        u = sock(fam, socket.SOCK_DGRAM, "e10000")
        if fam == socket.AF_INET6:
            u.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        u.bind((any_, 3784))
        u.setblocking(False)
        udps.append(u)

    def fam(addr):
        return socket.AF_INET6 if ":" in addr else socket.AF_INET

    def connect(vrf, src, dst, port):
        s = sock(fam(dst), socket.SOCK_STREAM, vrf)
        s.settimeout(1.5)
        try:
            s.bind((src, 0))
            s.connect((dst, port))
            return "connected"
        except socket.timeout:
            return "timeout"
        except ConnectionRefusedError:
            return "reset"
        except OSError as e:
            return "error %d" % e.errno
        finally:
            s.close()

    def udp(vrf, src, dst):
        s = sock(fam(dst), socket.SOCK_DGRAM, vrf)
        s.settimeout(1.5)
        try:
            s.bind((src, 0))
            s.connect((dst, 3784))
            s.send(b"probe")
            try:
                s.recv(64)
                result = "answer"
            except socket.timeout:
                result = "timeout"
            except ConnectionRefusedError:
                result = "refused"
            for u in udps:
                try:
                    u.recv(64)
                    result = "delivered" if result == "timeout" else result + "+delivered"
                except BlockingIOError:
                    pass
            return result
        finally:
            s.close()

    def echo(vrf, src, dst):
        v6 = ":" in dst
        s = sock(fam(dst), socket.SOCK_RAW, vrf, socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP)
        s.bind((src, 0))
        s.settimeout(1.5)
        ident = os.getpid() & 0xFFFF
        typ, rep = (128, 129) if v6 else (8, 0)
        body = struct.pack("!BBHHH", typ, 0, 0, ident, 1) + b"probe!"
        if not v6:
            c = sum(struct.unpack("!%dH" % (len(body) // 2), body))
            c = (c >> 16) + (c & 0xFFFF)
            c = ~(c + (c >> 16)) & 0xFFFF
            body = body[:2] + struct.pack("!H", c) + body[4:]
        s.sendto(body, (dst, 0))
        end = time.time() + 1.5
        try:
            while time.time() < end:
                data, peer = s.recvfrom(2048)
                if not v6:
                    data = data[(data[0] & 0x0F) * 4:]
                if data[0] == rep and struct.unpack("!H", data[4:6])[0] == ident and peer[0] == dst:
                    return "reply"
        except socket.timeout:
            pass
        finally:
            s.close()
        return "timeout"

    def probe():
        before = decoy_tx()
        r = {}
        for who, vrf, s6, s4 in (("tenant", "ten", TEN6, TEN4), ("ce", "cev", CE6, CE4)):
            r[who] = {"echo6": echo(vrf, s6, ICMP6), "echo4": echo(vrf, s4, XE4),
                      "tcp6": connect(vrf, s6, ICMP6, 179), "tcp4": connect(vrf, s4, XE4, 179),
                      "udp6": udp(vrf, s6, ICMP6), "udp4": udp(vrf, s4, XE4)}
        for l in listeners:  # accepted connections are dropped again
            l.setblocking(False)
            try:
                while True:
                    l.accept()[0].close()
            except BlockingIOError:
                pass
        r["decoy_tx"] = decoy_tx() - before
        return r

    out = {"before": probe()}
    for f in sys.argv[1:3]:
        subprocess.run(["nft", "-f", f], check=True, capture_output=True)
    out["after"] = probe()
    out["ruleset"] = subprocess.run(["nft", "list", "ruleset"], capture_output=True, text=True).stdout
    print(json.dumps(out))
''')


class RealKernel(A.ApplyCase):
    def test_probes(self):
        import test_icmp_source_announce as G
        if not G.can_probe():
            self.skipTest("no unprivileged user and network namespace with VRF devices, nft and ip here")
        self.h.conf("root", with_circuit(root6()))
        self.h.ok(self, "root")
        files = []
        for t in (T4, T6):
            files.append(os.path.join(self.tmp, t.split()[1] + ".nft"))
            with open(files[-1], "w") as f:
                f.write(self.h.get_state()["ns"][""]["nft"][t])
        script = os.path.join(self.tmp, "probe.py")
        with open(script, "w") as f:
            f.write(PROBE)
        p = subprocess.run(["unshare", "-rn", sys.executable, "-I", script] + files, capture_output=True, text=True,
                           timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = json.loads(p.stdout)
        if self.evidence:
            with open(os.path.join(self.evidence, "root-real-kernel-probes.json"), "w") as f:
                json.dump(r, f, indent=1)
        everything = {"echo6": "reply", "echo4": "reply", "tcp6": "connected", "tcp4": "connected",
                      "udp6": "delivered", "udp4": "delivered"}
        # without the filters a network reaches the node's listeners in the VRF, both families
        self.assertEqual(r["before"], {"tenant": everything, "ce": everything, "decoy_tx": 0})
        # with them: from the network (a slave that is no leg) echo still answered, IPv6 reset / refused as a closed
        # port, IPv4 dropped without an answer; from the circuit's device (a leg) the listener is reached as before;
        # nothing left through the main table's decoy
        self.assertEqual(r["after"], {
            "tenant": {"echo6": "reply", "echo4": "reply", "tcp6": "reset", "tcp4": "timeout", "udp6": "refused",
                       "udp4": "timeout"},
            "ce": everything, "decoy_tx": 0})
        counts = {c: int(n) for n, c in re.findall(r'counter packets (\d+) bytes \d+ \S+(?: with [^"]*)? comment "([^"]+)"',
                                                    r["ruleset"])}
        self.assertGreaterEqual(counts.get("TCP without ACK or RST: reset", 0), 1, r["ruleset"])
        self.assertGreaterEqual(counts.get("UDP: port unreachable", 0), 1, r["ruleset"])
        self.assertGreaterEqual(counts.get("from a network to the node: only ICMP and TCP with ACK or RST", 0), 2,
                                r["ruleset"])


if __name__ == "__main__":
    unittest.main()

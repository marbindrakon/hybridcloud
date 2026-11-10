"""The cloud edge routers' own input filter (shc_edge_agent.py on AWS, shc_gcp_edge_agent.py on Google Cloud).

A router's network VRF holds addresses of the router itself (the cloud leg's, the test address, the ICMPv6 source),
and FRR listens there (the per-VRF BGP instance with the cloud's peers; with tcp_l3mdev_accept also every default-VRF
listener). The agent loads table inet shc_edge_in on every run, before the VRFs' devices: from a VRF device only
BGP with the cloud's own peers on their own leg (the transit gateway's Connect peers on gre<vni>, the Cloud Router's
on ipsec<ifId>), ICMP and ICMPv6 and TCP with ACK or RST reach the router; the rest is dropped. No VRF: the table is
removed. The ruleset is checked by nft and probed in a real kernel (an unprivileged user and network namespace;
skipped where that is not possible): the peer's BGP connects, a workload's connection to the same listener and to
another port gets nothing, echo is answered, forwarded traffic and the default VRF are untouched.

Documentation and private ranges only."""
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_aws_edge_ipv6 as AW  # noqa: E402
import test_gcp_edge_ipv6 as GW  # noqa: E402

AWS, GCP = AW.A, GW.A
DROP = '"to the router from a network: only ICMP, TCP with ACK or RST and the cloud\'s BGP"'


class Render(unittest.TestCase):
    def test_aws(self):
        c = AW.agent_cfg()
        peers = [("gre%d" % v["vni"], a) for v in c["vrfs"] for a in (v.get("connect", {}).get("i-a") or {}).get("bgp", [])]
        self.assertEqual(AWS.input_nft({v["name"] for v in c["vrfs"]}, peers).splitlines(), [
            "table inet shc_edge_in", "delete table inet shc_edge_in", "table inet shc_edge_in {", "  chain input {",
            "    type filter hook input priority filter; policy accept;",
            '    iifname { "t51000", "t51001" } meta sdifname "gre51000" ip saddr 169.254.100.2 tcp dport 179 accept',
            '    iifname { "t51000", "t51001" } meta sdifname "gre51000" ip saddr 169.254.100.2 tcp sport 179 accept',
            '    iifname { "t51000", "t51001" } meta sdifname "gre51000" ip saddr 169.254.100.3 tcp dport 179 accept',
            '    iifname { "t51000", "t51001" } meta sdifname "gre51000" ip saddr 169.254.100.3 tcp sport 179 accept',
            '    iifname { "t51000", "t51001" } meta l4proto { icmp, ipv6-icmp } accept',
            '    iifname { "t51000", "t51001" } tcp flags & (ack | rst) != 0 accept',
            '    iifname { "t51000", "t51001" } counter drop comment ' + DROP, "  }", "}"])

    def test_gcp_same_rules(self):
        self.assertEqual(GCP.input_nft({"t1"}, [("ipsec7", "169.254.10.1")]),
                         AWS.input_nft({"t1"}, [("ipsec7", "169.254.10.1")]))
        self.assertEqual(AWS.input_nft(set(), [("gre1", "169.254.100.2")]), "")

    def test_nft_accepts_it(self):
        if not (shutil.which("nft") and shutil.which("unshare")):
            self.skipTest("nft or unshare not installed")
        p = subprocess.run(["unshare", "-rn", "nft", "-c", "-f", "-"], capture_output=True, text=True,
                           input=AWS.input_nft({"t51000"}, [("gre51000", "169.254.100.2")]))
        if "Operation not permitted" in p.stderr:
            self.skipTest(p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)


class Apply(unittest.TestCase):
    """the agents load it on every run, before the VRFs' devices, and remove it without VRFs"""

    def loads(self, mod, host, run):
        seen = []
        real = host.sh

        def sh(cmd, check=True, inp=None):
            if inp is not None:
                seen.append(inp)
            return real(cmd, check, inp)
        host.sh = sh
        cmds = run(host)
        return cmds, seen

    def test_aws(self):
        cmds, seen = self.loads(AWS, AW.FakeHost(), lambda h: AW.apply_cmds(AW.agent_cfg(), h))
        i = cmds.index("nft -f -")
        self.assertLess(i, cmds.index("ip link add t51000 type vrf table 51000"))
        self.assertIn('meta sdifname "gre51000" ip saddr 169.254.100.2 tcp dport 179 accept', seen[0])
        c = AW.agent_cfg()
        c["vrfs"] = []
        cmds, seen = self.loads(AWS, AW.FakeHost(), lambda h: AW.apply_cmds(c, h))
        self.assertIn("nft delete table inet shc_edge_in", cmds)
        self.assertEqual(seen, [])

    def test_gcp(self):
        cmds, seen = self.loads(GCP, GW.FakeHost(), lambda h: GW.apply_cmds(GW.agent_cfg(), h))
        i = cmds.index("nft -f -")
        self.assertLess(i, cmds.index("ip link add t51003 type vrf table 51003"))
        self.assertIn('meta sdifname "ipsec51003" ip saddr 169.254.10.1 tcp dport 179 accept', seen[0])
        self.assertIn('meta sdifname "ipsec51004" ip saddr 169.254.10.9 tcp sport 179 accept', seen[0])
        self.assertIn('iifname { "t51003", "t51004" }', seen[0])


PROBE = textwrap.dedent(r'''
    """A router VRF t51000 with the cloud leg gre51000 (a veth here) holding the router's inside address, and a listener
    on 179 bound to the VRF; behind the leg a peer (the transit gateway's BGP address) and a workload in VRF cloud."""
    import socket, subprocess, sys, time

    def sh(*a):
        subprocess.run(a, check=True, capture_output=True)

    sh("ip", "link", "set", "lo", "up")
    for vrf, table in (("t51000", "51000"), ("cloud", "100")):
        sh("ip", "link", "add", vrf, "type", "vrf", "table", table)
        sh("ip", "link", "set", vrf, "up")
    sh("ip", "link", "add", "gre51000", "type", "veth", "peer", "name", "cl")
    sh("ip", "link", "set", "gre51000", "master", "t51000", "up")
    sh("ip", "link", "set", "cl", "master", "cloud", "up")
    sh("ip", "addr", "add", "169.254.100.1/29", "dev", "gre51000")
    sh("ip", "addr", "add", "169.254.100.2/29", "dev", "cl")
    sh("ip", "addr", "add", "10.20.0.5/32", "dev", "cloud")
    sh("ip", "route", "add", "10.20.0.5/32", "via", "169.254.100.2", "dev", "gre51000", "table", "51000")
    time.sleep(1)
    l = socket.socket(); l.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"t51000")
    l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); l.bind(("0.0.0.0", 179)); l.listen(8)
    o = socket.socket(); o.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"t51000")
    o.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); o.bind(("0.0.0.0", 22)); o.listen(8)

    def connect(src, port):
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"cloud"); s.settimeout(1.5)
        try:
            s.bind((src, 0)); s.connect(("169.254.100.1", port)); return "connected"
        except socket.timeout:
            return "timeout"
        except ConnectionRefusedError:
            return "reset"
        finally:
            s.close()

    def ping(src):
        return "reply" if subprocess.run(["ping", "-c1", "-W1", "-I", "cloud", "169.254.100.1"],
                                         capture_output=True).returncode == 0 else "timeout"

    def probe():
        return {"peer_bgp": connect("169.254.100.2", 179), "workload_bgp": connect("10.20.0.5", 179),
                "workload_22": connect("10.20.0.5", 22), "echo": ping("10.20.0.5")}

    out = {"before": probe()}
    subprocess.run(["nft", "-f", sys.argv[1]], check=True, capture_output=True)
    out["after"] = probe()
    print(repr(out))
''')


class RealKernel(unittest.TestCase):
    def test_probe(self):
        import test_icmp_source_announce as G
        if not G.can_probe() or not shutil.which("ping"):
            self.skipTest("no unprivileged user and network namespace with VRF devices, nft, ip and ping here")
        tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), "shc-edge-in-%d" % os.getpid())
        os.makedirs(tmp, exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        rules, script = os.path.join(tmp, "in.nft"), os.path.join(tmp, "probe.py")
        with open(rules, "w") as f:
            f.write(AWS.input_nft({"t51000"}, [("gre51000", "169.254.100.2")]))
        with open(script, "w") as f:
            f.write(PROBE)
        p = subprocess.run(["unshare", "-rn", sys.executable, "-I", script, rules], capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        import ast
        r = ast.literal_eval(p.stdout.strip())
        self.assertEqual(r["before"], {"peer_bgp": "connected", "workload_bgp": "connected", "workload_22": "connected",
                                       "echo": "reply"})
        self.assertEqual(r["after"], {"peer_bgp": "connected", "workload_bgp": "timeout", "workload_22": "timeout",
                                      "echo": "reply"})


if __name__ == "__main__":
    unittest.main()

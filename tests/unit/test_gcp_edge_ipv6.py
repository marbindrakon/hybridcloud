"""IPv6 on the Google Cloud edge (design/IPV6-PLAN.md §6.1, §6.3): the routers' config (net-up's tunnels merge with
Google's IPv6 next hops and the VRF's IPv6 side, the site-level ICMPv6 sources, everything IPv6 removed with the edge
switched off), the router agent's Libreswan, FRR and kernel rendering (two conns per IPv6 tunnel on one
ipsec-interface, the IPv4-only render unchanged byte for byte), its state report, and the gateway job's tasks that
compute the routers' ICMPv6 sources. The Google APIs are not called: the config secret is an in-memory store.
Documentation and private ranges only."""
import contextlib
import copy
import hashlib
import io
import ipaddress
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_gcp_edge as E  # noqa: E402
import shc_gcp_edge_agent as A  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
CG = os.path.join(ROLES, "cloudgateway_provision")
COMMON_TASKS = os.path.join(ROOT, "eda", "common", "tasks")
REAL = load(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml"))["fabric_realization_defaults"]

RA, RB = "shc-edge-site-g-a", "shc-edge-site-g-b"
NODES = {RA: {"name": RA, "address": "192.0.2.10", "externalIp": "203.0.113.10", "underlay": "10.250.1.10", "publicKey": "ka"},
         RB: {"name": RB, "address": "192.0.2.11", "externalIp": "203.0.113.11", "underlay": "10.250.1.11", "publicKey": "kb"}}
# Google's next-hop pair per peer (the router's side /126 on its XFRM device) and the placement's external /64
IN6_A, NH6_A = "2001:db8:2d00:2::a5aa/126", "2001:db8:2d00:2::a5a9"
IN6_B, NH6_B = "2001:db8:2d00:2::a5ae/126", "2001:db8:2d00:2::a5ad"
SUBNET6 = "2001:db8:aaaa:640::/64"
NET6 = "2001:db8:4005:1f00::/56"
ICMP_A = "2001:db8:4005:1::64:5/128"


def tunnel(ifid, peer, inside, bgp, psk, inside6=None, bgp6=None):
    t = {"ifId": ifid, "peerIp": peer, "inside": inside, "bgp": [bgp], "psk": psk}
    if inside6:
        t["inside6"], t["bgp6"] = inside6, [bgp6]
    return t


def agent_cfg(ipv6=None, icmp=True, test=True, v6tunnels=True):
    v1 = {"name": "t51003", "vni": 51003, "mtu": 1370, "peerAsn": 65020,
          "vpn": {RA: [tunnel(51003, "198.51.100.1", "169.254.10.2/30", "169.254.10.1", "s-a",
                              *((IN6_A, NH6_A) if v6tunnels else ()))],
                  RB: [tunnel(51003, "198.51.100.2", "169.254.10.6/30", "169.254.10.5", "s-b",
                              *((IN6_B, NH6_B) if v6tunnels else ()))]}}
    if test:
        v1["testAddress"] = {RA: "198.18.1.1", RB: "198.18.1.2"}
    if ipv6 is not None:
        v1["ipv6"] = ipv6
    v2 = {"name": "t51004", "vni": 51004, "mtu": 1370, "peerAsn": 65020,
          "vpn": {RA: [tunnel(51004, "198.51.100.3", "169.254.10.10/30", "169.254.10.9", "s-c")]}}
    c = {"site": "site-g", "asn": 64512, "bgpPassword": "pw", "vpnMtu": 1406,
         "tunnel": {"cidr": "192.0.2.0/24", "mtu": 1420, "listenPort": 51820}, "nodes": copy.deepcopy(NODES),
         "gateways": [{"name": "gw1", "publicKey": "k1", "address": "192.0.2.1"},
                      {"name": "gw2", "publicKey": "k2", "address": "192.0.2.2"}],
         "vrfs": [v1, v2]}
    if icmp:
        c["ipv6"] = {"icmp6": {RA: ICMP_A}}
    return c


def six(prefixes=(SUBNET6,), network=(NET6,)):
    return {"prefixes": list(prefixes), "network": list(network)}


# the agent's renders before IPv6 for this configuration, byte for byte
IPV4_FRR = (
    "frr defaults traditional\nhostname shc-edge-site-g-a\nservice integrated-vtysh-config\n!\nvrf t51003\n vni 51003\n"
    "exit-vrf\n!\nroute-map t51003-vpn-in permit 10\nexit\nroute-map t51003-vpn-out permit 10\nexit\n!\n"
    "ip prefix-list t51003-test seq 5 permit 198.18.1.1/32\nroute-map t51003-connected permit 10\n"
    " match ip address prefix-list t51003-test\nexit\n!\nvrf t51004\n vni 51004\nexit-vrf\n!\n"
    "route-map t51004-vpn-in permit 10\nexit\nroute-map t51004-vpn-out permit 10\nexit\n!\nrouter bgp 64512\n"
    " bgp router-id 192.0.2.10\n no bgp default ipv4-unicast\n neighbor GW peer-group\n neighbor GW remote-as 64512\n"
    " neighbor GW bfd\n neighbor GW timers connect 10\n neighbor GW password pw\n neighbor 192.0.2.1 peer-group GW\n"
    " neighbor 192.0.2.2 peer-group GW\n address-family l2vpn evpn\n  neighbor GW activate\n  advertise-all-vni\n"
    " exit-address-family\nexit\n!\nrouter bgp 64512 vrf t51003\n bgp router-id 192.0.2.10\n"
    " no bgp default ipv4-unicast\n neighbor VPN peer-group\n neighbor VPN remote-as 65020\n neighbor VPN timers 20 60\n"
    " neighbor VPN timers connect 10\n neighbor 169.254.10.1 peer-group VPN\n neighbor 169.254.10.1 interface ipsec51003\n"
    " address-family ipv4 unicast\n  neighbor VPN activate\n  neighbor VPN route-map t51003-vpn-in in\n"
    "  neighbor VPN route-map t51003-vpn-out out\n  redistribute connected route-map t51003-connected\n"
    " exit-address-family\n address-family l2vpn evpn\n  advertise ipv4 unicast\n exit-address-family\nexit\n!\n"
    "router bgp 64512 vrf t51004\n bgp router-id 192.0.2.10\n no bgp default ipv4-unicast\n neighbor VPN peer-group\n"
    " neighbor VPN remote-as 65020\n neighbor VPN timers 20 60\n neighbor VPN timers connect 10\n"
    " neighbor 169.254.10.9 peer-group VPN\n neighbor 169.254.10.9 interface ipsec51004\n address-family ipv4 unicast\n"
    "  neighbor VPN activate\n  neighbor VPN route-map t51004-vpn-in in\n  neighbor VPN route-map t51004-vpn-out out\n"
    " exit-address-family\n address-family l2vpn evpn\n  advertise ipv4 unicast\n exit-address-family\nexit\n!\n")


def conn(name, peer, ifid, sub="0.0.0.0/0"):
    return ("conn %s\n    type=tunnel\n    authby=secret\n    ikev2=insist\n    left=10.250.1.10\n    leftid=203.0.113.10\n"
            "    leftnexthop=10.250.1.1\n    right=%s\n    rightid=%s\n    leftsubnet=%s\n    rightsubnet=%s\n"
            "    ike=aes_gcm256-sha2_512;dh20\n    esp=aes_gcm256;dh20\n    pfs=yes\n    ikelifetime=36000s\n"
            "    salifetime=10800s\n    dpddelay=10\n    dpdaction=restart\n    encapsulation=yes\n"
            "    ipsec-interface=%d\n    auto=start\n" % (name, peer, peer, sub, sub, ifid))


IPV4_IPSEC = (conn("shc-51003-51003", "198.51.100.1", 51003) + "\n" + conn("shc-51004-51004", "198.51.100.3", 51004),
              '203.0.113.10 198.51.100.1 : PSK "s-a"\n203.0.113.10 198.51.100.3 : PSK "s-c"\n')


def render(c, me=RA):
    return A.frr_conf(c, me, c["nodes"][me])


def ipsec(c, me=RA):
    return A.ipsec_conf(c, me, c["nodes"][me], "10.250.1.10", "10.250.1.1")


def lines(text, prefix):
    return [x for x in text.splitlines() if x.startswith(prefix)]


class AgentRender(unittest.TestCase):
    def test_ipv4_render_unchanged(self):
        # no IPv6 anywhere; site-level ICMPv6 sources alone; tunnels with Google's IPv6 next hops but no VRF ipv6
        for c in (agent_cfg(icmp=False, v6tunnels=False), agent_cfg(), agent_cfg(icmp=True, v6tunnels=True)):
            self.assertEqual(render(c), IPV4_FRR)
            self.assertEqual(ipsec(c), IPV4_IPSEC)
        # a VRF with an ipv6 block whose tunnels have no inside6 yet (the peer without IPv6) is still IPv4 only
        c = agent_cfg(six(), v6tunnels=False)
        self.assertEqual(render(c), IPV4_FRR)
        self.assertEqual(ipsec(c), IPV4_IPSEC)

    def test_ipv6_vrf_frr(self):
        out = render(agent_cfg(six()))
        self.assertEqual(lines(out, "ipv6 prefix-list t51003-in6"),
                         ["ipv6 prefix-list t51003-in6 seq 5 permit 2001:db8:aaaa:640::/64 le 128"])
        # never the default route toward Google, never the placement's own /64
        self.assertEqual(lines(out, "ipv6 prefix-list t51003-out6"),
                         ["ipv6 prefix-list t51003-out6 seq 5 deny ::/0",
                          "ipv6 prefix-list t51003-out6 seq 10 deny 2001:db8:aaaa:640::/64 le 128",
                          "ipv6 prefix-list t51003-out6 seq 15 permit 2001:db8:4005:1f00::/56 le 128"])
        self.assertNotIn("permit ::/0", out)
        self.assertIn("route-map t51003-vpn6-in permit 10\n match ipv6 address prefix-list t51003-in6\nexit\n", out)
        self.assertIn("route-map t51003-vpn6-out permit 10\n match ipv6 address prefix-list t51003-out6\n"
                      " set ipv6 next-hop global 2001:db8:2d00:2::a5aa\nexit\n", out)
        self.assertIn(" exit-address-family\n address-family ipv6 unicast\n  neighbor VPN activate\n"
                      "  neighbor VPN route-map t51003-vpn6-in in\n  neighbor VPN route-map t51003-vpn6-out out\n"
                      " exit-address-family\n address-family l2vpn evpn\n  advertise ipv4 unicast\n"
                      "  advertise ipv6 unicast route-map t51003-evpn6\n exit-address-family\n", out)
        self.assertIn("route-map t51003-evpn6 permit 10\n match ipv6 address prefix-list t51003-in6\nexit\n", out)
        # the ICMPv6 source on the fabric-side routes, the Cloud Router's routes as they are
        self.assertIn("vrf t51003\n vni 51003\n ipv6 protocol bgp route-map t51003-src6\nexit-vrf\n", out)
        self.assertIn("route-map t51003-src6 permit 10\n match ipv6 address prefix-list t51003-in6\nexit\n"
                      "route-map t51003-src6 permit 20\n set src 2001:db8:4005:1::64:5\nexit\n", out)
        # the IPv4 side as before, the IPv4-only VRF untouched
        self.assertIn("  neighbor VPN route-map t51003-vpn-in in\n  neighbor VPN route-map t51003-vpn-out out\n", out)
        self.assertNotIn("t51004-in6", out)
        self.assertEqual(out.split("router bgp 64512 vrf t51004\n")[1],
                         IPV4_FRR.split("router bgp 64512 vrf t51004\n")[1])

    def test_ipv6_vrf_other_router_own_next_hop(self):
        out = render(agent_cfg(six()), RB)
        self.assertIn(" set ipv6 next-hop global 2001:db8:2d00:2::a5ae\n", out)
        # no ICMPv6 source for this router: no src route-map
        self.assertNotIn("src6", out)
        self.assertIn("address-family ipv6 unicast", out)

    def test_nothing_to_announce_or_accept(self):
        out = render(agent_cfg(six(prefixes=(), network=())))
        self.assertEqual(lines(out, "ipv6 prefix-list t51003-in6"), ["ipv6 prefix-list t51003-in6 seq 5 deny ::/0 le 128"])
        self.assertEqual(lines(out, "ipv6 prefix-list t51003-out6"), ["ipv6 prefix-list t51003-out6 seq 5 deny ::/0"])

    def test_two_ipv6_tunnels_on_one_router_take_the_session_address(self):
        c = agent_cfg(six())
        c["vrfs"][0]["vpn"][RA].append(tunnel(51013, "198.51.100.9", "169.254.10.14/30", "169.254.10.13", "s-d",
                                              "2001:db8:2d00:2::a5b2/126", "2001:db8:2d00:2::a5b1"))
        out = render(c)
        self.assertNotIn("next-hop global", out)
        self.assertIn(" neighbor 169.254.10.13 interface ipsec51013\n", out)

    def test_ipv6_conns(self):
        conns, secrets = ipsec(agent_cfg(six()))
        self.assertEqual(conns, conn("shc-51003-51003-v4", "198.51.100.1", 51003) + "\n"
                         + conn("shc-51003-51003-v6", "198.51.100.1", 51003, "::/0") + "\n"
                         + conn("shc-51004-51004", "198.51.100.3", 51004))
        # one secret per tunnel (by the IDs), shared by both conns: the file is the IPv4-only one
        self.assertEqual(secrets, IPV4_IPSEC[1])
        # the two conns differ only in their name and subnets (identical IKE parameters, same ipsec-interface)
        v4, v6 = conns.split("\n\n")[:2]
        strip = [x for x in v4.splitlines() if "subnet" not in x and not x.startswith("conn ")]
        self.assertEqual(strip, [x for x in v6.splitlines() if "subnet" not in x and not x.startswith("conn ")])
        self.assertEqual(A.wanted_conns(agent_cfg(six()), RA),
                         ["shc-51003-51003-v4", "shc-51003-51003-v6", "shc-51004-51004"])
        self.assertEqual(A.wanted_conns(agent_cfg(), RA), ["shc-51003-51003", "shc-51004-51004"])

    def test_loaded_conns(self):
        status = ('000 "shc-51003-51003": 10.250.1.10[203.0.113.10]...198.51.100.1; erouted; eroute owner: #2\n'
                  '000 "shc-51003-51003":   ike_life: 36000s;\n'
                  '"shc-51004-51004-v6": 10.250.1.10...198.51.100.3\n'
                  '000 #3: "shc-51004-51004-v4":4500 STATE_V2_ESTABLISHED_CHILD_SA\n'
                  '000 "other": 1.2.3.4\n000 \n')
        self.assertEqual(A.loaded_conns(status), {"shc-51003-51003", "shc-51004-51004-v6"})


class FakeHost:
    """The commands apply() runs, against a router with nothing configured yet (Libreswan's conns as loaded)."""

    def __init__(self, links=(), addrs6=None, loaded=""):
        self.cmds, self.links, self.addrs6, self.loaded = [], set(links), addrs6 or {}, loaded

    def sh(self, cmd, check=True, inp=None):
        self.cmds.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        c = cmd if isinstance(cmd, list) else cmd.split()
        if c[:4] == ["ip", "-j", "-6", "addr"]:
            dev = c[c.index("dev") + 1]
            return json.dumps([{"addr_info": [{"family": "inet6", "local": a.split("/")[0], "prefixlen": int(a.split("/")[1]),
                                               "scope": "global"} for a in self.addrs6.get(dev, [])]}])
        if c[:3] == ["ip", "link", "add"]:
            self.links.add(c[3])
        if c[:3] == ["ipsec", "auto", "--status"]:
            return self.loaded
        if "default" in cmd and "awk" in cmd:
            return "eth0\n" if "$5" in cmd else "10.250.1.1\n"
        if "addr show dev eth0" in cmd:
            return "10.250.1.10/32\n"
        return ""

    def link_exists(self, name):
        return name in self.links


class _Run:
    returncode = 0


def apply_cmds(c, host, me=RA, ipsec_changed=True):
    with mock.patch.object(A, "sh", host.sh), mock.patch.object(A, "link_exists", host.link_exists), \
            mock.patch.object(A, "write_if_changed", lambda path, *a, **k: ipsec_changed and "ipsec" in path), \
            mock.patch.object(A.subprocess, "run", lambda *a, **k: _Run()):
        A.apply(c, me)
    return host.cmds


class AgentApply(unittest.TestCase):
    def test_ipv6_vrf(self):
        host = FakeHost(addrs6={"ipsec51003": ["2001:db8:2d00:2::9/126"]}, loaded='000 "shc-51003-51003": x\n')
        cmds = apply_cmds(agent_cfg(six(), test=False), host)
        self.assertIn("sysctl -qw net.ipv6.conf.all.forwarding=1 net.ipv6.conf.default.forwarding=1 net.ipv6.icmp.ratelimit=0", cmds)
        # the old single conn goes before the two per-family conns come up
        i_del = cmds.index("ipsec auto --delete shc-51003-51003")
        self.assertLess(i_del, cmds.index("ipsec auto --replace shc-51003-51003-v4"))
        self.assertLess(cmds.index("ipsec auto --replace shc-51003-51003-v4"), cmds.index("ipsec auto --replace shc-51003-51003-v6"))
        self.assertIn("ipsec auto --asynchronous --up shc-51003-51003-v6", cmds)
        self.assertNotIn("ipsec auto --delete shc-51004-51004", cmds)
        # the XFRM device after Libreswan: VRF master, MTU, both addresses (the stale IPv6 one removed)
        i_up = cmds.index("ipsec auto --asynchronous --up shc-51004-51004")
        i_master = cmds.index("ip link set ipsec51003 master t51003 mtu 1406 up")
        self.assertLess(i_up, i_master)
        self.assertLess(i_master, cmds.index("ip addr replace 169.254.10.2/30 dev ipsec51003"))
        self.assertIn("ip -6 addr del 2001:db8:2d00:2::9/126 dev ipsec51003", cmds)
        self.assertIn("ip -6 addr replace 2001:db8:2d00:2::a5aa/126 dev ipsec51003 nodad", cmds)
        # and once more after FRR, since Libreswan brought its conns up asynchronously
        self.assertEqual(cmds.count("ip link set ipsec51003 master t51003 mtu 1406 up"), 2)
        self.assertGreater(len(cmds) - 1 - cmds[::-1].index("ip link set ipsec51003 master t51003 mtu 1406 up"),
                           cmds.index("ip link set vx51004 master br51004 mtu 1370 up"))
        # the ICMPv6 source and the unreachable default in the VRF
        self.assertIn("ip link add lo51003 type dummy", cmds)
        self.assertIn("ip link set lo51003 master t51003 up", cmds)
        self.assertIn("ip -4 addr flush dev lo51003 scope global", cmds)
        self.assertIn("ip -6 addr replace 2001:db8:4005:1::64:5/128 dev lo51003 nodad", cmds)
        self.assertIn("ip -6 route replace unreachable default metric 4278198272 table 51003", cmds)
        # nothing IPv6 for the IPv4-only VRF; the MTUs as before
        self.assertFalse([x for x in cmds if "51004" in x and ("-6" in x.split() or "-v6" in x)])
        self.assertIn("ip link set vx51003 master br51003 mtu 1370 up", cmds)

    def test_ipv4_only_runs_no_ipv6_command(self):
        cmds = apply_cmds(agent_cfg(), FakeHost(loaded='000 "shc-51003-51003": x\n000 "shc-51009-51009": y\n'))
        self.assertEqual([x for x in cmds if ("-6" in x.split() and "route del" not in x) or "ipv6" in x], [])
        self.assertIn("ip addr replace 198.18.1.1/32 dev lo51003", cmds)
        self.assertIn("ipsec auto --replace shc-51003-51003", cmds)
        self.assertIn("ipsec auto --replace shc-51004-51004", cmds)
        # a conn no longer configured is deleted (Libreswan 4.x numbers its status lines)
        self.assertIn("ipsec auto --delete shc-51009-51009", cmds)
        self.assertNotIn("ipsec auto --delete shc-51003-51003", cmds)

    def test_unchanged_ipsec_touches_no_conn(self):
        cmds = apply_cmds(agent_cfg(six()), FakeHost(), ipsec_changed=False)
        self.assertFalse([x for x in cmds if x.startswith("ipsec auto")])
        # the devices are asserted on every run all the same
        self.assertEqual(cmds.count("ip link set ipsec51003 master t51003 mtu 1406 up"), 1)
        self.assertIn("ip -6 addr replace 2001:db8:2d00:2::a5aa/126 dev ipsec51003 nodad", cmds)

    def test_ipv6_switched_off_removes_it(self):
        host = FakeHost(links={"lo51003", "ipsec51003"}, addrs6={"ipsec51003": [IN6_A], "lo51003": [ICMP_A]})
        c = agent_cfg(six(), test=True, v6tunnels=False)
        cmds = apply_cmds(c, host)
        self.assertIn("ip -6 addr del %s dev ipsec51003" % IN6_A, cmds)
        self.assertIn("ip -6 addr del %s dev lo51003" % ICMP_A, cmds)
        self.assertIn("ip -6 route del unreachable default metric 4278198272 table 51003", cmds)
        self.assertFalse([x for x in cmds if x.startswith("sysctl")])


class AgentState(unittest.TestCase):
    def vtysh(self, outputs):
        def sh(cmd, check=True, inp=None):
            c = cmd if isinstance(cmd, str) else " ".join(cmd)
            if c.startswith("wg show"):
                return "pubkey 1700000000\n"
            if c.startswith("ipsec trafficstatus"):
                return ('006 #3: "shc-51003-51003-v4", type=ESP, add_time=1, inBytes=1, outBytes=2\n'
                        '006 #2: "shc-51003-51003-v6", type=ESP, add_time=1, inBytes=1, outBytes=0\n')
            for k, v in outputs.items():
                if c.endswith(k):
                    return json.dumps(v)
            return "{}"
        return sh

    OUT = {
        "show bgp vrf t51003 summary json": {
            "ipv4Unicast": {"peers": {"169.254.10.1": {"state": "Established", "pfxRcd": 1, "pfxSnt": 3}}},
            "ipv6Unicast": {"peers": {"169.254.10.1": {"state": "Established", "pfxRcd": 7, "pfxSnt": 9}}}},
        "show bgp vrf t51003 ipv6 unicast summary json": {
            "peers": {"169.254.10.1": {"state": "Established", "pfxRcd": 1, "pfxSnt": 1}}},
        "show ipv6 route vrf t51003 json": {SUBNET6: [{"protocol": "bgp"}],
                                           "2001:db8:2d00:2::a5a8/126": [{"protocol": "connected"}],
                                           "::/0": [{"protocol": "kernel"}]},
        "show ip route vrf t51003 json": {"10.0.0.0/24": [{"protocol": "bgp"}]}}

    def test_state_reports_ipv6(self):
        with mock.patch.object(A, "sh", self.vtysh(self.OUT)):
            st = A.state(agent_cfg(six()), RA)
        # the IPv4 counts are not overwritten by the IPv6 family's
        self.assertEqual(st["bgp"]["t51003"]["169.254.10.1"], {"state": "Established", "pfxRcd": 1, "pfxSnt": 3})
        self.assertEqual(st["bgp6"], {"t51003": {"169.254.10.1": {"state": "Established", "af6": "negotiated",
                                                                  "pfxRcd": 1, "pfxSnt": 1}}})
        self.assertEqual(st["routes6"]["t51003"], {SUBNET6: ["bgp"], "2001:db8:2d00:2::a5a8/126": ["connected"],
                                                   "::/0": ["kernel"]})
        self.assertEqual(st["ipsec"], {"shc-51003-51003-v4": "established", "shc-51003-51003-v6": "established"})
        with mock.patch.object(A, "sh", self.vtysh(self.OUT)):
            st4 = A.state(agent_cfg(), RA)
        self.assertEqual(sorted(st4), ["bgp", "ipsec", "routes", "wg"])

    def test_bgp6_peers(self):
        j = {"peers": {"169.254.10.1": {"state": "Established", "pfxRcd": 1, "pfxSnt": 2},
                       "169.254.10.5": {"state": "Established"}, "169.254.10.9": {"state": "Connect"}}}
        self.assertEqual({k: v["af6"] for k, v in A.bgp6_peers(j).items()},
                         {"169.254.10.1": "negotiated", "169.254.10.5": "NoNeg", "169.254.10.9": "Connect"})
        self.assertEqual(A.bgp6_peers({"ipv6Unicast": j})["169.254.10.1"]["af6"], "negotiated")


# ---- the helper (the config secret is an in-memory store)

def edge_cfg(ipv6=True):
    c = {"project": "hub-p", "region": "region-1", "zones": ["region-1-a", "region-1-b"], "vpcName": "hub",
         "subnetName": "edge", "machineType": "e2-small", "image": {"project": "x", "family": "y"}, "site": "site-g",
         "asn": 64512, "tunnel": {"cidr": "192.0.2.0/24", "mtu": 1420, "listenPort": 51820},
         "vpn": {"cloudRouterAsn": 65020, "mtu": 1406}}
    if ipv6 is not None:
        c["ipv6"] = {"enabled": ipv6}
    return c


class FakeEdge(E.Edge):
    def __init__(self, cfg, conf, reports=None):
        with mock.patch.object(E.g, "Identity") as ident:
            ident.return_value.platform.return_value = "token"
            super().__init__(cfg)
        self.store, self.reports = copy.deepcopy(conf), reports or {}

    def read_config(self):
        return copy.deepcopy(self.store)

    def write_config(self, c):
        if c != self.store:
            self.changed.append("config")
            self.store = copy.deepcopy(c)

    def node_reports(self):
        return self.reports

    def instances(self):
        return {}


def site_conf(vrfs=()):
    return {"site": "site-g", "asn": 64512, "vpnMtu": 1406, "tunnel": {"cidr": "192.0.2.0/24", "mtu": 1420, "listenPort": 51820},
            "nodes": copy.deepcopy(NODES), "gateways": [], "vrfs": list(vrfs)}


def stored_vrf(v6=False):
    return {"name": "t51003", "vni": 51003, "mtu": 1370, "peerAsn": 65020,
            "vpn": {RA: [tunnel(51003, "198.51.100.1", "169.254.10.2/30", "169.254.10.1", "s-a",
                                *((IN6_A, NH6_A) if v6 else ()))],
                    RB: [tunnel(51003, "198.51.100.2", "169.254.10.6/30", "169.254.10.5", "s-b",
                                *((IN6_B, NH6_B) if v6 else ()))]}}


def file_tunnels(psk_a=None, psk_b=None, v6=True):
    return {RA: [tunnel(51003, "198.51.100.1", "169.254.10.2/30", "169.254.10.1", psk_a, *((IN6_A, NH6_A) if v6 else ()))],
            RB: [tunnel(51003, "198.51.100.2", "169.254.10.6/30", "169.254.10.5", psk_b, *((IN6_B, NH6_B) if v6 else ()))]}


def net_up(e, tunnels, ipv6=None, use_file=False):
    with mock.patch.dict(os.environ, {"SHC_EDGE_VPN_TUNNELS": json.dumps(tunnels)}):
        os.environ.pop("SHC_EDGE_VPN_TUNNELS_FILE", None)
        if not use_file:
            return e.net_up(51003, ipv6=ipv6)
        with tempfile.NamedTemporaryFile("w", suffix=".tunnels") as f:
            json.dump(tunnels, f)
            f.flush()
            os.environ["SHC_EDGE_VPN_TUNNELS_FILE"] = f.name
            return e.net_up(51003, ipv6=ipv6)


def failure(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), unittest.TestCase().assertRaises(SystemExit):
        fn()
    return json.loads(buf.getvalue())["error"]


class TunnelsMerge(unittest.TestCase):
    def test_null_secret_keeps_the_stored_one_and_ipv6_fields_merge_in(self):
        e = FakeEdge(edge_cfg(), site_conf([stored_vrf()]))
        r = net_up(e, file_tunnels(), ipv6={"prefixes": [SUBNET6], "network": [NET6]}, use_file=True)
        vrf = e.store["vrfs"][0]
        self.assertEqual(vrf["vpn"][RA], [{"ifId": 51003, "peerIp": "198.51.100.1", "inside": "169.254.10.2/30",
                                           "bgp": ["169.254.10.1"], "psk": "s-a", "inside6": IN6_A, "bgp6": [NH6_A]}])
        self.assertEqual(vrf["vpn"][RB][0]["psk"], "s-b")
        self.assertEqual(vrf["ipv6"], {"prefixes": [SUBNET6], "network": [NET6]})
        # the result never carries a secret
        self.assertNotIn("s-a", json.dumps(r))
        self.assertEqual(r["tunnels"][RA][0]["inside6"], IN6_A)
        self.assertEqual(r["ipv6"], {"prefixes": [SUBNET6], "network": [NET6]})

    def test_new_secret_and_non_secret_fields_replace_the_stored(self):
        e = FakeEdge(edge_cfg(), site_conf([stored_vrf(v6=True)]))
        t = file_tunnels(psk_a="new-a", v6=False)
        t[RB][0]["inside"] = "169.254.10.18/30"
        net_up(e, t)
        vrf = e.store["vrfs"][0]
        self.assertEqual(vrf["vpn"][RA][0]["psk"], "new-a")
        self.assertEqual(vrf["vpn"][RB][0]["inside"], "169.254.10.18/30")
        # the file lists every tunnel: no inside6 there = the peer has no IPv6 (any more); no --ipv6 = IPv4 only
        self.assertNotIn("inside6", vrf["vpn"][RA][0])
        self.assertNotIn("ipv6", vrf)

    def test_null_secret_for_an_unknown_tunnel_names_it(self):
        e = FakeEdge(edge_cfg(), site_conf([stored_vrf()]))
        t = file_tunnels()
        t[RB][0]["peerIp"] = "198.51.100.7"
        msg = failure(lambda: net_up(e, t))
        self.assertIn("tunnel ipsec51003 of %s to 198.51.100.7" % RB, msg)
        self.assertIn("no shared secret", msg)
        # a VRF the routers don't hold at all
        msg = failure(lambda: net_up(FakeEdge(edge_cfg(), site_conf()), file_tunnels(psk_a="x")))
        self.assertIn("tunnel ipsec51003 of %s to 198.51.100.2" % RB, msg)

    def test_bad_ipv6_inputs_are_refused(self):
        e = FakeEdge(edge_cfg(), site_conf([stored_vrf()]))
        t = file_tunnels()
        t[RA][0]["inside6"] = "192.0.2.9/30"
        self.assertIn("not IPv6", failure(lambda: net_up(e, t)))
        for bad in ({"prefixes": ["::/0"], "network": []}, {"prefixes": [SUBNET6], "network": ["::/0"]},
                    {"prefixes": ["10.0.0.0/24"], "network": []}, {"prefixes": ["nonsense"], "network": []}):
            self.assertTrue(failure(lambda: net_up(e, file_tunnels(), ipv6=bad)))

    def test_edge_ipv6_off_stores_nothing_ipv6(self):
        for off in (False, None):
            e = FakeEdge(edge_cfg(off), site_conf([stored_vrf()]))
            r = net_up(e, file_tunnels(), ipv6={"prefixes": [SUBNET6], "network": [NET6]})
            vrf = e.store["vrfs"][0]
            self.assertEqual(vrf, stored_vrf())
            self.assertEqual(r, {"vni": 51003, "vrf": "t51003", "tunnels": {
                RA: [{"ifId": 51003, "peerIp": "198.51.100.1", "inside": "169.254.10.2/30", "bgp": ["169.254.10.1"]}],
                RB: [{"ifId": 51003, "peerIp": "198.51.100.2", "inside": "169.254.10.6/30", "bgp": ["169.254.10.5"]}]}})

    def test_net_known_unchanged(self):
        e = FakeEdge(edge_cfg(), site_conf([stored_vrf(v6=True)]))
        self.assertEqual(e.net_known(51003), {"vni": 51003, "known": {RA: ["198.51.100.1"], RB: ["198.51.100.2"]}})


class SiteIcmp6(unittest.TestCase):
    def test_gateways_carry_the_routers_icmp6(self):
        e = FakeEdge(edge_cfg(), site_conf())
        e.set_gateways([{"name": "gw1", "publicKey": "k1", "address": "192.0.2.1"}],
                       {RA: ICMP_A, RB: "2001:db8:4005:1::65:5/128", "shc-edge-site-g-z": "2001:db8:4005:1::66:5/128"})
        # only the site's routers
        self.assertEqual(e.store["ipv6"], {"icmp6": {RA: ICMP_A, RB: "2001:db8:4005:1::65:5/128"}})
        # without --icmp6 the stored sources stay; an empty map clears them
        e.set_gateways([])
        self.assertEqual(e.store["ipv6"]["icmp6"][RA], ICMP_A)
        e.set_gateways([], {})
        self.assertEqual(e.store["ipv6"], {"icmp6": {}})
        self.assertIn("not an IPv6 /128", failure(lambda: e.set_gateways([], {RA: "2001:db8:4005:1::/64"})))

    def test_up_takes_them_too(self):
        e = FakeEdge(edge_cfg(), site_conf())
        conf = e.set_icmp6(site_conf(), {RA: ICMP_A})
        self.assertEqual(conf["ipv6"], {"icmp6": {RA: ICMP_A}})

    def test_edge_switched_off_removes_every_ipv6_side(self):
        v = stored_vrf(v6=True)
        v["ipv6"] = {"prefixes": [SUBNET6], "network": [NET6]}
        conf = site_conf([v])
        conf["ipv6"] = {"icmp6": {RA: ICMP_A}}
        e = FakeEdge(edge_cfg(False), conf)
        e.set_gateways([], {RA: ICMP_A})
        self.assertEqual(e.store, site_conf([stored_vrf()]))
        # never on: the gateways call writes the config as before IPv6 (no ipv6 key, nothing changed)
        e = FakeEdge(edge_cfg(None), site_conf([stored_vrf()]))
        e.set_gateways([])
        self.assertEqual(e.changed, [])
        self.assertNotIn("ipv6", e.store)

    def test_startup_script(self):
        e = FakeEdge(edge_cfg(False), site_conf())
        off = e.startup_script()
        e.v6 = True
        on = e.startup_script()
        # with IPv6 off the script is the one from before IPv6, byte for byte
        self.assertEqual(hashlib.sha256((E.STARTUP % {"site": "site-g", "project": "p", "agent": "AGENT", "sysctl6": ""})
                                        .encode()).hexdigest(),
                         "fe1aa5c51fa15a7c656ac6e2ac9b6d4deb11434779950c5cebd1e97a448befbc")
        self.assertNotIn("ipv6", off)
        self.assertIn("'net.ipv6.conf.all.forwarding = 1' 'net.ipv6.conf.default.forwarding = 1' "
                      "'net.ipv6.icmp.ratelimit = 0' > /etc/sysctl.d/91-shc-edge-ipv6.conf\nsysctl --system", on)
        self.assertEqual(on.replace(
            "printf '%s\\n' 'net.ipv6.conf.all.forwarding = 1' 'net.ipv6.conf.default.forwarding = 1' "
            "'net.ipv6.icmp.ratelimit = 0' > /etc/sysctl.d/91-shc-edge-ipv6.conf\n", ""), off)


class EdgeStatus(unittest.TestCase):
    def test_ipv6_status(self):
        v = stored_vrf(v6=True)
        v["ipv6"] = {"prefixes": [SUBNET6], "network": [NET6]}
        conf = site_conf([v, dict(stored_vrf(), name="t51004", vni=51004)])
        conf["ipv6"] = {"icmp6": {RA: ICMP_A}}
        reports = {RA: {"state": {"bgp6": {"t51003": {"169.254.10.1": {"af6": "negotiated"}}},
                                  "routes6": {"t51003": {SUBNET6: ["bgp"], "2001:db8:2d00:2::a5a8/126": ["connected"]}}}},
                   RB: {"state": {"bgp6": {"t51003": {"169.254.10.5": {"af6": "NoNeg"}}}, "routes6": {"t51003": {}}}}}
        out = FakeEdge(edge_cfg(), conf, reports).status()
        self.assertEqual(out["vrfs"], [{"name": "t51003", "vni": 51003, "ipv6": {"prefixes": [SUBNET6], "network": [NET6]}},
                                       {"name": "t51004", "vni": 51004}])
        self.assertEqual(out["ipv6"], {"t51003": {"prefixes": [SUBNET6], "network": [NET6], "routers": {
            RA: {"inside6": {"ipsec51003": IN6_A}, "icmp6": ICMP_A, "sessions6": "1/1",
                 "routes6": {"bgp": 1, "connected": 1}, "default6": False, "missing6": []},
            RB: {"inside6": {"ipsec51003": IN6_B}, "icmp6": "", "sessions6": "0/1", "routes6": {}, "default6": False,
                 "missing6": [SUBNET6]}}}})
        # IPv6 off and never configured: the status as before
        out = FakeEdge(edge_cfg(None), site_conf([stored_vrf()])).status()
        self.assertNotIn("ipv6", out)
        self.assertEqual(out["vrfs"], [{"name": "t51003", "vni": 51003}])


# ---- the gateway job

def gw_router(name):
    return {"name": name, "instance": "1%d" % len(name), "zone": "z", "publicIp": "203.0.113.10", "tunnelAddress": "192.0.2.10",
            "publicKey": "k"}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GatewayJob(unittest.TestCase):
    def post(self):
        return load(os.path.join(CG, "tasks", "site_gcp_post.yml"))

    def icmp_tasks(self):
        return [copy.deepcopy(by_name(self.post(), n)) for n in
                ("The ICMPv6 source of each router", "Collect the routers' ICMPv6 sources")]

    def test_icmp6_per_router(self):
        core = dict(REAL["core"], loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
        out = run_play(self.icmp_tasks() + [write_out("{'icmp6': cg_gcp_icmp6, 'errors': cg_gcp_icmp6_errors}")], {
            "role_path": CG, "fab_name": "site-g", "fab_real": {"core": core}, "cg_pe": {"index": 5},
            "cg_ipv6_core": True,
            # out of order on purpose: the position is by name
            "cg_gcp_routers": [gw_router(RB), gw_router(RA)]})
        self.assertEqual(out["errors"], [])
        base = int(ipaddress.ip_network("2001:db8:4005:1::/64").network_address)
        self.assertEqual(out["icmp6"], {RA: "%s/128" % ipaddress.IPv6Address(base + (100 << 16) + 5),
                                        RB: "%s/128" % ipaddress.IPv6Address(base + (101 << 16) + 5)})
        self.assertEqual(out["icmp6"][RA], ICMP_A)
        # the helper takes them as they are
        self.assertEqual(E.icmp6_entries(out["icmp6"], NODES), out["icmp6"])

    def test_without_the_ipv6_core_no_icmp6(self):
        out = run_play(self.icmp_tasks() + [write_out("cg_gcp_icmp6")], {
            "role_path": CG, "fab_name": "site-g", "fab_real": {"core": dict(REAL["core"], icmpSource6="2001:db8:4005:1::/64")},
            "cg_pe": {"index": 5}, "cg_ipv6_core": False, "cg_gcp_routers": [gw_router(RA)]})
        self.assertEqual(out, {})

    def test_gateways_call_arguments(self):
        hand = by_name(self.post(), "Hand them to the routers")
        args = {"name": "args", "ansible.builtin.set_fact": {"_args": hand["vars"]["gcp_args"]}}
        first = copy.deepcopy(by_name(self.post(), "The border gateway nodes as the routers see them"))
        gws = {"cg_wg_nodes": [{"name": "n1", "publicKey": "k1"}],
               "fab_spec": {"siteUnderlay": {"addresses": [{"node": "n1", "address": "192.0.2.1"}]}}}
        gw_json = json.dumps([{"name": "n1", "publicKey": "k1", "address": "192.0.2.1"}])
        # edge IPv6 off (or the default): the call as before IPv6
        for edge in ({"ipv6": {"enabled": False}}, {}):
            out = run_play([first, args, write_out("_args")], dict(gws, gcp_edge_cfg=edge))
            self.assertEqual([out[0], out[1], json.loads(out[2])], ["gateways", "--gateways", json.loads(gw_json)])
            self.assertEqual(len(out), 3)
        out = run_play([first, args, write_out("_args")],
                       dict(gws, gcp_edge_cfg={"ipv6": {"enabled": True}}, cg_gcp_icmp6={RA: ICMP_A}))
        self.assertEqual(out[3:4], ["--icmp6"])
        self.assertEqual(json.loads(out[4]), {RA: ICMP_A})

    def test_realization_defaults(self):
        t = by_name(load(os.path.join(COMMON_TASKS, "gcp_realization.yml")), "The installation's GCP realization")
        out = run_play([copy.deepcopy(t), write_out("gcp_real")],
                       {"gcp_realization": {"hub": {"project": "p"}, "edge": {"vpcName": "hub"}}})
        self.assertEqual(out["edge"], {"vpcName": "hub", "ipv6": {"enabled": False}})
        out = run_play([copy.deepcopy(t), write_out("gcp_real")],
                       {"gcp_realization": {"hub": {}, "edge": {"ipv6": {"enabled": True}}}})
        self.assertEqual(out["edge"]["ipv6"], {"enabled": True})
        out = run_play([copy.deepcopy(t), write_out("gcp_real")], {"gcp_realization": {"hub": {}}})
        self.assertNotIn("edge", out)


if __name__ == "__main__":
    unittest.main()

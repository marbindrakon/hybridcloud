"""The IPv6 transport of the fabric core in eda/common/files/shc_fabric.py: check-fabric's validation of the
realization keys core.loopbackPool6 / sidOffset6 / icmpSource6, and the IPv6 keys pe-addresses returns (loopback6,
loopbackIp6, sid6, icmp6). Documentation prefixes only."""
import ipaddress
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_fabric as F  # noqa: E402

CORE = {"bridge": "br-core", "loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24", "isisArea": "49.0001",
        "srgb": {"start": 16000, "end": 23999}}
CORE6 = dict(CORE, loopbackPool6="2001:db8:4005:ffff::/64", sidOffset6=1000, icmpSource6="2001:db8:4005:1::/64")
SPEC = {"borderGateway": {"nodes": [{"name": "bgw-1"}, {"name": "bgw-2"}]}}


def fabric(core):
    return F.check_fabric({"spec": SPEC, "realization": {"core": core}})


class CheckFabric(unittest.TestCase):
    def test_ipv4_only_core_unchanged(self):
        r = fabric(dict(CORE, loopbackPool6="", sidOffset6=1000, icmpSource6=""))
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["peIndexMax"], 254)

    def test_dual_stack_core_passes(self):
        self.assertEqual(fabric(CORE6)["errors"], [])

    def test_loopback_pool6_must_be_ipv6(self):
        self.assertIn("not an IPv6 network", " ".join(fabric(dict(CORE6, loopbackPool6="10.254.0.0/24"))["errors"]))

    def test_loopback_pool6_at_least_120(self):
        self.assertEqual(fabric(dict(CORE6, loopbackPool6="2001:db8:4005:ffff::/120"))["errors"], [])
        e = fabric(dict(CORE6, loopbackPool6="2001:db8:4005:ffff::/121"))["errors"]
        self.assertTrue(any("loopbackPool6" in x and "at least a /120" in x for x in e), e)

    def test_loopback_pool6_not_strict_is_refused(self):
        self.assertTrue(fabric(dict(CORE6, loopbackPool6="2001:db8:4005:ffff::1/64"))["errors"])

    def test_icmp_source6_at_least_96(self):
        self.assertEqual(fabric(dict(CORE6, icmpSource6="2001:db8:4005:1::/96"))["errors"], [])
        e = fabric(dict(CORE6, icmpSource6="2001:db8:4005:1::/97"))["errors"]
        self.assertTrue(any("icmpSource6" in x and "at least a /96" in x for x in e), e)

    def test_ranges_apart(self):
        e = fabric(dict(CORE6, icmpSource6="2001:db8:4005:ffff::/96"))["errors"]
        self.assertTrue(any("overlaps core.icmpSource6" in x for x in e), e)

    def test_sid_offset_inside_srgb(self):
        # SRGB 16000-23999: index 0..7999; 7746 + 254 = 8000 is one too many
        self.assertEqual(fabric(dict(CORE6, sidOffset6=7745))["errors"], [])
        e = fabric(dict(CORE6, sidOffset6=7746))["errors"]
        self.assertTrue(any("sidOffset6" in x and "srgb" in x for x in e), e)

    def test_sid_offset_above_ipv4_indexes(self):
        e = fabric(dict(CORE6, sidOffset6=200))["errors"]
        self.assertTrue(any("not above the highest provider-edge index" in x for x in e), e)
        self.assertTrue(fabric(dict(CORE6, sidOffset6=0))["errors"])
        self.assertTrue(fabric(dict(CORE6, sidOffset6="x"))["errors"])

    def test_sid_offset_ignored_without_ipv6_loopbacks(self):
        self.assertEqual(fabric(dict(CORE, sidOffset6=0))["errors"], [])

    def test_icmp_source6_alone_is_allowed(self):
        self.assertEqual(fabric(dict(CORE, icmpSource6="2001:db8:4005:1::/64"))["errors"], [])


class PeAddresses(unittest.TestCase):
    def pe(self, core, index, **kw):
        return F.pe_addresses(dict({"core": core, "index": index}, **kw))

    def test_ipv4_only_core_empty_keys(self):
        r = self.pe(CORE, 2, nodeOrdinal=1)
        self.assertEqual(r["errors"], [])
        self.assertEqual((r["loopback6"], r["loopbackIp6"], r["sid6"], r["icmp6"]), ("", "", 0, ""))
        # today's keys unchanged
        self.assertEqual((r["loopback"], r["loopbackIp"], r["link"], r["net"], r["sid"]),
                         ("10.255.0.2/32", "10.255.0.2", "10.255.1.2/24", "49.0001.0000.0000.0002.00", 16002))

    def test_root_and_gateway_loopbacks(self):
        root, gw = self.pe(CORE6, 1), self.pe(CORE6, 2)
        self.assertEqual((root["loopback6"], root["loopbackIp6"], root["sid6"]),
                         ("2001:db8:4005:ffff::1/128", "2001:db8:4005:ffff::1", 17001))
        self.assertEqual((gw["loopback6"], gw["loopbackIp6"], gw["sid6"]),
                         ("2001:db8:4005:ffff::2/128", "2001:db8:4005:ffff::2", 17002))
        self.assertEqual(root["icmp6"], "", "no nodeOrdinal: no ICMP source")

    def test_icmp6_per_node(self):
        self.assertEqual(self.pe(CORE6, 1, nodeOrdinal=1)["icmp6"], "2001:db8:4005:1::1:1/128")
        self.assertEqual(self.pe(CORE6, 2, nodeOrdinal=2)["icmp6"], "2001:db8:4005:1::2:2/128")
        # Ansible passes numbers as text
        self.assertEqual(self.pe(CORE6, "3", nodeOrdinal="2")["icmp6"], "2001:db8:4005:1::2:3/128")
        self.assertEqual(self.pe(CORE6, 1, nodeOrdinal="")["icmp6"], "")

    def test_icmp6_bad_ordinal(self):
        self.assertTrue(self.pe(CORE6, 1, nodeOrdinal=0)["errors"])
        self.assertTrue(self.pe(dict(CORE6, icmpSource6="2001:db8:4005:1::/96"), 1, nodeOrdinal=65536)["errors"])

    def test_canonical_text(self):
        r = self.pe(dict(CORE6, loopbackPool6="2001:DB8:4005:FFFF:0::/64"), 10)
        self.assertEqual(r["loopbackIp6"], "2001:db8:4005:ffff::a")
        self.assertEqual(str(ipaddress.ip_address(r["loopbackIp6"])), r["loopbackIp6"])

    def test_index_outside_srgb_with_offset(self):
        self.assertTrue(self.pe(dict(CORE6, sidOffset6=7999), 2)["errors"])

    def test_cli(self):
        req = {"action": "pe-addresses", "core": CORE6, "index": "2", "nodeOrdinal": "1"}
        p = subprocess.run([sys.executable, "-I", os.path.join(FILES, "shc_fabric.py")], input=json.dumps(req),
                           capture_output=True, text=True, check=True)
        r = json.loads(p.stdout)
        self.assertEqual((r["loopback6"], r["icmp6"]), ("2001:db8:4005:ffff::2/128", "2001:db8:4005:1::1:2/128"))


if __name__ == "__main__":
    unittest.main()

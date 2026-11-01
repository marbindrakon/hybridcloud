"""check-gateway (shc_fabric.py): two tunnel gateways of one fabric may not share a WireGuard listen port.

A tunnel gateway's WireGuard socket is bound on the border gateway node itself, so a second gateway with the same
port on the same nodes never brings its device up ("Address already in use"). The check names the other gateway.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "eda", "common", "files"))
import shc_fabric  # noqa: E402

FABRIC = {"borderGateway": {"nodes": [{"name": "bgw-1"}, {"name": "bgw-2"}]}}
CORE = {"loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24"}


def gw(name, cloud, cidr, transport="wireguard", port=None, vlan=None):
    spec = {"fabricRef": {"name": "lab"}, "cloudRef": {"name": cloud}, "transport": transport,
            "siteUnderlay": {"cidr": cidr, "addresses": [{"node": "bgw-1", "address": cidr.rsplit(".", 1)[0] + ".1"},
                                                         {"node": "bgw-2", "address": cidr.rsplit(".", 1)[0] + ".2"}]}}
    if transport == "wireguard":
        spec["wireguard"] = {} if port is None else {"listenPort": port}
    if vlan is not None:
        spec["siteUnderlay"]["vlan"] = vlan
    return {"metadata": {"name": name}, "spec": spec}


def check(gateway, others, default=51820):
    return shc_fabric.check_gateway({"fabric": FABRIC, "core": CORE, "gateway": gateway["spec"],
                                     "others": others, "wireguardListenPort": default})["errors"]


def port_errors(errors):
    return [e for e in errors if "listen port" in e]


class WireGuardListenPort(unittest.TestCase):
    def test_two_defaults_collide(self):
        errs = port_errors(check(gw("gcp-use4", "gcp-use4", "10.240.2.0/24"), [gw("aws-use2", "aws-use2", "10.240.1.0/24")]))
        self.assertEqual(len(errs), 1)
        self.assertIn("51820", errs[0])
        self.assertIn("CloudGateway aws-use2", errs[0])

    def test_explicit_port_against_the_default(self):
        errs = port_errors(check(gw("gcp-use4", "gcp-use4", "10.240.2.0/24", port=51820),
                                 [gw("aws-use2", "aws-use2", "10.240.1.0/24")]))
        self.assertEqual(len(errs), 1)

    def test_distinct_ports_pass(self):
        self.assertEqual(port_errors(check(gw("gcp-use4", "gcp-use4", "10.240.2.0/24", port=51821),
                                           [gw("aws-use2", "aws-use2", "10.240.1.0/24")])), [])

    def test_vlan_gateways_have_no_port(self):
        self.assertEqual(port_errors(check(gw("gcp-use4", "gcp-use4", "10.240.2.0/24"),
                                           [gw("iaas1", "iaas1", "172.18.233.0/24", transport="none", vlan=233)])), [])

    def test_other_fabric_is_ignored(self):
        other = gw("aws-use2", "aws-use2", "10.240.1.0/24")
        other["spec"]["fabricRef"] = {"name": "other"}
        self.assertEqual(port_errors(check(gw("gcp-use4", "gcp-use4", "10.240.2.0/24"), [other])), [])

    def test_no_default_and_no_port_checks_nothing(self):
        self.assertEqual(port_errors(check(gw("gcp-use4", "gcp-use4", "10.240.2.0/24"),
                                           [gw("aws-use2", "aws-use2", "10.240.1.0/24")], default=None)), [])


if __name__ == "__main__":
    unittest.main()

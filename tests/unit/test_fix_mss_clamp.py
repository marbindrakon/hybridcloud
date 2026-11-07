"""shc-fabric-apply, the TCP MSS clamps on both transports (fake kernel of test_shc_fabric_apply; `nft -c` where an
unprivileged network namespace allows it). One working clamp per direction, the rules that never matched removed:
- inet shc_egress6 (root): a network's SYNs toward the internet clamped at its xe veth ("mss out", on forward and
  output: an MPLS pop transmits on the VRF device and passes output, a decapsulated packet and a circuit's pass
  forward); the internet's SYN-ACKs into a network clamped where they are still forwarded, in the egress VRF toward
  xo<n> (chain clamp_return, forward hook, "mss in"); no rule on iifname "xe*" (in forward and output such a packet's
  input is the network's VRF device, never the veth: it never matched).
- inet shc_gw_mss (gateway): SYNs and SYN-ACKs toward a site clamped to the route's MTU on the site's bridge br<vni>,
  on mpls on output (chain clamp_core: a popped packet is transmitted on the VRF device), on srv6 on forward (chain
  clamp: decapsulated on the input path); no rule on oifname "vx*" (a routed packet leaves by br<vni>).
Documentation prefixes only."""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_root_circuit_ipv6 as C  # noqa: E402
from test_shc_fabric_apply import WG6, WG_CONF, ApplyCase, blade  # noqa: E402
from test_srv6_fabric_apply import GW_SRV6, ROOT_SRV6, WG_SRV6, gw_full, nft_check, root_full  # noqa: E402

MSS_OUT = ('    oifname "xe*" tcp flags & syn == syn tcp option maxseg size > 1360 counter tcp option maxseg size set 1360 '
           'comment "mss out"\n')
CLAMP = "  chain clamp {\n" + MSS_OUT + "  }\n"
CLAMP_RETURN = ("  chain clamp_return {\n    type filter hook forward priority mangle; policy accept;\n"
                '    meta nfproto ipv6 oifname "xo*" tcp flags & syn == syn tcp option maxseg size > 1360 counter '
                'tcp option maxseg size set 1360 comment "mss in, at the egress veth"\n  }\n')
GW_HEAD = "add table inet shc_gw_mss\ndelete table inet shc_gw_mss\ntable inet shc_gw_mss {\n"
GW_RULE = '    oifname "br*" tcp flags syn tcp option maxseg size set rt mtu\n'
GW_MSS = {
    "mpls": GW_HEAD + "  chain clamp_core {\n    type filter hook output priority mangle; policy accept;\n" + GW_RULE
    + "  }\n}\n",
    "srv6": GW_HEAD + "  chain clamp {\n    type filter hook forward priority mangle; policy accept;\n" + GW_RULE
    + "  }\n}\n",
}


class EgressClamp(ApplyCase):
    def table(self, extra):
        h = self.h
        h.set_state(blade())
        h.conf("root", root_full(**extra))
        h.ok(self, "root")
        return h.get_state()["ns"][""]["nft"]["inet shc_egress6"]

    def test_both_transports(self):
        tables = {}
        for t, extra in (("mpls", {}), ("srv6", ROOT_SRV6)):
            text = tables[t] = self.table(extra)
            self.assertIn(CLAMP, text, t)
            self.assertIn(CLAMP_RETURN, text, t)
            self.assertNotIn('iifname "xe*" tcp', text, t)
            self.assertNotIn('"mss in"', text, t)
            # the clamp chain is jumped to from forward and output, clamp_return hooks forward only
            for hook in ("forward", "output"):
                self.assertIn("  chain clamp_%s { type filter hook %s priority mangle; policy accept; meta nfproto ipv6 "
                              "jump clamp; }\n" % (hook, hook), text)
            nft_check(self, text)
        # the transports differ only in the zone map (srv6: the VRF device in prerouting too)
        self.assertEqual(tables["srv6"].replace(', "e10000" : 1 }', " }"), tables["mpls"])


class GatewayClamp(ApplyCase):
    def test_both_transports(self):
        for t, rx, gx, wx in (("mpls", {}, {}, {}), ("srv6", ROOT_SRV6, GW_SRV6, WG_SRV6)):
            h = self.h
            h.set_state(blade())
            for f in os.listdir(h.etc):
                os.unlink(os.path.join(h.etc, f))
            h.conf("root", root_full(**rx))
            h.ok(self, "root")
            for name, conf in (("gw-iaas1", gw_full(**gx)), ("gw-aws1", dict(copy.deepcopy(WG_CONF), **dict(WG6, **wx)))):
                h.conf(name, conf)
                h.ok(self, name)
                text = h.get_state()["ns"][name]["nft"]["inet shc_gw_mss"]
                self.assertEqual(text, GW_MSS[t], (t, name))
                self.assertNotIn('"vx*"', text)
                nft_check(self, text.replace("add table inet shc_gw_mss\n", "table inet shc_gw_mss\n"))
            # no VRF left: no table
            h.conf("gw-iaas1", gw_full(**dict(gx, vrfs=[])))
            h.ok(self, "gw-iaas1")
            self.assertNotIn("inet shc_gw_mss", h.get_state()["ns"]["gw-iaas1"]["nft"])

    def test_nft_accepts_both_layouts(self):
        for t in ("mpls", "srv6"):
            with self.subTest(transport=t):
                err = C.nft_check(GW_MSS[t].replace("add table inet shc_gw_mss\n", "table inet shc_gw_mss\n"))
                self.assertIsNone(err)


if __name__ == "__main__":
    import unittest
    unittest.main()

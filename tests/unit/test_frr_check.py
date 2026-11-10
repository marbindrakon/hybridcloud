"""shc-frr-check: a border gateway node's FRR instance holds the whole configuration file before the job records it.

The --test outputs below are FRR 8.5.3's frr-reload.py (the blades' version), taken from a lab container that ran the
rendered root and gateway configurations: the lines that never match by design (frr version, the kernel's hostname
and domainname, service integrated-vtysh-config, mpls enable, a stanza's exit line) are left out, everything else
counts, with its context, passwords hidden. The note on the node decides between a failure (lines a previous check
did not leave) and an accepted record (FRR left the same lines after another start or reload).
"""
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT  # noqa: E402

SCRIPT = os.path.join(ROOT, "eda/common/files/bgw/shc-frr-check")


def load():
    loader = importlib.machinery.SourceFileLoader("shc_frr_check", SCRIPT)
    spec = importlib.util.spec_from_loader("shc_frr_check", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# FRR 8.5.3 after two reloads of the rendered root configuration (MPLS core) in a container whose kernel has no MPLS:
# isisd keeps the segment routing block out, and the reload still ended with exit 0
ROOT_LEFT = """
Lines To Delete
===============
no hostname 22cfc36fde7f
no domainname home.signal9.gg

Lines To Add
============
router isis CORE
 segment-routing global-block 16000 23999
frr version 8.5
hostname bgw-1
service integrated-vtysh-config
"""

# a gateway instance (its hostname is the instance's, FRR shows the kernel's): nothing left that counts
GW_CLEAN = """
Lines To Delete
===============
no hostname bgw-1
no domainname home.signal9.gg

Lines To Add
============
frr version 8.5
hostname bgw-1-gw-site1
service integrated-vtysh-config
"""

# FRR 8.5.3's layout for entries in stanzas: the context is printed again for each entry; a stanza with nothing under
# it is an entry of its own
NESTED = """
Lines To Delete
===============
router bgp 65010
 no neighbor 10.0.0.1 password s3cret
router bgp 65010
 no neighbor 10.0.0.1 timers connect 10

Lines To Add
============
interface lo
 mpls enable
interface core0
 ip router isis CORE
router bgp 65010 vrf t51000
 address-family ipv4 unicast
  neighbor 192.0.2.99 activate
vrf t51001
vrf t51001
 vni 51001
vrf t51001
 exit-vrf
router bgp 65010 vrf t51009
"""


# a gateway instance with two tenant VRFs as the blades' FRR 8.5.3 (RHEL build) showed it on a fully loaded instance
# (D.pre.check, both blades, every gateway): the EVPN route targets of each VRF, <AS>:<its L3 VNI>, never show in the
# running configuration (FRR holds them as its own automatic ones)
GW_BLADE = """
Lines To Delete
===============
no hostname bgw-1.example

Lines To Add
============
router bgp 65010 vrf t51003
 address-family l2vpn evpn
  route-target import 65010:51003
router bgp 65010 vrf t51003
 address-family l2vpn evpn
  route-target export 65010:51003
router bgp 65010 vrf t51004
 address-family l2vpn evpn
  route-target import 65010:51004
router bgp 65010 vrf t51004
 address-family l2vpn evpn
  route-target export 65010:51004
frr version 8.5
hostname bgw-1-gw-site1
service integrated-vtysh-config
"""
# the same instance's file, as the gateway template renders its VRFs
GW_BLADE_CONF = """frr version 8.5
frr defaults traditional
hostname bgw-1-gw-site1
!
vrf t51003
 vni 51003
exit-vrf
!
vrf t51004
 vni 51004
exit-vrf
!
router bgp 65010 vrf t51003
 address-family l2vpn evpn
  route-target import 65010:51003
  route-target export 65010:51003
 exit-address-family
exit
"""


class DerivedRouteTargets(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_the_blades_gateway_output_is_all_held(self):
        self.assertEqual(self.m.left_over(GW_BLADE, [], GW_BLADE_CONF), [])

    def test_without_the_file_they_count(self):
        self.assertEqual(len(self.m.left_over(GW_BLADE, [])), 4)

    def test_only_the_derived_value(self):
        def left(entry, conf=GW_BLADE_CONF):
            return self.m.left_over("\nLines To Add\n============\n" + entry, [], conf)
        # another value missing still fails
        self.assertEqual(left("router bgp 65010 vrf t51003\n address-family l2vpn evpn\n  route-target import 65010:9999\n"),
                         ["router bgp 65010 vrf t51003: address-family l2vpn evpn: route-target import 65010:9999"])
        # another VRF's VNI, a VRF the file gives no VNI, another AS: all count
        self.assertEqual(len(left("router bgp 65010 vrf t51003\n address-family l2vpn evpn\n  route-target export 65010:51004\n")), 1)
        self.assertEqual(len(left("router bgp 65010 vrf t51009\n address-family l2vpn evpn\n  route-target import 65010:51009\n")), 1)
        self.assertEqual(len(left("router bgp 65011 vrf t51003\n address-family l2vpn evpn\n  route-target import 65010:51003\n")), 1)
        # a 4-byte AS (FRR derives from its low 16 bits), the VPN address family, a deletion: all count
        conf4 = GW_BLADE_CONF.replace("65010", "4200000001")
        self.assertEqual(len(left("router bgp 4200000001 vrf t51003\n address-family l2vpn evpn\n  route-target import 4200000001:51003\n", conf4)), 1)
        self.assertEqual(len(left("router bgp 65010 vrf t51003\n address-family ipv4 unicast\n  route-target import 65010:51003\n")), 1)
        self.assertEqual(self.m.left_over("\nLines To Delete\n===============\nrouter bgp 65010 vrf t51003\n address-family l2vpn evpn\n"
                                          "  no route-target import 65010:51003\n", [], GW_BLADE_CONF),
                         ["router bgp 65010 vrf t51003: address-family l2vpn evpn: no route-target import 65010:51003"])

    def test_the_vnis_of_the_file(self):
        self.assertEqual(self.m.vrf_vnis(GW_BLADE_CONF), {"t51003": 51003, "t51004": 51004})
        self.assertEqual(self.m.vrf_vnis("vrf a\n vni 7 prefix-routes-only\nexit-vrf\nvrf b\nexit-vrf\n"), {"a": 7})

    def test_main_reads_the_file(self):
        d = tempfile.mkdtemp(prefix="shc-frr-check-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        conf, out, fake = (os.path.join(d, x) for x in ("frr.conf.shc", "out", "frr-reload.py"))
        open(conf, "w").write(GW_BLADE_CONF)
        open(out, "w").write(GW_BLADE)
        open(fake, "w").write("#!%s\nimport sys\nsys.stdout.write(open(%r).read())\n" % (sys.executable, out))
        os.chmod(fake, 0o755)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main(["--conf", conf, "--note", os.path.join(d, "n"), "--reload-script", fake])
        self.assertEqual(buf.getvalue().splitlines(), ["result=applied"])


class Parse(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_lines_that_never_match_by_design_are_left_out(self):
        self.assertEqual(self.m.left_over(GW_CLEAN, []), [])
        self.assertEqual(self.m.left_over(ROOT_LEFT, []), ["router isis CORE: segment-routing global-block 16000 23999"])

    def test_entries_carry_their_context_and_passwords_are_hidden(self):
        self.assertEqual(self.m.left_over(NESTED, []), sorted([
            "router bgp 65010: no neighbor 10.0.0.1 password <hidden>",
            "router bgp 65010: no neighbor 10.0.0.1 timers connect 10",
            "interface core0: ip router isis CORE",
            "router bgp 65010 vrf t51000: address-family ipv4 unicast: neighbor 192.0.2.99 activate",
            "vrf t51001",
            "vrf t51001: vni 51001",
            "router bgp 65010 vrf t51009",
        ]))

    def test_an_installations_pattern_matches_the_line_or_its_context(self):
        self.assertEqual(self.m.left_over(ROOT_LEFT, [r"^segment-routing global-block "]), [])
        self.assertEqual(self.m.left_over(ROOT_LEFT, [r"^router isis CORE: segment-routing"]), [])
        self.assertEqual(len(self.m.left_over(ROOT_LEFT, [r"^router bgp"])), 1)

    def test_no_output_is_nothing_left(self):
        self.assertEqual(self.m.left_over("", []), [])


class Note(unittest.TestCase):
    """main(): the result, and the note that tells a line FRR never takes from one it has not taken yet."""

    def setUp(self):
        self.m = load()
        self.d = tempfile.mkdtemp(prefix="shc-frr-check-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, True))
        self.note = os.path.join(self.d, "root.frr-unapplied")
        self.out = os.path.join(self.d, "out")
        self.fake = os.path.join(self.d, "frr-reload.py")
        self.answer(GW_CLEAN)

    def answer(self, text, rc=0):
        with open(self.out, "w") as f:
            f.write(text)
        with open(self.fake, "w") as f:
            f.write("#!%s\nimport sys\nsys.stdout.write(open(%r).read())\nsys.stderr.write('vtysh: no\\n')\n"
                    "sys.exit(%d)\n" % (sys.executable, self.out, rc))
        os.chmod(self.fake, 0o755)

    def check(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main(["--conf", "/etc/frr/frr.conf", "--note", self.note, "--reload-script", self.fake] + list(extra))
        self.assertEqual(rc, 0)
        return buf.getvalue().splitlines()

    def test_all_applied(self):
        self.assertEqual(self.check(), ["result=applied"])
        self.assertFalse(os.path.exists(self.note))

    def test_left_once_fails_left_again_is_accepted_then_cleared(self):
        self.answer(ROOT_LEFT)
        line = "line: router isis CORE: segment-routing global-block 16000 23999"
        self.assertEqual(self.check(), ["result=unapplied", line])
        self.assertEqual(self.check(), ["result=accepted", line])
        self.assertEqual(self.check(), ["result=accepted", line])
        self.answer(GW_CLEAN)
        self.assertEqual(self.check(), ["result=applied"])
        self.assertFalse(os.path.exists(self.note))
        self.answer(ROOT_LEFT)                     # after it was taken once, a new miss fails again
        self.assertEqual(self.check()[0], "result=unapplied")

    def test_a_new_line_on_top_of_a_known_one_fails(self):
        self.answer(ROOT_LEFT)
        self.check()
        self.answer(ROOT_LEFT + "router bgp 65010\n bgp router-id 192.0.2.4\n")
        out = self.check()
        self.assertEqual(out[0], "result=unapplied")
        self.assertEqual(len(out), 3)
        self.assertEqual(self.check()[0], "result=accepted")

    def test_fewer_lines_than_known_are_accepted(self):
        self.answer(ROOT_LEFT + "router bgp 65010\n bgp router-id 192.0.2.4\n")
        self.check()
        self.answer(ROOT_LEFT)
        self.assertEqual(self.check()[0], "result=accepted")

    def test_a_check_that_cannot_run_counts_as_a_line(self):
        self.answer("", rc=1)
        out = self.check()
        self.assertEqual(out, ["result=unapplied", "line: check: frr-reload.py --test failed (rc 1): vtysh: no"])
        self.assertEqual(self.check()[0], "result=accepted")

    def test_the_note_keeps_no_password(self):
        self.answer(NESTED)
        self.check()
        with open(self.note) as f:
            text = f.read()
        self.assertNotIn("s3cret", text)
        self.assertEqual(os.stat(self.note).st_mode & 0o777, 0o600)

    def test_the_pathspace_and_patterns_are_passed(self):
        with open(self.fake, "w") as f:
            f.write("#!%s\nimport sys\nopen(%r, 'w').write(' '.join(sys.argv[1:]))\n" % (sys.executable, self.out))
        self.check("--pathspace", "gw-site1", "--ignore", "^x$")
        with open(self.out) as f:
            self.assertEqual(f.read(), "--test --pathspace gw-site1 /etc/frr/frr.conf")

    def test_a_bad_pattern_is_refused(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e, \
                open(os.devnull, "w") as null, __import__("contextlib").redirect_stderr(null):
            self.m.main(["--conf", "c", "--note", self.note, "--ignore", "("])
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

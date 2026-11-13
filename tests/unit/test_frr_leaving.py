"""What the fabric job takes out of the root's FRR before the apply (files/shc_frr_leaving.py, node_root.yml "take out
of FRR what this run removes").

frr-reload.py 8.5 removes a BGP neighbour named by an interface (a network's IPv6 egress session, xe<t> in the
network's VRF, xo<t> in the egress VRF) by its defining line first; bgpd then refuses the "no" of any second line of
that neighbour at router level (its description: "% Create the peer-group first"), and the reload exits 1. Through
systemd's reload of the stock frr unit that exit stopped and restarted FRR: every session of the node reset (gate s6
F12, 18 restarts on one node). Two guards, both tested here:
- the job removes those neighbours itself before the reload (the helper's commands, from FRR's running configuration
  against the rendered file); a neighbour or instance the file keeps is never touched (removing one the reload adds
  back is a session reset);
- the template gives a neighbour named by an interface no other line at router level, so the reload also removes
  one without error where the helper did not act.
The model test renders the plans of a run before and after a removing operation, applies the helper's commands to
the first, and checks that what is left for the reload to delete holds no interface neighbour at all; on f98cbae1
(descriptions rendered, no helper) the same operations leave interface neighbours with a description to the reload.
What FRR itself does with the commands is the optional real-FRR test (test_frr_reload_real.py) and the gate."""
import copy
import io
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import ROOT  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_frr_leaving as L  # noqa: E402

RUNNING = """frr version 8.5.3
frr defaults traditional
hostname bgw-1
!
router bgp 65010
 bgp router-id 10.255.0.1
 neighbor PE peer-group
exit
!
router bgp 65010 vrf e10000
 bgp router-id 10.255.0.1
 neighbor xe10000 interface v6only remote-as external
 neighbor xe10000 description IPv6 egress of acme/core
 neighbor 169.254.128.4 remote-as 65010
 !
 address-family ipv6 unicast
  neighbor xe10000 activate
 exit-address-family
exit
!
router bgp 65010 vrf e10001
 bgp router-id 10.255.0.1
exit
!
router bgp 65010 vrf service
 bgp router-id 10.255.128.73
exit
!
router bgp 65011 vrf egress
 neighbor xo10000 interface v6only remote-as external
 neighbor xo10000 description IPv6 egress of acme/core
 neighbor xo10001 interface v6only remote-as external
 neighbor 2001:db8:237::2 remote-as 65002
 !
 address-family ipv6 unicast
  neighbor xo10000 activate
  neighbor xo10001 activate
 exit-address-family
exit
!
"""


def desired(*drop):
    """RUNNING without the lines that contain any of drop, and without the descriptions (the template's file)."""
    out = [l for l in RUNNING.splitlines() if "description" not in l and not any(d in l for d in drop)]
    return "\n".join(out) + "\n"


class Leaving(unittest.TestCase):
    def test_nothing_when_the_file_has_everything(self):
        self.assertEqual(L.leaving(RUNNING, desired(), "e[0-9]+", names=["service"]), [])

    def test_a_network_that_goes_away(self):
        # its VRF's instance goes whole (one command), the egress VRF keeps its instance and loses the network's xo
        want = desired("xe10000", "xo10000", "169.254.128.4")
        want = want.replace("router bgp 65010 vrf e10000\n bgp router-id 10.255.0.1\n !\n address-family ipv6 unicast\n"
                            " exit-address-family\nexit\n!\n", "")
        self.assertNotIn("vrf e10000", want)
        self.assertEqual(L.leaving(RUNNING, want, "e[0-9]+", names=["service"]),
                         [("", "no router bgp 65010 vrf e10000"),
                          ("router bgp 65011 vrf egress", "no neighbor xo10000 interface")])

    def test_a_network_that_keeps_its_vrf_and_loses_ipv6_egress(self):
        self.assertEqual(L.leaving(RUNNING, desired("xe10000", "xo10000"), "e[0-9]+"),
                         [("router bgp 65010 vrf e10000", "no neighbor xe10000 interface"),
                          ("router bgp 65011 vrf egress", "no neighbor xo10000 interface")])

    def test_kept_neighbours_in_another_order_are_never_touched(self):
        # the file lists the egress VRF's neighbours in another order: nothing to do
        want = desired().replace(" neighbor xo10000 interface v6only remote-as external\n neighbor xo10001 interface "
                                 "v6only remote-as external\n", " neighbor xo10001 interface v6only remote-as external\n"
                                 " neighbor xo10000 interface v6only remote-as external\n")
        self.assertNotEqual(want, desired())
        self.assertEqual(L.leaving(RUNNING, want, "e[0-9]+"), [])

    def test_an_instance_outside_the_pattern_is_left_to_the_reload(self):
        # IPv6 egress off for the fabric: the egress instance goes (by the reload, one command); only the network VRF's
        # xe is removed here; an instance the file lacks never gets neighbour commands
        want = re.sub(r"router bgp 65011 vrf egress\n.*?\nexit\n!\n", "", desired("xe10000"), flags=re.S)
        self.assertNotIn("vrf egress", want)
        self.assertEqual(L.leaving(RUNNING, want, "e[0-9]+", names=["service"]),
                         [("router bgp 65010 vrf e10000", "no neighbor xe10000 interface")])

    def test_the_service_vrf_by_name_and_never_the_default_instance(self):
        want = desired().replace("router bgp 65010 vrf service\n bgp router-id 10.255.128.73\nexit\n!\n", "")
        want = want.replace("router bgp 65010\n bgp router-id 10.255.0.1\n neighbor PE peer-group\nexit\n!\n", "")
        self.assertEqual(L.leaving(RUNNING, want, "e[0-9]+", names=["service"]),
                         [("", "no router bgp 65010 vrf service")])
        self.assertEqual(L.leaving(RUNNING, want, "e[0-9]+"), [])

    def test_neighbours_off(self):
        # the role default fabric_frr_remove_first false: the instances only, as before this round
        self.assertEqual(L.leaving(RUNNING, desired("xe10000", "xo10000"), "e[0-9]+", neighbours=False), [])

    def test_address_neighbours_are_left_to_the_reload(self):
        self.assertEqual(L.leaving(RUNNING, desired("169.254.128.4", "2001:db8:237::2"), "e[0-9]+"), [])


class Main(unittest.TestCase):
    """The script on the node: reads FRR, sends each removal in its own vtysh call, never fails the step."""

    def run_main(self, desired_text, active=True, running=RUNNING, answers=None, args=("--pattern", "e[0-9]+")):
        calls = []

        def run(cmd):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "-q"]:
                return (0 if active else 3), ""
            if cmd == ["vtysh", "-c", "show running-config"]:
                return (0, running) if running is not None else (1, "")
            return (answers or {}).get(cmd[-1], (0, ""))
        out = io.StringIO()
        self.assertEqual(L.main(list(args), desired_text, run=run, out=out), 0)
        return calls, out.getvalue().splitlines()

    def test_removals_one_call_each(self):
        calls, out = self.run_main(desired("xe10000", "xo10000"))
        self.assertEqual(calls[2:], [
            ["vtysh", "-c", "configure terminal", "-c", "router bgp 65010 vrf e10000", "-c", "no neighbor xe10000 interface"],
            ["vtysh", "-c", "configure terminal", "-c", "router bgp 65011 vrf egress", "-c", "no neighbor xo10000 interface"]])
        self.assertEqual(out, ["removed: router bgp 65010 vrf e10000: no neighbor xe10000 interface",
                               "removed: router bgp 65011 vrf egress: no neighbor xo10000 interface"])

    def test_inactive_or_unreadable_frr_is_left_alone(self):
        calls, out = self.run_main(desired("xe10000"), active=False)
        self.assertEqual(len(calls), 1)
        self.assertIn("not active", out[0])
        calls, out = self.run_main(desired("xe10000"), running=None)
        self.assertEqual(len(calls), 2)
        self.assertTrue(out[0].startswith("unreadable: "))

    def test_a_refusal_is_reported_and_left_to_the_reload(self):
        calls, out = self.run_main(desired("xe10000"), answers={"no neighbor xe10000 interface": (0, "% Create the peer-group first")})
        self.assertEqual(out, ["refused: router bgp 65010 vrf e10000: no neighbor xe10000 interface (% Create the peer-group first)"])

    def test_the_command_line_of_the_job(self):
        # node_root.yml passes --pattern e[0-9]+, the service VRF by --name and --no-neighbours when the role default is off
        calls, out = self.run_main(desired("xe10000", "xo10000"), args=("--pattern", "e[0-9]+", "--name", "service",
                                                                        "--no-neighbours"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(out, [])


try:
    import test_frr_templates as T
    import test_service_vrf as S
except ImportError:  # pragma: no cover - the fixtures need yaml and jinja2
    T = S = None


def router_level(text):
    """{(instance, neighbour named by an interface): [its lines at router level]} of a rendered root file."""
    out, inst = {}, None
    ifs = {}
    for line in text.splitlines():
        if not line.startswith(" "):
            inst = line if line.startswith("router bgp ") else None
            continue
        m = re.match(r"^ neighbor (\S+) interface\b", line)
        if inst and m:
            ifs.setdefault(inst, set()).add(m.group(1))
    for line in text.splitlines():
        if not line.startswith(" "):
            inst = line if line.startswith("router bgp ") else None
            continue
        w = line.split()
        if inst and line.startswith(" neighbor ") and not line.startswith("  ") and w[1] in ifs.get(inst, ()):
            out.setdefault((inst, w[1]), []).append(line)
    return out


def apply(text, commands):
    """The running configuration after the helper's commands: an instance removed whole, a neighbour removed with
    every line that names it in its instance (what bgpd does)."""
    blocks = re.split(r"(?m)^(?=router bgp )", text)
    out = []
    for b in blocks:
        head = b.splitlines()[0] if b else ""
        if ("", "no " + head) in commands:
            b = re.sub(r"(?s)^.*?\nexit\n", "", b, count=1)
        for ctx, cmd in commands:
            if ctx == head and cmd.startswith("no neighbor "):
                name = cmd.split()[2]
                b = "".join(l for l in b.splitlines(True) if not re.match(r"^\s+neighbor %s " % re.escape(name), l))
        out.append(b)
    return "".join(out)


@unittest.skipIf(S is None, "the template fixtures are not importable")
class RenderedPlans(unittest.TestCase):
    """The rendered root file of real plans (the planner and the template, test_service_vrf's fixtures)."""

    def render(self, v6egress=True, nets=None, core=None):
        real = S.realization(core or S.CORE6, v6egress)
        egp, att, vrfs, svc = S.pipeline(S.F, real, True, nets=nets if nets is not None else S.NETS)
        return T.render(T.RT, S.template_vars(real, egp, att, vrfs, svc, "bgw-1", True))

    def test_an_interface_neighbour_has_one_line_at_router_level(self):
        # DESIGN A.2/A.4 O2: frr-reload.py 8.5 removes the defining line first and then fails on any other
        seen = 0
        for core in (S.CORE6, S.CORE_SRV6):
            for text in (self.render(core=core), self.render(core=core, nets=[S.hn("core", ["10.60.0.0/16"])])):
                for key, lines in router_level(text).items():
                    seen += 1
                    self.assertEqual(len(lines), 1, (key, lines))
                    self.assertRegex(lines[0], r"^ neighbor \S+ interface v6only remote-as external$")
        self.assertGreaterEqual(seen, 4)

    def model(self, before, after, helper=True):
        """The helper's commands, and what the reload is left to delete of the neighbours named by an interface:
        {(instance, neighbour): its router-level lines} in FRR's running configuration (before, after the commands)
        that the file (after) lacks, in the instances the file keeps (an instance the file lacks goes by one command)."""
        cmds = L.leaving(before, after, "e[0-9]+", names=["service"]) if helper else []
        run = apply(before, cmds)
        kept = set(re.findall(r"(?m)^router bgp .*$", after))
        want = router_level(after)
        left = {k: ls for k, ls in router_level(run).items() if k not in want and k[0] in kept}
        return cmds, left

    def check(self, before, after):
        cmds, left = self.model(before, after)
        self.assertEqual(left, {})
        # the template's guard alone (the helper did not act: FRR not active at that step, the role default off): the
        # reload deletes each such neighbour by its one line, which frr-reload.py 8.5 does without error
        _, alone = self.model(before, after, helper=False)
        self.assertTrue(alone)
        self.assertTrue(all(len(ls) == 1 for ls in alone.values()), alone)
        return cmds

    @unittest.skipIf(S is None or S.git_show("eda/rulebooks/roles/hybridfabric_provision/templates/frr-root.conf.j2") is None,
                     "the release is not readable from Git")
    def test_the_release_before_left_them_with_a_description(self):
        # f98cbae1 (the live code): no helper, a description on each: R1 leaves the reload an interface neighbour
        # with two lines at router level, the F12 reload failure
        import subprocess
        old_t = subprocess.run(["git", "-C", ROOT, "show", "f98cbae1:eda/rulebooks/roles/hybridfabric_provision/"
                                "templates/frr-root.conf.j2"], capture_output=True, text=True).stdout
        if not old_t:
            self.skipTest("f98cbae1 not in this checkout")
        real = S.realization(S.CORE6, True)

        def old(nets):
            egp, att, vrfs, svc = S.pipeline(S.F, real, True, nets=nets)
            return T.render_text(old_t, S.template_vars(real, egp, att, vrfs, svc, "bgw-1", True))
        before, after = old(S.NETS), old([n for n in S.NETS if n["metadata"]["name"] != "core"])
        _, left = self.model(before, after, helper=False)
        self.assertEqual({k: len(v) for k, v in left.items()}, {("router bgp 65011 vrf egress", "xo10000"): 2})

    def test_network_deleted_r1(self):
        before = self.render()
        after = self.render(nets=[n for n in S.NETS if n["metadata"]["name"] != "core"])
        cmds = self.check(before, after)
        self.assertIn(("router bgp 65011 vrf egress", "no neighbor xo10000 interface"), cmds)
        self.assertIn(("", "no router bgp 65010 vrf e10000"), cmds)

    def test_ipv6_egress_off_for_the_fabric_r9(self):
        before = self.render()
        after = self.render(v6egress=False)
        cmds = self.check(before, after)
        self.assertEqual(cmds, [("router bgp 65010 vrf e10000", "no neighbor xe10000 interface")])

    def test_network_turns_internet_egress_off(self):
        # core has nothing else on the node: its VRF goes (one command) and the egress VRF, which stays, loses xo
        nets = copy.deepcopy(S.NETS)
        before = self.render(nets=nets)
        for n in nets:
            if n["metadata"]["name"] == "core":
                n["spec"]["egress"]["internet"] = False
        after = self.render(nets=nets)
        cmds = self.check(before, after)
        self.assertIn(("router bgp 65011 vrf egress", "no neighbor xo10000 interface"), cmds)

    def test_a_rerun_takes_nothing_out(self):
        text = self.render()
        self.assertEqual(L.leaving(text, text, "e[0-9]+", names=["service"]), [])


if __name__ == "__main__":
    unittest.main()

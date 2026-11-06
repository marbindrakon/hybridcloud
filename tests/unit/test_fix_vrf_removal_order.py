"""A gateway VRF that a job removes leaves FRR before the apply deletes its devices (node_gateway.yml, "take the VRFs
this run removes out of FRR before the apply deletes their devices"), on both transports. Deleted first, FRR 8.5.3
zebra could crash withdrawing the site's remote EVPN routes of that VRF (a neighbour entry on a device already gone);
watchfrr then restarted the whole instance. The step sends, for every VRF device t<n> of the namespace that the
configuration no longer lists, `no vni` in its vrf stanza and then `no router bgp … vrf` (FRR refuses the second
while the VRF still has its layer 3 VNI), then waits for zebra. Nothing at all on a first run (no namespace), with
nothing removed (no FRR command, so no reload of any kind), or while the instance is not active. A job that adds and
removes VRFs at once takes only the removed ones out (the added ones get their devices from the apply first, as
before). The step comes after the apply configuration is written and before the apply; the reload and the step that
removes the inactive VRF from zebra stay after the apply.

The tests run the rendered script in bash with ip, systemctl, vtysh and sleep replaced by stubs that answer from
files and log what they were asked."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT, load  # noqa: E402
from test_gateway_readiness import render, shell_of  # noqa: E402

GW = os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudgateway_provision", "tasks", "node_gateway.yml")
P = "{{ bgw_node.name }}: "
T = P + "take the VRFs this run removes out of FRR before the apply deletes their devices"

# the running configuration of a gateway with two networks (the template's form)
RUNNING = textwrap.dedent("""\
    frr version 8.5
    vrf t51000
     vni 51000
    exit-vrf
    !
    vrf t51003
     vni 51003
    exit-vrf
    !
    router bgp 65010
    !
    router bgp 65010 vrf t51000
     sid vpn per-vrf export auto
    exit
    !
    router bgp 65010 vrf t51003
     sid vpn per-vrf export auto
    exit
    """)
VRFS = "t51000           UP             \nt51003           UP             \n"

STUBS = {
    "ip": """#!/bin/bash
echo "ip $*" >> "$STUB/calls"
case "$*" in
  "netns list") cat "$STUB/netns" 2>/dev/null ;;
  *"ip -br link show type vrf") cat "$STUB/vrfs" ;;
  *) exit 1 ;;
esac
""",
    "systemctl": """#!/bin/bash
echo "systemctl $*" >> "$STUB/calls"
[ -e "$STUB/active" ]
""",
    "vtysh": """#!/bin/bash
echo "vtysh $*" >> "$STUB/calls"
case "$*" in
  *"-c show running-config"*) [ -e "$STUB/running" ] || exit 1; cat "$STUB/running" ;;
  *"-c configure terminal"*) cat "$STUB/conf-out" 2>/dev/null; exit 0 ;;
  *) exit 1 ;;
esac
""",
    "sleep": """#!/bin/bash
echo "sleep $*" >> "$STUB/calls"
""",
}


def gw_vars(*vrfs):
    return {"bgw_node": {"name": "bgw-1"}, "fab_name": "site-a", "cg_vrfs": [{"vrf": v} for v in vrfs]}


class Node:
    def __init__(self, test, *want):
        self.d = tempfile.mkdtemp(prefix="shc-vrf-out-")
        test.addCleanup(shutil.rmtree, self.d, True)
        self.bin, self.stub = os.path.join(self.d, "bin"), os.path.join(self.d, "stub")
        os.makedirs(self.bin)
        os.makedirs(self.stub)
        for name, text in STUBS.items():
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.script = render(shell_of(GW, T), **gw_vars(*want))
        for name, text in (("netns", "gw-site-a (id: 3)\n"), ("active", ""), ("vrfs", VRFS), ("running", RUNNING)):
            self.put(name, text)

    def put(self, name, text):
        with open(os.path.join(self.stub, name), "w") as f:
            f.write(text)

    def drop(self, name):
        os.remove(os.path.join(self.stub, name))

    def run(self):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], STUB=self.stub)
        p = subprocess.run(["bash", "-s"], input=self.script, capture_output=True, text=True, env=env, timeout=60)
        try:
            with open(os.path.join(self.stub, "calls")) as f:
                calls = f.read().splitlines()
        except FileNotFoundError:
            calls = []
        return p.returncode, p.stdout, p.stderr, calls


def configured(calls):
    return [c for c in calls if "configure terminal" in c]


class TakeOutFirst(unittest.TestCase):
    def test_the_removed_vrf_leaves_frr(self):
        rc, out, err, calls = Node(self, "t51000").run()
        self.assertEqual((rc, out.splitlines(), err), (0, ["out=t51003"], ""))
        self.assertEqual(configured(calls), [
            "vtysh -N gw-site-a -c configure terminal -c vrf t51003 -c no vni 51003 -c exit-vrf "
            "-c no router bgp 65010 vrf t51003 -c end"])
        self.assertIn("sleep 3", calls)
        self.assertIn("systemctl -q is-active shc-gw@site-a.service", calls)
        self.assertIn("ip netns exec gw-site-a ip -br link show type vrf", calls)

    def test_nothing_removed_sends_nothing(self):
        for want in (("t51000", "t51003"), ("t51000", "t51003", "t51009")):
            with self.subTest(want=want):
                rc, out, _, calls = Node(self, *want).run()
                self.assertEqual((rc, out.splitlines()), (0, ["out="]))
                self.assertEqual([c for c in calls if c.startswith(("vtysh", "sleep"))], [])

    def test_adds_and_removes_at_once(self):
        # t51009 is new (no device yet: the apply creates it, the reload configures it), t51003 goes
        rc, out, _, calls = Node(self, "t51000", "t51009").run()
        self.assertEqual(out.splitlines(), ["out=t51003"])
        self.assertEqual(len(configured(calls)), 1)
        self.assertNotIn("t51009", configured(calls)[0])
        self.assertNotIn("t51000", configured(calls)[0])

    def test_several_removed(self):
        n = Node(self)
        rc, out, _, calls = n.run()
        self.assertEqual(out.splitlines(), ["out=t51000 t51003"])
        self.assertEqual(configured(calls), [
            "vtysh -N gw-site-a -c configure terminal -c vrf t51000 -c no vni 51000 -c exit-vrf "
            "-c no router bgp 65010 vrf t51000 -c vrf t51003 -c no vni 51003 -c exit-vrf "
            "-c no router bgp 65010 vrf t51003 -c end"])

    def test_first_run_or_inactive_instance(self):
        n = Node(self, "t51000")
        n.put("netns", "gw-site-b (id: 4)\n")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines(), calls), (0, ["out="], ["ip netns list"]))
        n = Node(self, "t51000")
        n.drop("active")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines()), (0, ["out="]))
        self.assertEqual([c for c in calls if c.startswith(("vtysh", "sleep")) or "link show" in c], [])

    def test_already_out_of_frr(self):
        n = Node(self, "t51000")
        n.put("running", RUNNING.split("vrf t51003\n vni 51003\nexit-vrf\n!\n")[0]
              + RUNNING.split("router bgp 65010 vrf t51003")[0].split("exit-vrf\n!\n", 2)[-1])
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines(), configured(calls)), (0, ["out="], []))

    def test_unreadable_or_refused_goes_on(self):
        n = Node(self, "t51000")
        n.drop("running")
        rc, out, err, calls = n.run()
        self.assertEqual((rc, out.splitlines(), configured(calls)), (0, ["out="], []))
        self.assertIn("unreadable", err)
        n = Node(self, "t51000")
        n.put("conf-out", "% Please unconfigure l3vni 51003\n")
        rc, out, err, _ = n.run()
        self.assertEqual((rc, out.splitlines()), (0, ["out=t51003"]))
        self.assertIn("FRR refused part of it: % Please unconfigure l3vni 51003", err)


class Order(unittest.TestCase):
    def test_before_the_apply_after_the_configuration(self):
        names = [t.get("name") for t in load(GW)]
        i = names.index(T)
        self.assertLess(names.index(P + "gateway instance config"), i)
        self.assertLess(i, names.index(P + "apply the gateway instance (namespace, site leg, core leg)"))
        # the render, the reload and the removal of the inactive VRF stay after the apply
        for later in ("FRR configuration", "reload the FRR configuration",
                      "remove from FRR the VRFs its configuration no longer has"):
            self.assertLess(names.index(P + "apply the gateway instance (namespace, site leg, core leg)"),
                            names.index(P + later), later)


if __name__ == "__main__":
    unittest.main()

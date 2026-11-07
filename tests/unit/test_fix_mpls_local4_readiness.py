"""Readiness requires the IPv4 local rule behind the VRFs' l3mdev rule on both transports, decided by the facts.

The apply moves the IPv4 rule "lookup local" from 0 to 32765 on both transports, in the root namespace and in every
gateway namespace (also in a namespace without a VRF: its local4 last runs unconditionally), so a packet from a VRF
never reaches the node's default-VRF addresses. The kernel adds the l3mdev rule only with a namespace's first VRF
device. The checks therefore decide:
- an l3mdev rule present: a lookup local rule exists and every one lies behind it;
- no l3mdev rule and no VRF device: exactly one lookup local rule, at 32765;
- no l3mdev rule while VRF devices exist: fail, naming them.
On mpls the wait scripts of the root (node_root.yml "wait for BGP and IS-IS") and of each gateway ("wait for the core
...") hold a shell block after the MPLS check; on srv6 the shared SRv6 check (inline Python) decides the same way.

The rule listings: tests/fixtures/srv6-replica-p4/ (ip4-rule-default.txt is a replica namespace's real
`ip -4 rule show` with the kernel's defaults; ip4-rule-srv6.txt the same with the rule at 32765); the VRF device
listing (`ip -br link show type vrf`) is hand-built."""
import copy
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT  # noqa: E402
from test_gateway_readiness import render, shell_of, snippet  # noqa: E402
import test_srv6_tasks as st  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "srv6-replica-p4")


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


DEFAULT, MOVED = fixture("ip4-rule-default.txt"), fixture("ip4-rule-srv6.txt")
L3MDEV = "1000:\tfrom all lookup [l3mdev-table]\n"
NO_VRF_MOVED = MOVED.replace(L3MDEV, "")
NO_VRF_DEFAULT = DEFAULT.replace(L3MDEV, "")
BOTH = "0:\tfrom all lookup local\n" + MOVED
VRFS = "t51000           UP             \nt51003           UP             \n"

# (rules, VRF devices, passes, message on failure)
CASES = (
    (MOVED, VRFS, True, None),
    (DEFAULT, VRFS, False, "the IPv4 rule lookup local is"),
    (BOTH, VRFS, False, "the IPv4 rule lookup local is"),
    (MOVED.replace("32765:\tfrom all lookup local\n", ""), VRFS, False, "the IPv4 rule lookup local is"),
    (NO_VRF_MOVED, "", True, None),
    (NO_VRF_DEFAULT, "", False, "the IPv4 rule lookup local is"),
    (NO_VRF_MOVED.replace("32765:", "0:\tfrom all lookup local\n32765:"), "", False, "the IPv4 rule lookup local is"),
    (NO_VRF_MOVED.replace("32765:\tfrom all lookup local\n", ""), "", False, "the IPv4 rule lookup local is"),
    (NO_VRF_MOVED, VRFS, False, "no IPv4 l3mdev rule (VRF devices: t51000"),
)


def block(script):
    """The check's lines (from its comment to its last failure line), from a rendered mpls wait script."""
    lines = script.splitlines()
    i = next(n for n, l in enumerate(lines) if l.strip().startswith("# the IPv4 local rule after the l3mdev rule"))
    j = next(n for n in range(i, len(lines)) if lines[n].strip().startswith('[ "$rc" = 0 ] || {'))
    return "\n".join(l.strip() for l in lines[i:j + 1])


STUB = r'''
ip() {
  case "$*" in
    *"-br link show type vrf") printf '%s' "$VRFS" ;;
    *"-4 rule show") printf '%s' "$RULES" ;;
    *) return 1 ;;
  esac
}
'''


def judge(check, rules, vrfs):
    p = subprocess.run(["bash", "-s"], input="set -e\n" + STUB + check + "\necho passed\n", capture_output=True,
                       text=True, env=dict(os.environ, RULES=rules, VRFS=vrfs))
    return p.returncode, p.stdout, p.stderr


class MplsCheck(unittest.TestCase):
    def scripts(self):
        gw = render(shell_of(st.GW, st.T_GW_WAIT), **dict(copy.deepcopy(st.GW_VARS), fab_node_transport="mpls"))
        rt = render(shell_of(st.RT, st.T_RT_WAIT), **dict(copy.deepcopy(st.ROOT_VARS), fab_node_transport="mpls"))
        return (("gateway", gw, "ip -n gw-iaas1"), ("root", rt, "ip"))

    def test_the_facts_decide(self):
        for who, script, ipx in self.scripts():
            check = block(script)
            self.assertIn("v=$(%s -br link show type vrf)" % ipx, check)
            self.assertIn("rc=0; %s -4 rule show | awk" % ipx, check)
            for rules, vrfs, ok, msg in CASES:
                with self.subTest(who=who, rules=rules, vrfs=vrfs):
                    rc, out, err = judge(check, rules, vrfs)
                    if ok:
                        self.assertEqual((rc, out.strip()), (0, "passed"), err)
                    else:
                        self.assertEqual((rc, out), (1, ""))
                        self.assertIn(msg, err)
        _, _, err = judge(block(self.scripts()[0][1]), NO_VRF_MOVED, VRFS)
        self.assertIn("no IPv4 l3mdev rule (VRF devices: t51000 t51003)", err)

    def test_after_the_mpls_check_and_only_on_mpls(self):
        for who, script, _ in self.scripts():
            mpls = "net.mpls.conf.core0.input" if who == "gateway" else "net.mpls.platform_labels"
            self.assertTrue(script.lstrip().startswith("set -e"))
            self.assertLess(script.index(mpls), script.index("-4 rule show | awk"))
        for path, name, v in ((st.GW, st.T_GW_WAIT, st.GW_VARS), (st.RT, st.T_RT_WAIT, st.ROOT_VARS)):
            self.assertNotIn("-4 rule show | awk", render(shell_of(path, name), **st.srv6(v)))

    def test_bash_syntax(self):
        for _, script, _ in self.scripts():
            p = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


class Srv6Check(unittest.TestCase):
    """The shared SRv6 check (root and gateway: the same code) on the same cases."""

    def run_check(self, who, rules, vrfs):
        cc = st.Srv6CoreCheck()
        if who == "gateway":
            code, args = snippet(render(shell_of(st.GW, st.T_GW_WAIT), **st.srv6(st.GW_VARS)))
            a = cc.gw_ok()
        else:
            code, args = snippet(render(shell_of(st.RT, st.T_RT_WAIT), **st.srv6(st.ROOT_VARS)))
            a = st.core_answers([], [], own=[st.RT_PE["locator"]], running=cc.root_running(),
                                guard=st.guard_table("br-core"))
        a.pop("ip -4 rule show")
        first = {"ip -4 rule show": rules}
        if vrfs is not None:
            first["-br link show type vrf"] = vrfs
        return st.run_code(code, args, dict(first, **a))

    def test_the_facts_decide(self):
        for who in ("gateway", "root"):
            for rules, vrfs, ok, msg in CASES:
                with self.subTest(who=who, rules=rules, vrfs=vrfs):
                    rc, _, err, calls = self.run_check(who, rules, vrfs)
                    if ok:
                        self.assertEqual(rc, 0, err)
                        self.assertNotIn("lookup local", err)
                        self.assertNotIn("l3mdev", err)
                    else:
                        self.assertEqual(rc, 1)
                        self.assertIn(msg, err)
                    # the VRF devices are read only when the l3mdev rule is missing
                    self.assertEqual(any("vrf" in c and "link" in c for c in calls), "l3mdev-table" not in rules)

    def test_unreadable_vrf_devices_count_as_present(self):
        rc, _, err, _ = self.run_check("root", NO_VRF_MOVED, None)
        self.assertEqual(rc, 1)
        self.assertIn("no IPv4 l3mdev rule (VRF devices: unreadable)", err)

    def test_the_gateway_reads_its_namespace(self):
        _, _, _, calls = self.run_check("gateway", NO_VRF_MOVED, "")
        self.assertIn(["ip", "netns", "exec", "gw-iaas1", "ip", "-br", "link", "show", "type", "vrf"], calls)


if __name__ == "__main__":
    unittest.main()

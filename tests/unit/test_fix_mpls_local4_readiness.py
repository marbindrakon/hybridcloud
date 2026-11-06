"""On the MPLS core too, readiness requires the IPv4 local rule behind the VRFs' l3mdev rule.

The apply moves the IPv4 rule "lookup local" from 0 to 32765 on both transports, in the root namespace and in every
gateway namespace, so a packet from a VRF never reaches the node's default-VRF addresses. On srv6 the SRv6 check
(inline Python, tests in test_srv6_tasks) requires it; on mpls the wait scripts of the root (node_root.yml "wait for
BGP and IS-IS") and of each gateway ("wait for the core ...") have a one-line awk check after the MPLS check. Without
an l3mdev rule (no VRF was ever created in the namespace) the local rule must be behind 1000, the preference the
kernel gives that rule.

The rule listings: tests/fixtures/srv6-replica-p4/ (ip4-rule-default.txt is a replica namespace's real
`ip -4 rule show` with the kernel's defaults; ip4-rule-srv6.txt the same with the rule at 32765)."""
import copy
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT  # noqa: E402
from test_gateway_readiness import render, shell_of  # noqa: E402
import test_srv6_tasks as st  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "srv6-replica-p4")


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


DEFAULT, MOVED = fixture("ip4-rule-default.txt"), fixture("ip4-rule-srv6.txt")
NO_VRF_MOVED = MOVED.replace("1000:\tfrom all lookup [l3mdev-table]\n", "")
NO_VRF_DEFAULT = DEFAULT.replace("1000:\tfrom all lookup [l3mdev-table]\n", "")
BOTH = "0:\tfrom all lookup local\n" + MOVED
FOREIGN_FIRST = "0:\tfrom all lookup local\n100:\tfrom all lookup 7\n" + MOVED.split("\n", 1)[1]


def check_line(script, netns):
    """The check's command (from `ip ... rule show | awk ...` to its `|| { ... }`), from a rendered wait script."""
    lines = script.splitlines()
    i = next(n for n, l in enumerate(lines) if "-4 rule show | awk" in l)
    want = "ip -n %s -4 rule show | awk" % netns if netns else "ip -4 rule show | awk"
    assert lines[i].strip().startswith(want), lines[i]
    assert lines[i + 1].strip().startswith("|| { echo "), lines[i + 1]
    return lines[i].strip().rstrip("\\").strip(), lines[i + 1].strip()


def judge(awk_cmd, rules):
    """The exit code of the check's awk on a rule listing."""
    awk = awk_cmd.split("| ", 1)[1]
    p = subprocess.run(["bash", "-s"], input="printf '%%s' \"$RULES\" | %s\n" % awk, capture_output=True, text=True,
                       env=dict(os.environ, RULES=rules))
    return p.returncode


class MplsLocal4Check(unittest.TestCase):
    def scripts(self):
        gw = render(shell_of(st.GW, st.T_GW_WAIT), **dict(copy.deepcopy(st.GW_VARS), fab_node_transport="mpls"))
        rt = render(shell_of(st.RT, st.T_RT_WAIT), **dict(copy.deepcopy(st.ROOT_VARS), fab_node_transport="mpls"))
        return (("gateway", gw, "gw-iaas1"), ("root", rt, ""))

    def test_the_rule_order_decides(self):
        for who, script, netns in self.scripts():
            cmd, _ = check_line(script, netns)
            for rules, ok in ((MOVED, True), (DEFAULT, False), (BOTH, False), (NO_VRF_MOVED, True),
                              (NO_VRF_DEFAULT, False), (FOREIGN_FIRST, False), ("", False),
                              (MOVED.replace("32765:\tfrom all lookup local\n", ""), False)):
                with self.subTest(who=who, rules=rules):
                    self.assertEqual(judge(cmd, rules) == 0, ok)

    def test_a_failure_says_why_and_fails_the_wait(self):
        for who, script, netns in self.scripts():
            _, msg = check_line(script, netns)
            self.assertIn("the IPv4 rule lookup local is not after the l3mdev rule of the VRFs", msg)
            self.assertTrue(msg.endswith(">&2; exit 1; }"), msg)
            self.assertNotIn("'", msg)
            # under set -e, after the MPLS check
            self.assertTrue(script.lstrip().startswith("set -e"))
            mpls = "net.mpls.conf.core0.input" if who == "gateway" else "net.mpls.platform_labels"
            self.assertLess(script.index(mpls), script.index("-4 rule show | awk"))

    def test_srv6_keeps_its_own_check(self):
        gw = render(shell_of(st.GW, st.T_GW_WAIT), **st.srv6(st.GW_VARS))
        rt = render(shell_of(st.RT, st.T_RT_WAIT), **st.srv6(st.ROOT_VARS))
        for script in (gw, rt):
            self.assertNotIn("-4 rule show | awk", script)
            self.assertIn('"ip", "-4", "rule", "show"', script)

    def test_bash_syntax(self):
        for _, script, _ in self.scripts():
            p = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


@unittest.skipUnless(st.HAVE_ANSIBLE, "ansible-playbook not installed")
class AnsibleRender(unittest.TestCase):
    def test_render_is_ansibles(self):
        for path, name, v in ((st.GW, st.T_GW_WAIT, st.GW_VARS), (st.RT, st.T_RT_WAIT, st.ROOT_VARS)):
            v = dict(copy.deepcopy(v), fab_node_transport="mpls")
            sh = shell_of(path, name)
            got = st.run_play([{"ansible.builtin.set_fact": {"rendered": sh}}, st.write_out("rendered")], v)
            self.assertEqual(render(sh, **v).rstrip("\n"), got.rstrip("\n"))


if __name__ == "__main__":
    unittest.main()

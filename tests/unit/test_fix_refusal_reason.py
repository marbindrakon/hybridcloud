"""The apply's refusals reach the status message on both transports.

shc-fabric-apply prints what it refuses on either transport as "shc-fabric-apply: <reason>" (a foreign IPv4 policy
rule in front of the local table, a VRF that is not the fabric's on a table it will use, a common step that failed)
and what only the SRv6 core refuses as "transport srv6: <reason>", both with exit 4. The jobs take those lines
into the failure and the status message: from the root unit's journal when its start failed (node_root.yml "the
root instance runs, or why not" and "stop when the root instance did not start"), and from the step's stderr when
an apply run directly fails (the node-loop rescue of both provision roles). The run helpers are those of
test_srv6_tasks (RootUnitStartReason, DirectApplyReason), used here on the common prefix."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, by_name, load  # noqa: E402
import test_srv6_tasks as st  # noqa: E402

FOREIGN = "shc-fabric-apply: IPv4 policy rules between 0 and 32765 would come before the local table: 100: from all lookup 7"
UNIT_MSG = {"failed": False, "msg": "Unable to start service shc-fabric-apply@root.service"}
MATCH = "^(transport srv6|shc-fabric-apply): "


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RootUnitStart(unittest.TestCase):
    def steps(self, journal):
        return st.RootUnitStartReason("test_active_goes_on").run_steps("failed", journal, UNIT_MSG)[0]

    def test_a_common_refusal_names_the_reason(self):
        out = self.steps("Starting shc fabric instance root...\n" + FOREIGN + "\n"
                         "shc-fabric-apply@root.service: Main process exited, code=exited, status=4/NOPERMISSION\n")
        self.assertIn("did not start: " + FOREIGN, out)

    def test_both_kinds_in_order(self):
        out = self.steps(FOREIGN + "\ntransport srv6: net.ipv4.conf.all.rp_filter is 1; the root's network VRFs need it 0\n")
        self.assertIn("did not start: " + FOREIGN + "; transport srv6: net.ipv4.conf.all.rp_filter is 1", out)

    def test_systemd_lines_that_name_the_unit_are_no_reason(self):
        # systemd's own lines start "shc-fabric-apply@root.service: ", not "shc-fabric-apply: "
        out = self.steps("shc-fabric-apply@root.service: Main process exited, code=exited, status=1/FAILURE\n")
        self.assertIn("did not start: Unable to start service shc-fabric-apply@root.service", out)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class DirectApply(unittest.TestCase):
    def test_a_common_refusal_reaches_the_message(self):
        helper = st.DirectApplyReason("test_refusal_reaches_the_message")
        for path in (st.HF_MAIN, st.CG_MAIN):
            with self.subTest(path=path):
                out = helper.run_failing(path, "echo '%s' >&2; echo 'other noise' >&2; exit 4" % FOREIGN, before="")
                self.assertEqual(out, "bgw-1: apply the root instance: non-zero return code: " + FOREIGN)
                # a timed-out step still keeps the lease keeper's message
                self.assertEqual(helper.run_failing(path, "echo '%s' >&2; sleep 5" % FOREIGN, before="", timeout=1), "")


class OneMatchEverywhere(unittest.TestCase):
    def test_the_capture_sites_share_the_prefixes(self):
        rt = load(st.RT)
        sh = by_name(rt, st.P + "the root instance runs, or why not")["ansible.builtin.shell"]
        self.assertIn("grep -E '%s'" % MATCH, sh)
        self.assertIn("select('match', '%s')" % MATCH,
                      by_name(rt, st.P + "stop when the root instance did not start")["vars"]["_why"])
        for path, loop in ((st.HF_MAIN, "Configure the nodes, one at a time"),
                           (st.CG_MAIN, "Configure the gateway on the nodes, one at a time")):
            rescue = by_name(load(path), loop)["rescue"][0]
            self.assertIn("select('match', '%s')" % MATCH, rescue["vars"]["_why"])


if __name__ == "__main__":
    unittest.main()

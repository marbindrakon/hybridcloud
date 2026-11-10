"""The fabric job's IPv6 egress outage report: once the outage lasted sessionDownAlertMinutes, then at most every
health.eventRepeatMinutes (reportedAt), as the fabric health check reports it; and the networks are woken when the
set of nodes without upstream changes, not only when degraded flips."""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

MAIN = os.path.join(ROOT, "eda/rulebooks/roles/hybridfabric_provision/tasks/main.yml")


def ago(minutes):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def node(name, healthy):
    return {"name": name, "egressIpv6": {"address": "2001:db8::13/64", "healthy": healthy, "default": healthy,
                                         "table": True, "upstream": []}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Report(unittest.TestCase):
    def run_tasks(self, results, prev):
        tasks = [by_name(load(MAIN), "Set the IPv6 egress status"),
                 by_name(load(MAIN), "Decide whether the IPv6 egress upstream outage is reported now")]
        return run_play(tasks + [write_out("{'report': hf_egress6_report, 'status': hf_egress6_status}")],
                        {"hf_egress": {"ipv6": True}, "hf_node_results": results,
                         "fab_st": {"realization": {"egress": {"ipv6": prev}}},
                         "fab_real": {"egress": {"ipv6": {"sessionDownAlertMinutes": 5}},
                                      "health": {"eventRepeatMinutes": 60}}})

    def test_once_then_repeated(self):
        down = [node("bgw-1", True), node("bgw-2", False)]
        out = self.run_tasks(down, {"degraded": True, "degradedSince": ago(10), "degradedNodes": ["bgw-2"]})
        self.assertTrue(out["report"])
        self.assertTrue(out["status"]["reportedAt"])
        out = self.run_tasks(down, {"degraded": True, "degradedSince": ago(20), "degradedNodes": ["bgw-2"],
                                    "reportedAt": ago(10)})
        self.assertFalse(out["report"])
        self.assertEqual(out["status"]["reportedAt"][:16], ago(10)[:16])
        out = self.run_tasks(down, {"degraded": True, "degradedSince": ago(90), "degradedNodes": ["bgw-2"],
                                    "reportedAt": ago(70)})
        self.assertTrue(out["report"])

    def test_not_before_the_minutes_and_cleared_on_recovery(self):
        out = self.run_tasks([node("bgw-1", False)], {})
        self.assertFalse(out["report"])
        out = self.run_tasks([node("bgw-1", True)], {"degraded": True, "degradedSince": ago(30), "reportedAt": ago(20)})
        self.assertFalse(out["report"])
        self.assertIsNone(out["status"]["reportedAt"])

    def test_networks_woken_when_the_down_set_changes(self):
        t = by_name(load(MAIN), "Wake the networks whose egress changed")
        expr = {"name": "flip", "ansible.builtin.set_fact": {"flip": "{{ _degraded_flip }}"}, "vars": t["vars"]}
        for now, prev, want in ((["bgw-2"], {"degraded": True, "degradedNodes": ["bgw-2"]}, False),
                                (["bgw-1", "bgw-2"], {"degraded": True, "degradedNodes": ["bgw-2"]}, True),
                                ([], {"degraded": True, "degradedNodes": ["bgw-2"]}, True)):
            got = run_play([expr, write_out("flip")],
                           {"hf_egress6_status": {"degraded": bool(now), "degradedNodes": now},
                            "fab_st": {"realization": {"egress": {"ipv6": prev}}}, "hf_egress": {"networks": {}},
                            "hf_egress_before": [], "hf_egress6_before": [], "hf_egress_status": []})
            self.assertEqual(str(got).lower() == "true", want, (now, prev))


if __name__ == "__main__":
    unittest.main()

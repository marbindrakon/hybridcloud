"""An AWS placement's teardown (networkplacement_teardown main.yml): the VPC, the IPAM pool, the DHCP option set and
the IPAM are the ones the placement's status records (status.realization.site.vpc, .ipv6.pool, .ipv6.dhcpOptions,
.ipv6.ipam), passed to
place-down, which deletes nothing else in the account; with no VPC recorded the helper deletes nothing there and the
deletion message names what carries the placement's tag. Names are examples."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

MAIN = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_teardown", "tasks", "main.yml")


def call_probe():
    call = by_name(load(MAIN), "Remove the placement's VPC (and the network's hub side with its last placement)")
    return {"ansible.builtin.set_fact": {"_args": call["vars"]["aws_args"]},
            "vars": {k: v for k, v in call["vars"].items() if k not in ("aws_args", "aws_config")}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AwsTeardownReadsStatus(unittest.TestCase):
    def test_arguments(self):
        site = {"account": "111111111111", "region": "r1"}
        v6site = dict(site, vpc="vpc-1", ipv6={"source": "Network", "prefix": "3fff:0:4000:10::/60",
                                               "pool": "ipam-pool-1", "dhcpOptions": "dopt-1", "ipam": "ipam-1"})
        cases = [
            {"st": {"realization": {"site": dict(site, vpc="vpc-1")}}, "spec": {"ipv4": {}}},
            {"st": {"realization": {"site": v6site}}, "spec": {"ipv6": {"source": "Network"}}},
            {"st": {"realization": {"site": site}}, "spec": {"ipv4": {}}},
            {"st": {}, "spec": {"ipv4": {}}},
        ]
        probe = call_probe()
        probe["vars"] = dict(probe["vars"], fab_st="{{ case.st }}", fab_spec="{{ case.spec }}")
        out = run_cases([probe], cases, "_args",
                        extra_vars={"fab_namespace": "entity-acme-corp", "fab_name": "core-prod",
                                    "npd_real": {"l3Vni": 51000}, "_npd_aws_env": {}})
        head = ["place-down", "--placement", "entity-acme-corp/core-prod", "--vni", "51000"]
        self.assertEqual(out[0], head + ["--vpc-id", "vpc-1"])
        self.assertEqual(out[1][:7], head + ["--vpc-id", "vpc-1"])
        v6 = json.loads(out[1][8])
        self.assertEqual((out[1][7], v6["pool"], v6["dhcpOptions"], v6["ipam"], v6["only"]),
                         ("--ipv6", "ipam-pool-1", "dopt-1", "ipam-1", True))
        # nothing recorded: no --vpc-id, and the helper then deletes nothing in the account
        self.assertEqual(out[2], head)
        self.assertEqual(out[3], head)

    def test_the_message_names_what_was_left(self):
        note = by_name(load(MAIN), "Note what was not deleted in the account")
        cases = [
            {"r": {"deleted": False, "note": "the placement recorded no VPC, so nothing was deleted in account 1",
                   "left": ["vpc vpc-9"]}},
            {"r": {"deleted": True, "left": ["vpc vpc-9"]}},
            {"r": {"deleted": True}},
        ]
        step = dict(note, vars=dict(note.get("vars") or {}, aws_result="{{ {'result': case.r} }}"))
        out = run_cases([{"ansible.builtin.set_fact": {"npd_note": ""}}, step], cases, "npd_note")
        self.assertEqual(out[0], "; the placement recorded no VPC, so nothing was deleted in account 1; left in the "
                                 "account, as no status records them: vpc vpc-9")
        self.assertEqual(out[1], "; left in the account, as no status records them: vpc vpc-9")
        self.assertEqual(out[2], "")


if __name__ == "__main__":
    unittest.main()

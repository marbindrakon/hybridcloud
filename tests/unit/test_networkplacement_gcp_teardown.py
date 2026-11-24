"""A Google Cloud placement's teardown (networkplacement_teardown main.yml): the VPC and the external VPN gateway are
read from the placement's status (status.realization.site.vpc, .externalGateway, written by the place-up), never
rebuilt from the names; without a recorded VPC nothing on Google Cloud is deleted and the deletion message says so.
Names are examples."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

MAIN = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_teardown", "tasks", "main.yml")


def tasks():
    t = load(MAIN)
    rec = by_name(t, "The placement's recorded Google Cloud objects")
    note = by_name(t, "Note a placement without a recorded network")
    call = by_name(t, "Remove the placement's VPC and tunnels")
    # what the call would be given, and whether it runs
    probe = {"ansible.builtin.set_fact": {"_args": call["vars"]["gcp_args"], "_runs": "{{ %s }}" % call["when"]},
             "vars": {k: v for k, v in call["vars"].items() if k != "gcp_args"}}
    return [rec, dict(note), probe]


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GcpTeardownReadsStatus(unittest.TestCase):
    def test_cases(self):
        site = {"project": "p1", "region": "r1"}
        cases = [
            {"fab_st": {"realization": {"site": dict(site, vpc="shc-acme-corp--core-prod",
                                                     externalGateway="shc-edge-site1")}}},
            {"fab_st": {"realization": {"site": dict(site, vpc="shc-acme-corp--core-prod")}}},
            {"fab_st": {"realization": {"site": site}}},
            {"fab_st": {}},
        ]
        steps = tasks()
        # each case brings its own status
        for s in steps:
            s["vars"] = dict(s.get("vars") or {}, fab_st="{{ case.fab_st }}")
        out = run_cases(steps, cases, "{'args': _args, 'runs': _runs | bool, 'note': npd_note | default('')}",
                        extra_vars={"fab_namespace": "entity-acme-corp", "fab_name": "core-prod", "_npd_gcp_env": {}})
        self.assertEqual(out[0]["args"], ["place-down", "--placement", "core-prod", "--vpc", "shc-acme-corp--core-prod",
                                          "--external-gateway", "shc-edge-site1"])
        self.assertTrue(out[0]["runs"])
        self.assertEqual(out[1]["args"], ["place-down", "--placement", "core-prod", "--vpc", "shc-acme-corp--core-prod"])
        for o in out[2:]:
            self.assertFalse(o["runs"])
            self.assertIn("no Google Cloud network was recorded", o["note"])
        self.assertEqual(out[0]["note"], "")


if __name__ == "__main__":
    unittest.main()

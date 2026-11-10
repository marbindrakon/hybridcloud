"""aap-config: the fabric-health job template and its schedule, rendered from the fabric realization's health block
(env FABRIC_REALIZATION as aap-config reads it): interval in whole minutes, the default and minimum the realization
documents, off with enabled false."""
import json
import os
import sys
import unittest
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import HAVE_ANSIBLE, ROOT, load, run_play, write_out  # noqa: E402

SCHED = os.path.join(ROOT, "aap-config/controller/schedules.yml")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Schedule(unittest.TestCase):
    def render(self, real):
        env = {"FABRIC_REALIZATION": json.dumps(real)} if real is not None else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if real is None:
                os.environ.pop("FABRIC_REALIZATION", None)
            return run_play([{"name": "load", "ansible.builtin.include_vars": {"file": SCHED}},
                             write_out("controller_schedules")], {})

    def test_default_and_minimum(self):
        (s,) = self.render(None)
        self.assertEqual(s["unified_job_template"], "fabric-health")
        self.assertTrue(s["rrule"].endswith("RRULE:FREQ=MINUTELY;INTERVAL=10"))
        self.assertTrue(s["enabled"] in (True, "True", "true"))
        (s,) = self.render({"health": {"intervalSeconds": 60}})
        self.assertTrue(s["rrule"].endswith("INTERVAL=5"))
        (s,) = self.render({"health": {"intervalSeconds": "900", "enabled": False}})
        self.assertTrue(s["rrule"].endswith("INTERVAL=15"))
        self.assertIn(str(s["enabled"]).lower(), ("false",))

    def test_default_matches_the_realization(self):
        real = load(os.path.join(ROOT, "eda/common/vars/fabric_realization.yml"))["fabric_realization_defaults"]
        self.assertEqual(real["health"]["intervalSeconds"], 600)

    def test_job_template(self):
        jts = {j["name"]: j for j in load(os.path.join(ROOT, "aap-config/controller/job_templates.yml"))["controller_templates"]}
        jt = jts["fabric-health"]
        self.assertEqual(jt["playbook"], "rulebooks/fabric-health-playbook.yml")
        self.assertFalse(jt["allow_simultaneous"])
        self.assertGreater(jt["timeout"], 0)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "eda", jt["playbook"])))


if __name__ == "__main__":
    unittest.main()

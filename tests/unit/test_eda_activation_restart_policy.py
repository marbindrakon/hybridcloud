"""Every event activation the platform defines (aap-config/eda/rulebook_activations.yml) restarts always.

Each rulebook is a consumer that is meant to run for ever: a clean exit means its event source ended, and with
restart policy on-failure the activation then stays "completed" and its events are no longer handled. With always
the automation platform restarts it after a clean exit too."""
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

DEFINITIONS = os.path.join(ROOT, "aap-config", "eda", "rulebook_activations.yml")


class RestartPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DEFINITIONS) as f:
            cls.activations = yaml.safe_load(f)["eda_rulebook_activations"]

    def test_every_activation_restarts_always(self):
        self.assertTrue(self.activations)
        wrong = [a["name"] for a in self.activations if a.get("restart_policy") != "always"]
        self.assertEqual(wrong, [], "activations without restart_policy always")

    def test_every_activation_is_named_once(self):
        names = [a["name"] for a in self.activations]
        self.assertEqual(len(names), len(set(names)))

    def test_every_rulebook_exists(self):
        missing = [a["rulebook"] for a in self.activations
                   if not os.path.exists(os.path.join(ROOT, "eda", "rulebooks", a["rulebook"]))]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

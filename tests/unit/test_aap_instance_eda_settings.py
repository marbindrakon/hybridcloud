"""Settings of the Event-Driven Ansible server through the instance chart (bootstrap/helm/charts/aap-instance:
eda.extraSettings -> AnsibleAutomationPlatform spec.eda.extra_settings), rendered with helm template.

Nothing is set by default, so the rendered instance is what it was before; a list of {setting, value} is passed
through with every value as a string (the server reads them as environment variables); an item without a setting
or a value fails the render with the value's name."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import ROOT  # noqa: E402

CHART = os.path.join(ROOT, "bootstrap", "helm", "charts", "aap-instance")


def render(*sets, values=None):
    args = ["helm", "template", "t", CHART, "--set", "namespace=example-aap", "--show-only", "templates/instance.yaml"]
    for s in sets:
        args += ["--set", s]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values or {}, f)
    try:
        p = subprocess.run(args + ["-f", f.name], capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(f.name)
    docs = [d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict)] if p.returncode == 0 else []
    return p, docs


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class EdaSettings(unittest.TestCase):
    def eda(self, *sets):
        p, docs = render(*sets)
        self.assertEqual(p.returncode, 0, p.stderr)
        return docs[0]["spec"]["eda"]

    def test_nothing_by_default(self):
        self.assertEqual(self.eda(), {"replicas": 1})
        with open(os.path.join(CHART, "values.yaml")) as f:
            self.assertEqual(yaml.safe_load(f)["eda"]["extraSettings"], [])

    def test_settings_are_passed_through_as_strings(self):
        eda = self.eda("eda.extraSettings[0].setting=EDA_ACTIVATION_MAX_RESTARTS_ON_FAILURE",
                       "eda.extraSettings[0].value=100",
                       "eda.extraSettings[1].setting=EDA_EXAMPLE_FLAG", "eda.extraSettings[1].value=true")
        self.assertEqual(eda["extra_settings"], [
            {"setting": "EDA_ACTIVATION_MAX_RESTARTS_ON_FAILURE", "value": "100"},
            {"setting": "EDA_EXAMPLE_FLAG", "value": "true"}])

    def test_a_null_value_or_a_map_is_refused(self):
        for values, text in (({"eda": {"extraSettings": [{"setting": "EDA_X", "value": None}]}},
                               "eda.extraSettings: each item needs a setting and a value"),
                              ({"eda": {"extraSettings": {"EDA_X": "1"}}}, "eda.extraSettings must be a list")):
            with self.subTest(values=values):
                p, _ = render(values=values)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(text, p.stderr)

    def test_an_incomplete_item_is_refused(self):
        for sets in (["eda.extraSettings[0].setting=EDA_X"], ["eda.extraSettings[0].value=1"]):
            with self.subTest(sets=sets):
                p, _ = render(*sets)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("eda.extraSettings", p.stderr)


if __name__ == "__main__":
    unittest.main()

"""The rule for names in the API, against the jobs' copy of the rule.

The CRDs carry the rule as CEL on metadata.name; eda/common/files/shc_names.py carries it for the jobs (NAME_PATTERN,
NAME_MAX, GATEWAY_NAME_MAX). Both must say the same. tests/cel-ipam/cel_cases_names.py runs the CEL on an API server
with the same names; this file needs no API server.
"""
import importlib.util
import os
import re
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASES = os.path.join(ROOT, "operator", "config", "crd", "bases")
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_names  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.join(ROOT, "tests", "schema-replay"))
    spec.loader.exec_module(mod)
    return mod


CASES = _load(os.path.join(ROOT, "tests", "cel-ipam", "cel_cases_names.py"), "cel_cases_names")


def crds():
    out = {}
    for f in sorted(os.listdir(BASES)):
        with open(os.path.join(BASES, f)) as fh:
            d = yaml.safe_load(fh)
        out[d["spec"]["names"]["kind"]] = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    return out


def name_rules(schema):
    md = schema.get("properties", {}).get("metadata", {})
    return (md.get("properties", {}).get("name", {}) or {}).get("x-kubernetes-validations", [])


class TheRuleInTheAPI(unittest.TestCase):
    def test_every_kind_is_classified(self):
        kinds = set(crds())
        self.assertEqual(kinds, set(CASES.RULED) | set(CASES.UNRULED) | {"TenantProfile"},
                         "a new kind must be added to RULED or UNRULED in tests/cel-ipam/cel_cases_names.py")

    def test_pattern_and_limit_equal_the_jobs_rule(self):
        schemas = crds()
        for kind, limit in CASES.RULED.items():
            rules = name_rules(schemas[kind])
            self.assertEqual(len(rules), 1, kind)
            rule = " ".join(rules[0]["rule"].split())
            pattern = re.search(r"self\.matches\('([^']*)'\)", rule).group(1)
            size = int(re.search(r"self\.size\(\) <= (\d+)", rule).group(1))
            self.assertEqual(pattern, shc_names.NAME_PATTERN, kind)
            self.assertEqual(size, limit, kind)
            self.assertIn("!self.contains('--')", rule, kind)
            self.assertIn("at most %d characters" % limit, " ".join(rules[0]["message"].split()), kind)
        self.assertEqual(CASES.RULED["CloudGateway"], shc_names.GATEWAY_NAME_MAX)
        self.assertEqual({v for k, v in CASES.RULED.items() if k != "CloudGateway"}, {shc_names.NAME_MAX})

    def test_configuration_kinds_carry_no_rule_of_ours(self):
        schemas = crds()
        for kind in CASES.UNRULED + ["TenantProfile"]:
            self.assertEqual(name_rules(schemas[kind]), [], kind)

    def test_the_tenants_old_rule_is_gone(self):
        rules = crds()["Entity"].get("x-kubernetes-validations", [])
        self.assertFalse([r for r in rules if "metadata.name" in r["rule"]])

    def test_the_jobs_rule_agrees_on_the_names(self):
        for kind, limit in CASES.RULED.items():
            for n in CASES.ACCEPTED + ["x" * limit]:
                self.assertEqual(shc_names.check_name(n, "name", limit), "", (kind, n))
            for n in CASES.REFUSED + ["x" * (limit + 1), ""]:
                self.assertNotEqual(shc_names.check_name(n, "name", limit), "", (kind, n))

    def test_google_rule_is_the_installers(self):
        # the OpenShift installer, pkg/validate/validate.go GCPClusterName: "^goog" and ".*g[o0]{2}gle.*"
        rules = crds()["PlatformOpenshift"]["x-kubernetes-validations"]
        rule = [" ".join(r["rule"].split()) for r in rules if "goog" in r["rule"]]
        self.assertEqual(len(rule), 1)
        self.assertIn("self.spec.type != 'gcp'", rule[0])
        self.assertIn("!self.metadata.name.startsWith('goog')", rule[0])
        self.assertIn("!self.metadata.name.matches('g[o0]{2}gle')", rule[0])
        self.assertTrue(rule[0].startswith("oldSelf.hasValue() ||"))
        for n in CASES.GOOGLE_REFUSED:
            self.assertTrue(re.match("^goog", n) or re.search("g[o0]{2}gle", n), n)
        for n in CASES.GOOGLE_ACCEPTED:
            self.assertFalse(re.match("^goog", n) or re.search("g[o0]{2}gle", n), n)

    def test_samples_obey_the_rule(self):
        for dirpath, _, files in os.walk(os.path.join(ROOT, "samples")):
            for f in files:
                if not f.endswith(".yaml"):
                    continue
                with open(os.path.join(dirpath, f)) as fh:
                    for d in yaml.safe_load_all(fh):
                        if d and d.get("kind") in CASES.RULED:
                            n = d["metadata"]["name"]
                            self.assertEqual(shc_names.check_name(n, "name", CASES.RULED[d["kind"]]), "", (f, n))

    def test_envtest_runner_exists(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tests", "cel-ipam", "cel_cases_names.py")))


if __name__ == "__main__":
    unittest.main()

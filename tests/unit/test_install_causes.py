"""The cause of a failed installation attempt (module_utils/shc_install.classify): each captured or hand-made provision
in tests/fixtures/install-causes/cases.yaml gives exactly its reason and sentence, and no text of the installer's log
or of Hive's messages ever reaches the sentence.

Run: python3 -m unittest tests.unit.test_install_causes  (or discover -s tests/unit -p test_install_causes.py)
"""
import os
import random
import re
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)

import shc_install as I  # noqa: E402
import shc_status_reasons as R  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "install-causes")

# What a log or a cloud's error body may carry that a tenant must never read: tokens, URLs, ids, addresses, host,
# project and account names, the hub's namespaces and pods.
HOSTILE = [
    "eyJhbGciOiJSUzI1NiIsImtpZCI6IkhPU1RJTEUifQ.eyJzdWIiOiJzeXN0ZW06c2VydmljZWFjY291bnQifQ.c2lnbmF0dXJl",
    "https://keystone.platform.example.net:5000/v3/auth/tokens?token=SECRETTOKEN123",
    "https://hostile-bucket.s3.amazonaws.com/bootstrap.ign?X-Amz-Signature=deadbeefcafebabe",
    "192.0.2.77", "198.51.100.23:6443", "2001:db8:dead:beef::1", "arn:aws:iam::123456789012:role/hostile-role",
    "hostile-sa@example-project.iam.gserviceaccount.com", "project-id-4f1c2b9e8a7d", "hostile-hub-namespace",
    "hostile-install-pod-x7k2p", "password=hunter2", "AKIAHOSTILEKEY0000000", "registry.platform.example.net",
    "0a1b2c3d-4e5f-4061-8a7b-9c0d1e2f3a4b",
]
HOSTILE_PATTERN = re.compile("|".join(re.escape(h) for h in HOSTILE) + r"|192\.0\.2|198\.51\.100|2001:db8|https?://|@")


# what the tenant typed (shown only when the log names exactly that value)
TENANT = {"compute": "example.large", "controlPlane": "example.xlarge", "image": "rhcos-example"}


def load_cases():
    with open(os.path.join(FIXTURES, "cases.yaml")) as fh:
        cases = yaml.safe_load(fh)
    for c in cases:
        if c.get("logFile"):
            with open(os.path.join(FIXTURES, c["logFile"])) as fh:
                c["log"] = fh.read()
    return cases


class FixtureCases(unittest.TestCase):
    def test_every_fixture(self):
        cases = load_cases()
        self.assertGreater(len(cases), 10)
        for c in cases:
            with self.subTest(c["name"]):
                got = I.classify(c["provider"], c.get("hive"), c.get("log"), c.get("tenant"), second=c.get("hiveMessage") or "")
                self.assertEqual(got["reason"], c["reason"])
                if "message" in c:
                    self.assertEqual(got["text"], c["message"])
                for part in c.get("contains", []):
                    self.assertIn(part, got["text"])
                self.assertIn(got["reason"], R.REASONS, "every cause reason is in the catalogue")

    def test_the_reasons_of_the_causes_are_in_the_catalogue(self):
        for reason in list(I.CAUSE_REASONS.values()) + [I.OVERDUE_REASON]:
            self.assertIn(reason, R.REASONS)


class Quota(unittest.TestCase):
    def test_both_forms_and_repeats_once(self):
        log = ("Cores is not available because the required number of resources (72) is more than the limit of 64\n"
               "Cores is not available because the required number of resources (72) is more than the limit of 64\n"
               "Port is not available because the required number of resources (60) is more than remaining quota of 10")
        self.assertEqual(I.parse_quota(log), [("Cores", 72, 64, "limit"), ("Ports", 60, 10, "left")])

    def test_numbers_only_digits(self):
        log = "Cores is not available because the required number of resources (7e2) is more than the limit of 64"
        self.assertEqual(I.parse_quota(log), [])

    def test_a_long_log_is_read_from_its_end_only(self):
        head = "Cores is not available because the required number of resources (72) is more than the limit of 64\n"
        log = head + ("x" * (I.LOG_SCAN_LIMIT + 10))
        self.assertNotEqual(I.classify("openstack", "UnknownError", log)["reason"], "QuotaExceeded")
        self.assertEqual(I.classify("openstack", "UnknownError", ("x" * (I.LOG_SCAN_LIMIT * 2)) + "\n" + head)["reason"],
                         "QuotaExceeded")

    def test_the_resource_word_comes_from_the_fixed_list(self):
        log = ("evil.example.org is not available because the required number of resources (1) is more than "
               "the limit of 0")
        got = I.classify("openstack", "", log)
        self.assertEqual(got["text"], "the site reports too little quota")


class Hostile(unittest.TestCase):
    """Hostile text in every place a pattern reads never reaches the sentence."""

    TEMPLATES = [
        "{h} is not available because the required number of resources (5) is more than the limit of 1",
        "Cores is not available in {h} because the required number of resources (5) is more than the limit of 1",
        "compute[0].platform.openstack.type: Not found: \"{h}\"",
        "controlPlane.platform.aws.type: Invalid value: \"{h}\": instance type {h} not found",
        "platform.openstack.clusterOSImage: Not found: \"{h}\"",
        "failed to create a network client: Authentication failed at {h}",
        "level=fatal msg={h}",
        "{h}",
    ]

    def check(self, got):
        self.assertIsNone(HOSTILE_PATTERN.search(got["text"]), got["text"])
        self.assertLessEqual(len(got["text"]), 400)

    def test_templates(self):
        for tpl in self.TEMPLATES:
            for h in HOSTILE:
                for provider in ("openstack", "aws", "gcp"):
                    for hive in ("UnknownError", "", h, "AWSEC2QuotaExceeded"):
                        got = I.classify(provider, hive, tpl.format(h=h), TENANT)
                        self.check(got)

    def test_random_mixtures(self):
        rnd = random.Random(20261118)
        parts = HOSTILE + [t.format(h=rnd.choice(HOSTILE)) for t in self.TEMPLATES] + [
            "Cores is not available because the required number of resources (72) is more than the limit of 64",
            "\"", "\\\"", "\n", ": ", "level=error msg="]
        for _ in range(1500):
            log = "".join(rnd.choice(parts) for _ in range(rnd.randint(1, 12)))
            self.check(I.classify(rnd.choice(["openstack", "aws", "gcp"]), rnd.choice(["UnknownError", "X"]), log,
                                  TENANT))

    def test_a_tenant_value_with_odd_characters_is_not_shown(self):
        got = I.classify("openstack", "", 'compute[0].platform.openstack.type: Not found: "a b/c"', {"compute": "a b/c"})
        self.assertEqual(got["text"], "the site refused the worker flavor: it does not exist")


if __name__ == "__main__":
    unittest.main()

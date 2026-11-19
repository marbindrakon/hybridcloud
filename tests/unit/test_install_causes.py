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

    def test_the_whole_stored_log_is_read(self):
        """A quota sentence or a flavor warning near the start of a long log still decides the cause."""
        head = "Cores is not available because the required number of resources (72) is more than the limit of 64\n"
        log = head + ("x" * 3_000_000)
        self.assertLess(len(log), I.LOG_SCAN_LIMIT)
        self.assertEqual(I.classify("openstack", "UnknownError", log)["reason"], "QuotaExceeded")
        warn = "level=warning msg=Unable to find flavor with name example.large\n"
        got = I.classify("openstack", "UnknownError", warn + "y" * 3_000_000, {"compute": "example.large"})
        self.assertEqual(got["reason"], "InstallSettingRefused")

    def test_beyond_the_bound_the_first_and_last_halves_are_read(self):
        head = "Cores is not available because the required number of resources (72) is more than the limit of 64\n"
        filler = "x" * (I.LOG_SCAN_LIMIT * 2)
        for log in (head + filler, filler + "\n" + head):
            self.assertEqual(I.classify("openstack", "UnknownError", log)["reason"], "QuotaExceeded")
        self.assertEqual(I.LOG_SCAN_LIMIT, 4 * 1024 * 1024)

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
        "level=warning msg=Unable to find flavor with name {h}",
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

    def gate(self, name):
        import json
        with open(os.path.join(ROOT, "tests", "fixtures", "install-causes", name)) as fh:
            return json.load(fh)

    def test_gate_capture_quota_of_three_resources(self):
        """Captured quota stop: three resources in one line, security group rules in the "remaining quota" form."""
        for a in self.gate("gate-openstack-quota-three-resources.json"):
            got = I.classify("openstack", a["reason"], a["installLog"])
            self.assertEqual(got["reason"], "QuotaExceeded")
            for part in ("Security group rules (56 needed, 48 left)", "Cores (180 needed, limit 128)",
                         "RAM (MB) (491520 needed, limit 262144)"):
                self.assertIn(part, got["text"], a["attempt"])

    def test_gate_capture_missing_image(self):
        """Captured late failure of a missing image: the image setting named when the cluster's spec named it."""
        for a in self.gate("gate-openstack-missing-image.json"):
            got = I.classify("openstack", a["reason"], a["installLog"], {"image": "example-missing-image"})
            self.assertEqual((got["reason"], got["text"]),
                             ("InstallSettingRefused", 'the site refused the image "example-missing-image": it does '
                                                       'not exist'))
            self.assertEqual(I.classify("openstack", a["reason"], a["installLog"], {})["reason"], "InstallFailed")

    def test_gate_capture_bootstrap_then_quota(self):
        """Captured: attempt 0 timed out in its bootstrap (the general failure, with the installer's own sentence),
        attempts 1 and 2 were refused by the security group rule quota."""
        attempts = self.gate("gate-openstack-bootstrap-then-quota.json")
        first = I.classify("openstack", attempts[0]["reason"], attempts[0]["installLog"])
        self.assertEqual((first["reason"], first["text"]),
                         ("InstallFailed", "the installer's bootstrap of the cluster did not complete"))
        for a in attempts[1:]:
            got = I.classify("openstack", a["reason"], a["installLog"])
            self.assertEqual((got["reason"], got["text"]),
                             ("QuotaExceeded", "the site reports too little quota: Security group rules (56 needed, "
                                               "48 left)"))

    def test_a_missing_image_is_named_only_when_the_spec_names_it(self):
        log = "level=warning msg=Failed to generate OpenStack cloud info: Unable to find image with name example-image\n"
        got = I.classify("openstack", "UnknownError", log, {"image": "example-image"})
        self.assertEqual((got["reason"], got["text"]),
                         ("InstallSettingRefused", 'the site refused the image "example-image": it does not exist'))
        self.assertEqual(I.classify("openstack", "UnknownError", log, {"image": ""})["reason"], "InstallFailed")
        self.assertEqual(I.classify("openstack", "UnknownError", log, {"image": "other"})["reason"], "InstallFailed")
        self.assertNotIn("example-image", I.classify("openstack", "UnknownError", log, {})["text"])

    def test_a_bootstrap_that_did_not_complete(self):
        log = ('level=error msg=Bootstrap failed to complete: Get "https://api.demo.example.org:6443/version": dial tcp '
               '192.0.2.10:6443: i/o timeout\n')
        got = I.classify("openstack", "UnknownError", log)
        self.assertEqual((got["reason"], got["text"]),
                         ("InstallFailed", "the installer's bootstrap of the cluster did not complete"))
        # only the installer's own error line: the words elsewhere do not count
        self.assertEqual(I.classify("openstack", "UnknownError", "level=info msg=Bootstrap failed to complete")["text"],
                         "the cause is not one the platform recognizes")

    def test_a_tenant_value_must_match_whole(self):
        """A value with a trailing newline is not the tenant's value as typed: not shown."""
        got = I.classify("openstack", "", 'compute[0].platform.openstack.type: Not found: "example.large\n"',
                         {"compute": "example.large\n"})
        self.assertNotIn("example.large", got["text"])
        self.assertIsNone(I.TENANT_VALUE.fullmatch("example.large\n"))
        self.assertEqual(I.classify("openstack", "AWSEC2QuotaExceeded\n", "")["reason"], "InstallFailed")

    def test_a_tenant_value_with_odd_characters_is_not_shown(self):
        got = I.classify("openstack", "", 'compute[0].platform.openstack.type: Not found: "a b/c"', {"compute": "a b/c"})
        self.assertEqual(got["text"], "the site refused the worker flavor: it does not exist")


if __name__ == "__main__":
    unittest.main()

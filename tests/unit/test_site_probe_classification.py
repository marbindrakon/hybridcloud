"""Site probe errors are classified: unreachable before credentials (cloudinfrastructure_provision)."""
import os
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TASKS = os.path.join(ROOT, "eda/rulebooks/roles/cloudinfrastructure_provision/tasks/openshift.yml")

UNREACHABLE = [
    "HTTPSConnectionPool(host='api.site.example', port=6443): Max retries exceeded with url: /version "
    "(Caused by NewConnectionError('<urllib3.connection.HTTPSConnection>: Failed to establish a new connection: "
    "[Errno 113] No route to host'))",
    "Failed to get client due to HTTPSConnectionPool(host='api.x', port=6443): Read timed out. (read timeout=20)",
    "[Errno -2] Name or service not known",
    "Connection refused",
]
CREDENTIALS = ["Failed to get client due to 401\nReason: Unauthorized", "403 Forbidden: clusterversions is forbidden"]


@unittest.skipIf(yaml is None, "PyYAML not installed")
class Classification(unittest.TestCase):
    def setUp(self):
        with open(TASKS) as fh:
            tasks = yaml.safe_load(fh)
        task = next(t for t in tasks if t.get("name") == "Set the API failure reason")
        pat = task["vars"]["ci_unreachable_pattern"]
        self.unreachable = re.compile(pat)
        self.creds = re.compile(r"401|403|[Uu]nauthori[sz]ed|[Ff]orbidden")

    def classify(self, msg):
        if self.creds.search(msg):
            return "CredentialsInvalid"
        if self.unreachable.search(msg):
            return "SiteUnreachable"
        return "ProbeFailed" if msg else "CredentialsInvalid"

    def test_cases(self):
        for msg in UNREACHABLE:
            self.assertEqual(self.classify(msg), "SiteUnreachable", msg)
        for msg in CREDENTIALS:
            self.assertEqual(self.classify(msg), "CredentialsInvalid", msg)
        self.assertEqual(self.classify("the server could not find the requested resource"), "ProbeFailed")


if __name__ == "__main__":
    unittest.main()

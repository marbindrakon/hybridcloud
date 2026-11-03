"""shc_gcp_project.py external: the credentials it runs with. --key-file activates the project owner's initial
credentials for the run and revokes them at the end; --active-account uses the account gcloud has active (a project
owner's own session) and neither activates nor revokes anything; exactly one of the two. gcloud is replaced by a
recorder (no cloud is reached). Generic names only."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "eda", "common", "files"))
import shc_gcp_project as P  # noqa: E402

PROJECT = "tenant-proj"
MEMBER = "shc-automation@tenant-proj.iam.gserviceaccount.com"


class FakeGcloud:
    """Answers the gcloud calls external() makes for a project that already has everything (idempotent re-run)."""

    def __init__(self, active=("owner@example.com",)):
        self.calls = []
        self.active = list(active)

    def __call__(self, args, capture_output=True, text=True, check=False):
        self.calls.append(list(args))
        rest = args[1:]
        if rest[:2] == ["auth", "list"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(self.active) + "\n", "")
        if rest[:2] in (["auth", "activate-service-account"], ["auth", "revoke"]):
            return subprocess.CompletedProcess(args, 0, "", "")
        if rest[:2] == ["projects", "describe"]:
            body = {"projectNumber": "123", "labels": dict(P.LABELS)}
        elif rest[:3] == ["iam", "service-accounts", "list"]:
            body = [{"email": MEMBER}]
        elif rest[:2] == ["projects", "get-iam-policy"]:
            body = {"bindings": [{"role": "roles/owner", "members": ["serviceAccount:" + MEMBER]}]}
        elif rest[:3] == ["iam", "service-accounts", "get-iam-policy"]:
            hub = "serviceAccount:shc-platform-automation@hub-proj.iam.gserviceaccount.com"
            hive = "principalSet://iam.googleapis.com/projects/42/locations/global/workloadIdentityPools/shc-central/*"
            body = {"bindings": [{"role": "roles/iam.serviceAccountTokenCreator", "members": [hub, "serviceAccount:" + MEMBER]},
                                 {"role": "roles/iam.workloadIdentityUser", "members": [hive]}]}
        elif rest[:2] == ["services", "list"]:
            body = [{"config": {"name": s}} for s in P.NEEDED_APIS]
        else:
            body = None
        return subprocess.CompletedProcess(args, 0, json.dumps(body) if body is not None else "", "")

    def verbs(self):
        return [" ".join(c[1:3]) for c in self.calls]


def run_external(argv, fake):
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["shc_gcp_project.py", "external", "--project", PROJECT,
                                         "--hub-project", "hub-proj", "--hub-number", "42"] + argv), \
            mock.patch.object(P.subprocess, "run", fake), redirect_stdout(out):
        try:
            P.main()
            code = 0
        except SystemExit as e:
            code = e.code
    lines = [x for x in out.getvalue().splitlines() if x.strip()]
    return code, (json.loads(lines[-1]) if lines else None)


class ExternalCredentials(unittest.TestCase):
    def test_active_account_is_used_as_is(self):
        fake = FakeGcloud()
        code, res = run_external(["--active-account"], fake)
        self.assertEqual(code, 0, res)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["changed"], [])
        self.assertEqual(res["account"], "active")
        self.assertNotIn("auth activate-service-account", fake.verbs())
        self.assertNotIn("auth revoke", fake.verbs())
        # every project call runs as the active account
        for c in fake.calls:
            if c[1] not in ("auth",):
                self.assertIn("owner@example.com", c, c)

    def test_active_account_needs_exactly_one_signed_in_account(self):
        for active in ((), ("a@example.com", "b@example.com")):
            fake = FakeGcloud(active=active)
            code, res = run_external(["--active-account"], fake)
            self.assertEqual(code, 1)
            self.assertFalse(res["ok"])
            self.assertIn("gcloud auth login", res["error"])
            self.assertEqual(fake.verbs(), ["auth list"], active)

    def test_key_file_is_activated_and_revoked(self):
        fake = FakeGcloud()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"client_email": "initial@tenant-proj.iam.gserviceaccount.com"}, fh)
        try:
            code, res = run_external(["--key-file", fh.name], fake)
        finally:
            os.unlink(fh.name)
        self.assertEqual(code, 0, res)
        verbs = fake.verbs()
        self.assertEqual(verbs[0], "auth activate-service-account")
        self.assertEqual(verbs[-1], "auth revoke")
        self.assertIn("initial@tenant-proj.iam.gserviceaccount.com", fake.calls[-1])

    def test_exactly_one_of_key_file_and_active_account(self):
        for argv in ([], ["--key-file", "/nonexistent.json", "--active-account"]):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                code, _ = run_external(argv, FakeGcloud())
            self.assertEqual(code, 2, argv)


if __name__ == "__main__":
    unittest.main()

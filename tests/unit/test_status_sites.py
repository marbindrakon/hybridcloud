"""Every job status write names its outcome: a ready write carries a message, a failed write a reason
(or its own Ready condition). Scans the include sites of patch_cr_status.yml in the job roles."""
import glob
import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _walk(tasks, path, out):
    if not isinstance(tasks, list):
        return
    for t in tasks:
        if not isinstance(t, dict):
            continue
        for key in ("block", "rescue", "always"):
            if key in t:
                _walk(t[key], path, out)
        inc = t.get("ansible.builtin.include_tasks") or t.get("include_tasks")
        if isinstance(inc, str) and "patch_cr_status" in inc:
            body = (t.get("vars") or {}).get("cr_status_body")
            if isinstance(body, dict):
                out.append((path, t.get("name", ""), body))


def status_sites(root=ROOT):
    files = glob.glob(os.path.join(root, "eda/rulebooks/roles/*/tasks/*.yml")) + \
        glob.glob(os.path.join(root, "eda/common/tasks/*.yml"))
    out = []
    for f in sorted(files):
        with open(f) as fh:
            _walk(yaml.safe_load(fh), os.path.relpath(f, root), out)
    return out


def _has_ready(body):
    conds = body.get("conditions")
    return isinstance(conds, list) and any(isinstance(c, dict) and c.get("type") == "Ready" for c in conds)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class StatusSites(unittest.TestCase):
    def test_ready_writes_have_a_message(self):
        bad = ["%s | %s" % (p, n) for p, n, b in status_sites()
               if b.get("status") == "ready" and not b.get("message") and not _has_ready(b)]
        self.assertEqual(bad, [])

    def test_failed_writes_have_a_reason(self):
        bad = ["%s | %s" % (p, n) for p, n, b in status_sites()
               if "failed" in str(b.get("status")) and not b.get("reason") and not _has_ready(b)]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    for p, n, b in status_sites():
        print(p, "|", n, "|", b.get("status"), "| reason" if b.get("reason") else "", "| message" if b.get("message") else "")

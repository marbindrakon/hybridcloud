"""The PlatformMarks condition of the three OpenStack jobs that keep a project the platform made (an environment's,
the site's networks', the site's tenant DNS): when the site refuses the write of the platform's mark on a project that
is ours by its record, the object stays Ready for everyone who reads it. The condition is Unknown (neither a fault nor
a confirmation) with a ready-class reason, the status library derives phase Ready and the Ready summary, the tenant's
own message carries nothing of it, and the condition's message is the job's product sentence (the right the site
account lacks), never the site's answer."""
import os
import re
import sys
import unittest

import jinja2
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")

import shc_status as St  # noqa: E402
import shc_status_reasons as R  # noqa: E402

# the job's note (shc_openstack.mark_not_written, without importing the SDK it needs)
NOTE = ("The platform could not write its mark (a project tag) and description on the environment's project: the "
        "site account may not update projects in their domain. Nothing is wrong with the environment: the project is "
        "the platform's by the id it recorded. The platform team gives the site account the right to update "
        "projects, with their tags, in that domain")

# (file, the variable the condition reads, its value when the site refused the write)
WRITERS = (
    ("cloudoso_provision/tasks/main.yml", {"oso_mark_note": NOTE}),
    ("cloudgateway_provision/tasks/site_openstack.yml", {"os_result": {"markNote": NOTE}}),
    ("cloudinfrastructure_provision/tasks/openstack.yml", {"ci_probe": {"notes": [NOTE]}}),
)
EMPTY = {"oso_mark_note": "", "os_result": {"markNote": ""}, "ci_probe": {"notes": []}}


def conditions(path):
    """Every PlatformMarks condition literal in a task file (a list item with type PlatformMarks)."""
    found = []

    def walk(node, local):
        if isinstance(node, dict):
            if node.get("type") == "PlatformMarks":
                found.append((node, local))
            here = dict(local, **(node.get("vars") or {})) if isinstance(node.get("vars"), dict) else local
            for v in node.values():
                walk(v, here)
        elif isinstance(node, list):
            for v in node:
                walk(v, local)

    with open(os.path.join(ROLES, path)) as f:
        walk(yaml.safe_load(f), {})
    return found


def render(text, ctx):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["search"] = lambda v, p: re.search(p, str(v)) is not None
    return env.from_string(str(text)).render(**ctx)


class PlatformMarks(unittest.TestCase):
    def rendered(self, path, ctx):
        conds = conditions(path)
        self.assertEqual(len(conds), 1, path)
        cond, local = conds[0]
        full = dict(EMPTY, **ctx)
        used = " ".join(str(v) for v in cond.values())
        # the task's own vars the condition reads (templates over the same context)
        for k, v in local.items():
            if re.search(r"\b%s\b" % re.escape(k), used):
                full[k] = yaml.safe_load(render(v, full)) if isinstance(v, str) and "{{" in v else v
        return {k: render(v, full).strip() for k, v in cond.items()}

    def test_a_refused_mark_keeps_the_object_ready(self):
        for path, ctx in WRITERS:
            with self.subTest(path=path):
                c = self.rendered(path, ctx)
                self.assertEqual(c["status"], "Unknown")
                self.assertEqual(R.PART_REASONS[c["reason"]]["class"], R.READY)  # never a Ready reason
                self.assertTrue(c["message"].startswith(NOTE))
                for raw in ("403", "Forbidden", "identity:", "update_project"):
                    self.assertNotIn(raw, c["message"])
                obj = {"metadata": {"generation": 2},
                       "status": {"observedGeneration": 2, "conditions": [
                           {"type": "Ready", "status": "True", "reason": "EDASucceeded", "message": ""}]}}
                patch, info = St.compute_status_patch(obj, {"status": "ready", "ready": True, "conditions": [
                    {"type": "Ready", "status": "True", "reason": "EDASucceeded", "message": ""}, c]},
                    {"action": "ready"})
                self.assertEqual(info["phase"], "Ready")
                self.assertEqual(patch["status"]["summary"], R.REASONS["EDASucceeded"]["summary"])

    def test_a_written_mark_says_so(self):
        for path, _ in WRITERS:
            with self.subTest(path=path):
                c = self.rendered(path, {})
                self.assertEqual((c["status"], c["reason"]), ("True", "Verified"))

    def test_the_tenants_message_carries_nothing_of_it(self):
        with open(os.path.join(ROLES, "cloudoso_provision/tasks/main.yml")) as f:
            text = f.read()
        ready = text[text.index("cr_status_body:"):text.index("conditions:", text.index("cr_status_body:"))]
        self.assertNotIn("oso_mark_note", ready)


if __name__ == "__main__":
    unittest.main()

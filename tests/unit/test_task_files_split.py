"""Every free-form shell or command task must survive ansible's own argument splitter.

ansible loads a task file by splitting each free-form `shell:`/`command:` string into arguments, and that splitter
counts quotes across the whole string, comments included: one apostrophe in a comment inside a `shell: |` block (an
odd number of them) makes the whole task file fail to load ("failed at splitting arguments, either an unbalanced
jinja2 block or quotes"), and the job stops before it reaches a node. The other tests read task contents and never
load a file the way a job does, so this one runs the splitter over every such task."""
import glob
import os
import unittest

import play_helper as ph

try:
    from ansible.errors import AnsibleError
    from ansible.parsing.splitter import split_args
except ImportError:  # no ansible on this host
    split_args = None

KEYS = ("ansible.builtin.shell", "shell", "ansible.builtin.command", "command")


def task_files():
    found = []
    for pattern in ("eda/rulebooks/roles/*/tasks/*.yml", "eda/common/tasks/*.yml", "eda/rulebooks/*.yml",
                    "eda/playbooks/**/*.yml"):
        found += glob.glob(os.path.join(ph.ROOT, pattern), recursive=True)
    return sorted(set(found))


def broken(tasks):
    """(task name, first line of the error) for every free-form task the splitter refuses."""
    out = []
    for t in ph.walk(tasks):
        for key in KEYS:
            v = t.get(key)
            if isinstance(v, str):
                try:
                    split_args(v)
                except AnsibleError as e:
                    out.append((t.get("name"), str(e).splitlines()[0]))
    return out


@unittest.skipUnless(split_args, "ansible is not installed")
class FreeFormTasksSplit(unittest.TestCase):
    def test_the_splitter_refuses_an_odd_apostrophe_in_a_comment(self):
        # the defect this test exists for, so a change in ansible's splitter that makes the test vacuous shows
        bad = [{"name": "x", "ansible.builtin.shell": "# the VRFs' rule\ntrue\n"}]
        self.assertEqual([n for n, _ in broken(bad)], ["x"])

    def test_every_free_form_task_splits(self):
        files, failures = task_files(), []
        self.assertGreater(len(files), 50)
        for path in files:
            try:
                doc = ph.load(path)
            except Exception:  # not plain YAML (a rulebook with tags the loader lacks): not a task file
                continue
            if not isinstance(doc, list):
                continue
            plays = [p for p in doc if isinstance(p, dict)]
            tasks = []
            for p in plays:
                if any(k in p for k in ("tasks", "pre_tasks", "post_tasks", "handlers")):
                    for k in ("tasks", "pre_tasks", "post_tasks", "handlers"):
                        tasks += p.get(k) or []
                else:
                    tasks.append(p)
            for name, err in broken(tasks):
                failures.append(f"{os.path.relpath(path, ph.ROOT)}: {name!r}: {err}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()

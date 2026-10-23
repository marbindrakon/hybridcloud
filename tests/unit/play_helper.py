"""Shared helpers for the unit tests that run task files (or single tasks) in ansible-playbook on localhost."""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HAVE_ANSIBLE = shutil.which("ansible-playbook") is not None


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def walk(tasks):
    """Every task, also the ones nested in block/rescue/always."""
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            yield from walk(t.get(key))


def by_name(tasks, name):
    return next(t for t in walk(tasks) if t.get("name") == name)


def write_out(expr):
    return {"name": "write the result", "ansible.builtin.copy": {"dest": "{{ out }}", "content": "{{ %s | to_json }}" % expr}}


def run_play(tasks, extra_vars, files=None):
    """Run tasks on localhost and return the JSON the tasks wrote to {{ out }}.

    files: optional {relative path: task list} written next to the play (for include_tasks of a copied task file).
    """
    tmp = tempfile.mkdtemp(prefix="shc-play-test-")
    try:
        out = os.path.join(tmp, "out.json")
        with open(os.path.join(tmp, "tasks.yml"), "w") as f:
            yaml.safe_dump(tasks, f)
        for rel, content in (files or {}).items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                yaml.safe_dump(content, f)
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent('''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: tasks.yml
                '''))
        with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\n")
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump(dict(extra_vars, out=out), f)
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"],
                              cwd=tmp, env=env, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-2000:])
        with open(out) as f:
            return json.load(f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

"""Every command a job runs on a border gateway node ends there at a limit, whether or not the job is still connected.

A node that stops answering mid-step ends the job without its rescue or always (the lease is kept), and the node may
never see the session end. So the bound is on the node: every module a job runs there goes through the become
command of common/tasks/bgw_connect.yml (sudo, then timeout(1), then env), which ends the module and everything in its
process group at the limit. The lease holders set the limit per step from what their lease leaves
(common/vars/fabric_lease.yml fab_lease_node_become, on their node-loop blocks); the other roles that read the nodes
get the connection's default.

Static checks (they fail when a node task is added that would escape the bound):
- the connection: the become command and its flags, the SSH client's dead-peer detection;
- the holders' node-loop blocks set the per-step become command, and their node steps are all inside those loops;
- every task delegated to a node runs a module through the become command: no raw or script, no become off, no
  connection or become settings of its own, no async; its shell text starts nothing outside its process group
  (nohup, setsid, systemd-run, a command sent to the background), and starts or stops a unit only in the steps listed
  here (timers and a oneshot without a stop action, each bounded by its own unit);
- the numbers: the node ends a holder's step by the step's own limit; a reader's limit covers its own wait.
With ansible-playbook: the become command as built runs on this machine for a stand-in node (a sudo stand-in on
PATH): a step's limit follows the lease, and a step that hangs ends at it with nothing left running, which keeps the
lease as a timeout does.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, walk  # noqa: E402

ROLES = os.path.join(ROOT, "eda/rulebooks/roles")
COMMON = os.path.join(ROOT, "eda/common")
# holder role: (main file, node-loop block, the node file its loop includes)
HOLDERS = {"hybridfabric_provision": ("Configure the nodes, one at a time", "node_root.yml"),
           "cloudgateway_provision": ("Configure the gateway on the nodes, one at a time", "node_gateway.yml"),
           "cloudgateway_teardown": ("One node at a time", "node_gateway_down.yml"),
           "hybridfabric_teardown": ("Remove the root instance, one node at a time", "node_root_down.yml")}
READERS = {"attachmentcircuit_provision", "networkplacement_provision", "fabric_orphan_check"}
# files of a holder included from its node file (the includes run inside the node loop)
NODE_INCLUDES = {"hybridfabric_provision": {"node_transport.yml", "core_guard_unit.yml"},
                 "hybridfabric_teardown": {"node_programs_down.yml"}}
MODULES = {"ansible.builtin.shell", "ansible.builtin.command", "ansible.builtin.systemd", "ansible.builtin.copy",
           "ansible.builtin.template", "ansible.builtin.file", "ansible.builtin.lineinfile", "ansible.builtin.stat",
           "ansible.builtin.slurp"}
OWN_SETTINGS = re.compile(r"^ansible_(become|connection|host|port|user|ssh|python_interpreter|shell)")
# outside the process group timeout(1) ends: daemons, detached sessions, transient units, background jobs
ESCAPE = re.compile(r"\b(nohup|setsid|systemd-run|disown|daemonize|start-stop-daemon)\b|(?<![&|>])&\s*($|#|\n)|&\s*\)")
# a unit start, stop or reload from a shell step: the systemd job runs in PID 1, outside the group. Only these, each
# on a timer or a oneshot without a stop action (no long stop), so a stop returns at once
UNIT_ACTION = re.compile(r"systemctl\s+(-q\s+)?(start|stop|restart|reload|try-restart|reload-or-restart|kill|"
                         r"(enable|disable)\s+--now)\b")
UNIT_ACTIONS_ALLOWED = {
    ("cloudgateway_teardown", "{{ bgw_node.name }}: stop the instance's SRv6 core guard"),
    ("hybridfabric_provision", "{{ bgw_node.name }}: no IPv6 egress guard without IPv6 egress"),
    ("hybridfabric_teardown", "{{ bgw_node.name }}: once no instance is left, remove the fabric's programs and units"),
    ("hybridfabric_teardown", "{{ bgw_node.name }}: remove the SRv6 core guard units"),
    ("hybridfabric_teardown", "{{ bgw_node.name }}: remove the IPv6 egress guard"),
}
BECOME = re.compile(r"^sudo -H -S -n /usr/bin/timeout -k 5 (?P<limit>.+) /usr/bin/env$")


def task_lists(base):
    for d, _, files in os.walk(base):
        for f in sorted(files):
            if f.endswith(".yml"):
                path = os.path.join(d, f)
                try:
                    doc = load(path)
                except yaml.YAMLError:
                    continue
                if isinstance(doc, list):
                    yield path, doc


def walk_inherit(tasks, keys=("delegate_to", "become", "vars", "async"), parent=None):
    """Every task with what it inherits from its blocks."""
    parent = parent or {}
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        own = dict(parent)
        for k in keys:
            if k in t:
                own[k] = (dict(parent.get(k) or {}, **t[k]) if k == "vars" else t[k])
        yield t, own
        for key in ("block", "rescue", "always"):
            yield from walk_inherit(t.get(key), keys, own)


def connecting_roles():
    roles = set()
    for path, _ in task_lists(ROLES):
        with open(path) as f:
            if "common/tasks/bgw_connect.yml" in f.read():
                roles.add(os.path.relpath(path, ROLES).split(os.sep)[0])
    return roles


def node_tasks():
    """(role, file, task, inherited) for every task that runs on a border gateway node."""
    for role in sorted(connecting_roles()):
        for path, tasks in task_lists(os.path.join(ROLES, role)):
            for t, inh in walk_inherit(tasks):
                if "block" in t:
                    continue
                d = inh.get("delegate_to")
                if d and not str(d).startswith("localhost"):
                    yield role, os.path.basename(path), t, inh


def add_host():
    return by_name(load(os.path.join(COMMON, "tasks/bgw_connect.yml")),
                   "Reach the border gateway nodes over SSH")["ansible.builtin.add_host"]


class Connection(unittest.TestCase):
    def test_every_node_command_runs_as_root_under_timeout(self):
        h = add_host()
        self.assertIs(h["ansible_become"], True)
        m = BECOME.match(" ".join(h["ansible_become_exe"].split()))
        self.assertTrue(m, h["ansible_become_exe"])
        self.assertIn("bgw_node_limit", m.group("limit"))
        self.assertIn("stepTimeoutSeconds", m.group("limit"))
        # the flags are in the become command: ansible's own would land in front of env (the become user too, which
        # env takes as "-u root": unset a variable named root); no become password, whose prompt would land there
        self.assertEqual(h["ansible_become_flags"], "")
        self.assertNotIn("ansible_become_user", h)
        self.assertFalse({k for k in h if "pass" in k})

    def test_the_ssh_client_notices_a_silent_node(self):
        args = add_host()["ansible_ssh_common_args"]
        self.assertIn("-o ServerAliveInterval=15", args)
        self.assertIn("-o ServerAliveCountMax=3", args)
        self.assertIn("-o ConnectTimeout=15", args)

    def test_the_holders_set_the_per_step_limit_on_their_node_loops(self):
        lease = load(os.path.join(COMMON, "vars/fabric_lease.yml"))
        m = BECOME.match(" ".join(lease["fab_lease_node_become"].split()))
        self.assertTrue(m, lease["fab_lease_node_become"])
        self.assertEqual(m.group("limit"), "{{ [fab_lease_step_timeout | int - 5, 1] | max }}")
        for role, (block, node_file) in HOLDERS.items():
            with self.subTest(role=role):
                b = by_name(load(os.path.join(ROLES, role, "tasks/main.yml")), block)
                self.assertEqual(b["timeout"], "{{ fab_lease_step_timeout }}")
                self.assertEqual(b["vars"]["ansible_become_exe"], "{{ hostvars[inventory_hostname].fab_lease_node_become }}")
                self.assertIn(node_file, [t.get("ansible.builtin.include_tasks") for t in walk(b["block"])])

    def test_a_readers_limit_covers_its_wait(self):
        connect = by_name(load(os.path.join(ROLES, "networkplacement_provision/tasks/gateway_routes.yml")),
                          "Connect to the border gateway nodes")
        self.assertEqual(connect["vars"]["bgw_node_limit"],
                         "{{ np_route_wait | default(0) | int + fab_real.nodes.configLease.stepTimeoutSeconds | int }}")


class NodeTasks(unittest.TestCase):
    def test_the_roles_that_reach_the_nodes(self):
        self.assertEqual(connecting_roles(), set(HOLDERS) | READERS)

    def test_a_holders_node_steps_are_all_inside_its_node_loop(self):
        for role, file_, t, _ in node_tasks():
            if role in HOLDERS:
                with self.subTest(role=role, file=file_, task=t.get("name")):
                    self.assertIn(file_, {HOLDERS[role][1]} | NODE_INCLUDES.get(role, set()))

    def test_every_node_task_runs_a_module_through_the_become_command(self):
        found = 0
        for role, file_, t, inh in node_tasks():
            found += 1
            with self.subTest(role=role, file=file_, task=t.get("name")):
                mods = [k for k in t if k.startswith("ansible.") or k in ("raw", "script", "shell", "command")]
                self.assertEqual(len(mods), 1, mods)
                self.assertIn(mods[0], MODULES)
                self.assertIsNot(inh.get("become", True), False)
                self.assertNotIn("async", inh)
                self.assertFalse([k for k in (inh.get("vars") or {}) if OWN_SETTINGS.match(k)
                                  and not (role in HOLDERS and k == "ansible_become_exe")])
                if mods[0] == "ansible.builtin.copy":
                    self.assertNotIn("remote_src", t[mods[0]])
        self.assertGreater(found, 60)

    def test_node_commands_start_nothing_outside_their_process_group(self):
        units = set()
        for role, file_, t, _ in node_tasks():
            body = t.get("ansible.builtin.shell") or t.get("ansible.builtin.command")
            if body is None:
                continue
            text = body if isinstance(body, str) else str(body.get("cmd") or body.get("argv") or "")
            text = "\n".join(re.sub(r"\s#.*$", "", line) for line in text.splitlines())   # comments aside
            with self.subTest(role=role, file=file_, task=t.get("name")):
                self.assertIsNone(ESCAPE.search(text), text)
                if UNIT_ACTION.search(text):
                    units.add((role, t["name"]))
        self.assertEqual(units, UNIT_ACTIONS_ALLOWED)

    def test_the_patterns_catch_what_they_are_for(self):
        for bad in ("nohup x", "setsid x", "systemd-run x", "sleep 9 &", "(x &)", "x & # bg", "a &\nb"):
            self.assertIsNotNone(ESCAPE.search(bad), bad)
        for ok in ("a && b", "a >&2", "x 2>&1", "a &>/dev/null", "[[ $o != *%* ]] && b", "a || b &&\n c"):
            self.assertIsNone(ESCAPE.search(ok), ok)
        self.assertIsNotNone(UNIT_ACTION.search('systemctl disable --now "$u"'))
        self.assertIsNone(UNIT_ACTION.search("systemctl -q is-active frr"))


class Numbers(unittest.TestCase):
    def test_the_node_ends_a_holders_step_by_the_steps_own_limit(self):
        # TERM at the step's limit less 5 s, KILL 5 s later: the commands end on the node no later than the job
        # stops waiting (plus the connection's set-up before the module started), inside the lease's margin
        lease = load(os.path.join(COMMON, "vars/fabric_lease.yml"))
        term, kill = 5, 5
        self.assertIn("-k %d " % kill, lease["fab_lease_node_become"])
        self.assertIn("- %d," % term, lease["fab_lease_node_become"])
        self.assertLessEqual(kill - term, 0)
        # the provision applies keep their own limit inside it (TERM 15 s before the step's, KILL 10 s later), so their
        # own rc 124 comes first
        for role, node_file in (("hybridfabric_provision", "node_root.yml"), ("cloudgateway_provision", "node_gateway.yml")):
            for t in walk(load(os.path.join(ROLES, role, "tasks", node_file))):
                cmd = str(t.get("ansible.builtin.command") or "")
                if "shc-fabric-apply" in cmd and cmd.strip().endswith(" up"):
                    self.assertTrue(cmd.startswith("timeout -k 10 {{ [fab_lease_step_timeout | int - 15, 15] | max }} "))
        self.assertLess(15 - 10, term + 1)


FAKE_SUDO = """#!/bin/sh
# sudo for the tests: the become command runs on this machine as this user
while [ $# -gt 0 ]; do case "$1" in -H|-S|-n) shift ;; *) break ;; esac; done
exec "$@"
"""


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class OnANode(unittest.TestCase):
    """The become command as bgw_connect.yml and the holders build it, on this machine (a stand-in node reached with
    the local connection, sudo replaced on PATH), inside a holder's node loop with the real lease files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shc-node-bound-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "bin"))
        with open(os.path.join(self.tmp, "bin/sudo"), "w") as f:
            f.write(FAKE_SUDO)
        os.chmod(os.path.join(self.tmp, "bin/sudo"), 0o755)
        shutil.copytree(os.path.join(COMMON, "tasks"), os.path.join(self.tmp, "common/tasks"))
        shutil.copytree(os.path.join(COMMON, "vars"), os.path.join(self.tmp, "common/vars"))
        self.real = load(os.path.join(COMMON, "vars/fabric_realization.yml"))["fabric_realization_defaults"]

    def run_loop(self, step, left, step_timeout=180):
        h = add_host()
        node = {"name": "bgw-1", "ansible_connection": "local", "ansible_become": True,
                "ansible_become_exe": h["ansible_become_exe"], "ansible_become_flags": "",
                "ansible_python_interpreter": sys.executable}
        real = dict(self.real, nodes=dict(self.real["nodes"], configLease=dict(self.real["nodes"]["configLease"],
                                                                               stepTimeoutSeconds=step_timeout)))
        out = os.path.join(self.tmp, "out.json")
        node_file = os.path.join(self.tmp, "node.yml")
        with open(node_file, "w") as f:
            yaml.safe_dump([dict(step, name="bgw-1: the step", delegate_to="bgw-1", register="_step")], f)
        play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                 "tasks": [
                     # facts of the job's host, as the jobs set them (a delegated step does not see them directly)
                     {"ansible.builtin.set_fact": {"fab_real": real, "fab_namespace": "sovereign-cloud"}},
                     {"ansible.builtin.include_vars": os.path.join(self.tmp, "common/vars/fabric_lease.yml")},
                     {"ansible.builtin.add_host": node},
                     {"ansible.builtin.set_fact": {"fab_lease": {"namespace": "sovereign-cloud", "fabric": "lab",
                                                                 "holder": "A",
                                                                 "until": "{{ now().timestamp() + %d }}" % left}}},
                     {"name": "Configure the nodes, one at a time", "timeout": "{{ fab_lease_step_timeout }}",
                      "vars": {"ansible_become_exe": "{{ hostvars[inventory_hostname].fab_lease_node_become }}"},
                      "block": [{"name": "the node loop", "ansible.builtin.include_tasks": node_file, "timeout": 0}],
                      "rescue": [{"ansible.builtin.include_tasks": os.path.join(self.tmp, "common/tasks/fabric_lease_keep.yml"),
                                  "vars": {"fab_lease_keep_raise": False}, "timeout": 0}]},
                     {"ansible.builtin.copy": {"dest": out, "content": "{{ {'step': _step | default({}), "
                                               "'kept': fab_lease_keep | default(false), "
                                               "'why': fab_lease_keep_msg | default('')} | to_json }}"}}]}]
        with open(os.path.join(self.tmp, "play.yml"), "w") as f:
            yaml.safe_dump(play, f)
        env = dict(os.environ, PATH=os.path.join(self.tmp, "bin") + os.pathsep + os.environ["PATH"],
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_REMOTE_TMP=os.path.join(self.tmp, "rt"), ANSIBLE_LOCAL_TEMP=os.path.join(self.tmp, "lt"),
                   ANSIBLE_CONFIG=os.path.join(self.tmp, "none.cfg"))
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml"], cwd=self.tmp, env=env,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-2000:])
        import json
        with open(out) as f:
            return json.load(f)

    # the step prints the limit timeout(1) runs it under: the nearest ancestor that is timeout
    SHOW = {"ansible.builtin.shell": textwrap.dedent("""\
        p=$PPID
        while [ "$p" -gt 1 ]; do
          a=$(tr '\\0' ' ' < /proc/$p/cmdline)
          case "$a" in /bin/sh\\ *|sh\\ *) ;; *timeout\\ -k\\ *) echo "$a" | sed -E 's/.*timeout -k ([0-9]+) ([0-9]+) .*/\\1 \\2/'; exit 0 ;; esac
          p=$(awk '{print $4}' /proc/$p/stat)
        done
        echo none""")}

    def test_the_limit_on_the_node_follows_the_lease(self):
        self.assertEqual(self.run_loop(self.SHOW, 600)["step"]["stdout"], "5 175")
        kill, term = self.run_loop(self.SHOW, 150)["step"]["stdout"].split()
        self.assertEqual(kill, "5")
        self.assertIn(int(term), (84, 85))      # 150 - 60 - 5, the template runs a moment later

    def test_a_step_that_hangs_ends_on_the_node_at_its_limit_and_keeps_the_lease(self):
        # a step limit of 12 s: the node ends the step at 7 s, before the job would stop waiting, as when the job can
        # no longer reach the node; the command's background child goes with it (the same process group)
        marker = os.path.join(self.tmp, "finished")
        tag = "shc-node-bound-%d" % os.getpid()
        step = {"ansible.builtin.shell": "sh -c 'sleep 40; touch %s' %s & sleep 40; touch %s" % (marker, tag, marker)}
        out = self.run_loop(dict(step), 600, step_timeout=12)
        self.assertTrue(out["kept"], out)
        self.assertIn("the node ended the step at its time limit (rc 124)", out["why"])
        running = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    running.append(f.read().replace(b"\0", b" ").decode(errors="replace"))
            except OSError:
                pass
        self.assertFalse([a for a in running if tag in a])
        self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()

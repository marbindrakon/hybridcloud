"""After the fabric's teardown nothing of the fabric stays enabled or running on a border gateway node.

hybridfabric_teardown node_root_down.yml: every FRR daemon the fabric turns on goes off again (bgpd, isisd and bfdd),
the IPv6 egress guard's timer is stopped and disabled and its files removed, and, as the last step of the root's and
of every gateway's teardown (node_programs_down.yml), once no instance of the fabric is left on the node the root unit
is stopped and disabled and the apply script and the unit templates are removed. While a root or gateway instance is
left (its configuration, a gateway unit running, failed or enabled) all of that stays: a gateway's own teardown needs
the script, and a gateway unit requires the root unit. A second run changes nothing.

The scripts run in bash with systemctl replaced by a stub that answers from files and logs what it was asked, and
the node's paths moved under a temporary root."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
RD = os.path.join(ROLES, "hybridfabric_teardown", "tasks", "node_root_down.yml")
PD = os.path.join(ROLES, "hybridfabric_teardown", "tasks", "node_programs_down.yml")
GD = os.path.join(ROLES, "cloudgateway_teardown", "tasks", "node_gateway_down.yml")
RP = os.path.join(ROLES, "hybridfabric_provision", "tasks", "node_root.yml")
P = "{{ bgw_node.name }}: "
T_PROGRAMS = P + "once no instance is left, remove the fabric's programs and units"
T_EGRESS6 = P + "remove the IPv6 egress guard"

# systemctl: list-units prints $STUB/list-units; is-active / is-failed / is-enabled <unit> succeed when
# $STUB/<verb>.<unit> exists; disable --now / stop / disable remove the matching state files; everything is logged
SYSTEMCTL = textwrap.dedent("""\
    #!/bin/bash
    echo "$*" >> "$STUB/calls"
    [ "$1" = -q ] && shift
    case "$1" in
      list-units) cat "$STUB/list-units" 2>/dev/null ;;
      is-active|is-failed|is-enabled) [ -e "$STUB/$1.$2" ]; exit ;;
      disable) if [ "$2" = --now ]; then rm -f "$STUB/is-enabled.$3" "$STUB/is-active.$3"; else rm -f "$STUB/is-enabled.$2"; fi ;;
      stop) rm -f "$STUB/is-active.$2" ;;
      reset-failed) rm -f "$STUB/is-failed.$2" ;;
    esac
    exit 0
    """)

INSTALLED = ["usr/local/sbin/shc-fabric-apply", "etc/systemd/system/shc-fabric-apply@.service",
             "etc/systemd/system/shc-gw@.service", "etc/systemd/system/shc-core-guard@.service",
             "etc/systemd/system/shc-core-guard@.timer", "etc/systemd/system/frr.service.d/50-shc-reload.conf"]


def shell(path, name):
    sh = by_name(load(path), name)["ansible.builtin.shell"]
    return sh["cmd"] if isinstance(sh, dict) else sh


class _Node:
    def __init__(self, test, script):
        self.d = tempfile.mkdtemp(prefix="shc-teardown-")
        test.addCleanup(shutil.rmtree, self.d, True)
        self.bin, self.stub = os.path.join(self.d, "bin"), os.path.join(self.d, "stub")
        for x in (self.bin, self.stub, self.p("etc/systemd/system"), self.p("etc/shc-fabric"), self.p("usr/local/sbin"),
                  self.p("run")):
            os.makedirs(x, exist_ok=True)
        with open(os.path.join(self.bin, "systemctl"), "w") as f:
            f.write(SYSTEMCTL)
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        # every node path under the temporary root (the script names them only after a blank, '=' or '"')
        self.script = re.sub(r'(?<=[ ="])/(etc|usr|run)/', self.d + r"/\1/", script)

    def p(self, rel):
        return os.path.join(self.d, rel)

    def touch(self, *rels):
        for rel in rels:
            os.makedirs(os.path.dirname(self.p(rel)), exist_ok=True)
            open(self.p(rel), "w").close()

    def write(self, rel, text):
        with open(self.p(rel), "w") as f:
            f.write(text)

    def state(self, *names):
        for n in names:
            open(os.path.join(self.stub, n), "w").close()

    def run(self):
        try:
            os.remove(os.path.join(self.stub, "calls"))
        except FileNotFoundError:
            pass
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], STUB=self.stub)
        p = subprocess.run(["bash", "-s"], input=self.script, capture_output=True, text=True, env=env, timeout=30)
        try:
            with open(os.path.join(self.stub, "calls")) as f:
                calls = f.read().splitlines()
        except FileNotFoundError:
            calls = []
        return p.returncode, p.stdout, p.stderr, calls

    def exists(self, rel):
        return os.path.exists(self.p(rel))


class ProgramsDown(unittest.TestCase):
    def node(self):
        n = _Node(self, shell(PD, T_PROGRAMS))
        n.touch(*INSTALLED)
        # as the root teardown leaves it today: the root unit disabled but still "active (exited)"
        n.state("is-active.shc-fabric-apply@root.service")
        return n

    def test_the_last_instance_gone_leaves_nothing(self):
        n = self.node()
        rc, out, err, calls = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=yes"), err)
        self.assertIn("stop shc-fabric-apply@root.service", calls)
        self.assertIn("daemon-reload", calls)
        for rel in INSTALLED + ["etc/shc-fabric"]:
            self.assertFalse(n.exists(rel), rel)
        # a second run is a no-op: nothing stopped, nothing reloaded
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=no"))
        self.assertEqual([c for c in calls if not c.startswith(("-q is-", "list-units"))], [])

    def test_the_frr_drop_in_goes_and_another_one_stays(self):
        # the fabric's drop-in of the frr unit (the reload's exit status ignored) goes with its directory; a drop-in
        # of someone else keeps the directory
        n = self.node()
        rc, out, _, calls = n.run()
        self.assertFalse(n.exists("etc/systemd/system/frr.service.d"))
        n = self.node()
        n.touch("etc/systemd/system/frr.service.d/90-local.conf")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=yes"))
        self.assertFalse(n.exists("etc/systemd/system/frr.service.d/50-shc-reload.conf"))
        self.assertTrue(n.exists("etc/systemd/system/frr.service.d/90-local.conf"))
        self.assertIn("daemon-reload", calls)

    def test_a_failed_or_enabled_root_unit_goes_too(self):
        n = self.node()
        os.remove(os.path.join(n.stub, "is-active.shc-fabric-apply@root.service"))
        n.state("is-failed.shc-fabric-apply@root.service", "is-enabled.shc-fabric-apply@root.service")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=yes"))
        self.assertIn("reset-failed shc-fabric-apply@root.service", calls)
        self.assertIn("disable shc-fabric-apply@root.service", calls)

    def test_an_instance_left_keeps_everything(self):
        cases = {
            "the root's configuration": lambda n: n.touch("etc/shc-fabric/root.json"),
            "a gateway's configuration": lambda n: n.touch("etc/shc-fabric/gw-site-b.json"),
            "a gateway unit running": lambda n: n.write("stub/list-units",
                                                        "shc-gw@site-b.service loaded active running gateway site-b\n"),
            "a gateway unit enabled": lambda n: n.touch("etc/systemd/system/multi-user.target.wants/shc-gw@site-b.service"),
        }
        for what, make in cases.items():
            with self.subTest(what):
                n = self.node()
                make(n)
                rc, out, _, calls = n.run()
                self.assertEqual(rc, 0)
                self.assertTrue(out.startswith("kept: instances left on the node: "), out)
                self.assertEqual([c for c in calls if not c.startswith("list-units")], [])
                for rel in INSTALLED:
                    self.assertTrue(n.exists(rel), rel)
        n = self.node()
        n.touch("etc/shc-fabric/root.json", "etc/shc-fabric/gw-site-b.json")
        _, out, _, _ = n.run()
        self.assertEqual(out.strip(), "kept: instances left on the node: root gw-site-b")

    def test_other_files_keep_the_directory(self):
        n = self.node()
        n.touch("etc/shc-fabric/notes.txt")
        rc, out, _, _ = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=yes"))
        self.assertTrue(n.exists("etc/shc-fabric/notes.txt"))
        self.assertFalse(n.exists("usr/local/sbin/shc-fabric-apply"))

    def test_bash_syntax_and_no_jinja(self):
        sh = shell(PD, T_PROGRAMS)
        self.assertNotIn("{{", sh)
        self.assertNotIn("{%", sh)
        self.assertNotIn("{#", sh)
        p = subprocess.run(["bash", "-n"], input=sh, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)


class EgressGuardDown(unittest.TestCase):
    FILES = ["etc/systemd/system/shc-egress6-guard.timer", "etc/systemd/system/shc-egress6-guard.service",
             "usr/local/sbin/shc-egress6-guard", "run/shc-egress6-guard.shut"]

    def test_removed_then_a_no_op(self):
        n = _Node(self, shell(RD, T_EGRESS6))
        n.touch(*self.FILES)
        n.state("is-enabled.shc-egress6-guard.timer", "is-active.shc-egress6-guard.timer")
        rc, out, err, calls = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=yes"), err)
        self.assertIn("disable --now shc-egress6-guard.timer", calls)
        self.assertIn("daemon-reload", calls)
        for rel in self.FILES:
            self.assertFalse(n.exists(rel), rel)
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.strip()), (0, "changed=no"))
        self.assertNotIn("daemon-reload", calls)

    def test_same_steps_as_the_fabric_job_without_ipv6_egress(self):
        # the fabric job removes the guard the same way when IPv6 egress goes (node_root.yml)
        self.assertEqual(shell(RD, T_EGRESS6), shell(RP, P + "no IPv6 egress guard without IPv6 egress"))


class Wiring(unittest.TestCase):
    def names(self, path):
        return [t.get("name", "") for t in load(path)]

    def test_root_teardown(self):
        tasks = load(RD)
        names = self.names(RD)
        self.assertEqual(by_name(tasks, P + "FRR daemons off")["loop"], ["bgpd", "isisd", "bfdd"])
        # the guard comes off before the root instance does; the programs go last, after the root's configuration
        self.assertLess(names.index(T_EGRESS6), names.index(P + "remove the root instance (core bridge, loopback)"))
        self.assertEqual(names[-2:], [P + "remove the root instance config", P + "the fabric's programs and units"])
        self.assertEqual(tasks[-1]["ansible.builtin.include_tasks"], "node_programs_down.yml")
        # the script stays until the last step: no earlier step removes it
        for t in tasks[:-1]:
            self.assertNotIn("/usr/local/sbin/shc-fabric-apply\n", str(t.get("ansible.builtin.shell", "")))

    def test_gateway_teardown(self):
        tasks = load(GD)
        self.assertEqual(self.names(GD)[-2:], [P + "remove the instance's configuration", P + "the fabric's programs and units"])
        self.assertEqual(tasks[-1]["ansible.builtin.include_tasks"],
                         "{{ role_path }}/../hybridfabric_teardown/tasks/node_programs_down.yml")
        self.assertTrue(os.path.exists(os.path.join(ROLES, "cloudgateway_teardown", "..", "hybridfabric_teardown", "tasks",
                                                    "node_programs_down.yml")))

    def test_every_daemon_the_fabric_turns_on_goes_off(self):
        on = by_name(load(RP), P + "FRR daemons")["loop"]
        for d in re.findall(r"'(\w+d)'", on):
            self.assertIn(d, by_name(load(RD), P + "FRR daemons off")["loop"])

    def test_programs_step_is_delegated_and_reports_changes(self):
        t = by_name(load(PD), T_PROGRAMS)
        self.assertEqual(t["delegate_to"], "{{ bgw_node.name }}")
        self.assertEqual(t["args"], {"executable": "/bin/bash"})
        self.assertEqual(t["changed_when"], "'changed=yes' in _ft_programs.stdout")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AnsibleView(unittest.TestCase):
    def test_scripts_reach_the_node_as_written(self):
        for path, name in ((PD, T_PROGRAMS), (RD, T_EGRESS6)):
            sh = shell(path, name)
            got = run_play([{"ansible.builtin.set_fact": {"rendered": sh}}, write_out("rendered")], {})
            self.assertEqual(got.rstrip("\n"), sh.rstrip("\n"), name)


if __name__ == "__main__":
    unittest.main()

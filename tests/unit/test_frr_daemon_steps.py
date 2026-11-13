"""The product's shell of the root's two FRR daemon steps (node_root.yml), run as written.

"start an enabled FRR daemon that does not run (reload of the frr unit)" and "every FRR daemon the file enables runs"
are taken from the task file and rendered with their own variables (the daemon list, and the wait between the checks
after the reload set to 0); only the pid directory /var/run/frr is moved to a temporary one and `systemctl` is a fake
on PATH: `is-active frr` answers from a state file, `reload frr` is logged and (unless told not to) writes a pid file
for each daemon in <dir>/starts, `show -p ExecReload` prints a status. Review V5: the play harness of
test_frr_reload_due.py replaces these bodies; this runs them."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import jinja2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT, by_name, load  # noqa: E402

HF = os.path.join(ROOT, "eda/rulebooks/roles/hybridfabric_provision/tasks/node_root.yml")
P = "{{ bgw_node.name }}: "
RELOAD = P + "start an enabled FRR daemon that does not run (reload of the frr unit)"
CHECK = P + "every FRR daemon the file enables runs"

SYSTEMCTL = textwrap.dedent("""\
    #!/bin/bash
    echo "$*" >> "$FAKE/calls"
    [ "$1" = -q ] && shift
    case "$1" in
      is-active) [ "$(cat "$FAKE/state")" = active ]; exit ;;
      reload)
        for d in $(cat "$FAKE/starts" 2>/dev/null); do echo "$LIVE" > "$PIDS/$d.pid"; done
        exit 0 ;;
      show) echo "{ path=/usr/libexec/frr/frrinit.sh ; status=0/SUCCESS }"; exit 0 ;;
    esac
    exit 0
    """)


def body(name, want="bgpd isisd bfdd", wait=0):
    t = by_name(load(HF), name)
    sh = t["ansible.builtin.shell"]
    v = dict(t.get("vars", {}), _want=want)
    if "_wait" in v:
        assert v["_wait"] == 2, v           # the product's default
        v["_wait"] = wait
    return jinja2.Environment().from_string(sh).render(**v)


class DaemonSteps(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="shc-daemons-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.bin, self.pids = os.path.join(self.d, "bin"), os.path.join(self.d, "run")
        os.makedirs(self.bin)
        os.makedirs(self.pids)
        with open(os.path.join(self.bin, "systemctl"), "w") as f:
            f.write(SYSTEMCTL)
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        self.put("state", "active")
        # a live process for the daemons that run: this test's own pid
        self.live = os.getpid()

    def put(self, name, text):
        with open(os.path.join(self.d, name), "w") as f:
            f.write(text)

    def alive(self, *daemons):
        for dmn in daemons:
            with open(os.path.join(self.pids, dmn + ".pid"), "w") as f:
                f.write("%d\n" % self.live)

    def run_step(self, name):
        sh = body(name).replace("/var/run/frr/", self.pids + "/")
        self.assertNotIn("/var/run/frr", sh)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE=self.d, PIDS=self.pids, LIVE=str(self.live))
        p = subprocess.run(["bash", "-c", sh], capture_output=True, text=True, env=env, timeout=60)
        calls = open(os.path.join(self.d, "calls")).read().split("\n") if os.path.exists(os.path.join(self.d, "calls")) else []
        return p.returncode, p.stdout.strip().splitlines(), [c for c in calls if c]

    def test_all_alive_no_reload(self):
        self.alive("bgpd", "isisd", "bfdd")
        rc, out, calls = self.run_step(RELOAD)
        self.assertEqual((rc, out), (0, []))
        self.assertEqual(calls, ["-q is-active frr"])

    def test_one_missing_one_reload_then_up(self):
        self.alive("bgpd", "isisd")
        self.put("starts", "bfdd")
        rc, out, calls = self.run_step(RELOAD)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out[0], "not running: bfdd")
        self.assertTrue(out[1].startswith("reloaded status=0/SUCCESS"), out)
        self.assertEqual([c for c in calls if c.startswith("reload")], ["reload frr"])

    def test_a_stale_pid_counts_as_not_running(self):
        self.alive("bgpd", "isisd")
        with open(os.path.join(self.pids, "bfdd.pid"), "w") as f:
            f.write("999999999\n")
        self.put("starts", "bfdd")
        rc, out, calls = self.run_step(RELOAD)
        self.assertEqual((rc, out[0]), (0, "not running: bfdd"))

    def test_never_up_fails_named_after_the_wait(self):
        self.alive("bgpd", "isisd")
        rc, out, calls = self.run_step(RELOAD)
        self.assertEqual(rc, 1)
        self.assertEqual(out[-1], "still not running after the reload of the frr unit: bfdd")
        self.assertEqual([c for c in calls if c.startswith("reload")], ["reload frr"])

    def test_inactive_or_activating_does_nothing(self):
        for state in ("inactive", "activating", "reloading"):
            self.put("state", state)
            if os.path.exists(os.path.join(self.d, "calls")):
                os.remove(os.path.join(self.d, "calls"))
            rc, out, calls = self.run_step(RELOAD)
            self.assertEqual((rc, out, calls), (0, ["frr is not active"], ["-q is-active frr"]), state)

    def test_the_check_after_a_start(self):
        self.alive("bgpd", "isisd", "bfdd")
        self.assertEqual(self.run_step(CHECK)[:2], (0, []))
        os.remove(os.path.join(self.pids, "isisd.pid"))
        rc, out, calls = self.run_step(CHECK)
        self.assertEqual((rc, out), (1, ["not running: isisd"]))
        self.assertEqual(calls, [])             # read only

    def test_the_wait_is_the_tasks_own(self):
        t = by_name(load(HF), RELOAD)
        self.assertEqual(t["vars"]["_wait"], 2)
        self.assertIn("sleep {{ _wait }}", t["ansible.builtin.shell"])


if __name__ == "__main__":
    unittest.main()

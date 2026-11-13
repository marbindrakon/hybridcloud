"""A job that follows an interrupted one reloads FRR; a job with nothing pending does not.

FRR runs what its last start or reload loaded. A job that wrote the root's /etc/frr/frr.conf (or daemons) or a
gateway's frr.conf.shc and stopped before the reload (crash, failed reload, step timeout, lost lease) leaves a file
the next job finds unchanged. The node files keep the digest of what FRR last loaded (/etc/shc-fabric/root.frr-loaded,
gw-<gateway>.frr-loaded) and reload while the file differs from it.

The tests run the real steps of node_root.yml and node_gateway.yml, from the record of what was loaded to the record
after the reload, in ansible-playbook on localhost: the node's files live under a temporary root, the template writes
the content a test wants, and systemd is a stub that logs what it was asked to do and "loads" the files (the content
FRR then runs is kept next to them). A crash is the job ending after a given step. Skipped without ansible-playbook.

The record is written only when FRR holds the whole file (shc-frr-check, run for real here against a stand-in for
frr-reload.py --test that compares the file with what the stub loaded): a line the stub's FRR never takes (<root>/never)
fails the job and leaves the reload due; a second start or reload that leaves the same line is recorded and reported.
"""
import copy
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, load, run_play, write_out  # noqa: E402

HF = os.path.join(ROOT, "eda/rulebooks/roles/hybridfabric_provision/tasks/node_root.yml")
CHECK = os.path.join(ROOT, "eda/common/files/bgw/shc-frr-check")
# frr-reload.py --test as the check runs it: the lines of the file that the stub's FRR does not run
FAKE_TEST = """#!%s
import sys
conf, running = sys.argv[-1], open(%r).read().splitlines()
left = [x for x in open(conf).read().splitlines() if x.strip() and x not in running]
if left:
    print("\\nLines To Add\\n============")
    print("\\n".join(left))
"""
CG = os.path.join(ROOT, "eda/rulebooks/roles/cloudgateway_provision/tasks/node_gateway.yml")
PFX = "{{ bgw_node.name }}: "

# the root's steps in the file's order (not adjacent: the removal step and the apply run between the daemons and the file)
ROOT_STEPS = ["what FRR last loaded (a node without the record: the files as they are)",
              "FRR daemons",
              "the lease lasts for the reload of the frr unit",
              "start an enabled FRR daemon that does not run (reload of the frr unit)",
              "FRR configuration",
              "is a reload due (the files differ from what FRR last loaded)",
              "is FRR running",
              "what FRR still lacks of its files",
              "the lease lasts for the FRR start",
              "start FRR (enabled at boot)",
              "every FRR daemon the file enables runs",
              "the lease lasts for the FRR reload",
              "reload the FRR configuration",
              "the reload's errors",
              "record what FRR loaded",
              "FRR holds the whole configuration",
              "a reload error FRR did not need",
              "lines FRR does not take"]
GW_STEPS = ["what the instance last loaded (without the record: the file as it is)",
            "FRR configuration",
            "is a reload due (the file differs from what the instance last loaded)",
            "is the gateway instance running",
            "what the instance still lacks of its file",
            "the lease lasts for the instance's start",
            "start the gateway instance (enabled at boot)",
            "the lease lasts for the FRR reload",
            "reload the FRR configuration",
            "record what the instance loaded",
            "a reload the record made due changed the node",
            "the instance holds the whole configuration",
            "lines the instance does not take"]


def section(path, steps):
    """The node file's steps of these names, which must be in this order (the gateway's: adjacent)."""
    tasks = load(path)
    names = [t.get("name", "").replace(PFX, "") for t in tasks]
    idx = [names.index(n) for n in steps]
    assert idx == sorted(idx), [names[i] for i in sorted(idx)]
    if path == CG:
        assert idx == list(range(idx[0], idx[0] + len(steps))), idx
    return [tasks[i] for i in idx]


class _Node:
    """A border gateway node's FRR files under a temporary root, and the FRR stub's state."""

    def __init__(self, test, gateway):
        self.r = tempfile.mkdtemp(prefix="shc-frr-due-")
        test.addCleanup(shutil.rmtree, self.r, True)
        self.gateway = gateway
        conf_dir = "etc/frr/gw-site1" if gateway else "etc/frr"
        os.makedirs(os.path.join(self.r, conf_dir))
        os.makedirs(os.path.join(self.r, "etc/shc-fabric"))
        self.conf = os.path.join(self.r, conf_dir, "frr.conf.shc" if gateway else "frr.conf")
        self.daemons = None if gateway else os.path.join(self.r, "etc/frr/daemons")
        self.record = os.path.join(self.r, "etc/shc-fabric", "gw-site1.frr-loaded" if gateway else "root.frr-loaded")

    def p(self, name):
        return os.path.join(self.r, name)

    def put(self, name, text):
        with open(self.p(name) if not os.path.isabs(name) else name, "w") as f:
            f.write(text)

    def get(self, name):
        path = self.p(name) if not os.path.isabs(name) else name
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    def files(self):
        """What FRR loads from: the configuration (and the root's daemons file)."""
        return [self.conf] + ([self.daemons] if self.daemons else [])

    def running(self, conf, daemons="bgpd=yes\nisisd=yes\n", active=True):
        """A node whose FRR is running conf (and daemons), loaded from the files as they are."""
        self.put(self.conf, conf)
        if self.daemons:
            self.put(self.daemons, daemons)
        self.put("lconf", conf)
        if self.daemons:
            self.put("dloaded", daemons)
        self.put("loaded", "".join(self.get(f) for f in self.files()))
        self.put("state", "active\n" if active else "inactive\n")

    def runs(self):
        """What FRR runs: the files' content at its last start or reload."""
        return self.get("loaded")


def stub(task, node):
    """The task for localhost: paths under the node's root, the template writes {{ desired }}, systemd is a stub that
    keeps the unit's state in <root>/state (active, inactive, failed, activating, reloading) and a queued job in
    <root>/job.

    The start task behaves as the systemd module does: "started" leaves an active or activating unit alone (the module
    counts both as running), runs `systemctl start` otherwise, which on a reloading unit only waits for that reload (no
    new load) and on an inactive or failed one starts it (it loads the files, or fails with <root>/start_rc 1).
    "restarted" loads the files. A state that renders as omit asks for nothing (ansible-core 2.16 renders the
    placeholder, 2.19 and later ""). The reload loads the files, or fails with <root>/reload_rc 1."""
    t = copy.deepcopy(task)
    t.pop("delegate_to", None)
    t.pop("no_log", None)
    never = node.p("never")
    # what FRR runs (<root>/loaded) = the configuration it took (<root>/lconf: the file less what it never takes) and
    # the daemons watchfrr runs (<root>/dloaded, the root only). frr-reload.py takes the configuration only; a start,
    # restart or the unit's reload (frrinit.sh, a new watchfrr) the daemons file too
    conf_cmd = "cat {conf} | (if [ -s {never} ]; then grep -vxF -f {never} || true; else cat; fi) > {lconf}".format(
        conf=node.conf, never=never, lconf=node.p("lconf"))
    join_cmd = "{{ cat {lconf}; cat {dl} 2>/dev/null || true; }} > {loaded}".format(lconf=node.p("lconf"), dl=node.p("dloaded"),
                                                                loaded=node.p("loaded"))
    # a daemon named in <root>/daemon_dead never starts
    daemons_cmd = ("(if [ -s {dead} ]; then grep -vxF -f {dead} {d} || true; else cat {d}; fi) > {dl}; ".format(
        dead=node.p("daemon_dead"), d=node.daemons, dl=node.p("dloaded"))) if node.daemons else ""
    load_cmd = conf_cmd + "; " + daemons_cmd + join_cmd
    reload_cmd = conf_cmd + "; " + join_cmd
    # the daemons of _want that watchfrr does not run (the root's daemons checks)
    missing_fn = ("missing() {{ m=''; for d in {{{{ _want }}}}; do grep -qx \"$d=yes\" {dl} 2>/dev/null || m=\"$m $d\"; "
                  "done; echo \"${{m# }}\"; }}; ").format(dl=node.p("dloaded"))
    fake = node.p("frr-reload-test.py")
    if not os.path.exists(fake):
        node.put(fake, FAKE_TEST % (sys.executable, node.p("loaded")))
        os.chmod(fake, 0o755)
    swap = lambda s: s.replace("/etc/frr/", node.r + "/etc/frr/").replace("/etc/shc-fabric/", node.r + "/etc/shc-fabric/")  # noqa: E731
    if str(t.get("ansible.builtin.include_tasks", "")).endswith("fabric_lease_ensure.yml"):
        return {"name": t["name"], "ansible.builtin.debug": {"msg": "the lease lasts"}}
    # FRR's pending difference (common/tasks/frr_pending.yml), as the check computes it: the file's lines FRR does not
    # run, less those its note holds (a hand change on top: <root>/pending). Only while FRR runs
    if str(t.get("ansible.builtin.include_tasks", "")).endswith("frr_pending.yml"):
        note = os.path.join(node.r, "etc/shc-fabric", ("gw-site1" if node.gateway else "root") + ".frr-unapplied")
        rd = "(lookup('ansible.builtin.file', '%s', errors='ignore') | default('', true)).splitlines()"
        expr = ("{{ ((%s | reject('in', %s) | reject('in', %s) | select | list) + (%s | select | list)) | unique | list"
                " if (%s) else [] }}") % (rd % node.conf, rd % node.p("loaded"), rd % note, rd % node.p("pending"),
                                          "frr_pending_when | bool")
        return {"name": t["name"], "vars": t.get("vars", {}), "ansible.builtin.set_fact": {"frr_pending_lines": expr}}
    if "ansible.builtin.set_fact" in t or "ansible.builtin.fail" in t or "ansible.builtin.debug" in t:
        return t
    # the root's reload of the unit when a daemon the file enables does not run (frrinit.sh reload through systemd: a new
    # watchfrr with the daemons file's list, and frr-reload.py of the file on disk): logged as unit-reloaded with the
    # configuration on disk then (<root>/unit-reload-saw); with <root>/unit_reload_fails the daemon still does not start
    if t["name"].endswith("start an enabled FRR daemon that does not run (reload of the frr unit)"):
        t["ansible.builtin.shell"] = missing_fn + (
            "[ \"$(cat {state})\" = active ] || {{ echo 'frr is not active'; exit 0; }}; m=$(missing); [ -z \"$m\" ] && exit 0; "
            "echo \"not running: $m\"; echo unit-reloaded >> {log}; cat {conf} > {saw}; "
            "if [ ! -e {fails} ]; then {load}; fi; echo reloaded status=0/SUCCESS; m=$(missing); [ -z \"$m\" ] && exit 0; "
            "echo \"still not running after the reload of the frr unit: $m\"; exit 1").format(
                state=node.p("state"), log=node.p("actions"), conf=node.conf, saw=node.p("unit-reload-saw"),
                fails=node.p("unit_reload_fails"), load=load_cmd)
        t.pop("args", None)
        return t
    if t["name"].endswith("every FRR daemon the file enables runs"):
        t["ansible.builtin.shell"] = missing_fn + 'm=$(missing); [ -z "$m" ] && exit 0; echo "not running: $m"; exit 1'
        t.pop("args", None)
        t["delay"] = 0
        t["retries"] = 1
        return t
    # the root's reload: frr-reload.py run by the job (exit status <root>/reload_rc; with <root>/reload_loads it loads
    # the files even when it exits non-zero; it prints <root>/reload_says)
    if "frr-reload.py --reload" in str(t.get("ansible.builtin.command", "")):
        assert "--rundir /var/run/frr /etc/frr/frr.conf" in t["ansible.builtin.command"], t
        assert t["failed_when"] is False, t
        t.pop("ansible.builtin.command")
        t["ansible.builtin.shell"] = (
            "echo reloaded >> {log}; rc=$(cat {rc} 2>/dev/null || echo 0); cat {says} 2>/dev/null; "
            "if [ \"$rc\" = 0 ] || [ -e {loads} ]; then {load}; fi; exit $rc".format(
                log=node.p("actions"), rc=node.p("reload_rc"), says=node.p("reload_says"), loads=node.p("reload_loads"),
                load=reload_cmd))
        return t
    if "ansible.builtin.shell" in t:
        sh = swap(t["ansible.builtin.shell"])
        sh = re.sub(r'systemctl is-active "?\$?[\w@.{} -]+?"?\)', "cat %s)" % node.p("state"), sh)
        sh = re.sub(r'systemctl show -p Job --value "?\$?[\w@.{} -]+?"?\)', "cat %s 2>/dev/null)" % node.p("job"), sh)
        assert "systemctl" not in sh, sh
        sh = sh.replace("/usr/local/sbin/shc-frr-check", "%s %s --reload-script %s" % (sys.executable, CHECK, fake))
        t["ansible.builtin.shell"] = sh
    elif "ansible.builtin.lineinfile" in t:
        t["ansible.builtin.lineinfile"]["path"] = swap(t["ansible.builtin.lineinfile"]["path"])
    elif "ansible.builtin.template" in t:
        dest = swap(t.pop("ansible.builtin.template")["dest"])
        t["ansible.builtin.copy"] = {"dest": dest, "content": "{{ desired }}"}
    elif "ansible.builtin.copy" in t:
        # the root writes the configuration the run rendered once (_hf_frr_text)
        assert t["ansible.builtin.copy"]["content"] == "{{ _hf_frr_text }}", t
        t["ansible.builtin.copy"] = {"dest": swap(t["ansible.builtin.copy"]["dest"]), "content": "{{ desired }}"}
    elif "ansible.builtin.command" in t:
        assert "systemctl is-active" in t["ansible.builtin.command"], t
        t["ansible.builtin.command"] = "cat %s" % node.p("state")
    elif "ansible.builtin.systemd" in t:
        sd = t.pop("ansible.builtin.systemd")
        if sd["state"] == "reloaded":
            t["ansible.builtin.shell"] = (
                "echo reloaded >> {log}; rc=$(cat {rc} 2>/dev/null || echo 0); [ \"$rc\" = 0 ] || exit 1; "
                "{load}".format(log=node.p("actions"), rc=node.p("reload_rc"), load=load_cmd))
            if "delay" in t:
                t["delay"] = 0
        else:
            t.setdefault("vars", {})["_state"] = sd["state"]
            t["ansible.builtin.shell"] = (
                "s='{{{{ _state }}}}'; now=$(cat {state}); case \"$s\" in ''|*omit_place_holder*) exit 0 ;; esac; "
                "if [ \"$s\" = started ]; then case \"$now\" in active|activating) exit 0 ;; reloading) "
                "echo waited >> {log}; echo changed; exit 0 ;; esac; fi; "
                "echo \"$s\" >> {log}; [ \"$(cat {rc} 2>/dev/null || echo 0)\" = 0 ] || exit 1; "
                "{load}; echo active > {state}; echo changed".format(
                    log=node.p("actions"), load=load_cmd, state=node.p("state"),
                    rc=node.p("start_rc")))
            t["changed_when"] = "'changed' in _stub_out.stdout"
            t["register"] = t.get("register", "_stub_out")
            t["changed_when"] = "'changed' in %s.stdout" % t["register"]
    else:
        raise AssertionError("no stub for %s" % t.get("name"))
    return t


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class _Case(unittest.TestCase):
    GATEWAY = False

    def setUp(self):
        self.node = _Node(self, self.GATEWAY)
        self.steps = section(CG if self.GATEWAY else HF, GW_STEPS if self.GATEWAY else ROOT_STEPS)

    def job(self, desired, crash_after=None, reload_rc=0, start_rc=0, probe=None, **extra):
        """One job's FRR steps on the node; returns (what systemd was asked, whether the job failed), or with probe
        (an expression) also its value at the end."""
        n = self.node
        n.put("reload_rc", "%d\n" % reload_rc)
        n.put("start_rc", "%d\n" % start_rc)
        if os.path.exists(n.p("actions")):
            os.remove(n.p("actions"))
        tasks = [stub(t, n) for t in self.steps]
        if crash_after is not None:
            names = [t["name"].replace(PFX, "") for t in self.steps]
            tasks = tasks[:names.index(crash_after) + 1]
        variables = {"bgw_node": {"name": "bgw-1"}, "desired": desired}
        if self.GATEWAY:
            variables.update(fab_name="site1", _cg_daemons={"changed": False}, _cg_unit_changed=False,
                             _cg_files={"changed": False})
        else:
            variables.update(fab_real={"attachments": {"enabled": False}}, _hf_apply={"stdout": ""},
                             hf_circuit_passwords={})
        variables.update(extra)
        play = [{"name": "the node's FRR steps", "block": tasks,
                 "rescue": [{"ansible.builtin.set_fact": {"_job_failed": True,
                                                          "_job_msg": "{{ ansible_failed_result.msg | default('') }}"}}]},
                write_out("[_job_failed | default(false), %s]" % (probe or "none"))]
        failed, value = run_play(play, variables)
        actions = (n.get("actions") or "").split()
        return (actions, failed, value) if probe else (actions, failed)

    def loaded_digest_matches(self):
        import hashlib
        h = hashlib.sha256("".join(self.node.get(f) for f in self.node.files()).encode()).hexdigest()
        return (self.node.get(self.node.record) or "").strip() == h

    def want(self, conf):
        """What FRR should run once conf is loaded (the root also loads its daemons file)."""
        return conf + (self.node.get(self.node.daemons) if self.node.daemons else "")

    # ---- the cases, for the root and for a gateway ----

    def test_a_pending_difference_makes_the_reload_due(self):
        # files and record agree, but FRR's running configuration still lacks a line (a hand change): the job reloads
        self.node.running("conf A\n")
        self.assertEqual(self.job("conf A\n"), ([], False))
        self.node.put("pending", "neighbor X shutdown\n")
        actions, failed = self.job("conf A\n")
        self.assertFalse(failed)
        self.assertIn("reload", " ".join(actions))
        os.remove(self.node.p("pending"))
        self.assertEqual(self.job("conf A\n"), ([], False))

    def test_nothing_pending_reloads_nothing(self):
        self.node.running("conf A\n")
        self.assertEqual(self.job("conf A\n"), ([], False))      # the first run records what is there
        self.assertEqual(self.job("conf A\n"), ([], False))
        self.assertTrue(self.loaded_digest_matches())

    def test_first_run_after_the_upgrade_records_without_reloading(self):
        # a node that has run for months: no record yet, FRR runs the files, nothing changes
        self.node.running("conf A\n")
        self.assertIsNone(self.node.get(self.node.record))
        actions, failed = self.job("conf A\n")
        self.assertEqual((actions, failed), ([], False))
        self.assertTrue(self.loaded_digest_matches())

    def test_first_run_after_the_upgrade_with_a_change_reloads_once(self):
        self.node.running("conf A\n")
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_a_job_interrupted_between_write_and_reload_is_healed_by_the_next(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.assertEqual(self.job("conf B\n", crash_after="FRR configuration"), ([], False))
        self.assertEqual(self.node.runs(), self.want("conf A\n"))       # the file says B, FRR still runs A
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))   # the file is unchanged: the record says due
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_an_interruption_on_the_first_run_after_the_upgrade_is_healed(self):
        # the record is taken before the run writes the file, so even the first run's crash leaves the reload due
        self.node.running("conf A\n")
        self.job("conf B\n", crash_after="FRR configuration")
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))

    def test_a_failed_reload_is_retried_by_the_next_job(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        actions, failed = self.job("conf B\n", reload_rc=1)
        self.assertTrue(failed)
        self.assertEqual(set(actions), {"reloaded"})
        self.assertEqual(self.node.runs(), self.want("conf A\n"))
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_a_job_stopped_after_the_reload_reloads_once_more_then_nothing(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.job("conf B\n", crash_after="reload the FRR configuration")
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))   # a reload with nothing left to change
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_a_stopped_instance_is_started_and_recorded(self):
        self.node.running("conf A\n", active=False)
        self.assertEqual(self.job("conf B\n"), (["started"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))


class _States(_Case):
    """FRR caught between states: nothing may be recorded unless this job knows FRR runs the file (F3)."""

    def test_an_instance_still_starting_is_not_recorded(self):
        # activating: the start task leaves it (the module counts it as running), the reload needs active; the file
        # this job wrote may be newer than what that start read
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("state", "activating\n")
        self.assertEqual(self.job("conf B\n"), ([], False))
        self.assertEqual(self.node.runs(), self.want("conf A\n"))
        self.node.put("state", "active\n")
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))   # still due
        self.assertEqual(self.node.runs(), self.want("conf B\n"))

    def test_an_instance_reloading_on_its_own_is_not_recorded(self):
        # reloading: the module runs systemctl start, which only waits for that reload (changed, nothing new loaded)
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("state", "reloading\n")
        self.assertEqual(self.job("conf B\n"), (["waited"], False))
        self.node.put("state", "active\n")
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))

    def test_a_failed_start_is_not_recorded(self):
        self.node.running("conf A\n", active=False)
        self.job("conf A\n", start_rc=1)
        actions, failed = self.job("conf B\n", start_rc=1)
        self.assertTrue(failed)
        self.assertNotEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), (["started"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_a_job_queued_for_the_unit_is_not_recorded(self):
        # active, but a job for the unit is still queued (a restart of its own): what it will load is unknown
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("job", "4711\n")
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))
        os.remove(self.node.p("job"))
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))   # the record was not written: due once more
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_a_reload_only_the_record_made_due_is_reported_as_a_change(self):
        # the gate's "a re-run changes nothing" then sees the job that healed an interrupted one as a change, once
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.job("conf B\n", crash_after="FRR configuration")
        actions, failed, changed = self.job("conf B\n", probe=self.CHANGED)
        self.assertEqual((actions, failed, changed), (["reloaded"], False, True))
        actions, failed, changed = self.job("conf B\n", probe=self.CHANGED)
        self.assertEqual((actions, failed, changed), ([], False, False))


class _Unapplied(_States):
    """A start or reload that ends without an error and part of the file unapplied (shc-frr-check)."""

    def note(self):
        return self.node.get(self.node.record.replace(".frr-loaded", ".frr-unapplied"))

    def test_a_line_left_unapplied_fails_the_job_and_the_next_reloads_again(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("never", "line X\n")        # a daemon that is not up yet: the reload leaves its line out
        actions, failed, msg = self.job("conf A\nline X\n", probe="_job_msg | default('')")
        self.assertEqual((actions, failed), (["reloaded"], True))
        self.assertIn("(line X)", msg)
        self.assertIn("the next run reloads again", msg)
        self.assertFalse(self.loaded_digest_matches())
        os.remove(self.node.p("never"))            # it is up now
        self.assertEqual(self.job("conf A\nline X\n"), (["reloaded"], False))
        self.assertTrue(self.loaded_digest_matches())
        self.assertIsNone(self.note())
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))

    def test_a_pending_line_a_reload_does_not_fix_converges_in_one_run(self):
        # a record from before the load check: file and record agree, FRR lacks a line it never takes; the run that
        # reloads for it checks although the file is unchanged, and since the line was missing before and after the
        # reload it is accepted at once: recorded, noted, not failed, no reload again
        self.node.running("conf A\nline X\n")
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))
        self.node.put("loaded", "conf A\n")
        self.node.put("never", "line X\n")
        self.node.put("pending", "line X\n")
        actions, failed, out = self.job("conf A\nline X\n", probe=self.RECORDED + ".stdout_lines")
        self.assertEqual((actions, failed), (["reloaded"], False))
        self.assertEqual(out[0], "result=accepted")
        note = os.path.join(self.node.r, "etc/shc-fabric", ("gw-site1" if self.GATEWAY else "root") + ".frr-unapplied")
        self.assertIn("line X", self.node.get(note) or "")
        os.remove(self.node.p("pending"))            # the reader leaves noted lines out from now on
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))

    def test_review_reproduction_a_line_never_taken_with_an_unchanged_file_reloads_once(self):
        # the reviewers' case: the record equals the file's digest and FRR always leaves one line (the stand-in
        # frr-reload.py --test reports it every time); the pending difference is derived from what FRR runs, less
        # the note. One reload, the note takes the line, then nothing is due on any later run
        self.node.running("conf A\nline X\n")
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))
        self.node.put("never", "line X\n")
        self.node.put("loaded", "conf A\n")
        runs = [self.job("conf A\nline X\n") for _ in range(3)]
        self.assertEqual(runs, [(["reloaded"], False), ([], False), ([], False)])
        note = os.path.join(self.node.r, "etc/shc-fabric", ("gw-site1" if self.GATEWAY else "root") + ".frr-unapplied")
        self.assertEqual((self.node.get(note) or "").strip(), "line X")

    def test_a_start_with_an_unchanged_file_is_checked(self):
        # FRR found stopped, the file and the record unchanged, a daemon that leaves a line out: the start is checked,
        # the job fails once naming it; the next run (the line noted) reloads nothing
        self.node.running("conf A\nline X\n")
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))
        self.node.put("state", "inactive\n")
        self.node.put("never", "line X\n")
        actions, failed = self.job("conf A\nline X\n")
        self.assertEqual(len(actions), 1)
        self.assertTrue(failed)
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))

    def test_a_pending_line_and_a_new_one_left_still_fails(self):
        self.node.running("conf A\n")
        self.assertEqual(self.job("conf A\n"), ([], False))
        self.node.put("never", "line Y\n")
        self.node.put("pending", "line Z\n")
        actions, failed = self.job("conf A\nline Y\n")
        self.assertTrue(failed)

    def test_a_line_frr_never_takes_is_recorded_after_the_second_reload_and_reported(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("never", "line X\n")
        self.assertEqual(self.job("conf A\nline X\n"), (["reloaded"], True))
        actions, failed, out = self.job("conf A\nline X\n", probe=self.RECORDED + ".stdout_lines")
        self.assertEqual((actions, failed), (["reloaded"], False))
        self.assertEqual(out, ["result=accepted", "line: line X", "recorded"])
        self.assertTrue(self.loaded_digest_matches())
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))   # no loop: nothing due
        # a later change that leaves the same line out: known, recorded at once
        self.assertEqual(self.job("conf B\nline X\n"), (["reloaded"], False))
        # a new line left out on top of it: a failure again, naming only what is left now
        self.node.put("never", "line X\nline Y\n")
        actions, failed, msg = self.job("conf B\nline X\nline Y\n", probe="_job_msg | default('')")
        self.assertTrue(failed)
        self.assertIn("(line X; line Y)", msg)

    def test_a_start_that_leaves_a_line_unapplied_fails_too(self):
        self.node.running("conf A\n", active=False)
        self.node.put("never", "line X\n")
        self.assertEqual(self.job("conf A\nline X\n"), (["started"], True))
        self.assertFalse(self.loaded_digest_matches())
        os.remove(self.node.p("never"))
        self.assertEqual(self.job("conf A\nline X\n"), (["reloaded"], False))
        self.assertTrue(self.loaded_digest_matches())

    def test_without_the_check_any_successful_reload_is_recorded(self):
        real = {"attachments": {"enabled": False}, "nodes": {"frrLoadCheck": {"enabled": False, "ignore": []}}}
        self.node.running("conf A\n")
        self.job("conf A\n", fab_real=real)
        self.node.put("never", "line X\n")
        self.assertEqual(self.job("conf A\nline X\n", fab_real=real), (["reloaded"], False))
        self.assertTrue(self.loaded_digest_matches())

    def test_an_installations_ignore_pattern_is_passed_on(self):
        real = {"attachments": {"enabled": False}, "nodes": {"frrLoadCheck": {"enabled": True, "ignore": ["^line X$"]}}}
        self.node.running("conf A\n")
        self.job("conf A\n", fab_real=real)
        self.node.put("never", "line X\n")
        self.assertEqual(self.job("conf A\nline X\n", fab_real=real), (["reloaded"], False))
        self.assertTrue(self.loaded_digest_matches())


class RootReload(_Unapplied):
    GATEWAY = False
    RECORDED = "_hf_frr_recorded"
    # the node record counts the reload (node_root.yml "record the node": _hf_reload is in its list)
    CHANGED = "[_hf_files | default({}), _hf_daemons, _hf_frrconf, _hf_frr_state, _hf_reload] | select('changed') | list | length > 0"

    def test_the_node_record_counts_the_reload(self):
        tasks = load(HF)
        record = next(t for t in tasks if t.get("name", "").endswith(": record the node"))
        self.assertIn("_hf_reload | default({})", record["ansible.builtin.set_fact"]["hf_changed_nodes"])

    def test_an_interrupted_daemons_change_is_healed(self):
        # Review M2: a run that wrote bfdd=yes and stopped before the unit's reload. The next run finds the daemons file
        # unchanged; frr-reload.py does not start a daemon (the stub is honest: it loads the configuration only), so
        # the run sees bfdd not running and reloads the unit (a new watchfrr with the file's list)
        self.node.running("conf A\n")
        self.job("conf A\n")
        real = {"attachments": {"enabled": True}}
        self.job("conf A\n", crash_after="FRR daemons", fab_real=real)
        self.assertIn("bfdd=yes", self.node.get(self.node.daemons))
        self.assertNotIn("bfdd", self.node.runs())
        self.assertEqual(self.job("conf A\n", fab_real=real), (["unit-reloaded", "reloaded"], False))
        self.assertIn("bfdd=yes", self.node.runs())
        self.assertEqual(self.job("conf A\n", fab_real=real), ([], False))

    def test_frr_reload_alone_never_starts_a_daemon(self):
        # the honest stub: the job's frr-reload.py takes the configuration, not the daemons file (f98cbae1's stub
        # loaded both, which hid M2)
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put(self.node.daemons, "bgpd=yes\nisisd=yes\nbfdd=yes\n")
        actions, failed = self.job("conf B\n")
        self.assertEqual((actions, failed), (["reloaded"], False))     # bfdd is not wanted (attachments off)
        self.assertNotIn("bfdd", self.node.runs())

    def test_a_daemon_the_unit_reload_does_not_start_fails_the_job_named(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("unit_reload_fails", "")
        actions, failed, out = self.job("conf A\n", fab_real={"attachments": {"enabled": True}},
                                        probe="_hf_daemons_reload.stdout | default('')")
        self.assertEqual((actions, failed), (["unit-reloaded"], True))
        self.assertIn("still not running after the reload of the frr unit: bfdd", out)

    def test_a_daemon_that_does_not_start_with_frr_fails_the_job(self):
        # FRR found stopped: the start starts every enabled daemon; one that does not run fails the job, named
        self.node.running("conf A\n", active=False)
        self.node.put("daemon_dead", "isisd=yes\n")
        actions, failed, out = self.job("conf A\n", probe="_hf_daemons_run.stdout | default('')")
        self.assertEqual((actions, failed), (["started"], True))
        self.assertEqual(out, "not running: isisd")
        os.remove(self.node.p("daemon_dead"))
        # running FRR without isisd: the next run reloads the unit for it
        self.assertEqual(self.job("conf A\n")[0][0], "unit-reloaded")

    # ---- gate-s6 F12: the job runs frr-reload.py itself; its exit status alone never fails the job or stops FRR ----

    F12_SAYS = ('"router bgp 65012 vrf egress --  no" we failed to remove this command\n'
                '% Specify remote-as or peer-group commands first\n')

    def test_a_reload_error_that_leaves_nothing_unapplied_is_a_success_with_a_note(self):
        # F12 itself: frr-reload.py exits 1 on a line FRR did not need (the neighbour was already gone with its first
        # line); FRR holds the whole file. No systemd call on frr at all (on f98cbae1 the job ran systemctl reload frr,
        # and the stock unit stopped and restarted FRR on the failure), the job succeeds, the file is recorded and
        # the reload's error is reported
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("reload_says", self.F12_SAYS)
        self.node.put("reload_loads", "")
        actions, failed, out = self.job("conf B\n", reload_rc=1, probe="[_hf_reload_rc, _hf_reload_errors]")
        self.assertEqual((actions, failed), (["reloaded"], False))
        self.assertEqual(int(out[0]), 1)
        self.assertEqual(self.node.get(self.node.record.replace(".frr-loaded", ".frr-unapplied")), None)
        self.assertEqual(out[1], ['"router bgp 65012 vrf egress --  no" we failed to remove this command',
                                  "% Specify remote-as or peer-group commands first"])
        self.assertTrue(self.loaded_digest_matches())
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_the_node_record_names_a_reload_error_frr_did_not_need(self):
        tasks = load(HF)
        record = next(t for t in tasks if t.get("name", "").endswith(": record the node"))
        msg = record["ansible.builtin.set_fact"]["hf_node_results"]
        self.assertIn("the reload reported an error FRR did not need: ' ~ _hf_reload_errors | first", msg)
        self.assertIn("and 'result=applied' in (_hf_frr_recorded.stdout_lines | default([]))", msg)
        note = next(t for t in tasks if t.get("name", "").endswith(": a reload error FRR did not need"))
        self.assertIn("'result=applied' in", " ".join(note["when"]))

    def test_a_reload_error_with_lines_left_fails_with_them_and_the_error(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("reload_says", self.F12_SAYS)
        actions, failed, msg = self.job("conf B\n", reload_rc=1, probe="_job_msg | default('')")
        self.assertEqual((actions, failed), (["reloaded"], True))
        self.assertIn("(conf B)", msg)
        self.assertIn("The reload exited 1: \"router bgp 65012 vrf egress --  no\" we failed to remove this command", msg)
        self.assertFalse(self.loaded_digest_matches())
        self.assertEqual(self.job("conf B\n"), (["reloaded"], False))   # the next run reloads again

    def test_review_rv1_a_reload_ended_by_its_limit_twice_is_never_recorded(self):
        # Review H1 scenario a: the file changes, frr-reload.py is ended by the step's limit (exit 124) having applied
        # nothing, in two runs. Both fail and record nothing (f98cbae1: the same); the second must not accept the lines
        # the first left; a third run whose reload works records it
        self.node.running("conf A\n")
        self.job("conf A\n")
        for _ in range(2):
            actions, failed, out = self.job("conf A\nline B\n", reload_rc=124, probe=self.RECORDED + ".stdout_lines")
            self.assertEqual((actions, failed), (["reloaded"], True))
            self.assertEqual(out[:2], ["result=unapplied", "line: line B"])
            self.assertFalse(self.loaded_digest_matches())
            self.assertEqual(self.node.runs(), self.want("conf A\n"))
        # the failed reloads' checks do not count as loads: no note is left that a later check could accept
        self.assertIsNone(self.node.get(self.node.record.replace(".frr-loaded", ".frr-unapplied")))
        self.assertEqual(self.job("conf A\nline B\n"), (["reloaded"], False))
        self.assertTrue(self.loaded_digest_matches())
        self.assertEqual(self.job("conf A\nline B\n"), ([], False))

    def test_review_rv2_a_failed_repair_reload_is_not_accepted(self):
        # Review H1 scenario b: the file is recorded as loaded, FRR lost a line (a hand change); the run woken for it
        # reloads, the reload exits 1 having applied nothing: the job fails, nothing is accepted or noted
        self.node.running("conf A\nline X\n")
        self.assertEqual(self.job("conf A\nline X\n"), ([], False))
        self.node.put("lconf", "conf A\n")
        self.node.put("loaded", "conf A\n" + self.node.get("dloaded"))
        actions, failed, out = self.job("conf A\nline X\n", reload_rc=1, probe=self.RECORDED + ".stdout_lines")
        self.assertEqual((actions, failed), (["reloaded"], True))
        self.assertEqual(out[0], "result=unapplied")
        self.assertIsNone(self.node.get(self.node.record.replace(".frr-loaded", ".frr-unapplied")))
        # the next run (a working reload) repairs it
        self.assertEqual(self.job("conf A\nline X\n"), (["reloaded"], False))
        self.assertEqual(self.node.runs(), self.want("conf A\nline X\n"))

    def test_a_failed_reload_keeps_the_note_of_lines_frr_does_not_take(self):
        # a standing accepted line (FRR never takes it) and a reload that exits 1: the job fails once (lines left are
        # not forgiven), the note keeps what it held, and the next working reload accepts the line again at once
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("never", "line X\n")
        self.job("conf A\nline X\n")
        self.job("conf A\nline X\n")
        note = self.node.record.replace(".frr-loaded", ".frr-unapplied")
        before = self.node.get(note)
        self.assertIn("line X", before)
        self.node.put("reload_loads", "")
        actions, failed = self.job("conf B\nline X\n", reload_rc=1)
        self.assertEqual((actions, failed), (["reloaded"], True))
        self.assertEqual(self.node.get(note), before)
        os.remove(self.node.p("reload_loads"))
        self.assertEqual(self.job("conf B\nline X\n"), (["reloaded"], False))

    def test_no_note_without_a_check(self):
        # a non-zero reload while a job for the unit is queued: nothing is recorded or checked, and no "FRR holds the
        # whole configuration" note (Review H1, the smaller form)
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("job", "4711\n")
        self.node.put("reload_says", self.F12_SAYS)
        actions, failed, out = self.job("conf B\n", reload_rc=1, probe=self.RECORDED + ".stdout_lines")
        self.assertEqual(out, ["not recorded: frr active, job 4711"])
        tasks = load(HF)
        note = next(t for t in tasks if t.get("name", "").endswith(": a reload error FRR did not need"))
        self.assertIn("'result=applied' in (_hf_frr_recorded.stdout_lines | default([]))", note["when"])

    def test_the_reload_errors_hide_a_password(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put("reload_says", '"router bgp 65010 vrf e1 --  no neighbor 10.0.0.1 password s3cr3t" we failed\n')
        self.node.put("reload_loads", "")
        actions, failed, errors = self.job("conf B\n", reload_rc=1, probe="_hf_reload_errors")
        self.assertEqual(errors, ['"router bgp 65010 vrf e1 --  no neighbor 10.0.0.1 password <hidden>'])
        self.assertNotIn("s3cr3t", str(errors))

    def test_a_reload_that_runs_out_of_time_is_judged_the_same_way(self):
        # timeout ended frr-reload.py (exit 124) after part of the file: the check sees what is left
        self.node.running("conf A\n")
        self.job("conf A\n")
        actions, failed, msg = self.job("conf B\n", reload_rc=124, probe="_job_msg | default('')")
        self.assertEqual((actions, failed), (["reloaded"], True))
        self.assertIn("The reload exited 124: no output (exit 124)", msg)
        self.node.put("reload_loads", "")
        self.assertEqual(self.job("conf B\n", reload_rc=124), (["reloaded"], False))
        self.assertTrue(self.loaded_digest_matches())

    def test_without_the_check_a_failed_reload_is_not_recorded(self):
        real = {"attachments": {"enabled": False}, "nodes": {"frrLoadCheck": {"enabled": False, "ignore": []}}}
        self.node.running("conf A\n")
        self.job("conf A\n", fab_real=real)
        self.node.put("reload_loads", "")
        actions, failed = self.job("conf B\n", fab_real=real, reload_rc=1)
        self.assertEqual((actions, failed), (["reloaded"], True))
        self.assertFalse(self.loaded_digest_matches())

    def test_a_newly_enabled_daemon_reloads_the_unit_before_the_file_is_written(self):
        # bfdd switched on: the unit's own reload (frrinit.sh, a new watchfrr) runs while frr.conf is still the old
        # one (before the removal step, Review M1: RootOrder); then the job writes the file and runs frr-reload.py
        self.node.running("conf A\n")
        self.job("conf A\n")
        real = {"attachments": {"enabled": True}}
        actions, failed = self.job("conf B\n", fab_real=real)
        self.assertEqual((actions, failed), (["unit-reloaded", "reloaded"], False))
        self.assertEqual(self.node.get("unit-reload-saw"), "conf A\n")
        self.assertIn("bfdd=yes", self.node.runs())
        self.assertEqual(self.job("conf B\n", fab_real=real), ([], False))

    def test_a_newly_enabled_daemon_on_a_stopped_frr_only_starts_it(self):
        self.node.running("conf A\n", active=False)
        actions, failed = self.job("conf A\n", fab_real={"attachments": {"enabled": True}})
        self.assertEqual((actions, failed), (["started"], False))

    def test_the_only_systemd_reload_left_is_the_daemons_one(self):
        # the configuration reload is frr-reload.py run by the job, with frrinit.sh's paths, bounded on the node, never
        # through systemd; the step's exit status never fails it (the check decides)
        tasks = load(HF)
        sd = [t["name"] for t in tasks if "ansible.builtin.systemd" in t and t["ansible.builtin.systemd"].get("name") == "frr"]
        self.assertEqual([n.replace(PFX, "") for n in sd], ["start FRR (enabled at boot)"])
        self.assertNotIn("reloaded", str([t["ansible.builtin.systemd"] for t in tasks if "ansible.builtin.systemd" in t]))
        reload = next(t for t in tasks if t.get("name", "").endswith(": reload the FRR configuration"))
        self.assertEqual(reload["ansible.builtin.command"].split(),
                         ["timeout", "-k", "10", "{{", "[fab_lease_step_timeout", "|", "int", "-", "15,", "15]", "|",
                          "max", "}}", "env", "PYTHONNOUSERSITE=1", "/usr/libexec/frr/frr-reload.py", "--reload",
                          "--bindir", "/usr/bin", "--confdir", "/etc/frr", "--rundir", "/var/run/frr", "/etc/frr/frr.conf"])
        shells = [str(t.get("ansible.builtin.shell", "")) for t in tasks]
        self.assertEqual([s for s in shells if "systemctl reload" in s and "frr" in s],
                         [next(s for s in shells if "systemctl reload frr" in s)])
        daemons = next(t for t in tasks if t.get("name", "").endswith("(reload of the frr unit)"))
        self.assertNotIn("when", daemons)        # whenever a daemon the file enables does not run (Review M2)
        self.assertIn("kill -0", daemons["ansible.builtin.shell"])

    def test_a_restart_for_a_new_bridge_mac_records_without_a_reload(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.assertEqual(self.job("conf B\n", _hf_apply={"stdout": "bridge-mac=changed"}), (["restarted"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))


class RootOrder(unittest.TestCase):
    """node_root.yml: what FRR loses goes first, then the kernel, the daemons, the file, the reload, the check."""

    def names(self):
        return [t.get("name", "").replace(PFX, "") for t in load(HF)]

    def test_order(self):
        n = self.names()
        # Review M1: the drop-in and its daemon-reload, the daemons and the unit's reload, THEN the removal step (the unit's
        # reload loads the file on disk, which still has what the step removes)
        order = ["the drop-in directory of the frr unit", "install the fabric apply script and unit",
                 "load the units' new definitions",
                 "what FRR last loaded (a node without the record: the files as they are)", "FRR daemons",
                 "the lease lasts for the reload of the frr unit",
                 "start an enabled FRR daemon that does not run (reload of the frr unit)",
                 "the FRR configuration this run wants",
                 "take out of FRR what this run removes (before the kernel objects)",
                 "run the root instance at boot (and now)", "apply the root instance", "FRR configuration",
                 "start FRR (enabled at boot)", "every FRR daemon the file enables runs",
                 "reload the FRR configuration", "the reload's errors", "record what FRR loaded",
                 "FRR holds the whole configuration", "a reload error FRR did not need",
                 "remove from FRR the VRFs its configuration no longer has", "record the node"]
        self.assertEqual([x for x in n if x in order], order)

    def test_the_drop_in_is_installed_with_the_units(self):
        tasks = load(HF)
        install = next(t for t in tasks if t.get("name", "").endswith(": install the fabric apply script and unit"))
        self.assertIn({"src": "frr-shc-reload.conf", "dest": "/etc/systemd/system/frr.service.d/50-shc-reload.conf",
                       "mode": "0644"}, install["loop"])
        self.assertEqual(install["register"], "_hf_files")
        dr = next(t for t in tasks if t.get("name", "").endswith(": load the units' new definitions"))
        self.assertEqual((dr["ansible.builtin.systemd"], dr["when"]), ({"daemon_reload": True}, "_hf_files is changed"))
        unit = next(t for t in tasks if t.get("name", "").endswith(": run the root instance at boot (and now)"))
        self.assertNotIn("daemon_reload", unit["ansible.builtin.systemd"])
        with open(os.path.join(ROOT, "eda/common/files/bgw/frr-shc-reload.conf")) as f:
            lines = [l for l in f.read().splitlines() if l and not l.startswith("#")]
        # the stock unit's ExecReload with its exit status ignored, nothing else (Restart=, ExecStop, the watchdog as
        # shipped)
        self.assertEqual(lines, ["[Service]", "ExecReload=", "ExecReload=-/usr/libexec/frr/frrinit.sh reload"])

    def test_the_file_written_is_the_one_the_removal_step_compared(self):
        tasks = load(HF)
        want = next(t for t in tasks if t.get("name", "").endswith(": the FRR configuration this run wants"))
        self.assertEqual(want["ansible.builtin.set_fact"]["_hf_frr_text"],
                         "{{ lookup('ansible.builtin.template', 'frr-root.conf.j2') }}")
        step = next(t for t in tasks if t.get("name", "").endswith("(before the kernel objects)"))
        # the step gets only the file's BGP lines (Review H2: never a password), RenderedPlans in test_frr_leaving
        self.assertIn("_hf_frr_text | regex_findall(", step["ansible.builtin.command"]["stdin"])
        conf = next(t for t in tasks if t.get("name", "").endswith(": FRR configuration"))
        self.assertEqual(conf["ansible.builtin.copy"]["content"], "{{ _hf_frr_text }}")
        self.assertFalse([t for t in tasks if "ansible.builtin.template" in t])

    @unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_the_lookup_renders_what_the_template_module_wrote(self):
        # the configuration was written by the template module before; the lookup must give the same bytes (trim
        # blocks, the trailing newline), or the first run after the release reloads for nothing
        d = tempfile.mkdtemp(prefix="shc-render-")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "templates"))
        text = "! a\n{% for x in xs %}\nline {{ x }}\n{% endfor %}\n{% if xs %}\n tail\n{% endif %}\n!\n"
        with open(os.path.join(d, "templates", "t.j2"), "w") as f:
            f.write(text)
        tasks = [{"ansible.builtin.template": {"src": os.path.join(d, "templates", "t.j2"), "dest": os.path.join(d, "a")}},
                 {"ansible.builtin.set_fact": {"_t": "{{ lookup('ansible.builtin.template', '%s') }}"
                                               % os.path.join(d, "templates", "t.j2")}},
                 {"ansible.builtin.copy": {"content": "{{ _t }}", "dest": os.path.join(d, "b")}},
                 write_out("true")]
        run_play(tasks, {"xs": [1, 2]})
        with open(os.path.join(d, "a")) as fa, open(os.path.join(d, "b")) as fb:
            a, b = fa.read(), fb.read()
        self.assertEqual(a, "! a\nline 1\nline 2\n tail\n!\n")
        self.assertEqual(b, a)


class GatewayReload(_Unapplied):
    GATEWAY = True
    RECORDED = "_cg_frr_recorded"
    # the node record counts _cg_frrconf among its changes (node_gateway.yml "record the node")
    CHANGED = "_cg_frrconf is changed or _cg_state is changed"

    def test_the_node_record_counts_the_configuration(self):
        tasks = load(CG)
        record = next(t for t in tasks if t.get("name", "").endswith(": record the node"))
        self.assertIn("_cg_frrconf", record["ansible.builtin.set_fact"]["cg_changed_nodes"])

    def test_a_new_unit_restarts_and_records_without_a_reload(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.assertEqual(self.job("conf B\n", _cg_unit_changed=True), (["restarted"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))

    def test_a_script_only_change_neither_restarts_nor_reloads(self):
        # a new apply script alone (_cg_files changed, the unit not) leaves the running instance alone
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.assertEqual(self.job("conf A\n", _cg_files={"changed": True}), ([], False))

    def test_the_record_follows_the_desired_file_not_frrs_own_copy(self):
        # FRR rewrites frr.conf after every reload; only frr.conf.shc counts
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.node.put(os.path.join(os.path.dirname(self.node.conf), "frr.conf"), "! written by FRR\nconf A\n")
        self.assertEqual(self.job("conf A\n"), ([], False))


del _Case, _States, _Unapplied

if __name__ == "__main__":
    unittest.main()

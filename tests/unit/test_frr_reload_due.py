"""A job that follows an interrupted one reloads FRR; a job with nothing pending does not.

FRR runs what its last start or reload loaded. A job that wrote the root's /etc/frr/frr.conf (or daemons) or a
gateway's frr.conf.shc and stopped before the reload (crash, failed reload, step timeout, lost lease) leaves a file
the next job finds unchanged. The node files keep the digest of what FRR last loaded (/etc/shc-fabric/root.frr-loaded,
gw-<gateway>.frr-loaded) and reload while the file differs from it.

The tests run the real steps of node_root.yml and node_gateway.yml, from the record of what was loaded to the record
after the reload, in ansible-playbook on localhost: the node's files live under a temporary root, the template writes
the content a test wants, and systemd is a stub that logs what it was asked to do and "loads" the files (the content
FRR then runs is kept next to them). A crash is the job ending after a given step. Skipped without ansible-playbook.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, load, run_play, write_out  # noqa: E402

HF = os.path.join(ROOT, "eda/rulebooks/roles/hybridfabric_provision/tasks/node_root.yml")
CG = os.path.join(ROOT, "eda/rulebooks/roles/cloudgateway_provision/tasks/node_gateway.yml")
PFX = "{{ bgw_node.name }}: "

ROOT_STEPS = ["what FRR last loaded (a node without the record: the files as they are)",
              "FRR daemons",
              "FRR configuration",
              "is a reload due (the files differ from what FRR last loaded)",
              "is FRR running",
              "start FRR (enabled at boot)",
              "reload the FRR configuration",
              "record what FRR loaded"]
GW_STEPS = ["what the instance last loaded (without the record: the file as it is)",
            "FRR configuration",
            "is a reload due (the file differs from what the instance last loaded)",
            "is the gateway instance running",
            "start the gateway instance (enabled at boot)",
            "reload the FRR configuration",
            "record what the instance loaded"]


def section(path, steps):
    """The node file's steps from the first to the last name, which must be exactly these, in this order."""
    tasks = load(path)
    names = [t.get("name", "").replace(PFX, "") for t in tasks]
    i = names.index(steps[0])
    part = tasks[i:i + len(steps)]
    assert [t["name"].replace(PFX, "") for t in part] == steps, [t["name"] for t in part]
    return part


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
        self.put("loaded", "".join(self.get(f) for f in self.files()))
        self.put("state", "active\n" if active else "inactive\n")

    def runs(self):
        """What FRR runs: the files' content at its last start or reload."""
        return self.get("loaded")


def stub(task, node):
    """The task for localhost: paths under the node's root, the template writes {{ desired }}, systemd is a stub (a
    state that renders as omit asks for nothing: ansible-core 2.16 renders the placeholder, 2.19 and later "")."""
    t = copy.deepcopy(task)
    t.pop("delegate_to", None)
    t.pop("no_log", None)
    load_files = " ".join(node.files())
    swap = lambda s: s.replace("/etc/frr/", node.r + "/etc/frr/").replace("/etc/shc-fabric/", node.r + "/etc/shc-fabric/")  # noqa: E731
    if "ansible.builtin.shell" in t:
        t["ansible.builtin.shell"] = swap(t["ansible.builtin.shell"])
    elif "ansible.builtin.lineinfile" in t:
        t["ansible.builtin.lineinfile"]["path"] = swap(t["ansible.builtin.lineinfile"]["path"])
    elif "ansible.builtin.template" in t:
        dest = swap(t.pop("ansible.builtin.template")["dest"])
        t["ansible.builtin.copy"] = {"dest": dest, "content": "{{ desired }}"}
    elif "ansible.builtin.command" in t:
        assert "systemctl is-active" in t["ansible.builtin.command"], t
        t["ansible.builtin.command"] = "cat %s" % node.p("state")
    elif "ansible.builtin.systemd" in t:
        sd = t.pop("ansible.builtin.systemd")
        if sd["state"] == "reloaded":
            t["ansible.builtin.shell"] = (
                "echo reloaded >> {log}; rc=$(cat {rc} 2>/dev/null || echo 0); [ \"$rc\" = 0 ] || exit 1; "
                "cat {files} > {loaded}".format(log=node.p("actions"), rc=node.p("reload_rc"), files=load_files,
                                               loaded=node.p("loaded")))
            if "delay" in t:
                t["delay"] = 0
        else:
            t.setdefault("vars", {})["_state"] = sd["state"]
            t["ansible.builtin.shell"] = (
                "s='{{{{ _state }}}}'; case \"$s\" in ''|*omit_place_holder*) exit 0 ;; esac; echo \"$s\" >> {log}; "
                "cat {files} > {loaded}; echo active > {state}".format(log=node.p("actions"), files=load_files,
                                                                      loaded=node.p("loaded"), state=node.p("state")))
    else:
        raise AssertionError("no stub for %s" % t.get("name"))
    return t


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class _Case(unittest.TestCase):
    GATEWAY = False

    def setUp(self):
        self.node = _Node(self, self.GATEWAY)
        self.steps = section(CG if self.GATEWAY else HF, GW_STEPS if self.GATEWAY else ROOT_STEPS)

    def job(self, desired, crash_after=None, reload_rc=0, **extra):
        """One job's FRR steps on the node; returns (what systemd was asked, whether the job failed)."""
        n = self.node
        n.put("reload_rc", "%d\n" % reload_rc)
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
                 "rescue": [{"ansible.builtin.set_fact": {"_job_failed": True}}]},
                write_out("_job_failed | default(false)")]
        failed = run_play(play, variables)
        actions = (n.get("actions") or "").split()
        return actions, failed

    def loaded_digest_matches(self):
        import hashlib
        h = hashlib.sha256("".join(self.node.get(f) for f in self.node.files()).encode()).hexdigest()
        return (self.node.get(self.node.record) or "").strip() == h

    def want(self, conf):
        """What FRR should run once conf is loaded (the root also loads its daemons file)."""
        return conf + (self.node.get(self.node.daemons) if self.node.daemons else "")

    # ---- the cases, for the root and for a gateway ----

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


class RootReload(_Case):
    GATEWAY = False

    def test_an_interrupted_daemons_change_is_healed(self):
        # the daemons file is loaded by the reload too (it restarts watchfrr with the file's list)
        self.node.running("conf A\n")
        self.job("conf A\n")
        real = {"attachments": {"enabled": True}}
        self.job("conf A\n", crash_after="FRR daemons", fab_real=real)
        self.assertIn("bfdd=yes", self.node.get(self.node.daemons))
        self.assertNotIn("bfdd", self.node.runs())
        self.assertEqual(self.job("conf A\n", fab_real=real), (["reloaded"], False))
        self.assertIn("bfdd=yes", self.node.runs())
        self.assertEqual(self.job("conf A\n", fab_real=real), ([], False))

    def test_a_restart_for_a_new_bridge_mac_records_without_a_reload(self):
        self.node.running("conf A\n")
        self.job("conf A\n")
        self.assertEqual(self.job("conf B\n", _hf_apply={"stdout": "bridge-mac=changed"}), (["restarted"], False))
        self.assertEqual(self.node.runs(), self.want("conf B\n"))
        self.assertEqual(self.job("conf B\n"), ([], False))


class GatewayReload(_Case):
    GATEWAY = True

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


del _Case

if __name__ == "__main__":
    unittest.main()

"""FRR's pending difference (files/shc_frr_pending.py) on top of the post-reload verification (shc-frr-check), and its
one call site per user: common/tasks/frr_pending.yml (the fabric and gateway jobs make the reload due when lines are
pending) and the health reader's FRR drift. Read only: the check's note is read, never written; lines the note holds
(FRR refused them after two loads) are not pending, so no reload loops on them. Without the check on the node
nothing is pending and the jobs behave as before."""
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402


def module(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


import importlib.machinery  # noqa: E402

P = module(os.path.join(ROOT, "eda/common/files/shc_frr_pending.py"), "shc_frr_pending")
R = module(os.path.join(ROOT, "eda/common/files/shc_node_health.py"), "shc_node_health")
TASKS = os.path.join(ROOT, "eda/common/tasks/frr_pending.yml")
# the post-reload verification, from this tree once merged, else from its branch's worktree (read only)
CHECK = next((p for p in (os.path.join(ROOT, "eda/common/files/bgw/shc-frr-check"),
                          os.path.expanduser("~/dev/hybridcloud-ops1-d/eda/common/files/bgw/shc-frr-check"))
              if os.path.exists(p)), None)
TEST_OUT = """Lines To Delete
===============

Lines To Add
============
frr version 8.5.3
ip prefix-list SHC-GATE-H2 seq 5 deny 192.0.2.0/24
router bgp 65010 vrf egress
 neighbor 2001:db8::2 shutdown
"""


def fake_checker(left, note_lines=(), err=None):
    c = types.SimpleNamespace(RELOAD="/x/frr-reload.py", TIMEOUT=90, calls=[])
    c.run_test = lambda conf, ps, script: (c.calls.append((conf, ps, script)) or (None if err else "text", err))
    c.left_over = lambda text, ignore, conf_text=None: [l for l in left if not any(i in l for i in ignore)]
    c.read_note = lambda path: set(note_lines) if path else set()
    return c


class Pending(unittest.TestCase):
    def test_the_desired_file_is_passed_for_the_derived_route_targets(self):
        c = fake_checker([])
        seen = []
        c.left_over = lambda text, ignore, conf_text=None: seen.append(conf_text) or []
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write("vrf t1\n vni 1\nexit-vrf\n")
            f.flush()
            P.frr_pending_difference(None, "gw-a", f.name, checker=c)
        self.assertEqual(seen, ["vrf t1\n vni 1\nexit-vrf\n"])

    def test_lines_minus_the_note_and_ignore(self):
        c = fake_checker(["a: x", "b: y", "c: z"], note_lines=["b: y"])
        self.assertEqual(P.frr_pending_difference(None, "gw-a", "/f", note="/n", ignore=["c:"], checker=c), ["a: x"])
        self.assertEqual(c.calls, [("/f", "gw-a", "/x/frr-reload.py")])
        self.assertEqual(c.TIMEOUT, P.TIMEOUT)

    def test_cannot_tell(self):
        self.assertIsNone(P.frr_pending_difference(None, "", "/f", checker=fake_checker([], err="check: x")))
        if not os.path.exists(P.CHECKER):
            self.assertIsNone(P.frr_pending_difference(None, "", "/f"))

    @unittest.skipUnless(CHECK, "the post-reload verification (shc-frr-check) is not in reach")
    def test_with_the_real_check_read_only(self):
        tmp = tempfile.mkdtemp(prefix="shc-pending-real-")
        self.addCleanup(shutil.rmtree, tmp, True)
        reload = os.path.join(tmp, "frr-reload.py")
        with open(reload, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n%sEOF\n" % TEST_OUT)
        os.chmod(reload, os.stat(reload).st_mode | stat.S_IEXEC)
        c = module(CHECK, "shc_frr_check_real")
        c.RELOAD = reload
        all_lines = P.frr_pending_difference(None, "", "/etc/frr/frr.conf", checker=c)
        self.assertIn("ip prefix-list SHC-GATE-H2 seq 5 deny 192.0.2.0/24", all_lines)
        self.assertFalse(any("frr version" in l for l in all_lines))
        refused = [l for l in all_lines if "shutdown" in l]
        self.assertEqual(len(refused), 1)
        note = os.path.join(tmp, "root.frr-unapplied")
        with open(note, "w") as f:
            f.write(refused[0] + "\n")
        before = os.stat(note).st_mtime_ns
        got = P.frr_pending_difference(None, "", "/etc/frr/frr.conf", note=note, checker=c)
        self.assertEqual(got, [l for l in all_lines if l != refused[0]])
        self.assertEqual(os.stat(note).st_mtime_ns, before, "the note was written")
        self.assertEqual(sorted(os.listdir(tmp)), ["frr-reload.py", "root.frr-unapplied"])


EVIDENCE = "/home/aaustin/cc-workspaces/shc-networking-invest/p-ops/evidence/gate/pre/frrtest-bgw-1.txt"
GW_ATUIN_CONF = """frr version 8.5.3
frr defaults traditional
hostname bgw-1-gw-atuin
vrf t51003
 vni 51003
exit-vrf
vrf t51004
 vni 51004
exit-vrf
router bgp 65010 vrf t51003
 address-family l2vpn evpn
  route-target import 65010:51003
  route-target export 65010:51003
 exit-address-family
exit
router bgp 65010 vrf t51004
 address-family l2vpn evpn
  route-target import 65010:51004
  route-target export 65010:51004
 exit-address-family
exit
"""


class LiveOutput(unittest.TestCase):
    """frr-reload.py --test as FRR 8.5.3 printed it on a gateway instance of the lab (pre-release check): the VRFs'
    EVPN route targets FRR derives itself are not pending once the compared file's text is passed."""

    @unittest.skipUnless(CHECK and os.path.exists(EVIDENCE), "the check or the recorded output is not in reach")
    def test_gateway_instance_output_is_nothing_pending(self):
        with open(EVIDENCE) as f:
            text = f.read()
        part = text.split("===== frrtest gw-atuin")[1].split("===== frrtest ")[0]
        out = part[part.index("Lines To Delete"):part.rindex("rc=")]
        tmp = tempfile.mkdtemp(prefix="shc-pending-live-")
        self.addCleanup(shutil.rmtree, tmp, True)
        reload = os.path.join(tmp, "frr-reload.py")
        with open(reload, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n%sEOF\n" % out)
        os.chmod(reload, 0o755)
        conf = os.path.join(tmp, "frr.conf.shc")
        with open(conf, "w") as f:
            f.write(GW_ATUIN_CONF)
        c = module(CHECK, "shc_frr_check_live")
        c.RELOAD = reload
        self.assertEqual(P.frr_pending_difference(None, "gw-atuin", conf, checker=c), [])
        # without the file's text the route targets would count (what the release before saw)
        self.assertTrue(any("route-target" in l for l in c.left_over(c.run_test(conf, "gw-atuin", reload)[0], [])))


class CheckTimeout(unittest.TestCase):
    @unittest.skipUnless(CHECK, "the post-reload verification (shc-frr-check) is not in reach")
    def test_a_test_out_of_time_ends_what_it_started(self):
        tmp = tempfile.mkdtemp(prefix="shc-pending-timeout-")
        self.addCleanup(shutil.rmtree, tmp, True)
        pidf = os.path.join(tmp, "child")
        reload = os.path.join(tmp, "frr-reload.py")
        with open(reload, "w") as f:   # a "vtysh" child that hangs, and the script waiting for it
            f.write("#!/bin/sh\nsleep 60 &\necho $! > %s\nwait\n" % pidf)
        os.chmod(reload, 0o755)
        c = module(CHECK, "shc_frr_check_timeout")
        c.TIMEOUT = 1
        text, err = c.run_test("/x", "", reload)
        self.assertIsNone(text)
        self.assertIn("TimeoutExpired", err)
        with open(pidf) as f:
            pid = int(f.read())
        import time
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the child of the timed-out test is still running")


class NoBytecode(unittest.TestCase):
    @unittest.skipUnless(CHECK, "the post-reload verification (shc-frr-check) is not in reach")
    def test_loading_the_check_writes_nothing_next_to_it(self):
        tmp = tempfile.mkdtemp(prefix="shc-pending-pyc-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dst = os.path.join(tmp, "shc-frr-check")
        shutil.copy(CHECK, dst)
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = False
        try:
            self.assertIsNotNone(P.load_checker(dst))
        finally:
            sys.dont_write_bytecode = old
        self.assertEqual(os.listdir(tmp), ["shc-frr-check"])


class Reader(unittest.TestCase):
    def test_reader_passes_instance_note_and_ignore(self):
        src = ("SEEN = {}\n"
               "def frr_pending_difference(run, pathspace, desired, note=None, ignore=()):\n"
               "    return ['%s|%s|%s|%s' % (pathspace, desired, note, ','.join(ignore))]\n")
        gw = {"name": "gw-atuin", "kind": "gateway"}
        self.assertEqual(R.frr_pending(None, gw, src, ignore=["^foo"]),
                         ["gw-atuin|/etc/frr/gw-atuin/frr.conf.shc|/etc/shc-fabric/gw-atuin.frr-unapplied|^foo"])
        self.assertEqual(R.frr_pending(None, {"name": "root", "kind": "root"}, src),
                         ["|/etc/frr/frr.conf|/etc/shc-fabric/root.frr-unapplied|"])
        self.assertIsNone(R.frr_pending(None, gw, None))
        self.assertIsNone(R.frr_pending(None, gw, "raise RuntimeError('x')"))
        with open(os.path.join(ROOT, "eda/common/files/shc_frr_pending.py")) as f:
            real = f.read()
        if not os.path.exists(P.CHECKER):  # the real helper on a machine without the check: not checked
            self.assertIsNone(R.frr_pending(None, gw, real))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CallSite(unittest.TestCase):
    def run_site(self, left=None, note=(), when=True, enabled=True):
        tmp = tempfile.mkdtemp(prefix="shc-pending-")
        self.addCleanup(shutil.rmtree, tmp, True)
        role = os.path.join(tmp, "a", "b", "c")
        os.makedirs(role)
        os.makedirs(os.path.join(tmp, "a", "common", "files"))
        with open(os.path.join(ROOT, "eda/common/files/shc_frr_pending.py")) as f:
            src = f.read()
        chk = os.path.join(tmp, "shc-frr-check")
        if left is not None:
            with open(chk, "w") as f:
                f.write("RELOAD = 'x'\nTIMEOUT = 90\n"
                        "def run_test(conf, ps, script):\n    return 'text', None\n"
                        "def left_over(text, ignore, conf_text=None):\n    return [l for l in %r if not any(i in l for i in ignore)]\n"
                        "def read_note(path):\n    return set(%r)\n" % (list(left), list(note)))
        src = src.replace('CHECKER = "/usr/local/sbin/shc-frr-check"', "CHECKER = %r" % chk)
        # role_path/../../../common/files: role = tmp/a/b/c -> tmp/common/files
        os.makedirs(os.path.join(tmp, "common", "files"), exist_ok=True)
        with open(os.path.join(tmp, "common", "files", "shc_frr_pending.py"), "w") as f:
            f.write(src)
        tasks = load(TASKS)
        for t in tasks:
            t.pop("delegate_to", None)
        tasks = [{"name": "init", "ansible.builtin.set_fact": {"frr_pending_lines": ["stale from the last node"]}}] + tasks
        return run_play(tasks + [write_out("frr_pending_lines")],
                        {"bgw_node": {"name": "localhost"}, "frr_pending_pathspace": "", "role_path": role,
                         "frr_pending_file": "/etc/frr/frr.conf", "frr_pending_note": "/n", "frr_pending_when": when,
                         "fab_real": {"nodes": {"frrLoadCheck": {"enabled": enabled, "ignore": ["skip:"]}}}})

    def test_without_the_check_nothing_is_pending(self):
        self.assertEqual(self.run_site(None), [])

    def test_with_the_check(self):
        self.assertEqual(self.run_site(["a: x", "skip: y", "b: refused"], note=["b: refused"]), ["a: x"])

    def test_not_read_while_frr_is_down_or_the_check_is_off(self):
        self.assertEqual(self.run_site(["a: x"], when=False), [])
        self.assertEqual(self.run_site(["a: x"], enabled=False), [])

    def test_bounded_and_wired_into_both_reloads(self):
        t = load(TASKS)[0]
        self.assertIn("['timeout', '-k', '5', '30', 'python3', '-', '--note', frr_pending_note]",
                      t["ansible.builtin.command"]["argv"])
        for path, name in (("eda/rulebooks/roles/hybridfabric_provision/tasks/node_root.yml",
                            "{{ bgw_node.name }}: reload the FRR configuration"),
                           ("eda/rulebooks/roles/cloudgateway_provision/tasks/node_gateway.yml",
                            "{{ bgw_node.name }}: reload the FRR configuration")):
            when = " ".join(str(w) for w in by_name(load(os.path.join(ROOT, path)), name)["when"])
            self.assertIn("frr_pending_lines", when)


if __name__ == "__main__":
    unittest.main()

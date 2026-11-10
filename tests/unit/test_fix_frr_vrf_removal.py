"""A VRF the configuration no longer has leaves nothing in FRR, on the root and on every gateway, on both transports.

frr-reload never sends `no vrf <name>`: for a vrf stanza the new file lacks it removes only the lines inside it, so
zebra and the other daemons keep the VRF as configured (`show vrf`: `vrf <name> inactive (configured)`) until FRR
restarts. The node tasks therefore remove, after the reload, every VRF zebra lists as inactive that the rendered
configuration does not name and that has no BGP instance left (node_root.yml and node_gateway.yml, "remove from FRR
the VRFs its configuration no longer has"; the same code in both).

The tests run the rendered script in bash with `systemctl` and `vtysh` replaced by stubs that answer from files and
log what they were asked. The inactive line is the replica's: finding 5's evidence
(p-srv6/replica/out/base-72f86002-h2/evidence/vrf-removed/V2-detail.txt, FRR 8.5.3: `show vrf` of gw-site-a after
the network was removed)."""
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases, run_play, write_out  # noqa: E402
from test_gateway_readiness import render, shell_of  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
RT = os.path.join(ROLES, "hybridfabric_provision", "tasks", "node_root.yml")
GW = os.path.join(ROLES, "cloudgateway_provision", "tasks", "node_gateway.yml")
P = "{{ bgw_node.name }}: "
T = P + "remove from FRR the VRFs its configuration no longer has"

# the replica's show vrf after the network t51003 was removed from gw-site-a (V2-detail.txt, as recorded)
SHOW_VRF = "vrf t51000 id 3 table 51000 (configured)\nvrf t51003 inactive (configured)\n"
GW_CONF = textwrap.dedent("""\
    frr version 8.5
    vrf t51000
     vni 51000
    exit-vrf
    !
    router bgp 65010
    !
    router bgp 65010 vrf t51000
     address-family ipv4 unicast
      sid vpn per-vrf export auto
     exit-address-family
    exit
    """)
GW_VARS = {"bgw_node": {"name": "bgw-1"}, "fab_name": "site-a"}
RT_VARS = {"bgw_node": {"name": "bgw-1"}}

VTYSH = textwrap.dedent("""\
    #!/bin/bash
    # stub: answers from $STUB/<file>, logs every call
    echo "$*" >> "$STUB/calls"
    case "$*" in
      *"-c show vrf"*) [ -e "$STUB/show-vrf" ] || exit 1; cat "$STUB/show-vrf" ;;
      *"-c show running-config"*) cat "$STUB/running" ;;
      *"-c configure terminal"*) cat "$STUB/no-vrf-out" 2>/dev/null; exit "$(cat "$STUB/no-vrf-rc" 2>/dev/null || echo 0)" ;;
      *) exit 1 ;;
    esac
    """)
SYSTEMCTL = textwrap.dedent("""\
    #!/bin/bash
    echo "systemctl $*" >> "$STUB/calls"
    [ -e "$STUB/active" ]
    """)


class _Node:
    def __init__(self, test, path, variables, conf_rel):
        self.d = tempfile.mkdtemp(prefix="shc-vrf-gone-")
        test.addCleanup(shutil.rmtree, self.d, True)
        self.bin, self.stub = os.path.join(self.d, "bin"), os.path.join(self.d, "stub")
        os.makedirs(self.bin)
        os.makedirs(self.stub)
        for name, text in (("vtysh", VTYSH), ("systemctl", SYSTEMCTL)):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.conf = os.path.join(self.d, conf_rel)
        os.makedirs(os.path.dirname(self.conf))
        # the script with its rendered configuration under the test's root
        self.script = render(shell_of(path, T), **variables).replace("conf=/etc/frr/", "conf=%s/etc/frr/" % self.d)
        self.put("active", "")
        self.put("running", "")

    def put(self, name, text):
        with open(os.path.join(self.stub, name), "w") as f:
            f.write(text)

    def drop(self, name):
        os.remove(os.path.join(self.stub, name))

    def run(self):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], STUB=self.stub)
        p = subprocess.run(["bash", "-s"], input=self.script, capture_output=True, text=True, env=env)
        try:
            with open(os.path.join(self.stub, "calls")) as f:
                calls = f.read().splitlines()
        except FileNotFoundError:
            calls = []
        return p.returncode, p.stdout, p.stderr, calls


def configured(calls):
    return [c for c in calls if "configure terminal" in c]


class GatewayRemoval(unittest.TestCase):
    def node(self):
        n = _Node(self, GW, GW_VARS, "etc/frr/gw-site-a/frr.conf.shc")
        with open(n.conf, "w") as f:
            f.write(GW_CONF)
        n.put("show-vrf", SHOW_VRF)
        n.put("running", GW_CONF)
        return n

    def test_the_removed_vrf_goes(self):
        rc, out, err, calls = self.node().run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines(), ["removed=t51003"])
        self.assertEqual(configured(calls), ["-N gw-site-a -c configure terminal -c no vrf t51003"])
        # the instance's own pathspace on every call
        self.assertTrue(all(c.startswith("-N gw-site-a ") for c in calls if not c.startswith("systemctl")), calls)
        self.assertIn("systemctl -q is-active shc-gw@site-a.service", calls)

    def test_nothing_to_remove_is_a_no_op(self):
        n = self.node()
        n.put("show-vrf", "vrf t51000 id 3 table 51000 (configured)\n")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines(), configured(calls)), (0, ["removed="], []))

    def test_a_vrf_of_the_configuration_stays_even_without_its_device(self):
        n = self.node()
        n.put("show-vrf", "vrf t51000 inactive (configured)\n")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines(), configured(calls)), (0, ["removed="], []))

    def test_an_active_vrf_stays(self):
        # a VRF with a device that the file does not name is not this step's business (the apply removes devices)
        n = self.node()
        n.put("show-vrf", SHOW_VRF.replace("t51003 inactive", "t51003 id 9 table 51003"))
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines(), configured(calls)), (0, ["removed="], []))

    def test_a_vrf_with_a_bgp_instance_left_stays(self):
        n = self.node()
        n.put("running", GW_CONF + "router bgp 65010 vrf t51003\n bgp router-id 10.255.0.2\nexit\n")
        rc, out, err, calls = n.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["removed=", "kept=t51003"])
        self.assertIn("VRF t51003: it still has a BGP instance", err)
        self.assertEqual(configured(calls), [])

    def test_a_refusal_is_reported_and_the_job_goes_on(self):
        n = self.node()
        n.put("show-vrf", SHOW_VRF + "vrf t51004 inactive (configured)\n")
        n.put("no-vrf-out", "% Only inactive VRFs can be deleted\n")
        n.put("no-vrf-rc", "1")
        rc, out, err, calls = n.run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["removed=", "kept=t51003 t51004"])
        self.assertIn("VRF t51003: FRR keeps it: % Only inactive VRFs can be deleted", err)
        self.assertEqual(len(configured(calls)), 2)
        # an error text with exit 0 (vtysh returns 0 for a plain warning) counts as a refusal too
        n.put("no-vrf-rc", "0")
        _, out, _, _ = n.run()
        self.assertEqual(out.splitlines(), ["removed=", "kept=t51003 t51004"])

    def test_nothing_while_the_instance_is_down_or_unreadable(self):
        n = self.node()
        n.drop("active")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out, [c for c in calls if not c.startswith("systemctl")]), (0, "", []))
        n = self.node()
        n.drop("show-vrf")
        rc, out, err, calls = n.run()
        self.assertEqual((rc, out, configured(calls)), (0, "", []))
        self.assertIn("unreadable", err)
        n = self.node()
        os.remove(n.conf)
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out, configured(calls)), (0, "", []))

    def test_a_vrf_named_only_by_its_bgp_instance_in_the_file_stays(self):
        n = self.node()
        with open(n.conf, "a") as f:
            f.write("router bgp 65010 vrf t51003\nexit\n")
        rc, out, _, calls = n.run()
        self.assertEqual((rc, out.splitlines(), configured(calls)), (0, ["removed="], []))


class RootRemoval(unittest.TestCase):
    CONF = textwrap.dedent("""\
        vrf egress
         ip route 0.0.0.0/0 192.0.2.1
        exit-vrf
        !
        vrf e10000
         ip route 0.0.0.0/0 169.254.0.1
        exit-vrf
        !
        router bgp 65010 vrf e10001
        exit
        """)

    def test_root(self):
        n = _Node(self, RT, RT_VARS, "etc/frr/frr.conf")
        with open(n.conf, "w") as f:
            f.write(self.CONF)
        # e10002 lost its egress and circuits (device gone); e10001 is circuit-only and still placed; egress is up
        n.put("show-vrf", "vrf e10000 id 5 table 10000 (configured)\nvrf e10001 inactive\n"
                          "vrf e10002 inactive (configured)\nvrf egress id 4 table 9000 (configured)\n")
        n.put("running", self.CONF)
        rc, out, err, calls = n.run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines(), ["removed=e10002"])
        self.assertEqual(configured(calls), ["-c configure terminal -c no vrf e10002"])
        self.assertIn("systemctl -q is-active frr.service", calls)


class Placement(unittest.TestCase):
    def test_same_code_on_root_and_gateway(self):
        g = shell_of(GW, T).splitlines()
        r = shell_of(RT, T).splitlines()
        self.assertEqual(g[1:], r[1:])
        self.assertEqual(r[0], "u=frr.service; conf=/etc/frr/frr.conf; V=(vtysh)")
        self.assertEqual(g[0], "u=shc-gw@{{ fab_name }}.service; conf=/etc/frr/gw-{{ fab_name }}/frr.conf.shc; "
                               "V=(vtysh -N gw-{{ fab_name }})")

    def test_after_the_reload_and_its_record_on_both_transports(self):
        # after the record and the two steps on what the load check found (a failure, the lines FRR does not take)
        for path, after, before in ((RT, "lines FRR does not take", "install the IPv6 egress guard"),
                                    (GW, "lines the instance does not take", "keep the SRv6 core guard")):
            tasks = load(path)
            names = [t.get("name", "") for t in tasks]
            i = names.index(T)
            self.assertEqual(names[i - 1], P + after)
            self.assertEqual(names[i + 1], P + before)
            task = tasks[i]
            self.assertNotIn("when", task)  # mpls and srv6 alike
            self.assertEqual(task["args"], {"executable": "/bin/bash"})

    def test_counts_as_a_node_change(self):
        for path, reg, fact in ((RT, "_hf_vrfs_gone", "hf_changed_nodes"), (GW, "_cg_vrfs_gone", "cg_changed_nodes")):
            tasks = load(path)
            self.assertEqual(by_name(tasks, T)["register"], reg)
            self.assertIn(reg + " | default({})", by_name(tasks, P + "record the node")["ansible.builtin.set_fact"][fact])

    def test_bash_syntax(self):
        for path, v in ((GW, GW_VARS), (RT, RT_VARS)):
            p = subprocess.run(["bash", "-n"], input=render(shell_of(path, T), **v), capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AnsibleView(unittest.TestCase):
    def test_render_is_ansible_render(self):
        for path, v in ((GW, GW_VARS), (RT, RT_VARS)):
            sh = shell_of(path, T)
            got = run_play([{"ansible.builtin.set_fact": {"rendered": sh}}, write_out("rendered")], copy.deepcopy(v))
            self.assertEqual(render(sh, **v).rstrip("\n"), got.rstrip("\n"))

    def test_changed_only_when_something_was_removed(self):
        for path in (RT, GW):
            task = by_name(load(path), T)
            expr = task["changed_when"].replace(task["register"], "_r")
            got = run_cases([{"ansible.builtin.set_fact": {"_r": {"stdout": "{{ case }}"}}}],
                            ["removed=", "removed=t51003", "removed=\nkept=t51003", "removed=e1 e2\nkept=e3", ""],
                            "(%s) | bool" % expr)
            self.assertEqual(got, [False, True, False, True, False])


if __name__ == "__main__":
    unittest.main()

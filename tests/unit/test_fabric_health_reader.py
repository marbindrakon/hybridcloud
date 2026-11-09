"""The fabric health job's node reader (eda/common/files/shc_node_health.py) with every command replaced: what it
reads per instance, that every command it runs is a read and carries its own time limit, and how it reports a failed
boot apply, a crash-looping gateway, a daemon down, a coredump, kernel drift, a reload not done, the IPv6 egress and
the site sessions."""
import base64
import importlib.util
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PATH = os.path.join(ROOT, "eda/common/files/shc_node_health.py")
spec = importlib.util.spec_from_file_location("shc_node_health", PATH)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

NOW = 1_800_000_000
ROOT_JSON = {"type": "root", "egress": {"vrf": "egress", "ipv6": {"address": "2001:db8:237::13/64", "wanMtu": 1500},
                                         "networks": [{"vrf": "e10000", "ipv6": {"block": "2001:db8:4000::/56"}},
                                                      {"vrf": "e10001"}]}}
WRITES = ("add", "del", "set", "restart", "start", "stop", "reload", "enable", "disable", "-w", "configure", "flush",
          "kill", "up", "down")


def show(units):
    blocks = []
    for name, p in units.items():
        d = {"Id": name, "LoadState": "loaded", "ActiveState": "active", "SubState": "exited", "Result": "success",
             "NRestarts": "0", "UnitFileState": "enabled", "InvocationID": "inv-" + name}
        d.update(p)
        blocks.append("\n".join("%s=%s" % kv for kv in d.items()))
    return "\n\n".join(blocks) + "\n"


class Node:
    """A fake node: the files and the answers of the commands the reader runs."""

    def __init__(self):
        self.files = {"/etc/shc-fabric/root.json": json.dumps(ROOT_JSON), "/proc/uptime": "600.5 1000.0",
                      "/proc/sys/kernel/random/boot_id": "boot-1\n",
                      "/etc/shc-fabric/root.applied": "lo 10.255.0.1/32\nbr-core UP\n",
                      "/etc/shc-fabric/gw-atuin.applied": "gw-atuin\nt51003\n",
                      "/etc/frr/frr.conf": "frr root\n", "/etc/frr/daemons": "bgpd=yes\n",
                      "/etc/frr/gw-atuin/frr.conf.shc": "frr gw\n"}
        self.files["/etc/shc-fabric/root.frr-loaded"] = R.digest(["/etc/frr/frr.conf", "/etc/frr/daemons"], self.read)
        self.files["/etc/shc-fabric/gw-atuin.frr-loaded"] = R.digest(["/etc/frr/gw-atuin/frr.conf.shc"], self.read)
        self.units = {u: {} for u in ("shc-fabric-apply@root.service", "frr.service", "shc-gw@atuin.service",
                                      "shc-egress6-guard.timer", "shc-core-guard@root.timer",
                                      "shc-core-guard@gw-atuin.timer")}
        self.units["frr.service"]["SubState"] = "running"
        self.units["shc-gw@atuin.service"]["SubState"] = "running"
        self.snap = {"root": "lo 10.255.0.1/32\nbr-core UP\n", "gw-atuin": "gw-atuin\nt51003\n"}
        self.watch = {"": ["zebra", "bgpd", "isisd", "staticd"], "gw-atuin": ["zebra", "bgpd", "isisd", "staticd"]}
        self.down = {}
        self.journal = {}
        self.coredumps = []
        self.peers6 = {"2001:db8:237::2": {"state": "Established", "remoteAs": 65002},
                       "2001:db8:237::3": {"state": "Established", "remoteAs": 65002}}
        self.site = {"172.18.103.10": {"state": "Established"}, "172.18.103.11": {"state": "Established"},
                     "10.255.0.1": {"state": "Established"}}
        self.calls = []

    def read(self, path):
        return self.files.get(path)

    def run(self, cmd, t=None):
        self.calls.append((list(cmd), t))
        c = " ".join(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            names = cmd[cmd.index("--") + 1:]
            return 0, show({n: self.units.get(n, {"LoadState": "not-found", "ActiveState": "inactive",
                                                  "UnitFileState": ""}) for n in names})
        if cmd[:2] == ["systemctl", "list-units"]:
            return 0, "".join("%s loaded failed failed x\n" % u for u, p in self.units.items()
                              if p.get("ActiveState") == "failed")
        if cmd[0] == "journalctl" and "-u" in cmd:
            return 0, "\n".join(self.journal.get(cmd[cmd.index("-u") + 1], [])) + "\n"
        if cmd[0] == "journalctl":
            return 0, "\n".join(json.dumps(e) for e in self.coredumps) + "\n"
        if cmd[0] == R.APPLY:
            return 0, self.snap[cmd[1]]
        if cmd[0] == "vtysh":
            ps = cmd[2] if cmd[1] == "-N" else ""
            q = cmd[-1]
            if q == "show watchfrr":
                rows = ["watchfrr global phase: Idle", " Daemon status:"]
                rows += ["  %-20s %s" % (d, "Down" if d in self.down.get(ps, []) else "Up") for d in self.watch[ps]]
                return 0, "\n".join(rows) + "\n"
            if "l2vpn evpn summary" in q:
                return 0, json.dumps({"peers": self.site})
            if "ipv6 unicast summary" in q:
                return 0, json.dumps({"peers": self.peers6})
            if "ipv6 route vrf" in q:
                return 0, json.dumps({"::/0": [{"protocol": "bgp"}]})
        if cmd[:3] == ["ip", "-j", "-6"]:
            return 0, json.dumps([{"addr_info": [{"local": "2001:db8:237::13"}]}])
        if cmd[:2] == ["nft", "list"]:
            return 0, "table inet shc_egress6 {}\n"
        raise AssertionError("unexpected command: " + c)

    def read_node(self, srv6=True):
        inp = {"instances": [{"name": "root", "kind": "root"},
                             {"name": "gw-atuin", "kind": "gateway", "siteCidr": "172.18.103.0/24"}],
               "transport": "srv6" if srv6 else "mpls", "windowSeconds": 3600, "cmdTimeout": 15}
        return R.read_node(inp, self.run, self.read, now=NOW)


class ReaderTest(unittest.TestCase):
    def test_healthy_node(self):
        n = Node()
        out = n.read_node()
        root, gw = out["instances"]["root"], out["instances"]["gw-atuin"]
        self.assertEqual(out["bootedAt"], NOW - 600)
        self.assertEqual(out["failedUnits"], [])
        self.assertEqual(root["start"]["active"], "active")
        self.assertEqual(root["start"]["lines"], [])
        self.assertEqual(root["kernelDrift"], [])
        self.assertEqual(root["frr"], {"reloadDue": False, "recorded": True, "pending": None})
        self.assertEqual(set(root["routing"]["daemons"].values()), {"Up"})
        self.assertTrue(root["egress6"]["healthy"])
        self.assertEqual(root["egress6"]["networks"], 1)
        self.assertEqual(gw["siteSessions"], {"established": 2, "known": 2})
        self.assertEqual(sorted(root["units"]), ["frr.service", "shc-core-guard@root.timer",
                                                  "shc-egress6-guard.timer", "shc-fabric-apply@root.service"])
        self.assertEqual(sorted(gw["units"]), ["shc-core-guard@gw-atuin.timer", "shc-gw@atuin.service"])

    def test_mpls_has_no_core_guard(self):
        out = Node().read_node(srv6=False)
        self.assertNotIn("shc-core-guard@root.timer", out["instances"]["root"]["units"])

    def test_every_command_reads_and_is_bounded(self):
        n = Node()
        n.units["shc-fabric-apply@root.service"].update(ActiveState="failed", Result="exit-code")
        n.read_node()
        for cmd, t in n.calls:
            words = set(cmd) - {"--"}
            if cmd[0] == R.APPLY:
                self.assertEqual(cmd[2], "snapshot")
            elif cmd[0] == "vtysh":
                self.assertTrue(cmd[-1].startswith("show "), cmd)
            else:
                self.assertFalse(words & set(WRITES) - {"show"}, cmd)
        # the real runner bounds every command
        run = R.make_run(1)
        rc, _ = run([sys.executable, "-c", "import time; time.sleep(5)"])
        self.assertEqual(rc, 124)

    def test_failed_boot_apply_and_its_reason(self):
        n = Node()
        n.units["shc-fabric-apply@root.service"].update(ActiveState="failed", Result="exit-code", SubState="failed")
        n.journal["shc-fabric-apply@root.service"] = ["something else", "transport srv6: net.vrf.strict_mode=1 refused"]
        out = n.read_node()
        st = out["instances"]["root"]["start"]
        self.assertEqual((st["active"], st["result"]), ("failed", "exit-code"))
        self.assertEqual(st["lines"], ["transport srv6: net.vrf.strict_mode=1 refused"])
        self.assertIn("shc-fabric-apply@root.service", out["failedUnits"])
        self.assertTrue(any("_SYSTEMD_INVOCATION_ID=inv-shc-fabric-apply@root.service" in c for c, _ in
                            [(" ".join(x), t) for x, t in n.calls]))

    def test_root_applied_without_ipv6_keeps_the_warning(self):
        n = Node()
        n.journal["shc-fabric-apply@root.service"] = ["warning: router advertisements on eno1; IPv6 core not applied",
                                                      "changed=yes"]
        st = n.read_node()["instances"]["root"]["start"]
        self.assertEqual(st["lines"], ["warning: router advertisements on eno1; IPv6 core not applied"])

    def test_gateway_crash_loop_daemon_down_and_coredump(self):
        n = Node()
        n.units["shc-gw@atuin.service"].update(ActiveState="activating", SubState="auto-restart", NRestarts="7")
        n.down[""] = ["zebra"]
        n.coredumps = [{"COREDUMP_EXE": "/usr/lib/frr/zebra", "COREDUMP_CMDLINE": "/usr/lib/frr/zebra -d -F traditional",
                        "COREDUMP_TIMESTAMP": str((NOW - 100) * 1000000)},
                       {"COREDUMP_EXE": "/usr/lib/frr/bgpd", "COREDUMP_CMDLINE": "/usr/lib/frr/bgpd -N gw-atuin -d",
                        "COREDUMP_TIMESTAMP": str((NOW - 50) * 1000000)},
                       {"COREDUMP_EXE": "/usr/bin/python3.9", "COREDUMP_CMDLINE": "python3"}]
        out = n.read_node()
        gw, root = out["instances"]["gw-atuin"], out["instances"]["root"]
        self.assertEqual((gw["start"]["sub"], gw["start"]["restarts"]), ("auto-restart", 7))
        self.assertIsNone(gw["routing"]["daemons"])  # not read while the instance is not running
        self.assertEqual(root["routing"]["daemons"]["zebra"], "Down")
        self.assertEqual([c["daemon"] for c in root["routing"]["crashes"]], ["zebra"])
        self.assertEqual([c["daemon"] for c in gw["routing"]["crashes"]], ["bgpd"])

    def test_kernel_and_frr_drift(self):
        n = Node()
        n.snap["root"] = "lo 10.255.0.1/32 10.255.0.250/32\nbr-core UP\n"
        n.files["/etc/frr/frr.conf"] = "frr root changed\n"
        root = n.read_node()["instances"]["root"]
        self.assertEqual(root["kernelDrift"], ["-lo 10.255.0.1/32", "+lo 10.255.0.1/32 10.255.0.250/32"])
        self.assertTrue(root["frr"]["reloadDue"])

    def test_no_record_is_not_drift(self):
        n = Node()
        del n.files["/etc/shc-fabric/root.applied"]
        del n.files["/etc/shc-fabric/root.frr-loaded"]
        root = n.read_node()["instances"]["root"]
        self.assertIsNone(root["kernelDrift"])
        self.assertFalse(root["frr"]["reloadDue"])
        self.assertFalse(root["frr"]["recorded"])

    def test_upstream_down_and_site_sessions_down(self):
        n = Node()
        n.peers6 = {a: dict(p, state="Idle") for a, p in n.peers6.items()}
        n.site = {"172.18.103.10": {"state": "Active"}, "10.255.0.1": {"state": "Established"}}
        out = n.read_node()
        self.assertFalse(out["instances"]["root"]["egress6"]["healthy"])
        self.assertEqual(out["instances"]["gw-atuin"]["siteSessions"], {"established": 0, "known": 1})

    def test_runs_as_the_job_runs_it(self):
        """python3 - <base64 input> with the file on stdin: prints one JSON line (here every command is missing or
        fails, which the reader reports instead of failing)."""
        inp = base64.b64encode(json.dumps({"instances": [], "transport": "mpls"}).encode()).decode()
        with open(PATH) as f:
            p = subprocess.run([sys.executable, "-", inp], stdin=f, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["instances"], {})


if __name__ == "__main__":
    unittest.main()

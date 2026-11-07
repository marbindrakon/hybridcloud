"""The SRv6 prechecks of shc-fabric-apply read "ip -j -d link show type vrf". On the border gateway blades (RHEL 9.8,
kernel 5.14.0-687, iproute2 6.17) a listing by kind prints one empty object per link while the kind's module is not
loaded, which is the state of a fresh boot before the root apply's modprobe (the fake kernel models it for every
listing by kind that matches nothing). The first live reboot on SRv6 (2026-11-07) refused the root
apply at boot with "VRFs that share a table: table null: , , , , , , , ," and left the node without its VRFs. The
prechecks must read only entries that name a device."""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import ApplyCase, blade, writes  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6, root_full  # noqa: E402

APPLY = os.path.join(os.path.dirname(__file__), "..", "..", "eda", "common", "files", "bgw", "shc-fabric-apply")


class VrfJsonAtBoot(unittest.TestCase):
    def test_the_jq_programs_ignore_empty_entries(self):
        # the three jq programs of the prechecks, fed the blade's boot-time output
        with open(APPLY) as f:
            src = f.read()
        programs = [l for l in src.splitlines() if "ip -j -d link show type vrf | jq -r" in l]
        self.assertEqual(len(programs), 3, programs)
        for l in programs:
            self.assertIn("def vrfs: map(select(.ifname != null));", l)
            self.assertNotIn("[.[] |", l)

    def test_fresh_boot_shape_passes_the_prechecks(self):
        boot = json.dumps([{} for _ in range(9)])  # bgw-1 at boot, 2026-11-07 05:36 UTC
        prog = ('def vrfs: map(select(.ifname != null)); [vrfs | group_by(.linkinfo.info_data.table | tostring)[] '
                '| select(length > 1) | "table \\(.[0].linkinfo.info_data.table): \\(map(.ifname) | join(", "))"] | join("; ")')
        out = subprocess.run(["jq", "-r", prog], input=boot, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "")
        shared = json.dumps([{"ifname": "red", "linkinfo": {"info_data": {"table": 77}}},
                             {"ifname": "blue", "linkinfo": {"info_data": {"table": 77}}}, {}])
        out = subprocess.run(["jq", "-r", prog], input=shared, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "table 77: red, blue")


class FreshBootApply(ApplyCase):
    def test_root_up_on_a_host_without_vrfs(self):
        # the fake kernel now answers "type vrf" with empty objects while no VRF exists: a fresh root apply must pass
        # its prechecks and create its VRFs
        h = self.h
        h.set_state(blade())
        h.conf("root", root_full(**ROOT_SRV6))
        rc, out, err, log = h.run("root")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("share a table", err)
        self.assertTrue(any("link add" in l and "type vrf" in l for l in writes(log)), writes(log)[:10])


if __name__ == "__main__":
    unittest.main()

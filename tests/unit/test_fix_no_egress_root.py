"""shc-fabric-apply under errexit and pipefail: a lookup of something that is not there (yet) reads as "not there"
instead of failing the run.
- A root without internet egress and without circuits (a fabric that never had egress0): up applies cleanly, a second
  up reads changed=no, down and up again work; IPv4 only and with the IPv6 core on mpls, and on srv6. The release
  before this fix stopped there (the lookup of egress0's VRF failed the pipeline), checked against it where the
  checkout holds it.
- The root's router advertisement check: a route from router advertisements whose device is gone since the route was
  read does not count as a host interface that relies on them.
- post (gateway, after FRR starts): vtysh fails until the instance's daemons answer; the wait goes on instead of the
  run ending at the first try.
Run against the fake kernel of test_shc_fabric_apply. Documentation prefixes and an arbitrary block of 5f00::/16."""
import copy
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_shc_fabric_apply import GW_CONF, ROOT6, ROOT_CONF, SCRIPT_REL, ApplyCase, ROOT, blade, writes  # noqa: E402
from test_srv6_fabric_apply import ROOT_SRV6  # noqa: E402

BASE = "9c6f99fb"  # the integrated head before this fix


def git_show(rel):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{rel}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def bare_root(**over):
    """the root of a fabric without internet egress and without attachment circuits"""
    c = {k: copy.deepcopy(v) for k, v in ROOT_CONF.items() if k not in ("egress", "attachments")}
    c.update(copy.deepcopy(over))
    return c


VARIANTS = (("mpls, IPv4 only", {}), ("mpls, IPv6 core", ROOT6), ("srv6", dict(ROOT6, **ROOT_SRV6)))


class NoEgressRoot(ApplyCase):
    def test_up_twice_down_up(self):
        for name, over in VARIANTS:
            with self.subTest(variant=name):
                h = self.h
                h.set_state(blade())
                h.conf("root", bare_root(**over))
                out, log = h.ok(self, "root")
                self.keep("no-egress-root-" + name.replace(", ", "-").replace(" ", "-"), log, out)
                self.assertTrue(out.strip().endswith("changed=yes"), out)
                st = h.get_state()["ns"][""]
                self.assertNotIn("egress0", st["links"])
                self.assertEqual([d for d, l in st["links"].items() if l.get("kind") == "vrf"], [])
                self.assertIn("br-core", st["links"])
                out, _ = h.ok(self, "root")
                self.assertTrue(out.strip().endswith("changed=no"), out)
                h.ok(self, "root", "down")
                self.assertNotIn("br-core", h.get_state()["ns"][""]["links"])
                h.ok(self, "root", "down")  # down of a root that is down already
                out, _ = h.ok(self, "root")
                self.assertTrue(out.strip().endswith("changed=yes"), out)

    def test_release_before_stopped_there(self):
        base = git_show(SCRIPT_REL)
        if base is None:
            self.skipTest(f"{BASE} not in this checkout")
        h = self.h
        h.scripts["old"] = h.copy_script(base, "apply-old")
        for name, over in VARIANTS:
            with self.subTest(variant=name):
                h.set_state(blade())
                h.conf("root", bare_root(**over))
                rc, out, err, log = h.run("root", script="old")
                self.assertNotEqual(rc, 0, out)
                self.assertNotIn("changed=", out)
                self.assertRegex(log[-1], r"^R \[root\] ip -j link show egress0  -> rc=")


class RouterAdvertisementDeviceGone(ApplyCase):
    RA = {"type": "unicast", "dst": "2001:db8:77::/64", "via": "fe80::1", "dev": "gone0", "proto": "ra",
          "metric": "1024", "table": "main"}

    def test_gone_device_does_not_count(self):
        h = self.h
        st = blade()
        st["ns"][""]["routes6"].append(dict(self.RA))
        h.set_state(st)
        h.conf("root", bare_root(**dict(ROOT6, **ROOT_SRV6)))
        rc, out, err, log = h.run("root")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("relies on router advertisements", out + err)


class PostWaitsForFrr(ApplyCase):
    def setUp(self):
        super().setUp()
        h = self.h
        frr = os.path.join(self.tmp, "frr")
        os.makedirs(os.path.join(frr, "gw-iaas1"))
        with open(os.path.join(frr, "gw-iaas1", "frr.conf"), "w") as f:
            f.write("router bgp 65010\n bgp router-id 172.18.236.4\nexit\n")
        for k, p in list(h.scripts.items()):
            with open(p) as f:
                text = f.read()
            with open(p, "w") as f:
                f.write(text.replace("/etc/frr/", frr + "/"))

    def test_vtysh_failing_is_waited_on(self):
        h = self.h
        h.conf("gw-iaas1", dict(copy.deepcopy(GW_CONF), vrfs=[]))
        rc, out, err, log = h.run("gw-iaas1", "post", fail=r"vtysh -N gw-iaas1 -c show vrf")
        self.assertEqual(rc, 0, err)
        self.assertTrue([l for l in log if "show vrf" in l and l.endswith("-> rc=2")], log)
        self.assertTrue([l for l in log if "show bgp summary" in l], log)


if __name__ == "__main__":
    unittest.main()

"""shc-fabric-apply with transport absent or "mpls" against the script of the release before the SRv6 transport (BASE),
run against the fake kernel of test_shc_fabric_apply: the same calls in the same order, the same output, kernel state
after every run, and files, except for exactly the differences this file lists as data (calls added or removed,
nftables text, and the IPv4 rules and routes the fixes keep; every one with its reason). Any other difference fails.
Two instance sets, two rounds up, then down: a
dual-stack root with IPv6 egress and an IPv4 and an IPv6 circuit, a VLAN gateway with icmp6Announced and a WireGuard
gateway; and the same IPv4 only. Skipped where the history lacks BASE."""
import copy
import difflib
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_root_circuit_ipv6 as C  # noqa: E402
from test_shc_fabric_apply import CIRCUIT, GW_CONF, ROOT_CONF, WG6, WG_CONF, ApplyCase, blade  # noqa: E402
from test_srv6_fabric_apply import gw_full, root_full  # noqa: E402

BASE = "72f86002"
METRIC = "4278198272"
NS = r"\[[a-z0-9-]+\]"
SLICE = "2001:db8:4005:1::1:0/112"  # the first node's slice of icmpSource6 2001:db8:4005:1::/64, as the plan gives it
ROOT_ICMP6 = "2001:db8:4005:1::1:1"  # the root's own ICMPv6 source on that node (ROOT6), in the slice

# Calls the script issues that BASE does not (a line of the call log, as a regular expression), with the reason.
ADDED = [
    # F-L1: the VRFs' IPv4 separation, both transports
    (r"^R %s ip -4 rule show$" % NS, "F-L1: the IPv4 rules read (precheck, the move, the snapshot)"),
    (r"^W %s ip -4 rule add pref 32765 table local$" % NS, "F-L1: the local table behind the VRFs' rule"),
    (r"^W %s ip -4 rule del pref 0 table local$" % NS, "F-L1: (added before the old one goes)"),
    (r"^W \[root\] ip -4 rule add pref 0 table local$", "F-L1: root down puts the kernel's order back"),
    (r"^W \[root\] ip -4 rule del pref 32765 table local$", "F-L1: root down"),
    (r"^R %s ip -4 route show table \d+$" % NS, "F-L1: a VRF's IPv4 unreachable default, read"),
    (r"^W %s ip -4 route replace unreachable default metric %s table \d+$" % (NS, METRIC),
     "F-L1: a VRF's IPv4 unreachable default"),
    (r"^R %s ip -4 route show table all$" % NS, "F-L1: the snapshot"),
    (r"^W \[root\] ip -4 route del unreachable default metric %s table \d+$" % METRIC,
     "F-L1: a removed VRF's IPv4 unreachable default"),
    (r"^R \[root\] ip -j -d link show (egress|e\d+)$", "F-L1: a removed root VRF's table, read without the IPv6 keys too"),
    (r"^R \[root\] ip netns list$", "F-L1: the gateway's precheck (a namespace that exists already), on mpls too"),
    # F-L6: a VRF other than the configuration's on a table it will use, both transports
    (r"^R %s ip -j -d link show type vrf$" % NS, "F-L6: the namespace's VRFs and their tables, read by the precheck"),
    # egress0's own MAC (2026-11-07: a random one per boot left the hand-off router's ARP cache stale for the NAT pool)
    (r"^W \[root\] ip link add egress0 link \S+ address 02:e9(:[0-9a-f]{2}){4} type macvlan mode bridge$",
     "egress0 created with a MAC derived from the egress address"),
    (r"^R \[root\] cat /sys/class/net/egress0/address$", "egress0's MAC, checked on every run"),
]
# Calls of BASE the script no longer issues.
REMOVED = [
    (r"^W \[root\] ip link add egress0 link \S+ type macvlan mode bridge$", "egress0 is created with its MAC now"),
]

# nftables text the script loads differently: (table, regular expression on BASE's text, replacement, reason, always).
# Each applies once wherever BASE loaded that table (always: it must; else: where the text has it, at most once); the
# edited text must then equal this script's.
NFT = [
    ("inet shc_egress6",
     r'(    oifname "xe\*" [^\n]* comment "mss out"\n)    iifname "xe\*" tcp flags & syn == syn tcp option maxseg size > '
     r'(\d+) counter tcp option maxseg size set \2 comment "mss in"\n(  \}\n  chain clamp_forward [^\n]*\n'
     r'  chain clamp_output [^\n]*\n)',
     r'\1\3  chain clamp_return {\n    type filter hook forward priority mangle; policy accept;\n    meta nfproto ipv6 '
     r'oifname "xo*" tcp flags & syn == syn tcp option maxseg size > \2 counter tcp option maxseg size set \2 '
     r'comment "mss in, at the egress veth"\n  }\n',
     "F-L2a: the IPv6 clamp into a network at the egress VRF's veth xo<n> (forward) instead of iifname xe* (never "
     "matched)", True),
    ("inet shc_gw_mss",
     r'  chain clamp \{\n    type filter hook forward priority mangle; policy accept;\n    oifname "vx\*" tcp flags syn '
     r'tcp option maxseg size set rt mtu\n  \}\n',
     "",
     "F-L2b: mpls: no forward chain (its oifname vx* rule never matched; nothing reaches br<vni> by forward); the "
     "output chain clamp_core stays", True),
    ("inet shc_egress6",
     r'(elements = \{ [^}]*, %s), %s \}' % (re.escape(SLICE), re.escape(ROOT_ICMP6)), r'\1 }',
     "F-L3: the root's ICMPv6 source left out of src6 where the node's slice of icmpSource6 (from the plan) covers it "
     "(nftables refuses the overlap)", False),
]

KERNEL4 = [{"pref": "0", "table": "local"}, {"pref": "32766", "table": "main"}, {"pref": "32767", "table": "default"}]
L3MDEV = {"pref": "1000", "table": "[l3mdev-table]"}
MOVED4 = [L3MDEV, {"pref": "32765", "table": "local"}, {"pref": "32766", "table": "main"},
          {"pref": "32767", "table": "default"}]


def by_pref(rules):
    return sorted(rules, key=lambda r: int(r["pref"]))


def allowed(line, table):
    return any(re.search(rx, line) for rx, _ in table)


def compare_logs(case, what, old, new):
    """the call logs equal but for the listed differences (ADDED in new, REMOVED from old), in the same order"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        gone = [l for l in old[i1:i2] if not allowed(l, REMOVED)]
        came = [l for l in new[j1:j2] if not allowed(l, ADDED)]
        case.assertEqual(gone, came, "%s: calls differ from the base beyond the listed differences:\n- %s\n+ %s" % (
            what, "\n- ".join(old[i1:i2]), "\n+ ".join(new[j1:j2])))


def normalize(case, what, st, root_down=False):
    """the new state with the listed differences checked and taken out (root_down: after the root's down)"""
    st = copy.deepcopy(st)
    for name, ns in st["ns"].items():
        vrf_tables = sorted(str(l["table"]) for l in ns["links"].values() if l["kind"] == "vrf")
        # F-L1: the local rule behind the l3mdev rule (root down: the kernel's order again)
        rules = ns.pop("rules4", None)
        if rules is not None:
            want = by_pref(KERNEL4 + [L3MDEV]) if (name == "" and root_down) else (
                MOVED4 if (vrf_tables or L3MDEV in rules) else MOVED4[1:])
            case.assertEqual(by_pref(rules), by_pref(want), "%s: IPv4 rules in [%s]" % (what, name or "root"))
        # F-L1: one IPv4 unreachable default per VRF table, nothing else
        routes = ns.pop("routes4", None)
        if routes is not None:
            case.assertEqual(sorted(r["table"] for r in routes), vrf_tables, "%s: [%s]" % (what, name or "root"))
            for r in routes:
                case.assertEqual({k: r[k] for k in ("type", "dst", "metric")},
                                 {"type": "unreachable", "dst": "default", "metric": METRIC}, what)
    return st


def assert_runs_like_base(case, what, old_runs, new_runs):
    """runs: [(stdout, call log)] of the base script and of this one, the same instances in the same order"""
    case.assertEqual(len(old_runs), len(new_runs), what)
    for i, ((oo, ol), (no, nl)) in enumerate(zip(old_runs, new_runs)):
        compare_logs(case, "%s, run %d" % (what, i + 1), ol, nl)
        case.assertEqual(oo, no, what)


def base_edited(case, what, st, used=None):
    """BASE's state with the listed nftables differences applied (each exactly once per table it loaded)"""
    st = copy.deepcopy(st)
    for name, ns in st["ns"].items():
        for table, rx, repl, why, always in NFT:
            if table in ns["nft"]:
                ns["nft"][table], n = re.subn(rx, repl, ns["nft"][table])
                case.assertIn(n, (1,) if always else (0, 1), "%s: [%s] %s: %s" % (what, name or "root", table, why))
                if used is not None and n:
                    used.add(rx)
    return st


def assert_state_like_base(case, what, old, new, root_down=False, used=None):
    for ns in old["ns"].values():  # the base script never touches what the fixes add
        case.assertNotIn("rules4", ns, what)
        case.assertNotIn("routes4", ns, what)
    old, new = base_edited(case, what, old, used), normalize(case, what, new, root_down)
    # egress0's MAC: random from the fake kernel under BASE, derived from the egress address now; the fake kernel's
    # generated MACs of the devices created after it shift by one
    for st in (old, new):
        st.pop("macseq", None)
        for ns in st["ns"].values():
            for name, link in ns["links"].items():
                if name == "egress0" and st is new:
                    case.assertRegex(link["address"], r"^02:e9(:[0-9a-f]{2}){4}$", what)
                if name == "egress0" or re.match(r"^02:00:00:00:", link["address"] or ""):
                    link["address"] = "MAC"
                    for a in link.get("addrs", []):  # the link-local address the fake kernel derives from the MAC
                        if str(a.get("local", "")).startswith("fe80::"):
                            a["local"] = "LL"
    case.assertEqual(old, new, what)


class AgainstBase(ApplyCase):
    @classmethod
    def setUpClass(cls):
        try:
            p = subprocess.run(["git", "-C", C.ROOT, "show", "%s:eda/common/files/bgw/shc-fabric-apply" % BASE],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            p = None
        if p is None or p.returncode != 0:
            raise unittest.SkipTest("%s not in this checkout's history" % BASE)
        cls.baseline = p.stdout

    # ---------------------------------------------------------------------------------------------- the sequences
    def instances(self, which):
        if which == "dual":
            root = root_full()
            root["egress"]["networks"][0]["ipv6"]["src6"] = [root["egress"]["networks"][0]["ipv6"]["block"], SLICE]
            return [("root", root), ("gw-iaas1", gw_full()), ("gw-aws1", dict(copy.deepcopy(WG_CONF), **WG6))]
        root = copy.deepcopy(ROOT_CONF)
        root["attachments"]["circuits"] = [copy.deepcopy(CIRCUIT)]
        return [("root", root), ("gw-iaas1", copy.deepcopy(GW_CONF)), ("gw-aws1", copy.deepcopy(WG_CONF))]

    def sequence(self, script, which, extra):
        h = self.h
        h.set_state(blade())
        for f in os.listdir(h.etc):
            os.unlink(os.path.join(h.etc, f))
        runs = []
        steps = [(name, conf, "up") for _ in range(2) for name, conf in self.instances(which)]
        steps += [(name, None, "down") for name in ("gw-aws1", "gw-iaas1", "root")]
        for name, conf, act in steps:
            if conf is not None:
                h.conf(name, dict(conf, **extra))
            out, log = h.ok(self, name, act, script=script)
            runs.append({"what": "%s %s" % (name, act), "out": out, "log": log, "state": h.get_state(),
                         # (a listed difference: the newer apply also records what it left, <instance>.applied)
                         "files": sorted(f for f in os.listdir(h.etc) if not f.endswith(".applied"))})
        return runs

    def check(self, which, extra, used=None):
        old, new = self.sequence("old", which, {}), self.sequence("new", which, extra)
        for o, n in zip(old, new):
            what = "%s %s %s" % (which, extra or "absent", n["what"])
            compare_logs(self, what, o["log"], n["log"])
            self.assertEqual(o["out"], n["out"], what)
            self.assertEqual(o["files"], n["files"], what)
            assert_state_like_base(self, what, o["state"], n["state"], n["what"] == "root down", used)

    def test_dual_stack(self):
        used = set()
        for extra in ({}, {"transport": "mpls"}):
            self.check("dual", extra, used)
        # every listed nftables difference occurred
        self.assertEqual([why for _, rx, _, why, _ in NFT if rx not in used], [])

    def test_ipv4_only(self):
        self.check("ipv4", {})

    def test_an_unlisted_difference_fails(self):
        base = ["R [root] ip netns list", "W [root] ip link add egress type vrf table 9000", "W [root] nft -f -"]
        compare_logs(self, "listed", base, base[:2] + ["W [root] ip -4 rule add pref 32765 table local"] + base[2:])
        for new in (base[:2] + ["W [root] ip link set egress up"] + base[2:], base[:1] + base[2:], base[::-1]):
            with self.assertRaises(AssertionError):
                compare_logs(self, "unlisted", base, new)

    def test_listed_differences_are_seen(self):
        """every listed difference occurs in the sequences (a stale entry would hide nothing, but says what isn't so)"""
        seen = set()
        for which in ("dual", "ipv4"):
            for run in self.sequence("new", which, {}):
                for l in run["log"]:
                    seen |= {rx for rx, _ in ADDED if re.search(rx, l)}
        self.assertEqual([rx for rx, _ in ADDED if rx not in seen], [])


if __name__ == "__main__":
    unittest.main()

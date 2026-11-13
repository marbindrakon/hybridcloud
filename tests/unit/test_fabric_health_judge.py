"""The fabric health job's judge (eda/common/files/shc_fabric_health.py): findings, conditions, events and wakes from
the node reads, with paused sites, a held lease, repeated sightings, backoff and recovery; and that its writes are
partial status writes that the provisioning jobs' writes keep (status library, compute_status_patch)."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PATH = os.path.join(ROOT, "eda/common/files/shc_fabric_health.py")
spec = importlib.util.spec_from_file_location("shc_fabric_health", PATH)
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)
sys.path.insert(0, os.path.join(ROOT, "eda/rulebooks/collections"))
from ansible_collections.hybridsovereign.shc.plugins.module_utils import shc_status as S  # noqa: E402

NOW = "2026-11-10T10:00:00Z"
LATER = "2026-11-10T10:10:00Z"
MUCH_LATER = "2026-11-10T11:30:00Z"


def unit(active="active", **kw):
    d = {"active": active, "sub": "running" if active == "active" else "dead", "result": "success", "restarts": 0,
         "enabled": "enabled", "loaded": True}
    d.update(kw)
    return d


def inst(kind="root", **kw):
    su = "shc-fabric-apply@root.service" if kind == "root" else "shc-gw@atuin.service"
    ru = "frr.service" if kind == "root" else su
    d = {"start": dict(unit(), unit=su, lines=[], incomplete=[]), "units": {su: unit(), ru: unit()},
         "routing": {"unit": ru, "daemons": {"zebra": "Up", "bgpd": "Up"}, "crashes": []},
         "kernelDrift": [], "frr": {"reloadDue": False, "recorded": True, "pending": None}}
    if kind == "root":
        d["egress6"] = {"address": "2001:db8::13/64", "upstream": [{"address": "2001:db8::2", "asn": 65002,
                                                                    "state": "Established"}],
                        "default": True, "table": True, "networks": 1, "healthy": True, "errors": []}
    else:
        d["siteSessions"] = {"established": 2, "known": 2}
    for k, v in kw.items():
        d[k] = v
    return d


def node(name, root=None, gw=None, reachable=True, booted=1_800_000_000):
    if not reachable:
        return {"name": name, "reachable": False, "error": "ssh: connect timed out"}
    return {"name": name, "reachable": True,
            "read": {"bootedAt": booted, "failedUnits": [],
                     "instances": {"root": root or inst(), "gw-atuin": gw or inst("gateway")}}}


def req(nodes, fab_status=None, gws=None, now=NOW, skipped=None, health=None, e6=True):
    return {"now": now, "health": health or {"intervalSeconds": 600},
            "egress6": {"enabled": e6, "sessionDownAlertMinutes": 5},
            "fabric": {"name": "lab", "namespace": "sovereign-cloud", "ready": True, "status": fab_status or {}},
            "gateways": gws if gws is not None else [
                {"name": "atuin", "namespace": "sovereign-cloud", "ready": True, "paused": False, "status": {}},
                {"name": "iaas1", "namespace": "sovereign-cloud", "ready": True, "paused": True, "status": {}}],
            "nodes": nodes, "skipped": skipped}


def cond(body, t):
    return next((c for c in body["conditions"] if c["type"] == t), None)


def apply(status, body):
    """status after a partial write of body (status library, as cr_status computes it)."""
    obj = {"metadata": {"generation": 3, "resourceVersion": "1"}, "status": copy.deepcopy(status)}
    patch, _ = S.compute_status_patch(obj, copy.deepcopy(body), {"partial": True}, now=NOW)
    return S._merge_patch(obj["status"], patch["status"]) if patch else obj["status"]


def twice(nodes, **kw):
    """(the second check's answer, the first's): the same reads two checks running, statuses written in between"""
    r = req(nodes, **kw)
    first = J.judge(r)
    r2 = copy.deepcopy(r)
    r2["now"] = LATER
    r2["fabric"]["status"] = apply(r["fabric"]["status"], first["fabric"]["status"])
    by = {g["name"]: g for g in first["gateways"]}
    for g in r2["gateways"]:
        if g["name"] in by:
            g["status"] = apply(g.get("status") or {}, by[g["name"]]["status"])
    return J.judge(r2), first


class JudgeTest(unittest.TestCase):
    def test_healthy(self):
        a = J.judge(req([node("bgw-1"), node("bgw-2")]))
        f = a["fabric"]
        self.assertEqual(cond(f["status"], "NodesHealthy")["status"], "True")
        self.assertEqual(cond(f["status"], "Degraded")["status"], "False")
        self.assertEqual(cond(f["status"], "IPv6EgressUpstream")["status"], "True")
        self.assertFalse(f["wake"])
        self.assertEqual(f["events"], [])
        self.assertTrue(f["status"]["health"]["healthy"])
        self.assertEqual(f["status"]["realization"]["health"]["intervalSeconds"], 600)
        self.assertNotIn("errors", f["status"]["realization"]["egress"]["ipv6"]["nodes"]["bgw-1"])
        gw = {g["name"]: g for g in a["gateways"]}
        self.assertEqual(cond(gw["atuin"]["status"], "NodesHealthy")["status"], "True")
        self.assertFalse(a["wakeNetworks"])

    def test_paused_site_is_never_judged(self):
        gwread = inst("gateway", siteSessions={"established": 0, "known": 0},
                      start=dict(unit("failed"), unit="shc-gw@iaas1.service", lines=[]))
        n = node("bgw-1")
        n["read"]["instances"]["gw-iaas1"] = gwread
        a = J.judge(req([n]))
        g = {x["name"]: x for x in a["gateways"]}["iaas1"]
        c = cond(g["status"], "NodesHealthy")
        self.assertEqual((c["status"], c["reason"]), ("Unknown", "SitePaused"))
        self.assertEqual(cond(g["status"], "Degraded")["status"], "False")
        self.assertEqual((g["events"], g["wake"]), ([], False))
        # written once, not again while nothing changes
        st = apply({}, g["status"])
        a2 = J.judge(req([n], gws=[{"name": "iaas1", "namespace": "x", "ready": True, "paused": True, "status": st}],
                         now=LATER))
        self.assertFalse(a2["gateways"][0]["changed"])

    def test_boot_apply_failure_counts_at_once_and_backs_off(self):
        # a failed oneshot is a state, not a moment: StartFailed counts at its first sighting
        bad = inst(start=dict(unit("failed", result="exit-code"), unit="shc-fabric-apply@root.service",
                              lines=["transport srv6: net.vrf.strict_mode=1 refused"], incomplete=[]))
        f = J.judge(req([node("bgw-1"), node("bgw-2", root=bad)]))["fabric"]
        c = cond(f["status"], "NodesHealthy")
        self.assertEqual((c["status"], c["reason"]), ("False", "StartFailed"))
        self.assertIn("net.vrf.strict_mode=1 refused", c["message"])
        self.assertNotIn("transport srv6:", c["message"])
        self.assertEqual(cond(f["status"], "Degraded")["reason"], "StartFailed")
        self.assertTrue(f["wake"])
        self.assertEqual({e["reason"] for e in f["events"]}, {"NodeStartFailed", "HealthWake"})
        n2 = f["status"]["health"]["nodes"][1]
        self.assertEqual((n2["reason"], n2["findings"][0]["confirmed"]), ("StartFailed", True))
        st = apply({}, f["status"])
        # the woken run did not fix it: no second wake within the backoff, no second event
        f2 = J.judge(req([node("bgw-1"), node("bgw-2", root=bad)], fab_status=st, now=LATER))["fabric"]
        self.assertFalse(f2["wake"])
        self.assertEqual(f2["events"], [])
        st2 = apply(st, f2["status"])
        f3 = J.judge(req([node("bgw-1"), node("bgw-2", root=bad)], fab_status=st2, now=MUCH_LATER))["fabric"]
        self.assertTrue(f3["wake"])
        fnd = [x for x in f2["status"]["health"]["nodes"] if x["name"] == "bgw-2"][0]
        self.assertEqual((fnd["findings"][0]["since"], fnd["findings"][0]["checks"]), (NOW, 2))

    def test_gate_h9_a_reboot_whose_apply_fails(self):
        """As captured: the first check finds the node rebooting (unreachable), the next one the failed boot apply
        with the node's state unapplied (drift lines) and the gateways down with it."""
        prev = J.judge(req([node("bgw-1"), node("bgw-2", reachable=False)]))
        self.assertFalse(prev["fabric"]["status"]["health"]["healthy"])          # a first sighting is listed
        self.assertEqual(cond(prev["fabric"]["status"], "NodesHealthy")["status"], "True")
        fst = apply({}, prev["fabric"]["status"])
        gst = apply({}, {x["name"]: x for x in prev["gateways"]}["atuin"]["status"])
        bad = inst(start=dict(unit("failed", result="exit-code"), unit="shc-fabric-apply@root.service",
                              lines=["shc-fabric-apply: gate H9 injected failure"], incomplete=[]),
                   kernelDrift=["-lo 127.0.0.1/8 10.255.0.1/32", "-br-core 10.255.1.1/24"],
                   frr={"reloadDue": False, "recorded": True, "pending": ["router bgp 65011 vrf egress: x"],
                        "notTaken": []})
        gw = inst("gateway", kernelDrift=["-gw-atuin"])
        a = J.judge(req([node("bgw-1"), node("bgw-2", root=bad, gw=gw, booted=1_800_100_000)], fab_status=fst,
                        gws=[{"name": "atuin", "namespace": "sovereign-cloud", "ready": True, "paused": False,
                              "status": gst}], now=LATER))
        f = a["fabric"]
        c = cond(f["status"], "NodesHealthy")
        self.assertEqual((c["status"], c["reason"]), ("False", "StartFailed"))
        self.assertIn("gate H9 injected failure", c["message"])
        self.assertEqual(cond(f["status"], "Degraded")["reason"], "StartFailed")
        n2 = [x for x in f["status"]["health"]["nodes"] if x["name"] == "bgw-2"][0]
        self.assertEqual([x["reason"] for x in n2["findings"]], ["StartFailed"])   # not also drift
        self.assertTrue(f["wake"])
        self.assertIn("NodeStartFailed", [e["reason"] for e in f["events"]])
        g = a["gateways"][0]
        self.assertEqual(cond(g["status"], "NodesHealthy")["reason"], "StartFailed")
        self.assertEqual(cond(g["status"], "Degraded")["status"], "True")

    def test_drift_wakes_on_the_second_sighting_then_recovers(self):
        drift = inst(kernelDrift=["+lo 10.255.0.250/32"])
        f1 = J.judge(req([node("bgw-1", root=drift)]))["fabric"]
        self.assertFalse(f1["wake"])
        self.assertEqual(cond(f1["status"], "NodesHealthy")["reason"], "ConfigurationDrift")
        self.assertEqual(cond(f1["status"], "Degraded")["status"], "False")
        self.assertEqual([e["reason"] for e in f1["events"]], ["ConfigurationDrift"])
        st = apply({}, f1["status"])
        f2 = J.judge(req([node("bgw-1", root=drift)], fab_status=st, now=LATER))["fabric"]
        self.assertTrue(f2["wake"])
        st = apply(st, f2["status"])
        f3 = J.judge(req([node("bgw-1")], fab_status=st, now=MUCH_LATER))["fabric"]
        self.assertEqual(cond(f3["status"], "NodesHealthy")["status"], "True")
        self.assertEqual([e["reason"] for e in f3["events"]], ["NodeHealthRecovered"])

    def test_start_incomplete_until_an_apply_without_warnings(self):
        r = inst(start=dict(unit(), unit="shc-fabric-apply@root.service", lines=[],
                            incomplete=["warning: interface eno1 relies on router advertisements; IPv6 core not applied"]))
        f = twice([node("bgw-1", root=r)])[0]["fabric"]
        c = cond(f["status"], "NodesHealthy")
        self.assertEqual(c["reason"], "StartIncomplete")
        self.assertNotIn("warning:", c["message"])
        st = apply({}, f["status"])
        f2 = J.judge(req([node("bgw-1")], fab_status=st, now=LATER))["fabric"]
        self.assertEqual(cond(f2["status"], "NodesHealthy")["status"], "True")

    def test_a_check_without_reads_or_a_reason_is_refused(self):
        r = req([node("bgw-1")])
        r["expectedNodes"] = ["bgw-1", "bgw-2"]
        with self.assertRaises(ValueError):
            J.judge(r)
        r["nodes"] = []
        with self.assertRaises(ValueError):
            J.judge(r)
        r["skipped"] = ""            # what ansible-core 2.16 made of a None: refused, never "not skipped"
        with self.assertRaises(ValueError):
            J.judge(r)
        r["skipped"] = {"reason": "LeaseHeld"}
        self.assertNotIn("checkedAt", J.judge(r)["fabric"]["status"]["health"])

    def test_a_read_that_failed_is_not_unreachable(self):
        a = J.judge(req([node("bgw-1"), {"name": "bgw-2", "reachable": True, "error": "the read ran out of time"}]))
        c = cond(a["fabric"]["status"], "NodesHealthy")
        self.assertEqual((c["status"], c["reason"]), ("False", "ReadFailed"))
        self.assertEqual(cond(a["fabric"]["status"], "Degraded")["status"], "False")
        self.assertFalse(a["fabric"]["wake"])

    def test_a_transient_state_seen_once_counts_for_nothing(self):
        g = inst("gateway", start=dict(unit("activating", sub="auto-restart", restarts=1), unit="shc-gw@atuin.service",
                                       lines=[], incomplete=[]))
        a = J.judge(req([node("bgw-1", gw=g)]))
        atuin = {x["name"]: x for x in a["gateways"]}["atuin"]
        self.assertEqual(cond(atuin["status"], "NodesHealthy")["status"], "True")
        self.assertEqual((atuin["wake"], atuin["events"]), (False, []))
        self.assertEqual(atuin["status"]["health"]["nodes"][0]["findings"][0]["checks"], 1)
        # gone at the next check: never a condition, never an event
        st = apply({}, atuin["status"])
        b = J.judge(req([node("bgw-1")], gws=[{"name": "atuin", "namespace": "sovereign-cloud", "ready": True,
                                                "paused": False, "status": st}], now=LATER))
        self.assertEqual(b["gateways"][0]["events"], [])

    def test_gateways_not_realized_or_busy_are_not_checked(self):
        g = inst("gateway", start=dict(unit("inactive"), unit="shc-gw@new.service", lines=[], incomplete=[]))
        n = node("bgw-1")
        n["read"]["instances"]["gw-new"] = g
        gws = [{"name": "new", "namespace": "sovereign-cloud", "ready": False, "paused": False, "judged": False,
                "status": {}}]
        a = twice([n], gws=gws)[0]
        c = cond(a["gateways"][0]["status"], "NodesHealthy")
        self.assertEqual((c["status"], c["reason"]), ("Unknown", "NotChecked"))
        self.assertEqual(cond(a["gateways"][0]["status"], "Degraded")["status"], "False")
        self.assertFalse(a["gateways"][0]["changed"])

    def test_gateway_drift_with_more_checks_still_wakes(self):
        g = inst("gateway", frr={"reloadDue": True, "recorded": True, "pending": None, "notTaken": []})
        gws = [{"name": "atuin", "namespace": "sovereign-cloud", "ready": True, "paused": False, "status": {}}]
        st, woke = {}, []
        for i, now in enumerate(("2026-11-10T10:00:00Z", "2026-11-10T10:10:00Z", "2026-11-10T10:20:00Z",
                                 "2026-11-10T10:30:00Z")):
            a = J.judge(req([node("bgw-1", gw=g)], gws=[dict(gws[0], status=st)], now=now,
                            health={"wakeAfterChecks": 3}))
            gw = a["gateways"][0]
            if gw["changed"]:
                st = apply(st, gw["status"])
            woke.append(gw["wake"])
        self.assertEqual(woke, [False, False, True, False])

    def test_a_removed_node_leaves_the_egress_state(self):
        prev = {"realization": {"egress": {"ipv6": {"enabled": True, "degraded": True, "degradedNodes": ["bgw-3"],
                                                    "nodes": {"bgw-1": {"healthy": True}, "bgw-3": {"healthy": False}}}}}}
        a = J.judge(req([node("bgw-1")], fab_status=prev))
        e6 = a["fabric"]["status"]["realization"]["egress"]["ipv6"]
        self.assertEqual((e6["nodes"]["bgw-3"], e6["degradedNodes"], e6["degraded"]), (None, [], False))
        self.assertTrue(a["wakeNetworks"])

    def test_a_fabric_with_the_check_off(self):
        r = req([], gws=[{"name": "atuin", "namespace": "sovereign-cloud", "ready": True, "paused": False,
                          "status": {}}])
        r["enabled"] = False
        a = J.judge(r)
        f = a["fabric"]["status"]
        self.assertEqual(cond(f, "NodesHealthy")["reason"], "NotChecked")
        self.assertFalse(f["realization"]["health"]["enabled"])
        self.assertTrue(f["health"]["lastRunAt"])
        self.assertNotIn("lastRunAt", a["gateways"][0]["status"]["health"])
        self.assertEqual(cond(a["gateways"][0]["status"], "NodesHealthy")["reason"], "NotChecked")

    def test_a_paused_gateway_keeps_its_last_wake_and_is_not_rewritten(self):
        gws = [{"name": "iaas1", "namespace": "x", "ready": True, "paused": True,
                "status": {"health": {"lastWakeAt": NOW, "checkedAt": NOW, "nodes": []},
                           "conditions": [{"type": "NodesHealthy", "status": "Unknown", "reason": "SitePaused"},
                                          {"type": "Degraded", "status": "False", "reason": "AsExpected"}]}}]
        a = J.judge(req([node("bgw-1")], gws=gws, now=LATER))
        self.assertFalse(a["gateways"][0]["changed"])

    def test_lines_not_taken_are_reported_never_woken(self):
        g = inst("gateway", frr={"reloadDue": False, "recorded": True, "pending": [], "notTaken": ["router bgp 1: x"]})
        a = J.judge(req([node("bgw-1", gw=g)], health={"wakeAfterChecks": 1}))
        atuin = {x["name"]: x for x in a["gateways"]}["atuin"]
        self.assertEqual(cond(atuin["status"], "NodesHealthy")["reason"], "ConfigurationNotTaken")
        self.assertEqual(cond(atuin["status"], "Degraded")["status"], "False")
        self.assertFalse(atuin["wake"])

    def test_gateway_drift_wakes_the_gateway_not_the_fabric(self):
        g = inst("gateway", frr={"reloadDue": True, "recorded": True, "pending": None})
        a = J.judge(req([node("bgw-1", gw=g)], health={"wakeAfterChecks": 1}))
        self.assertFalse(a["fabric"]["wake"])
        atuin = {x["name"]: x for x in a["gateways"]}["atuin"]
        self.assertTrue(atuin["wake"])
        self.assertTrue(atuin["changed"])
        self.assertEqual(atuin["status"]["health"]["nodes"][0]["findings"][0]["checks"], 1)

    def test_crash_loop_daemon_down_restart_and_site_sessions(self):
        g = inst("gateway", start=dict(unit("activating", sub="auto-restart", restarts=7), unit="shc-gw@atuin.service",
                                       lines=[]), siteSessions={"established": 0, "known": 2})
        r = inst(routing={"unit": "frr.service", "daemons": {"zebra": "Down", "bgpd": "Up"},
                          "crashes": [{"daemon": "zebra", "at": 1_800_000_000, "pathspace": ""}]})
        a, _ = twice([node("bgw-1", root=r, gw=g)])
        f = a["fabric"]["status"]
        self.assertEqual(cond(f, "NodesHealthy")["reason"], "RoutingDaemonDown")
        reasons = [x["reason"] for x in f["health"]["nodes"][0]["findings"]]
        self.assertEqual(reasons, ["RoutingDaemonDown", "RoutingDaemonRestarted"])
        self.assertFalse(a["fabric"]["wake"])
        atuin = {x["name"]: x for x in a["gateways"]}["atuin"]
        self.assertEqual([x["reason"] for x in atuin["status"]["health"]["nodes"][0]["findings"]],
                         ["ServiceFailed", "SiteSessionsDown"])
        self.assertTrue(atuin["wake"])

    def test_an_automatic_restart_of_the_routing_service(self):
        # gate-s6 F12: systemd stopped and restarted FRR after a failed reload; no core dump, every session reset. The
        # root's restarts go to the fabric, a gateway unit's to that gateway; same reason as a crash, no wake
        r = inst(routing={"unit": "frr.service", "daemons": {"zebra": "Up", "bgpd": "Up"}, "crashes": [],
                          "restarts": [{"at": 1_800_000_000, "count": 17}, {"at": 1_800_000_300, "count": 18}]})
        g = inst("gateway", routing={"unit": "shc-gw@atuin.service", "daemons": {"zebra": "Up"}, "crashes": [],
                                     "restarts": [{"at": 1_800_000_100, "count": 1}]})
        a = J.judge(req([node("bgw-1", root=r, gw=g), node("bgw-2")]))
        f = a["fabric"]["status"]
        nodes = {n["name"]: n for n in f["health"]["nodes"]}
        self.assertEqual([x["reason"] for x in nodes["bgw-1"]["findings"]], ["RoutingDaemonRestarted"])
        self.assertEqual(nodes["bgw-1"]["findings"][0]["message"],
                         "the routing service restarted by itself 2 time(s) in the last 60 minutes, last at "
                         "2027-01-15T08:05:00Z")
        self.assertEqual(nodes["bgw-2"]["findings"], [])
        self.assertFalse(a["fabric"]["wake"])
        atuin = {x["name"]: x for x in a["gateways"]}["atuin"]
        msgs = [x["message"] for n in atuin["status"]["health"]["nodes"] for x in n["findings"]]
        self.assertEqual(msgs, ["the routing service restarted by itself 1 time(s) in the last 60 minutes, last at "
                                "2027-01-15T08:01:40Z"])
        # with a crash as well: one finding, both messages
        r["routing"]["crashes"] = [{"daemon": "bgpd", "at": 1_800_000_200, "pathspace": ""}]
        f = J.judge(req([node("bgw-1", root=r)]))["fabric"]["status"]
        found = f["health"]["nodes"][0]["findings"]
        self.assertEqual([x["reason"] for x in found], ["RoutingDaemonRestarted"])
        self.assertIn("1 routing daemon crash(es)", found[0]["message"])
        self.assertIn("restarted by itself 2 time(s)", found[0]["message"])

    def test_unit_not_enabled_and_guard_down(self):
        r = inst()
        r["units"]["frr.service"] = unit(enabled="disabled")
        r["units"]["shc-egress6-guard.timer"] = unit("inactive")
        f = J.judge(req([node("bgw-1", root=r)]))["fabric"]          # a disabled unit is a state: counts at once
        msg = cond(f["status"], "NodesHealthy")["message"]
        self.assertIn("not started at boot", msg)
        self.assertIn("a guard of the configuration is inactive", msg)
        self.assertTrue(f["wake"])

    def test_unreachable_node(self):
        a, first = twice([node("bgw-1"), node("bgw-2", reachable=False)])
        self.assertEqual(cond(first["fabric"]["status"], "NodesHealthy")["status"], "True")   # a reboot on purpose
        f = a["fabric"]
        self.assertEqual(cond(f["status"], "NodesHealthy")["reason"], "NodeUnreachable")
        self.assertFalse(f["wake"])
        n2 = [n for n in f["status"]["health"]["nodes"] if n["name"] == "bgw-2"][0]
        self.assertFalse(n2["reachable"])
        # the unreachable node keeps its last IPv6 egress entry, the reachable one is refreshed
        self.assertEqual(sorted(f["status"]["realization"]["egress"]["ipv6"]["nodes"]), ["bgw-1"])

    def test_lease_held_skips_and_keeps_findings(self):
        bad = inst(kernelDrift=["+x"])
        st = apply({}, J.judge(req([node("bgw-1", root=bad)]))["fabric"]["status"])
        a = J.judge(req([], fab_status=st, now=LATER, skipped={"reason": "LeaseHeld"}))
        body = a["fabric"]["status"]
        self.assertEqual(set(body), {"health", "realization"})
        # a skipped run reports (lastRunAt) without claiming a read (checkedAt)
        self.assertEqual(body["health"], {"skipped": {"reason": "LeaseHeld", "since": LATER}, "lastRunAt": LATER})
        self.assertEqual(a["gateways"], [])
        st2 = apply(st, body)
        self.assertEqual((st2["health"]["checkedAt"], st2["health"]["lastRunAt"]), (NOW, LATER))
        self.assertEqual(st2["health"]["nodes"], st["health"]["nodes"])
        self.assertFalse(a["fabric"]["wake"])

    def test_not_ready_is_never_woken(self):
        bad = inst(start=dict(unit("failed"), unit="shc-fabric-apply@root.service", lines=[]))
        r = req([node("bgw-1", root=bad)])
        r["fabric"]["ready"] = False
        self.assertFalse(J.judge(r)["fabric"]["wake"])

    def test_upstream_event_once_and_network_wakes_on_any_node_change(self):
        down = inst(egress6=dict(inst()["egress6"], healthy=False))
        a = J.judge(req([node("bgw-1"), node("bgw-2", root=down)]))
        self.assertTrue(a["wakeNetworks"])
        c = cond(a["fabric"]["status"], "IPv6EgressUpstream")
        self.assertEqual((c["status"], c["reason"]), ("False", "Degraded"))
        self.assertNotIn("IPv6EgressUpstreamDown", [e["reason"] for e in a["fabric"]["events"]])
        st = apply({}, a["fabric"]["status"])
        a = J.judge(req([node("bgw-1"), node("bgw-2", root=down)], fab_status=st, now=LATER))
        self.assertFalse(a["wakeNetworks"])
        self.assertEqual([e["reason"] for e in a["fabric"]["events"]], ["IPv6EgressUpstreamDown"])
        st = apply(st, a["fabric"]["status"])
        later2 = "2026-11-10T10:20:00Z"
        a = J.judge(req([node("bgw-1", root=down), node("bgw-2", root=down)], fab_status=st, now=later2))
        self.assertTrue(a["wakeNetworks"], "1 node down -> 2 nodes down must wake the networks")
        self.assertEqual(a["fabric"]["events"], [], "reported again only after eventRepeatMinutes")
        self.assertEqual(a["fabric"]["status"]["realization"]["egress"]["ipv6"]["degradedSince"], NOW)

    def test_reboot_event(self):
        st = apply({}, J.judge(req([node("bgw-1")]))["fabric"]["status"])
        a = J.judge(req([node("bgw-1", booted=1_800_100_000)], fab_status=st, now=LATER))
        self.assertEqual([e["reason"] for e in a["fabric"]["events"]], ["NodeRebooted"])

    def test_numbers_as_strings(self):
        bad = inst(kernelDrift=["+x"])
        st = apply({}, J.judge(req([node("bgw-1", root=bad)]))["fabric"]["status"])
        f = J.judge(req([node("bgw-1", root=bad)], fab_status=st, now=LATER,
                        health={"wakeAfterChecks": "2", "wakeBackoffMinutes": "0", "intervalSeconds": "600"}))["fabric"]
        self.assertTrue(f["wake"])
        self.assertEqual(J.settings({"wakeAfterChecks": 0})["wakeAfterChecks"], 0)

    def test_checks_switched_off(self):
        bad = inst(kernelDrift=["+x"])
        f = J.judge(req([node("bgw-1", root=bad)], health={"checks": {"drift": False}}))["fabric"]
        self.assertEqual(cond(f["status"], "NodesHealthy")["status"], "True")

    def test_cli(self):
        p = subprocess.run([sys.executable, PATH], input=json.dumps(req([node("bgw-1")])), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("fabric", json.loads(p.stdout))


class CoexistenceTest(unittest.TestCase):
    """The provisioning jobs' writes keep what the health job wrote, and the health write leaves the provisioning
    bookkeeping alone; both writers derive the phase the same way."""

    def health_status(self, bad=True):
        r = inst(start=dict(unit("failed"), unit="shc-fabric-apply@root.service", lines=[])) if bad else inst()
        return twice([node("bgw-1", root=r)])[0]["fabric"]["status"]

    def base(self):
        return {"metadata": {"generation": 4, "resourceVersion": "9"},
                "status": {"observedGeneration": 4, "ready": True, "status": "ready",
                           "lastHandledReconcileRequest": "2026-11-09T00:00:00Z",
                           "delivery": {"eventId": "e1", "completedEventId": "e1"},
                           "conditions": [{"type": "Ready", "status": "True", "reason": "Ready",
                                           "lastTransitionTime": NOW, "message": ""}],
                           "nodes": [{"name": "bgw-1", "ready": True}],
                           "realization": {"model": "linux-pe", "egress": {"ipv6": {"enabled": True, "nodes": {}}}}}}

    def test_health_write_leaves_the_bookkeeping(self):
        obj = self.base()
        patch, info = S.compute_status_patch(obj, copy.deepcopy(self.health_status()), {"partial": True}, now=NOW)
        for k in ("observedGeneration", "delivery", "lastHandledReconcileRequest", "edaJobs", "nodes", "ready",
                  "status"):
            self.assertNotIn(k, patch["status"], k)
        merged = S._merge_patch(obj["status"], patch["status"])
        self.assertEqual(merged["phase"], "Degraded")
        self.assertEqual(merged["realization"]["model"], "linux-pe")
        self.assertNotIn("Reconciling", [c["type"] for c in merged["conditions"]])

    def full_job_write(self, status, body, action="ready", event="e2"):
        obj = {"metadata": {"generation": 4, "resourceVersion": "10"}, "status": status}
        st = dict(status, delivery={"eventId": event})
        obj["status"] = st
        patch, info = S.compute_status_patch(obj, body, {"action": action, "event_id": event}, now=LATER)
        self.assertIsNotNone(patch)
        return S._merge_patch(st, patch["status"])

    def test_fabric_job_keeps_health(self):
        obj = self.base()
        st = S._merge_patch(obj["status"], S.compute_status_patch(obj, self.health_status(), {"partial": True},
                                                                   now=NOW)[0]["status"])
        # a ready fabric job's body (fabric_finish.yml: status, ready, observedGeneration, nodes, realization, its
        # own conditions)
        body = {"status": "ready", "ready": True, "observedGeneration": 4, "message": "ok",
                "nodes": [{"name": "bgw-1", "ready": True, "lastAppliedAt": LATER}],
                "realization": {"model": "linux-pe", "egress": {"enabled": True, "ipv6": {"enabled": True,
                                                                                        "degraded": False}},
                                "translation": None},
                "conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": "ok"},
                               {"type": "IPv6EgressUpstream", "status": "True", "reason": "Ready", "message": ""}]}
        after = self.full_job_write(st, body)
        self.assertEqual(after["health"], st["health"])
        types = {c["type"]: c for c in after["conditions"]}
        self.assertEqual(types["NodesHealthy"]["reason"], "StartFailed")
        self.assertEqual(types["Degraded"]["status"], "True")
        self.assertEqual(after["phase"], "Degraded")
        self.assertEqual(after["realization"]["health"], st["realization"]["health"])
        # the next health check (healthy) and the phase both writers derive
        st2 = S._merge_patch(after, S.compute_status_patch(
            {"metadata": {"generation": 4, "resourceVersion": "11"}, "status": after},
            self.health_status(bad=False), {"partial": True}, now=MUCH_LATER)[0]["status"])
        self.assertEqual(st2["phase"], "Ready")
        self.assertEqual(st2["observedGeneration"], 4)

    def test_failed_job_and_gateway_job_keep_health(self):
        obj = self.base()
        st = S._merge_patch(obj["status"], S.compute_status_patch(obj, self.health_status(), {"partial": True},
                                                                   now=NOW)[0]["status"])
        after = self.full_job_write(st, {"status": "failed", "ready": False, "message": "boom",
                                         "nodes": [{"name": "bgw-1"}]}, action="failed")
        self.assertEqual(after["health"], st["health"])
        self.assertIn("NodesHealthy", [c["type"] for c in after["conditions"]])
        self.assertEqual(after["phase"], "Failed")
        # a gateway job's ready write (cloudgateway_provision: nodes with sessions, realization, Ready)
        after = self.full_job_write(st, {"status": "ready", "ready": True, "observedGeneration": 4,
                                         "nodes": [{"name": "bgw-1", "siteSessions": 4, "coreSessions": 1}],
                                         "realization": {"vrfs": 2}, "message": "ok"})
        self.assertEqual(after["health"], st["health"])
        self.assertEqual(after["phase"], "Degraded")


class ServiceSessionsTest(unittest.TestCase):
    """ServiceSessionsDown: a node with platform circuits and none of their sessions established degrades the fabric
    once a second check confirms it; no wake; check key serviceSessions."""

    def root(self, established, known=2):
        return inst(serviceSessions={"vrf": "service", "established": established, "known": known,
                                     "peers": [{"address": "10.255.128.75", "state": "Active"}]})

    def test_down_confirmed_on_the_second_check(self):
        first = J.judge(req([node("bgw-1", root=self.root(0))]))["fabric"]
        f = first["status"]
        self.assertEqual(cond(f, "NodesHealthy")["status"], "True")
        self.assertEqual(f["health"]["nodes"][0]["findings"][0]["reason"], "ServiceSessionsDown")
        a, _ = twice([node("bgw-1", root=self.root(0))])
        f = a["fabric"]["status"]
        self.assertEqual(cond(f, "NodesHealthy")["reason"], "ServiceSessionsDown")
        self.assertEqual(cond(f, "Degraded")["reason"], "ServiceSessionsDown")
        self.assertIn("no platform circuit session is established in the service VRF service (10.255.128.75 Active)",
                      cond(f, "NodesHealthy")["message"])
        self.assertEqual(f["health"]["nodes"][0]["findings"][0]["check"], "ServiceSessions")
        self.assertFalse(a["fabric"]["wake"])

    def test_a_node_with_nothing_kept_is_named(self):
        # Review R4 V2-M1: no service on this node while the fabric keeps it elsewhere: its own finding says so
        r = inst(serviceSessions={"vrf": "", "established": 0, "known": 1, "peers": [], "links": [],
                                  "missing": "no service applied here to keep"})
        f = J.judge(req([node("bgw-2", root=r)]))["fabric"]["status"]
        fd = f["health"]["nodes"][0]["findings"][0]
        self.assertEqual(fd["reason"], "ServiceSessionsDown")
        self.assertIn("no service on this node: no service applied here to keep", fd["message"])

    def test_one_session_up_or_no_service(self):
        for r in (self.root(1), inst(serviceSessions=None), self.root(0, known=0)):
            f = J.judge(req([node("bgw-1", root=r)]))["fabric"]["status"]
            self.assertEqual(f["health"]["nodes"][0]["findings"], [])

    def test_check_off(self):
        a = J.judge(req([node("bgw-1", root=self.root(0))], health={"checks": {"serviceSessions": False}}))
        self.assertEqual(a["fabric"]["status"]["health"]["nodes"][0]["findings"], [])


class ServiceLinksTest(unittest.TestCase):
    """ServiceLinkDown (Review R3 N6): per node and network, the iBGP session between the service VRF and the network's
    VRF. A session down while the node's platform circuit is up is a finding (confirmed on a second check, degrading,
    no wake); every circuit session down is the circuit's finding alone (ServiceSessionsDown), not one per network."""

    def root(self, established=1, states=("Idle", "Established")):
        links = [{"network": "acme/n%d" % i, "vrf": "e%d" % (10 + i), "address": "192.0.2.%d" % (2 * i), "state": st}
                 for i, st in enumerate(states, 1)]
        return inst(serviceSessions={"vrf": "service", "established": established, "known": 1,
                                     "peers": [{"address": "198.51.100.1",
                                                "state": "Established" if established else "Active"}],
                                     "links": links})

    def test_a_network_session_down_while_the_circuit_is_up(self):
        first = J.judge(req([node("bgw-1", root=self.root())]))["fabric"]
        f = first["status"]
        self.assertEqual(cond(f, "NodesHealthy")["status"], "True")
        self.assertEqual([x["reason"] for x in f["health"]["nodes"][0]["findings"]], ["ServiceLinkDown"])
        a, _ = twice([node("bgw-1", root=self.root())])
        f = a["fabric"]["status"]
        self.assertEqual(cond(f, "NodesHealthy")["reason"], "ServiceLinkDown")
        self.assertEqual(cond(f, "Degraded")["reason"], "ServiceLinkDown")
        msg = cond(f, "NodesHealthy")["message"]
        self.assertIn("acme/n1", msg)
        self.assertIn("192.0.2.2 Idle", msg)
        self.assertNotIn("acme/n2", msg)
        finding = f["health"]["nodes"][0]["findings"][0]
        self.assertEqual(finding["check"], "ServiceSessions")
        self.assertEqual(f["health"]["nodes"][0]["reason"], "ServiceLinkDown")
        self.assertFalse(a["fabric"]["wake"])
        self.assertIn("ServiceLinkDown", [e["reason"] for e in a["fabric"]["events"]])

    def test_every_network_down_with_the_circuit_is_the_circuits_finding(self):
        a, _ = twice([node("bgw-1", root=self.root(established=0, states=("Idle", "Idle")))])
        f = a["fabric"]["status"]
        self.assertEqual([x["reason"] for x in f["health"]["nodes"][0]["findings"]], ["ServiceSessionsDown"])

    def test_all_up_no_links_or_check_off(self):
        for r in (self.root(states=("Established", "Established")), self.root(states=())):
            f = J.judge(req([node("bgw-1", root=r)]))["fabric"]["status"]
            self.assertEqual(f["health"]["nodes"][0]["findings"], [])
        a = J.judge(req([node("bgw-1", root=self.root())], health={"checks": {"serviceSessions": False}}))
        self.assertEqual(a["fabric"]["status"]["health"]["nodes"][0]["findings"], [])

    def test_the_reason_is_registered_and_ordered(self):
        self.assertIn("ServiceLinkDown", J.ORDER)
        self.assertLess(J.ORDER.index("ServiceSessionsDown"), J.ORDER.index("ServiceLinkDown"))
        self.assertNotIn("ServiceLinkDown", J.WAKE_NOW | J.WAKE_LATER)


if __name__ == "__main__":
    unittest.main()

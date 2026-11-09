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
    d = {"start": dict(unit(), unit=su, lines=[]), "units": {su: unit(), ru: unit()},
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

    def test_boot_apply_failure_wakes_at_once_and_backs_off(self):
        bad = inst(start=dict(unit("failed", result="exit-code"), unit="shc-fabric-apply@root.service",
                              lines=["transport srv6: net.vrf.strict_mode=1 refused"]))
        a = J.judge(req([node("bgw-1"), node("bgw-2", root=bad)]))
        f = a["fabric"]
        c = cond(f["status"], "NodesHealthy")
        self.assertEqual((c["status"], c["reason"]), ("False", "StartFailed"))
        self.assertIn("net.vrf.strict_mode=1 refused", c["message"])
        self.assertNotIn("transport srv6:", c["message"])
        self.assertEqual(cond(f["status"], "Degraded")["reason"], "StartFailed")
        self.assertTrue(f["wake"])
        self.assertEqual({e["reason"] for e in f["events"]}, {"NodeStartFailed", "HealthWake"})
        st = apply({}, f["status"])
        # the woken run did not fix it: no second wake within the backoff, no second event
        f2 = J.judge(req([node("bgw-1"), node("bgw-2", root=bad)], fab_status=st, now=LATER))["fabric"]
        self.assertFalse(f2["wake"])
        self.assertEqual(f2["events"], [])
        st2 = apply(st, f2["status"])
        f3 = J.judge(req([node("bgw-1"), node("bgw-2", root=bad)], fab_status=st2, now=MUCH_LATER))["fabric"]
        self.assertTrue(f3["wake"])
        # since is kept, the count goes up
        fnd = [x for x in J.judge(req([node("bgw-1"), node("bgw-2", root=bad)], fab_status=st2,
                                      now=LATER))["fabric"]["status"]["health"]["nodes"] if x["name"] == "bgw-2"][0]
        self.assertEqual(fnd["findings"][0]["since"], NOW)
        self.assertEqual(fnd["findings"][0]["checks"], 3)

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
        a = J.judge(req([node("bgw-1", root=r, gw=g)]))
        f = a["fabric"]["status"]
        self.assertEqual(cond(f, "NodesHealthy")["reason"], "RoutingDaemonDown")
        reasons = [x["reason"] for x in f["health"]["nodes"][0]["findings"]]
        self.assertEqual(reasons, ["RoutingDaemonDown", "RoutingDaemonRestarted"])
        self.assertFalse(a["fabric"]["wake"])
        atuin = {x["name"]: x for x in a["gateways"]}["atuin"]
        self.assertEqual([x["reason"] for x in atuin["status"]["health"]["nodes"][0]["findings"]],
                         ["ServiceFailed", "SiteSessionsDown"])
        self.assertTrue(atuin["wake"])

    def test_unit_not_enabled_and_guard_down(self):
        r = inst()
        r["units"]["frr.service"] = unit(enabled="disabled")
        r["units"]["shc-egress6-guard.timer"] = unit("inactive")
        f = J.judge(req([node("bgw-1", root=r)]))["fabric"]
        msg = cond(f["status"], "NodesHealthy")["message"]
        self.assertIn("not started at boot", msg)
        self.assertIn("a guard of the configuration is inactive", msg)
        self.assertTrue(f["wake"])

    def test_unreachable_node(self):
        a = J.judge(req([node("bgw-1"), node("bgw-2", reachable=False)]))
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
        self.assertEqual(body["health"], {"skipped": {"reason": "LeaseHeld", "since": LATER}})
        self.assertEqual(a["gateways"], [])
        st2 = apply(st, body)
        self.assertEqual(st2["health"]["checkedAt"], NOW)
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
        return J.judge(req([node("bgw-1", root=r)]))["fabric"]["status"]

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


if __name__ == "__main__":
    unittest.main()

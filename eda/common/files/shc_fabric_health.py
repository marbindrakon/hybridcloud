#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Judge of the fabric health job (eda/rulebooks/roles/fabric_health): turns the node reads of one fabric
(files/shc_node_health.py on each border gateway node) and the objects' current status into the partial status
writes, the events and the wakes. Pure: reads a JSON request on stdin, prints a JSON answer; no I/O otherwise.

Request:
  now           RFC 3339 UTC
  health        the realization's health block (intervalSeconds, wakeAfterChecks, wakeBackoffMinutes,
                daemonRestartWindowMinutes, eventRepeatMinutes, checks)
  egress6       {enabled, sessionDownAlertMinutes} (the realization's egress.ipv6, enabled from the fabric's status)
  fabric        {name, namespace, ready, status}
  gateways      [{name, namespace, ready, paused, deleting, status}]
  nodes         [{name, reachable, error?, read?}] (read: the node reader's output)
  skipped       null or {reason: LeaseHeld|JobInFlight, holder?} (the node reads were skipped or discarded)
  expectedNodes the fabric's node names: without a skip reason every one of them must be in nodes (else an error:
                nothing is written for a check that did not read the nodes)
Answer:
  fabric        {status, changed, events, wake}
  gateways      [{name, namespace, status, changed, events, wake}]
  wakeNetworks  the networks with IPv6 egress must be woken (the IPv6 egress state of a node changed)
The status bodies are partial writes: health, conditions, and on the fabric realization.health and
realization.egress.ipv6. They never carry observedGeneration, delivery or lastHandledReconcileRequest.
"""
import datetime
import json
import sys

# reasons of the NodesHealthy condition, worst first
ORDER = ("NodeUnreachable", "ReadFailed", "StartFailed", "ServiceFailed", "RoutingDaemonDown", "StartIncomplete", "SiteSessionsDown",
         "ServiceSessionsDown", "ServiceLinkDown", "ConfigurationDrift", "ConfigurationNotTaken", "RoutingDaemonRestarted")
# the object works with reduced health (Degraded=True, phase Degraded)
DEGRADING = {"NodeUnreachable", "StartFailed", "ServiceFailed", "RoutingDaemonDown", "StartIncomplete",
             "SiteSessionsDown", "ServiceSessionsDown", "ServiceLinkDown"}
# a full run of the object's job repairs these: at once, or after the drift was seen wakeAfterChecks times
WAKE_NOW = {"StartFailed", "StartIncomplete", "ServiceFailed"}
WAKE_LATER = {"ConfigurationDrift"}
# reasons that count (conditions, events, wakes) only once two consecutive checks found them: a single read can land
# on a unit restarting by itself, a daemon in watchfrr's own restart, a node rebooting on purpose
CONFIRM = DEGRADING | WAKE_NOW
CHECK_OF = {"NodeUnreachable": "Reachability", "ReadFailed": "Reachability", "StartFailed": "Start", "StartIncomplete": "Start",
            "ServiceFailed": "Service", "RoutingDaemonDown": "RoutingDaemon", "RoutingDaemonRestarted": "RoutingDaemon",
            "ConfigurationDrift": "Configuration", "ConfigurationNotTaken": "Configuration",
            "SiteSessionsDown": "SiteSessions", "ServiceSessionsDown": "ServiceSessions",
            "ServiceLinkDown": "ServiceSessions"}
EVENT_OF = {"NodeUnreachable": "NodeUnreachable", "ReadFailed": "NodeReadFailed", "StartFailed": "NodeStartFailed", "StartIncomplete": "NodeStartFailed",
            "ServiceFailed": "NodeServiceFailed", "RoutingDaemonDown": "RoutingDaemonDown",
            "RoutingDaemonRestarted": "RoutingDaemonRestarted", "ConfigurationDrift": "ConfigurationDrift",
            "ConfigurationNotTaken": "ConfigurationNotTaken",
            "SiteSessionsDown": "SiteSessionsDown", "ServiceSessionsDown": "ServiceSessionsDown",
            "ServiceLinkDown": "ServiceLinkDown"}
DEFAULTS = {"intervalSeconds": 600, "wakeAfterChecks": 2, "wakeBackoffMinutes": 60, "daemonRestartWindowMinutes": 60,
            "eventRepeatMinutes": 60, "checks": {}}
CHECKS = ("units", "routingDaemons", "drift", "upstream", "siteSessions", "serviceSessions", "pools")
FMT = "%Y-%m-%dT%H:%M:%SZ"
TOOL_PREFIX = ("shc-fabric-apply: ", "transport srv6: ", "warning: ")


def ts(text):
    try:
        return datetime.datetime.strptime(str(text)[:19] + "Z", FMT).replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime(FMT)


def num(v, default):
    """A realization number (operator and runner facts may be strings); 0 is a value, not 'unset'."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def settings(h):
    h = dict(DEFAULTS, **{k: v for k, v in (h or {}).items() if v is not None})
    out = {k: num(h.get(k), DEFAULTS[k]) for k in DEFAULTS if k != "checks"}
    checks = {c: True for c in CHECKS}
    for k, v in (h.get("checks") or {}).items():
        if k in checks:
            checks[k] = v if isinstance(v, bool) else str(v).lower() in ("true", "yes", "1")
    out["checks"] = checks
    return out


def clean(lines):
    out = []
    for l in lines or []:
        for p in TOOL_PREFIX:
            if l.startswith(p):
                l = l[len(p):]
        out.append(l)
    return out


def instance_findings(inst, kind, cfg):
    """[(reason, message)] for one instance read on one node."""
    if inst is None:
        return []
    chk = cfg["checks"]
    found = []
    start = inst.get("start") or {}
    lines = clean(start.get("lines"))
    why = (": " + "; ".join(lines)) if lines else ""
    if chk["units"]:
        if start.get("active") != "active":
            if kind == "gateway" and (start.get("sub") == "auto-restart" or (start.get("restarts") or 0) > 0) and not lines:
                found.append(("ServiceFailed", "the instance keeps restarting (%s, %d restarts)"
                              % (start.get("sub") or start.get("active"), start.get("restarts") or 0)))
            else:
                # a failed or never started oneshot is a state, not a moment: it counts at once (unlike a unit
                # caught activating)
                found.append(("StartFailed", "the configuration is not applied on the node (%s%s)%s"
                              % (start.get("active"), ", " + start["result"] if start.get("result") not in (None, "", "success") else "",
                                 why), start.get("active") in ("failed", "inactive")))
        elif start.get("incomplete"):
            # the last successful apply's own record (a later repair by a job clears it)
            found.append(("StartIncomplete", "the configuration was applied in part: "
                          + "; ".join(clean(start["incomplete"])), True))
        routing = (inst.get("routing") or {}).get("unit")
        units = inst.get("units") or {}
        for name, u in sorted(units.items()):
            if name == start.get("unit"):
                pass
            elif u.get("active") != "active":
                what = "the routing service" if name == routing else "a guard of the configuration"
                found.append(("ServiceFailed", "%s is %s" % (what, u.get("active")), u.get("active") == "failed"))
            if u.get("enabled") not in ("enabled", "enabled-runtime", "static", "indirect", "generated", "alias"):
                found.append(("ServiceFailed", "a service of the configuration is not started at boot (%s)"
                              % (u.get("enabled") or "unknown"), True))
    if chk["routingDaemons"]:
        r = inst.get("routing") or {}
        down = sorted(d for d, s in (r.get("daemons") or {}).items() if str(s).lower() != "up")
        if down:
            found.append(("RoutingDaemonDown", "routing daemons not running: " + ", ".join(down)))
        crashes = r.get("crashes") or []
        if crashes:
            last = max(c.get("at") or 0 for c in crashes)
            found.append(("RoutingDaemonRestarted", "%d routing daemon crash(es) in the last %d minutes (%s), last at %s"
                          % (len(crashes), cfg["daemonRestartWindowMinutes"],
                             ", ".join(sorted({c.get("daemon", "?") for c in crashes})), iso(last) if last else "?")))
        # systemd restarted the routing service by itself (a failed reload of the stock unit, watchfrr ending): every
        # session of the instance reset, and no core dump tells it
        restarts = r.get("restarts") or []
        if restarts:
            last = max(x.get("at") or 0 for x in restarts)
            found.append(("RoutingDaemonRestarted", "the routing service restarted by itself %d time(s) in the last %d "
                          "minutes, last at %s" % (len(restarts), cfg["daemonRestartWindowMinutes"],
                                                   iso(last) if last else "?")))
    if chk["drift"]:
        kd = inst.get("kernelDrift")
        if kd:
            found.append(("ConfigurationDrift", "the node's network state differs from the last applied configuration: "
                          + "; ".join(kd[:5])))
        frr = inst.get("frr") or {}
        if frr.get("reloadDue"):
            found.append(("ConfigurationDrift", "the routing configuration on the node is not the one loaded"))
        if frr.get("notTaken"):
            # FRR refused these after two loads; no run reloads for them (reported, never woken for)
            found.append(("ConfigurationNotTaken", "the routing configuration has lines the node's FRR does not take: "
                          + "; ".join(frr["notTaken"][:5])))
        if frr.get("pending"):
            found.append(("ConfigurationDrift", "the running routing configuration differs from the desired one: "
                          + "; ".join(frr["pending"][:5])))
    if chk["siteSessions"] and kind == "gateway":
        s = inst.get("siteSessions")
        if s is not None and (s.get("established") or 0) == 0:
            found.append(("SiteSessionsDown", "no session with the site is established"))
    if chk["serviceSessions"] and kind == "root":
        # the service VRF's hand-off (a node with platform circuits): no session up means the hub reaches no cluster
        # through this node (a fabric finding: the networks' conditions are not changed; no wake, a run repairs no
        # session)
        s = inst.get("serviceSessions")
        if s is not None and s.get("missing"):
            found.append(("ServiceSessionsDown", "no service on this node: %s" % s["missing"]))
        elif s is not None and (s.get("known") or 0) > 0 and (s.get("established") or 0) == 0:
            down = ", ".join("%s %s" % (p.get("address"), p.get("state")) for p in s.get("peers") or [])
            found.append(("ServiceSessionsDown", "no platform circuit session is established in the service VRF %s%s"
                          % (s.get("vrf") or "", (" (" + down + ")") if down else "")))
        elif s is not None and (s.get("established") or 0) > 0:
            # each network's service link (iBGP between the service VRF and the network's VRF): one down while the
            # node's platform circuit is up leaves that network's VRF on the node without the hub's ranges, and the
            # node stops announcing the network's /32 cluster addresses to the hub (the other nodes still carry both). With every circuit session down the circuit's finding says it all. No wake: a
            # run repairs only a missing link or neighbour, which the drift check reports (and wakes for) already
            for l in s.get("links") or []:
                if l.get("state") != "Established":
                    found.append(("ServiceLinkDown", "the service link of %s has no session (%s %s): its VRF %s on this "
                                  "node does not learn the hub's ranges and the node does not announce the network's "
                                  "cluster addresses to the hub (a cluster's subnet excepted); the other nodes carry "
                                  "both directions"
                                  % (l.get("network") or "?", l.get("address") or "?", l.get("state") or "Missing",
                                     l.get("vrf") or "?")))
    return found


def merge_findings(found, prev_findings, now, cfg):
    """One finding per reason (messages joined), with since and checks carried from the previous check."""
    by, stable = {}, set()
    for item in found:
        reason, msg = item[0], item[1]
        by.setdefault(reason, []).append(msg)
        if len(item) > 2 and item[2]:
            stable.add(reason)
    prev = {f.get("reason"): f for f in prev_findings or []}
    out = []
    for reason in ORDER:
        if reason not in by:
            continue
        p = prev.get(reason)
        checks = (num(p.get("checks"), 1) + 1) if p else 1
        out.append({"check": CHECK_OF[reason], "reason": reason, "message": "; ".join(dict.fromkeys(by[reason]))[:1000],
                    "since": p.get("since") if p and p.get("since") else now, "checks": checks,
                    # counts in the conditions, events and wakes: a state seen once, or a moment seen twice
                    "confirmed": reason not in CONFIRM or reason in stable or checks >= 2
                                 or bool(p and p.get("confirmed"))})
    return out


def effective(f):
    """A finding that counts: a lasting state at once, a moment once a second check found it too."""
    if "confirmed" in f:
        return bool(f["confirmed"])
    return f.get("reason") not in CONFIRM or num(f.get("checks"), 1) >= 2


def counted(n):
    return [f for f in n.get("findings") or [] if effective(f)]


def worst(nodes):
    reasons = [f["reason"] for n in nodes for f in counted(n)]
    for r in ORDER:
        if r in reasons:
            return r
    return None


def conditions(nodes, scope):
    w = worst(nodes)
    if w is None:
        return [{"type": "NodesHealthy", "status": "True", "reason": "AsExpected",
                 "message": "every node is healthy"},
                {"type": "Degraded", "status": "False", "reason": "AsExpected", "message": ""}]
    msgs = ["%s: %s" % (n["name"], f["message"]) for n in nodes for f in counted(n) if f["reason"] == w]
    degrading = sorted({f["reason"] for n in nodes for f in counted(n)} & DEGRADING,
                       key=ORDER.index)
    conds = [{"type": "NodesHealthy", "status": "False", "reason": w, "message": "; ".join(msgs)[:1000]}]
    if degrading:
        d = degrading[0]
        conds.append({"type": "Degraded", "status": "True", "reason": d,
                      "message": "; ".join("%s: %s" % (n["name"], f["message"]) for n in nodes
                                           for f in counted(n) if f["reason"] == d)[:1000]})
    else:
        conds.append({"type": "Degraded", "status": "False", "reason": "AsExpected", "message": ""})
    return conds


def prev_health(status):
    return (status or {}).get("health") or {}


def node_entries(nodes_in, part, kind, prev, now, cfg, gateway=None):
    """status.health.nodes of one object: the instance `part` on every node."""
    pnodes = {n.get("name"): n for n in prev.get("nodes") or []}
    out, events = [], []
    for n in nodes_in:
        p = pnodes.get(n["name"]) or {}
        if not n.get("reachable"):
            found = [("NodeUnreachable", n.get("error") or "the node did not answer")]
            booted = p.get("bootedAt")
        elif n.get("read") is None:
            # the node answered but its read failed or ran out of time: nothing is known about it this time
            found = [("ReadFailed", n.get("error") or "the node's read failed")]
            booted = p.get("bootedAt")
        else:
            read = n.get("read") or {}
            found = instance_findings((read.get("instances") or {}).get(part), kind, cfg)
            # an instance whose configuration did not start is not applied at all: its differences from the last
            # applied state are that, not drift. A gateway on a node whose root did not start cannot run either
            root_start = ((read.get("instances") or {}).get("root") or {}).get("start") or {}
            root_down = cfg["checks"]["units"] and bool(root_start) and root_start.get("active") in ("failed", "inactive")
            if kind == "gateway" and root_down:
                found.append(("StartFailed", "the node's own fabric configuration did not start", True))
            if any(f[0] == "StartFailed" for f in found):
                found = [f for f in found if f[0] != "ConfigurationDrift"]
            # a failed unit of ours that is no instance's own (e.g. a guard's run) belongs to the root, or to the
            # gateway it names
            for u in read.get("failedUnits") or []:
                mine = (gateway and ("gw@" + gateway + "." in u or "gw-" + gateway + "." in u))
                theirs = "gw@" in u or "@gw-" in u
                if (kind == "gateway" and mine) or (kind == "root" and not theirs):
                    known = set(((read.get("instances") or {}).get(part) or {}).get("units") or {})
                    if u not in known:
                        found.append(("ServiceFailed", "a service of the configuration failed"))
            booted = iso(read["bootedAt"]) if read.get("bootedAt") else p.get("bootedAt")
            pb = ts(p.get("bootedAt"))
            nb = ts(booted)
            if pb and nb and abs((nb - pb).total_seconds()) > 120:
                events.append({"type": "Normal", "reason": "NodeRebooted",
                               "message": "%s booted at %s (before: %s)" % (n["name"], booted, p.get("bootedAt"))})
        entry = {"name": n["name"], "reachable": bool(n.get("reachable")),
                 "findings": merge_findings(found, p.get("findings"), now, cfg)}
        # the node's worst counted finding, for views and alerts that show one line per node (null: none)
        top = next((f for r in ORDER for f in entry["findings"] if f["reason"] == r and effective(f)), None)
        entry["reason"] = top["reason"] if top else None
        entry["message"] = top["message"][:300] if top else None
        if booted:
            entry["bootedAt"] = booted
        out.append(entry)
        old = {f.get("reason") for f in p.get("findings") or [] if effective(f)}
        for f in entry["findings"]:
            if effective(f) and f["reason"] not in old:
                events.append({"type": "Warning", "reason": EVENT_OF[f["reason"]],
                               "message": "%s: %s" % (n["name"], f["message"])[:1000]})
    if not any(counted(n) for n in out) and any(counted(n) for n in pnodes.values()):
        events.append({"type": "Normal", "reason": "NodeHealthRecovered", "message": "every node is healthy again"})
    return out, events


def wake(nodes, prev, ready, now_dt, cfg):
    if not ready:
        return False
    last = ts(prev.get("lastWakeAt"))
    if last and (now_dt - last).total_seconds() < cfg["wakeBackoffMinutes"] * 60:
        return False
    for n in nodes:
        for f in n.get("findings") or []:
            if (f["reason"] in WAKE_NOW and effective(f)) or (
                    f["reason"] in WAKE_LATER and f["checks"] >= max(1, cfg["wakeAfterChecks"])):
                return True
    return False


def egress6_status(nodes_in, prev, e6, now, now_dt):
    """status.realization.egress.ipv6 from the nodes' reads (the fabric job's shape), the event, whether the
    networks must be woken. A node that was not read keeps its last entry."""
    prev = prev or {}
    if not e6.get("enabled"):
        return None, None, False
    # only the fabric's nodes: one removed from the fabric is dropped (null in the merge patch)
    names = [n["name"] for n in nodes_in]
    nodes = {k: v for k, v in (prev.get("nodes") or {}).items() if k in names and v}
    read_any = False
    for n in nodes_in:
        r = (((n.get("read") or {}).get("instances") or {}).get("root") or {}).get("egress6") if n.get("reachable") else None
        if r is not None:
            read_any = True
            nodes[n["name"]] = {k: v for k, v in r.items() if k != "errors"}
    if not read_any:
        return None, None, False
    down = sorted(k for k, v in nodes.items() if not v.get("healthy"))
    gone = {k: None for k in (prev.get("nodes") or {}) if k not in names}
    if down:
        since = (prev.get("degradedSince") or now) if prev.get("degraded") else now
    else:
        since = ""
    st = {"enabled": True, "nodes": dict(nodes, **gone), "degraded": bool(down), "degradedNodes": down,
          "degradedSince": since}
    event = None
    if down and since:
        minutes = num(e6.get("sessionDownAlertMinutes"), 5)
        repeat = num(e6.get("eventRepeatMinutes"), 60)
        if (now_dt - ts(since)).total_seconds() >= minutes * 60:
            last = ts(prev.get("reportedAt"))
            if last is None or (now_dt - last).total_seconds() >= repeat * 60:
                event = {"type": "Warning", "reason": "IPv6EgressUpstreamDown",
                         "message": "IPv6 internet egress: %s without an upstream session or the default route since %s; "
                                    "the networks' IPv6 internet traffic takes the other node(s) meanwhile"
                                    % (", ".join(down), since)}
                st["reportedAt"] = now
            else:
                st["reportedAt"] = prev.get("reportedAt")
        else:
            st["reportedAt"] = prev.get("reportedAt") or None
    else:
        st["reportedAt"] = None
    flip = sorted(prev.get("degradedNodes") or []) != down or bool(prev.get("degraded")) != bool(down)
    return st, event, flip


def egress6_condition(st):
    if st is None:
        return []
    if st["degraded"]:
        return [{"type": "IPv6EgressUpstream", "status": "False", "reason": "Degraded",
                 "message": "no upstream session or default route on %s since %s"
                            % (", ".join(st["degradedNodes"]), st["degradedSince"])}]
    return [{"type": "IPv6EgressUpstream", "status": "True", "reason": "Ready",
             "message": "every node has an upstream session and the default route"}]


def comparable(body, cap=2):
    """What decides whether a gateway's write is needed (the check time alone never is)."""
    # (a null in the merge patch removes the key: absent and null are the same here)
    h = {k: v for k, v in json.loads(json.dumps(body.get("health") or {})).items()
         if v is not None and k not in ("checkedAt", "lastRunAt")}
    # the count matters until it reached what any rule waits for (two sightings, wakeAfterChecks): written until then
    for n in h.get("nodes") or []:
        for f in n.get("findings") or []:
            f["checks"] = min(num(f.get("checks"), 1), cap)
    return json.dumps({"health": h, "conditions": [{k: c.get(k) for k in ("type", "status", "reason")}
                                                   for c in body.get("conditions") or []]}, sort_keys=True)


def current_comparable(status, cap=2):
    st = status or {}
    types = ("NodesHealthy", "Degraded")
    conds = [c for c in st.get("conditions") or [] if c.get("type") in types]
    conds.sort(key=lambda c: types.index(c["type"]))
    return comparable({"health": st.get("health") or {}, "conditions": conds}, cap)


def judge(req):
    now = req.get("now") or datetime.datetime.now(datetime.timezone.utc).strftime(FMT)
    now_dt = ts(now)
    cfg = settings(req.get("health"))
    fab = req.get("fabric") or {}
    fstatus = fab.get("status") or {}
    fprev = prev_health(fstatus)
    nodes_in = req.get("nodes") or []
    skipped = req.get("skipped")
    real = {"health": {"intervalSeconds": cfg["intervalSeconds"], "checks": cfg["checks"]}}
    answer = {"fabric": {"events": [], "wake": False, "changed": True}, "gateways": [], "wakeNetworks": False}

    cap = max(2, cfg["wakeAfterChecks"])
    real["health"]["enabled"] = bool(req.get("enabled", True))
    if not req.get("enabled", True):
        # the fabric's check is off (realization fabrics.<name>.health.enabled false, e.g. nodes that are off on
        # purpose): nothing is read or judged; the fabric and its gateways read Unknown/NotChecked
        off = {"health": {"checkedAt": None, "healthy": None, "nodes": [], "skipped": None},
               "conditions": [{"type": "NodesHealthy", "status": "Unknown", "reason": "NotChecked",
                               "message": "the health check is off for this fabric"},
                              {"type": "Degraded", "status": "False", "reason": "AsExpected", "message": ""}]}
        answer["fabric"]["status"] = dict(off, realization=real, health=dict(off["health"], lastRunAt=now))
        for g in req.get("gateways") or []:
            if not g.get("deleting"):
                answer["gateways"].append({"name": g["name"], "namespace": g.get("namespace"), "status": off,
                                           "changed": comparable(off, cap) != current_comparable(g.get("status"), cap),
                                           "events": [], "wake": False})
        return answer

    if skipped is not None and not isinstance(skipped, dict):
        raise ValueError("skipped must be null or {reason}, not %r" % (skipped,))
    expected = req.get("expectedNodes")
    if not skipped and expected is not None and sorted(n.get("name") for n in nodes_in) != sorted(expected):
        # a check is recorded only for nodes that were read: neither read nor a reason to skip is this job's error
        raise ValueError("the nodes were not read (%s of %s) and there is no reason to skip them"
                         % (sorted(n.get("name") for n in nodes_in), sorted(expected)))

    if skipped:
        health = {"skipped": {"reason": skipped.get("reason") or "LeaseHeld",
                              "since": (fprev.get("skipped") or {}).get("since") or now},
                  # every run reports, also one that left the nodes alone
                  "lastRunAt": now}
        answer["fabric"]["status"] = {"health": health, "realization": real}
        return answer

    fnodes, fevents = node_entries(nodes_in, "root", "root", fprev, now, cfg)
    fwake = wake(fnodes, fprev, bool(fab.get("ready")), now_dt, cfg)
    # healthy: the check found nothing at all (a finding still waiting for its second sighting is something)
    fhealth = {"checkedAt": now, "lastRunAt": now, "healthy": not any(n["findings"] for n in fnodes), "nodes": fnodes, "skipped": None,
               "lastWakeAt": now if fwake else fprev.get("lastWakeAt")}
    fconds = conditions(fnodes, "fabric")
    e6prev = ((fstatus.get("realization") or {}).get("egress") or {}).get("ipv6") or {}
    e6 = dict(req.get("egress6") or {})
    e6.setdefault("enabled", bool(e6prev.get("enabled")))
    e6.setdefault("eventRepeatMinutes", cfg["eventRepeatMinutes"])
    e6st, e6event, flip = (egress6_status(nodes_in, e6prev, e6, now, now_dt) if cfg["checks"]["upstream"]
                           else (None, None, False))
    if e6st is not None:
        real["egress"] = {"ipv6": e6st}
        fconds += egress6_condition(e6st)
        if e6event:
            fevents.append(e6event)
    if fwake:
        fevents.append({"type": "Normal", "reason": "HealthWake",
                        "message": "a full run is requested to repair: " + ", ".join(
                            sorted({f["reason"] for n in fnodes for f in counted(n)}))})
    answer["fabric"] = {"status": {"health": fhealth, "conditions": fconds, "realization": real},
                        "changed": True, "events": fevents, "wake": fwake}
    answer["wakeNetworks"] = bool(e6st is not None and flip)

    for g in req.get("gateways") or []:
        gstatus = g.get("status") or {}
        gprev = prev_health(gstatus)
        if g.get("deleting"):
            continue
        # a paused site's gateway, and one not on the nodes yet or with a change of its own in flight (new, Waiting,
        # Failed, Updating: its job reports it): not read, not judged
        hold = ("SitePaused", "the site is paused: its gateway is not checked") if g.get("paused") else (
            None if g.get("judged", True) else
            ("NotChecked", "the gateway is not realized on the nodes or a change of it is in flight"))
        if hold:
            body = {"health": {"checkedAt": now, "healthy": None, "nodes": [], "skipped": None,
                               "lastWakeAt": gprev.get("lastWakeAt")},
                    "conditions": [{"type": "NodesHealthy", "status": "Unknown", "reason": hold[0], "message": hold[1]},
                                   {"type": "Degraded", "status": "False", "reason": "AsExpected", "message": ""}]}
            answer["gateways"].append({"name": g["name"], "namespace": g.get("namespace"), "status": body,
                                       "changed": comparable(body, cap) != current_comparable(gstatus, cap),
                                       "events": [], "wake": False})
            continue
        gnodes, gevents = node_entries(nodes_in, "gw-" + g["name"], "gateway", gprev, now, cfg, gateway=g["name"])
        gwake = wake(gnodes, gprev, bool(g.get("ready")), now_dt, cfg)
        if gwake:
            gevents.append({"type": "Normal", "reason": "HealthWake",
                            "message": "a full run is requested to repair: " + ", ".join(
                                sorted({f["reason"] for n in gnodes for f in counted(n)}))})
        body = {"health": {"checkedAt": now, "healthy": not any(n["findings"] for n in gnodes), "nodes": gnodes,
                           "skipped": None,
                           "lastWakeAt": now if gwake else gprev.get("lastWakeAt")},
                "conditions": conditions(gnodes, "gateway")}
        answer["gateways"].append({"name": g["name"], "namespace": g.get("namespace"), "status": body,
                                   "changed": gwake or bool(gevents) or comparable(body, cap) != current_comparable(gstatus, cap),
                                   "events": gevents, "wake": gwake})
    return answer


def main():
    req = json.load(sys.stdin)
    print(json.dumps(judge(req), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

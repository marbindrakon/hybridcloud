#!/usr/bin/env python3
"""Tenancy health of the hybridsovereign.redhat API, run as a CronJob on the cluster that holds the CRs.

Passes (each configurable, config file sections in brackets):
1. [redrive]  dispatch re-drive and stall detection (eda_redrive.run_redrive, unchanged logic).
2. [tenancy]  per Entity: conditions ControllerAvailable (the tenant's namespace operator Deployment has an
              available replica; not restarting; not killed for memory) and DeliveryStalled (a change in the
              tenant has had no job for deliveryStalledMinutes), written on the Entity and mirrored on the
              tenant's TenantProfile "shc" when that kind exists.
3. [phase]    status.phase / phaseSince / summary backfill for objects whose stored phase differs from the
              one derived from their current status (shc_status.derive_phase). write: false only reports.
4. [failedRecheck] a failed object of a kind without a periodic re-run gets a Retry-now request every
              intervalMinutes, at most maxRechecks times, then waits for a person.

Exits 1 when any pass found a problem (the platform's job alert fires), 2 on an API error.
usage: shc_tenancy.py [--config FILE] [--dry-run]
"""
import argparse
import datetime as dt
import json
import os
import sys

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eda_redrive  # noqa: E402
import shc_status  # noqa: E402

GROUP = eda_redrive.GROUP
RECONCILE_NOW = eda_redrive.RECONCILE_NOW
RECHECKS = f"{GROUP}/rechecks"
RECHECKED_AT = f"{GROUP}/recheckedAt"
OPERATOR = "hybridsovereign-namespace-operator"
STALLED_MESSAGE = "Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost."

DEFAULTS = {
    "redrive": {"enabled": True},
    "tenancy": {"enabled": True, "unavailableMinutes": 2, "restartWindowMinutes": 15, "restartThreshold": 3,
                "deliveryStalledMinutes": 15, "operatorDeployment": OPERATOR, "entityNamespace": "sovereign-cloud",
                "tenantNamespacePrefix": "entity-"},
    "phase": {"enabled": True, "write": False, "reportLimit": 50},
    "failedRecheck": {"enabled": True, "intervalMinutes": 30, "maxRechecks": 6,
                      "kinds": ["Rbac", "Team", "Project", "Persona", "Vault", "VaultKV", "AAPOrg", "QuayOrg"]},
}


def load_config(path):
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    # a flat config is the former eda-redrive config
    if data and not any(k in data for k in DEFAULTS):
        data = {"redrive": data}
    for key, value in data.items():
        if isinstance(value, dict) and key in cfg:
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def ts(value):
    return eda_redrive.parse_time(value)


def minutes_since(value, now):
    return eda_redrive.minutes_since(value, now)


def stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- pass 2: tenancy ----------------------------------------------------------------------------------------

def controller_condition(deploy, pods, tcfg, now):
    """ControllerAvailable condition of one tenant from its Deployment and pods."""
    if deploy is None:
        return {"type": "ControllerAvailable", "status": "False", "reason": "ControllerUnavailable",
                "message": "The tenant controller is not deployed."}
    st = deploy.status
    available = (st.available_replicas or 0) > 0 if st else False
    if not available:
        since = None
        for c in (st.conditions or []) if st else []:
            if c.type == "Available" and c.status != "True":
                since = c.last_transition_time
        age = (now - since).total_seconds() / 60 if since else None
        if age is None or age >= tcfg["unavailableMinutes"]:
            return {"type": "ControllerAvailable", "status": "False", "reason": "ControllerUnavailable",
                    "message": "The tenant controller has had no available replica for %s min." %
                               ("?" if age is None else "%.0f" % age)}
    window = tcfg["restartWindowMinutes"]
    for pod in pods:
        for cs in (pod.status.container_statuses or []) if pod.status else []:
            term = cs.last_state.terminated if cs.last_state else None
            recent = term is not None and term.finished_at is not None and \
                (now - term.finished_at).total_seconds() / 60 <= window
            if recent and term.reason == "OOMKilled":
                return {"type": "ControllerAvailable", "status": "False", "reason": "ControllerOutOfMemory",
                        "message": "The tenant controller was stopped for using too much memory %s." %
                                   term.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if recent and (cs.restart_count or 0) >= tcfg["restartThreshold"]:
                return {"type": "ControllerAvailable", "status": "False", "reason": "ControllerRestarting",
                        "message": "The tenant controller restarted %d times (last %s)." %
                                   (cs.restart_count, term.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"))}
    return {"type": "ControllerAvailable", "status": "True", "reason": "AsExpected", "message": ""}


# Ready reasons that report a delivery problem themselves; a Failed/Waiting phase with one of them still counts.
DELIVERY_REASONS = ("NoJobStarted", "ControllerUnavailable", "ControllerRestarting", "ControllerOutOfMemory")


def delivery_condition(crs, tcfg, now):
    """DeliveryStalled condition of one tenant from its CRs.

    Counts only undelivered work: an event published but not picked up by a job (or whose publish keeps
    failing), a redrive verdict (Stalled/NoAcknowledgement) for the current event, or a change with no
    event at all (generation > observedGeneration). Not counted: objects whose current event's job finished
    (completedEventId == eventId), objects whose job is running, and objects a controller reported as
    failed, blocked or waiting (phase Failed or Waiting: a refused input, OperatorFailed, a dependency wait).
    """
    limit = tcfg["deliveryStalledMinutes"]
    stalled = []
    for kind, cr in crs:
        meta, status = cr["metadata"], cr.get("status") or {}
        if meta.get("deletionTimestamp") and status.get("deletionComplete"):
            continue
        phase = shc_status.derive_phase(cr)
        if phase == "Ready":
            continue
        delivery = status.get("delivery") or {}
        ref = f"{kind}/{meta['name']}"
        event_id = delivery.get("eventId")
        completed = bool(event_id) and delivery.get("completedEventId") == event_id
        acknowledged = bool(event_id) and delivery.get("acknowledgedEventId") == event_id
        if completed:
            continue  # a job finished this event (failed or not): not a delivery problem
        if delivery.get("state") in ("Stalled", "NoAcknowledgement"):
            stalled.append(ref)
            continue
        if acknowledged:
            continue  # a job is running; a job past its timeout is the redrive's Stalled verdict above
        if event_id:
            # published and not picked up, or the publish keeps failing (no publishedAt, publishFailingSince
            # set); an id with neither is a publish in flight
            age = minutes_since(delivery.get("publishedAt") or delivery.get("publishFailingSince"), now)
            if age is not None and age > limit:
                stalled.append(ref)
            continue
        conds = status.get("conditions") or []
        ready_cond = shc_status.find_condition(conds, "Ready") or {}
        if phase in ("Failed", "Waiting") and ready_cond.get("reason") not in DELIVERY_REASONS:
            continue  # a controller reported it: refused, failed, blocked or waiting
        generation = meta.get("generation") or 0
        observed = status.get("observedGeneration")
        try:
            observed = int(observed) if observed is not None else 0
        except (TypeError, ValueError):
            observed = 0
        if generation > observed:
            age = minutes_since(meta.get("creationTimestamp"), now)
            if age is not None and age > limit:
                stalled.append(ref)
    if stalled:
        return {"type": "DeliveryStalled", "status": "True", "reason": "NoJobStarted", "message": STALLED_MESSAGE}, stalled
    return {"type": "DeliveryStalled", "status": "False", "reason": "AsExpected", "message": ""}, []


def write_conditions(api, version, plural, obj, conds, now, dry_run, writes):
    status = obj.get("status") or {}
    old = status.get("conditions") or []
    merged = shc_status.merge_conditions(old, conds, stamp(now))
    keep = [c for c in old if c.get("type") not in shc_status.LEGACY_TYPES]
    if merged == keep:
        return False
    meta = obj["metadata"]
    patch = {"metadata": {"resourceVersion": meta["resourceVersion"]}, "status": {"conditions": merged}}
    writes.append(f"{obj.get('kind', plural)} {meta['namespace']}/{meta['name']}: " +
                  ", ".join(f"{c['type']}={c['status']}/{c['reason']}" for c in conds))
    if not dry_run:
        try:
            api.patch_namespaced_custom_object_status(GROUP, version, meta["namespace"], plural, meta["name"], patch)
        except ApiException as exc:
            if exc.status != 409:  # a conflict is retried by the next run
                raise
    return True


def run_tenancy(api, kinds, cfg, now, dry_run):
    tcfg = cfg["tenancy"]
    apps, core = client.AppsV1Api(), client.CoreV1Api()
    by_kind = {k["kind"]: k for k in kinds}
    entity_kind = by_kind.get("Entity")
    profile_kind = by_kind.get("TenantProfile")
    counts = {"entities": 0, "controllerUnavailable": 0, "deliveryStalled": 0, "conditionWrites": 0}
    problems, writes = [], []
    if not entity_kind:
        return counts, problems, writes
    for entity in entity_kind["items"]:
        meta = entity["metadata"]
        if meta.get("namespace") != tcfg["entityNamespace"]:
            continue
        counts["entities"] += 1
        name = meta["name"]
        ns = tcfg["tenantNamespacePrefix"] + name
        try:
            deploy = apps.read_namespaced_deployment(tcfg["operatorDeployment"], ns)
        except ApiException as exc:
            if exc.status != 404:
                raise
            deploy = None
        pods = []
        if deploy is not None:
            pods = core.list_namespaced_pod(ns, label_selector=f"app.kubernetes.io/name={tcfg['operatorDeployment']}").items
        if meta.get("deletionTimestamp") and deploy is None:
            continue  # the cascade removed the controller on purpose
        ctrl = controller_condition(deploy, pods, tcfg, now)
        crs = [(k["kind"], cr) for k in kinds if k["kind"] not in ("Entity", "TenantProfile")
               for cr in k["items"] if cr["metadata"].get("namespace") == ns]
        deliv, stalled = delivery_condition(crs, tcfg, now)
        if ctrl["status"] == "False":
            counts["controllerUnavailable"] += 1
            problems.append(f"Entity {name}: ControllerAvailable=False/{ctrl['reason']}: {ctrl['message']}")
        if deliv["status"] == "True":
            counts["deliveryStalled"] += 1
            problems.append(f"Entity {name}: DeliveryStalled for {', '.join(stalled[:10])}")
        if write_conditions(api, entity_kind["version"], entity_kind["plural"], entity, [ctrl, deliv], now, dry_run, writes):
            counts["conditionWrites"] += 1
        if profile_kind:
            for prof in profile_kind["items"]:
                if prof["metadata"].get("namespace") == ns and prof["metadata"]["name"] == "shc":
                    if write_conditions(api, profile_kind["version"], profile_kind["plural"], prof, [ctrl, deliv],
                                        now, dry_run, writes):
                        counts["conditionWrites"] += 1
    return counts, problems, writes


# ---- pass 3: phase backfill ---------------------------------------------------------------------------------

def run_phase(api, kinds, cfg, now, dry_run):
    pcfg = cfg["phase"]
    write = bool(pcfg.get("write")) and not dry_run
    counts = {"phaseDiffs": 0, "phaseWrites": 0}
    report = []
    for k in kinds:
        for cr in k["items"]:
            patch = shc_status.phase_backfill_patch(cr, stamp(now))
            if patch is None:
                continue
            counts["phaseDiffs"] += 1
            meta, status = cr["metadata"], cr.get("status") or {}
            if len(report) < pcfg.get("reportLimit", 50):
                report.append(f"{k['kind']} {meta['namespace']}/{meta['name']}: phase {status.get('phase')!r} -> "
                              f"{patch['status'].get('phase', status.get('phase'))!r}"
                              + (" (legacy conditions dropped)" if "conditions" in patch["status"] else ""))
            if write:
                try:
                    api.patch_namespaced_custom_object_status(GROUP, k["version"], meta["namespace"], k["plural"],
                                                              meta["name"], patch)
                    counts["phaseWrites"] += 1
                except ApiException as exc:
                    if exc.status not in (404, 409):
                        raise
    return counts, report


# ---- pass 4: slow re-check of failed objects -------------------------------------------------------------------

def run_failed_recheck(api, kinds, cfg, now, dry_run):
    fcfg = cfg["failedRecheck"]
    counts = {"rechecked": 0, "recheckExhausted": 0}
    actions = []
    for k in kinds:
        if k["kind"] not in fcfg["kinds"]:
            continue
        for cr in k["items"]:
            meta = cr["metadata"]
            ann = meta.get("annotations") or {}
            phase = shc_status.derive_phase(cr)
            ns, name = meta["namespace"], meta["name"]

            def patch_ann(values):
                if not dry_run:
                    api.patch_namespaced_custom_object(GROUP, k["version"], ns, k["plural"], name,
                                                       {"metadata": {"annotations": values}})

            if phase != "Failed" or meta.get("deletionTimestamp"):
                if RECHECKS in ann and phase == "Ready":
                    patch_ann({RECHECKS: None, RECHECKED_AT: None})
                continue
            done = int(ann.get(RECHECKS, "0") or 0)
            if done >= fcfg["maxRechecks"]:
                counts["recheckExhausted"] += 1
                continue
            age = minutes_since(ann.get(RECHECKED_AT), now)
            if age is not None and age < fcfg["intervalMinutes"]:
                continue
            counts["rechecked"] += 1
            actions.append(f"recheck {k['kind']} {ns}/{name} ({done + 1}/{fcfg['maxRechecks']})")
            patch_ann({RECONCILE_NOW: stamp(now), RECHECKS: str(done + 1), RECHECKED_AT: stamp(now)})
    return counts, actions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/shc-tenancy/config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    api = eda_redrive.connect()
    now = dt.datetime.now(dt.timezone.utc)
    kinds = eda_redrive.collect(api)
    summary = {"time": stamp(now), "dryRun": args.dry_run}
    problems = []
    if cfg["redrive"].get("enabled", True):
        rcfg = {k: v for k, v in cfg["redrive"].items() if k != "enabled"}
        counts, probs = eda_redrive.run_redrive(api, kinds, rcfg, now, args.dry_run)
        summary["redrive"] = counts
        problems += probs
    if cfg["tenancy"].get("enabled", True):
        counts, probs, writes = run_tenancy(api, kinds, cfg, now, args.dry_run)
        summary["tenancy"] = counts
        problems += probs
        for w in writes:
            print(("would write " if args.dry_run else "wrote ") + w)
    if cfg["phase"].get("enabled", True):
        counts, report = run_phase(api, kinds, cfg, now, args.dry_run)
        summary["phase"] = dict(counts, write=bool(cfg["phase"].get("write")))
        for r in report:
            print("phase", r)
    if cfg["failedRecheck"].get("enabled", True):
        counts, actions = run_failed_recheck(api, kinds, cfg, now, args.dry_run)
        summary["failedRecheck"] = counts
        for a in actions:
            print(("would " if args.dry_run else "") + a)
    print(json.dumps(summary))
    for p in problems:
        print("PROBLEM", p)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiException as exc:
        print(f"API error: {exc.status} {exc.reason}", file=sys.stderr)
        sys.exit(2)

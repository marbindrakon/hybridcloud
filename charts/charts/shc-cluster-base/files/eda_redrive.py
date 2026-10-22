#!/usr/bin/env python3
"""Re-drive and stall detection for operator → Kafka → EDA → AAP dispatch.

shc LAB-PLAN §11.1, design/DELIVERY-HARDENING.md §3.4. Runs as a CronJob on the cluster that holds the
hybridsovereign.redhat CRs. For every CR with outstanding work (not ready, or ready with a current event
that no job completed yet: a re-run keeps a Ready object Ready until its job writes):

- published (status.delivery.publishedAt) but not acknowledged by an EDA job within ackTimeoutMinutes:
  annotate reconcileNow=true (the legacy request every operator generation acts on), or redeliver=true for a
  CR being deleted (the operator re-publishes with a new eventId), at most maxRedrives
  times, and only for kinds in redriveKinds; otherwise status.delivery.state=NoAcknowledgement
- acknowledged but not completed within the kind's jobTimeoutMinutes: status.delivery.state=Stalled
  (never re-driven: the job may still be changing things)
- acknowledged, still Reconciling=True/JobRunning, and no completing status write within the kind's
  jobFinishedMinutes (default ackTimeoutMinutes; a kind with its own jobTimeoutMinutes uses that): the job most
  likely ended without its final write (it died before it, e.g. a failed name lookup). Reported as
  status.delivery.state=NoCompletion, never re-driven (Retry now after a look at the job)
- not published and status.delivery.publishFailingSince older than publishFailedMinutes: reported
  (the operator is already retrying)

Exits 1 while any CR is NoAcknowledgement, Stalled, NoCompletion or PublishFailed, so the platform's job alert
fires. Events from operators without eventId support carry no status.delivery and are skipped.

usage: eda_redrive.py [--config FILE] [--dry-run]
The logic is run_redrive(); shc_tenancy.py imports it (this entry point is kept for one release).
"""
import argparse
import datetime as dt
import json
import sys

import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

GROUP = "hybridsovereign.redhat"
REDRIVES = f"{GROUP}/redrives"
RECONCILE_NOW = "ansible.sdk.operatorframework.io/reconcileNow"
# Machine writers use the legacy literal: operators of every generation act on it each time and remove it after
# publishing, while a timestamp is only understood by the current ones (namespace operators upgrade per tenant).
RECONCILE_REQUEST = "true"
# delete finalizers publish once (LAB-PLAN §11.6); this asks them to publish the delete event again
REDELIVER = f"{GROUP}/redeliver"

DEFAULTS = {
    "ackTimeoutMinutes": 10,
    "jobTimeoutMinutes": 30,
    "publishFailedMinutes": 15,
    "maxRedrives": 3,
    "redriveKinds": [],
    "kinds": {},
}


def parse_time(value):
    if not value:
        return None
    return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def minutes_since(value, now):
    t = parse_time(value)
    return (now - t).total_seconds() / 60 if t else None


def load_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    except FileNotFoundError:
        pass
    return cfg


def connect():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()


def collect(api):
    """Every namespaced kind of the group with its CRs: [{kind, plural, version, items}]."""
    out = []
    for crd in client.ApiextensionsV1Api().list_custom_resource_definition().items:
        if crd.spec.group != GROUP or crd.spec.scope != "Namespaced":
            continue
        version = next(v.name for v in crd.spec.versions if v.storage)
        out.append({"kind": crd.spec.names.kind, "plural": crd.spec.names.plural, "version": version,
                    "labels": dict(crd.metadata.labels or {}),
                    "items": api.list_cluster_custom_object(GROUP, version, crd.spec.names.plural).get("items", [])})
    return out


def job_finished_minutes(kind, cfg):
    """Minutes after the job's start by which its completing status write is expected (see the module docstring)."""
    cfg = dict(DEFAULTS, **(cfg or {}))
    kcfg = cfg["kinds"].get(kind, {}) or {}
    if "jobFinishedMinutes" in kcfg:
        return kcfg["jobFinishedMinutes"]
    if "jobTimeoutMinutes" in kcfg:
        return kcfg["jobTimeoutMinutes"]
    return cfg.get("jobFinishedMinutes", cfg["ackTimeoutMinutes"])


def job_running(status):
    """True when the object's status still says a job runs (Reconciling=True/JobRunning)."""
    for c in (status or {}).get("conditions") or []:
        if isinstance(c, dict) and c.get("type") == "Reconciling":
            return str(c.get("status")) == "True" and c.get("reason") == "JobRunning"
    return False


def no_completion(kind, status, cfg, now):
    """Minutes since the job started when its completing write is overdue (job_finished_minutes), else None."""
    delivery = (status or {}).get("delivery") or {}
    event_id = delivery.get("eventId")
    if not event_id or delivery.get("acknowledgedEventId") != event_id or delivery.get("completedEventId") == event_id:
        return None
    if not job_running(status):
        return None
    age = minutes_since(delivery.get("jobStartedAt"), now)
    if age is None or age <= job_finished_minutes(kind, cfg):
        return None
    return age


def settled(status):
    """True when an object needs no delivery checks: ready, and its current event (if any) completed.

    A re-run (Retry now, a machine wake, a period) keeps a Ready object Ready until its job writes, so
    ready alone does not mean the last event was delivered.
    """
    status = status or {}
    if status.get("ready") is not True:
        return False
    delivery = status.get("delivery") or {}
    event_id = delivery.get("eventId")
    return not event_id or delivery.get("completedEventId") == event_id


def run_redrive(api, kinds, cfg, now, dry_run):
    """Pass over every CR: re-drive unacknowledged events, mark stalled jobs. Returns (counts, problems)."""
    cfg = dict(DEFAULTS, **(cfg or {}))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    args = argparse.Namespace(dry_run=dry_run)

    counts = {"crs": 0, "notReady": 0, "readyOutstanding": 0, "published": 0, "acknowledged": 0, "redriven": 0,
              "noAcknowledgement": 0, "stalled": 0, "noCompletion": 0, "publishFailed": 0, "legacy": 0}
    problems = []

    for k in kinds:
        version, plural, kind = k["version"], k["plural"], k["kind"]
        kcfg = cfg["kinds"].get(kind, {})
        job_timeout = kcfg.get("jobTimeoutMinutes", cfg["jobTimeoutMinutes"])
        may_redrive = kind in cfg["redriveKinds"] and kcfg.get("redrive", True)

        for cr in k["items"]:
            counts["crs"] += 1
            meta, status = cr["metadata"], cr.get("status") or {}
            ns, name = meta["namespace"], meta["name"]
            ref = f"{kind} {ns}/{name}"
            annotations = meta.get("annotations") or {}
            delivery = status.get("delivery") or {}

            def patch_status(delivery_patch):
                if not args.dry_run:
                    api.patch_namespaced_custom_object_status(
                        GROUP, version, ns, plural, name, {"status": {"delivery": delivery_patch}})

            def patch_annotations(ann):
                if not args.dry_run:
                    api.patch_namespaced_custom_object(
                        GROUP, version, ns, plural, name, {"metadata": {"annotations": ann}})

            event_id = delivery.get("eventId")
            if settled(status):
                # ready and nothing outstanding: clear our bookkeeping
                if REDRIVES in annotations:
                    patch_annotations({REDRIVES: None})
                if delivery.get("state"):
                    patch_status({"state": None, "stateSince": None})
                continue

            if status.get("ready") is True:
                counts["readyOutstanding"] += 1  # a re-run of a Ready object: its event still has to finish
            else:
                counts["notReady"] += 1
            if not event_id:
                counts["legacy"] += 1
                continue
            # delivery.publishedAt is cleared before each publish and set once Kafka accepted the event.
            # (The Published condition is informational: EDA's merge patches replace the conditions list.)
            if not delivery.get("publishedAt"):
                # failing: the operator retries with backoff; report once it has failed for a while
                age = minutes_since(delivery.get("publishFailingSince"), now)
                if age is not None and age > cfg["publishFailedMinutes"]:
                    published = next((c for c in status.get("conditions") or [] if c.get("type") == "Published"), {})
                    counts["publishFailed"] += 1
                    problems.append(f"{ref}: PublishFailed for {age:.0f} min: {published.get('message', '')}")
                continue
            counts["published"] += 1

            acknowledged = delivery.get("acknowledgedEventId") == event_id
            completed = delivery.get("completedEventId") == event_id
            if completed:
                continue  # EDA finished this event (failed or not); not a delivery problem
            if acknowledged:
                counts["acknowledged"] += 1
                age = minutes_since(delivery.get("jobStartedAt"), now)
                if age is not None and age > job_timeout:
                    counts["stalled"] += 1
                    problems.append(f"{ref}: Stalled, job {delivery.get('jobId', '?')} started {age:.0f} min ago "
                                    f"(timeout {job_timeout} min)")
                    if delivery.get("state") != "Stalled":
                        patch_status({"state": "Stalled", "stateSince": stamp})
                    continue
                overdue = no_completion(kind, status, cfg, now)
                if overdue is not None:
                    counts["noCompletion"] += 1
                    problems.append(f"{ref}: NoCompletion, job {delivery.get('jobId', '?')} started {overdue:.0f} min ago "
                                    f"and has not written its final status (expected within "
                                    f"{job_finished_minutes(kind, cfg)} min); it most likely ended early")
                    if delivery.get("state") != "NoCompletion":
                        patch_status({"state": "NoCompletion", "stateSince": stamp})
                continue

            age = minutes_since(delivery.get("publishedAt"), now)
            if age is None or age <= cfg["ackTimeoutMinutes"]:
                continue
            redrives = int(annotations.get(REDRIVES, "0") or 0)
            if may_redrive and redrives < cfg["maxRedrives"]:
                counts["redriven"] += 1
                print(f"redrive {ref}: event {event_id} unacknowledged for {age:.0f} min "
                      f"(redrive {redrives + 1}/{cfg['maxRedrives']})")
                trigger = REDELIVER if meta.get("deletionTimestamp") else RECONCILE_NOW
                patch_annotations({trigger: RECONCILE_REQUEST if trigger == RECONCILE_NOW else "true",
                                   REDRIVES: str(redrives + 1)})
            else:
                counts["noAcknowledgement"] += 1
                why = "redrive disabled for this kind" if not may_redrive else f"{redrives} redrives"
                problems.append(f"{ref}: NoAcknowledgement for event {event_id}, {age:.0f} min ({why})")
                if delivery.get("state") != "NoAcknowledgement":
                    patch_status({"state": "NoAcknowledgement", "stateSince": stamp})

    return counts, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/eda-redrive/config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    api = connect()
    now = dt.datetime.now(dt.timezone.utc)
    counts, problems = run_redrive(api, collect(api), cfg, now, args.dry_run)
    print(json.dumps({"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "dryRun": args.dry_run, **counts}))
    for p in problems:
        print("PROBLEM", p)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiException as exc:
        print(f"API error: {exc.status} {exc.reason}", file=sys.stderr)
        sys.exit(2)

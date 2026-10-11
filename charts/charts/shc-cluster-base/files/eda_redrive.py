#!/usr/bin/env python3
"""Re-drive and stall detection for operator → Kafka → EDA → AAP dispatch.

shc LAB-PLAN §11.1, design/DELIVERY-HARDENING.md §3.4. Runs as a CronJob on the cluster that holds the
hybridsovereign.redhat CRs. For every CR that is not ready:

- published (status.delivery.publishedAt) but not acknowledged by an EDA job within ackTimeoutMinutes:
  annotate reconcileNow=true, or redeliver=true for a CR being deleted (the operator re-publishes with
  a new eventId), at most maxRedrives
  times, and only for kinds in redriveKinds; otherwise status.delivery.state=NoAcknowledgement
- acknowledged but not completed within the kind's jobTimeoutMinutes: status.delivery.state=Stalled
  (never re-driven: the job may still be changing things)
- not published and status.delivery.publishFailingSince older than publishFailedMinutes: reported
  (the operator is already retrying)

Exits 1 while any CR is NoAcknowledgement, Stalled or PublishFailed, so the platform's job alert
fires. Events from operators without eventId support carry no status.delivery and are skipped.

usage: eda_redrive.py [--config FILE] [--dry-run]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/eda-redrive/config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = dict(DEFAULTS)
    try:
        with open(args.config) as f:
            cfg.update(yaml.safe_load(f) or {})
    except FileNotFoundError:
        pass

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    crds = client.ApiextensionsV1Api().list_custom_resource_definition().items
    api = client.CustomObjectsApi()
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    counts = {"crs": 0, "notReady": 0, "published": 0, "acknowledged": 0, "redriven": 0,
              "noAcknowledgement": 0, "stalled": 0, "publishFailed": 0, "legacy": 0}
    problems = []

    for crd in crds:
        if crd.spec.group != GROUP or crd.spec.scope != "Namespaced":
            continue
        version = next(v.name for v in crd.spec.versions if v.storage)
        plural, kind = crd.spec.names.plural, crd.spec.names.kind
        kcfg = cfg["kinds"].get(kind, {})
        job_timeout = kcfg.get("jobTimeoutMinutes", cfg["jobTimeoutMinutes"])
        may_redrive = kind in cfg["redriveKinds"] and kcfg.get("redrive", True)

        for cr in api.list_cluster_custom_object(GROUP, version, plural).get("items", []):
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

            if status.get("ready") is True:
                # ready again: clear our bookkeeping
                if REDRIVES in annotations:
                    patch_annotations({REDRIVES: None})
                if delivery.get("state"):
                    patch_status({"state": None, "stateSince": None})
                continue

            counts["notReady"] += 1
            event_id = delivery.get("eventId")
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

            age = minutes_since(delivery.get("publishedAt"), now)
            if age is None or age <= cfg["ackTimeoutMinutes"]:
                continue
            redrives = int(annotations.get(REDRIVES, "0") or 0)
            if may_redrive and redrives < cfg["maxRedrives"]:
                counts["redriven"] += 1
                print(f"redrive {ref}: event {event_id} unacknowledged for {age:.0f} min "
                      f"(redrive {redrives + 1}/{cfg['maxRedrives']})")
                trigger = REDELIVER if meta.get("deletionTimestamp") else RECONCILE_NOW
                patch_annotations({trigger: "true", REDRIVES: str(redrives + 1)})
            else:
                counts["noAcknowledgement"] += 1
                why = "redrive disabled for this kind" if not may_redrive else f"{redrives} redrives"
                problems.append(f"{ref}: NoAcknowledgement for event {event_id}, {age:.0f} min ({why})")
                if delivery.get("state") != "NoAcknowledgement":
                    patch_status({"state": "NoAcknowledgement", "stateSince": stamp})

    print(json.dumps({"time": stamp, "dryRun": args.dry_run, **counts}))
    for p in problems:
        print("PROBLEM", p)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiException as exc:
        print(f"API error: {exc.status} {exc.reason}", file=sys.stderr)
        sys.exit(2)

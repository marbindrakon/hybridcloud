#!/usr/bin/env python3
"""One-off migration of every hybridsovereign.redhat object's status to the status contract.

  backfill  status.lastHandledReconcileRequest = the object's current reconcileNow annotation, so the switch to
            "any new value is a Retry-now request" does not wake every object once
  strip     drop the legacy condition types Running, Successful and Failure (written by the operator SDK
            before manageStatus: false) and store the derived status.phase / phaseSince / summary

Each object is patched on /status with its resourceVersion (a conflict re-reads it, up to 3 times).

usage: strip_legacy_conditions.py (--dry-run | --apply | --verify) [--only backfill|strip] [--kubeconfig FILE]
  --verify   exit 1 when any object still carries a legacy condition type (or an unhandled annotation with
             --only backfill)
The status library is imported from (first found): $SHC_STATUS_PATH, /etc/shc-tenancy, the collection in this
repository.
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for path in (os.environ.get("SHC_STATUS_PATH", ""), "/etc/shc-tenancy",
             os.path.join(HERE, "..", "..", "eda", "rulebooks", "collections", "ansible_collections",
                          "hybridsovereign", "shc", "plugins", "module_utils")):
    if path and os.path.exists(os.path.join(path, "shc_status.py")):
        sys.path.insert(0, path)
        break
import shc_status  # noqa: E402

from kubernetes import client, config  # noqa: E402
from kubernetes.client.rest import ApiException  # noqa: E402

GROUP = "hybridsovereign.redhat"
RECONCILE_NOW = "ansible.sdk.operatorframework.io/reconcileNow"


def plan(obj, only, now):
    """The /status merge patch for one object, or None."""
    meta = obj.get("metadata") or {}
    status = obj.get("status") or {}
    body = {}
    if only in (None, "backfill"):
        request = str((meta.get("annotations") or {}).get(RECONCILE_NOW) or "").strip()
        if request and status.get("lastHandledReconcileRequest") != request:
            body["lastHandledReconcileRequest"] = request
    if only in (None, "strip"):
        patch = shc_status.phase_backfill_patch(obj, now)
        if patch:
            body.update(patch["status"])
    if not body:
        return None
    return {"metadata": {"resourceVersion": meta.get("resourceVersion")}, "status": body}


def legacy_types(obj):
    return sorted({c.get("type") for c in ((obj.get("status") or {}).get("conditions") or [])
                   if isinstance(c, dict) and c.get("type") in shc_status.LEGACY_TYPES})


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    ap.add_argument("--only", choices=["backfill", "strip"])
    ap.add_argument("--kubeconfig")
    args = ap.parse_args()
    if args.kubeconfig:
        config.load_kube_config(args.kubeconfig)
    else:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
    api = client.CustomObjectsApi()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = {"objects": 0, "planned": 0, "patched": 0, "conflicts": 0, "errors": 0,
              "backfill": 0, "Running": 0, "Successful": 0, "Failure": 0, "remaining": 0}
    for crd in client.ApiextensionsV1Api().list_custom_resource_definition().items:
        if crd.spec.group != GROUP or crd.spec.scope != "Namespaced":
            continue
        version = next(v.name for v in crd.spec.versions if v.storage)
        plural, kind = crd.spec.names.plural, crd.spec.names.kind
        for obj in api.list_cluster_custom_object(GROUP, version, plural).get("items", []):
            counts["objects"] += 1
            meta = obj["metadata"]
            ref = f"{kind} {meta['namespace']}/{meta['name']}"
            if args.verify:
                left = legacy_types(obj) if args.only in (None, "strip") else []
                p = plan(obj, "backfill", now) if args.only in (None, "backfill") else None
                if left or p:
                    counts["remaining"] += 1
                    print(f"remaining {ref}: {left or ''}{' unhandled reconcileNow' if p else ''}")
                continue
            for attempt in range(3):
                patch = plan(obj, args.only, now)
                if patch is None:
                    break
                if attempt == 0:
                    counts["planned"] += 1
                    for t in legacy_types(obj):
                        counts[t] += 1
                    if "lastHandledReconcileRequest" in patch["status"]:
                        counts["backfill"] += 1
                    print(("would patch " if args.dry_run else "patch ") + ref + ": " +
                          ", ".join(sorted(patch["status"].keys())))
                if args.dry_run:
                    break
                try:
                    api.patch_namespaced_custom_object_status(GROUP, version, meta["namespace"], plural, meta["name"], patch)
                    counts["patched"] += 1
                    break
                except ApiException as exc:
                    if exc.status == 409 and attempt < 2:
                        counts["conflicts"] += 1
                        obj = api.get_namespaced_custom_object(GROUP, version, meta["namespace"], plural, meta["name"])
                        continue
                    if exc.status == 404:
                        break
                    counts["errors"] += 1
                    print(f"error {ref}: {exc.status} {exc.reason}", file=sys.stderr)
                    break
    print(json.dumps(dict(counts, mode="verify" if args.verify else ("apply" if args.apply else "dry-run"),
                          only=args.only or "all", time=now)))
    if args.verify:
        return 1 if counts["remaining"] else 0
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
# Status library of the hybridsovereign.redhat API: one place that merges conditions, keeps the
# delivery bookkeeping, counts failed teardowns and derives status.phase / status.summary.
#
# Pure Python (standard library only, no Ansible or Kubernetes imports) so the same file runs in
# AAP jobs (as module_utils of the hybridsovereign.shc collection), in the shc-tenancy CronJob and
# in the one-off migration script. scripts/sync-shc-status.sh copies it into
# charts/charts/shc-cluster-base/files/. Edit only this copy.
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import copy
import datetime
import re

try:  # inside the collection
    from ansible_collections.hybridsovereign.shc.plugins.module_utils import shc_status_reasons as _reasons
except ImportError:  # plain copy next to this file (CronJob, scripts)
    import shc_status_reasons as _reasons  # type: ignore

LEGACY_TYPES = ("Running", "Successful", "Failure")
PHASES = ("Waiting", "Creating", "Updating", "Ready", "Degraded", "Failed", "Deleting")
SUMMARY_MAX = 140
DEFAULT_GIVE_UP_ATTEMPTS = 10

# Words a tenant-facing summary never contains (realization detail and internal tool names).
BANNED_SUMMARY = re.compile(
    r"\b(EDA|AAP|reconcil\w*|VNI|VRF|vault|backend|pending|gateway|fabric node|BGW)\b", re.IGNORECASE)

# Legacy status.status values (the action of the writing job) -> synthesized Ready condition.
_LEGACY_READY = {
    "ready": ("True", "Ready"),
    "success": ("True", "Ready"),
    "failed": ("False", "Failed"),
    "pending": ("False", "Waiting"),
    "reconciling": ("False", "InProgress"),
}

# Job status recorded in status.edaJobs per action.
_JOB_STATUS = {"ready": "success", "success": "success", "failed": "failed",
               "reconciling": "running", "pending": "waiting"}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cond_status(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    text = str(value) if value is not None else "Unknown"
    low = text.lower()
    if low == "true":
        return "True"
    if low == "false":
        return "False"
    if low == "unknown":
        return "Unknown"
    return text


def find_condition(conditions, ctype):
    for cond in conditions or []:
        if isinstance(cond, dict) and cond.get("type") == ctype:
            return cond
    return None


def _cond_true(conditions, ctype):
    cond = find_condition(conditions, ctype)
    return cond is not None and _cond_status(cond.get("status")) == "True"


def _default_reason(status):
    if status == "True":
        return "AsExpected"
    if status == "False":
        return "NotReady"
    return "Unknown"


def merge_conditions(old, new, now=None):
    """Merge `new` conditions into `old` by type.

    Keeps every other type, drops the legacy operator types, keeps lastTransitionTime when the
    status did not change, coerces status to a string and fills an empty reason.
    """
    now = now or utcnow()
    out = []
    index = {}
    for cond in old or []:
        if not isinstance(cond, dict) or not cond.get("type") or cond.get("type") in LEGACY_TYPES:
            continue
        if cond["type"] in index:
            continue
        index[cond["type"]] = len(out)
        out.append(dict(cond))
    for cond in new or []:
        if not isinstance(cond, dict) or not cond.get("type") or cond.get("type") in LEGACY_TYPES:
            continue
        item = dict(cond)
        item["status"] = _cond_status(item.get("status"))
        if not item.get("reason"):
            item["reason"] = _default_reason(item["status"])
        if item.get("message") is None:
            item["message"] = ""
        else:
            item["message"] = str(item["message"])
        prev = out[index[item["type"]]] if item["type"] in index else None
        if prev is not None and _cond_status(prev.get("status")) == item["status"] and prev.get("lastTransitionTime"):
            item["lastTransitionTime"] = prev["lastTransitionTime"]
        elif not item.get("lastTransitionTime") or prev is not None:
            item["lastTransitionTime"] = now
        if "observedGeneration" in item and item["observedGeneration"] is not None:
            try:
                item["observedGeneration"] = int(item["observedGeneration"])
            except (TypeError, ValueError):
                item.pop("observedGeneration")
        if prev is not None:
            out[index[item["type"]]] = item
        else:
            index[item["type"]] = len(out)
            out.append(item)
    return out


def synthesize_ready(body, old_conditions):
    """A Ready condition derived from the legacy body (status / ready / reason / message).

    Returns None when the body carries its own Ready condition or no legacy state. A legacy
    "reconciling" never replaces an existing Ready=True (a routine re-run does not flip Ready).
    """
    if find_condition(body.get("conditions"), "Ready") is not None:
        return None
    action = body.get("status")
    if isinstance(action, str) and action.lower() in _LEGACY_READY:
        status, reason = _LEGACY_READY[action.lower()]
        if action.lower() == "reconciling":
            if _cond_true(old_conditions, "Ready"):
                return None
            reason = "InProgress"
        elif body.get("reason"):
            reason = str(body["reason"])
    elif isinstance(body.get("ready"), bool):
        status, reason = ("True", "Ready") if body["ready"] else ("False", "NotReady")
        if body.get("reason"):
            reason = str(body["reason"])
    else:
        return None
    return {"type": "Ready", "status": status, "reason": reason,
            "message": str(body.get("message") or "")}


def keeps_ready(obj, new_conditions=None):
    """True when a "reconciling" write must not flip the object out of Ready: it is Ready (Ready condition
    True) and its spec was handled (status.observedGeneration >= metadata.generation), and the write carries no
    Ready condition of its own. Controllers mark creates and spec changes not ready before they publish, so
    this is a re-run (Retry now, a wake, a periodic reconcile).
    """
    if find_condition(new_conditions, "Ready") is not None:
        return False
    obj = obj or {}
    status = obj.get("status") or {}
    if not _cond_true(status.get("conditions") or [], "Ready"):
        return False
    try:
        generation = int((obj.get("metadata") or {}).get("generation") or 0)
        observed = int(status.get("observedGeneration") or 0)
    except (TypeError, ValueError):
        return False
    return generation > 0 and observed >= generation


def apply_delivery(cur_status, body, action, event_id, job, partial, now):
    """Delivery bookkeeping of one job write (status.delivery and status.edaJobs)."""
    cur_delivery = (cur_status or {}).get("delivery") or {}
    if event_id and not partial:
        if action == "reconciling":
            # a second "reconciling" write for the same event keeps jobStartedAt (the stall timer)
            if cur_delivery.get("acknowledgedEventId") != event_id:
                body["delivery"] = {"acknowledgedEventId": event_id,
                                    "jobId": str((job or {}).get("jobId", "")), "jobStartedAt": now}
        else:
            body["delivery"] = {"completedEventId": event_id, "completedAt": now}
    if not partial and job is not None:
        entry = dict(job)
        entry.setdefault("timestamp", now)
        entry["status"] = _JOB_STATUS.get(action, "running")
        body["edaJobs"] = [entry]
    return body


def superseded(cur_status, event_id):
    """True when the object's current event is another one (this job's event was replaced)."""
    if not event_id:
        return False
    cur_id = ((cur_status or {}).get("delivery") or {}).get("eventId", "")
    return bool(cur_id) and cur_id != event_id


def count_teardown_failure(obj, body, action, give_up_attempts=DEFAULT_GIVE_UP_ATTEMPTS, now=None):
    """Count a failed teardown job in status.retry; returns a Stalled condition at the give-up threshold.

    Only a failed write on a deleting object counts; waits (pending) never do.
    """
    now = now or utcnow()
    meta = (obj or {}).get("metadata") or {}
    if not meta.get("deletionTimestamp") or action != "failed":
        return None
    retry = dict(((obj or {}).get("status") or {}).get("retry") or {})
    try:
        attempts = int(retry.get("attempts") or 0) + 1
    except (TypeError, ValueError):
        attempts = 1
    retry["attempts"] = attempts
    retry["failingSince"] = retry.get("failingSince") or now
    retry["lastFailureTime"] = now
    body["retry"] = retry
    if give_up_attempts and attempts >= int(give_up_attempts):
        return {"type": "Stalled", "status": "True", "reason": "TeardownFailed",
                "message": "Deleting failed %d times; automatic retries stopped. Use Retry now after the cause is fixed." % attempts}
    return None


def _merge_patch(target, patch):
    """RFC 7386 merge patch (None removes a key)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict):
            out[key] = _merge_patch(out.get(key), value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def derive_phase(obj, conditions=None):
    """status.phase of an object (first matching rule wins). Controllers never read the result."""
    obj = obj or {}
    meta = obj.get("metadata") or {}
    status = obj.get("status") or {}
    conds = conditions if conditions is not None else (status.get("conditions") or [])
    if meta.get("deletionTimestamp"):
        return "Deleting"
    if _cond_true(conds, "Stalled"):
        return "Failed"
    ready_cond = find_condition(conds, "Ready")
    ready = _cond_status(ready_cond.get("status")) == "True" if ready_cond is not None else status.get("ready") is True
    observed = status.get("observedGeneration")
    try:
        observed = int(observed) if observed is not None else None
    except (TypeError, ValueError):
        observed = None
    generation = meta.get("generation")
    if ready:
        if _cond_true(conds, "Degraded"):
            return "Degraded"
        if observed is not None and generation is not None and int(generation) > observed:
            return "Updating"
        return "Ready"
    legacy = str(status.get("status") or "").lower()
    rclass = _reasons.reason_class(ready_cond.get("reason")) if ready_cond is not None else None
    if legacy == "failed" or rclass in (_reasons.FAILED, _reasons.BLOCKED):
        return "Failed"
    if legacy == "pending" or rclass == _reasons.WAITING:
        return "Waiting"
    if not observed and status.get("realized") is not True:
        return "Creating"
    return "Updating"


# Which reason classes fit a phase (a reason of another class falls back to the phase sentence).
_PHASE_CLASSES = {
    "Ready": (_reasons.READY,),
    "Degraded": (_reasons.READY, _reasons.FAILED, _reasons.WAITING),
    "Failed": (_reasons.FAILED, _reasons.BLOCKED),
    "Waiting": (_reasons.WAITING,),
    "Creating": (_reasons.PROGRESS,),
    "Updating": (_reasons.PROGRESS,),
    "Deleting": (_reasons.PROGRESS, _reasons.BLOCKED, _reasons.FAILED, _reasons.WAITING),
}


def safe_summary(text, phase):
    fallback = _reasons.PHASE_SUMMARIES.get(phase, "")
    if not text or BANNED_SUMMARY.search(text):
        return fallback
    text = " ".join(str(text).split())
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 1].rstrip() + "…"
    return text


def build_summary(obj, conditions, phase):
    """Tenant-safe one-line summary for a phase, from the reasons registry (never the raw message)."""
    conds = conditions or []
    deciding = None
    if phase == "Deleting":
        for ctype in ("Stalled", "DeletionBlocked"):
            if _cond_true(conds, ctype):
                deciding = find_condition(conds, ctype)
                break
        ready_cond = find_condition(conds, "Ready")
        if deciding is None and ready_cond is not None and ready_cond.get("reason") == "Deleted":
            deciding = ready_cond
    elif phase == "Failed" and _cond_true(conds, "Stalled"):
        deciding = find_condition(conds, "Stalled")
    elif phase == "Degraded":
        deciding = find_condition(conds, "Degraded")
    else:
        deciding = find_condition(conds, "Ready")
    entry = _reasons.lookup(deciding.get("reason")) if deciding else None
    if entry and entry["class"] in _PHASE_CLASSES.get(phase, ()):
        return safe_summary(entry["summary"], phase)
    return safe_summary(None, phase)


def phase_fields(obj, merged_status, now=None):
    """phase / phaseSince / summary for the object with `merged_status` as its status."""
    now = now or utcnow()
    view = {"metadata": (obj or {}).get("metadata") or {}, "status": merged_status or {}}
    conds = view["status"].get("conditions") or []
    phase = derive_phase(view, conds)
    cur = ((obj or {}).get("status") or {})
    since = cur.get("phaseSince") if cur.get("phase") == phase and cur.get("phaseSince") else now
    return {"phase": phase, "phaseSince": since, "summary": build_summary(view, conds, phase)}


def compute_status_patch(obj, body, opts=None, now=None):
    """Compute the merge patch for one status write.

    obj   the live object (GET result, with metadata.resourceVersion)
    body  the status fields the job wants to write (legacy shape accepted)
    opts  action, event_id, partial, job (edaJobs entry), give_up_attempts
    Returns (patch or None, info). patch is {"metadata": {"resourceVersion"}, "status": {...}}.
    """
    opts = opts or {}
    now = now or utcnow()
    obj = obj or {}
    cur_status = obj.get("status") or {}
    action = str(opts.get("action") or body.get("status") or "reconcile")
    event_id = opts.get("event_id") or ""
    partial = bool(opts.get("partial"))
    info = {"superseded": False, "action": action}
    if superseded(cur_status, event_id):
        info.update({"superseded": True, "eventId": event_id,
                     "currentEventId": cur_status.get("delivery", {}).get("eventId", "")})
        return None, info

    body = copy.deepcopy(body or {})
    if "observedGeneration" in body and body["observedGeneration"] is not None:
        try:
            body["observedGeneration"] = int(body["observedGeneration"])
        except (TypeError, ValueError):
            body.pop("observedGeneration")
    apply_delivery(cur_status, body, action, event_id, opts.get("job"), partial, now)

    old_conds = cur_status.get("conditions") or []
    new_conds = [c for c in (body.pop("conditions", None) or []) if isinstance(c, dict)]
    running_message = "Applying the change."
    if action == "reconciling" and keeps_ready(obj, new_conds):
        # A re-run of a Ready object: the legacy fields keep saying ready as well (status.ready readers such as
        # parent and dependency checks see no flip). The job's message goes on the Reconciling condition.
        running_message = str(body.get("message") or "") or running_message
        if str(body.get("status") or "").lower() == "reconciling":
            body.pop("status")
        if body.get("ready") is False:
            body.pop("ready")
        body.pop("reason", None)
        body.pop("message", None)
    synth = synthesize_ready(dict(body, conditions=new_conds), old_conds)
    if synth is not None:
        new_conds.append(synth)
    if not partial:
        if action == "reconciling":
            new_conds.append({"type": "Reconciling", "status": "True", "reason": "JobRunning",
                              "message": running_message})
        elif find_condition(old_conds, "Reconciling") is not None or action in _JOB_STATUS:
            new_conds.append({"type": "Reconciling", "status": "False", "reason": "AsExpected", "message": ""})
    deleting = bool((obj.get("metadata") or {}).get("deletionTimestamp"))
    if not partial and action in ("ready", "success"):
        # a successful write ends an earlier give-up, also a teardown that succeeded after Stalled=TeardownFailed
        # (Retry now, a re-delivered event or a job started by hand): the controller then releases the finalizer
        if _cond_true(old_conds, "Stalled"):
            new_conds.append({"type": "Stalled", "status": "False", "reason": "AsExpected", "message": ""})
        if deleting and cur_status.get("retry"):
            body["retry"] = None
    if not partial and deleting and body.get("deletionComplete") is True:
        # a finished teardown: the object only waits for its finalizer; it is not ready (whatever the job wrote)
        new_conds = [c for c in new_conds if c.get("type") != "Ready"]
        new_conds.append({"type": "Ready", "status": "False", "reason": "Deleted",
                          "message": str(body.get("message") or "Deleted")})
        body["ready"] = False
    stalled = count_teardown_failure(obj, body, action, opts.get("give_up_attempts", DEFAULT_GIVE_UP_ATTEMPTS), now)
    if stalled is not None:
        new_conds.append(stalled)
    merged = merge_conditions(old_conds, new_conds, now)
    legacy_present = any(isinstance(c, dict) and c.get("type") in LEGACY_TYPES for c in old_conds)
    if new_conds or legacy_present or merged != old_conds:
        body["conditions"] = merged

    merged_status = _merge_patch(cur_status, body)
    fields = phase_fields(obj, merged_status, now)
    body.update(fields)
    info["phase"] = fields["phase"]
    patch = {"status": body}
    rv = (obj.get("metadata") or {}).get("resourceVersion")
    if rv:
        patch["metadata"] = {"resourceVersion": rv}
    return patch, info


def phase_backfill_patch(obj, now=None):
    """Patch that fixes stored phase/summary (and drops legacy types) without other changes, or None."""
    now = now or utcnow()
    status = (obj or {}).get("status") or {}
    conds = status.get("conditions") or []
    body = {}
    if any(isinstance(c, dict) and c.get("type") in LEGACY_TYPES for c in conds):
        body["conditions"] = merge_conditions(conds, [], now)
    merged_status = _merge_patch(status, body)
    fields = phase_fields(obj, merged_status, now)
    if status.get("phase") != fields["phase"] or status.get("summary") != fields["summary"]:
        body.update(fields)
    if not body:
        return None
    patch = {"status": body}
    rv = ((obj or {}).get("metadata") or {}).get("resourceVersion")
    if rv:
        patch["metadata"] = {"resourceVersion": rv}
    return patch

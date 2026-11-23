#!/usr/bin/env python3
"""Check of the automation platform's event activations, run as a CronJob outside the platform.

Modes:
  check (default)  list the activations through the event-automation API and bring back the watched ones (restart
                   policy `always`) that are enabled but not running: disable, wait until stopped, enable, wait
                   until running. Activations with another restart policy are not watched (listed once per run in
                   the log). Bounded: at most maxRepairsPerHour repairs per activation in any hour; past that it is
                   reported and left alone until it is seen running with no repair in the last hour. One repaired
                   more than maxRepairsPerDay times in a day is reported even though each repair worked.
                   Reported, never repaired: an activation that cannot run (its status message matches
                   cannotRunPatterns, or the platform refuses to enable it as invalid), the platform's workers
                   offline, a status this check does not know, two running activation Jobs for one activation (the
                   older one is removed only with removeStaleJobs), an empty list (unless allowNoActivations).

                   The repair count lives in a ConfigMap (stateConfigMap). Before anything is disabled or enabled,
                   the counted repair and the mark "disabled by this check" are written there; if that write fails,
                   nothing is touched. So whatever stops a run midway (an API error, a timeout, the pod killed), an
                   activation this check disabled is either enabled again by the same run or marked, and the next
                   run enables it. An activation disabled by anyone else is never enabled.

                   Exit 0: nothing needs a person and the run was complete. Exit 1: something was reported, a repair
                   did not complete, or a read or write the run needed failed (including the activations list).
  api              one authenticated read of the activations list. Exit 0 when it answers, 1 when it does not
                   answer, answers an error, or refuses the credential.

The decisions are pure functions (decide, plan_actions, job_findings, stale_jobs, prune_state); run_check does the
reads and writes they ask for. Standard library only (the job image's Python 3.9 or later). Only https is used,
redirects are not followed, and a next-page link must stay on the same origin.

Configuration: a JSON file (--config); see DEFAULTS. The credential comes from files in credentialDir: `token` (sent
as a bearer token), or `username` and `password` (basic authentication). It never appears in any output.

usage: eda_activations.py [--config FILE] [--mode check|api] [--dry-run]
"""
import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULTS = {
    "url": "",                      # the platform gateway's base URL (https)
    "apiPath": "/api/eda/v1",
    "credentialDir": "/etc/eda-credential",
    "caFile": "",                   # extra CA bundle for the gateway; empty = the image's trust store
    "requestTimeoutSeconds": 20,
    "automationNamespace": "",      # where the platform runs the activation Jobs
    "stateConfigMap": "eda-activations-state",
    "stateNamespace": "",           # the check's own namespace (the CronJob's)
    "maxRepairsPerHour": 3,
    "maxRepairsPerDay": 6,          # more repairs than this in a day: reported although each repair worked
    "notRunningMinutes": 4,         # not running this long (seen on an earlier run, or since a repair) before a repair
    "transitionalMinutes": 10,      # pending, starting or stopping this long counts as not running
    "stopTimeoutSeconds": 90,
    "startTimeoutSeconds": 180,
    "pollSeconds": 5,
    "cannotRunPatterns": ["pull failed", "container request is not valid"],
    "removeStaleJobs": False,
    "allowNoActivations": False,
    "kubeURL": "",                  # empty = in-cluster
    "kubeTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "kubeCAFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
}

HOUR = 3600
DAY = 86400
WATCHED_POLICY = "always"
RUNNING = "running"
TRANSITIONAL = {"pending", "starting", "stopping"}
NOT_RUNNING = {"failed", "stopped", "completed", "unresponsive", "error"}
# the platform refuses an enable (409) while the activation is in one of these
ENABLE_BLOCKED = {"starting", "stopping", "deleting", "running", "unresponsive"}
JOB_NAME = re.compile(r"^activation-job-(\d+)-(\d+)$")

# verdicts
OK = "Running"
DISABLED = "Disabled"
NOT_WATCHED = "NotWatched"
DELETING = "Deleting"
WAITING = "Waiting"
REPAIR = "Repair"
ENABLE = "EnableAgain"
CANNOT_RUN = "CannotRun"
LIMIT = "RepairLimitReached"
LEFT_DISABLED = "LeftDisabled"
WORKERS_OFFLINE = "WorkersOffline"
UNKNOWN = "UnknownStatus"
PROBLEMS = {CANNOT_RUN, LIMIT, LEFT_DISABLED, WORKERS_OFFLINE, UNKNOWN}

# values never to show (the credential); every line this check prints or reports passes redact()
SECRETS = set()


def redact(text):
    text = str(text)
    for s in SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class ApiError(Exception):
    """The API did not answer, or answered something other than what was asked for."""

    def __init__(self, what, status=None):
        super().__init__(redact(what))
        self.status = status


# ---- decisions (pure) -----------------------------------------------------------------------------------------

def latest_instance(detail):
    """The newest instance of an activation detail (highest id), or None."""
    instances = [i for i in (detail or {}).get("instances") or [] if isinstance(i, dict) and "id" in i]
    return max(instances, key=lambda i: int(i["id"])) if instances else None


def cannot_run(activation, instance, patterns):
    """The pattern an activation's or its latest instance's status message matches, or None."""
    texts = [str((activation or {}).get("status_message") or ""), str((instance or {}).get("status_message") or "")]
    for p in patterns or []:
        for t in texts:
            if p and p.lower() in t.lower():
                return p
    return None


def in_window(times, now, window):
    return [t for t in times or [] if now - t < window]


def watched(activation):
    return str(activation.get("restart_policy") or "") == WATCHED_POLICY


def decide(activation, instance, record, now, cfg):
    """One activation's verdict: (verdict, reason, new_record).

    activation: an item of the activations list; instance: its latest instance (or None); record: what the state
    holds for it ({repairs: [unix seconds, last day], notRunningSince, gaveUp, disabledByCheck}); now: unix seconds.
    """
    record = record or {}
    rec = {"name": activation.get("name"), "repairs": in_window(record.get("repairs"), now, DAY),
           "notRunningSince": record.get("notRunningSince"), "gaveUp": bool(record.get("gaveUp")),
           "disabledByCheck": bool(record.get("disabledByCheck"))}
    hour = in_window(rec["repairs"], now, HOUR)
    cap = cfg["maxRepairsPerHour"]
    status = str(activation.get("status") or "")
    if not activation.get("is_enabled"):
        if rec["disabledByCheck"]:
            # this check disabled it and its enable did not go through: never leave it disabled silently
            if rec["gaveUp"] or len(hour) >= cap:
                rec["gaveUp"] = True
                return LEFT_DISABLED, "left disabled by this check's repair and the platform did not take the " \
                                      "enable again; enable it after fixing the cause", rec
            return ENABLE, "left disabled by this check's earlier repair", rec
        # disabled by a person (or by something else): not ours
        rec.update(notRunningSince=None, gaveUp=False)
        return DISABLED, "disabled", rec
    rec["disabledByCheck"] = False
    if not watched(activation):
        rec.update(notRunningSince=None, gaveUp=False)
        return NOT_WATCHED, "restart policy %s" % (activation.get("restart_policy") or "unset"), rec
    if status == "deleting":
        return DELETING, "being deleted", rec
    if status == "workers offline":
        return WORKERS_OFFLINE, "the platform's activation workers are offline; nothing to repair here", rec
    inst_status = str((instance or {}).get("status") or "")
    if status == RUNNING and (instance is None or inst_status == RUNNING):
        rec["notRunningSince"] = None
        if rec["gaveUp"] and not hour:
            rec["gaveUp"] = False
        return OK, "running", rec
    if status != RUNNING and status not in TRANSITIONAL and status not in NOT_RUNNING:
        return UNKNOWN, "status %r is not one this check knows; not repaired" % status, rec
    if status == RUNNING:
        why = "status running but its latest instance %s is %s" % (instance.get("id"), inst_status or "unknown")
    else:
        why = "status %s" % status
        msg = str(activation.get("status_message") or "").strip()
        if msg:
            why += ": " + msg[:200]
    pattern = cannot_run(activation, instance, cfg["cannotRunPatterns"])
    if pattern:
        return CANNOT_RUN, "%s (matches %r: fix the cause, then enable it)" % (why, pattern), rec
    if rec["gaveUp"]:
        return LIMIT, "%s; the repairs did not bring it back, left alone until it runs" % why, rec
    if not rec["notRunningSince"]:
        rec["notRunningSince"] = now
    grace = cfg["transitionalMinutes"] if status in TRANSITIONAL else cfg["notRunningMinutes"]
    waited = (now - rec["notRunningSince"]) / 60
    if waited < grace:
        return WAITING, "%s; not running for %.0f min, repaired after %s min" % (why, waited, grace), rec
    if len(hour) >= cap:
        rec["gaveUp"] = True
        return LIMIT, "%s; %d repairs in the last hour did not bring it back, left alone until it runs" % (
            why, len(hour)), rec
    return REPAIR, why, rec


def still_due(verdict, fresh):
    """Whether an action decided on an earlier read still applies to the activation as it reads now."""
    if fresh is None:
        return False
    if verdict == ENABLE:
        return not fresh.get("is_enabled")
    return bool(fresh.get("is_enabled")) and watched(fresh) and fresh.get("status") not in (RUNNING, "deleting")


def mark_before_acting(rec, verdict, now):
    """The record written before the action: the repair counted, the grace restarted from now (a slow start is not
    pushed back again on the next run), and the activation marked as disabled by this check."""
    rec = dict(rec)
    rec["repairs"] = list(rec.get("repairs") or []) + [now]
    rec["notRunningSince"] = now
    rec["disabledByCheck"] = True
    return rec


def parse_jobs(jobs):
    """{activation id: [(instance id, job name, active pods)]} of the platform's activation Jobs."""
    out = {}
    for j in jobs or []:
        name = ((j or {}).get("metadata") or {}).get("name") or ""
        m = JOB_NAME.match(name)
        if not m:
            continue
        active = int(((j.get("status") or {}).get("active")) or 0)
        out.setdefault(int(m.group(1)), []).append((int(m.group(2)), name, active))
    return out


def job_findings(activation, jobs_of_activation):
    """A report line when one activation has more than one Job with a running pod, else None."""
    running = sorted(j for j in jobs_of_activation or [] if j[2] > 0)
    if len(running) < 2:
        return None
    return "%d activation Jobs with running pods: %s" % (len(running), ", ".join(j[1] for j in running))


def stale_jobs(activation, instance, jobs_of_activation):
    """Names of the activation Jobs that are safe to remove: the activation runs, its latest instance runs and has
    a running Job, and the Job belongs to an older instance of the same activation. Anything less certain: []."""
    if not activation.get("is_enabled") or activation.get("status") != RUNNING:
        return []
    if not instance or instance.get("status") != RUNNING:
        return []
    latest = int(instance["id"])
    running = [j for j in jobs_of_activation or [] if j[2] > 0]
    if not any(j[0] == latest for j in running):
        return []
    return [j[1] for j in sorted(running) if j[0] < latest]


def prune_state(state, activation_ids):
    """The state without activations that no longer exist (a record of an existing activation is always kept)."""
    keep = {str(i) for i in activation_ids}
    return {k: v for k, v in (state or {}).items() if k in keep}


def summarize(lines):
    """The termination message: one line per finding, at most 4000 bytes."""
    text = "\n".join(line for line in lines if line) or "every watched activation runs"
    return redact(text)[:4000]


# ---- I/O --------------------------------------------------------------------------------------------------------

def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as f:
            cfg.update(json.load(f) or {})
    return cfg


def read_credential(directory):
    def read(name):
        p = os.path.join(directory, name)
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip()
        return ""
    token = read("token")
    if token:
        SECRETS.add(token)
        return "Bearer " + token
    user, password = read("username"), read("password")
    if user and password:
        basic = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        SECRETS.update({password, basic})
        return "Basic " + basic
    raise SystemExit("the credential in %s has neither a token nor a username and password" % directory)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect is answered as an error, never followed with the credential


def make_transport(ca_file=None):
    """(request, timeout) -> (status, body). Redirects are not followed."""
    handlers = [_NoRedirect()]
    if ca_file is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file or None)))
    opener = urllib.request.build_opener(*handlers)

    def send(req, timeout):
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()
    return send


def origin(url):
    p = urllib.parse.urlsplit(url)
    return (p.scheme, p.netloc)


class Http:
    def __init__(self, base, auth, ca_file, timeout, transport=None):
        self.base = base.rstrip("/")
        if transport is None:
            if not self.base.startswith("https://"):
                raise SystemExit("the URL %s must use https" % self.base)
            transport = make_transport(ca_file or "")
        self.auth = auth
        self.timeout = timeout
        self.send = transport

    def url(self, path):
        if path.startswith("/"):
            return self.base + path
        if origin(path) != origin(self.base):
            raise ApiError("a link to another origin was refused: %s" % urllib.parse.urlsplit(path).netloc)
        return path

    def call(self, method, path, body=None, ok=(200,)):
        url = self.url(path)
        data = None
        headers = {"Accept": "application/json", "Authorization": self.auth}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        where = urllib.parse.urlsplit(url).path
        try:
            code, raw = self.send(req, self.timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError("%s %s: no answer (%s)" % (method, where, getattr(e, "reason", e)))
        if code not in ok:
            raise ApiError("%s %s: answered %d%s" % (method, where, code, error_text(raw)), status=code)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError("%s %s: answered %d with a body that is not JSON" % (method, where, code), status=code)


def error_text(raw):
    """A short reason from an error body: its errors/detail field, if it is JSON (redacted by ApiError)."""
    try:
        doc = json.loads(raw or b"")
    except ValueError:
        return ""
    if isinstance(doc, dict):
        for key in ("errors", "detail", "message", "reason"):
            if doc.get(key):
                return " (%s)" % str(doc[key])[:200]
    if isinstance(doc, str):
        return " (%s)" % doc[:200]
    return ""


class Eda:
    def __init__(self, http, api_path):
        self.http = http
        self.p = api_path.rstrip("/")

    def activations(self, page_size=200):
        out, path, seen = [], "%s/activations/?page_size=%d" % (self.p, page_size), set()
        while path:
            if path in seen:
                raise ApiError("GET %s: the pages of the activations list repeat" % path)
            seen.add(path)
            page = self.http.call("GET", path)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise ApiError("GET %s: the answer is not a list of activations" % path)
            out.extend(page["results"])
            path = page.get("next")
            if path and not path.startswith("http") and not path.startswith("/"):
                path = "%s/activations/%s" % (self.p, path)
        return out

    def detail(self, aid):
        return self.http.call("GET", "%s/activations/%d/" % (self.p, aid))

    def disable(self, aid):
        self.http.call("POST", "%s/activations/%d/disable/" % (self.p, aid), body={}, ok=(200, 202, 204))

    def enable(self, aid):
        self.http.call("POST", "%s/activations/%d/enable/" % (self.p, aid), body={}, ok=(200, 202, 204))


class Kube:
    def __init__(self, cfg, transport=None):
        url = cfg["kubeURL"]
        if not url:
            host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host:
                raise SystemExit("not in a cluster and kubeURL is not set")
            url = "https://%s:%s" % (host if ":" not in host else "[%s]" % host, port)
        token = ""
        if os.path.exists(cfg["kubeTokenFile"]):
            with open(cfg["kubeTokenFile"]) as f:
                token = f.read().strip()
        SECRETS.add(token)
        ca = cfg["kubeCAFile"] if os.path.exists(cfg["kubeCAFile"]) else ""
        self.http = Http(url, "Bearer " + token, ca, cfg["requestTimeoutSeconds"], transport)

    def jobs(self, ns):
        doc = self.http.call("GET", "/apis/batch/v1/namespaces/%s/jobs" % urllib.parse.quote(ns))
        return (doc or {}).get("items") or []

    def delete_job(self, ns, name):
        self.http.call("DELETE", "/apis/batch/v1/namespaces/%s/jobs/%s" % (ns, name),
                       body={"propagationPolicy": "Background"}, ok=(200, 202))

    def read_state(self, ns, name):
        """(state dict, resourceVersion) or ({}, None) when the ConfigMap does not exist yet."""
        try:
            cm = self.http.call("GET", "/api/v1/namespaces/%s/configmaps/%s" % (ns, name))
        except ApiError as e:
            if e.status == 404:
                return {}, None
            raise
        raw = ((cm or {}).get("data") or {}).get("state.json") or "{}"
        try:
            state = json.loads(raw)
        except ValueError:
            raise ApiError("the ConfigMap %s does not hold readable JSON" % name)
        if not isinstance(state, dict):
            raise ApiError("the ConfigMap %s does not hold a state" % name)
        return state, cm["metadata"].get("resourceVersion")

    def write_state(self, ns, name, state, version):
        """Writes the state against the version read (a conflicting write fails); returns the new version."""
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": ns,
                             "labels": {"app.kubernetes.io/name": "eda-activations"}},
                "data": {"state.json": json.dumps(state, sort_keys=True)}}
        if version is None:
            doc = self.http.call("POST", "/api/v1/namespaces/%s/configmaps" % ns, body=body, ok=(201,))
        else:
            body["metadata"]["resourceVersion"] = version
            doc = self.http.call("PUT", "/api/v1/namespaces/%s/configmaps/%s" % (ns, name), body=body, ok=(200,))
        return ((doc or {}).get("metadata") or {}).get("resourceVersion")


def wait_for(eda, ids, done, timeout, poll, clock, sleep):
    """Poll the activations list until done(status) holds for every id or the timeout passes: {id: last status}."""
    deadline = clock() + timeout
    while True:
        by_id = {int(a["id"]): a for a in eda.activations()}
        last = {i: str((by_id.get(i) or {}).get("status") or "absent") for i in ids}
        if all(done(s) for s in last.values()) or clock() >= deadline:
            return last
        sleep(poll)


def run_check(eda, kube, cfg, now, dry_run, clock=time.time, sleep=time.sleep, out=print):
    """One check. Returns (counts, report lines); a non-empty report means exit 1. Raises ApiError when the
    activations list itself does not answer (nothing was judged or touched then)."""
    counts = {"activations": 0, "enabled": 0, "running": 0, "waiting": 0, "notWatched": 0, "repaired": 0,
              "notRunningAfterRepair": 0, "reported": 0, "twoJobs": 0, "staleJobsRemoved": 0}
    report = []

    def say(line):
        out(redact(line))

    def problem(line):
        report.append(redact(line))

    items = eda.activations()
    counts["activations"] = len(items)
    if not items and not cfg["allowNoActivations"]:
        problem("the activations list is empty: the credential's account may not see them "
                "(allowNoActivations is off); nothing was judged or changed")
        return counts, report

    try:
        state, version = kube.read_state(cfg["stateNamespace"], cfg["stateConfigMap"])
        state_ok = True
    except ApiError as e:
        state, version, state_ok = {}, None, False
        problem("the repair count (ConfigMap %s) cannot be read, so nothing is repaired: %s"
                % (cfg["stateConfigMap"], e))

    try:
        jobs = parse_jobs(kube.jobs(cfg["automationNamespace"]))
    except ApiError as e:
        jobs = None
        problem("the activation Jobs in %s cannot be listed: %s" % (cfg["automationNamespace"], e))

    new_state, due, not_watched = {}, [], []
    refs = {}
    for a in items:
        aid, name = int(a["id"]), a.get("name") or str(a.get("id"))
        ref = refs[aid] = "activation %s (id %d)" % (name, aid)
        old = state.get(str(aid))
        if a.get("is_enabled"):
            counts["enabled"] += 1
        instance = None
        if a.get("is_enabled") and watched(a) and a.get("status") != "deleting":
            try:
                instance = latest_instance(eda.detail(aid))
            except ApiError as e:
                problem("%s: its instances cannot be read: %s" % (ref, e))
                if old is not None:
                    new_state[str(aid)] = old
                counts["reported"] += 1
                continue
        verdict, why, rec = decide(a, instance, old, now, cfg)
        new_state[str(aid)] = rec
        if verdict == OK:
            counts["running"] += 1
        elif verdict == NOT_WATCHED:
            counts["notWatched"] += 1
            not_watched.append(name)
        elif verdict == WAITING:
            counts["waiting"] += 1
            say("wait %s: %s" % (ref, why))
        elif verdict in (REPAIR, ENABLE):
            if state_ok:
                due.append((aid, verdict, why))
            else:
                counts["reported"] += 1
                problem("%s: %s (%s), not done: the repair count is unknown" % (ref, verdict, why))
        elif verdict in PROBLEMS:
            counts["reported"] += 1
            problem("%s: %s: %s" % (ref, verdict, why))
        if jobs is not None:
            two = job_findings(a, jobs.get(aid))
            if two:
                counts["twoJobs"] += 1
                stale = stale_jobs(a, instance, jobs.get(aid))
                removed = []
                if cfg["removeStaleJobs"] and stale and not dry_run:
                    for job in stale:
                        try:
                            kube.delete_job(cfg["automationNamespace"], job)
                            removed.append(job)
                        except ApiError as e:
                            problem("%s: the older Job %s could not be removed: %s" % (ref, job, e))
                if removed:
                    counts["staleJobsRemoved"] += len(removed)
                    say("removed %s: older activation Job(s) %s; the latest instance %s runs"
                        % (ref, ", ".join(removed), instance["id"]))
                else:
                    hint = (" (older: %s)" % ", ".join(stale)) if stale else ""
                    problem("%s: TwoJobs: %s%s" % (ref, two, hint))
    if not_watched:
        say("not watched (restart policy other than %s): %s" % (WATCHED_POLICY, ", ".join(sorted(not_watched))))

    ids = [a["id"] for a in items]
    if due and not dry_run:
        acted = act(eda, kube, cfg, now, due, refs, new_state, ids, version, counts, problem, say, clock, sleep)
        version = acted
    elif due:
        for aid, verdict, why in due:
            say("would %s %s: %s" % ("repair" if verdict == REPAIR else "enable", refs[aid], why))

    for key, rec in new_state.items():
        day = len(rec.get("repairs") or [])
        if day > cfg["maxRepairsPerDay"]:
            problem("%s: RepairedTooOften: %d repairs in the last day (more than %d); find why it stops"
                    % (refs.get(int(key), key), day, cfg["maxRepairsPerDay"]))

    if state_ok and not dry_run and version is not False:
        try:
            kube.write_state(cfg["stateNamespace"], cfg["stateConfigMap"], prune_state(new_state, ids), version)
        except ApiError as e:
            problem("the repair count could not be saved: %s" % e)
    return counts, report


def act(eda, kube, cfg, now, due, refs, new_state, ids, version, counts, problem, say, clock, sleep):
    """The repairs of one run. Returns the state's resourceVersion for the final write, or False when the final
    write must not happen (the record written before acting stands)."""
    # the activations as they are now (the list was read before up to one detail read per activation)
    try:
        fresh = {int(a["id"]): a for a in eda.activations()}
    except ApiError as e:
        problem("the activations could not be read again before the repairs, so nothing was repaired: %s" % e)
        return version
    todo = []
    for aid, verdict, why in due:
        if still_due(verdict, fresh.get(aid)):
            todo.append((aid, verdict, why))
        else:
            say("skip %s: it changed since it was read (%s)" % (refs[aid], (fresh.get(aid) or {}).get("status")))
    if not todo:
        return version
    for aid, verdict, why in todo:
        hour = len(in_window(new_state[str(aid)]["repairs"], now, HOUR))
        say("repair %s: %s (repair %d of at most %d this hour)" % (refs[aid], why, hour + 1, cfg["maxRepairsPerHour"]))
        new_state[str(aid)] = mark_before_acting(new_state[str(aid)], verdict, now)
    # durable before any change: the count, and the mark that tells the next run to enable it again
    try:
        version = kube.write_state(cfg["stateNamespace"], cfg["stateConfigMap"], prune_state(new_state, ids), version)
    except ApiError as e:
        problem("the repair count could not be saved before the repairs, so nothing was repaired: %s" % e)
        return False

    for aid, verdict, why in todo:
        if verdict == REPAIR:
            try:
                eda.disable(aid)
            except ApiError as e:
                problem("%s: the disable was refused (the enable is still tried): %s" % (refs[aid], e))
    stopped = {}
    try:
        stopped = wait_for(eda, [t[0] for t in todo], lambda s: s not in ENABLE_BLOCKED, cfg["stopTimeoutSeconds"],
                           cfg["pollSeconds"], clock, sleep)
    except ApiError as e:
        problem("waiting for the disabled activations to stop failed (the enables are still tried): %s" % e)
    enabled = []
    for aid, verdict, why in todo:
        rec = new_state[str(aid)]
        try:
            eda.enable(aid)
            enabled.append(aid)
            rec["disabledByCheck"] = False
        except ApiError as e:
            counts["notRunningAfterRepair"] += 1
            if e.status == 400:
                rec["gaveUp"] = True
                problem("%s: CannotRun: the platform refuses to enable it, so it stays disabled until a person "
                        "enables it: %s" % (refs[aid], e))
            else:
                problem("%s: NotRunningAfterRepair: the enable was refused (status %s), so it stays disabled "
                        "until the next run enables it: %s" % (refs[aid], stopped.get(aid, "unknown"), e))
    if enabled:
        try:
            started = wait_for(eda, enabled, lambda s: s == RUNNING, cfg["startTimeoutSeconds"],
                               cfg["pollSeconds"], clock, sleep)
        except ApiError as e:
            started = {}
            problem("waiting for the enabled activations to run failed: %s" % e)
        for aid in enabled:
            if started.get(aid) == RUNNING:
                counts["repaired"] += 1
                say("repaired %s: running again" % refs[aid])
            else:
                counts["notRunningAfterRepair"] += 1
                problem("%s: NotRunningAfterRepair: %s %d s after the enable" % (
                    refs[aid], started.get(aid, "unknown"), cfg["startTimeoutSeconds"]))
    return version


def write_termination(text):
    try:
        with open("/dev/termination-log", "w") as f:
            f.write(text)
    except OSError:
        pass


def main(argv=None, transport=None):
    """transport: for tests only (a plain-http fake); the real run uses https and the image's trust store."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/eda-activations/config.json")
    ap.add_argument("--mode", choices=["check", "api"], default="check")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    for key in ("url",) + (("automationNamespace", "stateNamespace") if args.mode == "check" else ()):
        if not cfg.get(key):
            raise SystemExit("setting %s is required" % key)
    http = Http(cfg["url"], read_credential(cfg["credentialDir"]), cfg["caFile"], cfg["requestTimeoutSeconds"],
                transport)
    eda = Eda(http, cfg["apiPath"])
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if args.mode == "api":
        try:
            http.call("GET", "%s/activations/?page_size=1" % eda.p)
        except ApiError as e:
            msg = redact("the event-automation API does not answer the check: %s" % e)
            print(msg)
            write_termination(msg)
            return 1
        print("the event-automation API answers")
        return 0

    try:
        counts, report = run_check(eda, Kube(cfg, transport), cfg, int(time.time()), args.dry_run)
    except ApiError as e:
        msg = redact("not checked: the activations list does not answer: %s" % e)
        print(msg)
        write_termination(msg)
        return 1
    print(json.dumps(dict({"time": stamp, "dryRun": args.dry_run}, **counts)))
    for line in report:
        print("PROBLEM", line)
    write_termination(summarize(report))
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main())

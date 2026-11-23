#!/usr/bin/env python3
"""Check of the automation platform's event activations, run as a CronJob outside the platform.

Modes:
  check (default)  list the activations through the event-automation API and restart the watched ones that do not
                   run. Watched: enabled, with restart policy `always`. A disabled activation is never touched and
                   never reported, whoever disabled it; one with another restart policy is named once per run in
                   the log. A watched activation is restarted (one call, POST .../restart/) when, for longer than
                   its grace, its status is not running, or its status is running while its newest instance is not
                   (the platform lost track of it). Pending, starting and stopping get the longer grace, counted
                   from when the check first saw it not running or last restarted it. Just before the call the
                   activation is read again; it is restarted only if it is still enabled and still due.

                   Bounded: at most maxRepairsPerHour restarts per activation in any hour (counted over an hour plus
                   runDeadlineSeconds, as a call can follow its count by up to the deadline); at the cap it is reported
                   and restarted again when the hour allows. More than maxRepairsPerDay in a day is reported
                   although each restart worked. Every restart is counted in a ConfigMap (stateConfigMap) before
                   it is made; if the count cannot be stored, nothing is restarted. A restart the platform refuses
                   as invalid or forbidden (400, 403) gives up on that activation until it runs again or is
                   disabled; it is reported each run. Reported, never restarted: an activation that cannot run (its
                   status message matches cannotRunPatterns), the platform's workers offline, a status this check
                   does not know, two running Jobs for one watched activation (the older one is removed only with
                   removeStaleJobs), an empty list (unless allowNoActivations).

                   A restart does not reset the platform's own failure budget: once that is used up, each further
                   failure of the activation waits for this check's next run.

                   Exit 0: nothing needs a person and the run was complete. Exit 1: something was reported, a
                   restart did not bring the activation back, another run holds the count, the stored count has the
                   wrong shape (nothing is restarted then), or a read or write the run needed failed (including the
                   activations list). A run cut by its deadline (a storm of restarts) continues on the next run; the
                   restarts it counted but did not reach stay counted.
  api              one authenticated read of the activations list. Exit 0 when it answers, 1 when it does not
                   answer, answers an error, or refuses the credential.

The only calls to the platform are GETs and POST .../restart/. The decisions are pure functions (decide, still_due,
job_findings, stale_jobs, prune_state, state_fault); run_check does the reads and writes they ask for. Standard
library only (the job image's Python 3.9 or later). https only, redirects not followed, a next-page link only on the same origin.

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
    "maxRepairsPerDay": 6,          # more restarts than this in a day: reported although each one worked
    "notRunningMinutes": 4,         # not running this long (seen on an earlier run, or since a restart)
    "transitionalMinutes": 10,      # pending, starting or stopping this long counts as not running
    "startTimeoutSeconds": 180,
    "runDeadlineSeconds": 0,        # the Job's deadline (the chart passes it): added to the hour of the cap
    "pollSeconds": 5,
    "cannotRunPatterns": ["pull failed", "container request is not valid"],
    "removeStaleJobs": False,
    "allowNoActivations": False,
    "kubeURL": "",                  # empty = in-cluster
    "kubeTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "kubeCAFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
}

HOUR = 3600


def cap_window(cfg):
    """The window of the hourly cap: an hour plus the Job's deadline. A restart is stamped when its count is written
    and made up to a deadline later in a slow run, so over this window no hour holds more than the cap of calls."""
    return HOUR + cfg["runDeadlineSeconds"]
DAY = 86400
WATCHED_POLICY = "always"
RUNNING = "running"
TRANSITIONAL = {"pending", "starting", "stopping"}
NOT_RUNNING = {"failed", "stopped", "completed", "unresponsive", "error"}
JOB_NAME = re.compile(r"^activation-job-(\d+)-(\d+)$")

# verdicts
OK = "Running"
DISABLED = "Disabled"
NOT_WATCHED = "NotWatched"
DELETING = "Deleting"
WAITING = "Waiting"
RESTART = "Restart"
CANNOT_RUN = "CannotRun"
LIMIT = "RepairLimitReached"
GAVE_UP = "RestartRefused"
WORKERS_OFFLINE = "WorkersOffline"
UNKNOWN = "UnknownStatus"
PROBLEMS = {CANNOT_RUN, LIMIT, GAVE_UP, WORKERS_OFFLINE, UNKNOWN}

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
    return bool(activation.get("is_enabled")) and str(activation.get("restart_policy") or "") == WATCHED_POLICY


def lost_track(activation, instance):
    """Status running while the newest instance is not running."""
    return activation.get("status") == RUNNING and instance is not None and instance.get("status") != RUNNING


def decide(activation, instance, record, now, cfg):
    """One activation's verdict: (verdict, reason, new_record).

    activation: an item of the activations list; instance: its newest instance (or None); record: what the state
    holds for it ({repairs: [unix seconds, last day], notRunningSince, gaveUp}); now: unix seconds.
    """
    record = record or {}
    rec = {"name": activation.get("name"), "repairs": in_window(record.get("repairs"), now, DAY),
           "notRunningSince": record.get("notRunningSince"), "gaveUp": bool(record.get("gaveUp"))}
    hour = in_window(rec["repairs"], now, cap_window(cfg))
    status = str(activation.get("status") or "")
    if not activation.get("is_enabled"):
        rec.update(notRunningSince=None, gaveUp=False)
        return DISABLED, "disabled", rec
    if not watched(activation):
        rec.update(notRunningSince=None, gaveUp=False)
        return NOT_WATCHED, "restart policy %s" % (activation.get("restart_policy") or "unset"), rec
    if status == "deleting":
        return DELETING, "being deleted", rec
    if status == "workers offline":
        return WORKERS_OFFLINE, "the platform's activation workers are offline; nothing to restart here", rec
    if status == RUNNING and not lost_track(activation, instance):
        rec["notRunningSince"] = None
        rec["gaveUp"] = False
        return OK, "running", rec
    if status != RUNNING and status not in TRANSITIONAL and status not in NOT_RUNNING:
        return UNKNOWN, "status %r is not one this check knows; not restarted" % status, rec
    if status == RUNNING:
        why = "status running but its newest instance %s is %s" % (instance.get("id"), instance.get("status"))
    else:
        why = "status %s" % status
        msg = str(activation.get("status_message") or "").strip()
        if msg:
            why += ": " + msg[:200]
    pattern = cannot_run(activation, instance, cfg["cannotRunPatterns"])
    if pattern:
        return CANNOT_RUN, "%s (matches %r: fix the cause; it runs again once restarted)" % (why, pattern), rec
    if rec["gaveUp"]:
        return GAVE_UP, "%s; the platform refused its restart, left alone until it runs or is disabled" % why, rec
    if not rec["notRunningSince"]:
        rec["notRunningSince"] = now
    grace = cfg["transitionalMinutes"] if status in TRANSITIONAL else cfg["notRunningMinutes"]
    waited = (now - rec["notRunningSince"]) / 60
    if waited < grace:
        return WAITING, "%s; not running for %.0f min, restarted after %s min" % (why, waited, grace), rec
    if len(hour) >= cfg["maxRepairsPerHour"]:
        return LIMIT, "%s; %d restarts in the last hour, restarted again when the hour allows" % (why, len(hour)), rec
    return RESTART, why, rec


def still_due(first, fresh, instance):
    """Whether an activation decided on the first read is still due to be restarted as it reads now."""
    if fresh is None or not watched(fresh):
        return False
    status = fresh.get("status")
    if status == RUNNING:
        return lost_track(fresh, instance)
    if status in NOT_RUNNING:
        return True
    if status in TRANSITIONAL:
        # the platform is working on one that was not transitional before: leave it to the platform
        return first.get("status") in TRANSITIONAL
    return False


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


def prune_state(state, activation_ids, now):
    """The state without activations that are not in the list, except those restarted within the last day: an
    activation missing from one read of the list keeps its count."""
    keep = {str(i) for i in activation_ids}
    return {k: v for k, v in (state or {}).items() if k in keep or in_window((v or {}).get("repairs"), now, DAY)}


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def state_fault(state):
    """What is wrong with the shape of a stored state, or None. A record is a map with optional keys repairs (a list
    of unix seconds), notRunningSince (unix seconds or null), gaveUp (true or false) and name."""
    for key, rec in state.items():
        if not isinstance(rec, dict):
            return "the record of activation %s is not a map" % key
        repairs = rec.get("repairs")
        if repairs is not None and (not isinstance(repairs, list) or not all(is_number(t) for t in repairs)):
            return "the record of activation %s has repairs that are not a list of times" % key
        since = rec.get("notRunningSince")
        if since is not None and not is_number(since):
            return "the record of activation %s has a notRunningSince that is not a time" % key
        if not isinstance(rec.get("gaveUp", False), bool):
            return "the record of activation %s has a gaveUp that is not true or false" % key
    return None


def summarize(lines):
    """The termination message: one line per finding, at most 4000 bytes."""
    text = "\n".join(line for line in lines if line) or "nothing needs a person (the counts line says what was seen)"
    return redact(text)[:4000]


# ---- I/O --------------------------------------------------------------------------------------------------------

WHOLE_NUMBERS = {"requestTimeoutSeconds": 1, "maxRepairsPerHour": 1, "maxRepairsPerDay": 1, "notRunningMinutes": 0,
                 "transitionalMinutes": 0, "startTimeoutSeconds": 1, "pollSeconds": 0, "runDeadlineSeconds": 0}
SWITCHES = ("removeStaleJobs", "allowNoActivations")


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as f:
            cfg.update(json.load(f) or {})
    for key, least in WHOLE_NUMBERS.items():
        v = cfg.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < least:
            raise SystemExit("setting %s must be a whole number of at least %d" % (key, least))
    for key in SWITCHES:
        if not isinstance(cfg.get(key), bool):
            raise SystemExit("setting %s must be true or false" % key)
    patterns = cfg.get("cannotRunPatterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise SystemExit("setting cannotRunPatterns must be a list of texts")
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
    """The event-automation API: reads, and the one action this check takes (restart)."""

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
        doc = self.http.call("GET", "%s/activations/%d/" % (self.p, aid))
        if not isinstance(doc, dict):
            raise ApiError("GET %s/activations/%d/: the answer is not an activation" % (self.p, aid))
        return doc

    def restart(self, aid):
        self.http.call("POST", "%s/activations/%d/restart/" % (self.p, aid), body={}, ok=(200, 202, 204))


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
        """Writes the state against the version read (409 when another run wrote since); returns the new version."""
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


class Conflict(Exception):
    """Another run wrote the count since this run read it."""


def store(kube, cfg, state, version):
    try:
        return kube.write_state(cfg["stateNamespace"], cfg["stateConfigMap"], state, version)
    except ApiError as e:
        if e.status == 409:
            raise Conflict()
        raise


def wait_running(eda, ids, cfg, clock, sleep):
    """Poll the activations list until every id runs or the timeout passes: {id: last status}."""
    deadline = clock() + cfg["startTimeoutSeconds"]
    while True:
        by_id = {int(a["id"]): a for a in eda.activations()}
        last = {i: str((by_id.get(i) or {}).get("status") or "absent") for i in ids}
        if all(s == RUNNING for s in last.values()) or clock() >= deadline:
            return last
        sleep(cfg["pollSeconds"])


def run_check(eda, kube, cfg, now, dry_run, clock=time.time, sleep=time.sleep, out=print):
    """One check. Returns (counts, report lines); a non-empty report means exit 1. Raises ApiError only when the
    activations list itself does not answer (nothing was judged or touched then)."""
    counts = {"activations": 0, "enabled": 0, "running": 0, "waiting": 0, "notWatched": 0, "restarted": 0,
              "notRunningAfterRestart": 0, "reported": 0, "twoJobs": 0, "staleJobsRemoved": 0}
    report = []

    def say(line):
        out(redact(line))

    def problem(line):
        report.append(redact(line))

    # the count first: a run that started after another one wrote it sees that write
    try:
        state, version = kube.read_state(cfg["stateNamespace"], cfg["stateConfigMap"])
        fault = state_fault(state)
        if fault:
            raise ApiError("the ConfigMap %s holds a state of the wrong shape: %s" % (cfg["stateConfigMap"], fault))
        state_ok = True
    except ApiError as e:
        state, version, state_ok = {}, None, False
        problem("the restart count (ConfigMap %s) cannot be read, so nothing is restarted: %s"
                % (cfg["stateConfigMap"], e))

    items = eda.activations()
    counts["activations"] = len(items)
    if not items:
        if not cfg["allowNoActivations"]:
            problem("the activations list is empty: the credential's account may not see them "
                    "(allowNoActivations is off); nothing was judged or changed")
        return counts, report   # an empty list never prunes the counts

    try:
        jobs = parse_jobs(kube.jobs(cfg["automationNamespace"]))
    except ApiError as e:
        jobs = None
        problem("the activation Jobs in %s cannot be listed: %s" % (cfg["automationNamespace"], e))

    new_state, due, not_watched, refs, watched_ids = {}, [], [], {}, set()
    for a in items:
        aid, name = int(a["id"]), a.get("name") or str(a.get("id"))
        if aid in refs:
            say("skip activation %s (id %d): listed twice in one read of the list; judged once" % (name, aid))
            continue
        ref = refs[aid] = "activation %s (id %d)" % (name, aid)
        if watched(a):
            watched_ids.add(aid)
        old = state.get(str(aid))
        if a.get("is_enabled"):
            counts["enabled"] += 1
        instance = None
        if watched(a) and a.get("status") != "deleting":
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
        elif verdict == RESTART:
            if state_ok:
                due.append((aid, a, why))
            else:
                counts["reported"] += 1
                problem("%s: not running (%s), not restarted: the restart count is unknown" % (ref, why))
        elif verdict in PROBLEMS:
            counts["reported"] += 1
            problem("%s: %s: %s" % (ref, verdict, why))
        if jobs is not None and watched(a):
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
    try:
        if due and not dry_run:
            version = restart_due(eda, kube, cfg, now, due, refs, state, new_state, ids, version, counts, problem, say,
                                  clock, sleep)
        elif due:
            for aid, a, why in due:
                say("would restart %s: %s" % (refs[aid], why))
        for key, rec in new_state.items():
            if int(key) not in watched_ids:
                continue   # a disabled or unwatched activation is never reported
            day = len(rec.get("repairs") or [])
            if day > cfg["maxRepairsPerDay"]:
                problem("%s: RepairedTooOften: %d restarts in the last day (more than %d); find why it stops"
                        % (refs.get(int(key), key), day, cfg["maxRepairsPerDay"]))
        if state_ok and not dry_run and version is not False:
            try:
                store(kube, cfg, prune_state(dict(state, **new_state), ids, now), version)
            except ApiError as e:
                problem("the restart count could not be saved: %s" % e)
    except Conflict:
        problem("another run of this check wrote the restart count meanwhile; this run stopped without acting further")
    return counts, report


def restart_due(eda, kube, cfg, now, due, refs, state, new_state, ids, version, counts, problem, say, clock, sleep):
    """The restarts of one run. Returns the state's resourceVersion for the final write, or False when the final
    write must not happen (nothing was restarted because the count could not be stored)."""
    # each restart is stamped with the time of the write that counts it, so a slow run does not date its restarts
    # back to its start
    stamp = max(now, int(clock()))
    planned = dict(new_state)
    for aid, a, why in due:
        rec = dict(new_state[str(aid)])
        rec["repairs"] = list(rec.get("repairs") or []) + [stamp]
        rec["notRunningSince"] = stamp
        planned[str(aid)] = rec
    say("restarts due in this run: %d (%s)" % (len(due), ", ".join(refs[aid] for aid, _, _ in due)))
    # every restart of the batch counted durably before the first call
    try:
        version = store(kube, cfg, prune_state(dict(state, **planned), ids, stamp), version)
    except ApiError as e:
        problem("the restart count could not be saved, so nothing was restarted: %s" % e)
        return False
    restarted = []
    for aid, a, why in due:
        ref = refs[aid]
        try:
            fresh = eda.detail(aid)
            fresh_instance = latest_instance(fresh)
        except ApiError as e:
            problem("%s: it could not be read again before its restart, not restarted: %s" % (ref, e))
            continue   # not made: the final write takes its count back (if that write fails, it stays over)
        if not still_due(a, fresh, fresh_instance):
            say("skip %s: it changed since it was read (%s, %s)" % (
                ref, "enabled" if fresh.get("is_enabled") else "disabled", fresh.get("status")))
            continue   # not made: the final write takes its count back
        new_state[str(aid)] = planned[str(aid)]
        hour = len(in_window(planned[str(aid)]["repairs"], stamp, cap_window(cfg)))
        say("restart %s: %s (restart %d of at most %d this hour)" % (ref, why, hour, cfg["maxRepairsPerHour"]))
        try:
            eda.restart(aid)
            restarted.append(aid)
        except ApiError as e:
            if e.status in (400, 403):
                new_state[str(aid)]["gaveUp"] = True
                problem("%s: RestartRefused: the platform refuses to restart it (invalid, disabled, or not allowed "
                        "to this account); left alone until it runs or is disabled: %s" % (ref, e))
            else:
                problem("%s: the restart failed (counted as an attempt): %s" % (ref, e))
    if restarted:
        try:
            started = wait_running(eda, restarted, cfg, clock, sleep)
        except ApiError as e:
            started = {}
            problem("waiting for the restarted activations to run failed: %s" % e)
        for aid in restarted:
            if started.get(aid) == RUNNING:
                counts["restarted"] += 1
                new_state[str(aid)]["notRunningSince"] = None
                say("restarted %s: running again" % refs[aid])
            else:
                counts["notRunningAfterRestart"] += 1
                problem("%s: NotRunningAfterRestart: %s %d s after the restart" % (
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

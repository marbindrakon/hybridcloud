#!/usr/bin/env python3
"""Check of the automation platform's event activations, run as a CronJob outside the platform.

Modes:
  check (default)  list the activations through the event-automation API and bring back the ones that are enabled
                   but not running: disable, wait until stopped, enable, wait until running. Bounded: at most
                   maxRepairsPerHour repairs per activation within one hour; past that the activation is reported
                   and left alone until it is seen running again with no repair in the last hour. Reported, never
                   repaired: an activation that cannot run (its status message matches cannotRunPatterns, or the
                   platform refuses to enable it as invalid), the platform's workers offline, a status this check
                   does not know, two running activation Jobs for one activation (the older one is removed only
                   with removeStaleJobs). The repair count is kept in a ConfigMap (stateConfigMap) so it survives
                   between runs; when that ConfigMap cannot be read, nothing is repaired.
                   Exit 0: every enabled activation runs (or waits within its grace time), or the API did not
                   answer (that is the api mode's report, so it is not reported twice). Exit 1: something above
                   was reported.
  api              one authenticated read of the activations list. Exit 0 when it answers, 1 when it does not
                   answer, answers an error, or refuses the credential.

Every decision is a pure function (decide, job_findings, stale_jobs, prune_state); main() only does the reads and
writes they ask for. Standard library only (the job image's Python 3.9 or later).

Configuration (YAML-free: a JSON file, --config): see DEFAULTS. The credential comes from files in credentialDir:
`token` (sent as a bearer token), or `username` and `password` (basic authentication). It is never logged.

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
    "url": "",                      # the platform gateway's base URL
    "apiPath": "/api/eda/v1",
    "credentialDir": "/etc/eda-credential",
    "caFile": "",                   # extra CA bundle for the gateway; empty = the image's trust store
    "requestTimeoutSeconds": 20,
    "automationNamespace": "",      # where the platform runs the activation Jobs
    "stateConfigMap": "eda-activations-state",
    "stateNamespace": "",           # the check's own namespace (the CronJob's)
    "maxRepairsPerHour": 3,
    "notRunningMinutes": 4,         # not running this long (seen on an earlier run) before a repair
    "transitionalMinutes": 10,      # pending, starting or stopping this long counts as not running
    "stopTimeoutSeconds": 90,
    "startTimeoutSeconds": 180,
    "pollSeconds": 5,
    "cannotRunPatterns": ["pull failed", "container request is not valid"],
    "removeStaleJobs": False,
    "kubeURL": "",                  # empty = in-cluster
    "kubeTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "kubeCAFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
}

HOUR = 3600
RUNNING = "running"
TRANSITIONAL = {"pending", "starting", "stopping"}
NOT_RUNNING = {"failed", "stopped", "completed", "unresponsive", "error"}
# the platform refuses an enable (409) while the activation is in one of these
ENABLE_BLOCKED = {"starting", "stopping", "deleting", "running", "unresponsive"}
JOB_NAME = re.compile(r"^activation-job-(\d+)-(\d+)$")

# verdicts
OK = "Running"
DISABLED = "Disabled"
DELETING = "Deleting"
WAITING = "Waiting"
REPAIR = "Repair"
ENABLE = "EnableAgain"
CANNOT_RUN = "CannotRun"
LIMIT = "RepairLimitReached"
WORKERS_OFFLINE = "WorkersOffline"
UNKNOWN = "UnknownStatus"
PROBLEMS = {CANNOT_RUN, LIMIT, WORKERS_OFFLINE, UNKNOWN}


class ApiError(Exception):
    """The API did not answer, or answered something other than what was asked for."""

    def __init__(self, what, status=None):
        super().__init__(what)
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


def repairs_in_window(record, now):
    return [t for t in (record or {}).get("repairs") or [] if now - t < HOUR]


def decide(activation, instance, record, now, cfg):
    """One activation's verdict: (verdict, reason, new_record).

    activation: an item of the activations list; instance: its latest instance (or None); record: what the state
    holds for it ({repairs: [unix seconds], notRunningSince, gaveUp}); now: unix seconds.
    """
    rec = {"name": activation.get("name"), "repairs": repairs_in_window(record, now),
           "notRunningSince": (record or {}).get("notRunningSince"), "gaveUp": bool((record or {}).get("gaveUp")),
           "disabledByCheck": bool((record or {}).get("disabledByCheck"))}
    status = str(activation.get("status") or "")
    if not activation.get("is_enabled"):
        if rec["disabledByCheck"]:
            # this check disabled it and its enable did not go through: never leave it disabled silently
            if len(rec["repairs"]) >= cfg["maxRepairsPerHour"]:
                rec["gaveUp"] = True
                return LIMIT, "left disabled by this check's repair; the platform did not take the enable " \
                              "again, enable it after fixing the cause", rec
            return ENABLE, "left disabled by this check's earlier repair", rec
        rec.update(notRunningSince=None, gaveUp=False)
        return DISABLED, "disabled", rec
    rec["disabledByCheck"] = False
    if status == "deleting":
        return DELETING, "being deleted", rec
    if status == "workers offline":
        return WORKERS_OFFLINE, "the platform's activation workers are offline; nothing to repair here", rec
    inst_status = str((instance or {}).get("status") or "")
    if status == RUNNING and (instance is None or inst_status == RUNNING):
        rec["notRunningSince"] = None
        if rec["gaveUp"] and not rec["repairs"]:
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
        return LIMIT, "%s; %d repairs in the last hour did not bring it back, left alone until it runs" % (
            why, cfg["maxRepairsPerHour"]), rec
    if not rec["notRunningSince"]:
        rec["notRunningSince"] = now
    grace = cfg["transitionalMinutes"] if status in TRANSITIONAL else cfg["notRunningMinutes"]
    waited = (now - rec["notRunningSince"]) / 60
    if waited < grace:
        return WAITING, "%s; seen %.0f min ago, repaired after %s min" % (why, waited, grace), rec
    if len(rec["repairs"]) >= cfg["maxRepairsPerHour"]:
        rec["gaveUp"] = True
        return LIMIT, "%s; %d repairs in the last hour did not bring it back, left alone until it runs" % (
            why, len(rec["repairs"])), rec
    return REPAIR, why, rec


def record_repair(rec, now):
    """The record after a repair attempt (counted whether it worked or not)."""
    rec = dict(rec)
    rec["repairs"] = list(rec.get("repairs") or []) + [now]
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
    """The state without activations that no longer exist."""
    keep = {str(i) for i in activation_ids}
    return {k: v for k, v in (state or {}).items() if k in keep}


def summarize(results):
    """The termination message: one line per reported activation, at most 4000 bytes."""
    lines = [line for line in results if line]
    text = "\n".join(lines) or "every enabled activation runs"
    return text[:4000]


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
        return "Bearer " + token
    user, password = read("username"), read("password")
    if user and password:
        return "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    raise SystemExit("the credential in %s has neither a token nor a username and password" % directory)


class Http:
    def __init__(self, base, auth, ca_file, timeout):
        self.base = base.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=ca_file or None) if self.base.startswith("https") else None

    def call(self, method, path, body=None, ok=(200,), content_type="application/json"):
        url = path if path.startswith("http") else self.base + path
        data = None
        headers = {"Accept": "application/json", "Authorization": self.auth}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
            e.close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError("%s %s: no answer (%s)" % (method, path, getattr(e, "reason", e)))
        if code not in ok:
            raise ApiError("%s %s: answered %d%s" % (method, path, code, error_text(raw)), status=code)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError("%s %s: answered %d with a body that is not JSON" % (method, path, code), status=code)


def error_text(raw):
    """A short, credential-free reason from an error body: its errors/detail field, if it is JSON."""
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
        out, path = [], "%s/activations/?page_size=%d" % (self.p, page_size)
        while path:
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
    def __init__(self, cfg):
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
        ca = cfg["kubeCAFile"] if os.path.exists(cfg["kubeCAFile"]) else None
        self.http = Http(url, "Bearer " + token, ca, cfg["requestTimeoutSeconds"])

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
            state = {}
        return (state if isinstance(state, dict) else {}), cm["metadata"].get("resourceVersion")

    def write_state(self, ns, name, state, version):
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": ns,
                             "labels": {"app.kubernetes.io/name": "eda-activations"}},
                "data": {"state.json": json.dumps(state, sort_keys=True)}}
        if version is None:
            self.http.call("POST", "/api/v1/namespaces/%s/configmaps" % ns, body=body, ok=(201,))
        else:
            body["metadata"]["resourceVersion"] = version
            self.http.call("PUT", "/api/v1/namespaces/%s/configmaps/%s" % (ns, name), body=body, ok=(200,))


def wait_for(eda, ids, done, timeout, poll, clock, sleep):
    """Poll the activations list until done(status) holds for every id or the timeout passes: {id: last status}."""
    last = {}
    deadline = clock() + timeout
    while True:
        by_id = {int(a["id"]): a for a in eda.activations()}
        last = {i: str((by_id.get(i) or {}).get("status") or "absent") for i in ids}
        if all(done(s) for s in last.values()) or clock() >= deadline:
            return last
        sleep(poll)


def run_check(eda, kube, cfg, now, dry_run, clock=time.time, sleep=time.sleep, out=print):
    """One check. Returns (counts, report lines). Raises ApiError only when the activations list does not answer."""
    counts = {"activations": 0, "enabled": 0, "running": 0, "waiting": 0, "repaired": 0, "notRunningAfterRepair": 0,
              "reported": 0, "twoJobs": 0, "staleJobsRemoved": 0}
    report = []
    items = eda.activations()
    counts["activations"] = len(items)

    try:
        state, version = kube.read_state(cfg["stateNamespace"], cfg["stateConfigMap"])
        state_ok = True
    except ApiError as e:
        state, version, state_ok = {}, None, False
        report.append("the repair count (ConfigMap %s) cannot be read, so nothing is repaired: %s"
                      % (cfg["stateConfigMap"], e))

    try:
        jobs = parse_jobs(kube.jobs(cfg["automationNamespace"]))
    except ApiError as e:
        jobs = None
        report.append("the activation Jobs in %s cannot be listed: %s" % (cfg["automationNamespace"], e))

    new_state, to_repair, instances = {}, [], {}
    for a in items:
        aid, name = int(a["id"]), a.get("name") or str(a.get("id"))
        ref = "activation %s (id %d)" % (name, aid)
        if a.get("is_enabled"):
            counts["enabled"] += 1
        instance = None
        if a.get("is_enabled") and a.get("status") not in ("deleting",):
            try:
                instance = latest_instance(eda.detail(aid))
            except ApiError as e:
                report.append("%s: its instances cannot be read: %s" % (ref, e))
                new_state[str(aid)] = state.get(str(aid), {})
                counts["reported"] += 1
                continue
        instances[aid] = instance
        verdict, why, rec = decide(a, instance, state.get(str(aid)), now, cfg)
        new_state[str(aid)] = rec
        if verdict == OK:
            counts["running"] += 1
        elif verdict == WAITING:
            counts["waiting"] += 1
            out("wait %s: %s" % (ref, why))
        elif verdict in (REPAIR, ENABLE):
            if state_ok:
                to_repair.append((aid, ref, why, verdict == REPAIR))
            else:
                report.append("%s: not running (%s), not repaired: the repair count is unknown" % (ref, why))
                counts["reported"] += 1
        elif verdict in PROBLEMS:
            counts["reported"] += 1
            report.append("%s: %s: %s" % (ref, verdict, why))
        if jobs is not None:
            two = job_findings(a, jobs.get(aid))
            if two:
                counts["twoJobs"] += 1
                stale = stale_jobs(a, instance, jobs.get(aid))
                if cfg["removeStaleJobs"] and stale and not dry_run:
                    removed = []
                    for job in stale:
                        try:
                            kube.delete_job(cfg["automationNamespace"], job)
                            removed.append(job)
                        except ApiError as e:
                            report.append("%s: the older Job %s could not be removed: %s" % (ref, job, e))
                    if removed:
                        counts["staleJobsRemoved"] += len(removed)
                        out("removed %s: older activation Job(s) %s; the latest instance %s runs"
                            % (ref, ", ".join(removed), instance["id"]))
                        continue
                hint = (" (older: %s)" % ", ".join(stale)) if stale else ""
                report.append("%s: TwoJobs: %s%s" % (ref, two, hint))

    if to_repair:
        for aid, ref, why, _ in to_repair:
            out("repair %s: %s (repair %d of at most %d this hour)" % (
                ref, why, len(new_state[str(aid)]["repairs"]) + 1, cfg["maxRepairsPerHour"]))
            new_state[str(aid)] = record_repair(new_state[str(aid)], now)
        if not dry_run:
            # all disables first, then all enables: a whole platform's activations come back in one wait
            ready = []
            for aid, ref, why, need_disable in to_repair:
                if not need_disable:
                    ready.append(aid)
                    continue
                try:
                    eda.disable(aid)
                    ready.append(aid)
                    new_state[str(aid)]["disabledByCheck"] = True
                except ApiError as e:
                    counts["notRunningAfterRepair"] += 1
                    report.append("%s: NotRunningAfterRepair: the disable was refused: %s" % (ref, e))
            stopped = wait_for(eda, ready, lambda s: s not in ENABLE_BLOCKED, cfg["stopTimeoutSeconds"],
                               cfg["pollSeconds"], clock, sleep) if ready else {}
            enabled = []
            for aid, ref, why, _ in to_repair:
                if aid not in ready:
                    continue
                try:
                    eda.enable(aid)
                    enabled.append(aid)
                    new_state[str(aid)]["disabledByCheck"] = False
                except ApiError as e:
                    counts["notRunningAfterRepair"] += 1
                    new_state[str(aid)]["disabledByCheck"] = True
                    if e.status == 400:
                        report.append("%s: CannotRun: the platform refuses to enable it, so it stays disabled: %s"
                                      % (ref, e))
                    else:
                        report.append("%s: NotRunningAfterRepair: the enable was refused (status %s), so it stays "
                                      "disabled until the next run: %s" % (ref, stopped.get(aid), e))
            started = wait_for(eda, enabled, lambda s: s == RUNNING, cfg["startTimeoutSeconds"],
                               cfg["pollSeconds"], clock, sleep) if enabled else {}
            for aid, ref, why, _ in to_repair:
                if aid not in enabled:
                    continue
                if started.get(aid) == RUNNING:
                    counts["repaired"] += 1
                    new_state[str(aid)]["notRunningSince"] = None
                    out("repaired %s: running again" % ref)
                else:
                    counts["notRunningAfterRepair"] += 1
                    report.append("%s: NotRunningAfterRepair: %s %d s after the enable" % (
                        ref, started.get(aid), cfg["startTimeoutSeconds"]))

    if state_ok and not dry_run:
        try:
            kube.write_state(cfg["stateNamespace"], cfg["stateConfigMap"],
                             prune_state(new_state, [a["id"] for a in items]), version)
        except ApiError as e:
            report.append("the repair count could not be saved (the next run cannot hold the limit): %s" % e)
    return counts, report


def write_termination(text):
    try:
        with open("/dev/termination-log", "w") as f:
            f.write(text)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/eda-activations/config.json")
    ap.add_argument("--mode", choices=["check", "api"], default="check")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    for key in ("url",) + (("automationNamespace", "stateNamespace") if args.mode == "check" else ()):
        if not cfg.get(key):
            raise SystemExit("setting %s is required" % key)
    http = Http(cfg["url"], read_credential(cfg["credentialDir"]), cfg["caFile"], cfg["requestTimeoutSeconds"])
    eda = Eda(http, cfg["apiPath"])
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if args.mode == "api":
        try:
            http.call("GET", "%s/activations/?page_size=1" % eda.p)
        except ApiError as e:
            msg = "the event-automation API does not answer the check: %s" % e
            print(msg)
            write_termination(msg)
            return 1
        print("the event-automation API answers")
        return 0

    try:
        counts, report = run_check(eda, Kube(cfg), cfg, int(time.time()), args.dry_run)
    except ApiError as e:
        # the api mode reports this; the activations are not judged in this run
        print("not checked: the activations list does not answer: %s" % e)
        return 0
    print(json.dumps(dict({"time": stamp, "dryRun": args.dry_run}, **counts)))
    for line in report:
        print("PROBLEM", line)
    write_termination(summarize(report))
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main())

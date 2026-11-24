#!/usr/bin/env python3
"""Writes this cluster's platform faults for the admin dashboard's banner, run as a CronJob of the operations layer.

Every run:
  1. reads the alerts of the cluster's platform monitoring from its Alertmanager (GET /api/v2/alerts, with this job's
     own service account, which holds the role monitoring-alertmanager-view in openshift-monitoring). Kept: the
     alerts whose name starts with alertPrefix (Shc) and that Alertmanager neither silences nor inhibits (a
     silence is the installation's way to say a fault is known);
  2. for each kept alert that belongs to a check CronJob (labels namespace = opsNamespace and cronjob matching
     checkCronJobs), adds the findings of that check's last failed run: the termination message of the newest failed
     Job's pod, one line per finding, when no run of the check succeeded after it;
  3. writes its own key <cluster>.json in the ConfigMap targetConfigMap of targetNamespace on the services cluster
     (a JSON merge patch of that one key; the ConfigMap is created when absent). Other clusters' keys are never read
     or written.

The key: {"schemaVersion": 1, "cluster", "writtenAt", "intervalSeconds", "faults": [{"id", "severity", "since",
"object", "summary", "details": [...]}], "alertsDelivered"}. Faults are ordered critical, warning, others, then by
since. alertsDelivered is true or false (OpenShift's AlertmanagerReceiversNotConfigured not firing, or firing) when
receiverCheck is on, else null. Only an alert's summary and the check's finding lines are written: each line is cut
to maxLineChars, web addresses and IP addresses are replaced, a crash report is replaced by one sentence, and the key
is kept under maxKeyBytes (details are shortened first, then the lowest faults left out).

The services cluster: on it (targetMode "local") the job writes with its own service account; on another cluster
(targetMode "remote") the API address, a bearer token and optionally a CA bundle come from files in targetDir, one per
key of the Secret the installation provides (targetServerKey, targetTokenKey, targetCAKey). A missing file fails the
run with the setting's name.

Exit 0: the key was written and the findings were read. Exit 1: the alerts could not be read or the key could not be
written (nothing is written then: a writer that could not look never reports "no faults"), or a check's findings
could not be read (written, with a line that says so). Standard library only (the job image's Python 3.9 or later).
No token appears in any output.

usage: platform_health_report.py [--config FILE]
"""
import argparse
import calendar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
DEFAULTS = {
    "cluster": "",
    "intervalSeconds": 300,
    "alertmanagerURL": "https://alertmanager-main.openshift-monitoring.svc:9094",
    "alertmanagerCAFile": SA_DIR + "/service-ca.crt",
    "alertPrefix": "Shc",
    "receiverAlert": "AlertmanagerReceiversNotConfigured",
    "receiverCheck": False,
    "opsNamespace": "",
    "checkCronJobs": "",            # a regular expression of the check CronJobs' names (whole name)
    "targetMode": "",               # local | remote
    "targetNamespace": "",
    "targetConfigMap": "shc-platform-health",
    "targetDir": "/etc/services-cluster",
    "targetServerKey": "",
    "targetTokenKey": "",
    "targetCAKey": "",
    "targetSecretSetting": "ops.platformHealth.report.servicesCluster.secret",
    "kubeURL": "",                  # empty = in-cluster
    "kubeTokenFile": SA_DIR + "/token",
    "kubeCAFile": SA_DIR + "/ca.crt",
    "requestTimeoutSeconds": 20,
    "maxLineChars": 300,
    "maxDetails": 20,
    "maxKeyBytes": 60000,
}
SCHEMA_VERSION = 1
SEVERITY_ORDER = {"critical": 0, "warning": 1}
# the label that names the object of an alert, in this order (the platform's rules name their object this way)
OBJECT_LABELS = ("cronjob", "persistentvolumeclaim", "postgrescluster", "statefulset", "deployment", "pod", "name")
CRASHED = "the check ended without reporting its findings; its Job's log says why"
UNREADABLE = "the check's findings could not be read: %s"
SECRETS = set()
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>)]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
IPV6_RE = re.compile(r"\[?(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?:/\d+)?\]?(?::\d+)?")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def redact(text):
    text = str(text)
    for s in SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class ApiError(Exception):
    def __init__(self, what, status=None):
        super().__init__(redact(what))
        self.status = status


# ---- pure parts -------------------------------------------------------------------------------------------------

def clean_line(text, limit):
    """One line fit for the object: no secret, no web or IP address, no control character, at most limit chars."""
    text = redact(text)
    text = URL_RE.sub("[address]", text)
    # an IPv6 address has "::" or seven colons (a time such as 12:30:45 has neither)
    text = IPV6_RE.sub(lambda m: "[address]" if "::" in m.group(0) or m.group(0).strip("[]").count(":") >= 7
                       else m.group(0), text)
    text = IPV4_RE.sub("[address]", text)
    text = CONTROL_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:max(limit - 1, 0)].rstrip() + "…"
    return text


def finding_lines(message, cfg):
    """The lines of a check's termination message, cleaned and bounded. A crash (a traceback that the pod's log
    fallback put there) is replaced by one sentence: raw log text never reaches the object."""
    if not message or not message.strip():
        return []
    if "Traceback (most recent call last)" in message:
        return [CRASHED]
    lines = [clean_line(x, cfg["maxLineChars"]) for x in message.splitlines()]
    lines = [x for x in lines if x]
    if len(lines) > cfg["maxDetails"]:
        more = len(lines) - (cfg["maxDetails"] - 1)
        lines = lines[:cfg["maxDetails"] - 1] + ["and %d more findings" % more]
    return lines


def fault_object(labels):
    """namespace/<the alert's object>, the object alone, or the namespace alone; "" when the alert names none."""
    ns = labels.get("namespace", "")
    for key in OBJECT_LABELS:
        if labels.get(key):
            return "%s/%s" % (ns, labels[key]) if ns else labels[key]
    return ns


def timestamp(value):
    """An RFC 3339 time as written by Alertmanager, to seconds and Z; "" when it is not one."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$", str(value or ""))
    if not m:
        return ""
    if m.group(2) == "Z":
        return m.group(1) + "Z"
    t = time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    sign = 1 if m.group(2)[0] == "+" else -1
    hh, mm = int(m.group(2)[1:3]), int(m.group(2)[4:6])
    secs = calendar.timegm(t) - sign * (hh * 3600 + mm * 60)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


def suppressed(alert):
    status = alert.get("status") or {}
    return status.get("state") == "suppressed" or bool(status.get("silencedBy")) or bool(status.get("inhibitedBy"))


def check_of(labels, cfg):
    """The check CronJob an alert belongs to, or None."""
    cj = labels.get("cronjob", "")
    if not cj or not cfg["checkCronJobs"] or labels.get("namespace") != cfg["opsNamespace"]:
        return None
    return cj if re.fullmatch(cfg["checkCronJobs"], cj) else None


def owned_by(job, cronjob):
    return any(r.get("kind") == "CronJob" and r.get("name") == cronjob
               for r in (job.get("metadata") or {}).get("ownerReferences") or [])


def job_state(job):
    conds = {c.get("type"): c.get("status") for c in (job.get("status") or {}).get("conditions") or []}
    if conds.get("Failed") == "True":
        return "failed"
    if conds.get("Complete") == "True":
        return "succeeded"
    return "running"


def last_failed_job(jobs, cronjob):
    """The newest failed Job of the CronJob when no Job of it succeeded after it (else None)."""
    mine = [j for j in jobs if owned_by(j, cronjob)]
    mine.sort(key=lambda j: ((j.get("metadata") or {}).get("creationTimestamp") or "",
                             (j.get("metadata") or {}).get("name") or ""))
    for job in reversed(mine):
        state = job_state(job)
        if state == "succeeded":
            return None
        if state == "failed":
            return job
    return None


def pod_message(pods):
    """The termination message of the newest pod's first terminated container."""
    pods = sorted(pods, key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")
    for pod in reversed(pods):
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            term = (cs.get("state") or {}).get("terminated") or (cs.get("lastState") or {}).get("terminated")
            if term and term.get("message"):
                return term["message"]
    return ""


def order(faults):
    return sorted(faults, key=lambda f: (SEVERITY_ORDER.get(f["severity"], 2), f["since"] or "~", f["id"], f["object"]))


def bound(doc, max_bytes):
    """Keeps the serialized key under max_bytes: details shortened to 5, then 1 line, then the lowest faults left out
    (the list is ordered, so critical faults stay longest)."""
    def size():
        return len(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode())
    for keep in (5, 1, 0):
        if size() <= max_bytes:
            return doc
        for f in doc["faults"]:
            if len(f["details"]) > keep:
                f["details"] = f["details"][:keep]
    while doc["faults"] and size() > max_bytes:
        doc["faults"].pop()
    return doc


def build_report(cfg, alerts, findings_for, now):
    """The key's document. findings_for(cronjob) -> list of lines (already cleaned)."""
    faults = []
    for a in alerts:
        labels = a.get("labels") or {}
        name = labels.get("alertname", "")
        if not name.startswith(cfg["alertPrefix"]) or suppressed(a):
            continue
        check = check_of(labels, cfg)
        faults.append({
            "id": clean_line(name, 120),
            "severity": clean_line(labels.get("severity") or "warning", 20),
            "since": timestamp(a.get("startsAt")),
            "object": clean_line(fault_object(labels), 253),
            "summary": clean_line((a.get("annotations") or {}).get("summary") or name, cfg["maxLineChars"]),
            "details": list(findings_for(check)) if check else [],
        })
    delivered = None
    if cfg["receiverCheck"]:
        delivered = not any((a.get("labels") or {}).get("alertname") == cfg["receiverAlert"] for a in alerts)
    doc = {"schemaVersion": SCHEMA_VERSION, "cluster": cfg["cluster"],
           "writtenAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "intervalSeconds": cfg["intervalSeconds"], "faults": order(faults), "alertsDelivered": delivered}
    return bound(doc, cfg["maxKeyBytes"])


# ---- I/O --------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect is answered as an error, never followed with the token


def make_transport(ca_file):
    """(request, timeout) -> (status, body). ca_file "" = the image's trust store. Redirects are not followed."""
    ctx = ssl.create_default_context(cafile=ca_file or None)
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))

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


class Http:
    def __init__(self, base, token, ca_file, timeout, transport=None, what="the API"):
        self.base = base.rstrip("/")
        if transport is None:
            if not self.base.startswith("https://"):
                raise SystemExit("the address of %s must use https" % what)
            transport = make_transport(ca_file)
        SECRETS.add(token)
        self.auth = "Bearer " + token
        self.timeout = timeout
        self.send = transport
        self.what = what

    def call(self, method, path, body=None, ok=(200,), content_type="application/json"):
        data = None
        headers = {"Accept": "application/json", "Authorization": self.auth}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        where = urllib.parse.urlsplit(path).path
        try:
            code, raw = self.send(req, self.timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError("%s: %s %s: no answer (%s)" % (self.what, method, where, getattr(e, "reason", e)))
        if code not in ok:
            raise ApiError("%s: %s %s answered %d" % (self.what, method, where, code), status=code)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError("%s: %s %s answered %d with a body that is not JSON" % (self.what, method, where, code),
                           status=code)


def read_file(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def in_cluster(cfg, transport, what):
    url = cfg["kubeURL"]
    if not url:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise SystemExit("not in a cluster and kubeURL is not set")
        url = "https://%s:%s" % (host if ":" not in host else "[%s]" % host, port)
    token = read_file(cfg["kubeTokenFile"])
    if not token:
        raise SystemExit("the job's service account token is missing")
    ca = cfg["kubeCAFile"] if os.path.exists(cfg["kubeCAFile"]) else ""
    return Http(url, token, ca, cfg["requestTimeoutSeconds"], transport, what)


def remote(cfg, transport):
    """The services cluster's API from the Secret's files; a missing one names the setting."""
    def need(key_setting, value_name):
        key = cfg[key_setting]
        value = read_file(os.path.join(cfg["targetDir"], key)) if key else ""
        if not value:
            raise SystemExit("the Secret named by %s has no key %s (setting ops.platformHealth.report."
                             "servicesCluster.%s)" % (cfg["targetSecretSetting"], key or "(unset)", value_name))
        return value
    server = need("targetServerKey", "serverKey")
    token = need("targetTokenKey", "tokenKey")
    ca = ""
    if cfg["targetCAKey"]:
        need("targetCAKey", "caKey")
        ca = os.path.join(cfg["targetDir"], cfg["targetCAKey"])
    return Http(server, token, ca, cfg["requestTimeoutSeconds"], transport, "the services cluster's API")


def read_alerts(am):
    doc = am.call("GET", "/api/v2/alerts?active=true")
    if not isinstance(doc, list) or not all(isinstance(a, dict) for a in doc):
        raise ApiError("the monitoring's alerts: the answer is not a list of alerts")
    return doc


class Findings:
    """The findings of each check's last failed run, read once per CronJob."""

    def __init__(self, kube, cfg):
        self.kube, self.cfg, self.cache, self.jobs, self.failed = kube, cfg, {}, None, []

    def __call__(self, cronjob):
        if cronjob not in self.cache:
            self.cache[cronjob] = self.read(cronjob)
        return self.cache[cronjob]

    def read(self, cronjob):
        ns = urllib.parse.quote(self.cfg["opsNamespace"])
        try:
            if self.jobs is None:
                doc = self.kube.call("GET", "/apis/batch/v1/namespaces/%s/jobs" % ns)
                self.jobs = (doc or {}).get("items") or []
            job = last_failed_job(self.jobs, cronjob)
            if job is None:
                return []
            sel = urllib.parse.quote("job-name=%s" % job["metadata"]["name"])
            doc = self.kube.call("GET", "/api/v1/namespaces/%s/pods?labelSelector=%s" % (ns, sel))
            return finding_lines(pod_message((doc or {}).get("items") or []), self.cfg)
        except ApiError as e:
            self.failed.append(str(e))
            return [clean_line(UNREADABLE % e, self.cfg["maxLineChars"])]


def write_key(target, cfg, key, text):
    """A merge patch of this cluster's key only; the ConfigMap is created when absent (once more as a patch when
    another writer created it meanwhile)."""
    ns, name = urllib.parse.quote(cfg["targetNamespace"]), urllib.parse.quote(cfg["targetConfigMap"])
    path = "/api/v1/namespaces/%s/configmaps/%s" % (ns, name)
    patch = {"data": {key: text}}
    try:
        target.call("PATCH", path, patch, content_type="application/merge-patch+json")
        return
    except ApiError as e:
        if e.status != 404:
            raise
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": cfg["targetConfigMap"], "namespace": cfg["targetNamespace"]}, "data": {key: text}}
    try:
        target.call("POST", "/api/v1/namespaces/%s/configmaps" % ns, body, ok=(200, 201))
    except ApiError as e:
        if e.status != 409:
            raise
        target.call("PATCH", path, patch, content_type="application/merge-patch+json")


def run(cfg, am, kube, target, now):
    """(exit code, document or None)."""
    try:
        alerts = read_alerts(am)
    except ApiError as e:
        print(redact("not written: the alerts could not be read: %s" % e))
        return 1, None
    findings = Findings(kube, cfg)
    doc = build_report(cfg, alerts, findings, now)
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    try:
        write_key(target, cfg, "%s.json" % cfg["cluster"], text)
    except ApiError as e:
        print(redact("not written: %s" % e))
        return 1, None
    print(json.dumps({"written": "%s.json" % cfg["cluster"], "faults": len(doc["faults"]),
                      "alertsDelivered": doc["alertsDelivered"]}))
    for f in findings.failed:
        print(redact("findings not read: %s" % f))
    return (1 if findings.failed else 0), doc


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as f:
            cfg.update(json.load(f) or {})
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", str(cfg["cluster"])) or len(cfg["cluster"]) > 63:
        raise SystemExit("setting cluster must be lower-case letters, digits and '-', at most 63 characters")
    for key in ("targetNamespace", "targetConfigMap", "opsNamespace"):
        if not cfg.get(key):
            raise SystemExit("setting %s is required" % key)
    if cfg["targetMode"] not in ("local", "remote"):
        raise SystemExit("setting targetMode must be local or remote")
    for key in ("intervalSeconds", "requestTimeoutSeconds", "maxLineChars", "maxDetails", "maxKeyBytes"):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or cfg[key] < 1:
            raise SystemExit("setting %s must be a whole number of at least 1" % key)
    if not isinstance(cfg["receiverCheck"], bool):
        raise SystemExit("setting receiverCheck must be true or false")
    return cfg


def main(argv=None, transport=None):
    """transport: for tests only (a plain-http fake); the real run uses https."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/platform-health-report/config.json")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    kube = in_cluster(cfg, transport, "this cluster's API")
    am_token = read_file(cfg["kubeTokenFile"])
    ca = cfg["alertmanagerCAFile"] if os.path.exists(cfg["alertmanagerCAFile"]) else ""
    am = Http(cfg["alertmanagerURL"], am_token, ca, cfg["requestTimeoutSeconds"], transport, "the monitoring")
    target = kube if cfg["targetMode"] == "local" else remote(cfg, transport)
    code, _ = run(cfg, am, kube, target, int(time.time()))
    return code


if __name__ == "__main__":
    sys.exit(main())

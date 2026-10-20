#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.1, §3.2): Google Cloud access for the platform's jobs without keys, standard library
only (no google-auth in the execution environments).

Identity chain of a job in the shc-cloud container group:
  JWT-SVID (the sidecar's file, audience = the hub's workload identity pool provider)
    -> STS token exchange (federated access token)
    -> impersonation of the hub's automation service account (iamcredentials generateAccessToken)
    -> impersonation of a tenant project's member service account (<memberServiceAccount>@<project>.iam...)
Environment (set by the container group from GCP_REALIZATION):
  GCP_TOKEN_FILE                     the JWT-SVID
  GCP_WORKLOAD_IDENTITY_AUDIENCE     //iam.googleapis.com/projects/<n>/locations/global/workloadIdentityPools/<pool>/providers/<p>
  GCP_AUTOMATION_SERVICE_ACCOUNT     the hub's automation service account email
  GCP_MEMBER_SERVICE_ACCOUNT         the per-project member account name (default shc-automation)
  GCP_ACCESS_TOKEN                   local checks only: a ready access token used as the platform identity

As a command:
  shc_gcp_api.py whoami [--project ID]     who the platform identity is, and (with --project) that the member
                                           account of that project can be entered. Prints one JSON line.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STS = "https://sts.googleapis.com/v1/token"
IAMCRED = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/%s:generateAccessToken"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
CRM = "https://cloudresourcemanager.googleapis.com/v3"
SERVICEUSAGE = "https://serviceusage.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
RETRY = (429, 500, 502, 503, 504)


class GcpError(Exception):
    def __init__(self, msg, code=0, status=""):
        super().__init__(msg)
        self.code, self.status = code, status


def fail(msg, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(1)


def http(method, url, body=None, token=None, headers=None, timeout=60, raw=False, ok404=False):
    """One REST call with retries on transient errors. JSON in and out; ok404 returns None on 404."""
    data, h = None, {"Accept": "application/json"}
    if body is not None:
        if isinstance(body, (dict, list)):
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        else:
            data = body if isinstance(body, bytes) else body.encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return b if raw else (json.loads(b) if b else {})
        except urllib.error.HTTPError as e:
            b = e.read()
            if e.code == 404 and ok404:
                return None
            if e.code in RETRY and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            try:
                err = json.loads(b).get("error", {})
                msg, status = err.get("message", str(err))[:600], err.get("status", "")
            except Exception:
                msg, status = b.decode(errors="replace")[:600], ""
            raise GcpError("%s %s: HTTP %d %s %s" % (method, url.split("?")[0], e.code, status, msg), e.code, status)
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise GcpError("%s %s: %s" % (method, url.split("?")[0], e.reason))


def list_all(url, key, token, params=None):
    """Every item of a paginated list (pageToken)."""
    items, params = [], dict(params or {})
    while True:
        q = urllib.parse.urlencode(params)
        page = http("GET", url + ("?" + q if q else ""), token=token)
        items += page.get(key, [])
        if not page.get("nextPageToken"):
            return items
        params["pageToken"] = page["nextPageToken"]


class Identity:
    """Access tokens for the platform (the hub's automation account) and for tenant projects' member accounts."""

    def __init__(self):
        self.automation_sa = os.environ.get("GCP_AUTOMATION_SERVICE_ACCOUNT", "")
        self.member_sa = os.environ.get("GCP_MEMBER_SERVICE_ACCOUNT", "shc-automation")
        self._tokens = {}  # key -> (token, expires_at)

    def _cached(self, key, mint):
        tok = self._tokens.get(key)
        if tok and tok[1] - time.time() > 300:
            return tok[0]
        value, ttl = mint()
        self._tokens[key] = (value, time.time() + ttl)
        return value

    def federated(self):
        def mint():
            path, aud = os.environ.get("GCP_TOKEN_FILE", ""), os.environ.get("GCP_WORKLOAD_IDENTITY_AUDIENCE", "")
            if not path or not aud:
                raise GcpError("no workload identity: GCP_TOKEN_FILE and GCP_WORKLOAD_IDENTITY_AUDIENCE are unset "
                               "(the job must run in the shc-cloud container group)")
            jwt = open(path).read().strip()
            r = http("POST", STS, urllib.parse.urlencode({
                "grantType": "urn:ietf:params:oauth:grant-type:token-exchange", "audience": aud, "scope": SCOPE,
                "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token", "subjectToken": jwt,
                "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt"}))
            return r["access_token"], int(r.get("expires_in", 3600))
        return self._cached("federated", mint)

    def impersonate(self, email, token, delegates=None, lifetime=3600):
        body = {"scope": [SCOPE], "lifetime": "%ds" % lifetime}
        if delegates:
            body["delegates"] = ["projects/-/serviceAccounts/" + d for d in delegates]
        r = http("POST", IAMCRED % email, body, token=token)
        return r["accessToken"]

    def platform(self):
        """The hub's automation account (or GCP_ACCESS_TOKEN for local checks)."""
        if os.environ.get("GCP_ACCESS_TOKEN"):
            return os.environ["GCP_ACCESS_TOKEN"]
        if not self.automation_sa:
            raise GcpError("GCP_AUTOMATION_SERVICE_ACCOUNT is unset")
        return self._cached("platform", lambda: (self.impersonate(self.automation_sa, self.federated()), 3600))

    def member_email(self, project):
        return "%s@%s.iam.gserviceaccount.com" % (self.member_sa, project)

    def project(self, project):
        """A tenant project's member account, entered from the platform identity."""
        return self._cached("project:" + project,
                            lambda: (self.impersonate(self.member_email(project), self.platform()), 3600))


def tokeninfo(token):
    return http("GET", TOKENINFO + "?" + urllib.parse.urlencode({"access_token": token}))


def enable_services(project, services, token, wait=300):
    """Enable the APIs a project lacks (serviceusage batchEnable). Returns the ones enabled now."""
    have = {s["config"]["name"] for s in list_all("%s/projects/%s/services" % (SERVICEUSAGE, project), "services",
                                                 token, {"filter": "state:ENABLED", "pageSize": 200})}
    missing = [s for s in services if s not in have]
    if not missing:
        return []
    op = http("POST", "%s/projects/%s/services:batchEnable" % (SERVICEUSAGE, project), {"serviceIds": missing}, token=token)
    deadline = time.time() + wait
    while not op.get("done") and time.time() < deadline:
        time.sleep(5)
        op = http("GET", "%s/%s" % (SERVICEUSAGE, op["name"]), token=token)
    if op.get("error"):
        raise GcpError("enabling %s: %s" % (missing, op["error"].get("message")))
    return missing


def wait_compute(op, token, timeout=600):
    """Wait for a Compute Engine operation (global, regional or zonal) and raise on error."""
    deadline = time.time() + timeout
    while op.get("status") != "DONE":
        if time.time() > deadline:
            raise GcpError("operation %s did not finish in %ds" % (op.get("name"), timeout))
        op = http("POST", op["selfLink"] + "/wait", {}, token=token)
    errs = (op.get("error") or {}).get("errors") or []
    if errs:
        raise GcpError("operation %s: %s" % (op.get("operationType"), "; ".join(e.get("message", e.get("code", "")) for e in errs)))
    return op


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("whoami")
    w.add_argument("--project")
    w.add_argument("--hub-project", default=os.environ.get("GCP_HUB_PROJECT", ""))
    a = ap.parse_args()
    ident = Identity()
    out = {"ok": True}
    try:
        tok = ident.platform()
        info = tokeninfo(tok)
        out["principal"] = info.get("email") or info.get("azp") or "?"
        out["scope"] = info.get("scope", "")
        if a.hub_project:
            p = http("GET", "%s/projects/%s" % (CRM, a.hub_project), token=tok)
            out["hub"] = {"project": p.get("projectId"), "number": (p.get("name") or "").split("/")[-1],
                          "state": p.get("state")}
        if a.project:
            ptok = ident.project(a.project)
            pinfo = tokeninfo(ptok)
            p = http("GET", "%s/projects/%s" % (CRM, a.project), token=ptok)
            out["project"] = {"project": p.get("projectId"), "number": (p.get("name") or "").split("/")[-1],
                              "principal": pinfo.get("email", "?"), "labels": p.get("labels", {}),
                              "state": p.get("state")}
    except GcpError as e:
        fail(str(e), code=e.code)
    except (OSError, KeyError) as e:
        fail("%s: %s" % (type(e).__name__, e))
    print(json.dumps(out))


if __name__ == "__main__":
    main()

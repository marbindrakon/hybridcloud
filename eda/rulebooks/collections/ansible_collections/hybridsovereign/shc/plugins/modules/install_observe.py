#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: install_observe
short_description: Observe the installation of an OpenShift cluster object on the hub
description:
  - C(mode=observe) reads the cluster object (PlatformOpenshift) on the services cluster, the hub's ClusterDeployment
    of its build and the ClusterProvision that ClusterDeployment names, decides what the installation's state is
    (module_utils shc_install.observe) and writes C(status.install), the C(Ready) condition and, on a stop, the
    C(Stalled) condition when something changed or the check time is due. It returns whether the watching job must
    end and why.
  - C(mode=check) reads the cluster object only and says whether it is still the job's object and request (the check
    right before the job makes the release).
  - C(mode=removal) reads the cluster object and the hub's ClusterDeployment and says whether the stopped build may be
    removed for a new installation (module_utils shc_install.may_remove_build). An unreadable hub or object never
    allows a removal.
  - Never copies text of the hub, the installer's log or the site into the status or its result messages.
options:
  mode: {description: observe, check or removal., type: str, default: observe, choices: [observe, check, removal]}
  host: {description: Services cluster API URL., type: str, required: true}
  api_key: {description: Services cluster bearer token., type: str, required: true, no_log: true}
  validate_certs: {description: Verify the services API certificate., type: bool, default: true}
  hub_host: {description: Hub API URL (observe, removal)., type: str, default: ""}
  hub_api_key: {description: Hub bearer token., type: str, default: "", no_log: true}
  hub_validate_certs: {description: Verify the hub API certificate., type: bool, default: false}
  name: {description: Name of the cluster object., type: str, required: true}
  namespace: {description: Namespace of the cluster object., type: str, required: true}
  build: {description: The build's name on the hub (its namespace and ClusterDeployment)., type: str, default: ""}
  uid: {description: The uid of the object the job works for., type: str, default: ""}
  event_id: {description: The event the job works on., type: str, default: ""}
  job_started_at: {description: When the job started (RFC 3339)., type: str, default: ""}
  settings: {description: windowMinutes, deadlineMinutes, writeEveryMinutes, provider, tenantValues., type: dict, default: {}}
  release_failed: {description: removal - a failed release of the build exists., type: bool, default: false}
  recheck: {description: removal - the fresh read right before the first delete of a removal this run decided., type: bool, default: false}
  job: {description: Job entry recorded in status.edaJobs (name, jobId, url)., type: dict}
  timeout: {description: HTTP timeout in seconds., type: int, default: 30}
author: [Hybrid Sovereign Cloud]
"""

RETURN = r"""
end: {description: The job must end now (observe)., type: bool, returned: always}
outcome: {description: gone, replaced, deleting, superseded, installed, stopped, overdue, window or none., type: str, returned: always}
ok: {description: check - the job may go on; removal - the build may be removed., type: bool, returned: check, removal}
why: {description: A sentence on the decision (no hub or installer text)., type: str, returned: always}
install: {description: status.install as decided., type: dict, returned: observe}
written: {description: A status write was made., type: bool, returned: observe}
"""

import datetime
import json

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import open_url
from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError

from ansible_collections.hybridsovereign.shc.plugins.module_utils import shc_install as I
from ansible_collections.hybridsovereign.shc.plugins.module_utils.shc_status import compute_status_patch

API = "hybridsovereign.redhat/v1alpha1"
HIVE = "hive.openshift.io/v1"


def _get(url, token, validate, timeout, method="GET", data=None, content_type=None):
    """(code, body) of a request; code None when the API could not be reached."""
    headers = {"Authorization": "Bearer %s" % token, "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        resp = open_url(url, method=method, data=data, headers=headers, validate_certs=validate, timeout=timeout)
        raw = resp.read()
        return getattr(resp, "status", None) or resp.getcode(), (json.loads(raw) if raw else {})
    except HTTPError as e:
        try:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {}
    except (URLError, ConnectionError, OSError, ValueError):
        return None, {}


def main():
    module = AnsibleModule(
        argument_spec=dict(
            mode=dict(type="str", default="observe", choices=["observe", "check", "removal"]),
            host=dict(type="str", required=True),
            api_key=dict(type="str", required=True, no_log=True),
            validate_certs=dict(type="bool", default=True),
            hub_host=dict(type="str", default=""),
            hub_api_key=dict(type="str", default="", no_log=True),
            hub_validate_certs=dict(type="bool", default=False),
            name=dict(type="str", required=True),
            namespace=dict(type="str", required=True),
            build=dict(type="str", default=""),
            uid=dict(type="str", default=""),
            event_id=dict(type="str", default=""),
            job_started_at=dict(type="str", default=""),
            settings=dict(type="dict", default={}),
            release_failed=dict(type="bool", default=False),
            recheck=dict(type="bool", default=False),
            job=dict(type="dict"),
            timeout=dict(type="int", default=30),
        ),
        supports_check_mode=True,
    )
    p = module.params
    obj_url = "%s/apis/%s/namespaces/%s/platformopenshifts/%s" % (p["host"].rstrip("/"), API, p["namespace"], p["name"])

    def read_obj():
        code, body = _get(obj_url, p["api_key"], p["validate_certs"], p["timeout"])
        if code == 404:
            return None, False
        if code != 200:
            return None, True
        return body, False

    def read_hub(path):
        url = "%s/apis/%s/namespaces/%s/%s" % (p["hub_host"].rstrip("/"), HIVE, p["build"], path)
        code, body = _get(url, p["hub_api_key"], p["hub_validate_certs"], p["timeout"])
        if code == 404:
            return None, False
        if code != 200:
            return None, True
        return body, False

    obj, obj_error = read_obj()

    if p["mode"] == "check":
        if obj_error:
            module.exit_json(changed=False, ok=True, end=False, outcome="",
                             why="the cluster object could not be read; the job goes on")
        ok, why = I.same_object(obj, p["uid"], p["event_id"].strip())
        module.exit_json(changed=False, ok=ok, end=not ok, outcome="" if ok else "ended", why=why)

    for key in ("hub_host", "hub_api_key", "build"):
        if not p[key]:
            module.fail_json(msg="install_observe: %s is required in mode %s" % (key, p["mode"]))

    if p["mode"] == "removal":
        cd, hub_error = read_hub("clusterdeployments/%s" % p["build"])
        if not obj_error and obj is not None:
            ok, why = I.same_object(obj, p["uid"], p["event_id"].strip())
            if not ok:
                module.exit_json(changed=False, ok=False, end=True, outcome="ended", why=why)
        ok, why = I.may_remove_build(None if obj_error else obj, cd, not hub_error, p["release_failed"],
                                     recheck=p["recheck"])
        module.exit_json(changed=False, ok=ok, end=False, outcome="", why=why)

    for key in ("windowMinutes", "deadlineMinutes", "writeEveryMinutes", "provider"):
        if p["settings"].get(key) in (None, ""):
            module.fail_json(msg="install_observe: the setting %s is missing" % key)

    settings = dict(p["settings"])
    tv = settings.get("tenantValues")
    if isinstance(tv, str):
        try:
            tv = json.loads(tv)
        except ValueError:
            tv = {}
    settings["tenantValues"] = tv if isinstance(tv, dict) else {}

    cd, prov, provs, hub_error = None, None, [], False
    if not obj_error and obj is not None:
        cd, hub_error = read_hub("clusterdeployments/%s" % p["build"])
        if cd is not None and not hub_error:
            # every provision of the build (Hive keeps the last ones; it clears provisionRef when it stops)
            listed, hub_error = read_hub("clusterprovisions")
            provs = [x for x in ((listed or {}).get("items") or [])
                     if isinstance(x, dict)
                     and ((x.get("spec") or {}).get("clusterDeploymentRef") or {}).get("name") in (None, p["build"])]
            ref = (((cd or {}).get("status") or {}).get("provisionRef") or {}).get("name") or ""
            prov = next((x for x in provs if (x.get("metadata") or {}).get("name") == ref), None) if ref else None

    now = datetime.datetime.now(datetime.timezone.utc)
    job = {"uid": p["uid"], "eventId": p["event_id"].strip(), "startedAt": p["job_started_at"]}
    res = I.observe(obj, job, cd, prov, now, settings, obj_error=obj_error, hub_error=hub_error, provisions=provs)
    out = {"end": res["end"], "outcome": res["outcome"] or "", "why": res["log"], "install": res["install"] or {},
           "written": False, "changed": False}
    if res["write"] and not module.check_mode:
        ready = [c for c in res["conditions"] if c.get("type") == "Ready"]
        body = {"status": res["status"], "install": res["install"], "conditions": res["conditions"]}
        if ready:
            body["reason"] = ready[0]["reason"]
            body["message"] = ready[0]["message"]
        opts = {"action": res["action"], "event_id": job["eventId"], "partial": False, "job": p["job"]}
        for attempt in range(5):
            if attempt:
                obj, obj_error = read_obj()
                if obj is None or obj_error:
                    break
            patch, info = compute_status_patch(obj, body, opts)
            if info.get("superseded"):
                out.update(end=True, outcome="superseded", why="a newer request for the cluster replaced this job's")
                break
            code, _ = _get(obj_url + "/status", p["api_key"], p["validate_certs"], p["timeout"], method="PATCH",
                           data=json.dumps(patch), content_type="application/merge-patch+json")
            if code in (200, 201):
                out.update(written=True, changed=True)
                break
            if code == 404:
                out.update(end=True, outcome="gone", why="the cluster object no longer exists")
                break
            if code != 409:
                # a failed write is not a failed job: the next observation writes again
                out["why"] = "%s; the status write failed (%s) and is made again at the next check" % (out["why"], code)
                break
        if not out["written"] and out["outcome"] in ("stopped", "overdue", "window"):
            # the ending is recorded before the job ends: look again (and write again) at the next check
            out["end"] = False
    module.exit_json(**out)


if __name__ == "__main__":
    main()

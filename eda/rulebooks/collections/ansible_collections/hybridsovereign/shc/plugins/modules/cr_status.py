#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: cr_status
short_description: Write the status of a hybridsovereign.redhat object
description:
  - Reads the object, merges the given status fields with the status contract of the API
    (conditions merged by type, legacy condition types dropped, delivery bookkeeping,
    teardown retry counting, derived C(status.phase) and C(status.summary)) and writes the
    C(/status) subresource with a JSON merge patch guarded by C(metadata.resourceVersion).
  - On a conflict the object is read again and the patch recomputed.
  - A write for an event that is no longer the object's current event is skipped and
    reported as superseded.
  - In check mode the object is read and the patch computed, nothing is written.
options:
  host: {description: API server URL., type: str, required: true}
  api_key: {description: Bearer token., type: str, required: true, no_log: true}
  validate_certs: {description: Verify the API server certificate., type: bool, default: true}
  ca_path: {description: CA bundle used to verify the API server., type: path}
  api_version: {description: group/version of the object., type: str, required: true}
  plural: {description: Resource plural., type: str, required: true}
  name: {description: Object name., type: str, required: true}
  namespace: {description: Object namespace., type: str, required: true}
  status: {description: Status fields to write., type: dict, default: {}}
  event_id: {description: Event this write belongs to., type: str, default: ""}
  action: {description: Job action (ready, failed, pending, reconciling, ...); defaults to status.status., type: str}
  partial: {description: Merge only the given fields (no delivery or job entry change)., type: bool, default: false}
  ignore_not_found: {description: Succeed without a write when the object does not exist., type: bool, default: false}
  job: {description: Job entry recorded in status.edaJobs (name, jobId, url)., type: dict}
  give_up_attempts: {description: Failed teardown jobs after which the object is marked Stalled., type: int, default: 10}
  read_only: {description: Only read the object (returned as resource)., type: bool, default: false}
  retries: {description: Attempts on a write conflict., type: int, default: 5}
  timeout: {description: HTTP timeout in seconds., type: int, default: 30}
author: [Hybrid Sovereign Cloud]
"""

EXAMPLES = r"""
- name: Mark a network ready
  hybridsovereign.shc.cr_status:
    host: "{{ api_host }}"
    api_key: "{{ token }}"
    api_version: hybridsovereign.redhat/v1alpha1
    plural: hybridnetworks
    name: core
    namespace: entity-example
    status: {status: ready, ready: true, message: Network is ready.}
"""

RETURN = r"""
superseded: {description: The event was replaced by a newer one; nothing was written., type: bool, returned: always}
skip_reason: {description: Why nothing was written (cr_not_found)., type: str, returned: when nothing was written}
http: {description: HTTP status of the write., type: int, returned: when written}
phase: {description: Derived status.phase., type: str, returned: when computed}
patch: {description: The computed merge patch., type: dict, returned: when computed}
resource: {description: The object after the write (or as read)., type: dict, returned: when available}
stdout: {description: JSON summary line (compatible with the former script output)., type: str, returned: always}
"""

import json
import random
import time

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import open_url
from ansible.module_utils.six.moves.urllib.error import HTTPError

from ansible_collections.hybridsovereign.shc.plugins.module_utils.shc_status import compute_status_patch


def _request(module, method, url, data=None, content_type=None):
    p = module.params
    headers = {"Authorization": "Bearer %s" % p["api_key"], "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        resp = open_url(url, method=method, data=data, headers=headers,
                        validate_certs=p["validate_certs"], ca_path=p.get("ca_path"),
                        timeout=p["timeout"])
        raw = resp.read()
        return getattr(resp, "status", None) or resp.getcode(), json.loads(raw) if raw else {}
    except HTTPError as e:
        try:
            raw = e.read()
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        return e.code, payload


def main():
    module = AnsibleModule(
        argument_spec=dict(
            host=dict(type="str", required=True),
            api_key=dict(type="str", required=True, no_log=True),
            validate_certs=dict(type="bool", default=True),
            ca_path=dict(type="path"),
            api_version=dict(type="str", required=True),
            plural=dict(type="str", required=True),
            name=dict(type="str", required=True),
            namespace=dict(type="str", required=True),
            status=dict(type="dict", default={}),
            event_id=dict(type="str", default=""),
            action=dict(type="str"),
            partial=dict(type="bool", default=False),
            ignore_not_found=dict(type="bool", default=False),
            job=dict(type="dict"),
            give_up_attempts=dict(type="int", default=10),
            read_only=dict(type="bool", default=False),
            retries=dict(type="int", default=5),
            timeout=dict(type="int", default=30),
        ),
        supports_check_mode=True,
    )
    p = module.params
    base = "%s/apis/%s/namespaces/%s/%s/%s" % (p["host"].rstrip("/"), p["api_version"], p["namespace"],
                                              p["plural"], p["name"])
    body = p["status"] or {}
    opts = {"action": p["action"] or body.get("status") or "reconcile", "event_id": (p["event_id"] or "").strip(),
            "partial": p["partial"], "job": p["job"], "give_up_attempts": p["give_up_attempts"]}
    last = None
    for attempt in range(max(1, p["retries"])):
        code, obj = _request(module, "GET", base)
        if code == 404:
            out = {"http": 404, "skip_reason": "cr_not_found", "superseded": False}
            if p["ignore_not_found"] or p["read_only"]:
                module.exit_json(changed=False, resource=None, stdout=json.dumps(out), **out)
            module.fail_json(msg="%s %s/%s not found" % (p["plural"], p["namespace"], p["name"]), stdout=json.dumps(out), **out)
        if code != 200:
            module.fail_json(msg="read failed (%s): %s" % (code, (obj or {}).get("message", "")), http=code, superseded=False,
                             stdout=json.dumps({"http": code}))
        if p["read_only"]:
            module.exit_json(changed=False, superseded=False, resource=obj, http=code, stdout=json.dumps({"http": code}))
        patch, info = compute_status_patch(obj, body, opts)
        if info.get("superseded"):
            out = {"superseded": True, "eventId": info.get("eventId"), "currentEventId": info.get("currentEventId")}
            module.exit_json(changed=False, resource=obj, stdout=json.dumps(out), **out)
        if module.check_mode:
            module.exit_json(changed=True, superseded=False, patch=patch, phase=info.get("phase"), resource=obj,
                             stdout=json.dumps({"check": True, "phase": info.get("phase")}))
        code, result = _request(module, "PATCH", base + "/status", data=json.dumps(patch),
                                content_type="application/merge-patch+json")
        last = (code, result)
        if code == 409:
            time.sleep(0.2 * (attempt + 1) + random.random() * 0.5)
            continue
        if code == 404 and p["ignore_not_found"]:
            out = {"http": 404, "skip_reason": "cr_not_found", "superseded": False}
            module.exit_json(changed=False, resource=None, stdout=json.dumps(out), **out)
        status = (result or {}).get("status") if isinstance(result, dict) else None
        out = {"http": code, "ready": bool((status or {}).get("ready", False)) if isinstance(status, dict) else False,
               "status": (status or {}).get("status", "MISSING") if isinstance(status, dict) else str(status),
               "phase": info.get("phase"), "superseded": False}
        if code not in (200, 201):
            module.fail_json(msg="status write failed (%s): %s" % (code, (result or {}).get("message", "") if isinstance(result, dict) else ""),
                             stdout=json.dumps(out), **out)
        module.exit_json(changed=True, patch=patch, resource=result, stdout=json.dumps(out), **out)
    module.fail_json(msg="status write conflicted %d times" % p["retries"], http=last[0] if last else None, superseded=False,
                     stdout=json.dumps({"http": last[0] if last else None}))


if __name__ == "__main__":
    main()

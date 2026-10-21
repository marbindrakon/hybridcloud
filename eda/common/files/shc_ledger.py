#!/usr/bin/env python3
"""Fabric allocation ledger and config lease (shc, design/P7-PLAN.md P7.3.4).

Reads one JSON request on stdin ({action, api, token, namespace, ...}), prints one JSON result; {"error": ...} and
exit 1 on failure. The token arrives on stdin, never in argv.

Ledger: ConfigMap fabric-ledger-<fabric> (key ledger.json) in the fabric's namespace, owned by the HybridFabric.
  {"version": 1, "fabric": <name>, "pools": {kind: [start, end]}, "alloc": {kind: {owner: value}}}
  kinds: vni, l2vni (per NetworkPlacement), vpnTarget (per HybridNetwork), peIndex (per provider edge: 1 is the
  border gateway root), egress (per HybridNetwork with internet egress: its index in the nodes' NAT pools),
  attachVlan / attachLink (per AttachmentCircuit: its VLAN on the bearer, its link subnet's index). Writes replace the ConfigMap at the resourceVersion read (optimistic concurrency, retried).
  ledger-ensure   {fabric, fabric_uid, pools, reserve: {kind: {owner: value}}}   create/update the pools; refused
                  when an allocated value would fall outside its new pool
  ledger-allocate {fabric, kind, owner}   the owner's value (lowest free one the first time)
  ledger-release  {fabric, kind, owner}
  An allocate or release that changes the ledger also writes its counts to the HybridFabric's status.allocated /
  status.available (best effort), so they follow every change and not only the fabric's own runs. Ensure, allocate
  and release also set the HybridFabric label hybridsovereign.redhat/allocations: "<n>", n = the allocations not
  held by the fabric itself (best effort); the admission policy shc-fabric-ledger-guard refuses deleting a fabric
  whose label is not "0".
  ledger-get      {fabric}
Lease: coordination.k8s.io Lease fabric-config-<fabric>, serializing the jobs that change the border gateway nodes.
  lease-acquire   {fabric, holder, duration (s, default 1800), wait (s, default 1200)}
  lease-release   {fabric, holder}
"""
import datetime
import json
import random
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()
KINDS = ("vni", "l2vni", "vpnTarget", "peIndex", "egress", "attachVlan", "attachLink")


class Api:
    def __init__(self, req):
        self.base = req["api"].rstrip("/")
        self.h = {"Authorization": f"Bearer {req['token']}", "Content-Type": "application/json"}

    def call(self, method, path, body=None, content_type=None):
        h = {**self.h, "Content-Type": content_type} if content_type else self.h
        r = requests.request(method, self.base + path, headers=h, data=json.dumps(body) if body else None,
                             verify=False, timeout=30)
        return r.status_code, (r.json() if r.text.startswith("{") else {"message": r.text[:300]})


def cm_path(ns, fabric=None):
    p = f"/api/v1/namespaces/{ns}/configmaps"
    return p + (f"/fabric-ledger-{fabric}" if fabric else "")


def counts(led):
    out = {"allocated": {}, "available": {}}
    for k in KINDS:
        s, e = led["pools"].get(k, [1, 0])
        n = len(led["alloc"].get(k, {}))
        out["allocated"][k] = n
        out["available"][k] = max(0, e - s + 1 - n)
    return out


ALLOCATIONS_LABEL = "hybridsovereign.redhat/allocations"


def fabric_owner(namespace, fabric):
    """The owner key the fabric's own reservations use (its border gateway root's peIndex)."""
    return f"HybridFabric/{namespace}/{fabric}"


def tenant_allocations(led, namespace, fabric):
    """Allocations of every kind not held by the fabric itself."""
    own = fabric_owner(namespace, fabric)
    return sum(1 for k in KINDS for o in (led.get("alloc") or {}).get(k, {}) if o != own)


def publish_label(api, req, led):
    """Merge-patch the allocations label on the HybridFabric. False when it could not (best effort)."""
    try:
        n = tenant_allocations(led, req["namespace"], req["fabric"])
        path = f"/apis/hybridsovereign.redhat/v1alpha1/namespaces/{req['namespace']}/hybridfabrics/{req['fabric']}"
        code, _ = api.call("PATCH", path, {"metadata": {"labels": {ALLOCATIONS_LABEL: str(n)}}},
                           content_type="application/merge-patch+json")
        return code == 200
    except Exception:  # noqa: BLE001 - the next change or fabric run publishes it
        return False


def read(api, req):
    code, cm = api.call("GET", cm_path(req["namespace"], req["fabric"]))
    if code == 404:
        return None, None
    if code != 200:
        raise RuntimeError(f"reading the ledger: HTTP {code} {cm.get('message', '')}")
    return cm, json.loads((cm.get("data") or {}).get("ledger.json") or "{}")


def write(api, req, cm, led):
    cm["data"] = {"ledger.json": json.dumps(led, sort_keys=True, indent=1)}
    code, out = api.call("PUT", cm_path(req["namespace"], req["fabric"]), cm)
    if code == 409:
        return False
    if code != 200:
        raise RuntimeError(f"writing the ledger: HTTP {code} {out.get('message', '')}")
    return True


def update(api, req, change):
    """read -> change(led) -> write at that resourceVersion; retried on conflict. change returns the result."""
    for attempt in range(12):
        cm, led = read(api, req)
        if cm is None:
            raise RuntimeError(f"ledger fabric-ledger-{req['fabric']} does not exist (the HybridFabric creates it)")
        result = change(led)
        if result.get("unchanged") or write(api, req, cm, led):
            result.pop("unchanged", None)
            return result
        time.sleep(0.2 + random.random() * (0.3 * attempt + 0.2))
    raise RuntimeError("the ledger kept changing underneath (12 conflicts)")


def publish_counts(api, req):
    """Write the ledger's current counts to the HybridFabric's status. Read again just before the write, so the
    last of several concurrent changes publishes the final counts. False when it could not."""
    try:
        cm, led = read(api, req)
        if cm is None:
            return False
        path = f"/apis/hybridsovereign.redhat/v1alpha1/namespaces/{req['namespace']}/hybridfabrics/{req['fabric']}/status"
        code, _ = api.call("PATCH", path, {"status": counts(led)}, content_type="application/merge-patch+json")
        labelled = publish_label(api, req, led)
        return code == 200 and labelled
    except Exception:  # noqa: BLE001 - the allocation stands; the next change or fabric run publishes the counts
        return False


def ledger_ensure(api, req):
    pools = {k: [int(v[0]), int(v[1])] for k, v in req["pools"].items()}
    cm, led = read(api, req)
    if cm is None:
        led = {"version": 1, "fabric": req["fabric"], "pools": pools, "alloc": {k: {} for k in KINDS}}
        for k, owners in (req.get("reserve") or {}).items():
            led["alloc"][k].update({o: int(v) for o, v in owners.items()})
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"fabric-ledger-{req['fabric']}", "namespace": req["namespace"],
                             "labels": {"hybridsovereign.redhat/fabric": req["fabric"]},
                             "ownerReferences": [{"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "HybridFabric",
                                                  "name": req["fabric"], "uid": req["fabric_uid"]}]},
                "data": {"ledger.json": json.dumps(led, sort_keys=True, indent=1)}}
        code, out = api.call("POST", cm_path(req["namespace"]), body)
        if code == 409:
            return ledger_ensure(api, req)
        if code not in (200, 201):
            raise RuntimeError(f"creating the ledger: HTTP {code} {out.get('message', '')}")
        return {"created": True, **counts(led), "labelPublished": publish_label(api, req, led)}

    def change(led):
        bad = []
        for k, (s, e) in pools.items():
            outside = sorted(v for v in led["alloc"].get(k, {}).values() if not s <= v <= e)
            if outside:
                bad.append(f"{k} {s}-{e} would drop allocated {outside[:10]}")
        if bad:
            raise RuntimeError("pool change refused: " + "; ".join(bad))
        before = json.dumps(led, sort_keys=True)
        led["pools"] = pools
        for k in KINDS:
            led["alloc"].setdefault(k, {})
        for k, owners in (req.get("reserve") or {}).items():
            for o, v in owners.items():
                holder = next((h for h, x in led["alloc"][k].items() if x == int(v)), None)
                if holder not in (None, o):
                    raise RuntimeError(f"{k} {v} is reserved for {o} but allocated to {holder}")
                led["alloc"][k][o] = int(v)
        return {"created": False, "unchanged": json.dumps(led, sort_keys=True) == before, **counts(led)}
    result = update(api, req, change)
    _, led = read(api, req)
    result["labelPublished"] = publish_label(api, req, led) if led is not None else False
    return result


def ledger_allocate(api, req):
    k, owner = req["kind"], req["owner"]

    def change(led):
        alloc = led["alloc"].setdefault(k, {})
        if owner in alloc:
            return {"value": alloc[owner], "unchanged": True}
        s, e = led["pools"][k]
        used = set(alloc.values())
        free = next((v for v in range(s, e + 1) if v not in used), None)
        if free is None:
            raise RuntimeError(f"{k} pool {s}-{e} is exhausted")
        alloc[owner] = free
        return {"value": free, "new": True}
    result = update(api, req, change)
    if result.get("new"):
        result["countsPublished"] = publish_counts(api, req)
    return result


def ledger_release(api, req):
    k, owner = req["kind"], req["owner"]

    def change(led):
        v = led["alloc"].setdefault(k, {}).pop(owner, None)
        return {"released": v, "unchanged": v is None}
    result = update(api, req, change)
    if result.get("released") is not None:
        result["countsPublished"] = publish_counts(api, req)
    return result


def ledger_get(api, req):
    cm, led = read(api, req)
    if cm is None:
        return {"exists": False}
    return {"exists": True, "ledger": led, **counts(led)}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def lease_path(ns, name=None):
    p = f"/apis/coordination.k8s.io/v1/namespaces/{ns}/leases"
    return p + (f"/{name}" if name else "")


def lease_acquire(api, req):
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    dur, deadline = int(req.get("duration", 1800)), time.time() + int(req.get("wait", 1200))
    while True:
        code, lease = api.call("GET", lease_path(req["namespace"], name))
        spec = {"holderIdentity": holder, "leaseDurationSeconds": dur, "acquireTime": ts(now()), "renewTime": ts(now())}
        if code == 404:
            code, out = api.call("POST", lease_path(req["namespace"]), {
                "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                "metadata": {"name": name, "namespace": req["namespace"]}, "spec": spec})
            if code in (200, 201):
                return {"acquired": True, "lease": name}
            if code != 409:
                raise RuntimeError(f"creating lease {name}: HTTP {code} {out.get('message', '')}")
            continue
        if code != 200:
            raise RuntimeError(f"reading lease {name}: HTTP {code} {lease.get('message', '')}")
        s = lease.get("spec") or {}
        cur = s.get("holderIdentity") or ""
        renew = s.get("renewTime")
        expired = True
        if cur and renew:
            t = datetime.datetime.strptime(renew.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
            expired = (now() - t).total_seconds() > int(s.get("leaseDurationSeconds") or dur)
        if not cur or cur == holder or expired:
            lease["spec"] = spec
            code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
            if code == 200:
                return {"acquired": True, "lease": name, "tookOverFrom": cur if cur and cur != holder else ""}
            if code != 409:
                raise RuntimeError(f"taking lease {name}: HTTP {code} {out.get('message', '')}")
            continue
        if time.time() > deadline:
            raise RuntimeError(f"the border gateway configuration is busy: lease {name} held by {cur}")
        time.sleep(15)


def lease_release(api, req):
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    code, lease = api.call("GET", lease_path(req["namespace"], name))
    if code != 200 or (lease.get("spec") or {}).get("holderIdentity") != holder:
        return {"released": False}
    lease["spec"]["holderIdentity"] = None
    code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
    return {"released": code == 200}


ACTIONS = {"ledger-ensure": ledger_ensure, "ledger-allocate": ledger_allocate, "ledger-release": ledger_release,
           "ledger-get": ledger_get, "lease-acquire": lease_acquire, "lease-release": lease_release}


def main():
    try:
        req = json.load(sys.stdin)
        fn = ACTIONS[req["action"]]
        print(json.dumps(fn(Api(req), req)))
        return 0
    except Exception as e:  # noqa: BLE001 - every failure becomes the job's message
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

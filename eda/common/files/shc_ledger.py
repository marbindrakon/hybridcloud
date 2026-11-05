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
  The holder renews it before each node and around its long steps. A lease not renewed for its duration (its holder
  crashed or hangs) is taken over by a job waiting for it; the former holder then stops at its next renewal, before
  it changes a node again. Expiry is judged by the duration the lease itself carries, so a lease written with another
  duration (an older job, another installation's value) keeps its own.
  lease-acquire   {fabric, holder, duration (s, default 600), wait (s, default 1200), stepTimeout (s, default 180)}
                  refused unless wait > duration and duration >= stepTimeout + 2 x RENEW_BUDGET + LEASE_SEGMENT: the
                  longest time a live holder can go between two renewals (the node steps between them, one step that
                  hangs until its timeout, the two renewals) must stay below the duration
  lease-renew     {fabric, holder, step (what the job does next, for the message)}: renewTime = now, by the holder
                  only, at the resourceVersion read; {"error": ...} when the lease is no longer this job's (taken over,
                  released or deleted) or the API did not answer within RENEW_BUDGET s: the job must stop there
  lease-release   {fabric, holder, delete (bool)}: frees the lease (delete: deletes it, at the resourceVersion read)
                  only while this job holds it
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
    # A request that could not reach the API (its name did not resolve, the connection was refused or reset, the
    # connect timed out) is sent again after these pauses (30 s in all): a blip must not end a job with the border
    # gateway lease still held (the next job waited 20 minutes for it) or between the steps of a ledger change.
    # Every write here is safe to repeat: ledger writes carry the resourceVersion they read and a lease conflict
    # reads the lease again.
    RETRY_PAUSES = (1, 2, 4, 8, 15)

    def __init__(self, req):
        self.base = req["api"].rstrip("/")
        self.h = {"Authorization": f"Bearer {req['token']}", "Content-Type": "application/json"}
        # time.time() after which no request is sent or repeated (a lease renewal's budget); None: no limit
        self.deadline = None

    def call(self, method, path, body=None, content_type=None):
        h = {**self.h, "Content-Type": content_type} if content_type else self.h
        for pause in self.RETRY_PAUSES + (None,):
            timeout = 30
            if self.deadline is not None:
                timeout = min(30.0, self.deadline - time.time())
                if timeout <= 0:
                    raise requests.exceptions.Timeout("no answer within the time allowed")
            try:
                r = requests.request(method, self.base + path, headers=h, data=json.dumps(body) if body else None,
                                     verify=False, timeout=timeout)
            except requests.exceptions.ConnectionError:
                if pause is None or (self.deadline is not None and time.time() + pause >= self.deadline):
                    raise
                time.sleep(pause)
                continue
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
    # from time.time(), so one clock drives the lease times and the waits (the tests replace it)
    return datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc)


def ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(v):
    """A Lease time (RFC 3339 MicroTime, also without the fraction) as an aware datetime."""
    v = str(v).replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(v, fmt)
        except ValueError:
            pass
    raise ValueError(f"unreadable lease time {v!r}")


def lease_path(ns, name=None):
    p = f"/apis/coordination.k8s.io/v1/namespaces/{ns}/leases"
    return p + (f"/{name}" if name else "")


# The border gateway lease. The defaults are the realization's nodes.configLease, which every holder passes in; the
# numbers rest on the holders' recorded step times (a node's steps between two renewals took at most 46 s, the
# longest wait between two renewals 201 s, a single step at most 19 s).
LEASE_DURATION = 600      # s without a renewal before a waiting job may take the lease over
LEASE_WAIT = 1200         # s a job waits for the lease before it gives up: longer than the duration
LEASE_STEP_TIMEOUT = 180  # s one step on a node may take: the holders run their node tasks with this timeout
RENEW_BUDGET = 60         # s a renewal may take, its retries included; past that it fails and the job stops
LEASE_SEGMENT = 60        # s the node steps between two renewals may take when none of them hangs
LEASE_POLL = 15           # s between two looks at a busy lease


class LeaseLost(RuntimeError):
    """The lease is no longer this job's: it must not change the border gateway nodes again."""


def lease_age(spec):
    """Seconds since the lease was last renewed; None when it carries no renewTime."""
    return (now() - parse_ts(spec["renewTime"])).total_seconds() if spec.get("renewTime") else None


def lease_limits(req):
    """(duration, wait) of an acquire, refused when a live holder could lose the lease or a waiter outlast it."""
    dur = int(req.get("duration") or LEASE_DURATION)
    wait = int(req.get("wait") or LEASE_WAIT)
    step = int(req.get("stepTimeout") or LEASE_STEP_TIMEOUT)
    floor = step + 2 * RENEW_BUDGET + LEASE_SEGMENT
    if dur < floor:
        raise RuntimeError(
            f"the border gateway lease duration {dur} s is too short: a job holding it may go {floor} s between two "
            f"renewals (one step until its {step} s timeout, {LEASE_SEGMENT} s of other steps, two renewals of up to "
            f"{RENEW_BUDGET} s); set nodes.configLease.durationSeconds to at least {floor}")
    if wait <= dur:
        raise RuntimeError(
            f"the border gateway lease wait {wait} s must be longer than its duration {dur} s, or a job waiting behind "
            "a holder that crashed gives up before the lease runs out; raise nodes.configLease.waitSeconds")
    return dur, wait


def lease_acquire(api, req):
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    dur, wait = lease_limits(req)
    deadline = time.time() + wait
    while True:
        code, lease = api.call("GET", lease_path(req["namespace"], name))
        stamp = ts(now())
        spec = {"holderIdentity": holder, "leaseDurationSeconds": dur, "acquireTime": stamp, "renewTime": stamp}
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
        # the lease's own duration: one written with another (an older job's 1800 s) is judged by that one
        lasts = int(s.get("leaseDurationSeconds") or dur)
        age = lease_age(s)
        expired = age is None or age > lasts
        if not cur or cur == holder or expired:
            lease["spec"] = spec
            code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
            if code == 200:
                took = cur if cur and cur != holder else ""
                return {"acquired": True, "lease": name, "tookOverFrom": took,
                        **({"notRenewedFor": round(age)} if took and age is not None else {})}
            if code != 409:
                raise RuntimeError(f"taking lease {name}: HTTP {code} {out.get('message', '')}")
            continue
        if time.time() > deadline:
            raise RuntimeError(
                f"the border gateway configuration is busy: lease {name} held by {cur} (renewed {round(age)} s ago; "
                f"taken over once it goes {lasts} s without a renewal); gave up after waiting {wait} s")
        time.sleep(LEASE_POLL)


def lease_lost(name, spec, stop):
    cur = spec.get("holderIdentity") or ""
    if cur:
        return (f"the border gateway configuration lease {name} was taken over by {cur} at "
                f"{spec.get('acquireTime') or 'an unknown time'}: this job had not renewed it within its duration. {stop}")
    return (f"the border gateway configuration lease {name} is no longer held by this job: another job took it over "
            f"after this job's renewals stopped, and has released it since. {stop}")


def lease_renew(api, req):
    """renewTime = now on the lease this job holds, at the resourceVersion read. Raises when the lease is no longer
    this job's or the API does not answer within RENEW_BUDGET s: the job then stops before its next step on the nodes.
    A lease that went past its duration but is still this job's is renewed: nobody took it in between (a takeover
    writes another holder, and only a release clears it), so this job still is the only one on the nodes."""
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    stop = (f"Stopped before {req.get('step') or 'its next step on the nodes'} so that two jobs never change the "
            "border gateway nodes at the same time; the next run repeats this job's work.")
    api.deadline = time.time() + RENEW_BUDGET
    try:
        for _ in range(5):
            code, lease = api.call("GET", lease_path(req["namespace"], name))
            if code == 404:
                raise LeaseLost(f"the border gateway configuration lease {name} no longer exists: it was deleted while "
                                f"this job held it. {stop}")
            if code != 200:
                raise RuntimeError(f"reading lease {name}: HTTP {code} {lease.get('message', '')}")
            s = lease.get("spec") or {}
            if (s.get("holderIdentity") or "") != holder:
                raise LeaseLost(lease_lost(name, s, stop))
            lasts = int(s.get("leaseDurationSeconds") or LEASE_DURATION)
            age = lease_age(s)
            s["renewTime"] = ts(now())
            lease["spec"] = s
            code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
            if code == 200:
                return {"renewed": True, "lease": name, "sinceLastRenewal": None if age is None else round(age),
                        "lapsed": age is not None and age > lasts}
            # 409: the lease changed since the read (a takeover, or this job's own renewal sent twice): read it again
            if code != 409:
                raise RuntimeError(f"renewing lease {name}: HTTP {code} {out.get('message', '')}")
        raise RuntimeError(f"lease {name} kept changing underneath (5 conflicts)")
    except LeaseLost:
        raise
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"could not renew the border gateway configuration lease {name}: the API did not answer "
                           f"within {RENEW_BUDGET} s ({e}). {stop}") from e
    except Exception as e:  # noqa: BLE001 - an unconfirmed lease stops the job like a lost one
        raise RuntimeError(f"could not renew the border gateway configuration lease {name}: {e}. {stop}") from e
    finally:
        api.deadline = None


def lease_release(api, req):
    name, holder = f"fabric-config-{req['fabric']}", req["holder"]
    code, lease = api.call("GET", lease_path(req["namespace"], name))
    if code != 200 or (lease.get("spec") or {}).get("holderIdentity") != holder:
        return {"released": False}
    if req.get("delete"):
        md = lease.get("metadata") or {}
        pre = {k: md[k] for k in ("uid", "resourceVersion") if md.get(k)}
        code, out = api.call("DELETE", lease_path(req["namespace"], name),
                             {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": pre})
        return {"released": code in (200, 202), "deleted": code in (200, 202)}
    lease["spec"]["holderIdentity"] = None
    code, out = api.call("PUT", lease_path(req["namespace"], name), lease)
    return {"released": code == 200}


ACTIONS = {"ledger-ensure": ledger_ensure, "ledger-allocate": ledger_allocate, "ledger-release": ledger_release,
           "ledger-get": ledger_get, "lease-acquire": lease_acquire, "lease-renew": lease_renew,
           "lease-release": lease_release}


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

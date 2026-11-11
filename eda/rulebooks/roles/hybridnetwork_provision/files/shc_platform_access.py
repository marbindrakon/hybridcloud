#!/usr/bin/env python3
"""Platform access of a HybridNetwork (the network job's part of the service routing domain).

Reads one JSON request on stdin ({action, ...}) and prints one JSON result; {"error": ...} and exit 1 on a request
it cannot read. Pure: no API calls (the job reads the objects and writes the results).

hub-check {prefixes, held, hub, reserved}
    The network's spec.prefixes against the infrastructure ranges the platform's management connects from (the
    realization's infrastructure.hub, either family). A prefix that overlaps one is refused unless the address space
    already holds it (held: the prefixes of status.addressSpace): a network that predates the rule keeps working and
    is reported instead. Returns {refused: [message], overlaps: [{prefix, range, purpose}]}.

platform-access {network, platformAccess, space, overlaps, hub, service, serviceStatus, reserved, clusters, ledger,
                 fabric}
    status.platformAccess and the condition PlatformReachable, and which cluster prefixes the network accepts into
    the service routing domain:
      network        "<namespace>/<name>"
      platformAccess spec.platformAccess (absent = true)
      space          the network's address space (both families)
      overlaps       hub-check's overlaps (grandfathered prefixes inside the hub's ranges)
      hub            infrastructure.hub
      service        the realization's service keys {enabled, ports, attachableRanges}
      serviceStatus  HybridFabric status.realization.service {circuits, networks, refused}
      reserved       HybridFabric status.reservedRanges rows {prefix, purpose, rule, site?}; site rows do not apply
      clusters       [{name, prefixes, deleting}] the PlatformOpenshifts that name the network (not fully deleted)
      ledger         the fabric ledger's servicePrefix allocations {owner: value}
      fabric         the fabric's name (messages)
    A prefix is accepted when the network is eligible and the prefix is IPv4, inside the network's space, inside
    service.attachableRanges (when set), clear of the reserved ranges and of every prefix accepted earlier for another
    network (first come first served: the ledger value, the lower the earlier; a prefix the fabric realizes counts as
    accepted). Returns {state, reason, message, condition, sources, ports, prefixes, accepted, allocate, release,
    held, attached, wakeFabric}.
"""
import ipaddress
import json
import sys

HUB_PURPOSE = "Networks the platform's management connects from"
KIND = "servicePrefix"
TENANT_REASONS = ("NoIPv4", "InfrastructureOverlap", "OutsideAttachableRanges")


def net(text):
    try:
        return ipaddress.ip_network(str(text).strip(), strict=False)
    except ValueError:
        return None


def nets(items):
    out = []
    for i in items or []:
        n = net(i)
        if n is not None and n not in out:
            out.append(n)
    return out


def overlaps(a, b):
    return a.version == b.version and a.overlaps(b)


def owner(network, prefix):
    return f"HybridNetwork/{network}/{prefix}"


def parse_owner(o):
    """'HybridNetwork/<ns>/<name>/<prefix>' -> ('<ns>/<name>', '<prefix>'), or None."""
    parts = str(o).split("/")
    if len(parts) < 5 or parts[0] != "HybridNetwork":
        return None
    return f"{parts[1]}/{parts[2]}", "/".join(parts[3:])


def hub_purpose(reserved, rng):
    for row in reserved or []:
        if net(row.get("prefix")) == rng and row.get("purpose"):
            return str(row["purpose"])
    return HUB_PURPOSE


def hub_check(req):
    hub = nets(req.get("hub"))
    held = {str(n) for n in nets(req.get("held"))}
    refused, found = [], []
    for p in req.get("prefixes") or []:
        n = net(p)
        if n is None:
            continue
        for h in hub:
            if not overlaps(n, h):
                continue
            purpose = hub_purpose(req.get("reserved"), h)
            if str(n) in held:
                found.append({"prefix": str(n), "range": str(h), "purpose": purpose})
            else:
                refused.append(f"prefix {n} overlaps {h}, reserved by the platform: {purpose}")
    return {"refused": refused, "overlaps": found}


def reserved_rows(req):
    """The ranges a cluster prefix must stay clear of: the published rows without the site ones (a site's own
    addresses are stepped around inside that site only), and the hub's ranges in any case."""
    rows = []
    for r in req.get("reserved") or []:
        n = net(r.get("prefix"))
        if n is None or r.get("site"):
            continue
        rows.append((n, str(r.get("purpose") or "")))
    for h in nets(req.get("hub")):
        if not any(n == h for n, _ in rows):
            rows.append((h, HUB_PURPOSE))
    return rows


def platform_access(req):
    key = str(req.get("network") or "")
    fabric = str(req.get("fabric") or "")
    svc = req.get("service") or {}
    sst = req.get("serviceStatus") or {}
    space = nets(req.get("space"))
    space4 = [n for n in space if n.version == 4]
    hub = nets(req.get("hub"))
    sources = [str(h) for h in hub if h.version == 4]
    ports = [int(p) for p in (svc.get("ports") or [])]
    attachable = [n for n in nets(svc.get("attachableRanges")) if n.version == 4]
    ledger = {str(k): v for k, v in (req.get("ledger") or {}).items()}
    clusters = sorted(req.get("clusters") or [], key=lambda c: str(c.get("name")))
    held = [str(c.get("name")) for c in clusters]
    spec_on = req.get("platformAccess") is not False
    on = spec_on or bool(clusters)

    mine = next((e for e in sst.get("networks") or [] if e.get("network") == key), None)
    realized = {str(n) for n in nets((mine or {}).get("prefixes"))}
    fabric_refused = {str(net(r.get("prefix"))): str(r.get("reason") or "ServicePrefixInUse")
                      for r in sst.get("refused") or [] if r.get("network") == key and net(r.get("prefix"))}

    # the network's own eligibility, in the order a reader acts on it
    reason, message = "", ""
    if not on:
        reason, message = "NotRequested", ("spec.platformAccess is off: the platform's management may not reach this "
                                           "network. Turn it on to build clusters the platform manages on it.")
    elif not svc.get("enabled"):
        reason, message = "ServiceNotOffered", (
            f"HybridFabric {fabric} does not offer the platform's connection for clusters on networks; ask the "
            "platform team.")
    elif not (sst.get("circuits") or []):
        reason, message = "CircuitNotDeployed", (
            "The platform's connection for clusters on networks is not ready: no platform circuit is realized on "
            f"HybridFabric {fabric}; ask the platform team.")
    elif not space4:
        reason, message = "NoIPv4", ("The network has no IPv4 address space; the platform reaches clusters over "
                                     "IPv4. Add an IPv4 prefix to spec.prefixes.")
    elif req.get("overlaps"):
        o = req["overlaps"][0]
        reason, message = "InfrastructureOverlap", (
            f"The network's address space {o['prefix']} overlaps {o['range']} ({o['purpose']}); the platform cannot "
            "reach it. Clusters the platform manages need a network clear of that range.")
    elif attachable and not any(overlaps(n, a) for n in space4 for a in attachable):
        reason, message = "OutsideAttachableRanges", (
            "Clusters the platform manages need a network with addresses inside "
            + ", ".join(str(a) for a in attachable) + " (TenantProfile status.managedClusterRanges).")
    eligible = not reason

    rows, accepted = [], []
    reserved = reserved_rows(req)
    others = []  # (network, prefix, rank): prefixes accepted for other networks
    for o, v in ledger.items():
        p = parse_owner(o)
        if p and p[0] != key and net(p[1]):
            others.append((p[0], net(p[1]), int(v)))
    for e in sst.get("networks") or []:
        if e.get("network") == key:
            continue
        for q in nets(e.get("prefixes")):
            if not any(n == e.get("network") and r == q for n, r, _ in others):
                others.append((e.get("network"), q, -1))

    for c in clusters:
        for p in c.get("prefixes") or []:
            n = net(p)
            row = {"prefix": str(n) if n else str(p), "cluster": str(c.get("name")), "state": "refused",
                   "reason": "", "message": ""}
            rows.append(row)
            if n is None:
                row.update(reason="InvalidSpec", message=f"{p} is not an address prefix")
                continue
            if not eligible:
                row.update(state="refused" if reason in TENANT_REASONS else "pending", reason=reason,
                           message=message)
                continue
            if n.version != 4:
                row.update(reason="FamilyUnavailable", message=f"{n}: the platform reaches clusters over IPv4 only")
                continue
            if not any(n.subnet_of(s) for s in space4):
                row.update(reason="OutsideAddressSpace", message=f"{n} is outside the network's address space")
                continue
            if attachable and not any(n.subnet_of(a) for a in attachable):
                row.update(reason="OutsideAttachableRanges",
                           message=f"{n} is outside the ranges for clusters the platform manages ("
                                   + ", ".join(str(a) for a in attachable) + ")")
                continue
            hit = next(((r, why) for r, why in reserved if overlaps(n, r)), None)
            if hit:
                row.update(reason="PrefixReserved",
                           message=f"{n} overlaps {hit[0]}, reserved by the platform: {hit[1]}")
                continue
            if str(n) in fabric_refused:
                row.update(reason=fabric_refused[str(n)],
                           message=f"{n} is already reached by the platform for another network"
                           if fabric_refused[str(n)] == "ServicePrefixInUse" else f"{n} is refused by the platform")
                continue
            mine_rank = int(ledger[owner(key, str(n))]) if owner(key, str(n)) in ledger else float("inf")
            if any(overlaps(n, q) and rank < mine_rank for _, q, rank in others):
                row.update(reason="ServicePrefixInUse",
                           message=f"{n} is already reached by the platform for another network; a new address "
                                   "for the cluster resolves it")
                continue
            if str(n) not in accepted:
                accepted.append(str(n))
            if str(n) in realized:
                row.update(state="ready")
            else:
                row.update(state="pending", message="the platform adds it to its connection with the network")

    accepted = sorted(accepted, key=lambda s: (ipaddress.ip_network(s).network_address, s))
    allocate = [owner(key, p) for p in accepted if owner(key, p) not in ledger]
    release = sorted(o for o in ledger if (parse_owner(o) or ("",))[0] == key
                     and parse_owner(o)[1] not in accepted)
    waiting = [p for p in accepted if p not in realized]
    attached = mine is not None and bool(realized)

    if not eligible:
        state = "off" if reason == "NotRequested" else "unavailable"
        cond = {"type": "PlatformReachable", "status": "False", "reason": reason, "message": message}
    elif waiting:
        state = "attached" if attached else "attachable"
        names = sorted({r["cluster"] for r in rows if r["prefix"] in waiting})
        message = (f"The platform is connecting to the network for cluster(s) {', '.join(names)}: "
                   f"{', '.join(waiting)}; try again in a few minutes.")
        reason = "AttachPending"
        cond = {"type": "PlatformReachable", "status": "False", "reason": reason, "message": message}
    elif accepted:
        state, reason = "attached", ""
        message = f"The platform reaches the clusters on this network at {', '.join(accepted)}."
        cond = {"type": "PlatformReachable", "status": "True", "reason": "Attached", "message": message}
    else:
        state, reason = "attachable", ""
        message = "Clusters the platform manages can be built on this network."
        cond = {"type": "PlatformReachable", "status": "True", "reason": "Attachable", "message": message}

    note = ""
    if not spec_on and clusters:
        note = (" spec.platformAccess off waits while clusters the platform manages use the network: "
                + ", ".join(held) + ".")
    out_message = message + note
    cond = dict(cond, message=cond["message"] + note)
    wake = sorted(accepted) != sorted(realized) or (mine is not None and not accepted)
    return {"state": state, "reason": cond["reason"] if cond["status"] == "False" else "",
            "message": out_message, "condition": cond, "sources": sources, "ports": ports, "prefixes": rows,
            "accepted": accepted, "allocate": allocate, "release": release,
            "held": held if (not spec_on and clusters) else [], "attached": attached, "wakeFabric": wake}


ACTIONS = {"hub-check": hub_check, "platform-access": platform_access}


def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
        fn = ACTIONS[req.get("action")]
    except (ValueError, KeyError) as e:
        print(json.dumps({"error": f"unreadable request: {e}"}))
        return 1
    print(json.dumps(fn(req), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

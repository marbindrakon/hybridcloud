#!/usr/bin/env python3
"""Fabric checks for the HybridFabric, CloudGateway and NetworkPlacement jobs (shc, design/P7-PLAN.md P7.3).

Reads one JSON request on stdin, prints one JSON result: {"errors": [...]} plus action-specific fields. Exit 0
unless the request itself is malformed. Pure computation: no API calls, nothing secret in or out.

actions:
  check-fabric    {spec}                                   core prefixes, node names
  check-gateway   {fabric: spec, gateway: spec, others: [CloudGateway objects or {name, spec}]}
                  nodes match the fabric's, one gateway per site, VLAN and site subnet unique on the fabric,
                  site subnet clear of the core prefixes
  check-placement {prefixes, siblings: [{name, prefixes}], reserved: [cidr]}
                  prefixes disjoint among themselves, from the network's other placements and from reserved
  cloudvirt-namespaces {entity, cloudvirt, namespaces: [{name, network?}]}
                  the site namespace of each CloudVirt namespace: <entity>-<cloudvirt>-<name>, cut to 54 characters
                  plus a hash when longer than 63 (namespace names are global on the site's cluster)
"""
import hashlib
import ipaddress
import json
import re
import sys


def nets(cidrs):
    out = []
    for c in cidrs or []:
        try:
            out.append(ipaddress.ip_network(str(c), strict=True))
        except ValueError as e:
            raise ValueError(f"{c}: {e}")
    return out


def overlaps(a, b):
    return a.version == b.version and a.overlaps(b)


def check_fabric(req):
    spec = req.get("spec") or {}
    errors = []
    core = spec.get("core") or {}
    try:
        lo, link = nets([core.get("loopbackPool"), core.get("linkSubnet")])
        if overlaps(lo, link):
            errors.append(f"core.loopbackPool {lo} overlaps core.linkSubnet {link}")
        nodes = (spec.get("borderGateway") or {}).get("nodes") or []
        # the border gateway takes index 0 and every cloud gateway one more; the link needs one address each
        if lo.num_addresses < 2:
            errors.append(f"core.loopbackPool {lo} is too small (at least a /31)")
        if link.num_addresses < 4:
            errors.append(f"core.linkSubnet {link} is too small (at least a /30)")
        names = [n.get("name") for n in nodes]
        if len(set(names)) != len(names):
            errors.append("borderGateway.nodes has duplicate names")
    except (ValueError, TypeError) as e:
        errors.append(f"core: {e}")
    srgb = core.get("srgb") or {"start": 16000, "end": 23999}
    lo_size = 0
    try:
        lo_size = ipaddress.ip_network(str(core.get("loopbackPool")), strict=True).num_addresses
    except ValueError:
        pass
    if lo_size and srgb["end"] - srgb["start"] + 1 < lo_size:
        errors.append(f"core.srgb {srgb['start']}-{srgb['end']} is smaller than core.loopbackPool ({lo_size} addresses)")
    pools = {k: spec.get(k) or {} for k in ("vniPool", "l2vniPool", "vpnTargetPool")}
    size = {k: (p.get("end", 0) - p.get("start", 1) + 1) for k, p in pools.items()}
    return {"errors": errors, "poolSize": size}


def check_gateway(req):
    fabric = req.get("fabric") or {}
    gw = req.get("gateway") or {}
    errors = []
    fnodes = [n.get("name") for n in (fabric.get("borderGateway") or {}).get("nodes") or []]
    under = gw.get("siteUnderlay") or {}
    addrs = under.get("addresses") or []
    gnodes = [a.get("node") for a in addrs]
    unknown = sorted(set(gnodes) - set(fnodes))
    missing = sorted(set(fnodes) - set(gnodes))
    if unknown:
        errors.append(f"siteUnderlay.addresses names nodes the fabric does not have: {', '.join(unknown)}")
    if missing:
        errors.append(f"siteUnderlay.addresses has no address for border gateway node(s): {', '.join(missing)}")
    ips = [a.get("address") for a in addrs]
    if len(set(ips)) != len(ips):
        errors.append("siteUnderlay.addresses repeats an address")
    try:
        site = ipaddress.ip_network(str(under.get("cidr")), strict=False)
    except ValueError as e:
        return {"errors": errors + [f"siteUnderlay.cidr: {e}"]}
    core = fabric.get("core") or {}
    for key in ("loopbackPool", "linkSubnet"):
        try:
            c = ipaddress.ip_network(str(core.get(key)), strict=True)
            if overlaps(site, c):
                errors.append(f"siteUnderlay.cidr {site} overlaps the fabric's core.{key} {c}")
        except ValueError:
            pass
    cloud = (gw.get("cloudRef") or {}).get("name")
    for o in req.get("others") or []:
        ospec = o.get("spec") or {}
        o = {"name": o.get("name") or (o.get("metadata") or {}).get("name"), "spec": ospec}
        if (ospec.get("cloudRef") or {}).get("name") == cloud:
            errors.append(f"site {cloud} already has CloudGateway {o.get('name')}")
        # VLANs and site subnets only have to be unique among the gateways of one fabric
        if (ospec.get("fabricRef") or {}).get("name") != (gw.get("fabricRef") or {}).get("name"):
            continue
        ou = ospec.get("siteUnderlay") or {}
        if ou.get("vlan") == under.get("vlan"):
            errors.append(f"VLAN {under.get('vlan')} is already used by CloudGateway {o.get('name')}")
        try:
            on = ipaddress.ip_network(str(ou.get("cidr")), strict=False)
            if overlaps(site, on):
                errors.append(f"siteUnderlay.cidr {site} overlaps CloudGateway {o.get('name')} ({on})")
        except ValueError:
            pass
    return {"errors": errors}


def check_placement(req):
    errors = []
    try:
        mine = nets(req.get("prefixes"))
    except ValueError as e:
        return {"errors": [f"prefixes: {e}"]}
    for i, a in enumerate(mine):
        for b in mine[i + 1:]:
            if overlaps(a, b):
                errors.append(f"prefixes {a} and {b} overlap")
    for s in req.get("siblings") or []:
        try:
            theirs = nets(s.get("prefixes"))
        except ValueError:
            continue
        for a in mine:
            for b in theirs:
                if overlaps(a, b):
                    errors.append(f"prefix {a} overlaps {b} of placement {s.get('name')}")
    for r in req.get("reserved") or []:
        try:
            rn = ipaddress.ip_network(str(r), strict=False)
        except ValueError:
            continue
        for a in mine:
            if overlaps(a, rn):
                errors.append(f"prefix {a} overlaps the site's network {rn}")
    return {"errors": errors}


def site_namespace(entity, cloudvirt, name):
    base = re.sub(r"[^a-z0-9-]", "-", f"{entity}-{cloudvirt}-{name}".lower())
    if len(base) <= 63:
        return base
    return base[:54].rstrip("-") + "-" + hashlib.sha1(base.encode()).hexdigest()[:8]


def cloudvirt_namespaces(req):
    out, errors, seen = [], [], {}
    for n in req.get("namespaces") or []:
        ns = site_namespace(req.get("entity", ""), req.get("cloudvirt", ""), n.get("name", ""))
        if ns in seen:
            errors.append(f"namespaces {seen[ns]} and {n.get('name')} map to the same site namespace {ns}")
        seen[ns] = n.get("name")
        out.append({"name": n.get("name"), "namespace": ns, "network": n.get("network") or ""})
    return {"errors": errors, "namespaces": out}


ACTIONS = {"check-fabric": check_fabric, "check-gateway": check_gateway, "check-placement": check_placement,
           "cloudvirt-namespaces": cloudvirt_namespaces}


def main():
    try:
        req = json.load(sys.stdin)
        fn = ACTIONS[req["action"]]
    except (ValueError, KeyError) as e:
        print(json.dumps({"error": f"bad request: {e}"}))
        return 2
    print(json.dumps(fn(req)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

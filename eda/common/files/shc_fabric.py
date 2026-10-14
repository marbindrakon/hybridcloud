#!/usr/bin/env python3
"""Fabric checks for the HybridFabric, CloudGateway and NetworkPlacement jobs (shc, design/P7-PLAN.md P7.3).

Reads one JSON request on stdin, prints one JSON result: {"errors": [...]} plus action-specific fields. Exit 0
unless the request itself is malformed. Pure computation: no API calls, nothing secret in or out.

actions:
  check-fabric    {spec, realization}                      core prefixes and the VPN pool (realization), node names
  check-gateway   {fabric: spec, core: realization core, gateway: spec, others: [CloudGateway objects or {name, spec}]}
                  nodes match the fabric's, one gateway per site, VLAN and site subnet unique on the fabric,
                  site subnet clear of the core prefixes
  check-placement {prefixes, siblings: [{name, prefixes}], reserved: [cidr]}
                  prefixes disjoint among themselves, from the network's other placements and from reserved
  pe-addresses    {core, index}   a provider edge's loopback (/32), core link address, IS-IS NET and node SID label
                  (index 1 is the border gateway root; the same addresses on every node)
  cloudvirt-namespaces {entity, cloudvirt, namespaces: [{name, network?}], network?}
                  the site namespace of each CloudVirt namespace: <entity>-<cloudvirt>-<name>, cut to 54 characters
                  plus a hash when longer than 63 (namespace names are global on the site's cluster), with its
                  labels; selector = all of the environment's namespaces, networkSelector = those naming network
  placement-names {namespace, name, entity, network, l3Vni}  names and labels of a placement's realized objects
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
    real = req.get("realization") or {}
    errors = []
    core = real.get("core") or {}
    try:
        lo, link = nets([core.get("loopbackPool"), core.get("linkSubnet")])
        if overlaps(lo, link):
            errors.append(f"realization core.loopbackPool {lo} overlaps core.linkSubnet {link}")
        nodes = (spec.get("borderGateway") or {}).get("nodes") or []
        # the border gateway takes index 0 and every cloud gateway one more; the link needs one address each
        if lo.num_addresses < 2:
            errors.append(f"realization core.loopbackPool {lo} is too small (at least a /31)")
        if link.num_addresses < 4:
            errors.append(f"realization core.linkSubnet {link} is too small (at least a /30)")
        names = [n.get("name") for n in nodes]
        if len(set(names)) != len(names):
            errors.append("borderGateway.nodes has duplicate names")
    except (ValueError, TypeError) as e:
        errors.append(f"realization core: {e}")
    srgb = core.get("srgb") or {"start": 16000, "end": 23999}
    lo_size = 0
    try:
        lo_size = ipaddress.ip_network(str(core.get("loopbackPool")), strict=True).num_addresses
    except ValueError:
        pass
    if lo_size and srgb["end"] - srgb["start"] + 1 < lo_size:
        errors.append(f"realization core.srgb {srgb['start']}-{srgb['end']} is smaller than core.loopbackPool ({lo_size} addresses)")
    pools = {"vniPool": spec.get("vniPool") or {}, "l2vniPool": spec.get("l2vniPool") or {},
             "vpnTargetPool": real.get("vpnTargetPool") or {}}
    size = {k: (p.get("end", 0) - p.get("start", 1) + 1) for k, p in pools.items()}
    vt = pools["vpnTargetPool"]
    for k in ("vniPool", "l2vniPool"):
        p = pools[k]
        if vt and p and not (vt.get("end", 0) < p.get("start", 0) or p.get("end", 0) < vt.get("start", 0)):
            errors.append(f"the realization's VPN route target pool {vt.get('start')}-{vt.get('end')} overlaps {k}")
    pe_max = 0
    try:
        link_size = ipaddress.ip_network(str(core.get("linkSubnet")), strict=True).num_addresses
        # host indexes 1..size-2 in both prefixes, and node SID labels within the SRGB
        pe_max = max(0, min(lo_size - 2, link_size - 2, srgb["end"] - srgb["start"]))
    except ValueError:
        pass
    return {"errors": errors, "poolSize": size, "peIndexMax": pe_max}


def pe_addresses(req):
    core, i = req.get("core") or {}, int(req["index"])
    lo = ipaddress.ip_network(str(core["loopbackPool"]), strict=True)
    link = ipaddress.ip_network(str(core["linkSubnet"]), strict=True)
    srgb = core.get("srgb") or {"start": 16000, "end": 23999}
    if not 1 <= i <= min(lo.num_addresses - 2, link.num_addresses - 2, srgb["end"] - srgb["start"]):
        return {"errors": [f"provider-edge index {i} does not fit the core prefixes or the SRGB"]}
    area = core.get("isisArea") or "49.0001"
    return {"errors": [], "index": i, "loopback": f"{lo.network_address + i}/32", "loopbackIp": str(lo.network_address + i),
            "link": f"{link.network_address + i}/{link.prefixlen}", "linkIp": str(link.network_address + i),
            "net": f"{area}.0000.0000.{i:04x}.00", "sid": srgb["start"] + i}


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
    # upstream route reflectors outside the site subnet are reached through siteUnderlay.gateway
    upstream_remote = [r for r in under.get("upstreamRouteReflectors") or []
                       if ipaddress.ip_address(r) not in site]
    core = req.get("core") or {}
    for key in ("loopbackPool", "linkSubnet"):
        try:
            c = ipaddress.ip_network(str(core.get(key)), strict=True)
            if overlaps(site, c):
                errors.append(f"siteUnderlay.cidr {site} overlaps the border gateway's core prefix {c}")
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
    return {"errors": errors, "upstreamRemote": upstream_remote}


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


def label_value(v):
    """A Kubernetes label value for v: itself when valid, else a shortened form with a hash."""
    v = str(v or "")
    if len(v) <= 63 and re.fullmatch(r"([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?", v):
        return v
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", v)[:54].strip("-_.")
    return base + "-" + hashlib.sha1(v.encode()).hexdigest()[:8]


def site_labels(entity, cloudvirt, network=""):
    """Labels of a CloudVirt's site namespaces; the placement's network selects namespaces by the same labels."""
    out = {"hybridsovereign.redhat/entity": label_value(entity),
           "hybridsovereign.redhat/cloudvirt": label_value(cloudvirt)}
    if network:
        out["hybridsovereign.redhat/hybridnetwork"] = label_value(network)
    return out


def cloudvirt_namespaces(req):
    out, errors, seen = [], [], {}
    for n in req.get("namespaces") or []:
        ns = site_namespace(req.get("entity", ""), req.get("cloudvirt", ""), n.get("name", ""))
        if ns in seen:
            errors.append(f"namespaces {seen[ns]} and {n.get('name')} map to the same site namespace {ns}")
        seen[ns] = n.get("name")
        labels = site_labels(req.get("entity", ""), req.get("cloudvirt", ""), n.get("network") or "")
        labels["hybridsovereign.redhat/cloudvirt-namespace"] = label_value(n.get("name"))
        out.append({"name": n.get("name"), "namespace": ns, "network": n.get("network") or "", "labels": labels})
    return {"errors": errors, "namespaces": out,
            "selector": site_labels(req.get("entity", ""), req.get("cloudvirt", "")),
            "networkSelector": site_labels(req.get("entity", ""), req.get("cloudvirt", ""), req.get("network") or "")
            if req.get("network") else {}}


def placement_names(req):
    """Names of a placement's realized objects. Site objects are named after the placement's L3 VNI: unique on the
    fabric and short, because OpenShift derives Linux device names (15 characters) from the network name."""
    vni, key = int(req["l3Vni"]), f"{req['namespace']}/{req['name']}"
    return {"vrf": f"t{vni}", "cudn": f"hn{vni}", "routeAdvertisement": f"hn{vni}",
            "osNetwork": f"hn{vni}", "osRouter": f"hn{vni}", "tag": f"shc-placement={key}",
            "labels": {"hybridsovereign.redhat/entity": label_value(req.get("entity", "")),
                       "hybridsovereign.redhat/networkplacement": label_value(req["name"]),
                       "hybridsovereign.redhat/hybridnetwork": label_value(req.get("network", "")),
                       "hybridsovereign.redhat/l3vni": str(vni)}}


ACTIONS = {"check-fabric": check_fabric, "check-gateway": check_gateway, "check-placement": check_placement,
           "cloudvirt-namespaces": cloudvirt_namespaces, "pe-addresses": pe_addresses,
           "placement-names": placement_names}


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

#!/usr/bin/env python3
"""Fabric checks for the HybridFabric, CloudGateway and NetworkPlacement jobs (shc, design/P7-PLAN.md P7.3).

Reads one JSON request on stdin, prints one JSON result: {"errors": [...]} plus action-specific fields. Exit 0
unless the request itself is malformed. Pure computation: no API calls, nothing secret in or out.

actions:
  check-fabric    {spec, realization}                      core prefixes and the VPN pool (realization), node names
  check-gateway   {fabric: spec, core: realization core, gateway: spec, others: [CloudGateway objects or {name, spec}]}
                  nodes match the fabric's, one gateway per site, VLAN and site subnet unique on the fabric,
                  site subnet clear of the core prefixes; transport wireguard: the peers' addresses inside the
                  site subnet and clear of the nodes' tunnel addresses
  check-placement {prefixes, siblings: [{name, prefixes}], reserved: [cidr]}
                  prefixes disjoint among themselves, from the network's other placements and from reserved
  pe-addresses    {core, index}   a provider edge's loopback (/32), core link address, IS-IS NET and node SID label
                  (index 1 is the border gateway root; the same addresses on every node)
  cloudvirt-namespaces {entity, cloudvirt, namespaces: [{name, network?}], network?}
                  the site namespace of each CloudVirt namespace: <entity>-<cloudvirt>-<name>, cut to 54 characters
                  plus a hash when longer than 63 (namespace names are global on the site's cluster), with its
                  labels; selector = all of the environment's namespaces, networkSelector = those naming network
  placement-names {namespace, name, entity, network, l3Vni}  names and labels of a placement's realized objects
  orphan-report   {crs, ledgers, sites, nodes}  the fabric orphan check's findings (see orphan_report)
  egress-plan     {egress: realization egress, nodes: [node names], networks: [{network, vpnTarget, index}],
                  domainAsn}  provider internet egress per node: the hand-off (egress VRF on the node's site network)
                  and per network a VRF e<vpnTarget>, a veth pair xe/xo into the egress VRF and one source NAT address
                  per node (the node's natPool + the network's egress index); errors for an unusable configuration
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
    eg = egress_check(real.get("egress") or {}, [n.get("name") for n in (spec.get("borderGateway") or {}).get("nodes") or []])
    errors += eg["errors"]
    size["egressPool"] = eg["poolSize"]
    return {"errors": errors, "poolSize": size, "peIndexMax": pe_max}


EGRESS_RESERVED_TABLES = {253, 254, 255}


def egress_check(eg, node_names):
    """Validate the realization's egress settings for the fabric's nodes. poolSize = networks with egress (0 when
    egress is off): the smallest node NAT pool, and no more than the link pool's /31s."""
    if not eg.get("enabled"):
        return {"errors": [], "poolSize": 0}
    errors, sizes = [], []
    for k in ("parentInterface", "gateway"):
        if not eg.get(k):
            errors.append(f"realization egress.{k} is required when egress is enabled")
    try:
        gw = ipaddress.ip_address(str(eg.get("gateway")))
    except ValueError:
        gw = None
    plen = int(eg.get("prefixLength") or 24)
    taken = []
    for name in node_names:
        n = (eg.get("nodes") or {}).get(name) or {}
        try:
            addr = ipaddress.ip_interface(f"{n.get('address')}/{plen}")
            pool = ipaddress.ip_network(str(n.get("natPool")), strict=True)
        except ValueError as e:
            errors.append(f"realization egress.nodes.{name}: address and natPool required ({e})")
            continue
        if gw is not None and gw not in addr.network:
            errors.append(f"realization egress.nodes.{name}.address {addr} is not on the gateway {gw}'s network")
        if pool.version != addr.version or not pool.subnet_of(addr.network):
            errors.append(f"realization egress.nodes.{name}.natPool {pool} is not inside {addr.network}")
        for other, o in taken:
            if overlaps(pool, o):
                errors.append(f"realization egress.nodes.{name}.natPool {pool} overlaps {other}")
        if addr.ip in pool or (gw is not None and gw in pool):
            errors.append(f"realization egress.nodes.{name}.natPool {pool} holds the node's address or the gateway")
        taken.append((f"egress.nodes.{name}.natPool", pool))
        sizes.append(pool.num_addresses)
    try:
        link = ipaddress.ip_network(str(eg.get("linkPool") or "169.254.0.0/16"), strict=True)
        sizes.append(link.num_addresses // 2)
    except ValueError as e:
        errors.append(f"realization egress.linkPool: {e}")
    vrf = eg.get("vrf") or {}
    if int(vrf.get("table") or 0) in EGRESS_RESERVED_TABLES or not vrf.get("name"):
        errors.append("realization egress.vrf needs a name and a table other than 253-255")
    return {"errors": errors, "poolSize": min(sizes) if sizes and not errors else 0}


def egress_plan(req):
    eg, node_names = req.get("egress") or {}, req.get("nodes") or []
    chk = egress_check(eg, node_names)
    if chk["errors"]:
        return {"errors": chk["errors"], "poolSize": 0, "nodes": {}, "networks": {}}
    if not eg.get("enabled"):
        return {"errors": [], "poolSize": 0, "nodes": {}, "networks": {}}
    asn = int(req.get("domainAsn") or 0)
    link = ipaddress.ip_network(str(eg.get("linkPool") or "169.254.0.0/16"), strict=True)
    base = int(eg.get("networkTableBase") or 100000)
    plen = int(eg.get("prefixLength") or 24)
    errors, nodes, networks = [], {}, {}
    for name in node_names:
        n = eg["nodes"][name]
        pool = ipaddress.ip_network(str(n["natPool"]), strict=True)
        out = []
        for w in sorted(req.get("networks") or [], key=lambda x: int(x["vpnTarget"])):
            t, i = int(w["vpnTarget"]), int(w["index"])
            if not 0 <= i < chk["poolSize"]:
                errors.append(f"{w['network']}: egress index {i} is outside the pool (0-{chk['poolSize'] - 1})")
                continue
            xo, xe = link.network_address + 2 * i, link.network_address + 2 * i + 1
            nat = str(pool.network_address + i)
            out.append({"network": w["network"], "vpnTarget": t, "vrf": f"e{t}", "table": base + t, "zone": i + 1,
                        "xe": f"xe{t}", "xo": f"xo{t}", "xeAddr": f"{xe}/31", "xoAddr": f"{xo}/31",
                        "xeIp": str(xe), "xoIp": str(xo), "nat": nat,
                        # RD type 0 (2-byte AS : 4-byte number) when the AS allows, else the loopback form at render
                        "rd": f"{asn}:{t}" if 0 < asn <= 65535 else ""})
            networks.setdefault(w["network"], {"vpnTarget": t, "index": i, "sourceAddresses": []})["sourceAddresses"].append(nat)
        nodes[name] = {"vrf": (eg.get("vrf") or {}).get("name", "egress"), "table": int((eg.get("vrf") or {}).get("table", 9000)),
                       "parent": eg["parentInterface"], "mtu": int(eg.get("mtu") or 9000),
                       "address": f"{n['address']}/{plen}", "gateway": str(eg["gateway"]),
                       "deny": [str(c) for c in eg.get("denyDestinations") or []],
                       "allow": [str(c) for c in eg.get("allowDestinations") or []],
                       "networks": out}
    return {"errors": errors, "poolSize": chk["poolSize"], "nodes": nodes, "networks": networks}


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
    if (gw.get("transport") or "none") == "wireguard":
        for i, peer in enumerate((gw.get("wireguard") or {}).get("peers") or []):
            for a in peer.get("allowedIPs") or []:
                try:
                    n = ipaddress.ip_network(str(a), strict=False)
                except ValueError as e:
                    errors.append(f"wireguard.peers[{i}].allowedIPs {a}: {e}")
                    continue
                if n.version != site.version or not n.subnet_of(site):
                    errors.append(f"wireguard.peers[{i}].allowedIPs {a} is outside siteUnderlay.cidr {site}")
                elif any(ipaddress.ip_address(x) in n for x in ips if x):
                    errors.append(f"wireguard.peers[{i}].allowedIPs {a} holds a node's tunnel address")
    elif under.get("vlan") is None:
        errors.append("siteUnderlay.vlan is required with transport none")
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
        # a gateway over a tunnel has no VLAN on the trunk
        if under.get("vlan") is not None and ou.get("vlan") == under.get("vlan"):
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


def orphan_report(req):
    """Fabric orphan check (report only): what the platform realized that no CR owns any more, and CR claims that
    the ledger does not back. Input: crs {fabrics, gateways, networks, placements, cloudvirts} (CR objects),
    ledgers {fabric: ledger}, sites [{gateway, type, cudns: [{name, l3vni}], routeAdvertisements: [...],
    namespaces: [{name, labels}], networks: [{name, sharedTo}], routers: [{name, evpnVni}]}],
    nodes [{fabric, node, netns: {name: [vrf]}}]. Returns {findings: [str], counts}."""
    crs, findings = req.get("crs") or {}, []

    def key(o):
        return f"{o['kind']}/{o['metadata']['namespace']}/{o['metadata']['name']}"

    owners = {key(o) for kind in ("fabrics", "gateways", "networks", "placements") for o in crs.get(kind) or []}
    placements = crs.get("placements") or []
    by_vni = {}
    for p in placements:
        r = (p.get("status") or {}).get("realization") or {}
        if r.get("l3Vni") is not None:
            by_vni[int(r["l3Vni"])] = p
    for fab, led in (req.get("ledgers") or {}).items():
        alloc = (led or {}).get("alloc") or {}
        for kind, entries in alloc.items():
            for owner, value in entries.items():
                if owner not in owners:
                    findings.append(f"ledger {fab}: {kind} {value} held by {owner}, which no longer exists")
        vnis = {int(v): o for o, v in (alloc.get("vni") or {}).items()}
        for v, p in by_vni.items():
            if vnis.get(v) != key(p) and ((p.get("status") or {}).get("cloudGateway")):
                gw = next((g for g in crs.get("gateways") or [] if g["metadata"]["name"] == p["status"]["cloudGateway"]), None)
                if gw and gw["spec"]["fabricRef"]["name"] == fab:
                    findings.append(f"{key(p)} claims VNI {v}, which the ledger of {fab} does not record for it")
    cvs = {(c["metadata"]["namespace"], c["metadata"]["name"]): c for c in crs.get("cloudvirts") or []}
    for site in req.get("sites") or []:
        gw, where = site.get("gateway"), f"site {site.get('site')}"
        for kind in ("cudns", "routeAdvertisements"):
            for o in site.get(kind) or []:
                p = by_vni.get(int(o.get("l3vni") or -1))
                if p is None or (p.get("status") or {}).get("cloudGateway") != gw:
                    findings.append(f"{where}: {kind[:-1]} {o['name']} (VNI {o.get('l3vni')}) has no placement")
        ns_owned = set()
        for c in cvs.values():
            for n in (c.get("status") or {}).get("namespaces") or []:
                if isinstance(n, dict) and n.get("namespace"):
                    ns_owned.add(n["namespace"])
        for n in site.get("namespaces") or []:
            if n["name"] not in ns_owned:
                findings.append(f"{where}: namespace {n['name']} (CloudVirt label {n.get('labels', {}).get('hybridsovereign.redhat/cloudvirt')}) "
                                "belongs to no CloudVirt")
        for kind in ("networks", "routers"):
            for o in site.get(kind) or []:
                m = re.fullmatch(r"hn(\d+)", o.get("name") or "")
                p = by_vni.get(int(m.group(1))) if m else None
                if p is None or (p.get("status") or {}).get("cloudGateway") != gw:
                    findings.append(f"{where}: {kind[:-1]} {o.get('name')} in the platform project has no placement")
                elif kind == "networks":
                    want = ((p.get("status") or {}).get("realization") or {}).get("site", {}).get("sharedTo")
                    extra = [t for t in o.get("sharedTo") or [] if t != want]
                    if extra:
                        findings.append(f"{where}: network {o['name']} is also shared to {', '.join(extra)}")
    gws = {g["metadata"]["name"]: g for g in crs.get("gateways") or []}
    fabs = {f["metadata"]["name"]: f for f in crs.get("fabrics") or []}
    for n in req.get("nodes") or []:
        for ns, vrfs in (n.get("netns") or {}).items():
            if ns == "root":
                eg = (((fabs.get(n.get("fabric")) or {}).get("status") or {}).get("realization") or {}).get("egress") or {}
                want = {f"e{x.get('vpnTarget')}" for x in eg.get("networks") or []}
                for v in vrfs:
                    if re.fullmatch(r"e\d+", v) and v not in want:
                        findings.append(f"node {n['node']}: egress VRF {v} belongs to no network with internet egress")
                continue
            g = gws.get(ns[3:]) if ns.startswith("gw-") else None
            if g is None:
                findings.append(f"node {n['node']}: namespace {ns} belongs to no CloudGateway")
                continue
            want = {v.get("vrf") for v in ((g.get("status") or {}).get("realization") or {}).get("vrfs") or []}
            for v in vrfs:
                if v not in want:
                    findings.append(f"node {n['node']}: VRF {v} in {ns} is not one of CloudGateway {g['metadata']['name']}'s")
    return {"errors": [], "findings": findings,
            "counts": {"placements": len(placements), "sites": len(req.get("sites") or []), "nodes": len(req.get("nodes") or [])}}


ACTIONS = {"check-fabric": check_fabric, "check-gateway": check_gateway, "check-placement": check_placement,
           "cloudvirt-namespaces": cloudvirt_namespaces, "pe-addresses": pe_addresses,
           "placement-names": placement_names, "orphan-report": orphan_report, "egress-plan": egress_plan}


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

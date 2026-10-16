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
  egress-plan     {egress: realization egress, nodes: [node names], networks: [{network, vpnTarget, index,
                  internet?, platform?}], domainAsn}  per node: the hand-off (egress VRF on the node's site network)
                  and per network a VRF e<vpnTarget> with a veth pair xe/xo into the egress VRF; internet (default
                  true): a default route and one source NAT address per node (the node's natPool + the network's
                  egress index); platform: [CIDRs of the network] the platform's sources (egress.platformSources)
                  may reach on egress.platformPorts (default 6443, 443); errors for an unusable configuration
  circuit-plan    {attachments: realization attachments, nodes: [node names], index}  an attachment circuit's link
                  subnet (the index-th of attachments.linkPool), each node's address and the CE's
  check-circuit   {accept: [prefix-list entries], placements: [{name, prefix}], reserved: [cidr], egressInternet,
                  advertiseDefault}  entries inside a placement or a reserved range are refused; notes for policy
                  that doesn't apply now; acceptsDefault
  (check-fabric also validates the realization's attachments: poolSize.attachVlanPool, poolSize.attachLinks)
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
    at = attachments_check(real.get("attachments") or {}, [n.get("name") for n in (spec.get("borderGateway") or {}).get("nodes") or []], core)
    errors += at["errors"]
    size["attachVlanPool"], size["attachLinks"] = at["vlanPool"], at["links"]
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
    sources = [str(ipaddress.ip_network(str(c), strict=True)) for c in eg.get("platformSources") or []]
    ports = [int(x) for x in eg.get("platformPorts") or [6443, 443]]
    seen = []
    for w in req.get("networks") or []:
        for c in w.get("platform") or []:
            try:
                net = ipaddress.ip_network(str(c), strict=True)
            except ValueError as e:
                errors.append(f"{w['network']}: platform prefix {c}: {e}")
                continue
            if not sources:
                errors.append(f"{w['network']}: platform reach needs realization egress.platformSources")
            for other, o in seen:
                if net.overlaps(o):
                    errors.append(f"{w['network']}: platform prefix {net} overlaps {other}'s {o}")
            for src in sources:
                if net.overlaps(ipaddress.ip_network(src)):
                    errors.append(f"{w['network']}: platform prefix {net} overlaps the platform's sources {src}")
            seen.append((w["network"], net))
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
            internet = bool(w.get("internet", True))
            nat = str(pool.network_address + i) if internet else ""
            platform = [str(ipaddress.ip_network(str(c), strict=True)) for c in w.get("platform") or []]
            out.append({"network": w["network"], "vpnTarget": t, "vrf": f"e{t}", "table": base + t, "zone": i + 1,
                        "xe": f"xe{t}", "xo": f"xo{t}", "xeAddr": f"{xe}/31", "xoAddr": f"{xo}/31",
                        "xeIp": str(xe), "xoIp": str(xo), "nat": nat, "internet": internet, "platform": platform,
                        # RD type 0 (2-byte AS : 4-byte number) when the AS allows, else the loopback form at render
                        "rd": f"{asn}:{t}" if 0 < asn <= 65535 else ""})
            entry = networks.setdefault(w["network"], {"vpnTarget": t, "index": i, "sourceAddresses": [],
                                                       "internet": internet, "platform": platform})
            if nat:
                entry["sourceAddresses"].append(nat)
        nodes[name] = {"vrf": (eg.get("vrf") or {}).get("name", "egress"), "table": int((eg.get("vrf") or {}).get("table", 9000)),
                       "parent": eg["parentInterface"], "mtu": int(eg.get("mtu") or 9000),
                       "address": f"{n['address']}/{plen}", "gateway": str(eg["gateway"]),
                       "deny": [str(c) for c in eg.get("denyDestinations") or []],
                       "allow": [str(c) for c in eg.get("allowDestinations") or []],
                       "platformSources": sources, "platformPorts": ports,
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


def attachments_check(at, node_names, core=None):
    """Validate the realization's attachments settings (attachment circuits on provider edges). vlanPool = the
    bearer VLANs ([1, 0] when attachments are off), links = /linkPrefixLength subnets in linkPool (0 when off)."""
    if not at.get("enabled"):
        return {"errors": [], "vlanPool": [1, 0], "links": 0}
    errors = []
    vp = at.get("vlanPool") or {}
    try:
        vs, ve = int(vp.get("start")), int(vp.get("end"))
        if not 2 <= vs <= ve <= 4094:
            errors.append(f"realization attachments.vlanPool {vs}-{ve} must lie within 2-4094")
    except (TypeError, ValueError):
        errors.append("realization attachments.vlanPool needs start and end")
        vs, ve = 1, 0
    links = 0
    try:
        pool = ipaddress.ip_network(str(at.get("linkPool")), strict=True)
        plen = int(at.get("linkPrefixLength") or 29)
        # every node, the CE and a second CE router need an address on the link
        if not pool.prefixlen <= plen <= 30 or (2 ** (32 - plen) - 2) < len(node_names) + 1:
            errors.append(f"realization attachments.linkPrefixLength /{plen} must fit in {pool} and hold the "
                          f"{len(node_names)} nodes plus the CE")
        else:
            links = 2 ** (plen - pool.prefixlen)
        for k in ("loopbackPool", "linkSubnet"):
            try:
                c = ipaddress.ip_network(str((core or {}).get(k)), strict=True)
                if overlaps(pool, c):
                    errors.append(f"realization attachments.linkPool {pool} overlaps core.{k} {c}")
            except ValueError:
                pass
    except ValueError as e:
        errors.append(f"realization attachments.linkPool: {e}")
    for pe, p in (at.get("providerEdges") or {}).items():
        for b, bearer in ((p or {}).get("bearers") or {}).items():
            if not (bearer or {}).get("interface"):
                errors.append(f"realization attachments.providerEdges.{pe}.bearers.{b}.interface is required")
            mtu = int((bearer or {}).get("mtu") or 1500)
            if not 576 <= mtu <= 9216:
                errors.append(f"realization attachments.providerEdges.{pe}.bearers.{b}.mtu {mtu} is out of range")
    return {"errors": errors, "vlanPool": [vs, ve] if not errors else [1, 0], "links": links if not errors else 0}


def circuit_plan(req):
    """An attachment circuit's link: the index-th /linkPrefixLength of linkPool; node i (in the given order) takes
    host address i+1, the CE the next one, a second CE router the one after."""
    at, names, index = req.get("attachments") or {}, req.get("nodes") or [], int(req.get("index"))
    pool = ipaddress.ip_network(str(at.get("linkPool")), strict=True)
    plen = int(at.get("linkPrefixLength") or 29)
    count = 2 ** (plen - pool.prefixlen) if pool.prefixlen <= plen else 0
    if not 0 <= index < count:
        return {"errors": [f"link index {index} is outside {pool} (/{plen}: {count} links)"]}
    link = ipaddress.ip_network(f"{pool.network_address + index * 2 ** (32 - plen)}/{plen}")
    hosts = list(link.hosts())
    if len(hosts) < len(names) + 2:
        return {"errors": [f"link {link} has too few addresses for {len(names)} nodes and the CE"]}
    return {"errors": [], "link": str(link), "prefixLength": plen,
            "nodes": [{"name": n, "address": str(hosts[i])} for i, n in enumerate(names)],
            "ce": str(hosts[len(names)]), "ceSecond": str(hosts[len(names) + 1])}


ACCEPT_RE = re.compile(r"^(\S+)(?: ge (\d+))?(?: le (\d+))?$")


def check_circuit(req):
    """A circuit's accept list against the network's placement prefixes and the platform's ranges (reserved).
    An entry inside a placement prefix or a reserved range is refused; an entry that covers one only gets a note
    (the edge drops overlapping routes regardless). acceptsDefault: an entry matches 0.0.0.0/0."""
    errors, notes, accepts_default = [], [], False
    placements = []
    for p in req.get("placements") or []:
        try:
            placements.append((p.get("name"), ipaddress.ip_network(str(p.get("prefix")), strict=True)))
        except ValueError:
            continue
    reserved = []
    for r in req.get("reserved") or []:
        try:
            reserved.append(ipaddress.ip_network(str(r), strict=False))
        except ValueError:
            continue
    for e in req.get("accept") or []:
        m = ACCEPT_RE.match(str(e).strip())
        try:
            net = ipaddress.ip_network(m.group(1), strict=True) if m else None
        except ValueError:
            net = None
        if net is None or net.version != 4:
            errors.append(f"accept entry '{e}' is not '<IPv4 prefix>[ ge <n>][ le <n>]'")
            continue
        ge = int(m.group(2)) if m.group(2) else None
        if net.prefixlen == 0 and ge is None:
            accepts_default = True
        for name, q in placements:
            if net.subnet_of(q):
                errors.append(f"accept entry '{e}' lies inside {q} of placement {name}")
            elif q.subnet_of(net):
                notes.append(f"routes overlapping {q} (placement {name}) are never accepted from the CE")
        for q in reserved:
            if net.subnet_of(q):
                errors.append(f"accept entry '{e}' lies inside the platform range {q}")
            elif q.subnet_of(net) and net.prefixlen > 0:
                notes.append(f"routes overlapping the platform range {q} are never accepted from the CE")
    egress = bool(req.get("egressInternet"))
    if accepts_default and egress:
        notes.append("0.0.0.0/0 is not accepted from the CE while the network has provider internet egress")
    if req.get("advertiseDefault") and not egress:
        notes.append("no default route is advertised: the network has no provider internet egress")
    return {"errors": errors, "notes": sorted(set(notes)), "acceptsDefault": accepts_default}


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
           "placement-names": placement_names, "orphan-report": orphan_report, "egress-plan": egress_plan,
           "circuit-plan": circuit_plan, "check-circuit": check_circuit}


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

#!/usr/bin/env python3
"""Fabric checks for the HybridFabric, CloudGateway and NetworkPlacement jobs (shc, design/P7-PLAN.md P7.3).

Reads one JSON request on stdin, prints one JSON result: {"errors": [...]} plus action-specific fields. Exit 0
unless the request itself is malformed. Pure computation: no API calls, nothing secret in or out.

actions:
  check-fabric    {spec, realization}                      core prefixes and the VPN pool (realization), node names
  check-gateway   {fabric: spec, core: realization core, attachments: realization attachments, gateway: spec,
                  others: [CloudGateway objects or {name, spec}]}
                  nodes match the fabric's, one gateway per site, VLAN and site subnet unique on the fabric and the
                  VLAN outside the attachment circuits' pool,
                  site subnet clear of the core prefixes; transport wireguard: the peers' addresses inside the
                  site subnet and clear of the nodes' tunnel addresses
  check-network   {prefixes, reserved: [cidr], circuits: [{name, accept}]}   a HybridNetwork's address space: no
                  prefix wholly inside a platform range (reserved; the IPAM steps around the ones inside a prefix),
                  none holding an accept entry of the network's attachment circuits; reserved / circuits: the errors
                  of each kind
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
  check-circuit   {accept: [prefix-list entries], space: [the network's prefixes], reserved: [cidr], egressInternet,
                  advertiseDefault}  entries inside the network's address space or a reserved range are refused;
                  notes for policy that doesn't apply now; acceptsDefault
  (check-fabric also validates the realization's attachments: poolSize.attachVlanPool, poolSize.attachLinks)
  attachments-plan {fabric, attachments: realization attachments, nodes: [node names], tableBase, domainAsn,
                  circuits, networks, placements: [CR objects], ledger: alloc, egress: egress-plan networks,
                  egressNodes: egress-plan nodes, reserved: [cidr], platformSources: [cidr], passwords: [circuits
                  (namespace/name) whose BGP password the job holds]}  the circuits the fabric's nodes realize: per circuit its VRF (the
                  network's root VRF e<vpnTarget>), VLAN device, conntrack zone (the network's egress zone, else its
                  VPN target) and routing policy (accept list; deny = the network's address space (spec.prefixes and
                  status.addressSpace) and its placements' prefixes (status.prefixes), the reserved ranges and
                  0.0.0.0/0 while the network wants internet egress; advertise = the placement prefixes and 0.0.0.0/0
                  when asked for and the network has internet egress; IPv4 only); per node the root's attachments
                  (root.json); circuits it can't realize under skipped; vrfs: per node the networks with a root
                  VRF (egress entry and/or circuits), for the node's FRR configuration
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
    else:
        # the trunk also carries the attachment circuits' VLANs (realization attachments.vlanPool)
        at = req.get("attachments") or {}
        vp = at.get("vlanPool") or {}
        if at.get("enabled") and int(vp.get("start", 1)) <= int(under["vlan"]) <= int(vp.get("end", 0)):
            errors.append(f"siteUnderlay.vlan {under['vlan']} is in the platform's attachment circuit VLAN range")
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


def check_network(req):
    """A network's address space against the platform's ranges and its circuits' accept entries."""
    try:
        mine = nets(req.get("prefixes"))
    except ValueError as e:
        return {"errors": [f"prefixes: {e}"], "reserved": [f"prefixes: {e}"], "circuits": []}
    reserved, circuits = [], []
    for r in req.get("reserved") or []:
        try:
            rn = ipaddress.ip_network(str(r), strict=False)
        except ValueError:
            continue
        # a platform range inside a prefix is only never allocated; a prefix inside one has nothing to allocate
        for a in mine:
            if a.version == rn.version and a.subnet_of(rn):
                reserved.append(f"prefix {a} lies inside the platform range {rn}")
    # the circuits accept routes from their CE; the network's space never holds one (as check-circuit refuses)
    for c in req.get("circuits") or []:
        for e in c.get("accept") or []:
            entry = accept_entry(e)
            for a in mine:
                if entry and entry[0].version == a.version and entry[0].subnet_of(a):
                    circuits.append(f"prefix {a} holds accept entry '{e}' of AttachmentCircuit {c.get('name')}")
    return {"errors": reserved + circuits, "reserved": reserved, "circuits": circuits}


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
    """A circuit's accept list against the network's address space and the platform's ranges (reserved). An entry
    inside the space or a reserved range is refused; an entry that covers part of one only gets a note (the edge
    drops overlapping routes regardless). acceptsDefault: an entry matches 0.0.0.0/0."""
    errors, notes, accepts_default = [], [], False
    space = []
    for p in req.get("space") or []:
        try:
            n = ipaddress.ip_network(str(p), strict=True)
        except ValueError:
            continue
        if n.version == 4 and n not in space:
            space.append(n)
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
        for q in space:
            if net.subnet_of(q):
                errors.append(f"accept entry '{e}' lies inside the network's address space {q}")
            elif q.subnet_of(net):
                notes.append(f"routes overlapping the network's address space {q} are never accepted from the CE")
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


def accept_entry(e):
    """A prefix-list entry "<prefix>[ ge <n>][ le <n>]" as (network, ge, le), or None when malformed."""
    m = ACCEPT_RE.match(str(e).strip())
    try:
        net = ipaddress.ip_network(m.group(1), strict=True) if m else None
    except ValueError:
        return None
    if net is None or net.version != 4:
        return None
    ge, le = (int(m.group(2)) if m.group(2) else None), (int(m.group(3)) if m.group(3) else None)
    if (ge is not None and not net.prefixlen < ge <= 32) or (le is not None and not net.prefixlen < le <= 32) \
            or (ge is not None and le is not None and ge > le):
        return None
    return net, ge, le


def collapse(networks):
    """The IPv4 networks without those inside another one (duplicates once)."""
    v4 = sorted({n for n in networks if n.version == 4}, key=lambda n: n.prefixlen)
    out = []
    for n in v4:
        if not any(n.subnet_of(o) for o in out):
            out.append(n)
    return out


def entry_text(net, ge=None, le=None):
    return str(net) + (f" ge {ge}" if ge is not None else "") + (f" le {le}" if le is not None else "")


def attachments_plan(req):
    """Which attachment circuits the fabric's nodes realize, and how (see the module docstring)."""
    at = req.get("attachments") or {}
    node_names = req.get("nodes") or []
    fabric = req.get("fabric")
    base = int(req.get("tableBase") or 100000)
    alloc = req.get("ledger") or {}
    egress = req.get("egress") or {}
    errors, skipped, circuits, status = [], [], [], []
    nodes = {n: {"circuits": []} for n in node_names}
    if not at.get("enabled"):
        return {"errors": [], "circuits": [], "skipped": [], "nodes": {}, "status": [],
                "vrfs": root_vrfs(req, [])}
    vp = at.get("vlanPool") or {}
    pool = ipaddress.ip_network(str(at.get("linkPool")), strict=True)
    reserved = []
    for r in req.get("reserved") or []:
        try:
            reserved.append(ipaddress.ip_network(str(r), strict=True))
        except ValueError as e:
            errors.append(f"reserved range {r}: {e}")
    sources = [str(ipaddress.ip_network(str(c), strict=True)) for c in req.get("platformSources") or []]
    nets_by_key, space = {}, {}
    for n in req.get("networks") or []:
        md = n.get("metadata") or {}
        k = f"{md.get('namespace')}/{md.get('name')}"
        nets_by_key[k] = n
        # the space as asked for and as the IPAM holds it (they differ while an edit is refused)
        for c in ((n.get("spec") or {}).get("prefixes") or []) + [
                a.get("prefix") for a in (n.get("status") or {}).get("addressSpace") or []]:
            try:
                space.setdefault(k, []).append(ipaddress.ip_network(str(c), strict=True))
            except ValueError:
                continue
    prefixes = {}
    for p in req.get("placements") or []:
        md, sp = p.get("metadata") or {}, p.get("spec") or {}
        if md.get("deletionTimestamp"):
            continue
        for c in (p.get("status") or {}).get("prefixes") or []:
            try:
                prefixes.setdefault(f"{md.get('namespace')}/{sp.get('network')}", []).append(
                    ipaddress.ip_network(str(c), strict=True))
            except ValueError:
                continue
    parents, vlans, passwords = set(), {}, set(req.get("passwords") or [])
    for c in sorted(req.get("circuits") or [], key=lambda o: (o["metadata"]["namespace"], o["metadata"]["name"])):
        md, sp, st = c.get("metadata") or {}, c.get("spec") or {}, c.get("status") or {}
        r = st.get("realization") or {}
        if st.get("fabric") != fabric or not r.get("vlan"):
            continue
        key = f"{md['namespace']}/{md['name']}"
        owner = f"AttachmentCircuit/{key}"

        def skip(why):
            skipped.append({"circuit": key, "reason": why})

        if md.get("deletionTimestamp") or r.get("release"):
            skip("being deleted")
            continue
        vlan, idx, t = int(r["vlan"]), int(r.get("linkIndex", -1)), int(r.get("vpnTarget") or 0)
        if (alloc.get("attachVlan") or {}).get(owner) != vlan or (alloc.get("attachLink") or {}).get(owner) != idx:
            skip(f"the fabric's ledger does not hold VLAN {vlan} and link {idx} for it")
            continue
        net_key = f"{md['namespace']}/{sp.get('network')}"
        if (alloc.get("vpnTarget") or {}).get(f"HybridNetwork/{net_key}") != t or net_key not in nets_by_key:
            skip(f"network {net_key} does not hold VPN target {t}")
            continue
        if not int(vp.get("start", 1)) <= vlan <= int(vp.get("end", 0)):
            skip(f"VLAN {vlan} is outside the pool {vp.get('start')}-{vp.get('end')}")
            continue
        if vlan in vlans:
            skip(f"VLAN {vlan} is also {vlans[vlan]}'s")
            continue
        link = ipaddress.ip_network(str(r.get("link")), strict=True)
        parent = str(r.get("interface") or "")
        device = f"{parent}.{vlan}"
        if not parent or len(device) > 15 or not link.subnet_of(pool):
            skip(f"no usable interface ({device}) or link {link} outside {pool}")
            continue
        accept, bad = [], []
        for e in r.get("accept") or []:
            a = accept_entry(e)
            (accept.append(entry_text(*a)) if a else bad.append(str(e)))
        if bad:
            skip(f"malformed accept entries: {', '.join(bad)}")
            continue
        if (r.get("authentication") or "md5") == "md5" and key not in passwords:
            skip("no usable BGP password in the platform's secret store")
            continue
        vlans[vlan] = key
        parents.add(parent)
        net = nets_by_key[net_key]
        internet_intent = bool(((net.get("spec") or {}).get("egress") or {}).get("internet"))
        eg = egress.get(net_key) or {}
        internet = bool(eg) and bool(eg.get("internet", True))
        zone = int(eg["index"]) + 1 if eg else t
        mine = sorted({n for n in prefixes.get(net_key, []) if n.version == 4},
                      key=lambda n: (int(n.network_address), n.prefixlen))
        own = sorted(collapse(space.get(net_key, []) + mine), key=lambda n: (int(n.network_address), n.prefixlen))
        deny = [entry_text(n, le=32) if n.prefixlen < 32 else str(n) for n in own + reserved]
        if internet_intent:
            deny.append("0.0.0.0/0")
        nat = bool(r.get("advertiseDefault")) and internet
        advertise = ([entry_text(n, le=32) if n.prefixlen < 32 else str(n) for n in mine]
                     if r.get("advertiseNetworkPrefixes", True) else []) + (["0.0.0.0/0"] if nat else [])
        addrs = {n.get("name"): n.get("address") for n in r.get("nodes") or []}
        plan = {"circuit": key, "network": net_key, "id": f"AC{vlan}", "vrf": f"e{t}", "table": base + t,
                "vpnTarget": t, "zone": zone, "vlan": vlan, "device": device, "parent": parent,
                "mtu": int(r.get("mtu") or 1500), "link": str(link), "ce": str(r.get("ce")),
                "peerAsn": int(r["peerAsn"]), "authentication": r.get("authentication") or "md5",
                "passwordPath": r.get("passwordPath") or "", "maxPrefixes": int(r.get("maxPrefixes") or 100),
                "maxPrefixAction": r.get("maxPrefixAction") or "teardown", "bfd": bool(r.get("bfd")),
                "accept": accept, "deny": deny, "advertise": advertise, "denyDefault": internet_intent,
                "advertiseDefault": nat, "nat": nat, "rev": str(r.get("rev") or ""),
                "nodes": {n: addrs[n] for n in node_names if addrs.get(n)}}
        policy = hashlib.sha1(json.dumps([deny, accept, advertise, nat], sort_keys=True).encode()).hexdigest()[:12]
        circuits.append(plan)
        status.append({"circuit": key, "network": net_key, "rev": plan["rev"], "policy": policy, "vlan": vlan,
                       "vrf": plan["vrf"], "device": device, "link": str(link),
                       "nodes": sorted(plan["nodes"].keys())})
        for n, a in plan["nodes"].items():
            nodes[n]["circuits"].append({"circuit": key, "device": device, "parent": parent, "vlan": vlan,
                                         "mtu": plan["mtu"], "vrf": plan["vrf"], "table": plan["table"],
                                         "zone": zone, "address": f"{a}/{link.prefixlen}", "link": str(link),
                                         "nat": nat})
    for n in nodes.values():
        n.update({"vlanPool": [int(vp.get("start", 1)), int(vp.get("end", 0))],
                  "parents": sorted(parents | {str(b.get("interface")) for p in (at.get("providerEdges") or {}).values()
                                               for b in ((p or {}).get("bearers") or {}).values()
                                               if (b or {}).get("interface")}),
                  "platformSources": sources})
    return {"errors": errors, "circuits": circuits, "skipped": skipped, "nodes": nodes, "status": status,
            "vrfs": root_vrfs(req, circuits)}


def root_vrfs(req, circuits):
    """Per node, the networks with a root VRF in vpnTarget order: the node's egress entry (egress-plan nodes) for a
    network with egress, plus the network's circuits on the node (a circuit-only network gets a bare entry)."""
    asn = int(req.get("domainAsn") or 0)
    out = {}
    for name in req.get("nodes") or []:
        by_vrf = {}
        for e in ((req.get("egressNodes") or {}).get(name) or {}).get("networks") or []:
            by_vrf[e["vrf"]] = dict(e, egress=True, circuits=[])
        for c in circuits:
            if name not in c["nodes"]:
                continue
            v = by_vrf.setdefault(c["vrf"], {"network": c["network"], "vpnTarget": c["vpnTarget"], "vrf": c["vrf"],
                                             "table": c["table"], "nat": "", "platform": [], "egress": False,
                                             "rd": f"{asn}:{c['vpnTarget']}" if 0 < asn <= 65535 else "",
                                             "circuits": []})
            v["circuits"].append(dict({k: x for k, x in c.items() if k != "nodes"}, address=c["nodes"][name]))
        out[name] = sorted(by_vrf.values(), key=lambda v: int(v["vpnTarget"]))
    return out


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
    the ledger does not back. Input: crs {fabrics, gateways, networks, placements, cloudvirts, circuits} (CR objects),
    ledgers {fabric: ledger} (with its IPAM section), sites [{gateway, type, cudns: [{name, l3vni}], routeAdvertisements: [...],
    namespaces: [{name, labels}], networks: [{name, sharedTo}], routers: [{name, evpnVni}]}],
    nodes [{fabric, node, netns: {name: [vrf]}, rootDevices: [{name, master}] (the root's VLAN devices in root VRFs
    or on a bearer interface within the attachment VLAN pool), rootBfd: [{peer, vrf, interface, status}] (the root
    bfdd's sessions)}]. Returns {findings: [str], counts}."""
    crs, findings = req.get("crs") or {}, []

    def key(o):
        return f"{o['kind']}/{o['metadata']['namespace']}/{o['metadata']['name']}"

    owners = {key(o) for kind in ("fabrics", "gateways", "networks", "placements", "circuits") for o in crs.get(kind) or []}
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
        for network, state in ((led or {}).get("ipam") or {}).items():
            if network not in owners:
                findings.append(f"ledger {fab}: IPAM address space of {network}, which no longer exists")
            for owner, cidrs in ((state or {}).get("alloc") or {}).items():
                if owner not in owners:
                    findings.append(f"ledger {fab}: IPAM {', '.join(cidrs)} of {network} held by {owner}, "
                                    "which no longer exists")
            for owner, cidrs in ((state or {}).get("external") or {}).items():
                if owner not in owners:
                    findings.append(f"ledger {fab}: IPAM provider-assigned {', '.join(cidrs)} of {network} recorded "
                                    f"for {owner}, which no longer exists")
        # platform pools: a block's owner must exist and its space must list the block as a platform block
        for pool, ps in ((led or {}).get("ipamPools") or {}).items():
            for owner, blocks in ((ps or {}).get("alloc") or {}).items():
                if owner not in owners:
                    findings.append(f"ledger {fab}: pool {pool} block {', '.join(blocks)} held by {owner}, "
                                    "which no longer exists")
                    continue
                plat = (((led or {}).get("ipam") or {}).get(owner) or {}).get("platform") or []
                missing = [b for b in blocks if b not in plat]
                if missing and owner in ((led or {}).get("ipam") or {}):
                    findings.append(f"ledger {fab}: pool {pool} block {', '.join(missing)} of {owner} is not in "
                                    "its address space")
        vnis = {int(v): o for o, v in (alloc.get("vni") or {}).items()}
        for v, p in by_vni.items():
            if vnis.get(v) != key(p) and ((p.get("status") or {}).get("cloudGateway")):
                gw = next((g for g in crs.get("gateways") or [] if g["metadata"]["name"] == p["status"]["cloudGateway"]), None)
                if gw and gw["spec"]["fabricRef"]["name"] == fab:
                    findings.append(f"{key(p)} claims VNI {v}, which the ledger of {fab} does not record for it")
    # a placement's subnets are the IPAM's: the ledger of its network's fabric holds them for it
    net_fabric = {f"HybridNetwork/{n['metadata']['namespace']}/{n['metadata']['name']}": (n.get("status") or {}).get("fabric")
                  for n in crs.get("networks") or []}
    for p in placements:
        mine = (p.get("status") or {}).get("prefixes") or []
        network = f"HybridNetwork/{p['metadata']['namespace']}/{(p.get('spec') or {}).get('network')}"
        led = (req.get("ledgers") or {}).get(net_fabric.get(network) or "")
        if not mine or led is None:
            continue
        nstate = (led.get("ipam") or {}).get(network) or {}
        held = ((nstate.get("alloc") or {}).get(key(p)) or []) + ((nstate.get("external") or {}).get(key(p)) or [])
        if sorted(held) != sorted(mine):
            findings.append(f"{key(p)} claims {', '.join(mine)}, which the IPAM of {net_fabric[network]} holds as "
                            f"{', '.join(held) or 'nothing'} for it")
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
                real = ((fabs.get(n.get("fabric")) or {}).get("status") or {}).get("realization") or {}
                want = {f"e{x.get('vpnTarget')}" for x in (real.get("egress") or {}).get("networks") or []}
                att = [a for a in real.get("attachments") or [] if n["node"] in (a.get("nodes") or [])]
                want |= {a.get("vrf") for a in att}
                for v in vrfs:
                    if re.fullmatch(r"e\d+", v) and v not in want:
                        findings.append(f"node {n['node']}: root VRF {v} belongs to no network with internet egress "
                                        "or attachment circuit")
                devs = {a.get("device") for a in att}
                for d in n.get("rootDevices") or []:
                    if d.get("name") not in devs:
                        findings.append(f"node {n['node']}: circuit device {d.get('name')} (VRF {d.get('master') or 'none'}) "
                                        "belongs to no attachment circuit")
                # a BFD session in a VRF the kernel no longer has outlived its circuit (bgpd only deregisters it while
                # the VRF exists); it stays in bfdd until bfdd restarts
                for b in n.get("rootBfd") or []:
                    if (b.get("vrf") or "default") != "default" and b.get("vrf") not in vrfs:
                        findings.append(f"node {n['node']}: BFD session to {b.get('peer')} in VRF {b.get('vrf')} "
                                        f"({b.get('interface') or 'no interface'}, {b.get('status') or 'unknown'}) "
                                        "outlives its VRF")
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


ACTIONS = {"check-fabric": check_fabric, "check-gateway": check_gateway, "check-network": check_network,
           "cloudvirt-namespaces": cloudvirt_namespaces, "pe-addresses": pe_addresses,
           "placement-names": placement_names, "orphan-report": orphan_report, "egress-plan": egress_plan,
           "circuit-plan": circuit_plan, "check-circuit": check_circuit, "attachments-plan": attachments_plan}


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
